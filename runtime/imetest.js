// 诊断：提示词框在中文输入法（IME）组合期间是否会打断拼音。
// 用 CDP 的 Input.imeSetComposition 模拟真实 IME 的预编辑序列。
// 全程在一次性临时工作目录里跑，不碰用户的真实工作目录。
import { spawn } from 'node:child_process'
import { rmSync } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9227
const BASE = process.argv[2] || 'http://127.0.0.1:8770'
const TMPWS = `${RUNTIME}/testws-ime`

const chrome = spawn(
  CHROME,
  [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--no-first-run', '--no-default-browser-check',
    // 全新 profile 首次启动会加载默认扩展，拖慢并霸占 target 列表
    '--disable-extensions', '--disable-component-extensions-with-background-pages',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${RUNTIME}/chrome-profile-ime`,
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
  await post('/api/processes', { defId: 'ai_image', x: 200, y: 120 })
  console.log(`已切到一次性工作目录（原目录: ${origWs}）`)

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
    // 注意：这里在 try 之外，必须自己把工作目录还回去，
    // 否则一次启动失败就会让服务永久卡在临时目录上
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

  try {
    await send('Page.navigate', { url: BASE })
    await sleep(3000)

    // 挂事件监听：记录 composition / input 事件序列
    await evalJs(`(() => {
      window.__ev = []
      const el = document.querySelector('.prompt-box')
      if (!el) return 'no textarea'
      for (const t of ['compositionstart','compositionupdate','compositionend','input']) {
        el.addEventListener(t, (e) => {
          window.__ev.push(t + '|data=' + JSON.stringify(e.data ?? null) + '|value=' + JSON.stringify(el.value))
        })
      }
      el.focus()
      return 'ok'
    })()`)

    console.log('\n===== 模拟中文输入法：打「sheng」再选「省」 =====')
    const steps = ['s', 'sh', 'she', 'shen', 'sheng']
    for (const t of steps) {
      await send('Input.imeSetComposition', { text: t, selectionStart: t.length, selectionEnd: t.length })
      await sleep(90)
      const v = await evalJs(`document.querySelector('.prompt-box').value`)
      console.log(`  imeSetComposition(${JSON.stringify(t).padEnd(8)}) → textarea.value = ${JSON.stringify(v)}`)
    }

    console.log('\n  --- 提交候选字「省」 ---')
    await send('Input.insertText', { text: '省' })
    await sleep(400)
    const finalV = await evalJs(`document.querySelector('.prompt-box').value`)
    console.log(`  最终 textarea.value = ${JSON.stringify(finalV)}`)

    const events = JSON.parse(await evalJs(`JSON.stringify(window.__ev)`))
    console.log('\n  事件序列：')
    for (const e of events) console.log('    ' + e)

    console.log('\n===== 判定 1：中文输入是否被 IME 组合打断 =====')
    check('最终内容包含完整的中文字', (finalV || '').includes('省'), JSON.stringify(finalV))
    check('没有把拼音残留在正文里', !/[a-z]{2,}/.test(finalV || ''), JSON.stringify(finalV))
    const nComp = events.filter((e) => e.startsWith('compositionstart')).length
    check(
      '整段拼音只开启一次组合（多次 = 被 React 打断）',
      nComp === 1,
      `compositionstart × ${nComp}`,
    )

    console.log('\n===== 判定 2：连打第二个词，组合仍不中断 =====')
    for (const t of ['r', 're', 'ren']) {
      await send('Input.imeSetComposition', { text: t, selectionStart: t.length, selectionEnd: t.length })
      await sleep(80)
    }
    await send('Input.insertText', { text: '人' })
    await sleep(500)
    const twoV = await evalJs(`document.querySelector('.prompt-box').value`)
    const ev2 = JSON.parse(await evalJs(`JSON.stringify(window.__ev)`))
    const comp2 = ev2.filter((e) => e.startsWith('compositionstart')).length
    console.log(`  连打后 value = ${JSON.stringify(twoV)}   compositionstart 累计 × ${comp2}`)
    check('连续两次中文输入都完整', twoV === '省人', JSON.stringify(twoV))
    check('两次输入共只开启 2 次组合', comp2 === 2, `× ${comp2}`)

    console.log('\n===== 判定 3：输入内容是否落库 =====')
    await sleep(1000) // 等过 onParam 的 500ms debounce
    const saved = (await state()).graph.processes[0]?.params?.prompt
    console.log(`  服务端 params.prompt = ${JSON.stringify(saved)}`)
    check('提示词已保存到服务端', saved === '省人', JSON.stringify(saved))

    console.log('\n===== 判定 4：刷新页面后是否正确回显 =====')
    await send('Page.navigate', { url: BASE })
    await sleep(3000)
    const reloaded = await evalJs(`document.querySelector('.prompt-box')?.value`)
    console.log(`  刷新后 textarea.value = ${JSON.stringify(reloaded)}`)
    check('已有提示词能正确回显', reloaded === '省人', JSON.stringify(reloaded))

    console.log('\n===== 判定 5：普通编辑（追加/删除）仍然可用 =====')
    await evalJs(`(() => {
      const el = document.querySelector('.prompt-box')
      el.focus()
      el.setSelectionRange(el.value.length, el.value.length)
      return el.value
    })()`)
    await send('Input.insertText', { text: '！' })
    await sleep(900)
    const appended = (await state()).graph.processes[0]?.params?.prompt
    console.log(`  追加后服务端值 = ${JSON.stringify(appended)}`)
    check('普通追加输入后能落库', appended === '省人！', JSON.stringify(appended))
  } finally {
    ws.close()
    chrome.kill()
    await post('/api/workspace', { path: origWs })
    rmSync(TMPWS, { recursive: true, force: true })
    rmSync(`${RUNTIME}/chrome-profile-ime`, { recursive: true, force: true })
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
