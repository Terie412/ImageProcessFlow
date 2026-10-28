# -*- coding: utf-8 -*-
"""「尺寸 = 自动」到底选哪个分辨率 —— **全项目唯一一份实现**。

## 自动 = 按参考图就近挑一个预设

节点上那个「尺寸」下拉默认是「自动」（参数值是空串）。空串**不是**「让模型随便挑」，
而是「按参考图的宽高，在下拉的全部选项里挑最像的那一个」：

1. 取**最后一张**连了线的参考图的宽高，当成向量 A
   —— DashScope 的口径就是「多图输入时取最后一张」（见官方 Qwen-Image-Edit API 文档）；
2. 把下拉里每个能解析成宽高的选项也当成一个向量，和 A 一起**归一到同一个面积**；
3. 取归一化后**欧氏距离最近**的那个。

命中的选项会**原样发给 API**，所以「自动」现在是可预测、可复现的：
节点上在**生成之前**就显示它选了哪个，而不是生成完才知道。

## 为什么必须先把面积归一化（这一步是核心）

直接拿 (720, 1280) 和 (1024, 1024) 算欧氏距离是**错的** —— 那个距离里同时混了
「形状」和「大小」两件事，结果会被大面积选项带偏。实测一个真实例子：

    输入 941×1672（比例 0.5628，一张 9:16 的竖图）
    ├─ 不归一化 → 选 1024×1536（比例 0.667）   ← 因为 1024×1536 的坐标离得"近"
    └─ 归一化后 → 选 720×1280（比例 0.5625）   ← 差 0.0003，这才是对的

归一化 = 两个向量都缩放成「面积 = REF_AREA」，缩放后面积相同，剩下的差异
**就只剩形状（宽高比）了** —— 而"像不像"本来就是形状说了算。

## 形状定档，面积定档内选哪个

形状最近只决定**落在哪一档比例**（1:1 / 16:9 / 9:16 / 4:3 / 3:4 / 3:2 / 2:3 …）。
一档里往往有 1K 和 2K 两个选项（`1536×1536` 与 `2048×2048`、`720×1280` 与 `1536×2752`），
形状几乎一样，靠**面积**决定：取面积最接近输入的那个（`|ln(面积比)|` 最小）。
所以 1024×1024 的输入回到 1024×1024，2048×2048 的输入去 2048×2048。

容差是 `_SHAPE_BAND`（约 1% 边长误差）。它必须存在 —— 官方预设的同一个比例不等于
精确同一个数（2K 的 16:9 是 2752×1536 = 1.7917，真值是 1.7778），差的那一点点
不该被当成"另一种形状"，否则一模一样比例的输入会因为小数点后几位被判到另一档去。

## 没有参考图怎么办

文生图没有参考图，也就没有向量 A。此时**不解析、不发 size**，交回模型自己决定，
节点上显示「模型自选」—— 不硬塞一个 1024×1024，那等于凭空替用户做掉模型本来会做的决定。
"""
from __future__ import annotations

import math
import re

# 归一化的参考面积。取 1024×1024 是因为它就是这套预设的中心尺度，
# 缩放后的数值也都落在 1024 附近，肉眼可读、便于调试。
REF_AREA = 1024 * 1024

# 「自动」这个语义由 manifest 的参数声明，不靠猜 key 名：
#     { "key": "size", "type": "enum", "autoFrom": "input", ... }
# 没声明 autoFrom 的 enum 参数一律不参与，节点上也不会多出提示行。
AUTO_FROM_INPUT = "input"

# 分隔符归一化：'720x1280' / '720*1280' / '720×1280' 都吃。
# （和脚本里发请求前那一步同源 —— DashScope 只认星号。）
_SIZE_SEP = re.compile(r"\s*[xX×*]\s*")

# 距离打平的判定阈值：归一化空间里 1e-6 已经远小于任何两个真实选项的间距，
# 但足以吸收浮点误差，让「同一个比例」的两个选项真的被判成平手。
_TIE_EPS = 1e-6

