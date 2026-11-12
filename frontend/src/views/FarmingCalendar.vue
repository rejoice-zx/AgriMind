<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useFarmSelectionStore } from '@/stores/farmSelection'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Warning,
  Plus,
  CloseBold,
  Clock,
  CircleCheck,
  Cpu,
  Notebook,
  Document
} from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'

import {
  getFieldsByFarm,
  getCropPlantings,
  getFarmingRecords,
  createFarmingRecord,
  generateAgriPlan,
  applyAgriPlan
} from '@/api/agriculture'
import type {
  FieldItem,
  CropPlanting,
  FarmingRecord,
  FarmingRecordCreate,
  AgriPlanResponse,
  PlanItem
} from '@/types/agriculture'
import FieldWeatherPanel from '@/components/FieldWeatherPanel.vue'
import { businessDate } from '@/utils/businessDate'
import { useAuthStore } from '@/stores/auth'
import { newRequestId } from '@/utils/requestId'
import { draftOwnerKey } from '@/utils/scopedDraft'
import { saveFarmingDraft, readFarmingDraft, clearFarmingDraft } from '@/utils/farmingDraft'
import { recordKind, RECORD_TYPE_OPTIONS, PHI_MAX_DAYS } from '@/constants/agri'
import { errorText } from '@/utils/errorText'
import SourceBadge from '@/components/common/SourceBadge.vue'
import { planConfirmationReason, planExecutionLabel, markPlanItemsCompleted, planProofNeedsRenewal, renewedPlanItems, planUpdateLabel } from '@/utils/planConfirmation'

// ==================== 状态 ====================

const loading = ref(false)
const planLoading = ref(false)
const recordsLoading = ref(false)
const cropLoading = ref(false)
const applyingPlan = ref(false)
const calendarDate = ref(new Date())

const farmSelection = useFarmSelectionStore()
const { farms, selectedFarmId } = storeToRefs(farmSelection)
let farmContextReady = false
let alive = true
watch(selectedFarmId, id => { if (farmContextReady) void handleFarmChange(id) }, { flush: 'sync' })
const fields = ref<FieldItem[]>([])
const selectedFieldId = ref<number | null>(null)
let fieldRequestVersion = 0
let farmRequestVersion = 0
let planRequestVersion = 0
let planTimer: ReturnType<typeof setInterval> | undefined
let lastPlanCheck = 0
const activeCrop = ref<CropPlanting | null>(null)
const activeCrops = ref<CropPlanting[]>([])
function selectPlanting(id: number) {
  ++planRequestVersion; planLoading.value = false
  activeCrop.value = activeCrops.value.find(crop => crop.id === id) || null
  currentPlan.value = null
  if (selectedFieldId.value) void fetchPlan(selectedFieldId.value)
}

// 历史履历与未来计划
const farmingRecords = ref<FarmingRecord[]>([])
const currentPlan = ref<AgriPlanResponse | null>(null)

// 弹窗状态：手动记一笔农事
const recordDialogVisible = ref(false)
const recordSaving = ref(false)
const auth = useAuthStore()
let restoringDraft = false
let calendarDraftOwner: string | undefined
const recordDraftNotice = ref('')
const recordForm = ref<FarmingRecordCreate>({
  field_id: 0,
  record_type: 'fertilization',
  operation_date: businessDate(),
  operation_content: '',
  chemical_name: '',
  dosage: '',
  dilution_ratio: '',
  safety_interval_days: undefined,
  operator: '',
  notes: ''
})
watch(recordForm, form => {
  const owner = draftOwnerKey(auth.user)
  if (!restoringDraft && owner && owner === calendarDraftOwner && recordDialogVisible.value && form.field_id === selectedFieldId.value) {
    recordDraftNotice.value = saveFarmingDraft(owner, form) ? '本机草稿已保存，重试复用提交编号' : '本机无法保存草稿，请保留页面'
  }
}, { deep: true, flush: 'sync' })

// 选中的具体日期（格式 YYYY-MM-DD）
const selectedDateStr = ref(businessDate())

