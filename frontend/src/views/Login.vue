<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock, UserFilled, Phone } from '@element-plus/icons-vue'
import { phoneRules, normalizePhone } from '@/utils/phone'
import { passwordRules, PASSWORD_HINT } from '@/utils/password'
import { errorText } from '@/utils/errorText'
import { useAuthStore } from '@/stores/auth'
import { registerCustomer } from '@/api/auth'
import { getFirstAccessibleRoute } from '@/router'
import {
  clearGuestIdentityData,
  getGuestConversationIds,
  getGuestToken,
  getOrCreateGuestDeviceId,
} from '@/utils/guestIdentity'
import type { FormInstance, FormRules } from 'element-plus'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

const GUEST_TOKEN_KEY = 'guest_token'
const VISITOR_ID_KEY = 'visitor_id'

// Active tab: login or register
const activeTab = ref('login')

// Login form
const loginFormRef = ref<FormInstance>()
const loginForm = reactive({
  username: '',
  password: ''
})

// Register form
const registerFormRef = ref<FormInstance>()
const registerForm = reactive({
  username: '',
  password: '',
  confirmPassword: '',
  display_name: '',
  phone: '',
  role: 'customer' as 'customer' | 'expert'
})

const registerLoading = ref(false)
const registrationNotice = ref('')

// Login validation rules
const loginRules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { max: 50, message: '用户名不超过 50 个字符', trigger: 'blur' }
  ],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

// Register validation rules
const registerRules: FormRules = {
  phone: phoneRules,
  role: [{ required: true, message: '请选择注册角色', trigger: 'change' }],
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 50, message: '用户名长度应为3-50个字符', trigger: 'blur' }
  ],
  password: passwordRules,
  confirmPassword: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    {
      validator: (_rule: unknown, value: string, callback: (err?: Error) => void) => {
        if (value !== registerForm.password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur'
    }
  ],
  display_name: [
    { required: true, message: '请输入昵称', trigger: 'blur' },
    { min: 1, max: 100, message: '昵称长度应为1-100个字符', trigger: 'blur' }
  ]
}

/** Get stored visitor_id for conversation migration */
function getVisitorId(): string | undefined {
  return localStorage.getItem(VISITOR_ID_KEY) || undefined
}

/** Clear guest token after successful login/register */
function clearGuestData() {
  localStorage.removeItem(GUEST_TOKEN_KEY)
  localStorage.removeItem(VISITOR_ID_KEY)
  clearGuestIdentityData()
}

// 登录后去向：只接受站内路径的 redirect（防止 //evil.com 之类的跳转），否则按角色去第一个可访问页面
function safeRedirect(value: unknown): string | null {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return null
  if (value === '/' || value.startsWith('/login')) return null
  return value
}

let loginSubmitting = false

// Handle login
async function handleLogin() {
  // 回车与点击可能同时触发，登录进行中不重复提交；标记必须在第一个 await 之前同步设置
  if (!loginFormRef.value || loginSubmitting || authStore.loading) return
  loginSubmitting = true
  try {
    await loginFormRef.value.validate()
  } catch {
    loginSubmitting = false
    return
  }

  const visitorId = getVisitorId()
  const success = await authStore.login({
    username: loginForm.username,
    password: loginForm.password,
    visitor_id: visitorId,
    guest_device_id: getOrCreateGuestDeviceId(),
    guest_token: getGuestToken(),
    guest_conversation_ids: getGuestConversationIds(),
  }).finally(() => { loginSubmitting = false })

  if (success) {
    clearGuestData()
    ElMessage.success('登录成功')
    router.push(safeRedirect(route.query.redirect) || getFirstAccessibleRoute(authStore.user?.role ?? null) || '/')
  }
}

