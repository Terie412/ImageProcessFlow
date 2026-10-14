// 验证：删数据节点才弹确认框；删处理节点/连线静默；输入框内按 Delete 不误删
// 全程在一次性临时工作目录里跑，不碰用户的真实工作目录。
import { spawn } from 'node:child_process'
import { readFileSync, rmSync } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9225
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const SEED = `${RUNTIME}/seed.png`
const TMPWS = `${RUNTIME}/testws`

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
const state = async () => (await fetch(`${BASE}/api/state`)).json()

const post = (path, body) =>
  fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json())

const patch = (path, body) =>
  fetch(`${BASE}${path}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((r) => r.json())

async function makeProcess(x, y) {
  const r = await post('/api/processes', { defId: 'ai_image', x, y })
  return r.graph.processes[r.graph.processes.length - 1].id
}

async function makeDataNode(x, y) {
  const fd = new FormData()
  fd.append('file', new Blob([readFileSync(SEED)], { type: 'image/png' }), 'seed.png')
  fd.append('resource_id', '')
  fd.append('x', String(x))
  fd.append('y', String(y))
  const r = await (await fetch(`${BASE}/api/resources/import`, { method: 'POST', body: fd })).json()
  return r.graph.resources[r.graph.resources.length - 1].id
}

async function useTempWorkspace() {
  const orig = (ORIG_WS = (await state()).workspace)
  rmSync(TMPWS, { recursive: true, force: true })
  await post('/api/workspace', { path: TMPWS })
  return orig
}

let ORIG_WS = null

async function main() {
  const results = []
  const check = (n, ok, d) => {
    results.push([ok, n, d])
    console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${n}${d ? '  — ' + d : ''}`)
  }

  const originalWs = await useTempWorkspace()
  console.log(`已切到一次性工作目录（原目录: ${originalWs}）`)

  // 「删除前确认」是设置项，用户可能已经关掉；测试期间强制打开，跑完恢复原值
  const originalSettings = (await state()).settings
  await patch('/api/settings', { confirmDeleteDataNode: true })
  console.log(`已临时开启「删除前确认」（原值 ${originalSettings.confirmDeleteDataNode}，测完恢复）`)

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
  let dialogText = null
  let autoAccept = true
  let dialogCount = 0
  const send = (method, params = {}) =>
    new Promise((res, rej) => {
      const mid = ++id
      pending.set(mid, { res, rej })
      ws.send(JSON.stringify({ id: mid, method, params }))
    })
  ws.onmessage = async (ev) => {
    const m = JSON.parse(ev.data)
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id)
      pending.delete(m.id)
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)
      return
    }
    if (m.method === 'Page.javascriptDialogOpening') {
      dialogText = m.params.message
      dialogCount++
      console.log(`  [确认框 #${dialogCount}] ${JSON.stringify(m.params.message)}`)
      await send('Page.handleJavaScriptDialog', { accept: autoAccept })
    }
  }
  await new Promise((r) => (ws.onopen = r))
  await send('Page.enable')
  await send('Runtime.enable')

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

  const click = async (x, y) => {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 })
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 })
    await sleep(220)
  }

  const key = async (k, vk) => {
    await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: k, code: k, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk })
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code: k, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk })
    await sleep(650)
  }

  const selectNode = async (nodeId) => {
    const b = await box(`.react-flow__node[data-id="${nodeId}"] .node-head`)
    if (!b) throw new Error(`找不到节点 ${nodeId}`)
    await click(Math.round(b.x + 50), Math.round(b.y + b.h / 2))
  }

  try {
    const procKeep = await makeProcess(80, 80)      // 留着用来测输入框内按键
    const procDel = await makeProcess(600, 80)      // 用来测「删处理节点静默」
    const dataDel = await makeDataNode(80, 500)     // 用来测「删数据节点弹框」
    const dataKeep = await makeDataNode(600, 500)   // 用来测「取消后保留」

    await send('Page.navigate', { url: BASE })
    await sleep(2800)

    const nameOf = (st, rid) => st.graph.resources.find((r) => r.id === rid)?.name

    // ---- 1. 删处理节点：应静默 ----
    console.log('\n[1] 选中处理节点 → 按 Delete（应静默删除，不弹框）')
    await selectNode(procDel)
    dialogText = null
    dialogCount = 0
    await key('Delete', 46)
    const s1 = await state()
    check('处理节点已删除', !s1.graph.processes.some((p) => p.id === procDel))
    check('没有弹确认框', dialogCount === 0)

    // ---- 2. 删数据节点：应弹框并点名图片 ----
    console.log('\n[2] 选中数据节点 → 按 Delete（应弹框并点名要删的图）')
    const dn = nameOf(await state(), dataDel)
    const before = await fetch(`${BASE}/api/images/${dn}`).then((r) => r.status)
    await selectNode(dataDel)
    dialogText = null
    dialogCount = 0
    await key('Delete', 46)
    const s2 = await state()
    check('弹出了确认框', dialogCount === 1)
    check('确认框点名了图片', (dialogText || '').includes(dn), JSON.stringify(dialogText))
    check('数据节点已删除', !s2.graph.resources.some((r) => r.id === dataDel))
    const after = await fetch(`${BASE}/api/images/${dn}`).then((r) => r.status)
    check('图片文件已永久删除', before === 200 && after === 404, `${before} → ${after}`)

    // ---- 3. 点「取消」应保留 ----
    console.log('\n[3] 删数据节点时点「取消」')
    const dn2 = nameOf(await state(), dataKeep)
    await selectNode(dataKeep)
    dialogCount = 0
    autoAccept = false
    await key('Delete', 46)
    check('弹出了确认框', dialogCount === 1)
    const s3 = await state()
    check('取消后节点仍在', s3.graph.resources.some((r) => r.id === dataKeep))
    const still = await fetch(`${BASE}/api/images/${dn2}`).then((r) => r.status)
    check('图片文件仍在', still === 200, `HTTP ${still}`)
    autoAccept = true

    // ---- 4. 输入框里按 Delete 不应误删 ----
    console.log('\n[4] 聚焦处理节点的「提示词」输入框后按 Delete')
    const cntBefore = (await state()).graph.processes.length
    const ta = await box(`.react-flow__node[data-id="${procKeep}"] .prompt-box`)
    await click(Math.round(ta.x + ta.w / 2), Math.round(ta.y + ta.h / 2))
    dialogCount = 0
    await key('Delete', 46)
    const cntAfter = (await state()).graph.processes.length
    check('处理节点数量不变', cntBefore === cntAfter, `${cntBefore} → ${cntAfter}`)
    check('没有弹确认框', dialogCount === 0)
  } finally {
    ws.close()
    chrome.kill()
    await patch('/api/settings', originalSettings)
    await post('/api/workspace', { path: originalWs })
    rmSync(TMPWS, { recursive: true, force: true })
    console.log(`\n已恢复工作目录 ${originalWs}、设置归位，并清理临时目录`)
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
