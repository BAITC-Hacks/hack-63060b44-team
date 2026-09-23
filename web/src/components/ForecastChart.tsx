import { useId, useMemo, useState } from 'react'
import { RotateCcw } from 'lucide-react'
import {
  Area,
  Brush,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  chartColors,
  ChartStyles,
  chartTooltipStyle,
  finiteValue,
  formatChartTime,
  TooltipValue,
} from './chartShared'

export interface ForecastChartPoint {
  target_time: string
  horizon: number
  turbine_1: number | null
  turbine_2: number | null
  farm_proxy: number | null
}

export interface ForecastHistoryPoint {
  target_time: string
  turbine_1: number | null
  turbine_2: number | null
  farm_proxy: number | null
}

export interface ForecastChartProps {
  points: ForecastChartPoint[]
  history?: ForecastHistoryPoint[]
  issueTime: string
  horizon: 24 | 48
  timezoneLabel?: string
  displayTimezone?: string
}

type Series = 'farm_proxy' | 'turbine_1' | 'turbine_2'
type Datum = ForecastHistoryPoint & {
  horizon: number | null
  actual: number | null
  kind: 'forecast' | 'history' | 'issue'
}
type Visibility = Record<Series, boolean>
const series: Array<{ key: Series; label: string }> = [
  { key: 'farm_proxy', label: 'Общий индекс' },
  { key: 'turbine_1', label: 'Турбина 1' },
  { key: 'turbine_2', label: 'Турбина 2' },
]

function ForecastTooltip({
  active,
  payload,
  visibility,
  timezoneLabel,
  displayTimezone,
}: {
  active?: boolean
  payload?: Array<{ payload?: Datum }>
  visibility: Visibility
  timezoneLabel: string
  displayTimezone: string
}) {
  const point = payload?.find((entry) => entry.payload)?.payload
  if (!active || !point || point.kind === 'issue') return null
  return (
    <div style={chartTooltipStyle}>
      <div style={{ fontWeight: 600 }}>{formatChartTime(point.target_time, displayTimezone, true)}</div>
      <div style={{ fontSize: 11, color: chartColors.muted, marginTop: 4 }}>
        UTC{timezoneLabel} · {point.kind === 'history' ? 'Факт до выпуска' : `Горизонт +${point.horizon} ч`}
      </div>
      <div style={{ marginTop: 10, paddingTop: 1, borderTop: '1px solid #2C3C51' }}>
        {point.kind === 'history' ? (
          <TooltipValue label="Факт общего индекса" value={point.actual} color={chartColors.actual} />
        ) : (
          series
            .filter((item) => visibility[item.key])
            .map((item) => (
              <TooltipValue
                key={item.key}
                label={item.label}
                value={point[item.key]}
                color={chartColors[item.key]}
              />
            ))
        )}
      </div>
      <div style={{ color: chartColors.muted, fontSize: 10, marginTop: 10 }}>Нормализованная шкала 0–1</div>
    </div>
  )
}

