import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'

const require = createRequire(new URL('../package.json', import.meta.url))
const ts = require('typescript'), vue = require('vue')
const { parse, compileScript } = require('@vue/compiler-sfc')
function execute(source, dependencies, globals = {}) {
  const module = { exports: {} }
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  new Script(code).runInContext(createContext({ module, exports: module.exports, console, ...globals,
    require: name => {
      if (name in dependencies) return dependencies[name]
      if (name.startsWith('@/components/')) return {}
      throw Error(name)
    }
  }))
  return module.exports
}
const phone = execute(readFileSync(new URL('../src/utils/phone.ts', import.meta.url), 'utf8'), {})
const confirmation = execute(readFileSync(new URL('../src/utils/planConfirmation.ts', import.meta.url), 'utf8'), {
  '@/utils/businessDate': { businessDate: () => '2026-10-12' }
})
function view(name, api = {}, userApi = {}, globals = {}) {
  let requestSequence = 0
  const { descriptor } = parse(readFileSync(new URL(`../src/views/${name}.vue`, import.meta.url), 'utf8'))
  const messages = [], routes = [], sessions = [], removals = [], mounted = [], unmounted = []
  const dependencies = {
    vue: { ...vue, onMounted(fn) { mounted.push(fn) }, onBeforeUnmount(fn) { unmounted.push(fn) } },
    pinia: { storeToRefs: vue.toRefs },
    '@/stores/farmSelection': { useFarmSelectionStore: () => vue.reactive({ farms: [], selectedFarmId: 1 }) },
    '@/stores/auth': { useAuthStore: () => ({ user: { id: 1, role: 'admin' }, setAuth: (...values) => sessions.push(values) }) },
    'vue-router': { useRouter: () => ({ push: path => routes.push(path) }), useRoute: () => ({ query: {} }) },
    '@/router': { getFirstAccessibleRoute: () => '/agri-chat' },
    'element-plus': { ElMessage: Object.fromEntries(['error', 'warning', 'success'].map(level => [level, value => messages.push({ level, value })])),
      ElMessageBox: { confirm: globals.confirm || (async () => true), prompt: async () => ({ value: '日期录错' }) } },
    '@element-plus/icons-vue': {}, '@/api/auth': api, '@/api/agriculture': api, '@/api/users': userApi,
    '@/utils/phone': phone, '@/utils/planConfirmation': confirmation,
    '@/utils/businessDate': { businessDate: () => '2026-10-12' },
    '@/utils/farmingDraft': { readFarmingDraft: () => null, saveFarmingDraft: () => true, clearFarmingDraft() {} },
    '@/utils/scopedDraft': { draftOwnerKey: user => user ? String(user.id) : undefined, saveScopedDraft: () => false, readScopedDraft: () => null },
    '@/utils/requestId': { newRequestId: () => `00000000-0000-4000-8000-${String(++requestSequence).padStart(12, '0')}` },
    '@/utils/guestIdentity': {
      clearGuestIdentityData: () => removals.push('identity'), getGuestConversationIds: () => ['owned-conversation'],
      getGuestToken: () => 'synthetic-guest-proof', getOrCreateGuestDeviceId: () => 'synthetic-device'
    }
  }
  const state = execute(compileScript(descriptor, { id: name }).content, dependencies, {
    localStorage: { getItem: () => 'synthetic-visitor', removeItem: key => removals.push(key) }, ...globals
  }).default.setup({}, { expose() {} })
  return { state, messages, routes, sessions, removals, mounted, unmounted }
}
function phoneForm(state, key) {
  const form = key === 'registerFormRef' ? state.registerForm : state.form
  return { validate: () => new Promise((resolve, reject) => {
    phone.phoneRules[1].validator({}, form.phone, error => error ? reject(error) : resolve())
  }), validateField: () => new Promise((resolve, reject) => {
    phone.phoneRules[1].validator({}, form.phone, error => error ? reject(error) : resolve())
  }) }
}

