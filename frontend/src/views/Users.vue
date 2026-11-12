<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Refresh, Check, Close } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import {
  getUsers,
  createUser,
  updateUser,
  deleteUser,
  getRegistrations,
  approveRegistration,
  rejectRegistration,
  resetPassword
} from '@/api/users'
import { useAuthStore } from '@/stores/auth'
import PageHeader from '@/components/PageHeader.vue'
import type { User } from '@/types'
import type { PendingRegistration } from '@/api/users'
import { phoneRules, normalizePhone } from '@/utils/phone'
import { passwordRules, passwordProblem, PASSWORD_HINT } from '@/utils/password'
import { roleLabel } from '@/constants/agri'
import { errorText, isDismissed } from '@/utils/errorText'
import { useNarrowScreen } from '@/composables/useBreakpoint'

const authStore = useAuthStore()
const isSuperAdmin = computed(() => authStore.user?.username === 'admin')
const isNarrow = useNarrowScreen()

// 用户列表：服务端分页（后端单页上限 100），不再在前端按角色拆三张表——超过一页时会漏人
const loading = ref(false)
const loadFailed = ref(false)
const userList = ref<User[]>([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)

const filters = reactive({
  role: '',
  isActive: undefined as boolean | undefined,
  username: ''
})

const ROLE_FILTERS = [
  { value: '', label: '全部' },
  { value: 'admin', label: '管理员' },
  { value: 'expert', label: '农技专家' },
  { value: 'customer', label: '农户' },
]

// 注册审核：默认只看待审核，处理完的记录按需切换查看
const pendingList = ref<PendingRegistration[]>([])
const pendingLoading = ref(false)
const registrationPage = ref(1)
const registrationTotal = ref(0)
const registrationFilter = ref<'' | 'pending' | 'approved' | 'rejected'>('pending')
const reviewingId = ref<number | null>(null)
const submittingUser = ref(false)
const togglingId = ref<number | null>(null)

// Dialog state
const dialogVisible = ref(false)
const isEditing = ref(false)
const editingId = ref<number | null>(null)
const editingOriginalRole = ref('')
const dialogTitle = computed(() => isEditing.value ? '编辑用户' : '添加用户')

const formRef = ref<FormInstance>()
const form = reactive({ username: '', password: '', display_name: '', email: '', phone: '', role: 'customer' })
const rules: FormRules = {
  phone: phoneRules,
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 50, message: '用户名长度应为 3-50 个字符', trigger: 'blur' }
  ],
  password: passwordRules,
  display_name: [
    { required: true, message: '请输入显示名称', trigger: 'blur' },
    { min: 1, max: 100, message: '显示名称长度应为 1-100 个字符', trigger: 'blur' }
  ],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }]
}
const editingSelf = computed(() => isEditing.value && editingId.value === authStore.user?.id)

// Password reset dialog
const passwordDialogVisible = ref(false)
const passwordUser = ref<User | null>(null)
const newPassword = ref('')
const resettingPassword = ref(false)
const newPasswordProblem = computed(() => newPassword.value ? passwordProblem(newPassword.value) : '')

async function loadUserList() {
  loading.value = true
  loadFailed.value = false
  try {
    const response = await getUsers({
      page: currentPage.value,
      page_size: pageSize.value,
      role: filters.role || undefined,
      is_active: filters.isActive,
      username: filters.username.trim() || undefined
    })
    userList.value = response.data.items
    total.value = response.data.total
  } catch (error: unknown) {
    userList.value = []; total.value = 0; loadFailed.value = true
    ElMessage.error(errorText(error, '读取用户列表失败'))
  } finally {
    loading.value = false
  }
}

async function loadPendingRegistrations() {
  pendingLoading.value = true
  try {
    const response = await getRegistrations({ page: registrationPage.value, page_size: 20,
      registration_status: registrationFilter.value || undefined })
    pendingList.value = response.data.items
    registrationTotal.value = response.data.total
  } catch (error: unknown) {
    pendingList.value = []; registrationTotal.value = 0
    ElMessage.error(errorText(error, '读取注册审核列表失败'))
  } finally {
    pendingLoading.value = false
  }
}

function filterRegistrations() { registrationPage.value = 1; void loadPendingRegistrations() }

function handleSearch() { currentPage.value = 1; loadUserList() }
function selectRole(role: string) { filters.role = role; handleSearch() }
function handleReset() {
  filters.role = ''; filters.isActive = undefined; filters.username = ''
  currentPage.value = 1; loadUserList()
}
function handleSizeChange(size: number) { pageSize.value = size; currentPage.value = 1; loadUserList() }

