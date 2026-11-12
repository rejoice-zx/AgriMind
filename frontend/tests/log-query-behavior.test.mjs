import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript'), vue = require('vue')
import { sharedModule } from './shared-modules.mjs'
function harness(api) {
  const { descriptor } = parse(readFileSync(new URL('../src/views/Logs.vue', import.meta.url), 'utf8'))
  const compiled = ts.transpileModule(compileScript(descriptor, { id: 'log-query' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const module = { exports: {} }, hooks = { dispose: [] }, errors = [], success = [], prompts = []
  const dependencies = {
    '@/components/PageHeader.vue': {},
    vue: { ...vue, onMounted() {}, onBeforeUnmount: fn => hooks.dispose.push(fn) },
    'element-plus': { ElMessage: { error: value => errors.push(value), success: value => success.push(value), warning() {} },
      ElMessageBox: { confirm: async () => true, prompt: async (message, title, options) => { prompts.push(options); return { value: '清空' } } } },
    '@element-plus/icons-vue': {}, '@/api/logs': api,
  }
  new Script(compiled).runInContext(createContext({ module, exports: module.exports, console, Blob, URL,
    setTimeout: fn => fn(),
    require: name => { if (name in dependencies) return dependencies[name]; const shared = sharedModule(name); if (shared) return shared; throw Error(`Unexpected dependency ${name}`) },
  }))
  return { state: module.exports.default.setup({}, { expose() {} }), hooks, errors, success, prompts }
}
function result(message, truncated = false) {
  return { data: { items: [{ timestamp: '2026-10-10T00:00:00Z', level: 'INFO', message,
    context: { request_id: 'synthetic-id', field_id: 1 } }], total: 1, truncated,
    legacy_time_entries: 2, unavailable_files: 0, unparsed_lines: 0, query_scope: 'recent_file_tails' } }
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

test('logs send correlation and offset-aware boundaries and retain query limitation metadata', async () => {
  const calls = []
  const { state } = harness({ getLogs: async params => { calls.push(params); return result('current', true) } })
  state.filters.associationType = 'field_id'; state.filters.associationId = '12'
  state.dateRange.value = [new Date('2026-10-10T08:00:00+08:00'), new Date('2026-10-10T09:00:00+08:00')]
  await state.loadLogs()
  assert.equal(calls[0].field_id, '12')
  assert.equal(calls[0].start_time, '2026-10-10T00:00:00.000Z')
  assert.equal(state.queryInfo.value.truncated, true)
  assert.equal(state.queryInfo.value.legacy, 2)
  assert.ok(state.formatContext(state.logList.value[0]).includes('field_id=1'))
})
test('older log query cannot replace latest selection or limitation metadata', async () => {
  const calls = []
  const { state } = harness({ getLogs: () => { const call = deferred(); calls.push(call); return call.promise } })
  const old = state.loadLogs(); const recent = state.loadLogs()
  calls[1].resolve(result('latest', false)); await recent
  calls[0].resolve(result('old', true)); await old
  assert.equal(state.logList.value[0].message, 'latest')
  assert.equal(state.queryInfo.value.truncated, false)
})
test('failed logs query clears stale records and totals rather than presenting them as fresh', async () => {
  let failed = false
  const { state, errors } = harness({ getLogs: async () => {
    if (failed) throw Error('controlled failure')
    return result('old')
  } })
  await state.loadLogs(); failed = true; await state.loadLogs()
  assert.equal(state.logList.value.length, 0); assert.equal(state.total.value, 0)
  assert.equal(state.queryInfo.value, null); assert.equal(state.failed.value, true)
  assert.equal(errors.length, 1)
})
test('leaving logs page suppresses late response', async () => {
  const call = deferred()
  const { state, hooks } = harness({ getLogs: () => call.promise })
  const query = state.loadLogs(); hooks.dispose.forEach(fn => fn())
  call.resolve(result('late')); await query
  assert.equal(state.logList.value.length, 0); assert.equal(state.queryInfo.value, null)
})
test('a failed clear response cannot display a success or refresh history', async () => {
  let queries = 0
  const { state, errors, success } = harness({ clearLogs: async () => ({ data: { success: false } }),
    getLogs: async () => { queries++; return result('synthetic') } })
  await state.handleClear('app')
  assert.equal(errors.length, 1); assert.equal(success.length, 0); assert.equal(queries, 0)
})
test('oversized download explains the limit even when the HTTP error body is a blob', async () => {
  const { state, errors } = harness({ downloadLogs: async () => { throw { response: { status: 413, data: new Blob(['synthetic error']) } } } })
  await state.handleDownload('app')
  assert.equal(errors.length, 1)
  assert.ok(errors[0].includes('8MiB'))
})
test('malformed association ids are rejected before a request is sent', async () => {
  const calls = []
  const { state } = harness({ getLogs: async params => { calls.push(params); return result('x') } })
  state.filters.associationType = 'field_id'; state.filters.associationId = 'abc'
  assert.equal(state.associationError.value, '应为正整数')
  await state.loadLogs(); assert.equal(calls.length, 0)
  state.filters.associationType = 'request_id'; state.filters.associationId = '3f2a6c1e-0000-4000-8000-1234567890ab'
  state.filters.keyword = '  timeout  '
  await state.loadLogs()
  assert.equal(calls.length, 1); assert.equal(calls[0].keyword, 'timeout')
})
test('clicking a context id filters by it and clear requires typing the confirmation word', async () => {
  const calls = []
  const { state, prompts } = harness({ getLogs: async params => { calls.push(params); return result('x') },
    clearLogs: async () => ({ data: { success: true } }) })
  state.filterByContext('field_id', 7)
  await new Promise(r => setImmediate(r))
  assert.equal(calls.at(-1).field_id, '7'); assert.equal(state.currentPage.value, 1)
  await state.handleClear('error')
  assert.equal(prompts[0].inputValidator('随便'), '请输入“清空”')
  assert.equal(prompts[0].inputValidator('清空'), true)
})
test('query limitations are merged into one list of only the conditions that occurred', async () => {
  const { state } = harness({ getLogs: async () => result('x', true) })
  await state.loadLogs()
  assert.equal(state.queryWarnings.value.length, 2)
  assert.ok(state.queryWarnings.value[1].includes('2 条旧格式'))
})