export function ForecastChart({
  points,
  history = [],
  issueTime,
  horizon,
  timezoneLabel = '+05:00',
  displayTimezone = 'Etc/GMT-5',
}: ForecastChartProps) {
  const uniqueId = useId().replace(/:/g, '')
  const gradientId = `forecast-fill-${uniqueId}`
  const descriptionId = `forecast-description-${uniqueId}`
  const [visibility, setVisibility] = useState<Visibility>({
    farm_proxy: true,
    turbine_1: true,
    turbine_2: true,
  })
  const [showHistory, setShowHistory] = useState(false)
  const [range, setRange] = useState<{ signature: string; start: number; end: number } | null>(null)
  const historyPoints = useMemo(() => {
    const issue = new Date(issueTime).valueOf()
    return history
      .filter((point) => {
        const time = new Date(point.target_time).valueOf()
        return time < issue && time >= issue - 24 * 3600_000
      })
      .sort((a, b) => new Date(a.target_time).valueOf() - new Date(b.target_time).valueOf())
  }, [history, issueTime])
  const hasHistory = historyPoints.some((point) => finiteValue(point.farm_proxy) !== null)
  const data = useMemo<Datum[]>(() => {
    const records = new Map<number, Datum>()
    if (showHistory && hasHistory) {
      historyPoints.forEach((point) =>
        records.set(new Date(point.target_time).valueOf(), {
          target_time: point.target_time,
          horizon: null,
          turbine_1: null,
          turbine_2: null,
          farm_proxy: null,
          actual: finiteValue(point.farm_proxy),
          kind: 'history',
        })
      )
      // An empty boundary point marks the issue without inventing a measurement.
      records.set(new Date(issueTime).valueOf(), {
        target_time: issueTime,
        horizon: null,
        turbine_1: null,
        turbine_2: null,
        farm_proxy: null,
        actual: null,
        kind: 'issue',
      })
    }
    points
      .filter((point) => point.horizon > 0 && point.horizon <= horizon)
      .forEach((point) =>
        records.set(new Date(point.target_time).valueOf(), {
          ...point,
          turbine_1: finiteValue(point.turbine_1),
          turbine_2: finiteValue(point.turbine_2),
          farm_proxy: finiteValue(point.farm_proxy),
          actual: null,
          kind: 'forecast',
        })
      )
    return [...records.entries()].sort(([a], [b]) => a - b).map(([, value]) => value)
  }, [points, horizon, showHistory, hasHistory, historyPoints, issueTime])
  const signature = `${issueTime}|${horizon}|${showHistory}|${data.length}|${data[0]?.target_time}|${data.at(-1)?.target_time}`
  const start = range?.signature === signature ? Math.min(range.start, Math.max(0, data.length - 1)) : 0
  const end =
    range?.signature === signature
      ? Math.min(range.end, Math.max(0, data.length - 1))
      : Math.max(0, data.length - 1)
  const isZoomed = start !== 0 || end !== data.length - 1
  const setVisibleRange = (nextStart: number, nextEnd: number) =>
    setRange({ signature, start: Math.max(0, nextStart), end: Math.min(data.length - 1, nextEnd) })
  const hasForecast = data.some(
    (point) => point.kind === 'forecast' && series.some((item) => point[item.key] !== null)
  )

  return (
    <div className="wp-chart-shell">
      <ChartStyles />
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          marginBottom: 13,
        }}
      >
        <div className="wp-chart-legend" aria-label="Показать линии прогноза">
          {series.map((item) => (
            <button
              key={item.key}
              type="button"
              aria-pressed={visibility[item.key]}
              onClick={() => setVisibility((current) => ({ ...current, [item.key]: !current[item.key] }))}
              style={{ opacity: visibility[item.key] ? 1 : 0.55 }}
            >
              <span
                aria-hidden="true"
                style={{
                  width: 17,
                  height: 3,
                  borderRadius: 3,
                  background: visibility[item.key] ? chartColors[item.key] : '#66758A',
                }}
              />
              <span style={{ textDecoration: visibility[item.key] ? 'none' : 'line-through' }}>
                {item.label}
              </span>
            </button>
          ))}
          {showHistory && hasHistory && (
            <span
              style={{ display: 'inline-flex', gap: 7, alignItems: 'center', fontSize: 12, color: '#C7D4E5' }}
            >
              <span aria-hidden="true" style={{ width: 17, borderTop: `2px dashed ${chartColors.actual}` }} />
              Факт индекса
            </span>
          )}
        </div>
        <span style={{ color: chartColors.muted, fontSize: 10, letterSpacing: '.04em' }}>
          ШКАЛА 0–1 · UTC{timezoneLabel}
        </span>
      </div>
      {!hasForecast ? (
        <div className="wp-chart-empty">Нет данных прогноза для выбранного выпуска.</div>
      ) : (
        <>
          <p
            id={descriptionId}
            style={{
              position: 'absolute',
              width: 1,
              height: 1,
              padding: 0,
              overflow: 'hidden',
              clipPath: 'inset(50%)',
              whiteSpace: 'nowrap',
            }}
          >
            Прогноз на {horizon} часов, нормализованная шкала от 0 до 1. Для просмотра точных значений
            сфокусируйте график и используйте стрелки влево и вправо. Масштаб меняется ползунками под графиком
            или полями начала и конца периода.
          </p>
          <div className="wp-chart-main" aria-describedby={descriptionId}>
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <ComposedChart
                data={data}
                margin={{ top: 14, right: 12, bottom: 5, left: -13 }}
                accessibilityLayer
              >
                <defs>
                  <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={chartColors.farm_proxy} stopOpacity={0.18} />
                    <stop offset="100%" stopColor={chartColors.farm_proxy} stopOpacity={0.01} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#26334A" strokeDasharray="3 6" vertical={false} strokeOpacity={0.7} />
                <XAxis
                  dataKey="target_time"
                  axisLine={false}
                  tickLine={false}
                  tick={{ fill: chartColors.muted, fontSize: 10 }}
                  tickMargin={11}
                  minTickGap={46}
                  tickFormatter={(value) => formatChartTime(String(value), displayTimezone)}
                />
                <YAxis
                  domain={[0, 1]}
                  ticks={[0, 0.25, 0.5, 0.75, 1]}
                  axisLine={false}
                  tickLine={false}
                  tick={{ fill: chartColors.muted, fontSize: 10 }}
                  tickFormatter={(value) =>
                    Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 })
                  }
                  width={47}
                />
                <Tooltip
                  content={
                    <ForecastTooltip
                      visibility={visibility}
                      timezoneLabel={timezoneLabel}
                      displayTimezone={displayTimezone}
                    />
                  }
                  cursor={{ stroke: '#60748F', strokeDasharray: '3 4' }}
                  isAnimationActive={false}
                />
                {showHistory && hasHistory && (
                  <ReferenceLine
                    x={issueTime}
                    stroke="#A5B7CB"
                    strokeDasharray="4 5"
                    label={{ value: 'Выпуск', position: 'insideTopRight', fill: '#C0D0E4', fontSize: 10 }}
                  />
                )}
                <Area
                  type="linear"
                  dataKey="farm_proxy"
                  name="Общий индекс"
                  stroke={chartColors.farm_proxy}
                  strokeWidth={2.5}
                  fill={`url(#${gradientId})`}
                  hide={!visibility.farm_proxy}
                  dot={false}
                  activeDot={{ r: 4, fill: chartColors.farm_proxy, stroke: '#101827', strokeWidth: 2 }}
                  connectNulls={false}
                  isAnimationActive={false}
                />
                <Line
                  type="linear"
                  dataKey="turbine_1"
                  name="Турбина 1"
                  stroke={chartColors.turbine_1}
                  strokeWidth={1.75}
                  hide={!visibility.turbine_1}
                  dot={false}
                  activeDot={{ r: 3, stroke: '#101827', strokeWidth: 2 }}
                  connectNulls={false}
                  isAnimationActive={false}
                />
                <Line
                  type="linear"
                  dataKey="turbine_2"
                  name="Турбина 2"
                  stroke={chartColors.turbine_2}
                  strokeWidth={1.75}
                  hide={!visibility.turbine_2}
                  dot={false}
                  activeDot={{ r: 3, stroke: '#101827', strokeWidth: 2 }}
                  connectNulls={false}
                  isAnimationActive={false}
                />
                {showHistory && hasHistory && (
                  <Line
                    type="linear"
                    dataKey="actual"
                    name="Факт общего индекса"
                    stroke={chartColors.actual}
                    strokeWidth={1.75}
                    strokeDasharray="5 4"
                    dot={false}
                    activeDot={{ r: 3 }}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                )}
                <Brush
                  dataKey="target_time"
                  height={20}
                  travellerWidth={7}
                  stroke="#405774"
                  fill="#0B1423"
                  startIndex={start}
                  endIndex={end}
                  tickFormatter={(value) => formatChartTime(String(value), displayTimezone)}
                  onChange={(next) => {
                    if (typeof next.startIndex === 'number' && typeof next.endIndex === 'number')
                      setVisibleRange(next.startIndex, next.endIndex)
                  }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
          <div className="wp-chart-controls">
            <label
              style={{
                display: 'inline-flex',
                gap: 7,
                alignItems: 'center',
                color: hasHistory ? '#BDCBDC' : chartColors.muted,
                fontSize: 11,
                cursor: hasHistory ? 'pointer' : 'default',
              }}
              title={
                hasHistory
                  ? 'Показать доступный факт общего индекса за 24 часа до выпуска'
                  : 'За 24 часа до выпуска нет фактических измерений'
              }
            >
              <input
                type="checkbox"
                checked={showHistory && hasHistory}
                disabled={!hasHistory}
                onChange={(event) => setShowHistory(event.target.checked)}
                style={{ accentColor: chartColors.farm_proxy, margin: 0, width: 14, height: 14 }}
              />
              {hasHistory ? 'История: 24 ч до выпуска' : 'История за 24 ч недоступна'}
            </label>
            <div className="wp-chart-range">
              <label>
                От{' '}
                <select
                  aria-label="Начало периода на графике"
                  value={start}
                  onChange={(event) => setVisibleRange(Number(event.target.value), end)}
                >
                  {data.map((point, index) => (
                    <option key={point.target_time} value={index} disabled={index > end}>
                      {formatChartTime(point.target_time, displayTimezone)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                До{' '}
                <select
                  aria-label="Конец периода на графике"
                  value={end}
                  onChange={(event) => setVisibleRange(start, Number(event.target.value))}
                >
                  {data.map((point, index) => (
                    <option key={point.target_time} value={index} disabled={index < start}>
                      {formatChartTime(point.target_time, displayTimezone)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="wp-chart-reset"
                disabled={!isZoomed}
                onClick={() => setRange(null)}
                aria-label="Сбросить масштаб графика"
              >
                <RotateCcw size={12} aria-hidden="true" />
                Сбросить
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

export default ForecastChart