function openAddDialog() {
  isEditing.value = false; editingId.value = null; editingOriginalRole.value = ''; resetForm()
  dialogVisible.value = true
}
function openEditDialog(user: User) {
  isEditing.value = true; editingId.value = user.id; editingOriginalRole.value = user.role
  form.username = user.username; form.password = ''
  form.display_name = user.display_name ?? ''
  // 早期客服角色已停用：编辑这类旧账号时必须重新选择角色，保存时走角色变更确认
  form.email = user.email || ''; form.role = user.role === 'cs' ? '' : user.role
  form.phone = user.phone || ''
  dialogVisible.value = true
}
function resetForm() {
  form.username = ''; form.password = ''; form.display_name = ''; form.email = ''; form.phone = ''; form.role = 'customer'
  formRef.value?.resetFields()
}

// 角色变更会直接改变对方能看到的数据与能做的操作，保存前说明后果
const ROLE_CONSEQUENCE: Record<string, string> = {
  admin: '将获得全部管理权限，包括用户管理、系统设置与日志。',
  expert: '将进入专家工作台，可查看并复核转交给专家的病害会诊。',
  customer: '将只能访问自己的农场、农事与对话档案。',
}

async function handleSubmit() {
  if (!formRef.value || submittingUser.value) return
  submittingUser.value = true
  try {
    if (isEditing.value) { await formRef.value.validateField(['username', 'display_name', 'phone', 'role']) }
    else { await formRef.value.validate() }
  } catch { submittingUser.value = false; return }
  if (isEditing.value && editingOriginalRole.value && form.role !== editingOriginalRole.value) {
    try {
      await ElMessageBox.confirm(
        `“${form.display_name || form.username}”的角色将从「${roleLabel(editingOriginalRole.value)}」改为「${roleLabel(form.role)}」。${ROLE_CONSEQUENCE[form.role] || ''}`,
        '确认修改角色',
        { type: 'warning', confirmButtonText: '确认修改', cancelButtonText: '取消' }
      )
    } catch (error) { submittingUser.value = false; if (isDismissed(error)) return; throw error }
  }
  try {
    if (isEditing.value && editingId.value) {
      // 自己的角色后端不允许修改，不发送 role 字段
      await updateUser(editingId.value, { display_name: form.display_name, phone: normalizePhone(form.phone), email: form.email || undefined,
        ...(editingSelf.value ? {} : { role: form.role }) })
      ElMessage.success('已保存')
    } else {
      await createUser({ username: form.username, password: form.password, display_name: form.display_name, phone: normalizePhone(form.phone), email: form.email || undefined, role: form.role })
      ElMessage.success('已添加用户')
    }
    dialogVisible.value = false; loadUserList()
  } catch (error: unknown) { ElMessage.error(errorText(error, '保存失败')) }
  finally { submittingUser.value = false }
}

function isSelf(user: User) { return user.id === authStore.user?.id }

function canDeleteUser(user: User): boolean {
  if (isSelf(user)) return false
  // 只有超级管理员(username=admin)才能删除、禁用其他管理员；后端同样校验
  if (user.role === 'admin' && !isSuperAdmin.value) return false
  return true
}

async function handleDelete(user: User) {
  const name = user.display_name || user.username
  try {
    await ElMessageBox.confirm(
      `将删除用户“${name}”（${user.username}）。删除后该账号无法登录，且无法恢复；如只是暂停使用，请改用“禁用”。`,
      '删除用户',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' }
    )
  } catch (error) { if (isDismissed(error)) return; throw error }
  try {
    await deleteUser(user.id)
    ElMessage.success('已删除'); loadUserList()
  } catch (error: unknown) { ElMessage.error(errorText(error, '删除失败')) }
}

function canToggleActive(user: User) {
  if (['pending', 'rejected'].includes(user.registration_status || '')) return false
  if (isSelf(user)) return false
  if (user.role === 'admin' && !isSuperAdmin.value) return false
  return true
}

async function handleToggleActive(user: User) {
  if (togglingId.value !== null) return
  if (user.is_active) {
    try {
      await ElMessageBox.confirm(
        `禁用后“${user.display_name || user.username}”将无法登录，已有数据保留，可随时重新启用。`,
        '禁用账号',
        { type: 'warning', confirmButtonText: '禁用', cancelButtonText: '取消' }
      )
    } catch (error) { if (isDismissed(error)) return; throw error }
  }
  togglingId.value = user.id
  try {
    await updateUser(user.id, { is_active: !user.is_active })
    ElMessage.success(user.is_active ? '已禁用' : '已启用'); loadUserList()
  } catch (error: unknown) { ElMessage.error(errorText(error, '操作失败')) }
  finally { togglingId.value = null }
}

