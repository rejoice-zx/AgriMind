<script setup lang="ts">
import AuthenticatedImage from '@/components/AuthenticatedImage.vue'
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useFarmSelectionStore } from '@/stores/farmSelection'
import FieldWeatherPanel from '@/components/FieldWeatherPanel.vue'
import AgriHistoryPanel from '@/components/AgriHistoryPanel.vue'
import { getAgriConversation, transferAgriTurn, getAgriConsultation, getAgentRequest, cancelAgentRequest } from '@/api/agriHistory'
import type { AgriConsultation } from '@/types/agriHistory'
import { newRequestId } from '@/utils/requestId'
import { useAuthStore } from '@/stores/auth'
import { draftOwnerKey, saveScopedDraft, readScopedDraft } from '@/utils/scopedDraft'
import { renderSafeMarkdown } from '@/utils/safeMarkdown'
import { ElMessage } from 'element-plus'
import {
  Position,
  Warning,
  CircleCheck,
  CircleClose,
  Document,
  Timer,
  InfoFilled,
  Picture,
  RefreshRight,
  RefreshLeft,
  Operation,
  Sunny,
  Close,
  MapLocation,
  Cpu,
  User,
  Reading,
  CollectionTag,
  Tools,
  FirstAidKit,
  MagicStick
} from '@element-plus/icons-vue'
import {
  getFieldsByFarm,
  getCropPlantings,
  getFarmingRecords,
  uploadDiagnosisImage,
  queryAgriAgentDecision
} from '@/api/agriculture'
import type {
  FieldItem,
  CropPlanting,
  FarmingRecord,
  AgentDecisionResponse,
  AgentDecisionRequest
} from '@/types/agriculture'

interface ChatMessage {
  id: string
  role: 'user' | 'agent'
  time: string
  content?: string
  decision?: AgentDecisionResponse
  loading?: boolean
  imageUrl?: string
  consultation?: AgriConsultation
  busy?: boolean
}

// ==================== 状态定义 ====================
const farmSelection = useFarmSelectionStore()
const { farms, selectedFarmId: currentFarmId } = storeToRefs(farmSelection)
let farmContextReady = false
watch(currentFarmId, id => { if (farmContextReady) void loadFields(id) }, { flush: 'sync' })
const fields = ref<FieldItem[]>([])
const selectedFieldId = ref<number | null>(null)
const activeCrop = ref<CropPlanting | null>(null)
const activeCrops = ref<CropPlanting[]>([])
function selectPlanting(id: number) { activeCrop.value = activeCrops.value.find(crop => crop.id === id) || null; retryRequest.value = null }
const recentRecords = ref<FarmingRecord[]>([])

const selectedField = computed(() => fields.value.find(field => field.id === selectedFieldId.value))
const sprayRecords = computed(() => recentRecords.value.filter(record => ['pest_control', 'spraying', '施药', '打药', '病虫害防治'].includes(record.record_type)))
let fieldRevision = 0
let farmRevision = 0

// 对话状态
const messages = ref<ChatMessage[]>([])
const inputQuery = ref('')
const inputImageUrl = ref('')
const sending = ref(false)
const historyLoading = ref(false)
const conversationId = ref<string>()
const conversationFieldId = ref<number | null>(null)
const turnPage = ref(1)
const turnTotal = ref(0)
const historyPanel = ref<InstanceType<typeof AgriHistoryPanel> | null>(null)
const retryRequest = ref<AgentDecisionRequest | null>(null)
const checkingRequest = ref(false)
let chatRevision = 0
const historyReadonly = computed(() => !!conversationId.value && conversationFieldId.value !== selectedFieldId.value)
const chatBodyRef = ref<HTMLElement | null>(null)
const draftAuth = useAuthStore()
const chatDraftNotice = ref('')
const chatDraftScope = () => `chat:${selectedFieldId.value || 'none'}:${activeCrop.value?.id || 'none'}`
let restoringChatDraft = false
watch([inputQuery, inputImageUrl, retryRequest], () => {
  if (restoringChatDraft) return
  const value = { query: inputQuery.value || retryRequest.value?.query || '', image: inputImageUrl.value,
    pending: retryRequest.value, conversation: conversationId.value }
  const hasDraft = value.query || value.image || value.pending
  const owner = draftOwnerKey(draftAuth.user)
  const saved = saveScopedDraft(owner, chatDraftScope(), hasDraft ? value : null)
  chatDraftNotice.value = owner && hasDraft ? (saved ? '本机草稿已保存，7天内可恢复' : '本机无法保存草稿，请保留页面并复制内容') : ''
}, { deep: true, flush: 'sync' })
async function restoreChatDraft() {
  if (sending.value || historyLoading.value) return
  const owner = draftOwnerKey(draftAuth.user)
  const scope = chatDraftScope()
  const saved = readScopedDraft<{ query: string; image: string; pending: AgentDecisionRequest | null; conversation?: string }>(owner, scope)
  if (!saved || typeof saved.query !== 'string' || typeof saved.image !== 'string') { ElMessage.info('当前账号和批次没有可恢复草稿'); return }
  if (saved.pending && (typeof saved.pending.request_id !== 'string' || saved.pending.field_id !== (selectedFieldId.value || undefined) || saved.pending.crop_planting_id !== activeCrop.value?.id)) { ElMessage.error('原提交标识与当前批次不一致，请先核对历史'); return }
  if (saved.conversation) await openConversation(saved.conversation)
  else if (conversationId.value) newConversation()
  if (owner !== draftOwnerKey(draftAuth.user) || scope !== chatDraftScope() || (saved.conversation && conversationId.value !== saved.conversation)) return
  restoringChatDraft = true
  inputQuery.value = saved.query; inputImageUrl.value = saved.image; retryRequest.value = saved.pending
  restoringChatDraft = false
  ElMessage.info('草稿已恢复；已提交的问题可先查对话档案，重试会复用原编号')
}
watch(() => draftOwnerKey(draftAuth.user), () => {
  restoringChatDraft = true; ++chatRevision
  sending.value = false
  inputQuery.value = ''; inputImageUrl.value = ''; retryRequest.value = null; messages.value = []
  conversationId.value = undefined; conversationFieldId.value = null
  restoringChatDraft = false
}, { flush: 'sync' })

// ==================== 输入区：表情选择 ====================
const inputRef = ref()
const emojiList = [
  '😊', '😂', '🤔', '👍', '🙏', '💪', '🌱', '🌾',
  '🍅', '🥒', '🍆', '🌶️', '🌽', '🥕', '🍇', '🍎',
  '🌳', '💧', '☀️', '🌧️', '⛅', '❄️', '🐛', '🦋',
  '🐝', '🌸', '🍀', '⚠️', '✅', '❌', '📌', '💡',
  '🔍', '🧪', '🚜', '📷', '❓', '🧑‍🌾', '🗓️', '📋'
]

// 在光标处插入表情（取不到 textarea 时追加到末尾）
function insertEmoji(emoji: string) {
  const el = inputRef.value?.textarea as HTMLTextAreaElement | undefined
  if (!el) {
    inputQuery.value += emoji
    return
  }
  const start = el.selectionStart ?? inputQuery.value.length
  const end = el.selectionEnd ?? start
  inputQuery.value = inputQuery.value.slice(0, start) + emoji + inputQuery.value.slice(end)
  nextTick(() => {
    el.focus()
    const pos = start + emoji.length
    el.setSelectionRange(pos, pos)
  })
}

