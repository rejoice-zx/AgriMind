import { ref, onMounted, onBeforeUnmount } from 'vue'

/** 断点与 tokens.css / UI_UX_DESIGN_SYSTEM §9 一致。 */
export const BREAKPOINTS = { sm: 640, md: 768, lg: 1024, xl: 1280 } as const

/**
 * 当前视口是否窄于 md（手机）。用于把弹窗切换为全屏、表单标签放到上方。
 * 只在跨过断点时更新，不会因手机地址栏伸缩而反复抖动。
 */
export function useNarrowScreen(breakpoint: number = BREAKPOINTS.md) {
  const isNarrow = ref(typeof window !== 'undefined' ? window.innerWidth < breakpoint : false)
  let query: MediaQueryList | null = null
  const update = (event: MediaQueryList | MediaQueryListEvent) => { isNarrow.value = event.matches }
  onMounted(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    query = window.matchMedia(`(max-width: ${breakpoint - 1}px)`)
    update(query)
    query.addEventListener('change', update)
  })
  onBeforeUnmount(() => query?.removeEventListener('change', update))
  return isNarrow
}
