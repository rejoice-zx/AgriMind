import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'

const require = createRequire(new URL('../package.json', import.meta.url))
const ts = require('typescript')
const axios = require('axios')
const compile = file => ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

function harness(adapter, { guest = false, fetchImpl, publicPage = false } = {}) {
  const storage = new Map(guest ? [['guest_token', 'guest-old'], ['visitor_id', 'visitor-old'], ['guest_conversation_ids', '["c1"]']] : [])
  const events = []
  const store = {
    user: guest ? null : { id: 1 }, token: guest ? 'guest-old' : 'account-old',
    refreshToken: guest ? null : 'refresh-1', sessionVersion: 1,
    setToken(value) { this.token = value },
    clearAuth() { this.sessionVersion++; this.user = null; this.token = null; this.refreshToken = null; events.push('clear') },
  }
  const localStorage = { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) }
  const imports = {
    axios,
    '@/stores/auth': { useAuthStore: () => store },
    '@/utils/guestIdentity': { getOrCreateGuestDeviceId: () => 'fixture-device' },
    'element-plus': { ElMessage: { error: () => events.push('toast') } },
    '@/router': { __esModule: true, default: { currentRoute: { value: { meta: { public: publicPage } } }, push: () => events.push('redirect') } },
  }
  const load = file => {
    const module = { exports: {} }
    new Script(compile(file)).runInContext(createContext({
      module, exports: module.exports, localStorage, console, Headers,
      fetch: fetchImpl ?? (() => { throw Error('unexpected real fetch') }),
      require: name => { if (!(name in imports)) throw Error(`Unexpected dependency: ${name}`); return imports[name] },
    }))
    return module.exports
  }
  const oldAdapter = axios.defaults.adapter
  axios.defaults.adapter = adapter
  const auth = load('../src/utils/authRefresh.ts')
  imports['@/utils/authRefresh'] = auth
  imports['@/utils/validationMessage'] = load('../src/utils/validationMessage.ts')
  const request = load('../src/utils/request.ts').default
  return { store, storage, events, auth, request, dispose: () => { axios.defaults.adapter = oldAdapter } }
}
const response = (config, data = {}) => ({ status: 200, data, headers: {}, config })
const unauthorized = config => { throw new axios.AxiosError('fixture 401', 'ERR_BAD_REQUEST', config, {}, { status: 401, data: {}, headers: {}, config }) }

test('a second axios 401 runs the instance cleanup and never refreshes a third time', async () => {
  let requests = 0, refreshes = 0
  const env = harness(async config => {
    if (config.url === '/api/auth/refresh') { refreshes++; return response(config, { access_token: 'account-new' }) }
    requests++; return unauthorized(config)
  })
  try {
    await assert.rejects(env.request.post('/chat/fixture', {}))
    assert.equal(requests, 2); assert.equal(refreshes, 1)
    assert.equal(env.store.token, null); assert(env.events.includes('redirect'))
  } finally { env.dispose() }
})

test('a rejected guest is replaced once, request retries, and device/history identity stays', async () => {
  let requests = 0, issued = 0
  const env = harness(async config => {
    if (config.url === '/api/auth/guest') { issued++; return response(config, { access_token: 'guest-new', visitor_id: 'visitor-new' }) }
    requests++
    if (config.headers.Authorization === 'Bearer guest-old') return unauthorized(config)
    return response(config, { items: [] })
  }, { guest: true, publicPage: true })
  try {
    await env.request.get('/chat/conversations')
    assert.equal(issued, 1); assert.equal(requests, 2)
    assert.equal(env.store.token, 'guest-new'); assert.equal(env.storage.get('guest_token'), 'guest-new')
    assert.equal(env.storage.get('guest_conversation_ids'), '["c1"]')
    await env.request.get('/chat/conversations'); assert.equal(issued, 1)
  } finally { env.dispose() }
})

test('wrong login password preserves signed guest proof for a later successful claim', async () => {
  let issues = 0
  const env = harness(async config => { if (config.url === '/api/auth/guest') issues++; return unauthorized(config) }, { guest: true, publicPage: true })
  try {
    await assert.rejects(env.request.post('/auth/login', { username: 'fixture' }))
    assert.equal(issues, 0); assert.equal(env.storage.get('guest_token'), 'guest-old')
    assert.equal(env.storage.get('visitor_id'), 'visitor-old')
    assert.equal(env.storage.get('guest_conversation_ids'), '["c1"]')
  } finally { env.dispose() }
})

test('refresh is shared and logout while pending cannot resurrect the old session', async () => {
  const pending = deferred(); let refreshes = 0
  const env = harness(async config => { refreshes++; await pending.promise; return response(config, { access_token: 'account-new' }) })
  try {
    const a = env.auth.refreshAccessToken(), b = env.auth.refreshAccessToken()
    assert.equal(a, b)
    env.store.clearAuth(); pending.resolve()
    await assert.rejects(a, env.auth.AuthSessionChangedError)
    await assert.rejects(b, env.auth.AuthSessionChangedError)
    assert.equal(refreshes, 1); assert.equal(env.store.token, null)
  } finally { env.dispose() }
})

