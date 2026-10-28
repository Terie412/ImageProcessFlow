import React from 'react'
import { Handle, Position } from '@xyflow/react'
import { imageUrl, posterUrl } from './api'

export default function ResourceNode({ id, data, selected }) {
  const {
    name, kind, hasImage, mtime, index, width, height, format, canvasId,
    // 媒体类型：'image'（默认，老数据没有这个字段）或 'video'。
    // 视频节点显示海报（服务端抽的首帧 jpg）+ 一个播放角标，元信息里多一个时长。
    media = 'image',
    posterMtime = 0,
    duration = null,
    // 上次「下载」到的**完整路径**（服务端存在 graph.json 的这个节点上）。
    // 有它就冒出一个「覆盖」按钮 —— 点一下直接盖掉那个文件，不再走另存为。
    lastExportPath,
    onReplace, onExport, onOverwrite,
    // 拖手柄时的下限：由 App.buildNodes 从服务端的 sizeSpec 带下来，这里不留兜底数字
    minW, minH, onResizeStart,
  } = data

  const isVideo = media === 'video'

  return (
    <div className={`node ${selected ? 'selected' : ''}`}>
      <Handle type="target" position={Position.Left} id="in" isConnectable={false} />
      <div className="node-head">
        <span className={`badge ${kind === 'input' ? 'input' : 'output'}`}>{name}</span>
        <span className="title">
          {kind === 'input' ? (isVideo ? '输入视频' : '输入图') : `输出 #${(index ?? 0) + 1}`}
        </span>
        <span className="spacer" />
        {hasImage && (width || format) ? (
          <span className="meta-right">
            {width && height ? `${width}×${height}` : null}
            {/* 视频多给一个时长：长度是这类节点最要紧的信息，格式（MP4）反而没什么可看 */}
            {isVideo
              ? duration
                ? ` · ${duration.toFixed(1)}s`
                : null
              : format
                ? ` · ${format}`
                : null}
          </span>
        ) : null}
      </div>

      <div className="thumb">
        {hasImage ? (
          isVideo ? (
            <div className="video-shot">
              {/* 海报是服务端拿 ffmpeg 抽的首帧（`posters/<短名>.jpg`）：
                  不直接用 <video> 当缩略图 —— 一个画布里几十个视频节点同时解码会卡。
                  缓存键带上 mtime，重跑出新视频会立刻换图。 */}
              <img src={posterUrl(canvasId, name, mtime || posterMtime)} alt={name} draggable={false} />
              <span className="video-badge" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="15" height="15">
                  <path d="M8 5.5v13l11-6.5-11-6.5z" fill="currentColor" />
                </svg>
              </span>
            </div>
          ) : (
            <img src={imageUrl(canvasId, name, mtime)} alt={name} draggable={false} />
          )
        ) : (
          <div className="ph">{isVideo ? '（无视频）' : '（无图片）'}</div>
        )}
      </div>

      <div className="node-foot">
        {kind === 'input' && (
          <button className="tiny nodrag" onClick={() => onReplace(id)}>
            {isVideo ? '替换视频' : '替换图片'}
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
