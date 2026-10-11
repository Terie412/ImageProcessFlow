// 验证区域编辑器 / 大图查看器的交互：
//   1) 节点预览：序号在框外（贴顶边时缩回框内）
//   2) 编辑器：序号在框外、四角把手透明、每行有显示/隐藏按钮
//   3) 编辑器滚轮缩放：比例不变形 + 以鼠标位置为锚点 + 中键拖动平移
//   4) 查看器：滚轮缩放、中键平移、只有关闭按钮能关
// 全程用一次性临时工作目录，不碰用户真实画布；不调用任何模型。
import { spawn } from 'node:child_process'
import { readFileSync, rmSync, writeFileSync } from 'node:fs'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const PORT = 9400 + (process.pid % 400)
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const SRC = 'D:/Workspace/ImageProcessFlow/workspace/images/B.png'
const TMPWS = 'D:/Workspace/ImageProcessFlow/runtime/testws-uicheck'
// 每次跑用一个新的 profile 目录：不用去删旧的（沙箱里删目录可能被拦），也就不会
// 因为残留 profile 导致 Chrome 起不来。
const PROFILE = `D:/Workspace/ImageProcessFlow/runtime/tmp/chrome-regionui-${Date.now()}`
const OUT = 'D:/Workspace/ImageProcessFlow/runtime/'

// 5 个真实识别结果 + 1 个贴顶边的（用来验证序号缩回框内）
const REGIONS = [
  { x: 170, y: 2401, w: 81, h: 77 },
  { x: 442, y: 2397, w: 86, h: 84 },
  { x: 717, y: 2398, w: 108, h: 81 },
  { x: 1006, y: 2395, w: 88, h: 87 },
  { x: 1291, y: 2394, w: 77, h: 85 },
  { x: 40, y: 6, w: 120, h: 90 },
]

try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* 占着就占着 */ }
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
const req = (method, path, body) =>
  fetch(`${BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => r.json())

let ORIG_WS = null
const results = []
const check = (n, ok, d = '') => {
  results.push([ok, n, d])
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${n}${d ? '  — ' + d : ''}`)
}

