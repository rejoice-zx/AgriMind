import axios from 'axios'
import { useAuthStore } from '@/stores/auth'
import { getOrCreateGuestDeviceId } from '@/utils/guestIdentity'

// ---------------------------------------------------------------------------
// Shared token refresh, single-flight (P2-STREAM / DS-22).
// Every caller — the axios interceptor and the raw-fetch SSE consumers — awaits
// the same in-flight promise, so N concurrent 401s trigger exactly one call.
// ---------------------------------------------------------------------------
let refreshInFlight: Promise<string> | null = null
let refreshSession: string | null = null
let guestInFlight: Promise<string> | null = null

export class AuthSessionChangedError extends Error {}
export class GuestIdentityChangedError extends Error {
  constructor() { super('访客身份已更新，旧会话已失效，请开始新对话') }
}
const guestIdentityListeners = new Set<() => void>()
export function onGuestIdentityChanged(listener: () => void): () => void {
  guestIdentityListeners.add(listener)
  return () => { guestIdentityListeners.delete(listener) }
}
export function isGuestScopedChatUrl(url: string): boolean {
  return /\/chat\/conversations\/[^/?#]+(?:\/|$|[?#])/.test(url)
}

export function refreshAccessToken(): Promise<string> {
  const authStore = useAuthStore()
  const refreshToken = authStore.refreshToken
  const version = authStore.sessionVersion
  const session = `${version}:${refreshToken}`
  if (!refreshToken) {
    return Promise.reject(new Error('no refresh token available'))
  }
  if (refreshInFlight && refreshSession === session) {
    return refreshInFlight
  }
  const settled = axios
    .post('/api/auth/refresh', { refresh_token: refreshToken })
    .then((res) => {
      if (authStore.sessionVersion !== version || authStore.refreshToken !== refreshToken) {
        throw new AuthSessionChangedError('authentication session changed')
      }
      const accessToken = res.data?.access_token
      if (typeof accessToken !== 'string' || !accessToken) throw new Error('invalid refresh response')
      authStore.setToken(accessToken)
      return accessToken
    }, (error) => {
      if (authStore.sessionVersion !== version || authStore.refreshToken !== refreshToken) {
        throw new AuthSessionChangedError('authentication session changed')
      }
      throw error
    })
  refreshInFlight = settled
  refreshSession = session
  const release = () => {
    if (refreshInFlight === settled) {
      refreshInFlight = null
    }
  }
  settled.then(release, release)
  return settled
}

/** Test hook: drop any in-flight refresh between cases. */
export function resetRefreshSingleFlight(): void {
  refreshInFlight = null
  refreshSession = null
}

/** Recover only a guest credential actually rejected by a protected endpoint.
 * Login failures must never replace a valid proof used for claiming history.
 */
export async function recoverGuestAccess(rejected: string): Promise<string> {
  const authStore = useAuthStore()
  if (authStore.user || authStore.refreshToken) throw new AuthSessionChangedError('guest session changed')
  const current = localStorage.getItem('guest_token')
  if (!current) throw new AuthSessionChangedError('guest session cleared')
  if (current !== rejected) return current
  if (guestInFlight) return guestInFlight
  const version = authStore.sessionVersion
  const settled = axios.post('/api/auth/guest', {}, {
    headers: { 'X-Device-ID': getOrCreateGuestDeviceId() },
  }).then((res) => {
    if (authStore.sessionVersion !== version || authStore.user || authStore.refreshToken ||
        localStorage.getItem('guest_token') !== rejected) {
      throw new AuthSessionChangedError('guest session changed')
    }
    const { access_token, visitor_id } = res.data
    if (typeof access_token !== 'string' || !access_token || typeof visitor_id !== 'string' || !visitor_id) {
      throw new Error('invalid guest response')
    }
    localStorage.setItem('guest_token', access_token)
    localStorage.setItem('visitor_id', visitor_id)
    authStore.setToken(access_token)
    guestIdentityListeners.forEach(listener => listener())
    return access_token as string
  })
  guestInFlight = settled
  const release = () => { if (guestInFlight === settled) guestInFlight = null }
  settled.then(release, release)
  return settled
}

function authHeaders(): Record<string, string> {
  const authStore = useAuthStore()
  const token = authStore.token || localStorage.getItem('guest_token') || ''
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  headers['X-Device-ID'] = getOrCreateGuestDeviceId()
  const visitor = localStorage.getItem('visitor_id')
  if (visitor) headers['X-Session-ID'] = visitor
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }
  return headers
}

/**
 * fetch() for POST/SSE streams with a bounded auth recovery.
 *
 * A refresh + single retry happens ONLY when the FIRST response is 401: nothing
 * has been consumed yet and the server has not processed the request, so
 * re-sending is safe. Once a stream body exists we never re-issue the call,
 * because the POST may already have persisted a message (P2-STREAM / DS-57).
 */
export async function fetchStreamWithAuthRetry(
  url: string,
  init: RequestInit = {},
): Promise<Response> {
  const doFetch = () =>
    fetch(url, {
      ...init,
      headers: { ...((init.headers as Record<string, string>) || {}), ...authHeaders() },
    })

  const initialToken = useAuthStore().token || localStorage.getItem('guest_token') || ''
  const initialVersion = useAuthStore().sessionVersion
  const first = await doFetch()
  if (first.status !== 401) {
    return first
  }
  const authStore = useAuthStore()
  const version = authStore.sessionVersion
  if (version !== initialVersion) return first
  const refresh = authStore.refreshToken
  try {
    if (authStore.refreshToken) {
      if (!authStore.token || authStore.token === initialToken) await refreshAccessToken()
    }
    else if (initialToken && initialToken === localStorage.getItem('guest_token') && !url.includes('/auth/')) {
      await recoverGuestAccess(initialToken)
      if (isGuestScopedChatUrl(url)) throw new GuestIdentityChangedError()
    } else if (!authStore.user && !authStore.refreshToken && localStorage.getItem('guest_token') &&
               initialToken !== localStorage.getItem('guest_token')) {
      if (isGuestScopedChatUrl(url)) throw new GuestIdentityChangedError()
    } else return first
  } catch (error) {
    if (error instanceof GuestIdentityChangedError) throw error
    if (!(error instanceof AuthSessionChangedError) && refresh &&
        authStore.sessionVersion === version && authStore.refreshToken === refresh) authStore.clearAuth()
    return first
  }
  if (authStore.sessionVersion !== version) return first
  const retriedToken = authStore.token
  const second = await doFetch()
  if (second.status === 401 && authStore.sessionVersion === version && authStore.token === retriedToken) {
    authStore.clearAuth()
  }
  return second
}
