<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useFarmSelectionStore } from '@/stores/farmSelection'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  OfficeBuilding,
  Sunny,
  Calendar,
  Warning,
  Refresh,
  Histogram,
  CircleCheck,
  CloseBold,
  Clock,
  Bell,
  InfoFilled,
  Document
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { recordKind } from '@/constants/agri'
import { errorText } from '@/utils/errorText'
import SourceBadge from '@/components/common/SourceBadge.vue'

import {
  getFieldsByFarm,
  getCropPlantings,
  getFarmingRecords,
  generateAgriPlan,
  applyAgriPlan
} from '@/api/agriculture'
import type {
  FieldItem,
  CropPlanting,
  FarmingRecord,
  AgriPlanResponse,
  PlanItem
} from '@/types/agriculture'
import FieldWeatherPanel from '@/components/FieldWeatherPanel.vue'
import PageHeader from '@/components/PageHeader.vue'
import { businessDate } from '@/utils/businessDate'
import { planConfirmationReason, planExecutionLabel, markPlanItemsCompleted, planProofNeedsRenewal, renewedPlanItems, planUpdateLabel } from '@/utils/planConfirmation'

const auth = useAuthStore()
const planError = ref('')

// ==================== 状态管理 ====================
const loading = ref(false)
const planLoading = ref(false)
const cropLoading = ref(false)
const recordsLoading = ref(false)
const applyingPlan = ref(false)

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
  if (selectedFieldId.value) void fetchAgriPlan(selectedFieldId.value)
}
const recentRecords = ref<FarmingRecord[]>([])

// 智能计划数据
const currentPlan = ref<AgriPlanResponse | null>(null)

// 选中的农场
const currentFarm = computed(() => {
  return farms.value.find(f => f.id === selectedFarmId.value) || null
})

// 选中的地块
const currentField = computed(() => {
  return fields.value.find(f => f.id === selectedFieldId.value) || null
})

// 今日之后、需核实或暂缓的提醒数（施药复核、天气风险等）
const blockedTasksCount = computed(() => {
  if (!currentPlan.value?.items) return 0
  return currentPlan.value.items.filter(item => !item.can_proceed).length
})

// 今日任务：首屏只突出今天；之后几天收进折叠区
const todayTasks = computed(() => {
  if (!currentPlan.value?.items) return []
  const todayStr = businessDate()
  return currentPlan.value.items.filter(item => item.date === todayStr)
})
const upcomingTasks = computed(() => {
  if (!currentPlan.value?.items) return []
  const todayStr = businessDate()
  return currentPlan.value.items.filter(item => item.date > todayStr)
})
const showUpcoming = ref(false)
// 最近一次现场观测/预报之外的"待办"：近期施药记录缺安全间隔期
const sprayMissingPhi = computed(() => recentRecords.value.filter(rec => recordKind(rec.record_type).key === 'spraying' && rec.safety_interval_days == null).length)
const currentOperator = computed(() => auth.user?.display_name || auth.user?.username || '')

const confirmableTodayTasks = computed(() => todayTasks.value.filter(item => !planConfirmationReason(item)))

// ==================== 数据加载 ====================

async function initDashboard() {
  loading.value = true
  farmContextReady = false
  try {
    if (!await farmSelection.loadFarms(30000)) return
    if (!alive) return
    farmContextReady = true
    await handleFarmChange(selectedFarmId.value)
  } catch (err: unknown) {
    ElMessage.error(errorText(err, '加载农场列表失败'))
  } finally {
    loading.value = false
  }
}

async function handleFarmChange(farmId: number | null) {
  const version = ++farmRequestVersion
  ++fieldRequestVersion
  selectedFarmId.value = farmId
  selectedFieldId.value = null; fields.value = []; activeCrop.value = null; activeCrops.value = []; currentPlan.value = null; recentRecords.value = []; planLoading.value = false
  cropLoading.value = false; recordsLoading.value = false
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
      activeCrop.value = null
      currentPlan.value = null
      recentRecords.value = []
    }
  } catch (err: any) {
    if (version !== farmRequestVersion) return
    ElMessage.error(errorText(err, '加载田块失败'))
  }
}

