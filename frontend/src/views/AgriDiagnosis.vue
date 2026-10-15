<script setup lang="ts">
import AuthenticatedImage from '@/components/AuthenticatedImage.vue'
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useFarmSelectionStore } from '@/stores/farmSelection'
import { useAuthStore } from '@/stores/auth'
import { newRequestId } from '@/utils/requestId'
import { draftOwnerKey, saveScopedDraft, readScopedDraft } from '@/utils/scopedDraft'
import DiagnosisHistoryPanel from '@/components/DiagnosisHistoryPanel.vue'
import AgriConsultationQueue from '@/components/AgriConsultationQueue.vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  UploadFilled,
  Picture,
  Service,
  Check,
  Warning,
  Refresh,
  Camera,
  Search,
  FirstAidKit,
  MapLocation,
  House,
  Sunny,
  EditPen,
  Avatar
} from '@element-plus/icons-vue'
import {
  getFieldsByFarm,
  getCropPlantings,
  uploadDiagnosisImage,
  diagnoseCropImage,
  getDiagnosisRequest,
  cancelDiagnosisRequest,
  getExpertTickets,
  getExpertTicketDetail,
  claimExpertTicket,
  resolveExpertTicket
} from '@/api/agriculture'
import type {
  FieldItem,
  CropPlanting,
  VisualDiagnosisResponse,
  VisualDiagnosisRequest,
  ExpertTicketItem,
  ExpertTicketDetail,
  ExpertResolveRequest
} from '@/types/agriculture'

// 当前激活的标签页
const auth = useAuthStore()
const canReview = computed(() => ['admin', 'expert'].includes(auth.user?.role || ''))
const activeTab = ref<'farmer' | 'expert'>('farmer')
const workbenchType = ref<'disease' | 'consultation'>('disease')
const consultationBusy = ref(false)

// ==================== 农户端状态 ====================
const diagCrop = ref('')
const historyVersion = ref(0)
const diagSymptoms = ref('')
const uploadedImageUrl = ref<string>('')
const diagLoading = ref(false)
const uploading = ref(false)
const queryingDiagnosis = ref(false)
const pendingDiagnosis = ref<{ key: string; payload: VisualDiagnosisRequest } | null>(null)
function diagnosisWasCancelled() { return diagResult.value?.error_code === 'cancelled' }
function diagnosisDraft() {
  return { image_url: uploadedImageUrl.value, field_id: diagFieldId.value || undefined,
    crop_planting_id: diagPlantingId.value,
    crop: diagCrop.value, symptoms_hint: diagSymptoms.value, auto_create_ticket: true }
}
function acceptDiagnosis(result: VisualDiagnosisResponse, key: string) {
  if (!alive) return
  historyVersion.value++
  if (JSON.stringify(diagnosisDraft()) !== key) return
  diagResult.value = result
  if (result.diagnosis_status === 'failed') {
    ElMessage.error(result.error_message || result.advice)
    return
  }
  ElMessage.success(result.replayed ? '已恢复原诊断结果，未重新调用模型' : '初诊分析已保存')
  if (result.ticket_id != null) ElMessage.warning('需要专家复核，已为您自动转接农技专家复核工单！')
  else if (result.need_expert) ElMessage.warning('建议农技专家复核，本次未创建专家工单，请查看诊断建议。')
}
async function queryDiagnosisStatus(cancel = false) {
  const pending = pendingDiagnosis.value
  if (!pending?.payload.request_id || queryingDiagnosis.value) return
  queryingDiagnosis.value = true
  try {
    const res = await (cancel ? cancelDiagnosisRequest(pending.payload.request_id) : getDiagnosisRequest(pending.payload.request_id))
    if (!alive || pendingDiagnosis.value?.payload.request_id !== pending.payload.request_id) return
    if (res.data.result) acceptDiagnosis(res.data.result, pending.key)
    else ElMessage.info(res.data.message)
  } catch (err: any) {
    if (alive && pendingDiagnosis.value?.payload.request_id === pending.payload.request_id) ElMessage.error('查询诊断状态失败: ' + (err.response?.data?.detail || err.message))
  } finally { queryingDiagnosis.value = false }
}
function newDiagnosis() {
  if (diagLoading.value || queryingDiagnosis.value) return
  pendingDiagnosis.value = null
  diagResult.value = null
}
async function retryFailedDiagnosis() {
  if (diagResult.value?.diagnosis_status !== 'failed' || diagLoading.value || uploading.value || queryingDiagnosis.value) return
  newDiagnosis()
  await startVisualDiagnosis()
}
async function handleDiagnosisDeleted(recordId: number) {
  if (diagResult.value?.disease_record_id === recordId) newDiagnosis()
  if (selectedTicketId.value === recordId) {
    ++detailVersion
    selectedTicketId.value = null
    ticketDetail.value = null
    detailLoading.value = false
  }
  if (canReview.value) await fetchTickets()
}
const diagResult = ref<VisualDiagnosisResponse | null>(null)
const farmSelection = useFarmSelectionStore()
const { farms: diagnosisFarms, selectedFarmId } = storeToRefs(farmSelection)
let farmContextReady = false
watch(selectedFarmId, () => { if (farmContextReady) void changeDiagnosisFarm() }, { flush: 'sync' })
const diagnosisFields = ref<FieldItem[]>([])
const diagFarmId = computed({
  get: () => selectedFarmId.value ?? undefined,
  set: (id: number | undefined) => { selectedFarmId.value = id || null }
})
const diagFieldId = ref<number>()
const diagPlantingId = ref<number>()
const diagnosisPlantings = ref<CropPlanting[]>([])
const plantingsLoading = ref(false)
let plantingVersion = 0
watch(diagFieldId, () => { void loadDiagnosisPlantings() }, { flush: 'sync' })
async function loadDiagnosisPlantings() {
  const version = ++plantingVersion
  const fieldId = diagFieldId.value
  diagPlantingId.value = undefined; diagnosisPlantings.value = []; newDiagnosis()
  if (!fieldId) { plantingsLoading.value = false; return }
  plantingsLoading.value = true
  try {
    const res = await getCropPlantings(fieldId, 'active')
    if (!alive || version !== plantingVersion) return
    diagnosisPlantings.value = res.data.items || []
    if (diagnosisPlantings.value.length === 1) diagPlantingId.value = diagnosisPlantings.value[0].id
  } catch {
    if (alive && version === plantingVersion) ElMessage.error('批次加载失败，请重新选择田块后诊断')
  } finally { if (version === plantingVersion) plantingsLoading.value = false }
}
const fieldsLoading = ref(false)
const diagnosisDraftNotice = ref('')
const diagnosisDraftScope = () => `diagnosis:${diagFieldId.value || 'none'}:${diagPlantingId.value || 'none'}`
let restoringDiagnosisDraft = false
watch([diagCrop, diagSymptoms, uploadedImageUrl, pendingDiagnosis], () => {
  if (restoringDiagnosisDraft) return
  const value = { crop: diagCrop.value, symptoms: diagSymptoms.value, image: uploadedImageUrl.value, pending: pendingDiagnosis.value }
  const hasDraft = value.crop || value.symptoms || value.image || value.pending
  const owner = draftOwnerKey(auth.user)
  const saved = saveScopedDraft(owner, diagnosisDraftScope(), hasDraft ? value : null)
  diagnosisDraftNotice.value = owner && hasDraft ? (saved ? '本机草稿已保存，7天内可恢复；已提交请求可查询原结果' : '本机无法保存草稿，请保留页面并复制内容') : ''
}, { deep: true, flush: 'sync' })
function restoreDiagnosisDraft() {
  if (diagLoading.value || plantingsLoading.value || queryingDiagnosis.value) return
  const saved = readScopedDraft<{ crop: string; symptoms: string; image: string; pending: typeof pendingDiagnosis.value }>(draftOwnerKey(auth.user), diagnosisDraftScope())
  if (!saved || typeof saved.crop !== 'string' || typeof saved.symptoms !== 'string' || typeof saved.image !== 'string') { ElMessage.info('当前账号和批次没有可恢复草稿'); return }
  if (saved.pending && (typeof saved.pending.payload?.request_id !== 'string' || saved.pending.payload.field_id !== (diagFieldId.value || undefined) || saved.pending.payload.crop_planting_id !== diagPlantingId.value)) { ElMessage.warning('原提交标识与当前批次不一致，请先核对诊断历史'); return }
  restoringDiagnosisDraft = true
  diagCrop.value = saved.crop; diagSymptoms.value = saved.symptoms; uploadedImageUrl.value = saved.image
  pendingDiagnosis.value = saved.pending; diagResult.value = null
  restoringDiagnosisDraft = false
  diagnosisDraftNotice.value = '已恢复草稿；若之前已提交，请先查询原诊断状态，不会自动重新调用模型'
}
watch(() => draftOwnerKey(auth.user), () => {
  restoringDiagnosisDraft = true
  diagCrop.value = ''; diagSymptoms.value = ''; uploadedImageUrl.value = ''; pendingDiagnosis.value = null; diagResult.value = null
  diagLoading.value = false
  restoringDiagnosisDraft = false
}, { flush: 'sync' })

