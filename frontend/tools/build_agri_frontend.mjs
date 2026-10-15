// Isolated build with deployment dotenv disabled and no existing output removal.
import { randomUUID } from 'node:crypto'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const frontend = resolve(root, 'frontend')
const output = resolve(process.env.AGRI_BUILD_SCRATCH || resolve(root, 'scratch'), `agri-build-${randomUUID()}`)
const { build } = await import(pathToFileURL(resolve(frontend, 'node_modules/vite/dist/node/index.js')).href)
await build({ root: frontend, configFile: resolve(frontend, 'vite.config.ts'), configLoader: 'runner',
  envDir: false, resolve: { preserveSymlinks: true }, build: { outDir: output, emptyOutDir: false } })
console.log(`BUILD_OUTPUT=${output}`)
