<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus, Search, Upload, Download, Delete, Edit, Setting, Refresh, Folder, EditPen, Close
} from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import {
  getKnowledgeList, createKnowledge, updateKnowledge, deleteKnowledge,
  importKnowledge, exportKnowledge, getIndexStatus, getKnowledgeCategories,
  deleteKnowledgeByCategory, rebuildIndexSSE
} from '@/api/knowledge'
import { getSettings, updateSettings } from '@/api/settings'
import type { KnowledgeItem, IndexStatus } from '@/types'
import type { KnowledgeImportResult } from '@/api/knowledge'

const authorityOptions = [
  { value: '未核验', label: '未核验' }, { value: 'L1', label: 'L1 规范来源标注' },
  { value: 'L2', label: 'L2 专业文献标注' }, { value: 'L3', label: 'L3 农技推广标注' },
  { value: 'L4', label: 'L4 专家案例标注' }
]
const sourceOptions = [
  { value: 'unverified', label: '未核验，暂不参与检索' }, { value: 'quarantined', label: '隔离，暂不参与检索' },
  { value: 'standard', label: '规范来源标注' }, { value: 'literature', label: '专业文献标注' },
  { value: 'extension', label: '农技推广标注' }, { value: 'expert', label: '专家案例标注' }
]
function sourceLabel(value: string | null | undefined) {
  return sourceOptions.find(option => option.value === value)?.label || value || '未填写'
}
function emptyKnowledgeForm() {
  return { question: '', answer: '', keywords: [] as string[], category: '通用', crop: '', region: '',
    growth_stage: '', topic: '', disease: '', source_type: 'unverified', authority_level: '未核验',
    source_name: '', version: '', publish_date: '', source_updated_at: '', url: '',
    effective_from: '', effective_to: '', is_current: 1 as 0 | 1 | undefined }
}

// ── 分类（动态从后端加载 + 本地自定义） ──
const categories = ref<string[]>([])
const activeCategory = ref('全部')
const newCatInput = ref('')
const showCatInput = ref(false)
const editingCat = ref<string | null>(null)
const editingCatValue = ref('')

async function loadCategories() {
  try {
    const res = await getKnowledgeCategories()
    const remote = res.data.categories || []
    // 合并：保留本地自定义的（不在远程中的）
    const localOnly = categories.value.filter(c => c !== '全部' && !remote.includes(c))
    categories.value = ['全部', ...remote, ...localOnly]
  } catch { /* keep current */ }
}

function addCategory() {
  const v = newCatInput.value.trim()
  if (!v) return
  if (categories.value.includes(v)) { ElMessage.warning('分类已存在'); return }
  categories.value.push(v)
  newCatInput.value = ''; showCatInput.value = false
}

function startEditCat(cat: string) { editingCat.value = cat; editingCatValue.value = cat }
function confirmEditCat(oldCat: string) {
  const v = editingCatValue.value.trim()
  if (!v || v === oldCat) { editingCat.value = null; return }
  if (categories.value.includes(v)) { ElMessage.warning('分类已存在'); editingCat.value = null; return }
  const idx = categories.value.indexOf(oldCat)
  if (idx !== -1) categories.value[idx] = v
  if (activeCategory.value === oldCat) activeCategory.value = v
  editingCat.value = null
}

function deleteCat(cat: string) {
  categories.value = categories.value.filter(c => c !== cat)
  if (activeCategory.value === cat) { activeCategory.value = '全部'; loadList() }
}

function selectCategory(cat: string) { activeCategory.value = cat; currentPage.value = 1; loadList() }

