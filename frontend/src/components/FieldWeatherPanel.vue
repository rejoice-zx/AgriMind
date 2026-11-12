<script setup lang="ts">
import { ref, watch, computed, onMounted, onBeforeUnmount } from 'vue'
import { Refresh, InfoFilled, Sunny, Location, Connection, Timer } from '@element-plus/icons-vue'
import { getFieldWeatherForecast, getFarmWeatherForecast } from '@/api/agriculture'
import type { FieldWeather } from '@/types/agriculture'

const props = withDefaults(defineProps<{
  fieldId?: number | null
  farmId?: number | null
  refreshKey?: number
  days?: number
  selectedDate?: string
  compact?: boolean
  inline?: boolean
}>(), { days: 7, compact: false, inline: false })

const weather = ref<FieldWeather | null>(null)
const loading = ref(false)
const error = ref('')
let revision = 0
let timer: ReturnType<typeof setInterval> | undefined

const forecast = computed(() => props.selectedDate ? weather.value?.forecasts.find(day => day.date === props.selectedDate) : weather.value?.forecasts[0])
const statusLabel = computed(() => ({ online: '已更新', partial: '数据不完整', offline: '服务离线', location_missing: '地点未绑定', unsupported_provider: '提供商未接入' }[weather.value?.status || 'offline']))
function value(number: number | null | undefined, unit = '') { return number == null ? '暂无数据' : `${number}${unit}` }
function time(value: string | null | undefined) { return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '未知' }

async function refresh() {
  if (loading.value || (!props.fieldId && !props.farmId) || document.hidden) return
  const id = props.fieldId || props.farmId!
  const isField = !!props.fieldId
  const requestRevision = ++revision
  loading.value = true
  error.value = ''
  try {
    const response = await (isField ? getFieldWeatherForecast(id, props.days) : getFarmWeatherForecast(id, props.days))
    if (requestRevision === revision && (isField ? props.fieldId === id : !props.fieldId && props.farmId === id)) weather.value = response.data
  } catch {
    if (requestRevision === revision) { weather.value = null; error.value = '天气获取失败，请重试；当前不能确认作业气象条件。' }
  } finally { if (requestRevision === revision) loading.value = false }
}

watch(() => [props.fieldId, props.farmId, props.refreshKey], () => { revision++; loading.value = false; weather.value = null; error.value = ''; void refresh() }, { immediate: true })
function visibilityChanged() { if (!document.hidden) void refresh() }
onMounted(() => {
  timer = setInterval(() => void refresh(), 10 * 60 * 1000)
  document.addEventListener('visibilitychange', visibilityChanged)
})
onBeforeUnmount(() => { revision++; clearInterval(timer); document.removeEventListener('visibilitychange', visibilityChanged) })
</script>

