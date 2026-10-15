// Exercise actual weather polling with controlled requests, visibility and unmount.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'
const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript } = require('@vue/compiler-sfc')
const ts = require('typescript')
const vue = require('vue')
const { descriptor } = parse(readFileSync(new URL('../src/components/FieldWeatherPanel.vue', import.meta.url), 'utf8'))
const javascript = ts.transpileModule(compileScript(descriptor, { id: 'weather-life' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
function harness(fetch) {
  const props = vue.reactive({ fieldId: null, days: 7 })
  const events = new Map(), timers = new Map(), hooks = { mounted: [], unmounted: [] }
  const document = { hidden: false, addEventListener: (name, fn) => events.set(name, fn), removeEventListener: name => events.delete(name) }
  const module = { exports: {} }
  new Script(javascript).runInContext(createContext({ module, exports: module.exports, document, Date, Intl,
    setInterval: (fn, ms) => { timers.set(1, { fn, ms }); return 1 }, clearInterval: id => timers.delete(id),
    require: name => name === 'vue' ? { ...vue, onMounted: fn => hooks.mounted.push(fn), onBeforeUnmount: fn => hooks.unmounted.push(fn) } : { getFieldWeatherForecast: fetch }
  }))
  const state = module.exports.default.setup(props, { expose() {} })
  hooks.mounted.forEach(fn => fn())
  return { props, state, events, timers, document, unmount: () => hooks.unmounted.forEach(fn => fn()) }
}
const response = id => ({ data: { field_id: id, forecasts: [], status: 'online' } })
test('hidden pages do not poll and unmount cancels timer and visibility listener', async () => {
  const calls = []
  const env = harness(async id => { calls.push(id); return response(id) })
  env.props.fieldId = 1
  await vue.nextTick(); await Promise.resolve()
  const count = calls.length
  assert.equal(env.timers.get(1).ms, 600000)
  env.document.hidden = true
  env.timers.get(1).fn(); await Promise.resolve()
  assert.equal(calls.length, count)
  env.unmount()
  assert.equal(env.timers.size, 0); assert.equal(env.events.size, 0)
})
test('late responses from a previous field cannot replace current data', async () => {
  const pending = new Map()
  const env = harness(id => new Promise(resolve => pending.set(id, resolve)))
  env.props.fieldId = 1; await vue.nextTick()
  env.props.fieldId = 2; await vue.nextTick()
  pending.get(2)(response(2)); await Promise.resolve(); await Promise.resolve()
  pending.get(1)(response(1)); await Promise.resolve(); await Promise.resolve()
  assert.equal(env.state.weather.value.field_id, 2)
  env.unmount()
})
test('failed refresh clears previous weather instead of presenting stale evidence', async () => {
  let fail = false
  const env = harness(async id => { if (fail) throw Error('controlled'); return response(id) })
  env.props.fieldId = 1; await vue.nextTick(); await Promise.resolve(); await Promise.resolve()
  assert.equal(env.state.weather.value.field_id, 1)
  fail = true; await env.state.refresh()
  assert.equal(env.state.weather.value, null)
  assert.ok(env.state.error.value.includes('失败'))
  env.unmount()
})
