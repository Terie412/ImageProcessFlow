# -*- coding: utf-8 -*-
"""节点尺寸的**唯一事实来源**。

## 为什么有这个模块

尺寸原先在**两个地方各算一遍**：前端 `App.jsx` 的 `RESOURCE_SIZE` / `PROCESS_SIZE`，
和服务端 `engine.py` 的 `PROCESS_DEFAULT_W` / `RESOURCE_DEFAULT_H`。两份常量必然漂移 ——
实测两边对「数据节点没存过高时该多高」的回答分别是 220 和 280，于是服务端排出来的
输出槽位间距比屏幕上看到的**宽 70px**（用户看到的缝是 280 而不是 10）。而且因为
节点尺寸只在**拖过右下角手柄**时才写进 graph.json，没拖过的节点每渲染一次都要重算一次，
这类漂移影响的是**半个画布**。

现在只留一条计算路径：

```
manifest.size（脚本作者声明的初始尺寸）
      ↓ 被覆盖
settings.nodeSize / settings.nodeMin（应用级，界面 ⚙ 里可改）
      ↓ 兜底
本模块的 SIZE_FALLBACK / MIN_FALLBACK —— 唯一一份常量
      ↓ 解析
/api/state.sizeSpec（前端唯一读取入口，只查表、自己不再有任何数字）
```

## 三个概念

| 概念 | 含义 | 权威来源 |
| --- | --- | --- |
| **初始尺寸** | 节点从没被拖过右下角手柄时该多大 | manifest（处理节点）/ `settings.nodeSize` |
| **最小尺寸** | 拖手柄时的下限 | `settings.nodeMin` |
| **最终尺寸** | `max(最小尺寸, 存的 w/h ?? 初始尺寸)` | 本模块 `size_of()` |

三者都按**具体节点类型**索引：`resource` / `process:<defId>` / `process`（兜底），
与 `settings.nodeMin` 的既有形状保持一致。

> ⚠️ 「最终尺寸」是**算出来的**，不是存下来的 —— 节点只在被拖过之后才有 `w`/`h`。
> 所以任何需要知道「这个节点多大」的代码（前端渲染、服务端的槽位落点 / 布局）
> 都必须走 `size_of()`，不能各写一份。
"""
from __future__ import annotations

# 尺寸的允许区间（宽高都按这个夹）。与前端表单的 min/max 一致，
# 通过 /api/state 的 `sizeRange` 下发，前端不再自己写一份。
SIZE_RANGE = (80, 4000)

# 兜底常量：**全项目唯一一份**。前端不再有自己的副本。
# 这两张表就是 `main.DEFAULT_SETTINGS` 里 `nodeSize` / `nodeMin` 的最终兜底，
# 两边**必须同值** —— 它们只在设置里那一项缺失时才轮到（正常运行时设置总是全的），
# 一旦不同就会出现"服务端排版按一个高、前端渲染按另一个高"的假缝（§3.5.1 那段）。
SIZE_FALLBACK = {
    "resource": {"w": 200, "h": 200},
    "process": {"w": 600, "h": 340},
}
MIN_FALLBACK = {
    "resource": {"w": 200, "h": 200},
    "process": {"w": 560, "h": 280},
}


def proc_key(def_id: str | None) -> str:
    """处理节点类型在尺寸表里的 key。"""
    return f"process:{def_id}"


def _clamp(v: float) -> float:
    lo, hi = SIZE_RANGE
    return max(lo, min(hi, float(v)))


