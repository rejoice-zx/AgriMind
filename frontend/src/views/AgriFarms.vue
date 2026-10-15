<script setup lang="ts">
import { businessDate } from '@/utils/businessDate'
import { saveFarmingDraft, readFarmingDraft, clearFarmingDraft } from '@/utils/farmingDraft'
import { draftOwnerKey } from '@/utils/scopedDraft'
import { newRequestId } from '@/utils/requestId'
import FarmLocationForm from '@/components/FarmLocationForm.vue'
import FieldWeatherPanel from '@/components/FieldWeatherPanel.vue'
import FieldEnvironmentPanel from '@/components/FieldEnvironmentPanel.vue'
import FieldCropHistory from '@/components/FieldCropHistory.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAuthStore } from '@/stores/auth'
import { ref, onMounted, onBeforeUnmount, computed, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useFarmSelectionStore } from '@/stores/farmSelection'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus,
  Refresh,
  Location,
  Calendar,
  DocumentAdd,
  List,
  Sunny,
  House,
  EditPen,
  Notebook
} from '@element-plus/icons-vue'
import {
  createFarm, updateFarmLocation, revokeFarmingRecord,
  setFarmArchive, setFieldArchive,
  getFieldsByFarm,
  createField,
  updateField,
  updateCropPlanting,
  getCropPlantings,
  createCropPlanting,
  getFarmingRecords,
  createFarmingRecord
} from '@/api/agriculture'
import type {
  FarmCreate,
  FieldItem,
  FieldCreate,
  CropPlantingCreate,
  CropPlanting,
  FarmingRecord,
  FarmingRecordCreate
} from '@/types/agriculture'

// ==================== 状态管理 ====================
const loading = ref(false)
const farmSelection = useFarmSelectionStore()
const { farms, selectedFarmId: currentFarmId } = storeToRefs(farmSelection)
let farmContextReady = false
watch(currentFarmId, () => { if (farmContextReady) void fetchFields() }, { flush: 'sync' })
const fields = ref<FieldItem[]>([])

// 农场创建表单
const farmDialogVisible = ref(false)
const farmForm = ref<FarmCreate>({
  name: '',
  province: '', city: '', district: '', address: '', area: 0.1, description: ''
})

// 地块创建表单
const fieldDialogVisible = ref(false)
const editingFieldId = ref<number | null>(null)
let fieldFormFarmId: number | null = null
let fieldFormOwner: string | undefined
const facilityLabels = { unknown: '未核实', open_field: '露地', greenhouse: '温室/大棚', tunnel: '拱棚/覆盖栽培', other: '其他设施' }
const fieldForm = ref<FieldCreate>({
  facility_type: 'unknown',
  name: '',
  area: 0,
  soil_type: '',
  irrigation_type: '',
  remark: ''
})

// 作物批次创建表单
const cropDialogVisible = ref(false)
const editingCropId = ref<number | null>(null)
const targetFieldForCrop = ref<FieldItem | null>(null)
const cropForm = ref<CropPlantingCreate>({
  crop: '番茄',
  variety: '',
  growth_stage: '待核实',
  planting_date: businessDate(),
  area: 0,
  status: 'active'
})

// 农事记录抽屉
const recordDrawerVisible = ref(false)
const currentFieldForRecord = ref<FieldItem | null>(null)
const farmingRecords = ref<FarmingRecord[]>([])
const recordStatus = ref<'active' | 'revoked' | 'all'>('active')
const recordHistoryOnly = ref(false), recordBatchFilter = ref<number | undefined>()
const recordLoading = ref(false)
const newRecordForm = ref<FarmingRecordCreate>({
  field_id: 0,
  record_type: 'spraying',
  operation_date: businessDate(),
  operation_content: '',
  chemical_name: '',
  dosage: '',
  dilution_ratio: '',
  safety_interval_days: undefined,
  operator: '',
  notes: ''
})

// 计算当前选中的农场对象
const currentFarm = computed(() => {
  return farms.value.find(f => f.id === currentFarmId.value) || null
})

function archivedField(field: FieldItem | null) { return !!(currentFarm.value?.archived_at || field?.archived_at) }
async function toggleArchive(field?: FieldItem) {
  if (!currentFarm.value || mutationBusy.value) return
  const asset = field || currentFarm.value
  const farmId = currentFarm.value.id
  const owner = draftOwnerKey(auth.user)
  const archived = !asset.archived_at
  mutationBusy.value = true
  try {
    await ElMessageBox.confirm(`${archived ? '归档后只能查看历史，所有农事和诊断记录保留' : '恢复后可继续登记业务'}。确定${archived ? '归档' : '恢复'}【${asset.name}】？`, '档案状态')
    if (currentFarmId.value !== farmId || draftOwnerKey(auth.user) !== owner || (field && field.farm_id !== farmId)) {
      ElMessage.warning('农场或账号已切换，请重新核对后操作'); return
    }
    if (field) await setFieldArchive(field.id, archived)
    else await setFarmArchive(asset.id, archived)
    await fetchFarms()
    ElMessage.success(archived ? '已归档，追溯历史保留' : '档案已恢复')
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.response?.data?.detail || '状态更新失败')
  } finally { mutationBusy.value = false }
}

