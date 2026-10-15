type DraftUser = { id: number; username?: string; created_at?: string }
export function draftOwnerKey(user: DraftUser | null | undefined): string | undefined {
  return user ? JSON.stringify([user.id, user.username || '', user.created_at || '']) : undefined
}
const key = (owner: string, scope: string) => `agrimind:draft:v2:${encodeURIComponent(owner)}:${encodeURIComponent(scope)}`

export function saveScopedDraft(owner: string | undefined, scope: string, payload: unknown): boolean {
  if (!owner) return false
  try {
    if (payload == null) localStorage.removeItem(key(owner, scope))
    else {
      const value = JSON.stringify({ savedAt: Date.now(), payload })
      if (value.length > 200000) return false
      localStorage.setItem(key(owner, scope), value)
    }
    return true
  } catch { return false }
}

export function readScopedDraft<T>(owner: string | undefined, scope: string): T | null {
  if (!owner) return null
  try {
    const raw = localStorage.getItem(key(owner, scope))
    if (!raw || raw.length > 200000) return null
    const saved = JSON.parse(raw)
    if (!Number.isFinite(saved?.savedAt) || saved.savedAt > Date.now() || Date.now() - saved.savedAt > 7 * 86400000) return null
    return saved.payload ?? null
  } catch { return null }
}
