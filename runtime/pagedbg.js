// 前端排错：打开页面，收集控制台日志与未捕获异常，并报告关键 DOM 数量。
// 用法: node runtime/pagedbg.js [url]
import { spawn } from 'node:child_process'
import { rmSync } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9231
const URL = process.argv[2] || 'http://127.0.0.1:8770'
const PROFILE = `${RUNTIME}/chrome-dbg`

rmSync(PROFILE, { recursive: true, force: true })
const chrome = spawn(
  CHROME,
  [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--window-size=1400,1000',
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
    } catch { /* 等启动 */ }
    await sleep(300)
  }
  if (!target) throw new Error('找不到调试目标（Chrome 没起来）')

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
    if (m.method === 'Runtime.consoleAPICalled') {
      const args = m.params.args.map((a) => a.value ?? a.description ?? '').join(' ')
      logs.push(`[${m.params.type}] ${args}`)
    }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails
      logs.push(`[异常] ${d.exception?.description || d.text}`.split('\n').slice(0, 3).join(' | '))
    }
  }
  await new Promise((r) => (ws.onopen = r))
  await send('Runtime.enable')
  await send('Page.enable')
  await send('Page.navigate', { url: URL })
  await sleep(4500)

  const evalJs = async (expr) =>
    (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value

  console.log('页面统计:')
  console.log('   .node        :', await evalJs(`document.querySelectorAll('.node').length`))
  console.log('   .prompt-box  :', await evalJs(`document.querySelectorAll('.prompt-box').length`))
  console.log('   润色按钮     :', await evalJs(`!!document.querySelector('.prompt-actions .mini-btn')`))
  console.log('   工具栏       :', await evalJs(`JSON.stringify([...document.querySelectorAll('.toolbar button')].map(b=>b.textContent.trim()))`))
  console.log('\n控制台输出:')
  if (!logs.length) console.log('   （干净，没有任何日志或异常）')
  logs.slice(0, 15).forEach((l) => console.log('   ' + l))

  ws.close()
  chrome.kill()
  rmSync(PROFILE, { recursive: true, force: true })
}

main().catch((e) => {
  console.error('ERR', e.message)
  chrome.kill()
  process.exit(1)
})
