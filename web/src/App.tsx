import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  ArrowUp,
  BarChart3,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Database,
  FileCheck2,
  FileText,
  History,
  Info,
  Layers3,
  LayoutDashboard,
  Loader2,
  MapPin,
  Menu,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Thermometer,
  TriangleAlert,
  Wind,
  X,
  XCircle,
} from 'lucide-react'
import { api, ApiError } from './api'
import type {
  Bootstrap,
  DataInfo,
  Entity,
  Job,
  Quality,
  RunDetail,
  RunSummary,
  Stage,
  TurbineId,
} from './types'
import { dateOnly, formatDate, formatNumber, inputDate, issueFromInput, mean, modelLabel } from './format'
import ForecastChart from './components/ForecastChart'
import MiniChart from './components/MiniChart'
import QualityChart from './components/QualityChart'

type Page = 'overview' | 'history' | 'quality' | 'data'
const NAV = [
  { id: 'overview', label: 'Обзор', icon: LayoutDashboard },
  { id: 'history', label: 'История прогнозов', icon: History },
  { id: 'quality', label: 'Качество модели', icon: BarChart3 },
  { id: 'data', label: 'Данные и допущения', icon: Database },
] as const
const TURBINES: TurbineId[] = ['turbine_1', 'turbine_2']
const ENTITY_NAMES: Record<Entity, string> = {
  farm_proxy: 'Индекс ветропарка',
  turbine_1: 'Турбина 1',
  turbine_2: 'Турбина 2',
}
const FEB_NOTICE =
  'Фактические измерения за февраль 2026 года не предоставлены. Точность февральского прогноза пока не рассчитана.'

function BrandMark({ size = 34 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" fill="none" aria-hidden="true">
      <path
        d="M20 22v13M20 19 17 5h6l-3 14ZM17.5 21 5 28l3-7 9-2M22 22l13 6-6-6-7-3"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx="20" cy="20" r="2.8" fill="var(--accent)" />
      <path d="M15 35h10" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  )
}
function Panel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>{children}</section>
}
function Notice({ children, tone = 'amber' }: { children: ReactNode; tone?: 'amber' | 'info' | 'error' }) {
  const Icon = tone === 'error' ? XCircle : tone === 'info' ? Info : TriangleAlert
  return (
    <div className={`notice notice-${tone}`} role={tone === 'error' ? 'alert' : undefined}>
      <Icon size={17} />
      <div>{children}</div>
    </div>
  )
}
function ErrorMessage({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <Notice tone="error">
      <strong>{error instanceof Error ? error.message : 'Не удалось загрузить данные.'}</strong>
      {error instanceof ApiError && error.action && <p>{error.action}</p>}
      {retry && (
        <button className="text-button" onClick={retry}>
          Повторить запрос <RefreshCw size={13} />
        </button>
      )}
    </Notice>
  )
}
function Empty({ title = 'Нет данных', children }: { title?: string; children?: ReactNode }) {
  return (
    <div className="empty-state">
      <Database size={30} />
      <h3>{title}</h3>
      <p>{children ?? 'Для этого выбора сохранённых результатов пока нет.'}</p>
    </div>
  )
}
function Loading({ label = 'Загружаем сохранённые данные…' }: { label?: string }) {
  return (
    <div className="loading-state" role="status">
      <Loader2 className="spin" size={24} />
      <span>{label}</span>
    </div>
  )
}

function RunStages({
  stages,
  live = false,
  createdAt,
  jobIssue,
}: {
  stages: Stage[]
  live?: boolean
  createdAt?: string
  jobIssue?: string
}) {
  const done = stages.filter((s) => s.status === 'completed').length
  return (
    <Panel className="stages-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">АГЕНТНЫЙ ЦИКЛ</div>
          <h2>Ход расчёта</h2>
        </div>
        <span className={`pill ${live ? 'pill-teal' : 'pill-muted'}`}>
          <span className="status-dot" />
          {live
            ? 'Выполняется'
            : stages.some((s) => s.status === 'error')
              ? 'Ошибка расчёта'
              : 'Журнал выпуска'}
        </span>
      </div>
      <div className="stage-progress">
        <span>Подтверждённые этапы</span>
        <b>
          {done}
          <span> / 6</span>
        </b>
      </div>
      <div className="progress-track">
        <i style={{ width: `${(done / 6) * 100}%` }} />
      </div>
      <ol className="stage-list">
        {stages.map((s, i) => (
          <li key={s.id} className={`stage-${s.status}`}>
            <span className="stage-icon">
              {s.status === 'completed' ? (
                <Check size={14} />
              ) : s.status === 'running' ? (
                <Loader2 className="spin" size={14} />
              ) : s.status === 'error' ? (
                <X size={14} />
              ) : (
                <span>{i + 1}</span>
              )}
            </span>
            <div>
              <strong>{s.label}</strong>
              <span>
                {
                  { completed: 'Готово', running: 'Выполняется', pending: 'Ожидает', error: 'Ошибка' }[
                    s.status
                  ]
                }
                {s.duration_seconds != null && ` · ${formatNumber(s.duration_seconds, 1)} с`}
              </span>
            </div>
            {s.evidence && (
              <span className="stage-evidence" title={s.evidence} aria-label={s.evidence}>
                <Info size={13} />
              </span>
            )}
          </li>
        ))}
      </ol>
      <div className="panel-footnote">
        <Clock3 size={14} />
        <span>
          {jobIssue
            ? `Задание на ${formatDate(jobIssue)} +05:00`
            : live
              ? 'Состояния поступают от процесса расчёта.'
              : createdAt
                ? `Сохранён ${formatDate(createdAt)}`
                : 'Состояния подтверждаются сохранёнными данными.'}
        </span>
      </div>
    </Panel>
  )
}