// ── 列表 ──
const loading = ref(false)
const list = ref<KnowledgeItem[]>([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const selectedCrop = ref('全部')
const selectedStage = ref(''), selectedRegion = ref(''), selectedDisease = ref('')
let listRevision = 0
const cropOptions = ['全部', '番茄', '黄瓜', '辣椒', '茄子', '西瓜', '通用']
const selected = ref<KnowledgeItem | null>(null)

async function loadList() {
  const revision = ++listRevision
  loading.value = true
  try {
    const res = await getKnowledgeList({
      page: currentPage.value, page_size: pageSize.value,
      category: activeCategory.value === '全部' ? undefined : activeCategory.value,
      keyword: keyword.value || undefined,
      crop: selectedCrop.value === '全部' ? undefined : selectedCrop.value,
      growth_stage: selectedStage.value || undefined,
      region: selectedRegion.value.trim() || undefined,
      disease: selectedDisease.value.trim() || undefined,
    })
    if (revision !== listRevision) return
    list.value = res.data.items; total.value = res.data.total
    if (selected.value && !list.value.find(i => i.id === selected.value!.id)) selected.value = null
  } catch (e: any) { if (revision === listRevision) ElMessage.error(e.response?.data?.detail || '加载失败') }
  finally { if (revision === listRevision) loading.value = false }
}
function handleSearch() { currentPage.value = 1; loadList() }

async function handleDeleteAllInCategory() {
  if (activeCategory.value === '全部') return
  try {
    await ElMessageBox.confirm(`确定删除分类「${activeCategory.value}」下的所有知识条目？此操作不可恢复。`, '确认删除', { type: 'warning', confirmButtonText: '全部删除', cancelButtonText: '取消' })
    const res = await deleteKnowledgeByCategory(activeCategory.value)
    ElMessage.success(`已删除 ${res.data.deleted_count} 条知识`)
    selected.value = null; loadList(); loadIndexStatus(); loadCategories()
  } catch (e: any) { if (e !== 'cancel') ElMessage.error(e.response?.data?.detail || '删除失败') }
}
function handlePageChange(p: number) { currentPage.value = p; loadList() }

function selectItem(item: KnowledgeItem) {
  selected.value = { ...item }; editMode.value = false
  form.question = item.question; form.answer = item.answer
  form.keywords = [...(item.keywords || [])]; form.category = item.category
  form.crop = item.crop || ''
  form.region = item.region || ''
  form.growth_stage = item.growth_stage || ''
  form.disease = item.disease || ''
  form.topic = item.topic || ''
  form.source_type = item.source_type || 'unverified'
  form.authority_level = item.authority_level || '未核验'
  form.source_name = item.source_name || ''
  form.publish_date = item.publish_date || ''
  form.url = item.url || ''
  form.version = item.version || ''
  form.source_updated_at = item.source_updated_at || ''
  form.effective_from = item.effective_from || ''
  form.effective_to = item.effective_to || ''
  form.is_current = item.is_current ?? undefined
}

async function handleDelete(item: KnowledgeItem, ev?: Event) {
  ev?.stopPropagation()
  try {
    await ElMessageBox.confirm(`确定删除「${item.question.substring(0, 20)}…」？`, '确认', { type: 'warning' })
    await deleteKnowledge(item.id); ElMessage.success('已删除')
    if (selected.value?.id === item.id) selected.value = null
    loadList(); loadIndexStatus(); loadCategories()
  } catch (e: any) { if (e !== 'cancel') ElMessage.error(e.response?.data?.detail || '删除失败') }
}

// ── 添加 ──
const addVisible = ref(false)
const addRef = ref<FormInstance>()
const addForm = reactive(emptyKnowledgeForm())
const addKw = ref('')
const addRules: FormRules = {
  question: [{ required: true, message: '请输入问题', trigger: 'blur' }],
  answer: [{ required: true, message: '请输入答案', trigger: 'blur' }],
  category: [{ required: true, message: '请选择分类', trigger: 'change' }]
}
function openAdd() {
  Object.assign(addForm, emptyKnowledgeForm())
  addKw.value = ''; addVisible.value = true
}
function pushAddKw() { const v = addKw.value.trim(); if (v && !addForm.keywords.includes(v)) { addForm.keywords.push(v); addKw.value = '' } }
async function submitAdd() {
  if (!addRef.value) return
  try { await addRef.value.validate() } catch { return }
  try {
    await createKnowledge(addForm)
    ElMessage.success('添加成功'); addVisible.value = false; loadList(); loadIndexStatus(); loadCategories()
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || '添加失败') }
}

// ── 详情编辑 ──
const form = reactive(emptyKnowledgeForm())
const editMode = ref(false)
const formKw = ref('')
function pushFormKw() { const v = formKw.value.trim(); if (v && !form.keywords.includes(v)) { form.keywords.push(v); formKw.value = '' } }
async function saveEdit() {
  if (!selected.value) return
  const knowledgeId = selected.value.id
  try {
    const res = await updateKnowledge(knowledgeId, { ...form, keywords: [...form.keywords] })
    ElMessage.success('已保存')
    const idx = list.value.findIndex(i => i.id === knowledgeId)
    if (idx !== -1) list.value[idx] = res.data
    if (selected.value?.id === knowledgeId) selectItem(res.data)
    loadIndexStatus(); loadCategories()
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || '保存失败') }
}
function cancelEdit() { editMode.value = false; if (selected.value) selectItem(selected.value) }

// ── 导入导出 ──
const importVisible = ref(false); const importFile = ref<File | null>(null); const importLoading = ref(false)
const importResult = ref<KnowledgeImportResult | null>(null)
function onFileChange(f: any) { importFile.value = f.raw; importResult.value = null }
async function doImport() {
  if (!importFile.value) { ElMessage.warning('请选择文件'); return }
  importLoading.value = true
  try {
    const data = JSON.parse(await importFile.value.text())
    const items = Array.isArray(data) ? data : data?.items
    if (!Array.isArray(items)) { ElMessage.error('JSON须为条目数组或包含items数组的导出对象'); return }
    const res = await importKnowledge(items)
    importResult.value = res.data
    const message = `已导入 ${res.data.success_count} 条，跳过 ${res.data.skip_count} 条，失败 ${res.data.errors.length} 条`
    if (res.data.errors.length) ElMessage.warning(message)
    else ElMessage.success(message)
    loadList(); loadIndexStatus(); loadCategories()
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || '导入失败') }
  finally { importLoading.value = false }
}
async function doExport() {
  try {
    const res = await exportKnowledge()
    const data = Array.isArray(res.data) ? res.data : res.data?.items
    if (!Array.isArray(data)) throw new Error('导出响应不包含条目数组')
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
    a.download = `knowledge_${new Date().toISOString().split('T')[0]}.json`; a.click(); URL.revokeObjectURL(a.href)
    ElMessage.success('导出成功')
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || '导出失败') }
}

// ── RAG 配置 ──
const indexStatus = ref<IndexStatus | null>(null)
const rebuildLoading = ref(false)
const ragForm = reactive({ chunk_size: 500, chunk_overlap: 50 })
const embeddingModel = ref('')
const ragSaving = ref(false)

// 重建进度
const rebuildActive = ref(false)
const rebuildTotal = ref(0)
const rebuildCurrent = ref(0)
const rebuildSuccessCount = ref(0)
const rebuildFailCount = ref(0)
const rebuildDone = ref(false)
const rebuildFailed = ref(false)

