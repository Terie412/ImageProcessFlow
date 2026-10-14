// 验证双击数据节点打开的大图查看器：打开 / 信息正确 / 适应窗口↔实际大小 / Esc 关闭
// 全程在一次性临时工作目录里跑，不碰用户的真实画布。
// 依赖测试图 runtime/viewer_big.png（3000×2000 带透明区），与 seed.png 一样是固定素材。
import { spawn } from 'node:child_process'
import { existsSync, readFileSync, rmSync } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9236
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const TMPWS = `${RUNTIME}/testws-viewer`
const PROFILE = `${RUNTIME}/chrome-profile-viewer`
const BIG = `${RUNTIME}/viewer_big.png`

try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着目录 */ }
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
const state = async () => (await fetch(`${BASE}/api/state`)).json()
const req = (method, path, body) =>
  fetch(`${BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => r.json())
const post = (p, b) => req('POST', p, b)

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

  // 造一张超大的透明图：既能验证「实际大小」（会超出窗口），也能验证透明底纹
  if (!existsSync(BIG)) {
    throw new Error(`缺少测试图 ${BIG}，请先跑 runtime/make_viewer_image.py 生成`)
  }
  const fd = new FormData()
  const { readFileSync } = await import('node:fs')
  fd.append('file', new Blob([readFileSync(BIG)], { type: 'image/png' }), 'big.png')
  fd.append('resource_id', '')
  fd.append('x', '200')
  fd.append('y', '150')
  const st = await (await fetch(`${BASE}/api/resources/import`, { method: 'POST', body: fd })).json()
  const rid = st.graph.resources[st.graph.resources.length - 1].id
  const name = st.graph.resources[st.graph.resources.length - 1].name
  const proc = await post('/api/processes', { defId: 'ai_image', x: 900, y: 600 })
  const pid = proc.graph.processes[proc.graph.processes.length - 1].id
  console.log(`已建测试画布：数据节点 ${name}（3000×2000）+ 一个处理节点`)

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
  // 真实双击：CDP 要发 clickCount 1 + 2 两对事件才会产生 dblclick
  const dblclick = async (x, y) => {
    await click(x, y, 1)
    await click(x, y, 2)
    await sleep(600)
  }
  const centerOf = async (selector) =>
    J(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)})
      if (!el) return 'null'
      const r = el.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
  // 取元素上「确实落在视口内」的一点（actual 模式下大图会超出窗口，直接取中心可能点空）
  const visiblePointOf = async (selector) =>
    J(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)})
      if (!el) return 'null'
      const r = el.getBoundingClientRect()
      const x = Math.min(Math.max(r.left + Math.min(r.width / 2, 240), 20), innerWidth - 20)
      const y = Math.min(Math.max(r.top + Math.min(r.height / 2, 240), 70), innerHeight - 20)
      const hit = document.elementFromPoint(x, y)
      return JSON.stringify({ x: Math.round(x), y: Math.round(y), hit: hit ? hit.tagName : 'null' })
    })()`)
  const viewerState = async () =>
    J(`(() => {
      const m = document.querySelector('.viewer-mask')
      if (!m) return JSON.stringify({ open: false })
      const img = m.querySelector('.viewer-stage img')
      const stage = m.querySelector('.viewer-stage')
      const cs = img ? getComputedStyle(img) : null
      return JSON.stringify({
        open: true,
        cls: stage.className,
        name: m.querySelector('.viewer-bar .badge')?.textContent || '',
        meta: m.querySelector('.viewer-meta')?.textContent?.replace(/\\s+/g, ' ').trim() || '',
        buttons: [...m.querySelectorAll('.viewer-bar button')].map((b) => b.textContent.trim()),
        src: img ? img.getAttribute('src') : '',
        natural: img ? [img.naturalWidth, img.naturalHeight] : null,
        shown: img ? [Math.round(img.getBoundingClientRect().width), Math.round(img.getBoundingClientRect().height)] : null,
        checker: cs ? cs.backgroundImage.includes('linear-gradient') : false,
        scrollable: stage ? stage.scrollWidth > stage.clientWidth || stage.scrollHeight > stage.clientHeight : false,
      })
    })()`)

  try {
    await send('Page.navigate', { url: BASE })
    await sleep(4000)

    console.log('\n[1] 双击数据节点打开查看器')
    check('双击前没有查看器', (await viewerState()).open === false)
    await dblclick((await centerOf(`.react-flow__node[data-id="${rid}"] .thumb`)).x,
                   (await centerOf(`.react-flow__node[data-id="${rid}"] .thumb`)).y)
    let v = await viewerState()
    console.log(`   打开: ${v.open}  名称 ${v.name}  信息 ${JSON.stringify(v.meta)}  按钮 ${JSON.stringify(v.buttons)}`)
    check('查看器打开了', v.open === true)
    check('显示的是这张图', v.name === name && v.src.includes(`/api/images/${name}`), `${v.name} / ${v.src}`)
    check('显示分辨率与格式', v.meta === '3000 × 2000 · PNG', v.meta)
    check('图片真的加载出来了', v.natural[0] === 3000 && v.natural[1] === 2000, JSON.stringify(v.natural))

    console.log('\n[2] 默认「适应窗口」')
    console.log(`   模式 ${v.cls}  渲染尺寸 ${v.shown}  可滚动 ${v.scrollable}`)
    check('默认是 fit 模式', v.cls.includes('fit'), v.cls)
    check('缩小到窗口内（≤1576 宽）', v.shown[0] <= 1577 && v.shown[0] > 300, JSON.stringify(v.shown))
    check('fit 模式不出滚动条', v.scrollable === false)

    console.log('\n[3] 切到「实际大小」')
    const btn = await centerOf('.viewer-bar button')
    await click(btn.x, btn.y, 1)
    await sleep(400)
    v = await viewerState()
    console.log(`   模式 ${v.cls}  渲染尺寸 ${v.shown}  可滚动 ${v.scrollable}  按钮 ${JSON.stringify(v.buttons)}`)
    check('切成 actual 模式', v.cls.includes('actual'), v.cls)
    check('按 1:1 显示（3000px 宽）', Math.abs(v.shown[0] - 3000) <= 2, JSON.stringify(v.shown))
    check('超出窗口时出现滚动', v.scrollable === true)
    check('按钮文字变成「适应窗口」', v.buttons[0] === '适应窗口', v.buttons.join('/'))

    console.log('\n[4] 透明区域有棋盘格底纹')
    check('图片带了棋盘格背景', v.checker === true)

    console.log('\n[5] 双击图片也能切换缩放')
    const imgC = await visiblePointOf('.viewer-stage img')
    console.log(`   落点 ${imgC.x},${imgC.y}（该点命中 ${imgC.hit}）`)
    check('取到的点确实落在图片上', imgC.hit === 'IMG', imgC.hit)
    await dblclick(imgC.x, imgC.y)
    v = await viewerState()
    check('双击图片切回 fit', v.cls.includes('fit'), v.cls)

    console.log('\n[6] 关闭方式')
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
    await sleep(400)
    check('Esc 能关掉', (await viewerState()).open === false)

    await dblclick((await centerOf(`.react-flow__node[data-id="${rid}"] .thumb`)).x,
                   (await centerOf(`.react-flow__node[data-id="${rid}"] .thumb`)).y)
    check('能再次打开', (await viewerState()).open === true)
    const closeBtn = await J(`(() => {
      const bs = [...document.querySelectorAll('.viewer-bar button')]
      const b = bs[bs.length - 1].getBoundingClientRect()
      return JSON.stringify({ x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) })
    })()`)
    await click(closeBtn.x, closeBtn.y, 1)
    await sleep(400)
    check('「关闭」按钮能关掉', (await viewerState()).open === false)

    await dblclick((await centerOf(`.react-flow__node[data-id="${rid}"] .thumb`)).x,
                   (await centerOf(`.react-flow__node[data-id="${rid}"] .thumb`)).y)
    check('第三次能打开', (await viewerState()).open === true)
    await click(60, 700, 1) // 点图片外面的暗色区域
    await sleep(400)
    check('点空白处能关掉', (await viewerState()).open === false)

    console.log('\n[7] 丈量：按住左键拖出半透明框，顶栏给图内像素尺寸')

    // 前面那步「点空白处能关掉」当前是失败的（查看器没有点暗处关闭），所以这里
    // 先自己确保状态：开着就用 Esc 关掉，再从数据节点双击打开 —— 初始必是「适应窗口」
    if ((await viewerState()).open) {
      await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
      await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
      await sleep(400)
    }
    const thumb = await centerOf(`.react-flow__node[data-id="${rid}"] .thumb`)
    await dblclick(thumb.x, thumb.y)
    const v7 = await viewerState()
    check('重新打开查看器，且是「适应窗口」', v7.open === true && v7.cls.includes('fit'), v7.cls)

    // 图内像素 ←→ 屏幕坐标（当前缩放多少都成立）
    const imgRect = async () =>
      J(`(() => {
        const el = document.querySelector('.viewer-stage img')
        const r = el.getBoundingClientRect()
        return JSON.stringify({
          left: r.left, top: r.top, w: r.width, h: r.height,
          nw: el.naturalWidth, nh: el.naturalHeight,
        })
      })()`)
    const toScreen = (R, ix, iy) => ({
      x: Math.round(R.left + (ix / R.nw) * R.w),
      y: Math.round(R.top + (iy / R.nh) * R.h),
    })
    // 反向换算：屏幕坐标 → 图内像素（与被测代码同一套公式，用来算**期望值**）。
    // 这样断言与当前缩放无关 —— 直接写死 600 的话，缩放到 0.377 时屏幕坐标一取整
    // 就会差出两三个图内像素，误报。
    const toImg = (R, p) => ({
      x: Math.round(Math.max(0, Math.min(R.nw, ((p.x - R.left) / R.w) * R.nw))),
      y: Math.round(Math.max(0, Math.min(R.nh, ((p.y - R.top) / R.h) * R.nh))),
    })
    const readout = async () =>
      (await evalJs(`(document.querySelector('.viewer-measure')?.textContent || '').trim()`))
    const markBox = async () =>
      J(`(() => {
        const el = document.querySelector('.viewer-mark')
        if (!el) return 'null'
        const r = el.getBoundingClientRect()
        const i = document.querySelector('.viewer-stage img').getBoundingClientRect()
        return JSON.stringify({
          dx: Math.round(r.left - i.left), dy: Math.round(r.top - i.top),
          w: Math.round(r.width), h: Math.round(r.height),
        })
      })()`)
    const drag = async (from, to, mid) => {
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: from.x, y: from.y, button: 'left', buttons: 1, clickCount: 1 })
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: to.x, y: to.y, button: 'left', buttons: 1 })
      await sleep(140)
      const during = mid ? await mid() : null
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: to.x, y: to.y, button: 'left', buttons: 0, clickCount: 1 })
      await sleep(140)
      return during
    }
    const toggleFit = async () => {
      const b = await J(`(() => {
        const el = [...document.querySelectorAll('.viewer-bar button')][0]
        const r = el.getBoundingClientRect()
        return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
      })()`)
      await click(b.x, b.y, 1)
      await sleep(500)
    }

    // 两种显示模式各量一遍：**适应窗口是缩放显示的**（最能暴露比例换算错误），实际大小是 1:1
    let fwdText = ''
    for (const tag of ['适应窗口', '实际大小']) {
      if (tag === '实际大小') await toggleFit()
      const R = await imgRect()
      const A = toScreen(R, 300, 200)
      const B = toScreen(R, 900, 800)
      const ea = toImg(R, A)
      const eb = toImg(R, B)
      const exp = {
        w: Math.abs(eb.x - ea.x),
        h: Math.abs(eb.y - ea.y),
        x: Math.min(ea.x, eb.x),
        y: Math.min(ea.y, eb.y),
      }
      const during = await drag(A, B, async () => ({ box: await markBox(), text: await readout() }))
      const scale = R.w / R.nw
      check(`[${tag}] 拖动中画出框，读数 = 屏幕坐标换算出的图内像素`,
        during.box !== null && during.text.includes(`${exp.w} × ${exp.h}`) && during.text.includes(`(${exp.x}, ${exp.y})`),
        `${during.text}  期望 ${exp.w} × ${exp.h} @ (${exp.x}, ${exp.y})  缩放 ${scale.toFixed(3)}`)
      check(`[${tag}] 框的渲染尺寸按当前缩放吻合`,
        Math.abs(during.box.w - exp.w * scale) <= 2 && Math.abs(during.box.h - exp.h * (R.h / R.nh)) <= 2,
        `${JSON.stringify(during.box)} vs 期望 ${Math.round(exp.w * scale)}`)
      check(`[${tag}] 松手后框消失、读数与拖动中一致`,
        (await markBox()) === null && (await readout()) === during.text, await readout())
      if (tag === '实际大小') {
        fwdText = during.text
      }
    }

    const RF = await imgRect()
    const A0 = toScreen(RF, 300, 200)
    const B0 = toScreen(RF, 900, 800)

    // 反向拖（右下 → 左上）：宽高仍是正的，起点仍取左上角 —— 读数应与正向完全一致
    await drag(B0, A0)
    check('反向拖的读数与正向一致', (await readout()) === fwdText, await readout())

    // 拖出图片外 → 夹回图内（右下角 = 图片右下角）
    const outside = { x: RF.left + RF.w + 400, y: RF.top + RF.h + 400 }
    await drag(A0, outside)
    const clamped = await readout()
    const ca = toImg(RF, A0)
    const expC = { w: RF.nw - ca.x, h: RF.nh - ca.y, x: ca.x, y: ca.y }
    check('拖出图外会夹回图内（右下角 = 图片右下角）',
      clamped.includes(`${expC.w} × ${expC.h}`) && clamped.includes(`(${expC.x}, ${expC.y})`),
      `${clamped}  期望 ${expC.w} × ${expC.h} @ (${expC.x}, ${expC.y})`)

    // 单击（没拖动）不留读数
    await click(A0.x, A0.y, 1)
    await sleep(140)
    check('单击一下把上一次的读数清掉', (await readout()).includes('测量 —'), await readout())

    if ((await viewerState()).open) {
      await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
      await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
      await sleep(400)
    }

    console.log('\n[8] 处理节点双击不打开查看器')
    await dblclick((await centerOf(`.react-flow__node[data-id="${pid}"] .node-head`)).x,
                   (await centerOf(`.react-flow__node[data-id="${pid}"] .node-head`)).y)
    check('处理节点双击无反应', (await viewerState()).open === false)
  } finally {
    ws.close()
    chrome.kill()
    await post('/api/workspace', { path: origWs })
    rmSync(TMPWS, { recursive: true, force: true })
    try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着目录 */ }
    console.log(`\n已恢复工作目录: ${origWs}`)
  }

  const bad = results.filter((r) => !r[0])
  console.log(`\n共 ${results.length} 项，通过 ${results.length - bad.length}，失败 ${bad.length}`)
  process.exit(bad.length ? 1 : 0)
}

main().catch(async (e) => {
  console.error('ERR', e.message)
  chrome.kill()
  // 兜底：setup 阶段就抛错时，脚本自己的 finally 还没轮到执行 ——
  // 这里保证工作目录一定被还回去，不会卡在一次性测试目录上
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