// 按日期索引的数据映射
const recordsByDate = computed(() => {
  const map: Record<string, FarmingRecord[]> = {}
  for (const rec of farmingRecords.value) {
    const d = rec.operation_date || rec.created_at?.split('T')[0]
    if (d) {
      if (!map[d]) map[d] = []
      map[d].push(rec)
    }
  }
  return map
})

const plansByDate = computed(() => {
  const map: Record<string, PlanItem[]> = {}
  if (currentPlan.value?.items) {
    for (const item of currentPlan.value.items) {
      if (!map[item.date]) map[item.date] = []
      map[item.date].push(item)
    }
  }
  return map
})

// 当前选中日期的记录与计划
const selectedDateRecords = computed(() => {
  return recordsByDate.value[selectedDateStr.value] || []
})

const selectedDatePlans = computed(() => {
  return plansByDate.value[selectedDateStr.value] || []
})

// ==================== 初始化与加载 ====================

async function initCalendar() {
  loading.value = true
  farmContextReady = false
  try {
    if (!await farmSelection.loadFarms(30000)) return
    if (!alive) return
    farmContextReady = true
    await handleFarmChange(selectedFarmId.value)
  } catch (err: any) {
    ElMessage.error('加载农场失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    loading.value = false
  }
}

async function handleFarmChange(farmId: number | null) {
  const version = ++farmRequestVersion
  ++fieldRequestVersion
  selectedFarmId.value = farmId
  selectedFieldId.value = null; fields.value = []; activeCrop.value = null; activeCrops.value = []; currentPlan.value = null; farmingRecords.value = []; planLoading.value = false
  recordsLoading.value = false; cropLoading.value = false
  if (farmId === null) return
  try {
    const res = await getFieldsByFarm(farmId)
    if (version !== farmRequestVersion) return
    fields.value = res.data.items || []
    if (fields.value.length > 0) {
      selectedFieldId.value = fields.value[0].id
      void handleFieldChange(fields.value[0].id)
    } else {
      selectedFieldId.value = null
      farmingRecords.value = []
      currentPlan.value = null
    }
  } catch (err: any) {
    if (version !== farmRequestVersion) return
    ElMessage.error('加载田块失败: ' + (err.response?.data?.detail || err.message))
  }
}

async function handleFieldChange(fieldId: number) {
  const version = ++fieldRequestVersion
  selectedFieldId.value = fieldId
  currentPlan.value = null
  farmingRecords.value = []
  activeCrop.value = null
  activeCrops.value = []
  ++planRequestVersion; planLoading.value = false; cropLoading.value = true
  recordsLoading.value = true
  await Promise.allSettled([
    getCropPlantings(fieldId, 'active').then(async res => {
      if (alive && version === fieldRequestVersion) {
        activeCrops.value = res.data.items || []
        activeCrop.value = activeCrops.value.length === 1 ? activeCrops.value[0] : null
        cropLoading.value = false
        if (activeCrops.value.length <= 1) await fetchPlan(fieldId, version)
      }
    }).catch(() => {
      if (version === fieldRequestVersion) ElMessage.warning('作物信息加载失败，请刷新重试')
    }).finally(() => { if (version === fieldRequestVersion) cropLoading.value = false }),
    getFarmingRecords(fieldId).then(res => {
      if (version === fieldRequestVersion) farmingRecords.value = res.data || []
    }).catch(() => {
      if (version === fieldRequestVersion) ElMessage.warning('农事履历加载失败，请刷新重试')
    }).finally(() => { if (version === fieldRequestVersion) recordsLoading.value = false }),
  ])
}

async function fetchPlan(fieldId: number, version = fieldRequestVersion, forceRefresh = false) {
  if (activeCrops.value.length > 1 && !activeCrop.value) { ElMessage.warning('请先选择作物批次'); return }
  const planVersion = ++planRequestVersion
  planLoading.value = true
  try {
    const res = await generateAgriPlan({ field_id: fieldId, crop_planting_id: activeCrop.value?.id, days: 14, force_refresh: forceRefresh })
    if (selectedFieldId.value === fieldId && version === fieldRequestVersion && planVersion === planRequestVersion) {
      currentPlan.value = res.data
      lastPlanCheck = Date.now()
    }
  } catch (err: any) {
    if (version !== fieldRequestVersion || planVersion !== planRequestVersion) return
    console.warn('推演计划提示:', err)
    ElMessage.warning('建议核查失败，当前展示上次成功结果，请稍后刷新')
  } finally {
    if (version === fieldRequestVersion && planVersion === planRequestVersion) planLoading.value = false
  }
}

function refreshPlanIfNeeded() {
  if (!alive || document.hidden || !selectedFieldId.value || cropLoading.value || planLoading.value || applyingPlan.value || (activeCrops.value.length > 1 && !activeCrop.value)) return
  if (currentPlan.value?.start_date !== businessDate() || Date.now() - lastPlanCheck >= 600000) {
    void fetchPlan(selectedFieldId.value)
  }
}

async function renewConfirmation(items: PlanItem[], fieldId: number) {
  const version = fieldRequestVersion
  const cropIds = new Set(items.map(item => item.crop_planting_id))
  const cropId = items[0]?.crop_planting_id
  if (cropIds.size !== 1 || cropId == null) throw new Error('请确认同一明确批次的事项')
  const selectedCropId = activeCrop.value?.id
  if (selectedCropId != null && selectedCropId !== cropId) throw new Error('当前批次与待确认事项不同，请查看当前计划')
  const planVersion = ++planRequestVersion
  planLoading.value = true
  try {
    const res = await generateAgriPlan({ field_id: fieldId, crop_planting_id: cropId, days: 14 })
    if (!alive || version !== fieldRequestVersion || selectedFieldId.value !== fieldId || selectedCropId !== activeCrop.value?.id || planVersion !== planRequestVersion) throw new Error('田块、批次或计划已切换，请查看当前计划后重新确认')
    currentPlan.value = res.data
    lastPlanCheck = Date.now()
    return renewedPlanItems(items, res.data)
  } finally { if (version === fieldRequestVersion && planVersion === planRequestVersion) planLoading.value = false }
}

function handleDateClick(dateStr: string) {
  selectedDateStr.value = dateStr
}

// 在日历上选了未来日期再点"记一笔"时，默认改为今天（实绩只能记已发生的事）
function recordDateFor(day: string) { return day > businessDate() ? businessDate() : day }

function openAddRecordDialog() {
  if (!selectedFieldId.value) {
    ElMessage.warning('请先选择农场与田块')
    return
  }
  restoringDraft = true
  const owner = draftOwnerKey(auth.user)
  calendarDraftOwner = owner
  const draft = owner ? readFarmingDraft(owner, selectedFieldId.value) : null
  if (draft) {
    recordForm.value = draft; recordDraftNotice.value = `已恢复草稿：批次#${draft.crop_planting_id ?? '未绑定'}，日期${draft.operation_date}；重试复用原编号，不自动改绑批次`
    recordDialogVisible.value = true; restoringDraft = false; return
  }
  recordForm.value.request_id = undefined
  recordForm.value = { field_id: selectedFieldId.value, crop_planting_id: activeCrop.value?.id,
    record_type: recordForm.value.record_type, operation_date: recordDateFor(selectedDateStr.value), operation_content: '', safety_interval_days: undefined }
  recordDialogVisible.value = true
  restoringDraft = false
}

async function submitRecord() {
  if (recordSaving.value) return
  if (!recordForm.value.operation_content && !recordForm.value.chemical_name) {
    ElMessage.warning('请填写农事操作说明或投入品名称')
    return
  }
  recordSaving.value = true
  recordForm.value.request_id ||= newRequestId()
  const submitted = { ...recordForm.value }
  const owner = draftOwnerKey(auth.user)
  try {
    const response = await createFarmingRecord(submitted)
    ElMessage.success(response.data.replayed ? '已恢复原记录，未重复登记' : '农事实绩记录已保存')
    if (owner === draftOwnerKey(auth.user) && submitted.field_id === selectedFieldId.value && JSON.stringify(recordForm.value) === JSON.stringify(submitted)) {
      if (owner) clearFarmingDraft(owner, submitted.field_id)
      recordDialogVisible.value = false
    } else if (owner === draftOwnerKey(auth.user) && submitted.field_id === selectedFieldId.value && recordForm.value.request_id === submitted.request_id) {
      recordForm.value.request_id = newRequestId()
    }
  } catch (err: any) {
    ElMessage.error('保存失败: ' + (err.response?.data?.detail || err.message))
    recordSaving.value = false
    return
  }
  // 保存已成功；刷新失败不能再提示“保存失败”，否则用户会重复登记（BUG-026）
  try {
    if (selectedFieldId.value === submitted.field_id) {
      const recRes = await getFarmingRecords(submitted.field_id)
      if (selectedFieldId.value === submitted.field_id) {
        farmingRecords.value = recRes.data || []
        void fetchPlan(submitted.field_id)
      }
    }
  } catch {
    ElMessage.warning('记录已保存，但履历列表刷新失败，请稍后手动刷新核对')
  } finally { recordSaving.value = false }
}

async function discardCalendarDraft() {
  const fieldId = selectedFieldId.value
  const owner = draftOwnerKey(auth.user)
  if (recordSaving.value || !fieldId || !owner) return
  try {
    await ElMessageBox.confirm('如上次提交超时，请先核对履历。清空只影响草稿，下一笔将使用新提交编号。', '重新登记')
    if (fieldId !== selectedFieldId.value || owner !== draftOwnerKey(auth.user)) return
    restoringDraft = true
    clearFarmingDraft(owner, fieldId)
    recordForm.value = { field_id: fieldId, crop_planting_id: activeCrop.value?.id, record_type: 'fertilization', operation_date: recordDateFor(selectedDateStr.value), operation_content: '' }
    recordDraftNotice.value = ''; restoringDraft = false
  } catch { /* Keep the existing draft when cancellation occurs. */ }
}

const applyingKey = ref('')
const planItemKey = (item: PlanItem) => `${item.date}:${item.record_type}:${item.action}`

async function handleApplyTask(item: PlanItem) {
  if (!selectedFieldId.value || applyingPlan.value) return
  const blocked = planConfirmationReason(item)
  if (blocked) { ElMessage.warning(blocked); return }
  const fieldId = selectedFieldId.value
  applyingPlan.value = true
  applyingKey.value = planItemKey(item)
  try {
    if (planProofNeedsRenewal(item)) item = (await renewConfirmation([item], fieldId))[0]!
    const res = await applyAgriPlan({
      field_id: fieldId,
      items: [item],
      // 后端以登录账号作为签署人；这里只作为显示用的执行人
      operator: auth.user?.display_name || auth.user?.username || ''
    })
    markPlanItemsCompleted(currentPlan.value, fieldId, [item], res.data.record_ids || [])
    ElMessage.success(res.data.message || '计划项已确认执行并落库为农事记录')

    try {
      const recRes = await getFarmingRecords(fieldId)
      if (selectedFieldId.value === fieldId) farmingRecords.value = recRes.data || []
    } catch { ElMessage.warning('事项已确认，履历刷新失败，请刷新页面查看') }
  } catch (err: unknown) {
    ElMessage.error(errorText(err, '确认失败，请刷新后重试'))
  } finally { applyingPlan.value = false; applyingKey.value = '' }
}

// 农事类型：历史数据里中英文同义值统一归类显示
const getRecordTypeTag = (type: string) => recordKind(type).tag
const recordTypeLabel = (type: string) => recordKind(type).label
// 已执行实绩不能登记在未来
const disableFutureDate = (date: Date) => date.getTime() > new Date(`${businessDate()}T23:59:59`).getTime()
const recordTypeOptions = RECORD_TYPE_OPTIONS
const phiMax = PHI_MAX_DAYS

onMounted(() => {
  initCalendar()
  planTimer = setInterval(refreshPlanIfNeeded, 60000)
  document.addEventListener('visibilitychange', refreshPlanIfNeeded)
})
onBeforeUnmount(() => {
  alive = false; farmContextReady = false; ++farmRequestVersion; ++fieldRequestVersion
  clearInterval(planTimer)
  document.removeEventListener('visibilitychange', refreshPlanIfNeeded)
})
</script>

<template>
  <div class="app-page calendar-page" v-loading="loading">
    <!-- 头部工具栏 -->
    <PageHeader
      title="农事日历"
      description="已执行的农事履历与系统检查提醒。检查提醒只是巡查清单，不是农艺处方。"
    >
      <template #actions>
        <el-select
          v-model="selectedFarmId"
          placeholder="选择农场"
          class="ctx-select"
          aria-label="选择农场"
        >
          <template #prefix><span class="ctx-prefix">农场</span></template>
          <el-option
            v-for="f in farms"
            :key="f.id"
            :label="f.name"
            :value="f.id"
          />
        </el-select>

        <el-select
          v-model="selectedFieldId"
          placeholder="选择田块"
          class="ctx-select ctx-select--narrow"
          aria-label="选择田块"
          @change="handleFieldChange"
        >
          <template #prefix><span class="ctx-prefix">田块</span></template>
          <el-option
            v-for="fd in fields"
            :key="fd.id"
            :label="fd.name"
            :value="fd.id"
          />
        </el-select>

        <el-select v-if="activeCrops.length > 1" :model-value="activeCrop?.id" :disabled="applyingPlan" placeholder="选择作物批次" class="ctx-select" aria-label="选择作物批次" @change="selectPlanting">
          <template #prefix><span class="ctx-prefix">批次</span></template>
          <el-option v-for="crop in activeCrops" :key="crop.id" :value="crop.id" :label="`${crop.crop} · ${crop.planting_date} · #${crop.id}`" />
        </el-select>
        <el-button :loading="planLoading" :disabled="!selectedFieldId || applyingPlan || cropLoading"
          @click="selectedFieldId && fetchPlan(selectedFieldId, fieldRequestVersion, true)">刷新检查提醒</el-button>
        <el-button
          type="primary"
          :icon="Plus"
          @click="openAddRecordDialog"
        >
          记一笔农事
        </el-button>
      </template>
    </PageHeader>

    <!-- 主体：左侧月度日历 + 右侧选中日期作业清单 -->
    <div class="calendar-layout">
      <!-- 左侧：Element Plus 月度日历卡片 -->
      <div class="calendar-main-card">
        <el-card shadow="never" class="inner-card">
          <div class="calendar-legend" aria-label="图例">
            <span><i class="legend-dot bg-record" />已执行履历（实心）</span>
            <span><i class="legend-dot legend-dot--hollow bg-plan" />检查提醒（空心）</span>
          </div>
          <el-calendar v-model="calendarDate">
            <template #date-cell="{ data }">
              <div
                class="cell-wrapper"
                :class="{ 'cell-active': data.day === selectedDateStr }"
                @click="handleDateClick(data.day)"
              >
                <div class="day-number">{{ data.day.split('-')[2] }}</div>

                <!-- 历史实绩小圆点 -->
                <div v-if="recordsByDate[data.day]" class="cell-event-badge bg-record" :aria-label="`${recordsByDate[data.day].length} 条已执行履历`">
                  <span>{{ recordsByDate[data.day].length }} 履历</span>
                </div>

                <!-- 智能计划小圆点 -->
                <div v-if="plansByDate[data.day]" class="cell-event-badge bg-plan" :aria-label="`${plansByDate[data.day].length} 条检查提醒`">
                  <span>{{ plansByDate[data.day].length }} 提醒</span>
                </div>
              </div>
            </template>
          </el-calendar>
        </el-card>
      </div>

      <!-- 右侧：选中日期的作业任务详情 -->
      <div class="calendar-side-card">
        <el-card shadow="never" class="inner-card">
          <template #header>
            <div class="side-header">
              <span class="side-title"><el-icon><Document /></el-icon> {{ selectedDateStr }} 农事明细</span>

            </div>
          </template>

          <FieldWeatherPanel :field-id="selectedFieldId" :days="14" :selected-date="selectedDateStr" />
          <el-divider />
          <!-- 1. 计划推演建议项 -->
          <div class="side-section-title">
            <span><el-icon><Cpu /></el-icon> 系统检查提醒</span>
            <SourceBadge kind="ai" label="系统生成 · 非处方" />
          </div>

          <div v-if="currentPlan" class="plan-metadata">
            <p>{{ planUpdateLabel(currentPlan) }}</p>
            <p>{{ currentPlan.update_reason }} · 检查建议不代替现场农艺判断。</p>
            <details v-if="currentPlan.changes?.length">
              <summary>本次更新说明</summary>
              <p v-for="change in currentPlan.changes" :key="change">{{ change }}</p>
            </details>
          </div>
          <div v-if="planLoading" class="empty-hint">正在读取或核查作业建议…</div>
          <div v-else-if="selectedDatePlans.length > 0" class="plan-items-list">
            <div
              v-for="(p, idx) in selectedDatePlans"
              :key="idx"
              class="side-task-card"
              :class="{ 'task-blocked': !p.can_proceed }"
            >
              <div class="side-task-header">
                <span class="task-action-name">{{ p.action }}</span>
                <span v-if="p.execution_status === 'completed'" class="status-safe">
                  <el-icon><CircleCheck /></el-icon> 已完成
                </span>
                <span v-else-if="p.execution_status === 'revoked' || p.execution_status === 'unavailable'" class="status-danger">
                  <el-icon><CloseBold /></el-icon> {{ planExecutionLabel(p) }}
                </span>
                <span v-else-if="p.can_proceed" class="status-pending">
                  <el-icon><Clock /></el-icon> 待确认
                </span>
                <span v-else class="status-danger">
                  <el-icon><Warning /></el-icon> 需核实 / 暂缓
                </span>
              </div>
              <p class="task-p">{{ p.description }}</p>
              <div v-if="p.risk_notes" class="task-risk">
                <el-icon><Warning /></el-icon> {{ p.risk_notes }}
              </div>
              <div class="task-foot">
                <span class="task-src">{{ p.source }}</span>
                <el-button
                  type="success"
                  size="small"
                  plain
                  :disabled="!!planConfirmationReason(p) || applyingPlan || planLoading"
                  :loading="applyingKey === planItemKey(p)"
                  @click="handleApplyTask(p)"
                >
                  {{ planExecutionLabel(p, '确认今日已完成') }}
                </el-button>
                <small v-if="planConfirmationReason(p)">{{ planConfirmationReason(p) }}</small>
              </div>
            </div>
          </div>
          <div v-else class="empty-hint">当日没有检查提醒</div>

          <el-divider style="margin: 16px 0" />

          <!-- 2. 当日已执行农事实绩 -->
          <div class="side-section-title">
            <span><el-icon><Notebook /></el-icon> 当日已执行农事履历</span>
            <SourceBadge kind="record" />
          </div>

          <div v-if="recordsLoading" class="empty-hint">正在加载农事履历…</div>
          <div v-else-if="selectedDateRecords.length > 0" class="records-list">
            <div
              v-for="r in selectedDateRecords"
              :key="r.id"
              class="side-record-card"
            >
              <div class="side-record-header">
                <el-tag size="small" :type="getRecordTypeTag(r.record_type)">
                  {{ recordTypeLabel(r.record_type) }}
                </el-tag>
                <span class="side-record-op">执行人：{{ r.operator || '未填写' }}</span>
              </div>
              <div class="side-record-content">
                {{ r.operation_content || r.chemical_name }}
              </div>
              <div v-if="r.safety_interval_days != null" class="phi-tag">
                安全间隔期 {{ r.safety_interval_days }} 天（录入值，以产品标签为准）
              </div>
            </div>
          </div>
          <div v-else class="empty-hint">当日还没有农事记录</div>
        </el-card>
      </div>
    </div>

    <!-- 弹窗：手工记录一笔农事 -->
    <el-dialog
      v-model="recordDialogVisible"
      title="记录已完成的农事"
      width="520px"
      destroy-on-close
    >
      <el-form label-position="top" :disabled="recordSaving">
        <el-alert v-if="recordDraftNotice" :title="recordDraftNotice" type="info" :closable="false" />
        <el-button link @click="discardCalendarDraft">清空草稿，另记一笔</el-button>
        <el-form-item label="农事作业类型">
          <el-select v-model="recordForm.record_type" style="width: 100%">
            <el-option v-for="option in recordTypeOptions" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>

        <el-form-item label="作业日期">
          <el-date-picker
            v-model="recordForm.operation_date"
            type="date"
            placeholder="选择作业日期"
            value-format="YYYY-MM-DD"
            :disabled-date="disableFutureDate"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="详细作业内容说明">
          <el-input
            v-model="recordForm.operation_content"
            type="textarea"
            :rows="2"
            placeholder="例如：滴灌追施平衡型高钾水溶肥5kg/亩，补充微量元素"
          />
        </el-form-item>

        <template v-if="recordKind(recordForm.record_type).key === 'spraying'">
          <el-form-item label="农药名称（产品标签上的完整名称）">
            <el-input v-model="recordForm.chemical_name" placeholder="例如：250克/升嘧菌酯悬浮剂" />
          </el-form-item>
          <el-form-item label="用量">
            <el-input v-model="recordForm.dosage" placeholder="例如：40 ml/亩" />
          </el-form-item>
          <el-form-item label="安全间隔期（天，按产品标签填写）">
            <el-input-number
              v-model="recordForm.safety_interval_days"
              :min="0"
              :max="phiMax"
              placeholder="未知可留空"
              class="phi-input"
            />
            <p class="app-note">不知道请留空，不要填 0；采收前核查会把未知间隔期当作风险提示。</p>
          </el-form-item>
        </template>

        <el-form-item label="作业人员">
          <el-input v-model="recordForm.operator" placeholder="例如：李农艺师" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="recordDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="recordSaving" :disabled="recordSaving" @click="submitRecord">保存农事记录</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.plan-metadata { margin: 12px 0 18px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.plan-metadata p { margin: 4px 0; }
.plan-metadata summary { cursor: pointer; }
.calendar-page {
  min-height: calc(100vh - 60px);
}

.calendar-layout {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: var(--app-space-5);
}

@media (max-width: 1100px) {
  .calendar-layout {
    grid-template-columns: 1fr;
  }
}

.inner-card {
  border-radius: var(--app-radius-lg);
  border: 1px solid var(--app-border-light);
  background: var(--app-surface);
}

/* 日历格子定制 */
.cell-wrapper {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 4px;
  cursor: pointer;
  border-radius: var(--app-radius-sm);
  transition: var(--app-transition);
}

.cell-wrapper:hover {
  background-color: var(--el-color-primary-light-9);
}

.cell-active {
  background-color: var(--el-color-primary-light-8) !important;
  border: 1px solid var(--el-color-primary-light-3);
}

.day-number {
  font-size: var(--app-text-sm);
  font-weight: 600;
  color: var(--app-text);
}

.cell-event-badge {
  font-size: var(--app-text-xs);
  padding: 2px 4px;
  border-radius: var(--app-radius-sm);
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.bg-record {
  background-color: var(--app-surface-2);
  color: var(--app-text-secondary);
  font-weight: 600;
}

.bg-plan {
  background-color: var(--el-color-primary-light-9);
  color: var(--el-color-primary-dark-2);
  font-weight: 600;
}

/* 右侧任务卡片 */
.side-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.side-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--app-text-base);
  font-weight: 700;
  color: var(--app-text);
}