async function handleApprove(reg: PendingRegistration) {
  if (reviewingId.value !== null || reg.registration_status !== 'pending') return
  reviewingId.value = reg.id
  try { await approveRegistration(reg.id); ElMessage.success('已通过审核，申请人现在可以登录'); await Promise.all([loadPendingRegistrations(), loadUserList()]) }
  catch (error: unknown) { ElMessage.error(errorText(error, '审批失败')) }
  finally { reviewingId.value = null }
}
async function handleReject(reg: PendingRegistration) {
  if (reviewingId.value !== null || reg.registration_status !== 'pending') return
  try {
    await ElMessageBox.confirm(`拒绝“${reg.display_name}”的注册申请？申请记录会保留，该账号不能登录。`, '拒绝申请',
      { type: 'warning', confirmButtonText: '拒绝', cancelButtonText: '取消' })
  } catch (error) { if (isDismissed(error)) return; throw error }
  reviewingId.value = reg.id
  try {
    await rejectRegistration(reg.id); ElMessage.success('已拒绝，申请记录保留'); await Promise.all([loadPendingRegistrations(), loadUserList()])
  } catch (error: unknown) { ElMessage.error(errorText(error, '操作失败')) }
  finally { reviewingId.value = null }
}

function registrationText(user: User) {
  if (user.registration_status === 'pending') return '待审核'
  if (user.registration_status === 'rejected') return '已拒绝'
  return user.role === 'customer' && !user.registration_reviewed_at ? '已通过（农户自动）' : '已通过'
}

function openPasswordDialog(user: User) { passwordUser.value = user; newPassword.value = ''; passwordDialogVisible.value = true }
async function handleResetPassword() {
  if (!passwordUser.value || resettingPassword.value) return
  const problem = passwordProblem(newPassword.value)
  if (problem) { ElMessage.warning(problem); return }
  resettingPassword.value = true
  try {
    await resetPassword(passwordUser.value.id, newPassword.value)
    ElMessage.success(`已重置“${passwordUser.value.username}”的密码，请通过线下渠道告知本人`)
    passwordDialogVisible.value = false
  } catch (error: unknown) { ElMessage.error(errorText(error, '重置失败')) }
  finally { resettingPassword.value = false }
}

function getRoleType(role: string): 'danger' | 'success' | 'primary' | 'info' {
  return role === 'admin' ? 'danger' : role === 'customer' ? 'success' : 'primary'
}
function formatDate(dateStr: string | undefined | null): string { if (!dateStr) return '—'; return new Date(dateStr).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }
function isActive(user: User): boolean { return user.is_active ?? false }

onMounted(() => { loadUserList(); loadPendingRegistrations() })
</script>

