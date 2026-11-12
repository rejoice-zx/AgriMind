<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Download, Delete, Timer, Odometer } from '@element-plus/icons-vue'
import { echarts } from '@/composables/useChart'
import PageHeader from '@/components/PageHeader.vue'
import { errorText, isDismissed } from '@/utils/errorText'
import {
  getPerformanceMetrics,
  clearPerformanceData,
  exportPerformanceReport
} from '@/api/performance'

// Types
interface MetricStats {
  name: string
  count: number
  successRate: number | null
  totalCount: number
  avgDuration: number
  minDuration: number
  maxDuration: number
  p50Duration: number
  p95Duration: number
  p99Duration: number
  outcomeCounts: Record<string, number>
  totalOutcomeCounts: Record<string, number>
}

interface PerformanceSummary {
  uptimeSeconds: number
  uptimeFormatted: string
  totalRequests: number
  overallSuccessRate: number | null
  startTime: string
  clearedAt: string | null
  degradedRequests: number | null
  processId: number | null
}

// State
const loading = ref(false)
const loadFailed = ref(false)
const summary = ref<PerformanceSummary | null>(null)
const metrics = ref<Record<string, MetricStats>>({})
let loadRevision = 0
let active = true

// ECharts
const barChartRef = ref<HTMLElement | null>(null)
const successChartRef = ref<HTMLElement | null>(null)
let barChartInstance: ReturnType<typeof echarts.init> | null = null
let successChartInstance: ReturnType<typeof echarts.init> | null = null

// Whether there is any metric data with actual calls
const hasData = computed(() => Object.values(metrics.value).some(m => m.count > 0))

/* ECharts canvas 无法解析 CSS var()，需先解析为实际颜色值 */
function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

// 坐标轴 / 图例配色（跟随令牌，亮暗主题自适应）
function chartAxisStyle() {
  const text = cssVar('--app-text-secondary')
  const line = cssVar('--app-border')
  const split = cssVar('--app-border-light')
  return {
    axisLabel: { color: text },
    axisLine: { lineStyle: { color: line } },
    splitLine: { lineStyle: { color: split } },
    nameTextStyle: { color: text }
  }
}

// Get metric display label
function getMetricLabel(key: string): string {
  const labels: Record<string, string> = {
    'agri_decision_request': '农业决策请求',
    'agri_tool_request': '工具调试请求',
    'agri_archive_request': '农业档案请求',
    'agri_consultation_request': '农业咨询请求',
    'agri_plan_request': '农事计划请求',
    'agri_diagnosis_request': '图像诊断/上传请求',
    'agri_expert_case_request': '图像病例请求',
    'agri_field_request': '农场/田块/履历请求',
    'agri_tool_get_field_info': '工具：田块档案',
    'agri_tool_get_weather': '工具：天气查询',
    'agri_tool_get_farming_history': '工具：农事追溯',
    'agri_tool_search_agri_knowledge': '工具：农业知识检索',
    'agri_tool_diagnose_crop_image': '工具：图像初诊',
    'agri_tool_calculate_gdd': '工具：积温计算',
    'embedding_api': 'Embedding（含排队及重试）',
    'embedding_http': 'Embedding HTTP尝试',
    'agri_decision_model': '决策模型处理',
    'agri_vision_model': '视觉模型处理',
    'agri_decision_model_http': '决策模型HTTP',
    'agri_vision_model_http': '视觉模型HTTP',
    'agri_database_statement': '农业请求数据库执行',
    'vector_search': '向量检索',
    'keyword_search': '关键词检索',
    'knowledge_add': '知识库添加',
    'knowledge_update': '知识库更新'
  }
  return labels[key] || key
}

