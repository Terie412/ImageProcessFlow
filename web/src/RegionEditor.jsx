import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { imageUrl } from './api'
import { anchorZoom, clampZoom, enableMiddleDragPan, wheelZoom } from './zoom'

// 区域坐标是**原图像素**，原点左上。太小的框没有意义，直接丢掉
const MIN_SIDE = 4

const clampRect = (r, W, H) => {
  const x = Math.max(0, Math.min(W - 1, Math.round(r.x)))
  const y = Math.max(0, Math.min(H - 1, Math.round(r.y)))
  const w = Math.max(1, Math.min(W - x, Math.round(r.w)))
  const h = Math.max(1, Math.min(H - y, Math.round(r.h)))
  return { x, y, w, h }
}

/**
 * 裁剪区域编辑器：在图上拖拽增删改区域，右边一列数字可以直接精修。
 * 坐标全程用「原图像素」，显示时按渲染比例换算。
 */
export default function RegionEditor({ image, regions, onSave, onClose }) {
  const imgW = image?.width || 0
  const imgH = image?.height || 0

  const [list, setList] = useState(() => (regions || []).map((r) => ({ ...r })))
  const [sel, setSel] = useState(-1)
  const [shot, setShot] = useState(null) // { w, h, k }：图片渲染尺寸 + 每像素对应几个显示像素
  const [hover, setHover] = useState(null) // 正在拖的把手，用于高亮
  const [zoom, setZoom] = useState(1) // 滚轮缩放倍率，1 = 适应窗口

  const stageRef = useRef(null)
  const canvasRef = useRef(null)
  const listRef = useRef(list)
  const dragRef = useRef(null)
  const zoomAnchorRef = useRef(null)
  useEffect(() => {
    listRef.current = list
  }, [list])

  // 显示尺寸 = 适应窗口的尺寸 × 缩放倍率；dk = 显示像素 / 图像像素
  const dispW = shot ? Math.max(1, Math.round(shot.w * zoom)) : 0
  const dispH = shot ? Math.max(1, Math.round(shot.h * zoom)) : 0
  const dk = shot && imgW ? dispW / imgW : 1

  /* ---------------- 量出图片的渲染尺寸 ---------------- */
  useEffect(() => {
    const el = stageRef.current
    if (!el || !imgW || !imgH) return undefined
    const measure = () => {
      // 用 clientWidth/Height（不含滚动条），否则缩放出滚动条会自己和自己较劲
      const k = Math.max(
        0.02,
        Math.min((el.clientWidth - 28) / imgW, (el.clientHeight - 28) / imgH),
      )
      setShot({ w: Math.floor(imgW * k), h: Math.floor(imgH * k), k })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [imgW, imgH])

  /* ---------------- 滚轮缩放（锚在光标下那一点）+ 中键平移 ---------------- */
  useEffect(() => {
    const el = stageRef.current
    if (!el || !shot) return undefined
    const onWheel = (ev) => {
      ev.preventDefault()
      const next = clampZoom(zoom * wheelZoom(ev.deltaY))
      if (next === zoom) return
      zoomAnchorRef.current = anchorZoom(el, canvasRef.current, ev.clientX, ev.clientY)
      setZoom(next)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [shot, zoom])

  useEffect(() => enableMiddleDragPan(stageRef.current), [])

  useLayoutEffect(() => {
    const f = zoomAnchorRef.current
    zoomAnchorRef.current = null
    if (f) f()
  }, [zoom])

  /* ---------------- 拖拽 ---------------- */
  const toImg = useCallback(
    (clientX, clientY) => {
      const box = canvasRef.current?.getBoundingClientRect()
      if (!box) return { x: 0, y: 0 }
      return { x: (clientX - box.left) / dk, y: (clientY - box.top) / dk }
    },
    [dk],
  )

  const onMove = useCallback(
    (ev) => {
      const d = dragRef.current
      if (!d || !shot) return
      const p = toImg(ev.clientX, ev.clientY)
      const next = listRef.current.slice()
      const cur = next[d.index]
      if (!cur) return
      const o = d.orig
      let x = cur.x
      let y = cur.y
      let w = cur.w
      let h = cur.h
      if (d.mode === 'create') {
        x = Math.min(d.x0, p.x)
        y = Math.min(d.y0, p.y)
        w = Math.abs(p.x - d.x0)
        h = Math.abs(p.y - d.y0)
      } else if (d.mode === 'move') {
        x = o.x + (p.x - d.x0)
        y = o.y + (p.y - d.y0)
      } else {
        let x0 = o.x
        let y0 = o.y
        let x1 = o.x + o.w
        let y1 = o.y + o.h
        if (d.handle[0] === 'n') y0 = p.y
        if (d.handle[0] === 's') y1 = p.y
        if (d.handle[1] === 'w') x0 = p.x
        if (d.handle[1] === 'e') x1 = p.x
        x = Math.min(x0, x1)
        y = Math.min(y0, y1)
        w = Math.abs(x1 - x0)
        h = Math.abs(y1 - y0)
      }
      next[d.index] = {
        ...next[d.index],
        x: Math.round(x),
        y: Math.round(y),
        w: Math.round(w),
        h: Math.round(h),
      }
      setList(next)
    },
    [shot, toImg],
  )

  const onUp = useCallback(() => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    const d = dragRef.current
    dragRef.current = null
    setHover(null)
    if (!d) return
    let next = listRef.current.map((r) => ({ ...r, ...clampRect(r, imgW, imgH) }))
    if (d.mode === 'create') {
      const made = next[d.index]
      if (!made || made.w < MIN_SIDE || made.h < MIN_SIDE) {
        next = next.filter((_, i) => i !== d.index) // 手抖点一下，不留碎框
        setSel(-1)
      } else {
        setSel(d.index)
      }
    }
    setList(next)
  }, [onMove, imgW, imgH])

  const startDrag = useCallback(
    (e, mode, index, handle = '') => {
      if (e.button !== 0 || !shot) return
      e.preventDefault()
      e.stopPropagation()
      const p = toImg(e.clientX, e.clientY)
      let idx = index
      if (mode === 'create') {
        idx = listRef.current.length
        setList([...listRef.current, { x: Math.round(p.x), y: Math.round(p.y), w: 0, h: 0 }])
        setSel(idx)
      } else {
        setSel(index)
      }
      dragRef.current = {
        mode,
        index: idx,
        handle,
        x0: p.x,
        y0: p.y,
        orig: listRef.current[index] ? { ...listRef.current[index] } : null,
      }
      setHover(handle || mode)
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [shot, toImg, onMove, onUp],
  )

  useEffect(() => () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }, [onMove, onUp])

  /* ---------------- 键盘：Delete 删掉选中的区域 ---------------- */
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') return // 交给外层关窗口
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      const t = e.target
      const tag = t && t.tagName ? t.tagName.toLowerCase() : ''
      if (tag === 'input' || tag === 'textarea' || (t && t.isContentEditable)) return
      if (sel < 0) return
      e.preventDefault()
      const next = listRef.current.filter((_, i) => i !== sel)
      setList(next)
      setSel(next.length ? Math.min(sel, next.length - 1) : -1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [sel])

  const patch = (i, k, v) => {
    const n = Number(v)
    if (!Number.isFinite(n)) return
    setList((l) => l.map((r, j) => (j === i ? { ...r, [k]: Math.round(n) } : r)))
  }

  /* 显示/隐藏某个区域：只影响这一张图的预览，不写进节点参数 */
  const toggleHide = (i) => {
    setList((l) => l.map((r, j) => (j === i ? { ...r, hidden: !r.hidden } : r)))
  }

  const style = (r) => ({
    left: `${(r.x / Math.max(1, imgW)) * 100}%`,
    top: `${(r.y / Math.max(1, imgH)) * 100}%`,
    width: `${(r.w / Math.max(1, imgW)) * 100}%`,
    height: `${(r.h / Math.max(1, imgH)) * 100}%`,
  })

  return (
    <div className="editor-mask" onMouseDown={onClose}>
      <div className="editor" onMouseDown={(e) => e.stopPropagation()}>
        <div className="editor-head">
          <span className="badge">{image?.name}</span>
          <b>裁剪区域</b>
          <span className="editor-meta">
            {imgW} × {imgH}
          </span>
          {zoom !== 1 ? (
            <button
              className="tiny"
              title="点一下回到适应窗口"
              onClick={() => setZoom(1)}
            >
              {Math.round(zoom * 100)}%
            </button>
          ) : null}
          <span className="spacer" />
          <span className="editor-hint">
            空白处拖出新框 · 框内拖动 · 拖四角改大小 · 滚轮缩放 · 中键平移 · Delete 删除
          </span>
        </div>

        <div className="editor-body">
          <div className="editor-stage" ref={stageRef}>
            <div
              className="editor-canvas"
              ref={canvasRef}
              style={shot ? { width: dispW, height: dispH } : { width: 0, height: 0 }}
              onMouseDown={(e) => startDrag(e, 'create', -1)}
            >
              <img src={imageUrl(image?.name, image?.mtime)} alt={image?.name} draggable={false} />
              {shot
                ? list.map((r, i) =>
                    r.hidden ? null : (
                      <div
                        key={i}
                        className={`ebox${i === sel ? ' sel' : ''}${
                          r.y * dk < 15 ? ' tight' : ''
                        }`}
                        style={style(r)}
                        onMouseDown={(e) => startDrag(e, 'move', i)}
                      >
                        <span className="ebox-tag">{i + 1}</span>
                        {['nw', 'ne', 'sw', 'se'].map((h) => (
                          <i
                            key={h}
                            className={`ebox-h ${h}${hover === h ? ' hot' : ''}`}
                            onMouseDown={(e) => startDrag(e, 'resize', i, h)}
                          />
                        ))}
                      </div>
                    ),
                  )
                : null}
            </div>
          </div>

          <div className="editor-side">
            <div className="editor-side-head">
              <span>{list.length} 个区域（像素坐标）</span>
            </div>
            <div className="editor-list">
              {list.length === 0 ? (
                <div className="editor-empty">还没有区域 —— 在图上拖一个框试试</div>
              ) : (
                list.map((r, i) => (
                  <div
                    key={i}
                    className={`erow${i === sel ? ' sel' : ''}${r.hidden ? ' off' : ''}`}
                    onMouseDown={() => setSel(i)}
                  >
                    <button
                      type="button"
                      className={`erow-eye${r.hidden ? '' : ' on'}`}
                      title={r.hidden ? '在图上显示这个区域' : '在图上暂时隐藏这个区域'}
                      onMouseDown={(e) => e.stopPropagation()}
                      onClick={(e) => {
                        e.stopPropagation()
                        toggleHide(i)
                      }}
                    />
                    <span className="erow-n">{i + 1}</span>
                    {['x', 'y', 'w', 'h'].map((k) => (
                      <label key={k} className="erow-f">
                        <span>{k.toUpperCase()}</span>
                        <input
                          type="number"
                          value={r[k]}
                          onChange={(e) => patch(i, k, e.target.value)}
                        />
                      </label>
                    ))}
                    <button
                      className="erow-del"
                      title="删除这个区域"
                      onClick={() => {
                        setList((l) => l.filter((_, j) => j !== i))
                        setSel(-1)
                      }}
                    >
                      ×
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>

        <div className="editor-foot">
          <button
            onClick={() => {
              setList([])
              setSel(-1)
            }}
            disabled={!list.length}
          >
            清空
          </button>
          <span className="spacer" />
          <button onClick={onClose}>取消</button>
          <button
            className="primary"
            onClick={() => onSave(list.map((r) => clampRect(r, imgW, imgH)))}
          >
            保存
          </button>
        </div>
      </div>
    </div>
  )
}
