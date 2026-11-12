import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'

const require = createRequire(new URL('../package.json', import.meta.url))
const vue = require('vue'), pinia = require('pinia'), ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
function execute(source, dependencies, globals = {}) {
  const module = { exports: {} }
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022
  } }).outputText
  new Script(code).runInContext(createContext({ module, exports: module.exports, console, ...globals,
    require: name => {
      if (name in dependencies) return dependencies[name]
      if (name.startsWith('@/components/') || name.startsWith('@/utils/') || name === '@/api/agriHistory') return {}
      throw Error(name)
    } }))
  return module.exports
}
const source = readFileSync(new URL('../src/stores/farmSelection.ts', import.meta.url), 'utf8')
function environment({ memory = new Map(), list = [{ id: 1 }, { id: 2 }], storage, clock = Date } = {}) {
  const auth = vue.reactive({ user: { id: 7, role: 'customer' }, sessionVersion: 0 })
  const api = { getFarms: async ({ skip, limit }) => ({ data: { items: list.slice(skip, skip + limit), total: list.length } }) }
  const root = pinia.createPinia()
  const storeModule = execute(source, { vue, pinia, '@/stores/auth': { useAuthStore: () => auth }, '@/api/agriculture': api }, {
    Date: clock,
    localStorage: storage || { getItem: key => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value), removeItem: key => memory.delete(key) }
  })
  const store = storeModule.useFarmSelectionStore(root)
  return { store, auth, api, memory, dispose: () => store.$dispose() }
}
function view(name, env, extraApi = {}, messages = []) {
  const { descriptor } = parse(readFileSync(new URL(`../src/views/${name}.vue`, import.meta.url), 'utf8'))
  const fieldCalls = []
  const api = {
    getFieldsByFarm: async id => { fieldCalls.push(id); return { data: { items: [] } } },
    ...extraApi
  }
  const scope = vue.effectScope()
  const state = scope.run(() => execute(compileScript(descriptor, { id: name }).content, {
    vue: { ...vue, onMounted() {}, onBeforeUnmount() {} }, pinia,
    '@/stores/farmSelection': { useFarmSelectionStore: () => env.store },
    '@/stores/auth': { useAuthStore: () => env.auth }, '@/api/agriculture': api,
    '@/utils/businessDate': { businessDate: () => '2026-10-12' },
    '@/utils/farmingDraft': { readFarmingDraft: () => null, saveFarmingDraft: () => true, clearFarmingDraft() {} },
    '@/utils/scopedDraft': { draftOwnerKey: user => user ? String(user.id) : undefined, saveScopedDraft: () => false, readScopedDraft: () => null },
    '@/utils/requestId': { newRequestId: () => '00000000-0000-4000-8000-000000000001' },
    'vue-router': { useRouter: () => ({}) }, '@element-plus/icons-vue': {},
    'element-plus': { ElMessage: Object.fromEntries(['error', 'warning', 'success', 'info'].map(level => [level, text => messages.push({ level, text })])) }
  }).default.setup({}, { expose() {} }))
  return { state, fieldCalls, dispose: () => scope.stop() }
}

test('selection survives a new store (browser reload) and all five pages load that farm', async () => {
  const memory = new Map()
  const first = environment({ memory })
  await first.store.loadFarms(); first.store.selectedFarmId = 2; first.dispose()
  const env = environment({ memory })
  for (const [name, init] of [['Dashboard', 'initDashboard'], ['AgriFarms', 'fetchFarms'], ['FarmingCalendar', 'initCalendar'], ['AgriChat', 'initData'], ['AgriDiagnosis', 'loadDiagnosisFarms']]) {
    const page = view(name, env)
    await page.state[init]()
    assert.deepEqual(page.fieldCalls, [2], name)
    page.dispose()
  }
  env.dispose()
})

test('mounted agriculture pages follow shared selection and clear old field context', async () => {
  const env = environment()
  const dashboard = view('Dashboard', env), calendar = view('FarmingCalendar', env)
  await dashboard.state.initDashboard(); await calendar.state.initCalendar()
  dashboard.state.selectedFieldId.value = 10
  calendar.state.selectedFieldId.value = 10
  dashboard.state.selectedFarmId.value = 2
  assert.equal(calendar.state.selectedFarmId.value, 2)
  assert.equal(dashboard.state.selectedFieldId.value, null)
  assert.equal(calendar.state.selectedFieldId.value, null)
  assert.deepEqual(dashboard.fieldCalls, [1, 2])
  assert.deepEqual(calendar.fieldCalls, [1, 2])
  dashboard.dispose(); calendar.dispose(); env.dispose()
})

test('saved selection outside first API page is retained; deleted farm and empty list fall back', async () => {
  const list = Array.from({ length: 125 }, (_, i) => ({ id: i + 1 }))
  const env = environment({ list, memory: new Map([['agrimind:selected-farm:7', '125']]) })
  await env.store.loadFarms()
  assert.equal(env.store.selectedFarmId, 125)
  list.pop(); await env.store.loadFarms()
  assert.equal(env.store.selectedFarmId, 1)
  list.length = 0; await env.store.loadFarms()
  assert.equal(env.store.selectedFarmId, null)
  assert.equal(env.memory.has('agrimind:selected-farm:7'), false)
  env.dispose()
})

