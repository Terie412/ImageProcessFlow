// 验证「程序化描边圆角框」节点：实时预览缩略图（参数改了立刻重画）/ 双击看大图 / 执行产出。
// 纯本地：不起模型、不花任何 API 钱。
// 全程在一次性临时工作目录里跑，不碰用户的真实画布。
import { spawn } from 'node:child_process'
import { rmSync } from 'node:fs'
import { CHROME, RUNTIME } from './_common.js'

const PORT = 9300 + (process.pid % 400)
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const TMPWS = `${RUNTIME}/testws-rounded`
const PROFILE = `${RUNTIME}/tmp/chrome-rounded-${Date.now()}`

try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着目录 */ }
const chrome = spawn(
  CHROME,
  [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--window-size=1680,1050',
    'about:blank',
  ],
  { stdio: 'ignore' },
)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const state = async () => (await fetch(`${BASE}/api/state`)).json()
const post = (p, b) =>
  fetch(`${BASE}${p}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(b ?? {}),
  }).then((r) => r.json())

let ORIG_WS = null

async function main() {
  const results = []
  const check = (n, ok, d) => {
    results.push([ok, n, d])
    console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${n}${d ? '  — ' + d : ''}`)
  }

  const origWs = (ORIG_WS = (await state()).workspace)
  rmSync(TMPWS, { recursive: true, force: true })
  await post('/api/workspace', { path: TMPWS })

  const st = await post('/api/processes', { defId: 'rounded_box', x: 300, y: 200 })
  const pid = st.graph.processes[st.graph.processes.length - 1].id
  const P = `.react-flow__node[data-id="${pid}"]`
  console.log(`已建测试画布 + 一个「程序化描边圆角框」节点（${pid}）`)

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
  const click = async (x, y, count) => {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: count })
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: count })
    await sleep(60)
  }
  const dblclick = async (x, y) => { await click(x, y, 1); await click(x, y, 2); await sleep(900) }
  const centerOf = async (selector) =>
    J(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)})
      if (!el) return 'null'
      const r = el.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)

  // 缩略图当前那几个像素（从 blob 上重新画进 canvas 读，能证明「图真的换了」而不只是换了地址）
  const shotPixels = async (points) =>
    J(`(() => {
      const img = document.querySelector(${JSON.stringify(P)} + ' .icon-shot img')
      if (!img) return JSON.stringify({ present: false })
      if (!img.complete || !img.naturalWidth) return JSON.stringify({ present: true, loaded: false })
      const c = document.createElement('canvas')
      c.width = img.naturalWidth; c.height = img.naturalHeight
      const ctx = c.getContext('2d')
      ctx.drawImage(img, 0, 0)
      const out = {}
      for (const p of ${JSON.stringify(points)}) {
        const d = ctx.getImageData(p[0], p[1], 1, 1).data
        out[p.join(',')] = [d[0], d[1], d[2], d[3]]
      }
      return JSON.stringify({ present: true, loaded: true, w: img.naturalWidth, h: img.naturalHeight, px: out })
    })()`)

  // 改一个数字参数（走真实 input 事件，React 的 onChange 才会收到）
  const setNumber = async (label, value) =>
    evalJs(`(() => {
      const fields = document.querySelectorAll(${JSON.stringify(P)} + ' .cfg-field')
      for (const f of fields) {
        const l = f.querySelector('.cfg-label')
        if (l && l.textContent.trim() === ${JSON.stringify(label)}) {
          const el = f.querySelector('input')
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
          setter.call(el, ${JSON.stringify(String(value))})
          el.dispatchEvent(new Event('input', { bubbles: true }))
          return true
        }
      }
      return false
    })()`)

  // 等到缩略图重新画好、且指定那一点的像素满足条件（防抖 150ms + 渲染 30ms）
  const waitPixel = async (point, want, ms = 6000) => {
    for (let i = 0; i < ms / 150; i++) {
      const r = await shotPixels([point])
      if (r.loaded && r.px[point.join(',')][3] === want) return { ok: true, shot: r }
      await sleep(150)
    }
    return { ok: false, shot: await shotPixels([point]) }
  }

  // SHOT=<前缀>：顺手把「节点本体」和「双击后的大图」各截一张，给人看用
  const SHOT = process.env.SHOT
  const snap = async (suffix) => {
    if (!SHOT) return
    const r = await send('Page.captureScreenshot', { format: 'png' })
    const { writeFileSync } = await import('node:fs')
    const f = `${SHOT}-${suffix}.png`
    writeFileSync(f, Buffer.from(r.data, 'base64'))
    console.log(`  截图 -> ${f}`)
  }

  await send('Page.navigate', { url: BASE })
  await sleep(1800)

  console.log('\n[1] 节点上出现预览缩略图，且尺寸 = 默认 512×512')
  let shot = null
  for (let i = 0; i < 40; i++) {
    shot = await shotPixels([[6, 6], [0, 256], [256, 256]])
    if (shot.loaded) break
    await sleep(200)
  }
  check('缩略图已渲染', !!shot?.loaded, shot?.loaded ? `${shot.w}×${shot.h}` : JSON.stringify(shot))
  check('默认尺寸 512×512', shot?.w === 512 && shot?.h === 512, `${shot?.w}×${shot?.h}`)
  check('左上角在圆角外（透明）', shot?.px['6,6']?.[3] === 0, JSON.stringify(shot?.px['6,6']))
  check('左边中点是描边色 #111111', shot?.px['0,256']?.join(',') === '17,17,17,255', JSON.stringify(shot?.px['0,256']))
  check('中心是主体色 #ffffff', shot?.px['256,256']?.join(',') === '255,255,255,255', JSON.stringify(shot?.px['256,256']))
  await snap('node')

  console.log('\n[2] 改参数 → 缩略图实时重画（圆角 X 改成 0 时，左上角应变成不透明的描边色）')
  check('找得到「圆角 X」输入框', (await setNumber('圆角 X', 0)) === true)
  let r0 = await waitPixel([6, 6], 255)
  check('圆角 X 改成 0：角落变成不透明（图真的重画了）', r0.ok, JSON.stringify(r0.shot?.px))
  await setNumber('圆角 X', 96)
  let r1 = await waitPixel([6, 6], 0)
  check('圆角 X 改回 96：角落又变透明', r1.ok, JSON.stringify(r1.shot?.px))

  console.log('\n[2b] 椭圆角：横竖两个半径各管一边')
  await setNumber('圆角 X', 8)
  await setNumber('圆角 Y', 60)
  // 三个取点（实测标定，512×512、描边 12、rx=8 ry=60）：
  //   (4,4) 在椭圆外 → 透明
  //   (2,10) 竖轴 60 时左边要到 y≈20 才有内容，所以它还是透明的 ——
  //          若竖轴也按 8 算，这里早该是实的了 ⇒ 这一条真正判别「竖轴是 60」
  //   (20,4) 横轴只有 8，顶边在 x≥8 就是实的 ⇒ 判别「横轴是 8」
  const want = { '4,4': 0, '2,10': 0, '20,4': 255 }
  let ell = null
  for (let i = 0; i < 40; i++) {
    ell = await shotPixels([[4, 4], [2, 10], [20, 4]])
    if (ell.loaded && Object.keys(want).every((k) => ell.px[k]?.[3] === want[k])) break
    await sleep(200)
  }
  check('角落被切掉：(4,4) 透明', ell.px['4,4']?.[3] === 0, JSON.stringify(ell.px['4,4']))
  check('竖轴 60：左边到 y≈20 才有内容，(2,10) 仍透明', ell.px['2,10']?.[3] === 0, JSON.stringify(ell.px['2,10']))
  check('横轴 8：顶边 x≥8 就实了，(20,4) 不透明', ell.px['20,4']?.[3] === 255, JSON.stringify(ell.px['20,4']))

  console.log('\n[3] 改尺寸参数 → 缩略图跟着变尺寸')
  await setNumber('宽', 320)
  await setNumber('高', 320)
  let sized = null
  for (let i = 0; i < 40; i++) {
    sized = await shotPixels([[6, 6]])
    if (sized.loaded && sized.w === 320 && sized.h === 320) break
    await sleep(200)
  }
  check('宽高 320 生效', sized?.w === 320 && sized?.h === 320, `${sized?.w}×${sized?.h}`)

  console.log('\n[4] 预览不算执行：画布上还没有产出、节点也没变脏')
  const s4 = await state()
  check('没有多出数据节点', s4.graph.resources.length === 0, `resources=${s4.graph.resources.length}`)
  check('节点仍是待执行', s4.graph.processes[0].status === 'idle', s4.graph.processes[0].status)

  console.log('\n[5] 双击缩略图 → 打开大图（和数据节点同一个查看器）')
  const shotPt = await centerOf(`${P} .icon-shot`)
  await dblclick(shotPt.x, shotPt.y)
  const viewer = await J(`(() => {
    const m = document.querySelector('.viewer-mask')
    if (!m) return JSON.stringify({ open: false })
    const img = m.querySelector('.viewer-stage img')
    return JSON.stringify({
      open: true,
      loaded: !!(img && img.complete && img.naturalWidth),
      w: img ? img.naturalWidth : 0,
      h: img ? img.naturalHeight : 0,
      meta: (m.querySelector('.viewer-meta') || {}).textContent || '',
      badge: (m.querySelector('.badge') || {}).textContent || '',
    })
  })()`)
  check('大图窗口打开了', viewer.open === true)
  check('大图尺寸 = 320×320', viewer.w === 320 && viewer.h === 320, `${viewer.w}×${viewer.h}`)
  check('顶栏标了尺寸与格式', /320 × 320/.test(viewer.meta) && /PNG/.test(viewer.meta), viewer.meta.trim())
  check('徽标是「预览」', viewer.badge === '预览', viewer.badge)
  await snap('viewer')

  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await sleep(400)
  check('Esc 关掉大图', (await J(`JSON.stringify({ open: !!document.querySelector('.viewer-mask') })`)).open === false)

  console.log('\n[6] 点「执行」→ 产出 320×320 的 PNG，缩略图仍在')
  const exec = await centerOf(`${P} .node-foot button.primary`)
  await click(exec.x, exec.y, 1)
  let s6 = null
  for (let i = 0; i < 60; i++) {
    s6 = await state()
    if (s6.graph.processes[0].status === 'done' || s6.graph.processes[0].status === 'error') break
    await sleep(250)
  }
  const out = s6.graph.resources[0]
  check('执行成功', s6.graph.processes[0].status === 'done', s6.graph.processes[0].error || '')
  check('产出 320×320 PNG', !!out && out.width === 320 && out.height === 320 && out.format === 'PNG',
        out ? `${out.width}×${out.height} ${out.format}` : '没有产出')
  check('执行后缩略图还在', (await shotPixels([[6, 6]])).present === true)

  ws.close()
  chrome.kill()
  await post('/api/workspace', { path: origWs })
  rmSync(TMPWS, { recursive: true, force: true })
  try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着目录 */ }
  console.log(`\n已恢复工作目录: ${origWs}`)

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