// ==================== 建议提问：拖动 / 滚轮横向滚动 ====================
const promptChipsRef = ref<HTMLElement | null>(null)
const chipsDrag = { down: false, startX: 0, startScroll: 0, moved: false }

function onChipsPointerDown(e: PointerEvent) {
  const el = promptChipsRef.value
  if (!el || e.pointerType === 'touch') return // 触屏走原生滚动
  chipsDrag.down = true
  chipsDrag.startX = e.clientX
  chipsDrag.startScroll = el.scrollLeft
  chipsDrag.moved = false
  el.setPointerCapture(e.pointerId)
}

function onChipsPointerMove(e: PointerEvent) {
  if (!chipsDrag.down || !promptChipsRef.value) return
  const dx = e.clientX - chipsDrag.startX
  if (Math.abs(dx) > 4) chipsDrag.moved = true
  promptChipsRef.value.scrollLeft = chipsDrag.startScroll - dx
}

function onChipsPointerUp() {
  chipsDrag.down = false
}

// 拖动后抑制误触点击
function onChipsClickCapture(e: MouseEvent) {
  if (chipsDrag.moved) {
    e.stopPropagation()
    e.preventDefault()
    chipsDrag.moved = false
  }
}

// 纵向滚轮转横向滚动
function onChipsWheel(e: WheelEvent) {
  const el = promptChipsRef.value
  if (!el) return
  if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
    el.scrollLeft += e.deltaY
    e.preventDefault()
  }
}

function newConversation() {
  if (sending.value || historyLoading.value) return
  ++chatRevision
  conversationId.value = undefined
  conversationFieldId.value = null
  retryRequest.value = null
  turnPage.value = 1; turnTotal.value = 0
  messages.value = []
  inputImageUrl.value = ''
}
async function openConversation(id: string, page = 1) {
  if (sending.value) return
  const revision = ++chatRevision
  historyLoading.value = true
  try {
    const response = await getAgriConversation(id, { offset: (page - 1) * 50, limit: 50 })
    if (revision !== chatRevision) return
    conversationId.value = response.data.conversation.id
    conversationFieldId.value = response.data.conversation.field_ref
    turnPage.value = page; turnTotal.value = response.data.total
    retryRequest.value = null
    messages.value = response.data.items.flatMap(turn => {
      const time = new Date(turn.created_at).toLocaleString()
      return [{ id: `user_${turn.id}`, role: 'user' as const, time, content: turn.query, imageUrl: turn.image_url || undefined },
        { id: turn.id, role: 'agent' as const, time, decision: turn.decision || undefined,
          consultation: turn.consultation || undefined,
          content: turn.status === 'pending' ? '该提问正在处理中，请稍后刷新历史核对结果。' : turn.error || undefined }]
    })
    scrollToBottom()
  } catch (error: any) {
    if (revision === chatRevision) ElMessage.error(error.response?.data?.detail || '读取对话失败')
  } finally { if (revision === chatRevision) historyLoading.value = false }
}
async function consult(msg: ChatMessage) {
  if (!msg.decision?.turn_id || msg.busy) return
  msg.busy = true
  try {
    msg.consultation = (await transferAgriTurn(msg.decision.turn_id)).data
    msg.decision.consultation_id = msg.consultation.id
  } catch (error: any) { ElMessage.error(error.response?.data?.detail || '转交专家失败') }
  finally { msg.busy = false }
}
async function refreshConsultation(msg: ChatMessage) {
  const id = msg.consultation?.id || msg.decision?.consultation_id
  if (!id || msg.busy) return
  msg.busy = true
  try { msg.consultation = (await getAgriConsultation(id)).data }
  catch (error: any) { ElMessage.error(error.response?.data?.detail || '读取专家反馈失败') }
  finally { msg.busy = false }
}
const consultationStatus = (status?: string) => ({ pending: '待专家接单', in_review: '专家处理中', resolved: '专家已答复' }[status || 'pending'] || status)

// 快捷推荐问题列表
const quickPrompts = computed(() => {
  const crop = activeCrop.value?.crop || '当前作物'
  const stage = activeCrop.value?.growth_stage
  return [
    `请核对${crop}近期作业的天气风险`,
    `${crop}${stage ? `在${stage}` : ''}的水肥管理需要补充哪些资料？`,
    `${crop}出现异常叶片，需要拍摄和记录哪些症状？`,
    `请检查${crop}近期农事记录中的注意事项`
  ]
})

// ==================== 初始化与联动 ====================

async function initData() {
  try {
    if (!await farmSelection.loadFarms()) return
    farmContextReady = true
    await loadFields(currentFarmId.value)
  } catch (err: any) {
    console.warn('获取农场信息失败:', err)
  }

  // 默认欢迎与引导消息
  messages.value.push({
    id: 'welcome',
    role: 'agent',
    time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    content: `👋 您好！我是 AgriMind 农业智能决策 Agent。
请选择您的真实田块。我会依据已录入档案、可用天气与知识库分析；缺少数据时会明确说明。
您可以点击输入框上方的快捷提问，或直接输入关于病害防治、用药禁忌、水肥配方与物候积温的任何咨询！`
  })
}

async function loadFields(farmId: number | null) {
  newConversation()
  const revision = ++farmRevision
  ++fieldRevision
  fields.value = []; selectedFieldId.value = null; activeCrop.value = null; activeCrops.value = []; recentRecords.value = []
  if (farmId === null) return
  try {
    const res = await getFieldsByFarm(farmId)
    if (revision !== farmRevision) return
    fields.value = res.data.items || []
    if (fields.value.length) {
      selectedFieldId.value = fields.value[0].id
      await loadFieldDetail(selectedFieldId.value)
    }
  } catch { ElMessage.error('加载田块失败') }
}
async function handleFieldSelect(fieldId: number) {
  newConversation()
  selectedFieldId.value = fieldId
  await loadFieldDetail(fieldId)
}
async function loadFieldDetail(fieldId: number) {
  const revision = ++fieldRevision
  activeCrop.value = null; activeCrops.value = []; recentRecords.value = []
  const [crop, records] = await Promise.allSettled([getCropPlantings(fieldId, 'active'), getFarmingRecords(fieldId)])
  if (revision !== fieldRevision || fieldId !== selectedFieldId.value) return
  if (crop.status === 'fulfilled') {
    activeCrops.value = [...(crop.value.data.items || [])].sort((a, b) => b.planting_date.localeCompare(a.planting_date) || b.id - a.id)
    activeCrop.value = activeCrops.value.length === 1 ? activeCrops.value[0] : null
  }
  if (records.status === 'fulfilled') recentRecords.value = records.value.data || []
}
async function uploadChatImage(options: any) {
  try {
    const response = await uploadDiagnosisImage(options.file)
    inputImageUrl.value = response.data.url
    options.onSuccess?.(response.data)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '图片上传失败')
    options.onError?.(error)
  }
}

// ==================== 发送与决策交互 ====================

function applyPrompt(prompt: string) {
  inputQuery.value = prompt
}

