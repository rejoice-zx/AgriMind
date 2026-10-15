<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { getAgriConsultation, getConsultationContext, resolveAgriConsultation } from '@/api/agriHistory'
import { draftOwnerKey, readScopedDraft, saveScopedDraft } from '@/utils/scopedDraft'
import { validationMessage } from '@/utils/validationMessage'
import type { AgriConsultation, ConsultationContext, ConsultationSubmission, LabelReviewEvidence, OperationReview, ReviewChecks } from '@/types/agriHistory'

const props = defineProps<{ ticket: AgriConsultation }>()
const emit = defineEmits<{ saved: [value: AgriConsultation]; busy: [value: boolean] }>()
const auth = useAuthStore()
const owner = computed(() => draftOwnerKey(auth.user))
const emptyChecks = (): ReviewChecks => ({ registration_checked: false, crop_and_use_checked: false, records_complete: false, phi_checked: false, site_and_weather_checked: false })
const blank = () => ({ answer: '', mode: 'general' as 'general' | 'spray' | 'harvest', outcome: 'needs_information' as OperationReview['outcome'],
  crop: null as number | null, operationDate: '', harvestDate: '', expiry: '', evidence: '', notes: '', checks: emptyChecks(),
  products: [] as (Omit<LabelReviewEvidence, 'phi_days'> & { phi_days: number | null })[] })
type Draft = { form: ReturnType<typeof blank>; pending: ConsultationSubmission | null }
const form = ref(blank())
const context = ref<ConsultationContext | null>(null)
const pending = ref<ConsultationSubmission | null>(null)
const availableDraft = ref<Draft | null>(null)
const busy = ref(false)
const loading = ref(false)
const storageFailed = ref(false)
const writable = computed(() => props.ticket.status === 'in_review' && props.ticket.assigned_to_current_user === true)
const reviewStatus = (value: string) => ({ no_signed_operation_review: '尚无作业签署回执', historical_non_approval: '原结论未通过',
  expired: '原结论已过期，需重新复核', context_changed: '资料已变化，需重新复核', current: '原资料一致且未过期，仍须现场确认' }[value] || '请核对原回执')