async function handleFieldChange(fieldId: number) {
  const version = ++fieldRequestVersion
  selectedFieldId.value = fieldId
  currentPlan.value = null
  activeCrop.value = null
  activeCrops.value = []
  recentRecords.value = []
  cropLoading.value = true
  planLoading.value = false
  ++planRequestVersion
  recordsLoading.value = true
  await Promise.allSettled([
    getCropPlantings(fieldId, 'active').then(async res => {
      if (!alive || version !== fieldRequestVersion) return
      activeCrops.value = res.data.items || []
      activeCrop.value = activeCrops.value.length === 1 ? activeCrops.value[0]! : null
      cropLoading.value = false
      if (activeCrops.value.length <= 1) await fetchAgriPlan(fieldId, version)
    }).catch(() => {
      if (version === fieldRequestVersion) ElMessage.warning('作物信息加载失败，请刷新重试')
    }).finally(() => { if (version === fieldRequestVersion) cropLoading.value = false }),
    getFarmingRecords(fieldId).then(res => {
      if (version === fieldRequestVersion) recentRecords.value = (res.data || []).slice(0, 8)
    }).catch(() => {
      if (version === fieldRequestVersion) ElMessage.warning('农事履历加载失败，请刷新重试')
    }).finally(() => { if (version === fieldRequestVersion) recordsLoading.value = false }),
  ])
}

async function fetchAgriPlan(fieldId: number, version = fieldRequestVersion, forceRefresh = false) {
  if (activeCrops.value.length > 1 && !activeCrop.value) { ElMessage.warning('请先选择作物批次'); return }
  const planVersion = ++planRequestVersion
  planLoading.value = true
  try {
    const planRes = await generateAgriPlan({
      field_id: fieldId,
      crop_planting_id: activeCrop.value?.id,
      days: 7,
      force_refresh: forceRefresh
    })
    if (selectedFieldId.value === fieldId && version === fieldRequestVersion && planVersion === planRequestVersion) {
      currentPlan.value = planRes.data
      planError.value = ''
      lastPlanCheck = Date.now()
    }
  } catch (err: unknown) {
    if (version !== fieldRequestVersion || planVersion !== planRequestVersion) return
    planError.value = errorText(err, '检查提醒暂时无法更新')
    if (forceRefresh || !currentPlan.value) ElMessage.warning(planError.value)
  } finally {
    if (version === fieldRequestVersion && planVersion === planRequestVersion) planLoading.value = false
  }
}

function refreshPlanIfNeeded() {
  if (!alive || document.hidden || !selectedFieldId.value || cropLoading.value || planLoading.value || applyingPlan.value || (activeCrops.value.length > 1 && !activeCrop.value)) return
  if (currentPlan.value?.start_date !== businessDate() || Date.now() - lastPlanCheck >= 600000) {
    void fetchAgriPlan(selectedFieldId.value)
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
    const res = await generateAgriPlan({ field_id: fieldId, crop_planting_id: cropId, days: 7 })
    if (!alive || version !== fieldRequestVersion || selectedFieldId.value !== fieldId || selectedCropId !== activeCrop.value?.id || planVersion !== planRequestVersion) throw new Error('田块、批次或计划已切换，请查看当前计划后重新确认')
    currentPlan.value = res.data
    lastPlanCheck = Date.now()
    return renewedPlanItems(items, res.data)
  } finally { if (version === fieldRequestVersion && planVersion === planRequestVersion) planLoading.value = false }
}

