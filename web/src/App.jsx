import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  SelectionMode,
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

/* 节点尺寸：**所有数字都来自服务端**，前端一个都不自己写。
 *
 * 唯一的事实来源是 `server/nodespec.py`，随 /api/state 下发三样：
 *   - graph 里每个节点的 `w` / `h` —— 服务端算好的**最终渲染尺寸**
 *   - `sizeSpec`                   —— 每个已知类型的 {w,h,minW,minH}（初始 / 最小）
 *   - `sizeRange`                  —— 宽高的允许区间（表单的 min/max 用）
 *
 * 为什么不在前端自己算：这里原有一份 `RESOURCE_SIZE` / `PROCESS_SIZE`，和服务端
 * `engine.py` 的那份漂移了 —— 数据节点没被拖过尺寸时，前端按 220 渲染、服务端按
 * 280 排输出槽位，于是每两个槽位之间空出 70px 的假缝。现在前端只查表。
 */
function specOf(state, key) {
  const sp = state?.sizeSpec
  if (!sp) return null
  return sp[key] || (String(key).startsWith('process:') ? sp.process : sp.resource) || null
}

/* 设置浮层里要列出的行：数据节点 + 每个已注册的处理节点类型。
   一律以服务端下发的 sizeSpec 为准 —— 那边没有的类型就不列。 */
