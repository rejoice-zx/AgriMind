import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const vue = require('vue'), ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }

function harness(view, api, history = {}, memory = new Map()) {
  const { descriptor } = parse(readFileSync(new URL(`../src/views/${view}.vue`, import.meta.url), 'utf8'))
  const code = ts.transpileModule(compileScript(descriptor, { id: 'analysis-lifecycle' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const module = { exports: {} }, scope = vue.effectScope(), hooks = [], messages = []
  const auth = vue.reactive({ user: { id: 1 } })
  let sequence = 0
  new Script(code).runInContext(createContext({ module, exports: module.exports, console,
    window: { removeEventListener() {}, clearTimeout() {} }, require: name => {
    if (name === 'vue') return { ...vue, onMounted() {}, onBeforeUnmount(fn) { hooks.push(fn) } }
    if (name === 'pinia') return { storeToRefs: vue.toRefs }
    if (name === '@/stores/farmSelection') return { useFarmSelectionStore: () => vue.reactive({ farms: [], selectedFarmId: null }) }
    if (name === '@/stores/auth') return { useAuthStore: () => auth }
    if (name === 'element-plus') return { ElMessage: Object.fromEntries(['error','warning','success','info'].map(level => [level, text => messages.push(text)])), ElMessageBox: {} }
    if (name === '@/api/agriculture') return api
    if (name === '@/api/agriHistory') return history
    if (name === '@/utils/requestId') return { newRequestId: () => `00000000-0000-4000-8000-${String(++sequence).padStart(12,'0')}` }
    if (name === '@/utils/safeMarkdown') return { renderSafeMarkdown: value => value }
    if (name === '@/utils/scopedDraft') return {
      draftOwnerKey: user => String(user.id),
      saveScopedDraft(owner, key, value) { if (value) memory.set(`${owner}:${key}`, JSON.parse(JSON.stringify(value))); else memory.delete(`${owner}:${key}`); return true },
      readScopedDraft: (owner, key) => memory.get(`${owner}:${key}`)
    }
    if (name.startsWith('@/components/') || name === '@element-plus/icons-vue') return {}
    throw Error(name)
  } }))
  const state = scope.run(() => module.exports.default.setup({}, { expose() {} }))
  return { state, auth, messages, memory, dispose() { hooks.forEach(fn => fn()); scope.stop() } }
}
const archive = id => ({ data: { conversation: { id, field_ref: null }, items: [{ id: 'turn', query: 'synthetic', status: 'failed', error: '用户已取消', created_at: '2026-10-14T00:00:00Z' }], total: 1 } })

test('chat restores an original request after lost response and reads receipt without another model submission', async () => {
  let calls = 0, requestId
  const first = harness('AgriChat', { queryAgriAgentDecision: async body => { calls++; requestId = body.request_id; throw Error('synthetic lost response') } })
  first.state.inputQuery.value = 'synthetic'
  await first.state.handleSendMessage(); first.dispose()
  const ids = []
  const restored = harness('AgriChat', {}, {
    getAgentRequest: async id => { ids.push(id); return { data: { status: 'completed', conversation_id: 'owned', message: 'saved' } } },
    getAgriConversation: async id => archive(id)
  }, first.memory)
  await restored.state.restoreChatDraft(); await restored.state.queryOriginalAnalysis()
  assert.deepEqual(ids, [requestId]); assert.equal(calls, 1)
  assert.equal(restored.state.conversationId.value, 'owned'); assert.equal(restored.state.retryRequest.value, null)
  restored.dispose()
})

test('chat cancels by original ID while waiting and ignores the late transport failure', async () => {
  const pending = deferred(), ids = []
  const env = harness('AgriChat', { queryAgriAgentDecision: () => pending.promise }, {
    cancelAgentRequest: async id => { ids.push(id); return { data: { status: 'failed', conversation_id: 'owned', message: '用户已取消' } } },
    getAgriConversation: async id => archive(id)
  })
  env.state.inputQuery.value = 'synthetic'
  const sending = env.state.handleSendMessage(), original = env.state.retryRequest.value.request_id
  await env.state.queryOriginalAnalysis(true)
  pending.reject({ response: { status: 409 } }); await sending
  assert.deepEqual(ids, [original]); assert.equal(env.state.sending.value, false)
  assert.equal(env.state.conversationId.value, 'owned'); assert.equal(env.state.retryRequest.value, null)
  assert.ok(!env.messages.some(text => text.includes('请求未完成')))
  env.dispose()
})

test('diagnosis restores the saved ID and queries a result without submitting again', async () => {
  let calls = 0, requestId
  const first = harness('AgriDiagnosis', { diagnoseCropImage: async body => { calls++; requestId = body.request_id; throw Error('synthetic lost response') } })
  first.state.uploadedImageUrl.value = 'synthetic-local'; await first.state.startVisualDiagnosis(); first.dispose()
  const ids = []
  const restored = harness('AgriDiagnosis', { getDiagnosisRequest: async id => {
    ids.push(id); return { data: { status: 'completed', result: { request_id: id, diagnosis_status: 'completed', need_expert: false }, message: 'saved' } }
  } }, {}, first.memory)
  restored.state.restoreDiagnosisDraft(); await restored.state.queryDiagnosisStatus()
  assert.deepEqual(ids, [requestId]); assert.equal(calls, 1)
  assert.equal(restored.state.diagResult.value.request_id, requestId)
  restored.dispose()
})

test('diagnosis cancellation stays a failed receipt and account changes reject a late result', async () => {
  const pending = deferred(), ids = []
  const env = harness('AgriDiagnosis', {
    diagnoseCropImage: () => pending.promise,
    cancelDiagnosisRequest: async id => { ids.push(id); return { data: { status: 'failed', result: { request_id: id, diagnosis_status: 'failed', error_code: 'cancelled', error_message: '用户已取消' } } } }
  })
  env.state.uploadedImageUrl.value = 'synthetic-local'
  const sending = env.state.startVisualDiagnosis(), original = env.state.pendingDiagnosis.value.payload.request_id
  await env.state.queryDiagnosisStatus(true)
  assert.equal(env.state.diagResult.value.error_code, 'cancelled'); assert.deepEqual(ids, [original])
  env.auth.user = { id: 2 }; env.state.uploadedImageUrl.value = 'synthetic-local'
  pending.resolve({ data: { request_id: original, diagnosis_status: 'completed', suspected_disease: 'late' } }); await sending
  assert.equal(env.state.diagResult.value, null); assert.equal(env.state.diagLoading.value, false)
  env.dispose()
})
