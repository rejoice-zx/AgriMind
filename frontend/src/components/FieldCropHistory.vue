<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Files } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { getCropHistory, getCropStatusHistory, reopenCrop } from '@/api/agriculture'
import { draftOwnerKey, readScopedDraft, saveScopedDraft } from '@/utils/scopedDraft'
import { newRequestId } from '@/utils/requestId'
import type { FieldItem, CropPlanting, CropStatusEvent } from '@/types/agriculture'

const props = defineProps<{ field: FieldItem; readonly?: boolean }>()
const emit = defineEmits<{ changed: [crop: CropPlanting]; records: [crop: CropPlanting] }>()
const auth = useAuthStore(), visible = ref(false), loading = ref(false), saving = ref(false)
const owner = computed(() => draftOwnerKey(auth.user))
const crops = ref<CropPlanting[]>([]), next = ref<number | null>(null), snapshot = ref<number | undefined>()
const selected = ref<CropPlanting | null>(null), events = ref<CropStatusEvent[]>([]), eventBusy = ref(false)
const eventNext = ref<number | null>(null), eventSnapshot = ref<number | undefined>()
const error = ref(''), eventError = ref(''), reason = ref(''), draftNotice = ref('')
const pending = ref<{ cropId: number; request_id: string; reason: string } | null>(null)
const labels: Record<string, string> = { active: '在种', harvested: '已采收', terminated: '已终止' }
const time = (text: string) => new Date(text).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
let revision = 0, cropRevision = 0, eventRevision = 0, alive = true
const draftScope = (cropId: number) => `crop-reopen:${props.field.id}:${cropId}`