// 预设快速测试病斑特征参考
const sampleImages = [
  {
    name: '番茄晚疫病 (典型特征)',
    symptoms: '叶片边缘水浸状暗褐色病斑，清晨棚内湿度大时病健交界处长出白色稀疏霉层',
    crop: '番茄'
  },
  {
    name: '番茄早期疑似斑 (低置信度需专家介入)',
    symptoms: '下部老叶散发不规则黄褐色微小点，轮纹微弱，疑似早疫病或缺素症',
    crop: '番茄'
  }
]


// ==================== 专家端状态 ====================
const ticketFilterStatus = ref<string>('all')
const tickets = ref<ExpertTicketItem[]>([])
const ticketsLoading = ref(false)
const selectedTicketId = ref<number | null>(null)
const ticketDetail = ref<ExpertTicketDetail | null>(null)
const detailLoading = ref(false)
const submittingResolve = ref(false)
const claiming = ref(false)
let alive = true
let listVersion = 0
let detailVersion = 0
const canSubmit = computed(() => canReview.value && ticketDetail.value?.status === 'in_review' && ticketDetail.value?.expert_id === auth.user?.id && !detailLoading.value)
function formatScore(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? `${Math.round(value * 100)}%` : '未提供有效评分'
}
function weatherValue(value: unknown, unit: string) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value}${unit}` : '未提供'
}
const ticketPage = ref(1)
const ticketPageSize = ref(20)
const ticketTotal = ref(0)

// 专家确诊表单
const resolveForm = ref<ExpertResolveRequest>({
  confirmed_disease: '',
  treatment_suggestion: '',
  notes: '',
  is_knowledge_candidate: 0
})

// ==================== 农户端操作 ====================

async function loadDiagnosisFarms() {
  try {
    if (!await farmSelection.loadFarms()) return
    farmContextReady = true
    await changeDiagnosisFarm()
  } catch (err: any) {
    ElMessage.warning('无法加载关联农场，可继续仅诊断: ' + (err.response?.data?.detail || err.message))
  }
}

async function changeDiagnosisFarm() {
  diagFieldId.value = undefined
  diagnosisFields.value = []
  const farmId = diagFarmId.value
  if (!farmId) {
    fieldsLoading.value = false
    return
  }
  fieldsLoading.value = true
  try {
    const res = await getFieldsByFarm(farmId)
    if (diagFarmId.value === farmId) diagnosisFields.value = res.data.items
  } catch (err: any) {
    if (diagFarmId.value === farmId) {
      ElMessage.error('加载田块失败: ' + (err.response?.data?.detail || err.message))
    }
  } finally {
    if (diagFarmId.value === farmId) fieldsLoading.value = false
  }
}

function selectSampleImage(sample: typeof sampleImages[0]) {
  if (diagLoading.value) return
  diagSymptoms.value = sample.symptoms
  diagCrop.value = sample.crop
  ElMessage.info(`已填入典型特征：${sample.name}，请拍摄或上传现场真实病叶照片`)
}


async function handleCustomUpload(options: any) {
  if (uploading.value || diagLoading.value) return
  uploading.value = true
  try {
    const res = await uploadDiagnosisImage(options.file)
    if (!alive) return
    uploadedImageUrl.value = res.data.url
    newDiagnosis()
    ElMessage.success('病叶现场照片上传成功')
  } catch (err: any) {
    ElMessage.error('图片上传失败: ' + (err.response?.data?.detail || err.message))
  } finally { uploading.value = false }
}

async function startVisualDiagnosis() {
  if (diagLoading.value || uploading.value || queryingDiagnosis.value || plantingsLoading.value) return
  if (diagnosisPlantings.value.length > 1 && !diagPlantingId.value) {
    ElMessage.warning('请明确选择本次诊断的作物批次'); return
  }
  if (!uploadedImageUrl.value) {
    ElMessage.warning('请先上传现场真实病斑照片')
    return
  }
  diagLoading.value = true
  diagResult.value = null

  const draft = diagnosisDraft()
  const key = JSON.stringify(draft)
  if (!pendingDiagnosis.value || pendingDiagnosis.value.key !== key) {
    pendingDiagnosis.value = { key, payload: { ...draft, request_id: newRequestId() } }
  }
  const requestId = pendingDiagnosis.value.payload.request_id
  const owner = draftOwnerKey(auth.user)
  try {
    const res = await diagnoseCropImage(pendingDiagnosis.value.payload)
    if (!alive || owner !== draftOwnerKey(auth.user) || pendingDiagnosis.value?.payload.request_id !== requestId) return
    acceptDiagnosis(res.data, key)
  } catch (err: any) {
    if (alive && owner === draftOwnerKey(auth.user) && pendingDiagnosis.value?.payload.request_id === requestId && !diagnosisWasCancelled()) {
      ElMessage.error('诊断请求失败: ' + (err.response?.data?.detail || err.message) + '；分析可能仍在继续，请查询原结果')
    }
  } finally {
    if (owner === draftOwnerKey(auth.user) && pendingDiagnosis.value?.payload.request_id === requestId) diagLoading.value = false
  }
}

// ==================== 专家端操作 ====================

async function fetchTickets() {
  if (!canReview.value) return
  const version = ++listVersion
  ticketsLoading.value = true
  try {
    const res = await getExpertTickets({ status: ticketFilterStatus.value === 'all' ? undefined : ticketFilterStatus.value,
      page: ticketPage.value, page_size: ticketPageSize.value })
    if (!alive || version !== listVersion) return
    tickets.value = res.data.items || []
    ticketTotal.value = res.data.total || 0
    if (!claiming.value && !submittingResolve.value && !tickets.value.some(t => t.ticket_id === selectedTicketId.value)) {
      selectedTicketId.value = null
      ticketDetail.value = null
      ++detailVersion
      if (tickets.value.length) await selectTicket(tickets.value[0].ticket_id)
    }
  } catch (err: any) {
    if (alive && version === listVersion) { tickets.value = []; ticketTotal.value = 0; ElMessage.error('加载专家工单失败: ' + (err.response?.data?.detail || err.message)) }
  } finally {
    if (alive && version === listVersion) ticketsLoading.value = false
  }
}
function handleTicketPageChange(newPage: number) {
  if (claiming.value || submittingResolve.value) return
  ticketPage.value = newPage
  fetchTickets()
}
async function selectTicket(ticketId: number, internal = false) {
  if (!canReview.value || (!internal && (claiming.value || submittingResolve.value))) return
  const version = ++detailVersion
  selectedTicketId.value = ticketId
  ticketDetail.value = null
  resolveForm.value = { confirmed_disease: '', treatment_suggestion: '', notes: '', is_knowledge_candidate: 0 }
  detailLoading.value = true
  try {
    const res = await getExpertTicketDetail(ticketId)
    if (alive && version === detailVersion && selectedTicketId.value === ticketId) ticketDetail.value = res.data
  } catch (err: any) {
    if (alive && version === detailVersion) ElMessage.error('加载工单详情失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    if (alive && version === detailVersion) detailLoading.value = false
  }
}
async function handleClaimTicket() {
  if (!selectedTicketId.value || !canReview.value || !ticketDetail.value?.claim_available || claiming.value || submittingResolve.value) return
  const id = selectedTicketId.value
  claiming.value = true
  try {
    await claimExpertTicket(id)
    if (!alive) return
    ElMessage.success('工单已认领')
    await selectTicket(id, true)
    await fetchTickets()
  } catch (err: any) {
    if (alive) { ElMessage.error('认领工单失败: ' + (err.response?.data?.detail || err.message)); await selectTicket(id, true) }
  } finally { claiming.value = false }
}
async function submitExpertResolve() {
  if (!selectedTicketId.value || !canSubmit.value || submittingResolve.value || claiming.value) return
  if (!resolveForm.value.confirmed_disease.trim() || resolveForm.value.treatment_suggestion.trim().length < 5) {
    ElMessage.warning('请填写复核结论和至少5个字的处置建议')
    return
  }
  const id = selectedTicketId.value
  submittingResolve.value = true
  try {
    const res = await resolveExpertTicket(id, { ...resolveForm.value })
    if (!alive) return
    ElMessageBox.alert(res.data.feedback_message, '专家复核已保存', { type: 'success', confirmButtonText: '确定' })
    historyVersion.value++
    await selectTicket(id, true)
    await fetchTickets()
  } catch (err: any) {
    if (alive) ElMessage.error('提交复核失败: ' + (err.response?.data?.detail || err.message))
  } finally { submittingResolve.value = false }
}

function formatTicketStatus(status: string) {
  switch (status) {
    case 'preliminary':
      return { label: '初步记录', tag: 'info' }
    case 'pending_review':
      return { label: '待专家接单', tag: 'danger' }
    case 'in_review':
      return { label: '专家审阅中', tag: 'warning' }
    case 'expert_reviewed':
      return { label: '已确诊出方', tag: 'success' }
    default:
      return { label: status, tag: 'info' }
  }
}

onBeforeUnmount(() => { alive = false; ++listVersion; ++detailVersion })
onMounted(() => {
  loadDiagnosisFarms()
  if (canReview.value) fetchTickets()
})
</script>

<template>
  <div class="app-page agri-diagnosis-page">
    <!-- 顶部导航 Tabs -->
    <div class="app-card header-card">
      <div class="tabs-control">
        <div
          class="tab-btn"
          :class="{ active: activeTab === 'farmer' }"
          @click="activeTab = 'farmer'"
        >
          <el-icon><Picture /></el-icon>
          <span>农户端 · 病叶图像智能初诊</span>
        </div>
        <div
          class="tab-btn"
          :class="{ active: activeTab === 'expert' }"
          v-if="canReview"
          @click="activeTab = 'expert'"
        >
          <el-icon><Service /></el-icon>
          <span>专家端 · 农技专家会诊工作台</span>
          <el-badge :value="tickets.filter(t => t.status === 'pending_review').length" :max="99" class="tab-badge" />
        </div>
      </div>
      <div class="tab-desc">
        <span v-if="activeTab === 'farmer'">
          <el-icon><Camera /></el-icon> 拍摄或上传农作物发病叶片照片，AgriMind 多模态视觉模型将实时解析病斑特征；关联田块后，低置信度结果可自动派单给农技专家。
        </span>
        <span v-else>
          <el-icon><Avatar /></el-icon> 农技专家协同复核工作台，整合现场病叶图像、田块土壤、时空微气象与用药历史；复核归档可选择保存为 L4 专家案例。
        </span>
      </div>
    </div>

    <!-- ==================== TAB 1: 农户端智能初诊 ==================== -->
    <div v-if="activeTab === 'farmer'" class="farmer-view-layout">
      <!-- 左侧：图像上传与表单 -->
      <div class="app-card farmer-input-card">
        <h3 class="card-title"><el-icon><Camera /></el-icon> 现场病害叶片取样</h3>

        <!-- 上传区域 -->
        <el-upload
          class="image-uploader"
          drag
          action="#"
          :http-request="handleCustomUpload"
          :show-file-list="false"
          :disabled="diagLoading || uploading"
        >
          <div v-if="uploadedImageUrl" class="uploaded-preview">
            <AuthenticatedImage :src="uploadedImageUrl" alt="病叶" class="preview-img" />
            <div class="replace-mask">点击重新上传</div>
          </div>
          <div v-else class="upload-placeholder">
            <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
            <div class="el-upload__text">
              拖拽病叶照片到此处，或 <em>点击上传</em>
            </div>
            <div class="el-upload__tip">
              支持 JPG / PNG / WEBP 格式照片，建议正对病斑特写
            </div>
          </div>
        </el-upload>

        <!-- 预设示例快速测试按钮 -->
        <div class="sample-actions">
          <span class="sample-label">快速填入典型特征：</span>
          <div class="sample-chips">

            <el-button
              v-for="(s, idx) in sampleImages"
              :key="idx"
              size="small"
              plain
              @click="selectSampleImage(s)"
            >
              {{ s.name }}
            </el-button>
          </div>
        </div>

        <el-divider />

        <!-- 补充特征表单 -->
        <el-form label-position="top" class="symptom-form">
          <el-form-item label="关联农场（可选）">
            <el-select v-model="diagFarmId" clearable placeholder="不选择则仅诊断" style="width: 100%" :disabled="diagLoading">
              <el-option v-for="farm in diagnosisFarms" :key="farm.id" :label="farm.name" :value="farm.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="关联田块（可选）">
            <el-select v-model="diagFieldId" clearable placeholder="不选择则不保存记录、不创建工单" style="width: 100%" :loading="fieldsLoading" :disabled="!diagFarmId || fieldsLoading || diagLoading">
              <el-option v-for="field in diagnosisFields" :key="field.id" :label="field.name" :value="field.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="diagFieldId && diagnosisPlantings.length" label="关联种植批次">
            <el-select v-model="diagPlantingId" :disabled="diagLoading || plantingsLoading" placeholder="请选择本次诊断的批次" style="width:100%" @change="newDiagnosis">
              <el-option v-for="crop in diagnosisPlantings" :key="crop.id" :value="crop.id" :label="`${crop.crop} · ${crop.planting_date} · #${crop.id}`" />
            </el-select>
          </el-form-item>
          <el-form-item label="农作物种类">
            <el-button link @click="restoreDiagnosisDraft">恢复本机草稿 / 原提交编号</el-button>
            <span v-if="diagnosisDraftNotice">{{ diagnosisDraftNotice }}</span>
            <el-input v-model="diagCrop" placeholder="例如：番茄" />
          </el-form-item>
          <el-form-item label="田间表观症状描述（可选辅助）">
            <el-input
              v-model="diagSymptoms"
              type="textarea"
              :rows="3"
              placeholder="例如：叶缘水浸状暗绿斑，叶背有稀疏白色霜霉，有枯死迹象"
            />
          </el-form-item>
          <el-button
            type="primary"
            size="large"
            style="width: 100%"
            :loading="diagLoading || plantingsLoading"
            @click="startVisualDiagnosis"
          >
            {{ pendingDiagnosis ? '提交／恢复同一诊断' : '开始图像初诊' }}
          </el-button>
        </el-form>
        <div v-if="pendingDiagnosis" style="margin-top: 12px">
          <p>断网或关闭页面后继续分析到处理时限；恢复草稿后查询原结果。主动取消会保存失败回执。</p>
          <el-button :loading="queryingDiagnosis" @click="queryDiagnosisStatus()">查询本次诊断</el-button>
          <el-button :disabled="queryingDiagnosis" @click="queryDiagnosisStatus(true)">取消本次分析</el-button>
          <el-button :disabled="diagLoading || queryingDiagnosis" @click="newDiagnosis">开始新的诊断</el-button>
        </div>
        <p v-if="diagLoading" class="form-tip">正在分析图片，通常需要数十秒，请等待结果。若网络中断，可查询本次诊断。</p>
      </div>

      <!-- 右侧：AI初诊结果看板 -->
      <div class="app-card farmer-result-card" v-loading="diagLoading">
        <h3 class="card-title"><el-icon><Search /></el-icon> AI 初诊研判报告</h3>
        <p v-if="diagResult?.processed_at">结果保存时间：{{ diagResult.processed_at }}；{{ diagResult.replayed ? '本次恢复历史结果' : '本次已保存' }}</p>

        <div v-if="!diagResult && !diagLoading" class="empty-diag-box">
          <div class="empty-icon"><el-icon :size="48"><FirstAidKit /></el-icon></div>
          <div class="empty-text">请在左侧上传病叶图片并点击诊断，AI 视觉模型将在此出具分析报告</div>
        </div>

        <div v-else-if="diagResult?.diagnosis_status === 'failed'" class="diag-report">
          <el-alert title="本次图像诊断未完成" :description="diagResult.error_message || diagResult.advice" type="error" :closable="false" show-icon />
          <p>错误码：{{ diagResult.error_code || 'vision_error' }}</p>
          <p v-if="diagResult.request_id">诊断请求ID：{{ diagResult.request_id }}</p>
          <p v-if="diagResult.ticket_id">此前已创建专家工单 #{{ diagResult.ticket_id }}，可在历史记录中查看。</p>
          <p v-else-if="diagResult.disease_record_id">此前已保存记录 #{{ diagResult.disease_record_id }}，可在历史记录中查看。</p>
          <p v-else>本次未生成病害记录或专家工单。</p>
          <el-button type="primary" :disabled="diagLoading || uploading || queryingDiagnosis" @click="retryFailedDiagnosis">重新诊断（再次调用模型）</el-button>
        </div>
        <div v-else-if="diagResult" class="diag-report">
          <!-- 核心初诊卡片 -->
          <div class="disease-headline">
            <div class="dh-left">
              <span class="crop-tag">{{ diagResult.crop }}</span>
              <span class="disease-name">{{ diagResult.suspected_disease }}</span>
            </div>
            <div class="dh-right">
              <span class="conf-title">模型评分（非准确率）</span>
              <span class="conf-value" :class="(diagResult.confidence ?? 0) >= 0.7 ? 'high' : 'low'">
                {{ formatScore(diagResult.confidence) }}
              </span>
            </div>
          </div>

          <!-- 低置信度派发工单告警 -->
          <div v-if="diagResult.need_expert" class="expert-ticket-alert">
            <el-icon class="alert-icon"><Warning /></el-icon>
            <div class="alert-text">
              <strong>【建议专家复核】请结合诊断建议处理</strong>
              <p v-if="diagResult.ticket_id != null">系统已自动生成专家工单（工单号 #{{ diagResult.ticket_id }}），已派发至农技专家工作台进行人机协同二次确诊！</p>
              <p v-else>本次未创建专家工单。{{ diagResult.advice }}</p>
            </div>
          </div>

          <!-- 特征检出列表 -->
          <div class="features-box" v-if="diagResult.features_detected && diagResult.features_detected.length > 0">
            <span class="sec-label">视觉识别关键特征：</span>
            <div class="tag-row">
              <el-tag
                v-for="(feat, idx) in diagResult.features_detected"
                :key="idx"
                type="info"
                effect="light"
              >
                ✓ {{ feat }}
              </el-tag>
            </div>
          </div>

          <!-- 防治建议 -->
          <div class="advice-box">
            <span class="sec-label">初诊建议（待核实）：</span>
            <p class="advice-content">{{ diagResult.advice }}</p>
          </div>

          <!-- 标准推荐药剂 -->
          <div class="treatment-box" v-if="diagResult.standard_treatment">
            <span class="sec-label">相关知识检索结果（须核对来源与适用条件）：</span>
            <div class="treatment-content">{{ diagResult.standard_treatment }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== TAB 2: 专家端会诊工作台 ==================== -->
    <div v-else class="expert-workbench-shell">
      <el-radio-group v-model="workbenchType" :disabled="claiming || submittingResolve || consultationBusy" style="margin-bottom:16px">
        <el-radio-button label="disease">病害会诊</el-radio-button>
        <el-radio-button label="consultation">农业咨询</el-radio-button>
      </el-radio-group>
      <AgriConsultationQueue v-if="workbenchType === 'consultation' && canReview" @busy="consultationBusy = $event" />
      <div v-else class="expert-workbench-layout">
      <!-- 左侧：工单列表 (320px) -->
      <div class="app-card tickets-list-col" v-loading="ticketsLoading">
        <div class="tickets-filter-bar">
          <el-radio-group v-model="ticketFilterStatus" size="small" @change="fetchTickets">
            <el-radio-button label="all">全部</el-radio-button>
            <el-radio-button label="pending_review">待接单</el-radio-button>
            <el-radio-button label="in_review">审阅中</el-radio-button>
            <el-radio-button label="expert_reviewed">已确诊</el-radio-button>
          </el-radio-group>
          <el-button :icon="Refresh" circle size="small" @click="fetchTickets" />
        </div>

        <div class="tickets-scroll-wrap">
          <el-empty v-if="tickets.length === 0" description="暂无符合条件的专家工单" />
          <div
            v-for="t in tickets"
            :key="t.ticket_id"
            class="ticket-item-card"
            :class="{ active: selectedTicketId === t.ticket_id }"
            @click="selectTicket(t.ticket_id)"
          >
            <div class="tic-header">
              <span class="tic-id">工单 #{{ t.ticket_id }}</span>
              <el-tag size="small" :type="formatTicketStatus(t.status).tag as any">
                {{ formatTicketStatus(t.status).label }}
              </el-tag>
            </div>
            <div class="tic-disease">
              {{ t.crop }} · {{ t.initial_suspected_disease }}
            </div>
            <div class="tic-meta">
              <span>田块: {{ t.field_name || '未录入' }}</span>
              <span>物候: {{ t.growth_stage }}</span>
            </div>
          </div>
        </div>

        <div class="tickets-pagination-bar" v-if="ticketTotal > ticketPageSize">
          <el-pagination
            small
            layout="prev, pager, next"
            :total="ticketTotal"
            :page-size="ticketPageSize"
            :current-page="ticketPage"
            @current-change="handleTicketPageChange"
          />
        </div>
      </div>

      <!-- 右侧：工单全息审阅与专家确诊处方中心 -->
      <div class="app-card ticket-detail-col" v-loading="detailLoading">
        <div v-if="!ticketDetail" class="empty-detail-box">
          请在左侧列表中点击选择要审阅的专家工单
        </div>

        <div v-else class="detail-content-wrap">
          <!-- 顶部工单状态与认领控制 -->
          <div class="detail-header-card">
            <div class="dh-info">
              <span class="dh-title">工单 #{{ ticketDetail.ticket_id }} · {{ ticketDetail.crop }}发病复核</span>
              <el-tag :type="formatTicketStatus(ticketDetail.status).tag as any" style="margin-left: 8px">
                {{ formatTicketStatus(ticketDetail.status).label }}
              </el-tag>
            </div>
            <div class="dh-actions">
              <span>认领人：{{ ticketDetail.expert_name || '未认领' }}</span>
              <el-button
                v-if="ticketDetail.claim_available"
                :loading="claiming"
                :disabled="submittingResolve"
                type="primary"
                @click="handleClaimTicket"
              >
                认领该工单
              </el-button>
            </div>
          </div>

          <!-- 会诊三联内容区 -->
          <div class="consult-columns">
            <!-- 栏 1：现场病斑图片与AI特征 -->
            <div class="consult-col pic-col">
              <h4 class="col-title"><el-icon><Picture /></el-icon> 现场病斑图像与AI特征</h4>
              <div class="detail-img-box">
                <AuthenticatedImage
                  v-if="ticketDetail.image_url"
                  :src="ticketDetail.image_url"
                  alt="病斑照片"
                  class="consult-img"
                />
                <div v-else class="no-img">暂无病斑照片</div>
              </div>
              <div class="ai-feat-list" v-if="ticketDetail.ai_features">
                <span class="sub-label">AI检出视觉特征：</span>
                <div class="tag-row">
                  <el-tag v-for="(f, idx) in ticketDetail.ai_features" :key="idx" size="small" type="info">
                    {{ f }}
                  </el-tag>
                </div>
              </div>
              <div class="symptom-text" v-if="ticketDetail.symptoms">
                <span class="sub-label">农户描述症状：</span>
                <p>{{ ticketDetail.symptoms }}</p>
              </div>
            </div>

            <!-- 栏 2：田块时空全景（土壤、微气象、施药史） -->
            <div class="consult-col env-col">
              <h4 class="col-title"><el-icon><MapLocation /></el-icon> 田块全景档案与时空环境</h4>

              <div class="env-group">
                <span class="env-group-title"><el-icon><House /></el-icon> 田块与土壤物理属性</span>
                <div class="env-kv"><span>地块名称:</span> <strong>{{ ticketDetail.field_name || '未录入' }}</strong></div>
                <div class="env-kv"><span>土壤类型:</span> <span>{{ ticketDetail.field_soil_type || '未录入' }}</span></div>
                <div class="env-kv"><span>作物及阶段:</span> <span>{{ ticketDetail.crop }} ({{ ticketDetail.growth_stage }})</span></div>
              </div>

              <div class="env-group">
                <span class="env-group-title"><el-icon><Sunny /></el-icon> 最新已存气象快照（非诊断时点实测）</span>
                <div class="weather-row" v-if="ticketDetail.current_weather">
                  <span>气温: {{ weatherValue(ticketDetail.current_weather.temperature, '℃') }}</span>
                  <span>湿度: {{ weatherValue(ticketDetail.current_weather.humidity, '%') }}</span>
                  <span>降水量: {{ weatherValue(ticketDetail.current_weather.rainfall_mm, 'mm') }}</span>
                  <span>风速: {{ weatherValue(ticketDetail.current_weather.wind_speed, 'm/s') }}</span>
                  <span>来源: {{ ticketDetail.current_weather.source || '未提供' }}</span>
                  <span>时间: {{ ticketDetail.current_weather.timestamp || '未提供' }}</span>
                </div>
                <div v-else class="weather-row">暂无已存快照</div>
              </div>

              <div class="env-group">
                <span class="env-group-title">近期农事记录（最多5条）</span>
                <div class="mini-timeline" v-if="ticketDetail.recent_farming_records && ticketDetail.recent_farming_records.length > 0">
                  <div v-for="(r, idx) in ticketDetail.recent_farming_records" :key="idx" class="history-item">
                    <span>{{ r.record_date }} · {{ r.record_type }}: {{ r.input_name || r.description || '未填写内容' }}</span>
                  </div>
                </div>
                <div v-else class="text-muted">暂无已存农事记录</div>
              </div>
            </div>

            <!-- 栏 3：专家确诊与知识反哺表单 -->
            <div class="consult-col form-col">
              <h4 class="col-title"><el-icon><EditPen /></el-icon> 专家最终确诊与处方开具</h4>

              <div v-if="ticketDetail.feedback" class="advice-box">
                <p>{{ ticketDetail.feedback.workflow_verified ? '已核验本流程归档' : '历史结论：缺少本流程认领凭据，来源待核验' }}</p>
                <p>复核人：{{ ticketDetail.feedback.expert_name || '历史未记录' }}</p>
                <p>{{ ticketDetail.feedback.confirmed_disease || '未记录结论名称' }}</p>
                <p style="white-space: pre-wrap">{{ ticketDetail.feedback.treatment_suggestion }}</p>
                <p style="white-space: pre-wrap">{{ ticketDetail.feedback.notes }}</p>
              </div>
              <p v-else-if="!canSubmit">请先认领；只有当前认领人可以提交复核。</p>
              <el-form v-if="!ticketDetail.feedback" :disabled="!canSubmit || submittingResolve || claiming" label-position="top" class="resolve-form">
                <el-form-item label="最终确诊病害全称" required>
                  <el-input v-model="resolveForm.confirmed_disease" placeholder="例如：番茄晚疫病 (Phytophthora infestans)" />
                </el-form-item>

                <el-form-item label="综合防治处方及安全用药建议" required>
                  <el-input
                    v-model="resolveForm.treatment_suggestion"
                    type="textarea"
                    :rows="4"
                    placeholder="精准药剂选择、稀释倍数、作业方式、安全间隔期与放风控湿指导"
                  />
                </el-form-item>

                <el-form-item label="诊断手记 / 典型病征说明">
                  <el-input
                    v-model="resolveForm.notes"
                    type="textarea"
                    :rows="2"
                    placeholder="关键诊断要点、鉴别依据"
                  />
                </el-form-item>

                <!-- 核心 Human-in-the-Loop 反哺复选框 -->
                <div class="knowledge-loop-box">
                  <el-checkbox
                    v-model="resolveForm.is_knowledge_candidate"
                    :true-label="1"
                    :false-label="0"
                  >
                    <strong>沉淀为 L4 级专家知识库实战案例 (Human-in-the-Loop)</strong>
                  </el-checkbox>
                  <p class="loop-tip">
                    勾选后保存当前填写的 L4 专家案例；不代表标准认证，不自动重建向量索引。
                  </p>
                </div>

                <el-button
                  type="success"
                  size="large"
                  style="width: 100%; margin-top: 16px"
                  :loading="submittingResolve"
                  @click="submitExpertResolve"
                >
                  <el-icon><Check /></el-icon>
                  提交专家确诊处方并归档
                </el-button>
              </el-form>
            </div>
          </div>
        </div>
      </div>
    </div>
    </div>
    <DiagnosisHistoryPanel v-if="activeTab === 'farmer'" :field-id="diagFieldId" :version="historyVersion" @deleted="handleDiagnosisDeleted" />
  </div>
</template>

<style scoped>
.agri-diagnosis-page {
  min-height: calc(100vh - 64px);
}

.header-card {
  padding: var(--app-space-4) var(--app-space-5);
  margin-bottom: var(--app-space-5);
}

.tabs-control {
  display: flex;
  gap: var(--app-space-4);
  margin-bottom: 10px;
}

.tab-btn {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  padding: 10px 20px;
  border-radius: var(--app-radius-md);
  background: var(--app-surface-2);
  color: var(--app-text-secondary);
  font-size: var(--app-text-base);
  font-weight: 600;
  cursor: pointer;
  transition: var(--app-transition);
  position: relative;
}

.tab-btn:hover {
  background: var(--app-hover);
}

.tab-btn.active {
  background: var(--el-color-primary);
  color: var(--el-color-white);
}

.tab-badge {
  margin-left: 6px;
}

.tab-desc {
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
}

.tab-desc span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

/* ================= 农户端 ================= */
.farmer-view-layout {
  display: grid;
  /* minmax(0,1fr)：防 grid blowout，内部长内容撑宽轨道会顶破 MainLayout 的 overflow-x:hidden 造成整页右侧裁切 */
  grid-template-columns: 460px minmax(0, 1fr);
  gap: var(--app-space-5);
}

.farmer-input-card,
.farmer-result-card {
  padding: var(--app-space-5);
}

.card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 var(--app-space-4);
  font-size: var(--app-text-lg);
  font-weight: 600;
  color: var(--app-text);
}

