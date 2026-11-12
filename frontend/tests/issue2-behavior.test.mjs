import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
import { sharedModule } from './shared-modules.mjs'
const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript'), vue = require('vue')
function setup(file, props = {}, api = {}, onEmit = () => {}, dependencies = {}) {
  const { descriptor } = parse(readFileSync(new URL(file, import.meta.url), 'utf8'))
  const compiled = ts.transpileModule(compileScript(descriptor, { id: 'issue2' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const module = { exports: {} }, disposers = []
  new Script(compiled).runInContext(createContext({ module, exports: module.exports, console, URL: dependencies.URL, require: name => {
    if (name in dependencies) return dependencies[name]
    const shared = sharedModule(name); if (shared) return shared
    if (name === 'pinia') return { storeToRefs: vue.toRefs }
    if (name === '@/stores/farmSelection') return { useFarmSelectionStore: () => vue.reactive({ farms: [], selectedFarmId: null }) }
    if (name === '@/stores/auth') return { useAuthStore: () => ({ user: { id: 1, role: 'customer' } }) }
    if (name === 'vue') return { ...vue, onMounted() {}, onBeforeUnmount: fn => disposers.push(fn) }
    if (name === 'element-plus') return { ElMessage: { error() {}, warning() {}, success() {} } }
    if (name === '@element-plus/icons-vue') return {}
    if (name === '@/api/agriculture') return api
    if (name === '@/utils/businessDate') return { businessDate: () => '2026-10-14' }
    if (name === '@/utils/farmingDraft') return { readFarmingDraft: () => null, saveFarmingDraft: () => true, clearFarmingDraft() {} }
    if (name === '@/utils/scopedDraft') return { draftOwnerKey: user => user ? String(user.id) : undefined, saveScopedDraft: () => false, readScopedDraft: () => null }
    if (name === '@/utils/requestId') return { newRequestId: () => '00000000-0000-4000-8000-000000000001' }
    if (name.startsWith('@/components/') || name === '@/api/agriHistory' || name.startsWith('@/utils/')) return {}
    throw Error(name)
  } }))
  return { state: module.exports.default.setup(props, { expose() {}, emit: onEmit }), dispose: () => disposers.forEach(fn => fn()) }
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
function location(api = {}) {
  const props = vue.reactive({ modelValue: { name: 'farm', area: 1, province: '甲省', city: '甲市', district: '甲县', latitude: 12, longitude: 30 } })
  const result = setup('../src/components/FarmLocationForm.vue', props, api, (name, value) => { if (name === 'update:modelValue') props.modelValue = value })
  return { ...result, props }
}

test('province and city changes clear dependent selection and previous coordinates', () => {
  const { state, props } = location()
  state.changeProvince('乙省')
  assert.equal(props.modelValue.city, ''); assert.equal(props.modelValue.district, '')
  assert.equal(props.modelValue.latitude, undefined)
  state.changeCity('乙市'); assert.equal(props.modelValue.district, '')
})

test('a unique matching region binds reference coordinates including zero', async () => {
  const { state, props } = location({ lookupRegion: async () => ({ data: { items: [{ name: '甲县', province: '甲省', city: '甲市', latitude: 0, longitude: 0, label: '甲省 / 甲市 / 甲县' }] } }) })
  await state.changeDistrict('甲县')
  assert.equal(props.modelValue.latitude, 0); assert.equal(props.modelValue.longitude, 0)
  assert.equal(props.modelValue.location_source, 'region_reference')
})

test('ambiguous places or another city are never automatically bound', async () => {
  const candidate = { name: '甲县', province: '甲省', city: '乙市', latitude: 1, longitude: 2, label: '乙市 / 甲县' }
  const { state, props } = location({ lookupRegion: async () => ({ data: { items: [candidate] } }) })
  await state.changeDistrict('甲县')
  assert.equal(props.modelValue.latitude, undefined)
  assert.ok(state.error.value)
  assert.equal(state.candidates.value.length, 0)
})

test('changing district queries the new code before parent v-model props flush', async () => {
  const props = vue.reactive({ modelValue: { name: 'farm', area: 1, province: '山东省', city: '潍坊市', district: '青州市', region_code: '370781' } })
  const calls = [], updates = []
  const point = { name: '寿光市', province: '山东省', city: '潍坊市', region_code: '370783', latitude: 36.88, longitude: 118.79, label: '山东省 / 潍坊市 / 寿光市' }
  const { state } = setup('../src/components/FarmLocationForm.vue', props, {
    lookupRegion: async (name, context) => { calls.push({ name, context }); return { data: { items: [point] } } }
  }, (_, value) => updates.push(value))
  state.regions.value = [{ code: '37', name: '山东省', children: [{ code: '3707', name: '潍坊市', children: [{ code: '370783', name: '寿光市' }] }] }]
  await state.changeDistrict('寿光市')
  assert.equal(calls[0].name, '寿光市')
  assert.equal(calls[0].context.region_code, '370783')
  assert.equal(updates.at(-1).district, '寿光市')
  assert.equal(updates.at(-1).region_code, '370783')
  assert.equal(updates.at(-1).latitude, 36.88)
})

test('municipality legacy city text binds canonical district reference and code', async () => {
  const point = { name: '朝阳区', province: '北京市', city: '市辖区', region_code: '110105', latitude: 39.92, longitude: 116.44, label: '北京市 / 市辖区 / 朝阳区' }
  const { state, props } = location({ lookupRegion: async () => ({ data: { items: [point] } }) })
  props.modelValue = { name: 'farm', area: 1, province: '北京', city: '北京', district: '朝阳区' }
  await state.search()
  assert.equal(props.modelValue.region_code, '110105')
  assert.equal(props.modelValue.province, '北京市')
  assert.equal(props.modelValue.city, '市辖区')
})

test('late lookup cannot overwrite coordinates after changing the province or manual coordinates', async () => {
  const pending = deferred()
  const { state, props } = location({ lookupRegion: () => pending.promise })
  const request = state.changeDistrict('甲县')
  state.changeProvince('乙省')
  state.coordinate('latitude', 50); state.coordinate('longitude', 60)
  pending.resolve({ data: { items: [{ name: '甲县', province: '甲省', city: '甲市', latitude: 1, longitude: 2, label: '旧位置' }] } })
  await request
  assert.equal(props.modelValue.latitude, 50); assert.equal(props.modelValue.longitude, 60)
  assert.equal(props.modelValue.location_source, 'manual')
})

test('manual region mode clears the old code and complete coordinate pair', () => {
  const { state, props } = location()
  props.modelValue.region_code = 'old'
  state.manualChanged()
  assert.equal(props.modelValue.region_code, undefined); assert.equal(props.modelValue.province, '')
  assert.equal(props.modelValue.latitude, undefined); assert.equal(props.modelValue.longitude, undefined)
})

test('place lookup failure leaves a truthful unbound state', async () => {
  const { state, props } = location({ lookupRegion: async () => { throw Error('offline') } })
  await state.changeDistrict('甲县')
  assert.equal(props.modelValue.latitude, undefined); assert.match(state.error.value, /实际经纬度/)
})

test('crop templates follow current crop and clicking only fills a draft', () => {
  const { state } = setup('../src/views/AgriChat.vue')
  state.activeCrop.value = { crop: '黄瓜', growth_stage: '结果期' }
  assert.ok(state.quickPrompts.value.every(text => text.includes('黄瓜')))
  assert.ok(state.quickPrompts.value.some(text => text.includes('结果期')))
  state.applyPrompt(state.quickPrompts.value[0])
  assert.equal(state.messages.value.length, 0)
  assert.equal(state.inputQuery.value, state.quickPrompts.value[0])
  state.activeCrop.value = null
  assert.ok(state.quickPrompts.value.every(text => !text.includes('黄瓜')))
})

test('selecting crop batch changes the prompt context and decision request', async () => {
  const sent=[]
  const { state }=setup('../src/views/AgriChat.vue', {}, { queryAgriAgentDecision: async payload => { sent.push(payload); return { data: { conclusion: 'controlled reply' } } } }, () => {}, {
    '@/utils/requestId': { newRequestId: () => '00000000-0000-4000-8000-000000000001' }
  })
  state.selectedFieldId.value=1
  state.activeCrops.value=[{ id:1, crop:'番茄' }, { id:2, crop:'黄瓜' }]
  state.selectPlanting(2)
  state.inputQuery.value=state.quickPrompts.value[0]
  await state.handleSendMessage()
  assert.equal(sent[0].crop_planting_id, 2)
  assert.match(sent[0].query, /黄瓜/)
})

test('authenticated image ignores an old response and releases current image on logout', async () => {
  const props=vue.reactive({ src:'/api/diagnosis/media/'+'1'.repeat(32)+'.png' })
  const auth=vue.reactive({ user: { id:1, role:'expert' }, sessionVersion:1 })
  const pending=[], revoked=[]
  const { state, dispose }=setup('../src/components/AuthenticatedImage.vue', props, {}, () => {}, {
    '@/stores/auth': { useAuthStore: () => auth },
    '@/api/diagnosisMedia': { readDiagnosisImage: () => { const request=deferred(); pending.push(request); return request.promise } },
    URL: { createObjectURL: () => 'blob:current', revokeObjectURL: value => revoked.push(value) }
  })
  props.src='/api/diagnosis/media/'+'2'.repeat(32)+'.png'; await vue.nextTick()
  pending[1].resolve({ data: { type:'image/png' } }); await vue.nextTick(); await vue.nextTick()
  assert.equal(state.objectUrl.value, 'blob:current')
  pending[0].resolve({ data: { type:'image/png' } }); await vue.nextTick(); await vue.nextTick()
  assert.equal(state.objectUrl.value, 'blob:current')
  auth.user=null; auth.sessionVersion++; await vue.nextTick()
  assert.equal(state.objectUrl.value, ''); assert.deepEqual(revoked, ['blob:current'])
  dispose()
})

test('expired revocation is hidden even without reloading records', () => {
  const { state }=setup('../src/views/AgriFarms.vue', {}, {}, () => {}, {
    '@/stores/auth': { useAuthStore: () => ({ user: { id:1, role:'customer' } }) }
  })
  const record={ can_delete:true, delete_deadline_at:'2026-10-11T01:10:00Z' }
  state.now.value=Date.parse('2026-10-11T01:09:59Z'); assert.equal(state.canRevoke(record), true)
  state.now.value=Date.parse('2026-10-11T01:10:00Z'); assert.equal(state.canRevoke(record), false)
})

test('an old field-record response cannot clear the new loader or populate another drawer', async () => {
  const requests=new Map()
  const { state }=setup('../src/views/AgriFarms.vue', {}, { getFarmingRecords: id => { const pending=deferred(); requests.set(id, pending); return pending.promise } }, () => {}, {
    '@/stores/auth': { useAuthStore: () => ({ user: { id:1, role:'customer' } }) }
  })
  const old=state.openRecordDrawer({ id:1 })
  const current=state.openRecordDrawer({ id:2 })
  requests.get(1).resolve({ data:[{ id:11 }] }); await old
  assert.equal(state.recordLoading.value, true); assert.equal(state.farmingRecords.value.length, 0)
  requests.get(2).resolve({ data:[{ id:22 }] }); await current
  assert.equal(state.recordLoading.value, false); assert.equal(state.farmingRecords.value[0].id, 22)
})

test('double clicking record save issues only one creation request', async () => {
  const pending=deferred(); let created=0
  const { state }=setup('../src/views/AgriFarms.vue', {}, { createFarmingRecord: () => { created++; return pending.promise } }, () => {}, {
    '@/stores/auth': { useAuthStore: () => ({ user: { id:1, role:'customer' } }) }
  })
  state.newRecordForm.value.operation_content='controlled operation'
  const first=state.submitNewRecord(); await state.submitNewRecord()
  assert.equal(created, 1)
  pending.resolve({ data:{} }); await first
  assert.equal(state.mutationBusy.value, false)
})
