<script setup lang="ts">
import { ref, watch, computed, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Odometer } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { getFieldEnvironment, createEnvironmentObservation, getEnvironmentHistory } from '@/api/agriculture'
import { newRequestId } from '@/utils/requestId'
import { draftOwnerKey, saveScopedDraft, readScopedDraft } from '@/utils/scopedDraft'
import type { FieldItem, FieldEnvironment, EnvironmentObservationCreate, EnvironmentHistoryItem } from '@/types/agriculture'

const props = defineProps<{ field: FieldItem; readonly?: boolean }>()
const emit = defineEmits<{ saved: [] }>()
const auth = useAuthStore()
const visible = ref(false), loading = ref(false), saving = ref(false)
const context = ref<FieldEnvironment | null>(null)
const error = ref(''), draftNotice = ref('')
const historyRows = ref<EnvironmentHistoryItem[]>([]), historyBusy = ref(false), historyError = ref(''), historyLoaded = ref(false)
const historyNext = ref<number | null>(null), historySnapshot = ref<number | undefined>()
let historyRevision = 0
const facilityNames: Record<string, string> = { unknown: '未核实', open_field: '露地', greenhouse: '温室/大棚', tunnel: '拱棚/覆盖栽培', other: '其他设施' }
const observationStates: Record<string, string> = { recent: '近期录入，仍需核实', stale: '已超过参考时限', context_changed: '设施类型已变化', scope_mismatch: '观测位置不匹配', future: '未来时间，不能作当前依据' }
const observedAt = ref<Date | null>(null)
type Form = Omit<EnvironmentObservationCreate, 'request_id' | 'facility_type' | 'observed_at' | 'measurement_scope'> & { measurement_scope?: EnvironmentObservationCreate['measurement_scope'] }
const emptyForm = (): Form => ({ temperature: undefined, humidity: undefined, ventilation: 'unknown', covering: 'unknown', notes: '' })
const form = ref<Form>(emptyForm())
const pending = ref<EnvironmentObservationCreate | null>(null)
const owner = computed(() => draftOwnerKey(auth.user))
const scope = computed(() => `environment:${props.field.id}`)
const states: Record<string, string> = { unknown: '未核实', open: '开启', closed: '关闭', partial: '部分', not_applicable: '不适用', covered: '覆盖', uncovered: '未覆盖' }
let revision = 0, restoring = false, alive = true
const time = (text?: string) => text ? new Date(text).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '未登记'

watch([form, observedAt, pending], () => {
  if (!visible.value || restoring) return
  draftNotice.value = saveScopedDraft(owner.value, scope.value, { form: form.value, observedAt: observedAt.value?.toISOString(), pending: pending.value })
    ? '草稿已保存在本机，7天内可恢复' : '本机无法保存草稿，请保留页面并复制内容'
}, { deep: true, flush: 'sync' })

