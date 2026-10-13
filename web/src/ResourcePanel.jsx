import React, { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import * as api from './api'
import ImageViewer from './ImageViewer'

/* 拖拽资源文件用的自定义 MIME。
 *
 * 画布的 onDrop 靠它区分「从资源面板拖出来的东西」和「从操作系统拖进来的文件」：
 * 带上这个类型时 dataTransfer.files 是空的，所以两条路不会同时命中。
 * 值就是相对资源目录的路径 —— 落点时服务端据此直接复制，不经浏览器上传。 */
export const RESOURCE_DRAG_MIME = 'application/x-ipf-resource'

/* 面板宽度可以拖右边框改，纯界面偏好 —— 记 localStorage，不进 graph.json */
const RES_W = { min: 180, max: 640, def: 262 }
const clampW = (v) => Math.max(RES_W.min, Math.min(RES_W.max, Math.round(v)))

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
function Rows({ rel, depth, tree, expanded, onToggle, onDragStart, onMenu, onPreview }) {
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
            <div
              className="res-row dir"
              style={pad}
              title={e.name}
              onClick={() => onToggle(e.rel)}
              onContextMenu={(ev) => onMenu(ev, { type: 'dir', rel: e.rel, name: e.name })}
            >
              <Chevron open={expanded.has(e.rel)} />
              <FolderIco />
              <span className="res-name">{e.name}</span>
            </div>
          ) : (
            <div
              className={`res-row file${e.isImage ? ' img' : ''}`}
              style={pad}
              title={e.isImage ? `${e.name}\n拖到画布即新建数据节点 · 双击预览` : `${e.name}\n（不是图片，不能拖）`}
              draggable={!!e.isImage}
              onDragStart={e.isImage ? (ev) => onDragStart(ev, e) : undefined}
              onDoubleClick={e.isImage ? () => onPreview(e) : undefined}
              onContextMenu={(ev) => onMenu(ev, { type: 'file', rel: e.rel, name: e.name })}
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
            <Rows
              rel={e.rel}
              depth={depth + 1}
              tree={tree}
              expanded={expanded}
              onToggle={onToggle}
              onDragStart={onDragStart}
              onMenu={onMenu}
              onPreview={onPreview}
            />
          ) : null}
        </React.Fragment>
      ))}
    </>
  )
}

/*
 * 资源面板：显示「资源目录」的目录结构与文件。
 *
 * - 读取是**懒加载**的：进入只读根目录一层，展开某个子目录时才请求它那一层。
 * - 图片可以**拖到画布**上自动建数据节点（拖 = 复制一份进当前画布的图片池）。
 * - 右键：空白处 / 目录 → 新建目录；目录与文件 → 重命名、删除。
 *
 * 资源目录现在**可写**了（新建目录 / 改名 / 删除），但仍然只动它自己里面的东西，
 * 而且**不创建文件**、**删除走回收站**（见 DESIGN.md §3.15）。
 */