// Load performance data
async function loadPerformanceData() {
  const revision = ++loadRevision
  loading.value = true
  loadFailed.value = false
  try {
    const metricsRes = await getPerformanceMetrics()
    if (!active || revision !== loadRevision) return
    const data = metricsRes.data.summary
    summary.value = {
      uptimeSeconds: data.uptime_seconds,
      uptimeFormatted: data.uptime_formatted,
      totalRequests: data.total_requests,
      overallSuccessRate: data.overall_success_rate,
      startTime: data.start_time,
      clearedAt: data.cleared_at,
      degradedRequests: data.degraded_requests ?? null,
      processId: data.process_id ?? null
    }

    const transformedMetrics: Record<string, MetricStats> = {}
    for (const [key, v] of Object.entries(metricsRes.data.metrics)) {
      transformedMetrics[key] = {
        name: v.name,
        count: v.count,
        totalCount: v.total_count,
        successRate: v.success_rate,
        avgDuration: v.avg_duration,
        minDuration: v.min_duration,
        maxDuration: v.max_duration,
        p50Duration: v.p50_duration,
        p95Duration: v.p95_duration,
        p99Duration: v.p99_duration,
        outcomeCounts: v.outcome_counts || {},
        totalOutcomeCounts: v.total_outcome_counts || {}
      }
    }
    metrics.value = transformedMetrics
    if (!hasData.value) disposeCharts()

    await nextTick()
    if (!active || revision !== loadRevision) return
    renderBarChart()
    renderSuccessChart()
  } catch (error: any) {
    if (!active || revision !== loadRevision) return
    summary.value = null; metrics.value = {}; disposeCharts()
    loadFailed.value = true
    ElMessage.error(errorText(error, '读取性能数据失败'))
  } finally {
    if (active && revision === loadRevision) loading.value = false
  }
}

// Render bar chart with ECharts
function renderBarChart() {
  if (!hasData.value) return
  if (!barChartRef.value) return

  if (!barChartInstance) {
    barChartInstance = echarts.init(barChartRef.value)
  }

  const entries = Object.entries(metrics.value).filter(([, m]) => m.count > 0)
  if (entries.length === 0) {
    barChartInstance.clear()
    return
  }

  const categories = entries.map(([k]) => getMetricLabel(k))
  const p50Data = entries.map(([, m]) => +(m.p50Duration * 1000).toFixed(2))
  const p95Data = entries.map(([, m]) => +(m.p95Duration * 1000).toFixed(2))
  const p99Data = entries.map(([, m]) => +(m.p99Duration * 1000).toFixed(2))
  const axis = chartAxisStyle()

  // 毫秒级数据库语句与秒级模型调用同框时，线性刻度会让小值不可见，跨度大时改用对数刻度
  const positive = p99Data.filter(value => value > 0)
  const useLog = positive.length > 1 && Math.max(...positive) / Math.min(...positive) > 100
  const brand = cssVar('--el-color-primary')
  barChartInstance.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      renderMode: 'richText',
      backgroundColor: cssVar('--app-surface'),
      borderColor: cssVar('--app-border'),
      textStyle: { color: cssVar('--app-text') },
      valueFormatter: (value: number) => `${value} 毫秒`
    },
    legend: {
      data: ['P50', 'P95', 'P99'],
      top: 0,
      textStyle: { color: cssVar('--app-text-secondary') }
    },
    grid: { left: 8, right: 24, top: 36, bottom: 36, containLabel: true },
    yAxis: {
      type: 'category',
      data: categories,
      inverse: true,
      ...axis,
      axisLabel: { ...axis.axisLabel, width: 120, overflow: 'truncate' }
    },
    xAxis: {
      type: useLog ? 'log' : 'value',
      ...axis,
      name: useLog ? '毫秒（对数刻度）' : '毫秒',
      nameLocation: 'middle',
      nameGap: 26
    },
    // 同一色相的深浅表示分位，不用红绿灯配色暗示"好坏"
    series: [
      { name: 'P50', type: 'bar', data: p50Data, itemStyle: { color: brand, opacity: 0.45 }, barMaxWidth: 14 },
      { name: 'P95', type: 'bar', data: p95Data, itemStyle: { color: brand, opacity: 0.75 }, barMaxWidth: 14 },
      { name: 'P99', type: 'bar', data: p99Data, itemStyle: { color: brand }, barMaxWidth: 14 }
    ]
  }, true)
}

