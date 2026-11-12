// -*- coding: utf-8 -*-
/**
 * AgriMind 农业智能数据类型契约
 */

// ==================== 农场、地块与作物种植 ====================
export type FacilityType = 'unknown' | 'open_field' | 'greenhouse' | 'tunnel' | 'other'
export interface ArchivePage<T> { items: T[]; next_before_id: number | null; snapshot_id: number }
export interface CropStatusEvent {
  id: number; crop_planting_id: number; previous_status: string; new_status: string
  reason: string | null; actor_name: string; created_at: string
}
export interface EnvironmentHistoryItem {
  id: number; facility_type: FacilityType; measurement_scope: string; observed_at: string; recorded_at: string
  temperature: number | null; humidity: number | null; ventilation: string; covering: string
  notes: string | null; observation_status: string; source: string
}
export interface EnvironmentObservationCreate {
  request_id: string
  facility_type: FacilityType
  measurement_scope: 'onsite_outdoor' | 'inside_facility'
  observed_at: string
  temperature?: number | null
  humidity?: number | null
  ventilation: 'unknown' | 'open' | 'closed' | 'partial' | 'not_applicable'
  covering: 'unknown' | 'covered' | 'uncovered' | 'partial' | 'not_applicable'
  notes?: string
}
export interface FieldEnvironment {
  facility_type: FacilityType
  facility_label: string
  outdoor_weather_applicable: boolean
  observation_status: 'missing' | 'context_changed' | 'scope_mismatch' | 'future' | 'stale' | 'recent'
  observation_max_age_minutes: number
  latest_observation: (Omit<EnvironmentObservationCreate, 'request_id'> & { id: number; recorded_at: string; source: string }) | null
  notice: string
  can_approve_operation: boolean
}

export interface Farm {
  archived_at?: string | null
  id: number
  name: string
  description?: string
  province?: string
  city?: string
  district?: string
  address?: string
  region_code?: string
  location_source?: 'manual' | 'region_reference'
  location_label?: string
  latitude?: number
  longitude?: number
  area: number
  owner_id: number
  created_at?: string
  updated_at?: string
}

export interface FarmCreate {
  name: string
  description?: string
  province?: string
  city?: string
  district?: string
  address?: string
  region_code?: string
  location_source?: 'manual' | 'region_reference'
  location_label?: string
  latitude?: number
  longitude?: number
  area: number
}

export interface FieldItem {
  facility_type?: FacilityType
  archived_at?: string | null
  id: number
  farm_id: number
  name: string
  area: number
  soil_type?: string
  irrigation_type?: string
  latitude?: number
  longitude?: number
  remark?: string
  created_at?: string
  updated_at?: string
  // 前端辅助绑定
  activeCrop?: CropPlanting
  cropOptions?: CropPlanting[]
}

export interface FieldCreate {
  facility_type?: FacilityType
  name: string
  area: number
  soil_type?: string
  irrigation_type?: string
  latitude?: number
  longitude?: number
  remark?: string
}

export interface CropPlanting {
  id: number
  field_id: number
  crop: string
  variety?: string
  planting_date: string
  expected_harvest_date?: string
  growth_stage: string
  area: number
  status: string
  created_at?: string
  updated_at?: string
}

export interface CropPlantingCreate {
  crop: string
  variety?: string
  planting_date: string
  expected_harvest_date?: string
  growth_stage?: string
  area?: number
  status?: string
}

export interface FarmingRecord {
  replayed?: boolean
  request_id?: string | null
  revoked_at?: string | null
  revoke_reason?: string | null
  revoked_by_name?: string | null
  id: number
  field_id: number
  crop_planting_id?: number
  record_type: 'fertilization' | 'spraying' | 'irrigation' | 'pruning' | 'weeding' | 'harvest' | 'inspection' | 'other'
  operation_date: string
  operation_content: string
  chemical_name?: string
  dosage?: string
  dilution_ratio?: string
  safety_interval_days?: number
  operator?: string
  notes?: string
  created_at?: string
  can_delete?: boolean
  delete_deadline_at?: string | null
}

export interface FarmingRecordCreate {
  request_id?: string
  field_id: number
  crop_planting_id?: number
  record_type: string
  operation_date: string
  operation_content: string
  chemical_name?: string
  dosage?: string
  dilution_ratio?: string
  safety_interval_days?: number
  operator?: string
  notes?: string
}

// ==================== 农业决策 Agent ====================

export interface ToolTrace {
  tool_name: string
  arguments: Record<string, any>
  result_summary: any
  execution_metadata?: Record<string, any>
  status: string
}

export interface AgentDecisionRequest {
  conversation_id?: string
  request_id?: string
  query: string
  field_id?: number
  crop_planting_id?: number
  image_url?: string
  location?: string
  enable_multimodal?: boolean
}

