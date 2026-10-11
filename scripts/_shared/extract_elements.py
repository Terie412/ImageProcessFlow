#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
extract_elements.py -- 通用「元素区域」自动解析 / 切图脚本

目标: 给一张"元素是分离的"图片（典型为透明底 PNG，背景也可能只有 alpha=1、2
      这类"几乎透明"；也兼容纯色底的不透明图），在**不预先知道元素个数**的前提下
      自动切出每个元素的区域。

两种互补策略（--method）:

  gutter  「沟槽切分」——递归地按"整行/整列全是背景"的空白沟槽把图切开。
           · 优点: 非常稳；只切本来就断开的区域，绝不会切穿一个连通元素
           · 适合: 图标表(sprite sheet)、底部导航栏、网格卡片等排列规整的图
  cluster 「间距聚类」——连通域 + 按外接框间距做单链接聚类，
           阈值取最小生成树边权的"最大跳变"处（自动，无需你给个数）。
           · 优点: 不要求元素对齐；适合散乱排布
           · 缺点: 元素间距与元素内部碎块间距接近时（密集网格）会拿不准
  auto    （默认）先用 gutter，若能切出 >=2 个区域就用它，否则退回 cluster

判定前景用的是什么?（会自动选择，无需你指定）
  1) 先看图像四周边缘，估计"背景"的颜色与 alpha
  2) 若背景是（近似）透明 —— 只用 alpha 通道判定。
     注意: 透明像素底下那层 RGB 是垃圾数据(可能是任意颜色)，绝不能用它判色！
  3) 若背景不透明 —— 再用"与背景色的差异"判定，阈值按背景噪声自动放大
  4) 若元素贴边导致边缘不统一 —— 退回全局主色方案

输出: 每个元素的紧致外接框 + 标注图 + JSON + 可选 RGBA 单品裁剪

纯 PIL + 标准库，无需 numpy / opencv。Python 3.9+

用法:
  python extract_elements.py B.png --crops
  python extract_elements.py sheet.png --method gutter --min-gutter 4
  python extract_elements.py x.png --method cluster --max-gap 40 --debug