test('expert submission shows pending notice without logging in, navigating, or discarding guest history', async () => {
  const sent = []
  const env = view('Login', { registerCustomer: async body => { sent.push(body); return { data: { status: 'pending', message: '已提交管理员审核' } } } })
  Object.assign(env.state.registerForm, { username: 'expert-test', display_name: '专家', password: 'fictional', confirmPassword: 'fictional', phone: '+86 138-0000-0001', role: 'expert' })
  env.state.registerFormRef.value = phoneForm(env.state, 'registerFormRef')
  await env.state.handleRegister()
  assert.equal(sent[0].role, 'expert'); assert.equal(sent[0].phone, '+8613800000001')
  assert.equal(env.state.registrationNotice.value, '已提交管理员审核')
  assert.equal(env.state.activeTab.value, 'login')
  assert.equal(env.sessions.length, 0); assert.equal(env.routes.length, 0); assert.equal(env.removals.length, 0)
})

test('farmer registration authenticates only after a successful registration response', async () => {
  const env = view('Login', { registerCustomer: async () => ({ data: { access_token: 'synthetic-access', refresh_token: 'synthetic-refresh', user: { id: 7, role: 'customer' } } }) })
  env.state.registerForm.phone = '13800000001'
  env.state.registerFormRef.value = phoneForm(env.state, 'registerFormRef')
  await env.state.handleRegister()
  assert.equal(env.sessions.length, 1); assert.deepEqual(env.routes, ['/'])
  assert.ok(env.removals.includes('identity'))
})

test('registration and admin user creation reject blank contact numbers before sending', async () => {
  let calls = 0
  const login = view('Login', { registerCustomer: async () => { calls++ } })
  login.state.registerFormRef.value = phoneForm(login.state, 'registerFormRef')
  await login.state.handleRegister()
  const admin = view('Users', {}, { createUser: async () => { calls++ } })
  admin.state.formRef.value = phoneForm(admin.state, 'formRef')
  await admin.state.handleSubmit()
  assert.equal(calls, 0)
  assert.equal(phone.validPhone('138-0000-0001'), true)
  assert.equal(phone.validPhone('abc13800000001'), false)
})

test('admin sends the required contact and shows all registration statuses', async () => {
  const sent = []
  const admin = view('Users', {}, {
    createUser: async body => { sent.push(body); return { data: {} } },
    getUsers: async () => ({ data: { items: [], total: 0 } }),
    getRegistrations: async () => ({ data: { items: [{ id: 2, registration_status: 'pending' }, { id: 3, role: 'customer', registration_status: 'approved' }], total: 2 } })
  })
  admin.state.form.phone = '010-12345678'
  admin.state.formRef.value = phoneForm(admin.state, 'formRef')
  await admin.state.handleSubmit(); await admin.state.loadPendingRegistrations()
  assert.equal(sent[0].phone, '01012345678')
  assert.equal(admin.state.pendingList.value.length, 2)
  assert.equal(admin.state.canToggleActive({ registration_status: 'pending' }), false)
  assert.equal(admin.state.canToggleActive({ registration_status: 'rejected' }), false)
})

const item = { date: '2026-10-12', record_type: 'field_management', action: '巡视长势', description: '人工核查',
  crop_planting_id: 11, can_proceed: true, is_spray: false, reason: '天气数据待核实', proof: 'synthetic' }