export interface AgentDecisionResponse {
  conversation_id?: string | null
  turn_id?: string | null
  consultation_id?: string | null
  query: string
  conclusion: string
  decision_type: string
  /** 模型自评分并按证据设上限，不是准确率；界面展示 evidence_level */
  confidence: number
  evidence_level?: 'knowledge' | 'records_only' | 'none'
  can_proceed: boolean
  risks: string[]
  recommendations: string[]
  evidences: Array<Record<string, any>>
  tool_traces: ToolTrace[]
  need_expert: boolean
  expert_reason?: string
}

export interface ToolDefinition {
  name: string
  description: string
  parameters: Record<string, any>
  category: string
}

// ==================== 多模态初诊与专家工单 ====================

export interface VisualDiagnosisRequest {
  request_id?: string
  image_url: string
  field_id?: number
  crop_planting_id?: number
  crop?: string
  symptoms_hint?: string
  auto_create_ticket?: boolean
}

export interface VisualDiagnosisResponse {
  diagnosis_status?: 'completed' | 'failed'
  error_code?: string | null
  error_message?: string | null
  request_id?: string | null
  replayed?: boolean
  processed_at?: string | null
  image_url: string
  crop: string
  suspected_disease: string
  confidence: number | null
  features_detected: string[]
  severity: string
  need_expert: boolean
  disease_record_id?: number | null
  ticket_id?: number | null
  advice: string
  standard_treatment?: string
}

export interface ExpertFeedback {
  confirmed_disease?: string | null
  treatment_suggestion: string
  notes?: string | null
  expert_case_id?: number | null
  expert_name?: string | null
  resolved_at?: string | null
  workflow_verified: boolean
}

export interface ExpertTicketItem {
  claim_available: boolean
  feedback?: ExpertFeedback | null
  diagnosis_method: string
  ticket_id: number
  disease_record_id: number
  field_id: number
  field_name?: string
  farm_name?: string
  region?: string
  crop: string
  variety?: string
  growth_stage: string
  image_url?: string
  initial_suspected_disease: string
  initial_confidence?: number
  symptoms?: string
  status: string // 'pending_review' | 'in_review' | 'expert_reviewed'
  expert_id?: number
  expert_name?: string
  created_at?: string
  updated_at?: string
}

export interface ExpertTicketDetail extends ExpertTicketItem {
  field_soil_type?: string
  field_area?: number
  current_weather?: Record<string, any>
  recent_farming_records: Array<Record<string, any>>
  ai_features: string[]
}

export interface ExpertResolveRequest {
  confirmed_disease: string
  treatment_suggestion: string
  notes?: string
  is_knowledge_candidate: number
}

export interface ExpertResolveResponse {
  ticket_id: number
  status: string
  confirmed_disease: string
  treatment_suggestion: string
  expert_case_id: number
  knowledge_item_id?: string
  feedback_message: string
}

export interface ImageUploadResponse {
  filename: string
  url: string
  size: number
  content_type: string
}

// ==================== 智能农事计划与日历 ====================

export interface PlanItem {
  execution_status?: 'pending' | 'completed' | 'revoked' | 'unavailable'
  execution_record_id?: number | null
  date: string
  record_type: string
  action: string
  description: string
  reason: string
  source: string
  risk_notes?: string
  is_spray: boolean
  can_proceed: boolean
  reason_code: string
  crop_planting_id?: number | null
  proof?: string | null
}

export interface AgriPlanRequest {
  field_id: number
  crop_planting_id?: number
  days?: number
  focus_topic?: string
  force_refresh?: boolean
}

export interface AgriPlanResponse {
  field_id: number
  field_name: string
  crop: string
  growth_stage: string
  start_date: string
  end_date: string
  summary: string
  items: PlanItem[]
  weather_summary: string
  total_tasks: number
  version?: number
  generated_at?: string | null
  evaluated_at?: string | null
  weather_fetched_at?: string | null
  weather_observed_at?: string | null
  update_reason?: string
  changes?: string[]
}

export interface AgriPlanApplyRequest {
  field_id: number
  items: PlanItem[]
  operator?: string
}

export interface AgriPlanApplyResponse {
  success: boolean
  created_count: number
  record_ids: number[]
  message: string
}

export interface WeatherSnapshot {
  id?: number
  field_id?: number
  temperature?: number | null
  humidity?: number | null
  rainfall?: number | null
  wind_speed?: number | null
  weather?: string
  timestamp?: string
  source?: string
  wind_direction?: string
  weather_condition?: string
  recorded_at?: string
  is_fallback?: boolean
}

export interface DailyWeather {
  date: string
  weather: string
  temp_max: number | null
  temp_min: number | null
  rainfall_mm: number | null
  rain_prob: number | null
  wind_speed: number | null
}

export interface FieldWeather {
  environment?: FieldEnvironment | null
  field_id: number
  status: 'online' | 'partial' | 'offline' | 'location_missing' | 'unsupported_provider'
  is_fallback: boolean
  latitude: number | null
  longitude: number | null
  coordinate_source: string | null
  source: string
  fetched_at: string | null
  summary: string
  current: WeatherSnapshot
  forecasts: DailyWeather[]
  spray_safety: { is_suitable_to_spray: boolean; spray_risks: string[]; is_offline_warning: boolean }
}