async function handleSendMessage() {
  const query = inputQuery.value.trim()
  if (activeCrops.value.length > 1 && !activeCrop.value) { ElMessage.error('请先选择本次咨询的作物批次'); return }
  if (!query || sending.value || historyLoading.value || historyReadonly.value || (conversationId.value && turnPage.value !== 1)) return
  const revision = chatRevision
  const draft: AgentDecisionRequest = { query, field_id: selectedFieldId.value || undefined,
    crop_planting_id: activeCrop.value?.id,
    image_url: inputImageUrl.value || undefined, enable_multimodal: true, conversation_id: conversationId.value }
  const old = retryRequest.value
  const payload: AgentDecisionRequest = { ...draft, request_id: old && old.query === draft.query && old.field_id === draft.field_id && old.crop_planting_id === draft.crop_planting_id && old.image_url === draft.image_url && old.conversation_id === draft.conversation_id ? old.request_id : newRequestId() }
  retryRequest.value = payload

  const userTime = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const userMsgId = 'user_' + Date.now()
  messages.value.push({
    id: userMsgId,
    role: 'user',
    time: userTime,
    content: query,
    imageUrl: payload.image_url
  })

  inputQuery.value = ''
  sending.value = true
  scrollToBottom()

  // 占位等待消息
  const agentMsgId = 'agent_' + Date.now()
  messages.value.push({
    id: agentMsgId,
    role: 'agent',
    time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    loading: true
  })
  scrollToBottom()

  try {
    const res = await queryAgriAgentDecision(payload)
    if (revision !== chatRevision) return
    retryRequest.value = null
    conversationId.value = res.data.conversation_id || conversationId.value
    conversationFieldId.value = payload.field_id ?? null
    historyPanel.value?.reload()

    // 更新回复
    const targetMsg = messages.value.find(m => m.id === agentMsgId)
    if (targetMsg) {
      targetMsg.loading = false
      targetMsg.decision = res.data
      if (res.data.turn_id) targetMsg.id = res.data.turn_id
      if (res.data.consultation_id) await refreshConsultation(targetMsg)
    }
  } catch (err: any) {
    if (revision !== chatRevision) return
    if ([400, 401, 403, 404, 422].includes(err.response?.status)) retryRequest.value = null
    const targetMsg = messages.value.find(m => m.id === agentMsgId)
    if (targetMsg) {
      targetMsg.loading = false
      targetMsg.content = `决策分析遇到异常: ${err.response?.data?.detail || err.message || '系统服务繁忙'}`
    }
    ElMessage.error('未收到结果，分析可能仍在继续；请查询原结果，重试会复用请求编号')
    if (!inputQuery.value) inputQuery.value = query
  } finally {
    if (revision === chatRevision) sending.value = false
    scrollToBottom()
  }
  // Reload the persisted page after success, eliminating duplicate placeholders on replay.
  if (revision === chatRevision && !retryRequest.value && conversationId.value) {
    await openConversation(conversationId.value)
  }
}

async function queryOriginalAnalysis(cancel = false) {
  const pending = retryRequest.value
  if (!pending?.request_id || checkingRequest.value) return
  const owner = draftOwnerKey(draftAuth.user), revision = chatRevision
  checkingRequest.value = true
  try {
    const response = await (cancel ? cancelAgentRequest(pending.request_id) : getAgentRequest(pending.request_id))
    if (revision !== chatRevision || owner !== draftOwnerKey(draftAuth.user) || retryRequest.value?.request_id !== pending.request_id) return
    ElMessage.info(response.data.message)
    if (response.data.status !== 'pending') {
      ++chatRevision
      sending.value = false
      retryRequest.value = null
      await openConversation(response.data.conversation_id)
      historyPanel.value?.reload()
    }
  } catch (error: any) {
    if (revision === chatRevision && owner === draftOwnerKey(draftAuth.user)) ElMessage.error(error.response?.data?.detail || '查询原回执失败，请保留原编号稍后查询')
  } finally { checkingRequest.value = false }
}

function scrollToBottom() {
  nextTick(() => {
    if (chatBodyRef.value) {
      chatBodyRef.value.scrollTop = chatBodyRef.value.scrollHeight
    }
  })
}

// 格式化置信度百分比
function formatConfidence(conf: number) {
  return `${Math.round(conf * 100)}%`
}

// ==================== 响应式侧栏与自适应布局 ====================
const leftSidebarOpen = ref(true)
const rightSidebarOpen = ref(true)
const isCompact = ref(false)
const isMobile = ref(false)

function toggleLeftSidebar() {
  if (isCompact.value && !leftSidebarOpen.value) {
    rightSidebarOpen.value = false
  }
  leftSidebarOpen.value = !leftSidebarOpen.value
}

function toggleRightSidebar() {
  if (isCompact.value && !rightSidebarOpen.value) {
    leftSidebarOpen.value = false
  }
  rightSidebarOpen.value = !rightSidebarOpen.value
}

function checkResponsive() {
  if (typeof window === 'undefined') return
  const w = window.innerWidth
  isMobile.value = w < 768
  isCompact.value = w < 1200
  if (w < 1100) {
    leftSidebarOpen.value = false
    rightSidebarOpen.value = false
  } else if (w < 1440) {
    leftSidebarOpen.value = true
    rightSidebarOpen.value = false
  } else {
    leftSidebarOpen.value = true
    rightSidebarOpen.value = true
  }
}

onMounted(() => {
  initData()
  checkResponsive()
  window.addEventListener('resize', checkResponsive)
})
onBeforeUnmount(() => {
  ++chatRevision; ++farmRevision; ++fieldRevision
  window.removeEventListener('resize', checkResponsive)
})
</script>