for (const [name, method, fetchMethod, days] of [
  ['Dashboard', 'handleApplySingleTask', 'fetchAgriPlan', 7],
  ['FarmingCalendar', 'handleApplyTask', 'fetchPlan', 14]
]) {
  const old = () => ({ ...item, proof: `${Math.floor(Date.now() / 1000) - 901}.old-proof` })
  const fresh = () => ({ ...item, proof: `${Math.floor(Date.now() / 1000)}.new-proof` })

  test(`${name} distinguishes entry from explicit refresh and keeps last plan if refresh fails`, async () => {
    const requests = []
    const plan = { field_id: 1, start_date: '2026-10-12', items: [item] }
    const env = view(name, { generateAgriPlan: async body => {
      requests.push(body)
      if (requests.length === 3) throw new Error('network unavailable')
      return { data: plan }
    } })
    env.state.selectedFieldId.value = 1
    await env.state[fetchMethod](1)
    await env.state[fetchMethod](1, undefined, true)
    await env.state[fetchMethod](1, undefined, true)
    assert.equal(requests[0].force_refresh, false)
    assert.equal(requests[1].force_refresh, true)
    assert.equal(requests[0].days, days)
    assert.equal(env.state.currentPlan.value.items[0].action, item.action)
    assert.equal(env.state.planLoading.value, false)
  })

  test(`${name} ignores an older response for the same field`, async () => {
    const pending = []
    const env = view(name, { generateAgriPlan: () => new Promise(resolve => pending.push(resolve)) })
    env.state.selectedFieldId.value = 1
    const first = env.state[fetchMethod](1), second = env.state[fetchMethod](1, undefined, true)
    pending[1]({ data: { field_id: 1, version: 2, items: [] } }); await second
    pending[0]({ data: { field_id: 1, version: 1, items: [] } }); await first
    assert.equal(env.state.currentPlan.value.version, 2)
  })

  test(`${name} renews an expired proof before confirming unchanged facts`, async () => {
    const sent = [], renewals = [], replacement = fresh()
    const env = view(name, {
      generateAgriPlan: async body => { renewals.push(body); return { data: { field_id: 1, items: [replacement] } } },
      applyAgriPlan: async body => { sent.push(body); return { data: { record_ids: [12] } } },
      getFarmingRecords: async () => ({ data: [] })
    })
    env.state.selectedFieldId.value = 1
    env.state.currentPlan.value = { field_id: 1, items: [old()] }
    env.state.planLoading.value = true
    await env.state[method](env.state.currentPlan.value.items[0])
    assert.equal(sent.length, 1)
    assert.equal(sent[0].items[0].proof, replacement.proof)
    assert.equal(renewals[0].crop_planting_id, 11)
    assert.equal(env.state.planLoading.value, false)
    assert.equal(env.state.currentPlan.value.items[0].execution_status, 'completed')
  })

  test(`${name} waits for batch loading and requires a choice for multiple active batches`, async () => {
    let finishCrops
    const requests = []
    const crops = [{ id: 11, crop: 'synthetic-A' }, { id: 12, crop: 'synthetic-B' }]
    const env = view(name, { getCropPlantings: () => new Promise(resolve => { finishCrops = resolve }),
      getFarmingRecords: async () => ({ data: [] }), generateAgriPlan: async body => {
        requests.push(body); return { data: { field_id: 1, crop_planting_id: body.crop_planting_id, items: [] } }
      } })
    const loading = env.state.handleFieldChange(1)
    assert.equal(requests.length, 0)
    finishCrops({ data: { items: crops } }); await loading
    assert.equal(requests.length, 0); assert.equal(env.state.activeCrop.value, null)
    env.state.selectPlanting(12); await Promise.resolve(); await Promise.resolve()
    assert.equal(requests[0].crop_planting_id, 12)
    assert.equal(env.state.activeCrop.value.id, 12)
  })

  test(`${name} loads the sole batch before requesting its plan`, async () => {
    const requests = []
    const env = view(name, { getCropPlantings: async () => ({ data: { items: [{ id: 11 }] } }),
      getFarmingRecords: async () => ({ data: [] }), generateAgriPlan: async body => {
        requests.push(body); return { data: { field_id: 1, items: [] } }
      } })
    await env.state.handleFieldChange(1)
    assert.equal(requests.length, 1); assert.equal(requests[0].crop_planting_id, 11)
  })

  test(`${name} cannot apply or restore a previous batch during proof renewal`, async () => {
    let finishPlan, applied = 0
    const env = view(name, { generateAgriPlan: () => new Promise(resolve => { finishPlan = resolve }), applyAgriPlan: async () => { ++applied } })
    env.state.selectedFieldId.value = 1; env.state.activeCrop.value = { id: 11 }
    const confirming = env.state[method](old())
    env.state.activeCrop.value = { id: 12 }; env.state.currentPlan.value = null
    finishPlan({ data: { field_id: 1, items: [fresh()] } }); await confirming
    assert.equal(applied, 0); assert.equal(env.state.currentPlan.value, null)
  })

  test(`${name} displays changed risks and requires a new click instead of confirming silently`, async () => {
    let applied = 0
    const env = view(name, {
      generateAgriPlan: async () => ({ data: { field_id: 1, items: [{ ...fresh(), reason: '更新后的强风提示' }] } }),
      applyAgriPlan: async () => { applied++ }
    })
    env.state.selectedFieldId.value = 1
    await env.state[method](old())
    assert.equal(applied, 0)
    assert.equal(env.state.currentPlan.value.items[0].reason, '更新后的强风提示')
    assert.ok(env.messages.some(message => /重新确认/.test(message.value)))
  })

  test(`${name} cannot confirm a previous field after proof renewal`, async () => {
    let resolve, applied = 0
    const env = view(name, {
      generateAgriPlan: () => new Promise(r => { resolve = r }),
      applyAgriPlan: async () => { applied++ }
    })
    env.state.selectedFieldId.value = 1
    const pending = env.state[method](old())
    env.state.selectedFieldId.value = 2
    resolve({ data: { field_id: 1, items: [fresh()] } }); await pending
    assert.equal(applied, 0)
    assert.equal(env.state.currentPlan.value, null)
  })

  test(`${name} checks date rollover and removes timers/listeners on unmount`, async () => {
    const timers = [], listeners = new Map(), cleared = [], calls = []
    const document = { hidden: false,
      addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }
    const env = view(name, { generateAgriPlan: async body => {
      calls.push(body); return { data: { field_id: 1, start_date: '2026-10-12', items: [] } }
    } }, {}, { document, setInterval: fn => { timers.push(fn); return 42 }, clearInterval: id => cleared.push(id) })
    // init is independently tested; the minimal store has no loader and exits via its catch.
    env.mounted.forEach(fn => fn())
    env.state.selectedFieldId.value = 1
    env.state.currentPlan.value = { field_id: 1, start_date: '2026-10-11', items: [] }
    timers[0]()
    await Promise.resolve(); await Promise.resolve()
    assert.equal(calls.length, 1)
    timers[0]()
    assert.equal(calls.length, 1)
    env.unmounted.forEach(fn => fn())
    assert.deepEqual(cleared, [42]); assert.equal(listeners.size, 0)
    timers[0](); assert.equal(calls.length, 1)
  })
}

