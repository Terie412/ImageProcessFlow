// 验证「润色」按钮的完整交互：点润色 → 内容被替换 + 出现撤销 → 撤销 → 恢复原值。
// 会真实调用 1 次 DeepSeek（很便宜，非生图）。全程在一次性临时工作目录里跑。
import { spawn } from 'node:child_process'
import { readFileSync, rmSync } from 'node:fs'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const PORT = 9228
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const SEED = 'D:/Workspace/ImageProcessFlow/runtime/seed.png'
const TMPWS = 'D:/Workspace/ImageProcessFlow/runtime/testws-polish'
const PROFILE = 'D:/Workspace/ImageProcessFlow/runtime/chrome-profile-polish'

const ORIGINAL = '把背景去掉，只留下三个形状'

try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着目录 */ }
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
const state = async () => (await fetch(`${BASE}/api/state`)).json()
const post = (path, body) =>
  fetch(`${BASE}${path}`, {
    method: 'POST',
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

  const rid = await makeDataNode(80, 200)
  const st = await post('/api/processes', { defId: 'qwen_image', x: 520, y: 200 })
  const pid = st.graph.processes[st.graph.processes.length - 1].id
  await post('/api/edges', { from: rid, to: pid, port: 'image1' })
  await fetch(`${BASE}/api/processes/${pid}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ params: { prompt: ORIGINAL } }),
  })

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
  const btnBox = async (label) =>
    JSON.parse(
      await evalJs(`(() => {
        const b = [...document.querySelectorAll('.prompt-actions .mini-btn')]
          .find(x => x.textContent.trim().includes(${JSON.stringify(label)}))
        if (!b) return 'null'
        const r = b.getBoundingClientRect()
        return JSON.stringify({ x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) })
      })()`),
    )
  const click = async (p) => {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: p.x, y: p.y, button: 'left', buttons: 1, clickCount: 1 })
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: p.x, y: p.y, button: 'left', buttons: 0, clickCount: 1 })
    await sleep(250)
  }
  const promptValue = async () => evalJs(`document.querySelector('.prompt-box')?.value ?? null`)
  const buttons = async () =>
    JSON.parse(await evalJs(`JSON.stringify([...document.querySelectorAll('.prompt-actions .mini-btn')].map(b=>b.textContent.trim()))`))

  try {
    await send('Page.navigate', { url: BASE })
    await sleep(3500)

    console.log('\n[1] 初始状态')
    check('提示词框是设定的内容', (await promptValue()) === ORIGINAL, JSON.stringify(await promptValue()))
    check('只有一个「润色」按钮、没有「撤销」', JSON.stringify(await buttons()) === '["润色"]', JSON.stringify(await buttons()))

    console.log('\n[2] 点「润色」（会真实调用 DeepSeek，等几秒）')
    const t0 = Date.now()
    await click(await btnBox('润色'))
    let busySeen = false
    for (let i = 0; i < 200; i++) {
      const bs = await buttons()
      if (bs.some((b) => b.includes('润色中'))) busySeen = true
      if (bs.some((b) => b === '润色')) break
      await sleep(500)
    }
    const cost = ((Date.now() - t0) / 1000).toFixed(1)
    console.log(`   等到了按钮复位，耗时 ${cost}s`)
    const after = await promptValue()
    console.log(`   润色后内容（前 110 字）: ${JSON.stringify((after || '').slice(0, 110))}`)
    check('按钮出现过「润色中…」状态', busySeen)
    check('提示词已被替换（内容变了）', after !== ORIGINAL && (after || '').length > 0)
    check('新内容比原文更长（说明展开了）', (after || '').length > ORIGINAL.length)
    check('出现了「撤销」按钮', (await buttons()).includes('撤销'), JSON.stringify(await buttons()))

    console.log('\n[3] 点「撤销」')
    await click(await btnBox('撤销'))
    await sleep(400)
    check('内容恢复成原文', (await promptValue()) === ORIGINAL, JSON.stringify(await promptValue()))
    check('「撤销」按钮消失', !(await buttons()).includes('撤销'), JSON.stringify(await buttons()))

    console.log('\n[4] 撤销后的值有没有写回服务端')
    await sleep(900) // 等过 onParam 的 debounce
    const saved = (await state()).graph.processes[0]?.params?.prompt
    check('服务端也是原文', saved === ORIGINAL, JSON.stringify(saved))
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
