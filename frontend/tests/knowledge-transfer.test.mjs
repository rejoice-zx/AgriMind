import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
import { sharedModule } from './shared-modules.mjs'
const require = createRequire(new URL('../package.json', import.meta.url))
const vue = require('vue'), ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const { descriptor } = parse(readFileSync(new URL('../src/views/Knowledge.vue', import.meta.url), 'utf8'))
const code = ts.transpileModule(compileScript(descriptor, { id: 'knowledge-transfer' }).content,
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const native = value => JSON.parse(JSON.stringify(value))

function harness(overrides = {}) {
  const messages = [], downloads = [], blobs = [], module = { exports: {} }, scope = vue.effectScope()
  const api = { getKnowledgeList: async () => ({ data: { items: [], total: 0 } }),
    getKnowledgeCategories: async () => ({ data: { categories: [] } }), getIndexStatus: async () => ({ data: {} }), ...overrides }
  new Script(code).runInContext(createContext({ module, exports: module.exports, TextDecoder,
    Blob: class { constructor(parts) { blobs.push(parts.join('')) } },
    document: { createElement: () => ({ click() { downloads.push({ href: this.href, download: this.download }) } }) },
    URL: { createObjectURL: () => 'blob:synthetic', revokeObjectURL() {} },
    require: name => {
      if (name === 'vue') return { ...vue, onMounted() {} }
      if (name === 'element-plus') return { ElMessage: Object.fromEntries(['success','error','warning'].map(level => [level, text => messages.push({ level, text })])), ElMessageBox: { confirm: async () => true } }
      if (name === '@/api/knowledge') return api
      if (name === '@/api/settings' || name === '@element-plus/icons-vue' || name === '@/components/PageHeader.vue') return {}
      const shared = sharedModule(name); if (shared) return shared
      throw Error(name)
    }
  }))
  const state = scope.run(() => module.exports.default.setup({}, { expose() {} }))
  return { state, messages, downloads, blobs, dispose() { scope.stop() } }
}

test('new and existing knowledge keep missing scope unknown and preserve quarantine on edit', async () => {
  let sent
  const env = harness({ updateKnowledge: async (id, body) => {
    sent = { id, body: native(body) }; return { data: { ...body, id, version: null } }
  } })
  env.state.openAdd()
  assert.equal(env.state.addForm.crop, ''); assert.equal(env.state.addForm.growth_stage, '')
  assert.equal(env.state.addForm.source_type, 'unverified'); assert.equal(env.state.addForm.authority_level, '未核验')
  env.state.selectItem({ id: 'missing', question: 'synthetic', answer: 'synthetic', keywords: [], category: 'synthetic' })
  assert.equal(env.state.form.crop, ''); assert.equal(env.state.form.authority_level, '未核验')
  assert.equal(env.state.form.is_current, undefined)
  const item = { id: 'isolated', question: 'synthetic isolated', answer: 'synthetic', category: 'synthetic', keywords: [],
    source_type: 'quarantined', region: 'synthetic-region', version: 'source-v2', is_current: 0,
    effective_from: '2020-01-01', effective_to: '2021-01-01', source_updated_at: '2020-12-01' }
  env.state.list.value = [item]; env.state.selectItem(item)
  env.state.form.answer = 'synthetic corrected'; await env.state.saveEdit()
  assert.equal(sent.body.source_type, 'quarantined'); assert.equal(sent.body.is_current, 0)
  assert.equal(sent.body.region, item.region); assert.equal(sent.body.effective_to, item.effective_to)
  assert.equal(env.state.selected.value.version, null) // show the server receipt, not an invented client value
  env.dispose()
})

test('partial import accepts an exported envelope and displays real counts and row errors', async () => {
  const original = [{ question: 'synthetic isolated', answer: 'synthetic', source_type: 'quarantined', effective_to: '2021-01-01' }]
  let sent
  const env = harness({ importKnowledge: async data => {
    sent = native(data); return { data: { success_count: 1, skip_count: 2, errors: ['第4条: 日期无效'] } }
  } })
  env.state.importVisible.value = true
  env.state.onFileChange({ raw: { text: async () => JSON.stringify({ items: original, total: 1 }) } })
  await env.state.doImport()
  assert.deepEqual(sent, original); assert.equal(env.state.importResult.value.skip_count, 2)
  assert.equal(env.state.importResult.value.errors[0], '第4条: 日期无效')
  assert.ok(env.messages.some(message => message.level === 'warning' && message.text.includes('失败 1 条')))
  assert.ok(!env.messages.some(message => message.level === 'success'))
  assert.equal(env.state.importVisible.value, true); assert.equal(env.state.importLoading.value, false)
  env.dispose()
})

test('download keeps exported agricultural metadata and quarantine intact', async () => {
  const items = [{ id: 'synthetic', question: 'synthetic', answer: 'synthetic', source_type: 'quarantined', is_current: 0,
    effective_from: '2020-01-01', effective_to: '2021-01-01', crop: null, region: 'synthetic', meta_info: { original: 'synthetic' } }]
  const env = harness({ exportKnowledge: async () => ({ data: { items, total: 1 } }) })
  await env.state.doExport()
  assert.deepEqual(JSON.parse(env.blobs[0]), items)
  assert.equal(env.downloads.length, 1); assert.match(env.downloads[0].download, /^knowledge_.*\.json$/)
  env.dispose()
})

test('a late edit receipt updates its own list row without replacing a new selection', async () => {
  let finish
  const env = harness({ updateKnowledge: () => new Promise(resolve => { finish = resolve }) })
  const old = { id: 'old', question: 'old synthetic', answer: 'old', keywords: [], category: 'synthetic', source_type: 'quarantined' }
  const current = { ...old, id: 'current', question: 'current synthetic' }
  env.state.list.value = [old, current]; env.state.selectItem(old)
  const saving = env.state.saveEdit(); env.state.selectItem(current)
  finish({ data: { ...old, answer: 'saved old' } }); await saving
  assert.equal(env.state.selected.value.id, 'current')
  assert.equal(env.state.list.value[0].answer, 'saved old'); assert.equal(env.state.form.question, 'current synthetic')
  env.dispose()
})

test('index rebuild requires a completion receipt and never calls errors or an early end successful', async () => {
  for (const event of [{ type: 'error', message: 'synthetic refused', fail_count: 1 }, { type: 'progress', current: 1, total: 2 }, { type: 'done', success_count: 2, fail_count: 0 }]) {
    let read = false
    const env = harness({ rebuildIndexSSE: async () => ({ read: async () => {
      if (read && event.type === 'done') throw Error('Do not wait for transport closure after a completed receipt')
      if (read) return { done: true }
      read = true
      return { done: false, value: new TextEncoder().encode('data: ' + JSON.stringify(event) + '\n\n') }
    } }) })
    await env.state.doRebuild()
    assert.equal(env.state.rebuildFailed.value, event.type !== 'done')
    assert.equal(env.state.rebuildLoading.value, false)
    assert.equal(env.messages.some(message => message.level === 'error'), event.type !== 'done')
    assert.equal(env.messages.some(message => message.level === 'success'), event.type === 'done')
    env.dispose()
  }
})

test('each knowledge row states whether it can take part in retrieval', () => {
  const env = harness({})
  const state = env.state
  assert.match(state.retrievalState({ is_current: 1, source_type: 'quarantined' }).label, /隔离 · 不参与检索/)
  assert.match(state.retrievalState({ is_current: 1, source_type: 'unverified' }).label, /未核验/)
  assert.match(state.retrievalState({ is_current: 1, source_type: 'literature', effective_to: '2000-01-01' }).label, /已过期/)
  assert.match(state.retrievalState({ is_current: 0, source_type: 'literature' }).label, /非当前版本/)
  assert.match(state.retrievalState({ is_current: 1, source_type: 'literature' }).label, /可参与检索/)
  env.dispose()
})
