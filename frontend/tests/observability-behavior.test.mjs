import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript'), vue = require('vue')
function harness(name, api) {
  const { descriptor } = parse(readFileSync(new URL(`../src/views/${name}.vue`, import.meta.url), 'utf8'))
  const javascript = ts.transpileModule(compileScript(descriptor, { id: name }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const module = { exports: {} }, hooks = { mount: [], dispose: [] }, errors = [], charts = [], downloads = []
  const dependencies = {
    '@/components/PageHeader.vue': {},
    vue: { ...vue, onMounted: fn => hooks.mount.push(fn), onBeforeUnmount: fn => hooks.dispose.push(fn) },
    echarts: { init: () => { const chart = { options: [], disposed: false,
      setOption(value) { this.options.push(value) }, clear() { this.options = [] }, resize() {}, dispose() { this.disposed = true } }
      charts.push(chart); return chart } },
    'element-plus': { ElMessage: { error: value => errors.push(value), success() {} }, ElMessageBox: { confirm: async () => true } },
    '@element-plus/icons-vue': {}, '@/api/statistics': api, '@/api/performance': api,
  }
  new Script(javascript).runInContext(createContext({ module, exports: module.exports, console, Blob,
    getComputedStyle: () => ({ getPropertyValue: () => '#64748b' }),
    window: { addEventListener() {}, removeEventListener() {} },
    URL: { createObjectURL: () => 'fixture-download', revokeObjectURL() {} },
    document: { documentElement: {}, createElement: () => ({ click() { downloads.push({ href: this.href, name: this.download }) } }) },
    require: name => { if (!(name in dependencies)) throw Error(`Unexpected dependency ${name}`); return dependencies[name] },
  }))
  return { state: module.exports.default.setup({}, { expose() {} }), hooks, errors, charts, downloads }
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
function statistics(day = '2026-10-09') {
  return { schema_version: 'agriculture-v1', start_date: day, end_date: day, generated_at: `${day}T00:00:00Z`,
    overview: { total_farms: 0, total_fields: 0, active_crop_plantings: 0, total_records: 0, total_conversations: 0, current_knowledge_items: 0 },
    categories: { knowledge: {}, active_crops: {}, decisions: {}, farming_records: {} }, daily: [], top_questions: [] }
}
function performance(rate = 0) {
  return { summary: { uptime_seconds: 1, uptime_formatted: '1秒', total_requests: 1,
    overall_success_rate: rate, start_time: '2026-10-09T00:00:00Z', cleared_at: null,
    storage_scope: 'current_process', metric_scope: 'agricultural_http_requests' },
    metrics: { agri_decision_request: { name: 'agri_decision_request', count: 1, total_count: 1,
      success_rate: rate, avg_duration: 0.1, min_duration: 0.1, max_duration: 0.1,
      p50_duration: 0.1, p95_duration: 0.1, p99_duration: 0.1 } } }
}
test('statistics preserves zero rates and distinguishes absent latency', () => {
  const { state } = harness('Statistics', {})
  assert.equal(state.formatRate(0), '0.0%')
  assert.equal(state.formatLatency(0), '0.00 ms')
  assert.equal(state.formatLatency(null), '无有效样本')
})
test('late date range statistics cannot replace the latest selection', async () => {
  const requests = []
  const { state } = harness('Statistics', { getAgriculturalStatistics: () => {
    const waiting = deferred(); requests.push(waiting); return waiting.promise
  } })
  state.dateRange.value = ['2026-10-08', '2026-10-08']
  const old = state.loadStatistics()
  state.dateRange.value = ['2026-10-09', '2026-10-09']
  const current = state.loadStatistics()
  requests[1].resolve({ data: statistics('2026-10-09') }); await current
  requests[0].resolve({ data: statistics('2026-10-08') }); await old
  assert.equal(state.snapshot.value.start_date, '2026-10-09')
  assert.equal(state.dateRange.value[0], '2026-10-09')
})
test('a statistics read failure clears prior data rather than presenting fake zeros', async () => {
  const { state } = harness('Statistics', { getAgriculturalStatistics: async () => { throw Error('controlled failure') } })
  state.snapshot.value = statistics()
  await state.loadStatistics()
  assert.equal(state.snapshot.value, null)
  assert.equal(state.failed.value, true)
  assert.equal(state.assets.value.length, 0)
})
test('unmount prevents a pending statistics response from initializing charts', async () => {
  const waiting = deferred()
  const { state, hooks, charts } = harness('Statistics', { getAgriculturalStatistics: () => waiting.promise })
  state.trendRef.value = {}
  const pending = state.loadStatistics()
  hooks.dispose.forEach(fn => fn())
  waiting.resolve({ data: statistics() }); await pending
  assert.equal(state.snapshot.value, null)
  assert.equal(charts.length, 0)
})
test('export captures its selected range while another refresh clears the screen', async () => {
  const waiting = deferred(), requests = []
  const { state, downloads } = harness('Statistics', { exportStatisticsReport: params => {
    requests.push(params); return waiting.promise
  } })
  state.snapshot.value = statistics('2026-10-08')
  const exporting = state.exportReport()
  state.snapshot.value = null
  waiting.resolve({ data: { content: 'actual report' } }); await exporting
  assert.equal(requests[0].start_date, '2026-10-08')
  assert.equal(downloads[0].name, 'agriculture-statistics-2026-10-08-2026-10-08.md')
})
test('performance never substitutes 100 percent for an actual zero', async () => {
  const { state } = harness('Performance', { getPerformanceMetrics: async () => ({ data: performance(0) }) })
  await state.loadPerformanceData()
  assert.equal(state.summary.value.overallSuccessRate, 0)
  assert.equal(state.formatPercent(state.summary.value.overallSuccessRate), '0.0%')
  assert.equal(state.summary.value.startTime, '2026-10-09T00:00:00Z')
})
test('performance renders absent samples separately and disposes charts when emptied', async () => {
  let response = performance(0)
  const { state, charts } = harness('Performance', { getPerformanceMetrics: async () => ({ data: response }) })
  state.barChartRef.value = {}; state.successChartRef.value = {}
  await state.loadPerformanceData()
  assert.equal(charts.length, 2)
  response = { ...performance(null), metrics: {} }
  response.summary.total_requests = 0
  await state.loadPerformanceData()
  assert.equal(state.formatPercent(state.summary.value.overallSuccessRate), '无样本')
  assert.equal(state.hasData.value, false)
  assert.ok(charts.every(chart => chart.disposed))
})
test('a failed performance read clears old values and retains a failure indicator', async () => {
  let failed = false
  const { state } = harness('Performance', { getPerformanceMetrics: async () => {
    if (failed) throw Error('controlled failure')
    return { data: performance(1) }
  } })
  await state.loadPerformanceData()
  failed = true; await state.loadPerformanceData()
  assert.equal(state.summary.value, null)
  assert.equal(state.loadFailed.value, true)
  assert.equal(state.hasData.value, false)
})
test('late performance response after unmount does not update or recreate charts', async () => {
  const waiting = deferred()
  const { state, hooks, charts } = harness('Performance', { getPerformanceMetrics: () => waiting.promise })
  state.barChartRef.value = {}; state.successChartRef.value = {}
  const pending = state.loadPerformanceData()
  hooks.dispose.forEach(fn => fn())
  waiting.resolve({ data: performance(1) }); await pending
  assert.equal(state.summary.value, null)
  assert.equal(charts.length, 0)
})

test('performance keeps HTTP completion and model degradation separate', async () => {
  const data = performance(1)
  data.summary.degraded_requests = 1
  data.summary.process_id = 1234
  data.metrics.agri_decision_request.outcome_counts = { degraded: 1 }
  data.metrics.agri_decision_request.total_outcome_counts = { success: 9, degraded: 1 }
  const { state } = harness('Performance', { getPerformanceMetrics: async () => ({ data }) })
  await state.loadPerformanceData()
  assert.equal(state.summary.value.overallSuccessRate, 1)
  assert.equal(state.summary.value.degradedRequests, 1)
  assert.equal(state.summary.value.processId, 1234)
  assert.equal(state.formatOutcomes(state.metrics.value.agri_decision_request.outcomeCounts), '降级 1')
  assert.equal(state.formatOutcomes({ success: 0, cancelled: 2 }), '完成 0；取消 2')
  assert.equal(state.formatOutcomes({}), '无分类样本')
  assert.equal(state.getMetricLabel('agri_database_statement'), '农业请求数据库执行')
})

test('performance does not manufacture zero degradation or a process id when fields are missing', async () => {
  const { state } = harness('Performance', { getPerformanceMetrics: async () => ({ data: performance(1) }) })
  await state.loadPerformanceData()
  assert.equal(state.summary.value.degradedRequests, null)
  assert.equal(state.summary.value.processId, null)
})
