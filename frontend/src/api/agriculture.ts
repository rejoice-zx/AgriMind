// -*- coding: utf-8 -*-
/**
 * AgriMind 农业综合业务与决策接口服务
 */
import request from '@/utils/request'

export interface RegionNode { code: string; name: string; children?: RegionNode[] }
export interface LocationCandidate { latitude: number; longitude: number; name: string; province: string; city: string; label: string; region_code?: string }
export function getRegions() { return request.get<{ items: RegionNode[]; notice: string }>('/locations/regions') }
export function lookupRegion(name: string, context: { region_code?: string; province?: string; city?: string } = {}) {
  return request.get<{ items: LocationCandidate[] }>('/locations/search', { params: { name, ...context } })
}
export function updateFarmLocation(id: number, data: Record<string, unknown>) { return request.put(`/farms/${id}`, data) }
export function revokeFarmingRecord(id: number, reason: string) { return request.delete(`/farming-records/${id}`, { params: { reason } }) }
export function getFarmWeatherForecast(id: number, days = 7) { return request.get<import('@/types/agriculture').FieldWeather>(`/farms/${id}/weather`, { params: { days } }) }
import type {
  Farm,
  FarmCreate,
  FieldItem,
  FieldCreate,
  CropPlanting,
  CropPlantingCreate,
  FarmingRecord,
  FarmingRecordCreate,
  AgentDecisionRequest,
  AgentDecisionResponse,
  VisualDiagnosisRequest,
  VisualDiagnosisResponse,
  ExpertTicketItem,
  ExpertTicketDetail,
  ExpertResolveRequest,
  ExpertResolveResponse,
  ImageUploadResponse,
  AgriPlanRequest,
  AgriPlanResponse,
  AgriPlanApplyRequest,
  AgriPlanApplyResponse,
  WeatherSnapshot
} from '@/types/agriculture'

// ==================== 农场与田块管理 ====================

export function getFarms(params?: { skip?: number; limit?: number }) {
  return request.get<{ total: number; items: Farm[] }>('/farms', { params })
}

export function createFarm(data: FarmCreate) {
  return request.post<Farm>('/farms', data)
}

export function setFarmArchive(id: number, archived: boolean) { return request.post<Farm>(`/farms/${id}/archive`, { archived }) }
export function setFieldArchive(id: number, archived: boolean) { return request.post<FieldItem>(`/fields/${id}/archive`, { archived }) }

export function getFieldsByFarm(farmId: number) {
  return request.get<{ total: number; items: FieldItem[] }>(`/farms/${farmId}/fields`)
}

export function createField(farmId: number, data: FieldCreate) {
  return request.post<FieldItem>(`/farms/${farmId}/fields`, data)
}

export function updateField(fieldId: number, data: Partial<FieldCreate>) {
  return request.put<FieldItem>(`/fields/${fieldId}`, data)
}
export function getFieldEnvironment(fieldId: number) {
  return request.get<import('@/types/agriculture').FieldEnvironment>(`/fields/${fieldId}/environment`)
}
export function createEnvironmentObservation(fieldId: number, data: import('@/types/agriculture').EnvironmentObservationCreate) {
  return request.post<{ observation_id: number; replayed: boolean; environment: import('@/types/agriculture').FieldEnvironment }>(`/fields/${fieldId}/environment-observations`, data)
}
export interface ArchivePaging { before_id?: number; snapshot_id?: number; limit?: number }
export function getEnvironmentHistory(fieldId: number, params: ArchivePaging = {}) {
  return request.get<import('@/types/agriculture').ArchivePage<import('@/types/agriculture').EnvironmentHistoryItem>>(`/fields/${fieldId}/environment-observations`, { params })
}
export function getCropHistory(fieldId: number, params: ArchivePaging = {}) {
  return request.get<import('@/types/agriculture').ArchivePage<CropPlanting>>(`/fields/${fieldId}/crop-history`, { params })
}
export function getCropStatusHistory(cropId: number, params: ArchivePaging = {}) {
  return request.get<import('@/types/agriculture').ArchivePage<import('@/types/agriculture').CropStatusEvent>>(`/crops/${cropId}/status-history`, { params })
}
export function reopenCrop(cropId: number, data: { request_id: string; reason: string }) {
  return request.post<{ crop: CropPlanting; event_id: number; replayed: boolean }>(`/crops/${cropId}/reopen`, data)
}

export function deleteField(fieldId: number) {
  return request.delete(`/fields/${fieldId}`)
}