test('today inspection is confirmable while future, past and spray items explain why blocked', () => {
  assert.equal(confirmation.planConfirmationReason(item), '')
  assert.match(confirmation.planConfirmationReason({ ...item, date: '2026-10-13' }), /未来/)
  assert.match(confirmation.planConfirmationReason({ ...item, date: '2026-10-11' }), /历史/)
  assert.match(confirmation.planConfirmationReason({ ...item, is_spray: true }), /专家复核/)
})

test('both plan pages reject duplicate confirmation and ignore records from a previous field', async () => {
  for (const [name, method] of [['Dashboard', 'handleApplySingleTask'], ['FarmingCalendar', 'handleApplyTask']]) {
    let resolve, calls = 0
    const pending = new Promise(r => { resolve = r })
    const env = view(name, {
      applyAgriPlan: async () => { calls++; return pending },
      getFarmingRecords: async () => ({ data: [{ id: 10 }] })
    })
    env.state.selectedFieldId.value = 1
    const first = env.state[method](item)
    await env.state[method](item)
    assert.equal(calls, 1)
    env.state.selectedFieldId.value = 2
    resolve({ data: { message: '已保存' } }); await first
    const records = name === 'Dashboard' ? env.state.recentRecords : env.state.farmingRecords
    assert.equal(records.value.length, 0)
    assert.equal(env.state.applyingPlan.value, false)
  }
})