// 一键执行落库
async function handleApplySingleTask(item: PlanItem) {
  if (!selectedFieldId.value || applyingPlan.value) return
  const blocked = planConfirmationReason(item)
  if (blocked) { ElMessage.warning(blocked); return }
  const fieldId = selectedFieldId.value
  try {
    applyingPlan.value = true
    if (planProofNeedsRenewal(item)) item = (await renewConfirmation([item], fieldId))[0]!
    const res = await applyAgriPlan({
      field_id: fieldId,
      items: [item],
      operator: currentOperator.value
    })
    markPlanItemsCompleted(currentPlan.value, fieldId, [item], res.data.record_ids || [])
    ElMessage.success(res.data.message || '计划项已成功落库为农事执行记录')
    // 刷新农事记录
    try {
      const recRes = await getFarmingRecords(fieldId)
      if (selectedFieldId.value === fieldId) recentRecords.value = (recRes.data || []).slice(0, 8)
    } catch { ElMessage.warning('事项已确认，履历刷新失败，请刷新页面查看') }
  } catch (err: unknown) {
    ElMessage.error(errorText(err, '确认失败，请刷新后重试'))
  } finally {
    applyingPlan.value = false
  }
}

async function handleApplyAllTasks() {
  if (!selectedFieldId.value || !currentPlan.value?.items || applyingPlan.value) return
  const fieldId = selectedFieldId.value
  let validItems = currentPlan.value.items.filter(i => !planConfirmationReason(i))
  if (validItems.length === 0) {
    ElMessage.warning('今日没有可确认的事项，请查看各项核实或复核原因')
    return
  }
  // 批量确认会写入多条"已完成"记录，先列出将要写入的事项
  try {
    await ElMessageBox.confirm(`将把以下 ${validItems.length} 项记为今日已完成（只记录"已检查"，不代表施肥或灌溉）：\n` +
      validItems.map(i => `· ${i.action}`).join('\n'), '确认今日已完成事项',
      { type: 'info', confirmButtonText: `确认 ${validItems.length} 项`, cancelButtonText: '取消' })
  } catch { return }
  if (applyingPlan.value || selectedFieldId.value !== fieldId) return
  try {
    applyingPlan.value = true
    if (validItems.some(planProofNeedsRenewal)) validItems = await renewConfirmation(validItems, fieldId)
    const res = await applyAgriPlan({
      field_id: fieldId,
      items: validItems,
      operator: currentOperator.value
    })
    markPlanItemsCompleted(currentPlan.value, fieldId, validItems, res.data.record_ids || [])
    ElMessage.success(res.data.message)
    try {
      const recRes = await getFarmingRecords(fieldId)
      if (selectedFieldId.value === fieldId) recentRecords.value = (recRes.data || []).slice(0, 8)
    } catch { ElMessage.warning('事项已确认，履历刷新失败，请刷新页面查看') }
  } catch (err: unknown) {
    ElMessage.error(errorText(err, '批量确认失败，请刷新后重试'))
  } finally {
    applyingPlan.value = false
  }
}

function getRecordTypeLabel(type: string) {
  const kind = recordKind(type)
  return { label: kind.label, tag: kind.tag }
}

