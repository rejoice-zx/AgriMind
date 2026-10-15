import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const vue = require('vue'), ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const { descriptor } = parse(readFileSync(new URL('../src/components/ConsultationReviewEditor.vue', import.meta.url), 'utf8'))
const options = { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }
const code = ts.transpileModule(compileScript(descriptor, { id: 'consultation-review' }).content, options).outputText
const native = value => JSON.parse(JSON.stringify(value))
const context = () => ({ context_fingerprint: 'a'.repeat(64), available_crops: [{ id: 11, crop: 'synthetic', planting_date: '2026-09-01' }],
  snapshot: { crop: { id: 11, crop: 'synthetic' }, records: [{ id: 7, record_type: '病虫害防治', input_name: 'synthetic recorded name', revoked_at: null }] },
  spraying_record_ids: [7], current_review_status: 'no_signed_operation_review', review_window_hours: 24, note: 'synthetic' })
const ticket = () => ({ id: 'synthetic-ticket', status: 'in_review', assigned_to_current_user: true, answer: null })
function harness(overrides = {}, { store = new Map(), initial = ticket(), account = { id: 1, username: 'synthetic', created_at: 'lifetime1' } } = {}) {
  const events = [], messages = [], scope = vue.effectScope(), props = vue.reactive({ ticket: initial }), auth = vue.reactive({ user: account })
  const localStorage = { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key) }
  function helper(path) {
    const module = { exports: {} }
    new Script(ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), options).outputText)
      .runInContext(createContext({ module, exports: module.exports, localStorage }))
    return module.exports
  }
  const draft = helper('../src/utils/scopedDraft.ts'), validation = helper('../src/utils/validationMessage.ts')
  const api = { getAgriConsultation: async () => ({ data: props.ticket }), getConsultationContext: async () => ({ data: context() }),
    resolveAgriConsultation: async () => { throw Error('unexpected submit') }, ...overrides }
  const module = { exports: {} }
  new Script(code).runInContext(createContext({ module, exports: module.exports, crypto: { randomUUID },
    require: name => {
      if (name === 'vue') return { ...vue, onBeforeUnmount() {} }
      if (name === 'element-plus') return { ElMessage: Object.fromEntries(['error','warning','success'].map(level => [level, message => messages.push({ level, message })])) }
      if (name === '@/stores/auth') return { useAuthStore: () => auth }
      if (name === '@/api/agriHistory') return api
      if (name === '@/utils/scopedDraft') return draft
      if (name === '@/utils/validationMessage') return validation
      throw Error(name)
    }
  }))
  const state = scope.run(() => module.exports.default.setup(props, { expose() {}, emit: (name, data) => events.push({ name, data: native(data) }) }))
  return { state, props, auth, store, events, messages, dispose: () => scope.stop() }
}

test('formal review starts without facts, incomplete approval is blocked, refreshed context clears attestations', async () => {
  let posts = 0
  const env = harness({ resolveAgriConsultation: async () => { ++posts } })
  env.state.form.value.answer = 'synthetic'; env.state.form.value.mode = 'harvest'
  assert.ok(Object.values(env.state.form.value.checks).every(value => value === false))
  await env.state.readContext(); env.state.referenceRecords()
  assert.equal(env.state.form.value.products[0].product_name, 'synthetic recorded name')
  assert.equal(env.state.form.value.products[0].registration_number, '')
  assert.equal(env.state.form.value.products[0].phi_days, null)
  env.state.form.value.outcome = 'approved'; await env.state.submit()
  assert.equal(posts, 0); assert.equal(env.state.pending.value, null)
  assert.ok(env.messages.some(item => item.level === 'warning'))
  Object.keys(env.state.form.value.checks).forEach(key => { env.state.form.value.checks[key] = true })
  await env.state.readContext()
  assert.equal(env.state.form.value.outcome, 'needs_information')
  assert.ok(Object.values(env.state.form.value.checks).every(value => value === false))
  env.dispose()
})

