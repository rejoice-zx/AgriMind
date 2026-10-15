// User types
// 字段名与后端 UserInfo（backend/app/schemas/auth.py）逐一对应，保持 snake_case。
export interface User {
  id: number
  username: string
  role: 'admin' | 'cs' | 'customer' | 'expert'
  display_name?: string
  email?: string
  phone?: string | null
  registration_status?: 'pending' | 'approved' | 'rejected' | null
  registration_reviewed_at?: string | null
  is_active: boolean
  created_at?: string
  last_login?: string
}

// Auth types
export interface LoginRequest {
  username: string
  password: string
  visitor_id?: string
  guest_device_id?: string
  guest_token?: string
  guest_conversation_ids?: string[]
}

export interface LoginResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user: User
}

export interface RegisterRequest {
  username: string
  password: string
  display_name: string
  phone: string
  role?: 'customer' | 'expert'
  email?: string
}

export interface CustomerRegisterRequest {
  username: string
  password: string
  display_name: string
  phone: string
  role?: 'customer' | 'expert'
  visitor_id?: string
  guest_device_id?: string
  guest_token?: string
  guest_conversation_ids?: string[]
}

export interface RegistrationPendingResponse {
  status: 'pending'
  message: string
}

// Knowledge types
export interface KnowledgeItem {
  id: string
  question: string
  answer: string
  keywords: string[]
  category: string
  score: number
  crop?: string | null
  region?: string | null
  growth_stage?: string | null
  topic?: string | null
  disease?: string | null
  source_type?: string | null
  source_name?: string | null
  authority_level?: string | null
  version?: string | null
  publish_date?: string | null
  url?: string | null
  source_updated_at?: string | null
  effective_from?: string | null
  effective_to?: string | null
  is_current?: 0 | 1 | null
  meta_info?: Record<string, unknown> | null
  created_at?: string
  updated_at?: string
}

export interface CreateKnowledgeRequest {
  question: string
  answer: string
  keywords: string[]
  category: string
  crop?: string
  region?: string
  growth_stage?: string
  topic?: string
  disease?: string
  source_type?: string
  source_name?: string
  authority_level?: string
  version?: string
  publish_date?: string
  url?: string
  source_updated_at?: string
  effective_from?: string
  effective_to?: string
  is_current?: 0 | 1
}

export interface UpdateKnowledgeRequest {
  question?: string
  answer?: string
  keywords?: string[]
  category?: string
  crop?: string
  region?: string
  growth_stage?: string
  topic?: string
  disease?: string
  source_type?: string
  source_name?: string
  authority_level?: string
  version?: string
  publish_date?: string
  url?: string
  source_updated_at?: string
  effective_from?: string
  effective_to?: string
  is_current?: 0 | 1
}

// 与后端 IndexStatusResponse（backend/app/schemas/knowledge.py）对齐。
export interface IndexStatus {
  count: number
  dimension: number
  embedding_model: string | null
  embedding_available: boolean
  embedding_dimension?: number | null
  needs_rebuild?: boolean
  rebuild_reason?: string | null
  index_generation?: string | null
  index_contract?: Record<string, unknown> | null
  current_contract?: Record<string, unknown> | null
  vector_compatible?: boolean
}

// Performance types
export interface PerformanceSummary {
  uptime_seconds: number
  uptime_formatted: string
  total_requests: number
  overall_success_rate: number | null
  start_time: string
  cleared_at: string | null
  degraded_requests: number
  process_id: number
  retained_samples_per_metric: number
  storage_scope: 'current_process'
  metric_scope: 'agricultural_http_requests'
}

export interface MetricStatsData {
  name: string
  count: number
  success_rate: number | null
  avg_duration: number
  min_duration: number
  max_duration: number
  p50_duration: number
  p95_duration: number
  p99_duration: number
  total_count: number
  total_success: number
  outcome_counts: Record<string, number>
  total_outcome_counts: Record<string, number>
}

export interface PerformanceMetrics {
  summary: PerformanceSummary
  metrics: Record<string, MetricStatsData>
}

// Log types
export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'

export interface LogEntry {
  timestamp: string
  level: LogLevel
  message: string
  source?: string
  context?: Record<string, string | number>
  event_id?: string | null
}

// Settings types
export interface SystemSettings {
  llmProvider: string
  llmApiKey?: string
  llmApiUrl?: string
  llmModel?: string
  ragTopK: number
  ragSimilarityThreshold: number
  ragUseKeywordSearch: boolean
  // Allow additional properties for API compatibility
  llm?: {
    provider?: string
    api_key?: string
    api_url?: string
    model?: string
    has_api_key?: boolean
  }
  rag?: {
    top_k?: number
    similarity_threshold?: number
    use_rewrite?: boolean
    max_context_length?: number
  }
  general?: {
    timezone?: string
  }
  agri_ai?: {
    llm_api_key?: string
    llm_base_url?: string
    llm_model?: string
    llm_api_key_masked?: string
    has_llm_key?: boolean
    vision_api_key?: string
    vision_base_url?: string
    vision_model?: string
    vision_api_key_masked?: string
    has_vision_key?: boolean
    weather_provider?: string
    weather_api_key?: string
    weather_api_key_masked?: string
    has_weather_key?: boolean
  }
}


// 与后端 LLMProviderInfo（backend/app/schemas/settings.py）对齐。
// 注意：Settings.vue 用的是它自己的本地 camel 视图模型 + 显式映射，不读这里的字段。
export interface LLMProvider {
  id: string
  name: string
  display_name: string
  default_api_url: string
  default_model: string
  supported_models: string[]
}

export interface TestConnectionResult {
  success: boolean
  message: string
  latency?: number
}

// RAG Config type
export interface RAGConfig {
  topK: number
  similarityThreshold: number
  useKeywordSearch: boolean
}

// API Response types
export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface ApiError {
  detail: string
}