test('an old refresh failure cannot clear a newer login', async () => {
  const pending = deferred()
  const env = harness(async config => {
    if (config.url === '/api/auth/refresh') { await pending.promise; return response(config, { access_token: 'old-user-new' }) }
    return unauthorized(config)
  })
  try {
    const request = env.request.get('/chat/conversations')
    await new Promise(resolve => setImmediate(resolve))
    env.store.sessionVersion++; env.store.token = 'other-account'; env.store.refreshToken = 'other-refresh'
    pending.resolve(); await assert.rejects(request)
    assert.equal(env.store.token, 'other-account'); assert(!env.events.includes('clear'))
  } finally { env.dispose() }
})

test('a rejected pending refresh cannot clear a newer login either', async () => {
  const pending = deferred()
  const env = harness(async config => {
    if (config.url === '/api/auth/refresh') await pending.promise
    return unauthorized(config)
  })
  try {
    const request = env.request.get('/chat/conversations')
    await new Promise(resolve => setImmediate(resolve))
    env.store.sessionVersion++; env.store.token = 'other-account'; env.store.refreshToken = 'other-refresh'
    pending.resolve(); await assert.rejects(request)
    assert.equal(env.store.token, 'other-account'); assert(!env.events.includes('clear'))
  } finally { env.dispose() }
})

test('fetch refresh failure clears only the session which requested the refresh', async () => {
  const env = harness(async config => unauthorized(config), { fetchImpl: async () => ({ status: 401 }) })
  try {
    assert.equal((await env.auth.fetchStreamWithAuthRetry('/api/chat/messages')).status, 401)
    assert.equal(env.store.token, null)
  } finally { env.dispose() }
})

test('concurrent fetch 401s refresh once and POST is retried only before a successful stream', async () => {
  const pending = deferred(); let refreshes = 0, fetches = 0, writes = 0
  const env = harness(async config => { refreshes++; await pending.promise; return response(config, { access_token: 'account-new' }) }, {
    fetchImpl: async (_url, init) => {
      fetches++
      if (init.headers.Authorization === 'Bearer account-old') return { status: 401 }
      writes++; return { status: 200, body: { fixture: 'started stream' } }
    },
  })
  try {
    const a = env.auth.fetchStreamWithAuthRetry('/api/chat/messages', { method: 'POST', body: '{}' })
    const b = env.auth.fetchStreamWithAuthRetry('/api/chat/messages', { method: 'POST', body: '{}' })
    await new Promise(resolve => setImmediate(resolve)); pending.resolve()
    assert.equal((await a).status, 200); assert.equal((await b).status, 200)
    assert.equal(refreshes, 1); assert.equal(fetches, 4); assert.equal(writes, 2)
    await env.auth.fetchStreamWithAuthRetry('/api/chat/messages', { method: 'POST', body: '{}' })
    assert.equal(fetches, 5); assert.equal(refreshes, 1)
  } finally { env.dispose() }
})

test('fetch retries at most once and clears auth after its second 401', async () => {
  let fetches = 0
  const env = harness(async config => response(config, { access_token: 'account-new' }), { fetchImpl: async () => { fetches++; return { status: 401 } } })
  try {
    assert.equal((await env.auth.fetchStreamWithAuthRetry('/api/chat/messages')).status, 401)
    assert.equal(fetches, 2); assert.equal(env.store.token, null)
  } finally { env.dispose() }
})

test('a delayed axios 401 reuses the completed refresh rather than refreshing again', async () => {
  const delayed = deferred(); let refreshes = 0
  const env = harness(async config => {
    if (config.url === '/api/auth/refresh') { refreshes++; return response(config, { access_token: 'account-new' }) }
    if (config.headers.Authorization === 'Bearer account-new') return response(config)
    if (config.url === '/chat/slow') await delayed.promise
    return unauthorized(config)
  })
  try {
    const slow = env.request.get('/chat/slow')
    await env.request.get('/chat/fast'); delayed.resolve(); await slow
    assert.equal(refreshes, 1)
  } finally { env.dispose() }
})

test('current-user lookup still refreshes an expired registered access credential', async () => {
  let refreshes = 0
  const env = harness(async config => {
    if (config.url === '/api/auth/refresh') { refreshes++; return response(config, { access_token: 'account-new' }) }
    if (config.headers.Authorization === 'Bearer account-old') return unauthorized(config)
    return response(config, { id: 1 })
  })
  try {
    assert.equal((await env.request.get('/auth/me')).data.id, 1)
    assert.equal(refreshes, 1)
  } finally { env.dispose() }
})

test('concurrent rejected guests share recovery and logout prevents a late identity replacement', async () => {
  const pending = deferred(); let issues = 0
  const env = harness(async config => { issues++; await pending.promise; return response(config, { access_token: 'guest-new', visitor_id: 'visitor-new' }) }, { guest: true })
  try {
    const a = env.auth.recoverGuestAccess('guest-old'), b = env.auth.recoverGuestAccess('guest-old')
    env.store.clearAuth(); pending.resolve()
    await assert.rejects(a, env.auth.AuthSessionChangedError)
    await assert.rejects(b, env.auth.AuthSessionChangedError)
    assert.equal(issues, 1); assert.equal(env.storage.get('guest_token'), 'guest-old')
    assert.equal(env.store.token, null)
  } finally { env.dispose() }
})
