// 验证节点样式调整：
//   ① 右下角缩放手柄不再压住「执行」按钮
//   ② 配置区（模型/尺寸/张数/开关）在默认宽度下最多占两排
//   ③ 画布左上角显示当前选中节点的尺寸，且拖动缩放手柄时实时更新
// 全程在一次性临时工作目录里跑，不碰用户的真实画布。
import { spawn } from 'node:child_process'
import { readFileSync, rmSync } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9234
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const SEED = `${RUNTIME}/seed.png`
const TMPWS = `${RUNTIME}/testws-style`
const PROFILE = `${RUNTIME}/chrome-profile-style`

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

  const rid1 = await makeDataNode(60, 120)
  await makeDataNode(60, 1100) // 把视野撑开，避免处理节点被右下角 MiniMap 压住
  const st = await post('/api/processes', { defId: 'ai_image', x: 420, y: 120 })
  const pid = st.graph.processes[st.graph.processes.length - 1].id
  const boot = await state()
  const msz = boot.manifests.ai_image.size
  // 下限要取**服务端算出来的有效值**（sizeSpec），不能只看 settings.nodeMin.process ——
  // 那只是通用兜底，`process:<defId>` 可以单独覆盖（ai_image 就有自己的一行）
  const pmin = boot.sizeSpec['process:ai_image'] || boot.settings.nodeMin.process

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
  const J = async (expr) => JSON.parse(await evalJs(expr))

  const sel = (nodeId) => `.react-flow__node[data-id="${nodeId}"]`
  const zoom = async () =>
    (await evalJs(`(() => {
      const t = document.querySelector('.react-flow__viewport')?.style.transform || ''
      const m = t.match(/scale\\(([\\d.]+)\\)/)
      return m ? Number(m[1]) : 1
    })()`)) || 1

  const nodeSize = async (nodeId) =>
    J(`(() => {
      const r = document.querySelector('${sel(nodeId)}').getBoundingClientRect()
      return JSON.stringify({ w: Math.round(r.width), h: Math.round(r.height) })
    })()`)

  // 手柄与按钮是否相交
  const handleHit = async (nodeId) =>
    J(`(() => {
      const node = document.querySelector('${sel(nodeId)}')
      const h = node.querySelector('.resize-handle').getBoundingClientRect()
      const btns = [...node.querySelectorAll('button')].map((b) => ({
        t: (b.textContent || '').trim().slice(0, 8), r: b.getBoundingClientRect(),
      }))
      const hit = btns.filter((b) => !(h.right <= b.r.left || h.left >= b.r.right || h.bottom <= b.r.top || h.top >= b.r.bottom))
      return JSON.stringify({
        handle: [Math.round(h.left), Math.round(h.top), Math.round(h.right), Math.round(h.bottom)],
        hit: hit.map((x) => x.t || '(无文字)'),
      })
    })()`)

  // 配置区每一排的内容（同一 flex 行的元素垂直居中，中心 y 相同）
  // 配置区每一排的内容。
  // **按纵向重叠分组**，不是按「中心 y 相等」：同一排里带提示的那个字段更高（两行），
  // 中心本来就对不齐 —— 那正是 `.cfg` 用 `align-items: flex-start` 的原因（见 styles.css）。
  // 按中心去重会把「模型/尺寸/张数」判成两排，误报。
  const cfgRows = async (nodeId) =>
    J(`(() => {
      const node = document.querySelector('${sel(nodeId)}')
      const items = [...node.querySelectorAll('.cfg > .cfg-field, .cfg > .cfg-checks')]
        .map((el) => {
          const r = el.getBoundingClientRect()
          const text = el.classList.contains('cfg-checks')
            ? [...el.querySelectorAll('.cfg-check')].map((x) => x.textContent.trim()).join('/')
            : (el.querySelector('.cfg-label')?.textContent || '').trim()
          return { r, text }
        })
        .sort((a, b) => a.r.top - b.r.top)
      const groups = []
      for (const it of items) {
        const hit = groups.find(
          (gr) =>
            Math.min(gr.bottom, it.r.bottom) - Math.max(gr.top, it.r.top) >
            Math.min(gr.h, it.r.height) * 0.5,
        )
        if (hit) {
          hit.members.push(it.text)
          hit.top = Math.min(hit.top, it.r.top)
          hit.bottom = Math.max(hit.bottom, it.r.bottom)
          hit.h = hit.bottom - hit.top
        } else {
          groups.push({ top: it.r.top, bottom: it.r.bottom, h: it.r.height, members: [it.text] })
        }
      }
      return JSON.stringify(groups.map((g) => g.members))
    })()`)

  const selInfo = async () => evalJs(`(() => {
    const el = document.querySelector('.sel-info')
    return el ? el.textContent.replace(/\\s+/g, ' ').trim() : ''
  })()`)

  async function click(x, y) {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 })
    await sleep(50)
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 })
    await sleep(400)
  }
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
    const r = await evalJs(`JSON.stringify({ t: document.querySelector('.sel-info')?.textContent.replace(/\\s+/g,' ').trim() || '' })`)
    await send('Input.dispatchMouseEvent', {
      type: 'mouseReleased', x: x + dx, y: y + dy, button: 'left', buttons: 0, clickCount: 1,
    })
    await sleep(400)
    return JSON.parse(r).t
  }
  const headPoint = async (nodeId) =>
    J(`(() => {
      const r = document.querySelector('${sel(nodeId)} .node-head').getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + 55), y: Math.round(r.top + r.height / 2) })
    })()`)
  const handlePoint = async (nodeId) =>
    J(`(() => {
      const r = document.querySelector('${sel(nodeId)} .resize-handle').getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
  // 找一处空白画布（不在任何节点/按钮/控件/缩略图上）
  const panePoint = async () =>
    J(`(() => {
      for (let y = 80; y < innerHeight - 80; y += 24) {
        for (let x = 80; x < innerWidth - 80; x += 24) {
          const el = document.elementFromPoint(x, y)
          if (!el) continue
          if (el.closest('.react-flow__node')) continue
          if (el.closest('button')) continue
          if (el.closest('.react-flow__minimap')) continue
          if (el.closest('.react-flow__controls')) continue
          if (el.closest('.react-flow__panel')) continue
          return JSON.stringify({ x, y })
        }
      }
      return 'null'
    })()`)

  try {
    await send('Page.navigate', { url: BASE })
    await sleep(4000)
    const z = await zoom()
    console.log(`   当前缩放 ${z.toFixed(3)}   处理节点默认 ${msz.defaultWidth}×${msz.defaultHeight}`)

    console.log('\n[1] 缩放手柄不压按钮')
    for (const [name, nid] of [['处理节点', pid], ['数据节点', rid1]]) {
      const r = await handleHit(nid)
      console.log(`   ${name} 手柄 ${r.handle.join(',')}   压住的按钮: ${r.hit.length ? r.hit.join('、') : '无'}`)
      check(`${name}的手柄没压在按钮上`, r.hit.length === 0, r.hit.join('、'))
    }
    const footBox = await J(`(() => {
      const node = document.querySelector('${sel(pid)}')
      const nr = node.getBoundingClientRect()
      const b = node.querySelector('.node-foot button').getBoundingClientRect()
      const h = node.querySelector('.resize-handle').getBoundingClientRect()
      return JSON.stringify({
        按钮底离节点底: Math.round(nr.bottom - b.bottom),
        按钮右到手柄左: Math.round(h.left - b.right),
        手柄右离节点右: Math.round(nr.right - h.right),
        手柄底离节点底: Math.round(nr.bottom - h.bottom),
      })
    })()`)
    console.log(
      `   按钮底边离节点底 ${footBox.按钮底离节点底}px · 按钮右边到手柄左边 ${footBox.按钮右到手柄左}px · ` +
      `手柄右下角离节点右下角 ${footBox.手柄右离节点右}px / ${footBox.手柄底离节点底}px`,
    )
    check(
      '手柄贴在节点右下角（不占布局）',
      footBox.手柄右离节点右 <= 2 && footBox.手柄底离节点底 <= 2,
      `${footBox.手柄右离节点右} / ${footBox.手柄底离节点底}`,
    )
    check('按钮与手柄之间留了缝，不抢位置', footBox.按钮右到手柄左 >= 4, `缝隙 ${footBox.按钮右到手柄左}px`)

    console.log('\n[2] 配置区排布')
    const rows = await cfgRows(pid)
    rows.forEach((r, i) => console.log(`   第 ${i + 1} 排: ${r.join('   ')}`))
    check('配置区不超过两排', rows.length <= 2, `${rows.length} 排`)
    check('模型/尺寸/张数在同一排', rows[0]?.length >= 3, (rows[0] || []).join(' '))
    check('开关单独占一排', (rows[1] || []).some((t) => t.includes('透明底')), (rows[1] || []).join(' '))

    console.log('\n[3] 选中节点显示尺寸')
    check('未选中任何节点时不显示', (await selInfo()) === '', JSON.stringify(await selInfo()))
    let p = await headPoint(pid)
    await click(p.x, p.y)
    const info1 = await selInfo()
    const sz1 = await nodeSize(pid)
    const expect1 = `${Math.round(sz1.w / z)} × ${Math.round(sz1.h / z)}`
    console.log(`   点中处理节点 → ${JSON.stringify(info1)}   节点实际 ${expect1}`)
    check('选中后出现尺寸信息', info1.includes('×'), info1)
    check('显示的是该节点的真实尺寸', info1.replace(/\s/g, '') === `尺寸${expect1.replace(/\s/g, '')}`, `${info1} vs 尺寸${expect1}`)

    const infoMid = await drag((await handlePoint(pid)).x, (await handlePoint(pid)).y, 90, 60)
    const sz2 = await nodeSize(pid)
    const expect2 = `${Math.round(sz2.w / z)} × ${Math.round(sz2.h / z)}`
    console.log(`   拖动中 → ${JSON.stringify(infoMid)}；松手后节点 ${expect2}`)
    check('拖动过程中尺寸实时更新', infoMid.replace(/\s/g, '') !== info1.replace(/\s/g, ''), infoMid)
    check('松手后显示新尺寸', (await selInfo()).replace(/\s/g, '') === `尺寸${expect2.replace(/\s/g, '')}`, `${await selInfo()} vs 尺寸${expect2}`)

    p = await headPoint(rid1)
    await click(p.x, p.y)
    const info3 = await selInfo()
    const szData = await nodeSize(rid1)
    const expectData = `${Math.round(szData.w / z)} × ${Math.round(szData.h / z)}`
    const m3 = info3.match(/(\d+)\s*×\s*(\d+)/)
    console.log(`   改点数据节点 → ${JSON.stringify(info3)}   节点实际 ${expectData}`)
    // 低缩放下 rect 会四舍五入，允许 1px 误差
    check(
      '切换选中对象后显示新节点的尺寸',
      !!m3 &&
        Math.abs(Number(m3[1]) - Math.round(szData.w / z)) <= 1 &&
        Math.abs(Number(m3[2]) - Math.round(szData.h / z)) <= 1 &&
        info3 !== info1,
      `${info3} vs 尺寸${expectData}`,
    )

    const empty = await panePoint()
    if (empty) {
      await click(empty.x, empty.y)
      check('点空白处后隐藏', (await selInfo()) === '', JSON.stringify(await selInfo()))
    } else {
      check('点空白处后隐藏', false, '没找到空白点')
    }

    console.log('\n[4] 最小宽度（服务端 sizeSpec 里的 minW / minH）')
    const hp = await handlePoint(pid)
    await click((await headPoint(pid)).x, (await headPoint(pid)).y)
    await drag(hp.x, hp.y, -1000, -1000)
    const shrunk = await nodeSize(pid)
    console.log(`   拖到极限 ${Math.round(shrunk.w / z)}×${Math.round(shrunk.h / z)}   最小应为 ${pmin.minW}×${pmin.minH}`)
    check('宽度卡在设置里的最小宽度', Math.round(shrunk.w / z) >= pmin.minW - 2, `${Math.round(shrunk.w / z)} >= ${pmin.minW}`)
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
