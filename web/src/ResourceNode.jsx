import React from 'react'
import { Handle, Position } from '@xyflow/react'
import { imageUrl } from './api'

export default function ResourceNode({ id, data, selected }) {
  const {
    name, kind, hasImage, mtime, index, width, height, format, canvasId,
    // 上次「下载」到的**完整路径**（服务端存在 graph.json 的这个节点上）。
    // 有它就冒出一个「覆盖」按钮 —— 点一下直接盖掉那个文件，不再走另存为。
    lastExportPath,
    onReplace, onExport, onOverwrite,
    // 拖手柄时的下限：由 App.buildNodes 从服务端的 sizeSpec 带下来，这里不留兜底数字
    minW, minH, onResizeStart,
  } = data

  return (
    <div className={`node ${selected ? 'selected' : ''}`}>
      <Handle type="target" position={Position.Left} id="in" isConnectable={false} />
      <div className="node-head">
        <span className={`badge ${kind === 'input' ? 'input' : 'output'}`}>{name}</span>
        <span className="title">{kind === 'input' ? '输入图' : `输出 #${(index ?? 0) + 1}`}</span>
        <span className="spacer" />
        {hasImage && (width || format) ? (
          <span className="meta-right">
            {width && height ? `${width}×${height}` : null}
            {format ? ` · ${format}` : null}
          </span>
        ) : null}
      </div>

      <div className="thumb">
        {hasImage ? (
          <img src={imageUrl(canvasId, name, mtime)} alt={name} draggable={false} />
        ) : (
          <div className="ph">（无图片）</div>
        )}
      </div>

      <div className="node-foot">
        {kind === 'input' && (
          <button className="tiny nodrag" onClick={() => onReplace(id)}>
            替换图片
          </button>
        )}
        <button className="tiny nodrag" disabled={!hasImage} onClick={() => onExport(id, name)}>
          下载
        </button>
        {/* 只对「下载过」的节点显示；悬停能看到它记住的完整路径 */}
        {lastExportPath ? (
          <button
            className="tiny nodrag"
            title={`覆盖到：${lastExportPath}`}
            disabled={!hasImage}
            onClick={() => onOverwrite(id, name)}
          >
            覆盖
          </button>
        ) : null}
      </div>

      <Handle type="source" position={Position.Right} id="out" />

      <div
        className="resize-handle nodrag"
        title="拖动改变节点大小"
        onMouseDown={(e) => onResizeStart(e, id, minW, minH)}
      />
    </div>
  )
}
