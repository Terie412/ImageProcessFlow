/*
 * 颜色库：侧栏「颜色」页签的内容（见 DESIGN.md §3.20）。
 *
 * 一个颜色 = **名字 + 值**。值是 `#rrggbb`（不透明，和取色器 / 节点颜色参数同一条口径，
 * 见 color.js）。数据在服务端 —— 文档级字段 `colors`，**跟随工作目录**（图表是项目的资产，
 * 和资源目录同一个理由）。所有画布共用同一份：它是「参考」不是画布内容。
 *
 * 三个写操作（新增 / 编辑 / 删除）都回整包状态，`applyState` 就地把列表换掉，
 * 前端不维护第二份颜色数据。
 *
 * 每行两个复制按钮：**RGB / Hex**。RGB 的格式与取色器里那个 RGB 输入框完全一致
 * （`255, 136, 0`，见 color.js 的 `toRgbText`）—— 复制出来能直接粘回那个框里。
 *
 * 编辑走弹窗（名字 + 取色器）：取色器是画布上那个自研组件（ColorPicker.jsx）。
 * 它内部用 `useViewport()` 量自己在画布里的落点，**不在 ReactFlowProvider 里会直接抛错**，
 * 所以这里套一层**一次性的** provider —— 弹窗里没有平移缩放，视口恒定不动。
 */
import React, { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { ReactFlowProvider } from '@xyflow/react'
import * as api from './api'
import ColorField from './ColorPicker'
import { toRgbText } from './color'

const HEX_RE = /^#[0-9a-f]{6}$/
const NAME_MAX = 60
// 新建时的起始值：白色。不猜「你想要什么颜色」，反正点开色块就能改
const NEW_VALUE = '#ffffff'

/* 复制到剪贴板。优先 `navigator.clipboard`（127.0.0.1 算安全上下文，走的就是这条路）；
   拿不到时退回一个临时 textarea + execCommand —— 老浏览器 / 非安全上下文下还能用。
   返回是否成功，失败由调用方说清楚（静默失败比报错更难受）。 */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch (e) {
    /* 下面兜底 */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '-1000px'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return !!ok
  } catch (e) {
    return false
  }
}

/** 一行颜色：色块（点开编辑）+ 名字 + 值 + 两个复制按钮 + 删除 */
function Row({ c, onEdit, onDelete, onCopy }) {
  return (
    <div className="cl-row" onDoubleClick={() => onEdit(c)}>
      <button
        className="cl-sw"
        style={{ background: c.value }}
        title={`${c.name}\n${c.value} · ${toRgbText(c.value)}\n点击编辑`}
        aria-label={`编辑 ${c.name}`}
        onClick={() => onEdit(c)}
      />
      <span className="cl-name" title={c.name}>
        {c.name}
      </span>
      <span className="cl-val" title={c.value}>
        {c.value}
      </span>
      <button
        className="cl-copy"
        title={`复制 RGB：${toRgbText(c.value)}`}
        onClick={() => onCopy(c, 'rgb')}
      >
        RGB
      </button>
      <button className="cl-copy" title={`复制 Hex：${c.value}`} onClick={() => onCopy(c, 'hex')}>
        HEX
      </button>
      <button className="cl-del" title={`删除「${c.name}」`} aria-label={`删除 ${c.name}`} onClick={() => onDelete(c)}>
        ✕
      </button>
    </div>
  )
}