async function refresh() {
  if (saving.value) return
  const id = props.field.id, actor = owner.value, version = ++revision
  loading.value = true; error.value = ''
  context.value = null
  try {
    const response = await getFieldEnvironment(id)
    if (alive && version === revision && id === props.field.id && actor === owner.value) context.value = response.data
  } catch { if (version === revision && alive) error.value = '现场观测读取失败，请重试' }
  finally { if (version === revision && alive) loading.value = false }
}
async function loadHistory(reset = false) {
  if (historyBusy.value && !reset) return
  const version = ++historyRevision, id = props.field.id, actor = owner.value
  if (reset) { historyRows.value = []; historyNext.value = null; historySnapshot.value = undefined; historyLoaded.value = false }
  historyBusy.value = true; historyError.value = ''
  try {
    const response = await getEnvironmentHistory(id, { before_id: reset ? undefined : historyNext.value ?? undefined,
      snapshot_id: historySnapshot.value, limit: 20 })
    if (!alive || version !== historyRevision || id !== props.field.id || actor !== owner.value) return
    historyRows.value = reset ? response.data.items : [...historyRows.value, ...response.data.items]
    historyNext.value = response.data.next_before_id; historySnapshot.value = response.data.snapshot_id; historyLoaded.value = true
  } catch { if (version === historyRevision && alive) historyError.value = '历史观测读取失败，请重试' }
  finally { if (version === historyRevision && alive) historyBusy.value = false }
}
async function refreshAll() { await refresh(); if (visible.value && alive) await loadHistory(true) }
async function open() { visible.value = true; await refreshAll() }
function restoreDraft() {
  if (saving.value) return
  const saved = readScopedDraft<{ form: Form; observedAt?: string; pending?: EnvironmentObservationCreate }>(owner.value, scope.value)
  if (!saved?.form) { ElMessage.warning('没有可恢复草稿，或草稿已过期'); return }
  restoring = true
  form.value = { ...emptyForm(), ...saved.form }
  observedAt.value = saved.observedAt && Number.isFinite(Date.parse(saved.observedAt)) ? new Date(saved.observedAt) : null
  pending.value = saved.pending || null
  restoring = false
  draftNotice.value = '已恢复原观测时间及提交编号；请核对田块与观测位置'
}
async function discard() {
  const actor = owner.value, id = props.field.id
  try { await ElMessageBox.confirm('若上次提交超时，请先刷新核对最近观测。确定清空本机草稿，另建一条观测？', '核对观测') } catch { return }
  if (!alive || actor !== owner.value || id !== props.field.id) return
  restoring = true; form.value = emptyForm(); observedAt.value = null; pending.value = null; restoring = false
  saveScopedDraft(actor, scope.value, null); draftNotice.value = ''
}
async function submit() {
  if (saving.value || props.readonly) return
  if (!pending.value) {
    if (!observedAt.value || !Number.isFinite(observedAt.value.getTime()) || !form.value.measurement_scope) {
      ElMessage.warning('请填写实际观测时间及观测位置'); return
    }
    if (observedAt.value.getTime() > Date.now()) { ElMessage.warning('观测时间不能在未来'); return }
    if (form.value.temperature == null && form.value.humidity == null && form.value.ventilation === 'unknown' && form.value.covering === 'unknown') {
      ElMessage.warning('请至少填写一项实测温湿度或已核实的通风/覆盖状态'); return
    }
    pending.value = { ...form.value, request_id: newRequestId(), facility_type: props.field.facility_type || 'unknown',
      observed_at: observedAt.value.toISOString(), measurement_scope: form.value.measurement_scope }
  }
  const id = props.field.id, actor = owner.value, payload = { ...pending.value }, version = revision
  saving.value = true
  try {
    const response = await createEnvironmentObservation(id, payload)
    if (!alive || id !== props.field.id || actor !== owner.value || version !== revision) return
    context.value = response.data.environment
    restoring = true; form.value = emptyForm(); observedAt.value = null; pending.value = null; restoring = false
    saveScopedDraft(actor, scope.value, null); draftNotice.value = ''
    ElMessage.success(response.data.replayed ? '已恢复原观测，未重复登记' : '现场观测已追加，历史保留')
    emit('saved')
    void loadHistory(true)
  } catch (err: any) {
    if (alive && id === props.field.id && actor === owner.value) ElMessage.error(err.response?.data?.detail || '观测提交未确认；保留原编号，可重试核对')
  } finally { if (alive) saving.value = false }
}
watch(() => [props.field.id, owner.value], () => {
  revision++; visible.value = false; context.value = null; loading.value = false
  historyRevision++; historyRows.value = []; historyBusy.value = false; historyLoaded.value = false; historyError.value = ''
  restoring = true; form.value = emptyForm(); observedAt.value = null; pending.value = null; restoring = false
})
watch(() => props.field.facility_type, () => { if (visible.value) void refresh() })
onBeforeUnmount(() => { alive = false; revision++; historyRevision++ })
</script>

