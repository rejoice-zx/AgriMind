<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { Search, Filter, Refresh } from '@element-plus/icons-vue'
import { getAgriConversations } from '@/api/agriHistory'
import type { AgriConversation } from '@/types/agriHistory'
const props = defineProps<{ disabled?: boolean }>()
const emit = defineEmits<{ select: [id: string]; create: [] }>()

// 紧凑时间格式：当年省略年份（如 10-11 09:23）
function formatTime(iso: string) {
  const d = new Date(iso)
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return d.getFullYear() === now.getFullYear() ? `${md} ${hm}` : `${d.getFullYear()}-${md}`
}
const query = ref('')
const fieldId = ref<number>()
const dates = ref<string[]>([])
const items = ref<AgriConversation[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
// 高级筛选默认折叠，把空间留给会话列表
const filtersOpen = ref(false)
let revision = 0
async function reload(reset = false) {
  if (reset) page.value = 1
  const current = ++revision
  loading.value = true
  try {
    const response = await getAgriConversations({ q: query.value || undefined, field_id: fieldId.value,
      date_from: dates.value?.[0], date_to: dates.value?.[1], offset: (page.value - 1) * 10, limit: 10 })
    if (current !== revision) return
    items.value = response.data.items
    total.value = response.data.total
  } catch {
    if (current === revision) ElMessage.error('读取对话历史失败')
  } finally { if (current === revision) loading.value = false }
}
defineExpose({ reload })
onMounted(() => reload())
onBeforeUnmount(() => { ++revision })
</script>
<template>
  <div class="history-panel">
    <div class="history-actions">
      <strong>农业对话档案</strong>
      <div class="history-actions-btns">
        <el-tooltip content="筛选条件" placement="top">
          <el-button text size="small" :icon="Filter" class="icon-btn" :class="{ 'filter-active': filtersOpen }" @click="filtersOpen = !filtersOpen" />
        </el-tooltip>
        <el-tooltip content="刷新列表" placement="top">
          <el-button text size="small" :icon="Refresh" class="icon-btn" :loading="loading" @click="reload()" />
        </el-tooltip>
        <el-button size="small" type="primary" plain :disabled="props.disabled" @click="emit('create')">新建</el-button>
      </div>
    </div>

    <el-input v-model="query" placeholder="搜索标题或提问" clearable maxlength="200" :prefix-icon="Search" @keyup.enter="reload(true)" @clear="reload(true)" />

    <!-- 高级筛选：默认折叠 -->
    <el-collapse-transition>
      <div v-show="filtersOpen" class="history-filters">
        <el-input-number v-model="fieldId" :min="1" :controls="false" placeholder="田块编号（可选）" style="width:100%" />
        <el-date-picker v-model="dates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="起始日期" end-placeholder="结束日期" style="width:100%" />
        <small>日期按北京时间的最后更新时间筛选</small>
        <el-button size="small" style="width:100%" :loading="loading" @click="reload(true)">查询 / 刷新</el-button>
      </div>
    </el-collapse-transition>

    <!-- 会话列表：填充剩余空间并内部滚动 -->
    <div v-loading="loading" class="history-list">
      <div
        v-for="item in items"
        :key="item.id"
        class="history-item"
        :class="{ 'is-disabled': props.disabled }"
        @click="!props.disabled && emit('select', item.id)"
      >
        <div class="history-item-title">{{ item.title }}</div>
        <div class="history-item-meta">{{ item.field_ref == null ? '未绑定田块' : `田块 #${item.field_ref}` }} · {{ formatTime(item.updated_at) }}</div>
      </div>
      <p v-if="!loading && !items.length" class="history-empty">暂无匹配记录</p>
    </div>

    <el-pagination v-model:current-page="page" class="history-pager" small layout="prev, pager, next" :page-size="10" :total="total" @current-change="reload()" />
  </div>
</template>
<style scoped>
.history-panel { display:flex; flex-direction:column; gap:var(--app-space-2); margin-top:var(--app-space-4); flex:1; min-height:0; }
.history-actions { display:flex; align-items:center; justify-content:space-between; }
.history-actions-btns { display:flex; align-items:center; gap:2px; }
.icon-btn { padding:4px; color:var(--app-text-secondary); }
.icon-btn:hover, .icon-btn.filter-active { color:var(--el-color-primary); }
.history-filters { display:flex; flex-direction:column; gap:var(--app-space-2); padding:var(--app-space-2); background:var(--app-surface-2); border-radius:var(--app-radius-md); }
.history-list { flex:1; min-height:140px; overflow-y:auto; display:flex; flex-direction:column; gap:2px; }
.history-item { padding:var(--app-space-2) var(--app-space-3); border-radius:var(--app-radius-md); cursor:pointer; transition:var(--app-transition); border:1px solid transparent; }
.history-item:hover { background:var(--app-hover); border-color:var(--app-border-light); }
.history-item.is-disabled { cursor:not-allowed; opacity:0.6; }
.history-item-title { font-size:var(--app-text-sm); color:var(--app-text); line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; word-break:break-word; }
.history-item-meta { margin-top:2px; font-size:var(--app-text-xs); color:var(--app-text-secondary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.history-empty { margin:var(--app-space-4) 0; text-align:center; font-size:var(--app-text-xs); color:var(--app-text-placeholder); }
.history-pager { justify-content:center; margin-top:var(--app-space-1); }
small { display:block; color:var(--app-text-secondary); }
</style>