.image-uploader :deep(.el-upload-dragger) {
  padding: var(--app-space-5);
  border-radius: var(--app-radius-lg);
  border-color: var(--app-border);
}

.uploaded-preview {
  position: relative;
  width: 100%;
  height: 220px;
  overflow: hidden;
  border-radius: var(--app-radius-md);
}

.preview-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.replace-mask {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  background: var(--el-overlay-color-lighter);
  color: var(--el-color-white);
  font-size: var(--app-text-xs);
  text-align: center;
  padding: 6px;
}

.sample-actions {
  margin-top: var(--app-space-4);
}

.sample-label {
  display: block;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin-bottom: 6px;
}

.sample-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-2);
}

.empty-diag-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 380px;
  color: var(--app-text-placeholder);
}

.empty-icon {
  margin-bottom: var(--app-space-3);
}

.empty-text {
  font-size: var(--app-text-base);
  max-width: 320px;
  text-align: center;
}

.diag-report {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-4);
}

.disease-headline {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-lg);
  padding: var(--app-space-4);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.dh-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.crop-tag {
  background: var(--app-hover);
  color: var(--app-text-secondary);
  padding: 4px 8px;
  border-radius: var(--app-radius-sm);
  font-size: var(--app-text-xs);
  font-weight: 600;
}

