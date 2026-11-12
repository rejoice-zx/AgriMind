<script setup lang="ts">
/**
 * 数据来源徽标：在同一界面上区分室外预报、现场观测、档案记录、模型生成、专家签署与知识库候选。
 * 用法：<SourceBadge kind="forecast" detail="Open-Meteo · 10:20" />
 */
import { computed } from 'vue'
import { SOURCE_LABELS, type SourceKind } from '@/constants/agri'

const props = defineProps<{ kind: SourceKind; detail?: string; label?: string }>()
const text = computed(() => props.label || SOURCE_LABELS[props.kind])
</script>

<template>
  <span class="source-badge" :class="`source-badge--${kind}`">
    <span class="source-badge__label">{{ text }}</span>
    <span v-if="detail" class="source-badge__detail">· {{ detail }}</span>
  </span>
</template>

<style scoped>
.source-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-1);
  max-width: 100%;
  padding: 1px var(--app-space-2);
  border: 1px solid var(--source-record-border);
  border-radius: var(--app-radius-round);
  background: var(--source-record-bg);
  color: var(--source-record-fg);
  font-size: var(--app-text-xs);
  line-height: 18px;
  white-space: nowrap;
}

.source-badge__label {
  font-weight: 500;
}

.source-badge__detail {
  overflow: hidden;
  text-overflow: ellipsis;
  opacity: 0.85;
}

.source-badge--forecast { color: var(--source-forecast-fg); background: var(--source-forecast-bg); border-color: var(--source-forecast-border); }
.source-badge--observation { color: var(--source-observation-fg); background: var(--source-observation-bg); border-color: var(--source-observation-border); }
.source-badge--ai { color: var(--source-ai-fg); background: var(--source-ai-bg); border-color: var(--source-ai-border); }
.source-badge--expert { color: var(--source-expert-fg); background: var(--source-expert-bg); border-color: var(--source-expert-border); }
.source-badge--knowledge { color: var(--source-knowledge-fg); background: var(--source-knowledge-bg); border-color: var(--source-knowledge-border); }
</style>
