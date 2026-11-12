import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw, NavigationGuardNext, RouteLocationNormalized } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

// Extend RouteMeta type
declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
    roles?: string[]
    title?: string
    icon?: string
    hidden?: boolean
    /** 侧栏分组：农场工作 / 系统管理 */
    group?: 'farm' | 'system'
  }
}

// Menu item interface for sidebar navigation
export interface MenuItem {
  path: string
  name: string
  title: string
  icon?: string
  roles: string[]
  group: 'farm' | 'system'
}

// Route definitions with meta information
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { 
      public: true,
      title: '登录',
      hidden: true
    }
  },
  {
    path: '/',
    // 按角色落地：专家去会诊工作台，其他角色去农场工作台（与登录后的去向一致）
    redirect: () => getFirstAccessibleRoute(useAuthStore().userRole) || '/dashboard',
    meta: { hidden: true }
  },
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: () => import('@/views/Dashboard.vue'),
    meta: { 
      roles: ['admin', 'cs', 'expert', 'customer'],
      title: '农场工作台',
      icon: 'HomeFilled',
      group: 'farm'
    }
  },
  {
    path: '/calendar',
    name: 'FarmingCalendar',
    component: () => import('@/views/FarmingCalendar.vue'),
    meta: { 
      roles: ['admin', 'cs', 'expert', 'customer'],
      title: '农事日历',
      icon: 'Calendar',
      group: 'farm'
    }
  },
  ...['/customer-chat', '/workbench', '/products', '/human-service'].map(path => ({
    path, redirect: '/retired', meta: { hidden: true }
  })),
  {
    path: '/retired',
    component: () => import('@/views/RetiredFeature.vue'),
    meta: { hidden: true, title: '功能已下线', roles: ['admin', 'cs', 'expert', 'customer'] }
  },
  {
    path: '/agri-chat',
    name: 'AgriChat',
    component: () => import('@/views/AgriChat.vue'),
    meta: { 
      roles: ['admin', 'cs', 'expert', 'customer'],
      title: '农业资料分析',
      icon: 'ChatDotRound',
      group: 'farm'
    }
  },
  {
    path: '/farms',
    name: 'AgriFarms',
    component: () => import('@/views/AgriFarms.vue'),
    meta: { 
      roles: ['admin', 'cs', 'expert', 'customer'],
      title: '农场与田块',
      icon: 'OfficeBuilding',
      group: 'farm'
    }
  },
  {
    path: '/diagnosis',
    name: 'AgriDiagnosis',
    component: () => import('@/views/AgriDiagnosis.vue'),
    meta: { 
      roles: ['admin', 'cs', 'expert', 'customer'],
      title: '病害初诊与会诊',
      icon: 'Camera',
      group: 'farm'
    }
  },
  {
    path: '/knowledge',
    name: 'Knowledge',
    component: () => import('@/views/Knowledge.vue'),
    meta: { 
      roles: ['admin'],
      title: '知识库管理',
      icon: 'Collection',
      group: 'system'
    }
  },
  {
    path: '/statistics',
    name: 'Statistics',
    component: () => import('@/views/Statistics.vue'),
    meta: { 
      roles: ['admin'],
      title: '平台运营统计',
      icon: 'DataAnalysis',
      group: 'system'
    }
  },
  {
    path: '/performance',
    name: 'Performance',
    component: () => import('@/views/Performance.vue'),
    meta: { 
      roles: ['admin'],
      title: '性能监控',
      icon: 'Monitor',
      group: 'system'
    }
  },
  {
    path: '/logs',
    name: 'Logs',
    component: () => import('@/views/Logs.vue'),
    meta: { 
      roles: ['admin'],
      title: '系统日志',
      icon: 'Document',
      group: 'system'
    }
  },
  {
    path: '/users',
    name: 'Users',
    component: () => import('@/views/Users.vue'),
    meta: { 
      roles: ['admin'],
      title: '用户管理',
      icon: 'User',
      group: 'system'
    }
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('@/views/Settings.vue'),
    meta: { 
      roles: ['admin'],
      title: '系统设置',
      icon: 'Setting',
      group: 'system'
    }
  },
  {
    path: '/network-error',
    name: 'NetworkError',
    component: () => import('@/views/NetworkError.vue'),
    meta: { public: true, hidden: true, title: '网络连接失败' }
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/NotFound.vue'),
    meta: { hidden: true, title: '页面不存在', roles: ['admin', 'cs', 'expert', 'customer'] }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// Navigation guard for authentication and authorization
router.beforeEach(async (
  to: RouteLocationNormalized,
  _from: RouteLocationNormalized,
  next: NavigationGuardNext
) => {
  const authStore = useAuthStore()
  
  // Public routes don't require authentication
  if (to.meta.public) {
    // If already authenticated and trying to access login, redirect to appropriate home
    if (authStore.isAuthenticated && to.path === '/login') {
      const landing = getFirstAccessibleRoute(authStore.userRole) || '/'
      next(landing)
      return
    }
    next()
    return
  }
  
  // Check if user is authenticated
  if (!authStore.isAuthenticated) {
    next({
      path: '/login',
      query: { redirect: to.fullPath }
    })
    return
  }
  
  // Initialize auth if needed (fetch user info if not loaded)
  if (!authStore.user) {
    const result = await authStore.fetchCurrentUser()
    if (result === 'network') {
      // 网络或服务暂不可用不等于登录失效，不清除会话、不跳登录页。
      next({ path: '/network-error', query: { redirect: to.fullPath } })
      return
    }
    if (!result) {
      next({ path: '/login', query: { redirect: to.fullPath } })
      return
    }
  }
  
  // Check role-based access
  const requiredRoles = to.meta.roles as string[] | undefined
  if (requiredRoles && requiredRoles.length > 0) {
    if (!authStore.hasRole(requiredRoles)) {
      // User doesn't have required role - redirect to first accessible route
      const accessibleRoute = getFirstAccessibleRoute(authStore.userRole)
      if (accessibleRoute) {
        next(accessibleRoute)
      } else {
        // No accessible routes - logout
        authStore.logout()
        next('/login')
      }
      return
    }
  }
  
  next()
})

// Helper function to get first accessible route for a role
export function getFirstAccessibleRoute(role: string | null): string | null {
  if (!role) return null
  
  // 专家进入会诊工作台；其余角色（含遗留的早期客服账号）进入农场工作台
  if (role === 'expert') return '/diagnosis'
  if (['admin', 'cs', 'customer'].includes(role)) return '/dashboard'
  return null
}

// Get menu items filtered by user role
export function getMenuItems(role: string | null): MenuItem[] {
  if (!role) return []
  
  return routes
    .filter(route => {
      // Exclude hidden routes and routes without roles
      if (route.meta?.hidden) return false
      if (!route.meta?.roles) return false
      // Check if user has required role
      return route.meta.roles.includes(role)
    })
    .map(route => ({
      path: route.path,
      name: route.name as string,
      title: route.meta?.title || route.name as string,
      icon: route.meta?.icon,
      roles: route.meta?.roles || [],
      group: route.meta?.group || 'farm'
    }))
}

// 浏览器标签标题随页面变化，便于多标签切换与历史记录辨认
router.afterEach?.(to => {
  if (typeof document !== 'undefined') {
    document.title = to.meta.title ? `${to.meta.title} · AgriMind` : 'AgriMind 农业信息管理与农技协作'
  }
})

// 懒加载页面的代码块下载失败（常见于部署更新后或网络中断）时，提示刷新而不是白屏
router.onError?.(error => {
  if (/Failed to fetch dynamically imported module|Loading chunk|Importing a module script failed/i.test(String(error?.message))) {
    router.push({ path: '/network-error', query: { reason: 'chunk' } })
  }
})


export default router
