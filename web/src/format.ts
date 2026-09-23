export const DISPLAY_TIMEZONE = 'Etc/GMT-5'
export const formatNumber = (value: number | null | undefined, digits = 3) =>
  value == null || !Number.isFinite(value)
    ? 'Нет данных'
    : new Intl.NumberFormat('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(
        value
      )
export const formatDate = (value: string | null | undefined, short = false) =>
  !value
    ? 'Нет данных'
    : new Intl.DateTimeFormat('ru-RU', {
        timeZone: DISPLAY_TIMEZONE,
        day: '2-digit',
        month: short ? 'short' : '2-digit',
        ...(short ? {} : { year: 'numeric' }),
        hour: '2-digit',
        minute: '2-digit',
      }).format(new Date(value))
export const dateOnly = (value: string) =>
  new Intl.DateTimeFormat('ru-RU', {
    timeZone: DISPLAY_TIMEZONE,
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  }).format(new Date(value))
export function inputDate(value: string) {
  return new Date(new Date(value).getTime() + 5 * 3600000).toISOString().slice(0, 16)
}
export const issueFromInput = (value: string) => `${value}:00+05:00`
export const mean = (values: (number | null | undefined)[]) => {
  const valid = values.filter((v): v is number => v != null && Number.isFinite(v))
  return valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : null
}
export const modelLabel = (model: string) =>
  ({
    extra_trees_weather: 'ExtraTrees + погода',
    extra_trees: 'ExtraTrees',
    persistence: 'Persistence',
    auto: 'Выбор по валидации',
  })[model] ?? model