function WeatherCard({ run }: { run: RunDetail }) {
  const weather = run.weather.turbine_1 ?? [],
    meta = run.weather_metadata.turbine_1 ?? {}
  const speed = mean(weather.map((v) => v.wind_speed)),
    temp = mean(weather.map((v) => v.temperature))
  const used = run.provenance.model_details.feature_mode === 'archive_weather'
  return (
    <Panel className="weather-panel">
      <div className="panel-heading">
        <div>
          <div className="eyebrow">АРХИВНЫЙ ВЫПУСК</div>
          <h2>Погодные условия</h2>
        </div>
        <Wind className="muted" size={21} />
      </div>
      {!weather.length ? (
        <Empty>В этом запуске погодные признаки не сохранены.</Empty>
      ) : (
        <>
          <div className="weather-values">
            <div>
              <Wind size={18} />
              <strong>
                {formatNumber(speed, 1)}
                <small> м/с</small>
              </strong>
              <span>ветер на высоте 10 м</span>
            </div>
            <div>
              <Thermometer size={18} />
              <strong>
                {formatNumber(temp, 1)}
                <small> °C</small>
              </strong>
              <span>температура на 2 м</span>
            </div>
          </div>
          <div className="weather-meta">
            <span>Источник</span>
            <b>NOAA · GFS</b>
            <span>Выпуск</span>
            <b>{formatDate(meta.run_time ?? weather[0]?.weather_run_time)} +05:00</b>
            <span>Сетка</span>
            <b>
              {meta.grid === '1p00'
                ? '1°'
                : meta.grid === '0p25'
                  ? '0,25°'
                  : meta.grid === '0p50'
                    ? '0,5°'
                    : (meta.grid ?? 'Нет данных')}
            </b>
          </div>
          <p className="microcopy">
            Средние прогнозные условия за выбранный горизонт.
            {!used && ' Погода сохранена, но эта модель не использует её в числовых признаках.'}
          </p>
          <div className="weather-warning">
            <Info size={14} />
            <span>
              Узел примерно в {formatNumber(meta.nearest_grid_point?.distance_km ?? null, 0)} км. Локальный
              рельеф и высота ступицы не представлены.
            </span>
          </div>
        </>
      )}
    </Panel>
  )
}

function Provenance({ run }: { run: RunDetail }) {
  return (
    <details className="panel provenance">
      <summary>
        <span>
          <FileCheck2 size={17} />
          Сведения о расчёте
        </span>
        <span className="summary-hint">
          Источники, модель и проверки <ChevronDown size={16} />
        </span>
      </summary>
      <div className="provenance-content">
        <div className="detail-grid">
          <div>
            <span>Модель</span>
            <strong>{modelLabel(run.summary.model)}</strong>
          </div>
          <div>
            <span>Момент выпуска</span>
            <strong>{formatDate(run.summary.issue_time)} UTC+05:00</strong>
          </div>
          <div>
            <span>Проверки результата</span>
            <strong>{run.provenance.checks.status === 'passed' ? 'Пройдены' : 'Нет подтверждения'}</strong>
          </div>
          <div>
            <span>Идентификатор выпуска</span>
            <code>{run.summary.run_id}</code>
          </div>
        </div>
        <p>
          Это нормализованный индекс по шкале 0–1. Определение нормализации и номинальные мощности не
          подтверждены.
        </p>
        <details>
          <summary>Технический манифест</summary>
          <pre>{JSON.stringify(run.provenance, null, 2)}</pre>
        </details>
        <details>
          <summary>Сохранённые события ({run.events.length})</summary>
          {run.events.length ? (
            <pre>{JSON.stringify(run.events, null, 2)}</pre>
          ) : (
            <p>
              Отдельные события не найдены. Этапы подтверждены манифестом; длительности не восстанавливаются
              предположениями.
            </p>
          )}
        </details>
      </div>
    </details>
  )
}

