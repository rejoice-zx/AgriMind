import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { User, LoginRequest } from '@/types'
import { login as apiLogin, getCurrentUser } from '@/api/auth'
import router from '@/router'

// Token storage keys
const TOKEN_KEY = 'token'
const REFRESH_TOKEN_KEY = 'refreshToken'
const USER_KEY = 'user'

export const useAuthStore = defineStore('auth', () => {
  // State
  const user = ref<User | null>(loadUserFromStorage())
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const refreshToken = ref<string | null>(localStorage.getItem(REFRESH_TOKEN_KEY))
  const loading = ref(false)
  const error = ref<string | null>(null)
  const sessionVersion = ref(0)

  // Getters
  const isAuthenticated = computed(() => !!token.value)
  const userRole = computed(() => user.value?.role || null)
  const isAdmin = computed(() => user.value?.role === 'admin')
  const isCs = computed(() => user.value?.role === 'cs')

  // Helper function to load user from storage
  function loadUserFromStorage(): User | null {
    try {
      const stored = localStorage.getItem(USER_KEY)
      return stored ? JSON.parse(stored) : null
    } catch {
      return null
    }
  }

  // Actions
  function setAuth(userData: User, accessToken: string, refresh: string) {
    sessionVersion.value++
    user.value = userData
    token.value = accessToken
    refreshToken.value = refresh
    localStorage.setItem(TOKEN_KEY, accessToken)
    localStorage.setItem(REFRESH_TOKEN_KEY, refresh)
    localStorage.setItem(USER_KEY, JSON.stringify(userData))
  }

  function clearAuth() {
    sessionVersion.value++
    user.value = null
    token.value = null
    refreshToken.value = null
    error.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(REFRESH_TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }

  function setUser(userData: User) {
    user.value = userData
    localStorage.setItem(USER_KEY, JSON.stringify(userData))
  }

  function setToken(accessToken: string, rotatedRefresh?: string) {
    token.value = accessToken
    localStorage.setItem(TOKEN_KEY, accessToken)
    // The server issues a fresh refresh token on every refresh; keep it so an active session
    // is not forced to log out when the original login token expires (BUG-021).
    if (rotatedRefresh) {
      refreshToken.value = rotatedRefresh
      localStorage.setItem(REFRESH_TOKEN_KEY, rotatedRefresh)
    }
  }

  function setError(errorMessage: string | null) {
    error.value = errorMessage
  }

  // Login action
  async function login(credentials: LoginRequest): Promise<boolean> {
    loading.value = true
    error.value = null
    
    try {
      const response = await apiLogin(credentials)
      const { access_token, refresh_token, user: userData } = response.data
      
      setAuth(userData, access_token, refresh_token)
      return true
    } catch (err: unknown) {
      const axiosError = err as { response?: { status?: number; data?: { detail?: string } } }
      if (axiosError.response?.status === 401) {
        error.value = '用户名或密码错误'
      } else if (axiosError.response?.data?.detail) {
        error.value = axiosError.response.data.detail
      } else {
        error.value = '登录失败，请稍后重试'
      }
      return false
    } finally {
      loading.value = false
    }
  }

  // Logout action
  function logout() {
    clearAuth()
    router.push('/login')
  }

  // Fetch current user info.
  // Returns true on success, false when the session is invalid (cleared), and 'network'
  // when the server could not be reached — an outage must not log the user out.
  async function fetchCurrentUser(): Promise<boolean | 'network'> {
    if (!token.value) {
      return false
    }

    try {
      const response = await getCurrentUser()
      setUser(response.data)
      return true
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status
      if (!status || status >= 500) return 'network'
      clearAuth()
      return false
    }
  }

  // Check if user has required role
  function hasRole(roles: string[]): boolean {
    if (!user.value) return false
    return roles.includes(user.value.role)
  }

  // Initialize auth state (call on app startup)
  async function initAuth(): Promise<void> {
    if (token.value && !user.value) {
      await fetchCurrentUser()
    }
  }

  return {
    // State
    user,
    token,
    refreshToken,
    sessionVersion,
    loading,
    error,
    // Getters
    isAuthenticated,
    userRole,
    isAdmin,
    isCs,
    // Actions
    setAuth,
    clearAuth,
    setUser,
    setToken,
    setError,
    login,
    logout,
    fetchCurrentUser,
    hasRole,
    initAuth
  }
})