// Render success rate gauge / pie chart
function renderSuccessChart() {
  if (!hasData.value) return
  if (!successChartRef.value) return

  if (!successChartInstance) {
    successChartInstance = echarts.init(successChartRef.value)
  }

  const entries = Object.entries(metrics.value).filter(([, m]) => m.count > 0)
  if (entries.length === 0) {
    successChartInstance.clear()
    return
  }

  const categories = entries.map(([k]) => getMetricLabel(k))
  const countData = entries.map(([, m]) => m.count)
  const axis = chartAxisStyle()

  successChartInstance.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      renderMode: 'richText',
      backgroundColor: cssVar('--app-surface'),
      borderColor: cssVar('--app-border'),
      textStyle: { color: cssVar('--app-text') },
      valueFormatter: (value: number) => `${value} 次`
    },
    grid: { left: 8, right: 24, top: 16, bottom: 36, containLabel: true },
    yAxis: {
      type: 'category',
      data: categories,
      inverse: true,
      ...axis,
      axisLabel: { ...axis.axisLabel, width: 120, overflow: 'truncate' }
    },
    xAxis: { type: 'value', minInterval: 1, min: 0, ...axis, name: '次', nameLocation: 'middle', nameGap: 26 },
    series: [
      { name: '最近样本数', type: 'bar', data: countData, itemStyle: { color: cssVar('--el-color-primary') }, barMaxWidth: 16 }
    ]
  }, true)
}

// Resize handler
function handleResize() {
  barChartInstance?.resize()
  successChartInstance?.resize()
}

// Format duration
function formatDuration(seconds: number): string {
  if (seconds < 0.001) return '< 1ms'
  if (seconds < 1) return `${(seconds * 1000).toFixed(1)}ms`
  return `${seconds.toFixed(2)}s`
}

// Format percentage
function formatOutcomes(counts: Record<string, number>): string {
  const labels: Record<string, string> = { success: "完成", error: "失败", degraded: "降级", cancelled: "取消", rejected: "拒绝", skipped: "跳过" }
  const entries = Object.entries(counts)
  return entries.length ? entries.map(([key, value]) => `${labels[key] || key} ${value}`).join("；") : "无分类样本"
}

function formatPercent(value: number | null): string {
  return value === null ? '无样本' : `${(value * 100).toFixed(1)}%`
}
// 完成比例标签：无样本显示灰色，不再落入"危险"红色
function rateTag(value: number | null): 'success' | 'warning' | 'danger' | 'info' {
  if (value === null || value === undefined) return 'info'
  return value >= 0.95 ? 'success' : value >= 0.8 ? 'warning' : 'danger'
}
const SMALL_SAMPLE = 20
function disposeCharts() {
  barChartInstance?.dispose(); successChartInstance?.dispose()
  barChartInstance = null; successChartInstance = null
}

// Clear performance data
async function handleClear() {
  try {
    await ElMessageBox.confirm(
      '将清空当前后端进程的全部性能采样和累计计数，清空后无法恢复。农业档案与业务数据不受影响。',
      '清空性能采样',
      { type: 'warning', confirmButtonText: '清空采样', cancelButtonText: '取消' }
    )
  } catch (error) { if (isDismissed(error)) return; throw error }
  try {
    await clearPerformanceData()
    ElMessage.success('已清空当前后端进程的性能采样')
    loadPerformanceData()
  } catch (error: unknown) {
    ElMessage.error(errorText(error, '清空失败'))
  }
}

// Export report
async function handleExport() {
  try {
    const response = await exportPerformanceReport()
    const content = typeof response.data === 'string' ? response.data : String(response.data)
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `performance_report_${new Date().toISOString().split('T')[0]}.txt`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    ElMessage.success('导出成功')
  } catch (error: unknown) {
    ElMessage.error(errorText(error, '导出失败'))
  }
}

// Format start time
function formatStartTime(isoString: string): string {
  return new Date(isoString).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
}

// Lifecycle
let themeObserver: MutationObserver | null = null

onMounted(() => {
  loadPerformanceData()
  window.addEventListener('resize', handleResize)
  // 亮暗主题切换后令牌色值变化，需重绘图表
  themeObserver = new MutationObserver(() => {
    renderBarChart()
    renderSuccessChart()
  })
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
})

onBeforeUnmount(() => {
  active = false; ++loadRevision
  window.removeEventListener('resize', handleResize)
  themeObserver?.disconnect()
  disposeCharts()
})
</script>

