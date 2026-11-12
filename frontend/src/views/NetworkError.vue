<script setup lang="ts">
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'

const router = useRouter()
const route = useRoute()
const chunk = computed(() => route.query.reason === 'chunk')
const target = computed(() => typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') ? route.query.redirect : '/')

function retry() {
  // 代码块加载失败时需要整页刷新以获取新版本资源；网络问题则直接回到原页面重试。
  if (chunk.value) window.location.reload()
  else router.replace(target.value)
}
</script>

<template>
  <div class="network-error">
    <el-result icon="error" :title="chunk ? '页面资源加载失败' : '暂时无法连接服务'"
      :sub-title="chunk ? '系统可能刚更新，或网络中断。刷新后会重新下载页面。' : '请检查网络或稍后重试。登录状态已保留，恢复后可继续操作。'">
      <template #extra>
        <el-button type="primary" @click="retry">{{ chunk ? '刷新页面' : '重试' }}</el-button>
        <el-button @click="router.replace('/login')">返回登录页</el-button>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.network-error {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  padding: var(--app-space-4);
  background: var(--app-bg);
}
</style>