<template>
  <div class="app-page users-container">
    <PageHeader title="用户管理" description="审核注册申请，管理账号角色与启用状态。" />

    <!-- 注册审核 -->
    <el-card class="section-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span>注册审核 <span class="app-num count">{{ registrationTotal }}</span></span>
          <div class="header-actions">
            <el-select v-model="registrationFilter" class="registration-filter" aria-label="审核状态" @change="filterRegistrations">
              <el-option label="待审核" value="pending" />
              <el-option label="已通过" value="approved" />
              <el-option label="已拒绝" value="rejected" />
              <el-option label="全部申请" value="" />
            </el-select>
            <el-button text :icon="Refresh" aria-label="刷新注册审核列表" @click="loadPendingRegistrations" />
          </div>
        </div>
      </template>
      <el-table v-loading="pendingLoading" :data="pendingList" stripe
        :empty-text="registrationFilter === 'pending' ? '没有待审核的申请' : '没有符合条件的申请'">
        <el-table-column prop="username" label="用户名" min-width="120" fixed="left" />
        <el-table-column prop="display_name" label="显示名称" min-width="120" />
        <el-table-column label="申请角色" width="100"><template #default="{ row }">{{ roleLabel(row.role) }}</template></el-table-column>
        <el-table-column prop="phone" label="联系电话" min-width="130" />
        <el-table-column label="审核状态" min-width="140"><template #default="{ row }">{{ registrationText(row) }}</template></el-table-column>
        <el-table-column label="申请时间" width="170">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150" :fixed="isNarrow ? false : 'right'">
          <template #default="{ row }">
            <template v-if="row.registration_status === 'pending'">
              <el-button type="success" text size="small" :icon="Check" :loading="reviewingId === row.id" :disabled="reviewingId !== null && reviewingId !== row.id" @click="handleApprove(row)">通过</el-button>
              <el-button type="danger" text size="small" :icon="Close" :disabled="reviewingId !== null" @click="handleReject(row)">拒绝</el-button>
            </template>
            <span v-else class="app-note">{{ row.registration_reviewed_at ? formatDate(row.registration_reviewed_at) : '自动通过' }}</span>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="registrationTotal > 20" class="pagination">
        <el-pagination v-model:current-page="registrationPage" :page-size="20" :total="registrationTotal"
          :small="isNarrow" layout="total, prev, pager, next" @current-change="loadPendingRegistrations" />
      </div>
    </el-card>

    <!-- 账号列表 -->
    <el-card class="section-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span>账号 <span class="app-num count">{{ total }}</span></span>
          <el-button type="primary" :icon="Plus" @click="openAddDialog">添加用户</el-button>
        </div>
      </template>

      <div class="role-tabs" role="tablist" aria-label="按角色筛选">
        <button v-for="item in ROLE_FILTERS" :key="item.value" type="button" role="tab"
          class="role-tab" :class="{ active: filters.role === item.value }" :aria-selected="filters.role === item.value"
          @click="selectRole(item.value)">{{ item.label }}</button>
      </div>

      <div class="toolbar">
        <el-input v-model="filters.username" placeholder="搜索用户名" clearable class="filter-username" aria-label="搜索用户名"
          :prefix-icon="Search" @keyup.enter="handleSearch" />
        <el-select v-model="filters.isActive" placeholder="启用状态" clearable class="filter-status" aria-label="启用状态" @change="handleSearch">
          <el-option label="已启用" :value="true" />
          <el-option label="已禁用" :value="false" />
        </el-select>
        <el-button type="primary" :icon="Search" @click="handleSearch">搜索</el-button>
        <el-button :icon="Refresh" @click="handleReset">重置</el-button>
      </div>

      <el-alert v-if="loadFailed" type="error" :closable="false" show-icon class="list-alert" title="用户列表读取失败，未显示旧数据">
        <el-button size="small" @click="loadUserList">重试</el-button>
      </el-alert>

      <el-table v-loading="loading" :data="userList" stripe empty-text="没有符合条件的账号">
        <el-table-column label="用户名" min-width="140" fixed="left">
          <template #default="{ row }">
            {{ row.username }}
            <el-tag v-if="isSelf(row)" size="small" effect="plain" class="self-tag">当前账号</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="显示名称" min-width="120">
          <template #default="{ row }">{{ row.display_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="角色" width="120">
          <template #default="{ row }"><el-tag :type="getRoleType(row.role)" size="small" effect="plain">{{ roleLabel(row.role) }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="phone" label="联系电话" min-width="130" />
        <el-table-column label="邮箱" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ row.email || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }"><el-tag :type="isActive(row) ? 'success' : 'info'" size="small">{{ isActive(row) ? '启用' : '禁用' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="最后登录" width="170">
          <template #default="{ row }">{{ formatDate(row.last_login) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" :fixed="isNarrow ? false : 'right'">
          <template #default="{ row }">
            <div class="action-btns">
              <el-button size="small" link type="primary" @click="openEditDialog(row)">编辑</el-button>
              <el-button size="small" link type="primary" @click="openPasswordDialog(row)">重置密码</el-button>
              <el-button v-if="canToggleActive(row)" size="small" link :type="isActive(row) ? 'warning' : 'success'"
                :loading="togglingId === row.id" @click="handleToggleActive(row)">{{ isActive(row) ? '禁用' : '启用' }}</el-button>
              <el-button v-if="canDeleteUser(row)" size="small" link type="danger" @click="handleDelete(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination">
        <el-pagination v-model:current-page="currentPage" v-model:page-size="pageSize" :page-sizes="[20, 50, 100]"
          :total="total" :small="isNarrow" :pager-count="isNarrow ? 5 : 7"
          :layout="isNarrow ? 'total, prev, pager, next' : 'total, sizes, prev, pager, next'"
          @current-change="loadUserList" @size-change="handleSizeChange" />
      </div>
    </el-card>

    <!-- 添加/编辑用户对话框 -->
    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="480px" destroy-on-close>
      <el-form ref="formRef" :model="form" :rules="rules" :label-position="isNarrow ? 'top' : 'right'" label-width="90px"
        :disabled="submittingUser">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" :disabled="isEditing" placeholder="3-50 个字符" autocomplete="off" />
        </el-form-item>
        <el-form-item v-if="!isEditing" label="初始密码" prop="password">
          <el-input v-model="form.password" type="password" show-password autocomplete="new-password" :placeholder="PASSWORD_HINT" />
        </el-form-item>
        <el-form-item label="显示名称" prop="display_name">
          <el-input v-model="form.display_name" placeholder="例如：张三 / 某某合作社" maxlength="100" />
        </el-form-item>
        <el-form-item label="联系电话" prop="phone">
          <el-input v-model="form.phone" placeholder="必填" maxlength="32" autocomplete="tel" />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input v-model="form.email" placeholder="可选" autocomplete="email" />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="form.role" class="full-width" :disabled="editingSelf" placeholder="请选择角色">
            <el-option label="管理员" value="admin" />
            <el-option label="农技专家" value="expert" />
            <el-option label="农户" value="customer" />
          </el-select>
          <p v-if="editingSelf" class="app-note form-note">不能修改自己的角色。</p>
          <p v-else-if="ROLE_CONSEQUENCE[form.role]" class="app-note form-note">{{ ROLE_CONSEQUENCE[form.role] }}</p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submittingUser" @click="handleSubmit">{{ isEditing ? '保存' : '添加' }}</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码对话框 -->
    <el-dialog v-model="passwordDialogVisible" title="重置密码" width="420px" destroy-on-close>
      <p v-if="passwordUser" class="reset-target">
        为 <strong>{{ passwordUser.display_name || passwordUser.username }}</strong>（{{ passwordUser.username }}）设置新密码。
      </p>
      <el-form :label-position="isNarrow ? 'top' : 'right'" label-width="80px" @submit.prevent="handleResetPassword">
        <el-form-item label="新密码" :error="newPasswordProblem">
          <el-input v-model="newPassword" type="password" show-password autocomplete="new-password" :placeholder="PASSWORD_HINT"
            :disabled="resettingPassword" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="passwordDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="resettingPassword" :disabled="!newPassword || !!newPasswordProblem" @click="handleResetPassword">重置密码</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.users-container {
  max-width: 1600px;
  margin: 0 auto;
}

