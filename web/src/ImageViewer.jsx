import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { anchorZoom, enableMiddleDragPan, wheelZoom } from './zoom'

// 像素读数用：0~255 → 两位十六进制
const hex2 = (v) => v.toString(16).padStart(2, '0')

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

  const stageRef = useRef(null)
  const imgRef = useRef(null)
  const anchorRef = useRef(null)
  const canvasRef = useRef(null)

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

  // 换图就重来：丢掉上一张的像素表与缩放态
  useEffect(() => {
    canvasRef.current = null
    setFit(true)
    setSize(null)
    setPixel(null)
    setNatural(null)
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
        <img
          ref={imgRef}
          src={src}
          alt={badge}
          draggable={false}
          title="滚轮缩放 · 中键拖动平移 · 双击切换「适应窗口 / 实际大小」"
          onLoad={capture}
          onMouseMove={onMove}
          onMouseLeave={() => setPixel(null)}
          onDoubleClick={toggleFit}
          style={
            size
              ? { width: size.w, height: size.h, maxWidth: 'none', maxHeight: 'none' }
              : undefined
          }
        />
      </div>
    </div>
  )
}
