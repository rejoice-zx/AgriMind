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
  }
}

// Menu item interface for sidebar navigation
export interface MenuItem {
  path: string
  name: string
  title: string
  icon?: string
  roles: string[]
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
    redirect: '/dashboard',
    meta: { hidden: true }
  },
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: () => import('@/views/Dashboard.vue'),
    meta: { 
      roles: ['admin', 'cs', 'expert', 'customer'],
      title: '农场决策大盘',
      icon: 'Odometer'
    }
  },
  {
    path: '/calendar',
    name: 'FarmingCalendar',
    component: () => import('@/views/FarmingCalendar.vue'),
    meta: { 
      roles: ['admin', 'cs', 'expert', 'customer'],
      title: '农事作业日历',
      icon: 'Calendar'
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
      title: '农业智能决策',
      icon: 'ChatDotRound'
    }
  },
  {
    path: '/farms',
    name: 'AgriFarms',
    component: () => import('@/views/AgriFarms.vue'),
    meta: { 
      roles: ['admin', 'cs', 'expert', 'customer'],
      title: '农场田块管理',
      icon: 'OfficeBuilding'
    }
  },
  {
    path: '/diagnosis',
    name: 'AgriDiagnosis',
    component: () => import('@/views/AgriDiagnosis.vue'),
    meta: { 
      roles: ['admin', 'cs', 'expert', 'customer'],
      title: '病害诊断与会诊',
      icon: 'Camera'
    }
  },
  {
    path: '/knowledge',
    name: 'Knowledge',
    component: () => import('@/views/Knowledge.vue'),
    meta: { 
      roles: ['admin'],
      title: '知识库管理',
      icon: 'Collection'
    }
  },
  {
    path: '/statistics',
    name: 'Statistics',
    component: () => import('@/views/Statistics.vue'),
    meta: { 
      roles: ['admin'],
      title: '数据统计',
      icon: 'DataAnalysis'
    }
  },
  {
    path: '/performance',
    name: 'Performance',
    component: () => import('@/views/Performance.vue'),
    meta: { 
      roles: ['admin'],
      title: '性能监控',
      icon: 'Odometer'
    }
  },
  {
    path: '/logs',
    name: 'Logs',
    component: () => import('@/views/Logs.vue'),
    meta: { 
      roles: ['admin'],
      title: '日志管理',
      icon: 'Document'
    }
  },
  {
    path: '/users',
    name: 'Users',
    component: () => import('@/views/Users.vue'),
    meta: { 
      roles: ['admin'],
      title: '用户管理',
      icon: 'User'
    }
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('@/views/Settings.vue'),
    meta: { 
      roles: ['admin'],
      title: '系统设置',
      icon: 'Setting'
    }
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    redirect: '/',
    meta: { hidden: true }
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
    const success = await authStore.fetchCurrentUser()
    if (!success) {
      next('/login')
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
  
  // 管理员、农户默认进入农场决策大盘，专家进入病害会诊，客服进入人工客服
  if (role === 'admin') return '/dashboard'
  if (role === 'cs') return '/dashboard'
  if (role === 'expert') return '/diagnosis'
  if (role === 'customer') return '/dashboard'

  
  for (const route of routes) {
    if (route.meta?.roles?.includes(role) && !route.meta?.hidden) {
      return route.path
    }
  }
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
      roles: route.meta?.roles || []
    }))
}


export default router
