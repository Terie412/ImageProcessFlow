# -*- coding: utf-8 -*-
"""色彩平衡节点：把整张图的色调**沿两条色偏轴整体平移**。

「这张图整体偏蓝，我想改成整体偏黄」就是这里干的事：把 warm 往正推。
它保住画面的内容、明暗层次和对比关系，只是整体换一层色 —— 和「单色替换」
（把所有像素的 RGB 抹成同一个值、做剪影）是两回事。

两条轴（都是 -50 ~ +50，0 是不动）：

    warm  黄 ↔ 蓝    正数往黄推、负数往蓝推
    tint  绿 ↔ 品红   正数往绿推、负数往品红推

## 为什么不是「转 LAB 加偏移」

LAB 的 b* / a* 轴才是这两条轴的正根（b* 就是蓝-黄、a* 就是绿-品红），
而且 L 不动＝天然保亮度。但 **PIL 的 LAB 只有 8 位**（a/b 各 ±128 档），
实测「什么都不改、只过一遍色彩空间」就会把高饱和色的单通道改掉十几级：

    纯蓝 (0,0,255) → (22,7,252)   纯绿 (0,255,0) → (13,254,14)
    高饱和蓝 (8,26,210) → (22,26,211)

一个「不改任何东西」的操作不该动图，所以换成正统的白平衡做法：
**在**线性光**里乘三个通道增益**（对角模型，相机和 RAW 转换器用的都是它）。
每通道只乘一个数 —— 可以用一条 256 项的 LUT 精确完成，**往返零误差**，
而且 0 位置严格恒等。

## 增益怎么由两条轴算出来

    warm > 0（变黄）→ 抬 R、G，压 B
    tint > 0（变绿）→ 抬 G，压 R、B

再按 Rec.709 的亮度权重把三个增益整体缩一下，让**整张图的平均线性亮度不变**
（manifest 的 `keepLuma`，默认开）。关掉就照原样乘 —— 那是「保黑点」的做法
（黑还是黑，但画面会整体变亮/变暗）。

> **为什么这个缩放要看图算，不能是一个固定倍数。** 一开始写的是「把增益向量自己的
> 加权亮度归一化到 1」，那只对**中性灰**成立：对彩色像素来说，三个通道各自的线性值
> 不同，同一个常数乘上去，Σ wᵢkᵢLᵢ 并不等于 Σ wᵢLᵢ。实测一张整体偏蓝的图
> `(60,90,170)` 推到 warm=+50，线性亮度掉了 **12%** —— 一个叫「保持亮度」的开关
> 不能让图变暗。现在的做法是拿**每个通道的直方图**算出这张图自己的平均线性亮度
> `Σ wᵢLᵢ`，再解出 `s = Σ wᵢLᵢ / Σ wᵢkᵢLᵢ`，然后就着这个 s 建 LUT。同一张偏蓝图
> 推到 warm=+50，线性亮度的相对残差从 **9.4% 降到 0.45%**（剩下的就是 8 位取整）。

## 输出格式

  - 带 Alpha（RGBA / LA / P+transparency）→ PNG（别的格式存不住透明）；
  - 不带 Alpha → 跟随原图格式（JPEG 进 JPEG 出、WEBP / BMP 同理）；
  - **两个值都是 0** → 直接复制文件，不重新编码（JPEG 不会因为「跑一遍」掉品质）。
手机照片的 EXIF 旋转标记会先被摆正（`exif_transpose`），和其他节点一致。

节点脚本契约（见 DESIGN.md §2.5）：
  stdin  ← 一行 JSON: {"params": {...}, "inputs": {...}, "output_dir": 目录}
  stdout → 日志（原样显示在节点上）
  产出   → 把图片写进 output_dir，命名为 0.png、1.png…（序号即输出槽位顺序）
  成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

示例 stdin:
    {
      "params":     { "warm": 35, "tint": 0, "keepLuma": true },
      "inputs":     { "image1": "/abs/workspace/images/A.png", "image2": null },
      "output_dir": "/abs/workspace/.tmp/<runId>"
    }
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys

from PIL import Image, ImageOps

# 能原样保留的格式 → (PIL 格式名, 文件扩展名)；不在表里的退化成 PNG
KEEP_FORMAT: dict[str, tuple[str, str]] = {
    "JPEG": ("JPEG", ".jpg"),
    "PNG": ("PNG", ".png"),
    "WEBP": ("WEBP", ".webp"),
    "BMP": ("BMP", ".bmp"),
}

# 两条轴的取值范围。manifest 里同样声明了 min/max（界面据此夹滑块），
# 这里再夹一层是因为 graph.json 可能被手改过 —— 夹住比直接失败友好。
AXIS_LIMIT = 50.0

# 轴推到满格时，三个通道增益的**相对**幅度（还没做亮度归一化）。
# 这几个系数是标定的结果，目标是「±50 是明显但可控的一档」——
# 满格暖色把 B 压到 0.50 倍、R/G 抬到 1.30 倍，做亮度归一化后落到
# 「中灰 128 → (131,131,83)」这一档（明显发黄、整体明暗不变）。
K_WARM = 0.30   # 暖色对 R / G 的提升
K_WARM_B = 0.50  # 暖色对 B 的压低（黄＝蓝的补色，所以幅度比 R/G 大）
K_TINT_G = 0.35  # 绿色对 G 的提升
K_TINT_RB = 0.20  # 绿色对 R / B 的压低

# Rec.709 亮度权重 —— 与 sRGB 的原色一致，用它把整体亮度锁住
LUMA = (0.2126, 0.7152, 0.0722)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def port_order(key: str) -> int:
    """image1 / image2 … 按结尾的数字排序，保证输出序号与输入口顺序一致。"""
    m = re.search(r"(\d+)$", key or "")
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------- 参数

def read_axis(v, name: str) -> float:
    """把一条轴的参数收成 -50~50 的数；不是数字就抛错（消息直接给用户看）。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        return 0.0  # 没填＝不动，不报错（滑块本来就有默认值 0）
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 不是数字：{v!r}")
    if x != x:  # NaN
        raise ValueError(f"{name} 不是数字")
    return max(-AXIS_LIMIT, min(AXIS_LIMIT, x))


