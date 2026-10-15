import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

// Storage key for theme
const THEME_KEY = 'theme'

// Get initial theme from localStorage or system preference
function getInitialTheme(): 'light' | 'dark' {
  const stored = localStorage.getItem(THEME_KEY) as 'light' | 'dark' | null
  if (stored) {
    return stored
  }
  // Check system preference
  if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    return 'dark'
  }
  return 'light'
}

export const useSettingsStore = defineStore('settings', () => {
  // State
  const theme = ref<'light' | 'dark'>(getInitialTheme())
  const sidebarCollapsed = ref<boolean>(
    localStorage.getItem('sidebarCollapsed') === 'true'
  )

  // Getters
  const isDarkTheme = computed(() => theme.value === 'dark')

  // Actions
  function setTheme(newTheme: 'light' | 'dark') {
    theme.value = newTheme
    localStorage.setItem(THEME_KEY, newTheme)
    applyTheme(newTheme)
  }

  function toggleTheme() {
    const newTheme = theme.value === 'dark' ? 'light' : 'dark'
    setTheme(newTheme)
  }

  function applyTheme(themeValue: 'light' | 'dark') {
    document.documentElement.setAttribute('data-theme', themeValue)
    if (themeValue === 'dark') {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  }

  function setSidebarCollapsed(collapsed: boolean) {
    sidebarCollapsed.value = collapsed
    localStorage.setItem('sidebarCollapsed', String(collapsed))
  }

  // Initialize theme on store creation
  function initTheme() {
    applyTheme(theme.value)
  }

  return {
    // State
    theme,
    sidebarCollapsed,
    // Getters
    isDarkTheme,
    // Actions
    setTheme,
    toggleTheme,
    applyTheme,
    setSidebarCollapsed,
    initTheme
  }
})