# 「同一个比例档」的容差（归一化空间，单位≈像素：按面积 1024² 归一后的坐标距离）。
#
# 预设里同一个比例未必是**精确**同一个数：官方 2K 给的是 2752×1536（1.7917），
# 16:9 的真值是 1.7778，两者差 0.8%，在归一化空间里约 6px。若只用「欧氏距离严格最近」，
# 那么一模一样的 16:9 输入会一直落到 1280×720（比例精确），永远轮不到 2752×1536 ——
# 哪怕输入本来就是 3840×2160。所以把「差不到一档」的选项算作**同一比例**，
# 再在档内按**面积**挑最接近输入的（原先只有精确打平时才走面积，`2048×2048` 这类
# 加入之前够用，加入之后就不够了）。
#
# 10px ≈ 1% 的边长误差：远小于相邻两档比例的间距（4:3 与 3:2 差 11%，约 90px），
# 又足以把 1.7917 / 1.7778、1.4906 / 1.5、0.5581 / 0.5625 认成同一档。
_SHAPE_BAND = 10.0


def parse_size(text) -> tuple[int, int] | None:
    """`'720x1280'` / `'720*1280'` / `'720×1280'` → `(720, 1280)`。

    解析不了（空串 / 缺一边 / 非数字 / 非正数）一律返回 None。
    """
    if not isinstance(text, str):
        return None
    parts = _SIZE_SEP.split(text.strip())
    if len(parts) != 2:
        return None
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return w, h


def _normalized(w: float, h: float) -> tuple[float, float]:
    """把 (w, h) 缩放到面积 = REF_AREA，形状不变。"""
    k = math.sqrt(REF_AREA / (w * h))
    return w * k, h * k


def nearest_size(aw: int, ah: int, options: list[str]) -> str | None:
    """在 `options` 里挑离 `(aw, ah)` 最近的一个，返回**选项的原始值**。

    两步走：
    1. 先在归一化空间里找**形状最近**的那个，得到最小距离 `best_dist`；
    2. 把距离在 `best_dist + _SHAPE_BAND` 以内的都当成**同一个比例档**，
       档内改按**面积**（`|ln(面积比)|`）挑最接近输入的那个。

    第 2 步是必需的：同一个比例有 1K / 2K 两个选项时（`1536×1536` 与 `2048×2048`、
    `720×1280` 与 `1536×2752`），形状几乎一样，只有面积能决定该选哪档 ——
    否则「输入 3840×2160 却自动出 1280×720」这种反直觉的结果会一直存在。
    """
    options = [o for o in (options or []) if parse_size(o)]
    if not options:
        return None

    tw, th = _normalized(aw, ah)
    area_a = float(aw) * float(ah)

    dists: list[tuple[float, float, str]] = []  # (形状距离, 面积对数差, 值)
    for value in options:
        w, h = parse_size(value)  # type: ignore[misc]
        uw, uh = _normalized(w, h)
        dists.append((
            math.hypot(tw - uw, th - uh),
            abs(math.log((w * h) / area_a)),
            value,
        ))

    best_dist = min(d for d, _, _ in dists)
    band = best_dist + _SHAPE_BAND
    same_shape = [(area_tie, d, v) for d, area_tie, v in dists if d <= band + _TIE_EPS]
    # 先比面积，面积也打平（同一个比例、同一个面积的不同写法）时比形状，最后按选项顺序。
    area_tie, _, value = min(same_shape, key=lambda t: (t[0], t[1]))
    return value