let indexReadVersion = 0
async function loadIndexStatus() {
  const version = ++indexReadVersion
  indexStatus.value = null
  try { const response = await getIndexStatus(); if (version === indexReadVersion) indexStatus.value = response.data }
  catch { if (version === indexReadVersion) ElMessage.error("索引状态读取失败，未显示上次结果") }
}
async function loadRAGConfig() {
  try {
    const d = (await getSettings()).data as any
    if (d.rag) { ragForm.chunk_size = d.rag.chunk_size ?? 500; ragForm.chunk_overlap = d.rag.chunk_overlap ?? 50 }
    if (d.embedding) embeddingModel.value = d.embedding.model || ''
  } catch { /* */ }
}
async function saveRAG() {
  ragSaving.value = true
  try { await updateSettings({ rag: { ...ragForm } } as any); ElMessage.success('分块配置已保存；已有索引需重建后采用新分块') }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || '保存失败') }
  finally { ragSaving.value = false }
}
async function doRebuild() {
  try {
    await ElMessageBox.confirm('重建索引可能需要一些时间，确定继续？', '确认', { type: 'warning' })
  } catch { return }

  rebuildLoading.value = true
  rebuildActive.value = true
  rebuildDone.value = false
  rebuildFailed.value = false
  rebuildTotal.value = 0
  rebuildCurrent.value = 0
  rebuildSuccessCount.value = 0
  rebuildFailCount.value = 0

  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  try {
    reader = await rebuildIndexSSE()
    const decoder = new TextDecoder()
    let buffer = ''
    let completedSuccessfully = false

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        let evt
        try { evt = JSON.parse(line.slice(6)) } catch { continue }
        if (evt.type === 'start') {
          rebuildTotal.value = evt.total
        } else if (evt.type === 'progress') {
          rebuildCurrent.value = evt.current
        } else if (evt.type === 'done') {
          completedSuccessfully = true
          rebuildDone.value = true
          rebuildSuccessCount.value = evt.success_count
          rebuildFailCount.value = evt.fail_count
          break
        } else if (evt.type === 'error') {
          rebuildFailCount.value = evt.fail_count || 0
          throw new Error(evt.message || '索引重建未完成')
        }
      }
      if (completedSuccessfully) break
    }

    if (!completedSuccessfully) throw new Error('未收到索引重建完成回执，请查询索引状态后再处理')
    if (rebuildFailCount.value > 0) {
      ElMessage.warning(`重建完成，成功 ${rebuildSuccessCount.value} 条，失败 ${rebuildFailCount.value} 条`)
    } else {
      ElMessage.success(`索引重建成功，共 ${rebuildSuccessCount.value} 条`)
    }
    loadIndexStatus()
  } catch (e: any) {
    ElMessage.error(e.message || '重建失败')
    rebuildFailed.value = true
    rebuildDone.value = true
    loadIndexStatus()
  } finally {
    try { await reader?.cancel() } catch { /* Stream cleanup does not alter the saved receipt. */ }
    try { reader?.releaseLock() } catch { /* Already released/unavailable readers need no further action. */ }
    rebuildLoading.value = false
  }
}

onMounted(() => { loadCategories(); loadList(); loadIndexStatus(); loadRAGConfig() })
</script>

