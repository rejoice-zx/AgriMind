import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript')
const vue = require('vue')
function setup(view, overrides = {}) {
  const { descriptor } = parse(readFileSync(new URL(`../src/views/${view}.vue`, import.meta.url), 'utf8'))
  const compiled = ts.transpileModule(compileScript(descriptor, { id: `rag-${view}` }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const calls = [], messages = []
  const api = { getSettings: async () => ({ data: { rag: { top_k: 7, similarity_threshold: 0, use_rewrite: false, max_context_length: 1500, chunk_size: 1000, chunk_overlap: 100 } } }),
    getLLMProviders: async () => ({ data: { providers: [] } }),
    updateSettings: async data => { calls.push(data); return { data } }, ...overrides }
  const module = { exports: {} }
  new Script(compiled).runInContext(createContext({ module, exports: module.exports, require: name => {
    if (name === '@/components/PageHeader.vue') return {}
    if (name === 'vue') return { ...vue, onMounted() {}, onBeforeUnmount() {} }
    if (name === 'element-plus') return { ElMessage: Object.fromEntries(['success', 'error', 'warning'].map(level => [level, text => messages.push({ level, text })])), ElMessageBox: {} }
    if (name === '@element-plus/icons-vue' || name === '@/api/knowledge') return {}
    if (name === '@/api/settings') return api
    throw Error(name)
  } }))
  return { state: module.exports.default.setup({}, { expose() {} }), calls, messages }
}

test('saved zero threshold and disabled rewriting survive page load and save', async () => {
  const { state, calls } = setup('Settings')
  await state.loadSettings()
  assert.equal(state.ragConfig.similarityThreshold, 0)
  assert.equal(state.ragConfig.useRewrite, false)
  assert.equal(state.ragConfig.topK, 7)
  await state.saveRAGSettings()
  assert.equal(calls[0].rag.similarity_threshold, 0)
  assert.equal(calls[0].rag.use_rewrite, false)
  assert.equal(calls[0].rag.max_context_length, 1500)
  assert.equal(state.saving.value, false)
})

test('knowledge page only submits chunk fields and makes rebuild requirement explicit', async () => {
  const { state, calls, messages } = setup('Knowledge')
  await state.loadRAGConfig()
  state.ragForm.chunk_overlap = 120
  await state.saveRAG()
  assert.deepEqual(Object.keys(calls[0].rag).sort(), ['chunk_overlap', 'chunk_size'])
  assert.equal(calls[0].rag.chunk_size, 1000)
  assert.equal(calls[0].rag.chunk_overlap, 120)
  assert.ok(messages.some(message => message.level === 'success' && message.text.includes('索引需重建')))
})

test('failed retrieval settings save does not present a success', async () => {
  const { state, messages } = setup('Settings', { updateSettings: async () => { throw Error('controlled rejection') } })
  await state.saveRAGSettings()
  assert.equal(state.saving.value, false)
  assert.ok(messages.some(message => message.level === 'error'))
  assert.ok(!messages.some(message => message.level === 'success'))
})
test('a failed index read clears the prior apparently healthy generation', async () => {
  const { state, messages } = setup('Knowledge', { getIndexStatus: async () => { throw Error('controlled index read failure') } })
  state.indexStatus.value = { embedding_model: 'old-model', count: 10, needs_rebuild: false }
  await state.loadIndexStatus()
  assert.equal(state.indexStatus.value, null)
  assert.ok(messages.some(message => message.level === 'error'))
})
