<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { Connection, Sunny, Camera, Cpu } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import {
  getSettings, getSettingsSecurityStatus,
  updateSettings,
  getLLMProviders,
  testWeatherConnection,
  discoverAgriModels, testAgriModel,
  type AgriModelKind, type ModelProbeResult, type ModelCapability
} from '@/api/settings'

const encryptionStatus = ref<{ encryption_ready: boolean; message: string } | null>(null)
async function loadSecurityStatus() {
  try { const result = await getSettingsSecurityStatus(); if (alive) encryptionStatus.value = result.data }
  catch { if (alive) encryptionStatus.value = { encryption_ready: false, message: '无法核实配置加密状态，请刷新后重试。' } }
}

// Types
interface LLMProvider {
  name: string
  displayName: string
  defaultApiUrl: string
  defaultModel: string
  supportedModels: string[]
}

interface RAGConfig {
  topK: number
  similarityThreshold: number
  useRewrite: boolean
  maxContextLength: number
}

// State
const loading = ref(false)
const saving = ref(false)
const savingAgriAI = ref(false)
const testingWeather = ref(false)

// LLM providers
const providers = ref<LLMProvider[]>([])

function defaultWeatherConfig() {
  return { endpoint: '', forecast_endpoint: '', source_name: '自定义天气', format: 'mapped_json',
    auth_type: 'none', auth_header: 'X-Api-Key', location_mode: 'separate', latitude_param: 'latitude',
    longitude_param: 'longitude', location_param: 'location', days_param: 'days',
    current_path: 'current', forecast_path: 'forecasts', temperature_unit: 'C', wind_unit: 'm/s',
    precipitation_unit: 'mm', probability_unit: 'percent', timestamp_timezone: 'Asia/Shanghai',
    current_fields: { timestamp: 'timestamp', temperature: 'temperature', humidity: 'humidity', rainfall: 'rainfall', wind_speed: 'wind_speed', weather: 'weather' },
    forecast_fields: { date: 'date', temp_max: 'temp_max', temp_min: 'temp_min', rainfall_mm: 'rainfall_mm', rain_prob: 'rain_prob', wind_speed: 'wind_speed', humidity: 'humidity', weather: 'weather' } }
}
// Agri AI & External Services config
const agriAIConfig = reactive({
  llm_api_key: '',
  llm_base_url: 'https://api.siliconflow.cn/v1',
  llm_model: 'deepseek-ai/DeepSeek-V3',
  has_llm_key: false,
  vision_api_key: '',
  vision_base_url: 'https://api.siliconflow.cn/v1',
  vision_model: 'Qwen/Qwen3-VL-8B-Instruct',
  has_vision_key: false,
  weather_provider: 'open_meteo',
  weather_custom: defaultWeatherConfig(),
  weather_api_key: '',
  has_weather_key: false
})

const weatherMappingText = reactive({ current: JSON.stringify(defaultWeatherConfig().current_fields, null, 2), forecast: JSON.stringify(defaultWeatherConfig().forecast_fields, null, 2) })
const weatherTools = reactive({ latitude: null as number | null, longitude: null as number | null, result: '', passed: false })
const savedWeatherScope = ref('')
let weatherRevision = 0
function weatherCredentialScope() {
  if (agriAIConfig.weather_provider !== 'custom') return JSON.stringify([agriAIConfig.weather_provider])
  const cfg = agriAIConfig.weather_custom
  return JSON.stringify(['custom', cfg.endpoint.trim(), cfg.forecast_endpoint.trim(), cfg.auth_type, cfg.auth_header])
}
function weatherSignature() { return JSON.stringify([agriAIConfig.weather_provider, agriAIConfig.weather_custom, weatherMappingText, agriAIConfig.weather_api_key, weatherTools.latitude, weatherTools.longitude]) }
function weatherDraft() {
  const config = { ...agriAIConfig.weather_custom }
  if (config.format === 'mapped_json') {
    config.current_fields = JSON.parse(weatherMappingText.current)
    config.forecast_fields = JSON.parse(weatherMappingText.forecast)
    if (!config.current_fields || Array.isArray(config.current_fields) || typeof config.current_fields !== 'object'
      || !config.forecast_fields || Array.isArray(config.forecast_fields) || typeof config.forecast_fields !== 'object') throw Error('字段映射须为JSON对象')
  }
  return config
}
function onWeatherEndpointChange() {
  agriAIConfig.weather_api_key = ''
  weatherTools.result = ''; weatherTools.passed = false; ++weatherRevision
}
watch(weatherSignature, () => { weatherTools.result = ''; weatherTools.passed = false; ++weatherRevision }, { flush: 'sync' })
let alive = true
onBeforeUnmount(() => { alive = false })
const normalizedURL = (value: string) => value.trim().replace(/\/+$/, '')
const savedModelEndpoints = reactive({ decision: '', vision: '' })
interface ModelToolState {
  details: ModelCapability[]
  provider: string; models: string[]; listMessage: string; truncated: boolean; listRequestId: string
  loading: boolean; testing: boolean; result: ModelProbeResult | null
  listEpoch: number; testEpoch: number
}
const emptyModelTool = (): ModelToolState => ({ provider: 'custom', models: [], details: [], listMessage: '', truncated: false, listRequestId: '',
  loading: false, testing: false, result: null, listEpoch: 0, testEpoch: 0 })
