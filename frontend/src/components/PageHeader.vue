<script setup lang="ts">
/**
 * 统一页面头部（art-design-pro 风格）
 * 用法：<PageHeader title="用户管理" description="管理系统用户与角色">
 *         <template #actions><el-button /></template>
 *       </PageHeader>
 */
defineProps<{
  title: string
  description?: string
}>()
</script>

<template>
  <div class="page-header">
    <div class="page-header-main">
      <h2 class="page-header-title">
        {{ title }}
      </h2>
      <p
        v-if="description"
        class="page-header-desc"
      >
        {{ description }}
      </p>
    </div>
    <div
      v-if="$slots.actions"
      class="page-header-actions"
    >
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--app-space-4);
  margin-bottom: var(--app-space-5);
}

.page-header-title {
  margin: 0;
  font-size: var(--app-text-2xl);
  font-weight: 600;
  color: var(--app-text);
  line-height: 1.3;
}

.page-header-desc {
  margin: var(--app-space-1) 0 0;
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
}

.page-header-actions {
  display: flex;
  align-items: center;
  gap: var(--app-space-3);
  flex-shrink: 0;
}

@media (max-width: 767px) {
  .page-header {
    flex-direction: column;
    align-items: stretch;
  }

  .page-header-actions {
    flex-wrap: wrap;
    gap: var(--app-space-2);
  }
}
</style>
