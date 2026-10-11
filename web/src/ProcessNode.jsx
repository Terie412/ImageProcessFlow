import React, { useEffect, useRef, useState } from 'react'
import { Handle, Position } from '@xyflow/react'
import { imageUrl } from './api'

const STATUS_TEXT = {
  idle: '待执行',
  dirty: '已过期',
  running: '执行中',
  done: '已完成',
  error: '出错',
}

// 「裁剪区域」预览里，把图像像素坐标映射成百分比（图层尺寸 = 图片渲染尺寸）
const pct = (v, total) => `${(v / Math.max(1, total)) * 100}%`

function regionStyle(r, imgW, imgH) {
  return {
    left: pct(r.x, imgW),
    top: pct(r.y, imgH),
    width: pct(r.w, imgW),
    height: pct(r.h, imgH),
  }
}

const opts = (spec) =>
  (spec.options || []).map((o) => (typeof o === 'string' ? { value: o, label: o } : o))

// manifest 里每个参数可以声明 width，决定它在配置区里占多宽（不够就自动折行）
const WCLASS = { grow: 'grow', narrow: 'narrow', full: 'full' }
const widthCls = (spec) => WCLASS[spec.width] || ''

export default function ProcessNode({ id, data, selected }) {
  const {
    manifest,
    params = {},
    portSpecs = [],
    portGroups = [],
    status = 'idle',
    dirty,
    error,
    inputs = {},
    canvasId,
    lastDuration,
    onParam,
    onExecute,
    onPorts,
    onRemovePort,
    onPolish,
    onDetect,
    onEditRegions,
    minW = 320,
    minH = 280,
    onResizeStart,
  } = data

  const specs = manifest?.params || []
  const primary = specs.find((s) => s.primary) || specs.find((s) => s.type === 'text')
  const others = specs.filter((s) => s !== primary)
  const primaryText = primary && primary.type === 'text' ? params[primary.key] ?? '' : ''

  const regions = primary?.type === 'regions' && Array.isArray(params[primary.key])
    ? params[primary.key]
    : []
  // 区域预览要画在「上游那张图」上：默认取第一个输入口（regions 参数也能用 source 指定）
  const srcPort = primary?.type === 'regions' ? primary.source || portSpecs[0]?.key : null
  const inputRes = srcPort ? inputs[srcPort] || null : null

  /* 提示词框是「非受控」的 —— 这一点很重要：
     中文输入法在组合（拼拼音）期间会持续触发 input 事件，如果 textarea 是受控的，
     React 会把 state 回写进 DOM，浏览器就认为内容被外部改动、直接掐断当前组合，
     表现为「打到 sh 就被定格成字母、拼音断掉」。所以这里让 DOM 自己管输入，
     只在**没有聚焦**时才把外部值同步进去（例如重开工作目录）。 */
  const taRef = useRef(null)
  const initialText = useRef(primaryText) // 只在首次挂载时取一次
  useEffect(() => {
    const el = taRef.current
    if (!el || document.activeElement === el) return
    if (el.value !== primaryText) el.value = primaryText
  }, [primaryText])

  const [polishing, setPolishing] = useState(false)
  const [undoPrompt, setUndoPrompt] = useState(null) // 润色前的内容，供撤销
  const [detecting, setDetecting] = useState(false)
  const [undoRegions, setUndoRegions] = useState(null) // 自动识别前的区域，供撤销

  /* 区域预览：图层要和图片的**渲染尺寸**完全重合，不然框会错位。
     容器是弹性尺寸、图片是 contain 缩放，所以拿容器实测 + 原图宽高算出渲染尺寸。 */
  const shotRef = useRef(null)
  const [shot, setShot] = useState(null)
  const imgW = inputRes?.width || 0
  const imgH = inputRes?.height || 0
  useEffect(() => {
    const el = shotRef.current
    if (!el || !imgW || !imgH || primary?.type !== 'regions') {
      setShot(null)
      return undefined
    }
    const measure = () => {
      const r = el.getBoundingClientRect()
      if (r.width < 2 || r.height < 2) return
      const k = Math.min(r.width / imgW, r.height / imgH)
      setShot({ w: Math.max(1, Math.floor(imgW * k)), h: Math.max(1, Math.floor(imgH * k)) })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [imgW, imgH, primary?.type, inputRes?.mtime])

  // 每 1 个图像像素对应几个显示像素 —— 角标贴到图顶边时得缩回框内，否则会被裁掉
  const regionK = shot && imgW ? shot.w / Math.max(1, imgW) : 1

  // 非受控 textarea：写值时必须手动动 DOM，光改 state 它不会跟着变
  const applyPrompt = (v) => {
    if (taRef.current) taRef.current.value = v
    if (primary) onParam(id, primary.key, v)
  }

  const doPolish = async () => {
    if (!primary || polishing) return
    const before = taRef.current ? taRef.current.value : primaryText
    setPolishing(true)
    try {
      const next = await onPolish(id)
      if (next) {
        setUndoPrompt(before)
        applyPrompt(next)
      }
    } finally {
      setPolishing(false)
    }
  }

  const undoPolish = () => {
    if (undoPrompt === null) return
    applyPrompt(undoPrompt)
    setUndoPrompt(null)
  }

  /* ---- 裁剪区域：自动识别 / 编辑 ---- */
  const applyRegions = (v) => {
    if (primary) onParam(id, primary.key, v)
  }

  const doDetect = async () => {
    if (!primary || detecting) return
    const before = regions
    setDetecting(true)
    try {
      const res = await onDetect(id)
      if (res && Array.isArray(res.regions)) {
        setUndoRegions(before)
        applyRegions(res.regions)
      }
    } finally {
      setDetecting(false)
    }
  }

  const undoDetect = () => {
    if (undoRegions === null) return
    applyRegions(undoRegions)
    setUndoRegions(null)
  }

  const openEditor = () => {
    if (primary && inputRes) onEditRegions(id, { image: { ...inputRes, canvasId }, regions })
  }

  if (!manifest) return <div className="node">未注册的脚本</div>

  const running = status === 'running'
  const effectiveStatus = status === 'idle' && dirty ? 'dirty' : status
  // 所有输入口都没有可见标签（例如裁剪节点那个无名单口）→ 左栏收窄成一条
  const barePorts =
    portSpecs.length > 0 && portSpecs.every((p) => !p.num && !(p.label || '').trim())

  return (
    <div className={`node ${selected ? 'selected' : ''}`}>
      <div className="node-head">
        <span className={`dot ${effectiveStatus}`} />
        <span className="title">{manifest.name}</span>
        <span className="spacer" />
        <span className="status">{STATUS_TEXT[effectiveStatus] || effectiveStatus}</span>
        {!running && lastDuration ? <span className="dur">{lastDuration}s</span> : null}
      </div>

      {/* 左栏 = 输入口（独占一列，撑满整个节点高度）；右栏 = 提示词 + 参数 + 执行 */}
      <div className="node-main">
        {portSpecs.length > 0 || portGroups.length > 0 ? (
          <div className={`ports${barePorts ? ' compact' : ''}`}>
            {portSpecs.map((p) => {
              const link = inputs[p.key]
              const linked = !!link
              // 动态端口才有「组」；固定端口不给删除按钮
              const grp = p.group ? portGroups.find((x) => x.key === p.group) : null
              // 端口上只显示序号（省地方）；显式写了空 label 的口就什么都不显示
              const label = p.num ?? (p.label || '')
              return (
                <div
                  className={`port-row ${linked ? 'linked' : ''}`}
                  key={p.key}
                  title={`${p.label || '输入图'}${linked ? ` · 已连图片 ${link.name}` : ' · 未连线'}`}
                >
                  <Handle type="target" position={Position.Left} id={p.key} />
                  {/* 一栏里只放「接口 + 序号 + 减号」，图片名不显示（鼠标悬停能看到） */}
                  {label ? <span className="port-label">{label}</span> : null}
                  {p.group ? (
                    <button
                      className="pin-btn row-del nodrag"
                      title="删除这个输入"
                      disabled={grp ? grp.count <= grp.min : false}
                      onClick={() => onRemovePort(id, p.key)}
                    >
                      −
                    </button>
                  ) : null}
                </div>
              )
            })}
            {portGroups.length > 0 ? (
              <div className="port-ctl">
                {portGroups.map((g) => (
                  <button
                    key={g.key}
                    className="pin-btn nodrag"
                    title="新增一个输入"
                    disabled={g.count >= g.max}
                    onClick={() => onPorts(id, g.key, g.count + 1)}
                  >
                    ＋
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        <div className="node-right">
          {primary?.type === 'regions' ? (
            <div className="regions">
              <div
                className="region-shot"
                ref={shotRef}
                onDoubleClick={openEditor}
                title={inputRes ? '双击编辑裁剪区域' : '先给左侧的输入口连一张图'}
              >
                {inputRes ? (
                  <div
                    className="region-canvas"
                    style={shot ? { width: shot.w, height: shot.h } : { width: 0, height: 0 }}
                  >
                    <img
                      src={imageUrl(canvasId, inputRes.name, inputRes.mtime)}
                      alt={inputRes.name}
                      draggable={false}
                    />
                    {shot ? (
                      <div className="region-layer">
                        {regions.map((r, i) => (
                          <span
                            className={`region-box${r.y * regionK < 13 ? ' tight' : ''}`}
                            key={i}
                            style={regionStyle(r, imgW, imgH)}
                          >
                            <i>{i + 1}</i>
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : (
                  <div className="ph">未连接图片</div>
                )}
              </div>

              <div className="region-bar">
                <span className="region-count">{regions.length} 个区域</span>
                <span className="spacer" />
                {undoRegions !== null && !detecting ? (
                  <button className="mini-btn nodrag" title="恢复识别前的区域" onClick={undoDetect}>
                    撤销
                  </button>
                ) : null}
                <button
                  className="mini-btn nodrag"
                  title="让模型看图挑切分参数，自动算出区域"
                  disabled={detecting || !inputRes}
                  onClick={doDetect}
                >
                  {detecting ? '识别中…' : '自动识别'}
                </button>
                <button
                  className="mini-btn nodrag"
                  title="在图上手动增删改区域"
                  disabled={!inputRes}
                  onClick={openEditor}
                >
                  编辑
                </button>
              </div>
            </div>
          ) : primary ? (
            <div className="prompt-wrap">
              <textarea
                ref={taRef}
                className="prompt-box nodrag nopan"
                defaultValue={initialText.current}
                placeholder={primary.placeholder || primary.label || '提示词'}
                onChange={(e) => onParam(id, primary.key, e.target.value)}
              />
              <div className="prompt-actions">
                {undoPrompt !== null && !polishing ? (
                  <button className="mini-btn nodrag" title="恢复润色前的内容" onClick={undoPolish}>
                    撤销
                  </button>
                ) : null}
                <button
                  className="mini-btn nodrag"
                  title="让文本模型结合参考图改写这条提示词"
                  disabled={polishing}
                  onClick={doPolish}
                >
                  {polishing ? '润色中…' : '润色'}
                </button>
              </div>
            </div>
          ) : null}

          {others.length ? (
            <div className="cfg">
            {others
              .filter((s) => s.type === 'enum')
              .map((s) => (
                <label className={`cfg-field nodrag nopan ${widthCls(s)}`.trim()} key={s.key}>
                  <span className="cfg-label">{s.label || s.key}</span>
                  <select
                    className="nodrag nopan"
                    value={params[s.key] ?? ''}
                    onChange={(e) => onParam(id, s.key, e.target.value)}
                  >
                    {opts(s).map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </label>
              ))}

            {others
              .filter((s) => s.type === 'number')
              .map((s) => (
                <label className={`cfg-field nodrag nopan ${widthCls(s)}`.trim()} key={s.key}>
                  <span className="cfg-label">{s.label || s.key}</span>
                  <input
                    className="nodrag nopan"
                    type="number"
                    min={s.min}
                    max={s.max}
                    value={params[s.key] ?? ''}
                    onChange={(e) => onParam(id, s.key, e.target.value === '' ? '' : Number(e.target.value))}
                  />
                </label>
              ))}

            {others.some((s) => s.type === 'bool') ? (
              <div className="cfg-checks">
                {others
                  .filter((s) => s.type === 'bool')
                  .map((s) => (
                    <label className="cfg-check nodrag nopan" key={s.key}>
                      <input
                        className="nodrag nopan"
                        type="checkbox"
                        checked={!!params[s.key]}
                        onChange={(e) => onParam(id, s.key, e.target.checked)}
                      />
                      <span>{s.label || s.key}</span>
                    </label>
                  ))}
              </div>
            ) : null}
            </div>
          ) : null}

          {error ? <div className="err-box">{error}</div> : null}

          <div className="node-foot">
            <button className="primary nodrag" disabled={running} onClick={() => onExecute(id)}>
              {running ? '执行中…' : '▶ 执行'}
            </button>
          </div>
        </div>
      </div>

      <Handle type="source" position={Position.Right} id="out" isConnectable={false} />

      <div
        className="resize-handle nodrag"
        title="拖动改变节点大小"
        onMouseDown={(e) => onResizeStart(e, id, minW, minH)}
      />
    </div>
  )
}
