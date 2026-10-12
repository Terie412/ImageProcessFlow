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
export const reloadRegistry = () => post('/api/registry/reload')
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
export const deleteEdge = (id) =>
  fetch(`/api/edges/${id}`, { method: 'DELETE' }).then(J)

export const executeNode = (id) => post(`/api/processes/${id}/execute`)
export const polishPrompt = (id) => post(`/api/processes/${id}/polish`)
export const detectRegions = (id) => post(`/api/processes/${id}/detect-regions`)

export const importImage = async (file, { resourceId = '', x = 0, y = 0 } = {}) => {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('resource_id', resourceId)
  fd.append('x', String(x))
  fd.append('y', String(y))
  return fetch('/api/resources/import', { method: 'POST', body: fd }).then(J)
}

/* 图片池是按画布分的，所以取图**必须带画布 id** ——
   两个画布各有一个 A 是完全正常的，少了 id 会把别的画布的 A 拿过来 */
export const imageUrl = (canvasId, name, mtime) =>
  `/api/images/${encodeURIComponent(canvasId || '')}/${encodeURIComponent(name)}?v=${mtime || 0}`