onMounted(() => {
  initDashboard()
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
  <div class="app-page dashboard-page" v-loading="loading">
    <!-- 统一页面头 -->
    <PageHeader
      title="农场工作台"
      description="当前田块的档案、室外天气参考、今日检查提醒与最近农事记录。"
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
          placeholder="选择田块/大棚"
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

        <el-select v-if="activeCrops.length > 1" :model-value="activeCrop?.id" placeholder="请选择作物批次" :disabled="applyingPlan" class="ctx-select" aria-label="选择作物批次" @change="selectPlanting">
          <template #prefix><span class="ctx-prefix">批次</span></template>
          <el-option v-for="crop in activeCrops" :key="crop.id" :value="crop.id" :label="`${crop.crop} · ${crop.planting_date} · #${crop.id}`" />
        </el-select>

        <el-button
          type="primary"
          plain
          :icon="Refresh"
          :loading="planLoading"
          :disabled="!selectedFieldId || applyingPlan || cropLoading"
          @click="selectedFieldId && fetchAgriPlan(selectedFieldId, fieldRequestVersion, true)"
        >
          刷新提醒
        </el-button>
      </template>
    </PageHeader>

    <!-- 顶部 4 维指标卡片 -->
    <div class="metrics-grid">
      <!-- 卡片 1: 基地田块总览 -->
      <div class="app-card app-card--hover metric-card">
        <div class="metric-content">
          <div class="metric-icon-box">
            <el-icon><OfficeBuilding /></el-icon>
          </div>
          <div class="metric-text">
            <span class="metric-label">当前田块</span>
            <div class="metric-value" :title="currentFarm && currentField ? `${currentFarm.name} · ${currentField.name}` : ''">{{ currentField ? currentField.name : currentFarm ? '该农场还没有田块' : '未选择农场' }}</div>

            <div class="metric-extra">
              <span>面积 <span class="app-num">{{ currentField?.area ?? '未录入' }}</span> 亩</span>
              <span class="dot">·</span>
              <span>土壤: {{ currentField?.soil_type || '未录入' }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 卡片 2: 物候与作物批次 -->
      <div class="app-card app-card--hover metric-card" v-loading="cropLoading">
        <div class="metric-content">
          <div class="metric-icon-box">
            <el-icon><Histogram /></el-icon>
          </div>
          <div class="metric-text">
            <span class="metric-label">在种作物</span>
            <div class="metric-value">
              {{ activeCrop ? `${activeCrop.crop} · ${activeCrop.variety || '品种未登记'}` : activeCrops.length > 1 ? '请选择批次' : '未登记在种作物' }}
            </div>
            <div class="metric-extra">
              <el-tag size="small" type="info" effect="plain">
                {{ activeCrop?.growth_stage || '生育期未录入' }}
              </el-tag>
              <span v-if="activeCrop" class="dot">·</span>
              <span v-if="activeCrop">定植 {{ activeCrop.planting_date }}</span>
              <span v-else-if="activeCrops.length > 1">多个在种批次，请在上方选择</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 卡片 3: 真实微气象适宜度 -->
      <div class="app-card app-card--hover metric-card">
        <div class="metric-content">
          <div class="metric-icon-box">
            <el-icon><Sunny /></el-icon>
          </div>
          <div class="metric-text">
            <FieldWeatherPanel :field-id="selectedFieldId" :compact="true" />

          </div>
        </div>
      </div>

      <!-- 卡片 4: 7 天科学任务 -->
      <div class="app-card app-card--hover metric-card">
        <div class="metric-content">
          <div class="metric-icon-box">
            <el-icon><Calendar /></el-icon>
          </div>
          <div class="metric-text">
            <span class="metric-label">今日检查提醒</span>
            <div class="metric-value app-num">
              {{ todayTasks.length }} 项
            </div>
            <div class="metric-extra">
              <span v-if="blockedTasksCount" class="metric-blocked"><el-icon><Warning /></el-icon>{{ blockedTasksCount }} 项需核实或复核</span>
              <span v-else>本周共 {{ currentPlan?.total_tasks || 0 }} 项</span>
              <span class="dot">·</span>
              <router-link to="/calendar" class="link-calendar">查看日历</router-link>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 主体区域：左侧智能排程建议 + 右侧农事履历与快捷入口 -->
    <div class="dashboard-body">
      <!-- 左侧：未来 7 天智能推演农事计划 -->
      <div class="plan-section">
        <el-card class="section-card" shadow="never" v-loading="planLoading">
          <template #header>
            <div class="card-header-flex">
              <div>
                <span class="card-title">
                  <el-icon><Calendar /></el-icon>
                  今日检查提醒
                  <SourceBadge kind="ai" label="系统生成 · 非处方" />
                </span>
                <span class="card-desc">
                  按固定巡查清单生成，并标注天气风险；确认只记录"已检查"，施药须另行记录并经专家复核。
                </span>
              </div>
              <div class="header-action-btns">
                <el-button
                  type="success"
                  size="small"
                  :loading="applyingPlan"
                  :disabled="planLoading || confirmableTodayTasks.length === 0"
                  @click="handleApplyAllTasks"
                >
                  确认今日已完成事项
                </el-button>
              </div>
            </div>
          </template>

          <el-alert v-if="planError" :title="planError" type="warning" :closable="false" show-icon class="plan-error">
            <template #default>显示的是上次成功获取的提醒，可点击右上角"刷新提醒"重试。</template>
          </el-alert>

          <!-- 计划任务时序列表 -->
          <div v-if="currentPlan?.items && currentPlan.items.length > 0" class="plan-timeline">
            <p v-if="!todayTasks.length" class="app-note">今天没有检查提醒。</p>
            <div
              v-for="item in (showUpcoming ? [...todayTasks, ...upcomingTasks] : todayTasks)"
              :key="`${item.date}-${item.record_type}-${item.action}`"
              class="plan-task-card"
              :class="{ 'blocked-task': !item.can_proceed, 'upcoming-task': item.date !== todayTasks[0]?.date && !todayTasks.includes(item) }"
            >
              <div class="task-date-col">
                <div class="date-badge">{{ item.date }}</div>
                <el-tag :type="getRecordTypeLabel(item.record_type).tag" size="small" effect="light">
                  {{ getRecordTypeLabel(item.record_type).label }}
                </el-tag>
              </div>

              <div class="task-detail-col">
                <div class="task-header-row">
                  <span class="task-title">{{ item.action }}</span>
                  <div class="task-status-badge">
                    <span v-if="item.execution_status === 'completed'" class="badge-safe">
                      <el-icon><CircleCheck /></el-icon> 已完成
                    </span>
                    <span v-else-if="item.execution_status === 'revoked' || item.execution_status === 'unavailable'" class="badge-block">
                      <el-icon><CloseBold /></el-icon> {{ planExecutionLabel(item) }}
                    </span>
                    <span v-else-if="item.can_proceed" class="badge-pending">
                      <el-icon><Clock /></el-icon> 待确认
                    </span>
                    <span v-else class="badge-block">
                      <el-icon><Warning /></el-icon> 需核实 / 暂缓
                    </span>
                  </div>
                </div>

                <p class="task-desc">{{ item.description }}</p>

                <details class="task-meta">
                  <summary>依据说明</summary>
                  <p>{{ item.reason }}</p>
                  <p class="app-note">来源：{{ item.source }}</p>
                </details>

                <div v-if="item.risk_notes" class="task-risk-alert">
                  <el-icon><Warning /></el-icon>
                  <span>{{ item.risk_notes }}</span>
                </div>
              </div>

              <div class="task-action-col">
                <el-button
                  type="primary"
                  size="small"
                  plain
                  :disabled="!!planConfirmationReason(item) || applyingPlan || planLoading"
                  @click="handleApplySingleTask(item)"
                >
                  {{ planExecutionLabel(item) }}
                </el-button>
                <small v-if="planConfirmationReason(item)">{{ planConfirmationReason(item) }}</small>
              </div>
            </div>
            <el-button v-if="upcomingTasks.length" text class="toggle-upcoming" @click="showUpcoming = !showUpcoming">
              {{ showUpcoming ? '收起之后几天的提醒' : `查看之后几天的提醒（${upcomingTasks.length} 项）` }}
            </el-button>
            <div v-if="currentPlan" class="plan-metadata">
              <p>{{ planUpdateLabel(currentPlan) }}；{{ currentPlan.update_reason }}</p>
              <details v-if="currentPlan.changes?.length">
                <summary>本次更新说明</summary>
                <p v-for="change in currentPlan.changes" :key="change">{{ change }}</p>
              </details>
            </div>
          </div>

          <el-empty
            v-else
            :description="!currentField ? '请先选择田块' : activeCrops.length > 1 && !activeCrop ? '请先选择作物批次' : '该田块还没有在种作物，登记种植批次后才会生成检查提醒'"
            :image-size="72"
          >
            <router-link v-if="currentField && !activeCrops.length" to="/farms"><el-button size="small">去登记种植批次</el-button></router-link>
          </el-empty>
        </el-card>
      </div>

      <!-- 右侧：快捷工具 + 田间履历追溯 -->
      <div class="side-section">
        <!-- 待办提醒：只列出可从已有数据确认的事项，不编造风险评分 -->
        <el-card class="section-card" shadow="never">
          <template #header>
            <span class="card-title">
              <el-icon><Bell /></el-icon>
              需要留意
            </span>
          </template>
          <ul class="todo-list">
            <li v-if="blockedTasksCount"><el-icon><Warning /></el-icon><span>{{ blockedTasksCount }} 项检查提醒需要核实或专家复核（如施药方案、天气风险）。</span></li>
            <li v-if="sprayMissingPhi"><el-icon><Warning /></el-icon><span>最近 {{ sprayMissingPhi }} 条施药记录没有填写安全间隔期，采收前核查会把它们视为风险。</span></li>
            <li v-if="activeCrop && activeCrop.growth_stage === '待核实'"><el-icon><InfoFilled /></el-icon><span>当前批次的生育期尚未核实，请到"农场与田块"更新。</span></li>
            <li v-if="!blockedTasksCount && !sprayMissingPhi && !(activeCrop && activeCrop.growth_stage === '待核实')" class="todo-empty"><el-icon><CircleCheck /></el-icon><span>暂无需要处理的事项。</span></li>
          </ul>
          <div class="todo-actions">
            <router-link to="/agri-chat"><el-button size="small">问资料分析</el-button></router-link>
            <router-link to="/diagnosis"><el-button size="small">上传病叶初诊</el-button></router-link>
          </div>
        </el-card>

        <!-- 近期真实田间农事执行履历 -->
        <el-card class="section-card" shadow="never" v-loading="recordsLoading">
          <template #header>
            <div class="card-header-flex">
              <span class="card-title">
                <el-icon><Document /></el-icon>
                最近农事记录
              </span>
              <router-link to="/calendar" class="sub-link">在日历中查看</router-link>
            </div>
          </template>

          <div v-if="recentRecords.length > 0" class="records-mini-list">
            <div
              v-for="rec in recentRecords"
              :key="rec.id"
              class="record-mini-item"
            >
              <div class="record-time">{{ rec.operation_date || rec.created_at?.split('T')[0] }}</div>
              <div class="record-info">
                <div class="record-title-row">
                  <el-tag size="small" :type="getRecordTypeLabel(rec.record_type).tag">
                    {{ getRecordTypeLabel(rec.record_type).label }}
                  </el-tag>
                  <span class="record-op">执行人：{{ rec.operator || '未填写' }}</span>
                </div>
                <div class="record-desc-text">
                  {{ rec.operation_content || rec.chemical_name || '农事作业完成' }}
                </div>
                <div v-if="rec.safety_interval_days != null" class="phi-warning-badge">
                  安全间隔期 {{ rec.safety_interval_days }} 天（录入值）
                </div>
              </div>
            </div>
          </div>
          <el-empty v-else description="暂无农事操作记录" :image-size="60" />
        </el-card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.plan-metadata { margin: 12px 0 18px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.plan-metadata p { margin: 4px 0; }
.plan-metadata summary { cursor: pointer; }
.dashboard-page {
  min-height: calc(100vh - 60px);
}

/* 顶部 4 维指标网格 - art-design-pro 风格等高卡片 */
.metrics-grid {
  display: grid;
  /* minmax(0,1fr)：允许网格项收缩，长文本走省略号，防止内容爆宽 */
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--app-space-4);
  margin-bottom: var(--app-space-6);
}

@media (max-width: 1200px) {
  .metrics-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .metrics-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.metric-card {
  padding: var(--app-space-5);
  min-width: 0;
  overflow: hidden;
}

.metric-content {
  display: flex;
  align-items: center;
  gap: var(--app-space-4);
  min-height: 72px;
}

.metric-icon-box {
  width: 52px;
  height: 52px;
  border-radius: var(--app-radius-xl);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--app-text-3xl);
  flex-shrink: 0;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  transition: transform 0.2s ease;
}

.metric-card:hover .metric-icon-box {
  transform: scale(1.05);
}

.metric-text {
  flex: 1;
  min-width: 0;
}

.metric-label {
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
  font-weight: 500;
  display: block;
  margin-bottom: var(--app-space-1);
}

.metric-value {
  font-size: var(--app-text-3xl);
  font-weight: 700;
  color: var(--app-text);
  margin-bottom: var(--app-space-1);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  letter-spacing: -0.01em;
}

.metric-extra {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.metric-blocked {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  color: var(--state-warning);
}

.dot {
  color: var(--app-border);
}

/* 移动端：文字链接触控区加固 */
@media (max-width: 767px) {
  .link-calendar,
  .sub-link {
    display: inline-flex;
    align-items: center;
    min-height: 32px;
  }
}

.link-calendar {
  color: var(--el-color-primary);
  text-decoration: none;
  font-weight: 600;
  transition: var(--app-transition);
}

.link-calendar:hover {
  color: var(--el-color-primary-dark-2);
  text-decoration: underline;
}

/* 页面主体 */
.dashboard-body {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: var(--app-space-5);
}

@media (max-width: 1024px) {
  .dashboard-body {
    grid-template-columns: 1fr;
  }
}

.side-section {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-5);
}

.card-header-flex {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-title {
  font-size: var(--app-text-lg);
  font-weight: 600;
  color: var(--app-text);
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}

.card-desc {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin-top: var(--app-space-1);
  display: block;
}

.plan-summary-banner {
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-8);
  border-left: 4px solid var(--el-color-primary);
  padding: var(--app-space-3) var(--app-space-4);
  border-radius: var(--app-radius-md);
  font-size: var(--app-text-sm);
  color: var(--el-color-primary-dark-2);
  margin-bottom: var(--app-space-5);
  display: flex;
  align-items: center;
  gap: var(--app-space-3);
  line-height: 1.5;
}

/* 计划任务卡片时序 */
.plan-timeline {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-4);
}

.plan-task-card {
  display: flex;
  align-items: flex-start;
  gap: var(--app-space-4);
  padding: var(--app-space-4);
  background: var(--app-surface);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-xl);
  box-shadow: var(--app-shadow-sm);
  transition: var(--app-transition);
}

.plan-task-card:hover {
  border-color: var(--el-color-primary-light-7);
  box-shadow: var(--app-shadow-md);
  transform: translateY(-1px);
}

.blocked-task {
  background: var(--el-color-danger-light-9);
  border-color: var(--el-color-danger-light-7);
}

.blocked-task:hover {
  border-color: var(--el-color-danger-light-5);
  box-shadow: var(--app-shadow-md);
}

.task-date-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  min-width: 90px;
}