.disease-name {
  font-size: var(--app-text-xl);
  font-weight: 700;
  color: var(--app-text);
}

.dh-right {
  text-align: right;
}

.conf-title {
  display: block;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.conf-value {
  font-size: var(--app-text-2xl);
  font-weight: 700;
}

.conf-value.high {
  color: var(--el-color-success);
}

.conf-value.low {
  color: var(--el-color-danger);
}

.expert-ticket-alert {
  background: var(--el-color-warning-light-9);
  border: 1px solid var(--el-color-warning-light-7);
  border-radius: var(--app-radius-md);
  padding: 12px 16px;
  display: flex;
  gap: var(--app-space-3);
  align-items: flex-start;
}

.alert-icon {
  font-size: 22px;
  color: var(--el-color-warning);
  margin-top: 2px;
}

.alert-text strong {
  color: var(--el-color-warning-dark-2);
  font-size: var(--app-text-base);
}

.alert-text p {
  margin: 4px 0 0;
  font-size: var(--app-text-xs);
  color: var(--el-color-warning-dark-2);
}

.sec-label {
  display: block;
  font-size: var(--app-text-sm);
  font-weight: 600;
  color: var(--app-text-secondary);
  margin-bottom: 8px;
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-2);
}

.advice-box,
.treatment-box {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-md);
  padding: 14px;
}