export default function ColorPanel({ state, applyState, flash }) {
  const colors = state?.colors || []
  const hasWs = !!state?.workspace

  const [edit, setEdit] = useState(null) // { id?, name, value } —— 有 id = 编辑，没有 = 新建
  const [del, setDel] = useState(null) // 待确认删除的那个颜色
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  // 换了工作目录 → 把弹窗都收掉（颜色库整个换了一套，留着旧弹窗会张冠李戴）
  useEffect(() => {
    setEdit(null)
    setDel(null)
    setErr('')
  }, [state?.workspace])

  const startAdd = () => {
    setErr('')
    setEdit({ name: '', value: NEW_VALUE })
  }

  const startEdit = (c) => {
    setErr('')
    setEdit({ id: c.id, name: c.name, value: c.value })
  }

  const submit = async () => {
    if (!edit || busy) return
    const name = edit.name.trim()
    if (!name) return setErr('名字不能是空的')
    if (name.length > NAME_MAX) return setErr(`名字最长 ${NAME_MAX} 个字`)
    // 取色器只产出合法的 6 位 hex，这里是兜底（比如以后有人塞了别的入口）
    if (!HEX_RE.test(edit.value)) return setErr(`颜色值要写成 #RRGGBB（当前是 ${edit.value}）`)

    const id = edit.id
    setBusy(true)
    setErr('')
    try {
      const s = id
        ? await api.updateColor(id, { name, value: edit.value })
        : await api.addColor(name, edit.value)
      applyState(s)
      setEdit(null)
      flash(id ? `已更新「${name}」` : `已加入颜色库：「${name}」`)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  const confirmDelete = async () => {
    if (!del || busy) return
    setBusy(true)
    try {
      applyState(await api.deleteColor(del.id))
      flash(`已删除「${del.name}」`)
      setDel(null)
    } catch (e) {
      flash(e.message)
      setDel(null)
    } finally {
      setBusy(false)
    }
  }

  const onCopy = async (c, kind) => {
    const text = kind === 'rgb' ? toRgbText(c.value) : c.value
    const ok = await copyText(text)
    flash(ok ? `已复制「${c.name}」的 ${kind === 'rgb' ? 'RGB' : 'Hex'}：${text}` : '复制失败：浏览器没给剪贴板权限')
  }

  return (
    <div className="cl-body">
      <div className="res-head">
        <span className="cl-count">{colors.length} 个颜色</span>
        <span className="spacer" />
        <button className="res-btn" title="新建一个颜色" disabled={!hasWs} onClick={startAdd}>
          ＋ 新建
        </button>
      </div>

      {!hasWs ? (
        <div className="res-hint">先选择一个工作目录 —— 颜色库跟随工作目录保存。</div>
      ) : !colors.length ? (
        <div className="res-hint">还没有颜色。点右上角「＋ 新建」记一个。</div>
      ) : (
        <div className="cl-list">
          {colors.map((c) => (
            <Row key={c.id} c={c} onEdit={startEdit} onDelete={setDel} onCopy={onCopy} />
          ))}
        </div>
      )}

      {/* 新建 / 编辑：名字 + 取色器。两者是同一个窗 —— 只在标题上区分 */}
      {edit
        ? createPortal(
            <div className="modal-mask" onMouseDown={() => !busy && setEdit(null)}>
              <div className="modal cl-edit" onMouseDown={(e) => e.stopPropagation()}>
                <h3>{edit.id ? '编辑颜色' : '新建颜色'}</h3>
                {/* 标题下面**不加说明文字**（2026-10-17 按用户要求去掉）：两个输入框自己
                    说得很清楚，值不合法时取色器也会当场报原因。少两行字，窗更矮。 */}
                <input
                  type="text"
                  autoFocus
                  value={edit.name}
                  placeholder="名字"
                  onChange={(e) => setEdit({ ...edit, name: e.target.value })}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') submit()
                    if (e.key === 'Escape') setEdit(null)
                  }}
                />
                <div className="cfg-field cl-field">
                  <span className="cfg-label">颜色</span>
                  {/* 一次性 provider：取色器要读「画布视口」来量浮层落点，
                      这里没有画布，给它一个恒定的就行（见文件头） */}
                  <ReactFlowProvider>
                    <ColorField
                      value={edit.value}
                      label="颜色"
                      /* 取色器浮层底部也列出颜色库 —— 编辑现有颜色时可以一键换成别的 */
                      colors={colors}
                      onCommit={(hex) => setEdit((m) => (m ? { ...m, value: hex } : m))}
                      onTip={setErr}
                    />
                  </ReactFlowProvider>
                  <span className="cl-field-val">{edit.value}</span>
                  <span className="cl-field-rgb">{toRgbText(edit.value)}</span>
                </div>
                {err ? <div className="err">{err}</div> : null}
                <div className="modal-foot">
                  <button onClick={() => setEdit(null)} disabled={busy}>
                    取消
                  </button>
                  <button className="primary" disabled={busy || !edit.name.trim()} onClick={submit}>
                    {busy ? '保存中…' : '保存'}
                  </button>
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}

      {/* 删除确认：颜色库没有回收站，删了就没了 —— 所以问一句 */}
      {del
        ? createPortal(
            <div className="modal-mask" onMouseDown={() => !busy && setDel(null)}>
              <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
                <h3>删除颜色</h3>
                <p>
                  将从颜色库里删掉 <b>{del.name}</b>（{del.value}）。
                  <br />
                  这里没有回收站 —— 删了就没了，想留着就点「取消」。
                </p>
                <div className="modal-foot">
                  <button onClick={() => setDel(null)} disabled={busy}>
                    取消
                  </button>
                  <button className="danger" disabled={busy} onClick={confirmDelete}>
                    {busy ? '删除中…' : '删除'}
                  </button>
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}
    </div>
  )
}
