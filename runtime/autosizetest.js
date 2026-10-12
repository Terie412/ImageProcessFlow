// 「尺寸 = 自动」的界面验证：
//   ① 节点上**生成之前**就显示出自动会选哪个预设（不用等生成完）
//   ② 提示挂在尺寸下拉**下面**，不把 select 挤窄、也没让配置区多出一排
//   ③ 参考图换掉 → 提示跟着变；显式选了尺寸 → 提示消失
// 输入图由脚本自己按指定宽高现造（PNG 手写 encoder），所以不依赖任何二进制种子文件。
//
//   SHOT=1 node runtime/autosizetest.js http://127.0.0.1:8770
import { spawn } from 'node:child_process'
import { rmSync, writeFileSync } from 'node:fs'
import { deflateSync } from 'node:zlib'
import { CHROME, RUNTIME } from './_common.js'

const PORT = 9241
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const TMPWS = `${RUNTIME}/testws-autosize`
const PROFILE = `${RUNTIME}/chrome-profile-autosize`

/* ---------------- 手写一个纯色 PNG（只为拿到指定分辨率） ---------------- */
const CRC_T = (() => {
  const t = new Int32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c
  }
  return t
})()
const crc32 = (buf) => {
  let c = -1
  for (const b of buf) c = CRC_T[(c ^ b) & 0xff] ^ (c >>> 8)
  return (c ^ -1) >>> 0
}
const chunk = (type, data) => {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const td = Buffer.concat([Buffer.from(type, 'latin1'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(td))
  return Buffer.concat([len, td, crc])
}
function pngRGB(w, h) {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(w, 0)
  ihdr.writeUInt32BE(h, 4)
  ihdr[8] = 8 // 位深
  ihdr[9] = 2 // truecolor
  const stride = w * 3 + 1
  const raw = Buffer.alloc(stride * h)
  for (let y = 0; y < h; y++) {
    const off = y * stride
    raw[off] = 0 // filter: none
    for (let x = 0; x < w; x++) {
      // 画几条斜纹，缩略图里看得出是张图而不是纯色块
      const v = ((x + y) % 64) / 64
      raw[off + 1 + x * 3] = 30 + v * 60
      raw[off + 2 + x * 3] = 40 + v * 40
      raw[off + 3 + x * 3] = 60 + v * 20
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着 */ }
const chrome = spawn(
  CHROME,
  [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--window-size=1600,1000',
    'about:blank',
  ],
  { stdio: 'ignore' },
)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const api = (path) => fetch(`${BASE}${path}`).then((r) => r.json())
const post = (path, body) =>
  fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json())
const patch = (path, body) =>
  fetch(`${BASE}${path}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json())

async function importImage(w, h, x, y) {
  const fd = new FormData()
  fd.append('file', new Blob([pngRGB(w, h)], { type: 'image/png' }), `${w}x${h}.png`)
  fd.append('resource_id', '')
  fd.append('x', String(x))
  fd.append('y', String(y))
  const r = await (await fetch(`${BASE}/api/resources/import`, { method: 'POST', body: fd })).json()
  return r.graph.resources[r.graph.resources.length - 1].id
}

let ORIG_WS = null

async function main() {
  const results = []
  const check = (n, ok, d) => {
    results.push([ok, n, d])
    console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${n}${d ? '  — ' + d : ''}`)
  }

  const origWs = (ORIG_WS = (await api('/api/state')).workspace)
  rmSync(TMPWS, { recursive: true, force: true })
  await post('/api/workspace', { path: TMPWS })
  console.log(`已切到一次性工作目录（原目录: ${origWs}）\n`)

  // 参考图 720×1280 —— 输入本身就在下拉选项里
  const rid = await importImage(720, 1280, 60, 120)
  const st = await post('/api/processes', { defId: 'qwen_image', x: 460, y: 120 })
  const pid = st.graph.processes.at(-1).id
  await post('/api/edges', { from: rid, to: pid, port: 'image1' })
  const s0 = await api('/api/state')
  const p0 = s0.graph.processes.find((p) => p.id === pid)

  console.log('[1] 服务端算出来的落点')
  check('autoSize.value = 720x1280',
    p0.autoSize?.value === '720x1280', JSON.stringify(p0.autoSize))
  check('autoSize.source = input（依据参考图而来）',
    p0.autoSize?.source === 'input')
  check('带上是哪张参考图',
    p0.autoSize?.from === '参考图1' && p0.autoSize?.image === 'A',
    `${p0.autoSize?.from} / ${p0.autoSize?.image}`)

  /* ---------------- 连上 CDP ---------------- */
  let target = null
  for (let i = 0; i < 120; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      target = list.find((t) => t.type === 'page')
      if (target) break
    } catch { /* 等启动 */ }
    await sleep(300)
  }
  if (!target) throw new Error('找不到调试目标')

  const ws = new WebSocket(target.webSocketDebuggerUrl)
  let id = 0
  const pending = new Map()
  const send = (method, params = {}) =>
    new Promise((res, rej) => {
      const mid = ++id
      pending.set(mid, { res, rej })
      ws.send(JSON.stringify({ id: mid, method, params }))
    })
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data)
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id)
      pending.delete(m.id)
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)
    }
  }
  await new Promise((r) => (ws.onopen = r))
  await send('Page.enable')
  await send('Runtime.enable')

  const evalJs = async (expr) =>
    (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value
  const J = async (expr) => JSON.parse(await evalJs(expr))
  const sel = (nodeId) => `.react-flow__node[data-id="${nodeId}"]`

  await send('Page.navigate', { url: BASE })
  await sleep(1800)

  try {
    console.log('\n[2] 节点上真的显示出来了')
    const hint = await evalJs(
      `(document.querySelector('${sel(pid)} .cfg-auto')?.textContent || '').trim()`,
    )
    check('提示文字 = 「自动 → 720×1280」', hint === '自动 → 720×1280', JSON.stringify(hint))

    const title = await evalJs(
      `(document.querySelector('${sel(pid)} .cfg-auto')?.title || '').trim()`,
    )
    check('悬停能看到依据（参考图1 · A · 720×1280）',
      /参考图1/.test(title) && /720×1280/.test(title), title)

    // select 没被提示挤窄：宽度与没有提示时同口径（定宽 148 的字段里 select 应还有余量）
    const widths = await J(`(() => {
      const f = document.querySelector('${sel(pid)} .cfg-field.col')
      const s = f.querySelector('select').getBoundingClientRect()
      const a = f.querySelector('.cfg-auto').getBoundingClientRect()
      return JSON.stringify({
        field: Math.round(f.getBoundingClientRect().width),
        select: Math.round(s.width),
        hint: Math.round(a.width),
      })
    })()`)
    console.log(`   字段 ${widths.field}px / select ${widths.select}px / 提示 ${widths.hint}px`)
    check('提示没有挤窄下拉框（select ≥ 90px）', widths.select >= 90, JSON.stringify(widths))
    check('提示没有溢出自段（hint ≤ field）', widths.hint <= widths.field, JSON.stringify(widths))

    // 配置区排数不能因为多了一行提示就变多
    const rows = await J(`(() => {
      const node = document.querySelector('${sel(pid)}')
      const items = [...node.querySelectorAll('.cfg > .cfg-field, .cfg > .cfg-checks')]
      const ys = new Set()
      for (const el of items) {
        const r = el.getBoundingClientRect()
        ys.add(Math.round((r.top + r.bottom) / 2))
      }
      return JSON.stringify([...ys])
    })()`)
    check('配置区仍是两排（提示没多推出一排）', rows.length <= 2, `rows=${rows.length}`)

    console.log('\n[3] 换参考图 / 显式选尺寸')
    // 节点默认只有一个输入口 —— 先加到 2 个，才能验证「取最后一张」
    await patch(`/api/processes/${pid}`, { ports: { image: 2 } })
    const rid2 = await importImage(1920, 1080, 60, 700)
    await post('/api/edges', { from: rid2, to: pid, port: 'image2' })
    let s = await api('/api/state')
    const p2 = s.graph.processes.find((p) => p.id === pid)
    check('接第二张（1920×1080）后落点变成 1280x720',
      p2.autoSize?.value === '1280x720', JSON.stringify(p2.autoSize))
    // 换序：1920×1080 挪到 image1、720×1280 挪到 image2 → 落点该回到 720x1280
    await post('/api/edges', { from: rid2, to: pid, port: 'image1' })
    await post('/api/edges', { from: rid, to: pid, port: 'image2' })
    s = await api('/api/state')
    check('取的是**最后一张**参考图（换序后 = 720x1280）',
      s.graph.processes.find((p) => p.id === pid).autoSize?.value === '720x1280',
      JSON.stringify(s.graph.processes.find((p) => p.id === pid).autoSize))
    // 删掉 image2 那条线，只剩 1920×1080 在 image1 → 应变 1280x720
    const e2 = (await api('/api/state')).graph.edges.find((e) => e.to === pid && e.port === 'image2')
    await fetch(`${BASE}/api/edges/${e2.id}`, { method: 'DELETE' })
    await sleep(200)
    s = await api('/api/state')
    check('只剩 1920×1080 时落点 = 1280x720',
      s.graph.processes.find((p) => p.id === pid).autoSize?.value === '1280x720',
      s.graph.processes.find((p) => p.id === pid).autoSize?.value)

    // ---- 界面：先把页面重载一次，让前端拿到与上面一致的 state ----
    // 上面全是直接打 API 改图，前端不会知道（真实使用里这些操作都走 applyState）。
    await send('Page.navigate', { url: BASE })
    await sleep(1800)

    const hintNow = async () =>
      (await evalJs(`(document.querySelector('${sel(pid)} .cfg-auto')?.textContent || '')`)).trim()
    const waitHint = async (want, ms = 4000) => {
      for (let t = 0; t < ms; t += 150) {
        const txt = await hintNow()
        if (txt === want) return txt
        await sleep(150)
      }
      return hintNow()
    }
    // React 受控组件用自己的 value tracker 去重，必须走原生 setter 才认。
    // 定位不能用 `.cfg-field.col` —— 选了显式尺寸之后 `col` 就被摘掉了。
    // 「自动」-capable 的那个 select 的特征是**有一个空值选项**。
    const setSelect = async (v) =>
      evalJs(`(() => {
        const el = [...document.querySelectorAll('${sel(pid)} .cfg-field select')]
          .find((s) => [...s.options].some((o) => o.value === ''))
        if (!el) return false
        const set = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set
        set.call(el, ${JSON.stringify(v)})
        el.dispatchEvent(new Event('change', { bubbles: true }))
        return true
      })()`)

    check('下拉框存在', await setSelect(''))
    check('重载后按当前参考图显示（只剩 1920×1080 → 1280×720）',
      (await waitHint('自动 → 1280×720')) === '自动 → 1280×720', await hintNow())

    // 显式选尺寸：提示必须**立刻**消失 —— 靠本地 params 判断，
    // 不能等 500ms 防抖 PATCH 再回灌 state（实测那样会卡住好几秒）
    await setSelect('1024x1024')
    await sleep(120)
    const gone = await hintNow()
    check('选显式尺寸后提示立刻消失（120ms，早于防抖落盘）', gone === '', JSON.stringify(gone))
    let pe = null
    for (let t = 0; t < 3000; t += 150) {
      pe = (await api('/api/state')).graph.processes.find((p) => p.id === pid)
      if (pe?.params?.size === '1024x1024') break
      await sleep(150)
    }
    check('参数最终落到服务端', pe?.params?.size === '1024x1024', JSON.stringify(pe?.params?.size))
    check('服务端仍照常下发落点（显不显示是前端的事）',
      pe?.autoSize?.source === 'input', JSON.stringify(pe?.autoSize))

    // 切回「自动」→ 提示立刻回来
    await setSelect('')
    await sleep(120)
    const back = await hintNow()
    const diag = await J(`(() => {
      const f = document.querySelector('${sel(pid)} .cfg-field')
      const sels = [...document.querySelectorAll('${sel(pid)} select')]
      return JSON.stringify({
        selects: sels.map((s) => s.value),
        fields: [...document.querySelectorAll('${sel(pid)} .cfg-field')].map((f) => f.className),
        hasAuto: !!document.querySelector('${sel(pid)} .cfg-auto'),
      })
    })()`)
    check('切回自动后提示立刻回来', back === '自动 → 1280×720',
      JSON.stringify({ back, ...diag }))

    if (process.env.SHOT) {
      // 裁到节点上再放大 2 倍 —— 整屏截图里那行提示只有几个像素高，看不出对错
      const rect = await J(`(() => {
        const r = document.querySelector('${sel(pid)}').getBoundingClientRect()
        return JSON.stringify({
          x: Math.floor(r.left) - 10, y: Math.floor(r.top) - 10,
          width: Math.ceil(r.width) + 20, height: Math.ceil(r.height) + 20,
        })
      })()`)
      const shot = await send('Page.captureScreenshot', {
        format: 'png',
        clip: { ...rect, scale: 2 },
      })
      writeFileSync(`${RUNTIME}/autosize_node.png`, Buffer.from(shot.data, 'base64'))
      console.log('   已截图 runtime/autosize_node.png')
    }
  } finally {
    ws.close()
    chrome.kill()
    await post('/api/workspace', { path: origWs })
    rmSync(TMPWS, { recursive: true, force: true })
    try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* 可能还占着 */ }
    console.log(`\n已恢复工作目录: ${origWs}`)
  }

  const bad = results.filter((r) => !r[0])
  console.log(`\n共 ${results.length} 项，通过 ${results.length - bad.length}，失败 ${bad.length}`)
  process.exit(bad.length ? 1 : 0)
}

main().catch(async (e) => {
  console.error('ERR', e.message)
  chrome.kill()
  if (ORIG_WS) {
    try {
      await post('/api/workspace', { path: ORIG_WS })
      rmSync(TMPWS, { recursive: true, force: true })
      console.log(`已兜底恢复工作目录: ${ORIG_WS}`)
    } catch (e2) {
      console.error('兜底恢复失败:', e2.message)
    }
  }
  process.exit(1)
})
