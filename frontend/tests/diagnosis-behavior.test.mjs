// Execute the real Vue setup code with controlled API responses; no DOM or network.
// Run: node --test frontend/tests/diagnosis-behavior.test.mjs
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'

const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript')
const vue = require('vue')
const source = readFileSync(new URL('../src/views/AgriDiagnosis.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'p0-diagnosis-behavior' }).content
const javascript = ts.transpileModule(compiled, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText

function harness(overrides = {}, role = 'customer') {
  const messages = []
  const requests = []
  const api = {
    diagnoseCropImage: async request => {
      requests.push(request)
      return { data: { need_expert: true, disease_record_id: null, ticket_id: null } }
    },
    getFieldsByFarm: async () => ({ data: { items: [] } }),
    getCropPlantings: async () => ({ data: { items: [] } }),
    ...overrides
  }
  const ElMessage = Object.fromEntries(['warning', 'success', 'error', 'info'].map(level => [
    level, text => messages.push({ level, text })
  ]))
  const module = { exports: {} }
  const context = createContext({ module, exports: module.exports, require: name => {
    if (name === 'vue') return { ...vue, onMounted: () => {}, onBeforeUnmount: () => {} }
    if (name === 'pinia') return { storeToRefs: vue.toRefs }
    if (name === '@/stores/farmSelection') return { useFarmSelectionStore: () => vue.reactive({ farms: [], selectedFarmId: null }) }
    if (name === 'element-plus') return { ElMessage, ElMessageBox: {} }
    if (name === '@element-plus/icons-vue') return {}
    if (name === '@/api/agriculture') return api
    if (name === '@/stores/auth') return { useAuthStore: () => ({ user: { id: 1, role } }) }
    if (name === '@/utils/requestId') return { newRequestId: () => String(Math.random()) }
    if (name === '@/utils/scopedDraft') return { draftOwnerKey: user => user ? String(user.id) : undefined, saveScopedDraft: () => false, readScopedDraft: () => null }
    if (name === '@/components/AuthenticatedImage.vue') return {}
    if (name === '@/components/AgriConsultationQueue.vue' || name === '@/components/DiagnosisHistoryPanel.vue') return {}
    throw new Error(`Unexpected runtime dependency: ${name}`)
  } })
  new Script(javascript, { filename: 'AgriDiagnosis.setup.cjs' }).runInContext(context)
  const state = module.exports.default.setup({}, { expose: () => {} })
  return { state, messages, requests }
}

test('unselected field sends no association and never claims a null ticket was created', async () => {
  const { state, messages, requests } = harness()
  state.uploadedImageUrl.value = '/test-leaf.jpg'
  await state.startVisualDiagnosis()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].field_id, undefined)
  assert.equal(state.diagResult.value.ticket_id, null)
  assert.equal(state.diagResult.value.disease_record_id, null)
  assert.equal(state.diagLoading.value, false)
  assert.ok(messages.some(message => message.text.includes('本次未创建专家工单')))
  assert.ok(!messages.some(message => message.text.includes('自动转接')))
})

test('selected field is passed and a real ticket produces the created-ticket message', async () => {
  const requests = []
  const { state, messages } = harness({ diagnoseCropImage: async request => {
    requests.push(request)
    return { data: { need_expert: true, disease_record_id: 42, ticket_id: 73 } }
  } })
  state.uploadedImageUrl.value = '/test-leaf.jpg'
  state.diagFieldId.value = 29
  await state.loadDiagnosisPlantings()
  await state.startVisualDiagnosis()
  assert.equal(requests[0].field_id, 29)
  assert.equal(state.diagResult.value.ticket_id, 73)
  assert.ok(messages.some(message => message.text.includes('自动转接')))
  assert.ok(!messages.some(message => message.text.includes('本次未创建')))
})

test('no image makes no diagnosis request', async () => {
  const { state, requests } = harness()
  await state.startVisualDiagnosis()
  assert.equal(requests.length, 0)
  assert.equal(state.diagLoading.value, false)
})

test('multiple active batches require a deliberate selection in visual diagnosis', async () => {
  const { state, requests, messages } = harness({ getCropPlantings: async () => ({ data: { items: [
    { id: 31, crop: '番茄', planting_date: '2026-01-01' }, { id: 32, crop: '黄瓜', planting_date: '2026-02-01' }
  ] } }) })
  state.diagFieldId.value = 29
  await state.loadDiagnosisPlantings()
  state.uploadedImageUrl.value = '/test-leaf.jpg'
  await state.startVisualDiagnosis()
  assert.equal(requests.length, 0)
  assert.ok(messages.some(message => message.text.includes('作物批次')))
  state.diagPlantingId.value = 32
  await state.startVisualDiagnosis()
  assert.equal(requests[0].field_id, 29)
  assert.equal(requests[0].crop_planting_id, 32)
})

test('diagnosis and expert reply forms contain no prefilled symptoms, pesticide doses or knowledge approval', () => {
  const { state } = harness()
  assert.equal(state.diagSymptoms.value, '')
  assert.equal(state.resolveForm.value.confirmed_disease, '')
  assert.equal(state.resolveForm.value.treatment_suggestion, '')
  assert.equal(state.resolveForm.value.is_knowledge_candidate, 0)
  assert.equal(state.canReview.value, false)
})

test('farm change clears selection and late responses cannot replace the current fields', async () => {
  const pending = new Map()
  const { state } = harness({ getFieldsByFarm: farmId => new Promise(resolve => pending.set(farmId, resolve)) })
  state.diagFieldId.value = 999
  state.diagFarmId.value = 1
  const first = state.changeDiagnosisFarm()
  assert.equal(state.diagFieldId.value, undefined)
  state.diagFarmId.value = 2
  const second = state.changeDiagnosisFarm()
  pending.get(1)({ data: { items: [{ id: 11 }] } })
  await first
  assert.equal(state.diagnosisFields.value.length, 0)
  assert.equal(state.fieldsLoading.value, true)
  pending.get(2)({ data: { items: [{ id: 22 }] } })
  await second
  assert.equal(state.diagnosisFields.value[0].id, 22)
  assert.equal(state.fieldsLoading.value, false)
})

test('clearing a farm rejects its pending field response', async () => {
  let resolveFields
  const { state } = harness({ getFieldsByFarm: () => new Promise(resolve => { resolveFields = resolve }) })
  state.diagFarmId.value = 1
  const loading = state.changeDiagnosisFarm()
  state.diagFarmId.value = undefined
  await state.changeDiagnosisFarm()
  resolveFields({ data: { items: [{ id: 11 }] } })
  await loading
  assert.equal(state.diagnosisFields.value.length, 0)
  assert.equal(state.diagFieldId.value, undefined)
  assert.equal(state.fieldsLoading.value, false)
})

test('a failed diagnosis restores loading state and reports failure without a fabricated result', async () => {
  const { state, messages } = harness({ diagnoseCropImage: async () => { throw new Error('test rejection') } })
  state.uploadedImageUrl.value = '/test-leaf.jpg'
  await state.startVisualDiagnosis()
  assert.equal(state.diagLoading.value, false)
  assert.equal(state.diagResult.value, null)
  assert.ok(messages.some(message => message.level === 'error' && message.text.includes('test rejection')))
})

test('a busy visual diagnosis cannot dispatch a duplicate request', async () => {
  let finish
  const calls = []
  const { state } = harness({ diagnoseCropImage: request => { calls.push(request); return new Promise(resolve => { finish = resolve }) } })
  state.uploadedImageUrl.value = '/real.jpg'
  const first = state.startVisualDiagnosis()
  await state.startVisualDiagnosis()
  assert.equal(calls.length, 1)
  finish({ data: { need_expert: false, ticket_id: null } })
  await first
  assert.equal(state.diagLoading.value, false)
})

test('late ticket details cannot replace the selected record and old form is cleared immediately', async () => {
  const pending = new Map()
  const { state } = harness({ getExpertTicketDetail: id => new Promise(resolve => pending.set(id, resolve)) }, 'expert')
  state.resolveForm.value.confirmed_disease = '上次输入'
  const first = state.selectTicket(10)
  assert.equal(state.resolveForm.value.confirmed_disease, '')
  assert.equal(state.ticketDetail.value, null)
  const second = state.selectTicket(20)
  pending.get(20)({ data: { ticket_id: 20, status: 'in_review', expert_id: 1 } })
  await second
  pending.get(10)({ data: { ticket_id: 10, status: 'in_review', expert_id: 1 } })
  await first
  assert.equal(state.ticketDetail.value.ticket_id, 20)
  assert.equal(state.canSubmit.value, true)
  assert.equal(state.detailLoading.value, false)
})

test('unclaimed, another expert and completed records cannot submit a prescription', async () => {
  let calls = 0
  const { state } = harness({ resolveExpertTicket: async () => { calls++ } }, 'expert')
  state.selectedTicketId.value = 10
  state.resolveForm.value = { confirmed_disease: '复核结论', treatment_suggestion: '补充真实照片再复核', notes: '', is_knowledge_candidate: 0 }
  for (const [status, expert_id] of [['pending_review', null], ['in_review', 2], ['expert_reviewed', 1]]) {
    state.ticketDetail.value = { ticket_id: 10, status, expert_id }
    assert.equal(state.canSubmit.value, false)
    await state.submitExpertResolve()
  }
  assert.equal(calls, 0)
})

test('claim stays on the captured record and blocks duplicate claim or switching during mutation', async () => {
  let finish
  const calls = []
  const { state } = harness({
    claimExpertTicket: id => { calls.push(id); return new Promise(resolve => { finish = resolve }) },
    getExpertTicketDetail: async id => ({ data: { ticket_id: id, status: 'in_review', expert_id: 1, claim_available: false } }),
    getExpertTickets: async () => ({ data: { items: [{ ticket_id: 10 }], total: 1 } })
  }, 'expert')
  state.selectedTicketId.value = 10
  state.ticketDetail.value = { ticket_id: 10, status: 'pending_review', claim_available: true }
  const first = state.handleClaimTicket()
  await state.handleClaimTicket()
  await state.selectTicket(20)
  assert.equal(state.selectedTicketId.value, 10)
  assert.deepEqual(calls, [10])
  finish({ data: {} })
  await first
  assert.equal(state.ticketDetail.value.ticket_id, 10)
  assert.equal(state.canSubmit.value, true)
  assert.equal(state.claiming.value, false)
})

test('unknown model scores and weather do not become zero or default readings', () => {
  const { state } = harness()
  for (const value of [null, undefined, NaN, '0.9', 2]) assert.equal(state.formatScore(value), '未提供有效评分')
  assert.equal(state.formatScore(0), '0%')
  assert.equal(state.weatherValue(null, 'mm'), '未提供')
  assert.equal(state.weatherValue(0, 'mm'), '0mm')
  assert.equal(state.diagCrop.value, '')
})

test('lost response retries the same request ID and an explicitly new diagnosis changes it', async () => {
  const calls = []
  let fail = true
  const { state } = harness({ diagnoseCropImage: async payload => {
    calls.push(payload)
    if (fail) throw Error('response lost')
    return { data: { need_expert: false, request_id: payload.request_id, replayed: true } }
  } })
  state.uploadedImageUrl.value = '/api/diagnosis/media/' + 'a'.repeat(32) + '.png'
  await state.startVisualDiagnosis()
  fail = false
  await state.startVisualDiagnosis()
  assert.equal(calls[0].request_id, calls[1].request_id)
  assert.equal(state.diagResult.value.replayed, true)
  state.newDiagnosis()
  await state.startVisualDiagnosis()
  assert.notEqual(calls[2].request_id, calls[1].request_id)
})

test('status lookup recovers a saved result without a model POST and does not apply an old-field result', async () => {
  let finish
  const requests = []
  const { state } = harness({ diagnoseCropImage: async payload => { requests.push(payload); throw Error('lost') },
    getDiagnosisRequest: () => new Promise(resolve => { finish = resolve }) })
  state.uploadedImageUrl.value = '/api/diagnosis/media/' + 'a'.repeat(32) + '.png'
  state.diagFieldId.value = 1
  await state.loadDiagnosisPlantings()
  await state.startVisualDiagnosis()
  const status = state.queryDiagnosisStatus()
  state.diagFieldId.value = 2
  finish({ data: { status: 'completed', result: { crop: 'old', replayed: true } } }); await status
  assert.equal(requests.length, 1)
  assert.equal(state.diagResult.value, null)
  assert.equal(state.queryingDiagnosis.value, false)
})

test('known model failure shows its cause and only explicit retry creates a new request', async () => {
  const calls = []
  const { state, messages } = harness({ diagnoseCropImage: async payload => {
    calls.push(payload)
    return { data: { diagnosis_status: 'failed', error_code: 'timeout', error_message: '等待模型响应超时',
      request_id: payload.request_id, need_expert: true, ticket_id: null, disease_record_id: null } }
  } })
  state.uploadedImageUrl.value = '/leaf.png'
  await state.startVisualDiagnosis()
  assert.equal(state.diagResult.value.error_code, 'timeout')
  assert.ok(messages.some(message => message.level === 'error' && message.text === '等待模型响应超时'))
  assert.ok(!messages.some(message => message.level === 'success' || message.text.includes('自动转接')))
  const firstId = calls[0].request_id
  await state.startVisualDiagnosis()
  assert.equal(calls[1].request_id, firstId) // recovering never silently repeats inference
  await state.retryFailedDiagnosis()
  assert.equal(calls.length, 3)
  assert.notEqual(calls[2].request_id, firstId)
  assert.equal(calls[2].image_url, '/leaf.png')
})

test('an ambiguous network failure cannot invoke the known-failure retry path', async () => {
  const calls = []
  const { state } = harness({ diagnoseCropImage: async payload => { calls.push(payload); throw Error('lost response') } })
  state.uploadedImageUrl.value = '/leaf.png'
  await state.startVisualDiagnosis()
  await state.retryFailedDiagnosis()
  assert.equal(calls.length, 1)
  assert.equal(state.pendingDiagnosis.value.payload.request_id, calls[0].request_id)
})

test('querying a failed receipt recovers its cause without issuing another diagnosis', async () => {
  const { state, messages } = harness({
    diagnoseCropImage: async () => { throw Error('lost response') },
    getDiagnosisRequest: async id => ({ data: { status: 'failed', result: {
      request_id: id, diagnosis_status: 'failed', error_code: 'http_401', error_message: '视觉模型鉴权失败', replayed: true,
    } } }),
  })
  state.uploadedImageUrl.value = '/leaf.png'
  await state.startVisualDiagnosis()
  await state.queryDiagnosisStatus()
  assert.equal(state.diagResult.value.error_code, 'http_401')
  assert.ok(!messages.some(message => message.level === 'success'))
})

test('admin deletion clears the matching diagnosis and refreshes the expert queue', async () => {
  let refreshes = 0
  const { state } = harness({ getExpertTickets: async () => { refreshes++; return { data: { items: [], total: 0 } } } }, 'admin')
  state.diagResult.value = { disease_record_id: 7, ticket_id: 7 }
  state.pendingDiagnosis.value = { key: 'old', payload: { request_id: 'old-request' } }
  state.selectedTicketId.value = 7
  state.ticketDetail.value = { ticket_id: 7 }
  await state.handleDiagnosisDeleted(7)
  assert.equal(state.diagResult.value, null)
  assert.equal(state.pendingDiagnosis.value, null)
  assert.equal(state.selectedTicketId.value, null)
  assert.equal(state.ticketDetail.value, null)
  assert.equal(refreshes, 1)
})
