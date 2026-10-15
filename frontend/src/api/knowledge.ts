import request from '@/utils/request'
import { fetchStreamWithAuthRetry } from '@/utils/authRefresh'
import type { 
  KnowledgeItem, 
  PaginatedResponse,
  CreateKnowledgeRequest,
  UpdateKnowledgeRequest,
  IndexStatus
} from '@/types'

export function getKnowledgeList(params: {
  page?: number
  page_size?: number
  category?: string
  keyword?: string
  crop?: string
  growth_stage?: string
  topic?: string
  region?: string
  disease?: string
  source_type?: string
}) {
  return request.get<PaginatedResponse<KnowledgeItem>>('/knowledge', { params })
}

export function getKnowledge(id: string) {
  return request.get<KnowledgeItem>(`/knowledge/${id}`)
}

export function createKnowledge(data: CreateKnowledgeRequest) {
  return request.post<KnowledgeItem>('/knowledge', data)
}

export function updateKnowledge(id: string, data: UpdateKnowledgeRequest) {
  return request.put<KnowledgeItem>(`/knowledge/${id}`, data)
}

export function deleteKnowledge(id: string) {
  return request.delete(`/knowledge/${id}`)
}

export function deleteKnowledgeByCategory(category: string) {
  return request.delete<{ deleted_count: number; category: string }>(`/knowledge/by-category/${encodeURIComponent(category)}`)
}

export interface KnowledgeImportResult {
  success_count: number
  skip_count: number
  errors: string[]
}
export function importKnowledge(data: unknown[], skipDuplicates = true) {
  return request.post<KnowledgeImportResult>('/knowledge/import', { items: data, skip_duplicates: skipDuplicates })
}

export function exportKnowledge() {
  return request.get<{ items: KnowledgeItem[]; total: number }>('/knowledge/export/all')
}

/**
 * 重建向量索引（SSE 流式进度）
 */
export async function rebuildIndexSSE(): Promise<ReadableStreamDefaultReader<Uint8Array>> {
  // Retry once after a first-response 401 (nothing consumed, no server write);
  // never re-issue once the stream has started.
  const resp = await fetchStreamWithAuthRetry('/api/knowledge/index/rebuild', { method: 'POST' })
  if (!resp.ok || !resp.body) {
    throw new Error(`重建请求失败: ${resp.status}`)
  }
  return resp.body.getReader()
}

export function getIndexStatus() {
  return request.get<IndexStatus>('/knowledge/index/status')
}

export function getKnowledgeCategories() {
  return request.get<{ categories: string[] }>('/knowledge/categories/list')
}
