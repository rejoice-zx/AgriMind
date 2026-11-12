<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { getAgriculturalStatistics, exportStatisticsReport } from '@/api/statistics'
import PageHeader from '@/components/PageHeader.vue'
import type { AgriStatisticsSnapshot } from '@/types/agriStatistics'
import { useChart, baseOption, axisStyle, type ChartTheme } from '@/composables/useChart'
import { recordKind } from '@/constants/agri'
import { errorText } from '@/utils/errorText'

const snapshot = ref<AgriStatisticsSnapshot | null>(null)
const dateRange = ref<string[]>([])
const loading = ref(false)
const failed = ref(false)
const exporting = ref(false)
const trendRef = ref<HTMLElement | null>(null)
const knowledgeRef = ref<HTMLElement | null>(null)
const cropRef = ref<HTMLElement | null>(null)
let revision = 0
let active = true
const assets = computed(() => {
  if (!snapshot.value) return []
  const o = snapshot.value.overview
  return [{ label: '农场（含已归档）', value: o.total_farms, unit: '个' }, { label: '田块（含已归档）', value: o.total_fields, unit: '个' },
    { label: '在种批次', value: o.active_crop_plantings, unit: '个' }, { label: '农事记录', value: o.total_records, unit: '条' },
    { label: '资料分析对话', value: o.total_conversations, unit: '个' }, { label: '标记为当前版本的知识', value: o.current_knowledge_items, unit: '条' }]
})
const formatRate = (value: number | null) => value === null ? '无已结束提问' : `${(value * 100).toFixed(1)}%`
const formatLatency = (value: number | null) => value === null ? '无有效样本' : `${value.toFixed(2)} ms`
const DECISION_LABELS: Record<string, string> = { SPRAY_DECISION: '施药', FERTILIZATION: '施肥', DIAGNOSIS: '诊断', GDD_PREDICTION: '积温', GENERAL: '一般问题' }
const distributionRows = (values?: Record<string, number>) => Object.entries(values || {}).map(([name, count]) => ({ name, count }))
const decisionRows = computed(() => distributionRows(snapshot.value?.categories.decisions).map(row => ({ ...row, name: DECISION_LABELS[row.name] || row.name })))
// 农事类型：历史数据里同一类作业有中英文多个写法，按展示类别合并计数
const farmingRows = computed(() => {
  const merged = new Map<string, number>()
  for (const [name, count] of Object.entries(snapshot.value?.categories.farming_records || {})) {
    const label = recordKind(name).label
    merged.set(label, (merged.get(label) || 0) + count)
  }
  return [...merged.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count)
})

// 三种计数口径不同，分开成三条线并标明单位"条"；纵轴从 0 开始
function trendOption(theme: ChartTheme, data: AgriStatisticsSnapshot | null) {
  if (!data?.daily.length) return null
  const daily = data.daily
  return { ...baseOption(theme), tooltip: { ...(baseOption(theme) as any).tooltip, trigger: 'axis' },
    grid: { top: 40, left: 48, right: 16, bottom: daily.length > 31 ? 64 : 48 },
    xAxis: { type: 'category', data: daily.map(item => item.date), ...axisStyle(theme) },
    yAxis: { type: 'value', minInterval: 1, min: 0, ...axisStyle(theme, '条') },
    dataZoom: daily.length > 31 ? [{ type: 'slider', height: 16, bottom: 28 }] : undefined,
    series: [{ name: '提问', type: 'line', data: daily.map(item => item.turns), showSymbol: daily.length <= 31 },
      { name: '农事记录', type: 'line', data: daily.map(item => item.farming_records), showSymbol: daily.length <= 31 },
      { name: '专家答复', type: 'line', data: daily.map(item => item.consultations_resolved), showSymbol: daily.length <= 31 }] }
}
// 饼图最多 6 类，其余合并为"其他"
function pieOption(theme: ChartTheme, values?: Record<string, number>) {
  const entries = Object.entries(values || {}).sort((a, b) => b[1] - a[1])
  if (!entries.length) return null
  const head = entries.slice(0, 5).map(([name, value]) => ({ name, value }))
  const rest = entries.slice(5).reduce((sum, [, value]) => sum + value, 0)
  const data = rest ? [...head, { name: '其他', value: rest }] : head
  return { ...baseOption(theme), tooltip: { ...(baseOption(theme) as any).tooltip, trigger: 'item', formatter: '{b}：{c} 条（{d}%）' },
    series: [{ type: 'pie', radius: ['38%', '64%'], center: ['50%', '45%'], data,
      label: { color: theme.textSecondary, formatter: '{b}\n{c}', fontSize: 12 }, labelLine: { length: 8, length2: 6 } }] }
}
const trendChart = useChart(trendRef, theme => trendOption(theme, snapshot.value))
const knowledgeChart = useChart(knowledgeRef, theme => pieOption(theme, snapshot.value?.categories.knowledge))
const cropChart = useChart(cropRef, theme => pieOption(theme, snapshot.value?.categories.active_crops))
function renderCharts() { trendChart.render(); knowledgeChart.render(); cropChart.render() }