async function loadCrops(reset = false) {
  if (loading.value && !reset) return
  const version = ++cropRevision, id = props.field.id, actor = owner.value
  if (reset) { crops.value = []; next.value = null; snapshot.value = undefined }
  loading.value = true; error.value = ''
  try {
    const response = await getCropHistory(id, { limit: 20, before_id: reset ? undefined : next.value ?? undefined, snapshot_id: snapshot.value })
    if (!alive || version !== cropRevision || id !== props.field.id || actor !== owner.value) return
    crops.value = reset ? response.data.items : [...crops.value, ...response.data.items]
    next.value = response.data.next_before_id; snapshot.value = response.data.snapshot_id
  } catch { if (alive && version === cropRevision) error.value = '批次档案读取失败，请重试' }
  finally { if (alive && version === cropRevision) loading.value = false }
}
async function open() { visible.value = true; await loadCrops(true) }
async function loadEvents(reset = false) {
  if (!selected.value || (eventBusy.value && !reset)) return
  const version = ++eventRevision, id = selected.value.id, actor = owner.value, context = revision
  if (reset) { events.value = []; eventNext.value = null; eventSnapshot.value = undefined }
  eventBusy.value = true; eventError.value = ''
  try {
    const response = await getCropStatusHistory(id, { limit: 20, before_id: reset ? undefined : eventNext.value ?? undefined, snapshot_id: eventSnapshot.value })
    if (!alive || version !== eventRevision || selected.value?.id !== id || actor !== owner.value || context !== revision) return
    events.value = reset ? response.data.items : [...events.value, ...response.data.items]
    eventNext.value = response.data.next_before_id; eventSnapshot.value = response.data.snapshot_id
  } catch { if (alive && version === eventRevision) eventError.value = '状态历史读取失败，请重试' }
  finally { if (alive && version === eventRevision) eventBusy.value = false }
}
async function selectCrop(crop: CropPlanting) {
  if (saving.value) return
  selected.value = crop; reason.value = ''; pending.value = null; draftNotice.value = ''
  await loadEvents(true)
}
function restoreRequest() {
  if (!selected.value || saving.value) return
  const saved = readScopedDraft<{ cropId: number; request_id: string; reason: string }>(owner.value, draftScope(selected.value.id))
  if (!saved || saved.cropId !== selected.value.id || !saved.request_id || !saved.reason) { ElMessage.warning('没有可恢复的请求，或本机草稿已过期'); return }
  pending.value = saved; reason.value = saved.reason; draftNotice.value = '已恢复原批次、原因和请求编号；重试核对原回执'
}
async function discardRequest() {
  if (!selected.value || saving.value) return
  const id = selected.value.id, actor = owner.value, context = revision
  try { await ElMessageBox.confirm('请先核对状态历史。确定仅清空本机恢复请求，再重新填写原因？', '核对批次') } catch { return }
  if (!alive || selected.value?.id !== id || actor !== owner.value || context !== revision) return
  saveScopedDraft(actor, draftScope(id), null); pending.value = null; reason.value = ''; draftNotice.value = ''
}
async function reopen() {
  if (!selected.value || saving.value || props.readonly) return
  const id = selected.value.id, actor = owner.value, context = revision, fieldId = props.field.id
  if (!pending.value) {
    if (!['harvested', 'terminated'].includes(selected.value.status)) { ElMessage.warning('当前批次已在种，请核对状态历史'); return }
    if (!reason.value.trim()) { ElMessage.warning('请填写恢复原因'); return }
    pending.value = { cropId: id, request_id: newRequestId(), reason: reason.value.trim() }
    draftNotice.value = saveScopedDraft(actor, draftScope(id), pending.value) ? '恢复请求已保存在本机，重试保留原编号' : '本机无法保存请求，请保留页面及编号'
  }
  if (pending.value.cropId !== id) { ElMessage.warning('原请求属于另一批次，请核对'); return }
  const body = { request_id: pending.value.request_id, reason: pending.value.reason }
  saving.value = true
  try {
    const response = await reopenCrop(id, body)
    if (!alive || context !== revision || actor !== owner.value || fieldId !== props.field.id || selected.value?.id !== id) return
    selected.value = response.data.crop
    saveScopedDraft(actor, draftScope(id), null); pending.value = null; reason.value = ''; draftNotice.value = ''
    ElMessage.success(response.data.replayed ? '已恢复原变更回执，当前状态以档案为准' : '已恢复在种，原结束记录与恢复原因均保留')
    await loadCrops(true); await loadEvents(true)
    emit('changed', response.data.crop)
  } catch (err: any) {
    if (alive && context === revision && actor === owner.value) ElMessage.error(err.response?.data?.detail || '恢复结果未确认，请保留原编号，核对历史后重试')
  } finally { if (alive) saving.value = false }
}
watch(() => [props.field.id, owner.value], () => {
  revision++; cropRevision++; eventRevision++; visible.value = false; crops.value = []; selected.value = null
  events.value = []; loading.value = false; eventBusy.value = false; pending.value = null; reason.value = ''; error.value = ''; eventError.value = ''
})
onBeforeUnmount(() => { alive = false; revision++; cropRevision++; eventRevision++ })
</script>

