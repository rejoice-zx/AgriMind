<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Refresh, Check, Close, ArrowUp, ArrowDown } from '@element-plus/icons-vue'
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

const authStore = useAuthStore()
const isSuperAdmin = computed(() => authStore.user?.username === 'admin')

// List state
const loading = ref(false)
const userList = ref<User[]>([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(100)

// Filter state
const filters = reactive({
  role: '',
  isActive: undefined as boolean | undefined,
  username: ''
})

// Grouped users
const adminUsers = computed(() => userList.value.filter(u => u.role === 'admin'))
const csUsers = computed(() => userList.value.filter(u => ['expert', 'cs'].includes(u.role)))
const customerUsers = computed(() => userList.value.filter(u => u.role === 'customer'))
const customerExpanded = ref(false)

// Pending registrations
const pendingList = ref<PendingRegistration[]>([])
const pendingLoading = ref(false)
const registrationPage = ref(1)
const registrationTotal = ref(0)
const registrationFilter = ref<'' | 'pending' | 'approved' | 'rejected'>('')
const reviewingId = ref<number | null>(null)
const submittingUser = ref(false)

// Dialog state
const dialogVisible = ref(false)
const dialogTitle = ref('添加用户')
const isEditing = ref(false)
const editingId = ref<number | null>(null)

// Form
const formRef = ref<FormInstance>()
const form = reactive({ username: '', password: '', display_name: '', email: '', phone: '', role: 'customer' })
const rules: FormRules = {
  phone: phoneRules,
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 50, message: '用户名长度应为3-50个字符', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少6个字符', trigger: 'blur' }
  ],
  display_name: [
    { required: true, message: '请输入显示名称', trigger: 'blur' },
    { min: 1, max: 100, message: '显示名称长度应为1-100个字符', trigger: 'blur' }
  ],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }]
}

// Password reset dialog
const passwordDialogVisible = ref(false)
const passwordUserId = ref<number | null>(null)
const newPassword = ref('')

async function loadUserList() {
  loading.value = true
  try {
    const response = await getUsers({
      page: currentPage.value,
      page_size: pageSize.value,
      role: filters.role || undefined,
      is_active: filters.isActive,
      username: filters.username || undefined
    })
    userList.value = response.data.items
    total.value = response.data.total
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '加载用户列表失败')
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
  } catch (error: any) {
    pendingList.value = []; registrationTotal.value = 0
    ElMessage.error(error.response?.data?.detail || '加载注册审核列表失败')
  } finally {
    pendingLoading.value = false
  }
}

function filterRegistrations() { registrationPage.value = 1; void loadPendingRegistrations() }

function handleSearch() { currentPage.value = 1; loadUserList() }
function handleReset() {
  filters.role = ''; filters.isActive = undefined; filters.username = ''
  currentPage.value = 1; loadUserList()
}

function openAddDialog() {
  dialogTitle.value = '添加用户'; isEditing.value = false; editingId.value = null; resetForm()
  dialogVisible.value = true
}
function openEditDialog(user: User) {
  dialogTitle.value = '编辑用户'; isEditing.value = true; editingId.value = user.id
  form.username = user.username; form.password = ''
  form.display_name = user.display_name ?? ''
  form.email = user.email || ''; form.role = user.role
  form.phone = user.phone || ''
  dialogVisible.value = true
}
function resetForm() {
  form.username = ''; form.password = ''; form.display_name = ''; form.email = ''; form.phone = ''; form.role = 'customer'
  formRef.value?.resetFields()
}

async function handleSubmit() {
  if (!formRef.value || submittingUser.value) return
  submittingUser.value = true
  try {
    if (isEditing.value) { await formRef.value.validateField(['username', 'display_name', 'phone', 'role']) }
    else { await formRef.value.validate() }
  } catch { submittingUser.value = false; return }
  try {
    if (isEditing.value && editingId.value) {
      await updateUser(editingId.value, { display_name: form.display_name, phone: normalizePhone(form.phone), email: form.email || undefined, role: form.role })
      ElMessage.success('更新成功')
    } else {
      await createUser({ username: form.username, password: form.password, display_name: form.display_name, phone: normalizePhone(form.phone), email: form.email || undefined, role: form.role })
      ElMessage.success('添加成功')
    }
    dialogVisible.value = false; loadUserList()
  } catch (error: any) { ElMessage.error(error.response?.data?.detail || '操作失败') }
  finally { submittingUser.value = false }
}

