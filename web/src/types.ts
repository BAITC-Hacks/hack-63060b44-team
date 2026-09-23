export type TurbineId = 'turbine_1' | 'turbine_2'
export type Entity = TurbineId | 'farm_proxy'
export type Nullable = number | null
export type Stage = {
  id: string
  label: string
  status: 'completed' | 'running' | 'pending' | 'error'
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  evidence: string
}
export type RunSummary = {
  run_id: string
  issue_time: string
  horizon: number
  model: string
  status: string
  stale_hours: Record<TurbineId, Nullable>
  last_observed_hour: Record<TurbineId, string | null>
  created_at: string
  is_replay: boolean
  collection: string
  csv_url: string
}
export type ForecastPoint = {
  target_time: string
  horizon: number
  turbine_1: Nullable
  turbine_2: Nullable
  farm_proxy: Nullable
}
export type WeatherPoint = {
  target_time: string
  wind_speed: Nullable
  temperature: Nullable
  weather_run_time: string
  weather_available_at: string
  weather_source: string
}
export type TurbineConfig = {
  latitude: number
  longitude: number
  map_source?: string
  rated_power_mw?: Nullable
}
export type WeatherMeta = {
  provider?: string
  grid?: string
  run_time?: string
  available_at?: string
  native_step_hours?: number
  nearest_grid_point?: { grid_latitude?: number; grid_longitude?: number; distance_km?: number }
  interpolation?: string
}
export type Provenance = {
  run_id: string
  source_version: string
  hourly_sha256: string
  forecast_sha256: string
  model_details: Record<string, unknown>
  checks: Record<string, unknown>
  model_selection: Record<string, unknown>
  assumptions: string[]
  input_quality: Record<string, unknown>
}
export type RunDetail = {
  summary: RunSummary
  forecast: ForecastPoint[]
  actual_history: Omit<ForecastPoint, 'horizon'>[]
  weather: Record<TurbineId, WeatherPoint[]>
  weather_metadata: Record<TurbineId, WeatherMeta>
  stages: Stage[]
  events: Record<string, unknown>[]
  provenance: Provenance
  turbines: Record<TurbineId, TurbineConfig>
}
export type Job = {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'interrupted'
  issue_time: string
  horizon: number
  model: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  run_id: string | null
  error: { message: string; action: string; detail?: string } | null
  stages: Stage[]
  events: Record<string, unknown>[]
}
export type AuditTurbine = {
  rows: number
  start_local: string
  end_local: string
  missing_ten_minute_slots: number
  expected_ten_minute_slots: number
  valid_hourly_targets: number
  longest_gap_hours: number
  february_2026_rows: number
}
export type Audit = {
  turbines: Record<TurbineId, AuditTurbine>
  alignment: {
    shared_timestamps: number
    only_turbine_1: number
    only_turbine_2: number
    valid_farm_proxy_hours: number
    hourly_slots: number
  }
  timezone_assumption: string
}
export type Bootstrap = {
  config: {
    timezone: string
    issue_hour_local: number
    target_min: number
    target_max: number
    min_hourly_samples: number
    turbines: Record<TurbineId, TurbineConfig>
    weather: Record<string, unknown>
    models: string[]
  }
  audit: Audit | null
  runs: RunSummary[]
  default_run_id: string | null
  availability: {
    saved_runs: number
    forecast_enabled: boolean
    raw_inputs_present: boolean
    weather_cache_present: boolean
    mode: string
    message: string
  }
  active_job: Job | null
}
export type Metric = {
  model: string
  entity: Entity
  horizon: string
  n: number
  mae: Nullable
  rmse: Nullable
  bias: Nullable
  coverage: Nullable
}
export type QualityPoint = {
  issue_time: string
  target_time: string
  model: string
  entity: Entity
  horizon: number
  prediction: Nullable
  actual: Nullable
}
export type Dataset = {
  id: string
  label: string
  role: 'selection' | 'control' | 'sensitivity'
  first_issue: string
  last_issue: string
  n_folds: number
  selected_model: string
  metrics: Metric[]
  predictions: QualityPoint[]
  note: string
}
export type Quality = {
  datasets: Dataset[]
  february_notice: string
  control_selection: Record<string, unknown>
}
export type DataInfo = {
  audit: Audit | null
  assumptions: string[]
  questions: string[]
  timezone: string
  aggregation_rule: string
  farm_definition: string
  weather_limitations: string[]
  february_notice: string
}
