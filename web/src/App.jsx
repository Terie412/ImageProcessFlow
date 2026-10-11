import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  useReactFlow,
} from '@xyflow/react'
import * as api from './api'
import ResourceNode from './ResourceNode'
import ProcessNode from './ProcessNode'
import RegionEditor from './RegionEditor'
import { anchorZoom, enableMiddleDragPan, wheelZoom } from './zoom'

const nodeTypes = { resource: ResourceNode, process: ProcessNode }

// 节点尺寸：graph 里没记 w/h 时用这里的默认值。
// 用户拖右下角改过之后，尺寸会写进 graph.json。
// 「最小尺寸」不在这里定死 —— 它由设置里的 nodeMin 决定（界面 ⚙ 里可调），
// 这两个常量只是设置缺失时的最后兜底。
const RESOURCE_SIZE = { w: 250, h: 280, minW: 200, minH: 180 }
const PROCESS_SIZE = { w: 600, h: 340, minW: 560, minH: 280 }

// 最小尺寸的允许区间（与服务端 NODE_MIN_RANGE 保持一致）
const MIN_RANGE = [80, 4000]
const clampMin = (v) => {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return null
  return Math.max(MIN_RANGE[0], Math.min(MIN_RANGE[1], Math.round(n)))
}

// 网格吸附：格子大小的允许区间与服务端 engine.GRID_SIZE_RANGE 保持一致
const GRID_RANGE = [1, 200]
const GRID_DEFAULT = 5
const clampGrid = (v) => {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return null
  return Math.max(GRID_RANGE[0], Math.min(GRID_RANGE[1], Math.round(n)))
}

/*
 * 最小尺寸按「具体节点类型」存，不是按大类：
 *   nodeMin.resource          → 数据节点
 *   nodeMin["process:<defId>"]→ 某个具体的处理节点类型（如 qwen_image）
 *   nodeMin.process           → 处理节点的兜底（新类型还没有自己的条目时用它）
 */
function minSizeOf(state, key, dflt) {
  const nm = state?.settings?.nodeMin || {}
  const spec = nm[key] || (key.startsWith('process:') ? nm.process : null) || {}
  return { w: clampMin(spec.w) ?? dflt.w, h: clampMin(spec.h) ?? dflt.h }
}

// 设置浮层里要列出的行：数据节点 + 每个已注册的处理节点类型
function minRows(state) {
  const mans = Object.values(state?.manifests || {}).sort((a, b) => a.id.localeCompare(b.id))
  return [
    { key: 'resource', label: '数据节点', dflt: { w: RESOURCE_SIZE.minW, h: RESOURCE_SIZE.minH } },
    ...mans.map((m) => ({
      key: `process:${m.id}`,
      label: m.name || m.id,
      dflt: { w: PROCESS_SIZE.minW, h: PROCESS_SIZE.minH },
    })),
  ]
}

const EDGE_STYLE = { stroke: '#4f9cf9' }
const EDGE_MARKER = { type: MarkerType.ArrowClosed, color: '#4f9cf9', width: 18, height: 18 }
const IMPLICIT_STYLE = { stroke: '#4a5563', strokeDasharray: '4 4' }

/* ------------------------------------------------------------------ */
/* 由服务端 graph 构造 React Flow 的 nodes / edges                      */
/* ------------------------------------------------------------------ */
function buildNodes(state, cbs) {
  if (!state?.graph) return []
  const resById = {}
  for (const r of state.graph.resources) resById[r.id] = r

  const portMap = {}
  for (const e of state.graph.edges) {
    if (!portMap[e.to]) portMap[e.to] = {}
    // 存整个数据节点对象：节点既要显示图片名，也要拿到宽高来画区域预览
    portMap[e.to][e.port] = resById[e.from] || null
  }

  const rmin = minSizeOf(state, 'resource', RESOURCE_SIZE)

  return [
    ...state.graph.resources.map((r) => ({
      id: r.id,
      type: 'resource',
      position: { x: r.x ?? 0, y: r.y ?? 0 },
      // 尺寸走 React Flow 的顶层 width/height。
      // 注意数据节点里 data.width/height 是「图片像素分辨率」，别搞混。
      width: Math.max(rmin.w, r.w ?? RESOURCE_SIZE.w),
      height: Math.max(rmin.h, r.h ?? RESOURCE_SIZE.h),
      data: { ...r, minW: rmin.w, minH: rmin.h, ...cbs },
    })),
    ...state.graph.processes.map((p) => {
      const sz = state.manifests?.[p.defId]?.size || {}
      const pmin = minSizeOf(state, `process:${p.defId}`, PROCESS_SIZE)
      return {
        id: p.id,
        type: 'process',
        position: { x: p.x ?? 0, y: p.y ?? 0 },
        // 存过的尺寸可能小于设置里的最小值，这里兜一下
        width: Math.max(pmin.w, p.w ?? sz.defaultWidth ?? PROCESS_SIZE.w),
        height: Math.max(pmin.h, p.h ?? sz.defaultHeight ?? PROCESS_SIZE.h),
        data: {
          ...p,
          manifest: state.manifests?.[p.defId],
          inputs: portMap[p.id] || {},
          minW: pmin.w,
          minH: pmin.h,
          ...cbs,
        },
      }
    }),
  ]
}