function Overview({ run, horizon, job }: { run: RunDetail; horizon: 24 | 48; job: Job | null }) {
  const values = run.forecast.map((r) => r.farm_proxy),
    avg = mean(values)
  const valid = run.forecast.filter((p) => p.farm_proxy != null)
  const peak = valid.length
    ? valid.reduce((a, b) => ((a.farm_proxy ?? -1) > (b.farm_proxy ?? -1) ? a : b))
    : null
  const mid = Math.floor(values.length / 2),
    first = mean(values.slice(0, mid)),
    second = mean(values.slice(mid))
  const change =
    first != null && first !== 0 && second != null ? ((second - first) / Math.abs(first)) * 100 : null
  const ages = TURBINES.map((t) => run.summary.stale_hours[t]).filter((v): v is number => v != null)
  const age = ages.length === 2 ? Math.max(...ages) : null
  const live = job?.status === 'running' || job?.status === 'queued'
  const showJob = job && job.status !== 'completed'
  return (
    <>
      <div className="kpi-grid">
        <Panel className="kpi">
          <div className="kpi-label">
            <span>Средний индекс</span>
            <Activity size={17} />
          </div>
          <div className="kpi-number accent">{formatNumber(avg)}</div>
          <p>
            нормализованная шкала 0–1 <span>· {horizon} ч</span>
          </p>
        </Panel>
        <Panel className="kpi">
          <div className="kpi-label">
            <span>Пиковый индекс</span>
            <ArrowUp size={17} />
          </div>
          <div className="kpi-number">{formatNumber(peak?.farm_proxy)}</div>
          <p>
            {peak ? formatDate(peak.target_time, true) : 'Нет данных'} <span>· UTC+05:00</span>
          </p>
        </Panel>
        <Panel className="kpi">
          <div className="kpi-label">
            <span>Изменение прогноза</span>
            <BarChart3 size={17} />
          </div>
          <div className={`kpi-number ${change != null && change >= 0 ? 'accent' : 'blue'}`}>
            {change == null ? 'Нет данных' : `${change >= 0 ? '+' : ''}${formatNumber(change, 1)}%`}
          </div>
          <p>
            вторая половина к первой <span>· средние</span>
          </p>
        </Panel>
        <Panel className={`kpi ${age != null && age > 24 ? 'kpi-warning' : ''}`}>
          <div className="kpi-label">
            <span>Возраст измерений</span>
            <Clock3 size={17} />
          </div>
          <div className="kpi-number">
            {formatNumber(age, 0)}
            {age != null && <small> ч</small>}
          </div>
          <p>после конца последнего часа</p>
        </Panel>
      </div>
      {age != null && age > 24 && (
        <Notice>
          Измерения устарели на {formatNumber(age, 0)} ч. В этом выпуске используются только доступные
          январские факты и соответствующий архив погоды.
        </Notice>
      )}
      <div className="dashboard-grid">
        <div className="main-column">
          <Panel className="forecast-panel">
            <div className="panel-heading">
              <div>
                <div className="eyebrow">ПОЧАСОВОЙ ПРОГНОЗ</div>
                <h2>Выработка ветропарка</h2>
              </div>
              <a className="button button-quiet button-sm" href={run.summary.csv_url}>
                <ArrowDownToLine size={15} />
                <span>CSV · {run.summary.horizon} ч</span>
              </a>
            </div>
            <div className="chart-caption">
              <span>Нормализованная мощность · 0–1</span>
              <span>UTC+05:00</span>
            </div>
            <ForecastChart
              points={run.forecast}
              history={run.actual_history}
              issueTime={run.summary.issue_time}
              horizon={horizon}
            />
            <div className="chart-bottom">
              <span>
                <span className="status-dot" /> {modelLabel(run.summary.model)}
              </span>
              <span>
                {run.summary.horizon > horizon
                  ? `Первые ${horizon} часа сохранённого выпуска на ${run.summary.horizon} ч`
                  : `${horizon} часовых значений`}
              </span>
            </div>
          </Panel>
          <div className="turbine-grid">
            {TURBINES.map((t, i) => {
              const config = run.turbines[t],
                color = i === 0 ? '#4385FF' : '#A68BFA'
              return (
                <Panel className="turbine-card" key={t}>
                  <div className="turbine-top">
                    <div className="turbine-icon" style={{ color, background: `${color}12` }}>
                      <BrandMark size={29} />
                    </div>
                    <div>
                      <h3>Турбина {i + 1}</h3>
                      <span className="coordinate">
                        <MapPin size={11} />
                        {config
                          ? `${config.latitude.toFixed(6)}, ${config.longitude.toFixed(6)}`
                          : 'Координаты не сохранены'}
                      </span>
                    </div>
                    <span className="turbine-code">T0{i + 1}</span>
                  </div>
                  <div className="turbine-chart-row">
                    <div>
                      <span className="micro-label">СРЕДНИЙ ПРОГНОЗ</span>
                      <strong style={{ color }}>{formatNumber(mean(run.forecast.map((p) => p[t])))}</strong>
                      <small>индекс 0–1</small>
                    </div>
                    <div className="mini-chart">
                      <MiniChart points={run.forecast.map((p) => p[t])} color={color} height={64} />
                    </div>
                  </div>
                  <div className="turbine-footer">
                    <span>
                      <span
                        className="status-dot"
                        style={{ background: run.summary.last_observed_hour[t] ? '#98A8BD' : '#D7A65B' }}
                      />
                      Последний измеренный час
                    </span>
                    <b>{formatDate(run.summary.last_observed_hour[t], true)}</b>
                  </div>
                </Panel>
              )
            })}
          </div>
        </div>
        <aside className="context-column">
          <RunStages
            stages={showJob ? job.stages : run.stages}
            live={!!live}
            createdAt={showJob ? undefined : run.summary.created_at}
            jobIssue={showJob ? job.issue_time : undefined}
          />
          <WeatherCard run={run} />
        </aside>
      </div>
      <Notice tone="info">{FEB_NOTICE}</Notice>
      <Provenance run={run} />
    </>
  )
}

