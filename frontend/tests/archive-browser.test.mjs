import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const vue = require('vue'), ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
import { sharedModule } from './shared-modules.mjs'
function harness(file, api, memory = new Map()) {
  const { descriptor } = parse(readFileSync(new URL(`../src/${file}.vue`, import.meta.url), 'utf8'))
  const code = ts.transpileModule(compileScript(descriptor, { id: 'archive-main' }).content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText
  const props = vue.reactive({ field: { id: 21, name: 'synthetic', facility_type: 'greenhouse' }, readonly: false })
  const auth = vue.reactive({ user: { id: 1, role: 'customer' } }), events = [], messages = [], hooks = []
  const module = { exports: {} }, scope = vue.effectScope()
  let sequence = 0
  const dependencies = {
    vue: { ...vue, onMounted() {}, onBeforeUnmount(fn) { hooks.push(fn) } },
    '@element-plus/icons-vue': {}, '@/stores/auth': { useAuthStore: () => auth }, '@/components/PageHeader.vue': {},
    '@/api/agriculture': api, '@/api/knowledge': api, '@/api/settings': {},
    'element-plus': { ElMessage: Object.fromEntries(['error','warning','success'].map(level => [level, text => messages.push(text)])), ElMessageBox: { confirm: async () => true, prompt: async () => ({ value: '' }) } },
    '@/utils/requestId': { newRequestId: () => `00000000-0000-4000-8000-${String(++sequence).padStart(12,'0')}` },
    '@/utils/scopedDraft': {
      draftOwnerKey: user => String(user.id),
      saveScopedDraft(owner, key, value) { if (value) memory.set(`${owner}:${key}`, JSON.parse(JSON.stringify(value))); else memory.delete(`${owner}:${key}`); return true },
      readScopedDraft: (owner, key) => memory.get(`${owner}:${key}`)
    }
  }
  new Script(code).runInContext(createContext({ module, exports: module.exports, console, Date, require: name => {
    if (name in dependencies) return dependencies[name]
    const shared = sharedModule(name); if (shared) return shared
    throw Error(name)
  } }))
  const state = scope.run(() => module.exports.default.setup(props, { expose() {}, emit: (...event) => events.push(event) }))
  return { state, props, auth, messages, events, memory, dispose() { hooks.forEach(fn => fn()); scope.stop() } }
}
const page = (items, snapshot_id = 0, next_before_id = null) => ({ data: { items, snapshot_id, next_before_id } })

test('owner reopens with a reason and restores the same request after a lost response', async () => {
  const crop = { id: 7, field_id: 21, crop: '番茄', status: 'harvested' }, sent = []
  const api = {
    getCropHistory: async () => page([{ ...crop }], 7),
    getCropStatusHistory: async () => page([{ id: 8, reason: 'synthetic correction', previous_status: 'harvested', new_status: 'active' }], 8),
    reopenCrop: async (id, body) => {
      sent.push({ id, body: { ...body } }); crop.status = 'active'
      if (sent.length === 1) throw Error('synthetic lost response')
      return { data: { crop: { ...crop }, replayed: true, event_id: 8 } }
    }
  }
  const first = harness('components/FieldCropHistory', api)
  await first.state.open(); await first.state.selectCrop({ ...crop })
  await first.state.reopen(); assert.equal(sent.length, 0)
  first.state.reason.value = 'synthetic correction'; await first.state.reopen()
  assert.ok(first.state.pending.value)
  first.dispose()
  const restored = harness('components/FieldCropHistory', api, first.memory)
  await restored.state.open(); await restored.state.selectCrop({ ...crop })
  restored.state.restoreRequest(); await restored.state.reopen()
  assert.equal(sent.length, 2); assert.deepEqual(sent[0], sent[1])
  assert.equal(restored.state.selected.value.status, 'active')
  assert.equal(restored.state.events.value[0].reason, 'synthetic correction')
  assert.equal(restored.memory.size, 0)
  restored.dispose()
})

test('environment history follows the original snapshot cursor', async () => {
  const calls = []
  const env = harness('components/FieldEnvironmentPanel', {
    getFieldEnvironment: async () => ({ data: {} }),
    getEnvironmentHistory: async (id, params) => {
      calls.push({ id, ...params })
      return calls.length === 1 ? page([{ id: 5 }, { id: 4 }], 5, 4) : page([{ id: 3 }], 5)
    }
  })
  await env.state.open(); await env.state.loadHistory()
  assert.equal(calls[1].snapshot_id, 5); assert.equal(calls[1].before_id, 4)
  assert.deepEqual(Array.from(env.state.historyRows.value, row => row.id), [5, 4, 3])
  env.dispose()
})

test('knowledge filtering passes the same dimensions and ignores an older result', async () => {
  const waiting = [], calls = []
  const env = harness('views/Knowledge', {
    getKnowledgeList: params => { calls.push({ ...params }); return new Promise(resolve => waiting.push(resolve)) }
  })
  env.state.selectedCrop.value = '番茄'; env.state.selectedStage.value = '开花期'
  env.state.selectedRegion.value = '山东潍坊市'; env.state.selectedDisease.value = '灰霉病'
  const old = env.state.loadList(), current = env.state.loadList()
  waiting[1]({ data: { items: [{ id: 'current' }], total: 1 } }); await current
  waiting[0]({ data: { items: [{ id: 'old' }], total: 1 } }); await old
  assert.equal(calls[0].growth_stage, '开花期'); assert.equal(calls[0].region, '山东潍坊市')
  assert.equal(calls[0].disease, '灰霉病'); assert.equal(calls[0].crop, '番茄')
  assert.equal(env.state.list.value[0].id, 'current')
  env.dispose()
})
