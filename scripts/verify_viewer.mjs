// 実サーバーと同梱bundleの接続検証。Node/Playwrightは開発時だけ使用する。
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const [url, modulePath, executablePath] = process.argv.slice(2)
const { chromium } = await import(pathToFileURL(modulePath).href)
const browser = await chromium.launch({
  executablePath, headless: true, chromiumSandbox: true,
  args: ['--disable-extensions', '--disable-background-networking',
    '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
})
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })
  const responses = []
  const errors = []
  let workers = 0
  page.on('worker', () => { workers++ })
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => responses.push({
    url: response.url(), status: response.status(),
    auth: response.request().headers().authorization,
  }))
  await page.goto(url)
  await page.waitForFunction(() => window.viewer?.ready || document.querySelector('[role="alert"]'))
  assert.equal(await page.locator('[role="alert"]').count(), 0)
  assert.equal(await page.evaluate(() => !!window.viewer?.ready), true)
  assert.ok(workers > 0, 'Workerが起動していない')
  assert.ok(responses.some(r => r.url.endsWith('.css') && r.status === 200))
  assert.ok(responses.some(r => r.url.endsWith('.png') && r.status === 200 && r.auth))
  assert.ok(responses.some(r => r.url.includes('/api/scene') && r.status === 200 && r.auth))
  await page.waitForTimeout(500)
  assert.ok(responses.some(r => r.url.endsWith('/api/events') && r.status === 200 && r.auth))
  const token = new URL(url).hash.slice(1).split('token=')[1]
  const saved = await page.evaluate(async token => {
    const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
    const response = await fetch('/api/scene', { headers })
    const compressed = new Blob([await response.arrayBuffer()])
    const raw = await new Response(compressed.stream().pipeThrough(new DecompressionStream('gzip')))
      .arrayBuffer()
    const digest = await crypto.subtle.digest('SHA-256', raw)
    const hash = Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('')
    return (await fetch('/api/edits', { method: 'POST', headers, body: JSON.stringify({
      formatVersion: 1, scene: hash,
      edits: [{ id: 'browser-delete', kind: 'delete', at: [0, 0, 0] }],
    }) })).status
  }, token)
  assert.equal(saved, 200)
  const reload = page.waitForEvent('domcontentloaded')
  const result = await page.evaluate(async token => {
    const response = await fetch('/api/regenerate', { method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: '{}' })
    return response.status
  }, token).catch(error => {
    // SSEによるreloadが先に始まると、旧ページのevaluateは破棄される。
    if (!error.message.includes('Execution context was destroyed')) throw error
    return 200
  })
  assert.equal(result, 200)
  await reload
  await page.waitForFunction(() => window.viewer?.ready || document.querySelector('[role="alert"]'))
  assert.equal(await page.evaluate(() => !!window.viewer?.ready), true)
  assert.equal(errors.length, 0, errors.join('\n'))
  console.log(JSON.stringify({ ready: true, bearer: true, sseReload: true, workers,
    css: true, textures: true, browser: browser.version() }))
} finally {
  await browser.close()
}