const modelTools = reactive<Record<AgriModelKind, ModelToolState>>({ decision: emptyModelTool(), vision: emptyModelTool() })
const visionModelFilter = ref<'candidates' | 'declared' | 'all'>('candidates')
const visionModelOptions = computed(() => {
  const tool = modelTools.vision
  if (visionModelFilter.value === 'all' || !tool.details.length) return tool.models
  return tool.models.filter(id => {
    const info = tool.details.find(model => model.id === id)
    return visionModelFilter.value === 'declared' ? info?.image_input === 'supported' : info?.vision_candidate
  })
})
const visionFilterMessage = computed(() => {
  const tool = modelTools.vision
  if (!tool.models.length) return ''
  if (!tool.details.length) return '平台只返回模型ID，无法可靠筛选图像能力；暂显示全部，支持手填和测试。'
  if (!visionModelOptions.value.length) return '当前筛选没有候选，请切换“全部模型”或手填；平台未声明能力不等于模型不支持图片。'
  return `当前显示 ${visionModelOptions.value.length} / ${tool.models.length} 项。平台声明与名称候选都需测试；名称不能证明图像能力。`
})
function visionModelLabel(id: string) {
  const info = modelTools.vision.details.find(model => model.id === id)
  if (!info) return id
  const label = info.image_input === 'supported' ? '平台声明图像输入' : info.image_input === 'unsupported'
    ? '平台未支持视觉聊天' : info.evidence === 'model_name' ? '视觉候选，待验证' : '图像能力未知'
  return `${id} · ${label}`
}
function modelCheckLabel(name: string) {
  return ({ configuration: '配置检查', model_list: '模型列表', text_call: '文本调用', image_call: '图像输入与颜色校验', transport: '请求通信' } as Record<string, string>)[name] || name
}
function modelCheckState(state: string) {
  return ({ passed: '通过', failed: '未通过', unverified: '未验证' } as Record<string, string>)[state] || state
}
function modelValues(kind: AgriModelKind) {
  return kind === 'decision' ? { url: agriAIConfig.llm_base_url, key: agriAIConfig.llm_api_key, model: agriAIConfig.llm_model }
    : { url: agriAIConfig.vision_base_url, key: agriAIConfig.vision_api_key, model: agriAIConfig.vision_model }
}
function canUseSavedKey(kind: AgriModelKind): boolean {
  return normalizedURL(modelValues(kind).url) === savedModelEndpoints[kind]
    && (kind === 'decision' ? agriAIConfig.has_llm_key : agriAIConfig.has_vision_key)
}
function clearModelTools(kind: AgriModelKind) {
  const tool = modelTools[kind]
  tool.models = []; tool.details = []; tool.listMessage = ''; tool.listRequestId = ''; tool.truncated = false; tool.result = null
  tool.listEpoch++; tool.testEpoch++; tool.loading = false; tool.testing = false
}
for (const kind of ['decision', 'vision'] as const) {
  watch(() => [modelValues(kind).url, modelValues(kind).key], () => {
    clearModelTools(kind)
    modelTools[kind].provider = providers.value.find(provider => normalizedURL(provider.defaultApiUrl) === normalizedURL(modelValues(kind).url))?.name || 'custom'
  }, { flush: 'sync' })
  watch(() => modelValues(kind).model, () => { modelTools[kind].testEpoch++; modelTools[kind].testing = false; modelTools[kind].result = null }, { flush: 'sync' })
}
watch(() => agriAIConfig.llm_api_key, () => {
  if (normalizedURL(agriAIConfig.llm_base_url) === normalizedURL(agriAIConfig.vision_base_url) && !agriAIConfig.vision_api_key) clearModelTools('vision')
}, { flush: 'sync' })
function selectModelPlatform(kind: AgriModelKind) {
  const provider = providers.value.find(item => item.name === modelTools[kind].provider)
  if (!provider) { clearModelTools(kind); return }
  if (kind === 'decision') {
    agriAIConfig.llm_api_key = ''; agriAIConfig.llm_base_url = provider.defaultApiUrl; agriAIConfig.llm_model = ''
  } else {
    agriAIConfig.vision_api_key = ''; agriAIConfig.vision_base_url = provider.defaultApiUrl; agriAIConfig.vision_model = ''
  }
}
function modelDraft(kind: AgriModelKind) {
  const values = modelValues(kind)
  const typedDecisionKey = kind === 'vision' && !canUseSavedKey('vision')
    && normalizedURL(values.url) === normalizedURL(agriAIConfig.llm_base_url) ? agriAIConfig.llm_api_key : ''
  return { kind, base_url: values.url, model: values.model, api_key: values.key || typedDecisionKey || undefined }
}
async function fetchModelList(kind: AgriModelKind) {
  const tool = modelTools[kind]
  if (tool.loading) return
  const epoch = ++tool.listEpoch
  tool.loading = true; tool.models = []; tool.details = []; tool.listMessage = ''; tool.listRequestId = ''; tool.truncated = false
  try {
    const response = await discoverAgriModels(modelDraft(kind))
    if (!alive || epoch !== tool.listEpoch) return
    tool.models = response.data.success ? response.data.models : []
    tool.details = response.data.success ? response.data.model_details || [] : []
    tool.listMessage = response.data.message
    tool.listRequestId = response.data.request_id || ''
    tool.truncated = response.data.truncated
  } catch {
    if (!alive || epoch !== tool.listEpoch) return
    tool.listMessage = '平台模型列表读取失败；可继续手填模型并测试调用'
  } finally { if (alive && epoch === tool.listEpoch) tool.loading = false }
}
async function runModelTest(kind: AgriModelKind) {
  const tool = modelTools[kind]
  if (tool.testing) return
  const epoch = ++tool.testEpoch
  tool.testing = true; tool.result = null
  try {
    const response = await testAgriModel(modelDraft(kind))
    if (!alive || epoch !== tool.testEpoch) return
    tool.result = response.data
  } catch (error: any) {
    if (!alive || epoch !== tool.testEpoch) return
    const requestId = error.response?.headers?.["x-request-id"] || error.response?.data?.request_id
    const detail = error.response?.data?.detail
    const message = typeof detail === 'string' ? detail : error.code === 'ECONNABORTED'
      ? '页面等待测试超时，尚未确认图像能力；请按请求ID核查'
      : '模型测试请求未完成，请核对配置及服务连接'
    tool.result = { success: false, message, authentication: 'unknown', vision: 'not_tested', request_id: requestId,
      checks: [{ name: 'transport', state: 'failed', code: error.response?.status ? `http_${error.response.status}` : 'request_failed', message }] }
    ElMessage.error(message)
  } finally { if (alive && epoch === tool.testEpoch) tool.testing = false }
}