<template>
  <el-button link type="primary" :icon="Files" :disabled="saving" @click="open">全部批次 / 历史档案</el-button>
  <el-dialog v-model="visible" :title="`${field.name} · 全部批次档案`" width="min(96vw, 760px)" append-to-body :close-on-click-modal="false">
    <div class="crop-dialog">
      <el-alert v-if="error" :title="error" type="error" :closable="false" class="dlg-alert" />
      <div class="dlg-bar">
        <el-button :loading="loading" :disabled="saving" @click="loadCrops(true)">刷新批次</el-button>
      </div>
      <el-table :data="crops" max-height="300" v-loading="loading" empty-text="暂无已加载批次">
        <el-table-column label="批次" min-width="160"><template #default="{ row }">#{{ row.id }} · {{ row.crop }}<br />{{ row.variety || '品种未登记' }} · {{ row.growth_stage }}</template></el-table-column>
        <el-table-column label="日期与状态" min-width="180"><template #default="{ row }">{{ row.planting_date }} → {{ row.expected_harvest_date || '预计采收未登记' }}<br />{{ labels[row.status] || row.status }}</template></el-table-column>
        <el-table-column label="查看" min-width="180"><template #default="{ row }"><el-button link :disabled="saving" @click="selectCrop(row)">状态与恢复</el-button><el-button link :disabled="saving" @click="emit('records', row)">本批次农事履历</el-button></template></el-table-column>
      </el-table>
      <div class="dlg-bar dlg-bar--top">
        <el-button v-if="next !== null" :loading="loading" :disabled="saving" @click="loadCrops()">加载更早批次</el-button>
        <p class="dlg-tip">按登记顺序分页，刷新查看新批次；历史批次与农事记录保留。</p>
      </div>
      <section v-if="selected" class="dlg-card">
        <h4 class="dlg-sub-title">#{{ selected.id }} {{ selected.crop }} · {{ labels[selected.status] || selected.status }}</h4>
        <el-alert v-if="eventError" :title="eventError" type="error" :closable="false" class="dlg-alert" />
        <el-table :data="events" max-height="220" v-loading="eventBusy" empty-text="暂无状态变更记录；旧批次此前未保存的历史不会被推测补写">
          <el-table-column label="变更" min-width="150"><template #default="{ row }">{{ labels[row.previous_status] || row.previous_status }} → {{ labels[row.new_status] || row.new_status }}<br />{{ time(row.created_at) }}</template></el-table-column>
          <el-table-column prop="actor_name" label="登记人" min-width="90" />
          <el-table-column label="原因" min-width="160"><template #default="{ row }">{{ row.reason || '普通状态登记，未填写原因' }}</template></el-table-column>
        </el-table>
        <div class="dlg-bar dlg-bar--top dlg-btn-row">
          <el-button :loading="eventBusy" @click="loadEvents(true)">刷新状态历史</el-button>
          <el-button v-if="eventNext !== null" :loading="eventBusy" @click="loadEvents()">更早状态变更</el-button>
        </div>
        <template v-if="!readonly">
          <p class="dlg-tip">所有者或管理员可填写原因恢复误结束的批次，不覆盖原结束记录。</p>
          <div class="dlg-btn-row">
            <el-button :disabled="saving" @click="restoreRequest">恢复本机的恢复请求</el-button>
            <el-button :disabled="saving" @click="discardRequest">核对后清空本机请求</el-button>
          </div>
          <template v-if="pending || selected.status !== 'active'">
            <el-input v-model="reason" type="textarea" :maxlength="500" :disabled="saving || !!pending" placeholder="必填：说明为什么恢复在种" />
            <p v-if="draftNotice" class="dlg-tip">{{ draftNotice }}</p>
            <div class="dlg-bar dlg-bar--top">
              <el-button type="primary" :loading="saving" @click="reopen">{{ pending ? '按原编号重试核对' : '填写原因后恢复在种' }}</el-button>
            </div>
          </template>
        </template>
      </section>
    </div>
  </el-dialog>
</template>

<style scoped>
/* 弹窗内容分组：卡片化区块 + 统一间距节奏 */
.dlg-alert {
  margin-bottom: var(--app-space-3);
}

.dlg-bar {
  margin-bottom: var(--app-space-2);
}

.dlg-bar--top {
  margin-top: var(--app-space-3);
  margin-bottom: 0;
}

.dlg-tip {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  line-height: 1.6;
  margin: var(--app-space-1) 0 0;
}

.dlg-card {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-lg);
  padding: var(--app-space-3) var(--app-space-4);
  margin-top: var(--app-space-4);
}

.dlg-sub-title {
  margin: 0 0 var(--app-space-3);
  font-size: var(--app-text-base);
  font-weight: 600;
  color: var(--app-text);
  padding-bottom: var(--app-space-2);
  border-bottom: 1px solid var(--app-border-light);
}

.dlg-btn-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-2);
}

.dlg-btn-row .el-button {
  margin-left: 0;
}

.dlg-card .dlg-tip {
  margin: var(--app-space-2) 0;
}
</style>