<template>
  <div class="agri-chat-container">
    <!-- 移动端/窄屏遮罩层 -->
    <div
      v-if="isCompact && (leftSidebarOpen || rightSidebarOpen)"
      class="sidebar-backdrop"
      @click="leftSidebarOpen = false; rightSidebarOpen = false"
    />

    <!-- 左栏：时空田块上下文与快捷问题 -->
    <aside
      class="left-sidebar"
      :class="{
        'sidebar-collapsed': !leftSidebarOpen,
        'drawer-mode': isCompact,
        'drawer-open': isCompact && leftSidebarOpen
      }"
    >
      <div class="sidebar-header">
        <span class="sidebar-title"><el-icon><MapLocation /></el-icon>田块时空上下文</span>
        <div class="header-tags">
          <el-tag size="small" type="success" effect="plain">自动注入</el-tag>
          <el-button
            v-if="isCompact"
            circle
            size="small"
            :icon="Close"
            class="close-drawer-btn"
            title="关闭侧栏"
            @click="leftSidebarOpen = false"
          />
        </div>
      </div>

      <!-- 农场与田块选择 -->
      <div class="selector-box">
        <label class="field-label">所属农场</label>
        <el-select
          v-model="currentFarmId"
          :disabled="sending || historyLoading"
          size="default"
          style="width: 100%"
        >
          <el-option
            v-for="farm in farms"
            :key="farm.id"
            :label="farm.name"
            :value="farm.id"
          />
        </el-select>

        <label class="field-label" style="margin-top: 12px">目标田块 / 温室大棚</label>
        <el-select
          v-model="selectedFieldId"
          :disabled="sending || historyLoading"
          size="default"
          style="width: 100%"
          @change="handleFieldSelect"
        >
          <el-option
            v-for="field in fields"
            :key="field.id"
            :label="`${field.name} (${field.area}亩)`"
            :value="field.id"
          />
        </el-select>
      </div>

      <!-- 当前选中田块画像摘要 -->
      <div class="field-card-preview" v-if="selectedFieldId">
        <div class="preview-row">
          <span class="pk">当前作物:</span>
          <span class="pv highlight">{{ activeCrop ? `${activeCrop.crop} - ${activeCrop.variety || '品种未录入'}` : '未录入活跃作物' }}</span>
        </div>
        <div class="preview-row">
          <span class="pk">生长阶段:</span>
          <el-tag size="small" type="warning" effect="dark">{{ activeCrop?.growth_stage || '阶段未录入' }}</el-tag>
        </div>
        <div class="preview-row">
          <span class="pk">土壤类型:</span>
          <span class="pv">{{ selectedField?.soil_type || '未录入' }}</span>
        </div>
        <div class="preview-row">
          <span class="pk">定植日期:</span>
          <span class="pv">{{ activeCrop?.planting_date || '未录入' }}</span>
        </div>
      </div>

      <!-- 快捷提问推荐 -->
      <AgriHistoryPanel ref="historyPanel" :disabled="sending || historyLoading" @select="openConversation" @create="newConversation" />
    </aside>

    <!-- 中栏：智能 Agent 决策交互流 (Flex-1) -->
    <main class="chat-main-column">
      <!-- 顶部指示条与自适应侧栏切换开关 -->
      <div class="chat-header">
        <div class="header-toggle-left">
          <el-tooltip :content="leftSidebarOpen ? '收起田块档案栏' : '展开田块档案栏'" placement="bottom">
            <el-button
              size="small"
              class="sidebar-pill-btn"
              :class="{ active: leftSidebarOpen }"
              @click="toggleLeftSidebar"
            >
              <el-icon><Operation /></el-icon>
              <span class="btn-text">田块档案</span>
            </el-button>
          </el-tooltip>
        </div>

        <div class="agent-identity">
          <div class="agent-avatar"><el-icon><Cpu /></el-icon></div>
          <div class="agent-info-text">
            <div class="agent-name-row">
              <span class="agent-name">AgriMind 农业智能决策协同中枢</span>
              <el-tag type="success" size="small" effect="light" class="react-tag">多工具协同 ReAct 模式</el-tag>
            </div>
            <div class="agent-desc">支持气象安全硬阻断 · 科学水肥建议 · 国家标准溯源 · 工具链全流程透明审计</div>
          </div>
        </div>

        <div class="header-toggle-right">
          <el-tooltip :content="rightSidebarOpen ? '收起气象与履历栏' : '展开气象与履历栏'" placement="bottom">
            <el-button
              size="small"
              class="sidebar-pill-btn"
              :class="{ active: rightSidebarOpen }"
              @click="toggleRightSidebar"
            >
              <el-icon><Sunny /></el-icon>
              <span class="btn-text">气象与履历</span>
            </el-button>
          </el-tooltip>
        </div>
      </div>

      <div v-if="conversationId" class="archive-toolbar">
        <el-tag size="small" type="info" effect="plain" class="archive-tag">已归档</el-tag>
        <span class="archive-page-info">第 {{ turnPage }} 页<small>（最新在第 1 页）</small></span>
        <el-tooltip content="历史分析仅反映当时资料，实施前需重新核对天气、投入品标签和现场情况" placement="bottom">
          <el-icon class="archive-info-icon"><InfoFilled /></el-icon>
        </el-tooltip>
        <div class="archive-toolbar-right">
          <el-button size="small" text :icon="RefreshRight" :disabled="sending || historyLoading" @click="openConversation(conversationId, turnPage)">刷新</el-button>
          <el-pagination v-model:current-page="turnPage" small layout="prev, pager, next" :page-size="50" :total="turnTotal" :disabled="sending || historyLoading" @current-change="openConversation(conversationId!, $event)" />
        </div>
      </div>
      <p v-if="conversationId && historyReadonly" class="archive-warn">此档案绑定田块 #{{ conversationFieldId ?? '无' }}，与当前选择不同。请先选择原田块再打开档案，或新建对话。</p>
      <p v-if="conversationId && turnPage !== 1" class="archive-warn">请回到第 1 页后继续提问。</p>
      <!-- 对话主体流 -->
      <div class="chat-messages-body" ref="chatBodyRef">
        <div
          v-for="msg in messages"
          :key="msg.id"
          class="message-row"
          :class="msg.role"
        >
          <!-- 头像 -->
          <div class="msg-avatar">
            <el-icon v-if="msg.role === 'user'"><User /></el-icon>
            <el-icon v-else><Cpu /></el-icon>
          </div>

          <!-- 消息体 -->
          <div class="msg-content-wrapper">
            <div class="msg-meta">
              <span class="sender-name">{{ msg.role === 'user' ? '我 (农户/农技员)' : 'AgriMind 决策中枢' }}</span>
              <span class="msg-time">{{ msg.time }}</span>
            </div>

            <!-- 普通文字回复或加载中 -->
            <div v-if="msg.loading" class="loading-bubble">
              <el-icon class="is-loading" style="font-size: 18px"><RefreshRight /></el-icon>
              <span>正在自主协同多工具分析地块档案、微气象预警与国标防治规程...</span>
            </div>

            <div v-else-if="msg.content" class="text-bubble">
              <div class="markdown-body" v-html="renderSafeMarkdown(msg.content)" />
            </div>

            <!-- 结构化决策卡片 -->
            <div v-else-if="msg.decision" class="decision-card">
              <!-- 核心裁决 Banner -->
              <div
                class="verdict-banner"
                :class="msg.decision.can_proceed ? 'allow' : 'block'"
              >
                <div class="verdict-main">
                  <el-icon class="verdict-icon">
                    <CircleCheck v-if="msg.decision.can_proceed" />
                    <CircleClose v-else />
                  </el-icon>
                  <div>
                    <div class="verdict-title">
                      {{ msg.decision.can_proceed ? '一般管理建议，仍须现场核对' : '暂缓实施 / 需人工复核' }}
                    </div>
                    <div class="verdict-desc markdown-body" v-html="renderSafeMarkdown(msg.decision.conclusion)" />
                  </div>
                </div>

                <div class="confidence-badge">
                  <span class="conf-label">置信度</span>
                  <span class="conf-val">{{ formatConfidence(msg.decision.confidence) }}</span>
                </div>
              </div>

              <!-- 风险告警 -->
              <div v-if="msg.decision.risks && msg.decision.risks.length > 0" class="card-section alert-section">
                <div class="section-badge warn">
                  <el-icon><Warning /></el-icon>
                  <span>潜在农事与灾害风险 ({{ msg.decision.risks.length }})</span>
                </div>
                <ul class="risk-list">
                  <li v-for="(risk, rIdx) in msg.decision.risks" :key="rIdx"><div class="markdown-body" v-html="renderSafeMarkdown(risk)" /></li>
                </ul>
              </div>

              <!-- 具体落地指导建议 -->
              <div v-if="msg.decision.recommendations && msg.decision.recommendations.length > 0" class="card-section">
                <div class="section-badge rec">
                  <el-icon><CircleCheck /></el-icon>
                  <span>科学处置建议与实操规程</span>
                </div>
                <div class="rec-steps">
                  <div
                    v-for="(rec, idx) in msg.decision.recommendations"
                    :key="idx"
                    class="rec-step-item"
                  >
                    <span class="step-num">{{ idx + 1 }}</span>
                    <div class="step-text markdown-body" v-html="renderSafeMarkdown(rec)" />
                  </div>
                </div>
              </div>

              <!-- 技术依据与国标标准展开 -->
              <el-collapse class="details-collapse">
                <el-collapse-item name="evidences">
                  <template #title>
                    <span class="collapse-title"><el-icon><Reading /></el-icon>支撑依据与国家标准 (GB/T) 溯源</span>
                  </template>
                  <div v-if="msg.decision.evidences && msg.decision.evidences.length > 0" class="evidences-list">
                    <div
                      v-for="(evi, eIdx) in msg.decision.evidences"
                      :key="eIdx"
                      class="evidence-item"
                    >
                      <span class="evi-title"><el-icon><CollectionTag /></el-icon>{{ evi.standard_no || evi.title || '规范规程' }}</span>
                      <div class="evi-desc markdown-body" v-html="renderSafeMarkdown(evi.summary || evi.content || evi.desc || '')" />
                    </div>
                  </div>
                  <div v-else class="text-muted">暂无外挂标准引用</div>
                </el-collapse-item>

                <!-- 工具链调用审计日志 -->
                <el-collapse-item name="traces">
                  <template #title>
                    <span class="collapse-title"><el-icon><Tools /></el-icon>Agent 工具调用透明审计跟踪 (Tool Traces)</span>
                  </template>
                  <div class="tool-traces-list">
                    <div
                      v-for="(trace, tIdx) in msg.decision.tool_traces"
                      :key="tIdx"
                      class="trace-item"
                    >
                      <div class="trace-header">
                        <span class="trace-tool-name"><code>{{ trace.tool_name }}</code></span>
                        <el-tag size="small" type="success">{{ trace.status }}</el-tag>
                      </div>
                      <div class="trace-body">
                        <div class="trace-sub"><span class="tk">入参:</span> <code>{{ JSON.stringify(trace.arguments) }}</code></div>
                        <div v-if="trace.execution_metadata?.effective_parameters" class="trace-sub"><span class="tk">本次检索参数:</span> <span>{{ JSON.stringify(trace.execution_metadata.effective_parameters) }}；候选 {{ trace.execution_metadata.eligible_candidates }} 条；{{ trace.execution_metadata.retrieval_path }}；来源标注未自动核验</span></div>
                        <div class="trace-sub"><span class="tk">结果概要:</span> <span>{{ typeof trace.result_summary === 'object' ? JSON.stringify(trace.result_summary) : trace.result_summary }}</span></div>
                      </div>
                    </div>
                  </div>
                </el-collapse-item>
              </el-collapse>

              <!-- 专家转接提示 -->
              <div v-if="msg.decision.need_expert" class="expert-turn-notice">
                <el-icon><InfoFilled /></el-icon>
                <span>{{ msg.decision.consultation_id ? '已创建农业咨询，等待专家处理。' : '建议农技专家复核，可转交本次提问。' }}</span>
                <router-link to="/diagnosis">进入会诊</router-link>
              </div>
            </div>
            <AuthenticatedImage v-if="msg.imageUrl" :src="msg.imageUrl" alt="本次提交的现场图片" style="max-width:240px;max-height:180px" />
            <div v-if="msg.decision?.turn_id" class="consultation-feedback">
              <template v-if="msg.decision.consultation_id || msg.consultation">
                <p>{{ consultationStatus(msg.consultation?.status) }} · {{ msg.consultation?.id || msg.decision.consultation_id }}</p>
                <div v-if="msg.consultation?.answer"><strong>专家答复：</strong><div class="markdown-body" v-html="renderSafeMarkdown(msg.consultation.answer)" /></div>
                <p v-if="msg.consultation?.answer">{{ msg.consultation.workflow_verified ? `签署人：${msg.consultation.reviewer_name} · ${msg.consultation.reviewed_at}` : '历史答复缺少本流程签署凭据' }}。答复保留当时结论，不能自动视为当前作业许可。</p>
                <el-button size="small" :loading="msg.busy" @click="refreshConsultation(msg)">刷新专家反馈</el-button>
              </template>
              <el-button v-else size="small" :loading="msg.busy" @click="consult(msg)">转交农技专家</el-button>
            </div>
          </div>
        </div>
      </div>

      <!-- 底部输入框 -->
      <div class="chat-input-bar">
        <el-select v-if="activeCrops.length > 1" :model-value="activeCrop?.id" :disabled="sending || historyLoading || historyReadonly" @change="selectPlanting" style="width:100%;margin-bottom:8px" aria-label="选择作物批次">
          <el-option v-for="crop in activeCrops" :key="crop.id" :value="crop.id" :label="`${crop.crop} · ${crop.variety || '品种未录入'} · 批次#${crop.id}`" />
        </el-select>
        <div class="prompt-chips-container" v-if="quickPrompts.length > 0">
          <div class="prompt-lead">
            <span class="prompt-lead-text"><el-icon><MagicStick /></el-icon>建议提问</span>
          </div>
          <div
            ref="promptChipsRef"
            class="prompt-chips input-prompts"
            aria-label="场景提问模板"
            @pointerdown="onChipsPointerDown"
            @pointermove="onChipsPointerMove"
            @pointerup="onChipsPointerUp"
            @pointercancel="onChipsPointerUp"
            @wheel="onChipsWheel"
            @click.capture="onChipsClickCapture"
          >
            <el-button
              v-for="prompt in quickPrompts"
              :key="prompt"
              round
              size="small"
              class="quick-prompt-btn"
              :disabled="sending || historyLoading || historyReadonly"
              @click="applyPrompt(prompt)"
            >
              {{ prompt }}
            </el-button>
          </div>
        </div>
        <!-- 草稿状态提示（有内容时才显示） -->
        <p v-if="chatDraftNotice" class="draft-notice">{{ chatDraftNotice }}</p>
        <!-- 输入框：上传图片 / 表情 / 发送集成在框内右侧 -->
        <div class="input-box">
          <div v-if="inputImageUrl" class="input-image-preview">
            <AuthenticatedImage :src="inputImageUrl" alt="已上传现场图片" class="preview-thumb" />
            <el-button text size="small" :icon="Close" class="preview-remove" title="移除图片" @click="inputImageUrl = ''" />
          </div>
          <el-input
            ref="inputRef"
            v-model="inputQuery"
            placeholder="请在此输入您的农事咨询，如：明天上午是否适合喷洒晚疫病药剂？"
            :rows="2"
            type="textarea"
            resize="none"
            class="main-textarea"
            @keydown.enter.prevent="handleSendMessage"
          />
          <div class="input-toolbar">
            <el-button text class="icon-btn restore-draft-btn" :icon="RefreshLeft" :disabled="sending || historyLoading" title="恢复本机草稿 / 原提问编号" @click="restoreChatDraft" />
            <template v-if="retryRequest">
              <el-button text :loading="checkingRequest" @click="queryOriginalAnalysis()">查询原结果</el-button>
              <el-button text :disabled="checkingRequest" @click="queryOriginalAnalysis(true)">取消本次分析</el-button>
            </template>
            <el-popover placement="top-end" :width="308" trigger="click" popper-class="emoji-popper">
              <template #reference>
                <el-button text class="icon-btn emoji-trigger" :disabled="sending || historyLoading || historyReadonly" title="插入表情">😊</el-button>
              </template>
              <div class="emoji-grid">
                <span v-for="emoji in emojiList" :key="emoji" class="emoji-item" @click="insertEmoji(emoji)">{{ emoji }}</span>
              </div>
            </el-popover>
            <el-upload :http-request="uploadChatImage" :show-file-list="false" accept="image/jpeg,image/png,image/webp" :disabled="sending || historyLoading || historyReadonly">
              <el-button text class="icon-btn" :icon="Picture" :disabled="sending || historyLoading || historyReadonly" title="上传现场图片" />
            </el-upload>
            <el-button
              type="primary"
              class="send-btn"
              :loading="sending"
              :disabled="historyLoading || historyReadonly || (conversationId && turnPage !== 1)"
              :icon="Position"
              @click="handleSendMessage"
            >
              提问决策
            </el-button>
          </div>
        </div>
      </div>
    </main>

    <!-- 右栏：实时微气象传感器与近期农事履历 -->
    <aside
      class="right-sidebar"
      :class="{
        'sidebar-collapsed': !rightSidebarOpen,
        'drawer-mode': isCompact,
        'drawer-open': isCompact && rightSidebarOpen
      }"
    >
      <div class="sidebar-header right-sidebar-header" v-if="isCompact">
        <span class="sidebar-title"><el-icon><Sunny /></el-icon>微气象与用药履历</span>
        <el-button
          circle
          size="small"
          :icon="Close"
          class="close-drawer-btn"
          title="关闭侧栏"
          @click="rightSidebarOpen = false"
        />
      </div>

      <div class="side-card"><FieldWeatherPanel :field-id="selectedFieldId" /></div>

      <!-- 近期农事施药与安全间隔期 -->
      <div class="side-card">
        <div class="side-card-title">
          <el-icon><Timer /></el-icon>
          <span>近期植保与用药履历 (PHI)</span>
        </div>

        <div v-if="sprayRecords.length === 0" class="empty-rec">
          当前地块近期无植保用药记录
        </div>
        <div v-else class="rec-list">
          <div
            v-for="rec in sprayRecords.slice(0, 3)"
            :key="rec.id"
            class="mini-rec-item"
          >
            <div class="mini-rec-head">
              <span class="rec-name"><el-icon><FirstAidKit /></el-icon>{{ rec.chemical_name || '投入品未录入' }}</span>
              <span class="rec-date">{{ rec.operation_date }}</span>
            </div>
            <div class="mini-rec-detail">
              <span>剂量: {{ rec.dosage || '未录入' }}</span>
              <el-tag size="small" type="danger" effect="light" v-if="rec.safety_interval_days">
                安全期: {{ rec.safety_interval_days }}天
              </el-tag>
            </div>
          </div>
        </div>
      </div>

      <!-- 物候有效积温 GDD 预测 -->
      <div class="side-card">
        <div class="side-card-title">
          <el-icon><Document /></el-icon>
          <span>有效积温 (GDD) 采收预测</span>
        </div>

        <p>尚缺定植以来完整历史温度序列，当前无法计算累计有效积温或预测采收天数。</p>
      </div>
    </aside>
  </div>