// Embedding uses its own model and same-address credentials.
const credentialSourceLabels: Record<string, string> = {
  saved_embedding: '独立Embedding密钥', agricultural_decision: '同地址农业决策密钥',
  legacy_shared: '同地址历史共享密钥（待迁移）', environment_embedding: '同地址部署Embedding密钥',
  environment_llm: '同地址部署模型密钥', missing: '未取得可用密钥'
}
const embeddingConfig = reactive<{
  provider: string
  apiKey: string
  apiUrl: string
  model: string
  dimension: number
  requestDimensions: boolean
  credentialSource: string
  hasApiKey: boolean
}>({
  provider: '',
  apiKey: '',
  apiUrl: '',
  model: 'BAAI/bge-large-zh-v1.5',
  requestDimensions: false,
  credentialSource: "missing",
  dimension: 1024,
  hasApiKey: false
})

const ragConfig = reactive<RAGConfig>({
  topK: 5,
  similarityThreshold: 0.5,
  useRewrite: true,
  maxContextLength: 2000
})

const generalConfig = reactive({
  timezone: 'Asia/Shanghai'
})

const timezoneOptions = [
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Asia/Singapore',
  'Asia/Hong_Kong',
  'America/New_York',
  'America/Los_Angeles',
  'Europe/London',
  'UTC',
]

// Load settings
async function loadSettings() {
  loading.value = true
  try {
    const [settingsResult, providersResult] = await Promise.allSettled([getSettings(), getLLMProviders()])
    if (!alive) return
    if (settingsResult.status === "rejected") throw settingsResult.reason
    const settingsRes = settingsResult.value
    const providersRes = providersResult.status === "fulfilled" ? providersResult.value : { data: [] }
    if (providersResult.status === "rejected") ElMessage.warning("平台预置列表读取失败；仍可使用自定义地址与手填模型")
    
    // Transform providers
    const providersData = providersRes.data as any
    providers.value = (providersData.providers || providersData || []).map((p: any) => ({
      name: p.name,
      displayName: p.display_name || p.displayName,
      defaultApiUrl: p.default_api_url || p.defaultApiUrl,
      defaultModel: p.default_model || p.defaultModel,
      supportedModels: p.supported_models || p.supportedModels || []
    }))
    
    const settingsData = settingsRes.data as any
    // Transform Embedding config
    const embedding = settingsData.embedding || {}
    embeddingConfig.provider = embedding.provider || 'siliconflow'
    embeddingConfig.apiKey = ''  // Don't show actual key
    embeddingConfig.apiUrl = embedding.api_url || ''
    embeddingConfig.model = embedding.model || 'BAAI/bge-large-zh-v1.5'
    embeddingConfig.dimension = embedding.dimension || 1024
    embeddingConfig.hasApiKey = embedding.has_api_key || false
    embeddingConfig.requestDimensions = !!embedding.request_dimensions
    embeddingConfig.credentialSource = embedding.credential_source || "missing"
    
    // Transform RAG config
    const rag = settingsData.rag || settingsData
    ragConfig.topK = rag.top_k ?? rag.ragTopK ?? 5
    ragConfig.similarityThreshold = rag.similarity_threshold ?? rag.ragSimilarityThreshold ?? 0.5
    ragConfig.useRewrite = rag.use_rewrite !== undefined ? rag.use_rewrite : true
    ragConfig.maxContextLength = rag.max_context_length || 2000

    // Transform General config
    const general = settingsData.general || {}
    generalConfig.timezone = general.timezone || 'Asia/Shanghai'

    // Transform Agri AI config
    const agri = settingsData.agri_ai || {}
    agriAIConfig.llm_base_url = agri.llm_base_url || 'https://api.siliconflow.cn/v1'
    agriAIConfig.llm_model = agri.llm_model || 'deepseek-ai/DeepSeek-V3'
    agriAIConfig.has_llm_key = !!agri.has_llm_key
    agriAIConfig.vision_base_url = agri.vision_base_url || 'https://api.siliconflow.cn/v1'
    agriAIConfig.vision_model = agri.vision_model || 'Qwen/Qwen3-VL-8B-Instruct'
    agriAIConfig.has_vision_key = !!agri.has_vision_key
    agriAIConfig.weather_provider = agri.weather_provider || 'open_meteo'
    agriAIConfig.weather_custom = { ...defaultWeatherConfig(), ...(agri.weather_custom || {}) }
    weatherMappingText.current = JSON.stringify(agriAIConfig.weather_custom.current_fields, null, 2)
    weatherMappingText.forecast = JSON.stringify(agriAIConfig.weather_custom.forecast_fields, null, 2)
    savedWeatherScope.value = weatherCredentialScope()
    agriAIConfig.has_weather_key = !!agri.has_weather_key
    savedModelEndpoints.decision = normalizedURL(agriAIConfig.llm_base_url)
    savedModelEndpoints.vision = normalizedURL(agriAIConfig.vision_base_url)
    for (const kind of ["decision", "vision"] as const) {
      modelTools[kind].provider = providers.value.find(item => normalizedURL(item.defaultApiUrl) === normalizedURL(modelValues(kind).url))?.name || "custom"
    }
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '加载设置失败')
  } finally {
    loading.value = false
  }
}