<template>
  <!-- 行内轻量徽标模式：专门用于农场看板地域旁边简略显示 -->
  <span v-if="inline" class="weather-inline-badge">
    <el-popover placement="bottom" :width="320" trigger="click" popper-class="weather-detail-popover">
      <template #reference>
        <span class="inline-badge-content" :class="{ 'is-loading': loading }" role="button" tabindex="0" aria-label="查看室外天气详情">
          <el-icon class="inline-weather-icon"><Sunny /></el-icon>
          <span class="inline-weather-title">室外气象：</span>
          <template v-if="!fieldId && !farmId">
            <span class="inline-text-muted">未选择</span>
          </template>
          <template v-else-if="error">
            <span class="inline-text-error">获取失败</span>
          </template>
          <template v-else-if="weather">
            <span class="inline-temp">{{ value(weather.current.temperature, '℃') }}</span>
            <span class="inline-desc">{{ forecast?.weather || weather.summary || '天气描述缺失' }}</span>
            <span class="inline-meta-extra">湿度 {{ value(weather.current.humidity, '%') }} · 降水 {{ value(weather.current.rainfall, 'mm') }}</span>
            <el-tag
              :type="weather.status === 'online' ? 'success' : 'warning'"
              size="small"
              effect="plain"
              class="inline-status-tag"
            >
              {{ statusLabel }}
            </el-tag>
          </template>
          <template v-else>
            <span class="inline-text-muted">加载中...</span>
          </template>
        </span>
      </template>

      <!-- 悬浮弹窗详情与手动刷新 -->
      <div class="popover-weather-content">
        <div class="popover-header">
          <strong>室外天气与预报详情</strong>
          <div style="display: flex; align-items: center; gap: 6px;">
            <el-tag size="small" :type="weather?.status === 'online' ? 'success' : 'warning'">{{ statusLabel }}</el-tag>
            <el-button
              circle
              size="small"
              :icon="Refresh"
              :loading="loading"
              :disabled="(!fieldId && !farmId) || loading"
              title="刷新天气"
              @click="refresh"
            />
          </div>
        </div>
        <div v-if="weather">
          <p class="popover-summary">{{ weather.summary }}；{{ weather.environment?.notice || '外部天气不能代表棚内实况，设施条件须现场核对。' }}</p>
          <div v-if="forecast" class="popover-forecast">
            <div><strong>预报日期:</strong> {{ forecast.date }} · {{ forecast.weather }}</div>
            <div><strong>气温区间:</strong> {{ value(forecast.temp_min, '℃') }} ~ {{ value(forecast.temp_max, '℃') }}</div>
            <div><strong>降雨概率:</strong> {{ value(forecast.rain_prob, '%') }} · 降水量 {{ value(forecast.rainfall_mm, 'mm') }}</div>
            <div><strong>最大风速:</strong> {{ value(forecast.wind_speed, 'm/s') }}</div>
          </div>
          <div class="popover-meta">
            <div class="popover-meta-item"><el-icon><Location /></el-icon><span>位置: {{ weather.latitude ?? '未绑定' }}, {{ weather.longitude ?? '未绑定' }}</span></div>
            <div class="popover-meta-item"><el-icon><Connection /></el-icon><span>来源: {{ weather.source }}</span></div>
            <div class="popover-meta-item"><el-icon><Timer /></el-icon><span>拉取: {{ time(weather.fetched_at) }}</span></div>
            <div class="popover-note">注: 室外气象模型无法代替棚内传感器，作业前请核实。</div>
          </div>
        </div>
        <div v-else-if="error" class="compact-error-text">{{ error }}</div>
        <div v-else class="compact-tip-text">请确认农场已绑定经纬度天气位置。</div>
      </div>
    </el-popover>
  </span>

  <!-- 紧凑模式：专门用于 Dashboard 顶部等高指标卡片 -->
  <section v-else-if="compact" class="weather-compact-panel" v-loading="loading">
    <div class="compact-header-row">
      <span class="compact-label">室外天气参考{{ weather?.environment ? ` · ${weather.environment.facility_label}` : '' }}</span>
      <div class="compact-actions">
        <el-tag
          :type="weather?.status === 'online' ? 'success' : 'warning'"
          size="small"
          effect="light"
          class="compact-status-tag"
        >
          {{ statusLabel }}
        </el-tag>
        <el-button
          circle
          size="small"
          :icon="Refresh"
          :loading="loading"
          :disabled="(!fieldId && !farmId) || loading"
          title="刷新天气"
          class="compact-refresh-btn"
          @click="refresh"
        />
      </div>
    </div>

    <div class="compact-body">
      <div v-if="!fieldId && !farmId" class="compact-tip-text">请选择田块后查看室外天气</div>
      <div v-else-if="error" class="compact-error-text" role="alert">{{ error }}</div>
      <template v-else-if="weather">
        <div class="compact-primary-row">
          <span class="compact-temp-value">{{ value(weather.current.temperature, '℃') }}</span>
          <span class="compact-weather-state">{{ forecast?.weather || weather.summary || '天气描述缺失' }}</span>
        </div>
        <div class="compact-sub-row">
          <span>湿度 {{ value(weather.current.humidity, '%') }}</span>
          <span class="sub-dot">·</span>
          <span>风速 {{ value(weather.current.wind_speed, 'm/s') }}</span>
          <span class="sub-dot">·</span>
          <span>降水 {{ value(weather.current.rainfall, 'mm') }}</span>
          <el-popover placement="bottom" :width="300" trigger="click" popper-class="weather-detail-popover">
            <template #reference>
              <button type="button" class="compact-info-btn" aria-label="查看天气预报详情"><el-icon class="compact-info-icon"><InfoFilled /></el-icon></button>
            </template>
            <div class="popover-weather-content">
              <div class="popover-header">
                <strong>室外天气与预报详情</strong>
                <el-tag size="small" :type="weather.status === 'online' ? 'success' : 'warning'">{{ statusLabel }}</el-tag>
              </div>
              <p class="popover-summary">{{ weather.summary }}；{{ weather.environment?.notice || '外部天气不能代表棚内实况，设施条件须现场核对。' }}</p>
              <div v-if="forecast" class="popover-forecast">
                <div><strong>预报日期:</strong> {{ forecast.date }} · {{ forecast.weather }}</div>
                <div><strong>气温区间:</strong> {{ value(forecast.temp_min, '℃') }} ~ {{ value(forecast.temp_max, '℃') }}</div>
                <div><strong>降雨概率:</strong> {{ value(forecast.rain_prob, '%') }} · 降水量 {{ value(forecast.rainfall_mm, 'mm') }}</div>
                <div><strong>最大风速:</strong> {{ value(forecast.wind_speed, 'm/s') }}</div>
              </div>
              <div class="popover-meta">
                <div class="popover-meta-item"><el-icon><Location /></el-icon><span>位置: {{ weather.latitude ?? '未绑定' }}, {{ weather.longitude ?? '未绑定' }}</span></div>
                <div class="popover-meta-item"><el-icon><Connection /></el-icon><span>来源: {{ weather.source }}</span></div>
                <div class="popover-meta-item"><el-icon><Timer /></el-icon><span>拉取: {{ time(weather.fetched_at) }}</span></div>
                <div class="popover-note">注: 室外气象模型无法代替棚内传感器，作业前请核实。</div>
              </div>
            </div>
          </el-popover>
        </div>
      </template>
      <div v-else class="compact-tip-text">正在查询室外天气…</div>
    </div>
  </section>

  <!-- 完整模式：用于侧边栏与独立卡片 -->
  <section v-else class="field-weather-full" v-loading="loading">
    <div class="weather-full-header">
      <div class="title-with-tag">
        <strong>室外天气与预报</strong>
        <el-tag :type="weather?.status === 'online' ? 'success' : 'warning'" size="small" effect="light">
          {{ statusLabel }}
        </el-tag>
      </div>
      <el-button size="small" :icon="Refresh" :disabled="(!fieldId && !farmId) || loading" @click="refresh">
        更新天气
      </el-button>
    </div>

    <div v-if="!fieldId && !farmId" class="empty-tip-card">
      请选择田块后查看室外天气；本系统未接入棚内传感器实测数据。
    </div>
    <div v-else-if="error" class="error-tip-card" role="alert">
      {{ error }}
    </div>
    <template v-else-if="weather">
      <!-- 当前天气速览 -->
      <div class="weather-hero-card">
        <div class="hero-left">
          <span class="hero-temp">{{ value(weather.current.temperature, '℃') }}</span>
          <span class="hero-condition">{{ forecast?.weather || weather.summary || '天气描述缺失' }}</span>
        </div>
        <div class="hero-right" v-if="forecast">
          <div class="temp-range">{{ value(forecast.temp_min, '℃') }} ~ {{ value(forecast.temp_max, '℃') }}</div>
          <div class="weather-date">{{ forecast.date }}</div>
        </div>
      </div>

      <p class="weather-summary-text">{{ weather.summary }}</p>
      <p v-if="weather.environment" class="weather-summary-text">{{ weather.environment.facility_label }} · {{ weather.environment.notice }}</p>

      <!-- 4 维微型度量指标卡 -->
      <div class="weather-metrics-grid">
        <div class="metric-mini-tile">
          <span class="tile-label">相对湿度</span>
          <span class="tile-val">{{ value(weather.current.humidity, '%') }}</span>
        </div>
        <div class="metric-mini-tile">
          <span class="tile-label">当前风速</span>
          <span class="tile-val">{{ value(weather.current.wind_speed, 'm/s') }}</span>
        </div>
        <div class="metric-mini-tile">
          <span class="tile-label">当前降水</span>
          <span class="tile-val">{{ value(weather.current.rainfall, 'mm') }}</span>
        </div>
        <div class="metric-mini-tile">
          <span class="tile-label">降水概率</span>
          <span class="tile-val">{{ value(forecast?.rain_prob, '%') }}</span>
        </div>
      </div>

      <!-- 未来预报精要 -->
      <div v-if="forecast" class="weather-forecast-box">
        <div class="forecast-title">未来 24h 农事作业气象评估</div>
        <div class="forecast-meta-row">
          <span>日降水量: {{ value(forecast.rainfall_mm, 'mm') }}</span>
          <span>日最大风速: {{ value(forecast.wind_speed, 'm/s') }}</span>
        </div>
      </div>
      <p v-else-if="selectedDate" class="empty-tip-card">{{ selectedDate }} 无可用预报；本面板不代填历史天气。</p>

      <!-- 来源与元数据 (精致收纳) -->
      <div class="weather-metadata-footer">
        <div class="meta-item"><span>来源:</span> {{ weather.source }}（室外模型）</div>
        <div class="meta-item"><span>坐标:</span> {{ weather.latitude ?? '未绑定' }}, {{ weather.longitude ?? '未绑定' }} · {{ weather.coordinate_source === 'farm' ? '农场坐标' : weather.coordinate_source === 'field' ? '田块坐标' : '无坐标' }}</div>
        <div class="meta-item"><span>时间:</span> {{ time(weather.current.timestamp) }} (拉取: {{ time(weather.fetched_at) }})</div>
        <div class="meta-notice">页面可见时每 10 分钟自动更新；外部天气不能代表棚内实况，设施条件须现场核对。风险阈值仅用于提醒，不能作为作业批准。</div>
      </div>
    </template>
    <div v-else class="empty-tip-card">正在查询天气，暂无可用结论。</div>
  </section>
