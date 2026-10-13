import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { anchorZoom, enableMiddleDragPan, wheelZoom } from './zoom'

// 像素读数用：0~255 → 两位十六进制
const hex2 = (v) => v.toString(16).padStart(2, '0')

// 丈量框：把拖出来的两个角整理成「左上角 + 宽高」（都是**图内像素**）
const norm = (m) => ({
  x: Math.min(m.x0, m.x1),
  y: Math.min(m.y0, m.y1),
  w: Math.abs(m.x1 - m.x0),
  h: Math.abs(m.y1 - m.y0),
})

// 屏幕坐标 → **图内像素**（夹在图片范围内）。
// 放在模块级是为了让 window 上的监听也能用，不牵扯组件的闭包。
const imgPoint = (img, ev) => {
  if (!img || !img.naturalWidth || !img.naturalHeight) return null
  const r = img.getBoundingClientRect()
  if (r.width < 1 || r.height < 1) return null
  const clamp = (v, hi) => Math.max(0, Math.min(hi, v))
  return {
    x: Math.round(clamp(((ev.clientX - r.left) / r.width) * img.naturalWidth, img.naturalWidth)),
    y: Math.round(clamp(((ev.clientY - r.top) / r.height) * img.naturalHeight, img.naturalHeight)),
  }
}

/*
 * 大图查看器：双击**图片数据节点**和**资源面板里的图片文件**走的是同一个组件。
 *
 * 抽出来共用是因为两边要求一模一样 —— 适应窗口 / 实际大小两种模式、滚轮以光标为锚点
 * 缩放、中键拖动平移、透明区垫棋盘格、顶栏实时显示光标处像素。复制一份必然会长歪。
 *
 * props：
 *   src        图片地址
 *   badge      左上角徽标（数据节点是短名 A/B；资源文件是文件名）
 *   badgeKind  'input' | 'output' | null —— 徽标配色，null 用中性色
 *   subtitle   徽标右边的小字（可选）
 *   width/height/format/alpha  服务端读文件头得到的信息，**都可以缺**：
 *              缺尺寸就用图片自身的 naturalWidth/Height 兜底；alpha 未知就只显示 RGB
 *   onClose
 */
