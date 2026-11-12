<script setup lang="ts">
/**
 * 区块级状态：加载 / 加载失败（可重试）/ 无数据。区块内容只在 state === 'ready' 时渲染。
 * 刷新时调用方应保持 state 为 'ready' 并只显示局部 loading，避免内容整块消失。
 */
defineProps<{
  state: 'loading' | 'error' | 'empty' | 'ready'
  errorText?: string
  emptyText?: string
  minHeight?: string
}>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div v-if="state === 'loading'" class="page-state" :style="{ minHeight }">
    <el-skeleton :rows="3" animated />
  </div>
  <div v-else-if="state === 'error'" class="page-state" :style="{ minHeight }" role="alert">
    <el-result icon="warning" :title="errorText || '加载失败'" sub-title="可能是网络或服务暂不可用">
      <template #extra>
        <el-button type="primary" plain @click="$emit('retry')">重试</el-button>
      </template>
    </el-result>
  </div>
  <div v-else-if="state === 'empty'" class="page-state" :style="{ minHeight }">
    <el-empty :description="emptyText || '暂无数据'" :image-size="72" />
  </div>
  <slot v-else />
</template>

<style scoped>
.page-state {
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: var(--app-space-4);
}

.page-state :deep(.el-result) {
  padding: var(--app-space-4) 0;
}
</style>