.side-section-title {
  font-size: var(--app-text-sm);
  font-weight: 700;
  color: var(--el-color-primary-dark-2);
  margin-bottom: 10px;
}

.side-section-title span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.plan-items-list, .records-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.side-task-card {
  padding: var(--app-space-3);
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-md);
}

.task-blocked {
  background: var(--el-color-danger-light-9);
  border-color: var(--el-color-danger-light-7);
}

.side-task-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.task-action-name {
  font-size: var(--app-text-base);
  font-weight: 700;
  color: var(--app-text);
}

.status-safe {
  font-size: var(--app-text-xs);
  color: var(--el-color-primary-dark-2);
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.status-danger {
  font-size: var(--app-text-xs);
  color: var(--state-warning);
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.status-pending {
  font-size: var(--app-text-xs);
  color: var(--state-info);
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

/* 上下文选择器：框内带"农场/田块/批次"前缀，未展开也能看出用途 */
.ctx-select {
  width: 260px;
}

.ctx-select--narrow {
  width: 200px;
}

.ctx-prefix {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  padding-right: 6px;
  margin-right: 2px;
  border-right: 1px solid var(--app-border-light);
  line-height: 1;
}

@media (max-width: 767px) {
  .ctx-select,
  .ctx-select--narrow {
    width: 100%;
  }
}

.phi-input {
  width: min(200px, 100%);
}

/* 日历图例：履历实心、提醒空心（形状 + 颜色双编码，不只靠颜色） */
.calendar-legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-4);
  margin-bottom: var(--app-space-2);
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.calendar-legend span {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-1);
}

.legend-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.legend-dot.bg-record {
  background: var(--app-text-placeholder);
}

.legend-dot--hollow.bg-plan {
  background: transparent;
  border: 2px solid var(--el-color-primary);
}

.task-p {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin: 0 0 6px 0;
  line-height: 1.4;
}

.task-risk {
  font-size: var(--app-text-xs);
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  padding: 2px 6px;
  border-radius: var(--app-radius-sm);
  margin-bottom: 6px;
}

.task-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.task-foot small {
  flex-basis: 100%;
  color: var(--app-text-secondary);
  line-height: 1.5;
}

.task-src {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

.empty-hint {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
  padding: 8px 0;
}

.side-record-card {
  padding: 10px;
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-sm);
}

.side-record-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.side-record-op {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

.side-record-content {
  font-size: var(--app-text-xs);
  color: var(--app-text);
}

.phi-tag {
  font-size: var(--app-text-xs);
  color: var(--el-color-danger);
  margin-top: 4px;
  font-weight: 600;
}

/* 移动端（≤767px）：头部紧凑 + 格内徽章改圆点 */
@media (max-width: 767px) {
  /* 日历头部：允许换行、降低高度，标题小字号 */
  .calendar-main-card :deep(.el-calendar__header) {
    flex-wrap: wrap;
    height: auto;
    padding: 8px 12px;
    row-gap: 6px;
  }

  .calendar-main-card :deep(.el-calendar__title) {
    font-size: var(--app-text-sm);
    font-weight: 600;
  }

  /* 格子行高压缩（37px 格宽下多留纵向空间） */
  .calendar-main-card :deep(.el-calendar-day) {
    height: 56px;
    padding: 2px;
  }

  .cell-wrapper {
    align-items: center;
    padding: 2px;
  }

  .day-number {
    font-size: var(--app-text-xs);
  }

  /* 履历/建议徽章：窄格下文字不可读，改为 8px 圆点（绿=建议、灰=履历） */
  .cell-event-badge {
    width: 8px;
    height: 8px;
    padding: 0;
    border-radius: 50%;
    margin-top: 3px;
  }

  .cell-event-badge span {
    display: none;
  }

  /* 圆点用实色保证可辨识：绿点=智能建议、灰点=历史履历 */
  .cell-event-badge.bg-record {
    background: var(--app-text-placeholder);
  }

  .cell-event-badge.bg-plan {
    background: transparent;
    border: 2px solid var(--el-color-primary);
  }
}
</style>
