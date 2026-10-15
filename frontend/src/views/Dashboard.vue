<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useFarmSelectionStore } from '@/stores/farmSelection'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  OfficeBuilding,
  Sunny,
  Calendar,
  Warning,
  Refresh,
  Histogram,
  Compass,
  CircleCheck,
  CloseBold
} from '@element-plus/icons-vue'

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

const router = useRouter()

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

// 气象阻断任务数统计
const blockedTasksCount = computed(() => {
  if (!currentPlan.value?.items) return 0
  return currentPlan.value.items.filter(item => !item.can_proceed).length
})

// 今日任务
const todayTasks = computed(() => {
  if (!currentPlan.value?.items) return []
  const todayStr = businessDate()
  return currentPlan.value.items.filter(item => item.date === todayStr)
})

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
  } catch (err: any) {
    ElMessage.error('加载农场列表失败: ' + (err.response?.data?.detail || err.message))
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
    ElMessage.error('加载地块失败: ' + (err.response?.data?.detail || err.message))
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
      lastPlanCheck = Date.now()
    }
  } catch (err: any) {
    if (version !== fieldRequestVersion || planVersion !== planRequestVersion) return
    ElMessage.warning('推演农事计划提示: ' + (err.response?.data?.detail || '暂无可用计划'))
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
      operator: '农场管理员'
    })
    markPlanItemsCompleted(currentPlan.value, fieldId, [item], res.data.record_ids || [])
    ElMessage.success(res.data.message || '计划项已成功落库为农事执行记录')
    // 刷新农事记录
    try {
      const recRes = await getFarmingRecords(fieldId)
      if (selectedFieldId.value === fieldId) recentRecords.value = (recRes.data || []).slice(0, 8)
    } catch { ElMessage.warning('事项已确认，履历刷新失败，请刷新页面查看') }
  } catch (err: any) {
    ElMessage.error('执行落库失败: ' + (err.response?.data?.detail || err.message))
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
  try {
    applyingPlan.value = true
    if (validItems.some(planProofNeedsRenewal)) validItems = await renewConfirmation(validItems, fieldId)
    const res = await applyAgriPlan({
      field_id: fieldId,
      items: validItems,
      operator: '农场管理员'
    })
    markPlanItemsCompleted(currentPlan.value, fieldId, validItems, res.data.record_ids || [])
    ElMessage.success(res.data.message)
    try {
      const recRes = await getFarmingRecords(fieldId)
      if (selectedFieldId.value === fieldId) recentRecords.value = (recRes.data || []).slice(0, 8)
    } catch { ElMessage.warning('事项已确认，履历刷新失败，请刷新页面查看') }
  } catch (err: any) {
    ElMessage.error('批量落库失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    applyingPlan.value = false
  }
}

function getRecordTypeLabel(type: string) {
  const map: Record<string, { label: string; tag: string }> = {
    irrigation: { label: '水肥灌溉', tag: 'primary' },
    fertilization: { label: '科学追肥', tag: 'warning' },
    pest_control: { label: '植保用药', tag: 'danger' },
    field_management: { label: '整枝农事', tag: 'success' },
    weeding: { label: '除草除杂', tag: 'info' },
    harvesting: { label: '采收上市', tag: 'success' }
  }
  return map[type] || { label: type, tag: 'info' }
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
      title="工作台"
      description="AgriMind 田块多维时空档案、真实微气象预警与未来 7 天智能排程决策中枢"
    >
      <template #actions>
        <el-select
          v-model="selectedFarmId"
          placeholder="选择示范农场"
          style="width: 220px"
        >
          <el-option
            v-for="f in farms"
            :key="f.id"
            :label="f.name"
            :value="f.id"
          />
        </el-select>

        <el-select
          v-model="selectedFieldId"
          placeholder="选择大棚/田块"
          style="width: 180px"
          @change="handleFieldChange"
        >
          <el-option
            v-for="fd in fields"
            :key="fd.id"
            :label="fd.name"
            :value="fd.id"
          />
        </el-select>

        <el-select v-if="activeCrops.length > 1" :model-value="activeCrop?.id" placeholder="请选择作物批次" :disabled="applyingPlan" style="width:220px" @change="selectPlanting">
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
          刷新推演
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
            <span class="metric-label">基地田块档案</span>
            <div class="metric-value">{{ currentFarm ? `${currentFarm.name} · ${currentField?.name || '主基地'}` : '未选择田块' }}</div>

            <div class="metric-extra">
              <span>面积 {{ currentField?.area || 0 }} 亩</span>
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
            <span class="metric-label">在茬物候作物</span>
            <div class="metric-value">
              {{ activeCrop ? `${activeCrop.crop} · ${activeCrop.variety || '品种未登记'}` : activeCrops.length > 1 ? '请选择批次' : '休耕/待种' }}
            </div>
            <div class="metric-extra">
              <el-tag size="small" type="success" effect="plain">
                {{ activeCrop?.growth_stage || '阶段未录入' }}
              </el-tag>
              <span class="dot">·</span>
              <span>状态: {{ activeCrop ? '在茬（不代表健康诊断）' : activeCrops.length > 1 ? '多个在种批次待选择' : '未录入活跃作物' }}</span>
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
            <div v-if="blockedTasksCount" class="metric-extra metric-blocked">
              <el-icon><Warning /></el-icon>
              <span>{{ blockedTasksCount }} 项待核实或复核</span>
            </div>
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
            <span class="metric-label">农事检查建议</span>
            <div class="metric-value">
              {{ currentPlan?.total_tasks || 0 }} 项
            </div>
            <div class="metric-extra">
              <span>今日需执行: {{ todayTasks.length }} 项</span>
              <span class="dot">·</span>
              <router-link to="/calendar" class="link-calendar">进入日历 →</router-link>
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
                  未来 7 天农事检查建议
                </span>
                <span class="card-desc">
                  基于田块档案与室外天气的待确认清单，施药配方需专家核验
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

          <div v-if="currentPlan?.summary" class="plan-summary-banner">
            <el-icon><Compass /></el-icon>
            <span>{{ currentPlan.summary }}</span>
          </div>
          <div v-if="currentPlan" class="plan-metadata">
            <p>{{ planUpdateLabel(currentPlan) }}</p>
            <p>{{ currentPlan.update_reason }} · 保留同日安排，检查建议不代替现场农艺判断。</p>
            <details v-if="currentPlan.changes?.length">
              <summary>本次更新说明</summary>
              <p v-for="change in currentPlan.changes" :key="change">{{ change }}</p>
            </details>
          </div>

          <!-- 计划任务时序列表 -->
          <div v-if="currentPlan?.items && currentPlan.items.length > 0" class="plan-timeline">
            <div
              v-for="(item, idx) in currentPlan.items"
              :key="idx"
              class="plan-task-card"
              :class="{ 'blocked-task': !item.can_proceed }"
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
                    <span v-else-if="item.can_proceed" class="badge-safe">
                      <el-icon><CircleCheck /></el-icon> 待人工确认
                    </span>
                    <span v-else class="badge-block">
                      <el-icon><CloseBold /></el-icon> 需核实 / 暂缓
                    </span>
                  </div>
                </div>

                <p class="task-desc">{{ item.description }}</p>

                <div class="task-meta-row">
                  <span class="meta-tag"><strong>推演依据:</strong> {{ item.reason }}</span>
                  <span class="meta-tag"><strong>标准规程:</strong> {{ item.source }}</span>
                </div>

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
          </div>

          <el-empty
            v-else
            description="暂无可推演农事任务，请确认田块是否已录入种植作物批次"
            :image-size="80"
          />
        </el-card>
      </div>

      <!-- 右侧：快捷工具 + 田间履历追溯 -->
      <div class="side-section">
        <!-- 快捷入口卡片 -->
        <el-card class="section-card" shadow="never">
          <template #header>
            <span class="card-title">
              <el-icon><Lightning /></el-icon>
              农业核心功能直达
            </span>
          </template>
          <div class="quick-links-grid">
            <div class="quick-btn-item" @click="router.push('/agri-chat')">
              <div class="quick-btn-icon">
                <el-icon><ChatDotRound /></el-icon>
              </div>
              <div class="quick-btn-title">农业决策智能体</div>
              <div class="quick-btn-sub">多工具自主协同研判</div>
            </div>
            <div class="quick-btn-item" @click="router.push('/diagnosis')">
              <div class="quick-btn-icon">
                <el-icon><Camera /></el-icon>
              </div>
              <div class="quick-btn-title">病叶视觉初诊</div>
              <div class="quick-btn-sub">多模态大模型+专家复核</div>
            </div>
            <div class="quick-btn-item" @click="router.push('/calendar')">
              <div class="quick-btn-icon">
                <el-icon><Calendar /></el-icon>
              </div>
              <div class="quick-btn-title">农事作业日历</div>
              <div class="quick-btn-sub">月度时序全景看板</div>
            </div>
            <div class="quick-btn-item" @click="router.push('/farms')">
              <div class="quick-btn-icon">
                <el-icon><House /></el-icon>
              </div>
              <div class="quick-btn-title">农场与地块管理</div>
              <div class="quick-btn-sub">物候种植档案建档</div>
            </div>
          </div>
        </el-card>

        <!-- 近期真实田间农事执行履历 -->
        <el-card class="section-card" shadow="never" v-loading="recordsLoading">
          <template #header>
            <div class="card-header-flex">
              <span class="card-title">
                <el-icon><Document /></el-icon>
                田间真实农事执行履历
              </span>
              <router-link to="/farms" class="sub-link">更多记录 →</router-link>
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
                  <span class="record-op">操作人: {{ rec.operator || '农户' }}</span>
                </div>
                <div class="record-desc-text">
                  {{ rec.operation_content || rec.chemical_name || '农事作业完成' }}
                </div>
                <div v-if="rec.safety_interval_days" class="phi-warning-badge">
                  安全间隔期(PHI): {{ rec.safety_interval_days }} 天
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
  margin-top: var(--app-space-1);
  color: var(--el-color-danger);
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

.badge-block {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
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

.task-meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-3);
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin-bottom: 6px;
}

/* 推演依据 / 标准规程：纯文本弱信息，不用色块（在常规卡与告警卡上都不突兀） */
.meta-tag {
  display: inline-flex;
  align-items: baseline;
  gap: 2px;
}

.meta-tag strong {
  color: var(--app-text);
  font-weight: 600;
  flex-shrink: 0;
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
.quick-links-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--app-space-3);
}

.quick-btn-item {
  padding: var(--app-space-4);
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-md);
  cursor: pointer;
  transition: var(--app-transition);
  text-align: center;
}

.quick-btn-item:hover {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
  transform: translateY(-2px);
}

.quick-btn-icon {
  width: 40px;
  height: 40px;
  margin: 0 auto var(--app-space-2);
  border-radius: var(--app-radius-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--app-text-xl);
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}

.quick-btn-title {
  font-size: var(--app-text-base);
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 2px;
}

.quick-btn-sub {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
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
