import { gzipSync } from 'node:zlib'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const assets = join(process.cwd(), 'dist', 'assets')
const rows = readdirSync(assets).filter((name) => /\.(js|css)$/.test(name)).map((name) => ({
  name,
  gzipKb: gzipSync(readFileSync(join(assets, name))).byteLength / 1024,
}))
const failures = []
for (const row of rows) {
  const lazyRoute = /^(Login|Users|Monitoring|Operations|DeviceDrawer)-/.test(row.name)
  const limit = row.name.endsWith('.css') ? 12 : lazyRoute ? 45 : 85
  if (row.gzipKb > limit) failures.push(`${row.name}: ${row.gzipKb.toFixed(2)} KB gzip exceeds ${limit} KB`)
}
if (!rows.some((row) => row.name.startsWith('index-') && row.name.endsWith('.js'))) {
  failures.push('Initial Fleet application chunk was not found.')
}
if (failures.length) {
  console.error(`Bundle budget failed:\n${failures.join('\n')}`)
  process.exit(1)
}
console.log(rows.map((row) => `${row.name}: ${row.gzipKb.toFixed(2)} KB gzip`).join('\n'))