<template>
  <div class="three-col">
    <!-- ====== 左栏：分类 ====== -->
    <aside class="col col-left">
      <div class="col-header"><span class="col-title">分类</span></div>
      <nav class="cat-nav">
        <div
          v-for="cat in categories" :key="cat"
          class="cat-item" :class="{ active: activeCategory === cat }"
        >
          <!-- 编辑态 -->
          <template v-if="editingCat === cat && cat !== '全部'">
            <el-input v-model="editingCatValue" size="small" style="flex:1" @keyup.enter="confirmEditCat(cat)" />
            <el-button :icon="Edit" link size="small" @click="confirmEditCat(cat)" />
            <el-button :icon="Close" link size="small" @click="editingCat = null" />
          </template>
          <!-- 正常态 -->
          <template v-else>
            <div class="cat-label" @click="selectCategory(cat)">
              <el-icon :size="15"><Folder /></el-icon>
              <span>{{ cat }}</span>
            </div>
            <div v-if="cat !== '全部'" class="cat-ops">
              <el-button :icon="EditPen" link size="small" @click.stop="startEditCat(cat)" />
              <el-button :icon="Delete" link size="small" type="danger" @click.stop="deleteCat(cat)" />
            </div>
          </template>
        </div>
      </nav>
      <!-- 添加分类 -->
      <div class="cat-add">
        <template v-if="showCatInput">
          <el-input v-model="newCatInput" size="small" placeholder="分类名称" @keyup.enter="addCategory" />
          <el-button size="small" type="primary" @click="addCategory">确定</el-button>
          <el-button size="small" @click="showCatInput = false">取消</el-button>
        </template>
        <el-button v-else :icon="Plus" size="small" text style="width:100%" @click="showCatInput = true">添加分类</el-button>
      </div>
      <div class="col-footer">
        <el-button text :icon="Upload" size="small" @click="importVisible = true">导入</el-button>
        <el-button text :icon="Download" size="small" @click="doExport">导出</el-button>
      </div>
    </aside>

    <!-- ====== 中栏：列表 ====== -->
    <section class="col col-center">
      <div class="col-header">
        <el-input v-model="keyword" placeholder="搜索问题或答案…" clearable style="flex:1" @keyup.enter="handleSearch" @clear="handleSearch">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="selectedCrop" placeholder="作物筛选" style="width:105px" size="default" @change="handleSearch">
          <el-option v-for="c in cropOptions" :key="c" :label="c" :value="c" />
        </el-select>
        <el-button type="primary" :icon="Plus" @click="openAdd">添加</el-button>
        <el-button v-if="activeCategory !== '全部'" type="danger" :icon="Delete" @click="handleDeleteAllInCategory">删除全部</el-button>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;padding:10px">
        <el-select v-model="selectedStage" clearable placeholder="生长期筛选" style="width:145px" @change="handleSearch">
          <el-option v-for="stage in ['苗期','开花期','坐果期','开花坐果期','果实膨大期','采收期','全生育期']" :key="stage" :label="stage" :value="stage" />
        </el-select>
        <el-input v-model="selectedRegion" clearable placeholder="省市区全称；支持省级简称" style="width:210px" @keyup.enter="handleSearch" @clear="handleSearch" />
        <el-input v-model="selectedDisease" clearable placeholder="病害名称" style="width:150px" @keyup.enter="handleSearch" @clear="handleSearch" />
        <el-button :icon="Search" @click="handleSearch">筛选</el-button>
        <span>管理页包含待核验/隔离条目，检索还会检查日期与来源。</span>
      </div>
      <div v-loading="loading" class="item-list">
        <div v-for="item in list" :key="item.id" class="item-card" :class="{ active: selected?.id === item.id }" @click="selectItem(item)">
          <div class="item-top">
            <div style="display:flex;align-items:center;gap:6px">
              <span class="item-id">{{ item.id }}</span>
              <el-tag size="small" effect="plain">{{ item.category }}</el-tag>
              <el-tag size="small" :type="item.authority_level === 'L1' ? 'success' : (item.authority_level === 'L2' ? 'primary' : 'warning')" effect="dark">
                {{ item.authority_level || '未核验' }}（标注）
              </el-tag>
            </div>
            <el-tag v-if="item.crop" size="small" type="success" effect="light">🌱 {{ item.crop }}</el-tag>
          </div>
          <p class="item-q">{{ item.question }}</p>
          <div v-if="item.growth_stage || item.disease" class="item-agri-meta">
            <span v-if="item.growth_stage" class="agri-tag">阶段: {{ item.growth_stage }}</span>
            <span v-if="item.disease" class="agri-tag">靶标: {{ item.disease }}</span>
          </div>
          <div class="item-bottom">
            <div class="item-kws">
              <el-tag v-for="kw in (item.keywords||[]).slice(0,2)" :key="kw" size="small" type="info" round>{{ kw }}</el-tag>
              <span v-if="(item.keywords||[]).length > 2" class="more-kw">+{{ item.keywords.length - 2 }}</span>
            </div>
            <el-button :icon="Delete" type="danger" text size="small" @click="handleDelete(item, $event)" />
          </div>
        </div>
        <el-empty v-if="!loading && list.length === 0" description="暂无数据" :image-size="64" />
      </div>
      <div class="col-footer center">
        <el-pagination v-model:current-page="currentPage" :page-size="pageSize" :total="total" small background layout="prev, pager, next" @current-change="handlePageChange" />
      </div>
    </section>

    <!-- ====== 右栏：可滚动详情 + 固定底部 RAG 配置 ====== -->
    <aside class="col col-right">
      <!-- 知识详情区域（选中时显示） -->
      <template v-if="selected">
        <div class="col-header"><span class="col-title">知识详情 — {{ selected.id }}</span></div>

        <!-- 可滚动区域：详情内容 + 编辑表单 -->
        <div class="detail-scroll">
          <el-card shadow="never" class="detail-block">
            <p class="meta-line"><span class="meta-label">ID</span>{{ selected.id }}</p>
            <p class="meta-line"><span class="meta-label">分类</span><el-tag size="small" effect="plain">{{ selected.category }}</el-tag></p>
            <p class="meta-line"><span class="meta-label">关键词</span>
              <template v-if="(selected.keywords||[]).length"><el-tag v-for="kw in selected.keywords" :key="kw" size="small" type="info" round style="margin-right:var(--app-space-1)">{{ kw }}</el-tag></template>
              <span v-else class="text-muted">无</span>
            </p>
          </el-card>

          <!-- 农业规程与权威溯源 -->
          <el-card shadow="never" class="detail-block">
            <template #header><span class="block-title">🌾 农业规程与溯源元数据</span></template>
            <p>来源与分级只是录入标注，不表示已核验；缺失范围保持未知。来源类型为“未核验”或“隔离”的条目不参与检索，其他来源仍仅作候选参考。</p>
            <div class="meta-grid">
              <div class="meta-line"><span class="meta-label">适用作物</span><el-tag size="small">{{ selected.crop || '未填写' }}</el-tag></div>
              <div class="meta-line"><span class="meta-label">适用地域</span>{{ selected.region || '未填写' }}</div>
              <div class="meta-line"><span class="meta-label">生长阶段</span><span>{{ selected.growth_stage || '未填写' }}</span></div>
              <div class="meta-line"><span class="meta-label">防治靶标</span><span>{{ selected.disease || '未填写' }}</span></div>
              <div class="meta-line"><span class="meta-label">来源类型</span>{{ sourceLabel(selected.source_type) }}</div>
              <div class="meta-line"><span class="meta-label">分级标注</span>
                <el-tag size="small" :type="selected.authority_level === 'L1' ? 'success' : (selected.authority_level === 'L2' ? 'primary' : 'warning')" effect="dark">
                  {{ selected.authority_level || '未核验' }}
                </el-tag>
              </div>
              <div class="meta-line full"><span class="meta-label">来源依据</span><span>{{ selected.source_name || '未填写' }}</span></div>
              <div class="meta-line"><span class="meta-label">来源版本</span>{{ selected.version || '未填写' }}</div>
              <div class="meta-line"><span class="meta-label">版本标注</span>{{ selected.is_current === 1 ? '当前版本' : selected.is_current === 0 ? '非当前版本' : '未知' }}</div>
              <div class="meta-line"><span class="meta-label">生效日期</span>{{ selected.effective_from || '未限定' }}</div>
              <div class="meta-line"><span class="meta-label">截止日期</span>{{ selected.effective_to || '未限定' }}</div>
              <div class="meta-line"><span class="meta-label">发布日期</span><span>{{ selected.publish_date || '未标明' }}</span></div>
              <div class="meta-line full">
                <span class="meta-label">溯源链接</span>
                <a v-if="selected.url" :href="selected.url" target="_blank" class="url-link">{{ selected.url }}</a>
                <span v-else class="text-muted">无</span>
              </div>
            </div>
          </el-card>
          <el-card shadow="never" class="detail-block">
            <template #header><span class="block-title">❓ 问题</span></template>
            <el-input v-if="editMode" v-model="form.question" type="textarea" :rows="2" />
            <p v-else class="block-text">{{ selected.question }}</p>
          </el-card>
          <el-card shadow="never" class="detail-block">
            <template #header><span class="block-title">💬 答案</span></template>
            <el-input v-if="editMode" v-model="form.answer" type="textarea" :rows="5" />
            <p v-else class="block-text pre">{{ selected.answer }}</p>
          </el-card>
          <template v-if="editMode">
            <el-card shadow="never" class="detail-block">
              <template #header><span class="block-title">编辑分类与关键词</span></template>
              <div class="field-row"><span class="field-label">分类</span>
                <el-select v-model="form.category" size="small" style="flex:1">
                  <el-option v-for="c in categories.filter(c=>c!=='全部')" :key="c" :label="c" :value="c" />
                </el-select>
              </div>
              <div class="field-row" style="margin-top:var(--app-space-3);align-items:flex-start"><span class="field-label">关键词</span>
                <div style="flex:1">
                  <div style="display:flex;gap:6px;margin-bottom:6px"><el-input v-model="formKw" size="small" placeholder="回车添加" @keyup.enter="pushFormKw" /><el-button size="small" @click="pushFormKw">添加</el-button></div>
                  <div style="display:flex;flex-wrap:wrap;gap:var(--app-space-1)"><el-tag v-for="(kw,i) in form.keywords" :key="kw" closable size="small" @close="form.keywords.splice(i,1)">{{ kw }}</el-tag></div>
                </div>
              </div>
            </el-card>
            <el-card shadow="never" class="detail-block">
              <template #header><span class="block-title">🌾 编辑农业规程属性</span></template>
              <div class="field-row"><span class="field-label">适用作物</span><el-input v-model="form.crop" size="small" placeholder="如：番茄" /></div>
              <div class="field-row"><span class="field-label">生长阶段</span>
                <el-select v-model="form.growth_stage" size="small" style="flex:1" clearable placeholder="选择阶段">
                  <el-option label="育苗期" value="育苗期" />
                  <el-option label="苗期营养生长" value="苗期营养生长" />
                  <el-option label="开花坐果期" value="开花坐果期" />
                  <el-option label="果实膨大转色期" value="果实膨大转色期" />
                  <el-option label="采收盛期" value="采收盛期" />
                  <el-option label="全生育期" value="全生育期" />
                </el-select>
              </div>
              <div class="field-row"><span class="field-label">防治病害</span><el-input v-model="form.disease" size="small" placeholder="如：灰霉病、早疫病" /></div>
              <div class="field-row"><span class="field-label">来源类型</span>
                <el-select v-model="form.source_type" size="small" style="flex:1">
                  <el-option v-for="option in sourceOptions" :key="option.value" :label="option.label" :value="option.value" />
                </el-select>
              </div>
              <div class="field-row"><span class="field-label">分级标注</span>
                <el-select v-model="form.authority_level" size="small" style="flex:1">
                  <el-option v-for="option in authorityOptions" :key="option.value" :label="option.label" :value="option.value" />
                </el-select>
              </div>
              <div class="field-row"><span class="field-label">来源依据</span><el-input v-model="form.source_name" size="small" placeholder="填写原文或产品标签的完整名称" /></div>
              <div class="field-row"><span class="field-label">适用地域</span><el-input v-model="form.region" size="small" placeholder="未提供请留空" /></div>
              <div class="field-row"><span class="field-label">主题</span><el-input v-model="form.topic" size="small" placeholder="未提供请留空" /></div>
              <div class="field-row"><span class="field-label">来源版本</span><el-input v-model="form.version" size="small" placeholder="未提供请留空" /></div>
              <div class="field-row"><span class="field-label">版本标注</span><el-select v-model="form.is_current" size="small" placeholder="未知，未修改"><el-option label="当前版本标注" :value="1" /><el-option label="非当前版本标注" :value="0" /></el-select></div>
              <div class="field-row"><span class="field-label">生效日期</span><el-input v-model="form.effective_from" size="small" placeholder="YYYY-MM-DD；未限定请留空" /></div>
              <div class="field-row"><span class="field-label">截止日期</span><el-input v-model="form.effective_to" size="small" placeholder="YYYY-MM-DD；未限定请留空" /></div>
              <div class="field-row"><span class="field-label">修订日期</span><el-input v-model="form.source_updated_at" size="small" placeholder="未提供请留空" /></div>
              <div class="field-row"><span class="field-label">发布日期</span><el-input v-model="form.publish_date" size="small" placeholder="如：2020-05-01" /></div>
              <div class="field-row"><span class="field-label">溯源网址</span><el-input v-model="form.url" size="small" placeholder="https://..." /></div>
            </el-card>
          </template>

        </div>
      </template>
      <div v-else class="empty-detail"><el-empty description="选择一条知识查看详情" :image-size="64" /></div>

      <!-- 固定底部：编辑按钮 + RAG 配置 -->
      <div class="right-fixed-bottom">
        <!-- 编辑/保存按钮（仅选中时显示） -->
        <template v-if="selected">
          <div v-if="editMode" class="action-bar" style="margin-bottom:var(--app-space-3)">
            <el-button type="primary" style="flex:1" @click="saveEdit">保存</el-button>
            <el-button style="flex:1" @click="cancelEdit">取消</el-button>
          </div>
          <el-button v-else type="primary" :icon="Edit" class="full-btn" style="margin-bottom:var(--app-space-3)" @click="editMode = true">编辑知识</el-button>
        </template>

        <el-divider style="margin:0 0 var(--app-space-3)" />

        <!-- 重建索引进度 -->
        <div v-if="rebuildActive" class="rebuild-progress">
          <div class="rebuild-progress-header">
            <span class="rebuild-progress-title">🔄 重建向量索引</span>
            <span v-if="rebuildDone" class="rebuild-progress-close" @click="rebuildActive = false">✕</span>
          </div>
          <el-progress :percentage="rebuildTotal ? Math.round(rebuildCurrent / rebuildTotal * 100) : 0" :status="rebuildFailed ? 'exception' : rebuildDone ? (rebuildFailCount > 0 ? 'warning' : 'success') : undefined" :stroke-width="14" />
          <div class="rebuild-progress-stats">
            <span v-if="!rebuildDone">{{ rebuildCurrent }} / {{ rebuildTotal }}</span>
            <span v-else-if="rebuildFailed">重建未确认完成，请核对索引状态</span>
            <span v-else>成功 {{ rebuildSuccessCount }}<template v-if="rebuildFailCount"> · 失败 {{ rebuildFailCount }}</template></span>
          </div>
        </div>

        <div class="rag-section">
          <div class="col-title" style="margin-bottom:var(--app-space-4)"><el-icon><Setting /></el-icon> 知识分块配置</div>
          <p>只修改后续索引写入的分块参数。现有索引需重建；检索数量、得分门限、改写与上下文上限在系统设置中配置。</p>
          <div class="rag-field-row"><span class="rag-field-label">Chunk 大小</span><el-input-number v-model="ragForm.chunk_size" :min="100" :max="5000" :step="100" size="small" /></div>
          <div class="rag-field-row"><span class="rag-field-label">重叠大小</span><el-input-number v-model="ragForm.chunk_overlap" :min="0" :max="1000" :step="10" size="small" /></div>
          <div class="rag-field-row"><span class="rag-field-label">Embedding</span><span class="rag-field-value">{{ embeddingModel || '未配置' }}</span></div>
          <div class="rag-btn-group">
            <el-button type="primary" :loading="ragSaving" @click="saveRAG">保存分块配置</el-button>
            <el-button :icon="Refresh" :loading="rebuildLoading" @click="doRebuild">重建向量索引</el-button>
          </div>
          <p v-if="indexStatus" class="index-info">{{ indexStatus.count }} 条向量 · 已建维度 {{ indexStatus.dimension }} · 已建模型 {{ indexStatus.embedding_model || '未标注' }}</p>
          <p v-if="indexStatus?.index_generation">已发布代次：{{ indexStatus.index_generation }}</p>
          <el-alert v-if="indexStatus?.needs_rebuild" :title="indexStatus.rebuild_reason || '需显式重建索引'" type="warning" :closable="false" />
          <p>正文保存后即时用于BM25检索；模型、维度、分块或知识正文改变后，需显式重建完整向量代次。重建完成前保持安全降级。</p>
        </div>
      </div>
    </aside>

    <!-- 添加对话框 -->
    <el-dialog v-model="addVisible" title="添加农业规程与知识" width="620px" destroy-on-close>
      <p>来源、分级与版本均须依据原文填写；标注不等于审核。缺失作物/范围保持未知，新条目默认未核验。</p>
      <el-form ref="addRef" :model="addForm" :rules="addRules" label-width="80px">
        <el-form-item label="问题" prop="question"><el-input v-model="addForm.question" type="textarea" :rows="2" placeholder="如：番茄开花坐果期灰霉病防治技术规程" /></el-form-item>
        <el-form-item label="答案" prop="answer"><el-input v-model="addForm.answer" type="textarea" :rows="4" placeholder="请输入标准规范推荐的防治用药、水肥或管理要点" /></el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="分类" prop="category">
              <el-select v-model="addForm.category" style="width:100%"><el-option v-for="c in categories.filter(c=>c!=='全部')" :key="c" :label="c" :value="c" /></el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="适用作物">
              <el-input v-model="addForm.crop" placeholder="如：番茄" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="生长阶段">
              <el-select v-model="addForm.growth_stage" style="width:100%" placeholder="选择阶段">
                <el-option label="育苗期" value="育苗期" />
                <el-option label="苗期营养生长" value="苗期营养生长" />
                <el-option label="开花坐果期" value="开花坐果期" />
                <el-option label="果实膨大转色期" value="果实膨大转色期" />
                <el-option label="采收盛期" value="采收盛期" />
                <el-option label="全生育期" value="全生育期" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="防治病害">
              <el-input v-model="addForm.disease" placeholder="如：灰霉病" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="分级标注">
              <el-select v-model="addForm.authority_level" style="width:100%">
                <el-option v-for="option in authorityOptions" :key="option.value" :label="option.label" :value="option.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="发布日期">
              <el-input v-model="addForm.publish_date" placeholder="如：2020-05-01" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="来源类型">
          <el-select v-model="addForm.source_type" style="width:100%"><el-option v-for="option in sourceOptions" :key="option.value" :label="option.label" :value="option.value" /></el-select>
        </el-form-item>
        <el-form-item label="来源依据">
          <el-input v-model="addForm.source_name" placeholder="填写原文或产品标签的完整名称" />
        </el-form-item>
        <el-form-item label="适用地域"><el-input v-model="addForm.region" placeholder="未提供请留空" /></el-form-item>
        <el-form-item label="主题"><el-input v-model="addForm.topic" placeholder="未提供请留空" /></el-form-item>
        <el-form-item label="来源版本"><el-input v-model="addForm.version" placeholder="未提供请留空" /></el-form-item>
        <el-form-item label="版本标注"><el-select v-model="addForm.is_current"><el-option label="当前版本标注" :value="1" /><el-option label="非当前版本标注" :value="0" /></el-select></el-form-item>
        <el-form-item label="生效日期"><el-input v-model="addForm.effective_from" placeholder="YYYY-MM-DD；未限定请留空" /></el-form-item>
        <el-form-item label="截止日期"><el-input v-model="addForm.effective_to" placeholder="YYYY-MM-DD；未限定请留空" /></el-form-item>
        <el-form-item label="修订日期"><el-input v-model="addForm.source_updated_at" placeholder="未提供请留空" /></el-form-item>
        <el-form-item label="溯源网址">
          <el-input v-model="addForm.url" placeholder="https://..." />
        </el-form-item>
        <el-form-item label="关键词">
          <div style="display:flex;gap:var(--app-space-2);margin-bottom:6px"><el-input v-model="addKw" placeholder="回车添加" style="width:180px" @keyup.enter="pushAddKw" /><el-button @click="pushAddKw">添加</el-button></div>
          <div style="display:flex;flex-wrap:wrap;gap:var(--app-space-1)"><el-tag v-for="(kw,i) in addForm.keywords" :key="kw" closable @close="addForm.keywords.splice(i,1)">{{ kw }}</el-tag></div>
        </el-form-item>
      </el-form>
      <template #footer><el-button @click="addVisible = false">取消</el-button><el-button type="primary" @click="submitAdd">确定</el-button></template>
    </el-dialog>

    <!-- 导入对话框 -->
    <el-dialog v-model="importVisible" title="导入知识库" width="480px">
      <p>支持导出数组或包含items数组的对象，保留来源、隔离与有效期；缺少来源时保持未核验。导入另建条目并跳过重复问题，不覆盖原ID或登记时间，也不等于备份恢复。</p>
      <el-upload drag :auto-upload="false" :limit="1" accept=".json" :on-change="onFileChange">
        <el-icon class="el-icon--upload"><Upload /></el-icon>
        <div class="el-upload__text">拖拽 JSON 文件到此处，或<em>点击上传</em></div>
      </el-upload>
      <div v-if="importResult" style="max-height:220px;overflow:auto;margin-top:12px" aria-live="polite">
        <el-alert :type="importResult.errors.length ? 'warning' : 'success'" :closable="false" :title="`已导入 ${importResult.success_count} 条，跳过 ${importResult.skip_count} 条，失败 ${importResult.errors.length} 条`" />
        <p v-for="(error,index) in importResult.errors" :key="index">{{ error }}</p>
      </div>
      <template #footer><el-button @click="importVisible = false">取消</el-button><el-button type="primary" :loading="importLoading" @click="doImport">导入</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.three-col { display: flex; height: calc(100vh - 60px); background: var(--app-bg); overflow: hidden; }
