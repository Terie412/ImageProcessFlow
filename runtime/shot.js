// 用 CDP 驱动无头 Chrome：导航 → 派发 contextmenu → 截图
import { spawn } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { CHROME, ROOT, RUNTIME } from './_common.js'

const PORT = 9222
const URL_ = process.argv[2] || 'http://127.0.0.1:8770'
const OUT = process.argv[3] || `${RUNTIME}/shot.png`
// SHOT_W / SHOT_H：换个视口宽度截图（看顶栏在窄窗口下怎么折行）
const WIN = `${process.env.SHOT_W || 1680},${process.env.SHOT_H || 1050}`

const chrome = spawn(
  CHROME,
  [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--no-first-run', '--no-default-browser-check',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${RUNTIME}/chrome-profile`,
    `--window-size=${WIN}`,
    'about:blank',
  ],
  { stdio: 'ignore' },
)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function main() {
  let target = null
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()
      target = list.find((t) => t.type === 'page')
      if (target) break
    } catch { /* 还没起来 */ }
    await sleep(250)
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
  await send('Page.navigate', { url: URL_ })
  await sleep(2600)

  const evalJs = async (expr) =>
    (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value

  console.log('--- 右键前 ---')
  console.log(await evalJs(`JSON.stringify({
    toolbar: [...document.querySelectorAll('.toolbar button')].map(e => e.textContent.trim()),
    menu: !!document.querySelector('.ctx-menu')
  })`))

  // SHOT_VIEWER=1：双击第一个数据节点，打开大图查看器
  if (process.env.SHOT_VIEWER) {
    const p = await evalJs(`(() => {
      // 挑第一个「真的有图」的数据节点（空节点双击不会打开查看器，那是设计如此）
      const node = [...document.querySelectorAll('.react-flow__node-resource')]
        .find((n) => n.querySelector('.thumb img'))
      const el = node && node.querySelector('.thumb')
      if (!el) return ''
      const r = el.getBoundingClientRect()
      const x = Math.min(Math.max(r.left + r.width / 2, 20), innerWidth - 20)
      const y = Math.min(Math.max(r.top + r.height / 2, 70), innerHeight - 20)
      const hit = document.elementFromPoint(x, y)
      return JSON.stringify({
        x: Math.round(x), y: Math.round(y),
        hit: hit ? (hit.className || hit.tagName) + '' : 'null',
        node: el.closest('.react-flow__node')?.getAttribute('data-id'),
      })
    })()`)
    if (p) {
      console.log('双击目标:', p)
      const { x, y } = JSON.parse(p)
      for (const count of [1, 2]) {
        await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: count })
        await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: count })
        await sleep(60)
      }
      await sleep(800)
      console.log(
        '查看器：',
        await evalJs(`(() => {
          const m = document.querySelector('.viewer-mask')
          if (!m) return '(没打开)'
          const img = m.querySelector('img')
          return JSON.stringify({
            name: m.querySelector('.badge')?.textContent,
            meta: m.querySelector('.viewer-meta')?.textContent.replace(/\\s+/g,' ').trim(),
            mode: m.querySelector('.viewer-stage').className.replace('viewer-stage ',''),
            shown: img ? [Math.round(img.getBoundingClientRect().width), Math.round(img.getBoundingClientRect().height)] : null,
          })
        })()`),
      )
    }
  }

  // SHOT_CLICK=<选择器>：截图前先点一下它（派真实鼠标事件，React 的 onClick 才认）。
  // 逗号分隔可以点一串，例如先点开菜单、再点里面那一项。
  for (const sel of (process.env.SHOT_CLICK || '').split(',').filter(Boolean)) {
    const p = await evalJs(`(() => {
      const b = document.querySelector(${JSON.stringify(sel)})
      if (!b) return ''
      const r = b.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
    if (!p) {
      console.log(`SHOT_CLICK: 找不到 ${sel}`)
      break
    }
    const { x, y } = JSON.parse(p)
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 })
    await sleep(60)
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 })
    await sleep(400)
  }

  // SHOT_SETTINGS=1：点开顶栏齿轮，截图设置浮层
  if (process.env.SHOT_SETTINGS) {
    const p = await evalJs(`(() => {
      const b = document.querySelector('.settings-wrap .icon-btn')
      if (!b) return ''
      const r = b.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) })
    })()`)
    if (p) {
      const { x, y } = JSON.parse(p)
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 })
      await sleep(60)
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 })
      await sleep(400)
      console.log(
        '设置浮层：',
        await evalJs(`(() => {
          const g = [...document.querySelectorAll('.settings-pop .set-group')].map(e => e.textContent)
          const v = [...document.querySelectorAll('.settings-pop .set-size input')].map(e => e.value)
          return JSON.stringify({ groups: g, minInputs: v })
        })()`),
      )
    }
  }

  // SHOT_SELECT=1：点一下处理节点，让左上角的「尺寸」浮层显示出来
  if (process.env.SHOT_SELECT) {
    const p = await evalJs(`(() => {
      const el = document.querySelector('.react-flow__node-process .node-head')
        || document.querySelector('.react-flow__node .node-head')
      if (!el) return ''
      const r = el.getBoundingClientRect()
      return JSON.stringify({ x: Math.round(r.left + 55), y: Math.round(r.top + r.height / 2) })
    })()`)
    if (p) {
      const { x, y } = JSON.parse(p)
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 })
      await sleep(60)
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 })
      await sleep(450)
      console.log(
        '已选中节点，左上角显示：',
        await evalJs(`document.querySelector('.sel-info')?.textContent.replace(/\\s+/g,' ').trim() || '(无)'`),
      )
    }
  }

  if (process.env.SHOT_MENU !== '0') {
    console.log('--- 在画布空白处派发 contextmenu ---')
    console.log(await evalJs(`(() => {
    const pane = document.querySelector('.react-flow__pane') || document.body
    const r = pane.getBoundingClientRect()
    const x = r.left + r.width * ${process.env.SHOT_FX || 0.22}, y = r.top + r.height * ${process.env.SHOT_FY || 0.72}
    pane.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 2 }))
    return JSON.stringify({ x: Math.round(x), y: Math.round(y) })
  })()`))
    await sleep(500)

    console.log('--- 右键后 ---')
    console.log(await evalJs(`JSON.stringify({
    menu: !!document.querySelector('.ctx-menu'),
    sections: [...document.querySelectorAll('.ctx-menu .ctx-title')].map(e => e.textContent),
    items: [...document.querySelectorAll('.ctx-menu .ctx-item')].map(e => e.textContent)
  })`))
  }

  const zoom = parseInt(process.env.SHOT_ZOOM || '0', 10)
  if (zoom > 0) {
    for (let i = 0; i < zoom; i++) {
      await evalJs(
        `(() => { const b = document.querySelector('.react-flow__controls-zoomin'); if (b) b.click(); return 1 })()`,
      )
      await sleep(180)
    }
    console.log(`已放大 ${zoom} 级`)
  }

  const shot = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(OUT, Buffer.from(shot.data, 'base64'))
  console.log('screenshot ->', OUT)

  ws.close()
  chrome.kill()
}

main().catch((e) => {
  console.error('ERR', e.message)
  chrome.kill()
  process.exit(1)
})