const auth = useAuthStore()
let draftOwner: string | undefined
let restoringDraft = false
const draftNotice = ref('')
watch(newRecordForm, form => {
  if (restoringDraft || recordHistoryOnly.value || !recordDrawerVisible.value || !draftOwner || draftOwnerKey(auth.user) !== draftOwner || form.field_id !== currentFieldForRecord.value?.id) return
  draftNotice.value = saveFarmingDraft(draftOwner, form) ? '草稿已保存在本机，7天内可恢复；提交重试复用编号' : '本机无法保存草稿，请保留页面并复制内容'
}, { deep: true, flush: 'sync' })
const locationDialog = ref(false)
const locationTargetId = ref<number | null>(null)
const locationForm = ref<FarmCreate>({ name: '', area: 0 })
const weatherVersion = ref(0)
const mutationBusy = ref(false)
const now = ref(Date.now())
let revokeTimer: ReturnType<typeof setInterval> | undefined
function canRevoke(record: FarmingRecord) { return !recordHistoryOnly.value && !archivedField(currentFieldForRecord.value) && !!record.can_delete && (auth.user?.role === 'admin' || (!!record.delete_deadline_at && Date.parse(record.delete_deadline_at) > now.value)) }
let fieldVersion = 0, recordsVersion = 0
let alive = true
function validCoordinates(form: FarmCreate) { return Number.isFinite(form.latitude) && Number.isFinite(form.longitude) }
function editLocation() {
  if (!currentFarm.value) return
  locationTargetId.value = currentFarm.value.id
  locationForm.value = { ...currentFarm.value }
  locationDialog.value = true
}
async function saveLocation() {
  if (!locationTargetId.value || mutationBusy.value) return
  if (!validCoordinates(locationForm.value)) { ElMessage.warning('请确认区域参考点或填写实际经纬度'); return }
  const id = locationTargetId.value
  mutationBusy.value = true
  try {
    await updateFarmLocation(id, locationForm.value as unknown as Record<string, unknown>)
    locationDialog.value = false; weatherVersion.value++
    await fetchFarms(); ElMessage.success('天气位置已保存，田块将继承农场位置')
  } catch (error: any) { ElMessage.error(error.response?.data?.detail || '位置保存失败') }
  finally { mutationBusy.value = false }
}
async function revokeRecord(record: FarmingRecord) {
  if (mutationBusy.value) return
  const fieldId = currentFieldForRecord.value?.id
  mutationBusy.value = true
  try {
    const result = await ElMessageBox.prompt('仅撤销误录；已实际实施的操作不能视为撤回。请填写原因。', '撤销农事记录', { inputValidator: value => (!!value?.trim() && value.length <= 500) || '请填写1至500字的撤销原因' })
    if (typeof result === 'string') return
    await revokeFarmingRecord(record.id, result.value.trim())
    if (fieldId && currentFieldForRecord.value?.id === fieldId) {
      recordStatus.value = 'revoked'
      await fetchFieldRecords(fieldId)
    }
    ElMessage.success('已撤销，原记录及原因保留在已撤销档案中')
  } catch (error: any) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error.response?.data?.detail || '撤销失败') }
  finally { mutationBusy.value = false }
}
const isMobile = ref(false)
function checkScreenSize() {
  if (typeof window !== 'undefined') {
    isMobile.value = window.innerWidth < 768
  }
}
onBeforeUnmount(() => {
  alive = false
  ++fieldVersion
  ++recordsVersion
  clearInterval(revokeTimer)
  if (typeof window !== 'undefined') {
    window.removeEventListener('resize', checkScreenSize)
  }
})

// ==================== 数据获取与加载 ====================

async function fetchFarms() {
  loading.value = true
  farmContextReady = false
  try {
    if (!await farmSelection.loadFarms()) return
    farmContextReady = true
    await fetchFields()
  } catch (err: any) {
    ElMessage.error(err.message || '获取农场列表失败')
  } finally {
    loading.value = false
  }
}

async function fetchFields() {
  const current = ++fieldVersion
  const farmId = currentFarmId.value
  fields.value = []
  if (!farmId) {
    fields.value = []
    return
  }
  loading.value = true
  try {
    const res = await getFieldsByFarm(farmId)
    const fieldList = res.data.items || []
    
    // 异步拉取每个地块当前的种植作物
    for (const f of fieldList) {
      try {
        const cropRes = await getCropPlantings(f.id, 'active')
        f.cropOptions = cropRes.data.items || []
        if (cropRes.data.items && cropRes.data.items.length === 1) {
          f.activeCrop = cropRes.data.items[0]
        }
      } catch {
        // 忽略个别作物拉取失败
      }
    }
    if (alive && current === fieldVersion && farmId === currentFarmId.value) fields.value = fieldList
  } catch (err: any) {
    ElMessage.error(err.message || '获取田块列表失败')
  } finally {
    loading.value = false
  }
}

// ==================== 农场增删改 ====================

async function submitCreateFarm() {
  if (mutationBusy.value) return
  if (!validCoordinates(farmForm.value)) { ElMessage.warning('请确认区域参考点或填写实际经纬度，以绑定天气位置'); return }
  if (!farmForm.value.name) {
    ElMessage.warning('请输入农场名称')
    return
  }
  mutationBusy.value = true
  try {
    const res = await createFarm(farmForm.value)
    ElMessage.success('农场创建成功')
    farmDialogVisible.value = false
    farmForm.value = { name: '', province: '', city: '', district: '', address: '', area: 0.1, description: '' }
    farmContextReady = false
    currentFarmId.value = res.data.id
    await fetchFarms()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '创建农场失败')
  } finally { mutationBusy.value = false }
}

// ==================== 地块增删改 ====================

