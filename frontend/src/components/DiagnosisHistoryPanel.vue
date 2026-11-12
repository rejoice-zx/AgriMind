<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getDiagnosisRecords, getDiagnosisRecord, requestDiagnosisReview, deleteDiagnosisRecord } from '@/api/agriculture'
import { useAuthStore } from '@/stores/auth'
import { ticketStatus } from '@/constants/agri'
import type { ExpertTicketItem, ExpertTicketDetail } from '@/types/agriculture'
const props = defineProps<{ fieldId?: number; version: number }>()
const emit = defineEmits<{ deleted: [recordId: number] }>()
const auth = useAuthStore()
const canDelete = computed(() => auth.user?.role === 'admin')
const deletingId = ref<number | null>(null)
const records = ref<ExpertTicketItem[]>([])
const detail = ref<ExpertTicketDetail | null>(null)
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const detailLoading = ref(false)
const transferring = ref(false)
let alive = true
let listVersion = 0
let detailVersion = 0
// 状态文案与初诊页共用一份映射（constants/agri.ts），避免两处说法不一致
function label(status: string) {
  return ticketStatus(status).label
}
async function load() {
  const version = ++listVersion
  loading.value = true
  try {
    const res = await getDiagnosisRecords({ field_id: props.fieldId, offset: (page.value - 1) * 20, limit: 20 })
    if (!alive || version !== listVersion) return
    records.value = res.data.items
    total.value = res.data.total
    const lastPage = Math.max(1, Math.ceil(total.value / 20))
    if (page.value > lastPage) {
      page.value = lastPage
      await load()
    }
  } catch (err: any) {
    if (alive && version === listVersion) { records.value = []; total.value = 0; ElMessage.error('病害记录加载失败：' + (err.response?.data?.detail || err.message)) }
  } finally { if (alive && version === listVersion) loading.value = false }
}
async function select(id: number) {
  if (transferring.value || deletingId.value !== null) return
  const version = ++detailVersion
  detail.value = null
  detailLoading.value = true
  try {
    const res = await getDiagnosisRecord(id)
    if (alive && version === detailVersion) detail.value = res.data
  } catch (err: any) {
    if (alive && version === detailVersion) ElMessage.error('病害详情加载失败：' + (err.response?.data?.detail || err.message))
  } finally { if (alive && version === detailVersion) detailLoading.value = false }
}
async function transfer() {
  if (!detail.value || detail.value.status !== 'preliminary' || transferring.value || deletingId.value !== null) return
  const id = detail.value.ticket_id
  const context = detailVersion
  transferring.value = true
  try {
    await requestDiagnosisReview(id)
    if (!alive) return
    ElMessage.success('已提交专家复核申请')
    transferring.value = false
    if (context === detailVersion) await select(id)
    await load()
  } catch (err: any) {
    if (alive) ElMessage.error('提交复核申请失败：' + (err.response?.data?.detail || err.message))
  } finally { transferring.value = false }
}
async function removeRecord(record: ExpertTicketItem) {
  if (!canDelete.value || transferring.value || deletingId.value !== null) return
  const id = record.ticket_id
  deletingId.value = id
  try {
    await ElMessageBox.confirm(
      `确定删除记录 #${id}「${record.initial_suspected_disease}」吗？关联的专家反馈和会诊工单将一并删除，无法恢复。已独立纳入知识库的条目保留。`,
      '删除病害记录', { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
    if (!alive || !canDelete.value) return
    ++listVersion; ++detailVersion
    loading.value = false; detailLoading.value = false
    await deleteDiagnosisRecord(id)
    if (!alive) return
    records.value = records.value.filter(row => row.ticket_id !== id)
    if (detail.value?.ticket_id === id) detail.value = null
    ElMessage.success('病害记录及关联专家反馈已删除')
    emit('deleted', id)
    await load()
  } catch (err: any) {
    if (alive && err !== 'cancel' && err !== 'close') {
      ElMessage.error('删除病害记录失败：' + (err.response?.data?.detail || err.message))
    }
  } finally { deletingId.value = null }
}
watch(() => [props.fieldId, props.version], () => {
  page.value = 1
  records.value = []
  total.value = 0
  detail.value = null
  ++detailVersion
  detailLoading.value = false
  load()
}, { immediate: true })
onBeforeUnmount(() => { alive = false; ++listVersion; ++detailVersion })
</script>

<template>
  <el-card class="history-panel">
    <template #header>病害记录与专家反馈 <el-button :disabled="transferring || deletingId !== null" :loading="loading" @click="load">刷新记录</el-button></template>
    <p>显示有权访问的已保存记录；未关联田块的单次诊断不会出现在此处。点击记录可查看最新复核结果。</p>
    <el-table :data="records" v-loading="loading" @row-click="(row: ExpertTicketItem) => select(row.ticket_id)">
      <el-table-column prop="ticket_id" label="编号" width="80" />
      <el-table-column prop="field_name" label="田块" min-width="100" />
      <el-table-column prop="initial_suspected_disease" label="初步记录名称" min-width="140" />
      <el-table-column label="状态" min-width="120"><template #default="{ row }"><el-tag size="small" :type="ticketStatus(row.status).tag">{{ label(row.status) }}</el-tag></template></el-table-column>
      <el-table-column prop="expert_name" label="认领/复核人" min-width="100" />
      <el-table-column v-if="canDelete" label="操作" width="100">
        <template #default="{ row }">
          <el-button type="danger" link :loading="deletingId === row.ticket_id" :disabled="transferring || deletingId !== null" @click.stop="removeRecord(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination v-if="total > 20" v-model:current-page="page" :disabled="transferring || deletingId !== null" :total="total" :page-size="20" layout="prev, pager, next" @current-change="load" />
    <div v-loading="detailLoading">
      <div v-if="detail" class="history-detail">
        <p class="hd-title">记录 #{{ detail.ticket_id }} · {{ label(detail.status) }} · 来源：{{ detail.diagnosis_method === 'manual' ? '人工巡查' : detail.diagnosis_method === 'ai_vision' ? '模型初诊' : detail.diagnosis_method }}</p>
        <p>初步记录：{{ detail.initial_suspected_disease }}；{{ detail.symptoms || '未填写症状' }}</p>
        <el-button v-if="detail.status === 'preliminary' && !detail.feedback" type="primary" :loading="transferring" :disabled="deletingId !== null" @click="transfer">申请专家复核</el-button>
        <div v-if="detail.feedback">
          <p>{{ detail.feedback.workflow_verified ? '本流程已归档的专家复核' : '历史结论：缺少本流程认领凭据，来源待核验' }}</p>
          <p>复核人：{{ detail.feedback.expert_name || '历史未记录' }}；时间：{{ detail.feedback.resolved_at || '历史未记录' }}</p>
          <p>复核结论：{{ detail.feedback.confirmed_disease || '未记录名称' }}</p>
          <p style="white-space: pre-wrap">{{ detail.feedback.treatment_suggestion }}</p>
          <p style="white-space: pre-wrap">{{ detail.feedback.notes }}</p>
        </div>
        <p v-else class="hd-empty">尚无专家反馈，请刷新查看处理进度。</p>
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.history-panel {
  margin-top: var(--app-space-5);
  /* 防内部表格/长内容撑破父级（配合外层 minmax(0,1fr)） */
  min-width: 0;
}

/* 展开详情：卡片化 + 防长串溢出 + 段落节奏 */
.history-detail {
  margin-top: var(--app-space-4);
  padding: var(--app-space-4) var(--app-space-5);
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-lg);
  overflow-wrap: break-word;
}

.history-detail p {
  margin: 0 0 var(--app-space-2);
  line-height: 1.7;
}

.history-detail p:last-child {
  margin-bottom: 0;
}

.history-detail .el-button {
  margin: var(--app-space-2) 0;
}

.hd-title {
  font-weight: 600;
  color: var(--app-text);
  padding-bottom: var(--app-space-2);
  margin-bottom: var(--app-space-3) !important;
  border-bottom: 1px solid var(--app-border-light);
}

.hd-empty {
  color: var(--app-text-secondary);
}
</style>
