import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript'), vue = require('vue')
const settings = () => ({ data: { agri_ai: { llm_base_url: 'https://provider.example/v1', llm_model: 'stored-text',
  vision_base_url: 'https://provider.example/v1', vision_model: 'stored-vision', has_llm_key: true, has_vision_key: false } } })
function setup(overrides = {}) {
  const { descriptor } = parse(readFileSync(new URL('../src/views/Settings.vue', import.meta.url), 'utf8'))
  const compiled = ts.transpileModule(compileScript(descriptor, { id: 'model-settings' }).content,
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const module = { exports: {} }, messages = [], calls = [], dispose = []
  const api = { getSettings: async () => settings(), getLLMProviders: async () => ({ data: { providers: [
    { name: 'fixture', display_name: 'Fixture', default_api_url: 'https://provider.example/v1', default_model: 'static-example', supported_models: ['static-example'] },
    { name: 'other', display_name: 'Other', default_api_url: 'https://other.example/v1', default_model: 'static-other', supported_models: [] },
  ] } }), discoverAgriModels: async body => { calls.push(body); return { data: { success: true, models: ['live-model'], message: 'live response', truncated: false } } },
    testAgriModel: async body => { calls.push(body); return { data: { success: true, checks: [], message: 'actual test', authentication: 'verified', vision: 'not_tested' } } },
    updateSettings: async body => { calls.push(body); return settings() }, ...overrides }
  new Script(compiled).runInContext(createContext({ module, exports: module.exports, console, require: name => {
    if (name === '@/components/PageHeader.vue') return {}
    if (name === 'vue') return { ...vue, onMounted() {}, onBeforeUnmount: fn => dispose.push(fn) }
    if (name === 'element-plus') return { ElMessage: Object.fromEntries(['success', 'error', 'warning'].map(level => [level, text => messages.push({ level, text })])) }
    if (name === '@element-plus/icons-vue') return {}
    if (name === '@/api/settings') return api
    throw Error(name)
  } }))
  return { state: module.exports.default.setup({}, { expose() {} }), messages, calls, dispose }
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

test('a preset-list failure does not discard loaded settings or prohibit manual configuration', async () => {
  const { state, messages } = setup({ getLLMProviders: async () => { throw Error('controlled list failure') } })
  await state.loadSettings()
  assert.equal(state.agriAIConfig.llm_model, 'stored-text')
  assert.equal(state.providers.value.length, 0)
  assert.ok(messages.some(value => value.level === 'warning'))
  state.agriAIConfig.llm_model = 'manual-choice'
  await state.runModelTest('decision')
  assert.equal(state.modelTools.decision.result.success, true)
})
test('selecting a new platform clears the typed old key and never assigns a static model as a live choice', async () => {
  const { state } = setup(); await state.loadSettings()
  state.agriAIConfig.llm_api_key = 'synthetic-old-key'
  state.modelTools.decision.provider = 'other'; state.selectModelPlatform('decision')
  assert.equal(state.agriAIConfig.llm_base_url, 'https://other.example/v1')
  assert.equal(state.agriAIConfig.llm_api_key, '')
  assert.equal(state.agriAIConfig.llm_model, '')
  assert.equal(state.canUseSavedKey('decision'), false)
})
test('discovery only displays real returned IDs and failure keeps the handwritten model', async () => {
  let fail = false
  const { state } = setup({ discoverAgriModels: async () => fail
    ? { data: { success: false, models: [], message: 'unsupported; manual entry allowed', truncated: false } }
    : { data: { success: true, models: ['actual-1', 'actual-2'], message: 'live', truncated: true } } })
  await state.loadSettings(); await state.fetchModelList('vision')
  assert.equal(state.modelTools.vision.models.join(','), 'actual-1,actual-2')
  assert.equal(state.modelTools.vision.truncated, true)
  state.agriAIConfig.vision_model = 'handwritten'; fail = true
  await state.fetchModelList('vision')
  assert.equal(state.modelTools.vision.models.length, 0)
  assert.equal(state.agriAIConfig.vision_model, 'handwritten')
})

test('vision filtering uses declarations and candidates, preserves unknown models and never changes selection', async () => {
  const models = ['declared', 'Pro/moonshotai/Kimi-K2.6', 'text-only', 'unknown']
  const model_details = [
    { id: models[0], image_input: 'supported', evidence: 'metadata', vision_candidate: true },
    { id: models[1], image_input: 'unknown', evidence: 'model_name', vision_candidate: true },
    { id: models[2], image_input: 'unsupported', evidence: 'metadata', vision_candidate: false },
    { id: models[3], image_input: 'unknown', evidence: 'unspecified', vision_candidate: false }
  ]
  const { state } = setup({ discoverAgriModels: async () => ({ data: { success: true, models, model_details, truncated: false } }) })
  await state.loadSettings(); state.agriAIConfig.vision_model = 'handwritten'; await state.fetchModelList('vision')
  assert.equal(state.visionModelOptions.value.join(','), 'declared,Pro/moonshotai/Kimi-K2.6')
  assert.match(state.visionModelLabel(models[1]), /待验证/)
  state.visionModelFilter.value = 'declared'
  assert.equal(state.visionModelOptions.value.join(','), 'declared')
  state.visionModelFilter.value = 'all'
  assert.equal(state.visionModelOptions.value.join(','), models.join(','))
  assert.equal(state.agriAIConfig.vision_model, 'handwritten')
  state.agriAIConfig.vision_base_url = 'https://other.example/v1'
  assert.equal(state.modelTools.vision.details.length, 0)
})

test('ID-only gateways stay usable and configuration errors are displayed with request identifiers', async () => {
  const { state } = setup({ testAgriModel: async () => {
    throw { response: { status: 503, data: { detail: '配置加密未就绪' }, headers: { 'x-request-id': 'safe-synthetic-request' } } }
  } })
  await state.loadSettings(); await state.fetchModelList('vision')
  assert.equal(state.visionModelOptions.value.join(','), 'live-model')
  assert.match(state.visionFilterMessage.value, /无法可靠筛选/)
  await state.runModelTest('vision')
  assert.equal(state.modelTools.vision.result.message, '配置加密未就绪')
  assert.equal(state.modelTools.vision.result.request_id, 'safe-synthetic-request')
  assert.equal(state.modelTools.vision.result.vision, 'not_tested')
})
test('old-address discovery cannot overwrite a new-address list or survive leaving settings', async () => {
  const requests = []
  const { state, dispose } = setup({ discoverAgriModels: () => { const call = deferred(); requests.push(call); return call.promise } })
  await state.loadSettings()
  const old = state.fetchModelList('decision')
  state.agriAIConfig.llm_base_url = 'https://other.example/v1'
  const current = state.fetchModelList('decision')
  requests[1].resolve({ data: { success: true, models: ['current'], message: 'current' } }); await current
  requests[0].resolve({ data: { success: true, models: ['old'], message: 'old' } }); await old
  assert.equal(state.modelTools.decision.models.join(','), 'current')
  const pending = state.fetchModelList('decision'); dispose.forEach(fn => fn())
  requests[2].resolve({ data: { success: true, models: ['after-unmount'], message: 'late' } }); await pending
  assert.equal(state.modelTools.decision.models.length, 0)
})
test('successful text-only response does not become successful visual certification and changed draft rejects late test', async () => {
  const call = deferred()
  const { state, calls } = setup({ testAgriModel: body => { calls.push(body); return call.promise } })
  await state.loadSettings()
  const pending = state.runModelTest('vision')
  await state.runModelTest('vision')
  assert.equal(calls.length, 1)
  state.agriAIConfig.vision_model = 'changed'
  call.resolve({ data: { success: false, checks: [], authentication: 'verified', vision: 'accepted_unverified', message: 'sample not verified' } })
  await pending
  assert.equal(state.modelTools.vision.result, null)
})
test('visual draft may explicitly reuse typed decision key only for the same address', async () => {
  const { state } = setup(); await state.loadSettings()
  state.agriAIConfig.llm_api_key = 'synthetic-draft-key'
  assert.equal(state.modelDraft('vision').api_key, 'synthetic-draft-key')
  state.agriAIConfig.vision_base_url = 'https://other.example/v1'
  assert.equal(state.modelDraft('vision').api_key, undefined)
})
test('save uses returned key availability and retains a new key typed while the older save is pending', async () => {
  const call = deferred()
  const { state, calls } = setup({ updateSettings: body => { calls.push(body); return call.promise } })
  await state.loadSettings()
  state.agriAIConfig.llm_base_url = 'https://other.example/v1'
  state.agriAIConfig.llm_api_key = 'synthetic-submitted-key'
  const pending = state.saveAgriAISettings()
  state.agriAIConfig.llm_api_key = 'synthetic-newer-draft-key'
  call.resolve({ data: { agri_ai: { llm_base_url: 'https://other.example/v1', vision_base_url: 'https://provider.example/v1',
    has_llm_key: true, has_vision_key: false, has_weather_key: false } } })
  await pending
  assert.equal(calls[0].agri_ai.llm_api_key, 'synthetic-submitted-key')
  assert.equal(state.agriAIConfig.llm_api_key, 'synthetic-newer-draft-key')
  assert.equal(state.agriAIConfig.has_vision_key, false)
})
test('embedding dimension request policy is explicit and save uses the actual returned credential state', async () => {
  const { state, calls } = setup({ getSettings: async () => ({ data: { embedding: {
    model: 'configured-embedding', dimension: 1024, request_dimensions: true, credential_source: 'agricultural_decision' } } }),
    updateSettings: async body => { calls.push(body); return { data: { embedding: { has_api_key: false, credential_source: 'missing' } } } } })
  await state.loadSettings()
  assert.equal(state.embeddingConfig.model, 'configured-embedding')
  assert.equal(state.embeddingConfig.requestDimensions, true)
  await state.saveEmbeddingSettings()
  assert.equal(calls[0].embedding.request_dimensions, true)
  assert.equal(state.embeddingConfig.hasApiKey, false)
  assert.equal(state.embeddingConfig.credentialSource, 'missing')
})
test('settings template retires the unrelated shared chat card and retains current agricultural and embedding controls', () => {
  const source = readFileSync(new URL('../src/views/Settings.vue', import.meta.url), 'utf8')
  assert.ok(!source.includes('基础 LLM 知识问答配置'))
  assert.ok(source.includes('embeddingConfig.requestDimensions'))
  assert.ok(source.includes('modelTools.decision'))
})

test('weather probe requires typed coordinates and passes a custom contract without saving', async () => {
  const calls = []
  const { state, messages } = setup({ testWeatherConnection: async body => { calls.push(body); return { data: { success: true, message: 'actual fields verified' } } } })
  await state.handleTestWeatherConnection()
  assert.equal(calls.length, 0)
  assert.match(messages.at(-1).text, /经纬度/)
  state.agriAIConfig.weather_provider = 'custom'
  state.agriAIConfig.weather_custom.endpoint = 'https://weather.example/current'
  state.agriAIConfig.weather_custom.auth_type = 'header'
  state.agriAIConfig.weather_api_key = 'synthetic-weather-key'
  state.weatherTools.latitude = 0; state.weatherTools.longitude = 0
  await state.handleTestWeatherConnection()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].latitude, 0)
  assert.equal(calls[0].weather_custom.endpoint, 'https://weather.example/current')
  assert.equal(calls[0].api_key, 'synthetic-weather-key')
  assert.equal(state.weatherTools.result, 'actual fields verified')
})
test('a changed weather draft or unmount rejects a late probe result and duplicate clicks', async () => {
  const calls = [], pending = deferred()
  const { state, dispose } = setup({ testWeatherConnection: body => { calls.push(body); return pending.promise } })
  state.weatherTools.latitude = 1; state.weatherTools.longitude = 2
  const first = state.handleTestWeatherConnection()
  await state.handleTestWeatherConnection()
  assert.equal(calls.length, 1)
  state.weatherTools.longitude = 3
  pending.resolve({ data: { success: true, message: 'stale probe' } }); await first
  assert.equal(state.weatherTools.result, '')
  const waiting = deferred()
  const later = setup({ testWeatherConnection: () => waiting.promise })
  later.state.weatherTools.latitude = 1; later.state.weatherTools.longitude = 2
  const request = later.state.handleTestWeatherConnection()
  later.dispose.forEach(fn => fn())
  waiting.resolve({ data: { success: true, message: 'unmounted' } }); await request
  assert.equal(later.state.weatherTools.result, '')
})
test('weather endpoint changes clear a typed old key and invalid mapping blocks the request', async () => {
  let calls = 0
  const { state, messages } = setup({ testWeatherConnection: async () => { ++calls; return { data: {} } } })
  state.agriAIConfig.weather_api_key = 'synthetic-old-key'
  state.onWeatherEndpointChange()
  assert.equal(state.agriAIConfig.weather_api_key, '')
  state.agriAIConfig.weather_provider = 'custom'
  state.weatherTools.latitude = 1; state.weatherTools.longitude = 2
  state.weatherMappingText.current = 'not-json'
  await state.handleTestWeatherConnection()
  assert.equal(calls, 0)
  assert.ok(messages.some(value => value.level === 'error'))
})
test('saving custom weather persists the contract and respects the returned key availability', async () => {
  const bodies = []
  const { state } = setup({ updateSettings: async body => { bodies.push(body); return { data: { agri_ai: {
    weather_provider: 'custom', weather_custom: body.agri_ai.weather_custom, has_weather_key: false,
    llm_base_url: 'https://provider.example/v1', vision_base_url: 'https://provider.example/v1' } } } } })
  state.agriAIConfig.weather_provider = 'custom'
  state.agriAIConfig.weather_custom.endpoint = 'https://weather.example/current'
  state.agriAIConfig.weather_api_key = 'synthetic-submitted-key'
  await state.saveAgriAISettings()
  assert.equal(bodies[0].agri_ai.weather_custom.current_fields.timestamp, 'timestamp')
  assert.equal(state.agriAIConfig.has_weather_key, false)
  assert.equal(state.agriAIConfig.weather_api_key, '')
})
