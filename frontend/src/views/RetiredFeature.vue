<script setup lang="ts">
import { computed } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { getFirstAccessibleRoute } from '@/router'

const auth = useAuthStore()
const home = computed(() => getFirstAccessibleRoute(auth.userRole) || '/dashboard')
</script>

<template>
  <div class="app-page retired">
    <el-result icon="info" title="该功能已停用" sub-title="旧版客服对话、工作台和商品管理已不再提供；农场档案、农事履历、资料分析与专家会诊不受影响。">
      <template #extra>
        <router-link :to="home"><el-button type="primary">返回首页</el-button></router-link>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.retired {
  display: flex;
  justify-content: center;
  padding-top: 8vh;
}
</style>
