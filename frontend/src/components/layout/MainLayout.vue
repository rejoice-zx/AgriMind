<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  Expand,
  Fold,
  SwitchButton,
  Moon,
  Sunny
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useSettingsStore } from '@/stores/settings'
import { getMenuItems, type MenuItem } from '@/router'
import { roleLabel } from '@/constants/agri'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const settingsStore = useSettingsStore()

// Sidebar state：桌面端折叠状态记在 settings store（刷新后保留）；窄于 lg 时强制折叠，宽于 xl 恢复用户选择
const COLLAPSE_BELOW = 1280
const MOBILE_BELOW = 768
const isCollapsed = ref(settingsStore.sidebarCollapsed)
const isMobile = ref(false)
const mobileDrawerVisible = ref(false)
let forcedCollapse = false

// Get menu items based on user role, grouped: 农场工作 / 系统管理
const menuItems = computed<MenuItem[]>(() => {
  return getMenuItems(authStore.userRole)
})
const menuGroups = computed(() => {
  const groups = [
    { key: 'farm', title: '农场工作', items: menuItems.value.filter(item => item.group !== 'system') },
    { key: 'system', title: '系统管理', items: menuItems.value.filter(item => item.group === 'system') },
  ]
  return groups.filter(group => group.items.length)
})
const roleText = computed(() => roleLabel(authStore.userRole))
const roleTagType = computed(() => authStore.isAdmin ? 'danger' : authStore.userRole === 'expert' ? 'success' : 'info')

// Current active menu
const activeMenu = computed(() => route.path)

// User display name
const userDisplayName = computed(() => {
  return authStore.user?.display_name || authStore.user?.username || '用户'
})

// Theme
const isDarkTheme = computed(() => settingsStore.theme === 'dark')

// Toggle sidebar collapse
function toggleSidebar() {
  if (isMobile.value) {
    mobileDrawerVisible.value = !mobileDrawerVisible.value
  } else {
    isCollapsed.value = !isCollapsed.value
    forcedCollapse = false
    settingsStore.setSidebarCollapsed(isCollapsed.value)
  }
}

// Handle menu select
function handleMenuSelect(path: string) {
  router.push(path)
  if (isMobile.value) {
    mobileDrawerVisible.value = false
  }
}

// Toggle theme
function toggleTheme() {
  const newTheme = isDarkTheme.value ? 'light' : 'dark'
  settingsStore.setTheme(newTheme)
  applyTheme(newTheme)
}

// Apply theme to document
function applyTheme(theme: 'light' | 'dark') {
  document.documentElement.setAttribute('data-theme', theme)
  if (theme === 'dark') {
    document.documentElement.classList.add('dark')
  } else {
    document.documentElement.classList.remove('dark')
  }
}

// Handle logout
function handleLogout() {
  authStore.logout()
  ElMessage.success('已退出登录')
}

// Check screen size for responsive layout: only react when a breakpoint is crossed,
// so the user's own collapse choice survives ordinary resizes.
let lastBand = ''
function checkScreenSize() {
  const w = window.innerWidth
  const band = w < MOBILE_BELOW ? 'mobile' : w < COLLAPSE_BELOW ? 'narrow' : 'wide'
  if (band === lastBand) return
  lastBand = band
  isMobile.value = band === 'mobile'
  if (band === 'narrow') {
    forcedCollapse = !isCollapsed.value
    isCollapsed.value = true
  } else if (band === 'wide' && forcedCollapse) {
    isCollapsed.value = settingsStore.sidebarCollapsed
    forcedCollapse = false
  }
}

// Initialize
onMounted(() => {
  checkScreenSize()
  window.addEventListener('resize', checkScreenSize)
  // Apply saved theme
  applyTheme(settingsStore.theme)
})
onBeforeUnmount(() => window.removeEventListener('resize', checkScreenSize))

// Watch for route changes to close mobile drawer
watch(() => route.path, () => {
  if (isMobile.value) {
    mobileDrawerVisible.value = false
  }
})
</script>

