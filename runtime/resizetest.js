// 验证节点右下角的手柄：能拖拽改尺寸、松手落盘、刷新后保持、受最小尺寸限制。
// 全程在一次性临时工作目录里跑，不碰用户的真实画布。
import { spawn } from 'node:child_process'
import { readFileSync, rmSync } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9229
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const SEED = `${RUNTIME}/seed.png`
const TMPWS = `${RUNTIME}/testws-resize`
const PROFILE = `${RUNTIME}/chrome-profile-resize`

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
const post = (path, body) =>
  fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json())

// 设置接口是 PATCH，别拿上面的 POST 去发 —— 405 会被 .then(r => r.json()) 悄悄吞掉
const patch = (path, body) =>
  fetch(`${BASE}${path}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json())

async function makeDataNode(x, y) {
  const fd = new FormData()
  fd.append('file', new Blob([readFileSync(SEED)], { type: 'image/png' }), 'seed.png')
  fd.append('resource_id', '')
  fd.append('x', String(x))
  fd.append('y', String(y))
  const r = await (await fetch(`${BASE}/api/resources/import`, { method: 'POST', body: fd })).json()
  return r.graph.resources[r.graph.resources.length - 1].id
}

let ORIG_WS = null
let ORIG_SCALE = null

async function main() {
  const results = []
  const check = (n, ok, d) => {
    results.push([ok, n, d])
    console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${n}${d ? '  — ' + d : ''}`)
  }

  const origWs = (ORIG_WS = (await state()).workspace)
  rmSync(TMPWS, { recursive: true, force: true })
  await post('/api/workspace', { path: TMPWS })
  console.log(`已切到一次性工作目录（原目录: ${origWs}）`)

  await makeDataNode(60, 120)
  // 再放一个远的节点把视野撑开：既要让 fitView 的缩放别太大，
  // 也要避免处理节点被画布右下角的 MiniMap 压住（那会挡住缩放手柄）
  await makeDataNode(60, 1100)
  const st = await post('/api/processes', { defId: 'ai_image', x: 420, y: 120 })
  const pid = st.graph.processes[st.graph.processes.length - 1].id
  // 下限要取**服务端算出来的有效值**（sizeSpec），不能只看 settings.nodeMin.process ——
  // 那只是通用兜底，`process:<defId>` 可以单独覆盖（本项目里 ai_image 就被覆盖成 200×200）
  const qspec = (await state()).sizeSpec['process:ai_image']
  const msz = qspec ? { w: qspec.minW, h: qspec.minH } : (await state()).settings.nodeMin.process

  let target = null
  for (let i = 0; i < 120; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      target = list.find((t) => t.type === 'page')
      if (target) break
    } catch { /* 等启动 */ }
    await sleep(300)
  }
  if (!target) {
    await post('/api/workspace', { path: origWs })
    rmSync(TMPWS, { recursive: true, force: true })
    throw new Error('找不到调试目标')
  }

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

  const handleBox = async (nodeId) =>
    JSON.parse(
      await evalJs(`(() => {
        const h = document.querySelector('.react-flow__node[data-id="${nodeId}"] .resize-handle')
        if (!h) return 'null'
        const r = h.getBoundingClientRect()
        return JSON.stringify({ x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) })
      })()`),
    )
  const nodeBox = async (nodeId) =>
    JSON.parse(
      await evalJs(`(() => {
        const n = document.querySelector('.react-flow__node[data-id="${nodeId}"]')
        const r = n.getBoundingClientRect()
        return JSON.stringify({ w: Math.round(r.width), h: Math.round(r.height) })
      })()`),
    )
  const zoom = async () => evalJs(`(() => {
    const t = document.querySelector('.react-flow__viewport')?.style.transform || ''
    const m = t.match(/scale\\(([\\d.]+)\\)/)
    return m ? Number(m[1]) : 1
  })()`)

  async function drag(x, y, dx, dy) {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 })
    await sleep(80)
    for (let i = 1; i <= 8; i++) {
      await send('Input.dispatchMouseEvent', {
        type: 'mouseMoved', x: Math.round(x + (dx * i) / 8), y: Math.round(y + (dy * i) / 8),
        button: 'left', buttons: 1,
      })
      await sleep(35)
    }
    await send('Input.dispatchMouseEvent', {
      type: 'mouseReleased', x: x + dx, y: y + dy, button: 'left', buttons: 0, clickCount: 1,
    })
    await sleep(500)
  }

  try {
    await send('Page.navigate', { url: BASE })
    await sleep(3800)

    const z = await zoom()
    console.log(`   当前缩放 ${z.toFixed(3)}`)

    console.log('\n[1] 手柄存在')
    check('处理节点右下角有缩放手柄', (await handleBox(pid)) !== null)
    check('数据节点右下角也有缩放手柄',
      (await handleBox((await state()).graph.resources[0].id)) !== null)

    console.log('\n[2] 拉大处理节点')
    const before = await nodeBox(pid)
    let h = await handleBox(pid)
    console.log(`   手柄坐标: ${h.x},${h.y}`)
    console.log(`   视口: ${await evalJs('innerWidth + "x" + innerHeight')}`)
    console.log(`   该坐标命中: ${await evalJs(`(() => {
      const el = document.elementFromPoint(${h.x}, ${h.y})
      return el ? ((el.className || '') + '' || el.tagName) : 'null（在视口外）'
    })()`)}`)
    await drag(h.x, h.y, 160, 120)
    console.log(`   onResizeStart 调用次数: ${await evalJs('window.__resizeDebug ?? 0')}`)
    const after = await nodeBox(pid)
    console.log(`   ${before.w}×${before.h}  →  ${after.w}×${after.h}`)
    check('宽度变大了', after.w > before.w, `${before.w} → ${after.w}`)
    check('高度变大了', after.h > before.h, `${before.h} → ${after.h}`)

    console.log('\n[3] 尺寸是否落盘')
    await sleep(600)
    const saved = (await state()).graph.processes.find((p) => p.id === pid)
    console.log(`   graph 里: w=${saved.w}  h=${saved.h}`)
    check('尺寸写进了 graph', typeof saved.w === 'number' && typeof saved.h === 'number',
      `w=${saved.w} h=${saved.h}`)
    check('落盘的尺寸与界面一致', Math.abs(saved.w - after.w / z) < 3,
      `${saved.w} vs ${(after.w / z).toFixed(1)}`)

    console.log('\n[4] 刷新页面后保持')
    await send('Page.navigate', { url: BASE })
    await sleep(3800)
    const afterReload = await nodeBox(pid)
    console.log(`   刷新后 ${afterReload.w}×${afterReload.h}`)
    check('刷新后尺寸没丢', Math.abs(afterReload.w - after.w) < 4 && Math.abs(afterReload.h - after.h) < 4,
      `${after.w}×${after.h} → ${afterReload.w}×${afterReload.h}`)

    console.log(`\n[5] 往小拖会被最小值拦住（effective min: ${msz.w}×${msz.h}，来自 sizeSpec）`)
    h = await handleBox(pid)
    await drag(h.x, h.y, -900, -900)
    const shrunk = await nodeBox(pid)
    const minW = Math.round(msz.w * z)
    const minH = Math.round(msz.h * z)
    console.log(`   拖到极限后 ${shrunk.w}×${shrunk.h}  （最小应为 ${minW}×${minH}）`)
    check('宽度被最小值挡住', shrunk.w >= minW - 3, `${shrunk.w} >= ${minW}`)
    check('高度被最小值挡住', shrunk.h >= minH - 3, `${shrunk.h} >= ${minH}`)

    console.log('\n[6] 缩放吸附：宽高吸到步长的整数倍（0 = 关闭）')
    ORIG_SCALE = (await state()).settings.scaleSnap
    // 设置是页面启动时读进 React state 的，改完必须重新加载才生效
    const setScale = async (v) => {
      await patch('/api/settings', { scaleSnap: v })
      await send('Page.navigate', { url: BASE })
      await sleep(3800)
    }
    const sizeNow = async () => {
      const p = (await state()).graph.processes.find((x) => x.id === pid)
      return { w: p.w, h: p.h }
    }
    const dragHandle = async (dx, dy) => {
      const hb = await handleBox(pid)
      await drag(hb.x, hb.y, dx, dy)
      await sleep(600)
      return sizeNow()
    }

    await setScale(25)
    // 先确认设置真的落到页面里了 —— 发错方法（PATCH 写成 POST）时接口只回 405，
    // 不校验的话整段会拿着**默认的 10** 跑，还看不出来
    const eff = (await state()).settings.scaleSnap
    check('设置已生效：服务端 scaleSnap = 25', eff === 25, String(eff))
    const b25 = await sizeNow()
    const r25 = await dragHandle(120, 90)
    console.log(`   步长 25：${b25.w}×${b25.h} → ${r25.w}×${r25.h}`)
    check('步长 25：确实被拖大了（不是没拖动）',
      r25.w > b25.w && r25.h > b25.h, `${b25.w}×${b25.h} → ${r25.w}×${r25.h}`)
    check('步长 25：落盘宽度是 25 的整数倍', r25.w % 25 === 0, `w=${r25.w}`)
    check('步长 25：落盘高度是 25 的整数倍', r25.h % 25 === 0, `h=${r25.h}`)

    // 0 = 关闭。从同一个起点只拖 3 个**屏幕**像素 —— 换算成画布像素是 round(3/zoom)，
    // 只要缩放不小于 MIN_ZOOM(0.15) 它就落在 2~20，绝不可能是 25 的倍数。起点是
    // 25 的整数倍，所以「前后都不再是整数倍」这条断言是算出来的，不靠运气。
    await setScale(0)
    const b0 = await sizeNow()
    const r0 = await dragHandle(3, 3)
    console.log(`   步长 0：${b0.w}×${b0.h} → ${r0.w}×${r0.h}（画布位移 ${r0.w - b0.w}）`)
    check('关闭吸附后拖动仍然有效', r0.w > b0.w, `${b0.w} → ${r0.w}`)
    check('关闭吸附后宽度不再被吸到 25 的整数倍', r0.w % 25 !== 0, `w=${r0.w}`)
    check('关闭吸附后高度不再被吸到 25 的整数倍', r0.h % 25 !== 0, `h=${r0.h}`)
  } finally {
    ws.close()
    chrome.kill()
    if (ORIG_SCALE !== null) {
      try { await patch('/api/settings', { scaleSnap: ORIG_SCALE }) } catch { /* 忽略 */ }
    }
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
  // 这里保证工作目录与缩放吸附设置一定被还回去
  if (ORIG_SCALE !== null) {
    try { await patch('/api/settings', { scaleSnap: ORIG_SCALE }) } catch { /* 忽略 */ }
  }
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