def gains_of(warm: float, tint: float) -> tuple[float, float, float]:
    """两条轴 → 三个通道增益（**还没做**亮度归一化）。

    warm > 0 抬 R/G、压 B；tint > 0 抬 G、压 R/B。都是**线性光**里的乘数。
    """
    t = warm / AXIS_LIMIT
    g = tint / AXIS_LIMIT
    return (
        1.0 + K_WARM * t - K_TINT_RB * g,
        1.0 + K_WARM * t + K_TINT_G * g,
        1.0 - K_WARM_B * t - K_TINT_RB * g,
    )


def luma_scale(rgb: Image.Image, gains) -> float:
    """解出让**这张图**平均线性亮度不变的缩放系数 `s = ΣwᵢLᵢ / ΣwᵢkᵢLᵢ`。

    `Lᵢ` 是第 i 个通道的平均**线性**值，由直方图算出来（不逐像素扫）。
    这个 s 必须按图算 —— 见模块开头那段说明：一个固定的常数只对中性灰成立。
    """
    hist = rgb.histogram()  # RGB 图返回 3 × 256，hist[i*256+v] = 值为 v 的像素数
    total = rgb.width * rgb.height
    if total <= 0:
        return 1.0
    lin = [srgb_to_linear(v / 255.0) for v in range(256)]

    before = after = 0.0
    for ci, k in enumerate(gains):
        h = hist[ci * 256:(ci + 1) * 256]
        mean_lin = sum(cnt * lin[v] for v, cnt in enumerate(h)) / total
        before += LUMA[ci] * mean_lin
        after += LUMA[ci] * k * mean_lin
    if after <= 1e-9:
        return 1.0
    return before / after


def luts_for(rgb: Image.Image, gains, keep_luma: bool):
    """这张图实际要用的三条 LUT（亮度和增益都定下来之后才建）。"""
    s = luma_scale(rgb, gains) if keep_luma else 1.0
    eff = tuple(k * s for k in gains)
    return tuple(channel_lut(k) for k in eff), eff


# ---------------------------------------------------------------- 线性光

def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def channel_lut(k: float) -> list[int] | None:
    """一个通道的 256 项查找表：`sRGB 字节 → 线性 → ×k → 回 sRGB 字节`。

    k 恰好是 1 时返回 None —— 调用方据此跳过这一通道，**保证 0 位置严格恒等**
    （不是「算一遍刚好也等于原值」，是真的一个像素都不碰）。
    """
    if abs(k - 1.0) < 1e-9:
        return None
    lut: list[int] = []
    for i in range(256):
        v = srgb_to_linear(i / 255.0) * k
        if v <= 0.0:
            lut.append(0)
        elif v >= 1.0:
            lut.append(255)
        else:
            lut.append(int(linear_to_srgb(v) * 255 + 0.5))
    return lut


def apply_luts(im: Image.Image, luts) -> Image.Image:
    """对 RGB 三个通道分别套 LUT（None 的通道原样不动）。"""
    r, g, b = im.split()
    chans = []
    for ch, lut in zip((r, g, b), luts):
        chans.append(ch.point(lut) if lut is not None else ch)
    return Image.merge("RGB", tuple(chans))


# ---------------------------------------------------------------- 单张处理

def is_translucent(im: Image.Image) -> bool:
    """源图是否本来就带透明通道 —— 判据是像素数据本身，不是文件格式。"""
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def save_as(out: Image.Image, dest_dir: str, idx: int, pil_fmt: str, ext: str) -> str:
    dest = os.path.join(dest_dir, f"{idx}{ext}")
    if pil_fmt == "JPEG":
        out.convert("RGB").save(dest, "JPEG", quality=95, subsampling=0, optimize=True)
    elif pil_fmt == "WEBP":
        out.save(dest, "WEBP", quality=95, method=6)
    elif pil_fmt == "BMP":
        out.convert("RGB").save(dest, "BMP")
    else:
        out.save(dest, "PNG", optimize=True)
        return "PNG"
    return pil_fmt