function openCreateField() {
  if (currentFarm.value?.archived_at || mutationBusy.value) return
  editingFieldId.value = null
  fieldFormFarmId = currentFarmId.value
  fieldFormOwner = draftOwnerKey(auth.user)
  fieldForm.value = {
    name: '',
    facility_type: 'unknown',
    area: 0,
    soil_type: '',
    irrigation_type: '',
    remark: ''
  }
  fieldDialogVisible.value = true
}

function openEditField(field: FieldItem) {
  if (archivedField(field) || mutationBusy.value) return
  editingFieldId.value = field.id
  fieldFormFarmId = field.farm_id
  fieldFormOwner = draftOwnerKey(auth.user)
  fieldForm.value = { name: field.name, area: field.area, facility_type: field.facility_type || 'unknown',
    soil_type: field.soil_type, irrigation_type: field.irrigation_type, remark: field.remark }
  fieldDialogVisible.value = true
}

async function submitCreateField() {
  if (!currentFarmId.value || mutationBusy.value) return
  if (currentFarmId.value !== fieldFormFarmId || draftOwnerKey(auth.user) !== fieldFormOwner) {
    ElMessage.warning('农场或账号已切换，请重新打开田块档案'); return
  }
  if (!fieldForm.value.name) {
    ElMessage.warning('请输入田块名称')
    return
  }
  mutationBusy.value = true
  try {
    if (editingFieldId.value) await updateField(editingFieldId.value, fieldForm.value)
    else await createField(fieldFormFarmId!, fieldForm.value)
    ElMessage.success(editingFieldId.value ? '田块档案已更新' : '地块创建成功')
    fieldDialogVisible.value = false
    weatherVersion.value++
    await fetchFields()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '创建田块失败')
  } finally { mutationBusy.value = false }
}

// ==================== 作物批次管理 ====================

function cropHistoryChanged(crop: CropPlanting) {
  const field = fields.value.find(item => item.id === crop.field_id)
  if (!field) return
  const active = (field.cropOptions || []).filter(item => item.id !== crop.id)
  if (crop.status === 'active') active.push(crop)
  field.cropOptions = active
  field.activeCrop = active.length === 1 ? active[0] : active.find(item => item.id === field.activeCrop?.id)
}

function openCreateCrop(field: FieldItem) {
  if (archivedField(field)) return
  editingCropId.value = null
  targetFieldForCrop.value = field
  cropForm.value = { crop: '番茄', variety: '', planting_date: businessDate(), growth_stage: '待核实', area: field.area, status: 'active' }
  cropDialogVisible.value = true
}

function openEditCrop(field: FieldItem) {
  if (archivedField(field)) return
  if (!field.activeCrop) return
  targetFieldForCrop.value = field
  editingCropId.value = field.activeCrop.id
  cropForm.value = { ...field.activeCrop }
  cropDialogVisible.value = true
}

async function submitCreateCrop() {
  if (!targetFieldForCrop.value) return
  try {
    if (editingCropId.value) await updateCropPlanting(editingCropId.value, cropForm.value)
    else await createCropPlanting(targetFieldForCrop.value.id, cropForm.value)
    ElMessage.success('种植档案已保存，历史农事记录保留')
    cropDialogVisible.value = false
    await fetchFields()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '创建作物档案失败')
  }
}

// ==================== 农事记录管理 ====================

async function openRecordDrawer(field: FieldItem, crop?: CropPlanting) {
  restoringDraft = true
  draftOwner = draftOwnerKey(auth.user)
  recordHistoryOnly.value = !!crop
  recordBatchFilter.value = crop?.id
  recordStatus.value = crop ? 'all' : 'active'
  farmingRecords.value = []
  currentFieldForRecord.value = field
  const draft = draftOwner ? readFarmingDraft(draftOwner, field.id) : null
  newRecordForm.value = draft || { field_id: field.id, crop_planting_id: field.activeCrop?.id,
    record_type: 'spraying', operation_date: businessDate(), operation_content: '', safety_interval_days: undefined }
  draftNotice.value = draft ? `已恢复草稿：批次#${draft.crop_planting_id ?? '未绑定'}，日期${draft.operation_date}；请核对后提交，不会自动改绑批次` : ''
  recordDrawerVisible.value = true
  restoringDraft = false
  await fetchFieldRecords(field.id)
}

async function fetchFieldRecords(fieldId: number) {
  const current = ++recordsVersion
  recordLoading.value = true
  try {
    const res = await getFarmingRecords(fieldId, recordStatus.value, recordBatchFilter.value)
    if (!alive || current !== recordsVersion || currentFieldForRecord.value?.id !== fieldId) return
    farmingRecords.value = Array.isArray(res.data) ? res.data : ((res.data as any)?.items || [])
  } catch (err: any) {
    if (alive && current === recordsVersion) ElMessage.error(err.message || '加载农事记录失败')
  } finally {
    if (alive && current === recordsVersion) recordLoading.value = false
  }
}

function refreshRecordHistory() {
  farmingRecords.value = []
  if (currentFieldForRecord.value) void fetchFieldRecords(currentFieldForRecord.value.id)
}

