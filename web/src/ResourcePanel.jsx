import React, { useCallback, useEffect, useState } from 'react'
import * as api from './api'

/* 拖拽资源文件用的自定义 MIME。
 *
 * 画布的 onDrop 靠它区分「从资源面板拖出来的东西」和「从操作系统拖进来的文件」：
 * 带上这个类型时 dataTransfer.files 是空的，所以两条路不会同时命中。
 * 值就是相对资源目录的路径 —— 落点时服务端据此直接复制，不经浏览器上传。 */
export const RESOURCE_DRAG_MIME = 'application/x-ipf-resource'

const fmtSize = (n) => {
  if (!Number.isFinite(n)) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`
}

const Chevron = ({ open }) => (
  <svg className={`res-caret${open ? ' open' : ''}`} viewBox="0 0 10 6" width="8" height="5" aria-hidden="true">
    <path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

const FolderIco = () => (
  <svg className="res-ico dir" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
    <path d="M2 4.4h3.9l1.3 1.6H14v6H2z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
  </svg>
)

const FileIco = () => (
  <svg className="res-ico file" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
    <path d="M4 2h5l3 3v9H4z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    <path d="M9 2v3h3" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
  </svg>
)

/* 一层目录的行。递归渲染展开的子目录 —— 数据都在父组件的 tree / expanded 里。 */
function Rows({ rel, depth, tree, expanded, onToggle, onDragStart }) {
  const node = tree[rel]
  if (!node) return null
  const pad = { paddingLeft: 8 + depth * 13 }

  if (!node.entries.length) {
    if (node.status === 'loading') return <div className="res-msg" style={pad}>读取中…</div>
    if (node.status === 'error') return <div className="res-msg err" style={pad} title={node.error}>读取失败</div>
    return <div className="res-msg" style={pad}>（空）</div>
  }

  return (
    <>
      {node.entries.map((e) => (
        <React.Fragment key={e.rel}>
          {e.type === 'dir' ? (
            <div className="res-row dir" style={pad} title={e.name} onClick={() => onToggle(e.rel)}>
              <Chevron open={expanded.has(e.rel)} />
              <FolderIco />
              <span className="res-name">{e.name}</span>
            </div>
          ) : (
            <div
              className={`res-row file${e.isImage ? ' img' : ''}`}
              style={pad}
              title={e.isImage ? `${e.name}\n拖到画布即可新建数据节点` : `${e.name}\n（不是图片，不能拖）`}
              draggable={!!e.isImage}
              onDragStart={e.isImage ? (ev) => onDragStart(ev, e) : undefined}
            >
              {e.isImage ? (
                <img className="res-thumb" src={api.resourceRawUrl(e.rel, e.mtime)} alt="" loading="lazy" draggable={false} />
              ) : (
                <FileIco />
              )}
              <span className="res-name">{e.name}</span>
              <span className="res-size">{fmtSize(e.size)}</span>
            </div>
          )}
          {e.type === 'dir' && expanded.has(e.rel) ? (
            <Rows rel={e.rel} depth={depth + 1} tree={tree} expanded={expanded} onToggle={onToggle} onDragStart={onDragStart} />
          ) : null}
        </React.Fragment>
      ))}
    </>
  )
}

/*
 * 资源面板：显示「资源目录」的目录结构与文件，可以把其中的图片**拖到画布**上
 * 自动建一个数据节点（拖 = 复制一份进当前画布的图片池，原文件不动）。
 *
 * 读取是**懒加载**的：进入只读根目录一层，展开某个子目录时才请求它那一层 ——
 * 资源目录可能很大，一次性递归会又慢又占内存。
 */
export default function ResourcePanel({ state, applyState, flash, open, onToggle }) {
  const dir = state?.resourceDir || ''
  const hasWs = !!state?.workspace

  const [tree, setTree] = useState({})
  const [expanded, setExpanded] = useState(() => new Set())
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  const load = useCallback(async (rel) => {
    setTree((t) => ({ ...t, [rel]: { status: 'loading', entries: [], error: '' } }))
    try {
      const r = await api.listResourceDir(rel)
      setTree((t) => ({ ...t, [rel]: { status: 'done', entries: r.entries || [], error: '' } }))
    } catch (e) {
      setTree((t) => ({ ...t, [rel]: { status: 'error', entries: [], error: e.message } }))
    }
  }, [])

  // 指定/更换资源目录、或换了工作目录 → 整棵树重置，重新读根目录
  useEffect(() => {
    setTree({})
    setExpanded(new Set())
    setEditing(false)
    if (dir) load('')
  }, [dir, state?.workspace, load])

  const refresh = () => {
    setTree({})
    setExpanded(new Set())
    if (dir) load('')
  }

  const toggleDir = (rel) => {
    if (expanded.has(rel)) {
      const nx = new Set(expanded)
      nx.delete(rel)
      setExpanded(nx)
      return
    }
    setExpanded(new Set(expanded).add(rel))
    const n = tree[rel]
    if (!n || (n.status !== 'done' && n.status !== 'loading')) load(rel)
  }

  const onDragStart = (ev, entry) => {
    ev.dataTransfer.setData(RESOURCE_DRAG_MIME, entry.rel)
    ev.dataTransfer.effectAllowed = 'copy'
  }

  const applyDir = async (path) => {
    try {
      applyState(await api.setResourceDir(path))
      setEditing(false)
    } catch (e) {
      flash(e.message)
    }
  }

  const browse = async () => {
    try {
      const s = await api.pickResourceDir()
      if (!s.cancelled) applyState(s)
    } catch (e) {
      flash(e.message)
    }
  }

  if (!open) return null

  return (
    <aside className="res-panel">
      <div className="res-head">
        <span className="res-title">资源</span>
        {dir ? (
          <button
            className="res-path"
            title={`${dir}\n资源目录只读，拖进画布的是副本\n点击可手动输入路径`}
            onClick={() => {
              setDraft(dir)
              setEditing(true)
            }}
          >
            {dir}
          </button>
        ) : (
          <span className="res-path empty">未指定</span>
        )}
        <span className="spacer" />
        {dir ? (
          <>
            <button className="res-btn" title="刷新" aria-label="刷新资源目录" onClick={refresh}>
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                <path d="M13 8a5 5 0 1 1-1.5-3.6" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
                <path d="M13 2.4V5h-2.6" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            <button className="res-btn" title="更换资源目录" aria-label="更换资源目录" onClick={browse}>
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
                <path d="M2 4.4h3.9l1.3 1.6H14v6H2z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
              </svg>
            </button>
          </>
        ) : null}
        <button className="res-btn" title="收起资源面板" aria-label="收起资源面板" onClick={onToggle}>
          <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <path d="M10 3L5 8l5 5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>

      {!hasWs ? (
        <div className="res-hint">先选择一个工作目录 —— 资源目录跟随工作目录保存。</div>
      ) : !dir || editing ? (
        <div className="res-setup">
          <div className="res-hint">指定一个目录作为资源目录。里面的图片可以拖到画布上新建数据节点。</div>
          <input
            className="res-input"
            type="text"
            autoFocus={editing}
            value={draft}
            placeholder="输入资源目录的完整路径"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') applyDir(draft.trim())
              if (e.key === 'Escape') setEditing(false)
            }}
          />
          <div className="res-setup-foot">
            <button className="res-btn wide" onClick={browse}>浏览…</button>
            <button className="res-btn wide primary" disabled={!draft.trim()} onClick={() => applyDir(draft.trim())}>
              确定
            </button>
            {editing ? <button className="res-btn" onClick={() => setEditing(false)}>取消</button> : null}
          </div>
        </div>
      ) : (
        <div className="res-tree">
          <Rows rel="" depth={0} tree={tree} expanded={expanded} onToggle={toggleDir} onDragStart={onDragStart} />
        </div>
      )}
    </aside>
  )
}