</template>

<style scoped>
/* 建议提问横向滑轨容器 (Art Design Pro 规范) */
.prompt-chips-container {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  overflow: hidden;
  padding: 2px 0;
}

.prompt-lead {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}

.prompt-lead-text {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-1);
  font-size: var(--app-text-xs);
  font-weight: 600;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-7);
  padding: 3px var(--app-space-2);
  border-radius: var(--app-radius-sm);
  white-space: nowrap;
}

.prompt-chips.input-prompts {
  display: flex !important;
  flex-direction: row !important;
  flex-wrap: nowrap !important;
  overflow-x: auto !important;
  gap: 8px;
  margin-bottom: 0 !important;
  padding: 2px 0;
  cursor: grab;
  /* 隐藏滚动条但保留滑动（拖拽/滚轮/触屏均可用） */
  scrollbar-width: none;
  -ms-overflow-style: none;
}

.prompt-chips.input-prompts:active {
  cursor: grabbing;
}

.prompt-chips.input-prompts::-webkit-scrollbar {
  display: none;
}

.prompt-chips.input-prompts :deep(.el-button),
.prompt-chips.input-prompts .quick-prompt-btn {
  flex-shrink: 0;
  white-space: nowrap !important;
  height: 28px;
  padding: 0 14px;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  background: var(--app-surface-2);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-round);
  transition: var(--app-transition);
  margin-left: 0 !important;
}