async function submitNewRecord() {
  if (recordHistoryOnly.value) return
  if (archivedField(currentFieldForRecord.value)) { ElMessage.warning('归档仅可查看历史'); return }
  if (mutationBusy.value) return
  if (!newRecordForm.value.operation_content) {
    ElMessage.warning('请填写农事操作内容')
    return
  }
  mutationBusy.value = true
  newRecordForm.value.request_id ||= newRequestId()
  const submittedField = newRecordForm.value.field_id
  const submittedOwner = auth.user?.id
  const submittedOwnerKey = draftOwnerKey(auth.user)
  const submittedDraft = { ...newRecordForm.value }
  try {
    const response = await createFarmingRecord(submittedDraft)
    if (auth.user?.id === submittedOwner && currentFieldForRecord.value?.id === submittedField &&
        JSON.stringify(newRecordForm.value) === JSON.stringify(submittedDraft)) {
      if (submittedOwnerKey) clearFarmingDraft(submittedOwnerKey, submittedField)
      restoringDraft = true
      newRecordForm.value = { field_id: submittedField, crop_planting_id: submittedDraft.crop_planting_id,
        record_type: submittedDraft.record_type, operation_date: businessDate(), operation_content: '', safety_interval_days: undefined }
      draftNotice.value = ''
      restoringDraft = false
    } else if (draftOwnerKey(auth.user) === submittedOwnerKey && currentFieldForRecord.value?.id === submittedField &&
        newRecordForm.value.request_id === submittedDraft.request_id) {
      newRecordForm.value.request_id = newRequestId()
    }
    ElMessage.success(response.data.replayed ? '已恢复原记录，未重复登记' : '农事作业记录保存成功')
    if (currentFieldForRecord.value) {
      recordStatus.value = 'active'
      await fetchFieldRecords(currentFieldForRecord.value.id)
    }
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '保存农事记录失败')
  } finally { mutationBusy.value = false }
}

async function discardRecordDraft() {
  if (mutationBusy.value || !currentFieldForRecord.value || !draftOwner) return
  const field = currentFieldForRecord.value
  const owner = draftOwner
  try {
    await ElMessageBox.confirm('如上次提交超时，请先核对履历。清空只影响本机草稿，下一笔将使用新提交编号。', '重新登记')
    if (currentFieldForRecord.value?.id !== field.id || draftOwnerKey(auth.user) !== owner) return
    restoringDraft = true
    clearFarmingDraft(owner, field.id)
    newRecordForm.value = { field_id: field.id, crop_planting_id: field.activeCrop?.id, record_type: 'spraying', operation_date: businessDate(), operation_content: '' }
    draftNotice.value = ''; restoringDraft = false
  } catch { /* Explicit cancellation keeps the previous draft and submission ID. */ }
}

// 格式化辅助
function formatRecordType(type: string) {
  const map: Record<string, { label: string; tag: string }> = {
    inspection: { label: '现场检查（非作业实施）', tag: 'info' },
    spraying: { label: '植保施药', tag: 'danger' },
    fertilization: { label: '水肥灌溉', tag: 'success' },
    irrigation: { label: '日常浇水', tag: 'primary' },
    pruning: { label: '整枝打杈', tag: 'warning' },
    weeding: { label: '人工除草', tag: 'info' },
    harvest: { label: '批次采收', tag: 'success' },
    other: { label: '其他农事', tag: '' }
  }
  return map[type] || { label: type, tag: 'info' }
}

onMounted(() => {
  checkScreenSize()
  if (typeof window !== 'undefined') {
    window.addEventListener('resize', checkScreenSize)
  }
  revokeTimer = setInterval(() => { now.value = Date.now() }, 1000)
  fetchFarms()
})
</script>