.advice-content,
.treatment-content {
  margin: 0;
  font-size: var(--app-text-sm);
  color: var(--app-text);
  line-height: 1.6;
}

.treatment-content {
  color: var(--el-color-primary-dark-2);
  font-weight: 500;
}

/* ================= 专家端 ================= */
.expert-workbench-layout {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: var(--app-space-5);
  min-height: 600px;
}

.tickets-list-col {
  padding: var(--app-space-4);
  display: flex;
  flex-direction: column;
}

.tickets-filter-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}

.tickets-scroll-wrap {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ticket-item-card {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-md);
  padding: var(--app-space-3);
  cursor: pointer;
  transition: var(--app-transition);
}

.ticket-item-card:hover {
  border-color: var(--app-border);
}

.ticket-item-card.active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
}

.tic-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.tic-id {
  font-size: var(--app-text-xs);
  font-weight: 600;
  color: var(--el-color-primary-dark-2);
}

.tic-disease {
  font-size: var(--app-text-base);
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 6px;
}

.tic-meta {
  display: flex;
  justify-content: space-between;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.ticket-detail-col {
  padding: var(--app-space-5);
  overflow-y: auto;
}

.empty-detail-box {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 400px;
  color: var(--app-text-placeholder);
  font-size: var(--app-text-base);
}

.detail-header-card {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--app-space-3);
  padding-bottom: var(--app-space-4);
  border-bottom: 1px solid var(--app-border-light);
  margin-bottom: var(--app-space-5);
}