.date-badge {
  font-size: var(--app-text-xs);
  font-weight: 700;
  color: var(--el-color-primary-dark-2);
  background: var(--el-color-primary-light-9);
  padding: 4px 10px;
  border-radius: var(--app-radius-round);
  letter-spacing: -0.01em;
}

.task-detail-col {
  flex: 1;
  min-width: 0;
}

.task-header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
  gap: 10px;
  min-width: 0;
}

.task-title {
  font-size: var(--app-text-lg);
  font-weight: 600;
  color: var(--app-text);
  min-width: 0;
  word-break: break-word;
}

.task-status-badge {
  font-size: var(--app-text-xs);
}

.badge-safe {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
  padding: 2px 10px;
  border-radius: var(--app-radius-round);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-weight: 600;
}

.badge-pending {
  color: var(--state-info);
  background: var(--app-surface-2);
  padding: 2px 10px;
  border-radius: var(--app-radius-round);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-weight: 600;
}

.badge-block {
  color: var(--state-warning);
  background: var(--el-color-warning-light-9);
  padding: 2px 8px;
  border-radius: var(--app-radius-xl);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-weight: 600;
}

.task-desc {
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
  margin: 0 0 var(--app-space-2) 0;
  line-height: 1.5;
}

.task-risk-alert {
  margin-top: 6px;
  font-size: var(--app-text-xs);
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  padding: 4px 8px;
  border-radius: var(--app-radius-sm);
  display: flex;
  align-items: center;
  gap: 6px;
}