test('account changes isolate preferences and ignore an old account response', async () => {
  const env = environment()
  await env.store.loadFarms(); env.store.selectedFarmId = 2
  let resolve
  env.api.getFarms = () => new Promise(r => { resolve = r })
  const old = env.store.loadFarms()
  env.auth.sessionVersion++; env.auth.user = { id: 8 }
  assert.equal(env.store.selectedFarmId, null)
  resolve({ data: { items: [{ id: 99 }], total: 1 } }); await old
  assert.equal(env.store.farms.length, 0)
  env.auth.user = { id: 7 }
  assert.equal(env.store.selectedFarmId, 2)
  env.dispose()
})

test('unavailable local storage does not prevent selecting farms', async () => {
  const deny = () => { throw Error('storage disabled') }
  const env = environment({ storage: { getItem: deny, setItem: deny, removeItem: deny } })
  await env.store.loadFarms(); env.store.selectedFarmId = 2
  assert.equal(env.store.selectedFarmId, 2)
  env.dispose()
})

test('short farm cache saves navigation requests but expires and respects refresh and session changes', async () => {
  let now = 1000, calls = 0
  const env = environment({ clock: { now: () => now } })
  const original = env.api.getFarms
  env.api.getFarms = async params => { calls++; return original(params) }
  await env.store.loadFarms(30000)
  await env.store.loadFarms(30000)
  assert.equal(calls, 1)
  now += 30001
  await env.store.loadFarms(30000)
  assert.equal(calls, 2)
  await env.store.loadFarms() // Explicit management-page refresh bypasses cached data.
  assert.equal(calls, 3)
  env.auth.sessionVersion++
  await env.store.loadFarms(30000)
  assert.equal(calls, 4)
  env.dispose()
})

const flush = () => new Promise(resolve => setImmediate(resolve))
for (const [name, init, records] of [['Dashboard', 'initDashboard', 'recentRecords'], ['FarmingCalendar', 'initCalendar', 'farmingRecords']]) {
  test(`${name} shows the page before weather, loads crop and records together, then requests the explicit batch plan`, async () => {
    const env = environment()
    const pending = new Map()
    const page = view(name, env, {
      getFieldsByFarm: async () => ({ data: { items: [{ id: 11 }] } }),
      getCropPlantings: () => new Promise((resolve, reject) => pending.set('crop', { resolve, reject })),
      getFarmingRecords: () => new Promise(resolve => pending.set('records', { resolve })),
      generateAgriPlan: () => new Promise(resolve => pending.set('plan', { resolve })),
    })
    await page.state[init]()
    assert.equal(page.state.loading.value, false)
    assert.equal(page.state.cropLoading.value, true)
    assert.deepEqual([...pending.keys()], ['crop', 'records'])
    pending.get('records').resolve({ data: [{ id: 123 }] }); await flush()
    assert.equal(page.state[records].value[0].id, 123)
    assert.equal(page.state.recordsLoading.value, false)
    pending.get('crop').resolve({ data: { items: [{ id: 21, crop: 'synthetic crop' }] } }); await flush()
    assert.equal(page.state.planLoading.value, true)
    assert.equal(pending.has('plan'), true)
    pending.get('plan').resolve({ data: { field_id: 11, items: [] } }); await flush()
    assert.equal(page.state.currentPlan.value.field_id, 11)
    assert.equal(page.state.planLoading.value, false)
    page.dispose(); env.dispose()
  })

  test(`${name} ignores every late response after switching fields`, async () => {
    const env = environment(), pending = new Map()
    const deferred = kind => id => new Promise(resolve => pending.set(`${kind}:${id}`, resolve))
    const page = view(name, env, {
      getCropPlantings: deferred('crop'), getFarmingRecords: deferred('records'),
      generateAgriPlan: ({ field_id }) => deferred('plan')(field_id),
    })
    const first = page.state.handleFieldChange(1)
    const second = page.state.handleFieldChange(2)
    for (const id of [2, 1]) {
      pending.get(`crop:${id}`)({ data: { items: [{ id: id * 10, crop: `crop-${id}` }] } })
      pending.get(`records:${id}`)({ data: [{ id }] })
      await flush()
      if (id === 2) pending.get(`plan:${id}`)({ data: { field_id: id, items: [] } })
      else assert.equal(pending.has(`plan:${id}`), false)
      await (id === 2 ? second : first)
    }
    assert.equal(page.state.activeCrop.value.crop, 'crop-2')
    assert.equal(page.state[records].value[0].id, 2)
    assert.equal(page.state.currentPlan.value.field_id, 2)
    assert.equal(page.state.planLoading.value, false)
    page.dispose(); env.dispose()
  })
}

test('calendar save that succeeds but fails to refresh never reports a failed save (BUG-026)', async () => {
  const env = environment(); await env.store.loadFarms()
  let created = 0
  const messages = []
  const page = view('FarmingCalendar', env, {
    createFarmingRecord: async () => { created++; return { data: { id: 1, replayed: false } } },
    getFarmingRecords: async () => { throw new Error('network') },
  }, messages)
  const { state } = page
  state.selectedFieldId.value = 5
  state.recordForm.value = { field_id: 5, record_type: 'inspection', operation_date: '2026-10-12', operation_content: '巡视' }
  await state.submitRecord()
  assert.equal(created, 1)
  assert.equal(state.recordSaving.value, false)
  // The dialog closes as for any successful save; a failed list refresh is only a warning.
  assert.equal(state.recordDialogVisible.value, false)
  assert.ok(!messages.some(m => m.level === 'error' && m.text.includes('保存失败')), JSON.stringify(messages))
  assert.ok(messages.some(m => m.level === 'warning' && m.text.includes('已保存')))
  page.dispose(); env.dispose()
})
