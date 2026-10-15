<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock, UserFilled, Phone } from '@element-plus/icons-vue'
import { phoneRules, normalizePhone } from '@/utils/phone'
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
    { min: 2, max: 50, message: '用户名长度应为2-50个字符', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 100, message: '密码长度应为6-100个字符', trigger: 'blur' }
  ]
}

// Register validation rules
const registerRules: FormRules = {
  phone: phoneRules,
  role: [{ required: true, message: '请选择注册角色', trigger: 'change' }],
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 50, message: '用户名长度应为3-50个字符', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 100, message: '密码长度应为6-100个字符', trigger: 'blur' }
  ],
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

// Handle login
async function handleLogin() {
  if (!loginFormRef.value) return
  try {
    await loginFormRef.value.validate()
  } catch {
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
  })

  if (success) {
    clearGuestData()
    ElMessage.success('登录成功')
    const redirect = route.query.redirect as string
    const role = authStore.user?.role
    if (role === 'expert') {
      router.push('/diagnosis')
    } else {
      router.push(redirect && redirect !== '/' ? redirect : '/agri-chat')
    }
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
    const axiosError = err as { response?: { status?: number; data?: { detail?: string } } }
    if (axiosError.response?.status === 409) {
      ElMessage.error('用户名已存在')
    } else if (axiosError.response?.data?.detail) {
      ElMessage.error(axiosError.response.data.detail)
    } else {
      ElMessage.error('注册失败，请稍后重试')
    }
  } finally {
    registerLoading.value = false
  }
}

// Handle Enter key press
function handleKeyPress(event: KeyboardEvent) {
  if (event.key === 'Enter') {
    if (activeTab.value === 'login') handleLogin()
    else handleRegister()
  }
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
      <el-alert v-if="registrationNotice" :title="registrationNotice" type="success" :closable="false" show-icon />
      <div class="login-header">
        <h1 class="login-title">AgriMind 智慧农业决策平台</h1>
        <p class="login-subtitle">农业智能决策与农技专家协同闭环系统</p>
      </div>

      <el-tabs v-model="activeTab" class="login-tabs">
        <el-tab-pane label="登录" name="login">
          <el-form
            ref="loginFormRef"
            :model="loginForm"
            :rules="loginRules"
            class="login-form"
            @keypress="handleKeyPress"
          >
            <el-form-item prop="username">
              <el-input v-model="loginForm.username" placeholder="用户名" size="large" :prefix-icon="User" clearable />
            </el-form-item>
            <el-form-item prop="password">
              <el-input v-model="loginForm.password" type="password" placeholder="密码" size="large" :prefix-icon="Lock" show-password clearable />
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
            @keypress="handleKeyPress"
          >
            <el-form-item prop="username">
              <el-input v-model="registerForm.username" placeholder="用户名" size="large" :prefix-icon="User" clearable />
            </el-form-item>
            <el-form-item prop="display_name">
              <el-input v-model="registerForm.display_name" placeholder="昵称" size="large" :prefix-icon="UserFilled" clearable />
            </el-form-item>
            <el-form-item prop="phone">
              <el-input v-model="registerForm.phone" placeholder="联系电话（必填）" size="large" :prefix-icon="Phone" maxlength="32" autocomplete="tel" clearable />
            </el-form-item>
            <el-form-item prop="role">
              <el-select v-model="registerForm.role" placeholder="选择注册角色" size="large" style="width: 100%">
                <el-option label="农户" value="customer" />
                <el-option label="农技专家（需管理员审核）" value="expert" />
              </el-select>
            </el-form-item>
            <el-alert v-if="registerForm.role === 'expert'" title="农技专家申请提交后需管理员审核，通过后才能登录。" type="info" :closable="false" />
            <el-form-item prop="password">
              <el-input v-model="registerForm.password" type="password" placeholder="密码" size="large" :prefix-icon="Lock" show-password clearable />
            </el-form-item>
            <el-form-item prop="confirmPassword">
              <el-input v-model="registerForm.confirmPassword" type="password" placeholder="确认密码" size="large" :prefix-icon="Lock" show-password clearable />
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
        <p class="footer-text">© 2026 AgriMind 农业智能决策与农技专家协同闭环系统</p>
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
