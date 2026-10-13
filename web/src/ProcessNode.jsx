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

/* 分辨率选项的显示：`720x1280` → `720×1280 (9:16)`。
 *
 * 比例是**从宽高算出来的**，不写进 manifest —— 派生值不存第二份，将来加一个
 * `2048x2048` 也不会漏写比例。只认长得像「宽x高」的选项（别的 enum 原样不动），
 * 而且只改**显示**：`value` 一个字符不动（发 API 走 value，autosize 也只按 value 解析）。
 * label 里已经有括号的（有人手写过比例）就不再追加。
 */
const SIZE_RE = /^(\d+)\s*[xX×*]\s*(\d+)$/
const gcd = (a, b) => (b ? gcd(b, a % b) : a)
const withRatio = (o) => {
  const m = SIZE_RE.exec(String(o.value || '').trim())
  if (!m || String(o.label || '').includes('(')) return o
  const w = Number(m[1])
  const h = Number(m[2])
  if (!w || !h) return o
  const g = gcd(w, h)
  return { ...o, label: `${o.label} (${w / g}:${h / g})` }
}

const opts = (spec) =>
  (spec.options || [])
    .map((o) => (typeof o === 'string' ? { value: o, label: o } : o))
    .map(withRatio)

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
    // 「尺寸 = 自动」会落到哪个预设 —— 服务端按最后一张参考图算好下发
    // （server/autosize.py）。必须在**生成之前**就能看到，否则要等生成完
    // 才知道模型挑了一个完全不同的分辨率。
    autoSize,
    onParam,
    onExecute,
    onPorts,
    onRemovePort,
    onPolish,
    onDetect,
    onEditRegions,
    // 拖手柄时的下限：一律由 App.buildNodes 从服务端的 sizeSpec 带下来，
    // 这里**不留兜底数字** —— 曾经有一份 320×280 从这里和服务端漂移过
    minW,
    minH,
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
    if (primary && inputRes) {
      // 标题用参数自己的 label（裁剪节点就是「裁剪区域」，区域抹除是「抹除区域」），
      // 编辑器本身是通用的，不写死某个节点的名字
      onEditRegions(id, {
        image: { ...inputRes, canvasId },
        regions,
        title: primary.label || '区域',
      })
    }
  }

  if (!manifest) return <div className="node">未注册的脚本</div>

  const running = status === 'running'
  const effectiveStatus = status === 'idle' && dirty ? 'dirty' : status
  // 所有输入口都没有可见标签（例如裁剪节点那个无名单口）→ 左栏收窄成一条
  const barePorts =
    portSpecs.length > 0 && portSpecs.every((p) => !p.num && !(p.label || '').trim())
  // 「自动识别」只有配了 detect 段的节点才有；没配的（如区域抹除）不显示按钮，
  // 免得点了才弹「这个节点没有配置自动识别」
  const hasDetect = !!manifest.detect

  /* 保持比例（调整分辨率）：开关打开时只有第一个连了图的输入口生效，
     其余已连接的口整行置灰（连线保留、执行时被脚本忽略）。
     「第一个」= 序号最小的连了图的口 —— image1 没连时生效的是 image2。 */
  const lockFirst = manifest?.id === 'resize' && !!params.keepRatio
  const firstLinkedKey = lockFirst
    ? portSpecs
        .filter((p) => inputs[p.key])
        .sort((a, b) => (a.num ?? 0) - (b.num ?? 0))[0]?.key
    : null

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
                  className={`port-row ${linked ? 'linked' : ''}${
                    lockFirst && linked && p.key !== firstLinkedKey ? ' off' : ''
                  }`}
                  key={p.key}
                  title={`${p.label || '输入图'}${
                    linked ? ` · 已连图片 ${link.name}` : ' · 未连线'
                  }${lockFirst && linked && p.key !== firstLinkedKey ? ' · 保持比例打开，本输入不生效' : ''}`}
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
                {hasDetect ? (
                  <button
                    className="mini-btn nodrag"
                    title="让模型看图挑切分参数，自动算出区域"
                    disabled={detecting || !inputRes}
                    onClick={doDetect}
                  >
                    {detecting ? '识别中…' : '自动识别'}
                  </button>
                ) : null}
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
              .map((s) => {
                const explicit = String(params[s.key] ?? '').trim()
                // 服务端会下发「自动会落到哪个预设」（见 server/autosize.py），
                // 但**显不显示由本地说了算** —— 参数改动走防抖 PATCH、不回灌 state，
                // 等 /api/state 的话「选了显式尺寸提示却不消失」会卡好几秒，
                // 反过来切回「自动」时提示也回不来。判断只依赖本地的 params。
                const auto = autoSize && autoSize.key === s.key && !explicit ? autoSize : null
                return (
                  <label
                    className={`cfg-field nodrag nopan ${widthCls(s)}${
                      auto ? ' col' : ''
                    }`.trim()}
                    key={s.key}
                  >
                    <span className="cfg-row">
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
                    </span>
                    {auto ? (
                      <span
                        className={`cfg-auto ${auto.source}`}
                        title={
                          auto.source === 'input'
                            ? `按最后一张参考图（${auto.from || '参考图'}${
                                auto.image ? ` · ${auto.image}` : ''
                              } ${auto.refWidth}×${auto.refHeight}）就近选中的预设`
                            : '没有参考图，尺寸交回模型自己决定'
                        }
                      >
                        {auto.source === 'input' ? `自动 → ${auto.label}` : '自动 → 模型自选'}
                      </span>
                    ) : null}
                  </label>
                )
              })}

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
                    // step 来自 manifest：小数参数（如 Alpha 的 0.05）必须声明，
                    // 否则浏览器按默认 step=1 走，上下箭头会一步跨过整个区间
                    step={s.step}
                    value={params[s.key] ?? ''}
                    onChange={(e) => onParam(id, s.key, e.target.value === '' ? '' : Number(e.target.value))}
                  />
                </label>
              ))}

            {others
              .filter((s) => s.type === 'slider')
              .map((s) => {
                // 滑块（色彩平衡的两条色偏轴）：拖动全程只走本地 setNodes，
                // 落盘由 App.pushParam 那层 500ms 防抖兜着，这里不用再防抖。
                const lo = Number(s.min ?? 0)
                const hi = Number(s.max ?? 100)
                const raw = Number(params[s.key])
                const val = Number.isFinite(raw)
                  ? Math.min(hi, Math.max(lo, raw))
                  : Number(s.default ?? lo)
                return (
                  <label className={`cfg-field nodrag nopan ${widthCls(s)}`.trim()} key={s.key}>
                    <span className="cfg-label">{s.label || s.key}</span>
                    <input
                      className="nodrag nopan cfg-slider"
                      type="range"
                      min={lo}
                      max={hi}
                      step={s.step ?? 1}
                      value={val}
                      onChange={(e) => onParam(id, s.key, Number(e.target.value))}
                    />
                    {/* 读数定宽 + 等宽数字：拖动时整行宽度不会随位数变化而抖 */}
                    <span className="cfg-num">{val > 0 ? `+${val}` : `${val}`}</span>
                  </label>
                )
              })}

            {others
              .filter((s) => s.type === 'color')
              .map((s) => {
                // 原生取色控件只认 #RRGGBB；历史值不合法时兜成黑，别让 React 报警
                const val = /^#[0-9a-fA-F]{6}$/.test(params[s.key] || '') ? params[s.key] : '#000000'
                return (
                  <label className={`cfg-field nodrag nopan ${widthCls(s)}`.trim()} key={s.key}>
                    <span className="cfg-label">{s.label || s.key}</span>
                    <input
                      className="nodrag nopan cfg-color"
                      type="color"
                      value={val}
                      onChange={(e) => onParam(id, s.key, e.target.value)}
                    />
                  </label>
                )
              })}

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