</template>

<style scoped>
/* 紧凑模式 */
.weather-compact-panel {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  height: 100%;
}

.compact-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}

.compact-label {
  font-size: var(--app-text-sm);
  color: var(--app-text-secondary);
  font-weight: 500;
}

.compact-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.compact-status-tag {
  font-size: var(--app-text-xs);
  height: 20px;
  line-height: 18px;
  padding: 0 6px;
  border-radius: var(--app-radius-sm);
}

.compact-refresh-btn {
  width: 22px;
  height: 22px;
  padding: 0;
  font-size: var(--app-text-xs);
}

.compact-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.compact-primary-row {
  display: flex;
  align-items: baseline;
  gap: var(--app-space-2);
  margin-bottom: var(--app-space-1);
}

.compact-temp-value {
  font-size: var(--app-text-xl);
  font-weight: 700;
  color: var(--app-text);
}

.compact-weather-state {
  font-size: var(--app-text-sm);
  color: var(--app-text);
  font-weight: 500;
}

.compact-sub-row {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  display: flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}

.sub-dot {
  color: var(--app-text-placeholder);
}

.compact-info-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 24px;
  min-height: 24px;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: pointer;
}

.compact-info-icon {
  font-size: var(--app-text-sm);
  color: var(--app-text-placeholder);
  cursor: pointer;
  margin-left: 2px;
  transition: color 0.2s;
}