const scope = () => `consultation:${props.ticket.id}`
let generation = 0, alive = true, changing = false
watch(busy, value => emit('busy', value))
watch(() => [props.ticket.id, owner.value], () => {
  changing = true
  ++generation; form.value = blank(); pending.value = null; context.value = null
  availableDraft.value = readScopedDraft<Draft>(owner.value, scope())
  loading.value = false; changing = false
}, { immediate: true, flush: 'sync' })
function persist() {
  storageFailed.value = !saveScopedDraft(owner.value, scope(), { form: form.value, pending: pending.value })
}
watch([form, pending], () => { if (!changing) { availableDraft.value = null; persist() } }, { deep: true, flush: 'sync' })
function restoreDraft() {
  const draft = availableDraft.value
  if (!draft || !writable.value) return
  changing = true
  // A draft never restores checked facts as fresh. A pending request retains its exact body for receipt recovery.
  form.value = { ...blank(), ...draft.form, checks: emptyChecks(), outcome: 'needs_information' }
  pending.value = draft.pending || null; availableDraft.value = null; context.value = null
  changing = false; persist()
}
async function readContext() {
  const id = props.ticket.id, account = owner.value, current = ++generation
  loading.value = true; context.value = null
  form.value.checks = emptyChecks(); form.value.outcome = 'needs_information'
  try {
    const response = await getConsultationContext(id, form.value.crop ?? undefined)
    if (alive && current === generation && account === owner.value && id === props.ticket.id) {
      context.value = response.data
      if (response.data.snapshot.crop) form.value.crop = response.data.snapshot.crop.id
    }
  } catch (error: any) { if (current === generation) ElMessage.error(validationMessage(error.response?.data?.detail || '读取复核资料失败')) }
  finally { if (current === generation) loading.value = false }
}
function changeMode() { context.value = null; form.value.checks = emptyChecks(); form.value.outcome = 'needs_information'; form.value.products = [] }
function addProduct(recordId?: number, name = '') {
  form.value.products.push({ record_id: recordId ?? null, product_name: name, registration_number: '', applicable_crop: '', label_reference: '', usage_scope: '', phi_days: null })
}
function referenceRecords() {
  if (!context.value) return
  form.value.products = []
  for (const record of context.value.snapshot.records) {
    if (context.value.spraying_record_ids.includes(record.id)) addProduct(record.id, record.input_name || '')
  }
}
function buildSubmission(): ConsultationSubmission {
  if (!form.value.answer.trim()) throw Error('请填写答复')
  let review: OperationReview | null = null
  if (form.value.mode !== 'general') {
    if (!context.value) throw Error('请先读取并核对当前复核资料')
    const approved = form.value.outcome === 'approved'
    if (approved && (!form.value.crop || !Object.values(form.value.checks).every(Boolean) || !form.value.evidence.trim() || !form.value.notes.trim())) throw Error('资料不足，不能签署通过；请逐项核对并填写依据和理由')
    if (approved && (!form.value.expiry || !form.value.operationDate || form.value.products.some(item => !Number.isInteger(item.phi_days)))) throw Error('请明确作业日期、截止时间及逐个产品标签PHI；未知不能填0')
    review = { purpose: form.value.mode, outcome: form.value.outcome, crop_planting_id: form.value.crop,
      context_fingerprint: context.value.context_fingerprint, planned_operation_date: form.value.operationDate || null,
      planned_harvest_date: form.value.harvestDate || null, valid_until: form.value.expiry ? new Date(form.value.expiry).toISOString() : null,
      evidence_references: form.value.evidence.trim(), review_notes: form.value.notes.trim(), checks: { ...form.value.checks },
      products: approved ? JSON.parse(JSON.stringify(form.value.products)) : [] }
  }
  return { answer: form.value.answer.trim(), request_id: crypto.randomUUID(), operation_review: review }
}
function receiptSaved(ticket: AgriConsultation) {
  // Only a receipt with the original number proves that our ambiguous submission completed.
  if (pending.value && ticket.request_id !== pending.value.request_id) throw Error('该咨询已有其他签署回执，请核对签署人、编号与内容')
  changing = true
  pending.value = null; availableDraft.value = null
  form.value = blank()
  emit('saved', ticket)
  saveScopedDraft(owner.value, scope(), null)
  changing = false
}
async function submit() {
  if (!writable.value || busy.value || pending.value) return
  try { pending.value = buildSubmission() } catch (error: any) { ElMessage.warning(error.message); return }
  persist()
  await retryOriginal()
}
async function retryOriginal() {
  if (!pending.value || busy.value) return
  const id = props.ticket.id, account = owner.value, body = JSON.parse(JSON.stringify(pending.value)) as ConsultationSubmission
  let attemptedSignature = false
  busy.value = true
  try {
    // Reconnect always queries the original consultation before any exact-body retry.
    const existing = (await getAgriConsultation(id)).data
    if (!alive || account !== owner.value || id !== props.ticket.id) return
    if (existing.status === 'resolved') { receiptSaved(existing); return }
    if (!existing.assigned_to_current_user) throw Error('接单状态已变化，请核对原咨询')
    attemptedSignature = true
    const result = await resolveAgriConsultation(id, body)
    if (!alive || account !== owner.value || id !== props.ticket.id) return
    receiptSaved(result.data); ElMessage.success('签署回执已保存，原对话可查看')
  } catch (error: any) {
    const sameContext = alive && account === owner.value && id === props.ticket.id
    // A read/auth failure cannot establish that the original signature did not commit.
    if (sameContext && attemptedSignature && [400, 422].includes(error.response?.status)) pending.value = null
    if (sameContext && attemptedSignature && error.response?.status === 409) {
      try {
        const latest = (await getAgriConsultation(id)).data
        if (!alive || account !== owner.value || id !== props.ticket.id) return
        if (latest.status === 'resolved') {
          if (latest.request_id === body.request_id) {
            receiptSaved(latest); ElMessage.success('已恢复原签署回执'); return
          }
          pending.value = null
          emit('saved', latest)
          ElMessage.warning('该咨询已有其他签署回执，请查看现有结论；本次未覆盖'); return
        }
        if (latest.status === 'in_review' && latest.assigned_to_current_user) {
          pending.value = null; context.value = null
          form.value.checks = emptyChecks(); form.value.outcome = 'needs_information'
        }
      } catch { /* Keep the original request until its receipt can be read. */ }
    }
    ElMessage.error(validationMessage(error.response?.data?.detail || error.message || '未取得回执，请查询原编号'))
  } finally { busy.value = false }
}
onBeforeUnmount(() => { alive = false; ++generation; emit('busy', false) })
</script>
<template>
  <div class="review-editor">
    <p v-if="storageFailed" role="alert">浏览器草稿存储不可用，请保留原咨询编号。</p>
    <el-button v-if="availableDraft && writable" @click="restoreDraft">恢复本账号在此咨询的草稿/原提交编号</el-button>
    <template v-if="writable || pending">
      <p v-if="pending">提交回执尚待核对，编号：{{ pending.request_id }}。请查询原回执后重试原内容。</p>
      <el-button v-if="pending" :loading="busy" @click="retryOriginal">查询回执 / 重试原提交</el-button>
      <fieldset :disabled="busy || !!pending || !writable">
        <label>答复 <textarea v-model="form.answer" rows="5" maxlength="6000" placeholder="说明已核实事实、缺少资料与结论" /></label>
        <label>答复类型 <select v-model="form.mode" @change="changeMode"><option value="general">普通咨询答复（无作业批准）</option><option value="spray">施药人工复核</option><option value="harvest">采收人工复核</option></select></label>
        <template v-if="form.mode !== 'general'">
          <el-button :loading="loading" @click="readContext">读取 / 重新核对当前资料</el-button>
          <template v-if="context">
            <p>{{ context.note }} 当前回执状态：{{ reviewStatus(context.current_review_status) }}</p>
            <label>明确在种批次 <select v-model="form.crop" @change="readContext"><option :value="null">请选择批次</option><option v-for="crop in context.available_crops" :key="crop.id" :value="crop.id">{{ crop.crop }} · {{ crop.planting_date }} · #{{ crop.id }}</option></select></label>
            <details><summary>逐项核对田块、全部批次履历及最新环境资料</summary><pre>{{ JSON.stringify(context.snapshot, null, 2) }}</pre></details>
            <label>结论 <select v-model="form.outcome"><option value="needs_information">待补资料</option><option value="rejected">不通过</option><option value="approved">人工核验通过（须满足全部条件）</option></select></label>
            <label>依据原文 / 标签存放位置 <textarea v-model="form.evidence" maxlength="4000" /></label>
            <label>核验理由 / 待补资料 <textarea v-model="form.notes" maxlength="4000" /></label>
            <template v-if="form.outcome === 'approved'">
              <p>来源与现场事实由签署人核实；填表不等于系统已验证标签。新履历或环境变化会使原结论待重新核对。</p>
              <label><input v-model="form.checks.registration_checked" type="checkbox">已核对产品登记及原始标签</label>
              <label><input v-model="form.checks.crop_and_use_checked" type="checkbox">已核对作物、对象、拟用剂量、方法、频次及限制</label>
              <label><input v-model="form.checks.records_complete" type="checkbox">已核实完整履历，包括系统未登记的施药；无记录不等于未施药</label>
              <label><input v-model="form.checks.phi_checked" type="checkbox">已逐项核对PHI及最大安全间隔</label>
              <label><input v-model="form.checks.site_and_weather_checked" type="checkbox">已核对现场、设施、天气与作业条件</label>
              <label>拟作业日期 <input v-model="form.operationDate" type="date"></label>
              <label v-if="form.mode === 'spray'">计划采收日期 <input v-model="form.harvestDate" type="date"></label>
              <label>结论截止时间（本机时区，最多未来24小时）<input v-model="form.expiry" type="datetime-local"></label>
              <el-button v-if="form.mode === 'spray'" @click="addProduct()">添加拟用产品</el-button>
              <el-button v-else @click="referenceRecords">列出有效施药记录（仅带出记录名称，不代填标签）</el-button>
              <div v-for="(product, index) in form.products" :key="index" class="product-evidence">
                <p>产品 {{ index + 1 }} <span v-if="product.record_id">· 原记录 #{{ product.record_id }}</span></p>
                <label>产品全名 <input v-model="product.product_name" maxlength="200"></label>
                <label>登记证号 <input v-model="product.registration_number" maxlength="100"></label>
                <label>标签适用作物 <input v-model="product.applicable_crop" maxlength="50"></label>
                <label>原始标签位置 / 页码 / 核验日期 <textarea v-model="product.label_reference" maxlength="1000" /></label>
                <label>标签用途、剂量、方法、频次和限制与拟作业的对照 <textarea v-model="product.usage_scope" maxlength="2000" /></label>
                <label>明确标签PHI（天；未知留空，0须有原文依据）<input v-model.number="product.phi_days" type="number" min="0" max="3650" step="1"></label>
                <el-button @click="form.products.splice(index, 1)">移除此产品草稿</el-button>
              </div>
            </template>
          </template>
        </template>
        <el-button type="primary" :loading="busy" :disabled="!form.answer.trim()" @click="submit">签署并保存答复</el-button>
      </fieldset>
    </template>
    <template v-if="ticket.status === 'resolved'">
      <p v-if="ticket.workflow_verified">签署人：{{ ticket.reviewer_name }} · {{ ticket.reviewed_at }} · 回执：{{ ticket.request_id || ticket.id }}</p>
      <p v-else>历史答复缺少本流程签署凭据，不能视为正式作业批准。</p>
      <p v-if="ticket.signed_review">{{ ticket.signed_review.purpose === 'spray' ? '施药' : '采收' }}历史结论：{{ { approved: '人工核验通过', rejected: '不通过', needs_information: '待补资料' }[ticket.signed_review.outcome] }}。原结论不自动证明当前可作业。</p>
      <el-button v-if="ticket.signed_review" :loading="loading" @click="readContext">核对原结论的当前资料状态</el-button>
      <p v-if="context">当前状态：{{ reviewStatus(context.current_review_status) }} · {{ context.note }}</p>
      <details v-if="ticket.signed_review"><summary>查看原签署依据及适用范围</summary><pre>{{ JSON.stringify(ticket.signed_review, null, 2) }}</pre></details>
    </template>
  </div>
</template>
<style scoped>
fieldset { border:0; padding:0; min-width:0; display:grid; gap:12px; }
label { display:block; margin:6px 0; }
textarea { display:block; width:100%; box-sizing:border-box; min-height:65px; }
select, input { max-width:100%; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; max-height:360px; overflow:auto; }
.product-evidence { border:1px solid var(--app-border-light); padding:12px; }
</style>