export default function ResourcePanel({ state, applyState, flash, open, onToggle }) {
  const dir = state?.resourceDir || ''
  const hasWs = !!state?.workspace

  const [tree, setTree] = useState({})
  const [expanded, setExpanded] = useState(() => new Set())
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')

  const [menu, setMenu] = useState(null)   // { x, y, target:{type,rel,name} }
  const [dlg, setDlg] = useState(null)     // { kind:'mkdir'|'rename'|'delete', … }
  const [dlgErr, setDlgErr] = useState('')
  const [busyAct, setBusyAct] = useState(false)
  // 双击图片文件打开的大图查看器（与数据节点共用 ImageViewer）。
  // info 是服务端读文件头得到的信息，拿不到也不影响看图 —— 尺寸交给图片自身兜底
  const [preview, setPreview] = useState(null) // { rel, name, mtime, info }

  // 面板宽度（可拖右边框改，记 localStorage）
  const [width, setWidth] = useState(() => {
    try {
      const v = Number(localStorage.getItem('ipf.resWidth'))
      return Number.isFinite(v) && v > 0 ? clampW(v) : RES_W.def
    } catch (e) {
      return RES_W.def
    }
  })
  const widthRef = useRef(width)
  useEffect(() => {
    widthRef.current = width
  }, [width])

  const saveWidth = (w) => {
    try {
      localStorage.setItem('ipf.resWidth', String(w))
    } catch (e) {
      /* 隐私模式下写不了，忽略 */
    }
  }

  // 拖右边缘改宽度：mousemove 只改 state（不落盘），松手才写 localStorage
  const startResize = (e) => {
    if (e.button !== 0) return
    e.preventDefault()
    let latest = widthRef.current
    let x0 = e.clientX
    const w0 = latest
    document.body.classList.add('col-resizing')
    const move = (ev) => {
      // 上限还要看窗口：别把画布挤得没地方
      const max = Math.max(RES_W.min, Math.min(RES_W.max, window.innerWidth - 240))
      latest = Math.max(RES_W.min, Math.min(max, Math.round(w0 + (ev.clientX - x0))))
      setWidth(latest)
    }
    const up = () => {
      document.body.classList.remove('col-resizing')
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      saveWidth(latest)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const resetWidth = () => {
    setWidth(RES_W.def)
    saveWidth(RES_W.def)
  }

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
    setMenu(null)
    setDlg(null)
    setPreview(null)
    if (dir) load('')
  }, [dir, state?.workspace, load])

  // 菜单开着时：点别处 / 按 Esc 就关掉
  useEffect(() => {
    if (!menu) return undefined
    const close = () => setMenu(null)
    const onKey = (e) => e.key === 'Escape' && setMenu(null)
    document.addEventListener('mousedown', close)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', close)
      window.removeEventListener('keydown', onKey)
    }
  }, [menu])

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

  // 双击图片文件 → 大图查看器。先把图开出来（框会立刻显示），图片信息随后补上
  const openPreview = async (entry) => {
    setPreview({ rel: entry.rel, name: entry.name, mtime: entry.mtime, info: null })
    try {
      const info = await api.resourceInfo(entry.rel)
      // 这期间用户可能已经关掉、或又双击了别的图 —— 只认当前这一张
      setPreview((p) => (p && p.rel === entry.rel ? { ...p, info } : p))
    } catch (e) {
      /* 读不到信息不挡看图：尺寸用图片自身兜底、像素读数只显示 RGB */
    }
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

  /* ---------------- 右键菜单 ---------------- */
  // 菜单挂到 body 上（Portal）、用**视口坐标**定位 —— 免得被面板或画布的定位上下文困住
  const openMenu = (ev, target) => {
    ev.preventDefault()
    ev.stopPropagation()
    setMenu({
      x: Math.max(4, Math.min(ev.clientX, window.innerWidth - 150)),
      y: Math.min(ev.clientY, window.innerHeight - 8),
      target,
    })
  }

  /* ---------------- 写操作 ---------------- */
  // 写完统一回「这一层的新列表」，就地把树里那格换掉，并把它展开（好让新项看得见）
  const applyListing = (res) => {
    const rel = res?.path ?? ''
    setTree((t) => ({ ...t, [rel]: { status: 'done', entries: res.entries || [], error: '' } }))
    setExpanded((prev) => new Set(prev).add(rel))
  }

  // 目录被改名 / 删掉后，它下面那些缓存（tree 与展开态）全失效 —— 整枝摘掉，下次展开再读
  const purgeBelow = (rel) => {
    const prefix = `${rel}/`
    setTree((t) => {
      const n = { ...t }
      for (const k of Object.keys(n)) {
        if (k === rel || k.startsWith(prefix)) delete n[k]
      }
      return n
    })
    setExpanded((prev) => {
      const n = new Set()
      for (const k of prev) {
        if (k !== rel && !k.startsWith(prefix)) n.add(k)
      }
      return n
    })
  }

  const submitDlg = async () => {
    if (!dlg || busyAct) return
    setBusyAct(true)
    setDlgErr('')
    try {
      if (dlg.kind === 'mkdir') {
        applyListing(await api.mkdirResource(dlg.parent, draft))
        flash(`已新建目录 ${draft.trim()}`)
      } else if (dlg.kind === 'rename') {
        applyListing(await api.renameResource(dlg.path, draft))
        purgeBelow(dlg.path)
        flash(`已重命名为 ${draft.trim()}`)
      } else {
        const res = await api.deleteResource(dlg.path)
        if (dlg.isDir) purgeBelow(dlg.path)
        applyListing(res)
        flash(res.toTrash ? `已移入回收站：${dlg.name}` : `已永久删除：${dlg.name}`)
      }
      setDlg(null)
    } catch (e) {
      setDlgErr(e.message)
    } finally {
      setBusyAct(false)
    }
  }

  if (!open) return null

  return (
    <aside className="res-panel" style={{ width }}>
      <div className="res-head">
        <span className="res-title">资源</span>
        {dir ? (
          <button
            className="res-path"
            title={`${dir}\n可在这里新建目录 / 改名 / 删除（删除进回收站）\n点击可手动输入路径`}
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
        /* 空白处右键 = 在资源目录根下新建目录；行上的右键会被行自己接走，冒泡不到这里 */
        <div className="res-tree" onContextMenu={(ev) => openMenu(ev, { type: 'blank' })}>
          <Rows
            rel=""
            depth={0}
            tree={tree}
            expanded={expanded}
            onToggle={toggleDir}
            onDragStart={onDragStart}
            onMenu={openMenu}
            onPreview={openPreview}
          />
        </div>
      )}

      {menu
        ? createPortal(
            <div
              className="ctx-menu res-menu"
              style={{ left: menu.x, top: menu.y }}
              onMouseDown={(e) => e.stopPropagation()}
            >
              <div className="ctx-title">{menu.target.type === 'blank' ? '资源目录根' : menu.target.name}</div>
              {menu.target.type !== 'file' ? (
                <button
                  className="ctx-item"
                  onClick={() => {
                    const parent = menu.target.type === 'dir' ? menu.target.rel : ''
                    setMenu(null)
                    setDlgErr('')
                    setDraft('')
                    setDlg({ kind: 'mkdir', parent })
                  }}
                >
                  新建目录
                </button>
              ) : null}
              {menu.target.type !== 'blank' ? (
                <>
                  <div className="ctx-sep" />
                  <button
                    className="ctx-item"
                    onClick={() => {
                      const t = menu.target
                      setMenu(null)
                      setDlgErr('')
                      setDraft(t.name)
                      setDlg({ kind: 'rename', path: t.rel, name: t.name })
                    }}
                  >
                    重命名
                  </button>
                  <button
                    className="ctx-item danger"
                    onClick={() => {
                      const t = menu.target
                      setMenu(null)
                      setDlgErr('')
                      setDlg({ kind: 'delete', path: t.rel, name: t.name, isDir: t.type === 'dir' })
                    }}
                  >
                    删除
                  </button>
                </>
              ) : null}
            </div>,
            document.body,
          )
        : null}

      {dlg
        ? createPortal(
            <div className="modal-mask" onMouseDown={() => !busyAct && setDlg(null)}>
              <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
                {dlg.kind === 'delete' ? (
                  <>
                    <h3>删除{dlg.isDir ? '目录' : '文件'}</h3>
                    <p>
                      将删除 <b>{dlg.name}</b>
                      {dlg.isDir ? '（连同它里面的所有内容）' : ''}。
                      <br />
                      会移入<b>回收站</b>，之后还能找回来。
                    </p>
                  </>
                ) : (
                  <>
                    <h3>{dlg.kind === 'mkdir' ? '新建目录' : '重命名'}</h3>
                    <p>
                      {dlg.kind === 'mkdir'
                        ? `在「${dlg.parent || '资源目录根'}」下新建一个目录。`
                        : '只能改名，不会移到别的目录。'}
                    </p>
                    <input
                      type="text"
                      autoFocus
                      value={draft}
                      placeholder="名字"
                      onFocus={(e) => e.target.select()}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') submitDlg()
                        if (e.key === 'Escape') setDlg(null)
                      }}
                    />
                  </>
                )}
                {dlgErr ? <div className="err">{dlgErr}</div> : null}
                <div className="modal-foot">
                  <button onClick={() => setDlg(null)} disabled={busyAct}>取消</button>
                  <button
                    className={dlg.kind === 'delete' ? 'danger' : 'primary'}
                    disabled={busyAct || (dlg.kind !== 'delete' && !draft.trim())}
                    onClick={submitDlg}
                  >
                    {busyAct ? '处理中…' : dlg.kind === 'delete' ? '删除' : '确定'}
                  </button>
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}

      {/* 双击图片文件打开的大图查看器 —— 与数据节点双击是同一个组件 */}
      {preview
        ? createPortal(
            <ImageViewer
              src={api.resourceRawUrl(preview.rel, preview.mtime)}
              badge={preview.name}
              subtitle={
                preview.rel.includes('/')
                  ? preview.rel.slice(0, preview.rel.lastIndexOf('/'))
                  : '资源目录根'
              }
              width={preview.info?.width}
              height={preview.info?.height}
              format={preview.info?.format}
              alpha={preview.info?.alpha}
              onClose={() => setPreview(null)}
            />,
            document.body,
          )
        : null}

      {/* 右边缘：拖动改宽度（不可见，只靠光标提示），双击恢复默认 */}
      <div
        className="res-resizer"
        title="拖动调整面板宽度（双击恢复默认）"
        onMouseDown={startResize}
        onDoubleClick={resetWidth}
      />
    </aside>
  )
}