.col { display: flex; flex-direction: column; overflow: hidden; }
.col-left  { width: 200px; min-width: 200px; background: var(--app-surface); border-right: 1px solid var(--app-border-light); }
.col-center { flex: 1; min-width: 0; border-right: 1px solid var(--app-border-light); background: var(--app-surface); }
.col-right { width: 380px; min-width: 340px; background: var(--app-surface); }

.col-header { display: flex; align-items: center; gap: var(--app-space-2); padding: var(--app-space-3) var(--app-space-4); border-bottom: 1px solid var(--app-border-light); flex-shrink: 0; }
.col-title { display: flex; align-items: center; gap: 6px; font-size: var(--app-text-base); font-weight: 600; color: var(--app-text); }
.col-footer { display: flex; align-items: center; justify-content: space-around; padding: var(--app-space-3); border-top: 1px solid var(--app-border-light); flex-shrink: 0; }
.col-footer.center { justify-content: center; }

/* 分类 */
.cat-nav { flex: 1; overflow-y: auto; padding: var(--app-space-1) 0; }
.cat-item { display: flex; align-items: center; gap: var(--app-space-1); padding: 6px var(--app-space-3); font-size: var(--app-text-sm); color: var(--app-text-secondary); transition: var(--app-transition); }
.cat-item:hover { background: var(--app-hover); }
.cat-item:hover .cat-ops { opacity: 1; }
.cat-item.active { color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.cat-item.active .cat-label { font-weight: 500; }
.cat-label { display: flex; align-items: center; gap: var(--app-space-2); flex: 1; cursor: pointer; padding: 2px 0; }
.cat-ops { display: flex; gap: 0; opacity: 0; transition: opacity .15s; flex-shrink: 0; }
.cat-add { padding: var(--app-space-2) var(--app-space-3); border-top: 1px solid var(--app-border-light); display: flex; gap: 6px; flex-wrap: wrap; }

/* 列表 */
.item-list { flex: 1; overflow-y: auto; padding: var(--app-space-3); }
.item-card { padding: var(--app-space-3) var(--app-space-4); border-radius: var(--app-radius-md); margin-bottom: 6px; cursor: pointer; border: 1px solid transparent; transition: var(--app-transition); }
.item-card:hover { background: var(--app-hover); }
.item-card.active { background: var(--el-color-primary-light-9); border-color: var(--el-color-primary-light-5); }
.item-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--app-space-1); }
.item-id { font-size: var(--app-text-xs); color: var(--app-text-placeholder); font-family: monospace; }
.item-q { font-size: var(--app-text-sm); color: var(--app-text); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin: 0 0 6px; }
.item-bottom { display: flex; justify-content: space-between; align-items: center; }
.item-kws { display: flex; align-items: center; gap: var(--app-space-1); }
.more-kw { font-size: var(--app-text-xs); color: var(--app-text-secondary); }

