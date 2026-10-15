<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { readDiagnosisImage } from '@/api/diagnosisMedia'
defineOptions({ inheritAttrs: false })
const props = defineProps<{ src?: string | null; alt?: string }>()
const auth = useAuthStore()
const objectUrl = ref('')
const loading = ref(false)
let alive = true
let version = 0
function clear() {
  if (objectUrl.value) URL.revokeObjectURL(objectUrl.value)
  objectUrl.value = ''
}
watch(() => [props.src, auth.sessionVersion, auth.user?.id, auth.user?.role], async () => {
  const current = ++version
  clear()
  loading.value = false
  if (!props.src || !auth.user) return
  loading.value = true
  try {
    const result = await readDiagnosisImage(props.src)
    if (!alive || current !== version) return
    if (!result.data.type.startsWith('image/')) throw Error('不是可显示的图片')
    objectUrl.value = URL.createObjectURL(result.data)
  } catch { /* Permission/unavailable state is explicit; the shared client handles session refresh. */ }
  finally { if (alive && current === version) loading.value = false }
}, { immediate: true })
onBeforeUnmount(() => { alive = false; ++version; clear() })
</script>
<template>
  <img v-if="objectUrl" v-bind="$attrs" :src="objectUrl" :alt="alt || '现场图片'" />
  <span v-else v-bind="$attrs" class="image-status">{{ loading ? '图片加载中' : '图片不可用或无访问权限' }}</span>
</template>
<style scoped>
.image-status { display: inline-flex; align-items: center; justify-content: center; font-size: var(--app-text-xs); color: var(--app-text-secondary); }
</style>