function sizeRows(state) {
  const mans = Object.values(state?.manifests || {}).sort((a, b) => a.id.localeCompare(b.id))
  return [
    { key: 'resource', label: '数据节点' },
    ...mans.map((m) => ({ key: `process:${m.id}`, label: m.name || m.id })),
  ].filter((r) => specOf(state, r.key))
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
 * 尺寸规格按「具体节点类型」索引，与服务端的 `settings.nodeSize` / `settings.nodeMin`
 * 完全同一套 key（`resource` / `process:<defId>` / `process` 兜底）：
 *   初始尺寸 nodeSize → 节点没被拖过右下角手柄时该多大
 *   最小尺寸 nodeMin  → 拖手柄时的下限
 * 具体取哪一条、怎么兜底，都在服务端 `nodespec.py` 里做完了，前端只查 `sizeSpec`。
 */

const EDGE_STYLE = { stroke: '#4f9cf9' }
const EDGE_MARKER = { type: MarkerType.ArrowClosed, color: '#4f9cf9', width: 18, height: 18 }
const IMPLICIT_STYLE = { stroke: '#4a5563', strokeDasharray: '4 4' }

// 分辨率参数的合法区间（与 resize manifest 的 min/max 保持一致）
const clampDim = (v) => Math.max(1, Math.min(16384, Math.round(v)))

/* 节点当前「生效的第一个输入」的数据节点（带原图宽高）。
   「第一个」= 序号最小的**连了图**的输入口 —— image1 没连时生效的是 image2。
   只认 width/height 都读得到的图；找不到返回 null。 */
function firstLinkedRes(st, pid) {
  const g = st?.graph
  if (!g) return null
  const portNum = (k) => {
    const m = /(\d+)$/.exec(k || '')
    return m ? Number(m[1]) : 0
  }
  const es = g.edges.filter((e) => e.to === pid).sort((a, b) => portNum(a.port) - portNum(b.port))
  for (const e of es) {
    const r = g.resources.find((x) => x.id === e.from)
    if (r?.width && r?.height) return r
  }
  return null
}

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

  return [
    ...state.graph.resources.map((r) => {
      const spec = specOf(state, 'resource')
      return {
        id: r.id,
        type: 'resource',
        position: { x: r.x ?? 0, y: r.y ?? 0 },
        // 尺寸走 React Flow 的顶层 width/height。注意数据节点里 data.width/height
        // 是「图片像素分辨率」，别搞混 —— 节点尺寸是 `w`/`h`，由服务端按
        // nodespec 算好下发（max(最小尺寸, 存的 ?? 初始尺寸)）。
        width: r.w,
        height: r.h,
        data: {
          ...r,
          canvasId: state.canvasId,
          // 拖手柄时的下限；拿不到规格就退化成当前尺寸（总比 0 强）
          minW: spec?.minW ?? r.w,
          minH: spec?.minH ?? r.h,
          ...cbs,
        },
      }
    }),
    ...state.graph.processes.map((p) => {
      const spec = specOf(state, `process:${p.defId}`)
      return {
        id: p.id,
        type: 'process',
        position: { x: p.x ?? 0, y: p.y ?? 0 },
        // 同上：`w`/`h` 是服务端算好的最终尺寸
        width: p.w,
        height: p.h,
        data: {
          ...p,
          canvasId: state.canvasId,
          manifest: state.manifests?.[p.defId],
          inputs: portMap[p.id] || {},
          minW: spec?.minW ?? p.w,
          minH: spec?.minH ?? p.h,
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
  const [canvasMenu, setCanvasMenu] = useState(false) // 画布分段控件右侧的 ⋯ 菜单
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
  const nodesRef = useRef([]) // 最新一帧的 React Flow 节点（读本地的 params，比服务端状态新鲜）
  const selectionRef = useRef({ nodes: [], edges: [] })

  const { screenToFlowPosition, fitView, getZoom, getViewport, setViewport } = useReactFlow()

  const flash = useCallback((msg) => {
    setToast(String(msg))
    setTimeout(() => setToast(null), 6000)
  }, [])

  /* 画布切换的收尾：恢复目标画布记过的视野。
     只在**画布真的变了**的时候做 —— 否则每次落盘回来的状态都会把用户刚做的
     平移缩放拽回去。 */
  const viewedCanvasRef = useRef(null)

  const applyViewport = useCallback(
    (s) => {
      const vp = (s.canvases || []).find((c) => c.id === s.canvasId)?.viewport
      if (vp && Number.isFinite(vp.zoom) && vp.zoom > 0) {
        fitted.current = true // 有记过的视野，就别再自动「适应窗口」
        setViewport({ x: vp.x, y: vp.y, zoom: vp.zoom }, { duration: 0 })
        return true
      }
      fitted.current = false
      return false
    },
    [setViewport],
  )

  const applyState = useCallback(
    (s) => {
      if (!s || s.cancelled) return
      const switched = viewedCanvasRef.current !== s.canvasId
      viewedCanvasRef.current = s.canvasId
      setState(s)
      setNodes(buildNodes(s, cbsRef.current))
      setEdges(buildEdges(s))
      if (switched && applyViewport(s)) return
      if (!fitted.current && (s.graph?.resources?.length || s.graph?.processes?.length)) {
        fitted.current = true
        setTimeout(() => fitView({ padding: 0.25, duration: 300 }), 60)
      }
    },
    [setNodes, setEdges, fitView, applyViewport],
  )

  /* ---------------- 画布：切换 / 新建 / 改名 / 删除 ----------------
     切走之前先把当前视野记到**将要离开的那个画布**上，切过去再恢复新画布的
     视野 —— 每个画布各记各的，切回来还在原来的位置。 */
  const rememberViewport = useCallback(async () => {
    const cid = viewedCanvasRef.current
    if (!cid) return
    try {
      await api.updateCanvas(cid, { viewport: getViewport() })
    } catch {
      /* 记不住就算了：视野丢了不影响数据 */
    }
  }, [getViewport])

  const switchCanvas = useCallback(
    async (cid) => {
      if (!cid || cid === viewedCanvasRef.current) return
      setMenu(null)
      await rememberViewport()
      try {
        applyState(await api.openCanvas(cid))
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash, rememberViewport],
  )

  const newCanvas = useCallback(async () => {
    await rememberViewport()
    try {
      applyState(await api.createCanvas())
    } catch (e) {
      flash(e.message)
    }
  }, [applyState, flash, rememberViewport])

  const [renameDraft, setRenameDraft] = useState(null)

  // 打开「重命名画布」弹窗：先把当前名字填成草稿
  const openRenameCanvas = useCallback(() => {
    const cur = (stateRef.current?.canvases || []).find((c) => c.id === stateRef.current?.canvasId)
    setRenameDraft(cur?.name || '')
  }, [])

  const commitRename = useCallback(async () => {
    const v = (renameDraft || '').trim()
    setRenameDraft(null)
    const cur = (stateRef.current?.canvases || []).find((c) => c.id === stateRef.current?.canvasId)
    if (!v || !cur || v === cur.name) return
    try {
      applyState(await api.updateCanvas(cur.id, { name: v }))
    } catch (e) {
      flash(e.message)
    }
  }, [applyState, flash, renameDraft])

  const removeCanvas = useCallback(async () => {
    const s = stateRef.current
    const cur = (s?.canvases || []).find((c) => c.id === s?.canvasId)
    if (!cur) return
    if ((s.canvases || []).length <= 1) return flash('至少要保留一个画布')
    const ok = window.confirm(
      `删除画布「${cur.name}」？\n` +
        `· 它里面的 ${cur.nodeCount} 个节点会一起消失\n` +
        '· 它自己的图片池会被永久删除（每个画布一份，别的画布不受影响）',
    )
    if (!ok) return
    try {
      applyState(await api.deleteCanvas(cur.id))
    } catch (e) {
      flash(e.message)
    }
  }, [applyState, flash])

  /* ---------------- 跨画布复制 / 粘贴节点 ----------------
     剪贴板放在服务端：粘贴时它得把图片文件从源画布的池子**复制**一份进目标画布，
     并在目标画布重新分配短名（图片池是按画布分的，同名在不同画布是不同文件）。 */
  const copySelection = useCallback(async () => {
    const ids = (selectionRef.current?.nodes || []).map((n) => n.id)
    if (!ids.length) return flash('先选中要复制的节点')
    try {
      const r = await api.copyNodes(ids)
      flash(`已复制 ${r.nodes} 个节点${r.edges ? ` 和 ${r.edges} 条连线` : ''}，切到别的画布粘贴即可`)
    } catch (e) {
      flash(e.message)
    }
  }, [flash])

  const pasteClipboard = useCallback(
    async (at) => {
      try {
        const s = await api.pasteNodes(at)
        applyState(s)
        const missing = s.paste?.missing || []
        flash(
          missing.length
            ? `已粘贴 ${s.paste.nodes} 个节点，但有 ${missing.length} 张图片文件没找到`
            : `已粘贴 ${s.paste?.nodes || 0} 个节点`,
        )
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash],
  )

  /* Ctrl+V 粘到**视野中心** —— 跨画布粘贴时原来的坐标很可能在新画布视野之外 */
  const pasteAtCenter = useCallback(() => {
    const r = canvasRef.current?.getBoundingClientRect()
    if (!r) return pasteClipboard()
    return pasteClipboard(screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 }))
  }, [pasteClipboard, screenToFlowPosition])

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
  // 单个参数的本地更新 + 防抖落盘（onParam 和比例联动都走这里，写进同一批 PATCH）
  const pushParam = useCallback(
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

  /* 保持比例（调整分辨率节点）：算出需要连带调整的姊妹参数，不需要就返回 null。
     读节点本地 params（nodesRef 比服务端状态新鲜，开关刚拨上也能立刻生效），
     原图宽高从服务端状态取（图片尺寸只有服务端算）。 */
  const ratioSibling = useCallback((pid, key, value) => {
    const node = nodesRef.current.find((n) => n.id === pid)
    const d = node?.data
    if (!d || d.manifest?.id !== 'resize') return null
    const params = d.params || {}
    if (key === 'keepRatio') {
      if (!value) return null // 关掉不同步；打开时维持宽、调高（时机 2）
    } else {
      if (!params.keepRatio) return null // 开关没开，改宽高就是普通编辑
      if (key !== 'width' && key !== 'height') return null
    }
    const res = firstLinkedRes(stateRef.current, pid)
    if (!res) return null // 没有生效输入，没什么可跟的
    const W = res.width
    const H = res.height
    const curW = Number(params.width) || 0
    const curH = Number(params.height) || 0
    if (key === 'keepRatio') {
      if (curW <= 0) return null
      const nh = clampDim((curW * H) / W)
      return nh !== curH ? { height: nh } : null
    }
    const v = Number(value)
    if (!Number.isFinite(v) || v <= 0) return null
    if (key === 'width') {
      const nh = clampDim((v * H) / W) // 时机 3：改宽 → 调高
      return nh !== curH ? { height: nh } : null
    }
    const nw = clampDim((v * W) / H) // 时机 3：改高 → 调宽
    return nw !== curW ? { width: nw } : null
  }, [])

  const onParam = useCallback(
    (pid, key, value) => {
      pushParam(pid, key, value)
      const sib = ratioSibling(pid, key, value)
      if (sib) {
        const [k, v] = Object.entries(sib)[0]
        pushParam(pid, k, v) // 合进同一批 PATCH，500ms 后一起落盘
      }
    },
    [pushParam, ratioSibling],
  )

  /* ---------------- 执行日志 ----------------
     日志数据（drawer）在执行期间始终收集，但面板**默认不弹出**；
     顶栏「执行日志」按钮随时可以点开看最近一次（进行中也行）。
     失败时 toast 会照常报错，不看日志也不会漏掉失败这件事。 */
  const [logOpen, setLogOpen] = useState(false)

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
    async (id, name) => {
      try {
        const r = await api.exportImage(name, id)
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
  nodesRef.current = nodes

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
      const prev = stateRef.current // 连线前的状态：用来判断节点之前有没有生效输入
      try {
        const st = await api.createEdge(conn.source, conn.target, conn.targetHandle)
        applyState(st)
        // 保持比例（时机 1）：开关开着、节点从没有输入 → 连上第一张图，
        // 维持宽不变、按原图比例调整高。之前已有生效输入就不动（追加连线不算）。
        const p = st.graph?.processes.find((x) => x.id === conn.target)
        if (p?.defId === 'resize' && p.params?.keepRatio && !firstLinkedRes(prev, conn.target)) {
          const res = firstLinkedRes(st, conn.target)
          const w = Number(p.params?.width) || 0
          if (res && w > 0) {
            const nh = clampDim((w * res.height) / res.width)
            if (nh !== (Number(p.params.height) || 0)) {
              applyState(await api.updateProcess(conn.target, { params: { height: nh } }))
            }
          }
        }
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash],
  )

  // 拖动结束：把**本次拖动的所有节点**一起落盘。第三个参数 nodes 是 React Flow
  // 给的「参与这次拖动的全部节点」（框选后整组拖动时 >1）——只存 node 一个的话，
  // 组里其他节点的新位置只活在内存里，下次任何状态刷新（连线/改参数/执行）
  // 都会被服务端的旧坐标弹回去，看起来就像「节点自己乱跑」。
  const onNodeDragStop = useCallback((evt, node, nodes) => {
    const items = (nodes?.length ? nodes : [node]).map((n) => ({
      id: n.id,
      x: n.position.x,
      y: n.position.y,
    }))
    api.savePositions(items).catch(() => {})
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
        // 让节点大致以点击位置为中心落下（宽度按服务端下发的初始尺寸算）
        const w = specOf(stateRef.current, `process:${defId}`)?.w ?? 0
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

  // 整张画布重新排版：分层列布局，处理节点 / 输出节点 / 输入图全都重排
  const formatAll = useCallback(async () => {
    try {
      const s = await api.formatAll()
      applyState(s)
      flash(s.moved ? `已重新排版，移动了 ${s.moved} 个节点` : '节点位置已经排好了，没有需要移动的')
    } catch (e) {
      flash(e.message)
    }
  }, [applyState, flash])

  const undoFormat = useCallback(async () => {
    try {
      const s = await api.undoFormat()
      applyState(s)
      flash(`已还原 ${s.restored} 个节点的位置`)
    } catch (e) {
      flash(e.message)
    }
  }, [applyState, flash])

  const saveSetting = useCallback(
    async (key, value) => {
      try {
        const s = await api.updateSettings({ [key]: value })
        // 尺寸设置会改变节点渲染（初始尺寸 / 最小尺寸都参与），所以要重建画布
        // 而不只是换 state —— 改完当场重排，不用刷新
        if (key === 'nodeSize' || key === 'nodeMin') applyState(s)
        else setState(s)
      } catch (e) {
        flash(e.message)
      }
    },
    [applyState, flash],
  )

  /* ---- 设置浮层里的「节点尺寸」：本地草稿（一行 = 一个节点类型，含初始/最小两组），
     失焦时才提交 ---- */
  const [sizeDraft, setSizeDraft] = useState(null)

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
      setSizeDraft({})
      setGridDraft(null)
    }
    setShowSettings((v) => !v)
  }, [showSettings])

  /* 尺寸草稿 → 提交。一次失焦只改一个字段，所以最多发出一个请求：
     改「初始」发 nodeSize、改「最小」发 nodeMin（服务端负责与当前值合并）。
     没改的那一组不发 —— 否则每次失焦都要重建一遍画布。 */
  const commitSize = useCallback(
    (key) => {
      const d = sizeDraft?.[key]
      if (!d) return
      const eff = specOf(state, key) || {}
      // 只接受正数；清空 / 乱填一律丢掉草稿，输入框退回显示服务端的值。
      // 区间由服务端夹（nodespec.SIZE_RANGE），前端不重复一份数字。
      const num = (v) => {
        const n = Number(v)
        return Number.isFinite(n) && n > 0 ? Math.round(n) : null
      }
      const group = (a, b) => {
        const w = num(d[a] ?? eff[a])
        const h = num(d[b] ?? eff[b])
        return w === null || h === null ? null : { w, h }
      }
      const size = group('w', 'h')
      const mins = group('minW', 'minH')
      if (!size || !mins) {
        setSizeDraft((s) => {
          const next = { ...(s || {}) }
          delete next[key]
          return next
        })
        return
      }
      setSizeDraft((s) => {
        const next = { ...(s || {}) }
        delete next[key] // 清掉草稿 → 输入框显示服务端（夹过）的值
        return next
      })
      if (size.w !== eff.w || size.h !== eff.h) saveSetting('nodeSize', { [key]: size })
      if (mins.w !== eff.minW || mins.h !== eff.minH) saveSetting('nodeMin', { [key]: mins })
    },
    [sizeDraft, state, saveSetting],
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

  // 画布 ⋯ 菜单：点到分段控件以外的地方就收起（与设置浮层同一套写法）
  useEffect(() => {
    if (!canvasMenu) return
    const onDoc = (e) => {
      if (!(e.target instanceof Element) || !e.target.closest('.canvas-pick')) setCanvasMenu(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [canvasMenu])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') {
        setMenu(null)
        setShowSettings(false)
        setCanvasMenu(false)
        setViewer(null)
        setEditing(null)
        return
      }
      // Ctrl+C / Ctrl+V：复制 / 粘贴节点（跨画布也用这一套）
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey) {
        const k = e.key.toLowerCase()
        if (k !== 'c' && k !== 'v') return
        const t0 = e.target
        const tg0 = t0 && t0.tagName ? t0.tagName.toLowerCase() : ''
        if (tg0 === 'input' || tg0 === 'textarea' || tg0 === 'select' || (t0 && t0.isContentEditable)) return
        e.preventDefault()
        if (k === 'c') copySelection()
        else pasteAtCenter()
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
  }, [deleteSelection, copySelection, pasteAtCenter])

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
        {/* ① 身份 */}
        <div className="brand">
          Image<span>Process</span>Flow
        </div>

        {/* ② 位置：工作目录 + 画布。
            左半屏是「你在哪个项目、哪个画布」，右半屏是「能做什么」——
            主次靠形态差：这两个是**有容器的控件**，右边一律是裸按钮。 */}
        <div className="where">
          {/* 路径本身就是按钮。原来是一个只读路径框 + 一个「工作目录」按钮，
              两样东西办一件事、还长得一样重。 */}
          <button
            className={`ws-chip${state?.workspace ? '' : ' empty'}`}
            title={state?.workspace ? `${state.workspace}\n点击切换工作目录` : '点击选择一个工作目录'}
            onClick={() => {
              setWsInput(state?.workspace || '')
              setWsErr('')
              setShowWs(true)
            }}
          >
            <svg className="ws-ico" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
              <path
                d="M2 4.6h4l1.4 1.7H14v6.3H2z"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.3"
                strokeLinejoin="round"
              />
            </svg>
            <span className="ws-path">{state?.workspace || '选择工作目录'}</span>
          </button>

          {/* 画布：一个分段控件（标签 + 下拉 + ＋ + ⋯ 共用一个边框）。
              改名 / 删除收进 ⋯ —— 低频、且删除是破坏性的，多一层正好。 */}
          <div className="canvas-pick" title="每个画布有自己的节点、连线与图片池">
            <span className="lbl">画布</span>
            <select
              value={state?.canvasId || ''}
              onChange={(e) => switchCanvas(e.target.value)}
              disabled={!state?.workspace}
              title="切换画布"
            >
              {(state?.canvases || []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}{c.nodeCount ? `（${c.nodeCount}）` : ''}
                </option>
              ))}
            </select>
            <button className="seg-btn" title="新建一个画布" onClick={newCanvas} disabled={!state?.workspace}>
              ＋
            </button>
            <button
              className="seg-btn"
              title="重命名 / 删除当前画布"
              aria-label="重命名或删除当前画布"
              onClick={() => setCanvasMenu((v) => !v)}
              disabled={!state?.workspace}
            >
              ⋯
            </button>
            {canvasMenu ? (
              <div className="canvas-menu">
                <button
                  className="ctx-item"
                  onClick={() => {
                    setCanvasMenu(false)
                    openRenameCanvas()
                  }}
                >
                  重命名画布
                </button>
                <button
                  className="ctx-item danger"
                  disabled={(state?.canvases || []).length <= 1}
                  title={(state?.canvases || []).length <= 1 ? '至少要保留一个画布' : ''}
                  onClick={() => {
                    setCanvasMenu(false)
                    removeCanvas()
                  }}
                >
                  删除画布
                </button>
              </div>
            ) : null}
          </div>
        </div>

        <div className="spacer" />

        {/* ③ 动作：幽灵按钮（无边框，悬停才显形），只强调「导入图片」。
            整组包一层 —— 窗口窄到折行时它们一起挪到第二行，不会被拆成两半。 */}
        <div className="tb-actions">
          <button className="tb-act" onClick={() => importRef.current?.click()} disabled={!state?.workspace}>
            ＋ 导入图片
          </button>
          <button className="tb-ghost" onClick={reloadScripts}>
            重新加载脚本
          </button>
          <button
            className="tb-ghost"
            onClick={() => setLogOpen(true)}
            disabled={!drawer}
            title="查看最近一次执行的日志"
          >
            执行日志{drawer?.status === 'running' ? '（进行中…）' : ''}
          </button>

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
                <div className="set-group">节点尺寸</div>
                <div className="set-head">
                  <span>类型</span>
                  <span>初始</span>
                  <span>最小</span>
                </div>
                {sizeRows(state).map((row) => {
                  const eff = specOf(state, row.key)
                  const val = (f) => sizeDraft?.[row.key]?.[f] ?? eff[f]
                  const edit = (f, raw) =>
                    setSizeDraft((d) => ({
                      ...(d || {}),
                      [row.key]: { ...eff, ...(d?.[row.key] || {}), [f]: raw },
                    }))
                  return (
                    // 每行两组：初始（w×h）与最小（w×h）。数字的权威在服务端 ——
                    // 这里只负责把用户输入的草稿发出去，回来的是被夹过的值
                    <div className="set-field set-sizes" key={row.key}>
                      <span className="set-name" title={row.label}>
                        {row.label}
                      </span>
                      {[['w', 'h'], ['minW', 'minH']].map((pair) => (
                        <div className="set-size" key={pair[0]}>
                          {pair.map((f, i) => (
                            <React.Fragment key={f}>
                              {i ? <span className="set-x">×</span> : null}
                              <input
                                type="number"
                                min={state?.sizeRange?.[0]}
                                max={state?.sizeRange?.[1]}
                                step={10}
                                value={val(f)}
                                onChange={(e) => edit(f, e.target.value)}
                                onKeyDown={(e) => e.key === 'Enter' && e.target.blur()}
                                onBlur={() => commitSize(row.key)}
                              />
                            </React.Fragment>
                          ))}
                        </div>
                      ))}
                    </div>
                  )
                })}
              </div>
            ) : null}
          </div>
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
          /* 左键交互：
             - 空白处按住拖动 → 框选（碰到节点的就算选中，Partial 模式）
             - 节点上按住拖动 → 移动节点；拖的是已框选的节点时整组一起动（ReactFlow 内置）
             画布平移只认**中键**（panOnDrag=[1]），左键不再平移。 */
          selectionOnDrag
          panOnDrag={[1]}
          selectionMode={SelectionMode.Partial}
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

            <div className="ctx-title">节点</div>
            <button
              className="ctx-item"
              onClick={() => {
                copySelection()
                setMenu(null)
              }}
            >
              复制选中的节点
            </button>
            <button
              className="ctx-item"
              onClick={() => {
                pasteClipboard({ x: menu.flowX, y: menu.flowY })
                setMenu(null)
              }}
            >
              粘贴到此处
            </button>

            <div className="ctx-sep" />

            <div className="ctx-title">画布</div>
            <button
              className="ctx-item"
              onClick={() => {
                formatAll()
                setMenu(null)
              }}
            >
              格式化所有节点
            </button>
            <button
              className="ctx-item"
              disabled={!state?.canUndoFormat}
              title={
                state?.canUndoFormat
                  ? '把节点放回格式化之前的位置（连点几次格式化也一次退回）'
                  : '这个画布还没有格式化过'
              }
              onClick={() => {
                undoFormat()
                setMenu(null)
              }}
            >
              撤销格式化
            </button>
          </div>
        ) : null}
      </div>

      {drawer && logOpen ? (
        <div className="drawer">
          <div className="drawer-head">
            <span className={`dot ${drawer.status}`} />
            <b>执行日志</b>
            <span style={{ color: 'var(--muted)' }}>
              {drawer.status === 'running' ? '进行中…' : drawer.status === 'error' ? '失败' : '完成'}
            </span>
            <span className="spacer" />
            <button className="tiny" onClick={() => setLogOpen(false)}>
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

      {renameDraft !== null ? (
        <div className="modal-mask">
          <div className="modal">
            <h3>重命名画布</h3>
            <p>画布名只是显示用的，不影响图片与文件。</p>
            <input
              type="text"
              autoFocus
              value={renameDraft}
              placeholder="画布名"
              onChange={(e) => setRenameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitRename()
                if (e.key === 'Escape') setRenameDraft(null)
              }}
            />
            <div className="modal-foot">
              <button className="primary" onClick={commitRename} disabled={!renameDraft.trim()}>
                确定
              </button>
              <button onClick={() => setRenameDraft(null)}>取消</button>
            </div>
          </div>
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
              src={api.imageUrl(state?.canvasId, viewer.name, viewer.mtime)}
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
