// 验证：按住可交互元素拖动不应移动节点；按住标题栏/端口行应能移动
// 全程在一次性临时工作目录里跑，不碰用户的真实工作目录。
import { spawn } from 'node:child_process'
import { rmSync } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9223
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const TMPWS = `${RUNTIME}/testws-drag`

const chrome = spawn(
  CHROME,
  [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--no-first-run', '--no-default-browser-check',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${RUNTIME}/chrome-profile`,
    '--window-size=1400,1000',
    'about:blank',
  ],
  { stdio: 'ignore' },
)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const post = (path, body) =>
  fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json())

let ORIG_WS = null

async function main() {
  const results = []
  const check = (n, ok, d) => {
    results.push([ok, n, d])
    console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${n}${d ? '  — ' + d : ''}`)
  }

  const originalWs = (ORIG_WS = (await (await fetch(`${BASE}/api/state`)).json()).workspace)
  rmSync(TMPWS, { recursive: true, force: true })
  await post('/api/workspace', { path: TMPWS })
  await post('/api/processes', { defId: 'ai_image', x: 200, y: 150 })
  console.log(`已切到一次性工作目录（原目录: ${originalWs}）`)

  let target = null
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      target = list.find((t) => t.type === 'page')
      if (target) break
    } catch { /* 等启动 */ }
    await sleep(250)
  }
  if (!target) {
    // 在 try 之外，必须自己把工作目录还回去，否则会永久卡在临时目录上
    await post('/api/workspace', { path: originalWs })
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

  try {
    await send('Page.navigate', { url: BASE })
    await sleep(2800)

    const evalJs = async (expr) =>
      (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value

    const box = async (sel) =>
      JSON.parse(
        await evalJs(`(() => {
          const e = document.querySelector(${JSON.stringify(sel)})
          if (!e) return 'null'
          const r = e.getBoundingClientRect()
          return JSON.stringify({ x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) })
        })()`),
      )

    async function drag(x, y, dx, dy) {
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 })
      await sleep(70)
      for (let i = 1; i <= 6; i++) {
        await send('Input.dispatchMouseEvent', {
          type: 'mouseMoved', x: Math.round(x + (dx * i) / 6), y: Math.round(y + (dy * i) / 6),
          button: 'left', buttons: 1,
        })
        await sleep(35)
      }
      await send('Input.dispatchMouseEvent', {
        type: 'mouseReleased', x: x + dx, y: y + dy, button: 'left', buttons: 0, clickCount: 1,
      })
      await sleep(300)
    }

    const nodeSel = '.react-flow__node-process'
    const b0 = await box(nodeSel)
    if (!b0) throw new Error('画布上没有处理节点')
    console.log(`处理节点初始位置: (${b0.x}, ${b0.y})  ${b0.w}×${b0.h}`)

    console.log('\n[1] 按住「提示词」输入框拖动')
    const ta = await box(`${nodeSel} .prompt-box`)
    await drag(Math.round(ta.x + ta.w / 2), Math.round(ta.y + ta.h / 2), 120, 90)
    const b1 = await box(nodeSel)
    check('节点不应移动', b1.x === b0.x && b1.y === b0.y, `→ (${b1.x}, ${b1.y})`)

    console.log('\n[2] 按住「模型」下拉框拖动')
    const sel = await box(`${nodeSel} .cfg-field select`)
    await drag(Math.round(sel.x + sel.w / 2), Math.round(sel.y + sel.h / 2), 120, 90)
    const b2 = await box(nodeSel)
    check('节点不应移动', b2.x === b1.x && b2.y === b1.y, `→ (${b2.x}, ${b2.y})`)

    console.log('\n[3] 按住标题栏拖动（对照组）')
    const head = await box(`${nodeSel} .node-head`)
    await drag(Math.round(head.x + 60), Math.round(head.y + head.h / 2), 120, 90)
    const b3 = await box(nodeSel)
    check('节点应当移动', b3.x !== b2.x || b3.y !== b2.y, `→ (${b3.x}, ${b3.y})`)

    console.log('\n[4] 按住输入口那一行拖动（对照组）')
    // 点标签区域：行首是连线圈、行尾是删除按钮，这两处都不该拖动节点
    const pl = await box(`${nodeSel} .port-label`)
    await drag(Math.round(pl.x + pl.w / 2), Math.round(pl.y + pl.h / 2), 110, 80)
    const b4 = await box(nodeSel)
    check('节点应当移动', b4.x !== b3.x || b4.y !== b3.y, `→ (${b4.x}, ${b4.y})`)
  } finally {
    ws.close()
    chrome.kill()
    await post('/api/workspace', { path: originalWs })
    rmSync(TMPWS, { recursive: true, force: true })
    console.log(`\n已恢复工作目录: ${originalWs}，并清理临时目录`)
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