<template>
  <div class="app-page agri-farms-page">
    <PageHeader
      title="智慧农场与种植地块看板"
      description="农场多租户空间配置、数字地块土壤档案与作物全生命周期生长批次管理"
    >
      <template #actions>
        <el-button v-if="currentFarm && (auth.user?.role === 'admin' || currentFarm.owner_id === auth.user?.id)" :loading="mutationBusy" @click="toggleArchive()">{{ currentFarm.archived_at ? '恢复农场' : '归档农场' }}</el-button>
        <el-button v-if="currentFarm && (auth.user?.role === 'admin' || currentFarm.owner_id === auth.user?.id)" :disabled="!!currentFarm.archived_at" @click="editLocation">绑定 / 修改天气位置</el-button>
        <div class="farm-select-wrap">
          <span class="label">当前农场：</span>
          <el-select
            v-model="currentFarmId"
            placeholder="请选择农场"
            class="farm-select"
          >
            <el-option
              v-for="farm in farms"
              :key="farm.id"
              :label="farm.name + (farm.archived_at ? '（已归档）' : '')"
              :value="farm.id"
            />
          </el-select>
        </div>

        <el-button type="primary" :icon="Plus" @click="farmDialogVisible = true">
          新建农场
        </el-button>
        <el-button :icon="Refresh" circle @click="fetchFields" />
      </template>
    </PageHeader>

    <!-- 农场基本属性概览 -->
    <div v-if="currentFarm" class="app-card farm-meta-card">
      <el-alert v-if="currentFarm.archived_at" title="农场已归档，可查看追溯历史；恢复后才能登记或修改。" type="info" :closable="false" />
      <div class="farm-meta-bar">
        <div class="meta-item">
          <el-icon><Location /></el-icon>
          <span>所在地域：{{ currentFarm.province }} {{ currentFarm.city }} {{ currentFarm.district }}</span>
        </div>
        <div class="meta-item weather-meta-item">
          <FieldWeatherPanel :farm-id="currentFarmId" :refresh-key="weatherVersion" inline />
        </div>
        <div class="meta-item">
          <el-icon><Sunny /></el-icon>
          <span>规划总面积：<strong>{{ currentFarm.area }} 亩</strong></span>
        </div>
        <div class="meta-item">
          <el-icon><List /></el-icon>
          <span>已建地块/大棚数：<strong>{{ fields.length }} 块</strong></span>
        </div>
        <div class="meta-item desc">
          <span>简介：{{ currentFarm.description || '无' }}</span>
        </div>
      </div>
    </div>

    <!-- 地块列表与卡片网格 -->
    <div class="content-body" v-loading="loading">
      <div class="section-header">
        <div class="section-title">
          <span>地块与日光温室大棚 ({{ fields.length }})</span>
          <el-tag size="small" type="success" effect="light">田块档案与农事追溯</el-tag>
        </div>
        <el-button type="success" :icon="Plus" :disabled="!!currentFarm?.archived_at || mutationBusy" @click="openCreateField">
          新增地块/大棚
        </el-button>
      </div>

      <el-empty
        v-if="fields.length === 0 && !loading"
        description="该农场暂无地块，点击上方按钮新增地块"
      />

      <div class="fields-grid" v-else>
        <div v-for="field in fields" :key="field.id" class="app-card app-card--hover field-card">
          <div class="card-header">
            <div class="field-name-wrap">
              <span class="field-icon"><el-icon><House /></el-icon></span>
              <span class="field-title">{{ field.name }}</span>
              <el-tag v-if="field.archived_at" type="info">已归档</el-tag>
            </div>
            <el-dropdown trigger="click">
              <span class="more-btn">•••</span>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item :disabled="archivedField(field) || mutationBusy || !(auth.user?.role === 'admin' || currentFarm?.owner_id === auth.user?.id)" @click="openEditField(field)">编辑田块 / 设施类型</el-dropdown-item>
                  <el-dropdown-item :disabled="!!currentFarm?.archived_at || mutationBusy || !(auth.user?.role === 'admin' || currentFarm?.owner_id === auth.user?.id)" @click="toggleArchive(field)">
                    {{ field.archived_at ? '恢复田块' : '归档田块（保留历史）' }}
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>

          <!-- 地块物理环境属性 -->
          <div class="field-specs">
            <div class="spec-pill"><span class="spec-k">环境</span><span class="spec-v">{{ facilityLabels[field.facility_type || 'unknown'] }}</span></div>
            <div class="spec-pill">
              <span class="spec-k">面积</span>
              <span class="spec-v">{{ field.area }} 亩</span>
            </div>
            <div class="spec-pill">
              <span class="spec-k">土壤</span>
              <span class="spec-v">{{ field.soil_type || '未登记' }}</span>
            </div>
            <div class="spec-pill">
              <span class="spec-k">灌溉</span>
              <span class="spec-v">{{ field.irrigation_type || '未登记' }}</span>
            </div>
          </div>

          <!-- 二级入口：现场环境观测 / 批次档案（一行紧凑排列） -->
          <div class="field-quick-links">
            <FieldEnvironmentPanel :field="field" :readonly="archivedField(field) || !(auth.user?.role === 'admin' || currentFarm?.owner_id === auth.user?.id)" @saved="weatherVersion++" />
            <span class="ql-divider"></span>
            <FieldCropHistory :field="field" :readonly="archivedField(field) || !(auth.user?.role === 'admin' || currentFarm?.owner_id === auth.user?.id)" @changed="cropHistoryChanged" @records="(crop: CropPlanting) => openRecordDrawer(field, crop)" />
          </div>

          <!-- 作物种植批次状态卡 -->
          <div class="crop-status-box" :class="{ 'has-crop': field.activeCrop }">
            <el-select v-if="(field.cropOptions?.length || 0) > 1" :model-value="field.activeCrop?.id" placeholder="请选择在种批次"
              @change="(id: number) => field.activeCrop = field.cropOptions?.find(crop => crop.id === id)">
              <el-option v-for="crop in field.cropOptions" :key="crop.id" :value="crop.id" :label="`${crop.crop} · ${crop.planting_date} · #${crop.id}`" />
            </el-select>
            <div v-if="field.activeCrop" class="active-crop-info">
              <div class="crop-title-row">
                <span class="crop-name">{{ field.activeCrop.crop }} ({{ field.activeCrop.variety || '品种未登记' }})</span>
                <el-tag size="small" type="warning" effect="dark">{{ field.activeCrop.growth_stage }}</el-tag>
              </div>
              <div class="crop-date-row">
                <span>定植: {{ field.activeCrop.planting_date }}</span>
                <span>预计采收: {{ field.activeCrop.expected_harvest_date || '未定' }}</span>
              </div>
              <el-button link type="primary" :disabled="archivedField(field)" @click="openEditCrop(field)">更新阶段 / 结束批次</el-button>
            </div>
            <div v-else class="no-crop-info">
              <span>{{ field.cropOptions?.length ? '请先明确选择在种批次' : '当前处于休耕/空茬状态' }}</span>
              <el-button type="primary" link size="small" :disabled="archivedField(field)" @click="openCreateCrop(field)">
                建立种植档案
              </el-button>
            </div>
          </div>

          <!-- 底部操作与农事追溯快捷键 -->
          <div class="card-actions">
            <el-button
              size="default"
              class="action-btn"
              :icon="List"
              @click="openRecordDrawer(field)"
            >
              农事履历与追溯
            </el-button>
            <el-button
              v-if="!field.activeCrop"
              size="default"
              type="primary"
              class="action-btn"
              :icon="DocumentAdd"
              @click="openCreateCrop(field)"
            >
              登记种植
            </el-button>
            <el-button
              v-else
              size="default"
              type="success"
              plain
              class="action-btn"
              :icon="Calendar"
              @click="openRecordDrawer(field)"
            >
              录入农事
            </el-button>
          </div>
        </div>
      </div>
    </div>

    <el-dialog v-model="locationDialog" title="绑定 / 修改天气位置" :width="isMobile ? '94%' : '680px'" :close-on-click-modal="!mutationBusy" destroy-on-close>
      <el-form label-width="100px"><FarmLocationForm :key="locationTargetId || 0" v-model="locationForm" /></el-form>
      <template #footer><el-button :loading="mutationBusy" @click="saveLocation">保存位置</el-button></template>
    </el-dialog>
    <!-- 创建农场对话框 -->
    <el-dialog v-model="farmDialogVisible" title="创建新数字农场" :width="isMobile ? '94%' : '560px'">
      <el-form :model="farmForm" label-width="100px" class="farm-dialog-form">
        <el-form-item label="农场名称" required>
          <el-input v-model="farmForm.name" placeholder="例如：寿光现代数字果蔬示范农场" />
        </el-form-item>
        <FarmLocationForm v-model="farmForm" />
        <el-form-item label="详细地址">
          <el-input v-model="farmForm.address" placeholder="例如：现代农业高新技术示范园区8号棚区" />
        </el-form-item>
        <el-form-item label="规划面积">
          <el-input-number v-model="farmForm.area" :min="0.1" :step="5" :precision="1" />
          <span style="margin-left: 10px; color: var(--app-text-placeholder)">亩</span>
        </el-form-item>
        <el-form-item label="农场描述">
          <el-input v-model="farmForm.description" type="textarea" :rows="3" placeholder="农场简介、主要种植品类及数字化设施" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="farmDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="mutationBusy" @click="submitCreateFarm">确认创建</el-button>
      </template>
    </el-dialog>

    <!-- 创建地块对话框 -->
    <el-dialog v-model="fieldDialogVisible" :title="editingFieldId ? '编辑田块 / 设施类型' : '新增地块 / 温室大棚'" :width="isMobile ? '94%' : '520px'">
      <el-form :model="fieldForm" label-width="100px" :disabled="mutationBusy">
        <el-form-item label="地块名称" required>
          <el-input v-model="fieldForm.name" placeholder="例如：1号日光温室大棚" />
        </el-form-item>
        <el-form-item label="地块面积" required>
          <el-input-number v-model="fieldForm.area" :min="0.1" :step="0.5" :precision="1" />
          <span style="margin-left: 10px; color: var(--app-text-placeholder)">亩</span>
        </el-form-item>
        <el-form-item label="设施类型">
          <el-select v-model="fieldForm.facility_type" style="width: 100%">
            <el-option v-for="(label, code) in facilityLabels" :key="code" :value="code" :label="label" />
          </el-select>
        </el-form-item>
        <p>按实际情况登记；旧档案默认未核实，系统不会从名称或备注推断设施类型。</p>
        <el-form-item label="土壤类型">
          <el-select v-model="fieldForm.soil_type" placeholder="请选择土壤类型" style="width: 100%">
            <el-option label="沙壤土 (保肥透气)" value="沙壤土" />
            <el-option label="轻壤土 (养分均衡)" value="轻壤土" />
            <el-option label="黏壤土 (保水保肥)" value="黏壤土" />
            <el-option label="黄绵土" value="黄绵土" />
          </el-select>
        </el-form-item>
        <el-form-item label="灌溉方式">
          <el-select v-model="fieldForm.irrigation_type" placeholder="请选择灌溉方式" style="width: 100%">
            <el-option label="水肥一体化膜下滴灌" value="水肥一体化膜下滴灌" />
            <el-option label="微喷灌系统" value="微喷灌系统" />
            <el-option label="智能高架管道喷灌" value="智能高架管道喷灌" />
            <el-option label="漫灌" value="漫灌" />
          </el-select>
        </el-form-item>
        <el-form-item label="设施备注">
          <el-input v-model="fieldForm.remark" type="textarea" :rows="2" placeholder="如配置的双向通风机、湿帘风机、遮阳网等" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="fieldDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="mutationBusy" @click="submitCreateField">{{ editingFieldId ? '保存档案' : '确认新增' }}</el-button>
      </template>
    </el-dialog>

    <!-- 登记作物种植档案对话框 -->
    <el-dialog v-model="cropDialogVisible" :title="editingCropId ? '更新种植档案' : '建立地块种植档案'" :width="isMobile ? '94%' : '540px'">
      <el-form :model="cropForm" label-width="110px">
        <el-form-item label="批次状态">
          <el-select v-model="cropForm.status">
            <el-option label="在种" value="active" />
            <el-option label="已完成采收" value="harvested" />
            <el-option label="已终止" value="terminated" />
          </el-select>
        </el-form-item>
        <el-form-item label="种植作物" required>
          <el-input v-model="cropForm.crop" placeholder="例如：番茄、黄瓜、甜椒" />
        </el-form-item>
        <el-form-item label="品种规格">
          <el-input v-model="cropForm.variety" placeholder="例如：普罗旺斯、草莓番茄" />
        </el-form-item>
        <el-form-item label="当前物候期" required>
          <el-select v-model="cropForm.growth_stage" style="width: 100%">
            <el-option label="待核实" value="待核实" />
            <el-option label="苗期 / 缓苗期" value="苗期" />
            <el-option label="开花坐果期 (需肥需水关键期)" value="开花坐果期" />
            <el-option label="膨果转色期" value="果实膨大期" />
            <el-option label="采收盛期" value="采收期" />
          </el-select>
        </el-form-item>
        <el-form-item label="定植日期" required>
          <el-date-picker
            v-model="cropForm.planting_date"
            type="date"
            placeholder="选择定植日期"
            value-format="YYYY-MM-DD"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="预计采收日期">
          <el-date-picker
            v-model="cropForm.expected_harvest_date"
            type="date"
            placeholder="预计采收日期"
            value-format="YYYY-MM-DD"
            style="width: 100%"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cropDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitCreateCrop">保存档案</el-button>
      </template>
    </el-dialog>

    <!-- 农事履历与追溯抽屉 -->
    <el-drawer
      v-model="recordDrawerVisible"
      :title="`农事记录与安全溯源 - ${currentFieldForRecord?.name || ''}`"
      :size="isMobile ? '100%' : '620px'"
    >
      <div class="drawer-content" v-loading="recordLoading">
        <!-- 快捷录入新记录卡片 -->
        <el-alert v-if="recordHistoryOnly" :title="`只读查看明确绑定批次 #${recordBatchFilter} 的履历；未绑定批次的记录请到田块全部履历查看。`" type="info" :closable="false" />
        <el-card v-if="!recordHistoryOnly" shadow="never" class="record-input-card">
          <template #header>
            <div class="card-header-inner">
              <span class="card-header-title"><el-icon><EditPen /></el-icon> 快速登记农事作业</span>
            </div>
          </template>

          <el-form :model="newRecordForm" label-width="90px" size="small" :disabled="archivedField(currentFieldForRecord)">
            <el-alert v-if="draftNotice" :title="draftNotice" type="info" :closable="false" />
            <el-button link :disabled="mutationBusy" @click="discardRecordDraft">清空草稿，另记一笔</el-button>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="作业类型">
                  <el-select v-model="newRecordForm.record_type" style="width: 100%">
                    <el-option label="植保施药" value="spraying" />
                    <el-option label="水肥灌溉" value="fertilization" />
                    <el-option label="日常浇水" value="irrigation" />
                    <el-option label="整枝打杈" value="pruning" />
                    <el-option label="人工除草" value="weeding" />
                    <el-option label="批次采收" value="harvest" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="作业日期">
                  <el-date-picker
                    v-model="newRecordForm.operation_date"
                    type="date"
                    value-format="YYYY-MM-DD"
                    style="width: 100%"
                  />
                </el-form-item>
              </el-col>
            </el-row>

            <el-form-item label="作业内容" required>
              <el-input v-model="newRecordForm.operation_content" placeholder="例：喷雾防治早疫病，严格打透叶背" />
            </el-form-item>

            <el-row :gutter="12" v-if="newRecordForm.record_type === 'spraying' || newRecordForm.record_type === 'fertilization'">
              <el-col :span="12">
                <el-form-item label="药剂/肥料">
                  <el-input v-model="newRecordForm.chemical_name" placeholder="如: 68%精甲霜·锰锌" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="用药剂量">
                  <el-input v-model="newRecordForm.dosage" placeholder="如: 100g/亩" />
                </el-form-item>
              </el-col>
            </el-row>

            <el-row :gutter="12" v-if="newRecordForm.record_type === 'spraying'">
              <el-col :span="12">
                <el-form-item label="安全间隔期">
                  <el-input-number v-model="newRecordForm.safety_interval_days" :min="0" :max="90" style="width: 100%" />
                  <span style="font-size: 11px; color: var(--app-text-placeholder)">天 (PHI)</span>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="操作农艺师">
                  <el-input v-model="newRecordForm.operator" placeholder="操作人员" />
                </el-form-item>
              </el-col>
            </el-row>

            <div style="text-align: right; margin-top: 10px">
              <el-button type="primary" size="small" @click="submitNewRecord">保存记录</el-button>
            </div>
          </el-form>
        </el-card>

        <!-- 历史时间轴 -->
        <div class="records-timeline-wrap">
          <h4 class="timeline-title"><el-icon><Notebook /></el-icon> 农事追溯历史档案 ({{ farmingRecords.length }})</h4>
          <el-radio-group v-model="recordStatus" @change="refreshRecordHistory" size="small">
            <el-radio-button value="active">有效记录</el-radio-button>
            <el-radio-button value="revoked">已撤销</el-radio-button>
            <el-radio-button value="all">全部档案</el-radio-button>
          </el-radio-group>
          <p class="app-text-secondary">误录撤销保留原内容、撤销时间和原因；已撤销记录不再参与统计与计划分析。</p>
          <el-empty v-if="farmingRecords.length === 0" description="暂无历史农事记录" />
          <el-timeline v-else class="records-timeline">
            <el-timeline-item
              v-for="record in farmingRecords"
              :key="record.id"
              :timestamp="record.operation_date"
              placement="top"
              :type="formatRecordType(record.record_type).tag as any"
            >
              <div class="timeline-card">
                <div class="timeline-head">
                  <el-tag size="small" :type="formatRecordType(record.record_type).tag as any">
                    {{ formatRecordType(record.record_type).label }}
                  </el-tag>
                  <el-tag v-if="record.revoked_at" type="info" size="small">已撤销</el-tag>
                  <el-button v-else-if="canRevoke(record)" type="danger" size="small" :loading="mutationBusy" @click="revokeRecord(record)">撤销误录</el-button>
                  <small v-else-if="recordHistoryOnly">历史档案只读</small>
                  <small v-else>非本人录入、已超时或创建者不可核实，请联系管理员</small>
                  <span class="operator-badge" v-if="record.operator">农艺师: {{ record.operator }}</span>
                </div>
                <div class="timeline-content">{{ record.operation_content }}</div>
                <div v-if="record.revoked_at" class="timeline-content">
                  撤销时间：{{ new Date(record.revoked_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }}
                  <br />撤销人：{{ record.revoked_by_name || '历史账号或已删除用户' }}
                  <br />撤销原因：{{ record.revoke_reason || '未记录原因' }}
                </div>
                <div class="timeline-meta" v-if="record.chemical_name">
                  <span class="meta-tag">{{ record.chemical_name }}</span>
                  <span class="meta-tag" v-if="record.dosage">剂量: {{ record.dosage }}</span>
                  <span class="meta-tag phi" v-if="record.safety_interval_days != null">
                    安全间隔期: {{ record.safety_interval_days }}天
                  </span>
                </div>
              </div>
            </el-timeline-item>
          </el-timeline>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.agri-farms-page {
  min-height: calc(100vh - 64px);
}

.farm-select-wrap {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}

.farm-select-wrap .label {
  font-size: var(--app-text-base);
  color: var(--app-text-secondary);
  font-weight: 500;
  white-space: nowrap;
}

.farm-select {
  width: 220px;
}

.farm-meta-card {
  padding: var(--app-space-4) var(--app-space-6);
  margin-bottom: var(--app-space-6);
}

.farm-meta-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--app-space-6);
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
}