<template>
  <div class="performance-container">
    <PageHeader
      title="性能监控"
      description="当前后端进程的农业请求与工具采样；重启后重置，多进程各自统计。完成比例不代表模型或诊断正确率。"
    >
      <template #actions>
        <el-button :icon="Refresh" @click="loadPerformanceData">刷新</el-button>
        <el-button :icon="Download" @click="handleExport">导出报告</el-button>
        <el-button type="danger" :icon="Delete" @click="handleClear">清空数据</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="loadFailed" class="page-alert" title="性能数据读取失败，未用旧数据或虚构比例替代" type="error" :closable="false" show-icon>
      <el-button size="small" @click="loadPerformanceData">重试</el-button>
    </el-alert>
    <!-- 口径说明：与页内说明文字同级的小字，点开才展开，不占一整条折叠面板 -->
    <details class="scope-note">
      <summary>统计口径说明</summary>
      <ul class="scope-list">
          <li>"完整成功比例"只统计已采集的农业接口请求中正常返回（HTTP 2xx）的比例，包含被权限或参数校验拒绝的请求；不代表模型或诊断正确率。</li>
          <li>工具和检索的耗时单独列出，不累加进请求总数；嵌套指标不能相加当作总耗时。</li>
          <li>明细基于最近 100 条样本；P50/P95/P99 用最近秩法计算，样本少于 {{ SMALL_SAMPLE }} 条时标注"样本不足"，此时 P99 接近最大值。</li>
          <li>数据只保存在当前后端进程内存中，重启后清零，多进程各自统计。</li>
      </ul>
    </details>
    <p v-if="summary?.clearedAt" class="page-hint">采样最近清空时间：{{ formatStartTime(summary.clearedAt) }}</p>
    <!-- Summary Cards -->
    <div v-loading="loading" class="summary-cards">
      <el-card class="summary-card" shadow="never">
        <div class="summary-icon">
          <el-icon size="24"><Timer /></el-icon>
        </div>
        <div class="summary-content">
          <div class="summary-value">{{ summary?.uptimeFormatted || '-' }}</div>
          <div class="summary-label">监控运行时长</div>
        </div>
      </el-card>
      <el-card class="summary-card" shadow="never">
        <div class="summary-icon">
          <el-icon size="24"><Odometer /></el-icon>
        </div>
        <div class="summary-content">
          <div class="summary-value">{{ summary?.totalRequests ?? '-' }}</div>
          <div class="summary-label">已采集的农业接口请求（累计）</div>
        </div>
      </el-card>
      <el-card class="summary-card" shadow="never">
        <div class="summary-icon success">
          <el-icon size="24"><Odometer /></el-icon>
        </div>
        <div class="summary-content">
          <div class="summary-value">{{ summary ? formatPercent(summary.overallSuccessRate) : '-' }}</div>
          <div class="summary-label">完整成功比例（HTTP 2xx）</div>
        </div>
      </el-card>
      <el-card class="summary-card" shadow="never">
        <div class="summary-content">
          <div class="summary-value small">{{ summary ? formatStartTime(summary.startTime) : '-' }}</div>
          <div class="summary-label">监控启动时间</div>
        </div>
      </el-card>
    </div>

    <!-- Charts Row -->
    <div class="charts-row">
      <!-- Bar Chart: P50/P95/P99 -->
      <el-card class="chart-card" shadow="never">
        <template #header><span>耗时分位（毫秒）</span></template>
        <div v-if="hasData" ref="barChartRef" class="chart-box" />
        <div v-else class="empty-box">
          <el-empty description="暂无性能数据" :image-size="60" />
        </div>
      </el-card>
      <!-- Mixed Chart: avg + count + success rate -->
      <el-card class="chart-card" shadow="never">
        <template #header><span>最近样本数（次）</span></template>
        <div v-if="hasData" ref="successChartRef" class="chart-box" />
        <div v-else class="empty-box">
          <el-empty description="暂无性能数据" :image-size="60" />
        </div>
      </el-card>
    </div>

    <el-alert v-if="summary" class="page-alert" :title="`后端进程 ${summary.processId ?? '未返回'}；检测到模型降级的完整2xx请求 ${summary.degradedRequests ?? '未返回'} 次（累计）`" type="info" :closable="false" />


    <!-- Metrics Table -->
    <el-card class="table-card" shadow="never">
      <template #header><span>详细指标</span></template>
      <el-table :data="Object.entries(metrics).filter(([, value]) => value.count > 0).map(([k, v]) => ({ key: k, ...v }))" stripe>
        <el-table-column label="指标" width="160" fixed="left">
          <template #default="{ row }">{{ getMetricLabel(row.key) }}</template>
        </el-table-column>
        <el-table-column label="最近样本" width="110" align="right">
          <template #default="{ row }">
            <span class="app-num">{{ row.count }}</span>
            <el-tag v-if="row.count < SMALL_SAMPLE" size="small" type="info" effect="plain" class="sample-tag">样本不足</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="totalCount" label="累计调用" width="100" align="right" />
        <el-table-column label="完成比例" width="100" align="right">
          <template #default="{ row }">
            <el-tag :type="rateTag(row.successRate)" size="small">
              {{ formatPercent(row.successRate) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近结果分类" min-width="200">
          <template #default="{ row }">{{ formatOutcomes(row.outcomeCounts) }}</template>
        </el-table-column>
        <el-table-column label="累计结果分类" min-width="200">
          <template #default="{ row }">{{ formatOutcomes(row.totalOutcomeCounts) }}</template>
        </el-table-column>
        <el-table-column label="平均耗时" width="120" align="right">
          <template #default="{ row }">{{ formatDuration(row.avgDuration) }}</template>
        </el-table-column>
        <el-table-column label="P50" width="100" align="right">
          <template #default="{ row }">{{ formatDuration(row.p50Duration) }}</template>
        </el-table-column>
        <el-table-column label="P95" width="100" align="right">
          <template #default="{ row }">{{ formatDuration(row.p95Duration) }}</template>
        </el-table-column>
        <el-table-column label="P99" width="100" align="right">
          <template #default="{ row }">{{ formatDuration(row.p99Duration) }}</template>
        </el-table-column>
        <el-table-column label="最小/最大" min-width="150">
          <template #default="{ row }">{{ formatDuration(row.minDuration) }} / {{ formatDuration(row.maxDuration) }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!hasData" description="暂无性能数据" />
    </el-card>
  </div>
</template>

<style scoped>
.performance-container {
  padding: var(--app-space-5) var(--app-space-6);
  max-width: 1400px;
  margin: 0 auto;
}

.page-alert {
  margin-bottom: var(--app-space-3);
}

.page-hint {
  margin: 0 0 var(--app-space-3);
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
  line-height: 1.6;
}

.summary-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--app-space-4);
  margin-bottom: var(--app-space-6);
}

