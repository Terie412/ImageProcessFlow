// 验证「设置」浮层：开关能持久化，并真的改变删除时是否弹确认框。
// 全程在一次性临时工作目录里跑，不碰用户的真实工作目录。
import { spawn } from 'node:child_process'
import { readFileSync, rmSync } from 'node:fs'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const PORT = 9226
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const SEED = 'D:/Workspace/ImageProcessFlow/runtime/seed.png'
const TMPWS = 'D:/Workspace/ImageProcessFlow/runtime/testws-settings'

const chrome = spawn(
  CHROME,
  [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--no-first-run', '--no-default-browser-check',
    `--remote-debugging-port=${PORT}`,
    '--user-data-dir=D:/Workspace/ImageProcessFlow/runtime/chrome-profile-settings',
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
  const origSettings = (await state()).settings
  rmSync(TMPWS, { recursive: true, force: true })
  await post('/api/workspace', { path: TMPWS })
  console.log(`已切到一次性工作目录（原目录: ${origWs}）`)

  // 设置是全局的，测试期间先归位到 true，跑完恢复用户原本的偏好
  await patch('/api/settings', { confirmDeleteDataNode: true })
  console.log(`已临时归位设置（原值 ${origSettings.confirmDeleteDataNode}，测完恢复）`)

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
    await patch('/api/settings', origSettings)
    await post('/api/workspace', { path: origWs })
    rmSync(TMPWS, { recursive: true, force: true })
    throw new Error('找不到调试目标')
  }

  const ws = new WebSocket(target.webSocketDebuggerUrl)
  let id = 0
  const pending = new Map()
  let dialogCount = 0
  let autoAccept = true
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
      dialogCount++
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
    await sleep(260)
  }

  const clickSel = async (sel) => {
    const b = await box(sel)
    if (!b) throw new Error(`找不到元素 ${sel}`)
    await click(Math.round(b.x + b.w / 2), Math.round(b.y + b.h / 2))
  }

  const key = async (k, vk) => {
    await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: k, code: k, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk })
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code: k, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk })
    await sleep(620)
  }

  const selectNode = async (nodeId) => {
    const b = await box(`.react-flow__node[data-id="${nodeId}"] .node-head`)
    if (!b) throw new Error(`找不到节点 ${nodeId}`)
    await click(Math.round(b.x + 50), Math.round(b.y + b.h / 2))
  }

  const menuOpen = async () => evalJs(`!!document.querySelector('.settings-pop')`)
  const checkboxChecked = async () =>
    evalJs(`(() => { const i = document.querySelector('.settings-pop .set-row input'); return i ? i.checked : null })()`)
  const serverFlag = async () => (await state()).settings.confirmDeleteDataNode
  const nameOf = (st, rid) => st.graph.resources.find((r) => r.id === rid)?.name

  try {
    const d1 = await makeDataNode(80, 480)   // 关掉确认后删它 → 应静默
    const d2 = await makeDataNode(620, 480)  // 打开确认后删它 → 应弹框

    await send('Page.navigate', { url: BASE })
    await sleep(2800)

    // ---- 1. 打开设置浮层 ----
    console.log('\n[1] 点顶栏齿轮（应弹出设置浮层，复选框默认勾选）')
    check('浮层初始是关闭的', (await menuOpen()) === false)
    await clickSel('.toolbar .icon-btn')
    check('浮层已打开', (await menuOpen()) === true)
    check('复选框初始为勾选', (await checkboxChecked()) === true)
    check('服务端初始值为 true', (await serverFlag()) === true)

    // ---- 2. 取消勾选 → 关掉确认 ----
    console.log('\n[2] 取消勾选（应写入服务端 false，浮层不关闭）')
    await clickSel('.settings-pop .set-row input')
    await sleep(400)
    check('浮层仍开着', (await menuOpen()) === true)
    check('复选框已取消', (await checkboxChecked()) === false)
    check('服务端已变 false', (await serverFlag()) === false)

    // ---- 3. 关掉确认后删数据节点：应静默 ----
    console.log('\n[3] 关掉确认后，删数据节点（应静默，不弹框，文件照样删）')
    await key('Escape', 27)
    check('浮层已关闭', (await menuOpen()) === false)
    const dn1 = nameOf(await state(), d1)
    const before1 = await fetch(`${BASE}/api/images/${dn1}`).then((r) => r.status)
    await selectNode(d1)
    dialogCount = 0
    await key('Delete', 46)
    const s3 = await state()
    check('没有弹确认框', dialogCount === 0)
    check('数据节点已被删除', !s3.graph.resources.some((r) => r.id === d1))
    const after1 = await fetch(`${BASE}/api/images/${dn1}`).then((r) => r.status)
    check('图片文件仍被永久删除', before1 === 200 && after1 === 404, `${before1} → ${after1}`)

    // ---- 4. 重新勾上 ----
    console.log('\n[4] 重新勾选（应回到 true）')
    await clickSel('.toolbar .icon-btn')
    await clickSel('.settings-pop .set-row input')
    await sleep(400)
    check('复选框已勾上', (await checkboxChecked()) === true)
    check('服务端已变 true', (await serverFlag()) === true)

    // ---- 5. 打开确认后删数据节点：应弹框 ----
    console.log('\n[5] 打开确认后，删数据节点（应弹框；点取消则保留）')
    await key('Escape', 27)
    const dn2 = nameOf(await state(), d2)
    await selectNode(d2)
    dialogCount = 0
    autoAccept = false
    await key('Delete', 46)
    check('弹出了确认框', dialogCount === 1)
    const s5 = await state()
    check('取消后节点仍在', s5.graph.resources.some((r) => r.id === d2))
    const still = await fetch(`${BASE}/api/images/${dn2}`).then((r) => r.status)
    check('图片文件仍在', still === 200, `HTTP ${still}`)
    autoAccept = true

    // ---- 6. 浮层截图 ----
    console.log('\n[6] 截图设置浮层')
    await clickSel('.toolbar .icon-btn')
    await sleep(500)
    const shot = await send('Page.captureScreenshot', { format: 'png' })
    const { writeFileSync } = await import('node:fs')
    writeFileSync('D:/Workspace/ImageProcessFlow/runtime/settings.png', Buffer.from(shot.data, 'base64'))
    console.log('  已保存 runtime/settings.png')
  } finally {
    ws.close()
    chrome.kill()
    await patch('/api/settings', origSettings)  // 恢复用户原本的偏好，不写死
    await post('/api/workspace', { path: origWs })
    rmSync(TMPWS, { recursive: true, force: true })
    rmSync('D:/Workspace/ImageProcessFlow/runtime/chrome-profile-settings', { recursive: true, force: true })
    console.log(`\n已恢复工作目录 ${origWs}、设置原值，临时目录已清理`)
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
