import request from '@/utils/request'
import type { PerformanceMetrics } from '@/types'

export function getPerformanceMetrics() {
  return request.get<PerformanceMetrics>('/performance/metrics')
}

export function clearPerformanceData() {
  return request.post('/performance/clear')
}

export function exportPerformanceReport() {
  return request.get<string>('/performance/export')
}