async function handleDelete(user: User) {
  try {
    await ElMessageBox.confirm(`确定要删除用户"${user.display_name ?? user.username}"吗？`, '确认删除', { type: 'warning' })
    await deleteUser(user.id)
    ElMessage.success('删除成功'); loadUserList()
  } catch (error: any) {
    if (error !== 'cancel') ElMessage.error(error.response?.data?.detail || '删除失败')
  }
}

function canDeleteUser(user: User): boolean {
  // 不能删除自己
  if (user.id === authStore.user?.id) return false
  // 只有超级管理员(username=admin)才能删除管理员
  if (user.role === 'admin' && !isSuperAdmin.value) return false
  return true
}

async function handleToggleActive(user: User) {
  try {
    await updateUser(user.id, { is_active: !user.is_active })
    ElMessage.success(user.is_active ? '已禁用' : '已启用'); loadUserList()
  } catch (error: any) { ElMessage.error(error.response?.data?.detail || '操作失败') }
}

async function handleApprove(reg: PendingRegistration) {
  if (reviewingId.value !== null || reg.registration_status !== 'pending') return
  reviewingId.value = reg.id
  try { await approveRegistration(reg.id); ElMessage.success('已通过审核，申请人现在可以登录'); await Promise.all([loadPendingRegistrations(), loadUserList()]) }
  catch (error: any) { ElMessage.error(error.response?.data?.detail || '审批失败') }
  finally { reviewingId.value = null }
}
async function handleReject(reg: PendingRegistration) {
  if (reviewingId.value !== null || reg.registration_status !== 'pending') return
  reviewingId.value = reg.id
  try {
    await ElMessageBox.confirm(`确定要拒绝用户"${reg.display_name}"的注册申请吗？`, '确认拒绝', { type: 'warning' })
    await rejectRegistration(reg.id); ElMessage.success('已拒绝，申请记录保留'); await Promise.all([loadPendingRegistrations(), loadUserList()])
  } catch (error: any) { if (error !== 'cancel') ElMessage.error(error.response?.data?.detail || '操作失败') }
  finally { reviewingId.value = null }
}

function registrationText(user: User) {
  if (user.registration_status === 'pending') return '待审核'
  if (user.registration_status === 'rejected') return '已拒绝'
  return user.role === 'customer' && !user.registration_reviewed_at ? '已通过（农户自动）' : '已通过'
}

function canToggleActive(user: User) { return !['pending', 'rejected'].includes(user.registration_status || '') }

function openPasswordDialog(user: User) { passwordUserId.value = user.id; newPassword.value = ''; passwordDialogVisible.value = true }
async function handleResetPassword() {
  if (!passwordUserId.value || !newPassword.value) { ElMessage.warning('请输入新密码'); return }
  if (newPassword.value.length < 6) { ElMessage.warning('密码至少6个字符'); return }
  try { await resetPassword(passwordUserId.value, newPassword.value); ElMessage.success('密码重置成功'); passwordDialogVisible.value = false }
  catch (error: any) { ElMessage.error(error.response?.data?.detail || '重置失败') }
}

function getRoleText(role: string): string { return ({ admin: '管理员', expert: '农技专家', customer: '农户', cs: '历史客服' } as Record<string, string>)[role] || role }
function getRoleType(role: string): string { return role === 'admin' ? 'danger' : role === 'customer' ? 'success' : 'primary' }
function formatDate(dateStr: string | undefined): string { if (!dateStr) return '-'; return new Date(dateStr).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }
function isActive(user: User): boolean { return user.is_active ?? false }

onMounted(() => { loadUserList(); loadPendingRegistrations() })
</script>

