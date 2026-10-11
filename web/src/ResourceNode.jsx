import React from 'react'
import { Handle, Position } from '@xyflow/react'
import { imageUrl } from './api'

export default function ResourceNode({ id, data, selected }) {
  const {
    name, kind, hasImage, mtime, index, width, height, format, onReplace, onExport,
    minW = 200, minH = 180, onResizeStart,
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
          <img src={imageUrl(name, mtime)} alt={name} draggable={false} />
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
        <button className="tiny nodrag" disabled={!hasImage} onClick={() => onExport(name)}>
          下载
        </button>
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
