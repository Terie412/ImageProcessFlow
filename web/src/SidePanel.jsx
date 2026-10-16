/*
 * 左侧侧栏的**外壳**：页签（资源 / 颜色）+ 面板宽度 + 收起。
 *
 * 为什么要单独一层：资源面板和颜色库是两个平级的页签，共用同一份宽度、同一个开合状态、
 * 同一条右边缘的拖宽手柄。宽度和开合都是纯界面偏好 —— 记 localStorage，不进 graph.json。
 *
 * 页签内容各自独立：切过去才挂载、切走就卸载。两个面板的「记忆」都不在组件里
 * （资源面板的展开状态记 localStorage、颜色库的数据在服务端），所以卸载不会丢东西 ——
 * 切回来时各自按记录重读一遍（资源面板的懒加载树本来就是这么设计的）。
 */
import React, { useEffect, useRef, useState } from 'react'
import ResourcePanel from './ResourcePanel'
import ColorPanel from './ColorPanel'

/* 面板宽度可以拖右边框改 */
const RES_W = { min: 180, max: 640, def: 262 }
const clampW = (v) => Math.max(RES_W.min, Math.min(RES_W.max, Math.round(v)))

const TAB_KEY = 'ipf.sideTab'
const TABS = [
  { id: 'res', label: '资源', title: '资源目录：拖图到画布，右键新建目录 / 改名 / 删除' },
  { id: 'color', label: '颜色', title: '颜色库：记下来的颜色，一键复制 RGB / Hex' },
]

export default function SidePanel({ state, applyState, flash, open, onToggle }) {
  /* 上次看的是哪个页签 —— 刷新页面、重开面板都回到那一页 */
  const [tab, setTab] = useState(() => {
    try {
      const t = localStorage.getItem(TAB_KEY)
      return TABS.some((x) => x.id === t) ? t : 'res'
    } catch (e) {
      return 'res'
    }
  })

  const pick = (id) => {
    setTab(id)
    try {
      localStorage.setItem(TAB_KEY, id)
    } catch (e) {
      /* 隐私模式下写不了，忽略 */
    }
  }

  // 面板宽度（可拖右边框改，记 localStorage）
  const [width, setWidth] = useState(() => {
    try {
      const v = Number(localStorage.getItem('ipf.resWidth'))
      return Number.isFinite(v) && v > 0 ? clampW(v) : RES_W.def
    } catch (e) {
      return RES_W.def
    }
  })
  const widthRef = useRef(width)
  useEffect(() => {
    widthRef.current = width
  }, [width])

  const saveWidth = (w) => {
    try {
      localStorage.setItem('ipf.resWidth', String(w))
    } catch (e) {
      /* 隐私模式下写不了，忽略 */
    }
  }

  // 拖右边缘改宽度：mousemove 只改 state（不落盘），松手才写 localStorage
  const startResize = (e) => {
    if (e.button !== 0) return
    e.preventDefault()
    let latest = widthRef.current
    let x0 = e.clientX
    const w0 = latest
    document.body.classList.add('col-resizing')
    const move = (ev) => {
      // 上限还要看窗口：别把画布挤得没地方
      const max = Math.max(RES_W.min, Math.min(RES_W.max, window.innerWidth - 240))
      latest = Math.max(RES_W.min, Math.min(max, Math.round(w0 + (ev.clientX - x0))))
      setWidth(latest)
    }
    const up = () => {
      document.body.classList.remove('col-resizing')
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      saveWidth(latest)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const resetWidth = () => {
    setWidth(RES_W.def)
    saveWidth(RES_W.def)
  }

  if (!open) return null

  return (
    <aside className="res-panel side-panel" style={{ width }}>
      <div className="side-tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`side-tab${tab === t.id ? ' on' : ''}`}
            aria-pressed={tab === t.id}
            title={t.title}
            onClick={() => pick(t.id)}
          >
            {t.label}
          </button>
        ))}
        <span className="spacer" />
        <button className="res-btn" title="收起侧栏" aria-label="收起侧栏" onClick={onToggle}>
          <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <path d="M10 3L5 8l5 5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>

      <div className="side-body">
        {tab === 'res' ? (
          <ResourcePanel state={state} applyState={applyState} flash={flash} />
        ) : (
          <ColorPanel state={state} applyState={applyState} flash={flash} />
        )}
      </div>

      {/* 右边缘：拖动改宽度（不可见，只靠光标提示），双击恢复默认 */}
      <div
        className="res-resizer"
        title="拖动调整面板宽度（双击恢复默认）"
        onMouseDown={startResize}
        onDoubleClick={resetWidth}
      />
    </aside>
  )
}