test('single confirmation immediately completes the displayed task and blocks another click on both pages', async () => {
  for (const [name, method] of [['Dashboard', 'handleApplySingleTask'], ['FarmingCalendar', 'handleApplyTask']]) {
    let calls = 0
    const env = view(name, {
      applyAgriPlan: async () => { calls++; return { data: { record_ids: [10], message: '已保存' } } },
      getFarmingRecords: async () => ({ data: [{ id: 10 }] })
    })
    env.state.selectedFieldId.value = 1
    env.state.currentPlan.value = { field_id: 1, items: [{ ...item }] }
    const task = env.state.currentPlan.value.items[0]
    await env.state[method](task)
    assert.equal(task.execution_status, 'completed')
    assert.equal(task.execution_record_id, 10)
    assert.equal(confirmation.planExecutionLabel(task), '已完成')
    assert.ok(confirmation.planConfirmationReason(task))
    await env.state[method](task)
    assert.equal(calls, 1)
    if (name === 'Dashboard') assert.equal(env.state.confirmableTodayTasks.value.length, 0)
  }
})

test('bulk confirmation excludes completed items and marks only the saved current plan items', async () => {
  const sent = []
  const env = view('Dashboard', {
    applyAgriPlan: async body => { sent.push(body); return { data: { record_ids: [20], message: '已保存' } } },
    getFarmingRecords: async () => ({ data: [] })
  })
  env.state.selectedFieldId.value = 1
  env.state.currentPlan.value = { field_id: 1, items: [
    { ...item, action: '已确认巡视', execution_status: 'completed', execution_record_id: 10 },
    { ...item }, { ...item, date: '2026-10-13' }
  ] }
  await env.state.handleApplyAllTasks()
  assert.equal(sent[0].items.length, 1)
  assert.equal(env.state.currentPlan.value.items[1].execution_status, 'completed')
  assert.equal(env.state.currentPlan.value.items[2].execution_status, undefined)
  assert.equal(env.state.confirmableTodayTasks.value.length, 0)
  await env.state.handleApplyAllTasks()
  assert.equal(sent.length, 1)
})

test('a successful save stays completed even if the history refresh fails', async () => {
  const env = view('Dashboard', {
    applyAgriPlan: async () => ({ data: { record_ids: [10], message: '已保存' } }),
    getFarmingRecords: async () => { throw Error('controlled history failure') }
  })
  env.state.selectedFieldId.value = 1
  env.state.currentPlan.value = { field_id: 1, items: [{ ...item }] }
  await env.state.handleApplySingleTask(env.state.currentPlan.value.items[0])
  assert.equal(env.state.currentPlan.value.items[0].execution_status, 'completed')
  assert.ok(env.messages.some(message => message.level === 'warning'))
  assert.ok(!env.messages.some(message => message.level === 'error'))
})

test('late confirmation does not mark a different field or crop planting as completed', () => {
  const anotherField = { field_id: 2, items: [{ ...item }] }
  confirmation.markPlanItemsCompleted(anotherField, 1, [item], [10])
  assert.equal(anotherField.items[0].execution_status, undefined)
  const anotherPlanting = { field_id: 1, items: [{ ...item, crop_planting_id: 2 }] }
  confirmation.markPlanItemsCompleted(anotherPlanting, 1, [{ ...item, crop_planting_id: 1 }], [10])
  assert.equal(anotherPlanting.items[0].execution_status, undefined)
  assert.match(confirmation.planConfirmationReason({ ...item, execution_status: 'revoked' }), /撤销/)
})

