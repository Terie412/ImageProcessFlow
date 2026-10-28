const J = async (r) => {
  if (!r.ok) {
    let msg = r.statusText
    try {
      const j = await r.json()
      msg = j.detail || j.hint || JSON.stringify(j)
    } catch (e) {
      try { msg = await r.text() } catch (e2) { /* ignore */ }
    }
    throw new Error(msg)
  }
  return r.json()
}

const post = (url, body) =>
  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(J)

const patch = (url, body) =>
  fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(J)

export const getState = () => fetch('/api/state').then(J)
// 脚本注册表重载（`POST /api/registry/reload`）是**开发者接口**，界面上没有入口 ——
// 服务端启动时扫一次 scripts/，改完脚本重启服务即可。要手工触发就用 curl。
export const updateSettings = (payload) => patch('/api/settings', payload)

/* 画布：一个工作目录里可以有多个，切换/新建/改名/删除都返回整包状态 */
export const createCanvas = (name) => post('/api/canvases', { name })
export const openCanvas = (id) => post(`/api/canvases/${id}/open`)
export const updateCanvas = (id, payload) => patch(`/api/canvases/${id}`, payload)
export const deleteCanvas = (id) => fetch(`/api/canvases/${id}`, { method: 'DELETE' }).then(J)

/* 跨画布复制/粘贴：剪贴板在服务端，粘贴时顺手把图片文件复制进目标画布 */
export const copyNodes = (ids) => post('/api/clipboard/copy', { ids })
export const pasteNodes = (at) => post('/api/clipboard/paste', at || {})

export const setWorkspace = (path) => post('/api/workspace', { path })
export const pickWorkspace = () => post('/api/workspace/pick')
export const exportImage = (name, resourceId) =>
  post('/api/workspace/export', { name, resource_id: resourceId || '' })
// 「覆盖」：直接写回上次下载的完整路径，不弹对话框（记忆文件不在时服务端退回另存为）
export const overwriteImage = (name, resourceId) =>
  post('/api/workspace/overwrite', { name, resource_id: resourceId || '' })

export const createProcess = (defId, x, y) => post('/api/processes', { defId, x, y })
export const updateProcess = (id, payload) => patch(`/api/processes/${id}`, payload)
// 整图重新排版 / 撤销（撤销每个画布只能用一次）
export const formatAll = () => post('/api/format')
export const undoFormat = () => post('/api/format/undo')
export const deletePort = (id, portKey) =>
  fetch(`/api/processes/${id}/ports/${portKey}`, { method: 'DELETE' }).then(J)
export const savePositions = (items) => patch('/api/positions', items)
export const deleteNode = (id) =>
  fetch(`/api/nodes/${id}`, { method: 'DELETE' }).then(J)

export const createEdge = (from, to, port) => post('/api/edges', { from, to, port })
// 「填充输入」：把一批数据节点按到本节点的距离填进它的输入口
// （端口多删少补、已连的覆盖、本节点自己的产出会被排除；顺序由服务端算）
export const fillInputs = (id, resourceIds) =>
  post(`/api/processes/${id}/fill-inputs`, { resources: resourceIds })
export const deleteEdge = (id) =>
  fetch(`/api/edges/${id}`, { method: 'DELETE' }).then(J)

export const executeNode = (id) => post(`/api/processes/${id}/execute`)
export const polishPrompt = (id) => post(`/api/processes/${id}/polish`)
export const detectRegions = (id) => post(`/api/processes/${id}/detect-regions`)

/* 「按参数造图」的节点（如程序化描边圆角框）的**实时预览**：把当前参数发过去，
   服务端用脚本自己的绘制代码画一张 PNG 回来，**不落盘、不占输出槽位**。
   参数取本地这份（不是等 500ms 落盘之后再说），否则缩略图会比手上的控件慢半拍。
   返回的是 blob URL —— 调用方负责在换图或关窗时 URL.revokeObjectURL()。 */
export const previewNode = async (id, params) => {
  const r = await fetch(`/api/processes/${id}/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ params: params || {} }),
  })
  if (!r.ok) return J(r) // J 会带着服务端的 detail 抛出来
  return URL.createObjectURL(await r.blob())
}

export const importImage = async (file, { resourceId = '', x = 0, y = 0 } = {}) => {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('resource_id', resourceId)
  fd.append('x', String(x))
  fd.append('y', String(y))
  return fetch('/api/resources/import', { method: 'POST', body: fd }).then(J)
}

/* 资源目录（工作目录之外的只读素材来源，路径跟随工作目录存在 graph.json 里）。
   列表是**懒加载**的：path 是相对资源目录根的路径，"" 表示根目录。 */
export const setResourceDir = (path) => post('/api/resourcedir', { path })
export const pickResourceDir = () => post('/api/resourcedir/pick')
export const listResourceDir = (path) =>
  fetch(`/api/resourcedir/list?path=${encodeURIComponent(path || '')}`).then(J)
// 从资源目录里取一个文件拖进画布 = 服务端直接**复制**一份进当前画布（不上传）
export const importFromResource = (path, x, y) =>
  post('/api/resources/import-from-resource', { path, x, y })
// 资源目录的右键写操作：只新建**目录**（不建文件）、改名、删除；删除默认进回收站
export const mkdirResource = (parent, name) => post('/api/resourcedir/mkdir', { parent, name })
export const renameResource = (path, name) => post('/api/resourcedir/rename', { path, name })
export const deleteResource = (path) => post('/api/resourcedir/delete', { path })
// 在系统文件管理器里打开一项（文件 = 打开所在目录并选中它）—— 服务端只负责把它显示出来
export const revealResource = (path) => post('/api/resourcedir/reveal', { path })
// 面板里的缩略图：只读地取资源目录里的原文件，交给浏览器缩放
export const resourceRawUrl = (path, mtime) =>
  `/api/resourcedir/raw?path=${encodeURIComponent(path || '')}&v=${mtime || 0}`
// 双击预览时读一张图的尺寸 / 格式 / 有没有 alpha（与数据节点同一口径，只读文件头）
export const resourceInfo = (path) =>
  fetch(`/api/resourcedir/info?path=${encodeURIComponent(path || '')}`).then(J)

/* 颜色库（文档级字段，跟随工作目录存在 graph.json 里，见 DESIGN.md §3.20）。
   一个颜色 = 名字 + 值（`#rrggbb`）。三个接口都回整包状态，调用方 applyState 即可。 */
export const addColor = (name, value) => post('/api/colors', { name, value })
export const updateColor = (id, payload) => patch(`/api/colors/${id}`, payload)
export const deleteColor = (id) =>
  fetch(`/api/colors/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(J)

/* 图片池是按画布分的，所以取图**必须带画布 id** ——
   两个画布各有一个 A 是完全正常的，少了 id 会把别的画布的 A 拿过来。
   视频节点的媒体文件和图片走**同一条路由**（池子只有一个，扩展名区分类型）；
   缩略图另有一条 —— 它存在 posters/ 里，不混进媒体池（见 server/workspace.py） */
export const imageUrl = (canvasId, name, mtime) =>
  `/api/images/${encodeURIComponent(canvasId || '')}/${encodeURIComponent(name)}?v=${mtime || 0}`

export const posterUrl = (canvasId, name, mtime) =>
  `/api/posters/${encodeURIComponent(canvasId || '')}/${encodeURIComponent(name)}?v=${mtime || 0}`
