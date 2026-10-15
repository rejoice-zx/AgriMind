import type { FarmingRecordCreate } from '@/types/agriculture'
const prefix = 'agrimind:farming-draft:v1:'
const key = (userId: number | string, fieldId: number) => `${prefix}${userId}:${fieldId}`

export function saveFarmingDraft(userId: number | string, draft: FarmingRecordCreate): boolean {
  try {
    const name = key(userId, draft.field_id)
    if (!draft.operation_content && !draft.chemical_name && !draft.notes) localStorage.removeItem(name)
    else localStorage.setItem(name, JSON.stringify({ savedAt: Date.now(), draft }))
    return true
  } catch { return false }
}

export function readFarmingDraft(userId: number | string, fieldId: number): FarmingRecordCreate | null {
  try {
    const raw = JSON.parse(localStorage.getItem(key(userId, fieldId)) || 'null')
    if (!raw || !Number.isFinite(raw.savedAt) || Date.now() - raw.savedAt > 7 * 86400000 || raw.savedAt > Date.now()) return null
    const draft = raw.draft
    if (draft?.field_id !== fieldId || typeof draft.operation_content !== 'string' || typeof draft.operation_date !== 'string') return null
    return draft
  } catch { return null }
}

export function clearFarmingDraft(userId: number | string, fieldId: number) {
  try { localStorage.removeItem(key(userId, fieldId)) } catch { /* Form remains usable when storage is unavailable. */ }
}
