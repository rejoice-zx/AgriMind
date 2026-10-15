import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const ts = require('typescript'), vue = require('vue')
const { parse, compileScript } = require('@vue/compiler-sfc')
const { descriptor } = parse(readFileSync(new URL('../src/components/FieldEnvironmentPanel.vue', import.meta.url), 'utf8'))
const code = ts.transpileModule(compileScript(descriptor, { id: 'environment-main' }).content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
function harness(api, saved = new Map()) {
  const props = vue.reactive({ field: { id: 21, facility_type: 'greenhouse' } })
  const auth = vue.reactive({ user: { id: 1 } }), events = [], messages = []
  let unmount
  const scope = vue.effectScope(), module = { exports: {} }
  const dependencies = {
    vue: { ...vue, onBeforeUnmount(fn) { unmount = fn } },
    'element-plus': { ElMessage: Object.fromEntries(['error', 'warning', 'success'].map(level => [level, value => messages.push(value)])), ElMessageBox: { confirm: async () => true } },
    '@/stores/auth': { useAuthStore: () => auth }, '@/api/agriculture': { getEnvironmentHistory: async () => ({ data: { items: [], snapshot_id: 0, next_before_id: null } }), ...api },
    '@/utils/requestId': { newRequestId: () => '00000000-0000-4000-8000-000000000001' },
    '@/utils/scopedDraft': {
      draftOwnerKey: user => String(user.id),
      saveScopedDraft(owner, key, payload) { if (payload) saved.set(`${owner}:${key}`, JSON.parse(JSON.stringify(payload))); else saved.delete(`${owner}:${key}`); return true },
      readScopedDraft: (owner, key) => saved.get(`${owner}:${key}`)
    }
  }
  new Script(code).runInContext(createContext({ module, exports: module.exports, Date, console, require: name => dependencies[name] }))
  const state = scope.run(() => module.exports.default.setup(props, { expose() {}, emit: event => events.push(event) }))
  return { props, auth, state, events, messages, saved, dispose() { unmount(); scope.stop() } }
}
test('lost observation response restores the same timestamp and UUID across page reload', async () => {
  const sent = []
  const environment = { facility_type: 'greenhouse', observation_status: 'recent' }
  const api = {
    getFieldEnvironment: async () => ({ data: environment }),
    createEnvironmentObservation: async (id, body) => {
      sent.push({ id, body: JSON.parse(JSON.stringify(body)) })
      if (sent.length === 1) throw Error('synthetic lost response')
      return { data: { replayed: true, observation_id: 5, environment } }
    }
  }
  const first = harness(api)
  await first.state.open()
  first.state.observedAt.value = new Date(Date.now() - 60000)
  Object.assign(first.state.form.value, { measurement_scope: 'inside_facility', humidity: 80 })
  await first.state.submit()
  assert.ok(first.state.pending.value)
  const memory = first.saved
  first.dispose()
  const restored = harness(api, memory)
  await restored.state.open(); restored.state.restoreDraft(); await restored.state.submit()
  assert.equal(sent.length, 2)
  assert.deepEqual(sent[0], sent[1])
  assert.equal(restored.state.pending.value, null)
  assert.equal(memory.size, 0)
  assert.ok(restored.messages.some(message => message.includes('未重复登记')))
  restored.dispose()
})
test('switching account hides old environment and ignores a late read', async () => {
  let resolve
  const env = harness({ getFieldEnvironment: () => new Promise(done => { resolve = done }) })
  const reading = env.state.open()
  env.auth.user.id = 2
  await vue.nextTick()
  resolve({ data: { facility_type: 'greenhouse' } }); await reading
  assert.equal(env.state.visible.value, false)
  assert.equal(env.state.context.value, null)
  env.dispose()
})