.prompt-chips.input-prompts :deep(.el-button:hover),
.prompt-chips.input-prompts .quick-prompt-btn:hover {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
  color: var(--el-color-primary);
  transform: translateY(-1px);
  box-shadow: var(--app-shadow-sm);
}

/* 归档视图顶部条：单行紧凑，释放对话区空间 */
.archive-toolbar {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  padding: 6px var(--app-space-4);
  background: var(--app-surface);
  border-bottom: 1px solid var(--app-border-light);
  color: var(--app-text-secondary);
  font-size: var(--app-text-xs);
}

.archive-tag {
  flex-shrink: 0;
}

.archive-page-info {
  white-space: nowrap;
}

.archive-info-icon {
  color: var(--app-text-placeholder);
  cursor: help;
  flex-shrink: 0;
}

.archive-toolbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}

.archive-warn {
  margin: 0;
  padding: 6px var(--app-space-4);
  background: var(--el-color-warning-light-9);
  border-bottom: 1px solid var(--app-border-light);
  color: var(--el-color-warning-dark-2);
  font-size: var(--app-text-xs);
}

.consultation-feedback {
  padding: var(--app-space-3);
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-md);
  margin-top: var(--app-space-2);
}

.agri-chat-container {
  position: relative;
  display: flex;
  width: 100%;
  max-width: 100%;
  height: calc(100vh - 60px);
  height: calc(100dvh - 60px);
  background-color: var(--app-bg);
  overflow: hidden;
  box-sizing: border-box;
}

/* 抽屉半透明遮罩 */
.sidebar-backdrop {
  position: fixed;
  inset: 0;
  background: var(--el-overlay-color-lighter);
  backdrop-filter: blur(2px);
  z-index: 150;
  transition: opacity 0.25s ease;
}

/* ================= 左栏 ================= */
.left-sidebar {
  width: 250px;
  min-width: 250px;
  flex-shrink: 0;
  background: var(--app-surface);
  border-right: 1px solid var(--app-border-light);
  display: flex;
  flex-direction: column;
  padding: var(--app-space-4);
  overflow-y: auto;
  transition: width 0.25s cubic-bezier(0.4, 0, 0.2, 1), min-width 0.25s, padding 0.25s, opacity 0.2s;
  box-sizing: border-box;
}

.left-sidebar.sidebar-collapsed {
  width: 0 !important;
  min-width: 0 !important;
  padding: 0 !important;
  opacity: 0 !important;
  overflow: hidden !important;
  border-right: none !important;
  pointer-events: none;
}

/* 窄屏与移动端抽屉模式 */
.left-sidebar.drawer-mode {
  position: fixed;
  top: 0;
  bottom: 0;
  left: 0;
  z-index: 200;
  width: 290px !important;
  min-width: 290px !important;
  box-shadow: var(--app-shadow-lg);
  transform: translateX(-100%);
  transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
}

.left-sidebar.drawer-mode.drawer-open {
  transform: translateX(0);
  opacity: 1 !important;
  pointer-events: auto;
}

.sidebar-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}

.sidebar-title {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-1);
  font-size: var(--app-text-base);
  font-weight: 600;
  color: var(--app-text);
}

.header-tags {
  display: flex;
  align-items: center;
  gap: 6px;
}

.close-drawer-btn {
  width: 24px;
  height: 24px;
  padding: 0;
}

.selector-box {
  margin-bottom: 14px;
}

.field-label {
  display: block;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin-bottom: var(--app-space-1);
  font-weight: 500;
}

