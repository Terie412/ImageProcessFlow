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

export const setWorkspace = (path) => post('/api/workspace', { path })
export const pickWorkspace = () => post('/api/workspace/pick')
export const exportImage = (name) => post('/api/workspace/export', { name })

export const createProcess = (defId, x, y) => post('/api/processes', { defId, x, y })
export const updateProcess = (id, payload) => patch(`/api/processes/${id}`, payload)
export const tidyOutputs = () => post('/api/tidy')
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

export const imageUrl = (name, mtime) =>
  `/api/images/${encodeURIComponent(name)}?v=${mtime || 0}`
