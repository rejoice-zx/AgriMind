import type { PlanItem, AgriPlanResponse } from '@/types/agriculture'
import { businessDate } from '@/utils/businessDate'

export function planConfirmationReason(item: PlanItem): string {
  if (item.execution_status === 'completed') return '该事项已确认完成，无需重复确认'
  if (item.execution_status === 'revoked') return '原确认记录已撤销，请核查历史档案，不能重复确认'
  if (item.execution_status === 'unavailable') return '历史确认记录不可用，请核查农事档案'
  if (item.is_spray) return '施药事项需专家复核，请在农事记录中填写实际操作与核验信息'
  if (item.date !== businessDate()) return item.date > businessDate()
    ? '未来事项尚未完成，请在作业当天确认' : '仅可确认今天的事项，历史操作请在农事记录中补录'
  if (!item.can_proceed) return item.risk_notes || item.reason || '此事项尚待核实'
  return ''
}

export function planExecutionLabel(item: PlanItem, pending = '确认已完成'): string {
  if (item.execution_status === 'completed') return '已完成'
  if (item.execution_status === 'revoked') return '确认记录已撤销'
  if (item.execution_status === 'unavailable') return '确认记录需核查'
  return pending
}

function taskKey(item: PlanItem): string {
  return JSON.stringify([item.crop_planting_id ?? null, item.date, item.record_type, item.action])
}

// Renew shortly before expiry; loading an existing plan issues fresh proofs without rescheduling it.
export function planProofNeedsRenewal(item: PlanItem): boolean {
  const issued = Number(item.proof?.split('.')[0])
  return Number.isFinite(issued) && issued > 0 && Date.now() / 1000 - issued >= 840
}

export function renewedPlanItems(items: PlanItem[], plan: AgriPlanResponse): PlanItem[] {
  return items.map(item => {
    const fresh = plan.items.find(candidate => taskKey(candidate) === taskKey(item))
    if (!fresh) throw new Error('计划已调整，请查看最新建议后重新确认')
    const blocked = planConfirmationReason(fresh)
    if (blocked) throw new Error(blocked)
    const facts: (keyof PlanItem)[] = ['description', 'reason', 'source', 'risk_notes', 'is_spray', 'can_proceed', 'reason_code']
    if (facts.some(key => fresh[key] !== item[key])) throw new Error('计划依据或风险提示已更新，请查看后重新确认')
    return fresh
  })
}

export function planUpdateLabel(plan: AgriPlanResponse): string {
  const format = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '暂无'
  return `第 ${plan.version || 1} 版 · 计划核查：${format(plan.evaluated_at)} · 天气获取：${format(plan.weather_fetched_at)} · 气象观测：${format(plan.weather_observed_at)}`
}

export function markPlanItemsCompleted(plan: AgriPlanResponse | null, fieldId: number,
  confirmedItems: PlanItem[], recordIds: number[]): void {
  if (!plan || plan.field_id !== fieldId) return
  const saved = new Map<string, number>()
  confirmedItems.forEach((item, index) => {
    const id = recordIds[index]
    if (id !== undefined && Number.isSafeInteger(id) && id > 0) saved.set(taskKey(item), id)
  })
  for (const item of plan.items) {
    const id = saved.get(taskKey(item))
    if (id === undefined) continue
    item.execution_status = 'completed'
    item.execution_record_id = id
  }
}