.field-card-preview {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-md);
  padding: 10px var(--app-space-3);
  margin-bottom: 14px;
}

.preview-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: var(--app-text-xs);
  margin-bottom: 6px;
}

.preview-row:last-child {
  margin-bottom: 0;
}

.preview-row .pk {
  color: var(--app-text-placeholder);
}

.preview-row .pv {
  color: var(--app-text);
  font-weight: 500;
}

.preview-row .pv.highlight {
  color: var(--el-color-primary);
  font-weight: 600;
}

/* ================= 中栏 ================= */
.chat-main-column {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--app-bg);
  position: relative;
  overflow: hidden;
}

.chat-header {
  background: var(--app-surface);
  padding: 10px var(--app-space-4);
  border-bottom: 1px solid var(--app-border-light);
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--app-space-3);
  box-shadow: var(--app-shadow-sm);
  flex-shrink: 0;
}

.header-toggle-left, .header-toggle-right {
  flex-shrink: 0;
}

.sidebar-pill-btn {
  height: 32px;
  padding: 0 10px;
  border-radius: var(--app-radius-md);
  border: 1px solid var(--app-border);
  color: var(--app-text-secondary);
  background: var(--app-surface);
  font-size: var(--app-text-xs);
  transition: var(--app-transition);
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.sidebar-pill-btn:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-7);
  background: var(--el-color-primary-light-9);
}

.sidebar-pill-btn.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-7);
}

.agent-identity {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.agent-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-7);
  color: var(--el-color-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--app-text-xl);
  flex-shrink: 0;
}

.agent-info-text {
  min-width: 0;
}

.agent-name-row {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}

.agent-name {
  font-size: var(--app-text-base);
  font-weight: 700;
  color: var(--app-text);
  white-space: nowrap;
}

.react-tag {
  font-size: var(--app-text-xs);
}

.agent-desc {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-messages-body {
  flex: 1;
  padding: var(--app-space-5);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--app-space-5);
}

.message-row {
  display: flex;
  gap: var(--app-space-3);
  max-width: 88%;
}

.message-row.user {
  align-self: flex-end;
  flex-direction: row-reverse;
}

.message-row.agent {
  align-self: flex-start;
}

.msg-avatar {
  width: 38px;
  height: 38px;
  border-radius: 50%;
  background: var(--app-surface-2);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--app-text-2xl);
  flex-shrink: 0;
  border: 1px solid var(--app-border);
  color: var(--app-text-secondary);
}

.message-row.user .msg-avatar {
  background: var(--el-color-primary-light-8);
  border-color: var(--el-color-primary-light-7);
  color: var(--el-color-primary-dark-2);
}

.msg-content-wrapper {
  display: flex;
  flex-direction: column;
}

.message-row.user .msg-content-wrapper {
  align-items: flex-end;
}

.msg-meta {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
  margin-bottom: var(--app-space-1);
  display: flex;
  gap: var(--app-space-2);
}

.text-bubble {
  background: var(--app-surface);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-xl) var(--app-radius-xl) var(--app-radius-xl) var(--app-radius-sm);
  padding: var(--app-space-3) var(--app-space-4);
  font-size: var(--app-text-base);
  color: var(--app-text);
  line-height: 1.6;
  box-shadow: var(--app-shadow-sm);
}

.message-row.user .text-bubble {
  background: var(--el-color-primary);
  color: var(--el-color-white);
  border-color: var(--el-color-primary);
  border-radius: var(--app-radius-xl) var(--app-radius-xl) var(--app-radius-sm) var(--app-radius-xl);
  box-shadow: var(--app-shadow-sm);
}

.loading-bubble {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-xl);
  padding: var(--app-space-3) var(--app-space-4);
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}

/* 决策卡片 */
.decision-card {
  background: var(--app-surface);
  border-radius: var(--app-radius-xl);
  border: 1px solid var(--app-border-light);
  box-shadow: var(--app-shadow-md);
  overflow: hidden;
  width: 100%;
  max-width: 640px;
  box-sizing: border-box;
}

.verdict-banner {
  padding: var(--app-space-4) var(--app-space-5);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.verdict-banner.allow {
  background: var(--el-color-primary-light-9);
  border-bottom: 1px solid var(--el-color-primary-light-7);
}

.verdict-banner.block {
  background: var(--el-color-danger-light-9);
  border-bottom: 1px solid var(--el-color-danger-light-7);
}

.verdict-main {
  display: flex;
  flex: 1;
  min-width: 0;
  align-items: flex-start;
  gap: var(--app-space-3);
}

.verdict-main > div {
  flex: 1;
  min-width: 0;
}

.verdict-icon {
  flex-shrink: 0;
  font-size: 26px;
  margin-top: 2px;
}

.verdict-banner.allow .verdict-icon {
  color: var(--el-color-primary);
}

.verdict-banner.block .verdict-icon {
  color: var(--el-color-danger);
}

.verdict-title {
  font-size: var(--app-text-lg);
  font-weight: 700;
  letter-spacing: -0.01em;
}

.verdict-banner.allow .verdict-title {
  color: var(--el-color-primary-dark-2);
}

.verdict-banner.block .verdict-title {
  color: var(--el-color-danger-dark-2);
}

.verdict-desc {
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
  margin-top: 2px;
}

.confidence-badge {
  flex-shrink: 0;
  margin-left: var(--app-space-3);
  text-align: right;
}

.conf-label {
  display: block;
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

.conf-val {
  font-size: var(--app-text-lg);
  font-weight: 700;
  color: var(--app-text);
}

.card-section {
  padding: 14px var(--app-space-4);
  border-bottom: 1px solid var(--app-border-light);
}

.card-section.alert-section {
  background: var(--el-color-warning-light-9);
}

.section-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--app-text-sm);
  font-weight: 600;
  margin-bottom: var(--app-space-2);
}

.section-badge.warn {
  color: var(--el-color-warning);
}

.section-badge.rec {
  color: var(--el-color-primary);
}

.risk-list {
  margin: 0;
  padding-left: var(--app-space-5);
  font-size: var(--app-text-sm);
  color: var(--el-color-warning-dark-2);
  line-height: 1.5;
}

.rec-steps {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-2);
}

.rec-step-item {
  display: flex;
  align-items: flex-start;
  gap: var(--app-space-2);
  font-size: var(--app-text-sm);
  color: var(--app-text);
}

.step-num {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: var(--app-text-xs);
  font-weight: 700;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 2px;
}

.details-collapse {
  border: none;
  padding: 0 var(--app-space-4);
}

.collapse-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.evidences-list {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-2);
}

.evidence-item {
  background: var(--app-surface-2);
  padding: var(--app-space-2) 10px;
  border-radius: var(--app-radius-sm);
}

.evi-title {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-1);
  font-size: var(--app-text-xs);
  font-weight: 600;
  color: var(--el-color-primary);
}

.evi-desc {
  margin: var(--app-space-1) 0 0;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.tool-traces-list {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-2);
}

.trace-item {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-sm);
  padding: var(--app-space-2) 10px;
}

.trace-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--app-space-1);
}

.trace-tool-name {
  font-size: var(--app-text-xs);
  font-weight: 600;
}

