import { useId } from 'react'
import { Area, AreaChart, ResponsiveContainer, YAxis } from 'recharts'
import { finiteValue } from './chartShared'

export interface MiniChartProps {
  points: Array<number | null>
  color: string
  height?: number
}

/** A compact view of the same forecast: missing measurements remain gaps. */
export function MiniChart({ points, color, height = 64 }: MiniChartProps) {
  const id = `mini-${useId().replace(/:/g, '')}`
  const data = points.map((value, index) => ({ index, value: finiteValue(value) }))
  if (!data.some((point) => point.value !== null)) {
    return (
      <div style={{ height, display: 'grid', placeItems: 'center', color: '#98A8BD', fontSize: 12 }}>
        Нет данных
      </div>
    )
  }
  return (
    <div style={{ width: '100%', minWidth: 0, height }} aria-hidden="true">
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <AreaChart data={data} margin={{ top: 6, right: 1, bottom: 2, left: 1 }} accessibilityLayer={false}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.2} />
              <stop offset="100%" stopColor={color} stopOpacity={0.015} />
            </linearGradient>
          </defs>
          <YAxis hide domain={[0, 1]} />
          <Area
            type="linear"
            dataKey="value"
            stroke={color}
            strokeWidth={2}
            fill={`url(#${id})`}
            connectNulls={false}
            dot={false}
            activeDot={false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

export default MiniChart