.compact-info-icon:hover {
  color: var(--el-color-primary);
}

.compact-tip-text {
  font-size: var(--app-text-sm);
  color: var(--app-text-placeholder);
  margin: 6px 0;
}

.compact-error-text {
  font-size: var(--app-text-xs);
  color: var(--el-color-danger);
  margin: 6px 0;
}

/* 完整模式 */
.field-weather-full {
  font-size: var(--app-text-sm);
  line-height: 1.5;
  color: var(--app-text-secondary);
}

.weather-full-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--app-space-3);
}

.title-with-tag {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}

.title-with-tag strong {
  font-size: var(--app-text-base);
  color: var(--app-text);
}

.weather-hero-card {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: var(--app-radius-lg);
  padding: var(--app-space-3) 14px;
  margin-bottom: 10px;
}

.hero-left {
  display: flex;
  align-items: baseline;
  gap: var(--app-space-2);
}

.hero-temp {
  font-size: var(--app-text-3xl);
  font-weight: 700;
  color: var(--app-text);
}

.hero-condition {
  font-size: var(--app-text-sm);
  font-weight: 600;
  color: var(--el-color-primary);
}

.hero-right {
  text-align: right;
}

.temp-range {
  font-size: var(--app-text-xs);
  font-weight: 600;
  color: var(--app-text-secondary);
}