def _num(v, dflt: float) -> float:
    """取一个正数；拿不到（None / 空串 / 非数字 / ≤0）就退回 dflt。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dflt
    return f if f > 0 else dflt


def _entry(table: dict | None, key: str) -> dict | None:
    """取表里**精确匹配** `key` 的那一条；没有返回 None。"""
    if not isinstance(table, dict):
        return None
    spec = table.get(key)
    return spec if isinstance(spec, dict) else None


def _manifest_default(manifest: dict | None) -> dict:
    """脚本作者声明的初始尺寸（只有处理节点有；数据节点没有 manifest）。"""
    return (manifest or {}).get("size") or {}


def spec_of(
    key: str,
    kind: str,
    manifest: dict | None = None,
    settings: dict | None = None,
) -> dict:
    """解析出**一个节点类型**的尺寸规格：`{w, h, minW, minH}`。

    `w` / `h` 是初始尺寸，`minW` / `minH` 是最小尺寸。

    **初始尺寸的优先级**（`exact` = 针对这个类型写的设置）：

        nodeSize[<key>]  >  manifest.size  >  nodeSize.process  >  SIZE_FALLBACK

    `nodeSize.process` 刻意排在 manifest 之后 —— 它是**没有声明 size 的类型**的兜底，
    不是「一把改掉所有处理节点」的总开关。否则它会把每个脚本自己声明的尺寸全盖掉
    （实测踩过：Qwen 节点该是 manifest 的 560×300，被通用兜底盖成 600×340）。

    **最小尺寸的优先级**（不涉及 manifest，那是脚本的属性、不是用户偏好）：

        nodeMin[<key>]  >  nodeMin.process  >  MIN_FALLBACK
    """
    st = settings or {}
    fb = SIZE_FALLBACK["resource" if kind == "resource" else "process"]

    # 初始尺寸
    size_table = st.get("nodeSize")
    exact = _entry(size_table, key)
    generic = None if kind == "resource" else _entry(size_table, "process")
    man = _manifest_default(manifest)

    def pick(field: str, manifest_field: str) -> float:
        for v in (
            (exact or {}).get(field),
            man.get(manifest_field),
            (generic or {}).get(field),
        ):
            if v is not None:
                return _clamp(_num(v, fb[field]))
        return _clamp(fb[field])

    w = pick("w", "defaultWidth")
    h = pick("h", "defaultHeight")

    # 最小尺寸
    min_table = st.get("nodeMin")
    m_exact = _entry(min_table, key)
    m_generic = None if kind == "resource" else _entry(min_table, "process")
    mfb = MIN_FALLBACK["resource" if kind == "resource" else "process"]

    def pick_min(field: str) -> float:
        for v in ((m_exact or {}).get(field), (m_generic or {}).get(field)):
            if v is not None:
                return _clamp(_num(v, mfb[field]))
        return _clamp(mfb[field])

    return {"w": w, "h": h, "minW": pick_min("w"), "minH": pick_min("h")}


def resolve_types(registry: dict | None, settings: dict | None) -> dict[str, dict]:
    """解析出**每个已知类型**的尺寸规格，供 `/api/state.sizeSpec` 下发。

    前端拿到这张表之后只做查表，不再自己推算尺寸。
    key 与 `settings.nodeMin` 一致：`resource` / `process` / `process:<defId>`。
    """
    out = {
        "resource": spec_of("resource", "resource", None, settings),
        "process": spec_of("process", "process", None, settings),
    }
    for def_id, manifest in sorted((registry or {}).items()):
        out[proc_key(def_id)] = spec_of(proc_key(def_id), "process", manifest, settings)
    return out


def size_of(
    node: dict | None,
    *,
    kind: str,
    manifest: dict | None = None,
    settings: dict | None = None,
) -> tuple[float, float]:
    """节点**最终渲染尺寸** `(w, h)` —— 前端画出来的就是这两个数。

    `max(最小尺寸, 节点存过的 w/h ?? 初始尺寸)`：存过的尺寸可能小于后来调大的下限，
    这里兜一层，保证不会渲染出一个「比下限还小」的节点。

    ⚠️ 前端 `buildNodes` 用的就是 `/api/state` 里这个函数算好的结果，
    不要再在别处写第二份公式。
    """
    n = node or {}
    key = "resource" if kind == "resource" else proc_key(n.get("defId"))
    spec = spec_of(key, kind, manifest, settings)
    return (
        max(spec["minW"], _num(n.get("w"), spec["w"])),
        max(spec["minH"], _num(n.get("h"), spec["h"])),
    )