.summary-card :deep(.el-card__body) {
  display: flex;
  align-items: center;
  gap: var(--app-space-4);
  width: 100%;
}

.summary-icon {
  width: 48px;
  height: 48px;
  border-radius: var(--app-radius-xl);
  background-color: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.summary-icon.success {
  background-color: var(--el-color-success-light-9);
  color: var(--el-color-success);
}

.summary-content { flex: 1; }

.summary-value {
  font-size: var(--app-text-3xl);
  font-weight: 600;
  color: var(--app-text);
  line-height: 1.2;
}

.summary-value.small {
  font-size: var(--app-text-base);
  font-weight: 500;
}

.summary-label {
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
  margin-top: var(--app-space-1);
}

.charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--app-space-5);
  margin-bottom: var(--app-space-5);
}

.chart-card { position: relative; }

.chart-box {
  width: 100%;
  height: 360px;
}

.empty-box {
  width: 100%;
  height: 360px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.table-card { margin-bottom: var(--app-space-5); }

.warning-text { color: var(--el-color-warning); }
.danger-text { color: var(--el-color-danger); }

.scope-note {
  margin: calc(-1 * var(--app-space-2)) 0 var(--app-space-3);
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.scope-note summary {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-height: 24px;
  cursor: pointer;
  list-style: none;
  user-select: none;
}

.scope-note summary::-webkit-details-marker {
  display: none;
}

.scope-note summary::before {
  content: '';
  width: 0;
  height: 0;
  border-left: 4px solid currentColor;
  border-top: 4px solid transparent;
  border-bottom: 4px solid transparent;
  transition: transform var(--app-duration-fast, 0.15s);
}

.scope-note[open] summary::before {
  transform: rotate(90deg);
}

.scope-note summary:hover {
  color: var(--app-text);
}

.scope-list {
  margin: var(--app-space-1) 0 0;
  padding-left: 1.4em;
  line-height: 1.7;
}

.sample-tag {
  margin-left: 4px;
}

@media (max-width: 1023px) {
  .charts-row { grid-template-columns: 1fr; }
}

@media (max-width: 768px) {
  .summary-cards { grid-template-columns: repeat(2, 1fr); }
}
</style>