/* 右栏 */
.detail-scroll { flex: 1; overflow-y: auto; padding: var(--app-space-3) var(--app-space-4); }
.empty-detail { flex: 1; display: flex; align-items: center; justify-content: center; }

.right-fixed-bottom { flex-shrink: 0; padding: var(--app-space-3) var(--app-space-4); border-top: 1px solid var(--app-border-light); max-height: 50%; overflow-y: auto; }

.detail-block { margin-bottom: var(--app-space-3); }
.detail-block :deep(.el-card__header) { padding: var(--app-space-3) var(--app-space-4); font-size: var(--app-text-sm); }
.detail-block :deep(.el-card__body) { padding: var(--app-space-3) var(--app-space-4); }
.meta-line { margin: 0 0 6px; font-size: var(--app-text-sm); color: var(--app-text-secondary); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.meta-label { font-weight: 600; min-width: 48px; color: var(--app-text); }
.block-title { font-size: var(--app-text-sm); font-weight: 600; }
.block-text { margin: 0; font-size: var(--app-text-sm); line-height: 1.65; color: var(--app-text); }
.block-text.pre { white-space: pre-wrap; }
.text-muted { color: var(--app-text-placeholder); font-size: var(--app-text-sm); }

.field-row { display: flex; align-items: center; gap: var(--app-space-3); margin-bottom: var(--app-space-2); }
.field-label { font-size: var(--app-text-sm); color: var(--app-text-secondary); min-width: 72px; white-space: nowrap; }
.action-bar { display: flex; gap: var(--app-space-2); }
.full-btn { width: 100%; }

.rag-btn-group { display: flex; flex-direction: column; gap: var(--app-space-2); margin-top: var(--app-space-3); }
.rag-btn-group .el-button { width: 100%; margin-left: 0; }
.rag-field-row { display: flex; align-items: center; gap: var(--app-space-3); margin-bottom: var(--app-space-4); }
.rag-field-label { font-size: var(--app-text-base); color: var(--app-text-secondary); min-width: 80px; white-space: nowrap; line-height: 1.8; }
.rag-field-value { font-size: var(--app-text-base); color: var(--app-text-placeholder); line-height: 1.8; }
.index-info { margin: var(--app-space-2) 0 0; font-size: var(--app-text-xs); color: var(--app-text-secondary); text-align: center; }

/* 重建索引进度 */
.rebuild-progress { margin-bottom: var(--app-space-4); padding: var(--app-space-3); background: var(--app-surface-2); border-radius: var(--app-radius-md); }
.rebuild-progress-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--app-space-2); }
.rebuild-progress-title { font-size: var(--app-text-sm); font-weight: 600; }
.rebuild-progress-close { cursor: pointer; color: var(--app-text-placeholder); font-size: var(--app-text-base); }
.rebuild-progress-close:hover { color: var(--app-text); }
.rebuild-progress-stats { margin-top: 6px; font-size: var(--app-text-xs); color: var(--app-text-secondary); text-align: center; }