function buildEdges(state) {
  if (!state?.graph) return []
  const implicit = state.graph.resources
    .filter((r) => r.kind === 'output' && r.producer)
    .map((r) => ({
      id: `implicit_${r.id}`,
      source: r.producer,
      sourceHandle: 'out',
      target: r.id,
      targetHandle: 'in',
      style: IMPLICIT_STYLE,
      deletable: false,
      selectable: false,
      focusable: false,
    }))
  const real = state.graph.edges.map((e) => ({
    id: e.id,
    source: e.from,
    sourceHandle: 'out',
    target: e.to,
    targetHandle: e.port,
    style: EDGE_STYLE,
    markerEnd: EDGE_MARKER,
  }))
  return [...implicit, ...real]
}

/* ------------------------------------------------------------------ */
export default function App() {
  const [state, setState] = useState(null)
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [drawer, setDrawer] = useState(null)
  const [toast, setToast] = useState(null)
  const [showWs, setShowWs] = useState(false)
  const [wsInput, setWsInput] = useState('')
  const [wsErr, setWsErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [menu, setMenu] = useState(null) // 画布右键菜单 { x, y, flowX, flowY }
  const [showSettings, setShowSettings] = useState(false)
  const [selIds, setSelIds] = useState([]) // 当前选中的节点 id（用于左上角显示尺寸）
  const [viewer, setViewer] = useState(null) // 双击数据节点打开的大图查看器
  const [viewerFit, setViewerFit] = useState(true) // 查看器：适应窗口 / 实际像素
  const [viewerSize, setViewerSize] = useState(null) // 滚轮缩放后的显式尺寸；null = 交给 CSS
  const [editing, setEditing] = useState(null) // 裁剪区域编辑器 { pid, image, regions }

  const canvasRef = useRef(null)
  const importRef = useRef(null)
  const viewerStageRef = useRef(null)
  const viewerImgRef = useRef(null)
  const viewerAnchorRef = useRef(null)
  const replaceRef = useRef(null)
  const replaceTarget = useRef(null)
  const importAt = useRef(null)
  const cbsRef = useRef({})
  const timers = useRef({})
  const pending = useRef({})
  const fitted = useRef(false)
  const stateRef = useRef(null)
  const selectionRef = useRef({ nodes: [], edges: [] })

  const { screenToFlowPosition, fitView, getZoom } = useReactFlow()

  const flash = useCallback((msg) => {
    setToast(String(msg))
    setTimeout(() => setToast(null), 6000)
  }, [])

  const applyState = useCallback(
    (s) => {
      if (!s || s.cancelled) return
      setState(s)
      setNodes(buildNodes(s, cbsRef.current))
      setEdges(buildEdges(s))
      if (!fitted.current && (s.graph?.resources?.length || s.graph?.processes?.length)) {
        fitted.current = true
        setTimeout(() => fitView({ padding: 0.25, duration: 300 }), 60)
      }
    },
    [setNodes, setEdges, fitView],
  )

  /* ---------------- 删除（按键与按钮共用同一条路径）---------------- */
  const deleteSelection = useCallback(
    async (selNodes, selEdges) => {
      const g = stateRef.current?.graph
      if (!g) return

      const nodeIds = selNodes.map((n) => n.id)
      const edgeIds = selEdges.filter((e) => !e.id.startsWith('implicit_')).map((e) => e.id)
      if (!nodeIds.length && !edgeIds.length) return

      // 推算这次删除会真正毁掉什么，再决定提示内容
      const resById = Object.fromEntries(g.resources.map((r) => [r.id, r]))
      const procIds = new Set(nodeIds.filter((id) => !resById[id]))
      const goneRes = new Set(nodeIds.filter((id) => resById[id]))
      for (const r of g.resources) {
        if (r.producer && procIds.has(r.producer)) goneRes.add(r.id) // 处理节点的输出会被连带删除
      }
      const files = [...goneRes].map((id) => resById[id]?.name).filter(Boolean)
      const goneEdges = new Set(edgeIds)
      for (const e of g.edges) {
        if (goneRes.has(e.from) || goneRes.has(e.to) || procIds.has(e.to) || procIds.has(e.from)) {
          goneEdges.add(e.id)
        }
      }

      const parts = []
      if (nodeIds.length) parts.push(`${nodeIds.length} 个节点`)
      if (edgeIds.length) parts.push(`${edgeIds.length} 条连线`)
      const lines = [`删除 ${parts.join(' 和 ')}？`]
      if (files.length) lines.push(`· 永久删除 ${files.length} 张图片：${files.join('、')}`)
      if (goneEdges.size) lines.push(`· 断开 ${goneEdges.size} 条连线`)

      // 只有「直接删除数据节点」时才可能确认 —— 它对应磁盘上的图片文件，是唯一会丢东西的操作。
      // 删处理节点 / 连线一律静默。
      // 是否真的弹框由设置项决定（默认开），见顶栏「设置」。
      const touchesDataNode = nodeIds.some((id) => resById[id])
      const askConfirm = stateRef.current?.settings?.confirmDeleteDataNode !== false
      if (touchesDataNode && askConfirm && !window.confirm(lines.join('\n'))) return

      let s = stateRef.current
      try {
        for (const id of nodeIds) s = await api.deleteNode(id)
        for (const id of edgeIds) s = await api.deleteEdge(id)
        applyState(s)
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash],
  )

  /* ---------------- 回调 ---------------- */
  const onParam = useCallback(
    (pid, key, value) => {
      setNodes((ns) =>
        ns.map((n) =>
          n.id === pid
            ? { ...n, data: { ...n.data, params: { ...(n.data.params || {}), [key]: value } } }
            : n,
        ),
      )
      pending.current[pid] = { ...(pending.current[pid] || {}), [key]: value }
      clearTimeout(timers.current[pid])
      timers.current[pid] = setTimeout(() => {
        const payload = pending.current[pid]
        delete pending.current[pid]
        if (payload) api.updateProcess(pid, { params: payload }).catch((e) => flash(e.message))
      }, 500)
    },
    [setNodes, flash],
  )

  const onExecute = useCallback(
    async (pid) => {
      setDrawer({ pid, lines: [], status: 'running', error: null })
      setNodes((ns) => ns.map((n) => (n.id === pid ? { ...n, data: { ...n.data, status: 'running' } } : n)))
      try {
        const { runId } = await api.executeNode(pid)
        const es = new EventSource(`/api/runs/${runId}/stream`)
        es.onmessage = (ev) => {
          let msg
          try {
            msg = JSON.parse(ev.data)
          } catch {
            return
          }
          if (msg.type === 'log') {
            setDrawer((d) => (d ? { ...d, lines: [...d.lines, msg.line] } : d))
          } else if (msg.type === 'end') {
            es.close()
            setDrawer((d) => (d ? { ...d, status: msg.status, error: msg.error } : d))
            if (msg.state) applyState(msg.state)
            if (msg.status !== 'done' && msg.error) flash(msg.error)
          }
        }
        es.onerror = () => es.close()
      } catch (e) {
        setDrawer((d) => (d ? { ...d, status: 'error', error: e.message } : d))
        flash(e.message)
      }
    },
    [applyState, setNodes, flash],
  )

  const onDelete = useCallback((id) => deleteSelection([{ id }], []), [deleteSelection])

  const onReplace = useCallback((id) => {
    replaceTarget.current = id
    replaceRef.current?.click()
  }, [])

  const onExport = useCallback(
    async (name) => {
      try {
        const r = await api.exportImage(name)
        if (r && r.ok) flash(`已保存到 ${r.saved}`)
      } catch (e) {
        flash(e.message)
      }
    },
    [flash],
  )

  const onPorts = useCallback(
    async (pid, group, count) => {
      if (count < 0) return
      try {
        applyState(await api.updateProcess(pid, { ports: { [group]: count } }))
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash],
  )

  // 删除某一个输入口；其后的端口由服务端依次前移（参考图3 → 参考图2）
  const onRemovePort = useCallback(
    async (pid, portKey) => {
      try {
        applyState(await api.deletePort(pid, portKey))
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash],
  )

  // 拖节点右下角的小方块改尺寸。
  // 自己实现而不用 React Flow 的 NodeResizer：后者在无头环境下对合成 pointer 事件不响应，
  // 而且我们只要右下角一个手柄，自己写反而更短更可控。
  const onResizeStart = useCallback(
    (e, pid, minW, minH) => {
      e.preventDefault()
      e.stopPropagation()
      const box = e.currentTarget.parentElement // .node
      const startX = e.clientX
      const startY = e.clientY
      const startW = box?.offsetWidth || minW
      const startH = box?.offsetHeight || minH
      const zoom = getZoom() || 1
      // clientX 是屏幕像素，节点尺寸是画布坐标，要除以缩放
      const sizeW = (v) => Math.max(minW, Math.round(v))
      const sizeH = (v) => Math.max(minH, Math.round(v))
      let last = { w: startW, h: startH }

      const onMove = (ev) => {
        last = {
          w: sizeW(startW + (ev.clientX - startX) / zoom),
          h: sizeH(startH + (ev.clientY - startY) / zoom),
        }
        setNodes((ns) =>
          ns.map((n) => (n.id === pid ? { ...n, width: last.w, height: last.h } : n)),
        )
      }
      const onUp = () => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
        api.savePositions([{ id: pid, w: last.w, h: last.h }]).catch(() => {})
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [getZoom, setNodes],
  )

  // 润色：把新提示词交回给节点自己填（这样它能记住旧值供撤销）
  // 注意必须定义在 cbs 之前 —— useMemo 的依赖数组是立即求值的
  const onPolish = useCallback(
    async (pid) => {
      try {
        return (await api.polishPrompt(pid)).prompt
      } catch (e) {
        flash(e.message)
        return null
      }
    },
    [flash],
  )

  // 自动识别裁剪区域：模型挑参数 → 跑几何切分 → 区域交回给节点填（节点自己记旧值供撤销）
  const onDetect = useCallback(
    async (pid) => {
      try {
        const res = await api.detectRegions(pid)
        const bits = [`识别出 ${res.count} 个区域`]
        if (res.method) bits.push(res.method)
        if (res.reason) bits.push(res.reason)
        flash(bits.join(' · ') + (res.warnings?.length ? '\n' + res.warnings.join('\n') : ''))
        return res
      } catch (e) {
        flash(`自动识别失败：${e.message}`)
        return null
      }
    },
    [flash],
  )

  // 双击预览缩略图 → 打开区域编辑器
  const onEditRegions = useCallback((pid, payload) => {
    setEditing({ pid, ...payload })
  }, [])

  const cbs = useMemo(
    () => ({
      onParam, onExecute, onDelete, onReplace, onExport, onPorts, onRemovePort, onPolish,
      onDetect, onEditRegions,
      onResizeStart,
    }),
    [onParam, onExecute, onDelete, onReplace, onExport, onPorts, onRemovePort, onPolish,
     onDetect, onEditRegions, onResizeStart],
  )
  cbsRef.current = cbs
  stateRef.current = state

  /* ---------------- 加载 ---------------- */
  useEffect(() => {
    api
      .getState()
      .then((s) => {
        applyState(s)
        if (!s.workspace) {
          setWsInput('')
          setShowWs(true)
        }
      })
      .catch((e) => flash(e.message))
  }, [applyState, flash])

  /* ---------------- 交互 ---------------- */
  const onConnect = useCallback(
    async (conn) => {
      try {
        applyState(await api.createEdge(conn.source, conn.target, conn.targetHandle))
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash],
  )

  const onNodeDragStop = useCallback((evt, node) => {
    api.savePositions([{ id: node.id, x: node.position.x, y: node.position.y }]).catch(() => {})
  }, [])
  // 双击数据节点 → 打开大图查看器（缩略图太小，抠图这种活必须能放大看）
  const onNodeDoubleClick = useCallback((evt, node) => {
    const d = node?.data
    if (node?.type !== 'resource' || !d?.hasImage) return
    setViewerFit(true)
    setViewerSize(null)
    setViewer({
      name: d.name,
      mtime: d.mtime,
      width: d.width,
      height: d.height,
      format: d.format,
      kind: d.kind,
      index: d.index,
    })
  }, [])

  /* 查看器里滚轮缩放：以光标为锚点，缩放后把滚动位置挪回去 */
  useEffect(() => {
    const el = viewerStageRef.current
    if (!el || !viewer) return undefined
    const onWheel = (ev) => {
      const img = viewerImgRef.current
      if (!img) return
      const r = img.getBoundingClientRect()
      if (r.width < 2) return
      ev.preventDefault()
      // 先把「光标指到的那一点」记下来，再改尺寸 —— 之后布局回来对账
      viewerAnchorRef.current = anchorZoom(el, img, ev.clientX, ev.clientY)
      const w = Math.min(20000, Math.max(16, r.width * wheelZoom(ev.deltaY)))
      setViewerSize({ w: Math.round(w), h: Math.max(1, Math.round((r.height * w) / r.width)) })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [viewer])

  useEffect(() => enableMiddleDragPan(viewerStageRef.current), [viewer])

  useLayoutEffect(() => {
    const f = viewerAnchorRef.current
    viewerAnchorRef.current = null
    if (f) f()
  }, [viewerSize])

  const addProcess = useCallback(
    async (defId, x, y) => {
      try {
        // 让节点大致以点击位置为中心落下（宽度按该节点类型的默认宽度算）
        const w = stateRef.current?.manifests?.[defId]?.size?.defaultWidth ?? PROCESS_SIZE.w
        applyState(await api.createProcess(defId, x - w / 2, y - 24))
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash],
  )

  const onDrop = useCallback(
    async (e) => {
      e.preventDefault()
      const files = [...(e.dataTransfer?.files || [])].filter((f) => f.type.startsWith('image/'))
      if (!files.length) return
      const pos = screenToFlowPosition({ x: e.clientX, y: e.clientY })
      setBusy(true)
      try {
        for (const f of files) {
          applyState(await api.importImage(f, { x: pos.x, y: pos.y }))
          pos.y += 200
        }
      } catch (err) {
        flash(err.message)
      } finally {
        setBusy(false)
      }
    },
    [applyState, screenToFlowPosition, flash],
  )

  const onPickFile = useCallback(
    async (e) => {
      const files = [...(e.target.files || [])]
      e.target.value = ''
      if (!files.length) return
      setBusy(true)
      const at = importAt.current
      importAt.current = null
      try {
        const pos = at || screenToFlowPosition({ x: 260, y: 220 })
        for (const f of files) {
          applyState(await api.importImage(f, { x: pos.x, y: pos.y }))
          pos.y += 220
        }
      } catch (err) {
        flash(err.message)
      } finally {
        setBusy(false)
      }
    },
    [applyState, screenToFlowPosition, flash],
  )

  const onPickReplace = useCallback(
    async (e) => {
      const f = (e.target.files || [])[0]
      e.target.value = ''
      if (!f || !replaceTarget.current) return
      setBusy(true)
      try {
        applyState(await api.importImage(f, { resourceId: replaceTarget.current }))
      } catch (err) {
        flash(err.message)
      } finally {
        replaceTarget.current = null
        setBusy(false)
      }
    },
    [applyState, flash],
  )

  const reloadScripts = useCallback(async () => {
    try {
      applyState(await api.reloadRegistry())
      flash('已重新加载脚本目录')
    } catch (e) {
      flash(e.message)
    }
  }, [applyState, flash])

  // 把输出节点重排到各自处理节点右侧（收拾历史上摆歪/叠在一起的画布）
  const tidyOutputs = useCallback(async () => {
    try {
      applyState(await api.tidyOutputs())
      flash('已把输出节点重排到各自处理节点的右侧')
    } catch (e) {
      flash(e.message)
    }
  }, [applyState, flash])

  const saveSetting = useCallback(
    async (key, value) => {
      try {
        const s = await api.updateSettings({ [key]: value })
        // 最小尺寸会影响节点渲染（渲染时按它兜底），所以要重建画布而不只是换 state
        if (key === 'nodeMin') applyState(s)
        else setState(s)
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash],
  )

  /* ---- 设置浮层里的「节点最小尺寸」：本地草稿（按具体节点类型分组），失焦时才提交 ---- */
  const [minDraft, setMinDraft] = useState(null)

  /* ---- 网格吸附：格大小也是一个失焦才提交的草稿；格点尺寸给 ReactFlow 用 ---- */
  const [gridDraft, setGridDraft] = useState(null)
  const gridSize = clampGrid(state?.settings?.gridSize ?? GRID_DEFAULT) ?? GRID_DEFAULT

  const commitGrid = useCallback(
    (raw) => {
      setGridDraft(null) // 清掉草稿 → 输入框退回显示服务端的值（被夹过的那个）
      const v = clampGrid(raw)
      if (v === null || v === gridSize) return
      saveSetting('gridSize', v)
    },
    [gridSize, saveSetting],
  )

  const toggleSettings = useCallback(() => {
    // 打开时清空草稿：输入框会直接显示服务端的当前值
    if (!showSettings) {
      setMinDraft({})
      setGridDraft(null)
    }
    setShowSettings((v) => !v)
  }, [showSettings])

  const commitMin = useCallback(
    (key) => {
      const d = minDraft?.[key]
      if (!d) return
      const w = clampMin(d.w)
      const h = clampMin(d.h)
      if (w === null || h === null) {
        // 输入不合法（例如清空了）→ 丢掉草稿，输入框退回显示服务端的当前值
        setMinDraft((s) => {
          const next = { ...(s || {}) }
          delete next[key]
          return next
        })
        return
      }
      const next = { w, h }
      setMinDraft((s) => ({ ...(s || {}), [key]: next }))
      // 只发这一个 key，服务端会与当前值合并
      saveSetting('nodeMin', { [key]: next })
    },
    [minDraft, saveSetting],
  )

  const chooseWorkspace = useCallback(async () => {
    setWsErr('')
    try {
      const s = await api.setWorkspace(wsInput.trim())
      applyState(s)
      setShowWs(false)
    } catch (e) {
      setWsErr(e.message)
    }
  }, [wsInput, applyState])

  const browseWorkspace = useCallback(async () => {
    setWsErr('')
    try {
      const s = await api.pickWorkspace()
      if (s?.cancelled) return
      applyState(s)
      setShowWs(false)
    } catch (e) {
      setWsErr(e.message)
    }
  }, [applyState])

  /* ---------------- 画布右键菜单 ---------------- */
  const openMenu = useCallback(
    (e) => {
      e.preventDefault()
      const rect = canvasRef.current?.getBoundingClientRect()
      if (!rect) return
      const flow = screenToFlowPosition({ x: e.clientX, y: e.clientY })
      // 贴近右/下边缘时把菜单往回收，避免溢出画布
      setMenu({
        x: Math.min(e.clientX - rect.left, Math.max(rect.width - 300, 8)),
        y: Math.min(e.clientY - rect.top, Math.max(rect.height - 340, 8)),
        flowX: flow.x,
        flowY: flow.y,
      })
    },
    [screenToFlowPosition],
  )

  useEffect(() => {
    if (!showSettings) return
    const onDoc = (e) => {
      if (!(e.target instanceof Element) || !e.target.closest('.settings-wrap')) setShowSettings(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [showSettings])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') {
        setMenu(null)
        setShowSettings(false)
        setViewer(null)
        setEditing(null)
        return
      }
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      // 有弹层开着时不吃删除键（否则会把画布上选中的节点删掉）
      if (document.querySelector('.viewer-mask, .editor-mask, .modal-mask')) return
      // 在输入控件里按键不触发删除
      const t = e.target
      const tag = t && t.tagName ? t.tagName.toLowerCase() : ''
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || (t && t.isContentEditable)) return
      const sel = selectionRef.current
      if (!sel.nodes.length && !sel.edges.length) return
      e.preventDefault()
      deleteSelection(sel.nodes, sel.edges)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [deleteSelection])

  const manifests = useMemo(
    () => Object.values(state?.manifests || {}).sort((a, b) => a.id.localeCompare(b.id)),
    [state],
  )

  // 画布左上角那块：当前选中节点的基础数据（现在只有尺寸）。
  // 从 nodes 里现取，所以拖右下角改大小时它是跟着动的。
  const selBox = useMemo(() => {
    if (selIds.length !== 1) return null
    const n = nodes.find((x) => x.id === selIds[0])
    if (!n) return null
    const w = Math.round(n.width ?? n.measured?.width ?? 0)
    const h = Math.round(n.height ?? n.measured?.height ?? 0)
    return w && h ? { w, h } : null
  }, [selIds, nodes])

  /* ---------------- 渲染 ---------------- */
  return (
    <div className="app">
      <div className="toolbar">
        <div className="brand">
          Image<span>Process</span>Flow
        </div>
        <div className="ws" title={state?.workspace || ''}>
          {state?.workspace || '未选择工作目录'}
        </div>
        <button onClick={() => { setWsInput(state?.workspace || ''); setWsErr(''); setShowWs(true) }}>
          工作目录
        </button>

        <div className="spacer" />

        <button onClick={() => importRef.current?.click()} disabled={!state?.workspace}>
          ＋ 导入图片
        </button>
        <button onClick={reloadScripts}>重新加载脚本</button>

        <div className="settings-wrap">
          <button
            className="icon-btn"
            title="设置"
            aria-label="设置"
            onClick={toggleSettings}
          >
            ⚙
          </button>
          {showSettings ? (
            <div className="settings-pop">
              <label className="set-row">
                <input
                  type="checkbox"
                  checked={state?.settings?.confirmDeleteDataNode !== false}
                  onChange={(e) => saveSetting('confirmDeleteDataNode', e.target.checked)}
                />
                <span>删除数据节点前弹出确认</span>
              </label>

              <div className="set-sep" />
              <div className="set-group">提示词润色（DeepSeek）</div>
              <label className="set-field">
                <span>API Key 的环境变量名</span>
                <input
                  type="text"
                  defaultValue={state?.settings?.textModelKeyEnv ?? ''}
                  placeholder="DEEPSEEK_API_KEY"
                  onKeyDown={(e) => e.key === 'Enter' && e.target.blur()}
                  onBlur={(e) => {
                    const v = e.target.value.trim()
                    if (v !== (state?.settings?.textModelKeyEnv ?? '')) saveSetting('textModelKeyEnv', v)
                  }}
                />
              </label>
              <label className="set-field">
                <span>模型</span>
                <input
                  type="text"
                  defaultValue={state?.settings?.textModelName ?? ''}
                  placeholder="deepseek-v4-flash-vision-exp"
                  onKeyDown={(e) => e.key === 'Enter' && e.target.blur()}
                  onBlur={(e) => {
                    const v = e.target.value.trim()
                    if (v !== (state?.settings?.textModelName ?? '')) saveSetting('textModelName', v)
                  }}
                />
              </label>

              <div className="set-sep" />
              <div className="set-group">网格吸附</div>
              <label className="set-row">
                <input
                  type="checkbox"
                  checked={!!state?.settings?.snapToGrid}
                  onChange={(e) => saveSetting('snapToGrid', e.target.checked)}
                />
                <span>拖动节点时吸附到网格</span>
              </label>
              <label className="set-field">
                <span>网格大小</span>
                <div className="set-size">
                  <input
                    type="number"
                    min={GRID_RANGE[0]}
                    max={GRID_RANGE[1]}
                    step={1}
                    value={gridDraft ?? gridSize}
                    onChange={(e) => setGridDraft(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && e.target.blur()}
                    onBlur={(e) => commitGrid(e.target.value)}
                  />
                  <span className="set-x">px</span>
                </div>
              </label>

              <div className="set-sep" />
              <div className="set-group">节点最小尺寸</div>
              {minRows(state).map((row) => {
                const eff = minSizeOf(state, row.key, row.dflt)
                const val = (axis) => minDraft?.[row.key]?.[axis] ?? eff[axis]
                return (
                  <label className="set-field" key={row.key}>
                    <span>{row.label}</span>
                    <div className="set-size">
                      {['w', 'h'].map((axis, i) => (
                        <React.Fragment key={axis}>
                          {i ? <span className="set-x">×</span> : null}
                          <input
                            type="number"
                            min={MIN_RANGE[0]}
                            max={MIN_RANGE[1]}
                            step={10}
                            value={val(axis)}
                            onChange={(e) =>
                              setMinDraft((d) => ({
                                ...(d || {}),
                                [row.key]: {
                                  w: d?.[row.key]?.w ?? eff.w,
                                  h: d?.[row.key]?.h ?? eff.h,
                                  [axis]: e.target.value,
                                },
                              }))
                            }
                            onKeyDown={(e) => e.key === 'Enter' && e.target.blur()}
                            onBlur={() => commitMin(row.key)}
                          />
                        </React.Fragment>
                      ))}
                    </div>
                  </label>
                )
              })}
            </div>
          ) : null}
        </div>
      </div>

      <div
        className="canvas"
        ref={canvasRef}
        onDrop={onDrop}
        onDragOver={(e) => e.preventDefault()}
        onWheel={() => menu && setMenu(null)}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeDragStop={onNodeDragStop}
          onSelectionChange={(p) => {
            selectionRef.current = { nodes: p.nodes, edges: p.edges }
            setSelIds(p.nodes.map((n) => n.id))
          }}
          deleteKeyCode={null}
          onPaneContextMenu={openMenu}
          onPaneClick={() => setMenu(null)}
          onNodeClick={() => setMenu(null)}
          onNodeDoubleClick={onNodeDoubleClick}
          onNodeContextMenu={(e) => e.preventDefault()}
          nodeTypes={nodeTypes}
          /* 网格吸附：拖动时就吸到格点上（服务端在落盘时还会再兜一遍，
             见 DESIGN.md §3.10）。关掉时 snapGrid 不起作用。 */
          snapToGrid={!!state?.settings?.snapToGrid}
          snapGrid={[gridSize, gridSize]}
          fitView
          minZoom={0.2}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="#2a2f38" gap={20} />
          <Controls />
          <MiniMap pannable zoomable nodeColor="#334" maskColor="rgba(12,14,18,.75)" />
        </ReactFlow>

        {selBox ? (
          <div className="sel-info">
            <span>尺寸</span>
            <b>
              {selBox.w} × {selBox.h}
            </b>
          </div>
        ) : null}

        {!busy && nodes.length === 0 && state?.workspace && !menu ? (
          <div className="empty-hint">
            把图片拖进画布即可新建数据节点
            <br />
            在空白处<b>点右键</b>可以创建处理节点
          </div>
        ) : null}

        {menu ? (
          <div className="ctx-menu" style={{ left: menu.x, top: menu.y }}>
            <div className="ctx-title">处理节点</div>
            {manifests.length === 0 ? (
              <div className="ctx-empty">scripts/ 下没有可用脚本</div>
            ) : (
              manifests.map((m) => (
                <button
                  key={m.id}
                  className="ctx-item"
                  onClick={() => {
                    addProcess(m.id, menu.flowX, menu.flowY)
                    setMenu(null)
                  }}
                >
                  {m.name}
                </button>
              ))
            )}

            <div className="ctx-sep" />

            <div className="ctx-title">数据节点</div>
            <button
              className="ctx-item"
              onClick={() => {
                importAt.current = { x: menu.flowX, y: menu.flowY }
                setMenu(null)
                importRef.current?.click()
              }}
            >
              导入图片…
            </button>

            <div className="ctx-sep" />

            <div className="ctx-title">画布</div>
            <button
              className="ctx-item"
              onClick={() => {
                tidyOutputs()
                setMenu(null)
              }}
            >
              重排输出节点
            </button>
          </div>
        ) : null}
      </div>

      {drawer ? (
        <div className="drawer">
          <div className="drawer-head">
            <span className={`dot ${drawer.status}`} />
            <b>执行日志</b>
            <span style={{ color: 'var(--muted)' }}>
              {drawer.status === 'running' ? '进行中…' : drawer.status === 'error' ? '失败' : '完成'}
            </span>
            <span className="spacer" />
            <button className="tiny" onClick={() => setDrawer(null)}>
              关闭
            </button>
          </div>
          <pre className="drawer-body">
            {drawer.lines.join('\n')}
            {drawer.error ? `\n\n[错误]\n${drawer.error}` : ''}
          </pre>
        </div>
      ) : null}

      {busy ? (
        <div className="modal-mask" style={{ background: 'rgba(8,10,14,.35)' }}>
          <div style={{ color: 'var(--muted)' }}>处理中…</div>
        </div>
      ) : null}

      {showWs ? (
        <div className="modal-mask">
          <div className="modal">
            <h3>选择工作目录</h3>
            <p>
              所有图片与画布都存放在这个目录里，工具不会碰目录以外的任何文件。
              <br />
              目录里的 <code>graph.json</code> 就是项目文件。
            </p>
            <input
              type="text"
              value={wsInput}
              placeholder="输入工作目录的完整路径"
              onChange={(e) => setWsInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && chooseWorkspace()}
            />
            {wsErr ? <div className="err">{wsErr}</div> : null}
            <div className="modal-foot">
              <button onClick={browseWorkspace}>浏览…</button>
              <button className="primary" onClick={chooseWorkspace} disabled={!wsInput.trim()}>
                确定
              </button>
              {state?.workspace ? <button onClick={() => setShowWs(false)}>取消</button> : null}
            </div>
          </div>
        </div>
      ) : null}

      {toast ? (
        <div
          style={{
            position: 'absolute',
            bottom: 16,
            left: '50%',
            transform: 'translateX(-50%)',
            background: '#3a2226',
            border: '1px solid #7a3436',
            color: '#ffc9c9',
            padding: '8px 14px',
            borderRadius: 8,
            maxWidth: 640,
            whiteSpace: 'pre-wrap',
            zIndex: 50,
          }}
        >
          {toast}
        </div>
      ) : null}

      {/* 双击数据节点打开的大图查看器 */}
      {viewer ? (
        <div className="viewer-mask">
          <div className="viewer-bar">
            <span className={`badge ${viewer.kind === 'input' ? 'input' : 'output'}`}>
              {viewer.name}
            </span>
            <span className="viewer-name">
              {viewer.kind === 'input' ? '输入图' : `输出 #${(viewer.index ?? 0) + 1}`}
            </span>
            <span className="spacer" />
            <span className="viewer-meta">
              {viewer.width && viewer.height ? `${viewer.width} × ${viewer.height}` : null}
              {viewer.format ? ` · ${viewer.format}` : null}
            </span>
            <button
              className="tiny"
              onClick={() => {
                setViewerSize(null)
                setViewerFit((v) => !v)
              }}
            >
              {viewerFit ? '实际大小' : '适应窗口'}
            </button>
            <button className="tiny" onClick={() => setViewer(null)}>
              关闭 (Esc)
            </button>
          </div>
          <div className={`viewer-stage ${viewerFit ? 'fit' : 'actual'}`} ref={viewerStageRef}>
            <img
              ref={viewerImgRef}
              src={api.imageUrl(viewer.name, viewer.mtime)}
              alt={viewer.name}
              draggable={false}
              title="滚轮缩放 · 中键拖动平移 · 双击切换「适应窗口 / 实际大小」"
              onDoubleClick={() => {
                setViewerSize(null)
                setViewerFit((v) => !v)
              }}
              style={
                viewerSize
                  ? {
                      width: viewerSize.w,
                      height: viewerSize.h,
                      maxWidth: 'none',
                      maxHeight: 'none',
                    }
                  : undefined
              }
            />
          </div>
        </div>
      ) : null}

      {/* 双击区域预览打开的区域编辑器 */}
      {editing ? (
        <RegionEditor
          image={editing.image}
          regions={editing.regions}
          onClose={() => setEditing(null)}
          onSave={(next) => {
            onParam(editing.pid, 'regions', next)
            setEditing(null)
          }}
        />
      ) : null}

      <input ref={importRef} type="file" accept="image/*" multiple hidden onChange={onPickFile} />
      <input ref={replaceRef} type="file" accept="image/*" hidden onChange={onPickReplace} />
    </div>
  )
}