.task-action-col {
  align-self: center;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 180px;
}

.task-action-col small {
  color: var(--app-text-secondary);
  line-height: 1.5;
}

/* 右侧快捷链接 */
/* 需要留意：从已有数据推出的待办，不是风险评分 */
.todo-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: var(--app-space-2);
}

.todo-list li {
  display: flex;
  gap: var(--app-space-2);
  align-items: flex-start;
  font-size: var(--app-text-sm);
  line-height: 1.6;
  color: var(--app-text);
}

.todo-list li .el-icon {
  flex-shrink: 0;
  margin-top: 3px;
  color: var(--state-warning);
}

.todo-list li.todo-empty {
  color: var(--app-text-secondary);
}

.todo-list li.todo-empty .el-icon {
  color: var(--state-success);
}

.todo-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-2);
  margin-top: var(--app-space-4);
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

.plan-error {
  margin-bottom: var(--app-space-3);
}

.toggle-upcoming {
  align-self: flex-start;
}

.upcoming-task {
  opacity: 0.85;
}

.task-meta {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin-bottom: var(--app-space-1);
}

.task-meta summary {
  cursor: pointer;
  min-height: 24px;
}

.task-meta p {
  margin: var(--app-space-1) 0 0;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

/* 履历简列 */
.records-mini-list {
  display: flex;
  flex-direction: column;
  gap: var(--app-space-3);
}

.record-mini-item {
  display: flex;
  gap: var(--app-space-3);
  padding-bottom: var(--app-space-3);
  border-bottom: 1px dashed var(--app-border);
}

.record-mini-item:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

.record-time {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  font-weight: 600;
  white-space: nowrap;
}

.record-info {
  flex: 1;
}

.record-title-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--app-space-1);
}

