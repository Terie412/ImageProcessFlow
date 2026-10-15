// 验证自研取色器：色块弹出 / hex 与 RGB 互相同步 / 失败在画布底部弹提示 / 拖色域与色相条 /
// 关掉浮层的几种方式 / 改色真的落到参数与预览上。
// 纯本地：不起模型、不花任何 API 钱。全程在一次性临时工作目录里跑，不碰用户的真实画布。
import { spawn } from 'node:child_process'
import { rmSync } from 'node:fs'
import { CHROME, RUNTIME } from './_common.js'

const PORT = 9300 + (process.pid % 400)
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const TMPWS = `${RUNTIME}/testws-colorpicker`
const PROFILE = `${RUNTIME}/tmp/chrome-color-${Date.now()}`

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

  // 用「程序化描边圆角框」当靶子：它有两个颜色参数，而且改色会立刻反映到预览图上
  const st = await post('/api/processes', { defId: 'rounded_box', x: 300, y: 200 })
  const pid = st.graph.processes[st.graph.processes.length - 1].id
  const P = `.react-flow__node[data-id="${pid}"]`
  console.log(`已建测试画布 + 一个圆角框节点（${pid}）`)

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
  await send('Network.enable')
  const patches = [] // 每一次 PATCH /api/processes/<id> 的请求体 */
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data)
    if (m.method === 'Network.requestWillBeSent') {
      const r = m.params.request
      if (r.method === 'PATCH' && r.url.includes('/api/processes/')) patches.push(r.postData || '')
    }
  })
  const evalJs = async (expr) =>
    (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value
  const J = async (expr) => JSON.parse(await evalJs(expr))
  const click = async (x, y, count = 1) => {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: count })
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: count })
    await sleep(80)
  }
  const centerOf = async (selector) =>
    J(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)})
      if (!el) return 'null'
      const r = el.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
  const clickSel = async (selector) => {
    const p = await centerOf(selector)
    if (!p) throw new Error(`找不到 ${selector}`)
    await click(p.x, p.y)
  }

  // 浮层里那两个输入框：0 = HEX、1 = RGB
  const fields = async () =>
    J(`(() => {
      const pop = document.querySelector('.color-pop')
      if (!pop) return JSON.stringify({ open: false })
      const ins = pop.querySelectorAll('input')
      return JSON.stringify({
        open: true,
        hex: ins[0] ? ins[0].value : null,
        rgb: ins[1] ? ins[1].value : null,
        left: Math.round(pop.getBoundingClientRect().left),
        top: Math.round(pop.getBoundingClientRect().top),
      })
    })()`)

  // SHOT=<前缀>：顺手截图，给人看用
  const SHOT = process.env.SHOT
  const snap = async (suffix) => {
    if (!SHOT) return
    const r = await send('Page.captureScreenshot', { format: 'png' })
    const { writeFileSync } = await import('node:fs')
    const f = `${SHOT}-${suffix}.png`
    writeFileSync(f, Buffer.from(r.data, 'base64'))
    console.log(`  截图 -> ${f}`)
  }

  // 往输入框里打字并回车（走原生 setter + input 事件，React 的受控输入才认）
  const typeAndEnter = async (idx, text) => {
    const ok = await evalJs(`(() => {
      const ins = document.querySelectorAll('.color-pop input')
      const el = ins[${idx}]
      if (!el) return false
      el.focus()
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
      setter.call(el, ${JSON.stringify(text)})
      el.dispatchEvent(new Event('input', { bubbles: true }))
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
      return true
    })()`)
    if (!ok) throw new Error('没有输入框')
    await sleep(180)
  }

  // 提示条只读、**不要去 remove 它的 DOM** —— 它是 React 渲染的，手动删掉之后
  // React 还捏着那个脱离文档的节点，后面所有提示都会打在空气里（第一次就这么被骗了）。
  // 所以每条期望都用一个**只可能由这一次操作产生**的片段，不用清空也能分辨新旧。
  const toastText = async () =>
    evalJs(`(() => {
      const t = document.querySelector('.toast')
      return t ? t.textContent : ''
    })()`)

  // 缩略图中心那个像素（改色最终要落到图上才算数）
  const shotCenter = async () =>
    J(`(() => {
      const img = document.querySelector(${JSON.stringify(P)} + ' .icon-shot img')
      if (!img || !img.complete || !img.naturalWidth) return JSON.stringify({ loaded: false })
      const c = document.createElement('canvas')
      c.width = img.naturalWidth; c.height = img.naturalHeight
      const ctx = c.getContext('2d')
      ctx.drawImage(img, 0, 0)
      const d = ctx.getImageData(c.width >> 1, c.height >> 1, 1, 1).data
      return JSON.stringify({ loaded: true, center: [d[0], d[1], d[2], d[3]] })
    })()`)

  const paramOf = async (key) => {
    const s = await state()
    return s.graph.processes.find((p) => p.id === pid)?.params?.[key]
  }

  // 无头 Chrome 没有 EyeDropper（浏览器自带 API），吸管按钮就不渲染 —— 注一个假的进去，
  // 这样按钮的**位置**和**点了之后取到什么**都能验。（真窗口里用的是真的那个）
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: `window.EyeDropper = class { open() { return Promise.resolve({ sRGBHex: '#123456' }) } }`,
  })
  await send('Page.navigate', { url: BASE })
  await sleep(1800)

  /* 打开某个标签那一行的取色器，并**确认浮层就是它那一行的**：
     ① 先用 Esc 把可能开着的浮层收掉 —— `b.click()` 是程序化点击，不派发 mousedown，
        而「点外面收起」认的是 mousedown，于是会出现两个浮层同时开着、querySelector
        拿到前面那一个的坑（真鼠标点不会：mousedown 会先把另一个收掉）。
     ② 打开后拿浮层和色块的位置对一下：浮层就该贴在色块下方，确认关联没串。 */
  const openField = async (label) => {
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
    await sleep(150)
    const box = await J(`(() => {
      const f = [...document.querySelectorAll(${JSON.stringify(P)} + ' .cfg-field')]
        .find((x) => x.querySelector('.cfg-label')?.textContent.trim() === ${JSON.stringify(label)})
      const b = f && f.querySelector('.cfg-color')
      if (!b) return 'null'
      const r = b.getBoundingClientRect()
      return JSON.stringify({ l: Math.round(r.left), t: Math.round(r.top), b: Math.round(r.bottom) })
    })()`)
    if (!box) throw new Error(`找不到色块：${label}`)
    await evalJs(`(() => {
      const f = [...document.querySelectorAll(${JSON.stringify(P)} + ' .cfg-field')]
        .find((x) => x.querySelector('.cfg-label')?.textContent.trim() === ${JSON.stringify(label)})
      f.querySelector('.cfg-color').click()
      return true
    })()`)
    await sleep(220)
    const pop = await J(`(() => {
      const p = document.querySelector('.color-pop')
      if (!p) return 'null'
      const r = p.getBoundingClientRect()
      return JSON.stringify({ l: Math.round(r.left), t: Math.round(r.top), h: Math.round(r.height) })
    })()`)
    if (!pop) throw new Error(`打开 ${label} 的取色器失败`)
    // 贴着色块下方；窗口底下放不下时会翻到色块上方 —— 两种都算对上
    const below = Math.abs(pop.t - box.b - 6) <= 1
    const above = Math.abs(box.t - (pop.t + pop.h) - 6) <= 1
    if (Math.abs(pop.l - box.l) > 1 || !(below || above)) {
      throw new Error(`开着的浮层不是「${label}」那一行的：浮层 ${pop.l},${pop.t} vs 色块 ${box.l},${box.b}`)
    }
  }

  console.log('\n[1] 点色块 → 弹出取色器，两个框显示当前值')
  await openField('主体色')
  let f = await fields()
  check('浮层打开了', f.open === true)
  check('HEX 显示当前值 #ffffff', f.hex === '#ffffff', String(f.hex))
  check('RGB 显示 255, 255, 255', f.rgb === '255, 255, 255', String(f.rgb))
  check('是挂在 body 上的 fixed 浮层（节点在 transform 里，fixed 才算得准）',
        (await evalJs(`getComputedStyle(document.querySelector('.color-pop')).position`)) === 'fixed')
  await snap('picker')
  await snap('picker')

  console.log('\n[2] 改十六进制 → RGB 同步跟着变，并落到参数与预览图上')
  await typeAndEnter(0, 'ff8800') // 故意不带 #：井号可有可无
  f = await fields()
  check('HEX 补上井号：#ff8800', f.hex === '#ff8800', String(f.hex))
  check('RGB 同步成 255, 136, 0', f.rgb === '255, 136, 0', String(f.rgb))
  await sleep(700) // 参数落盘走 500ms 防抖
  check('节点参数已变成 #ff8800', (await paramOf('fillColor')) === '#ff8800', String(await paramOf('fillColor')))
  for (let i = 0; i < 20; i++) {
    const s = await shotCenter()
    if (s.loaded && s.center[0] === 255 && s.center[1] === 136) break
    await sleep(150)
  }
  const s2 = await shotCenter()
  check('预览图中心变成 (255, 136, 0)', s2.center?.join(',') === '255,136,0,255', JSON.stringify(s2.center))

  console.log('\n[3] 改 RGB → 十六进制同步跟着变（空项自动丢掉）')
  await typeAndEnter(1, '0, 128, , 200, ') // 手抖：中间空一格、末尾多一个逗号
  f = await fields()
  check('RGB 归一成 0, 128, 200', f.rgb === '0, 128, 200', String(f.rgb))
  check('HEX 同步成 #0080c8', f.hex === '#0080c8', String(f.hex))

  console.log('\n[5] 各种失败：值不动、原文留着、画布底部弹出原因')
  await sleep(700) // 参数落盘走 500ms 防抖，等它落定再取「改之前」的值
  const before = await paramOf('fillColor')
  const fails = [
    [0, '#12345', '3 或 6 位'],
    [0, '#12zz45', '不是 0-9a-f'],
    [0, '   ', '不能是空的'],
    [0, '#ff8800ff', '不支持透明'],   // 8 位 hex：带 alpha，这个参数存不下
    [1, '1, 2', '要 3 个数'],
    [1, '16, 32, 48, 128', '把最后的 A 去掉'], // 4 个数：同上
    [1, '300, 0, 0', '收到 300'],
    [1, 'a, 0, 0', '不是数字'],
    [1, '-1, 0, 0', '收到 -1'],
  ]
  for (const [idx, text, want] of fails) {
    await typeAndEnter(idx, text)
    const t = await toastText()
    check(`「${text}」被挡下且说明了原因`, t.includes(want), t || '(没有提示)')
  }
  await snap('reject') // 顺手截一张「被挡下 + 底部提示」
  await snap('reject') // 顺手截一张「被挡下 + 底部提示」
  check('连续失败后参数一个都没变', (await paramOf('fillColor')) === before, String(await paramOf('fillColor')))
  check('失败时输入框保留原文（好接着改）', (await fields()).rgb.includes('-1, 0, 0'), (await fields()).rgb)

  console.log('\n[6] 拖色域 / 色相条：颜色跟着变，预览图跟着重画')
  await typeAndEnter(0, '#ff0000') // 先定成纯红，色相条停在 0°
  const sv = await centerOf('.color-pop .color-sv') // 中心 = s 50% / v 50%
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: sv.x, y: sv.y, button: 'left', buttons: 1, clickCount: 1 })
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: sv.x, y: sv.y, button: 'left', buttons: 1 })
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: sv.x, y: sv.y, button: 'left', buttons: 0, clickCount: 1 })
  await sleep(700)
  // 红（h=0）取 s=v=50% → (128, 64, 64)
  check('点色域中间 → 半亮度纯红 #804040', (await paramOf('fillColor')) === '#804040', String(await paramOf('fillColor')))
  const hue = await centerOf('.color-pop .color-hue') // 中心 = 色相 180°
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: hue.x, y: hue.y, button: 'left', buttons: 1, clickCount: 1 })
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: hue.x, y: hue.y, button: 'left', buttons: 0, clickCount: 1 })
  await sleep(700)
  // 只换色相、s/v 还是 50% → 青 (64, 128, 128)
  check('点色相条中间 → 色相到 180°（青）#408080',
        (await paramOf('fillColor')) === '#408080', String(await paramOf('fillColor')))
  for (let i = 0; i < 20; i++) {
    const s = await shotCenter()
    if (s.loaded && s.center?.[0] === 64 && s.center?.[1] === 128) break
    await sleep(150)
  }
  check('预览图跟着变成 (64, 128, 128)', (await shotCenter()).center?.join(',') === '64,128,128,255',
        JSON.stringify((await shotCenter()).center))

  console.log('\n[7] 关掉浮层：Esc / 点画布空白 / 再点一次色块')
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await sleep(250)
  check('Esc 关掉', (await fields()).open === false)
  await openField('主体色')
  check('再打开', (await fields()).open === true)
  const keep = await paramOf('fillColor')
  await click(60, 620) // 画布空白处
  await sleep(250)
  check('点浮层外面关掉', (await fields()).open === false)
  check('关浮层不影响已经改好的颜色', (await paramOf('fillColor')) === keep, `${keep} → ${await paramOf('fillColor')}`)

  console.log('\n[8] 第二个颜色参数（描边色）各走各的')
  await openField('描边色')
  f = await fields()
  check('描边色浮层显示的是它自己的值 #111111', f.hex === '#111111', String(f.hex))
  await typeAndEnter(0, '#00ff88')
  await sleep(700)
  check('描边色改成 #00ff88', (await paramOf('strokeColor')) === '#00ff88', String(await paramOf('strokeColor')))
  check('主体色没被带着动', (await paramOf('fillColor')) === keep, String(await paramOf('fillColor')))

  console.log('\n[9] 吸管按钮：在 HEX / RGB 的右侧、跨两行，点了就把取到的颜色填进来')
  await openField('主体色')
  const layout = await J(`(() => {
    const pop = document.querySelector('.color-pop')
    const eye = pop.querySelector('.color-eye')
    const ins = pop.querySelectorAll('input')
    const r = (el) => { const b = el.getBoundingClientRect(); return { l: Math.round(b.left), r: Math.round(b.right), w: Math.round(b.width), h: Math.round(b.height), t: Math.round(b.top), b: Math.round(b.bottom) } }
    return JSON.stringify({ eye: eye ? r(eye) : null, hex: r(ins[0]), rgb: r(ins[1]), pop: r(pop) })
  })()`)
  check('吸管按钮渲染出来了', !!layout.eye, JSON.stringify(layout.eye))
  check('它在两个输入框的右边（右列）', layout.eye && layout.eye.l >= layout.hex.r - 1, `eye.left=${layout.eye?.l} hex.right=${layout.hex.r}`)
  check('HEX / RGB 左边界对齐（一列）', layout.hex.l === layout.rgb.l, `${layout.hex.l} / ${layout.rgb.l}`)
  check('吸管跨两行（比一行高，约等于两行加行距）',
        layout.eye && layout.eye.h > layout.hex.h && layout.eye.h >= layout.hex.h + layout.rgb.h,
        `eye.h=${layout.eye?.h} hex.h=${layout.hex.h} rgb.h=${layout.rgb.h}`)
  check('吸管贴着浮层右边', layout.eye && Math.abs(layout.eye.r - (layout.pop.r - 10)) <= 1, `${layout.eye?.r} vs ${layout.pop.r}`)
  const eyePt = await centerOf('.color-pop .color-eye')
  await click(eyePt.x, eyePt.y)
  await sleep(700)
  // 参数落盘走 500ms 防抖，轮询等它落定（一次 sleep 卡在边界上会误报）
  let got = null
  for (let i = 0; i < 20; i++) {
    got = await paramOf('fillColor')
    if (got === '#123456') break
    await sleep(200)
  }
  check('点吸管 → 颜色变成它取到的 #123456', got === '#123456', String(got))
  f = await fields()
  check('两个框同步显示 18, 52, 86', f.hex === '#123456' && f.rgb === '18, 52, 86', `${f.hex} / ${f.rgb}`)
  await snap('layout')
  console.log('  本次跑过的 PATCH：')
  for (const b of patches) console.log('   ', b)

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