.weather-date {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
  margin-top: 2px;
}

.weather-summary-text {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin-bottom: 10px;
  line-height: 1.4;
}

.weather-metrics-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--app-space-2);
  margin-bottom: 10px;
}

.metric-mini-tile {
  background: var(--app-surface-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-md);
  padding: var(--app-space-2) 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.tile-label {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.tile-val {
  font-size: var(--app-text-sm);
  font-weight: 600;
  color: var(--app-text);
}

.weather-forecast-box {
  background: var(--app-surface-2);
  border: 1px dashed var(--app-border);
  border-radius: var(--app-radius-md);
  padding: var(--app-space-2) 10px;
  margin-bottom: 10px;
}

.forecast-title {
  font-size: var(--app-text-xs);
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: var(--app-space-1);
}

.forecast-meta-row {
  display: flex;
  justify-content: space-between;
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.weather-metadata-footer {
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
  line-height: 1.5;
  border-top: 1px solid var(--app-border-light);
  padding-top: var(--app-space-2);
}

.meta-item {
  margin-bottom: 2px;
  overflow-wrap: anywhere;
}

.meta-item span {
  color: var(--app-text-secondary);
}

.meta-notice {
  margin-top: 6px;
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

.empty-tip-card, .error-tip-card {
  padding: var(--app-space-3);
  border-radius: var(--app-radius-md);
  font-size: var(--app-text-xs);
  line-height: 1.5;
}

.empty-tip-card {
  background: var(--app-surface-2);
  color: var(--app-text-secondary);
  border: 1px dashed var(--app-border);
}

.error-tip-card {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
  border: 1px solid var(--el-color-danger-light-7);
}

/* Popover Content */
.popover-weather-content {
  font-size: var(--app-text-xs);
  color: var(--app-text);
}

.popover-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--app-space-2);
  padding-bottom: 6px;
  border-bottom: 1px solid var(--app-border-light);
}

.popover-summary {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  margin-bottom: var(--app-space-2);
}

.popover-forecast {
  background: var(--app-surface-2);
  border-radius: var(--app-radius-sm);
  padding: var(--app-space-2);
  margin-bottom: var(--app-space-2);
  line-height: 1.6;
}

.popover-meta {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
  line-height: 1.6;
}

.popover-meta-item {
  display: flex;
  align-items: center;
  gap: var(--app-space-1);
}

.popover-note {
  margin-top: 6px;
  font-size: var(--app-text-xs);
  color: var(--app-text-placeholder);
}

/* 行内徽标样式 (Art Design Pro 规范) */
.weather-inline-badge {
  display: inline-flex;
  align-items: center;
  cursor: pointer;
  vertical-align: middle;
}

.inline-badge-content {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-7);
  padding: 3px 10px;
  border-radius: var(--app-radius-round);
  font-size: var(--app-text-xs);
  color: var(--el-color-primary-dark-2);
  transition: var(--app-transition);
  box-shadow: var(--app-shadow-sm);
}

.inline-badge-content:hover {
  background: var(--el-color-primary-light-8);
  border-color: var(--el-color-primary-light-5);
  box-shadow: var(--app-shadow-md);
  transform: translateY(-1px);
}

.inline-weather-icon {
  color: var(--el-color-warning);
  font-size: var(--app-text-base);
}

.inline-weather-title {
  font-weight: 500;
  color: var(--app-text-secondary);
}

.inline-temp {
  font-weight: 700;
  color: var(--app-text);
}

.inline-desc {
  color: var(--el-color-primary);
  font-weight: 600;
}

.inline-meta-extra {
  font-size: var(--app-text-xs);
  color: var(--app-text-secondary);
}

.inline-status-tag {
  margin-left: 2px;
  height: 20px;
  padding: 0 6px;
  font-size: var(--app-text-xs);
  line-height: 18px;
}

.inline-text-muted {
  color: var(--app-text-placeholder);
  font-size: var(--app-text-xs);
}

.inline-text-error {
  color: var(--el-color-danger);
  font-size: var(--app-text-xs);
}
</style>