.record-op {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

.record-desc-text {
  font-size: var(--app-text-xs);
  color: var(--app-text);
  line-height: 1.4;
}

.phi-warning-badge {
  display: inline-block;
  font-size: var(--app-text-xs);
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  padding: 1px 6px;
  border-radius: var(--app-radius-sm);
  margin-top: var(--app-space-1);
  font-weight: 600;
}

.sub-link {
  font-size: var(--app-text-xs);
  color: var(--el-color-primary);
  text-decoration: none;
  transition: var(--app-transition);
}

.sub-link:hover {
  color: var(--el-color-primary-dark-2);
}

/* ================= 移动端响应式适配 ================= */
@media (max-width: 768px) {
  .card-header-flex {
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }

  .header-action-btns .el-button {
    width: 100%;
  }

  .plan-summary-banner {
    padding: 10px 12px;
    font-size: var(--app-text-xs);
    margin-bottom: var(--app-space-4);
  }

  /* 移动端任务卡片改为垂直流式排列，彻底消除文字挤压成单字竖列 */
  .plan-task-card {
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
    padding: 12px 14px;
  }

  .task-date-col {
    flex-direction: row;
    justify-content: flex-start;
    align-items: center;
    gap: 8px;
    min-width: unset;
    width: 100%;
  }

  .task-detail-col {
    width: 100%;
    min-width: 0;
  }

  .task-header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
    margin-bottom: 6px;
    flex-wrap: wrap;
  }

  .task-title {
    font-size: var(--app-text-lg);
    line-height: 1.4;
    word-break: break-word;
    flex: 1;
    min-width: 160px;
  }

  .task-status-badge {
    flex-shrink: 0;
  }

  .task-desc {
    font-size: var(--app-text-sm);
    line-height: 1.5;
    margin-bottom: var(--app-space-2);
    word-break: break-word;
  }

  .task-meta-row {
    flex-direction: column;
    gap: 4px;
    margin-bottom: 6px;
  }

  .meta-tag {
    word-break: break-all;
    line-height: 1.4;
    display: inline-block;
  }

  .task-risk-alert {
    word-break: break-all;
  }

  .task-action-col {
    align-self: stretch;
    width: 100%;
    max-width: none;
    margin-top: 2px;
  }

  .task-action-col .el-button {
    width: 100%;
  }

  .quick-links-grid {
    grid-template-columns: 1fr;
    gap: 10px;
  }
}
</style>