/* 认领人文字与按钮间距 */
.dh-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--app-space-3);
}

.dh-title {
  font-size: var(--app-text-xl);
  font-weight: 700;
  color: var(--app-text);
}

.consult-columns {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.2fr);
  gap: var(--app-space-5);
}

.consult-col {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-lg);
  padding: var(--app-space-4);
  display: flex;
  flex-direction: column;
}

.col-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 14px;
  font-size: var(--app-text-base);
  font-weight: 600;
  color: var(--app-text);
  border-bottom: 1px solid var(--app-border);
  padding-bottom: 8px;
}

.detail-img-box {
  width: 100%;
  height: 180px;
  background: var(--app-hover);
  border-radius: var(--app-radius-md);
  overflow: hidden;
  margin-bottom: var(--app-space-3);
}

.consult-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.no-img {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--app-text-placeholder);
  font-size: var(--app-text-xs);
}

.sub-label {
  display: block;
  font-size: var(--app-text-xs);
  font-weight: 600;
  color: var(--app-text-secondary);
  margin-bottom: 4px;
}

.symptom-text {
  margin-top: 10px;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.env-group {
  margin-bottom: 14px;
  background: var(--app-surface);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-sm);
  padding: 10px;
}

.env-group-title {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--app-text-xs);
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 6px;
}