test('revocation switches to an auditable history view and requests revoked rows', async () => {
  const calls = []
  const env = view('AgriFarms', {
    revokeFarmingRecord: async (id, reason) => calls.push({ id, reason }),
    getFarmingRecords: async (id, status) => { calls.push({ id, status }); return { data: [{ id: 8, revoked_at: '2026-10-12T01:00:00Z', revoke_reason: '日期录错' }] } }
  })
  env.state.currentFieldForRecord.value = { id: 1 }
  await env.state.revokeRecord({ id: 8 })
  assert.equal(env.state.recordStatus.value, 'revoked')
  assert.equal(calls[1].status, 'revoked')
  assert.equal(env.state.farmingRecords.value[0].revoke_reason, '日期录错')
})

test('farming retry reuses the original submission id after a lost response', async () => {
  const requests = []
  const env = view('AgriFarms', {
    getFarmingRecords: async () => ({ data: [] }),
    createFarmingRecord: async payload => {
      requests.push({ ...payload })
      if (requests.length === 1) throw Error('synthetic lost response')
      return { data: { id: 71, replayed: true } }
    }
  })
  await env.state.openRecordDrawer({ id: 21, name: 'field', farm_id: 1 })
  env.state.newRecordForm.value.operation_content = 'actual field work'
  await env.state.submitNewRecord()
  const pendingId = env.state.newRecordForm.value.request_id
  assert.ok(pendingId)
  await env.state.submitNewRecord()
  assert.equal(requests.length, 2)
  assert.equal(requests[0].request_id, requests[1].request_id)
  assert.equal(env.state.newRecordForm.value.request_id, undefined)
  assert.ok(env.messages.some(message => message.value.includes('未重复登记')))
})

test('farm archive confirmation cannot target a farm selected while the dialog was open', async () => {
  let confirm, calls = 0
  const env = view('AgriFarms', { setFarmArchive: async () => { calls++ } }, {}, {
    confirm: () => new Promise(resolve => { confirm = resolve })
  })
  env.state.farms.value = [{ id: 1, name: 'first' }, { id: 2, name: 'second' }]
  const pending = env.state.toggleArchive()
  env.state.currentFarmId.value = 2
  confirm(true); await pending
  assert.equal(calls, 0)
  assert.ok(env.messages.some(message => message.value.includes('已切换')))
})

test('field profile edit sends explicit facility type and rejects a switched farm', async () => {
  const requests = []
  const env = view('AgriFarms', {
    updateField: async (id, body) => { requests.push({ id, body: { ...body } }); return { data: {} } },
    getFieldsByFarm: async () => ({ data: { items: [] } })
  })
  env.state.farms.value = [{ id: 1, name: 'first' }, { id: 2, name: 'second' }]
  const field = { id: 21, farm_id: 1, name: 'actual plot', area: 2, facility_type: 'unknown' }
  env.state.openEditField(field)
  env.state.fieldForm.value.facility_type = 'greenhouse'
  await env.state.submitCreateField()
  assert.equal(requests[0].id, 21)
  assert.equal(requests[0].body.facility_type, 'greenhouse')
  env.state.openEditField(field)
  env.state.currentFarmId.value = 2
  await env.state.submitCreateField()
  assert.equal(requests.length, 1)
})

test('historical crop records remain read only and keep the explicit batch filter', async () => {
  const reads = []; let writes = 0
  const env = view('AgriFarms', {
    getFarmingRecords: async (...args) => { reads.push(args); return { data: [] } },
    createFarmingRecord: async () => { writes++; return { data: {} } }
  })
  await env.state.openRecordDrawer({ id: 21, farm_id: 1, name: 'synthetic' }, { id: 7, status: 'harvested', crop: '番茄' })
  assert.deepEqual(reads[0], [21, 'all', 7])
  env.state.newRecordForm.value.operation_content = 'should not submit from history'
  await env.state.submitNewRecord()
  assert.equal(writes, 0); assert.equal(env.state.recordHistoryOnly.value, true)
})
