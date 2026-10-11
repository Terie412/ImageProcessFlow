// 诊断缩放手柄：坐标对不对？命中的是谁？CDP 的事件到得到它吗？
import { spawn } from 'node:child_process'
import { rmSync } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9234
const URL = process.argv[2] || 'http://127.0.0.1:8770'
const PROFILE = `${RUNTIME}/chrome-ptr`

try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着目录 */ }
const chrome = spawn(
  CHROME,
  [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    '--window-size=1600,1000', 'about:blank',
  ],
  { stdio: 'ignore' },
)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function main() {
  let target = null
  for (let i = 0; i < 120; i++) {
    try {
      const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      target = l.find((t) => t.type === 'page')
      if (target) break
    } catch { /* 等 */ }
    await sleep(300)
  }
  if (!target) throw new Error('找不到调试目标')

  const ws = new WebSocket(target.webSocketDebuggerUrl)
  let id = 0
  const pending = new Map()
  const send = (m, p = {}) =>
    new Promise((res, rej) => {
      const i = ++id
      pending.set(i, { res, rej })
      ws.send(JSON.stringify({ id: i, method: m, params: p }))
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
  await send('Page.navigate', { url: URL })
  await sleep(4200)

  const evalJs = async (expr) =>
    (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value

  // 找处理节点的手柄
  const info = JSON.parse(await evalJs(`(() => {
    const nodes = [...document.querySelectorAll('.react-flow__node-process')]
    if (!nodes.length) return JSON.stringify({ err: '没有处理节点' })
    const n = nodes[0]
    const h = n.querySelector('.resize-handle')
    if (!h) return JSON.stringify({ err: '处理节点里没有 .resize-handle' })
    const r = h.getBoundingClientRect()
    const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2)
    const cs = getComputedStyle(h)
    return JSON.stringify({
      nodeId: n.getAttribute('data-id'),
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      center: [cx, cy],
      display: cs.display, visibility: cs.visibility, pointerEvents: cs.pointerEvents,
      topAtCenter: (document.elementFromPoint(cx, cy) || {}).className || null,
    }, null, 2)
  })()`))
  console.log('手柄信息:')
  console.log(JSON.stringify(info, null, 2).replace(/^/gm, '  '))
  if (info.err) { ws.close(); chrome.kill(); return }

  const [cx, cy] = info.center

  console.log('\n=== 在 document 上挂原生监听（捕获）===')
  await evalJs(`(() => {
    window.__ev = []
    for (const t of ['pointerdown','mousedown','pointermove','mousemove','pointerup','mouseup','click']) {
      document.addEventListener(t, (e) => {
        window.__ev.push(t + ' @ ' + Math.round(e.clientX) + ',' + Math.round(e.clientY) +
          ' target=' + ((e.target.className || e.target.tagName) + '').slice(0, 40))
      }, true)
    }
    return 'ok'
  })()`)

  console.log('\n=== CDP 派发：按下 → 移动 → 抬起 ===')
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: cx, y: cy, button: 'left', buttons: 1, clickCount: 1 })
  await sleep(150)
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: cx + 60, y: cy + 60, button: 'left', buttons: 1 })
  await sleep(150)
  const midway = await evalJs(`(() => {
    const n = document.querySelector('.react-flow__node-process')
    const r = n.getBoundingClientRect()
    return Math.round(r.width) + 'x' + Math.round(r.height)
  })()`)
  console.log('  拖动中节点尺寸:', midway)
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: cx + 60, y: cy + 60, button: 'left', buttons: 0, clickCount: 1 })
  await sleep(400)

  const evs = JSON.parse(await evalJs(`JSON.stringify(window.__ev)`))
  console.log('\n  事件序列:')
  evs.slice(0, 12).forEach((e) => console.log('   ', e))

  const after = await evalJs(`(() => {
    const n = document.querySelector('.react-flow__node-process')
    const r = n.getBoundingClientRect()
    return Math.round(r.width) + 'x' + Math.round(r.height)
  })()`)
  console.log('\n  拖动后节点尺寸:', after)
  console.log('  onResizeStart 被调用次数:', await evalJs('window.__resizeDebug ?? 0'))

  ws.close()
  chrome.kill()
  try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着目录 */ }
}

main().catch((e) => { console.error('ERR', e.message); chrome.kill(); process.exit(1) })