def dims_of_path(path) -> tuple[int, int] | None:
    """读图片**文件头**拿宽高（不加载像素，很快）。读不了就返回 None。

    只在 PIL 可用时工作；PIL 缺失或文件坏掉都退化成 None —— 上层会当作
    「没有参考图」处理，不会因此让执行挂掉。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        with Image.open(path) as im:
            w, h = im.size
        return (int(w), int(h)) if w and h else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# 与 manifest 的对接
# --------------------------------------------------------------------------
def auto_key(manifest: dict | None, backend: str | None = None) -> str | None:
    """这个 manifest 里哪个参数声明了「自动」语义。没有返回 None。

    一个节点可以有**多套**尺寸参数（`AI 生图 / 编辑` 的 `size` 与 `size_gpt`），
    各自声明 `autoFrom`。`backend` 给了就优先返回**同后端**的那一个，
    所以「选中 gpt 模型时读 size_gpt、选中 qwen 模型时读 size」这件事只有这里说了算。
    找不到同后端的，回退到第一个（不标 backend 的参数等于各后端通用）。
    """
    fallback: str | None = None
    for p in (manifest or {}).get("params", []) or []:
        if p.get("autoFrom") != AUTO_FROM_INPUT:
            continue
        if backend is not None and p.get("backend") == backend:
            return p.get("key")
        if fallback is None:
            fallback = p.get("key")
    return fallback


def options_of(manifest: dict | None, key: str) -> list[str]:
    """取某个 enum 参数里**能当分辨率用**的选项值（空值「自动」会被自动排除）。"""
    for p in (manifest or {}).get("params", []) or []:
        if p.get("key") != key:
            continue
        out: list[str] = []
        for o in p.get("options", []) or []:
            v = o.get("value") if isinstance(o, dict) else o
            if parse_size(v):
                out.append(v)
        return out
    return []


def resolve(
    manifest: dict | None,
    params: dict | None,
    ref_dims: tuple[int, int] | None,
    backend: str | None = None,
) -> tuple[str | None, dict | None]:
    """**执行时**用：算出该发给模型的尺寸。

    规则只有一条 —— **用户显式选过就尊重用户**，没选（空串）才走「自动」。
    返回 `(value, info)`：

    - `value` —— 该发给模型的分辨率字符串（如 `"720x1280"`）；`None` 表示**不发**。
    - `info`  —— 只在真的落到了「自动」时给（供打日志用）；用户显式选的返回 `None`。

    | 情形 | value | info |
    | --- | --- | --- |
    | 没声明 autoFrom | `None` | `None` |
    | 用户显式选了某个分辨率 | 原值 | `None` |
    | 自动 + 有参考图 | 命中的选项 | `{"source": "input", ...}` |
    | 自动 + 没有参考图 | `None`（交回模型） | `{"source": "model"}` |
    """
    key = auto_key(manifest, backend)
    if key is None:
        return None, None

    chosen = str((params or {}).get(key) or "").strip()
    if chosen:
        return chosen, None

    return pick(manifest, ref_dims, backend)


def pick(
    manifest: dict | None,
    ref_dims: tuple[int, int] | None,
    backend: str | None = None,
) -> tuple[str | None, dict | None]:
    """「自动」**会**落到哪个预设 —— **与用户当前选了什么无关**。

    界面用这个（见 `main._auto_size_hint`）。刻意不把「用户已显式选择」并进来：
    显不显示提示是**前端**的事（用户切回「自动」是纯本地操作，不该再等一次
    `/api/state`），服务端只管算。执行时该不该用这个值由 `resolve()` 决定。
    """
    key = auto_key(manifest, backend)
    if key is None:
        return None, None

    if not ref_dims:
        return None, {"key": key, "value": None, "label": "模型自选", "source": "model"}

    picked = nearest_size(ref_dims[0], ref_dims[1], options_of(manifest, key))
    if picked is None:
        return None, {"key": key, "value": None, "label": "模型自选", "source": "model"}

    return picked, {
        "key": key,
        "value": picked,
        "label": picked.replace("x", "×"),
        "source": "input",
        "refWidth": ref_dims[0],
        "refHeight": ref_dims[1],
    }
