import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const vue = require('vue'), ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const url = '/api/diagnosis/media/' + 'a'.repeat(32) + '.png'
const flush = async () => { await vue.nextTick(); await new Promise(resolve => setImmediate(resolve)) }
function script(source, dependencies, extras = {}) {
  const module = { exports: {} }
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  new Script(code).runInContext(createContext({ module, exports: module.exports, ...extras, require: name => {
    if (name in dependencies) return dependencies[name]
    throw Error(name)
  } }))
  return module.exports
}
function image(fetch) {
  const { descriptor } = parse(readFileSync(new URL('../src/components/AuthenticatedImage.vue', import.meta.url), 'utf8'))
  const props = vue.reactive({ src: url, alt: 'photo' })
  const auth = vue.reactive({ sessionVersion: 1, user: { id: 1, role: 'customer' } })
  const hooks = [], created = [], revoked = []
  const component = script(compileScript(descriptor, { id: 'private-image' }).content, {
    vue: { ...vue, onBeforeUnmount: fn => hooks.push(fn) },
    '@/stores/auth': { useAuthStore: () => auth }, '@/api/diagnosisMedia': { readDiagnosisImage: fetch }
  }, { URL: { createObjectURL: data => { const id = `blob:${created.length + 1}`; created.push(id); return id }, revokeObjectURL: id => revoked.push(id) } })
  const scope = vue.effectScope()
  const state = scope.run(() => component.default.setup(props, { expose() {} }))
  return { props, auth, state, created, revoked, unmount: () => { hooks.forEach(fn => fn()); scope.stop() } }
}

test('image reader uses authenticated client with a same-origin blob response and rejects arbitrary URLs', async () => {
  const calls = []
  const api = script(readFileSync(new URL('../src/api/diagnosisMedia.ts', import.meta.url), 'utf8'), {
    '@/utils/request': { default: { get: async (...args) => { calls.push(args); return { data: {} } } } }
  })
  await api.readDiagnosisImage(url)
  assert.equal(calls[0][0], url)
  assert.equal(calls[0][1].baseURL, '/')
  assert.equal(calls[0][1].responseType, 'blob')
  for (const invalid of ['https://outside.test/leaf.png', '/static/uploads/diagnosis/../leaf.png', url + '?access=anything', '/api/diagnosis/media/' + 'a'.repeat(32) + '.svg']) {
    await assert.rejects(api.readDiagnosisImage(invalid))
  }
  assert.equal(calls.length, 1)
})

test('image changes revoke old blobs and late bytes cannot replace current photo', async () => {
  const pending = new Map()
  const env = image(src => new Promise(resolve => pending.set(src, resolve)))
  const second = url.replace('a'.repeat(32), 'b'.repeat(32))
  env.props.src = second; await vue.nextTick()
  pending.get(second)({ data: { type: 'image/png' } }); await flush()
  assert.equal(env.state.objectUrl.value, 'blob:1')
  pending.get(url)({ data: { type: 'image/png' } }); await flush()
  assert.equal(env.created.length, 1)
  env.props.src = null; await flush()
  assert.equal(env.state.objectUrl.value, '')
  assert.deepEqual(env.revoked, ['blob:1'])
  env.unmount()
})

test('logout clears displayed pixels and rejects an in-flight old-account response', async () => {
  let finish
  const env = image(() => new Promise(resolve => { finish = resolve }))
  env.auth.user = null; env.auth.sessionVersion++; await vue.nextTick()
  finish({ data: { type: 'image/png' } }); await flush()
  assert.equal(env.state.objectUrl.value, '')
  assert.equal(env.created.length, 0)
  env.unmount()
})

test('role change and unmount release pixels; failed fetch does not retain an earlier image', async () => {
  let failed = false
  const env = image(async () => { if (failed) throw Error('denied'); return { data: { type: 'image/png' } } })
  await flush(); assert.equal(env.state.objectUrl.value, 'blob:1')
  failed = true; env.auth.user.role = 'cs'; await flush()
  assert.equal(env.state.objectUrl.value, '')
  assert.deepEqual(env.revoked, ['blob:1'])
  env.unmount()
  assert.equal(env.state.loading.value, false)
})
