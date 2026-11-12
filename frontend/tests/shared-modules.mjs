// Shared test helper: compile real project TS modules (constants, utils, composables) so view
// harnesses can resolve them without stubbing their behaviour. Only pure modules belong here.
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { Script, createContext } from 'node:vm'

const require = createRequire(new URL('../package.json', import.meta.url))
const ts = require('typescript')
const vue = require('vue')

const SHARED = {
  '@/constants/agri': '../src/constants/agri.ts',
  '@/components/common/SourceBadge.vue': '../src/components/common/SourceBadge.vue',
  '@/components/common/PageState.vue': '../src/components/common/PageState.vue',
  '@/utils/errorText': '../src/utils/errorText.ts',
  '@/utils/validationMessage': '../src/utils/validationMessage.ts',
  '@/utils/password': '../src/utils/password.ts',
  '@/composables/useBreakpoint': '../src/composables/useBreakpoint.ts',
}
const cache = new Map()

function compile(name, vueOverride) {
  let source = readFileSync(new URL(SHARED[name], import.meta.url), 'utf8')
  if (name.endsWith('.vue')) {
    // Real SFC (script + template) so SSR-rendering tests render the actual markup.
    const { parse, compileScript } = require('@vue/compiler-sfc')
    const { descriptor } = parse(source)
    source = compileScript(descriptor, { id: name, inlineTemplate: true }).content
  }
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText
  const module = { exports: {} }
  new Script(code).runInContext(createContext({ module, exports: module.exports, console, TextEncoder,
    require:dep => dep === 'vue' ? (vueOverride || vue) : sharedModule(dep, vueOverride) }))
  return name.endsWith('.vue') ? module.exports.default : module.exports
}

/** Returns the compiled shared module for `name`, or undefined when `name` is not a shared module. */
export function sharedModule(name, vueOverride) {
  if (!(name in SHARED)) return undefined
  const key = name + (vueOverride ? ':custom' : '')
  if (!cache.has(key)) cache.set(key, compile(name, vueOverride))
  return cache.get(key)
}

/** Minimal vue-router stand-in for views that register route-leave guards. */
export const routerStub = {
  onBeforeRouteLeave() {},
  useRouter: () => ({ push() {}, replace() {}, back() {} }),
  useRoute: () => ({ path: '/', fullPath: '/', query: {}, meta: {} }),
}