function HistoryPage({ runs, onOpen }: { runs: RunSummary[]; onOpen: (id: string) => void }) {
  const [from, setFrom] = useState(''),
    [to, setTo] = useState(''),
    [model, setModel] = useState('all'),
    [onlyReplay, setOnlyReplay] = useState(false),
    [page, setPage] = useState(0)
  const filtered = useMemo(
    () =>
      runs.filter((r) => {
        const day = inputDate(r.issue_time).slice(0, 10)
        return (
          (!from || day >= from) &&
          (!to || day <= to) &&
          (model === 'all' || r.model === model) &&
          (!onlyReplay || r.is_replay)
        )
      }),
    [runs, from, to, model, onlyReplay]
  )
  useEffect(() => setPage(0), [from, to, model, onlyReplay])
  const pageCount = Math.ceil(filtered.length / 10),
    visible = filtered.slice(page * 10, (page + 1) * 10)
  return (
    <>
      <div className="page-intro">
        <div>
          <div className="eyebrow">ПРОВЕРЯЕМАЯ ИСТОРИЯ</div>
          <h1>Сохранённые прогнозы</h1>
          <p>Каждая строка — реальный выпуск с собственным моментом расчёта.</p>
        </div>
        <div className="big-count">
          {runs.length}
          <span>сохранённых выпусков</span>
        </div>
      </div>
      <Panel>
        <div className="filters">
          <label>
            С даты
            <input
              type="date"
              value={from}
              onInput={(e) => setFrom(e.currentTarget.value)}
              onChange={(e) => setFrom(e.target.value)}
            />
          </label>
          <label>
            По дату
            <input
              type="date"
              value={to}
              onInput={(e) => setTo(e.currentTarget.value)}
              onChange={(e) => setTo(e.target.value)}
            />
          </label>
          <label>
            Модель
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="all">Все модели</option>
              {[...new Set(runs.map((r) => r.model))].map((m) => (
                <option key={m} value={m}>
                  {modelLabel(m)}
                </option>
              ))}
            </select>
          </label>
          <label className="checkbox-filter">
            <input type="checkbox" checked={onlyReplay} onChange={(e) => setOnlyReplay(e.target.checked)} />
            Только конкурсный replay
          </label>
          <button
            className="text-button"
            onClick={() => {
              setFrom('')
              setTo('')
              setModel('all')
              setOnlyReplay(false)
            }}
          >
            Сбросить
          </button>
        </div>
        <div className="table-container">
          {visible.length ? (
            <table>
              <thead>
                <tr>
                  <th>Момент выпуска · +05:00</th>
                  <th>Горизонт</th>
                  <th>Модель</th>
                  <th>Проверки</th>
                  <th>Возраст входов</th>
                  <th>
                    <span className="sr-only">Действия</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((r) => {
                  const ages = Object.values(r.stale_hours).filter((n): n is number => n != null)
                  return (
                    <tr key={r.run_id}>
                      <td>
                        <strong>{formatDate(r.issue_time)}</strong>
                        <small className="table-sub">
                          {r.is_replay
                            ? 'Конкурсный replay'
                            : r.collection.startsWith('web_forecasts')
                              ? 'Расчёт в интерфейсе'
                              : 'Исторический выпуск'}{' '}
                          · {r.run_id.slice(0, 7)}
                        </small>
                      </td>
                      <td>{r.horizon} ч</td>
                      <td>{modelLabel(r.model)}</td>
                      <td>
                        <span className={`text-status ${r.status === 'passed' ? 'text-success' : 'muted'}`}>
                          {r.status === 'passed' ? <CheckCircle2 size={13} /> : <Info size={13} />}{' '}
                          {r.status === 'passed' ? 'Пройдены' : 'Нет сведений'}
                        </span>
                      </td>
                      <td className={ages.length && Math.max(...ages) > 24 ? 'amber' : ''}>
                        {ages.length ? `${formatNumber(Math.max(...ages), 0)} ч` : 'Нет данных'}
                      </td>
                      <td>
                        <div className="row-actions">
                          <button className="text-button" onClick={() => onOpen(r.run_id)}>
                            Открыть <ArrowRight size={14} />
                          </button>
                          <a
                            href={r.csv_url}
                            className="icon-button"
                            title="Скачать CSV"
                            aria-label={`Скачать CSV выпуска ${formatDate(r.issue_time)}`}
                          >
                            <ArrowDownToLine size={16} />
                          </a>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          ) : (
            <Empty>Измените дату или фильтр модели.</Empty>
          )}
        </div>
        <div className="pagination">
          <span>Найдено {filtered.length} выпусков</span>
          <div>
            <button
              className="icon-button"
              disabled={page === 0}
              onClick={() => setPage((p) => p - 1)}
              aria-label="Предыдущая страница"
            >
              <ChevronLeft size={17} />
            </button>
            <span>
              {pageCount ? page + 1 : 0} / {pageCount}
            </span>
            <button
              className="icon-button"
              disabled={page + 1 >= pageCount}
              onClick={() => setPage((p) => p + 1)}
              aria-label="Следующая страница"
            >
              <ChevronRight size={17} />
            </button>
          </div>
        </div>
      </Panel>
    </>
  )
}

function QualityPage() {
  const [quality, setQuality] = useState<Quality | null>(null),
    [error, setError] = useState<unknown>(null),
    [datasetId, setDatasetId] = useState('validation_weather'),
    [entity, setEntity] = useState<Entity>('farm_proxy'),
    [scope, setScope] = useState('all'),
    [model, setModel] = useState('extra_trees_weather'),
    [issue, setIssue] = useState('')
  const load = useCallback(() => {
    setError(null)
    api<Quality>('/quality').then(setQuality).catch(setError)
  }, [])
  useEffect(load, [load])
  const dataset = quality?.datasets.find((d) => d.id === datasetId) ?? quality?.datasets[0]
  const models = dataset ? [...new Set(dataset.metrics.map((r) => r.model))] : []
  const issues = dataset ? [...new Set(dataset.predictions.map((r) => r.issue_time))].sort() : []
  const selectedModel = models.includes(model)
    ? model
    : dataset?.selected_model && models.includes(dataset.selected_model)
      ? dataset.selected_model
      : models[0]
  const selectedIssue = issues.includes(issue) ? issue : issues[0]
  const metrics = dataset?.metrics.filter((r) => r.entity === entity && String(r.horizon) === scope) ?? []
  const points =
    dataset?.predictions
      .filter(
        (p) =>
          p.model === selectedModel &&
          p.entity === entity &&
          p.issue_time === selectedIssue &&
          (scope === 'all' || (scope === '1-24' ? p.horizon <= 24 : p.horizon > 24))
      )
      .map((p) => ({ target_time: p.target_time, prediction: p.prediction, actual: p.actual })) ?? []
  if (error) return <ErrorMessage error={error} retry={load} />
  if (!quality) return <Loading />
  if (!dataset) return <Empty>Отчёты временной валидации не найдены.</Empty>
  return (
    <>
      <div className="page-intro">
        <div>
          <div className="eyebrow">ВРЕМЕННАЯ ВАЛИДАЦИЯ</div>
          <h1>Качество модели</h1>
          <p>Сравнение на исторических данных с соблюдением времени доступности.</p>
        </div>
        <span className="pill pill-blue">
          <FileCheck2 size={13} />
          Сохранённые метрики
        </span>
      </div>
      <Notice tone="amber">{quality.february_notice || FEB_NOTICE}</Notice>
      <Panel>
        <div className="filters quality-filters">
          <label>
            Выборка
            <select
              value={dataset.id}
              onChange={(e) => {
                setDatasetId(e.target.value)
                setIssue('')
              }}
            >
              {quality.datasets.map((d) => (
                <option value={d.id} key={d.id}>
                  {d.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Объект
            <select value={entity} onChange={(e) => setEntity(e.target.value as Entity)}>
              {Object.entries(ENTITY_NAMES).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Горизонты
            <select value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="all">Все · 1–48 ч</option>
              <option value="1-24">1–24 часа</option>
              <option value="25-48">25–48 часов</option>
            </select>
          </label>
        </div>
        <div className="evaluation-meta">
          <span>
            <CalendarDays size={14} />
            {dataset.first_issue ? dateOnly(dataset.first_issue) : 'Нет данных'} —{' '}
            {dataset.last_issue ? dateOnly(dataset.last_issue) : 'Нет данных'}
          </span>
          <span>
            <Layers3 size={14} />
            {dataset.n_folds} выпусков
          </span>
          <span className="pill pill-muted">
            {dataset.role === 'control'
              ? 'Контрольная проверка'
              : dataset.role === 'sensitivity'
                ? 'Проверка покрытия'
                : 'Выбор модели'}
          </span>
        </div>
        <div className="table-container">
          <table className="metrics-table">
            <thead>
              <tr>
                <th>Модель</th>
                <th>MAE ↓</th>
                <th>RMSE ↓</th>
                <th>Смещение</th>
                <th>Целей</th>
              </tr>
            </thead>
            <tbody>
              {metrics.map((m) => (
                <tr key={m.model} className={m.model === dataset.selected_model ? 'selected-metric' : ''}>
                  <td>
                    <strong>{modelLabel(m.model)}</strong>
                    {m.model === dataset.selected_model && (
                      <span className="metric-label">
                        {dataset.role === 'control' ? 'Выбрана до контроля' : 'Выбрана по MAE индекса'}
                      </span>
                    )}
                  </td>
                  <td>{formatNumber(m.mae, 4)}</td>
                  <td>{formatNumber(m.rmse, 4)}</td>
                  <td>
                    {m.bias != null && m.bias > 0 ? '+' : ''}
                    {formatNumber(m.bias, 4)}
                  </td>
                  <td>{formatNumber(m.n, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="quality-note">
          <Info size={16} />
          <p>
            {dataset.note || 'Короткая временная проверка не гарантирует качество на будущих сезонах.'} MAE и
            RMSE выражены в единицах нормализованного индекса. Смещение — прогноз минус факт.
          </p>
        </div>
      </Panel>
      <Panel className="quality-chart-panel">
        <div className="panel-heading">
          <div>
            <div className="eyebrow">ИСТОРИЧЕСКАЯ ПРОВЕРКА</div>
            <h2>Прогноз и факт</h2>
          </div>
        </div>
        <div className="filters compact-filters">
          <label>
            Модель
            <select value={selectedModel ?? ''} onChange={(e) => setModel(e.target.value)}>
              {models.map((m) => (
                <option value={m} key={m}>
                  {modelLabel(m)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Момент выпуска · UTC+05:00
            <select
              value={selectedIssue ?? ''}
              onInput={(e) => setIssue(e.currentTarget.value)}
              onChange={(e) => setIssue(e.target.value)}
            >
              {issues.map((d) => (
                <option value={d} key={d}>
                  {formatDate(d)}
                </option>
              ))}
            </select>
          </label>
        </div>
        {points.length ? (
          <QualityChart
            points={points}
            modelLabel={modelLabel(selectedModel ?? '')}
            issueTime={selectedIssue}
          />
        ) : (
          <Empty>Нет сохранённых пар «прогноз — факт» для этого выбора.</Empty>
        )}
        <p className="panel-footnote">
          На графике один выпуск. Перекрывающиеся горизонты соседних выпусков не объединяются в один ряд.
        </p>
      </Panel>
    </>
  )
}

function DataPage() {
  const [data, setData] = useState<DataInfo | null>(null),
    [error, setError] = useState<unknown>(null)
  const load = useCallback(() => {
    setError(null)
    api<DataInfo>('/data').then(setData).catch(setError)
  }, [])
  useEffect(load, [load])
  if (error) return <ErrorMessage error={error} retry={load} />
  if (!data) return <Loading />
  return (
    <>
      <div className="page-intro">
        <div>
          <div className="eyebrow">ПРОИСХОЖДЕНИЕ И ГРАНИЦЫ ДАННЫХ</div>
          <h1>Данные и допущения</h1>
          <p>Подтверждённые измерения отдельно от принятых правил проекта.</p>
        </div>
        <ShieldCheck className="accent" size={32} />
      </div>
      <Notice>{data.february_notice || FEB_NOTICE}</Notice>
      <div className="data-cards">
        {TURBINES.map((t, i) => {
          const audit = data.audit?.turbines[t]
          return (
            <Panel key={t} className="data-turbine">
              <div className="panel-heading">
                <h2>Турбина {i + 1}</h2>
                <Database size={18} style={{ color: i ? '#A68BFA' : '#4385FF' }} />
              </div>
              {audit ? (
                <>
                  <div className="data-large-number">
                    {formatNumber(audit.rows, 0)}
                    <span>записей</span>
                  </div>
                  <div className="data-stat-grid">
                    <div>
                      <span>Первое измерение</span>
                      <b>{formatDate(audit.start_local)}</b>
                    </div>
                    <div>
                      <span>Последнее измерение</span>
                      <b>{formatDate(audit.end_local)}</b>
                    </div>
                    <div>
                      <span>Пропущенные слоты</span>
                      <b className="amber">{formatNumber(audit.missing_ten_minute_slots, 0)}</b>
                    </div>
                    <div>
                      <span>Валидные часовые цели</span>
                      <b>{formatNumber(audit.valid_hourly_targets, 0)}</b>
                    </div>
                  </div>
                </>
              ) : (
                <Empty />
              )}
            </Panel>
          )
        })}
      </div>
      <div className="data-rules">
        <Panel>
          <div className="panel-heading">
            <h2>Правила подготовки</h2>
            <SlidersHorizontal size={18} />
          </div>
          <div className="rule-row">
            <span className="rule-number">01</span>
            <div>
              <h3>Почасовая агрегация</h3>
              <p>{data.aggregation_rule}</p>
            </div>
          </div>
          <div className="rule-row">
            <span className="rule-number">02</span>
            <div>
              <h3>Время измерений</h3>
              <p>
                Принят UTC{data.timezone}. Метка — начало десятиминутного интервала. Оба правила требуют
                подтверждения организаторов.
              </p>
            </div>
          </div>
          <div className="rule-row">
            <span className="rule-number">03</span>
            <div>
              <h3>Общий индекс</h3>
              <p>{data.farm_definition}</p>
            </div>
          </div>
          <div className="rule-row">
            <span className="rule-number">04</span>
            <div>
              <h3>Выравнивание по времени</h3>
              <p>
                Общих меток: {formatNumber(data.audit?.alignment.shared_timestamps, 0)}. Объединение по
                времени, без ID или позиции строки. Пропуски не заменяются нулями.
              </p>
            </div>
          </div>
        </Panel>
        <Panel>
          <div className="panel-heading">
            <h2>Ограничения погоды</h2>
            <Wind size={18} />
          </div>
          <ul className="readable-list">
            {data.weather_limitations.map((text, i) => (
              <li key={i}>{text}</li>
            ))}
          </ul>
          <div className="assumption-box">
            <Info size={17} />
            <p>
              Имя исходных CSV упоминает 28 февраля. Фактические данные заканчиваются 31 января 2026 года.
            </p>
          </div>
        </Panel>
      </div>
      <Panel>
        <div className="panel-heading">
          <div className="eyebrow-heading">
            <div className="eyebrow">ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ</div>
            <h2>Вопросы организаторам</h2>
          </div>
          <span className="pill pill-amber">Открыты</span>
        </div>
        <ol className="questions-list">
          {data.questions.map((q, i) => (
            <li key={i}>
              <span>{String(i + 1).padStart(2, '0')}</span>
              {q}
            </li>
          ))}
        </ol>
        <p className="panel-footnote">
          Вопросы подготовлены для уточнения. До ответа действуют явно указанные допущения.
        </p>
      </Panel>
    </>
  )
}

export default function App() {
  const menuRef = useRef<HTMLButtonElement>(null)
  const sidebarRef = useRef<HTMLElement>(null)
  const [page, setPage] = useState<Page>(
    NAV.find((n) => `#${n.id}` === window.location.hash)?.id ?? 'overview'
  )
  const [boot, setBoot] = useState<Bootstrap | null>(null),
    [bootError, setBootError] = useState<unknown>(null),
    [selectedId, setSelectedId] = useState(''),
    [run, setRun] = useState<RunDetail | null>(null),
    [runError, setRunError] = useState<unknown>(null),
    [runLoading, setRunLoading] = useState(false),
    [horizon, setHorizon] = useState<24 | 48>(48),
    [issue, setIssue] = useState(''),
    [mobileOpen, setMobileOpen] = useState(false),
    [job, setJob] = useState<Job | null>(null),
    [jobError, setJobError] = useState<unknown>(null),
    [notice, setNotice] = useState(''),
    [submitting, setSubmitting] = useState(false)
  const loadBootstrap = useCallback(async (initial = false) => {
    setBootError(null)
    try {
      const result = await api<Bootstrap>('/bootstrap')
      setBoot(result)
      if (result.active_job) setJob(result.active_job)
      if (initial) {
        const queryRun = new URLSearchParams(location.search).get('run')
        const id = result.runs.some((r) => r.run_id === queryRun) ? queryRun : result.default_run_id
        if (id) {
          setSelectedId(id)
          const summary = result.runs.find((r) => r.run_id === id)
          if (summary) {
            setIssue(inputDate(summary.issue_time))
            setHorizon(summary.horizon >= 48 ? 48 : 24)
          }
        } else setIssue('2026-01-31T23:00')
      }
    } catch (e) {
      setBootError(e)
    }
  }, [])
  useEffect(() => {
    void loadBootstrap(true)
  }, [loadBootstrap])
  useEffect(() => {
    const listen = () => {
      setPage(NAV.find((n) => `#${n.id}` === window.location.hash)?.id ?? 'overview')
      setMobileOpen(false)
    }
    window.addEventListener('hashchange', listen)
    return () => window.removeEventListener('hashchange', listen)
  }, [])
  useEffect(() => {
    if (!selectedId) return
    let alive = true
    setRunLoading(true)
    setRunError(null)
    api<RunDetail>(`/runs/${selectedId}?horizon=${horizon}`)
      .then((result) => {
        if (alive) {
          setRun(result)
          const url = new URL(location.href)
          url.searchParams.set('run', selectedId)
          history.replaceState(null, '', url)
        }
      })
      .catch((e) => {
        if (alive) setRunError(e)
      })
      .finally(() => {
        if (alive) setRunLoading(false)
      })
    return () => {
      alive = false
    }
  }, [selectedId, horizon])
  const active = job?.status === 'queued' || job?.status === 'running'
  useEffect(() => {
    if (!mobileOpen) return
    const elements = () => Array.from(sidebarRef.current?.querySelectorAll<HTMLElement>('a,button') ?? [])
    elements()[0]?.focus()
    const handle = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMobileOpen(false)
        menuRef.current?.focus()
      }
      if (e.key === 'Tab') {
        const controls = elements(),
          first = controls[0],
          last = controls[controls.length - 1]
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault()
          last?.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault()
          first?.focus()
        }
      }
    }
    document.addEventListener('keydown', handle)
    return () => document.removeEventListener('keydown', handle)
  }, [mobileOpen])
  useEffect(() => {
    if (!active || !job) return
    let stop = false
    const timer = window.setInterval(() => {
      api<{ job: Job }>(`/jobs/${job.job_id}`)
        .then(({ job: next }) => {
          if (stop) return
          setJob(next)
          if (next.status === 'completed') {
            void loadBootstrap()
            if (next.run_id) {
              setSelectedId(next.run_id)
              setHorizon(next.horizon >= 48 ? 48 : 24)
              setIssue(inputDate(next.issue_time))
              setPage('overview')
              location.hash = 'overview'
              setNotice('Расчёт завершён. Прогноз и сведения о нём сохранены.')
            }
          }
        })
        .catch(setJobError)
    }, 1500)
    return () => {
      stop = true
      clearInterval(timer)
    }
  }, [active, job?.job_id, loadBootstrap])
  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(''), 5000)
    return () => clearTimeout(timer)
  }, [notice])
  const matchingRun = useMemo(() => {
    if (!boot || !issue) return null
    const date = new Date(issueFromInput(issue)).valueOf()
    return (
      boot.runs
        .filter((r) => new Date(r.issue_time).valueOf() === date && r.horizon >= horizon)
        .sort(
          (a, b) =>
            Number(b.is_replay) - Number(a.is_replay) ||
            Number(b.model === 'extra_trees_weather') - Number(a.model === 'extra_trees_weather')
        )[0] ?? null
    )
  }, [boot, issue, horizon])
  const startJob = async () => {
    if (!issue || active || submitting) return
    setSubmitting(true)
    setJobError(null)
    try {
      const result = await api<{ job: Job }>('/jobs', {
        method: 'POST',
        body: JSON.stringify({ issue_time: issueFromInput(issue), horizon, model: 'auto' }),
      })
      setJob(result.job)
      setPage('overview')
      location.hash = 'overview'
      setNotice('Задание принято. Состояние расчёта отображается ниже.')
    } catch (e) {
      setJobError(e)
    } finally {
      setSubmitting(false)
    }
  }
  const openSaved = () => {
    if (!matchingRun) return
    if (!active) {
      setJob(null)
      setJobError(null)
    }
    setSelectedId(matchingRun.run_id)
    setPage('overview')
    location.hash = 'overview'
    setNotice('Сохранённый прогноз открыт без повторного обучения.')
  }
  const openHistory = (id: string) => {
    if (!active) {
      setJob(null)
      setJobError(null)
    }
    const summary = boot?.runs.find((r) => r.run_id === id)
    if (summary) {
      setIssue(inputDate(summary.issue_time))
      setHorizon(summary.horizon >= 48 ? 48 : 24)
    }
    setSelectedId(id)
    setPage('overview')
    location.hash = 'overview'
  }
  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">
        Перейти к содержимому
      </a>
      {mobileOpen && (
        <button
          className="mobile-scrim"
          onClick={() => setMobileOpen(false)}
          aria-label="Закрыть навигацию"
        />
      )}
      <aside ref={sidebarRef} className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`}>
        <a className="brand" href="#overview" onClick={() => setMobileOpen(false)}>
          <span className="brand-icon">
            <BrandMark />
          </span>
          <span>
            WindPulse<span className="brand-ai">AI</span>
          </span>
        </a>
        <div className="brand-caption">WIND ENERGY INTELLIGENCE</div>
        <div className="nav-label">РАБОЧАЯ ОБЛАСТЬ</div>
        <nav aria-label="Главная навигация">
          {NAV.map((n) => (
            <a
              key={n.id}
              onClick={() => setMobileOpen(false)}
              href={`#${n.id}`}
              className={`nav-link ${page === n.id ? 'nav-active' : ''}`}
              aria-current={page === n.id ? 'page' : undefined}
            >
              <n.icon size={18} />
              <span>{n.label}</span>
              {page === n.id && <i />}
            </a>
          ))}
        </nav>
        <div className="sidebar-station">
          <div className="station-visual">
            <BrandMark size={67} />
            <BrandMark size={46} />
            <span />
          </div>
          <span className="micro-label">ОБЪЕКТ ПРОГНОЗИРОВАНИЯ</span>
          <strong>Ветропарк · 2 турбины</strong>
          <span className="station-coords">43,64° N &nbsp; 78,54° E</span>
          <div className="station-line">
            <span className="status-dot" />
            Архивный режим
          </div>
        </div>
        <div className="sidebar-footer">
          <span>HACKALEM AI</span>
          <span>2026</span>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="topbar-left">
            <button
              ref={menuRef}
              className="icon-button mobile-menu"
              onClick={() => setMobileOpen((v) => !v)}
              aria-label="Открыть навигацию"
              aria-expanded={mobileOpen}
            >
              <Menu size={22} />
            </button>
            <span className="breadcrumb">
              Ветропарк<span>/</span>
              <b>{NAV.find((n) => n.id === page)?.label}</b>
            </span>
          </div>
          <div className="topbar-right">
            <span className="pill pill-muted">
              <History size={12} />
              Исторический прогноз
            </span>
            <span className="topbar-zone">UTC+05:00</span>
            <div className="operator-avatar" title="Демонстрация HackAlem AI">
              WP
            </div>
          </div>
        </header>
        <main id="main-content" tabIndex={-1}>
          {bootError ? (
            <ErrorMessage error={bootError} retry={() => void loadBootstrap(true)} />
          ) : !boot ? (
            <Loading label="Открываем локальные результаты WindPulse…" />
          ) : (
            <>
              <div className={`overview-header ${page !== 'overview' ? 'header-compact' : ''}`}>
                <div>
                  <div className="eyebrow">WINDPULSE AI</div>
                  <h1>{page === 'overview' ? 'Прогноз ветропарка' : 'Центр прогнозирования'}</h1>
                  <p>Интеллектуальный прогноз выработки ветропарка</p>
                </div>
                {page === 'overview' && (
                  <div className="header-proof">
                    <ShieldCheck size={17} />
                    <span>
                      Проверяемые данные
                      <br />
                      <b>Воспроизводимый расчёт</b>
                    </span>
                  </div>
                )}
              </div>
              <section className="forecast-toolbar" aria-label="Параметры прогноза">
                <label className="issue-picker">
                  <span>Момент выпуска</span>
                  <div>
                    <CalendarDays size={17} />
                    <input
                      type="datetime-local"
                      step={3600}
                      value={issue}
                      onInput={(e) => setIssue(e.currentTarget.value)}
                      onChange={(e) => setIssue(e.target.value)}
                      aria-label="Момент выпуска прогноза"
                    />
                    <span className="input-zone">+05:00</span>
                  </div>
                </label>
                <div className="horizon-picker">
                  <span className="control-label">Горизонт прогноза</span>
                  <div className="segmented" role="group" aria-label="Горизонт прогноза">
                    {([24, 48] as const).map((h) => (
                      <button
                        key={h}
                        className={horizon === h ? 'selected' : ''}
                        aria-pressed={horizon === h}
                        onClick={() => {
                          setHorizon(h)
                          if (run && h > run.summary.horizon) {
                            const longer = boot.runs.find(
                              (r) =>
                                r.issue_time === run.summary.issue_time &&
                                r.horizon >= h &&
                                r.model === run.summary.model
                            )
                            if (longer) setSelectedId(longer.run_id)
                          }
                        }}
                      >
                        {h} {h === 24 ? 'часа' : 'часов'}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="toolbar-actions">
                  <button
                    className="button button-primary"
                    disabled={
                      !issue ||
                      submitting ||
                      (!matchingRun && (active || !boot.availability.forecast_enabled))
                    }
                    onClick={matchingRun ? openSaved : startJob}
                  >
                    {submitting ? (
                      <Loader2 size={17} className="spin" />
                    ) : matchingRun ? (
                      <FileText size={17} />
                    ) : (
                      <Activity size={17} />
                    )}
                    <span>{matchingRun ? 'Открыть сохранённый прогноз' : 'Рассчитать прогноз'}</span>
                    <ArrowRight size={16} />
                  </button>
                  {matchingRun && (
                    <button
                      className="button button-secondary recalculate-button"
                      onClick={startJob}
                      disabled={!!active || submitting || !boot.availability.forecast_enabled}
                      title="Запустить расчёт в фоне из локального кэша"
                    >
                      <RefreshCw size={16} />
                      <span>Пересчитать</span>
                    </button>
                  )}
                </div>
              </section>
              {!boot.availability.forecast_enabled && <Notice>{boot.availability.message}</Notice>}
              {jobError && <ErrorMessage error={jobError} />}{' '}
              {job?.status === 'failed' && (
                <ErrorMessage
                  error={
                    new ApiError(job.error?.message ?? 'Расчёт завершился с ошибкой.', job.error?.action)
                  }
                />
              )}{' '}
              {job?.status === 'interrupted' && (
                <Notice>Расчёт был прерван перезапуском сервера. Запустите его повторно.</Notice>
              )}
              {active && job && (
                <div className="job-banner" role="status">
                  <Loader2 className="spin" size={18} />
                  <div>
                    <strong>Выполняется расчёт на {formatDate(job.issue_time)}</strong>
                    <span>Можно просматривать сохранённые выпуски. Результат появится после завершения.</span>
                  </div>
                  <span className="pill pill-teal">{job.horizon} ч · локальный кэш</span>
                </div>
              )}
              {page === 'overview' ? (
                runLoading ? (
                  <Loading label="Загружаем прогноз и происхождение данных…" />
                ) : runError ? (
                  <ErrorMessage error={runError} />
                ) : run ? (
                  <>
                    <div className="opened-run-label">
                      <span className="status-dot" />
                      Открыт выпуск <strong>{formatDate(run.summary.issue_time)} +05:00</strong>
                      <span className="muted">· {modelLabel(run.summary.model)}</span>
                    </div>
                    {horizon > run.summary.horizon && (
                      <Notice>
                        В этом выпуске сохранено {run.summary.horizon} часа. Ниже показан доступный горизонт;
                        для {horizon} часов откройте другой выпуск или запустите расчёт.
                      </Notice>
                    )}
                    <Overview
                      run={run}
                      horizon={Math.min(horizon, run.summary.horizon) as 24 | 48}
                      job={job}
                    />
                  </>
                ) : (
                  <>
                    {active && job ? (
                      <RunStages stages={job.stages} live />
                    ) : (
                      <Empty title="Сохранённых прогнозов нет">
                        Выберите исторический момент и запустите расчёт, когда исходные данные и погодный кэш
                        готовы.
                      </Empty>
                    )}
                  </>
                )
              ) : page === 'history' ? (
                <HistoryPage runs={boot.runs} onOpen={openHistory} />
              ) : page === 'quality' ? (
                <QualityPage />
              ) : (
                <DataPage />
              )}
              <footer className="main-footer">
                <span>
                  <BrandMark size={18} />
                  WindPulse AI <span>·</span> HackAlem AI
                </span>
                <span>Нормализованный индекс · не МВт и не МВт·ч</span>
              </footer>
            </>
          )}
        </main>
      </div>
      {notice && (
        <div className="toast" role="status">
          <CheckCircle2 size={18} />
          {notice}
          <button className="icon-button" onClick={() => setNotice('')} aria-label="Закрыть уведомление">
            <X size={15} />
          </button>
        </div>
      )}
    </div>
  )
}