.trace-body {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

.trace-sub code {
  color: var(--el-color-primary);
}

.expert-turn-notice {
  background: var(--el-color-info-light-9);
  border-top: 1px solid var(--el-color-info-light-7);
  padding: 10px var(--app-space-4);
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  font-size: var(--app-text-xs);
  color: var(--el-color-info-dark-2);
}

/* 底部输入框 */
.chat-input-bar {
  background: var(--app-surface);
  border-top: 1px solid var(--app-border-light);
  padding: 14px var(--app-space-5);
}

/* 一体化输入框：textarea + 框内右侧工具行 */
.input-box {
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-lg);
  background: var(--app-surface);
  padding: 6px 12px 8px;
  transition: var(--app-transition);
}

.input-box:focus-within {
  border-color: var(--el-color-primary);
  box-shadow: 0 0 0 2px var(--el-color-primary-light-8);
}

.input-image-preview {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-2);
  margin: 4px 0;
  padding: 4px;
  background: var(--app-surface-2);
  border-radius: var(--app-radius-md);
}

.preview-thumb {
  width: 48px;
  height: 48px;
  object-fit: cover;
  border-radius: var(--app-radius-sm);
  border: 1px solid var(--app-border-light);
}

.preview-remove {
  color: var(--app-text-secondary);
}

.preview-remove:hover {
  color: var(--el-color-danger);
}

.main-textarea :deep(.el-textarea__inner) {
  border: none;
  box-shadow: none;
  background: transparent;
  /* 行高 1.5 + 上下 2px，输入区高度微降 */
  padding: 2px;
  line-height: 1.5;
  transition: none;
}

/* 草稿提示：小字次要色，弱化存在 */
.draft-notice {
  margin: 0 0 6px 2px;
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
  line-height: 1.4;
}

.input-toolbar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
}

/* 恢复草稿钮贴左，与右侧发送区分离 */
.restore-draft-btn {
  margin-right: auto;
}

.input-toolbar .icon-btn {
  font-size: 18px;
  color: var(--app-text-secondary);
  padding: 6px;
  border-radius: var(--app-radius-sm);
  transition: var(--app-transition);
}

.input-toolbar .icon-btn:hover {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.emoji-trigger {
  font-size: 17px;
  line-height: 1;
}

.send-btn {
  height: 34px;
  padding: 0 18px;
  margin-left: var(--app-space-2);
  border-radius: var(--app-radius-round);
  background: var(--el-color-primary);
  border: none;
  font-weight: 600;
  box-shadow: var(--app-shadow-sm);
  transition: var(--app-transition);
}

.send-btn:hover {
  background: var(--el-color-primary-light-3);
  transform: translateY(-1px);
  box-shadow: var(--app-shadow-md);
}

/* ================= 右栏 ================= */
.right-sidebar {
  width: 360px;
  min-width: 360px;
  flex-shrink: 0;
  background: var(--app-surface);
  border-left: 1px solid var(--app-border-light);
  padding: var(--app-space-4);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  transition: width 0.25s cubic-bezier(0.4, 0, 0.2, 1), min-width 0.25s, padding 0.25s, opacity 0.2s;
  box-sizing: border-box;
}

.right-sidebar.sidebar-collapsed {
  width: 0 !important;
  min-width: 0 !important;
  padding: 0 !important;
  opacity: 0 !important;
  overflow: hidden !important;
  border-left: none !important;
  pointer-events: none;
}

/* 窄屏与移动端抽屉模式 */
.right-sidebar.drawer-mode {
  position: fixed;
  top: 0;
  bottom: 0;
  right: 0;
  z-index: 200;
  width: 360px !important;
  min-width: 360px !important;
  box-shadow: var(--app-shadow-lg);
  transform: translateX(100%);
  transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
}

.right-sidebar.drawer-mode.drawer-open {
  transform: translateX(0);
  opacity: 1 !important;
  pointer-events: auto;
}

.right-sidebar-header {
  margin-bottom: var(--app-space-2);
}

.side-card {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-xl);
  padding: 14px;
  box-shadow: var(--app-shadow-sm);
}

.side-card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--app-text-sm);
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: var(--app-space-3);
}

.rec-list {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-2);
}

.mini-rec-item {
  background: var(--app-surface);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-md);
  padding: var(--app-space-2) 10px;
}

.mini-rec-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: var(--app-text-xs);
  font-weight: 600;
}

.rec-name {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-1);
  color: var(--app-text);
}

.rec-date {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
  font-weight: normal;
}

.mini-rec-detail {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin-top: var(--app-space-1);
}

.empty-rec {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
  text-align: center;
  padding: 10px 0;
}

.side-card > p {
  margin: 0;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  line-height: 1.6;
}

/* ================= 响应式媒体查询 (移动端与窄屏适配) ================= */
@media (max-width: 1280px) {
  .agent-desc {
    display: none;
  }
}

@media (max-width: 768px) {
  .chat-header {
    padding: var(--app-space-2) 10px;
    gap: 6px;
  }

  .agent-desc {
    display: none;
  }

  .react-tag {
    display: none;
  }

  .btn-text {
    display: none;
  }

  .sidebar-pill-btn {
    padding: 0 var(--app-space-2);
    height: 34px;
  }

  .agent-avatar {
    width: 28px;
    height: 28px;
    font-size: var(--app-text-lg);
  }

  .agent-name {
    font-size: var(--app-text-sm);
    max-width: 130px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .message-row {
    max-width: 96%;
    gap: var(--app-space-2);
  }

  .msg-avatar {
    width: 30px;
    height: 30px;
    font-size: var(--app-text-lg);
  }

  .text-bubble {
    padding: var(--app-space-2) var(--app-space-3);
    font-size: var(--app-text-sm);
    border-radius: var(--app-radius-xl) var(--app-radius-xl) var(--app-radius-xl) var(--app-radius-sm);
  }

  .message-row.user .text-bubble {
    border-radius: var(--app-radius-xl) var(--app-radius-xl) var(--app-radius-sm) var(--app-radius-xl);
  }

  .decision-card {
    max-width: 100%;
  }

  .verdict-banner {
    padding: 10px var(--app-space-3);
  }

  .verdict-title {
    font-size: var(--app-text-base);
  }

  .verdict-icon {
    font-size: var(--app-text-2xl);
  }

  .confidence-badge {
    margin-left: 6px;
  }

  .chat-messages-body {
    padding: 10px;
    gap: var(--app-space-3);
  }

  .chat-input-bar {
    padding: var(--app-space-2) 10px;
  }

  .prompt-chips-container {
    gap: 6px;
    margin-bottom: 6px;
  }

  .prompt-lead-text {
    font-size: var(--app-text-xs);
    padding: 2px 6px;
  }

  .archive-toolbar {
    padding: 4px 10px;
  }

  .archive-page-info small {
    display: none;
  }

  .left-sidebar.drawer-mode,
  .right-sidebar.drawer-mode {
    width: min(88vw, 340px) !important;
    min-width: unset !important;
  }

  .send-btn {
    height: 32px;
    padding: 0 var(--app-space-3);
    font-size: var(--app-text-sm);
  }
}
</style>

<!-- 表情弹层样式（el-popover 传送至 body，需非 scoped） -->
<style>
.emoji-popper {
  padding: var(--app-space-2) !important;
}

.emoji-popper .emoji-grid {
  display: grid;
  grid-template-columns: repeat(8, 1fr);
  gap: 2px;
}

.emoji-popper .emoji-item {
  font-size: 20px;
  line-height: 1.4;
  text-align: center;
  padding: 5px 0;
  border-radius: var(--app-radius-sm);
  cursor: pointer;
  transition: background 0.15s;
  user-select: none;
}

.emoji-popper .emoji-item:hover {
  background: var(--app-hover);
}
</style>
