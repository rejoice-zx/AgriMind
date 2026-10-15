import type { AgentDecisionResponse } from './agriculture'

export interface AgriConversation {
  id: string
  title: string
  field_id: number | null
  field_ref: number | null
  created_at: string
  updated_at: string
}
export interface AgriConsultation {
  id: string
  turn_id: string
  conversation_id: string
  query: string
  image_url: string | null
  field_id: number | null
  status: 'pending' | 'in_review' | 'resolved'
  assigned_expert_id: number | null
  claim_available?: boolean
  assigned_to_current_user?: boolean
  workflow_verified?: boolean
  reviewer_name?: string | null
  reviewed_at?: string | null
  signed_review?: OperationReview | null
  request_id?: string | null
  answer: string | null
  decision: AgentDecisionResponse
  created_at: string
  updated_at: string
}
export interface LabelReviewEvidence {
  record_id?: number | null
  product_name: string
  registration_number: string
  applicable_crop: string
  label_reference: string
  usage_scope: string
  phi_days: number
}
export interface ReviewChecks {
  registration_checked: boolean
  crop_and_use_checked: boolean
  records_complete: boolean
  phi_checked: boolean
  site_and_weather_checked: boolean
}
export interface OperationReview {
  purpose: 'spray' | 'harvest'
  outcome: 'needs_information' | 'rejected' | 'approved'
  crop_planting_id: number | null
  context_fingerprint: string
  planned_operation_date: string | null
  planned_harvest_date: string | null
  valid_until: string | null
  evidence_references: string
  review_notes: string
  checks: ReviewChecks
  products: LabelReviewEvidence[]
}
export interface ConsultationSubmission {
  answer: string
  request_id: string
  operation_review: OperationReview | null
}
export interface ConsultationContext {
  context_fingerprint: string
  available_crops: { id: number; crop: string; planting_date: string }[]
  spraying_record_ids: number[]
  snapshot: {
    crop: { id: number; crop: string } | null
    records: { id: number; record_type: string; input_name: string | null; revoked_at: string | null; record_date: string; safety_interval_days: number | null }[]
    [key: string]: unknown
  }
  current_review_status: string
  review_window_hours: number
  note: string
}
export interface AgriTurn {
  id: string
  query: string
  image_url: string | null
  status: 'pending' | 'completed' | 'failed'
  decision: AgentDecisionResponse | null
  error: string | null
  created_at: string
  consultation: AgriConsultation | null
}