.env-kv {
  display: flex;
  justify-content: space-between;
  font-size: var(--app-text-xs);
  margin-bottom: 4px;
  color: var(--app-text-secondary);
}

.weather-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--app-text-xs);
  color: var(--el-color-primary-dark-2);
}

.mini-timeline {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.knowledge-loop-box {
  background: var(--el-color-success-light-9);
  border: 1px solid var(--el-color-success-light-7);
  border-radius: var(--app-radius-md);
  padding: var(--app-space-3);
  margin-top: 10px;
  overflow: hidden; /* 兜底防溢出 */
}

/* 勾选框标题允许换行：(Human-in-the-Loop) 英文串在窄栏不再顶破右边界 */
.knowledge-loop-box .el-checkbox {
  display: flex;
  align-items: flex-start;
  max-width: 100%;
  height: auto;
}

.knowledge-loop-box .el-checkbox :deep(.el-checkbox__input) {
  margin-top: 2px;
}

.knowledge-loop-box .el-checkbox :deep(.el-checkbox__label) {
  min-width: 0;
  white-space: normal;
  overflow-wrap: break-word;
  line-height: 1.5;
}

.loop-tip {
  margin: 6px 0 0;
  font-size: var(--app-text-xs);
  color: var(--el-color-success-dark-2);
  line-height: 1.4;
}

.text-muted {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

.tickets-pagination-bar {
  padding: 8px 12px;
  display: flex;
  justify-content: center;
  background: var(--app-surface);
  border-top: 1px solid var(--app-border-light);
}

/* 移动端与平板响应式适配 */
@media (max-width: 1024px) {
  .consult-columns {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 860px) {
  .farmer-view-layout {
    grid-template-columns: minmax(0, 1fr);
    gap: 14px;
  }

  .expert-workbench-layout {
    grid-template-columns: minmax(0, 1fr);
    gap: 14px;
  }

  .tabs-control {
    flex-wrap: wrap;
    gap: var(--app-space-2);
  }

  .tab-btn {
    padding: 8px 14px;
    font-size: var(--app-text-sm);
  }

  .header-card {
    padding: var(--app-space-3) var(--app-space-4);
    margin-bottom: 14px;
  }

  .uploaded-preview {
    height: 180px;
  }
}
</style>