/* 农业元数据展示样式 */
.item-agri-meta { display: flex; flex-wrap: wrap; gap: var(--app-space-1); margin-bottom: 6px; }
.agri-tag { font-size: var(--app-text-xs); background: var(--app-hover); color: var(--el-color-success-dark-2); padding: 1px 6px; border-radius: var(--app-radius-sm); }
.meta-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--app-space-2) var(--app-space-3); }
.meta-grid .full { grid-column: span 2; }
.url-link { color: var(--el-color-primary); text-decoration: none; word-break: break-all; font-size: var(--app-text-xs); }
.url-link:hover { text-decoration: underline; }

/* 移动端响应式适配 */
@media (max-width: 900px) {
  .three-col {
    flex-direction: column;
    height: auto;
    overflow-y: auto;
  }
  .col-left {
    width: 100%;
    min-width: 100%;
    max-height: 320px;
    border-right: none;
    border-bottom: 1px solid var(--app-border-light);
  }
  .col-center {
    width: 100%;
    min-width: 100%;
    min-height: 350px;
    border-right: none;
    border-bottom: 1px solid var(--app-border-light);
  }
  .col-right {
    width: 100%;
    min-width: 100%;
  }
  /* 移动端堆叠布局下父高为 auto，50% 限高会产生内层滚动条裁切表单，放开 */
  .right-fixed-bottom {
    max-height: none;
    overflow: visible;
  }
}
</style>
