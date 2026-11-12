import { validationMessage } from '@/utils/validationMessage'

/** Element Plus 确认框被取消或关闭时 reject 的值；它们不是错误。 */
export function isDismissed(error: unknown): boolean {
  return error === 'cancel' || error === 'close'
}

/**
 * 从任意异常中取一条给用户看的中文说明。
 *
 * - 后端业务 detail（字符串）原样展示；422 的数组 detail 转成"某字段填写有误"。
 * - 5xx 和未知错误不暴露内部信息，统一回落到 fallback。
 * - 原生 JS 异常（如 JSON 解析失败）不直接展示英文原文。
 */
export function errorText(error: unknown, fallback = '操作失败，请稍后重试'): string {
  const response = (error as { response?: { status?: number; data?: { detail?: unknown } } })?.response
  if (response) {
    const status = response.status ?? 0
    const detail = response.data?.detail
    if (status >= 500) return fallback
    if (Array.isArray(detail)) return validationMessage(detail)
    if (typeof detail === 'string' && detail.trim()) return detail.trim().slice(0, 200)
    if (status === 403) return '当前账号没有此操作权限'
    if (status === 404) return '内容不存在或已被删除'
    return fallback
  }
  if ((error as { request?: unknown })?.request) return '网络连接失败，请检查网络后重试'
  // 前端主动抛出的中文业务提示（如"计划已更新，请重新确认"）原样展示；英文原生异常不暴露给用户。
  const message = (error as { message?: unknown })?.message
  if (typeof message === 'string' && /[一-鿿]/.test(message)) return message.slice(0, 200)
  return fallback
}
