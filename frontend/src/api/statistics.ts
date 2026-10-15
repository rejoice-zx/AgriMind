import request from '@/utils/request'
import type { AgriStatisticsSnapshot } from '@/types/agriStatistics'

export interface StatisticsRange { start_date?: string; end_date?: string }
export function getAgriculturalStatistics(params: StatisticsRange = {}) {
  return request.get<AgriStatisticsSnapshot>('/statistics/snapshot', { params })
}
export function exportStatisticsReport(params: StatisticsRange = {}) {
  return request.get<{ content: string; generated_at: string }>('/statistics/export', { params })
}
