// Bundle size limits (gzip). Prints a headroom table on success and failure.
// Lazy page chunks of the whole revamp are listed once here (FND-1), so a page
// PR never has to edit this script. Any other chunk (shared code Vite splits out
// of src/ui, src/lib or src/words) gets the default limit and a warning, never a
// failure just for its name.
import { gzipSync } from 'node:zlib'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const assets = join(process.cwd(), 'dist', 'assets')
const LAZY_PAGE = /^(Login|UsersWorkspace|Attendance|FixTools|Home|Problems|Devices|DeviceView|ActivityTab|TechnicalLog|AdvancedTools|Firmware|Reconciliation|DeviceDrawer|PublicStatus|HealthCleanupDialog|Settings|InstallerAppDownload)-/
const PROVISIONING = /^FirmwareProvisioning-/
const CORE = /^(index|router|query|forms|react-core|vendor)-/
const LIMITS = { css: 14, provisioning: 12, lazy: 45, core: 85, other: 45 }

const rows = readdirSync(assets).filter((name) => /\.(js|css)$/.test(name)).map((name) => {
  const gzipKb = gzipSync(readFileSync(join(assets, name))).byteLength / 1024
  const kind = name.endsWith('.css') ? 'css' : PROVISIONING.test(name) ? 'provisioning' : LAZY_PAGE.test(name) ? 'lazy' : CORE.test(name) ? 'core' : 'other'
  return { name, gzipKb, kind, limit: LIMITS[kind] }
}).sort((left, right) => (right.gzipKb / right.limit) - (left.gzipKb / left.limit))

const failures = rows.filter((row) => row.gzipKb > row.limit).map((row) => `${row.name}: ${row.gzipKb.toFixed(2)} KB gzip exceeds ${row.limit} KB`)
if (!rows.some((row) => row.name.startsWith('index-') && row.name.endsWith('.js'))) {
  failures.push('Initial application chunk (index-*.js) was not found.')
}
const warnings = rows.filter((row) => row.kind === 'other').map((row) => `${row.name}: not a listed chunk name; checked against the default ${LIMITS.other} KB`)

const width = Math.max(10, ...rows.map((row) => row.name.length))
const table = [
  `${'chunk'.padEnd(width)}  ${'gzip KB'.padStart(8)}  ${'limit'.padStart(5)}  ${'headroom'.padStart(8)}  kind`,
  ...rows.map((row) => `${row.name.padEnd(width)}  ${row.gzipKb.toFixed(2).padStart(8)}  ${String(row.limit).padStart(5)}  ${(row.limit - row.gzipKb).toFixed(2).padStart(8)}  ${row.kind}`),
].join('\n')
console.log(table)
if (warnings.length) console.warn(`\nWarnings:\n${warnings.join('\n')}`)
if (failures.length) {
  console.error(`\nBundle budget failed:\n${failures.join('\n')}`)
  process.exit(1)
}
