<script setup lang="ts">
import AuthenticatedImage from '@/components/AuthenticatedImage.vue'
import ConsultationReviewEditor from '@/components/ConsultationReviewEditor.vue'
import { renderSafeMarkdown } from '@/utils/safeMarkdown'
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { getAgriConsultations, claimAgriConsultation } from '@/api/agriHistory'
import type { AgriConsultation } from '@/types/agriHistory'
const emit = defineEmits<{ busy: [value: boolean] }>()
const items = ref<AgriConsultation[]>([])
const selected = ref<AgriConsultation | null>(null)
const status = ref('pending')
const page = ref(1)
const total = ref(0)
const loading = ref(false)
const submitting = ref(false)
let revision = 0
let alive = true
watch(submitting, value => emit('busy', value))
const label = (value: string) => ({ pending: '待接单', in_review: '处理中', resolved: '已答复' }[value] || value)
async function load(reset = false) {
  if (reset) page.value = 1
  const current = ++revision
  loading.value = true
  try {
    const response = await getAgriConsultations({ status: status.value || undefined, offset: (page.value - 1) * 20, limit: 20 })
    if (!alive || current !== revision) return
    items.value = response.data.items
    total.value = response.data.total
  } catch { if (current === revision) ElMessage.error('加载农业咨询失败') }
  finally { if (current === revision) loading.value = false }
}
function select(item: AgriConsultation) { if (submitting.value) return; selected.value = item }
async function claim() {
  if (!selected.value || submitting.value) return
  submitting.value = true
  try { const id = selected.value.id; const result = await claimAgriConsultation(id); if (!alive || selected.value?.id !== id) return; selected.value = result.data; await load() }
  catch (error: any) { ElMessage.error(error.response?.data?.detail || '接单失败') }
  finally { submitting.value = false }
}
function saved(ticket: AgriConsultation) {
  if (!alive || selected.value?.id !== ticket.id) return
  selected.value = ticket
  void load()
}
onMounted(() => load())
onBeforeUnmount(() => { alive = false; ++revision; emit('busy', false) })
</script>
<template>
  <div class="consultation-workbench">
    <aside class="consultation-list">
    <div class="queue-filter">
      <el-select v-model="status" :disabled="submitting" @change="load(true)">
        <el-option label="待接单" value="pending" /><el-option label="处理中" value="in_review" />
        <el-option label="已答复" value="resolved" /><el-option label="全部" value="" />
      </el-select>
      <el-button :loading="loading" @click="load()">刷新</el-button>
    </div>
    <el-table :data="items" v-loading="loading" @row-click="select">
      <el-table-column prop="query" label="提问" show-overflow-tooltip />
      <el-table-column label="状态" width="100"><template #default="scope">{{ label(scope.row.status) }}</template></el-table-column>
      <el-table-column prop="assigned_expert_id" label="接单专家编号" width="130" />
    </el-table>
    <el-pagination v-model:current-page="page" layout="prev, pager, next, total" :page-size="20" :total="total" @current-change="load()" />
    </aside>
    <section v-if="selected" class="consultation-detail">
      <h4>{{ selected.query }}</h4>
      <AuthenticatedImage v-if="selected.image_url" :src="selected.image_url" alt="提问者提交的现场图片" style="max-width:400px;max-height:300px" />
      <strong>AI 分析：</strong>
      <div class="markdown-body" v-html="renderSafeMarkdown(selected.decision.conclusion)" />
      <div v-for="risk in selected.decision.risks" :key="risk" class="markdown-body" v-html="renderSafeMarkdown(risk)" />
      <small>此处保留当时的分析；专家应核对当前天气、投入品标签和现场资料。</small>
      <el-collapse>
        <el-collapse-item title="查看原分析依据与工具记录">
          <div v-for="(item, index) in selected.decision.evidences" :key="index"><strong>{{ item.title || item.standard_no || '依据' }}：</strong><div class="markdown-body" v-html="renderSafeMarkdown(item.summary || item.content || item.desc || '')" /></div>
          <div v-for="(trace, index) in selected.decision.tool_traces" :key="index">
            <strong>{{ trace.tool_name }} · {{ trace.status }}</strong>
            <pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{ JSON.stringify(trace.result_summary, null, 2) }}</pre>
          </div>
        </el-collapse-item>
      </el-collapse>
      <p>状态：{{ label(selected.status) }} · 咨询编号：{{ selected.id }}</p>
      <el-button v-if="selected.claim_available" :loading="submitting" @click="claim">接单</el-button>
      <div v-if="selected.status === 'resolved'"><strong>已保存答复：</strong><div class="markdown-body" v-html="renderSafeMarkdown(selected.answer || '')" /></div>
      <ConsultationReviewEditor :key="selected.id" :ticket="selected" @saved="saved" @busy="submitting = $event" />
    </section>
    <el-empty v-else description="请从左侧选择农业咨询" />
  </div>
</template>
<style scoped>
.consultation-workbench { display:grid; grid-template-columns:minmax(260px, 340px) minmax(0, 1fr); gap:var(--app-space-5); }
.consultation-list, .consultation-detail { background:var(--app-surface); border:1px solid var(--app-border-light); border-radius:var(--app-radius-xl); box-shadow:var(--app-shadow-sm); padding:var(--app-space-4); }
@media (max-width:900px) { .consultation-workbench { grid-template-columns:1fr; } }
.queue-filter { display:flex; gap:var(--app-space-3); margin-bottom:var(--app-space-3); }
.consultation-detail { border-top:1px solid var(--app-border-light); padding-top:var(--app-space-3); margin-top:var(--app-space-3); }
</style>
