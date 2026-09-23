import { useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
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

export interface QualityChartPoint {
  target_time: string
  prediction: number | null
  actual: number | null
}

export interface QualityChartProps {
  points: QualityChartPoint[]
  modelLabel?: string
  issueTime?: string
}

function QualityTooltip({
  active,
  payload,
  modelLabel,
  visible,
}: {
  active?: boolean
  payload?: Array<{ payload?: QualityChartPoint }>
  modelLabel: string
  visible: { prediction: boolean; actual: boolean }
}) {
  const point = payload?.find((entry) => entry.payload)?.payload
  if (!active || !point) return null
  return (
    <div style={chartTooltipStyle}>
      <div style={{ fontWeight: 600 }}>{formatChartTime(point.target_time, 'Etc/GMT-5', true)}</div>
      <div style={{ fontSize: 11, color: chartColors.muted, marginTop: 4 }}>
        UTC+05:00 · нормализованная шкала
      </div>
      <div style={{ marginTop: 10, paddingTop: 1, borderTop: '1px solid #2C3C51' }}>
        {visible.prediction && (
          <TooltipValue label={modelLabel} value={point.prediction} color={chartColors.farm_proxy} />
        )}
        {visible.actual && <TooltipValue label="Факт" value={point.actual} color={chartColors.actual} />}
      </div>
    </div>
  )
}

export function QualityChart({ points, modelLabel = 'Прогноз', issueTime }: QualityChartProps) {
  const [visible, setVisible] = useState({ prediction: true, actual: true })
  const data = points
    .map((point) => ({
      ...point,
      prediction: finiteValue(point.prediction),
      actual: finiteValue(point.actual),
    }))
    .sort((a, b) => new Date(a.target_time).valueOf() - new Date(b.target_time).valueOf())
  const hasData = data.some((point) => point.prediction !== null || point.actual !== null)
  const hasActual = data.some((point) => point.actual !== null)
  return (
    <div className="wp-chart-shell">
      <ChartStyles />
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 12,
          marginBottom: 14,
        }}
      >
        <div className="wp-chart-legend" aria-label="Линии контрольной проверки">
          <button
            type="button"
            aria-pressed={visible.prediction}
            onClick={() => setVisible((current) => ({ ...current, prediction: !current.prediction }))}
            style={{ opacity: visible.prediction ? 1 : 0.55 }}
          >
            <span
              aria-hidden="true"
              style={{ width: 17, height: 3, borderRadius: 3, background: chartColors.farm_proxy }}
            />
            <span style={{ textDecoration: visible.prediction ? 'none' : 'line-through' }}>{modelLabel}</span>
          </button>
          <button
            type="button"
            aria-pressed={visible.actual}
            onClick={() => setVisible((current) => ({ ...current, actual: !current.actual }))}
            style={{ opacity: visible.actual ? 1 : 0.55 }}
          >
            <span aria-hidden="true" style={{ width: 17, borderTop: `2px dashed ${chartColors.actual}` }} />
            <span style={{ textDecoration: visible.actual ? 'none' : 'line-through' }}>Факт</span>
          </button>
        </div>
        <span style={{ color: chartColors.muted, fontSize: 10, letterSpacing: '.04em' }}>
          ШКАЛА 0–1 · UTC+05:00
        </span>
      </div>
      {!hasData ? (
        <div className="wp-chart-empty">Для выбранной модели и периода нет сохранённых значений.</div>
      ) : (
        <>
          <div style={{ height: 300, width: '100%', minWidth: 0 }}>
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <LineChart data={data} margin={{ top: 14, right: 14, bottom: 8, left: -13 }} accessibilityLayer>
                <CartesianGrid stroke="#26334A" strokeDasharray="3 6" vertical={false} strokeOpacity={0.7} />
                <XAxis
                  dataKey="target_time"
                  axisLine={false}
                  tickLine={false}
                  tick={{ fill: chartColors.muted, fontSize: 10 }}
                  tickMargin={11}
                  minTickGap={50}
                  tickFormatter={(value) => formatChartTime(String(value))}
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
                  content={<QualityTooltip modelLabel={modelLabel} visible={visible} />}
                  cursor={{ stroke: '#60748F', strokeDasharray: '3 4' }}
                  isAnimationActive={false}
                />
                {issueTime && data.some((point) => point.target_time === issueTime) && (
                  <ReferenceLine
                    x={issueTime}
                    stroke="#A5B7CB"
                    strokeDasharray="4 5"
                    label={{ value: 'Выпуск', position: 'insideTopRight', fill: '#C0D0E4', fontSize: 10 }}
                  />
                )}
                <Line
                  type="linear"
                  dataKey="actual"
                  name="Факт"
                  stroke={chartColors.actual}
                  strokeWidth={1.8}
                  strokeDasharray="5 4"
                  hide={!visible.actual}
                  dot={false}
                  activeDot={{ r: 3, strokeWidth: 2, stroke: '#101827' }}
                  connectNulls={false}
                  isAnimationActive={false}
                />
                <Line
                  type="linear"
                  dataKey="prediction"
                  name={modelLabel}
                  stroke={chartColors.farm_proxy}
                  strokeWidth={2.4}
                  hide={!visible.prediction}
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: '#101827' }}
                  connectNulls={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
          {!hasActual && (
            <p style={{ color: '#E7B76F', fontSize: 12, margin: '8px 0 0' }}>
              Фактические значения для выбранного периода не предоставлены.
            </p>
          )}
        </>
      )}
    </div>
  )
}

export default QualityChart
