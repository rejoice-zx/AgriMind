import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const ts = require('typescript')
function load(file, dependencies = {}, globals = {}) {
  const module = { exports: {} }
  const code = ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText
  new Script(code).runInContext(createContext({ module, exports: module.exports, ...globals, require: name => dependencies[name] }))
  return module.exports
}
test('history reads later pages and preserves all 205 records', async () => {
  const offsets = []
  const api = load('../src/api/agriculture.ts', { '@/utils/request': { default: {
    get: async (_, { params }) => {
      offsets.push(params.skip)
      return { data: Array.from({ length: Math.min(100, 205 - params.skip) }, (_, i) => ({ id: params.skip + i })) }
    }
  } } })
  const result = await api.getFarmingRecords(1, 'all')
  assert.equal(result.data.length, 205)
  assert.deepEqual(offsets, [0, 100, 200])
  assert.equal(result.data.at(-1).id, 204)
})
test('422 detail identifies fields without echoing input values', () => {
  const { validationMessage } = load('../src/utils/validationMessage.ts')
  assert.match(validationMessage([{ loc: ['body', 'operation_date'], input: 'private-user-input' }]), /作业日期/)
  assert.doesNotMatch(validationMessage([{ loc: ['body', 'unexpected'], input: 'private-user-input' }]), /private-user-input/)
})

test('record drafts survive a reload but stay scoped to account and field', () => {
  const storage = new Map()
  const globals = { localStorage: { getItem: k => storage.get(k) || null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) } }
  const first = load('../src/utils/farmingDraft.ts', {}, globals)
  assert.equal(first.saveFarmingDraft(1, { field_id: 2, operation_date: '2026-10-14', operation_content: '待核对' }), true)
  const reloaded = load('../src/utils/farmingDraft.ts', {}, globals)
  assert.equal(reloaded.readFarmingDraft(1, 2).operation_content, '待核对')
  assert.equal(reloaded.readFarmingDraft(2, 2), null)
  assert.equal(reloaded.readFarmingDraft(1, 3), null)
  reloaded.clearFarmingDraft(1, 2)
  assert.equal(reloaded.readFarmingDraft(1, 2), null)
})

test('pending requests survive reload and a reused account id cannot read the old draft', () => {
  const storage = new Map()
  const globals = { localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) } }
  const first = load('../src/utils/scopedDraft.ts', {}, globals)
  const owner = first.draftOwnerKey({ id: 1, username: 'owner', created_at: '2026-01-01' })
  const payload = { query: 'draft', pending: { request_id: 'fixed-request-id', field_id: 12 } }
  first.saveScopedDraft(owner, 'chat:12:3', payload)
  const reloaded = load('../src/utils/scopedDraft.ts', {}, globals)
  assert.equal(reloaded.readScopedDraft(owner, 'chat:12:3').pending.request_id, 'fixed-request-id')
  assert.equal(reloaded.readScopedDraft(owner, 'chat:13:3'), null)
  const replacementOwner = first.draftOwnerKey({ id: 1, username: 'owner', created_at: '2026-10-14' })
  assert.equal(reloaded.readScopedDraft(replacementOwner, 'chat:12:3'), null)
})
