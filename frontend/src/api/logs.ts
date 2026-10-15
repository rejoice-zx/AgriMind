import request from '@/utils/request'
import type { LogEntry } from '@/types'

export interface LogQueryResponse {
  items: LogEntry[]
  total: number
  page: number
  page_size: number
  truncated: boolean
  query_scope: string
  scanned_bytes: number
  legacy_time_entries: number
  unavailable_files: number
  unparsed_lines: number
}

export function getLogs(params: {
  page?: number
  page_size?: number
  level?: string
  start_time?: string
  end_time?: string
  keyword?: string
  request_id?: string
  business_request_id?: string
  conversation_id?: string
  consultation_id?: string
  field_id?: number
  record_id?: number
}) {
  return request.get<LogQueryResponse>('/logs', { params })
}

export function downloadLogs(logType: 'app' | 'error') {
  return request.get(`/logs/download`, {
    params: { log_type: logType },
    responseType: 'blob'
  })
}

export function clearLogs(logType: 'app' | 'error') {
  return request.post('/logs/clear', { log_type: logType })
}
