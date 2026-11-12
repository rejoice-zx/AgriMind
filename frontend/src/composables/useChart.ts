/**
 * ECharts 统一封装：按需引入（只注册用到的图表与组件）、跟随亮/暗主题、按容器尺寸重排、卸载时销毁。
 *
 * 用法：
 *   const chart = useChart(containerRef, theme => ({ ...option }))
 *   chart.render()          // 数据变化后重绘
 *   chart.clear()           // 无数据时清空
 */
import { onBeforeUnmount, type Ref } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart, BarChart, PieChart, ScatterChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'

echarts.use([LineChart, BarChart, PieChart, ScatterChart, GridComponent, TooltipComponent,
  LegendComponent, DataZoomComponent, MarkLineComponent, CanvasRenderer])

export interface ChartTheme {
  dark: boolean
  text: string
  textSecondary: string
  axisLine: string
  splitLine: string
  tooltipBg: string
  tooltipBorder: string
  /** 分类色：同一色相族 + 中性色，避免红绿灯式误读。 */
  palette: string[]
}

export function chartTheme(): ChartTheme {
  const dark = typeof document !== 'undefined' && document.documentElement.classList.contains('dark')
  return dark
    ? { dark, text: '#e5eaf3', textSecondary: '#a3a6ad', axisLine: '#4c4d4f', splitLine: '#2d2d2d',
        tooltipBg: '#262727', tooltipBorder: '#4c4d4f', palette: ['#34d399', '#60a5fa', '#fbbf24', '#a78bfa', '#94a3b8', '#f472b6'] }
    : { dark, text: '#0f172a', textSecondary: '#64748b', axisLine: '#cbd5e1', splitLine: '#f1f5f9',
        tooltipBg: '#ffffff', tooltipBorder: '#e2e8f0', palette: ['#059669', '#2563eb', '#d97706', '#7c3aed', '#64748b', '#db2777'] }
}

/** 坐标轴通用样式（带轴名与单位）。 */
export function axisStyle(theme: ChartTheme, name?: string) {
  return {
    name, nameTextStyle: { color: theme.textSecondary, fontSize: 12 },
    axisLabel: { color: theme.textSecondary, fontSize: 12 },
    axisLine: { lineStyle: { color: theme.axisLine } },
    splitLine: { lineStyle: { color: theme.splitLine } },
  }
}

export function baseOption(theme: ChartTheme): EChartsCoreOption {
  return {
    color: theme.palette,
    textStyle: { color: theme.text, fontFamily: 'inherit' },
    legend: { textStyle: { color: theme.textSecondary }, type: 'scroll', bottom: 0 },
    tooltip: { backgroundColor: theme.tooltipBg, borderColor: theme.tooltipBorder,
      textStyle: { color: theme.text, fontSize: 12 }, renderMode: 'richText' },
  }
}

export function useChart(container: Ref<HTMLElement | null>, build: (theme: ChartTheme) => EChartsCoreOption | null) {
  let instance: echarts.ECharts | null = null
  let resizeObserver: ResizeObserver | null = null
  let themeObserver: MutationObserver | null = null

  function ensure() {
    if (!container.value) return null
    if (instance && instance.getDom() !== container.value) { instance.dispose(); instance = null }
    if (!instance) {
      instance = echarts.init(container.value)
      // 侧栏折叠、栅格换行都会改变容器宽度，只监听 window resize 不够。
      if (typeof ResizeObserver !== 'undefined') {
        resizeObserver = new ResizeObserver(() => instance?.resize())
        resizeObserver.observe(container.value)
      }
      if (!themeObserver && typeof MutationObserver !== 'undefined') {
        themeObserver = new MutationObserver(() => render())
        themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
      }
    }
    return instance
  }

  function render() {
    const chart = ensure()
    if (!chart) return
    const option = build(chartTheme())
    if (option) chart.setOption(option, true)
    else chart.clear()
  }

  function clear() { instance?.clear() }

  function dispose() {
    resizeObserver?.disconnect(); resizeObserver = null
    themeObserver?.disconnect(); themeObserver = null
    instance?.dispose(); instance = null
  }

  onBeforeUnmount(dispose)
  return { render, clear, dispose, get instance() { return instance } }
}

/** 已按需注册图表组件的 echarts 核心，供需要自行管理实例的页面使用。 */
export { echarts }
