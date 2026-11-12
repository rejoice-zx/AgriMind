<script setup lang="ts">
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Refresh, Download, Delete, ArrowDown } from '@element-plus/icons-vue'
import { getLogs, downloadLogs, clearLogs } from '@/api/logs'
import PageHeader from '@/components/PageHeader.vue'
import type { LogEntry } from '@/types'
import { errorText, isDismissed } from '@/utils/errorText'
import { useNarrowScreen } from '@/composables/useBreakpoint'

const isNarrow = useNarrowScreen()

// State
const loading = ref(false)
const logList = ref<LogEntry[]>([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(50)
const queryInfo = ref<{ truncated: boolean; legacy: number; unavailable: number; unparsed: number } | null>(null)
const failed = ref(false)
let loadVersion = 0
let alive = true
onBeforeUnmount(() => { alive = false; loadVersion++ })

// Filters
const filters = reactive({
  level: '',
  keyword: '',
  startTime: '',
  endTime: '',
  associationType: 'request_id',
  associationId: ''
})

// Date range
const dateRange = ref<[Date, Date] | null>(null)

// Log levels
const logLevels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

// 关联编号：前四类后端按 UUID 解析，田块/病害记录按正整数解析。前端先校验，避免发出必然 422 的请求。
const ASSOCIATIONS = [
  { value: 'request_id', label: 'HTTP 请求 ID', kind: 'uuid' },
  { value: 'business_request_id', label: '业务请求 ID', kind: 'uuid' },
  { value: 'conversation_id', label: '农业会话 ID', kind: 'uuid' },
  { value: 'consultation_id', label: '咨询工单 ID', kind: 'uuid' },
  { value: 'field_id', label: '田块 ID', kind: 'int' },
  { value: 'record_id', label: '病害记录 ID', kind: 'int' },
] as const
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const associationError = computed(() => {
  const value = filters.associationId.trim()
  if (!value) return ''
  const kind = ASSOCIATIONS.find(item => item.value === filters.associationType)?.kind
  if (kind === 'uuid' && !UUID_RE.test(value)) return '格式应为 36 位 UUID'
  if (kind === 'int' && !/^[1-9]\d*$/.test(value)) return '应为正整数'
  return ''
})
const associationPlaceholder = computed(() =>
  ASSOCIATIONS.find(item => item.value === filters.associationType)?.kind === 'int' ? '输入数字编号' : '粘贴 UUID')
const CLICKABLE_KEYS = new Set<string>(ASSOCIATIONS.map(item => item.value))

// 查询范围提示合并为一处：只列出本次实际出现的情况
const queryWarnings = computed(() => {
  const info = queryInfo.value
  if (!info) return []
  const items: string[] = []
  if (info.truncated) items.push('未读取全部日志：结果有范围限制，找不到不代表从未发生')
  if (info.legacy) items.push(`含 ${info.legacy} 条旧格式日志：无时区时间按北京时间解释`)
  if (info.unavailable) items.push(`${info.unavailable} 份日志文件暂不可读`)
  if (info.unparsed) items.push(`${info.unparsed} 行无法解析为独立事件（如堆栈续行），未计入总数`)
  return items
})

// Load logs
async function loadLogs() {
  if (associationError.value) {
    ElMessage.warning(`关联编号${associationError.value}`)
    return
  }
  const version = ++loadVersion
  loading.value = true
  failed.value = false
  logList.value = []
  total.value = 0
  queryInfo.value = null
  try {
    const params: any = {
      page: currentPage.value,
      page_size: pageSize.value
    }
    
    if (filters.associationId.trim()) params[filters.associationType] = filters.associationId.trim()
    if (filters.level) params.level = filters.level
    const keyword = filters.keyword.trim()
    if (keyword) params.keyword = keyword
    if (dateRange.value) {
      params.start_time = dateRange.value[0].toISOString()
      params.end_time = dateRange.value[1].toISOString()
    }
    
    const response = await getLogs(params)
    if (!alive || version !== loadVersion) return
    const data = response.data
    queryInfo.value = { truncated: data.truncated, legacy: data.legacy_time_entries, unavailable: data.unavailable_files, unparsed: data.unparsed_lines }
    logList.value = data.items || []
    total.value = data.total || 0
  } catch (error: any) {
    if (!alive || version !== loadVersion) return
    failed.value = true
    ElMessage.error(errorText(error, '读取日志失败'))
  } finally {
    if (alive && version === loadVersion) loading.value = false
  }
}

// Handle search
function handleSearch() {
  currentPage.value = 1
  loadLogs()
}

// Handle reset
function handleReset() {
  filters.level = ''
  filters.keyword = ''
  filters.associationId = ''
  filters.associationType = 'request_id'
  dateRange.value = null
  currentPage.value = 1
  loadLogs()
}

// 点击日志里的关联编号，直接按它筛选
function filterByContext(key: string, value: unknown) {
  filters.associationType = key
  filters.associationId = String(value)
  handleSearch()
}

// Handle page change
function handlePageChange(page: number) {
  currentPage.value = page
  loadLogs()
}

// Handle page size change
function handleSizeChange(size: number) {
  pageSize.value = size
  currentPage.value = 1
  loadLogs()
}

// Download logs
async function handleDownload(logType: 'app' | 'error') {
  try {
    const response = await downloadLogs(logType)
    const blob = response.data as Blob
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${logType}_${new Date().toISOString().split('T')[0]}.log`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    ElMessage.success('下载成功')
  } catch (error: any) {
    // 下载接口按 blob 返回，错误体也是 blob、读不到 detail，因此按状态码说明
    ElMessage.error(error?.response?.status === 413
      ? '日志文件超过 8MiB 下载上限，请在部署环境归档'
      : errorText(error, '下载失败'))
  }
}

// Clear logs
async function handleClear(logType: 'app' | 'error') {
  const name = logType === 'app' ? '应用日志' : '错误日志'
  // 不可恢复的操作：要求输入“清空”确认，避免误点下拉项
  try {
    await ElMessageBox.prompt(
      `将清空当前${name}文件，此操作不可恢复，之后无法回看排查。请输入“清空”确认。`,
      `清空${name}`,
      { type: 'warning', confirmButtonText: '确认清空', cancelButtonText: '取消',
        inputPlaceholder: '清空', inputValidator: (value: string) => value?.trim() === '清空' || '请输入“清空”' }
    )
  } catch (error) { if (isDismissed(error)) return; throw error }
  try {
    const response = await clearLogs(logType)
    if (!response.data.success) { ElMessage.error('清空失败，日志文件未变化'); return }
    ElMessage.success(`已清空${name}`)
    loadLogs()
  } catch (error: unknown) {
    ElMessage.error(errorText(error, '清空失败'))
  }
}

// Get level tag type
function getLevelType(level: string): string {
  const typeMap: Record<string, string> = {
    'DEBUG': 'info',
    'INFO': '',
    'WARNING': 'warning',
    'ERROR': 'danger',
    'CRITICAL': 'danger'
  }
  return typeMap[level] || 'info'
}

// Format timestamp
function formatTimestamp(timestamp: string): string {
  return new Date(timestamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
}

function contextEntries(row: LogEntry) {
  return Object.entries(row.context || {}).map(([key, value]) => ({ key, value, clickable: CLICKABLE_KEYS.has(key) }))
}

function formatContext(row: LogEntry): string {
  return Object.entries(row.context || {}).map(([key, value]) => `${key}=${value}`).join(" · ")
}

// Initialize
onMounted(() => {
  loadLogs()
})
</script>

<template>
  <div class="app-page logs-container">
    <!-- Header -->
    <PageHeader title="系统日志" description="按级别、关键词、关联编号和时间查找后端日志；下载内容已脱敏，单文件上限 8MiB。">
      <template #actions>
        <el-dropdown @command="handleDownload">
          <el-button :icon="Download">
            下载日志<el-icon class="el-icon--right">
              <arrow-down />
            </el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="app">
                应用日志
              </el-dropdown-item>
              <el-dropdown-item command="error">
                错误日志
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-dropdown @command="handleClear">
          <el-button
            type="danger"
            :icon="Delete"
          >
            清空日志<el-icon class="el-icon--right">
              <arrow-down />
            </el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="app">
                清空应用日志
              </el-dropdown-item>
              <el-dropdown-item command="error">
                清空错误日志
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </template>
    </PageHeader>

    <!-- Toolbar -->
    <el-card
      class="toolbar-card"
      shadow="never"
    >
      <div class="toolbar">
        <div class="toolbar-left">
          <el-select
            v-model="filters.level"
            placeholder="日志级别"
            clearable
            class="filter-level"
            aria-label="日志级别"
          >
            <el-option
              v-for="level in logLevels"
              :key="level"
              :label="level"
              :value="level"
            />
          </el-select>
          <el-input
            v-model="filters.keyword"
            placeholder="消息关键词"
            clearable
            maxlength="200"
            class="filter-keyword"
            aria-label="消息关键词"
            @keyup.enter="handleSearch"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
          <div class="filter-association">
            <el-select v-model="filters.associationType" class="filter-association-type" aria-label="关联类型">
              <el-option v-for="item in ASSOCIATIONS" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
            <el-input v-model="filters.associationId" :placeholder="associationPlaceholder" clearable
              class="filter-association-id" aria-label="关联编号" :class="{ 'is-invalid': associationError }"
              @keyup.enter="handleSearch" />
          </div>
          <el-date-picker
            v-model="dateRange"
            type="datetimerange"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            class="filter-date"
            aria-label="时间范围"
          />
          <el-button
            type="primary"
            :icon="Search"
            @click="handleSearch"
          >
            搜索
          </el-button>
          <el-button
            :icon="Refresh"
            @click="handleReset"
          >
            重置
          </el-button>
        </div>
      </div>
      <p v-if="associationError" class="field-error" role="alert">关联编号{{ associationError }}</p>
    </el-card>

    <el-alert v-if="failed" class="logs-alert" title="日志查询失败，未展示上次结果" type="error" :closable="false" show-icon>
      <el-button size="small" @click="loadLogs">重试</el-button>
    </el-alert>
    <el-alert v-if="queryWarnings.length" class="logs-alert" type="warning" :closable="false" show-icon
      title="本次查询结果不完整，请留意：">
      <ul class="warning-list"><li v-for="item in queryWarnings" :key="item">{{ item }}</li></ul>
    </el-alert>
    <p v-if="queryInfo" class="app-note scope-line">
      查询范围：每份日志只读末尾 1MiB，单次最多 8MiB、20000 条，含最多 5 份轮转文件；总数为窗口内去重后的匹配数，翻页期间日志可能继续写入。
    </p>

    <!-- Log Table -->
    <el-card
      class="table-card"
      shadow="never"
    >
      <el-table
        v-loading="loading"
        :data="logList"
        stripe
        empty-text="没有符合条件的日志"
      >
        <el-table-column
          label="时间"
          width="170"
          fixed="left"
        >
          <template #default="{ row }">
            {{ formatTimestamp(row.timestamp) }}
          </template>
        </el-table-column>
        <el-table-column
          label="级别"
          width="100"
        >
          <template #default="{ row }">
            <el-tag
              :type="getLevelType(row.level)"
              size="small"
            >
              {{ row.level }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="source"
          label="来源"
          width="150"
          show-overflow-tooltip
        />
        <el-table-column label="关联信息" min-width="280">
          <template #default="{ row }">
            <div v-if="contextEntries(row).length" class="context-list" :aria-label="formatContext(row)">
              <template v-for="item in contextEntries(row)" :key="item.key">
                <button v-if="item.clickable" type="button" class="context-chip" :title="`按 ${item.key} 筛选`"
                  @click="filterByContext(item.key, item.value)">{{ item.key }}={{ item.value }}</button>
                <span v-else class="context-chip context-chip--plain">{{ item.key }}={{ item.value }}</span>
              </template>
            </div>
            <span v-else class="app-note">旧记录无关联信息</span>
          </template>
        </el-table-column>
        <el-table-column
          prop="message"
          label="消息"
          min-width="400"
        >
          <template #default="{ row }">
            <div class="log-message">
              {{ row.message }}
            </div>
          </template>
        </el-table-column>
      </el-table>
      
      <!-- Pagination -->
      <div class="pagination">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[20, 50, 100, 200]"
          :total="total"
          :small="isNarrow"
          :pager-count="isNarrow ? 5 : 7"
          :layout="isNarrow ? 'total, prev, pager, next' : 'total, sizes, prev, pager, next, jumper'"
          @current-change="handlePageChange"
          @size-change="handleSizeChange"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.logs-container {
  max-width: 1600px;
  margin: 0 auto;
}

.toolbar-card,
.table-card {
  margin-bottom: var(--app-space-5);
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
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

.filter-level { width: 130px; }
.filter-keyword { width: 200px; }
.filter-association { display: flex; }
.filter-association-type { width: 140px; }
.filter-association-id { width: 300px; }
.filter-association-id.is-invalid :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px var(--el-color-danger) inset; }
.filter-date { max-width: 380px; }

.field-error {
  margin: var(--app-space-2) 0 0;
  font-size: var(--app-text-xs);
  color: var(--el-color-danger);
}

.warning-list {
  margin: 2px 0 0;
  padding-left: 1.2em;
  font-size: var(--app-text-xs);
  line-height: 1.6;
}

.scope-line {
  margin: 0 0 var(--app-space-3);
}

.context-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.context-chip {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: var(--app-text-xs);
  line-height: 1.5;
  padding: 0 6px;
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-sm);
  background: transparent;
  color: var(--el-color-primary);
  cursor: pointer;
  word-break: break-all;
  text-align: left;
}

.context-chip:hover { background: var(--el-color-primary-light-9); }
.context-chip--plain { color: var(--app-text-secondary); cursor: default; }
.context-chip--plain:hover { background: transparent; }

.pagination {
  margin-top: var(--app-space-5);
  display: flex;
  justify-content: flex-end;
  overflow-x: auto;
}

@media (max-width: 767px) {
  .toolbar-left > *,
  .filter-level,
  .filter-keyword,
  .filter-date { width: 100%; max-width: none; }
  .filter-association-type { width: 120px; flex-shrink: 0; }
  .filter-association-id { width: auto; flex: 1; min-width: 0; }
  .toolbar-left .el-button { flex: 1; }
  .pagination { justify-content: center; }
}

.log-message {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: var(--app-text-sm);
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.5;
}

/* 查询提示条：紧凑化，降低视觉冲击 */
.logs-alert {
  margin-bottom: var(--app-space-3);
  padding: 4px 12px;
  border-radius: var(--app-radius-md);
}

.logs-alert :deep(.el-alert__content) {
  padding: 0 var(--app-space-2);
}

.logs-alert :deep(.el-alert__title) {
  font-size: var(--app-text-xs);
  line-height: 1.6;
}

.logs-alert :deep(.el-alert__description) {
  font-size: var(--app-text-xs);
  margin-top: 2px;
  line-height: 1.5;
}

.logs-alert :deep(.el-alert__icon) {
  font-size: var(--app-text-base);
  width: var(--app-text-base);
}
</style>
