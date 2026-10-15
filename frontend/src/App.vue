<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import { MainLayout } from '@/components/layout'
import { useSettingsStore } from '@/stores/settings'

const route = useRoute()
const settingsStore = useSettingsStore()

// Check if current route is public (like login)
const isPublicRoute = computed(() => route.meta.public === true)

// Initialize theme on mount
onMounted(() => {
  const theme = settingsStore.theme
  document.documentElement.setAttribute('data-theme', theme)
  if (theme === 'dark') {
    document.documentElement.classList.add('dark')
  }
})
</script>

<template>
  <!-- Public routes (login) render without layout -->
  <RouterView v-if="isPublicRoute" />
  
  <!-- Protected routes render with main layout -->
  <MainLayout v-else>
    <RouterView />
  </MainLayout>
</template>

<style>
/* 全局设计令牌、基础样式与 Element Plus 覆写已收口至 src/styles/tokens.css */
</style>
