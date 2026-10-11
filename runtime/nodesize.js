// 查看页面上各节点的实际渲染尺寸与内联 style，排查尺寸不生效的问题。
import { spawn } from 'node:child_process'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const PORT = 9232
const URL = process.argv[2] || 'http://127.0.0.1:8770'
const PROFILE = 'D:/Workspace/ImageProcessFlow/runtime/chrome-size'

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

async function main() {
  let target = null
  for (let i = 0; i < 120; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      target = list.find((t) => t.type === 'page')
      if (target) break
    } catch { /* 等 */ }
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
  await send('Page.navigate', { url: URL })
  await sleep(4500)

  const out = await send('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const rows = []
      for (const n of document.querySelectorAll('.react-flow__node')) {
        const inner = n.querySelector('.node')
        const r = n.getBoundingClientRect()
        const ir = inner ? inner.getBoundingClientRect() : null
        rows.push({
          id: n.getAttribute('data-id'),
          cls: (n.className.match(/react-flow__node-(\\w+)/) || [])[1],
          styleW: n.style.width || '(none)',
          styleH: n.style.height || '(none)',
          domW: Math.round(r.width), domH: Math.round(r.height),
          innerW: ir ? Math.round(ir.width) : null,
          innerH: ir ? Math.round(ir.height) : null,
        })
      }
      const zoom = document.querySelector('.react-flow__viewport')?.style.transform
      return JSON.stringify({ zoom, rows }, null, 2)
    })()`,
  })
  console.log(out.result.value)
  ws.close()
  chrome.kill()
}

main().catch((e) => { console.error('ERR', e.message); chrome.kill(); process.exit(1) })
