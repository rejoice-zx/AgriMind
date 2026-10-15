import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'

const require = createRequire(new URL('../package.json', import.meta.url))
const ts = require('typescript'), vue = require('vue')
const { parse, compileScript, compileTemplate } = require('@vue/compiler-sfc')
const { renderToString } = require('@vue/server-renderer')
function execute(source, dependencies) {
  const module = { exports: {} }
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true
  } }).outputText
  new Script(code).runInContext(createContext({ module, exports: module.exports, console,
    require: name => { if (name in dependencies) return dependencies[name]; throw Error(name) } }))
  return module.exports
}
const markdown = execute(readFileSync(new URL('../src/utils/safeMarkdown.ts', import.meta.url), 'utf8'), {
  'markdown-it': require('markdown-it')
})
const render = markdown.renderSafeMarkdown

test('renders headings, nested lists, quotes and tables', () => {
  const html = render('# 田间记录\n\n1. 巡田\n   - 检查叶片\n\n> 待复核\n\n| 项目 | 状态 |\n| --- | --- |\n| 天气 | 待核实 |')
  for (const tag of ['h1', 'ol', 'ul', 'blockquote', 'table', 'th', 'td']) assert.match(html, new RegExp(`<${tag}[ >]`))
  assert.match(html, /检查叶片/)
})

test('code blocks preserve literal Markdown and line breaks', () => {
  const html = render('```html\n<strong>**原文**</strong>\n[文字](https://example.com)\n```\n\n`**不加粗**`')
  assert.match(html, /<pre><code class="language-html">&lt;strong&gt;\*\*原文\*\*&lt;\/strong&gt;\n/)
  assert.match(html, /<code>\*\*不加粗\*\*<\/code>/)
  assert.doesNotMatch(html, /<strong>|<a |<br>/)
})

test('model HTML and dangerous or obfuscated protocols cannot become active markup', () => {
  for (const input of ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
    '[点击](javascript:alert%281%29)', '[点击](jav&#x61;script:alert%281%29)',
    '[点击](vbscript:run)', '[点击](data:text/html,test)', '[点击](file:///local)', '[点击](//outside.example/a)']) {
    assert.doesNotMatch(render(input), /<(?:script|img|iframe|a)\b/i, input)
  }
})

test('uploaded photos remain separate from Markdown and safe links retain protection', () => {
  assert.doesNotMatch(render('![现场照片](https://outside.example/tracker.png)'), /<img\b/i)
  for (const href of ['https://example.com', 'mailto:expert@example.com', '/diagnosis', '#detail']) {
    const html = render(`[资料](${href})`)
    assert.match(html, /<a /); assert.match(html, /rel="noopener noreferrer nofollow"/)
  }
  assert.equal(render(''), '')
})

async function renderView(file, prepare) {
  const { descriptor } = parse(readFileSync(new URL(file, import.meta.url), 'utf8'))
  const noOp = () => {}
  const dependencies = {
    vue: { ...vue, onMounted: noOp, onBeforeUnmount: noOp },
    'element-plus': { ElMessage: { error: noOp, success: noOp } }, '@element-plus/icons-vue': {},
    '@/api/agriculture': {}, '@/api/agriHistory': {}, '@/utils/requestId': {}, '@/utils/safeMarkdown': markdown,
    '@/utils/scopedDraft': { draftOwnerKey: () => 'synthetic-markdown-owner', saveScopedDraft: () => true, readScopedDraft: () => null },
    '@/stores/auth': { useAuthStore: () => ({ user: { id: 1, role: 'expert' } }) },
    pinia: { storeToRefs: vue.toRefs },
    '@/stores/farmSelection': { useFarmSelectionStore: () => vue.reactive({ farms: [], selectedFarmId: null }) },
    '@/components/AuthenticatedImage.vue': {}, '@/components/FieldWeatherPanel.vue': {}, '@/components/AgriHistoryPanel.vue': {},
    '@/components/ConsultationReviewEditor.vue': {}
  }
  const component = execute(compileScript(descriptor, { id: 'markdown-view' }).content, dependencies).default
  const template = compileTemplate({ source: descriptor.template.content, filename: file,
    id: 'markdown-view', compilerOptions: { expressionPlugins: ['typescript'] } })
  assert.deepEqual(template.errors, [])
  const app = vue.createSSRApp({
    setup() { const state = component.setup({}, { expose: noOp, emit: noOp }); prepare(state); return { ...state } },
    render: execute(template.code, { vue }).render
  })
  // The queue table's scoped row slot is not part of the selected detail under test.
  app.component('ElTable', { render: () => null })
  app.config.warnHandler = noOp
  return renderToString(app)
}

const decision = { conclusion: '## 决策结论', risks: ['**风险提示**'], recommendations: ['- 检查叶片'],
  evidences: [{ title: '资料', summary: '| 项目 | 状态 |\n| --- | --- |\n| 温度 | 待核实 |' }],
  confidence: 0.5, can_proceed: false, tool_traces: [], need_expert: true, turn_id: 'turn' }

test('actual decision card renders conclusion, risk, recommendation, evidence and expert reply', async () => {
  const html = await renderView('../src/views/AgriChat.vue', state => {
    state.messages.value = [{ id: 'turn', role: 'agent', decision,
      consultation: { id: 'case', status: 'resolved', answer: '> 专家答复' } }]
  })
  assert.match(html, /<h2>决策结论<\/h2>/)
  assert.match(html, /<strong>风险提示<\/strong>/)
  assert.match(html, /<li>检查叶片<\/li>/)
  assert.match(html, /<table>/)
  assert.match(html, /<blockquote>\s*<p>专家答复<\/p>/)
})

test('expert consultation detail uses the same formatting for persisted analysis and answer', async () => {
  const html = await renderView('../src/components/AgriConsultationQueue.vue', state => {
    state.selected.value = { id: 'case', query: '问题', status: 'resolved', decision, answer: '## 已核实答复' }
  })
  assert.match(html, /<h2>决策结论<\/h2>/)
  assert.match(html, /<h2>已核实答复<\/h2>/)
  assert.match(html, /<table>/)
})
