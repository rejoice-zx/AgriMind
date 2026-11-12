<script setup lang="ts">
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { getFirstAccessibleRoute } from '@/router'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const home = computed(() => getFirstAccessibleRoute(auth.userRole) || '/login')
</script>

<template>
  <div class="app-page not-found">
    <el-result icon="warning" title="页面不存在" :sub-title="`没有找到 ${route.fullPath} 对应的页面，可能是地址输入有误或功能已调整。`">
      <template #extra>
        <el-button type="primary" @click="router.push(home)">返回首页</el-button>
        <el-button @click="router.back()">返回上一页</el-button>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.not-found {
  display: flex;
  justify-content: center;
  padding-top: 8vh;
  overflow-wrap: anywhere;
}
</style>