async function loadStatistics() {
  const current = ++revision
  // 刷新时保留上一次数据，避免整页卡片消失再出现
  loading.value = true; failed.value = false
  try {
    const response = await getAgriculturalStatistics({ start_date: dateRange.value?.[0], end_date: dateRange.value?.[1] })
    if (!active || current !== revision) return
    snapshot.value = response.data
    dateRange.value = [response.data.start_date, response.data.end_date]
    await nextTick()
    if (active && current === revision) renderCharts()
  } catch (error: unknown) {
    if (!active || current !== revision) return
    // 失败时不保留旧数据，避免把其他日期范围的数字当作本次结果
    failed.value = true; snapshot.value = null; renderCharts()
    ElMessage.error(errorText(error, '读取统计失败'))
  } finally { if (active && current === revision) loading.value = false }
}
// 日期快捷选项：跨度上限 90 天（后端限制）
const dateShortcuts = [
  { text: '最近 7 天', value: () => rangeDays(7) },
  { text: '最近 14 天', value: () => rangeDays(14) },
  { text: '最近 30 天', value: () => rangeDays(30) },
  { text: '最近 90 天', value: () => rangeDays(90) },
]
function rangeDays(days: number) {
  const end = new Date(); const start = new Date(); start.setDate(end.getDate() - days + 1)
  return [start, end]
}
async function exportReport() {
  if (!snapshot.value || exporting.value) return
  const range = { start_date: snapshot.value.start_date, end_date: snapshot.value.end_date }
  exporting.value = true
  try {
    const response = await exportStatisticsReport(range)
    if (!active) return
    const url = URL.createObjectURL(new Blob([response.data.content], { type: 'text/markdown;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url; link.download = `agriculture-statistics-${range.start_date}-${range.end_date}.md`
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error: unknown) { if (active) ElMessage.error(errorText(error, '导出失败')) }
  finally { if (active) exporting.value = false }
}
onMounted(() => { loadStatistics() })
// 图表实例由 useChart 在卸载时销毁
onBeforeUnmount(() => { active = false; ++revision })
</script>
<template>
  <div class="app-page statistics-page" v-loading="loading">
    <PageHeader title="平台运营统计" description="系统使用情况：档案数量、提问与咨询处理情况。这些是平台运营数据，不是农业生产或经营指标。全部日期按北京时间。">
      <template #actions>
        <div class="header-actions">
          <el-date-picker v-model="dateRange" type="daterange" value-format="YYYY-MM-DD" :shortcuts="dateShortcuts" start-placeholder="开始日期" end-placeholder="结束日期" aria-label="统计日期范围" @change="loadStatistics" />
          <el-button :loading="loading" @click="loadStatistics">刷新</el-button>
          <el-button :disabled="!snapshot || loading" :loading="exporting" @click="exportReport">导出报告</el-button>
        </div>
      </template>
    </PageHeader>
    <el-alert v-if="failed" type="error" title="统计读取失败，未用零值或旧数据替代" :closable="false" show-icon>
      <el-button size="small" @click="loadStatistics">重试</el-button>
    </el-alert>
    <p v-if="snapshot" class="data-time">读取时间：{{ new Date(snapshot.generated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }} · 下方数量为当前数据库中的记录，不随日期范围变化。</p>
    <div class="asset-cards"><el-card v-for="item in assets" :key="item.label" shadow="never"><strong class="app-num">{{ item.value }}</strong> <span class="asset-unit">{{ item.unit }}</span><p class="asset-label">{{ item.label }}</p></el-card></div>
    <el-card v-if="snapshot" class="period-card" shadow="never">
      <template #header>所选期间的资料分析提问 · {{ snapshot.start_date }} 至 {{ snapshot.end_date }}</template>
      <div class="period-values">
        <span>提问 <b class="app-num">{{ snapshot.overview.period_turns }}</b> 条</span><span>已完成 <b class="app-num">{{ snapshot.overview.completed_turns }}</b></span>
        <span>失败 <b class="app-num">{{ snapshot.overview.failed_turns }}</b></span><span>处理中 <b class="app-num">{{ snapshot.overview.pending_turns }}</b></span>
        <span>处理完成率 <b class="app-num">{{ formatRate(snapshot.overview.completion_rate) }}</b>（{{ snapshot.overview.completed_turns }}/{{ snapshot.overview.completed_turns + snapshot.overview.failed_turns }}）</span>
        <span>平均处理耗时 <b class="app-num">{{ formatLatency(snapshot.overview.avg_response_time_ms) }}</b>（{{ snapshot.overview.response_time_sample_count }} 条样本{{ snapshot.overview.response_time_sample_count < 20 ? '，样本不足' : '' }}）</span>
      </div>
      <p class="card-note">完成率 = 已完成 /（已完成 + 失败），不计处理中记录；包含明确阻断和资料不足的分析结果，不能代表诊断正确率或作业获准比例。</p>
      <p v-if="snapshot.overview.stale_pending_turns" class="card-note">其中 {{ snapshot.overview.stale_pending_turns }} 条处理超过30分钟尚待核对，可由提问者刷新原对话确认状态。</p>
      <p class="card-note">期间创建通用咨询：{{ snapshot.overview.period_consultations }} · 待接单 {{ snapshot.overview.pending_consultations }} · 处理中 {{ snapshot.overview.in_review_consultations }} · 已答复 {{ snapshot.overview.resolved_consultations }}</p>
      <p class="card-note">独立图像病例：{{ snapshot.overview.total_disease_records }} · 当前待复核 {{ snapshot.overview.pending_disease_cases }}（与通用咨询分别计数）</p>
    </el-card>
    <el-card class="chart-card" shadow="never"><template #header>每日数量（条）</template>
      <div v-show="snapshot?.daily.length" ref="trendRef" class="chart" role="img" aria-label="每日提问、农事记录与专家答复数量折线图" />
      <el-empty v-if="snapshot && !snapshot.daily.length" description="所选期间没有记录" :image-size="56" class="chart-empty" />
    </el-card>
    <div class="two-columns">
      <el-card shadow="never"><template #header>当前版本知识的分类（条）</template>
        <div v-show="snapshot && Object.keys(snapshot.categories.knowledge).length" ref="knowledgeRef" class="chart" role="img" aria-label="知识分类分布" />
        <el-empty v-if="snapshot && !Object.keys(snapshot.categories.knowledge).length" description="暂无当前版本的知识条目" :image-size="56" class="chart-empty" />
      </el-card>
      <el-card shadow="never"><template #header>在种批次的作物分布（个）</template>
        <div v-show="snapshot && Object.keys(snapshot.categories.active_crops).length" ref="cropRef" class="chart" role="img" aria-label="在种作物分布" />
        <el-empty v-if="snapshot && !Object.keys(snapshot.categories.active_crops).length" description="暂无在种批次" :image-size="56" class="chart-empty" />
      </el-card>
    </div>
    <div v-if="snapshot" class="two-columns">
      <el-card shadow="never"><template #header>期间完成的提问类型</template><el-table :data="decisionRows" empty-text="期间没有完成的提问"><el-table-column prop="name" label="问题类型" /><el-table-column prop="count" label="数量（条）" align="right" width="110" /></el-table></el-card>
      <el-card shadow="never"><template #header>期间农事记录类型</template><el-table :data="farmingRows" empty-text="期间没有农事记录"><el-table-column prop="name" label="农事类型" /><el-table-column prop="count" label="数量（条）" align="right" width="110" /></el-table></el-card>
    </div>
    <el-card v-if="snapshot" class="detail-card" shadow="never">
      <template #header>每日明细 · 提问按提问日期，农事按作业日期，专家答复按保存日期</template>
      <el-table :data="snapshot.daily" stripe>
        <el-table-column prop="date" label="日期" width="112" fixed="left" /><el-table-column prop="turns" label="提问" min-width="72" align="right" />
        <el-table-column prop="completed" label="已完成" min-width="80" align="right" /><el-table-column prop="failed" label="失败" min-width="72" align="right" /><el-table-column prop="pending" label="处理中" min-width="80" align="right" />
        <el-table-column prop="farming_records" label="农事记录" min-width="88" align="right" /><el-table-column prop="consultations_created" label="新咨询" min-width="80" align="right" /><el-table-column prop="consultations_resolved" label="专家答复" min-width="88" align="right" />
      </el-table>
    </el-card>
    <el-card v-if="snapshot" class="detail-card" shadow="never"><template #header>期间高频提问（重试同一问题不重复计数）</template><el-table :data="snapshot.top_questions"><el-table-column prop="question" label="提问" show-overflow-tooltip /><el-table-column prop="count" label="次数" width="100" /></el-table></el-card>
  </div>
</template>
<style scoped>
.statistics-page {
  max-width: 1440px;
  margin: 0 auto;
}

.header-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--app-space-3);
}

.header-actions .el-button + .el-button {
  margin-left: 0;
}

.data-time,
.card-note,
.asset-label {
  color: var(--app-text-secondary);
  font-size: var(--app-text-sm);
  line-height: 1.7;
}

.asset-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--app-space-3);
  margin: var(--app-space-4) 0;
}

.asset-cards strong {
  font-size: var(--app-text-3xl);
}

.asset-label {
  margin: var(--app-space-1) 0 0;
}

.period-card,
.chart-card,
.detail-card {
  margin: var(--app-space-4) 0;
}

.period-values {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-4);
}

.two-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--app-space-4);
  margin: var(--app-space-4) 0;
}

.chart {
  height: 300px;
  width: 100%;
}

.chart-empty {
  min-height: 300px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.asset-unit {
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
}

@media (max-width: 767px) {
  .chart {
    height: 240px;
  }

  .chart-empty {
    min-height: 240px;
  }
}

@media (max-width: 900px) {
  .two-columns {
    grid-template-columns: 1fr;
  }
}
</style>
