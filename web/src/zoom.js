/**
 * 滚轮缩放 + 中键平移的公共小工具 —— 区域编辑器和大图查看器共用。
 *
 * 缩放约定：`anchorZoom` 返回一个小函数，在**重新排版之后**（useLayoutEffect 里）调用。
 * 它是按「光标指到的那一点在内容里的相对位置」来对账的：
 * 测出排版后的新位置，再把滚动位置挪回去，于是缩放前后光标底下的像素是同一个。
 * 这么算不依赖内容是不是居中、也不依赖内容从哪个原点开始。
 */

export const MIN_ZOOM = 0.15
export const MAX_ZOOM = 12

export const clampZoom = (z) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z))

/** 滚轮 deltaY → 倍率：按指数走，手感均匀（上滚放大、下滚缩小）。 */
export const wheelZoom = (deltaY) => Math.exp(-deltaY * 0.0015)

/**
 * 以光标为锚点缩放。
 * @param stage  可滚动的容器
 * @param target 实际被缩放的内容元素（画布 / 图片）
 */
export function anchorZoom(stage, target, clientX, clientY) {
  if (!stage || !target) return () => {}
  const r0 = target.getBoundingClientRect()
  if (r0.width < 1 || r0.height < 1) return () => {}
  const fx = (clientX - r0.left) / r0.width
  const fy = (clientY - r0.top) / r0.height
  return () => {
    const r1 = target.getBoundingClientRect()
    if (r1.width < 1) return
    // 现在光标在内容里的偏移是 (clientX - r1.left)，要让它变成 fx * r1.width；
    // 往前滚一格内容就左移一格，所以滚动量 = 目标偏移 - 当前偏移。
    stage.scrollLeft += r1.left + fx * r1.width - clientX
    stage.scrollTop += r1.top + fy * r1.height - clientY
  }
}

/**
 * 鼠标中键拖动平移：直接改容器的 scrollLeft/scrollTop。
 * 返回解绑函数。拖动期间容器会带上 `panning` 类（用来换光标）。
 */
export function enableMiddleDragPan(stage) {
  if (!stage) return () => {}
  let on = false
  let sx = 0
  let sy = 0
  let sl = 0
  let st = 0

  const move = (e) => {
    if (!on) return
    e.preventDefault()
    stage.scrollLeft = sl - (e.clientX - sx)
    stage.scrollTop = st - (e.clientY - sy)
  }
  const up = () => {
    if (!on) return
    on = false
    stage.classList.remove('panning')
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
  }
  const down = (e) => {
    if (e.button !== 1) return // 只认中键
    e.preventDefault() // 顺手压掉 Windows 的中键自动滚动
    e.stopPropagation()
    on = true
    sx = e.clientX
    sy = e.clientY
    sl = stage.scrollLeft
    st = stage.scrollTop
    stage.classList.add('panning')
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  stage.addEventListener('mousedown', down)
  return () => {
    stage.removeEventListener('mousedown', down)
    up()
  }
}
