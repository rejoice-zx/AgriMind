import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
import { sharedModule } from './shared-modules.mjs'
const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript')
const vue = require('vue')
const { descriptor } = parse(readFileSync(new URL('../src/components/DiagnosisHistoryPanel.vue', import.meta.url), 'utf8'))
const javascript = ts.transpileModule(compileScript(descriptor, { id: 'diagnosis-history' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
const flush = async () => { await vue.nextTick(); await new Promise(resolve => setImmediate(resolve)) }
function harness(overrides = {}, role = 'customer', confirm = async () => {}) {
  const props = vue.reactive({ fieldId: undefined, version: 0 })
  const hooks = [], messages = [], confirmations = [], emitted = []
  const api = { getDiagnosisRecords: async () => ({ data: { total: 0, items: [] } }), ...overrides }
  const module = { exports: {} }
  new Script(javascript).runInContext(createContext({ module, exports: module.exports, require: name => {
    if (name === 'vue') return { ...vue, onBeforeUnmount: fn => hooks.push(fn) }
    if (name === 'element-plus') return { ElMessage: { error: message => messages.push(message), success() {} },
      ElMessageBox: { confirm: (...args) => { confirmations.push(args); return confirm(...args) } } }
    if (name === '@/stores/auth') return { useAuthStore: () => ({ user: { role } }) }
    if (name === '@/api/agriculture') return api
    const shared = sharedModule(name); if (shared) return shared
    throw Error(name)
  } }))
  const scope = vue.effectScope()
  const state = scope.run(() => module.exports.default.setup(props, { expose() {}, emit: (...args) => emitted.push(args) }))
  return { props, state, messages, confirmations, emitted, unmount: () => { hooks.forEach(fn => fn()); scope.stop() } }
}

test('field switches clear old records immediately and reject old list responses', async () => {
  const pending = new Map()
  const env = harness({ getDiagnosisRecords: params => new Promise(resolve => pending.set(params.field_id, resolve)) })
  env.props.fieldId = 1; await vue.nextTick()
  env.state.records.value = [{ ticket_id: 999 }]
  env.state.detail.value = { ticket_id: 999 }
  env.props.fieldId = 2; await vue.nextTick()
  assert.equal(env.state.records.value.length, 0)
  assert.equal(env.state.detail.value, null)
  pending.get(2)({ data: { total: 1, items: [{ ticket_id: 22 }] } }); await flush()
  pending.get(1)({ data: { total: 9, items: [{ ticket_id: 11 }] } }); await flush()
  assert.equal(env.state.records.value[0].ticket_id, 22)
  assert.equal(env.state.total.value, 1)
  env.unmount()
})

test('detail selection preserves saved feedback without inventing a reviewer or certification', async () => {
  const pending = new Map()
  const env = harness({ getDiagnosisRecord: id => new Promise(resolve => pending.set(id, resolve)) })
  const first = env.state.select(1), second = env.state.select(2)
  pending.get(2)({ data: { ticket_id: 2, feedback: { treatment_suggestion: '历史处置', workflow_verified: false, expert_name: null } } })
  await second
  pending.get(1)({ data: { ticket_id: 1 } }); await first
  assert.equal(env.state.detail.value.ticket_id, 2)
  assert.equal(env.state.detail.value.feedback.workflow_verified, false)
  assert.equal(env.state.detail.value.feedback.expert_name, null)
  assert.equal(env.state.detail.value.feedback.treatment_suggestion, '历史处置')
  env.unmount()
})

test('busy manual transfer dispatches once and a field change prevents reopening the previous detail', async () => {
  let finish
  const calls = [], detailCalls = []
  const env = harness({
    requestDiagnosisReview: id => { calls.push(id); return new Promise(resolve => { finish = resolve }) },
    getDiagnosisRecord: async id => { detailCalls.push(id); return { data: { ticket_id: id } } }
  })
  env.state.detail.value = { ticket_id: 10, status: 'preliminary' }
  const first = env.state.transfer()
  await env.state.transfer()
  await env.state.select(20)
  assert.deepEqual(calls, [10]); assert.equal(detailCalls.length, 0)
  env.props.fieldId = 2; await flush()
  finish({ data: { status: 'pending_review' } }); await first
  assert.equal(env.state.detail.value, null)
  assert.equal(detailCalls.length, 0)
  assert.equal(env.state.transferring.value, false)
  env.unmount()
})

test('failed and unmounted loads cannot leave a fabricated or late success', async () => {
  let fail = false, finish
  const env = harness({ getDiagnosisRecords: async () => {
    if (fail) throw Error('controlled failure')
    return { data: { total: 1, items: [{ ticket_id: 1 }] } }
  }, getDiagnosisRecord: () => new Promise(resolve => { finish = resolve }) })
  await flush(); assert.equal(env.state.total.value, 1)
  fail = true; await env.state.load()
  assert.equal(env.state.total.value, 0)
  assert.equal(env.state.records.value.length, 0)
  assert.ok(env.messages[0].includes('controlled failure'))
  const detail = env.state.select(1)
  env.unmount()
  finish({ data: { ticket_id: 1 } }); await detail
  assert.equal(env.state.detail.value, null)
})

test('farmers and experts cannot invoke deletion or its confirmation', async () => {
  for (const role of ['customer', 'expert', 'cs']) {
    const calls = []
    const env = harness({ deleteDiagnosisRecord: async id => { calls.push(id) } }, role)
    assert.equal(env.state.canDelete.value, false)
    await env.state.removeRecord({ ticket_id: 7 })
    assert.equal(calls.length, 0)
    assert.equal(env.confirmations.length, 0)
    env.unmount()
  }
})

test('cancelled admin confirmation leaves the record intact', async () => {
  const calls = []
  const env = harness({ deleteDiagnosisRecord: async id => { calls.push(id) } }, 'admin', async () => { throw 'cancel' })
  await flush()
  env.state.records.value = [{ ticket_id: 7 }]
  await env.state.removeRecord({ ticket_id: 7, initial_suspected_disease: '待鉴别' })
  assert.equal(calls.length, 0)
  assert.equal(env.state.records.value.length, 1)
  assert.equal(env.state.deletingId.value, null)
  assert.equal(env.messages.length, 0)
  assert.ok(env.confirmations[0][0].includes('专家反馈'))
  env.unmount()
})

test('confirmed deletion dispatches once, clears old details and moves off an empty last page', async () => {
  let finish, removed = false
  const calls = [], offsets = []
  const row = { ticket_id: 21, initial_suspected_disease: '待鉴别' }
  const env = harness({
    getDiagnosisRecords: async params => {
      offsets.push(params.offset)
      return { data: { total: removed ? 20 : 21, items: params.offset === 20 ? (removed ? [] : [row]) : [{ ticket_id: 1 }] } }
    },
    deleteDiagnosisRecord: id => { calls.push(id); return new Promise(resolve => { finish = () => { removed = true; resolve() } }) },
  }, 'admin')
  await flush()
  env.state.page.value = 2; await env.state.load()
  env.state.detail.value = row
  const pending = env.state.removeRecord(row); await flush()
  await env.state.removeRecord(row)
  await env.state.select(1)
  assert.deepEqual(calls, [21])
  finish(); await pending
  assert.equal(env.state.detail.value, null)
  assert.equal(env.state.page.value, 1)
  assert.equal(env.state.total.value, 20)
  assert.equal(env.state.records.value[0].ticket_id, 1)
  assert.deepEqual(env.emitted, [['deleted', 21]])
  assert.equal(env.state.deletingId.value, null)
  env.unmount()
})

test('failed deletion preserves record and feedback and reports error', async () => {
  const env = harness({ deleteDiagnosisRecord: async () => { throw Error('request rejected') } }, 'admin')
  await flush()
  const row = { ticket_id: 7, feedback: { treatment_suggestion: '既有反馈' } }
  env.state.records.value = [row]; env.state.detail.value = row
  await env.state.removeRecord(row)
  assert.equal(env.state.detail.value.feedback.treatment_suggestion, '既有反馈')
  assert.equal(env.state.records.value.length, 1)
  assert.equal(env.emitted.length, 0)
  assert.ok(env.messages[0].includes('request rejected'))
  env.unmount()
})

test('a detail response started before deletion cannot restore removed feedback', async () => {
  let finishDetail
  const env = harness({
    deleteDiagnosisRecord: async () => {},
    getDiagnosisRecord: () => new Promise(resolve => { finishDetail = resolve }),
  }, 'admin')
  const pending = env.state.select(7)
  await env.state.removeRecord({ ticket_id: 7, initial_suspected_disease: '待鉴别' })
  finishDetail({ data: { ticket_id: 7, feedback: { treatment_suggestion: '已删除' } } }); await pending
  assert.equal(env.state.detail.value, null)
  env.unmount()
})