export function getCropPlantings(fieldId: number, status?: string) {
  return request.get<{ total: number; items: CropPlanting[] }>(`/fields/${fieldId}/crops`, {
    params: { status }
  })
}

export function createCropPlanting(fieldId: number, data: CropPlantingCreate) {
  return request.post<CropPlanting>(`/fields/${fieldId}/crops`, data)
}

export function updateCropPlanting(cropId: number, data: Partial<CropPlantingCreate>) {
  return request.put<CropPlanting>(`/crops/${cropId}`, data)
}

export async function getFarmingRecords(fieldId: number, recordStatus: 'active' | 'revoked' | 'all' = 'active', cropId?: number) {
  const rows: FarmingRecord[] = []
  for (let skip = 0; ; skip += 100) {
    const response = await request.get<FarmingRecord[]>(`/farming-records/field/${fieldId}`, {
      params: { record_status: recordStatus, skip, limit: 100, ...(cropId == null ? {} : { crop_planting_id: cropId }) }
    })
    rows.push(...response.data)
    if (response.data.length < 100) return { ...response, data: rows }
  }
}

export function getFieldLatestWeather(fieldId: number) {
  return request.get<WeatherSnapshot | null>(`/fields/${fieldId}/weather/latest`)
}

export function getFieldWeatherForecast(fieldId: number, days = 7) {
  return request.get<import('@/types/agriculture').FieldWeather>(`/fields/${fieldId}/weather/forecast`, { params: { days } })
}

export function createFarmingRecord(data: FarmingRecordCreate) {
  return request.post<FarmingRecord>('/farming-records', data)
}

// ==================== 农业智能决策 Agent ====================

export function queryAgriAgentDecision(data: AgentDecisionRequest) {
  return request.post<AgentDecisionResponse>('/agent/decide', data, { timeout: 150000 })
}

// ==================== 多模态初诊与专家工作台 ====================

export function uploadDiagnosisImage(file: File) {
  const formData = new FormData()
  formData.append('file', file)
  return request.post<ImageUploadResponse>('/diagnosis/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data'
    }
  })
}

export function diagnoseCropImage(data: VisualDiagnosisRequest) {
  return request.post<VisualDiagnosisResponse>('/diagnosis/visual-diagnose', data, { timeout: 120000 })
}

export function getExpertTickets(params?: { status?: string; crop_name?: string; limit?: number; offset?: number; page?: number; page_size?: number }) {
  return request.get<{ total: number; items: ExpertTicketItem[] }>('/expert/tickets', { params })
}

export function getExpertTicketDetail(ticketId: number) {
  return request.get<ExpertTicketDetail>(`/expert/tickets/${ticketId}`)
}

export function claimExpertTicket(ticketId: number) {
  return request.post<ExpertTicketItem>(`/expert/tickets/${ticketId}/claim`)
}

export function resolveExpertTicket(ticketId: number, data: ExpertResolveRequest) {
  return request.post<ExpertResolveResponse>(`/expert/tickets/${ticketId}/resolve`, data)
}

// ==================== 智能农事作业计划与排程 ====================

export function generateAgriPlan(data: AgriPlanRequest) {
  return request.post<AgriPlanResponse>('/agri/plan', data)
}

export function applyAgriPlan(data: AgriPlanApplyRequest) {
  return request.post<AgriPlanApplyResponse>('/agri/plan/apply', data)
}


export function getDiagnosisRecords(params?: { field_id?: number; offset?: number; limit?: number }) {
  return request.get<{ total: number; items: ExpertTicketItem[] }>('/diagnosis/records', { params })
}
export function getDiagnosisRecord(recordId: number) {
  return request.get<ExpertTicketDetail>(`/diagnosis/records/${recordId}`)
}
export function deleteDiagnosisRecord(recordId: number) {
  return request.delete<void>(`/diagnosis/records/${recordId}`)
}
export function requestDiagnosisReview(recordId: number) {
  return request.post<ExpertTicketItem>(`/diagnosis/records/${recordId}/request-review`)
}

export function getDiagnosisRequest(requestId: string) {
  return request.get<{ request_id: string; status: string; result: VisualDiagnosisResponse | null; message: string }>(`/diagnosis/requests/${requestId}`)
}

export function cancelDiagnosisRequest(requestId: string) {
  return request.post<{ request_id: string; status: string; result?: VisualDiagnosisResponse | null; message: string }>(`/diagnosis/requests/${requestId}/cancel`)
}