async function main() {
  ORIG_WS = (await state()).workspace
  rmSync(TMPWS, { recursive: true, force: true })
  await req('POST', '/api/workspace', { path: TMPWS })

  const fd = new FormData()
  fd.append('file', new Blob([readFileSync(SRC)], { type: 'image/png' }), 'B.png')
  fd.append('resource_id', '')
  fd.append('x', '120')
  fd.append('y', '120')
  const st = await (await fetch(`${BASE}/api/resources/import`, { method: 'POST', body: fd })).json()
  const rid = st.graph.resources[st.graph.resources.length - 1].id
  const st2 = await req('POST', '/api/processes', { defId: 'crop', x: 700, y: 120 })
  const pid = st2.graph.processes[st2.graph.processes.length - 1].id
  await req('POST', '/api/edges', { from: rid, to: pid, port: 'image' })
  await req('PATCH', `/api/processes/${pid}`, { params: { regions: REGIONS } })
  console.log(`已建临时画布：数据节点 + 裁剪节点 ${pid}（${REGIONS.length} 个区域）`)

  let target = null
  for (let i = 0; i < 120; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      target = list.find((t) => t.type === 'page')
      if (target) break
    } catch { /* 等 Chrome */ }
    await sleep(300)
  }
  if (!target) throw new Error('Chrome 没起来')
  const ws = new WebSocket(target.webSocketDebuggerUrl)
  let id = 0
  const pending = new Map()
  const logs = []
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
      return
    }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails
      logs.push('[异常] ' + (d.exception?.description || d.text).split('\n').slice(0, 2).join(' | '))
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      logs.push('[console.error] ' + m.params.args.map((a) => a.value ?? a.description).join(' '))
    }
  }
  await new Promise((r) => (ws.onopen = r))
  await send('Runtime.enable')
  await send('Page.enable')
  await send('Page.navigate', { url: BASE })
  await sleep(5000)

  const js = async (expr) =>
    (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value
  const shot = async (name, clip) => {
    const r = await send('Page.captureScreenshot', clip ? { clip, format: 'png' } : { format: 'png' })
    writeFileSync(OUT + name, Buffer.from(r.data, 'base64'))
    console.log('  截图:', name)
  }
  const rectOf = (sel) =>
    js(`(() => { const e = document.querySelector(${JSON.stringify(sel)});
       if (!e) return null; const r = e.getBoundingClientRect();
       return { x: r.x, y: r.y, w: r.width, h: r.height, sw: e.scrollWidth, sh: e.scrollHeight,
                sl: e.scrollLeft, st: e.scrollTop } })()`)

  console.log('\n=== 1. 节点预览里的序号位置 ===')
  const tag = await js(`(() => { const e = document.querySelector('.region-box i');
     if (!e) return null; const b = e.getBoundingClientRect(); const p = e.parentElement.getBoundingClientRect();
     return { outside: b.bottom <= p.top + 1 } })()`)
  check('序号在框外（贴框上沿上方）', !!tag && tag.outside, JSON.stringify(tag))
  const tightTag = await js(`(() => { const t = [...document.querySelectorAll('.region-box')].find((b) => b.classList.contains('tight'));
     if (!t) return null; const i = t.querySelector('i').getBoundingClientRect(); const p = t.getBoundingClientRect();
     return { inside: i.top >= p.top - 1 } })()`)
  check('贴顶边的框：序号缩回框内（不被裁）', !!tightTag && tightTag.inside, JSON.stringify(tightTag))

  console.log('\n=== 2. 打开编辑器 ===')
  await js(`(() => { const e = document.querySelector('.region-shot'); const r = e.getBoundingClientRect();
     e.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, clientX: r.x + 10, clientY: r.y + 10 })) })()`)
  await sleep(700)
  check('编辑器已打开', await js(`!!document.querySelector('.editor')`))
  const etag = await js(`(() => { const e = document.querySelector('.ebox-tag');
     if (!e) return null; const c = getComputedStyle(e); const b = e.getBoundingClientRect();
     const p = e.parentElement.getBoundingClientRect();
     return { top: c.top, outside: b.bottom <= p.top + 1 } })()`)
  check('编辑器：序号也在框外', !!etag && etag.outside, JSON.stringify(etag))
  const handle = await js(`(() => { const h = document.querySelector('.ebox-h'); const c = getComputedStyle(h);
     return { bg: c.backgroundColor, border: c.borderTopWidth } })()`)
  check('编辑器：四角把手纯透明', handle.bg === 'rgba(0, 0, 0, 0)' && handle.border === '0px', JSON.stringify(handle))
  const eyes = await js(`({ total: document.querySelectorAll('.erow-eye').length,
     on: document.querySelectorAll('.erow-eye.on').length, boxes: document.querySelectorAll('.ebox').length })`)
  check('每行都有显示/隐藏按钮，默认全显示', eyes.total === REGIONS.length && eyes.on === REGIONS.length, JSON.stringify(eyes))
  await shot('uicheck_2_editor.png')

  console.log('\n=== 3. 编辑器滚轮缩放：比例 + 鼠标锚点 ===')
  const c0 = await rectOf('.editor-canvas')
  const AX = c0.x + c0.w * 0.62
  const AY = c0.y + c0.h * 0.72
  await js(`(() => { const e = document.querySelector('.editor-stage');
     e.dispatchEvent(new WheelEvent('wheel', { deltaY: -900, clientX: ${AX}, clientY: ${AY}, bubbles: true, cancelable: true })) })()`)
  await sleep(500)
  const c1 = await rectOf('.editor-canvas')
  const ratio0 = c0.w / c0.h
  const ratio1 = c1.w / c1.h
  check('放大后不形变（宽高比不变）', Math.abs(ratio1 / ratio0 - 1) < 0.01,
    `${ratio0.toFixed(4)} → ${ratio1.toFixed(4)}（${Math.round(c0.w)}×${Math.round(c0.h)} → ${Math.round(c1.w)}×${Math.round(c1.h)}）`)
  const fx1 = (AX - c1.x) / c1.w
  const fy1 = (AY - c1.y) / c1.h
  check('缩放锚在鼠标位置（同一像素仍停在光标下）',
    Math.abs(fx1 - 0.62) < 0.02 && Math.abs(fy1 - 0.72) < 0.02,
    `锚点 ${fx1.toFixed(3)}/${fy1.toFixed(3)}（期望 0.620/0.720）`)
  const st1 = await rectOf('.editor-stage')
  check('内容超出后出现滚动范围', st1.sw > st1.w + 1 && st1.sh > st1.h + 1,
    `scroll ${Math.round(st1.sw)}×${Math.round(st1.sh)} vs 视口 ${Math.round(st1.w)}×${Math.round(st1.h)}`)

  console.log('\n=== 3b. 点百分比回到适应窗口 + 放大到区域上（对照用户截图） ===')
  await js(`(() => { const b = [...document.querySelectorAll('.editor-head button')].find((x) => x.textContent.includes('%'));
     if (b) b.click() })()`)
  await sleep(400)
  const cReset = await rectOf('.editor-canvas')
  check('点百分比可以回到适应窗口', Math.abs(cReset.w / c0.w - 1) < 0.02, `${Math.round(c0.w)} → ${Math.round(cReset.w)}`)
  // 锚到第一个区域附近（底部导航栏在 88% 高度处），放大看细节
  const RX = cReset.x + cReset.w * 0.16
  const RY = cReset.y + cReset.h * 0.885
  await js(`(() => { const e = document.querySelector('.editor-stage');
     e.dispatchEvent(new WheelEvent('wheel', { deltaY: -1620, clientX: ${RX}, clientY: ${RY}, bubbles: true, cancelable: true })) })()`)
  await sleep(600)
  const cBig = await rectOf('.editor-canvas')
  const bigRatio = (cBig.w / cBig.h) / (c0.w / c0.h)
  check('高倍放大后依然不变形', Math.abs(bigRatio - 1) < 0.01,
    `${Math.round(cBig.w)}×${Math.round(cBig.h)}，宽高比偏差 ${((bigRatio - 1) * 100).toFixed(2)}%`)
  const bigAnchor = ((RY - cBig.y) / cBig.h)
  check('高倍放大时锚点依然准', Math.abs(bigAnchor - 0.885) < 0.02, bigAnchor.toFixed(3))
  await shot('uicheck_5_editor_big.png')

  console.log('\n=== 4. 编辑器中键拖动平移 ===')
  const before = await rectOf('.editor-stage')
  const bx = before.x + before.w / 2
  const by = before.y + before.h / 2
  await js(`(() => { const e = document.querySelector('.editor-stage');
     e.dispatchEvent(new MouseEvent('mousedown', { button: 1, buttons: 4, bubbles: true, cancelable: true, clientX: ${bx}, clientY: ${by} }));
     window.dispatchEvent(new MouseEvent('mousemove', { buttons: 4, bubbles: true, clientX: ${bx - 160}, clientY: ${by - 120} }));
     window.dispatchEvent(new MouseEvent('mouseup', { button: 1, bubbles: true })) })()`)
  await sleep(300)
  const afterPan = await rectOf('.editor-stage')
  check('中键拖动把视图挪动了',
    Math.abs(afterPan.sl - before.sl) > 100 && Math.abs(afterPan.st - before.st) > 50,
    `scroll ${Math.round(before.sl)},${Math.round(before.st)} → ${Math.round(afterPan.sl)},${Math.round(afterPan.st)}`)
  await shot('uicheck_3_editor_zoom.png')

  console.log('\n=== 5. 单区域显示/隐藏 ===')
  await js(`document.querySelectorAll('.erow-eye')[0].click()`)
  await sleep(300)
  const after = await js(`({ boxes: document.querySelectorAll('.ebox').length, off: document.querySelectorAll('.erow.off').length })`)
  check('隐藏一个 → 图上少一个框', after.boxes === REGIONS.length - 1 && after.off === 1, JSON.stringify(after))
  await js(`document.querySelectorAll('.erow-eye')[0].click()`)
  await sleep(300)
  check('再点一下 → 又显示出来', (await js(`document.querySelectorAll('.ebox').length`)) === REGIONS.length)
  await js(`document.querySelector('.editor-mask').dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))`)
  await sleep(400)
  check('编辑器已关闭且没写回参数', await js(`!document.querySelector('.editor')`))

  console.log('\n=== 6. 查看器：缩放 / 平移 / 关闭条件 ===')
  await js(`(() => { const n = document.querySelector('.react-flow__node-resource'); const r = n.getBoundingClientRect();
     n.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, clientX: r.x + 20, clientY: r.y + 10 })) })()`)
  await sleep(900)
  check('查看器已打开', await js(`!!document.querySelector('.viewer-mask')`))

  const v0 = await rectOf('.viewer-stage img')
  const VX = v0.x + v0.w * 0.4
  const VY = v0.y + v0.h * 0.4
  await js(`(() => { const e = document.querySelector('.viewer-stage');
     e.dispatchEvent(new WheelEvent('wheel', { deltaY: -1600, clientX: ${VX}, clientY: ${VY}, bubbles: true, cancelable: true })) })()`)
  await sleep(500)
  const v1 = await rectOf('.viewer-stage img')
  check('查看器滚轮放大', v1.w > v0.w * 1.3, `${Math.round(v0.w)} → ${Math.round(v1.w)}`)
  check('查看器放大后也不形变', Math.abs((v1.w / v1.h) / (v0.w / v0.h) - 1) < 0.01,
    `${(v0.w / v0.h).toFixed(4)} → ${(v1.w / v1.h).toFixed(4)}`)
  const vfx = (VX - v1.x) / v1.w
  check('查看器缩放也锚在鼠标位置', Math.abs(vfx - 0.4) < 0.02, vfx.toFixed(3))

  const pv = await rectOf('.viewer-stage')
  await js(`(() => { const e = document.querySelector('.viewer-stage');
     e.dispatchEvent(new MouseEvent('mousedown', { button: 1, buttons: 4, bubbles: true, cancelable: true, clientX: ${VX}, clientY: ${VY} }));
     window.dispatchEvent(new MouseEvent('mousemove', { buttons: 4, bubbles: true, clientX: ${VX - 140}, clientY: ${VY - 90} }));
     window.dispatchEvent(new MouseEvent('mouseup', { button: 1, bubbles: true })) })()`)
  await sleep(300)
  const pv2 = await rectOf('.viewer-stage')
  check('查看器中键拖动平移', Math.abs(pv2.sl - pv.sl) > 80 && Math.abs(pv2.st - pv.st) > 40,
    `scroll ${Math.round(pv.sl)},${Math.round(pv.st)} → ${Math.round(pv2.sl)},${Math.round(pv2.st)}`)

  await js(`(() => { const e = document.querySelector('.viewer-stage');
     e.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: ${VX}, clientY: ${VY} }));
     e.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: ${VX}, clientY: ${VY} }));
     const m = document.querySelector('.viewer-mask');
     m.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 30, clientY: 700 }));
     m.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: 30, clientY: 700 })) })()`)
  await sleep(400)
  check('点画面 / 点背景都不再关窗口', await js(`!!document.querySelector('.viewer-mask')`))
  await shot('uicheck_4_viewer_zoom.png')

  const closed = await js(`(() => {
     const b = [...document.querySelectorAll('.viewer-bar button')].find((x) => x.textContent.includes('关闭'));
     if (!b) return '找不到关闭按钮';
     b.click(); return 'clicked' })()`)
  await sleep(400)
  check('只有关闭按钮能关（点了就关）', closed === 'clicked' && !(await js(`!!document.querySelector('.viewer-mask')`)), String(closed))

  console.log('\n控制台异常:', logs.length ? logs.slice(0, 8).join(' ;; ') : '（无）')
  ws.close()
  chrome.kill()

  const bad = results.filter((r) => !r[0])
  console.log(`\n共 ${results.length} 项，通过 ${results.length - bad.length}，失败 ${bad.length}`)
  return bad.length
}

main()
  .then(async (code) => {
    if (ORIG_WS) await req('POST', '/api/workspace', { path: ORIG_WS })
    await sleep(400)
    try { rmSync(TMPWS, { recursive: true, force: true }) } catch { /* 忽略 */ }
    console.log('已恢复工作目录:', ORIG_WS)
    process.exit(code)
  })
  .catch(async (e) => {
    console.error('ERR', e.message)
    if (ORIG_WS) {
      try { await req('POST', '/api/workspace', { path: ORIG_WS }) } catch { /* ignore */ }
    }
    try { rmSync(TMPWS, { recursive: true, force: true }) } catch { /* 忽略 */ }
    chrome.kill()
    process.exit(1)
  })