// Handle customer registration
async function handleRegister() {
  if (!registerFormRef.value || registerLoading.value) return
  registerLoading.value = true
  try {
    await registerFormRef.value.validate()
  } catch {
    registerLoading.value = false
    return
  }

  try {
    const visitorId = getVisitorId()
    const res = await registerCustomer({
      username: registerForm.username,
      password: registerForm.password,
      display_name: registerForm.display_name,
      phone: normalizePhone(registerForm.phone),
      role: registerForm.role,
      visitor_id: visitorId,
      guest_device_id: getOrCreateGuestDeviceId(),
      guest_token: getGuestToken(),
      guest_conversation_ids: getGuestConversationIds(),
    })
    if ('status' in res.data && res.data.status === 'pending') {
      registrationNotice.value = res.data.message
      ElMessage.success(res.data.message)
      loginForm.username = registerForm.username
      loginForm.password = ''
      registerForm.password = ''; registerForm.confirmPassword = ''
      activeTab.value = 'login'
      return
    }
    if (!('access_token' in res.data)) return
    const { access_token, refresh_token, user } = res.data
    authStore.setAuth(user, access_token, refresh_token)
    registrationNotice.value = ''
    clearGuestData()
    ElMessage.success('注册成功')
    router.push('/')
  } catch (err: unknown) {
    // 409 可能是用户名或其他唯一字段冲突，优先展示后端说明
    const status = (err as { response?: { status?: number } })?.response?.status
    ElMessage.error(errorText(err, status === 409 ? '用户名已被占用，请换一个' : '注册失败，请稍后重试'))
  } finally {
    registerLoading.value = false
  }
}

// Handle Enter key press（输入法组字中的回车不提交）
function handleKeyPress(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  if (activeTab.value === 'login') handleLogin()
  else handleRegister()
}

// Check if already authenticated on mount
onMounted(() => {
  if (authStore.isAuthenticated && authStore.user) {
    router.push(getFirstAccessibleRoute(authStore.user.role) || '/')
  }
})
</script>

