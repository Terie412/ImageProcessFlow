/* 颜色的解析与换算：取色器的两个文本框、色域面板、色相条共用这一份。
 *
 * 为什么抽出来：`#RRGGBB` 和 `R, G, B, A` 两种写法**互相同步**，解析一散就必然出现
 * 「hex 框认、RGBA 框不认」这种不一致；而且失败时要能**说清为什么失败**，
 * 所以解析函数一律返回 `{ ok: false, err }` 而不是 null / 抛异常。
 *
 * 一条口径（2026-10-15 定）：颜色参数存的是 `#RRGGBB`，**不支持透明**。
 * 所以 RGBA 写法里的 A（以及 8 位 hex 的后两位）只做校验、**不参与存储** ——
 * 输入 A≠255 时返回 `alphaIgnored`，由调用方决定要不要提一句（别静默吞掉）。
 */

/** `[r,g,b]` → `#rrggbb`（小写、带 #） */
export const rgbToHex = ([r, g, b]) =>
  '#' + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('')

/** `#rrggbb` → `[r,g,b]`；不合法返回 null（这里只用于读回自己存的值，不做用户输入校验） */
export function hexToRgb(hex) {
  const s = String(hex || '').trim().replace(/^#+/, '')
  if (!/^[0-9a-fA-F]{6}$/.test(s)) return null
  return [parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)]
}

/** 存下来的值 → 文本框里那串 `r, g, b, 255` */
export const toRgbaText = (hex) => {
  const rgb = hexToRgb(hex)
  return rgb ? `${rgb[0]}, ${rgb[1]}, ${rgb[2]}, 255` : ''
}

/**
 * 解析十六进制写法。允许：
 *   - 前面带不带 `#`、带几个都行；大小写随意
 *   - 3 位（`#abc` → `#aabbcc`）、6 位、8 位（**后两位当 alpha，只校验不存**）
 * 返回 `{ ok: true, rgb, alphaIgnored }` 或 `{ ok: false, err }`（err 是给用户看的一句话）。
 */
export function parseHex(text) {
  const raw = String(text ?? '').trim()
  if (!raw) return { ok: false, err: '十六进制不能是空的' }
  const s = raw.replace(/^#+/, '').toLowerCase()
  if (/[^0-9a-f]/.test(s)) {
    const bad = s.match(/[^0-9a-f]/)[0]
    return { ok: false, err: `十六进制里有不是 0-9a-f 的字符「${bad}」（收到 ${raw}）` }
  }
  if (s.length === 3) {
    const [r, g, b] = [...s].map((c) => parseInt(c + c, 16))
    return { ok: true, rgb: [r, g, b], alphaIgnored: false }
  }
  if (s.length === 6) {
    return {
      ok: true,
      rgb: [parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)],
      alphaIgnored: false,
    }
  }
  if (s.length === 8) {
    return {
      ok: true,
      rgb: [parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)],
      // 后两位是 alpha：这个参数存不下它，所以只在它不是 ff 时提醒一句
      alphaIgnored: s.slice(6, 8) !== 'ff',
    }
  }
  return {
    ok: false,
    err: `十六进制要 3、6 或 8 位（收到 ${s.length} 位：${raw}）`,
  }
}

/**
 * 解析 `R, G, B[, A]` 写法。
 *   - 逗号分隔；**先去掉空项**（`17,,17,17` / `17, 17, 17,` 这类手抖都算数）
 *   - 3 或 4 项；4 项时 A 只校验不存（同上，返回 `alphaIgnored`）
 *   - 每项 0~255，可以带小数（四舍五入）
 */
export function parseRgba(text) {
  const raw = String(text ?? '').trim()
  if (!raw) return { ok: false, err: 'RGBA 不能是空的' }
  const parts = raw.split(',').map((s) => s.trim()).filter((s) => s !== '') // 空项直接丢掉
  if (parts.length !== 3 && parts.length !== 4) {
    return {
      ok: false,
      err: `RGBA 要 3 个（R, G, B）或 4 个数（R, G, B, A），逗号分隔 —— 收到 ${parts.length} 个：${raw}`,
    }
  }
  const names = ['R', 'G', 'B', 'A']
  const nums = []
  for (let i = 0; i < parts.length; i++) {
    if (!/^-?\d+(\.\d+)?$/.test(parts[i])) {
      return { ok: false, err: `${names[i]} 不是数字：「${parts[i]}」（收到 ${raw}）` }
    }
    const v = Number(parts[i])
    if (v < 0 || v > 255) {
      return { ok: false, err: `${names[i]} 要在 0~255 之间，收到 ${parts[i]}` }
    }
    nums.push(Math.round(v))
  }
  return {
    ok: true,
    rgb: nums.slice(0, 3),
    alphaIgnored: nums.length === 4 && nums[3] !== 255,
  }
}

/** `[r,g,b]`（0~255）→ `{h: 0~360, s: 0~1, v: 0~1}` */
export function rgbToHsv([r, g, b]) {
  const R = r / 255
  const G = g / 255
  const B = b / 255
  const max = Math.max(R, G, B)
  const min = Math.min(R, G, B)
  const d = max - min
  let h = 0
  if (d > 0) {
    if (max === R) h = ((G - B) / d) % 6
    else if (max === G) h = (B - R) / d + 2
    else h = (R - G) / d + 4
    h *= 60
    if (h < 0) h += 360
  }
  return { h, s: max > 0 ? d / max : 0, v: max }
}

/** `{h,s,v}` → `[r,g,b]`（0~255，四舍五入） */
export function hsvToRgb({ h, s, v }) {
  const c = v * s
  const hh = (((h % 360) + 360) % 360) / 60
  const x = c * (1 - Math.abs((hh % 2) - 1))
  const m = v - c
  const seg = [[c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x]][Math.floor(hh) % 6]
  return seg.map((k) => Math.round((k + m) * 255))
}
