// 验证：① 设置里的「节点最小尺寸」能改、能兜底渲染 ② 输入口与提示词左右并排
//   ③ 输入口只显示序号、不显示图片名、＋ 在下方
// 全程在一次性临时工作目录里跑，不碰用户的真实画布。
import { spawn } from 'node:child_process'
import { readFileSync, rmSync, readFileSync as rd } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9235
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const SEED = `${RUNTIME}/seed.png`
const TMPWS = `${RUNTIME}/testws-layout`
const PROFILE = `${RUNTIME}/chrome-profile-layout`
const SETTINGS_FILE = `${ROOT}/settings.json`

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
const req = (method, path, body) =>
  fetch(`${BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(async (r) => ({ status: r.status, body: await r.json().catch(() => null) }))
const post = (p, b) => req('POST', p, b).then((r) => r.body)
const patch = (p, b) => req('PATCH', p, b)

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

  const st0 = await state()
  const origWs = (ORIG_WS = st0.workspace)
  const origSettings = st0.settings
  console.log(`原设置: ${JSON.stringify(origSettings.nodeMin)}`)

  // ---------- 1. 后端设置 ----------
  console.log('\n[1] nodeMin 设置（后端）')
  console.log(`   默认值: ${JSON.stringify(st0.settings.nodeMin)}`)
  check('数据节点宽 200 / 处理节点兜底高 280',
    st0.settings.nodeMin?.resource?.w === 200 && st0.settings.nodeMin?.process?.h === 280,
    JSON.stringify(st0.settings.nodeMin))

  let r = await patch('/api/settings', { nodeMin: { resource: { w: 300, h: 250 } } })
  console.log(`   只改数据节点 → ${JSON.stringify(r.body.settings.nodeMin)}`)
  check('改数据节点生效', r.body.settings.nodeMin.resource.w === 300 && r.body.settings.nodeMin.resource.h === 250)
  check('处理节点不受影响（部分更新会合并）', r.body.settings.nodeMin.process.w === 560)

  r = await patch('/api/settings', { nodeMin: { resource: { w: 10, h: 99999 } } })
  console.log(`   填 10 / 99999 → ${JSON.stringify(r.body.settings.nodeMin.resource)}`)
  check('过小被夹到 80', r.body.settings.nodeMin.resource.w === 80)
  check('过大被夹到 4000', r.body.settings.nodeMin.resource.h === 4000)

  r = await patch('/api/settings', { nodeMin: 'nope' })
  check('传字符串被拒（保持原值）', r.body.settings.nodeMin.resource.w === 80, JSON.stringify(r.body.settings.nodeMin.resource))

  r = await patch('/api/settings', { nodeMin: { bogus: { w: 1 }, resource: { w: 320, h: 260 } } })
  check('未知节点类型被忽略', !('bogus' in r.body.settings.nodeMin), JSON.stringify(r.body.settings.nodeMin))
  check('合法部分照常写入', r.body.settings.nodeMin.resource.w === 320)

  const onDisk = JSON.parse(rd(SETTINGS_FILE, 'utf-8'))
  check('落盘到 settings.json', onDisk.nodeMin?.resource?.w === 320, JSON.stringify(onDisk.nodeMin))

  // ---------- 准备画布 ----------
  rmSync(TMPWS, { recursive: true, force: true })
  await post('/api/workspace', { path: TMPWS })
  const rid1 = await makeDataNode(60, 120)
  await makeDataNode(60, 1100)
  const st = await post('/api/processes', { defId: 'ai_image', x: 420, y: 120 })
  const pid = st.graph.processes[st.graph.processes.length - 1].id
  await patch(`/api/processes/${pid}`, { ports: { image: 2 } })
  // 把两个节点都人为设成比最小值小
  await patch('/api/positions', [{ id: rid1, w: 120, h: 100 }])
  await patch('/api/positions', [{ id: pid, w: 200, h: 150 }])
  console.log(`\n已把数据节点设为 120×100、处理节点设为 200×150（都小于最小值）`)

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
  const zoom = async () =>
    (await evalJs(`(() => {
      const t = document.querySelector('.react-flow__viewport')?.style.transform || ''
      const m = t.match(/scale\\(([\\d.]+)\\)/)
      return m ? Number(m[1]) : 1
    })()`)) || 1
  const renderedSize = async (n) =>
    J(`(() => {
      const r = document.querySelector('${sel(n)}').getBoundingClientRect()
      return JSON.stringify({ w: Math.round(r.width), h: Math.round(r.height) })
    })()`)
  async function click(x, y) {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 })
    await sleep(50)
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 })
    await sleep(300)
  }

  try {
    await send('Page.navigate', { url: BASE })
    await sleep(4000)
    const z = await zoom()

    console.log('\n[2] 渲染时按设置里的最小值兜底')
    let s = await renderedSize(rid1)
    console.log(`   数据节点：存 120×100 → 渲染 ${Math.round(s.w / z)}×${Math.round(s.h / z)}   最小 320×260`)
    check('数据节点被顶到最小值', Math.round(s.w / z) >= 320 - 2 && Math.round(s.h / z) >= 260 - 2,
      `${Math.round(s.w / z)}×${Math.round(s.h / z)}`)
    s = await renderedSize(pid)
    console.log(`   处理节点：存 200×150 → 渲染 ${Math.round(s.w / z)}×${Math.round(s.h / z)}   最小 560×280`)
    check('处理节点被顶到最小值', Math.round(s.w / z) >= 560 - 2 && Math.round(s.h / z) >= 280 - 2,
      `${Math.round(s.w / z)}×${Math.round(s.h / z)}`)

    console.log('\n[3] 左栏只放输入口，其余全在右侧')
    const geo = await J(`(() => {
      const n = document.querySelector('${sel(pid)}')
      const g = (s) => n.querySelector(s).getBoundingClientRect()
      const p = g('.ports'), t = g('.prompt-wrap'), c = g('.cfg'), f = g('.node-foot')
      return JSON.stringify({
        ports: [Math.round(p.left), Math.round(p.top), Math.round(p.right), Math.round(p.bottom)],
        prompt: [Math.round(t.left), Math.round(t.top), Math.round(t.right), Math.round(t.bottom)],
        cfgLeft: Math.round(c.left),
        footLeft: Math.round(f.left),
        nodeBottom: Math.round(n.getBoundingClientRect().bottom),
        portsBottom: Math.round(p.bottom),
      })
    })()`)
    console.log(`   输入栏 x:${geo.ports[0]}~${geo.ports[2]}  提示词 x:${geo.prompt[0]}~${geo.prompt[2]}`)
    check('输入栏在提示词左边', geo.ports[2] <= geo.prompt[0] + 2, `${geo.ports[2]} <= ${geo.prompt[0]}`)
    const overlap = Math.min(geo.ports[3], geo.prompt[3]) - Math.max(geo.ports[1], geo.prompt[1])
    check('两栏垂直方向有重叠（是并排不是上下）', overlap > 40, `重叠 ${overlap}px`)
    console.log(`   参数区左边界 ${geo.cfgLeft} · 执行按钮左边界 ${geo.footLeft}（输入栏右边界 ${geo.ports[2]}）`)
    check('参数区在输入口栏右侧', geo.cfgLeft >= geo.ports[2] - 2, `${geo.cfgLeft} >= ${geo.ports[2]}`)
    check('执行按钮在输入口栏右侧', geo.footLeft >= geo.ports[2] - 2, `${geo.footLeft} >= ${geo.ports[2]}`)
    check('输入口栏撑满节点剩余高度',
      Math.abs(geo.portsBottom - geo.nodeBottom) <= 3, `${geo.portsBottom} vs ${geo.nodeBottom}`)

    console.log('\n[3b] 输入口栏更紧凑了')
    const compact = await J(`(() => {
      const b = document.querySelector('${sel(pid)} .pin-btn').getBoundingClientRect()
      const row = document.querySelector('${sel(pid)} .port-row')
      const col = document.querySelector('${sel(pid)} .ports').getBoundingClientRect()
      const label = document.querySelector('${sel(pid)} .port-label')
      return JSON.stringify({
        btn: Math.round(b.width / ${z} * 100) / 100,
        rowFont: parseFloat(getComputedStyle(row).fontSize),
        labelFont: parseFloat(getComputedStyle(label).fontSize),
        colWidth: Math.round(col.width / ${z}),
      })
    })()`)
    console.log(`   ＋/− 按钮 ${compact.btn}px · 行字号 ${compact.rowFont}px · 序号字号 ${compact.labelFont}px · 栏宽 ${compact.colWidth}px`)
    check('加减号按钮变小（≤18px）', compact.btn <= 18, `${compact.btn}px`)
    check('输入口字号变小（≤11px）', compact.rowFont <= 11 && compact.labelFont <= 11.5,
      `${compact.rowFont}/${compact.labelFont}`)
    check('栏宽收窄（≤72px）', compact.colWidth <= 72, `${compact.colWidth}px`)

    console.log('\n[3c] 润色按钮在提示词框右侧、贴底')
    const pa = await J(`(() => {
      const g = (s) => document.querySelector('${sel(pid)} ' + s).getBoundingClientRect()
      const t = g('.prompt-box'), a = g('.prompt-actions'), w = g('.prompt-wrap')
      return JSON.stringify({
        boxRight: Math.round(t.right), boxBottom: Math.round(t.bottom),
        actLeft: Math.round(a.left), actBottom: Math.round(a.bottom),
        wrapBottom: Math.round(w.bottom),
      })
    })()`)
    console.log(`   提示词框右边 ${pa.boxRight} / 底边 ${pa.boxBottom}   按钮左边 ${pa.actLeft} / 底边 ${pa.actBottom}`)
    check('按钮在提示词框右侧（同一行并排）', pa.actLeft >= pa.boxRight - 2, `${pa.actLeft} >= ${pa.boxRight}`)
    check('按钮贴在这一行底部', Math.abs(pa.actBottom - pa.wrapBottom) <= 4,
      `按钮底 ${pa.actBottom} vs 行底 ${pa.wrapBottom}`)
    check('按钮与输入框底边对齐', Math.abs(pa.actBottom - pa.boxBottom) <= 4,
      `${pa.actBottom} vs ${pa.boxBottom}`)

    console.log('\n[4] 输入口一行的内容')
    const rows = await J(`(() => {
      const box = document.querySelector('${sel(pid)} .ports')
      const out = [...box.querySelectorAll('.port-row')].map((el) => ({
        label: el.querySelector('.port-label')?.textContent || '',
        handle: !!el.querySelector('.react-flow__handle'),
        minus: !!el.querySelector('.row-del'),
        chips: el.querySelectorAll('.chip, .badge').length,
        title: el.getAttribute('title') || '',
      }))
      const plus = box.querySelector('.port-ctl .pin-btn')
      const last = box.querySelector('.port-row:last-of-type') || box.querySelectorAll('.port-row')[box.querySelectorAll('.port-row').length - 1]
      return JSON.stringify({
        rows: out,
        plusInPorts: !!plus,
        plusBelowLast: plus && last
          ? Math.round(plus.getBoundingClientRect().top) >= Math.round(last.getBoundingClientRect().bottom) - 2
          : false,
      })
    })()`)
    console.log(`   每行: ${rows.rows.map((r) => `[${r.label}]${r.handle ? '◉' : ''}${r.minus ? '−' : ''}`).join('  ')}`)
    console.log(`   第一行 tooltip: ${JSON.stringify(rows.rows[0]?.title)}`)
    check('只显示序号（1、2）', rows.rows.map((r) => r.label).join(',') === '1,2', rows.rows.map((r) => r.label).join(','))
    check('不显示图片名', rows.rows.every((r) => r.chips === 0))
    check('每行都有接口圈和减号', rows.rows.every((r) => r.handle && r.minus))
    check('tooltip 保留完整名字', (rows.rows[0]?.title || '').includes('参考图1'), rows.rows[0]?.title)
    check('＋ 在输入口栏内、且在最后一个输入口下方', rows.plusInPorts && rows.plusBelowLast)

    console.log('\n[5] 设置浮层里改最小尺寸（真实输入）')
    await click(0, 0) // 先点空白关掉可能开着的浮层
    const gear = await J(`(() => {
      const b = document.querySelector('.settings-wrap .icon-btn').getBoundingClientRect()
      return JSON.stringify({ x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) })
    })()`)
    await click(gear.x, gear.y)
    const ui = await J(`(() => {
      const groups = [...document.querySelectorAll('.settings-pop .set-group')].map((e) => e.textContent)
      const inputs = [...document.querySelectorAll('.settings-pop .set-size input')].map((e) => e.value)
      return JSON.stringify({ groups, inputs })
    })()`)
    console.log(`   分组: ${JSON.stringify(ui.groups)}   各输入框: ${JSON.stringify(ui.inputs)}`)
    check('有「节点最小尺寸」分组', ui.groups.includes('节点最小尺寸'), ui.groups.join('/'))
    // 期望值从服务端现算：数据节点 + 每个已注册的处理节点类型（顺序同前端 minRows）
    const nmNow = (await state()).settings.nodeMin
    const mans = Object.keys((await state()).manifests).sort()
    const expectInit = [
      nmNow.resource.w, nmNow.resource.h,
      ...mans.flatMap((id) => {
        const spec = nmNow[`process:${id}`] || nmNow.process
        return [spec.w, spec.h]
      }),
    ].join(',')
    check('输入框初值 = 数据节点 + 每个处理节点类型的当前最小值',
      ui.inputs.join(',') === expectInit, `${ui.inputs.join(',')} vs ${expectInit}`)

    // 真实改：把数据节点最小宽改成 400
    const box = await J(`(() => {
      const r = document.querySelectorAll('.settings-pop .set-size input')[0].getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
    await click(box.x, box.y)
    await send('Input.dispatchKeyEvent', { type: 'keyDown', modifiers: 2, key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65 })
    await send('Input.dispatchKeyEvent', { type: 'keyUp', modifiers: 2, key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65 })
    await send('Input.insertText', { text: '400' })
    await sleep(200)
    // 失焦触发提交
    const blank = await J(`(() => {
      const r = document.querySelector('.settings-pop .set-group').getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + 5), y: Math.round(r.top + r.height / 2) })
    })()`)
    await click(blank.x, blank.y)
    await sleep(900)
    const after = (await state()).settings.nodeMin
    console.log(`   输入 400 后服务端: ${JSON.stringify(after.resource)}`)
    check('界面上输入的数字写进了服务端', after.resource.w === 400, JSON.stringify(after.resource))
    const s2 = await renderedSize(rid1)
    console.log(`   数据节点渲染宽度 → ${Math.round(s2.w / z)}（最小 400）`)
    check('节点立刻按新最小值重排', Math.round(s2.w / z) >= 400 - 2, `${Math.round(s2.w / z)}`)

    console.log('\n[6] 最小尺寸按「具体处理节点类型」分开，而不是按大类')
    const uiRows = await J(`JSON.stringify([...document.querySelectorAll('.settings-pop .set-field > span')].map(e => e.textContent))`)
    console.log(`   设置浮层里列出的行: ${JSON.stringify(uiRows)}`)
    check('数据节点一行 + 每个处理节点类型各一行（用 manifest 名字）',
      uiRows.includes('数据节点')
      && uiRows.some((l) => l.includes('Qwen'))
      && uiRows.some((l) => l.includes('裁剪')),
      uiRows.join(' / '))

    const rr = await patch('/api/settings', { nodeMin: { 'process:ai_image': { w: 700, h: 400 } } })
    console.log(`   只设 process:ai_image=700×400 → ${JSON.stringify(rr.body.settings.nodeMin)}`)
    check('具体类型被写入', rr.body.settings.nodeMin['process:ai_image']?.w === 700)
    check('通用 process 兜底不受影响', rr.body.settings.nodeMin.process.w === 560,
      `process.w=${rr.body.settings.nodeMin.process.w}`)

    // 通过接口改的，前端不会自动刷新 —— 重载页面再量
    await send('Page.navigate', { url: BASE })
    await sleep(3800)
    const s3 = await renderedSize(pid)
    console.log(`   处理节点渲染 ${Math.round(s3.w / z)}×${Math.round(s3.h / z)}（该类型专属最小 700×400）`)
    check('节点按该类型的专属最小值重排',
      Math.round(s3.w / z) >= 700 - 2 && Math.round(s3.h / z) >= 400 - 2,
      `${Math.round(s3.w / z)}×${Math.round(s3.h / z)}`)

    const del = await patch('/api/settings', { nodeMin: { 'process:ai_image': null } })
    console.log(`   传 null 删除后 → ${JSON.stringify(del.body.settings.nodeMin)}`)
    check('传 null 能删掉该项（回到通用兜底）',
      !('process:ai_image' in del.body.settings.nodeMin), JSON.stringify(del.body.settings.nodeMin))
    check('基准项 resource / process 删不掉',
      !!del.body.settings.nodeMin.resource && !!del.body.settings.nodeMin.process)
  } finally {
    ws.close()
    chrome.kill()
    // 把测试期间新建的 process:* 项删掉（传 null），再把原值写回
    const origMin = origSettings.nodeMin || {}
    const nowMin = (await state()).settings.nodeMin || {}
    const cleanup = { ...origMin }
    for (const k of Object.keys(nowMin)) {
      if (!(k in origMin)) cleanup[k] = null
    }
    await patch('/api/settings', { nodeMin: cleanup })
    await patch('/api/settings', origSettings)
    await post('/api/workspace', { path: origWs })
    rmSync(TMPWS, { recursive: true, force: true })
    try { rmSync(PROFILE, { recursive: true, force: true }) } catch { /* Chrome 可能还占着目录 */ }
    const back = (await state()).settings.nodeMin
    console.log(`\n已恢复工作目录与设置: ${JSON.stringify(back)}`)
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