<template>
  <div class="main-layout">
    <!-- Desktop Sidebar -->
    <aside
      v-if="!isMobile"
      class="sidebar"
      :class="{ 'sidebar-collapsed': isCollapsed }"
    >
      <div class="sidebar-header">
        <div class="logo-container">
          <img src="/favicon.svg" alt="logo" class="logo-icon" style="width: 28px; height: 28px;" />
          <span
            v-show="!isCollapsed"
            class="logo-text"
          >AgriMind 农业协作</span>
        </div>
      </div>
      
      <el-menu
        :default-active="activeMenu"
        :collapse="isCollapsed"
        :collapse-transition="false"
        class="sidebar-menu"
        @select="handleMenuSelect"
      >
        <template v-for="group in menuGroups" :key="group.key">
          <div v-if="menuGroups.length > 1 && !isCollapsed" class="menu-group-title">{{ group.title }}</div>
          <div v-else-if="menuGroups.length > 1 && group.key === 'system'" class="menu-group-divider" role="separator" />
          <el-menu-item
            v-for="item in group.items"
            :key="item.path"
            :index="item.path"
          >
            <el-icon v-if="item.icon">
              <component :is="item.icon" />
            </el-icon>
            <template #title>
              {{ item.title }}
            </template>
          </el-menu-item>
        </template>
      </el-menu>
    </aside>

    <!-- Mobile Drawer -->
    <el-drawer
      v-if="isMobile"
      v-model="mobileDrawerVisible"
      direction="ltr"
      :with-header="false"
      size="240px"
      class="mobile-drawer"
    >
      <div class="sidebar-header">
        <div class="logo-container">
          <img src="/favicon.svg" alt="logo" class="logo-icon" style="width: 28px; height: 28px;" />
          <span class="logo-text">AgriMind 农业协作</span>
        </div>
      </div>
      
      <el-menu
        :default-active="activeMenu"
        class="sidebar-menu"
        @select="handleMenuSelect"
      >
        <template v-for="group in menuGroups" :key="group.key">
          <div v-if="menuGroups.length > 1" class="menu-group-title">{{ group.title }}</div>
          <div v-else-if="menuGroups.length > 1 && group.key === 'system'" class="menu-group-divider" role="separator" />
          <el-menu-item
            v-for="item in group.items"
            :key="item.path"
            :index="item.path"
          >
            <el-icon v-if="item.icon">
              <component :is="item.icon" />
            </el-icon>
            <template #title>
              {{ item.title }}
            </template>
          </el-menu-item>
        </template>
      </el-menu>
    </el-drawer>

    <!-- Main Content Area -->
    <div
      class="main-container"
      :class="{ 'main-expanded': isCollapsed && !isMobile }"
    >
      <!-- Top Bar -->
      <header class="top-bar">
        <div class="top-bar-left">
          <el-button
            class="toggle-btn"
            :icon="isCollapsed || isMobile ? Expand : Fold"
            :aria-label="isMobile ? '打开导航菜单' : (isCollapsed ? '展开侧栏' : '收起侧栏')"
            @click="toggleSidebar"
          />
          <span class="page-title">{{ route.meta.title || '首页' }}</span>
        </div>
        
        <div class="top-bar-right">
          <!-- Theme Toggle -->
          <el-tooltip :content="isDarkTheme ? '切换亮色主题' : '切换暗色主题'">
            <el-button
              class="theme-btn"
              :icon="isDarkTheme ? Sunny : Moon"
              :aria-label="isDarkTheme ? '切换亮色主题' : '切换暗色主题'"
              @click="toggleTheme"
            />
          </el-tooltip>
          
          <!-- User Dropdown -->
          <el-dropdown
            trigger="click"
            @command="handleLogout"
          >
            <div class="user-info" role="button" tabindex="0" :aria-label="`当前用户 ${userDisplayName}，${roleText}，打开账号菜单`">
              <el-avatar
                :size="32"
                class="user-avatar"
              >
                {{ userDisplayName.charAt(0).toUpperCase() }}
              </el-avatar>
              <span class="user-name">{{ userDisplayName }}</span>
              <el-tag
                size="small"
                :type="roleTagType"
                class="role-tag"
              >
                {{ roleText }}
              </el-tag>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item
                  :icon="SwitchButton"
                  command="logout"
                >
                  退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <!-- Page Content -->
      <main class="page-content">
        <slot />
      </main>
    </div>
  </div>
</template>

<style scoped>
.main-layout {
  display: flex;
  min-height: 100vh;
  background-color: var(--app-bg);
}

/* Sidebar */
.sidebar {
  width: 220px;
  background-color: var(--app-surface);
  border-right: 1px solid var(--app-border-light);
  display: flex;
  flex-direction: column;
  transition: width 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  position: fixed;
  left: 0;
  top: 0;
  bottom: 0;
  z-index: 100;
  box-shadow: var(--app-shadow-sm);
}

.sidebar-collapsed {
  width: 64px;
}

.sidebar-header {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid var(--app-border-light);
  padding: 0 var(--app-space-4);
}

.logo-container {
  display: flex;
  align-items: center;
  gap: 10px;
  overflow: hidden;
}

