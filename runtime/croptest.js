// 验证裁剪节点：预览缩略图 / 自动识别 / 区域编辑器（增删改）/ 执行
// 会真实调用 1 次文本模型（自动识别），不调用任何生图模型。
// 全程在一次性临时工作目录里跑，不碰用户的真实画布。
import { spawn } from 'node:child_process'
import { readFileSync, rmSync, writeFileSync } from 'node:fs'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const PORT = 9300 + (process.pid % 400)
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const SRC = 'D:/Workspace/ImageProcessFlow/workspace/images/B.png'
const TMPWS = 'D:/Workspace/ImageProcessFlow/runtime/testws-crop'
// 每次跑用一个新的 profile 目录：不用去删旧的（沙箱里删目录可能被拦），
// 也就不会因为残留 profile 导致 Chrome 起不来。
const PROFILE = `D:/Workspace/ImageProcessFlow/runtime/tmp/chrome-crop-${Date.now()}`

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
const state = async () => (await fetch(`${BASE}/api/state`)).json()
const req = (method, path, body) =>
  fetch(`${BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => r.json())
const post = (p, b) => req('POST', p, b)

let ORIG_WS = null

async function main() {
  const results = []
  const check = (n, ok, d) => {
    results.push([ok, n, d])
    console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${n}${d ? '  — ' + d : ''}`)
  }

  ORIG_WS = (await state()).workspace
  rmSync(TMPWS, { recursive: true, force: true })
  await post('/api/workspace', { path: TMPWS })

  const fd = new FormData()
  fd.append('file', new Blob([readFileSync(SRC)], { type: 'image/png' }), 'B.png')
  fd.append('resource_id', '')
  fd.append('x', '150')
  fd.append('y', '150')
  const st = await (await fetch(`${BASE}/api/resources/import`, { method: 'POST', body: fd })).json()
  const rid = st.graph.resources[st.graph.resources.length - 1].id
  const rname = st.graph.resources[st.graph.resources.length - 1].name
  const st2 = await post('/api/processes', { defId: 'crop', x: 700, y: 150 })
  const pid = st2.graph.processes[st2.graph.processes.length - 1].id
  await post('/api/edges', { from: rid, to: pid, port: 'image' })
  console.log(`已建画布：数据节点 ${rname} + 裁剪节点 ${pid}（已连线）`)

  let target = null
  for (let i = 0; i < 120; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      target = list.find((t) => t.type === 'page')
      if (target) break
    } catch { /* 等启动 */ }
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
  const evalJs = async (expr) =>
    (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value
  const J = async (expr) => JSON.parse(await evalJs(expr))
  const sel = (n) => `.react-flow__node[data-id="${n}"]`

  const click = async (x, y, count = 1) => {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: count })
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: count })
    await sleep(60)
  }
  const dblclick = async (x, y) => {
    await click(x, y, 1)
    await click(x, y, 2)
    await sleep(700)
  }
  const drag = async (x, y, dx, dy) => {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 })
    await sleep(70)
    for (let i = 1; i <= 8; i++) {
      await send('Input.dispatchMouseEvent', {
        type: 'mouseMoved', x: Math.round(x + (dx * i) / 8), y: Math.round(y + (dy * i) / 8),
        button: 'left', buttons: 1,
      })
      await sleep(30)
    }
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y: y + dy, button: 'left', buttons: 0, clickCount: 1 })
    await sleep(350)
  }
  const center = async (selector) =>
    J(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)})
      if (!el) return 'null'
      const r = el.getBoundingClientRect()
      return JSON.stringify({
        x: Math.round(r.left + Math.min(r.width / 2, 60)),
        y: Math.round(r.top + r.height / 2),
        w: Math.round(r.width), h: Math.round(r.height),
        left: Math.round(r.left), top: Math.round(r.top),
      })
    })()`)

  const nodeInfo = async () =>
    J(`(() => {
      const n = document.querySelector('${sel(pid)}')
      if (!n) return JSON.stringify({ found: false })
      const shot = n.querySelector('.region-shot')
      const img = shot && shot.querySelector('img')
      const ports = n.querySelector('.ports')
      return JSON.stringify({
        found: true,
        portsWidth: ports ? Math.round(ports.getBoundingClientRect().width) : null,
        portLabelCount: n.querySelectorAll('.port-label').length,
        portCircle: n.querySelectorAll('.port-row .react-flow__handle').length,
        hasShot: !!shot,
        imgLoaded: img ? img.naturalWidth > 0 : false,
        boxes: n.querySelectorAll('.region-box').length,
        count: (n.querySelector('.region-count') || {}).textContent || '',
        buttons: [...n.querySelectorAll('.region-bar .mini-btn')].map((b) => b.textContent.trim()),
      })
    })()`)

  try {
    await send('Page.navigate', { url: BASE })
    await sleep(4000)

    console.log('\n[1] 节点长相：无标签单口 + 区域预览')
    let info = await nodeInfo()
    console.log(`   ${JSON.stringify(info)}`)
    check('裁剪节点渲染出来了', info.found)
    check('端口栏收窄（无标签）', info.portsWidth !== null && info.portsWidth <= 40, `${info.portsWidth}px`)
    check('没有端口文字标签', info.portLabelCount === 0, String(info.portLabelCount))
    check('有一个连线圈', info.portCircle === 1, String(info.portCircle))
    check('有区域预览区', info.hasShot)
    check('上游图已加载进预览', info.imgLoaded)
    check('初始 0 个区域', info.boxes === 0 && info.count.includes('0 个'), info.count)
    check('有自动识别 / 编辑按钮', info.buttons.join('/') === '自动识别/编辑', info.buttons.join('/'))

    console.log('\n[2] 点「自动识别」（真实调 1 次文本模型）')
    const btn = await center(`${sel(pid)} .region-bar .mini-btn:last-child`)
    const detectBtn = await J(`(() => {
      const bs = [...document.querySelectorAll('${sel(pid)} .region-bar .mini-btn')]
      const b = bs.find((x) => x.textContent.includes('自动识别'))
      const r = b.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
    void btn
    await click(detectBtn.x, detectBtn.y, 1)
    let ok = false
    for (let i = 0; i < 60; i++) {
      await sleep(500)
      info = await nodeInfo()
      const cur = (await state()).graph.processes.find((p) => p.id === pid)
      const n = (cur?.params?.regions || []).length
      if (n > 0) { ok = true; break }
    }
    info = await nodeInfo()
    const proc = (await state()).graph.processes.find((p) => p.id === pid)
    const regs = proc?.params?.regions || []
    console.log(`   识别到 ${regs.length} 个区域，预览里画了 ${info.boxes} 个框，按钮: ${info.buttons.join('/')}`)
    check('识别出区域并写进了参数', ok && regs.length > 0, `${regs.length}`)
    check('预览里画出同样数量的框', info.boxes === regs.length, `${info.boxes} vs ${regs.length}`)
    check('出现了「撤销」按钮', info.buttons.includes('撤销'), info.buttons.join('/'))

    console.log('\n[3] 双击预览打开编辑器')
    const shot = await center(`${sel(pid)} .region-shot`)
    await dblclick(shot.x, shot.y)
    const ed = await J(`(() => {
      const m = document.querySelector('.editor-mask')
      if (!m) return JSON.stringify({ open: false })
      return JSON.stringify({
        open: true,
        rows: m.querySelectorAll('.erow').length,
        boxes: m.querySelectorAll('.ebox').length,
        hint: (m.querySelector('.editor-hint') || {}).textContent || '',
        canvas: (() => { const c = m.querySelector('.editor-canvas').getBoundingClientRect(); return [Math.round(c.width), Math.round(c.height), Math.round(c.left), Math.round(c.top)] })(),
      })
    })()`)
    console.log(`   ${JSON.stringify(ed)}`)
    check('编辑器打开了', ed.open)
    check('行数 = 区域数', ed.rows === regs.length, `${ed.rows} vs ${regs.length}`)
    check('图上画了同样数量的框', ed.boxes === regs.length, `${ed.boxes} vs ${regs.length}`)

    console.log('\n[4] 在空白处拖出新框')
    const [cw, chh, cl, ct] = ed.canvas
    const px = cl + Math.round(cw * 0.06)
    const py = ct + Math.round(chh * 0.06)
    await drag(px, py, Math.round(cw * 0.3), Math.round(chh * 0.18))
    let ed2 = await J(`(() => {
      const m = document.querySelector('.editor-mask')
      return JSON.stringify({ rows: m.querySelectorAll('.erow').length, boxes: m.querySelectorAll('.ebox').length })
    })()`)
    console.log(`   拖完后 ${ed2.rows} 行 / ${ed2.boxes} 个框`)
    check('新框被加进去了', ed2.rows === regs.length + 1, `${ed2.rows}`)

    console.log('\n[5] 改数值 + Delete 删除 + 保存')
    // 改**第 2 行**（待会儿删的是第 1 行，这样能同时验证「改动保住了」和「删对了行」）
    const wInput = await J(`(() => {
      const inp = document.querySelectorAll('.editor-mask .erow')[1].querySelectorAll('input')[2]
      const r = inp.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
    await click(wInput.x, wInput.y, 1)
    await send('Input.dispatchKeyEvent', { type: 'keyDown', modifiers: 2, key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65 })
    await send('Input.dispatchKeyEvent', { type: 'keyUp', modifiers: 2, key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65 })
    await send('Input.insertText', { text: '120' })
    await sleep(300)
    const w1 = await evalJs(`document.querySelectorAll('.editor-mask .erow')[1].querySelectorAll('input')[2].value`)
    console.log(`   第 2 行的 W 改成了 ${w1}`)
    check('数值输入生效', String(w1) === '120', String(w1))

    // 顺序：先记录节点数，再选中一行按 Delete
    const nodesBefore = (await state()).graph.processes.length
    // 点「行号」而不是输入框：输入框里按 Delete 不该删行（那是保护行为）
    const row0 = await J(`(() => {
      const r = document.querySelectorAll('.editor-mask .erow')[0].querySelector('.erow-n').getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
    await click(row0.x, row0.y, 1)
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Delete', code: 'Delete', windowsVirtualKeyCode: 46 })
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Delete', code: 'Delete', windowsVirtualKeyCode: 46 })
    await sleep(400)
    const after = await J(`(() => {
      const m = document.querySelector('.editor-mask')
      return JSON.stringify({ rows: m.querySelectorAll('.erow').length, open: !!m })
    })()`)
    const nodesAfter = (await state()).graph.processes.length
    console.log(`   Delete 后 ${after.rows} 行；节点数 ${nodesBefore} → ${nodesAfter}`)
    check('Delete 删掉了这一行', after.rows === regs.length, `${after.rows} vs ${regs.length}`)
    check('编辑器还开着', after.open)
    check('Delete 没有误删画布上的节点', nodesAfter === nodesBefore, `${nodesBefore} → ${nodesAfter}`)

    const saveBtn = await J(`(() => {
      const bs = [...document.querySelectorAll('.editor-mask .editor-foot button')]
      const b = bs.find((x) => x.textContent.trim() === '保存')
      const r = b.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
    await click(saveBtn.x, saveBtn.y, 1)
    await sleep(1200)
    const closed = await evalJs(`!!document.querySelector('.editor-mask')`)
    const finalRegs = ((await state()).graph.processes.find((p) => p.id === pid)?.params?.regions) || []
    console.log(`   保存后：编辑器 ${closed ? '还开着' : '已关闭'}，服务端 ${finalRegs.length} 个区域`)
    check('保存后编辑器关闭', closed === false)
    check('保存的区域数 = 编辑后的行数', finalRegs.length === regs.length, `${finalRegs.length} vs ${regs.length}`)
    check('改过的 W=120 被保存下来了', finalRegs.some((r) => r.w === 120), JSON.stringify(finalRegs.map((r) => r.w)))

    console.log('\n[6] 执行裁剪')
    const exec = await J(`(() => {
      const b = document.querySelector('${sel(pid)} .node-foot button')
      const r = b.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
    await click(exec.x, exec.y, 1)
    let done = false
    for (let i = 0; i < 40; i++) {
      await sleep(500)
      const err = await evalJs(`(document.querySelector('${sel(pid)} .err-box')||{}).textContent||''`)
      if (err) { console.log('   错误:', err.slice(0, 120)); break }
      const n = (await state()).graph.resources.filter((r) => r.producer === pid).length
      if (n >= finalRegs.length) { done = true; break }
    }
    const outs = (await state()).graph.resources.filter((r) => r.producer === pid)
    console.log(`   产出 ${outs.length} 张: ${outs.map((o) => `${o.width}×${o.height}`).join(', ')}`)
    check('执行成功且输出数 = 区域数', done && outs.length === finalRegs.length, `${outs.length} vs ${finalRegs.length}`)
    check('每张的尺寸 = 对应区域尺寸',
      outs.every((o, i) => o.width === finalRegs[i].w && o.height === finalRegs[i].h),
      JSON.stringify(outs.map((o) => [o.width, o.height])) + ' vs ' + JSON.stringify(finalRegs.map((r) => [r.w, r.h])))

    if (process.env.SHOT) {
      let shotPng = await send('Page.captureScreenshot', { format: 'png' })
      writeFileSync('D:/Workspace/ImageProcessFlow/runtime/crop_node.png', Buffer.from(shotPng.data, 'base64'))
      const s2 = await center(`${sel(pid)} .region-shot`)
      await dblclick(s2.x, s2.y)
      await sleep(600)
      shotPng = await send('Page.captureScreenshot', { format: 'png' })
      writeFileSync('D:/Workspace/ImageProcessFlow/runtime/crop_editor.png', Buffer.from(shotPng.data, 'base64'))
      console.log('   已截图 runtime/crop_node.png + runtime/crop_editor.png')
    }
  } finally {
    ws.close()
    chrome.kill()
    if (ORIG_WS) {
      await post('/api/workspace', { path: ORIG_WS })
      rmSync(TMPWS, { recursive: true, force: true })
      try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着目录 */ }
      console.log(`\n已恢复工作目录: ${ORIG_WS}`)
    }
  }

  const bad = results.filter((r) => !r[0])
  console.log(`\n共 ${results.length} 项，通过 ${results.length - bad.length}，失败 ${bad.length}`)
  process.exit(bad.length ? 1 : 0)
}

main().catch(async (e) => {
  console.error('ERR', e.message)
  chrome.kill()
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