<template>
  <el-button link type="primary" :icon="Odometer" :disabled="saving" @click="open">现场环境 / 观测记录</el-button>
  <el-dialog v-model="visible" :title="`${field.name} · 现场环境`" width="min(94vw, 580px)" append-to-body :close-on-click-modal="false">
    <div v-loading="loading" class="env-dialog">
      <el-alert v-if="error" :title="error" type="error" :closable="false" class="dlg-alert" />
      <template v-if="context">
        <div class="dlg-card">
          <p class="dlg-lead">{{ context.facility_label }} · {{ context.notice }}</p>
          <div v-if="context.latest_observation" class="dlg-grid">
            <p>最近人工观测：{{ time(context.latest_observation.observed_at) }}（北京时间） · {{ context.latest_observation.measurement_scope === 'inside_facility' ? '设施内' : '田块室外' }}</p>
            <p>温度 {{ context.latest_observation.temperature ?? '未知' }} ℃ · 湿度 {{ context.latest_observation.humidity ?? '未知' }} %</p>
            <p>通风 {{ states[context.latest_observation.ventilation] }} · 覆盖 {{ states[context.latest_observation.covering] }}</p>
            <p v-if="context.latest_observation.notes">{{ context.latest_observation.notes }}</p>
            <p class="dlg-meta">登记时间 {{ time(context.latest_observation.recorded_at) }} · 人工录入，非连续传感器监测</p>
          </div>
        </div>
      </template>
      <div class="dlg-section">
        <div class="dlg-bar">
          <el-button :loading="loading || historyBusy" :disabled="saving" @click="refreshAll">刷新观测与历史</el-button>
        </div>
        <el-collapse>
          <el-collapse-item title="全部人工观测（按登记顺序，可继续翻页）" name="history">
            <el-alert v-if="historyError" :title="historyError" type="error" :closable="false" class="dlg-alert" />
            <el-table :data="historyRows" max-height="360" v-loading="historyBusy" empty-text="暂无已加载观测">
              <el-table-column label="时间与位置" min-width="180"><template #default="{ row }">#{{ row.id }} · {{ time(row.observed_at) }}<br />{{ row.measurement_scope === 'inside_facility' ? '设施内' : '田块室外' }} · {{ facilityNames[row.facility_type] }}<br />登记 {{ time(row.recorded_at) }}</template></el-table-column>
              <el-table-column label="原始观测" min-width="160"><template #default="{ row }">{{ row.temperature ?? '未知' }}℃ / {{ row.humidity ?? '未知' }}%<br />通风 {{ states[row.ventilation] }} · 覆盖 {{ states[row.covering] }}<br />{{ row.notes || '未填写说明' }}</template></el-table-column>
              <el-table-column label="当前参考状态" min-width="130"><template #default="{ row }">{{ observationStates[row.observation_status] }}</template></el-table-column>
            </el-table>
            <div class="dlg-bar dlg-bar--top">
              <el-button v-if="historyNext !== null" :loading="historyBusy" @click="loadHistory()">加载更早观测</el-button>
              <el-button v-else-if="historyError" :loading="historyBusy" @click="loadHistory(true)">重试读取历史</el-button>
              <p v-else-if="historyLoaded" class="dlg-tip">已显示本次快照内全部 {{ historyRows.length }} 条；刷新后查看新登记。历史记录不覆盖，状态提示按当前档案和读取时间计算。</p>
            </div>
          </el-collapse-item>
        </el-collapse>
      </div>
      <template v-if="!readonly">
        <div class="dlg-card">
          <p class="dlg-lead">追加真实观测</p>
          <p class="dlg-tip">留空表示未知，错误记录请另记说明，不覆盖历史。</p>
          <div class="dlg-btn-row">
            <el-button :disabled="saving" @click="restoreDraft">恢复本机草稿</el-button>
            <el-button :disabled="saving" @click="discard">核对后另建观测</el-button>
          </div>
          <el-alert v-if="pending" title="保留原提交编号及内容；重试将核对原记录。如需修改，请先核对历史再另建。" type="info" :closable="false" class="dlg-alert" />
          <el-form :model="form" label-width="100px" :disabled="saving || !!pending">
            <el-form-item label="观测时间" required><el-date-picker v-model="observedAt" type="datetime" placeholder="选择实际观测时间" /></el-form-item>
            <el-form-item label="观测位置" required><el-select v-model="form.measurement_scope" placeholder="选择实际观测位置"><el-option label="设施内部" value="inside_facility" /><el-option label="田块室外" value="onsite_outdoor" /></el-select></el-form-item>
            <el-form-item label="实测温度℃"><el-input-number v-model="form.temperature" :min="-80" :max="80" :precision="1" controls-position="right" /></el-form-item>
            <el-form-item label="实测湿度%"><el-input-number v-model="form.humidity" :min="0" :max="100" :precision="1" controls-position="right" /></el-form-item>
            <el-form-item label="通风状态"><el-select v-model="form.ventilation"><el-option v-for="code in ['unknown', 'open', 'closed', 'partial', 'not_applicable']" :key="code" :value="code" :label="states[code]" /></el-select></el-form-item>
            <el-form-item label="覆盖状态"><el-select v-model="form.covering"><el-option v-for="code in ['unknown', 'covered', 'uncovered', 'partial', 'not_applicable']" :key="code" :value="code" :label="states[code]" /></el-select></el-form-item>
            <el-form-item label="观测说明"><el-input v-model="form.notes" type="textarea" :maxlength="1000" placeholder="测量位置、仪器或误录更正说明" /></el-form-item>
          </el-form>
          <p v-if="draftNotice" class="dlg-tip">{{ draftNotice }}</p>
          <div class="dlg-bar dlg-bar--top">
            <el-button type="primary" :loading="saving" :disabled="loading" @click="submit">{{ pending ? '按原编号重试核对' : '追加现场观测' }}</el-button>
          </div>
        </div>
      </template>
    </div>
  </el-dialog>
</template>

<style scoped>
/* 弹窗内容分组：卡片化区块 + 统一间距节奏 */
.dlg-alert {
  margin-bottom: var(--app-space-3);
}

.dlg-card {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-lg);
  padding: var(--app-space-3) var(--app-space-4);
  margin-bottom: var(--app-space-4);
}

.dlg-card p {
  margin: 0 0 6px;
  line-height: 1.6;
}

.dlg-card p:last-child {
  margin-bottom: 0;
}

.dlg-lead {
  font-weight: 600;
  color: var(--app-text);
}

.dlg-meta {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.dlg-section {
  margin-bottom: var(--app-space-4);
}

.dlg-bar {
  margin-bottom: var(--app-space-2);
}

.dlg-bar--top {
  margin-top: var(--app-space-3);
  margin-bottom: 0;
}

.dlg-btn-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--app-space-2);
  margin-bottom: var(--app-space-3);
}

.dlg-btn-row .el-button {
  margin-left: 0;
}

.dlg-tip {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  line-height: 1.6;
  margin: 0 0 var(--app-space-2);
}
</style>
