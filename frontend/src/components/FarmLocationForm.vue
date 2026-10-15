<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { getRegions, lookupRegion, type RegionNode, type LocationCandidate } from '@/api/agriculture'
import type { FarmCreate } from '@/types/agriculture'
const props = defineProps<{ modelValue: FarmCreate }>()
const emit = defineEmits<{ 'update:modelValue': [value: FarmCreate] }>()
const regions = ref<RegionNode[]>([])
const manual = ref(false)
const error = ref('')
const searching = ref(false)
const candidates = ref<LocationCandidate[]>([])
let revision = 0, alive = true
const province = computed(() => regions.value.find(p => p.name === props.modelValue.province))
const cities = computed(() => province.value?.children || [])
const city = computed(() => cities.value.find(c => c.name === props.modelValue.city))
const districts = computed(() => city.value?.children || [])
const hasCoordinates = computed(() => Number.isFinite(props.modelValue.latitude) && Number.isFinite(props.modelValue.longitude))
function update(data: Partial<FarmCreate>) { emit('update:modelValue', { ...props.modelValue, ...data }) }
function manualText(data: Partial<FarmCreate>) { ++revision; searching.value = false; candidates.value = []; update({ ...data, region_code: undefined, latitude: undefined, longitude: undefined, location_source: undefined, location_label: undefined }) }
function changeProvince(name: string) { ++revision; searching.value = false; candidates.value = []; update({ province: name, city: '', district: '', region_code: undefined, latitude: undefined, longitude: undefined, location_source: undefined, location_label: undefined }) }
function changeCity(name: string) { ++revision; searching.value = false; candidates.value = []; update({ city: name, district: '', region_code: undefined, latitude: undefined, longitude: undefined, location_source: undefined, location_label: undefined }) }
async function changeDistrict(name: string) {
  const code = districts.value.find(d => d.name === name)?.code
  update({ district: name, region_code: code,
    latitude: undefined, longitude: undefined, location_source: undefined, location_label: undefined })
  await search(name, code)
}
function normalized(name: string) { return name.trim().replace(/(壮族自治区|回族自治区|维吾尔自治区|自治区|特别行政区|省|市|县|区)$/, '') }
async function search(name = props.modelValue.district || '', code = props.modelValue.region_code) {
  if (!name) return
  const current = ++revision, expectedProvince = props.modelValue.province || '', expectedCity = props.modelValue.city || ''
  searching.value = true; error.value = ''; candidates.value = []
  try {
    const response = await lookupRegion(name, { region_code: code, province: expectedProvince, city: expectedCity })
    if (!alive || current !== revision) return
    candidates.value = response.data.items.filter(item => {
      const municipality = ['北京市', '天津市', '上海市', '重庆市'].includes(item.province)
      const cityMatches = normalized(item.city) === normalized(expectedCity) ||
        (municipality && ['市辖区', '县'].includes(item.city) && normalized(expectedCity) === normalized(item.province))
      return (!code || item.region_code === code) && normalized(item.name) === normalized(name) &&
        (!expectedProvince || normalized(item.province) === normalized(expectedProvince)) && (!expectedCity || cityMatches)
    })
    const matches = candidates.value.filter(item => !!expectedProvince && !!expectedCity &&
      Number.isFinite(item.latitude) && Number.isFinite(item.longitude))
    if (matches.length === 1) choose(matches[0]!)
    else error.value = candidates.value.length ? '请核对并选择区域参考点，或填写实际经纬度。' : '未找到区域参考点，请填写实际经纬度。'
  } catch { if (alive && current === revision) error.value = '区域定位暂不可用，请填写实际经纬度。' }
  finally { if (alive && current === revision) searching.value = false }
}
function choose(item: LocationCandidate) {
  update({ latitude: item.latitude, longitude: item.longitude, location_source: 'region_reference', location_label: item.label,
    ...(item.region_code ? { region_code: item.region_code, province: item.province, city: item.city, district: item.name } : {}) })
  error.value = ''
}
function coordinate(key: 'latitude' | 'longitude', value: number | undefined | null) {
  ++revision; searching.value = false; candidates.value = []
  update({ [key]: value ?? undefined, location_source: 'manual', location_label: undefined })
}
function manualChanged() { ++revision; searching.value = false; candidates.value = []; update({ province: '', city: '', district: '', region_code: undefined, latitude: undefined, longitude: undefined, location_source: undefined, location_label: undefined }) }
onMounted(async () => {
  try {
    const result = await getRegions()
    if (!alive) return
    regions.value = result.data.items
    // Preserve legacy/free-text locations instead of trapping them in disabled selects.
    if ((props.modelValue.province && !province.value) || (props.modelValue.city && !city.value) ||
      (props.modelValue.district && !districts.value.some(d => d.name === props.modelValue.district))) manual.value = true
  }
  catch { if (alive) { manual.value = true; error.value = '区划列表加载失败，请手动填写地点和实际经纬度。' } }
})
onBeforeUnmount(() => { alive = false; ++revision })
</script>
<template>
  <el-form-item label="省 / 市 / 区县">
    <div class="location-selects" v-if="!manual">
      <el-select :model-value="modelValue.province" placeholder="选择省" filterable @change="changeProvince"><el-option v-for="p in regions" :key="p.code" :label="p.name" :value="p.name" /></el-select>
      <el-select :model-value="modelValue.city" placeholder="选择市" filterable :disabled="!province" @change="changeCity"><el-option v-for="c in cities" :key="c.code" :label="c.name" :value="c.name" /></el-select>
      <el-select :model-value="modelValue.district" placeholder="选择区县" filterable :disabled="!city" @change="changeDistrict"><el-option v-for="d in districts" :key="d.code" :label="d.name" :value="d.name" /></el-select>
    </div>
    <div v-else class="location-selects"><el-input :model-value="modelValue.province" placeholder="省" @update:model-value="manualText({ province: $event })" /><el-input :model-value="modelValue.city" placeholder="市" @update:model-value="manualText({ city: $event })" /><el-input :model-value="modelValue.district" placeholder="区县" @update:model-value="manualText({ district: $event })" /></div>
    <el-checkbox v-model="manual" @change="manualChanged">未收录地区手动填写</el-checkbox>
    <small>2023年区划数据，部分地区未收录。优先按区划查询本地区域参考坐标；未收录地点查询仅发送区县名称，不发送详细地址。参考点不是农场精确位置，可填写实际经纬度。</small>
  </el-form-item>
  <el-form-item label="天气位置">
    <div><el-button :loading="searching" :disabled="!modelValue.district" @click="search()">重新查询区域参考点</el-button>
      <p v-if="error" role="alert">{{ error }}</p>
      <el-button v-for="(item, index) in candidates" :key="index" text @click="choose(item)">{{ item.label }}</el-button>
      <p v-if="hasCoordinates">{{ modelValue.location_source === 'region_reference' ? `已绑定区域参考点：${modelValue.location_label}（非农场精确位置）` : '已填写实际经纬度（WGS84）' }}</p>
    </div>
  </el-form-item>
  <el-form-item label="纬度 / 经度"><div class="location-selects">
    <el-input-number :model-value="modelValue.latitude" :min="-90" :max="90" :precision="6" placeholder="纬度" @change="coordinate('latitude', $event)" />
    <el-input-number :model-value="modelValue.longitude" :min="-180" :max="180" :precision="6" placeholder="经度" @change="coordinate('longitude', $event)" />
  </div></el-form-item>
</template>
<style scoped>
.location-selects { display:flex; gap:8px; width:100%; }
.location-selects > * { flex:1; min-width:0; }
small { display:block; color: var(--app-text-secondary); line-height:1.5; }
</style>
