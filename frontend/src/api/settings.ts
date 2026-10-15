import request from '@/utils/request'
import type { SystemSettings, LLMProvider, TestConnectionResult } from '@/types'

export function getSettings() {
  return request.get<SystemSettings>('/settings')
}

export function getSettingsSecurityStatus() {
  return request.get<{ encryption_ready: boolean; message: string }>('/settings/security-status')
}

export function updateSettings(data: Partial<SystemSettings>) {
  return request.put<SystemSettings>('/settings', data)
}

export function getLLMProviders() {
  return request.get<LLMProvider[]>('/settings/llm-providers')
}

export function testConnection(data: { 
  provider: string
  api_key: string
  api_url?: string
  model?: string 
}) {
  return request.post<TestConnectionResult>('/settings/test-connection', data)
}

export function testVisionConnection(data: {
  api_key?: string
  base_url?: string
  model?: string
}) {
  return request.post<TestConnectionResult>('/settings/test-vision-connection', data)
}

export function testWeatherConnection(data: {
  provider?: string
  weather_custom?: Record<string, unknown>
  latitude?: number
  longitude?: number
  api_key?: string
}) {
  return request.post<TestConnectionResult>('/settings/test-weather-connection', data)
}

export type AgriModelKind = 'decision' | 'vision'
export interface AgriModelDraft { kind: AgriModelKind; api_key?: string; base_url?: string; model?: string }
export interface ModelCheck { name: string; state: 'passed' | 'failed' | 'unverified'; code: string; message: string; upstream_trace_id?: string | null }
export interface ModelCapability {
  id: string
  image_input: 'supported' | 'unsupported' | 'unknown'
  evidence: 'metadata' | 'model_name' | 'unspecified'
  vision_candidate: boolean
  message: string
}
export interface ModelProbeResult {
  success: boolean
  message: string
  authentication: 'unknown' | 'verified' | 'failed'
  vision: 'not_tested' | 'sample_verified' | 'accepted_unverified' | 'failed'
  checks: ModelCheck[]
  request_id?: string | null
}
export interface ModelDiscoveryResult { success: boolean; message: string; models: string[]; model_details?: ModelCapability[]; truncated: boolean; code: string; request_id?: string | null }
export function discoverAgriModels(data: AgriModelDraft) {
  return request.post<ModelDiscoveryResult>('/settings/model-discovery', data)
}
export function testAgriModel(data: AgriModelDraft) {
  return request.post<ModelProbeResult>('/settings/model-test', data, { timeout: 90000 })
}