.meta-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.meta-item.desc {
  flex-grow: 1;
  color: var(--app-text-placeholder);
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--app-space-4);
}

.section-title {
  display: flex;
  align-items: center;
  gap: var(--app-space-3);
  font-size: var(--app-text-lg);
  font-weight: 600;
  color: var(--app-text);
}

.fields-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--app-space-5);
}

.field-card {
  padding: var(--app-space-5);
  display: flex;
  flex-direction: column;
}

.field-card:hover {
  transform: translateY(-2px);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--app-space-4);
}

.field-name-wrap {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}

.field-icon {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--app-text-lg);
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  border-radius: var(--app-radius-md);
}

.field-title {
  font-size: var(--app-text-lg);
  font-weight: 600;
  color: var(--app-text);
}

.more-btn {
  cursor: pointer;
  color: var(--app-text-placeholder);
  font-size: var(--app-text-lg);
  padding: 2px 6px;
  border-radius: var(--app-radius-sm);
}

.more-btn:hover {
  background: var(--app-hover);
}

.field-specs {
  display: flex;
  gap: var(--app-space-2);
  margin-bottom: var(--app-space-4);
}

.spec-pill {
  flex: 1;
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-sm);
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.spec-k {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

.spec-v {
  font-size: var(--app-text-xs);
  font-weight: 600;
  color: var(--app-text);
  margin-top: 2px;
  text-align: center;
  line-height: 1.4;
  overflow-wrap: break-word;
}

/* 二级入口行：两个 link 按钮一行排列 + 分隔线，不再整行拉伸居中 */
.field-quick-links {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
  margin-top: calc(-1 * var(--app-space-2));
  margin-bottom: var(--app-space-3);
}

.field-quick-links .el-button.is-link {
  min-width: 0;
  padding-left: 4px;
  padding-right: 4px;
}

.ql-divider {
  width: 1px;
  height: 12px;
  background: var(--app-border);
  flex-shrink: 0;
}

.crop-status-box {
  background: var(--app-surface-2);
  border: 1px dashed var(--app-border);
  border-radius: var(--app-radius-md);
  padding: var(--app-space-3);
  margin-bottom: var(--app-space-4);
  flex-grow: 1;
}

.crop-status-box.has-crop {
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-8);
}