export default function ImageViewer({
  src,
  badge,
  badgeKind = null,
  subtitle = null,
  width = null,
  height = null,
  format = null,
  alpha = null,
  onClose,
}) {
  const [fit, setFit] = useState(true)      // 适应窗口 / 实际像素
  const [size, setSize] = useState(null)   // 滚轮缩放后的显式尺寸；null = 交给 CSS
  const [pixel, setPixel] = useState(null) // 光标下那一像素 {x,y,r,g,b,a}；不在图上时 null
  const [natural, setNatural] = useState(null) // 服务端没给尺寸时的兜底（图片自身大小）
  // 丈量：`mark` 是**拖动期间**的框（图内像素），松手就清空；
  // `last` 是刚量完的那一次读数 —— 框按需求消失，但数字留着，不然得按住不放才读得到。
  const [mark, setMark] = useState(null)
  const [last, setLast] = useState(null)

  const stageRef = useRef(null)
  const imgRef = useRef(null)
  const anchorRef = useRef(null)
  const canvasRef = useRef(null)
  const markStartRef = useRef(null)   // 按下时的角（图内像素）
  const markLiveRef = useRef(null)    // 当前拖出来的框（图内像素）

  const w = width || natural?.w || null
  const h = height || natural?.h || null

  // onClose 每次渲染都是新函数，用 ref 兜住，免得 Esc 的监听反复重挂
  const closeRef = useRef(onClose)
  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') closeRef.current?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  /* 把图原样画进**离屏 canvas**（只画一次），供像素读数查表。
     用 naturalWidth/Height 建画布 = 图内像素坐标，与渲染缩放无关。 */
  const capture = useCallback(() => {
    const img = imgRef.current
    if (!img || !img.complete || !img.naturalWidth) return
    const c = document.createElement('canvas')
    c.width = img.naturalWidth
    c.height = img.naturalHeight
    const ctx = c.getContext('2d', { willReadFrequently: true })
    if (!ctx) return
    ctx.drawImage(img, 0, 0)
    canvasRef.current = c
    setNatural({ w: img.naturalWidth, h: img.naturalHeight })
  }, [])

  // 换图就重来：丢掉上一张的像素表、缩放态与丈量结果
  useEffect(() => {
    canvasRef.current = null
    markStartRef.current = null
    markLiveRef.current = null
    setFit(true)
    setSize(null)
    setPixel(null)
    setNatural(null)
    setMark(null)
    setLast(null)
    // 命中浏览器缓存时 load 可能早于绑定（甚至根本不派发），这里兜一次
    if (imgRef.current?.complete) capture()
  }, [src, capture])

  const toggleFit = () => {
    setSize(null)
    setFit((v) => !v)
  }

  /* 光标在图内 → 换算出图内像素坐标，读那一个像素（RGBA）。
     用 1×1 的 getImageData 而不是缓存整张：大图全缓存动辄几十 MB，
     逐点读既省内存又够快。 */
  const onMove = useCallback((ev) => {
    const c = canvasRef.current
    if (!c || !c.width || !c.height) return
    const r = ev.currentTarget.getBoundingClientRect()
    if (r.width < 1 || r.height < 1) return
    const x = Math.floor(((ev.clientX - r.left) / r.width) * c.width)
    const y = Math.floor(((ev.clientY - r.top) / r.height) * c.height)
    if (x < 0 || y < 0 || x >= c.width || y >= c.height) {
      setPixel(null)
      return
    }
    const ctx = c.getContext('2d')
    if (!ctx) return
    const d = ctx.getImageData(x, y, 1, 1).data
    setPixel({ x, y, r: d[0], g: d[1], b: d[2], a: d[3] })
  }, [])

  /* ---------------- 丈量：按住左键拖出一个半透明的框 ----------------
     拖出来的位置一律换算成**图内像素**（和上面那个像素读数同一套坐标），所以
     缩放 / 滚动都不影响读数；两个端点各自**吸附到整数像素**，尺寸天然是整数，
     这样「宽 240」就真的是 240 个像素。 */

  // 屏幕坐标 → 图内像素（夹在图片范围内）
  const onMarkDown = (ev) => {
    if (ev.button !== 0) return // 中键留给平移
    const p = imgPoint(imgRef.current, ev)
    if (!p) return
    ev.preventDefault() // 免得选中文字 / 触发原生图片拖拽
    // 起点也存成 x0/y0，和 onMarkMove 展开的那份保持同一个形状 ——
    // 之前存 {x,y} 再用 {...起点, x1, y1} 展开，norm() 里的 m.x0 就是 undefined，
    // 宽高会算成 NaN（框的位置看着还对，因为浏览器的非法样式值不覆盖旧值）
    markStartRef.current = { x0: p.x, y0: p.y }
    markLiveRef.current = { x0: p.x, y0: p.y, x1: p.x, y1: p.y }
    setMark(markLiveRef.current)
    setLast(null) // 开始新的一次丈量，上一次的读数让位
  }

  // 拖到图片**外面**也要继续跟 —— 靠 imgPoint 的夹取让框贴在边缘上
  const onMarkMove = useCallback((ev) => {
    if (!markStartRef.current) return
    const p = imgPoint(imgRef.current, ev)
    if (!p) return
    markLiveRef.current = { ...markStartRef.current, x1: p.x, y1: p.y }
    setMark(markLiveRef.current)
  }, [])

  // 松手：**框消失**，但这一次的读数留在顶栏（否则得按住不放才读得到数）
  const onMarkUp = useCallback(() => {
    if (!markStartRef.current) return
    markStartRef.current = null
    const m = markLiveRef.current
    markLiveRef.current = null
    setMark(null)
    // 只是点了一下（没拖出东西）就不留读数 —— 也顺手把上一次的量清掉
    if (m && (Math.abs(m.x1 - m.x0) >= 1 || Math.abs(m.y1 - m.y0) >= 1)) setLast(norm(m))
  }, [])

  // 挂在 window 上：鼠标移出图片、甚至在图片外松手，都能正确收尾
  useEffect(() => {
    window.addEventListener('mousemove', onMarkMove)
    window.addEventListener('mouseup', onMarkUp)
    return () => {
      window.removeEventListener('mousemove', onMarkMove)
      window.removeEventListener('mouseup', onMarkUp)
    }
  }, [onMarkMove, onMarkUp])

  // 正在量的那个框：`box` 用来画框（拖动中才有），`shown` 是顶栏要显示的那一组数
  const box = mark ? norm(mark) : null
  const shown = box || last

  // 滚轮缩放：以光标为锚点，缩放后把滚动位置挪回去
  useEffect(() => {
    const el = stageRef.current
    if (!el) return undefined
    const onWheel = (ev) => {
      const img = imgRef.current
      if (!img) return
      const r = img.getBoundingClientRect()
      if (r.width < 2) return
      ev.preventDefault()
      // 先把「光标指到的那一点」记下来，再改尺寸 —— 之后布局回来对账
      anchorRef.current = anchorZoom(el, img, ev.clientX, ev.clientY)
      const nw = Math.min(20000, Math.max(16, r.width * wheelZoom(ev.deltaY)))
      setSize({ w: Math.round(nw), h: Math.max(1, Math.round((r.height * nw) / r.width)) })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  useEffect(() => enableMiddleDragPan(stageRef.current), [])

  useLayoutEffect(() => {
    const f = anchorRef.current
    anchorRef.current = null
    if (f) f()
  }, [size])

  return (
    <div className="viewer-mask">
      <div className="viewer-bar">
        <span className={badgeKind ? `badge ${badgeKind}` : 'badge'}>{badge}</span>
        {subtitle ? <span className="viewer-name" title={subtitle}>{subtitle}</span> : null}
        <span className="spacer" />
        {/* 丈量读数：按住左键在图上拖，这里实时给出**图内像素**的宽高与左上角坐标。
            框松手就消失，这次读数会留到下一次拖动（见 onMarkUp） */}
        <span className="viewer-measure" title="按住左键在图上拖动即可丈量元素的尺寸">
          {shown ? (
            <>
              <span className="viewer-size">
                {shown.w} × {shown.h}
              </span>
              <span className="viewer-origin">
                px @ ({shown.x}, {shown.y})
              </span>
            </>
          ) : (
            <span className="viewer-idle">测量 —</span>
          )}
        </span>
        {/* 光标下那一像素的通道值（图内像素坐标）。没有 alpha 通道的图只显示 RGB */}
        <span className="viewer-pixel" title="把鼠标移到图上，这里显示光标处像素的通道值">
          {pixel ? (
            <>
              <span className="viewer-swatch">
                <i
                  style={{
                    backgroundColor: `rgba(${pixel.r},${pixel.g},${pixel.b},${(pixel.a / 255).toFixed(3)})`,
                  }}
                />
              </span>
              <span className="viewer-xy">
                {pixel.x}, {pixel.y}
              </span>
              <span className="viewer-color">
                {`Color(${pixel.r}, ${pixel.g}, ${pixel.b}${alpha ? `, ${pixel.a}` : ''})`}
              </span>
              <span className="viewer-hex">
                {`#${hex2(pixel.r)}${hex2(pixel.g)}${hex2(pixel.b)}${alpha ? hex2(pixel.a) : ''}`}
              </span>
            </>
          ) : (
            <span className="viewer-idle">像素 —</span>
          )}
        </span>
        <span className="viewer-meta">
          {w && h ? `${w} × ${h}` : null}
          {format ? ` · ${format}` : null}
        </span>
        <button className="tiny" onClick={toggleFit}>
          {fit ? '实际大小' : '适应窗口'}
        </button>
        <button className="tiny" onClick={() => closeRef.current?.()}>
          关闭 (Esc)
        </button>
      </div>
      <div className={`viewer-stage ${fit ? 'fit' : 'actual'}`} ref={stageRef}>
        {/* 包一层只为了让丈量框能按**百分比**贴在图上 —— 缩放 / 滚动时框跟着图走，
            不用去算 stage 的滚动偏移。（原来 img 上的 margin 挪到了这一层） */}
        <div className="viewer-photo">
          <img
            ref={imgRef}
            src={src}
            alt={badge}
            draggable={false}
            title="按住左键拖动丈量尺寸 · 滚轮缩放 · 中键拖动平移 · 双击切换「适应窗口 / 实际大小」"
            onLoad={capture}
            onMouseMove={onMove}
            onMouseDown={onMarkDown}
            onMouseUp={onMarkUp}
            onMouseLeave={() => setPixel(null)}
            onDoubleClick={toggleFit}
            style={
              size
                ? { width: size.w, height: size.h, maxWidth: 'none', maxHeight: 'none' }
                : undefined
            }
          />
          {box && natural ? (
            <div
              className="viewer-mark"
              style={{
                left: `${(box.x / natural.w) * 100}%`,
                top: `${(box.y / natural.h) * 100}%`,
                width: `${(box.w / natural.w) * 100}%`,
                height: `${(box.h / natural.h) * 100}%`,
              }}
            />
          ) : null}
        </div>
      </div>
    </div>
  )
}