.logo-icon {
  color: var(--el-color-primary);
  flex-shrink: 0;
}

.logo-text {
  font-size: 15px;
  font-weight: 700;
  color: var(--app-text);
  white-space: nowrap;
  letter-spacing: -0.01em;
}

.sidebar-menu {
  flex: 1;
  border-right: none;
  overflow-y: auto;
  padding: 10px 0;
}

.sidebar-menu :deep(.el-menu-item) {
  margin: 4px 10px;
  border-radius: var(--app-radius-md);
  height: 42px;
  line-height: 42px;
  color: var(--app-text-secondary);
  font-size: var(--app-text-sm);
  transition: var(--app-transition);
}

.sidebar-menu :deep(.el-menu-item:hover) {
  background-color: var(--app-hover);
  color: var(--app-text);
}

.sidebar-menu :deep(.el-menu-item.is-active) {
  background-color: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-weight: 600;
}

.menu-group-title {
  padding: var(--app-space-3) var(--app-space-5) var(--app-space-1);
  font-size: var(--app-text-xs);
  font-weight: 500;
  color: var(--app-text-placeholder);
  letter-spacing: 0.04em;
}

.menu-group-divider {
  height: 1px;
  margin: var(--app-space-2) var(--app-space-3);
  background: var(--app-border-light);
}

.sidebar-menu:not(.el-menu--collapse) {
  width: 100%;
}

/* Main Container */
.main-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  margin-left: 220px;
  width: calc(100% - 220px);
  max-width: calc(100% - 220px);
  min-width: 0;
  box-sizing: border-box;
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  min-height: 100vh;
}

.main-expanded {
  margin-left: 64px;
  width: calc(100% - 64px);
  max-width: calc(100% - 64px);
}

/* Top Bar - 毛玻璃 */
.top-bar {
  height: 60px;
  background-color: var(--app-topbar-bg);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--app-border-light);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--app-space-6);
  position: sticky;
  top: 0;
  z-index: 99;
}

.top-bar-left {
  display: flex;
  align-items: center;
  gap: var(--app-space-4);
}

.toggle-btn {
  border: none;
  background: transparent;
  font-size: 18px;
  color: var(--app-text-secondary);
  width: 32px;
  height: 32px;
  border-radius: var(--app-radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: var(--app-transition);
}

.toggle-btn:hover {
  background: var(--app-hover);
  color: var(--app-text);
}

.page-title {
  font-size: var(--app-text-lg);
  font-weight: 600;
  color: var(--app-text);
}

.top-bar-right {
  display: flex;
  align-items: center;
  gap: var(--app-space-4);
}

.theme-btn {
  border: none;
  background: transparent;
  font-size: 18px;
  color: var(--app-text-secondary);
  width: 32px;
  height: 32px;
  border-radius: var(--app-radius-sm);
  transition: var(--app-transition);
}

.theme-btn:hover {
  background: var(--app-hover);
  color: var(--app-text);
}

.user-info {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  padding: 4px 10px;
  border-radius: var(--app-radius-round);
  border: 1px solid var(--app-border-light);
  background: var(--app-surface-2);
  transition: var(--app-transition);
}

.user-info:hover {
  background-color: var(--app-hover);
  border-color: var(--app-border);
}

.user-avatar {
  background: var(--el-color-primary);
  color: #ffffff;
  font-weight: 600;
}

.user-name {
  font-size: var(--app-text-sm);
  font-weight: 500;
  color: var(--app-text);
}

/* Page Content */
.page-content {
  flex: 1;
  padding: 0;
  overflow-y: auto;
  overflow-x: hidden;
  background-color: var(--app-bg);
  min-width: 0;
  width: 100%;
  box-sizing: border-box;
}

/* Mobile Styles */
@media (max-width: 767px) {
  .top-bar {
    padding: 0 var(--app-space-3);
  }

  .main-container {
    margin-left: 0 !important;
    width: 100% !important;
    max-width: 100% !important;
  }

  .main-expanded {
    margin-left: 0 !important;
    width: 100% !important;
    max-width: 100% !important;
  }

  .user-name,
  .role-tag {
    display: none;
  }

  .page-title {
    font-size: 15px;
  }
}

/* Dark Theme（仅 Element 组件局部微调，颜色令牌由 html.dark 统一驱动） */
html.dark .sidebar-menu,
html.dark :deep(.el-menu) {
  background-color: var(--app-surface);
}

html.dark :deep(.el-menu-item.is-active) {
  background-color: var(--app-hover);
}

html.dark :deep(.el-drawer) {
  background-color: var(--app-surface);
}
</style>
