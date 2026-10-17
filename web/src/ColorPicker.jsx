/* 自研取色器：点节点上的色块弹出，色域 + 色相条 + **两个纯文本输入框**（十六进制 / RGB），
 * 最下面还有一列**颜色库**（调用方传进来的 `colors`，见文件末尾的 `.color-lib`）。
 *
 * 为什么要自研：原先用的是 `<input type="color">`，弹出来的是**浏览器原生取色器** ——
 * 它的 DOM 我们碰不到，往里加输入框是不可能的（能看到、能点，但不受我们控制）。
 * 需求是「同一个框里既能打 hex 又能打 RGB」，所以只能自己画一个。
 *
 * 两个文本框的规矩（见 `color.js`）：成功才同步另一侧；失败不动值、只把**原因**
 * 交给 `onTip`（画布底部那行临时提示），用户输入的原文留着方便改。
 * 颜色参数**没有透明度**，所以标签是 `RGB` 而不是 `RGBA`，带 alpha 的写法
 * （4 个数 / 8 位 hex）一律报错，不做「收了但丢掉」。
 *
 * 布局：左列两个输入框上下排，右列一个吸管按钮（跨两行）—— 见 `.color-grid`。
 *
 * 定位：节点在 React Flow 的 transform 里，**里面写 position: fixed 会以视口元素为
 * 参照系**（还会跟着缩放一起被放大）。所以浮层用 portal 挂到 body 上、自己量锚点的
 * 屏幕坐标；画布平移或缩放时靠 `useViewport()` 重渲染再量一次 —— 和资源面板的右键
 * 菜单同一套路（它也挂在 body 上）。
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useViewport } from '@xyflow/react'
import { hexToRgb, hsvToRgb, parseHex, parseRgb, rgbToHex, rgbToHsv, toRgbText } from './color'

const SV_W = 208
const SV_H = 128
const HUE_H = 13
const POP_W = SV_W + 20 // 左右各 10px 内边距

const clamp01 = (v) => Math.max(0, Math.min(1, v))
const HEX_OK = /^#[0-9a-fA-F]{6}$/

/** 节点上的那个色块 + 它自己的浮层。`onCommit(hex)` / `onTip(msg)` */
export default function ColorField({ value, label, onCommit, onTip, colors }) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef(null)
  // 历史值不合法（手改过 graph.json）时兜成黑色，别让 React 收到非法样式
  const v = HEX_OK.test(value || '') ? value : '#000000'

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className="nodrag nopan cfg-color"
        style={{ background: v }}
        title={`${label}：${v} —— 点开取色器，也可以直接输入 #RRGGBB 或 R, G, B`}
        onClick={() => setOpen((o) => !o)}
      />
      {open ? (
        <Popover
          anchor={btnRef}
          value={v}
          colors={colors}
          onCommit={onCommit}
          onTip={onTip}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  )
}

