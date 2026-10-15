<script setup lang="ts">
import { useAuthStore } from '@/stores/auth'
const auth = useAuthStore()
</script>
<template>
  <el-result icon="info" title="旧电商功能已下线" sub-title="旧 AI 工作台、人工客服、电商聊天和商品管理已退出当前农业系统。">
    <template #extra>
      <router-link :to="auth.userRole === 'expert' ? '/diagnosis' : '/agri-chat'"><el-button type="primary">进入农业服务</el-button></router-link>
    </template>
  </el-result>
</template>
