export interface AgriOverview {
  total_farms: number
  total_fields: number
  active_crop_plantings: number
  total_records: number
  total_conversations: number
  current_knowledge_items: number
  total_disease_records: number
  pending_disease_cases: number
  period_turns: number
  completed_turns: number
  failed_turns: number
  pending_turns: number
  stale_pending_turns: number
  completion_rate: number | null
  avg_response_time_ms: number | null
  response_time_sample_count: number
  period_consultations: number
  pending_consultations: number
  in_review_consultations: number
  resolved_consultations: number
}
export interface AgriDailyStats {
  date: string
  turns: number
  completed: number
  failed: number
  pending: number
  conversations: number
  farming_records: number
  consultations_created: number
  consultations_resolved: number
}
export interface AgriStatisticsSnapshot {
  schema_version: 'agriculture-v1'
  scope: 'current_agricultural_records'
  timezone: 'Asia/Shanghai'
  generated_at: string
  start_date: string
  end_date: string
  overview: AgriOverview
  daily: AgriDailyStats[]
  categories: { knowledge: Record<string, number>; active_crops: Record<string, number>; decisions: Record<string, number>; farming_records: Record<string, number> }
  top_questions: { question: string; count: number }[]
}