function Popover({ anchor, value, colors, onCommit, onTip, onClose }) {
  const vp = useViewport() // 画布平移 / 缩放时要重新量锚点，靠它触发重渲染
  const popRef = useRef(null)
  const svRef = useRef(null)
  const hueRef = useRef(null)
  const dragRef = useRef(null) // 'sv' | 'hue' | null
  const hsvRef = useRef(null) // 拖动期间用它算，避免闭包里拿到旧值

  const [pos, setPos] = useState(null)
  const [hsv, setHsv] = useState(() => rgbToHsv(hexToRgb(value) || [0, 0, 0]))
  const [hexText, setHexText] = useState(value)
  const [rgbText, setRgbText] = useState(toRgbText(value))
  // 颜色库默认**收起**：颜色会越积越多，一上来摊开整张列表会把浮层撑得很高
  // （而且下面还要放输入框）。收起时只占一行，点开才列。
  const [libOpen, setLibOpen] = useState(false)
  hsvRef.current = hsv

  // 调用方没给颜色库就是空 —— 那一块干脆不画。定义在定位那个 effect 之前：
  // 它的依赖数组要在渲染期就拿到值（写在后面会撞上 TDZ）
  const lib = colors || []
  const libCount = lib.length

  /* onClose 每次渲染都是新的函数，用 ref 兜住，两个监听都只挂**一次**。
     这是 ImageViewer 里踩过的同一个坑：把 onClose 直接放进依赖数组，监听就会被
     反复摘掉又挂上 —— 正巧在「摘掉」那一瞬间来的 Esc 就没人接，表现为「Esc 时灵时不灵」。 */
  const closeRef = useRef(onClose)
  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])

  /* 外部值变了（拖色域、吸管、或别处改了这个参数）→ 同步文本框与色域位置。
     拖动过程**不重算 hsv**：值要过一遍 8 位 hex，来回取整会让圆点在拖动时轻微抖动。 */
  useEffect(() => {
    const rgb = hexToRgb(value)
    if (!rgb) return
    if (!dragRef.current) {
      setHsv((old) => {
        const next = rgbToHsv(rgb)
        // 灰色（s=0）时色相没有定义：保留上一个 h，否则拖到黑/白时色相条会突然跳回红
        return next.s === 0 && old.s === 0 ? { ...next, h: old.h } : next
      })
    }
    setHexText(value)
    setRgbText(toRgbText(value))
  }, [value])

  /* 位置：先量锚点、再按浮层实际尺寸夹进窗口（右下角就往上翻）。
     画布平移 / 缩放会走到这里（vp 变了 → 重渲染 → 重新量）。
     **展开颜色库也要重算**（`libOpen` 在依赖里）：浮层会一下子变高，不重算的话
     底下那几行就掉到窗口外去了；最后那一行是兜底 —— 翻上去仍然放不下时，宁可
     整块往上挪，也不能让列表的下半截点不到。 */
  useLayoutEffect(() => {
    const a = anchor.current?.getBoundingClientRect()
    const el = popRef.current
    if (!a || !el) return
    const w = el.offsetWidth || POP_W
    const h = el.offsetHeight || 260
    let left = a.left
    let top = a.bottom + 6
    if (left + w > window.innerWidth - 8) left = Math.max(8, window.innerWidth - w - 8)
    if (top + h > window.innerHeight - 8) top = Math.max(8, a.top - h - 6)
    if (top + h > window.innerHeight - 8) top = Math.max(8, window.innerHeight - h - 8)
    setPos({ left, top })
  }, [anchor, vp.x, vp.y, vp.zoom, libOpen, libCount])

  // 点到浮层外面收起（点自己那个色块不算 —— 交给它的 onClick 去开关，否则会「关了又开」）
  useEffect(() => {
    const onDoc = (e) => {
      const t = e.target
      if (!(t instanceof Element)) return
      if (popRef.current?.contains(t) || anchor.current?.contains(t)) return
      closeRef.current?.()
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [anchor])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') closeRef.current?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  /* 统一入口：本地状态与外部值一起更新。传进来的必定是合法颜色
     （不合法的在 commitHex / commitRgb 那层就挡下并报原因了）。 */
  const applyRgb = (rgb) => {
    const hex = rgbToHex(rgb)
    const next = rgbToHsv(rgb)
    hsvRef.current = next
    setHsv(next)
    setHexText(hex)
    setRgbText(toRgbText(hex))
    onCommit(hex)
  }

  const applyHsv = (patch) => {
    const next = { ...(hsvRef.current || hsv), ...patch }
    hsvRef.current = next
    setHsv(next)
    onCommit(rgbToHex(hsvToRgb(next)))
  }

  // ---- 色域 / 色相条：按下即可拖，拖到外面也继续跟（setPointerCapture）----
  const moveIn = (which, e) => {
    if (which === 'sv') {
      const r = svRef.current.getBoundingClientRect()
      applyHsv({
        s: clamp01((e.clientX - r.left) / r.width),
        v: 1 - clamp01((e.clientY - r.top) / r.height),
      })
    } else {
      const r = hueRef.current.getBoundingClientRect()
      applyHsv({ h: clamp01((e.clientX - r.left) / r.width) * 360 })
    }
  }
  const down = (which) => (e) => {
    e.preventDefault()
    dragRef.current = which
    try {
      e.currentTarget.setPointerCapture(e.pointerId)
    } catch { /* 拿不到就算了，只是拖出元素外会断 */ }
    moveIn(which, e)
  }
  const move = (which) => (e) => {
    if (dragRef.current === which) moveIn(which, e)
  }
  const up = (e) => {
    dragRef.current = null
    try {
      e.currentTarget.releasePointerCapture(e.pointerId)
    } catch { /* 同上 */ }
  }

  const commitHex = () => {
    const r = parseHex(hexText)
    if (!r.ok) return onTip(r.err) // 值不动、原文留着，让人接着改
    applyRgb(r.rgb)
  }
  const commitRgb = () => {
    const r = parseRgb(rgbText)
    if (!r.ok) return onTip(r.err)
    applyRgb(r.rgb)
  }
  // 回车即提交；失焦也提交（但点浮层里的别处失焦时，浮层还在，不影响）
  const onKeyDown = (commit) => (e) => {
    if (e.key !== 'Enter') return
    e.preventDefault()
    commit()
  }

  const canPick = typeof window !== 'undefined' && !!window.EyeDropper
  const pickScreen = async () => {
    try {
      const res = await new window.EyeDropper().open()
      const r = parseHex(res?.sRGBHex || '')
      if (r.ok) applyRgb(r.rgb)
    } catch { /* 用户按 Esc 取消：什么都不做 */ }
  }

  const hue = hsv.h
  const svBg =
    `linear-gradient(to top, #000, rgba(0,0,0,0)),` +
    `linear-gradient(to right, #fff, hsl(${hue}, 100%, 50%))`

  return createPortal(
    <div
      ref={popRef}
      className="color-pop"
      style={{ left: pos ? pos.left : 0, top: pos ? pos.top : 0, visibility: pos ? 'visible' : 'hidden' }}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div
        ref={svRef}
        className="color-sv nodrag"
        style={{ width: SV_W, height: SV_H, background: svBg }}
        onPointerDown={down('sv')}
        onPointerMove={move('sv')}
        onPointerUp={up}
        onPointerCancel={up}
      >
        <span className="color-knob" style={{ left: hsv.s * SV_W, top: (1 - hsv.v) * SV_H }} />
      </div>

      <div
        ref={hueRef}
        className="color-hue nodrag"
        style={{ width: SV_W, height: HUE_H }}
        onPointerDown={down('hue')}
        onPointerMove={move('hue')}
        onPointerUp={up}
        onPointerCancel={up}
      >
        <span className="color-knob" style={{ left: (hue / 360) * SV_W, top: HUE_H / 2 }} />
      </div>

      {/* 左列两个输入框上下排，右列吸管（跨两行）—— 见 styles.css 的 .color-grid */}
      <div className="color-grid">
        <label className="color-fld">
          <span className="color-lbl">HEX</span>
          <input
            className="nodrag"
            value={hexText}
            spellCheck={false}
            placeholder="#ff8800"
            onChange={(e) => setHexText(e.target.value)}
            onBlur={commitHex}
            onKeyDown={onKeyDown(commitHex)}
          />
        </label>

        {canPick ? (
          <button type="button" className="color-eye" title="在屏幕上吸一个颜色" onClick={pickScreen}>
            ⌖
          </button>
        ) : null}

        <label className="color-fld">
          <span className="color-lbl">RGB</span>
          <input
            className="nodrag"
            value={rgbText}
            spellCheck={false}
            placeholder="255, 136, 0"
            onChange={(e) => setRgbText(e.target.value)}
            onBlur={commitRgb}
            onKeyDown={onKeyDown(commitRgb)}
          />
        </label>
      </div>

      {/* 颜色库：**默认收起的一行**，点开才列 —— 颜色会越积越多，一上来摊开整张列表
          会把浮层撑得老高（下面还得放输入框）。
          选完**立刻收起浮层**：这是「快速选择」，不是让人在这儿挑色阶。
          数据由调用方给（节点走 data.colors、颜色库的编辑窗走 state.colors），
          没有就不画这一块，取色器其余功能一点不受影响。 */}
      {libCount ? (
        <div className="color-lib">
          <button
            type="button"
            className={`color-lib-toggle${libOpen ? ' open' : ''}`}
            aria-expanded={libOpen}
            title={libOpen ? '收起颜色库' : '展开颜色库'}
            onClick={() => setLibOpen((o) => !o)}
          >
            <span className="color-lib-h">颜色库</span>
            <span className="color-lib-n">{libCount}</span>
            <span className="color-lib-caret">▾</span>
          </button>

          {libOpen ? (
            <div className="color-lib-list">
              {lib.map((c) => {
                // 存下来的值理论上都是 #rrggbb（服务端归一过），坏值只当没有色块画
                const ok = HEX_OK.test(c.value || '')
                return (
                  <button
                    key={c.id}
                    type="button"
                    className={`color-lib-row${ok && c.value === value ? ' on' : ''}`}
                    title={`${c.name} · ${c.value}`}
                    onClick={() => {
                      const rgb = ok ? hexToRgb(c.value) : null
                      if (!rgb) return
                      applyRgb(rgb)
                      closeRef.current?.()
                    }}
                  >
                    <span className="color-lib-sw" style={{ background: ok ? c.value : 'transparent' }} />
                    <span className="color-lib-name">{c.name}</span>
                  </button>
                )
              })}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>,
    document.body,
  )
}
