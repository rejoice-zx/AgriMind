import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { getFarms } from '@/api/agriculture'
import { useAuthStore } from '@/stores/auth'
import type { Farm } from '@/types/agriculture'

/** One farm context for all agriculture pages; preferences are isolated by user. */
export const useFarmSelectionStore = defineStore('farmSelection', () => {
  const auth = useAuthStore()
  const farms = ref<Farm[]>([])
  const selected = ref<number | null>(null)
  let requestVersion = 0
  let fetchedAt = 0
  let fetchedSession: number | undefined
  const storageKey = () => auth.user ? `agrimind:selected-farm:${auth.user.id}` : null

  watch(() => auth.user?.id, () => {
    ++requestVersion
    fetchedAt = 0
    farms.value = []
    selected.value = null
    try {
      const key = storageKey()
      const value = key ? Number(localStorage.getItem(key)) : 0
      if (Number.isSafeInteger(value) && value > 0) selected.value = value
    } catch { /* Storage can be unavailable; keep an in-memory selection. */ }
  }, { immediate: true, flush: 'sync' })

  const selectedFarmId = computed({
    get: () => selected.value,
    set: (id: number | null) => {
      selected.value = id
      try {
        const key = storageKey()
        if (key) {
          if (id === null) localStorage.removeItem(key)
          else localStorage.setItem(key, String(id))
        }
      } catch { /* Selecting a farm must still work with storage disabled. */ }
    }
  })

  async function loadFarms(maxAgeMs = 0) {
    if (maxAgeMs > 0 && fetchedAt > 0 && fetchedSession === auth.sessionVersion && Date.now() - fetchedAt < maxAgeMs) return true
    const version = ++requestVersion
    const session = auth.sessionVersion
    const items: Farm[] = []
    // Do not discard a saved selection just because it is outside the first page.
    for (let skip = 0; ; skip += 100) {
      const response = await getFarms({ skip, limit: 100 })
      if (version !== requestVersion || session !== auth.sessionVersion) return false
      const page = response.data.items || []
      items.push(...page)
      if (!page.length || items.length >= response.data.total) break
    }
    farms.value = items
    fetchedAt = Date.now()
    fetchedSession = session
    if (!items.some(farm => farm.id === selected.value)) {
      selectedFarmId.value = items[0]?.id ?? null
    }
    return true
  }

  return { farms, selectedFarmId, loadFarms }
})