def balance_one(path: str, dest_dir: str, idx: int, warm: float, tint: float,
                gains, keep_luma: bool, is_noop: bool) -> None:
    """处理一张图：三个通道套增益 LUT，按源图的 alpha / 格式落盘。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"输入图片不存在: {path}")

    with Image.open(path) as opened:
        src_fmt = (opened.format or "").upper() or "?"
        im = ImageOps.exif_transpose(opened)  # 手机拍的图带旋转标记，先摆正
        alpha_src = is_translucent(im)
        size = im.size

    if is_noop:
        # 两个轴都是 0 —— 原样复制，不重新编码（JPEG 不会因为「跑一遍」掉品质）
        ext = KEEP_FORMAT.get(src_fmt, ("PNG", ".png"))[1]
        shutil.copyfile(path, os.path.join(dest_dir, f"{idx}{ext}"))
        log(f"  #{idx + 1}  {os.path.basename(path)}  {size[0]}×{size[1]}  "
            f"{src_fmt} → {ext.lstrip('.').upper()}  两轴为 0，原样复制")
        return

    if alpha_src:
        rgba = im.convert("RGBA")
        _r, _g, _b, a = rgba.split()
        rgb = Image.merge("RGB", (_r, _g, _b))
    else:
        a = None
        rgb = im.convert("RGB")

    # LUT 按**这张图**建 ——「保持亮度」的缩放系数依赖图的平均亮度（见 luma_scale）
    luts, eff = luts_for(rgb, gains, keep_luma)
    out = apply_luts(rgb, luts)

    if a is not None:
        dest = os.path.join(dest_dir, f"{idx}.png")
        Image.merge("RGBA", (*out.split(), a)).save(dest, "PNG", optimize=True)
        fmt = "PNG"
    else:
        pil_fmt, ext = KEEP_FORMAT.get(src_fmt, ("PNG", ".png"))
        fmt = save_as(out, dest_dir, idx, pil_fmt, ext)

    log(f"  #{idx + 1}  {os.path.basename(path)}  {size[0]}×{size[1]}  "
        f"{src_fmt} → {fmt}  黄蓝 {warm:+g} / 绿品红 {tint:+g}  "
        f"增益 R×{eff[0]:.3f} G×{eff[1]:.3f} B×{eff[2]:.3f}")


def main() -> int:
    try:  # Windows 控制台默认不是 utf-8，日志里的中文会炸
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    raw = sys.stdin.read()
    if not raw.strip():
        print("stdin 没有收到 JSON payload", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"payload 不是合法 JSON: {e}", file=sys.stderr)
        return 1

    params = payload.get("params") or {}
    inputs = payload.get("inputs") or {}
    out_dir = payload.get("output_dir")
    if not out_dir:
        print("payload 缺少 output_dir", file=sys.stderr)
        return 1

    try:
        warm = read_axis(params.get("warm"), "黄↔蓝")
        tint = read_axis(params.get("tint"), "绿↔品红")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3

    keep_luma = params.get("keepLuma")
    keep_luma = True if keep_luma is None else bool(keep_luma)

    # ---- 输入：按端口顺序取所有连了图的 ----
    srcs = [(key, inputs[key]) for key in sorted(inputs, key=port_order) if inputs.get(key)]
    if not srcs:
        print("一个输入口都没连图 —— 把左侧的输入口接上图片再执行", file=sys.stderr)
        return 3

    gains = gains_of(warm, tint)
    is_noop = all(abs(k - 1.0) < 1e-9 for k in gains)

    if is_noop:
        log("[色彩] 两条轴都是 0 —— 不改颜色")
    else:
        log(f"[色彩] 黄↔蓝 {warm:+g}，绿↔品红 {tint:+g}"
            + ("（保持亮度）" if keep_luma else "（不保持亮度）"))
        log(f"[增益] 基准 R ×{gains[0]:.3f}  G ×{gains[1]:.3f}  B ×{gains[2]:.3f}"
            "   （在线性光下施加）")
        if keep_luma:
            log("       保持亮度：每张图会按自己的平均亮度再整体缩一下，逐张见下")
    log(f"[输入] 共 {len(srcs)} 张：" + "，".join(os.path.basename(p) for _, p in srcs))
    log("")

    os.makedirs(out_dir, exist_ok=True)
    for i, (_key, path) in enumerate(srcs):
        try:
            balance_one(path, out_dir, i, warm, tint, gains, keep_luma, is_noop)
        except Exception as e:  # noqa: BLE001
            print(f"处理第 {i + 1} 张（{os.path.basename(path)}）失败: {e}", file=sys.stderr)
            return 1

    log("")
    log(f"完成，共 {len(srcs)} 张输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