.crop-title-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.crop-name {
  font-size: var(--app-text-base);
  font-weight: 600;
  color: var(--el-color-primary-dark-2);
}

.crop-date-row {
  display: flex;
  justify-content: space-between;
  font-size: var(--app-text-xs);
  color: var(--el-color-primary);
}

.no-crop-info {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: var(--app-text-sm);
  color: var(--app-text-placeholder);
  padding: 4px 0;
}

.card-actions {
  display: flex;
  gap: var(--app-space-3);
}

.card-actions .action-btn {
  flex: 1;
}

/* 抽屉样式 */
.drawer-content {
  padding: var(--app-space-3);
}

.record-input-card {
  margin-bottom: var(--app-space-6);
}

.card-header-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.card-header-inner {
  font-size: var(--app-text-base);
  font-weight: 600;
  color: var(--app-text);
}

.timeline-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 var(--app-space-4);
  font-size: var(--app-text-base);
  color: var(--app-text);
}

.records-timeline {
  padding-left: var(--app-space-3);
}

.timeline-card {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-md);
  padding: 10px 14px;
}

.timeline-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.operator-badge {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

.timeline-content {
  font-size: var(--app-text-sm);
  color: var(--app-text);
  line-height: 1.4;
  margin-bottom: 6px;
}

.timeline-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-2);
  font-size: var(--app-text-xs);
}

.meta-tag {
  background: var(--app-hover);
  padding: 2px 8px;
  border-radius: var(--app-radius-sm);
  color: var(--app-text-secondary);
}

.meta-tag.phi {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}

/* 移动端与窄屏响应式自适应 */
@media (max-width: 768px) {
  .farm-meta-card {
    padding: var(--app-space-3) var(--app-space-4);
    margin-bottom: var(--app-space-4);
  }

  .farm-select-wrap {
    /* 独占一行，避免与按钮同行被挤压裁切 */
    flex: 1 1 100%;
    min-width: 0;
    order: -1;
  }

  .farm-select {
    width: 100%;
  }

  .farm-meta-bar {
    gap: var(--app-space-3);
    flex-direction: column;
    align-items: flex-start;
  }

  .fields-grid {
    grid-template-columns: 1fr;
    gap: var(--app-space-3);
  }

  .field-card {
    padding: var(--app-space-4);
  }
}
</style>
