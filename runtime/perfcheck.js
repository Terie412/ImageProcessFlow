// 量「渲染进程有多忙」：TaskDuration / ScriptDuration 占墙上时间的百分比。
//
// 为什么需要它：2026-10-15 那次——取色器的 Esc 时灵时不灵，往下挖发现 App 一秒渲染
// 16104 次、渲染进程 100% CPU（根因见 DESIGN.md §3.19：`<ReactFlow>` 的内联属性）。
// 这种毛病**界面看不出坏**，只是发涩，靠肉眼看是发现不了的；一个百分比就露馅了。
//
// 用法（先把服务起起来，任意端口都行）：
//     node runtime/perfcheck.js http://127.0.0.1:8770           只看当前画布
//     node runtime/perfcheck.js http://127.0.0.1:8770 rounded_box   顺手建一个该类型的节点
//
// **不改你的工作目录**：建节点会动当前画布，所以要测就切到一次性目录上测。
// 判读：正常应该是个位数百分比（空转基本是 0%）。几十以上就是有东西在空转，
// 上百就是死循环。对照一次 about:blank 可以确认是页面自己的问题、不是探针的。
import { spawn } from 'node:child_process'
import { rmSync } from 'node:fs'
import { CHROME, RUNTIME } from './_common.js'

const PORT = 9300 + (process.pid % 400)
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const DEF = process.argv[3] || '' // 空 = 不建节点
const SECONDS = Number(process.env.SECONDS || 3)
const PROFILE = `${RUNTIME}/tmp/chrome-perf-${Date.now()}`

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

async function main() {
  if (DEF) {
    const st = await fetch(`${BASE}/api/processes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ defId: DEF, x: 300, y: 200 }),
    }).then((r) => r.json())
    console.log(`已建一个 ${DEF} 节点，画布上共 ${st.graph.processes.length} 个节点`)
  }

  let target = null
  for (let i = 0; i < 80; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      target = list.find((t) => t.type === 'page')
      if (target) break
    } catch { /* 等 Chrome 起来 */ }
    await sleep(200)
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
  await send('Performance.enable')

  if (BASE !== 'about:blank') {
    await send('Page.navigate', { url: BASE })
    await sleep(2500)
  }

  const metric = async (name) => {
    const ms = (await send('Performance.getMetrics')).metrics
    return ms.find((m) => m.name === name)?.value ?? 0
  }
  const names = ['TaskDuration', 'ScriptDuration', 'LayoutDuration', 'RecalcStyleDuration']
  const before = {}
  for (const n of names) before[n] = await metric(n)

  const t0 = Date.now()
  await sleep(SECONDS * 1000)
  const wall = (Date.now() - t0) / 1000

  const out = []
  for (const n of names) {
    const now = await metric(n)
    out.push(`${n}=${(((now - before[n]) / wall) * 100).toFixed(0)}%`)
  }
  console.log(`${BASE} 墙上 ${wall.toFixed(2)}s：`, out.join('  '))
  console.log('（正常是个位数；几十以上说明有东西在空转，上百就是死循环 → 见 DESIGN.md §3.19）')

  ws.close()
  chrome.kill()
  try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* ignore */ }
}

main().catch((e) => {
  console.error('ERR', e.message)
  chrome.kill()
  process.exit(1)
})