test('lost response keeps the original UUID and reconnect reads saved receipt without another signature', async () => {
  let saved = ticket(), posts = 0, gets = 0, submitted
  const env = harness({ getAgriConsultation: async () => { ++gets; return { data: saved } }, resolveAgriConsultation: async (id, body) => {
    ++posts; submitted = native(body); saved = { ...ticket(), status: 'resolved', request_id: body.request_id, workflow_verified: true }
    throw Error('synthetic network disconnect after commit')
  } })
  env.state.form.value.answer = 'synthetic signed answer'
  await env.state.submit()
  assert.equal(posts, 1); assert.equal(env.state.pending.value.request_id, submitted.request_id)
  await env.state.retryOriginal()
  assert.equal(posts, 1); assert.equal(gets, 2); assert.equal(env.state.pending.value, null)
  assert.equal(env.events.filter(item => item.name === 'saved').length, 1)
  assert.equal(env.store.size, 0)
  env.dispose()
})

test('draft restore is explicit, separates account lifetime, and retries the exact original body', async () => {
  const store = new Map(); let original
  const first = harness({ resolveAgriConsultation: async (id, body) => { original = native(body); throw Error('synthetic offline') } }, { store })
  first.state.form.value.answer = 'synthetic offline answer'; await first.state.submit(); first.dispose()
  const second = harness({}, { store })
  assert.equal(second.state.form.value.answer, ''); assert.equal(second.state.pending.value, null)
  assert.ok(second.state.availableDraft.value)
  second.state.restoreDraft()
  assert.deepEqual(native(second.state.pending.value), original)
  second.auth.user = { id: 1, username: 'synthetic', created_at: 'lifetime2' }
  assert.equal(second.state.availableDraft.value, null); assert.equal(second.state.pending.value, null)
  second.dispose()
  let replay
  const third = harness({ resolveAgriConsultation: async (id, body) => {
    replay = native(body); return { data: { ...ticket(), status: 'resolved', request_id: body.request_id } }
  } }, { store })
  third.state.restoreDraft(); await third.state.retryOriginal()
  assert.deepEqual(replay, original); third.dispose()
})

test('read-only or unclaimed consultation cannot submit a new signature', async () => {
  let posts = 0
  const env = harness({ resolveAgriConsultation: async () => { ++posts } }, { initial: { ...ticket(), assigned_to_current_user: false } })
  env.state.form.value.answer = 'synthetic'; await env.state.submit()
  assert.equal(posts, 0); assert.equal(env.state.pending.value, null)
  env.dispose()
})

test('receipt read or authentication rejection preserves the original pending request', async () => {
  let readFails = false, posts = 0
  const env = harness({ getAgriConsultation: async () => {
    if (readFails) throw { response: { status: 401, data: { detail: 'synthetic expired login' } } }
    return { data: ticket() }
  }, resolveAgriConsultation: async () => { ++posts; throw Error('synthetic lost response') } })
  env.state.form.value.answer = 'synthetic'; await env.state.submit()
  const original = native(env.state.pending.value)
  readFails = true; await env.state.retryOriginal()
  assert.deepEqual(native(env.state.pending.value), original); assert.equal(posts, 1)
  env.dispose()
})

test('signature conflict recovers an original receipt that completed after the first read', async () => {
  let signed, gets = 0
  const env = harness({ getAgriConsultation: async () => ({ data: ++gets === 1 ? ticket() : signed }),
    resolveAgriConsultation: async (id, body) => {
      signed = { ...ticket(), status: 'resolved', request_id: body.request_id }
      throw { response: { status: 409, data: { detail: 'synthetic racing commit' } } }
    } })
  env.state.form.value.answer = 'synthetic'; await env.state.submit()
  assert.equal(gets, 2); assert.equal(env.state.pending.value, null)
  assert.equal(env.events.filter(item => item.name === 'saved').length, 1)
  env.dispose()
})