.section-card {
  margin-bottom: var(--app-space-4);
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-3);
  flex-wrap: wrap;
}

.count {
  margin-left: 4px;
  color: var(--app-text-secondary);
  font-weight: 500;
}

/* 头部操作组：筛选框+刷新靠右成组 */
.header-actions {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}

.registration-filter {
  width: 130px;
}

.role-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-1);
  margin-bottom: var(--app-space-3);
  border-bottom: 1px solid var(--app-border-light);
}

.role-tab {
  min-height: var(--touch-min, 36px);
  padding: 0 var(--app-space-3);
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--app-text-secondary);
  font: inherit;
  font-size: var(--app-text-sm);
  cursor: pointer;
  margin-bottom: -1px;
}

.role-tab:hover {
  color: var(--app-text);
}

.role-tab.active {
  color: var(--el-color-primary);
  border-bottom-color: var(--el-color-primary);
  font-weight: 500;
}

.toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--app-space-3);
  margin-bottom: var(--app-space-3);
}

.toolbar .el-button + .el-button {
  margin-left: 0;
}

.filter-username { width: 200px; }
.filter-status { width: 130px; }
.full-width { width: 100%; }

.list-alert {
  margin: 0 0 var(--app-space-3);
}

.self-tag {
  margin-left: 4px;
}

.form-note {
  margin: 4px 0 0;
  line-height: 1.5;
}

.reset-target {
  margin: 0 0 var(--app-space-3);
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
}

.action-btns {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  flex-wrap: wrap;
}

.action-btns .el-button + .el-button {
  margin-left: 0;
}

.pagination {
  margin-top: var(--app-space-4);
  display: flex;
  justify-content: flex-end;
  overflow-x: auto;
}

@media (max-width: 767px) {
  .filter-username,
  .filter-status { width: 100%; }
  .toolbar .el-button { flex: 1; }
  .role-tabs { flex-wrap: nowrap; overflow-x: auto; }
  .role-tab { flex-shrink: 0; }
  .pagination { justify-content: center; }
}
</style>
