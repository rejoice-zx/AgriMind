import request from '@/utils/request'
import type { AgriConversation, AgriTurn, AgriConsultation, ConsultationContext, ConsultationSubmission } from '@/types/agriHistory'
import type { AgentDecisionResponse } from '@/types/agriculture'

export interface AgentRequestStatus {
  request_id: string
  status: string
  conversation_id: string
  turn_id: string
  result?: AgentDecisionResponse | null
  message: string
}
export function getAgentRequest(id: string) {
  return request.get<AgentRequestStatus>(`/agent/requests/${id}`)
}
export function cancelAgentRequest(id: string) {
  return request.post<AgentRequestStatus>(`/agent/requests/${id}/cancel`)
}

export function getAgriConversations(params: { q?: string; field_id?: number; date_from?: string; date_to?: string; offset?: number; limit?: number } = {}) {
  return request.get<{ total: number; items: AgriConversation[] }>('/agent/conversations', { params })
}
export function getAgriConversation(id: string, params: { offset?: number; limit?: number } = {}) {
  return request.get<{ conversation: AgriConversation; total: number; items: AgriTurn[] }>(`/agent/conversations/${id}`, { params })
}
export function transferAgriTurn(id: string) {
  return request.post<AgriConsultation>(`/agent/turns/${id}/consultation`)
}
export function getAgriConsultation(id: string) {
  return request.get<AgriConsultation>(`/agri-consultations/${id}`)
}
export function getAgriConsultations(params: { status?: string; offset?: number; limit?: number } = {}) {
  return request.get<{ total: number; items: AgriConsultation[] }>('/agri-consultations', { params })
}
export function claimAgriConsultation(id: string) {
  return request.post<AgriConsultation>(`/agri-consultations/${id}/claim`)
}
export function resolveAgriConsultation(id: string, submission: string | ConsultationSubmission) {
  return request.post<AgriConsultation>(`/agri-consultations/${id}/resolve`, typeof submission === 'string' ? { answer: submission } : submission)
}
export function getConsultationContext(id: string, crop_planting_id?: number) {
  return request.get<ConsultationContext>(`/agri-consultations/${id}/review-context`, { params: { crop_planting_id } })
}
