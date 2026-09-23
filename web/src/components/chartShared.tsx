import type { CSSProperties } from 'react'

export const chartColors = {
  farm_proxy: '#29F0D0',
  turbine_1: '#4385FF',
  turbine_2: '#A68BFA',
  actual: '#B6C4D7',
  text: '#EDF3FF',
  muted: '#98A8BD',
}

export const chartNumber = (value: number | null | undefined) =>
  typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('ru-RU', { minimumFractionDigits: 3, maximumFractionDigits: 3 })
    : 'Нет данных'

export const finiteValue = (value: number | null | undefined) =>
  typeof value === 'number' && Number.isFinite(value) ? value : null

export function formatChartTime(value: string, timeZone = 'Etc/GMT-5', detailed = false) {
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return value
  return new Intl.DateTimeFormat('ru-RU', {
    timeZone,
    day: '2-digit',
    month: detailed ? 'long' : '2-digit',
    ...(detailed ? { year: 'numeric' as const } : {}),
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(date)
}

export const chartTooltipStyle: CSSProperties = {
  background: '#111E30',
  border: '1px solid #33455F',
  borderRadius: 12,
  padding: '12px 14px',
  boxShadow: '0 12px 32px #0007',
  minWidth: 208,
  color: chartColors.text,
  fontSize: 12,
  fontVariantNumeric: 'tabular-nums',
}

export function ChartStyles() {
  return (
    <style>{`
    .wp-chart-shell { min-width: 0; width: 100%; font-variant-numeric: tabular-nums; }
    .wp-chart-shell button, .wp-chart-shell select, .wp-chart-shell input { font: inherit; }
    .wp-chart-shell button:focus-visible, .wp-chart-shell select:focus-visible,
    .wp-chart-shell input:focus-visible, .wp-chart-shell [tabindex]:focus-visible {
      outline: 2px solid #29F0D0; outline-offset: 4px;
    }
    .wp-chart-shell .recharts-surface:focus-visible { outline: 2px solid #29F0D0; outline-offset: -2px; }
    .wp-chart-legend { display: flex; flex-wrap: wrap; gap: 6px 15px; align-items: center; }
    .wp-chart-legend button { display: inline-flex; align-items: center; gap: 7px; border: 0; padding: 5px 0; background: none; cursor: pointer; font-size: 12px; color: #C7D4E5; }
    .wp-chart-controls { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-top: 9px; }
    .wp-chart-range { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; color: #98A8BD; font-size: 11px; }
    .wp-chart-range label { display: inline-flex; align-items: center; gap: 6px; }
    .wp-chart-range select { background: #0B1321; border: 1px solid #29384D; color: #C7D4E5; border-radius: 6px; padding: 5px; max-width: 160px; min-height: 30px; }
    .wp-chart-reset { display: inline-flex; align-items: center; gap: 5px; border: 0; color: #B3C5DA; background: transparent; cursor: pointer; font-size: 11px; padding: 6px; }
    .wp-chart-reset:disabled { color: #6E8097; cursor: default; }
    .wp-chart-empty { display: grid; place-content: center; min-height: 230px; text-align: center; color: #98A8BD; font-size: 13px; padding: 20px; }
    .wp-chart-main { height: clamp(280px, 24vw, 340px); width: 100%; min-width: 0; }
    .wp-chart-shell .recharts-brush-texts { fill: #B6C4D7; }
    @media (max-width: 600px) {
      .wp-chart-main { height: 275px; }
      .wp-chart-range { width: 100%; }
      .wp-chart-range select { max-width: 126px; }
      .wp-chart-legend { gap: 4px 12px; }
    }
    @media (prefers-reduced-motion: reduce) { .wp-chart-shell * { animation: none !important; transition: none !important; } }
  `}</style>
  )
}

export function TooltipValue({
  label,
  value,
  color,
}: {
  label: string
  value: number | null | undefined
  color: string
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 20,
        marginTop: 9,
      }}
    >
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, color: '#BDCBDC' }}>
        <span aria-hidden="true" style={{ height: 6, width: 6, borderRadius: '50%', background: color }} />
        {label}
      </span>
      <strong style={{ color: chartColors.text, fontWeight: 600 }}>{chartNumber(value)}</strong>
    </div>
  )
}