// Save Embedding settings
async function saveEmbeddingSettings() {
  saving.value = true
  try {
    const data: any = {
      embedding: {
        provider: embeddingConfig.provider,
        api_url: embeddingConfig.apiUrl,
        model: embeddingConfig.model,
        dimension: embeddingConfig.dimension,
        request_dimensions: embeddingConfig.requestDimensions
      }
    }
    
    // Only include API key if it was changed
    if (embeddingConfig.apiKey) {
      data.embedding.api_key = embeddingConfig.apiKey
    }
    
    const response = await updateSettings(data)
    ElMessage.success('Embedding设置已保存；现有索引需核对或重建')
    embeddingConfig.hasApiKey = !!(response.data as any).embedding?.has_api_key
    embeddingConfig.credentialSource = (response.data as any).embedding?.credential_source || "missing"
    embeddingConfig.apiKey = ''  // Clear the input
    // Saved credential availability comes from the response.
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

// Save RAG settings
async function saveRAGSettings() {
  saving.value = true
  try {
    await updateSettings({
      rag: {
        top_k: ragConfig.topK,
        similarity_threshold: ragConfig.similarityThreshold,
        use_rewrite: ragConfig.useRewrite,
        max_context_length: ragConfig.maxContextLength
      }
    })
    ElMessage.success('RAG设置已保存')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

// Save General settings
async function saveGeneralSettings() {
  saving.value = true
  try {
    await updateSettings({
      general: {
        timezone: generalConfig.timezone
      }
    })
    ElMessage.success('通用设置已保存')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

// Save Agri AI settings
async function saveAgriAISettings() {
  savingAgriAI.value = true
  try {
    const data: any = {
      agri_ai: {
        llm_base_url: agriAIConfig.llm_base_url,
        llm_model: agriAIConfig.llm_model,
        vision_base_url: agriAIConfig.vision_base_url,
        vision_model: agriAIConfig.vision_model,
        weather_provider: agriAIConfig.weather_provider,
        weather_custom: agriAIConfig.weather_provider === "custom" ? weatherDraft() : undefined
      }
    }
    if (agriAIConfig.llm_api_key) {
      data.agri_ai.llm_api_key = agriAIConfig.llm_api_key
    }
    if (agriAIConfig.vision_api_key) {
      data.agri_ai.vision_api_key = agriAIConfig.vision_api_key
    }
    if (agriAIConfig.weather_api_key) {
      data.agri_ai.weather_api_key = agriAIConfig.weather_api_key
    }
    const response = await updateSettings(data)
    if (!alive) return
    const saved = (response.data as any).agri_ai
    if (saved) {
      savedModelEndpoints.decision = normalizedURL(saved.llm_base_url)
      savedModelEndpoints.vision = normalizedURL(saved.vision_base_url)
      agriAIConfig.has_llm_key = !!saved.has_llm_key
      agriAIConfig.has_vision_key = !!saved.has_vision_key
      agriAIConfig.has_weather_key = !!saved.has_weather_key
      const savedCustom = saved.weather_custom || defaultWeatherConfig()
      savedWeatherScope.value = saved.weather_provider === "custom"
        ? JSON.stringify(["custom", savedCustom.endpoint.trim(), savedCustom.forecast_endpoint.trim(), savedCustom.auth_type, savedCustom.auth_header])
        : JSON.stringify([saved.weather_provider || "open_meteo"])
    }
    ElMessage.success('农业智能中枢与服务配置已保存！')
    if (agriAIConfig.llm_api_key === data.agri_ai.llm_api_key) agriAIConfig.llm_api_key = ''
    if (agriAIConfig.vision_api_key === data.agri_ai.vision_api_key) agriAIConfig.vision_api_key = ''
    if (agriAIConfig.weather_api_key === data.agri_ai.weather_api_key) agriAIConfig.weather_api_key = ''
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || error.message || '保存农业AI配置失败')
  } finally {
    savingAgriAI.value = false
  }
}

// Compatibility handler for the existing visual test button.
async function handleTestVisionConnection() { await runModelTest('vision') }

// Test Weather connection
async function handleTestWeatherConnection() {
  if (testingWeather.value) return
  if (weatherTools.latitude === null || weatherTools.longitude === null) { ElMessage.error('请填写本次测试地点的经纬度'); return }
  const signature = weatherSignature(); const revision = ++weatherRevision
  testingWeather.value = true; weatherTools.result = ''; weatherTools.passed = false
  try {
    const response = await testWeatherConnection({
      provider: agriAIConfig.weather_provider,
      api_key: agriAIConfig.weather_api_key || undefined,
      weather_custom: agriAIConfig.weather_provider === "custom" ? weatherDraft() : undefined, latitude: weatherTools.latitude, longitude: weatherTools.longitude
    })
    if (!alive || signature !== weatherSignature() || revision !== weatherRevision) return
    weatherTools.result = response.data.message || '天气接口未返回有效测试说明'
    weatherTools.passed = !!response.data.success
    if (response.data.success) ElMessage.success(weatherTools.result)
    else ElMessage.error(weatherTools.result)
  } catch (error: any) {
    if (!alive || signature !== weatherSignature() || revision !== weatherRevision) return
    weatherTools.result = error.response?.data?.detail || error.message || '测试气象连接失败'
    ElMessage.error(weatherTools.result)
  } finally { if (alive) testingWeather.value = false }
}


// Initialize
onMounted(() => {
  loadSecurityStatus()
  loadSettings()
})
</script>

<template>
  <div class="settings-container">
    <el-alert v-if="encryptionStatus && !encryptionStatus.encryption_ready" :title="encryptionStatus.message" type="warning" :closable="false" show-icon style="margin-bottom: var(--app-space-4)" />
    <PageHeader
      title="系统设置"
      description="AgriMind 农业智能决策中枢、多模态病害视觉诊断与真实气象时序系统参数配置"
    />

    <div
      v-loading="loading"
      class="settings-content"
    >
      <!-- AgriMind Core AI & External Services Settings -->
      <el-card
        class="settings-card agri-core-card"
        shadow="never"
      >
        <template #header>
          <div class="card-header">
            <span><el-icon><Cpu /></el-icon> 农业决策大模型、多模态病害视觉与真实气象系统（AgriMind 核心服务）</span>
          </div>
        </template>
        
        <el-form
          label-width="120px"
          label-position="left"
        >
          <!-- 1. 农业决策大模型 -->
          <el-divider content-position="left">
            <span class="section-title">
              <el-icon><Cpu /></el-icon> 农业决策智能体 (Agent Decision LLM)
            </span>
          </el-divider>
          
          <el-form-item label="决策平台">
            <el-select v-model="modelTools.decision.provider" class="ctl-sm" @change="selectModelPlatform('decision')">
              <el-option v-for="provider in providers" :key="provider.name" :label="provider.displayName + '（兼容接口预置）'" :value="provider.name" />
              <el-option label="自定义兼容地址" value="custom" />
            </el-select>
            <div class="form-tip">切换地址不自动沿用旧平台密钥；区域或网关地址请按账号资料修改。</div>
          </el-form-item>
          <el-form-item label="决策大模型 API Key">
            <el-input
              v-model="agriAIConfig.llm_api_key"
              type="password"
              :placeholder="canUseSavedKey('decision') ? '已配置（留空保持不变）' : '请输入硅基流动等 API Key'"
              class="ctl"
              show-password
            />
            <el-tag
              v-if="canUseSavedKey('decision')"
              type="success"
              size="small"
              style="margin-left: var(--app-space-3)"
            >
              已配置密钥
            </el-tag>
          </el-form-item>

          <el-form-item label="模型 Base URL">
            <el-input
              v-model="agriAIConfig.llm_base_url"
              placeholder="https://api.siliconflow.cn/v1"
              class="ctl"
            />
          </el-form-item>

          <el-form-item label="决策模型名称">
            <el-select v-model="agriAIConfig.llm_model" filterable allow-create default-first-option placeholder="读取列表或手填模型ID" class="ctl">
              <el-option v-for="model in modelTools.decision.models" :key="model" :label="model" :value="model" />
            </el-select>
            <el-button :loading="modelTools.decision.loading" @click="fetchModelList('decision')">读取模型</el-button>
            <div class="form-tip">
              模型列表来自本次平台响应，不保证模型用途或实际调用权限；支持手填模型ID。
            </div>
          </el-form-item>

          <el-form-item>
            <el-button :loading="modelTools.decision.testing" @click="runModelTest('decision')">测试文本调用</el-button>
          </el-form-item>
          <el-alert v-if="modelTools.decision.listMessage" :title="modelTools.decision.listMessage" type="info" :closable="false" />
          <p v-if="modelTools.decision.listRequestId">模型列表请求ID：{{ modelTools.decision.listRequestId }}</p>
          <el-alert v-if="modelTools.decision.truncated" title="列表仅展示本次响应前500项或首个分页；仍支持手填" type="warning" :closable="false" />
          <div v-if="modelTools.decision.result">
            <el-alert :title="modelTools.decision.result.message" :type="modelTools.decision.result.success ? 'success' : 'warning'" :closable="false" />
            <p v-if="modelTools.decision.result.request_id">测试请求ID：{{ modelTools.decision.result.request_id }}</p>
            <p v-for="check in modelTools.decision.result.checks" :key="check.name">{{ modelCheckLabel(check.name) }} · {{ modelCheckState(check.state) }}（{{ check.code }}）：{{ check.message }}</p>
          </div>

          <!-- 2. 多模态病害视觉诊断 -->
          <el-divider content-position="left">
            <span class="section-title">
              <el-icon><Camera /></el-icon> 多模态病叶视觉诊断方案 (Vision Multi-Modal API)
            </span>
          </el-divider>

          <el-form-item label="视觉平台">
            <el-select v-model="modelTools.vision.provider" class="ctl-sm" @change="selectModelPlatform('vision')">
              <el-option v-for="provider in providers" :key="provider.name" :label="provider.displayName + '（兼容接口预置）'" :value="provider.name" />
              <el-option label="自定义兼容地址" value="custom" />
            </el-select>
            <div class="form-tip">切换地址不自动沿用旧平台密钥；区域或网关地址请按账号资料修改。</div>
          </el-form-item>
          <el-form-item label="视觉多模态 API Key">
            <el-input
              v-model="agriAIConfig.vision_api_key"
              type="password"
              :placeholder="canUseSavedKey('vision') ? '已配置（留空保持不变）' : '仅相同地址可复用决策密钥；切换地址需输入密钥'"
              class="ctl"
              show-password
            />
            <el-tag
              v-if="canUseSavedKey('vision')"
              type="success"
              size="small"
              style="margin-left: var(--app-space-3)"
            >
              已配置视觉密钥
            </el-tag>
            <el-button 
              type="info"
              plain
              size="small"
              :icon="Connection" 
              :loading="modelTools.vision.testing"
              style="margin-left: var(--app-space-3)"
              @click="handleTestVisionConnection"
            >
              测试文本与图像
            </el-button>
          </el-form-item>

          <el-form-item label="视觉 Base URL">
            <el-input
              v-model="agriAIConfig.vision_base_url"
              placeholder="https://api.siliconflow.cn/v1"
              class="ctl"
            />
          </el-form-item>

          <el-form-item label="多模态视觉模型">
            <el-select v-model="agriAIConfig.vision_model" filterable allow-create default-first-option placeholder="读取列表或手填模型ID" class="ctl">
              <el-option v-for="model in visionModelOptions" :key="model" :label="visionModelLabel(model)" :value="model" />
            </el-select>
            <el-button :loading="modelTools.vision.loading" @click="fetchModelList('vision')">读取模型</el-button>
            <div class="vision-filter-row">
              <span class="vision-filter-label">列表筛选</span>
              <el-select v-model="visionModelFilter" class="ctl-xs" aria-label="视觉模型筛选">
                <el-option label="图像候选（含待验证）" value="candidates" />
                <el-option label="平台声明图像输入" value="declared" />
                <el-option label="全部模型" value="all" />
              </el-select>
            </div>
            <div class="form-tip">
              图像测试使用合成色块，可能产生平台调用费用；通过不代表病害诊断效果，未知评分仍保持未知。
            </div>
          </el-form-item>

          <p v-if="visionFilterMessage" class="form-tip">{{ visionFilterMessage }}</p>
          <p class="form-tip">测试分别检查模型列表、文本回复和随机色块图像；慢模型最长等待约80秒。列表不可用可继续手填；只通过文本不代表通过图像。</p>

          <el-alert v-if="modelTools.vision.listMessage" :title="modelTools.vision.listMessage" type="info" :closable="false" />
          <p v-if="modelTools.vision.listRequestId">模型列表请求ID：{{ modelTools.vision.listRequestId }}</p>
          <el-alert v-if="modelTools.vision.truncated" title="列表仅展示本次响应前500项或首个分页；仍支持手填" type="warning" :closable="false" />
          <div v-if="modelTools.vision.result">
            <el-alert :title="modelTools.vision.result.message" :type="modelTools.vision.result.success ? 'success' : 'warning'" :closable="false" />
            <p v-if="modelTools.vision.result.request_id">测试请求ID：{{ modelTools.vision.result.request_id }}</p>
            <p v-for="check in modelTools.vision.result.checks" :key="check.name">
              {{ modelCheckLabel(check.name) }} · {{ modelCheckState(check.state) }}（{{ check.code }}）：{{ check.message }}
              <br v-if="check.upstream_trace_id" /><small v-if="check.upstream_trace_id">平台追踪ID：{{ check.upstream_trace_id }}</small>
            </p>
            <p v-if="modelTools.vision.result.vision === 'not_tested'">图像检查尚未执行，不能据此判断所选模型不支持图片。</p>
          </div>

          <!-- 3. 真实气象时序系统 -->
          <el-divider content-position="left">
            <span class="section-title">
              <el-icon><Sunny /></el-icon> 真实经纬度气象时序与预测服务 (Weather Service API)
            </span>
          </el-divider>

          <el-form-item label="气象服务提供商">
            <el-select
              v-model="agriAIConfig.weather_provider"
              @change="onWeatherEndpointChange"
              placeholder="选择气象数据提供商"
              class="ctl"
            >
              <el-option
                label="Open-Meteo 天气预报（免Key）"
                value="open_meteo"
              />
              <el-option label="自定义HTTPS天气接口" value="custom" />
            </el-select>
          </el-form-item>

          <template v-if="agriAIConfig.weather_provider === 'custom'">
            <el-form-item label="来源名称"><el-input v-model="agriAIConfig.weather_custom.source_name" maxlength="80" placeholder="便于识别的数据来源名称" /></el-form-item>
            <el-form-item label="当前天气接口"><el-input v-model="agriAIConfig.weather_custom.endpoint" @change="onWeatherEndpointChange" placeholder="完整HTTPS GET地址，不含密钥或查询参数" /></el-form-item>
            <el-form-item label="预报接口"><el-input v-model="agriAIConfig.weather_custom.forecast_endpoint" @change="onWeatherEndpointChange" placeholder="可留空；留空时从同一响应读取预报。另填时需同一域名和端口" /></el-form-item>
            <el-form-item label="响应格式"><el-select v-model="agriAIConfig.weather_custom.format"><el-option label="通用JSON字段映射" value="mapped_json" /><el-option label="Open-Meteo兼容JSON" value="open_meteo" /></el-select></el-form-item>
            <el-form-item label="鉴权方式"><el-select v-model="agriAIConfig.weather_custom.auth_type" @change="onWeatherEndpointChange"><el-option label="无需鉴权" value="none" /><el-option label="Bearer" value="bearer" /><el-option label="自定义请求头" value="header" /></el-select></el-form-item>
            <el-form-item v-if="agriAIConfig.weather_custom.auth_type === 'header'" label="鉴权请求头"><el-input v-model="agriAIConfig.weather_custom.auth_header" @change="onWeatherEndpointChange" placeholder="例如X-Api-Key" /></el-form-item>
            <el-form-item v-if="agriAIConfig.weather_custom.auth_type !== 'none'" label="天气密钥"><el-input v-model="agriAIConfig.weather_api_key" type="password" show-password autocomplete="off" :placeholder="agriAIConfig.has_weather_key && savedWeatherScope === weatherCredentialScope() ? '同地址已有密钥，留空保留' : '请填写此接口密钥'" /></el-form-item>
            <el-collapse>
              <el-collapse-item title="坐标参数、JSON映射与单位（按接口文档填写）" name="weather-mapping">
                <template v-if="agriAIConfig.weather_custom.format === 'mapped_json'">
                <el-form-item label="坐标格式"><el-select v-model="agriAIConfig.weather_custom.location_mode"><el-option label="经纬度分开传递" value="separate" /><el-option label="经度,纬度" value="longitude_latitude" /><el-option label="纬度,经度" value="latitude_longitude" /></el-select></el-form-item>
                <template v-if="agriAIConfig.weather_custom.location_mode === 'separate'">
                  <el-form-item label="纬度参数名"><el-input v-model="agriAIConfig.weather_custom.latitude_param" /></el-form-item>
                  <el-form-item label="经度参数名"><el-input v-model="agriAIConfig.weather_custom.longitude_param" /></el-form-item>
                </template>
                <el-form-item v-else label="位置参数名"><el-input v-model="agriAIConfig.weather_custom.location_param" /></el-form-item>
                <el-form-item label="天数参数名"><el-input v-model="agriAIConfig.weather_custom.days_param" placeholder="接口不支持时留空" /></el-form-item>
                </template>
                <p v-if="agriAIConfig.weather_custom.format === 'open_meteo'" class="form-tip">该格式采用Open-Meteo标准坐标／字段／天数／北京时间／m/s请求参数，按响应中的单位解析。</p>
                <template v-if="agriAIConfig.weather_custom.format === 'mapped_json'">
                  <el-form-item label="当前对象路径"><el-input v-model="agriAIConfig.weather_custom.current_path" placeholder="current或data.now，根对象留空" /></el-form-item>
                  <el-form-item label="预报数组路径"><el-input v-model="agriAIConfig.weather_custom.forecast_path" placeholder="forecasts或data.daily" /></el-form-item>
                  <el-form-item label="当前字段映射"><el-input v-model="weatherMappingText.current" type="textarea" :rows="7" /></el-form-item>
                  <el-form-item label="预报字段映射"><el-input v-model="weatherMappingText.forecast" type="textarea" :rows="8" /></el-form-item>
                  <el-form-item label="温度单位"><el-select v-model="agriAIConfig.weather_custom.temperature_unit"><el-option label="摄氏度" value="C" /><el-option label="华氏度" value="F" /><el-option label="开尔文" value="K" /></el-select></el-form-item>
                  <el-form-item label="风速单位"><el-select v-model="agriAIConfig.weather_custom.wind_unit"><el-option label="m/s" value="m/s" /><el-option label="km/h" value="km/h" /><el-option label="knots" value="knots" /></el-select></el-form-item>
                  <el-form-item label="降水单位"><el-select v-model="agriAIConfig.weather_custom.precipitation_unit"><el-option label="mm" value="mm" /><el-option label="cm" value="cm" /><el-option label="in" value="in" /></el-select></el-form-item>
                  <el-form-item label="降雨概率"><el-select v-model="agriAIConfig.weather_custom.probability_unit"><el-option label="0至100百分比" value="percent" /><el-option label="0至1小数" value="fraction" /></el-select></el-form-item>
                  <el-form-item label="无时区时间"><el-select v-model="agriAIConfig.weather_custom.timestamp_timezone"><el-option label="北京时间" value="Asia/Shanghai" /><el-option label="UTC" value="UTC" /></el-select></el-form-item>
                </template>
              </el-collapse-item>
            </el-collapse>
            <div class="form-tip">仅支持HTTPS GET、Bearer或独立请求头鉴权，以及上述JSON契约。参数名称和单位按接口文档填写；不同签名协议需另做适配。缺失／过期字段保留未知，不采用接口自报的“适宜喷药”结论。</div>
          </template>
          <el-form-item label="测试纬度"><el-input-number v-model="weatherTools.latitude" :min="-90" :max="90" :precision="6" /></el-form-item>
          <el-form-item label="测试经度"><el-input-number v-model="weatherTools.longitude" :min="-180" :max="180" :precision="6" /></el-form-item>
          <el-form-item label="气象接口连通性">
            <el-button 
              type="warning"
              plain
              size="small"
              :icon="Connection" 
              :loading="testingWeather" 
              @click="handleTestWeatherConnection"
            >
              测试真实气象接口连通性
            </el-button>
            <div class="form-tip">
              测试使用上方填写的地点，可能产生平台调用费用；业务查询使用田块／农场绑定坐标。测试不保存配置，不证明数据质量或作业获准。
            </div>
          </el-form-item>

          <el-alert v-if="weatherTools.result" :title="weatherTools.result" :type="weatherTools.passed ? 'success' : 'warning'" :closable="false" />

          <el-form-item style="margin-top: var(--app-space-6);">
            <el-button
              type="primary"
              :loading="savingAgriAI"
              @click="saveAgriAISettings"
            >
              保存农业中枢与服务配置
            </el-button>
          </el-form-item>
        </el-form>
      </el-card>

      <!-- Embedding Settings -->
      <el-card
        class="settings-card"
        shadow="never"
      >
        <template #header>
          <div class="card-header">
            <span><el-icon><Setting /></el-icon> Embedding模型配置</span>
          </div>
        </template>
        
        <el-form
          label-width="140px"
          label-position="left"
        >
          <el-form-item label="提供商">
            <el-select
              v-model="embeddingConfig.provider"
              placeholder="选择提供商（通常与LLM相同）"
              class="ctl-sm"
            >
              <el-option
                v-for="provider in providers"
                :key="provider.name"
                :label="provider.displayName"
                :value="provider.name"
              />
            </el-select>
            <div class="form-tip">
              通常使用与LLM相同的提供商
            </div>
          </el-form-item>
          
          <el-form-item label="API密钥">
            <el-input
              v-model="embeddingConfig.apiKey"
              type="password"
              :placeholder="embeddingConfig.hasApiKey ? '已配置（留空保持不变）' : '仅相同地址可复用农业决策或历史凭据'"
              class="ctl"
              show-password
            />
            <el-tag
              v-if="embeddingConfig.hasApiKey"
              type="success"
              size="small"
              style="margin-left: var(--app-space-3)"
            >
              已配置
            </el-tag>
            <el-tag
              v-else
              type="info"
              size="small"
              style="margin-left: var(--app-space-3)"
            >
              使用LLM密钥
            </el-tag>
          </el-form-item>
          
          <el-form-item label="API地址">
            <el-input
              v-model="embeddingConfig.apiUrl"
              placeholder="留空则使用提供商默认地址"
              class="ctl"
            />
          </el-form-item>
          
          <el-form-item label="模型">
            <el-select
              v-model="embeddingConfig.model"
              placeholder="选择Embedding模型"
              class="ctl"
              filterable
              allow-create
            >
              <el-option
                label="BAAI/bge-large-zh-v1.5"
                value="BAAI/bge-large-zh-v1.5"
              />
              <el-option
                label="BAAI/bge-m3"
                value="BAAI/bge-m3"
              />
              <el-option
                label="text-embedding-ada-002"
                value="text-embedding-ada-002"
              />
              <el-option
                label="text-embedding-3-small"
                value="text-embedding-3-small"
              />
              <el-option
                label="text-embedding-3-large"
                value="text-embedding-3-large"
              />
            </el-select>
          </el-form-item>
          
          <p class="form-tip">有效凭据来源：{{ credentialSourceLabels[embeddingConfig.credentialSource] || '来源待核对' }}。同地址复用不代表已验证平台权限；旧共享来源待迁移至独立配置。</p>
          <el-form-item label="请求指定输出维度">
            <el-switch v-model="embeddingConfig.requestDimensions" />
            <div class="form-tip">仅平台和模型支持dimensions参数时开启；不开启也会核验返回维度。</div>
          </el-form-item>
          <el-form-item label="向量维度">
            <el-input-number
              v-model="embeddingConfig.dimension"
              :min="64"
              :max="4096"
              :step="64"
              class="ctl-xs"
            />
            <div class="form-tip">
              预期返回维度，不自动猜测或改写；与实际响应不一致时拒绝该批向量。
            </div>
          </el-form-item>
          
          <el-form-item>
            <el-button
              type="primary"
              :loading="saving"
              @click="saveEmbeddingSettings"
            >
              保存Embedding设置
            </el-button>
          </el-form-item>
        </el-form>
      </el-card>

      <!-- RAG Settings -->
      <el-card
        class="settings-card"
        shadow="never"
      >
        <template #header>
          <div class="card-header">
            <span><el-icon><Setting /></el-icon> RAG参数配置（农业检索）</span>
          </div>
        </template>
        
        <el-form
          label-width="140px"
          label-position="left"
        >
          <el-form-item label="检索数量 (Top-K)">
            <el-slider
              v-model="ragConfig.topK"
              :min="1"
              :max="20"
              :step="1"
              show-input
              class="ctl"
            />
            <div class="form-tip">
              农业检索返回数量上限；下次检索生效
            </div>
          </el-form-item>
          
          <el-form-item label="最低融合得分">
            <el-slider
              v-model="ragConfig.similarityThreshold"
              :min="0"
              :max="1"
              :step="0.05"
              show-input
              class="ctl"
            />
            <div class="form-tip">
              低于此融合得分的农业候选将被过滤；该分数不是正确率
            </div>
          </el-form-item>
          
          <el-form-item label="查询改写">
            <el-switch v-model="ragConfig.useRewrite" />
            <span class="switch-label">{{ ragConfig.useRewrite ? '启用' : '禁用' }}</span>
            <div class="form-tip">
              启用查询改写（停用词过滤 + 同义词扩展）；实际效果需评测
            </div>
          </el-form-item>
          
          <el-form-item label="知识上下文字符上限">
            <el-input-number
              v-model="ragConfig.maxContextLength"
              :min="500"
              :max="10000"
              :step="500"
              class="ctl-xs"
            />
            <span class="unit">字符</span>
            <div class="form-tip">
              限制传递给模型的知识检索内容字符数；田块、天气及问题正文单独保留
            </div>
          </el-form-item>
          
          <el-form-item>
            <el-button
              type="primary"
              :loading="saving"
              @click="saveRAGSettings"
            >
              保存RAG设置
            </el-button>
          </el-form-item>
        </el-form>
      </el-card>

      <!-- General Settings -->
      <el-card
        class="settings-card"
        shadow="never"
      >
        <template #header>
          <div class="card-header">
            <span><el-icon><Setting /></el-icon> 通用配置</span>
          </div>
        </template>
        
        <el-form
          label-width="140px"
          label-position="left"
        >
          <el-form-item label="系统时区">
            <el-select
              v-model="generalConfig.timezone"
              placeholder="选择时区"
              class="ctl-sm"
              filterable
              allow-create
            >
              <el-option
                v-for="tz in timezoneOptions"
                :key="tz"
                :label="tz"
                :value="tz"
              />
            </el-select>
            <div class="form-tip">
              所有时间显示将使用此时区，默认 Asia/Shanghai (UTC+8)
            </div>
          </el-form-item>
          
          <el-form-item>
            <el-button
              type="primary"
              :loading="saving"
              @click="saveGeneralSettings"
            >
              保存通用设置
            </el-button>
          </el-form-item>
        </el-form>
      </el-card>

    </div>
  </div>
</template>

<style scoped>
.settings-container {
  padding: var(--app-space-5) var(--app-space-6);
  max-width: 900px;
  margin: 0 auto;
}

.settings-card {
  margin-bottom: var(--app-space-5);
}

/* 核心服务卡：主色 emerald 浅阶描边 + 顶部分层，暗色下随令牌自动转深 */
.agri-core-card {
  border: 1px solid var(--el-color-primary-light-7);
  background: linear-gradient(180deg, var(--el-color-primary-light-9) 0%, var(--app-surface) 120px);
}

.card-header {
  display: flex;
  align-items: center;
}

.card-header span {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  font-weight: 500;
}

.section-title {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-2);
  font-weight: 600;
  color: var(--el-color-primary);
}

/* 表单控件宽度阶梯：min() 上限 + 100% 自适应，替代写死 px */
.ctl {
  width: min(420px, 100%);
}

.ctl-sm {
  width: min(280px, 100%);
}

.ctl-xs {
  width: min(200px, 100%);
}

/* 视觉模型列表筛选：独立第二行，带归属标签 */
.vision-filter-row {
  flex-basis: 100%;
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  margin-top: var(--app-space-2);
}

.vision-filter-label {
  flex-shrink: 0;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.form-tip {
  flex-basis: 100%;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin-top: var(--app-space-1);
}

.switch-label {
  margin-left: var(--app-space-3);
  font-size: var(--app-text-base);
  color: var(--app-text);
}

.unit {
  margin-left: var(--app-space-2);
  font-size: var(--app-text-base);
  color: var(--app-text-secondary);
}

/* Responsive */
@media (max-width: 768px) {
  .settings-container {
    padding: var(--app-space-4);
  }

  :deep(.el-form-item__content) {
    flex-wrap: wrap;
  }
}
</style>