<template>
  <div class="app-page users-container">
    <PageHeader title="用户管理" description="管理系统用户和注册审批" />

    <!-- 待审批 -->
    <el-card class="section-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span>注册审核列表 ({{ registrationTotal }})</span>
          <div class="header-actions">
            <el-select v-model="registrationFilter" @change="filterRegistrations" style="width: 150px">
              <el-option label="全部注册申请" value="" />
              <el-option label="待审核" value="pending" />
              <el-option label="已通过" value="approved" />
              <el-option label="已拒绝" value="rejected" />
            </el-select>
            <el-button text :icon="Refresh" @click="loadPendingRegistrations" />
          </div>
        </div>
      </template>
      <el-table v-loading="pendingLoading" :data="pendingList" stripe>
        <el-table-column prop="username" label="用户名" width="150" />
        <el-table-column prop="display_name" label="显示名称" width="150" />
        <el-table-column label="申请角色" width="110"><template #default="{ row }">{{ getRoleText(row.role) }}</template></el-table-column>
        <el-table-column prop="phone" label="联系电话" min-width="150" />
        <el-table-column label="审核状态" min-width="160"><template #default="{ row }">{{ registrationText(row) }}</template></el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="200" />
        <el-table-column label="申请时间" width="180">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <template v-if="row.registration_status === 'pending'">
              <el-button type="success" text size="small" :icon="Check" :disabled="reviewingId !== null" @click="handleApprove(row)">通过</el-button>
              <el-button type="danger" text size="small" :icon="Close" :disabled="reviewingId !== null" @click="handleReject(row)">拒绝</el-button>
            </template>
            <span v-else>{{ row.registration_reviewed_at ? formatDate(row.registration_reviewed_at) : '自动审核' }}</span>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination v-model:current-page="registrationPage" :page-size="20" :total="registrationTotal"
        layout="total, prev, pager, next" @current-change="loadPendingRegistrations" />
    </el-card>

    <!-- 搜索栏 -->
    <el-card class="section-card" shadow="never">
      <div class="toolbar">
        <div class="toolbar-left">
          <el-input v-model="filters.username" placeholder="搜索用户名" clearable style="width: 160px" :prefix-icon="Search" @keyup.enter="handleSearch" />
          <el-select v-model="filters.role" placeholder="选择角色" clearable style="width: 140px">
            <el-option label="管理员" value="admin" />
            <el-option label="农技专家" value="expert" />
            <el-option label="历史客服" value="cs" />
            <el-option label="农户" value="customer" />
          </el-select>
          <el-select v-model="filters.isActive" placeholder="选择状态" clearable style="width: 140px">
            <el-option label="已启用" :value="true" />
            <el-option label="已禁用" :value="false" />
          </el-select>
          <el-button type="primary" :icon="Search" @click="handleSearch">搜索</el-button>
          <el-button :icon="Refresh" @click="handleReset">重置</el-button>
        </div>
        <div class="toolbar-right">
          <el-button type="primary" :icon="Plus" @click="openAddDialog">添加用户</el-button>
        </div>
      </div>
    </el-card>

    <!-- 管理员列表 -->
    <el-card v-if="adminUsers.length > 0" class="section-card" shadow="never">
      <template #header>
        <span>管理员 ({{ adminUsers.length }})</span>
      </template>
      <el-table :data="adminUsers" stripe v-loading="loading">
        <el-table-column type="index" label="序号" width="60" />
        <el-table-column prop="username" label="用户名" width="140" />
        <el-table-column label="显示名称" width="140">
          <template #default="{ row }">{{ row.display_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="角色" width="90">
          <template #default="{ row }"><el-tag :type="getRoleType(row.role)" size="small">{{ getRoleText(row.role) }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="phone" label="联系电话" min-width="150" />
        <el-table-column prop="email" label="邮箱" min-width="180" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }"><el-tag :type="isActive(row) ? 'success' : 'info'" size="small">{{ isActive(row) ? '启用' : '禁用' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="最后登录" width="170">
          <template #default="{ row }">{{ formatDate(row.last_login) }}</template>
        </el-table-column>
        <el-table-column label="操作" min-width="200" fixed="right">
          <template #default="{ row }">
            <div class="action-btns">
              <el-button size="small" link type="primary" @click="openEditDialog(row)">编辑</el-button>
              <el-button size="small" link type="primary" @click="openPasswordDialog(row)">重置密码</el-button>
              <el-button v-if="canToggleActive(row)" size="small" link :type="isActive(row) ? 'info' : 'success'" @click="handleToggleActive(row)">{{ isActive(row) ? '禁用' : '启用' }}</el-button>
              <el-button v-if="canDeleteUser(row)" size="small" link type="danger" @click="handleDelete(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 客服列表 -->
    <el-card v-if="csUsers.length > 0" class="section-card" shadow="never">
      <template #header>
        <span>农技专家与历史客服 ({{ csUsers.length }})</span>
      </template>
      <el-table :data="csUsers" stripe v-loading="loading">
        <el-table-column type="index" label="序号" width="60" />
        <el-table-column prop="username" label="用户名" width="140" />
        <el-table-column label="显示名称" width="140">
          <template #default="{ row }">{{ row.display_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="角色" width="90">
          <template #default="{ row }"><el-tag :type="getRoleType(row.role)" size="small">{{ getRoleText(row.role) }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="phone" label="联系电话" min-width="150" />
        <el-table-column prop="email" label="邮箱" min-width="180" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }"><el-tag :type="isActive(row) ? 'success' : 'info'" size="small">{{ isActive(row) ? '启用' : '禁用' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="最后登录" width="170">
          <template #default="{ row }">{{ formatDate(row.last_login) }}</template>
        </el-table-column>
        <el-table-column label="操作" min-width="200" fixed="right">
          <template #default="{ row }">
            <div class="action-btns">
              <el-button size="small" link type="primary" @click="openEditDialog(row)">编辑</el-button>
              <el-button size="small" link type="primary" @click="openPasswordDialog(row)">重置密码</el-button>
              <el-button v-if="canToggleActive(row)" size="small" link :type="isActive(row) ? 'info' : 'success'" @click="handleToggleActive(row)">{{ isActive(row) ? '禁用' : '启用' }}</el-button>
              <el-button v-if="canDeleteUser(row)" size="small" link type="danger" @click="handleDelete(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 客户列表（可折叠） -->
    <el-card v-if="customerUsers.length > 0" class="section-card" shadow="never">
      <template #header>
        <div class="card-header clickable" @click="customerExpanded = !customerExpanded">
          <span>农户 ({{ customerUsers.length }})</span>
          <el-icon v-if="customerExpanded"><ArrowUp /></el-icon>
          <el-icon v-else><ArrowDown /></el-icon>
        </div>
      </template>
      <el-collapse-transition>
        <div v-show="customerExpanded">
          <el-table :data="customerUsers" stripe v-loading="loading">
            <el-table-column type="index" label="序号" width="60" />
            <el-table-column prop="username" label="用户名" width="140" />
            <el-table-column label="显示名称" width="140">
              <template #default="{ row }">{{ row.display_name || '-' }}</template>
            </el-table-column>
            <el-table-column label="角色" width="90">
              <template #default="{ row }"><el-tag :type="getRoleType(row.role)" size="small">{{ getRoleText(row.role) }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="phone" label="联系电话" min-width="150" />
            <el-table-column prop="email" label="邮箱" min-width="180" />
            <el-table-column label="状态" width="80">
              <template #default="{ row }"><el-tag :type="isActive(row) ? 'success' : 'info'" size="small">{{ isActive(row) ? '启用' : '禁用' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="最后登录" width="170">
              <template #default="{ row }">{{ formatDate(row.last_login) }}</template>
            </el-table-column>
            <el-table-column label="操作" min-width="200" fixed="right">
              <template #default="{ row }">
                <div class="action-btns">
                  <el-button size="small" link type="primary" @click="openEditDialog(row)">编辑</el-button>
                  <el-button size="small" link type="primary" @click="openPasswordDialog(row)">重置密码</el-button>
                  <el-button v-if="canToggleActive(row)" size="small" link :type="isActive(row) ? 'info' : 'success'" @click="handleToggleActive(row)">{{ isActive(row) ? '禁用' : '启用' }}</el-button>
                  <el-button v-if="canDeleteUser(row)" size="small" link type="danger" @click="handleDelete(row)">删除</el-button>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-collapse-transition>
    </el-card>

    <!-- 添加/编辑用户对话框 -->
    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="480px" destroy-on-close>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" :disabled="isEditing" placeholder="请输入用户名" />
        </el-form-item>
        <el-form-item v-if="!isEditing" label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="请输入密码" />
        </el-form-item>
        <el-form-item label="显示名称" prop="display_name">
          <el-input v-model="form.display_name" placeholder="请输入显示名称" />
        </el-form-item>
        <el-form-item label="联系电话" prop="phone">
          <el-input v-model="form.phone" placeholder="请输入联系电话（必填）" maxlength="32" autocomplete="tel" />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input v-model="form.email" placeholder="请输入邮箱（可选）" />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="form.role" style="width: 100%">
            <el-option label="管理员" value="admin" />
            <el-option label="农技专家" value="expert" />
            <el-option v-if="isEditing && form.role === 'cs'" label="历史客服" value="cs" />
            <el-option label="农户" value="customer" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submittingUser" @click="handleSubmit">确定</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码对话框 -->
    <el-dialog v-model="passwordDialogVisible" title="重置密码" width="400px" destroy-on-close>
      <el-form label-width="80px">
        <el-form-item label="新密码">
          <el-input v-model="newPassword" type="password" show-password placeholder="请输入新密码（至少6个字符）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="passwordDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleResetPassword">确定</el-button>
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
}

/* 头部操作组：筛选框+刷新靠右成组 */
.header-actions {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}

.card-header.clickable {
  cursor: pointer;
  user-select: none;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--app-space-3);
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: var(--app-space-3);
  flex-wrap: wrap;
}

.toolbar-left .el-button + .el-button {
  margin-left: 0;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: var(--app-space-3);
}

.action-btns {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  flex-wrap: nowrap;
}

.action-btns .el-button + .el-button {
  margin-left: 0;
}
</style>
