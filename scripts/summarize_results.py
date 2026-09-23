"""Build compact, reviewable reports from completed forecasts; no new forecasts."""
from pathlib import Path
import json

import pandas as pd

from windforecast.common import file_hash, save_json, source_version
from windforecast.evaluation import score_rows


def table(frame, columns):
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(f"{row[c]:.6f}" if isinstance(row[c], float) else str(row[c]) for c in columns) + " |")
    return "\n".join(lines)


def main():
    root = Path.cwd()
    reports = root / "reports"
    sections = ["# Фактически выполненные расчёты", "", "Исходные данные и параметры: `config.example.json`. Все численные метрики ниже — нормализованная мощность, не МВт. Февральской точности здесь нет.", ""]
    for directory, heading in [
        ("validation_observations", "13 еженедельных выпусков 01.11.2025–24.01.2026, покрытие 5/6"),
        ("validation_coverage6", "Те же 13 выпусков, покрытие 6/6"),
        ("validation_weather", "7 ежедневных выпусков 22–28.01.2026, покрытие 5/6"),
        ("holdout", "Последний исторический выпуск 29.01.2026"),
    ]:
        frame = pd.read_csv(reports / directory / "metrics.csv")
        subset = frame[frame.horizon == "all"]
        sections += [f"## {heading}", "", table(subset, ["model", "entity", "n", "mae", "rmse", "bias", "coverage"]), "",
                     f"[Все горизонты и нормированные метрики]({directory}/metrics.csv); [прогнозы и факты]({directory}/predictions.csv).", ""]
    # Keep model choice for a truly separate target period chronological.
    # Jan 22..26 validation labels end Jan 28 23:00 local. Jan 29 issue targets
    # Jan 30..31. No target overlap, and selection is knowable before issue.
    rows = pd.read_csv(reports / "validation_weather" / "predictions.csv")
    issues = pd.to_datetime(rows.issue_time, utc=True)
    early = rows.loc[issues <= pd.Timestamp("2026-01-26T18:00:00Z")].copy()
    scores = score_rows(early, 1.0)
    ranking = scores[(scores.entity == "farm_proxy") & (scores.horizon == "all")].sort_values("mae")
    winner = ranking.iloc[0].model
    holdout = pd.read_csv(reports / "holdout" / "predictions.csv")
    known_at = pd.to_datetime(early.target_time, utc=True).max() + pd.Timedelta(hours=1)
    holdout_issue = pd.to_datetime(holdout.issue_time, utc=True).min()
    assert known_at <= holdout_issue
    assert not set(early.target_time).intersection(holdout.target_time)
    path = reports / "pre_holdout_selection"
    path.mkdir(exist_ok=True)
    scores.to_csv(path / "metrics.csv", index=False)
    save_json(path / "selection.json", {"selected_model": winner, "validation_issues": "2026-01-22..2026-01-26",
              "known_at": known_at, "holdout_issue": holdout_issue, "disjoint_target_times": True,
              "note": "Chronological subset of the already saved folds; production selection is unchanged."})
    selected_test = pd.read_csv(reports / "holdout" / "metrics.csv")
    selected_test = selected_test[(selected_test.model == winner) & (selected_test.entity == "farm_proxy") & (selected_test.horizon == "all")].iloc[0]
    sections += ["## Разделение выбора и контрольной оценки", "",
                 "Семь январских выпусков служат выбору модели для февральского replay. Их целевые часы частично пересекаются с выпуском 29 января, поэтому это сравнение само по себе не является независимым тестом.", "",
                 f"Для отдельной хронологической проверки использованы только первые пять выпусков 22–26 января. Выбор по MAE индекса: **{winner}**. Все их цели известны к **{known_at.isoformat()}**, раньше контрольного выпуска **{holdout_issue.isoformat()}**. Целевые часы этих групп не пересекаются. Модели и параметры не менялись. MAE выбранной модели на контрольном выпуске: **{selected_test.mae:.6f}**, RMSE **{selected_test.rmse:.6f}**. Это один 48-часовой выпуск, поэтому вывод ограничен.", "",
                 "[Отдельный выбор по пяти выпускам](pre_holdout_selection/selection.json). Он не заменяет производственный выбор по семи выпускам, доступный до 31 января.", "",
                 "Покрытие целей во всех показанных срезах — 100%; пропуски исходного ряда влияют на обучение и лаги. Порог 6/6 меняет состав обучения и ухудшает MAE ExtraTrees индекса на 0,001322 (около 0,47%). Нельзя переносить этот результат на любые сезоны.", ""]
    replay = json.loads((root / "artifacts" / "replay.json").read_text(encoding="utf-8"))
    assert len(replay) == 29 and all(row["status"] == "ok" for row in replay)
    all_forecasts, manifest_summaries = [], []
    for record in replay:
        directory = Path(record["directory"])
        frame = pd.read_csv(directory / "forecast.csv")
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["source_version"] == source_version()
        assert manifest["checks"]["status"] == "passed" and len(frame) == 48
        issue = pd.to_datetime(frame.issue_time, utc=True)
        target = pd.to_datetime(frame.target_time, utc=True)
        assert ((target - issue).dt.total_seconds() / 3600 == frame.horizon).all()
        for turbine in ("turbine_1", "turbine_2"):
            assert (pd.to_datetime(frame[f"weather_available_at_{turbine}"], utc=True) <= issue).all()
        all_forecasts.append(frame)
        manifest_summaries.append({"issue": record["issue"], "run_id": manifest["run_id"], "model": manifest["model"],
                                   "source_version": manifest["source_version"], "checks": manifest["checks"],
                                   "stale_hours": manifest["stale_hours"], "manifest_sha256": file_hash(directory / "manifest.json"),
                                   "forecast_sha256": file_hash(directory / "forecast.csv")})
    combined = pd.concat(all_forecasts, ignore_index=True)
    assert len(combined) == 1392 and not combined.duplicated(["issue_time", "target_time"]).any()
    combined.to_csv(reports / "february_replay.csv", index=False)
    save_json(reports / "replay_summary.json", manifest_summaries)
    sections += ["## Конкурсный replay и воспроизводимость", "",
                 "Выполнены **29 выпусков** 31 января–28 февраля, каждый на 48 часов: **1392 строки**. Все использованные погодные выпуски и времена доступности не позже соответствующего issue. Все проверки диапазона, чисел и часов прошли. Входных фактов за февраль не добавлялось; последняя исходная почасовая цель остаётся январской. Последние выпуски выходят за февраль согласно полному горизонту.", "",
                 "[Все прогнозы](february_replay.csv), [краткие манифесты](replay_summary.json). Полные снимки и модели находятся в `artifacts/runs/`, исходный погодный кэш — в `data/weather/` (локальные каталоги, исключенные из Git).", "",
                 "Этот сводный отчёт не запускает тесты и не подтверждает новое прохождение проверок. Фактический протокол подготовки к сдаче: [submission/checks.md](submission/checks.md). Для нового окружения отдельно выполните `python -m pytest -q`. Проверки воспроизводимости снимков описаны отдельно от набора тестов.", "",
                 "ExtraTrees при покрытиях 5/6 и 6/6 повторно обучены из сохранённых снимков: максимальная разница 0.0. Погодный прогноз 31 января также воспроизведён с разницей 0.0. Дополнительные проверки последнего выпуска и повторного запуска сохранены в `reproducibility.json`.", "",
                 "Ограничения: неизвестные часовой пояс регистратора, семантика интервала и номинальные мощности; сетка GFS 1°/ветер 10 м; короткая погодная валидация; отсутствие февральских фактов. Историческая валидация получает ежедневные новые измерения и не проверяет качество при месяце без новых наблюдений, как в февральском replay. Метаданные Last-Modified — консервативное свидетельство существования версии объекта, а не полный первоначальный журнал публикаций."]
    (reports / "results.md").write_text("\n".join(sections) + "\n", encoding="utf-8")
    print("Saved reports/results.md, february_replay.csv, replay_summary.json")


if __name__ == "__main__":
    main()
