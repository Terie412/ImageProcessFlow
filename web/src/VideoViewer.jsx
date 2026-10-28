import React, { useEffect, useRef } from 'react'

/* 视频大图（双击视频数据节点打开）。
 *
 * 为什么**不复用** ImageViewer：那边一半的代码在干视频干不了的事 —— 把画布画到
 * 离屏 canvas 上读像素、拖框丈量尺寸、滚轮缩放 / 中键平移。给视频套一层「这些都是
 * 空操作」的分支，比另写一个 `<video controls>` 更绕。所以这里只有：一条顶栏 + 一个
 * 原生播放器。交互交给浏览器自带控件（进度 / 音量 / 全屏都在，不用自己造）。
 *
 * 播放地址就是媒体文件本身（`/api/images/<画布>/<名>`）。Starlette 的 FileResponse
 * 支持 Range 请求，所以拖进度条是真的 seek，不是重新下载整个文件。
 */
export default function VideoViewer({
  src,
  badge,
  badgeKind = null,
  subtitle = null,
  width = null,
  height = null,
  duration = null,
  fps = null,
  onClose,
}) {
  // onClose 每次渲染都是新函数，用 ref 兜住，免得 Esc 的监听反复重挂
  const closeRef = useRef(onClose)
  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') closeRef.current?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const meta = [
    width && height ? `${width} × ${height}` : null,
    typeof duration === 'number' && duration > 0 ? `${duration.toFixed(2)}s` : null,
    fps ? `${fps} fps` : null,
  ].filter(Boolean)

  return (
    <div className="viewer-mask">
      <div className="viewer-bar">
        <span className={badgeKind ? `badge ${badgeKind}` : 'badge'}>{badge}</span>
        {subtitle ? <span className="viewer-name" title={subtitle}>{subtitle}</span> : null}
        <span className="spacer" />
        <span className="viewer-meta">{meta.join(' · ')}</span>
        <button className="tiny" onClick={() => closeRef.current?.()}>
          关闭 (Esc)
        </button>
      </div>
      <div className="viewer-stage fit viewer-video">
        <video src={src} controls autoPlay playsInline preload="auto" />
      </div>
    </div>
  )
}
