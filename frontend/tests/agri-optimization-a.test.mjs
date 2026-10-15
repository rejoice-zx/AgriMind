import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript')
const vue = require('vue')
function execute(source, dependencies) {
  const module = { exports: {} }
  const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  new Script(javascript).runInContext(createContext({ module, exports: module.exports, console, require: name => {
    if (name in dependencies) return dependencies[name]
    throw new Error(`Unexpected dependency ${name}`)
  } }))
  return module.exports
}
function chat(overrides = {}, historyOverrides = {}) {
  const source = readFileSync(new URL('../src/views/AgriChat.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const requests = []
  const warnings = []
  let requestSequence = 0
  const api = {
    getFieldsByFarm: async () => ({ data: { items: [] } }),
    getCropPlantings: async () => ({ data: { items: [] } }),
    getFarmingRecords: async () => ({ data: [] }),
    uploadDiagnosisImage: async file => ({ data: { url: '/static/uploads/diagnosis/real.png' } }),
    queryAgriAgentDecision: async request => { requests.push(request); return { data: { conclusion: 'actual reply' } } },
    ...overrides
  }
  const exports = execute(compileScript(descriptor, { id: 'agri-a' }).content, {
    pinia: { storeToRefs: vue.toRefs },
    '@/stores/farmSelection': { useFarmSelectionStore: () => vue.reactive({ farms: [], selectedFarmId: null }) },
    vue: { ...vue, onMounted: () => {}, onBeforeUnmount: () => {} },
    'element-plus': { ElMessage: { error: message => warnings.push(message) } },
    '@element-plus/icons-vue': {}, '@/components/AuthenticatedImage.vue': {}, '@/api/agriculture': api,
    '@/components/FieldWeatherPanel.vue': {}, '@/components/AgriHistoryPanel.vue': {},
    '@/api/agriHistory': historyOverrides, '@/utils/requestId': { newRequestId: () => `00000000-0000-4000-8000-${String(++requestSequence).padStart(12, '0')}` },
    '@/stores/auth': { useAuthStore: () => ({ user: { id: 1 } }) },
    '@/utils/scopedDraft': { draftOwnerKey: user => user ? String(user.id) : undefined, saveScopedDraft: () => false, readScopedDraft: () => null },
    '@/utils/safeMarkdown': { renderSafeMarkdown: value => value }
  })
  return { state: exports.default.setup({}, { expose() {} }), requests, warnings }
}
test('out of order history responses cannot replace the current conversation', async () => {
  const pending = new Map()
  const { state } = chat({}, { getAgriConversation: id => new Promise(resolve => pending.set(id, resolve)) })
  const old = state.openConversation('old')
  const current = state.openConversation('current')
  pending.get('current')({ data: { conversation: { id: 'current', field_ref: 2 }, total: 0, items: [] } })
  await current
  pending.get('old')({ data: { conversation: { id: 'old', field_ref: 1 }, total: 0, items: [] } })
  await old
  assert.equal(state.conversationId.value, 'current')
  assert.equal(state.conversationFieldId.value, 2)
})
test('stored expert feedback is restored and a different field makes the archive read only', async () => {
  const { state, requests } = chat({}, { getAgriConversation: async () => ({ data: {
    conversation: { id: 'owned', field_ref: 5 }, total: 1, items: [{ id: 'turn', query: 'actual question', created_at: '2026-10-09T00:00:00Z', status: 'completed', decision: { conclusion: 'actual AI answer' }, consultation: { id: 'ticket', status: 'resolved', answer: 'actual expert answer' } }]
  } }) })
  state.selectedFieldId.value = 6
  await state.openConversation('owned')
  assert.equal(state.messages.value[1].consultation.answer, 'actual expert answer')
  assert.equal(state.historyReadonly.value, true)
  state.inputQuery.value = 'new question'
  await state.handleSendMessage()
  assert.equal(requests.length, 0)
  assert.equal(state.inputQuery.value, 'new question')
})
test('lost response and pending conflict preserve the same retry identifier', async () => {
  const sent = []
  const { state } = chat({ queryAgriAgentDecision: async payload => {
    sent.push(payload)
    if (sent.length === 1) throw Error('network lost')
    throw { response: { status: 409, data: { detail: 'still pending' } } }
  } })
  state.inputQuery.value = 'actual question'
  await state.handleSendMessage()
  await state.handleSendMessage()
  await state.handleSendMessage()
  assert.equal(new Set(sent.map(row => row.request_id)).size, 1)
  assert.equal(state.retryRequest.value.request_id, sent[0].request_id)
  state.inputQuery.value = 'intentionally changed question'
  await state.handleSendMessage()
  assert.notEqual(sent[3].request_id, sent[0].request_id)
})
test('manual transfer saves a real ticket id and displays returned feedback', async () => {
  const sent = []
  const { state } = chat({}, { transferAgriTurn: async id => {
    sent.push(id)
    return { data: { id: 'real-ticket', status: 'pending' } }
  }, getAgriConsultation: async () => ({ data: { id: 'real-ticket', status: 'resolved', answer: 'verified feedback' } }) })
  const message = { id: 'turn', role: 'agent', decision: { turn_id: 'turn-id' } }
  await state.consult(message)
  assert.deepEqual(sent, ['turn-id'])
  assert.equal(message.decision.consultation_id, 'real-ticket')
  await state.refreshConsultation(message)
  assert.equal(message.consultation.answer, 'verified feedback')
  assert.equal(message.busy, false)
})
test('field switching starts a clean conversation without carrying an old image', async () => {
  const { state } = chat()
  state.conversationId.value = 'old'
  state.inputImageUrl.value = '/old-image.png'
  state.messages.value = [{ id: 'old', role: 'user', content: 'old field' }]
  await state.handleFieldSelect(2)
  assert.equal(state.conversationId.value, undefined)
  assert.equal(state.messages.value.length, 0)
  assert.equal(state.inputImageUrl.value, '')
})
test('empty farm clears old crop, records and selected field', async () => {
  const { state } = chat()
  state.selectedFieldId.value = 10
  state.activeCrop.value = { crop: 'old crop' }
  state.recentRecords.value = [{ id: 1, record_type: 'pest_control' }]
  await state.loadFields(2)
  assert.equal(state.selectedFieldId.value, null)
  assert.equal(state.activeCrop.value, null)
  assert.equal(state.recentRecords.value.length, 0)
})
test('late old field results cannot replace the selected crop and records', async () => {
  const pending = new Map()
  const { state } = chat({ getCropPlantings: id => new Promise(resolve => pending.set(id, resolve)) })
  state.selectedFieldId.value = 1
  const first = state.loadFieldDetail(1)
  state.selectedFieldId.value = 2
  const second = state.loadFieldDetail(2)
  pending.get(2)({ data: { items: [{ crop: 'current' }] } })
  await second
  pending.get(1)({ data: { items: [{ crop: 'stale' }] } })
  await first
  assert.equal(state.activeCrop.value.crop, 'current')
})
test('upload returns the actual uploaded path used in the decision request', async () => {
  const { state, requests } = chat()
  let success
  await state.uploadChatImage({ file: { name: 'leaf.png' }, onSuccess: result => { success = result } })
  state.inputQuery.value = '现场问题'
  await state.handleSendMessage()
  assert.equal(success.url, '/static/uploads/diagnosis/real.png')
  assert.equal(requests[0].image_url, success.url)
})
test('the PHI panel shows spray records rather than all agricultural operations', () => {
  const { state } = chat()
  state.recentRecords.value = [{ id: 1, record_type: 'irrigation' }, { id: 2, record_type: 'pest_control' }]
  assert.deepEqual(Array.from(state.sprayRecords.value, row => row.id), [2])
})
test('a failed agricultural request restores its draft without replacing a newer draft', async () => {
  const { state } = chat({ queryAgriAgentDecision: async () => { throw Error('controlled failure') } })
  state.inputQuery.value = 'original question'
  await state.handleSendMessage()
  assert.equal(state.inputQuery.value, 'original question')
  assert.equal(state.sending.value, false)
})
test('actual route table retires all four pages and gives cs an agricultural landing', () => {
  const source = readFileSync(new URL('../src/router/index.ts', import.meta.url), 'utf8')
  let routes
  const result = execute(source, { 'vue-router': { createWebHistory: () => ({}), createRouter: options => { routes = options.routes; return { beforeEach() {} } } }, '@/stores/auth': { useAuthStore() {} } })
  assert.equal(result.getFirstAccessibleRoute('cs'), '/dashboard')
  for (const path of ['/customer-chat', '/workbench', '/human-service', '/products']) {
    assert.equal(routes.find(route => route.path === path).redirect, '/retired')
    assert.equal(result.getMenuItems('admin').some(item => item.path === path), false)
  }
})
