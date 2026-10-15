import axios, { AxiosError, InternalAxiosRequestConfig, AxiosResponse } from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import router from '@/router'
import { validationMessage } from '@/utils/validationMessage'
import { getOrCreateGuestDeviceId } from '@/utils/guestIdentity'
import { refreshAccessToken, recoverGuestAccess, AuthSessionChangedError, GuestIdentityChangedError, isGuestScopedChatUrl } from '@/utils/authRefresh'

// Create axios instance
const request = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// Shared single-flight token refresh (P2-STREAM / DS-22) — see utils/authRefresh.ts.
// The raw-fetch SSE consumers await the same in-flight promise, so concurrent
// 401s never trigger more than one /auth/refresh call.

// Request interceptor - attach JWT token
request.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const authStore = useAuthStore()
    const sessionConfig = config as InternalAxiosRequestConfig & { _authSessionVersion?: number }
    sessionConfig._authSessionVersion = authStore.sessionVersion
    if (authStore.token) {
      config.headers.Authorization = `Bearer ${authStore.token}`
    }
    config.headers['X-Device-ID'] = getOrCreateGuestDeviceId()
    const visitorId = localStorage.getItem('visitor_id')
    if (visitorId) {
      config.headers['X-Session-ID'] = visitorId
    }
    return config
  },
  (error: AxiosError) => {
    return Promise.reject(error)
  }
)

// Response interceptor - handle errors uniformly
request.interceptors.response.use(
  (response: AxiosResponse) => {
    return response
  },
  async (error: AxiosError<{ detail: string }>) => {
    const authStore = useAuthStore()

    if (error.response) {
      const { status, data } = error.response

      switch (status) {
        case 401: {
          // Token expired or invalid - try refresh (at most once per request)
          const config = error.config as
            | (InternalAxiosRequestConfig & { _retriedAfterRefresh?: boolean; _authSessionVersion?: number })
            | undefined
          const isRefreshCall = !!config?.url?.includes('/auth/refresh')
          const isAuthCall = /\/auth\/(?:login|register|register-customer|guest|refresh)(?:$|[?#])/.test(config?.url || '')
          const rejectedToken = String(config?.headers?.Authorization || '').replace(/^Bearer /, '')
          // A late response must not clear or replay a newer login session.
          if (config?._authSessionVersion !== authStore.sessionVersion) break
          if (!isAuthCall && config && !authStore.user && !authStore.refreshToken &&
              localStorage.getItem('guest_token') && rejectedToken !== localStorage.getItem('guest_token')) {
            if (isGuestScopedChatUrl(config.url || '')) {
              const changed = new GuestIdentityChangedError()
              ElMessage.error(changed.message)
              throw changed
            }
            if (!config._retriedAfterRefresh) {
              config._retriedAfterRefresh = true
              return request(config)
            }
          }
          if (!config?._retriedAfterRefresh && !isAuthCall && config && authStore.refreshToken &&
              authStore.token && rejectedToken !== authStore.token) {
            config._retriedAfterRefresh = true
            config.headers.Authorization = `Bearer ${authStore.token}`
            return request(config)
          }

          if (config?._retriedAfterRefresh) {
            if (rejectedToken && rejectedToken !== authStore.token) break
            // A second 401 after a successful refresh is not recoverable here.
            authStore.clearAuth()
            if (router.currentRoute.value.meta.public !== true) {
              router.push('/login')
              ElMessage.error('登录已过期，请重新登录')
            }
            break
          }

          if (!config?._retriedAfterRefresh && !isAuthCall && !authStore.refreshToken &&
              rejectedToken && rejectedToken === localStorage.getItem('guest_token')) {
            try {
              const accessToken = await recoverGuestAccess(rejectedToken)
              if (config) {
                if (isGuestScopedChatUrl(config.url || '')) throw new GuestIdentityChangedError()
                config._retriedAfterRefresh = true
                config.headers.Authorization = `Bearer ${accessToken}`
                return request(config)
              }
            } catch (refreshError) {
              if (refreshError instanceof GuestIdentityChangedError) {
                ElMessage.error(refreshError.message)
                throw refreshError
              }
              if (refreshError instanceof AuthSessionChangedError) break
            }
          } else if (authStore.refreshToken && !isRefreshCall && !isAuthCall) {
            try {
              const accessToken = await refreshAccessToken()
              if (config) {
                config._retriedAfterRefresh = true
                config.headers.Authorization = `Bearer ${accessToken}`
                return request(config)
              }
            } catch (refreshError) {
              if (refreshError instanceof AuthSessionChangedError) break
              // Refresh failed - only redirect if not on public page
              authStore.clearAuth()
              if (router.currentRoute.value.meta.public !== true) {
                router.push('/login')
                ElMessage.error('登录已过期，请重新登录')
              }
            }
          } else if (router.currentRoute.value.meta.public === true) {
            // Public page 401 (e.g. a wrong password on /login) says nothing about the
            // signed guest token's validity. Keep it: it is the proof required to migrate
            // guest conversations after a later successful login. CustomerChat still
            // re-requests a fresh token when it needs one.
            authStore.clearAuth()
          } else {
            authStore.clearAuth()
            router.push('/login')
            ElMessage.error('未授权访问')
          }
          break
        }

        case 403:
          ElMessage.error('权限不足')
          break

        case 404:
          ElMessage.error('资源不存在')
          break

        case 409:
          ElMessage.error(data?.detail || '资源已存在')
          break

        case 422: {
          const message = validationMessage(data?.detail)
          if (data) data.detail = message
          error.message = message
          ElMessage.error(message)
          break
        }

        case 429:
          // Auth endpoints send a dedicated "尝试过于频繁…" detail (P2-AUTH-BOUNDS);
          // fall back to the generic wording for the global bucket.
          ElMessage.error(data?.detail || '请求过于频繁，请稍后重试')
          break

        default:
          // 4xx may carry an actionable business message; 5xx must never surface
          // provider/internal detail to the user (P2-STREAM / DS-11).
          if (status >= 400 && status < 500 && data?.detail) {
            ElMessage.error(data.detail)
          } else {
            ElMessage.error('服务器错误，请稍后重试')
          }
          break
      }
    } else if (error.request) {
      if (error.code === 'ECONNABORTED' || error.message?.includes('timeout')) {
        ElMessage.error('请求超时，请稍后重试')
      } else {
        ElMessage.error('网络错误，请检查网络连接')
      }
    } else {
      ElMessage.error('请求配置错误')
    }

    return Promise.reject(error)
  }
)

export default request
