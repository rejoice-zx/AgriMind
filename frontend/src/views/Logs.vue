<script setup lang="ts">
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Refresh, Download, Delete, ArrowDown } from '@element-plus/icons-vue'
import { getLogs, downloadLogs, clearLogs } from '@/api/logs'
import PageHeader from '@/components/PageHeader.vue'
import type { LogEntry } from '@/types'

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

// Load logs
async function loadLogs() {
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
    if (filters.keyword) params.keyword = filters.keyword
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
    ElMessage.error(error.response?.data?.detail || '加载日志失败')
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
    URL.revokeObjectURL(url)
    ElMessage.success('下载成功')
  } catch (error: any) {
    ElMessage.error(error.response?.status === 413
      ? '日志文件超过8MiB下载上限，请在部署环境归档'
      : error.response?.data?.detail || '下载失败')
  }
}

// Clear logs
async function handleClear(logType: 'app' | 'error') {
  try {
    await ElMessageBox.confirm(
      `确定要清空${logType === 'app' ? '应用' : '错误'}日志吗？此操作不可恢复。`,
      '确认清空',
      { type: 'warning' }
    )
    const response = await clearLogs(logType)
    if (!response.data.success) { ElMessage.error('清空失败'); return }
    ElMessage.success('已清空日志')
    loadLogs()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error.response?.data?.detail || '清空失败')
    }
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
    <PageHeader title="日志管理" description="查看和管理系统日志；下载为脱敏后的当前文件，最大8MiB">
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
            style="width: 140px"
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
            placeholder="搜索关键词"
            clearable
            style="width: 200px"
            @keyup.enter="handleSearch"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
          <el-select v-model="filters.associationType" style="width: 150px" aria-label="关联类型">
            <el-option label="HTTP请求ID" value="request_id" />
            <el-option label="业务请求ID" value="business_request_id" />
            <el-option label="农业会话ID" value="conversation_id" />
            <el-option label="咨询工单ID" value="consultation_id" />
            <el-option label="田块ID" value="field_id" />
            <el-option label="病害记录ID" value="record_id" />
          </el-select>
          <el-input v-model="filters.associationId" placeholder="关联编号" clearable
            style="width: 260px" @keyup.enter="handleSearch" />
          <el-date-picker
            v-model="dateRange"
            type="datetimerange"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            style="width: 360px"
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
    </el-card>

    <el-alert v-if="failed" class="logs-alert" title="日志查询失败，未展示上次结果" type="error" :closable="false" />
    <el-alert v-if="queryInfo" class="logs-alert" :type="queryInfo.truncated ? 'warning' : 'info'" :closable="false"
      :title="queryInfo.truncated ? '结果有范围限制：未读取全部日志，未找到不代表从未发生' : '共计为当前查询窗口内去重后的匹配数'"
      description="每份受支持日志读取末尾最多1MiB，每次最多8MiB及20000条；包含最多5份轮转文件。分页期间日志可能更新。" />
    <el-alert v-if="queryInfo && queryInfo.legacy" class="logs-alert" type="warning" :closable="false"
      title="窗口含旧格式日志：无时区时间按北京时间解释；旧部署若用其他时区，需另行核对" />
    <el-alert v-if="queryInfo && queryInfo.unavailable" class="logs-alert" type="warning" :closable="false" title="部分日志暂不可读" />

    <el-alert v-if="queryInfo && queryInfo.unparsed" class="logs-alert" type="warning" :closable="false"
      title="窗口含无法解析为独立事件的旧行（如堆栈续行或缺失日期）；这些行未计入匹配总数" />

    <!-- Log Table -->
    <el-card
      class="table-card"
      shadow="never"
    >
      <el-table
        v-loading="loading"
        :data="logList"
        stripe
      >
        <el-table-column
          label="时间"
          width="180"
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
        <el-table-column label="关联信息" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">{{ formatContext(row) || '旧记录无关联信息' }}</template>
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
          layout="total, sizes, prev, pager, next, jumper"
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

.pagination {
  margin-top: var(--app-space-5);
  display: flex;
  justify-content: flex-end;
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
