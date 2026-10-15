<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import { getAgriculturalStatistics, exportStatisticsReport } from '@/api/statistics'
import PageHeader from '@/components/PageHeader.vue'
import type { AgriStatisticsSnapshot } from '@/types/agriStatistics'

const snapshot = ref<AgriStatisticsSnapshot | null>(null)
const dateRange = ref<string[]>([])
const loading = ref(false)
const failed = ref(false)
const exporting = ref(false)
const trendRef = ref<HTMLElement | null>(null)
const knowledgeRef = ref<HTMLElement | null>(null)
const cropRef = ref<HTMLElement | null>(null)
let trend: echarts.ECharts | null = null
let knowledge: echarts.ECharts | null = null
let crops: echarts.ECharts | null = null
let revision = 0
let active = true
const assets = computed(() => {
  if (!snapshot.value) return []
  const o = snapshot.value.overview
  return [{ label: '农场', value: o.total_farms }, { label: '田块', value: o.total_fields },
    { label: '活跃种植批次', value: o.active_crop_plantings }, { label: '农事履历条目', value: o.total_records },
    { label: '农业对话档案', value: o.total_conversations }, { label: '标记当前的知识条目', value: o.current_knowledge_items }]
})
const formatRate = (value: number | null) => value === null ? '无已结束提问' : `${(value * 100).toFixed(1)}%`
const formatLatency = (value: number | null) => value === null ? '无有效样本' : `${value.toFixed(2)} ms`
const distributionRows = (values?: Record<string, number>) => Object.entries(values || {}).map(([name, count]) => ({ name, count }))
function clearCharts() { trend?.clear(); knowledge?.clear(); crops?.clear() }
async function loadStatistics() {
  const current = ++revision
  loading.value = true; failed.value = false; snapshot.value = null
  clearCharts()
  try {
    const response = await getAgriculturalStatistics({ start_date: dateRange.value?.[0], end_date: dateRange.value?.[1] })
    if (!active || current !== revision) return
    snapshot.value = response.data
    dateRange.value = [response.data.start_date, response.data.end_date]
    await nextTick()
    if (active && current === revision) renderCharts()
  } catch (error: any) {
    if (!active || current !== revision) return
    failed.value = true
    ElMessage.error(error.response?.data?.detail || '加载农业统计失败')
  } finally { if (active && current === revision) loading.value = false }
}
function renderCharts() {
  if (!snapshot.value) return
  if (trendRef.value) {
    trend ||= echarts.init(trendRef.value)
    const daily = snapshot.value.daily
    trend.setOption({ tooltip: { trigger: 'axis', renderMode: 'richText' }, legend: { top: 0 },
      grid: { top: 45, left: 45, right: 20, bottom: 35 },
      xAxis: { type: 'category', data: daily.map(item => item.date) }, yAxis: { type: 'value', minInterval: 1 },
      series: [{ name: '归档提问', type: 'line', data: daily.map(item => item.turns) },
        { name: '农事履历', type: 'line', data: daily.map(item => item.farming_records) },
        { name: '专家当日答复', type: 'line', data: daily.map(item => item.consultations_resolved) }]
    }, true)
  }
  if (knowledgeRef.value) {
    knowledge ||= echarts.init(knowledgeRef.value)
    renderPie(knowledge, snapshot.value.categories.knowledge)
  }
  if (cropRef.value) {
    crops ||= echarts.init(cropRef.value)
    renderPie(crops, snapshot.value.categories.active_crops)
  }
}
function renderPie(chart: echarts.ECharts, values: Record<string, number>) {
  const data = Object.entries(values).map(([name, value]) => ({ name, value }))
  if (!data.length) { chart.clear(); return }
  chart.setOption({ tooltip: { trigger: 'item', renderMode: 'richText' },
    series: [{ type: 'pie', radius: ['35%', '65%'], data, label: { formatter: '{b}: {c}' } }] }, true)
}
function resize() { trend?.resize(); knowledge?.resize(); crops?.resize() }
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
    link.click(); URL.revokeObjectURL(url)
  } catch (error: any) { if (active) ElMessage.error(error.response?.data?.detail || '导出失败') }
  finally { if (active) exporting.value = false }
}
onMounted(() => { loadStatistics(); window.addEventListener('resize', resize) })
onBeforeUnmount(() => {
  active = false; ++revision
  window.removeEventListener('resize', resize)
  trend?.dispose(); knowledge?.dispose(); crops?.dispose()
  trend = knowledge = crops = null
})
</script>
<template>
  <div class="app-page statistics-page" v-loading="loading">
    <PageHeader title="农业业务统计" description="资产为当前全库留存数量，提问及通用咨询按创建日期筛选。全部日期使用北京时间。">
      <template #actions>
        <div class="header-actions">
          <el-date-picker v-model="dateRange" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期" @change="loadStatistics" />
          <el-button :loading="loading" @click="loadStatistics">刷新</el-button>
          <el-button :disabled="!snapshot || loading" :loading="exporting" @click="exportReport">生成最新报告</el-button>
        </div>
      </template>
    </PageHeader>
    <el-alert v-if="failed" type="error" title="统计读取失败，未用零值或旧数据替代" :closable="false" />
    <p v-if="snapshot" class="data-time">读取时间：{{ new Date(snapshot.generated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }} · 当前留存记录口径，删除业务记录后数量会改变。</p>
    <div class="asset-cards"><el-card v-for="item in assets" :key="item.label" shadow="never"><strong>{{ item.value }}</strong><p class="asset-label">{{ item.label }}</p></el-card></div>
    <el-card v-if="snapshot" class="period-card" shadow="never">
      <template #header>期间农业分析 · {{ snapshot.start_date }} 至 {{ snapshot.end_date }}</template>
      <div class="period-values">
        <span>归档提问：{{ snapshot.overview.period_turns }}</span><span>已完成：{{ snapshot.overview.completed_turns }}</span>
        <span>失败：{{ snapshot.overview.failed_turns }}</span><span>处理中：{{ snapshot.overview.pending_turns }}</span>
        <span>完成率：{{ formatRate(snapshot.overview.completion_rate) }}</span>
        <span>已完成分析平均耗时：{{ formatLatency(snapshot.overview.avg_response_time_ms) }}（{{ snapshot.overview.response_time_sample_count }}条有效样本）</span>
      </div>
      <p class="card-note">完成率 = 已完成 /（已完成 + 失败），不计处理中记录；包含明确阻断和资料不足的分析结果，不能代表诊断正确率或作业获准比例。</p>
      <p v-if="snapshot.overview.stale_pending_turns" class="card-note">其中 {{ snapshot.overview.stale_pending_turns }} 条处理超过30分钟尚待核对，可由提问者刷新原对话确认状态。</p>
      <p class="card-note">期间创建通用咨询：{{ snapshot.overview.period_consultations }} · 待接单 {{ snapshot.overview.pending_consultations }} · 处理中 {{ snapshot.overview.in_review_consultations }} · 已答复 {{ snapshot.overview.resolved_consultations }}</p>
      <p class="card-note">独立图像病例：{{ snapshot.overview.total_disease_records }} · 当前待复核 {{ snapshot.overview.pending_disease_cases }}（与通用咨询分别计数）</p>
    </el-card>
    <el-card class="chart-card" shadow="never"><template #header>期间活动趋势</template><div ref="trendRef" class="chart" /></el-card>
    <div class="two-columns">
      <el-card shadow="never"><template #header>标记当前版本的知识分类</template><div ref="knowledgeRef" class="chart" /><p v-if="snapshot && !Object.keys(snapshot.categories.knowledge).length" class="card-note">暂无当前知识条目</p></el-card>
      <el-card shadow="never"><template #header>当前活跃种植批次的作物分布</template><div ref="cropRef" class="chart" /><p v-if="snapshot && !Object.keys(snapshot.categories.active_crops).length" class="card-note">暂无活跃种植批次</p></el-card>
    </div>
    <div v-if="snapshot" class="two-columns">
      <el-card shadow="never"><template #header>期间完成分析的类型</template><el-table :data="distributionRows(snapshot.categories.decisions)"><el-table-column prop="name" label="实际记录类型" /><el-table-column prop="count" label="数量" /></el-table></el-card>
      <el-card shadow="never"><template #header>期间农事履历类型</template><el-table :data="distributionRows(snapshot.categories.farming_records)"><el-table-column prop="name" label="实际记录类型" /><el-table-column prop="count" label="数量" /></el-table></el-card>
    </div>
    <el-card v-if="snapshot" class="detail-card" shadow="never">
      <template #header>每日统计 · 提问结果按提问日期及当前状态计数；农事按作业日期，专家答复按保存日期</template>
      <el-table :data="snapshot.daily" stripe>
        <el-table-column prop="date" label="日期" width="120" /><el-table-column prop="turns" label="提问" />
        <el-table-column prop="completed" label="当前已完成" /><el-table-column prop="failed" label="当前失败" /><el-table-column prop="pending" label="当前处理中" />
        <el-table-column prop="farming_records" label="农事履历" /><el-table-column prop="consultations_created" label="咨询创建" /><el-table-column prop="consultations_resolved" label="当日专家答复" />
      </el-table>
    </el-card>
    <el-card v-if="snapshot" class="detail-card" shadow="never"><template #header>期间高频提问 · 归档提问次数，重试返回原结果不会新增</template><el-table :data="snapshot.top_questions"><el-table-column prop="question" label="提问" show-overflow-tooltip /><el-table-column prop="count" label="次数" width="100" /></el-table></el-card>
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

@media (max-width: 900px) {
  .two-columns {
    grid-template-columns: 1fr;
  }
}
</style>