"""

import argparse
import json
import os
import statistics

from PIL import Image, ImageChops, ImageDraw, ImageFont


# ============================ 背景估计 & 掩膜 ============================

def estimate_background(im, alpha_thr, thr_std=10.0):
    """从四边环带估计背景。返回 dict:
       bg        (r,g,b,a) 中位数
       uniform   边缘是否基本同一种背景
       opaque    背景是否"不透明"(=可以用颜色差判定前景)
       rgb_std   RGB 三通道背景噪声的标准差最大值
    """
    W, H = im.size
    b = max(2, int(min(W, H) * 0.012))
    strips = [im.crop((0, 0, W, b)), im.crop((0, H - b, W, H)),
              im.crop((0, 0, b, H)), im.crop((W - b, 0, W, H))]
    buf = bytearray()
    for s in strips:
        buf += s.tobytes()
    if len(buf) < 4:
        return {"bg": None, "uniform": False, "opaque": False, "rgb_std": 0.0}

    chans = [int(statistics.median(buf[c::4])) for c in range(4)]
    stds = [statistics.pstdev(buf[c::4]) for c in range(4)]
    ba = chans[3]
    avals = buf[3::4]
    frac_close = (sum(1 for v in avals if abs(v - ba) <= alpha_thr) / float(len(avals))
                  if avals else 0.0)
    return {"bg": tuple(chans),
            "uniform": frac_close >= 0.9,
            "opaque": ba >= 128,          # 背景够不透明，颜色才可信
            "rgb_std": max(stds[:3])}


def otsu_threshold(hist, lo=24, hi=200):
    """对灰度直方图做 Otsu 自动阈值（夹在 [lo,hi] 内），比按噪声推断稳得多。"""
    total = sum(hist)
    if total == 0:
        return lo
    sum_all = sum(i * hist[i] for i in range(256))
    sum_b = w_b = 0
    best, thr = -1.0, lo
    for i in range(256):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * hist[i]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best:
            best, thr = var, i
    return max(lo, min(thr, hi))


def build_mask(im, color_thr, alpha_thr):
    """返回 (mask L 图, 说明文字)"""
    W, H = im.size
    R, G, B, A = im.split()
    info = estimate_background(im, alpha_thr)
    bg, uniform, opaque, rgb_std = (info["bg"], info["uniform"],
                                    info["opaque"], info["rgb_std"])

    def diff(ch, v):
        return ImageChops.difference(ch, Image.new("L", (W, H), v))

    def thr_mask(d, t):
        return d.point(lambda v: 255 if v > t else 0)

    if uniform and bg is not None:
        br, bgc, bb, ba = bg
        m = thr_mask(diff(A, ba), alpha_thr)                 # alpha 差：总是可信
        desc = "背景 RGBA=%s" % (bg,)
        if opaque:
            # 背景不透明 -> 用颜色差；阈值用 Otsu 自动定（抗背景渐变/纹理）
            dc = ImageChops.lighter(ImageChops.lighter(diff(R, br), diff(G, bgc)),
                                    diff(B, bb))
            t = otsu_threshold(dc.histogram(), max(color_thr, 1))
            m = ImageChops.lighter(m, thr_mask(dc, t))
            desc += " + 颜色差(Otsu阈值%d)" % t
        else:
            desc += " (背景透明/半透明 -> 只用 alpha)"
        return m, desc

    # 边缘不统一：优先 alpha
    a = A.histogram()
    total = W * H
    if sum(a[:250]) / float(total) > 0.01:
        return thr_mask(A, alpha_thr), "边缘不统一 -> 只用 alpha 通道"

    # 全不透明且边缘不统一：用全局主色 + Otsu
    small = im.convert("RGB").resize((max(1, W // 4), max(1, H // 4)))
    gc = sorted(small.getcolors(small.size[0] * small.size[1]), reverse=True)[0][1]
    dc = ImageChops.lighter(ImageChops.lighter(diff(R, gc[0]), diff(G, gc[1])),
                            diff(B, gc[2]))
    t = otsu_threshold(dc.histogram(), max(color_thr, 1))
    return thr_mask(dc, t), "不透明且边缘不统一 -> 全局主色 %s + Otsu(%d)" % (gc, t)


# ============================ 游程连通域 ============================

def _find(p, x):
    while p[x] != x:
        p[x] = p[p[x]]
        x = p[x]
    return x


def _union(p, a, b):
    ra, rb = _find(p, a), _find(p, b)
    if ra == rb:
        return ra
    if ra < rb:
        p[rb] = ra
        return ra
    p[ra] = rb
    return rb


def connected_components(mb, W, H, min_area):
    """mb: 长度 W*H 的 bytes, 前景=255。返回 [(x0,y0,x1,y1,area), ...]"""
    parent, run_bbox, prev = [], {}, []
    ff, zz = b"\xff", b"\x00"
    for y in range(H):
        base = y * W
        row = mb[base:base + W]
        runs, i = [], row.find(ff)
        while i != -1:
            j = row.find(zz, i)
            if j == -1:
                j = W
            runs.append((i, j - 1))
            i = row.find(ff, j)
        if not runs:
            prev = []
            continue
        labeled = []
        for (x0, x1) in runs:
            lab = -1
            for (px0, px1, plab) in prev:
                if px0 > x1 + 1:
                    break
                if px1 < x0 - 1:
                    continue
                r = _find(parent, plab)
                lab = r if lab == -1 else _find(parent, _union(parent, lab, r))
            if lab == -1:
                lab = len(parent)
                parent.append(lab)
            labeled.append((x0, x1, lab))
            b = run_bbox.get(lab)
            if b is None:
                run_bbox[lab] = [x0, y, x1, y, x1 - x0 + 1]
            else:
                b[0] = min(b[0], x0); b[2] = max(b[2], x1)
                b[3] = y; b[4] += x1 - x0 + 1
        prev = labeled

    comps = {}
    for k, b in run_bbox.items():
        r = _find(parent, k)
        c = comps.get(r)
        if c is None:
            comps[r] = b[:]
        else:
            c[0] = min(c[0], b[0]); c[1] = min(c[1], b[1])
            c[2] = max(c[2], b[2]); c[3] = max(c[3], b[3]); c[4] += b[4]
    return [c for c in comps.values() if c[4] >= min_area]


# ============================ 沟槽切分 ============================

def _split_by_zeros(prof, min_gutter):
    """在 profile 里找"内部"连续 0 段(长度>=min_gutter)作为沟槽, 返回各非空段区间。"""
    n = len(prof)
    cuts, i = [], 0
    while i < n:
        if prof[i] == 0:
            j = i
            while j < n and prof[j] == 0:
                j += 1
            if i > 0 and j < n and (j - i) >= min_gutter:
                cuts.append((i, j - 1))
            i = j
        else:
            i += 1
    if not cuts:
        return []
    bands, prev = [], 0
    for (a, b) in cuts:
        if a - 1 >= prev:
            bands.append((prev, a - 1))
        prev = b + 1
    if prev <= n - 1:
        bands.append((prev, n - 1))
    return bands


def gutter_regions(mb, W, region, min_gutter):
    out, stack = [], [region]
    while stack:
        (x0, y0, x1, y1) = stack.pop()
        rp = [mb[y * W + x0: y * W + x1 + 1].count(255) for y in range(y0, y1 + 1)]
        if not any(rp):
            continue
        bands = _split_by_zeros(rp, min_gutter)
        if len(bands) >= 2:
            for (a, b) in bands:
                stack.append((x0, y0 + a, x1, y0 + b))
            continue
        cp = [mb[y0 * W + x: (y1 + 1) * W + x: W].count(255) for x in range(x0, x1 + 1)]
        bands = _split_by_zeros(cp, min_gutter)
        if len(bands) >= 2:
            for (a, b) in bands:
                stack.append((x0 + a, y0, x0 + b, y1))
            continue
        out.append((x0, y0, x1, y1))
    return out


def tight_bbox(mb, W, r):
    x0, y0, x1, y1 = r
    mnx = mny = mxx = mxy = None
    for y in range(y0, y1 + 1):
        base = y * W
        row = mb[base + x0: base + x1 + 1]
        a = row.find(b"\xff")
        if a == -1:
            continue
        b = row.rfind(b"\xff")
        mny = y if mny is None else mny
        mxy = y
        mnx = x0 + a if mnx is None or x0 + a < mnx else mnx
        mxx = x0 + b if mxx is None or x0 + b > mxx else mxx
    if mnx is None:
        return None
    return (mnx, mny, mxx, mxy)


# ============================ 间距聚类（MST 自动阈值） ============================

def box_gap(a, b):
    dx = max(0, max(a[0], b[0]) - min(a[2], b[2]) - 1)
    dy = max(0, max(a[1], b[1]) - min(a[3], b[3]) - 1)
    return max(dx, dy)


def cluster_comps(comps, max_gap, min_ratio=3.0, debug=False):
    n = len(comps)
    if n <= 1:
        return [[c] for c in comps] if comps else []

    # 最小生成树(Prim), 边权 = 外接框间距
    INF = float("inf")
    in_t, key, par, edges = [False] * n, [INF] * n, [-1] * n, []
    key[0] = 0
    for _ in range(n):
        u, best = -1, INF
        for i in range(n):
            if not in_t[i] and key[i] < best:
                best, u = key[i], i
        if u == -1:
            break
        in_t[u] = True
        if par[u] != -1:
            edges.append(key[u])
        for v in range(n):
            if not in_t[v]:
                d = box_gap(comps[u], comps[v])
                if d < key[v]:
                    key[v], par[v] = d, u

    ws = sorted(edges)
    best_r, cut = 0.0, -1
    for i in range(len(ws) - 1):
        r = (ws[i + 1] + 1.0) / (ws[i] + 1.0)
        if r > best_r:
            best_r, cut = r, i
    if best_r >= min_ratio and cut >= 0:
        thr = ((ws[cut] + 1.0) * (ws[cut + 1] + 1.0)) ** 0.5 - 1.0
    else:
        thr = 0.0
    thr = min(thr, float(max_gap))

    parent = list(range(n))

    def f(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if box_gap(comps[i], comps[j]) <= thr and f(i) != f(j):
                parent[f(j)] = f(i)

    groups = {}
    for i in range(n):
        groups.setdefault(f(i), []).append(comps[i])
    if debug:
        print("   聚类阈值=%.1f (MST最大跳变比=%.2f), %d 块 -> %d 个元素"
              % (thr, best_r, n, len(groups)))
    return list(groups.values())


# ============================ 输出 ============================

def checkerboard(size, cell=16, c1=(206, 206, 206), c2=(238, 238, 238)):
    img = Image.new("RGB", size, c1)
    d = ImageDraw.Draw(img)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if ((x // cell) + (y // cell)) % 2:
                d.rectangle([x, y, x + cell - 1, y + cell - 1], fill=c2)
    return img


def get_font(size):
    """尽量找一个能显示中文的字体，找不到就退回 PIL 内置位图字体。

    字体目录从 %WINDIR% 取，不写死盘符；非 Windows 上直接走兜底。
    """
    fonts_dir = os.environ.get("WINDIR")
    if fonts_dir:
        for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(os.path.join(fonts_dir, "Fonts", name), size)
            except Exception:
                pass
    return ImageFont.load_default()


def main():
    ap = argparse.ArgumentParser(description="通用元素区域自动解析 / 切图")
    ap.add_argument("image")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--method", choices=["auto", "gutter", "cluster"], default="auto")
    ap.add_argument("--color-thr", type=int, default=24)
    ap.add_argument("--alpha-thr", type=int, default=24)
    ap.add_argument("--min-area", type=int, default=0, help="连通域最小面积(0=自动)")
    ap.add_argument("--min-gutter", type=int, default=0, help="沟槽最少厚度像素(0=自动)")
    ap.add_argument("--max-gap", type=int, default=0, help="聚类最大间距(0=自动)")
    ap.add_argument("--pad", type=int, default=8)
    ap.add_argument("--crops", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    src = args.image
    outdir = args.outdir or os.path.dirname(os.path.abspath(src))
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]

    im = Image.open(src).convert("RGBA")
    W, H = im.size
    print("图像: %s  %dx%d" % (os.path.basename(src), W, H))

    mask_img, how = build_mask(im, args.color_thr, args.alpha_thr)
    print("掩膜策略:", how)
    mb = mask_img.tobytes()

    min_area = args.min_area or max(10, int(W * H * 2e-6))
    min_gutter = args.min_gutter or max(1, int(min(W, H) * 0.002))
    max_gap = args.max_gap or int(max(W, H) * 0.05)
    min_bbox_area = max(16, min_area)

    method = args.method
    boxes = []
    if method in ("auto", "gutter"):
        regs = gutter_regions(mb, W, (0, 0, W - 1, H - 1), min_gutter)
        cand = []
        for r in regs:
            t = tight_bbox(mb, W, r)
            if t and (t[2] - t[0] + 1) * (t[3] - t[1] + 1) >= min_bbox_area:
                cand.append(t)
        if len(cand) >= 2 or method == "gutter":
            boxes = cand
            method = "gutter"
            if args.debug:
                print("   沟槽切分: %d 个区域 (min_gutter=%d)" % (len(cand), min_gutter))

    if not boxes and method in ("auto", "cluster"):
        comps = connected_components(mb, W, H, min_area)
        if args.debug:
            print("   连通域: %d 块 (min_area=%d)" % (len(comps), min_area))
        groups = cluster_comps(comps, max_gap, debug=args.debug)
        boxes = [(min(c[0] for c in g), min(c[1] for c in g),
                  max(c[2] for c in g), max(c[3] for c in g)) for g in groups]
        method = "cluster"

    if not boxes:
        print("!! 未找到任何元素（掩膜为空？）。可尝试 --color-thr / --alpha-thr。")
        return

    hs = [b[3] - b[1] + 1 for b in boxes]
    band = max(1, int(statistics.median(hs) * 0.6))
    boxes.sort(key=lambda b: (round(((b[1] + b[3]) / 2) / band), b[0]))

    print("\n[%s] 解析出 %d 个元素:" % (method, len(boxes)))
    for i, (a, b, c, d) in enumerate(boxes, 1):
        print("  #%-3d x=%d..%d  y=%d..%d  尺寸=%dx%d"
              % (i, a, c, b, d, c - a + 1, d - b + 1))

    base = checkerboard((W, H))
    base.paste(im, (0, 0), im)
    dr = ImageDraw.Draw(base)
    palette = [(230, 30, 30), (0, 130, 240), (20, 170, 80), (250, 150, 0),
               (170, 40, 210), (0, 180, 180), (240, 60, 140), (120, 90, 30)]
    fs = max(16, int(min(W, H) * 0.016))
    font = get_font(fs)
    lw = max(2, fs // 8)
    for i, (a, b, c, d) in enumerate(boxes):
        col = palette[i % len(palette)]
        dr.rectangle([a - lw, b - lw, c + lw, d + lw], outline=col, width=lw)
        tag = str(i + 1)
        tw = dr.textlength(tag, font=font)
        dr.rectangle([a - lw, b - fs - 8, a - lw + tw + 10, b - lw], fill=col)
        dr.text((a + 2, b - fs - 6), tag, fill=(255, 255, 255), font=font)
    marked = os.path.join(outdir, stem + "_elements.png")
    base.save(marked)

    white = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    Image.alpha_composite(white, im).convert("RGB").save(
        os.path.join(outdir, stem + "_elements_onwhite.png"))

    with open(os.path.join(outdir, stem + "_elements.json"), "w", encoding="utf-8") as f:
        json.dump({"source": os.path.basename(src), "image_size": [W, H],
                   "method": method, "count": len(boxes),
                   "elements": [{"index": i + 1, "x0": a, "y0": b, "x1": c, "y1": d,
                                 "width": c - a + 1, "height": d - b + 1}
                                for i, (a, b, c, d) in enumerate(boxes)]},
                  f, ensure_ascii=False, indent=2)

    if args.crops:
        cdir = os.path.join(outdir, stem + "_crops")
        os.makedirs(cdir, exist_ok=True)
        for i, (a, b, c, d) in enumerate(boxes, 1):
            p = args.pad
            im.crop((max(0, a - p), max(0, b - p),
                     min(W, c + 1 + p), min(H, d + 1 + p))).save(
                os.path.join(cdir, "%02d.png" % i))
        print("单品裁剪:", cdir)

    print("标注图:", marked)


if __name__ == "__main__":
    main()