<template>
  <div class="login-container">
    <div class="login-card">
      <div class="login-header">
        <h1 class="login-title">AgriMind</h1>
        <p class="login-subtitle">农场档案、农事记录与农技专家会诊</p>
      </div>
      <el-alert v-if="registrationNotice" class="notice" type="info" :closable="false" show-icon
        :title="registrationNotice" description="审核通过前无法登录；结果请留意管理员通知。" />

      <el-tabs v-model="activeTab" class="login-tabs">
        <el-tab-pane label="登录" name="login">
          <el-form
            ref="loginFormRef"
            :model="loginForm"
            :rules="loginRules"
            class="login-form"
            @keydown="handleKeyPress"
            @submit.prevent
          >
            <el-form-item prop="username">
              <el-input v-model="loginForm.username" placeholder="用户名" size="large" :prefix-icon="User" clearable
                autocomplete="username" aria-label="用户名" />
            </el-form-item>
            <el-form-item prop="password">
              <el-input v-model="loginForm.password" type="password" placeholder="密码" size="large" :prefix-icon="Lock" show-password
                autocomplete="current-password" aria-label="密码" />
            </el-form-item>
            <el-form-item v-if="authStore.error" class="error-item">
              <el-alert :title="authStore.error" type="error" :closable="false" show-icon />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" size="large" class="login-button" :loading="authStore.loading" @click="handleLogin">
                {{ authStore.loading ? '登录中...' : '登录' }}
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="注册" name="register">
          <el-form
            ref="registerFormRef"
            :model="registerForm"
            :rules="registerRules"
            class="login-form"
            @keydown="handleKeyPress"
            @submit.prevent
          >
            <el-form-item prop="role">
              <el-radio-group v-model="registerForm.role" class="role-switch" aria-label="注册身份">
                <el-radio-button value="customer">我是农户</el-radio-button>
                <el-radio-button value="expert">申请成为农技专家</el-radio-button>
              </el-radio-group>
              <p class="role-note">
                {{ registerForm.role === 'expert'
                  ? '专家账号需管理员审核，通过后才能登录；审核期间请保持电话畅通。'
                  : '农户账号注册后即可登录，管理自己的农场与农事记录。' }}
              </p>
            </el-form-item>
            <el-form-item prop="username">
              <el-input v-model="registerForm.username" placeholder="用户名（3-50 个字符，用于登录）" size="large" :prefix-icon="User" clearable
                autocomplete="username" aria-label="用户名" />
            </el-form-item>
            <el-form-item prop="display_name">
              <el-input v-model="registerForm.display_name" placeholder="显示名称（例如姓名或合作社名）" size="large" :prefix-icon="UserFilled" clearable
                maxlength="100" autocomplete="name" aria-label="显示名称" />
            </el-form-item>
            <el-form-item prop="phone">
              <el-input v-model="registerForm.phone" placeholder="联系电话（必填）" size="large" :prefix-icon="Phone" maxlength="32" autocomplete="tel" clearable
                aria-label="联系电话" />
            </el-form-item>
            <el-form-item prop="password">
              <el-input v-model="registerForm.password" type="password" :placeholder="`密码：${PASSWORD_HINT}`" size="large" :prefix-icon="Lock" show-password
                autocomplete="new-password" aria-label="密码" />
            </el-form-item>
            <el-form-item prop="confirmPassword">
              <el-input v-model="registerForm.confirmPassword" type="password" placeholder="再次输入密码" size="large" :prefix-icon="Lock" show-password
                autocomplete="new-password" aria-label="确认密码" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" size="large" class="login-button" :loading="registerLoading" @click="handleRegister">
                {{ registerLoading ? '提交中...' : registerForm.role === 'expert' ? '提交专家注册申请' : '注册农户账号' }}
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>
      </el-tabs>

      <div class="login-footer">
        <p class="footer-text">AgriMind · 课程展示项目</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-container {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--app-bg);
  padding: var(--app-space-5);
}
.login-card {
  width: 100%;
  max-width: 420px;
  background: var(--app-surface);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-xl);
  box-shadow: var(--app-shadow-lg);
  padding: var(--app-space-7);
}
.login-header {
  text-align: center;
  margin-bottom: var(--app-space-6);
}
.login-title {
  font-size: var(--app-text-3xl);
  font-weight: 600;
  color: var(--app-text);
  margin: 0 0 var(--app-space-2) 0;
}
.login-subtitle {
  font-size: var(--app-text-base);
  color: var(--app-text-secondary);
  margin: 0;
}
.notice {
  margin-bottom: var(--app-space-4);
}
.role-switch {
  display: flex;
  width: 100%;
}
.role-switch :deep(.el-radio-button) {
  flex: 1;
}
.role-switch :deep(.el-radio-button__inner) {
  width: 100%;
}
.role-note {
  margin: var(--app-space-2) 0 0;
  font-size: var(--app-text-xs);
  line-height: 1.6;
  color: var(--app-text-secondary);
}
@media (max-width: 480px) {
  .login-container { padding: var(--app-space-3); align-items: flex-start; }
  .login-card { padding: var(--app-space-5); box-shadow: none; }
  .login-title { font-size: var(--app-text-2xl); }
}
.login-tabs :deep(.el-tabs__header) {
  margin-bottom: var(--app-space-5);
}
.login-tabs :deep(.el-tabs__nav-wrap::after) {
  height: 1px;
}
.login-form :deep(.el-input__wrapper) {
  border-radius: var(--app-radius-md);
}
.login-form :deep(.el-form-item) {
  margin-bottom: var(--app-space-5);
}
.error-item { margin-bottom: var(--app-space-4); }
.error-item :deep(.el-alert) { width: 100%; }
.login-button {
  width: 100%;
  border-radius: var(--app-radius-md);
  font-size: var(--app-text-lg);
  height: 44px;
}

.login-footer {
  text-align: center;
  padding-top: var(--app-space-5);
  border-top: 1px solid var(--app-border-light);
}
.footer-text {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin: 0;
}
</style>
