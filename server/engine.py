# -*- coding: utf-8 -*-
"""执行引擎：子进程调用脚本、临时目录、原子搬运、槽位重绑定、脏标记。

对应 DESIGN.md §2.2 与 §2.5：
- 执行 P 只跑 P（无级联），全部手动执行
- 数据节点是输出槽位的投影；原地刷新（id / 坐标不变）；槽位变少则直接移除文件
- 失败时保留旧输出（因为走临时目录，成功后才搬运）
- 执行后把直接下游处理节点标脏，不递归传递
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from workspace import IMAGE_EXTS, CanvasView, drop_tree
import autosize
import nodespec
import registry as registry_mod

RUN_TIMEOUT_SEC = 900

# 输出槽位落点：**按节点实际尺寸推算**，不能写死偏移量。
# 早期写死 380/210，那是按 280 宽的处理节点算的；节点一旦被调大（默认已到 600 宽、
# 数据节点 200 高），输出节点就会横向压住上游、纵向互相压。
# 纵向的间距同样不能写死 —— 见 `resource_height()`：必须按**数据节点的实际渲染高**
# 逐个累加，否则用户把数据节点调矮之后，每两个节点之间会空出一大截。
#
# ⚠️ 尺寸常量（默认宽高、最小宽高）**不在这里** —— 见 `nodespec.py`：
# 那是全项目唯一一份，前端也是从 /api/state 读同一份解析结果。
SLOT_GAP_X = 64              # 输出节点与处理节点之间的横向留白
SLOT_GAP_Y = 10              # 相邻输出槽位之间的纵向留白（用户手动排布时用的就是这个量级）


def _as_float(v, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def resource_size(r: dict | None, settings: dict | None) -> tuple[float, float]:
    """数据节点的**实际渲染尺寸** `(w, h)`。口径见 `nodespec.size_of()`。"""
    return nodespec.size_of(r, kind="resource", settings=settings)


def process_size(
    proc: dict | None, manifest: dict | None, settings: dict | None
) -> tuple[float, float]:
    """处理节点的**实际渲染尺寸** `(w, h)`。口径见 `nodespec.size_of()`。"""
    return nodespec.size_of(proc, kind="process", manifest=manifest, settings=settings)


def resource_height(r: dict | None, settings: dict | None) -> float:
    """数据节点的**实际渲染高度**。

    前端按同一个数渲染（`/api/state` 下发的 `w`/`h` 就是这么算的），所以这里
    直接复用 `nodespec.size_of()`，不再自己写 `max(下限, 存的 h ?? 默认)` ——
    那段公式曾经在前后端各写一份，两边的默认值不一样（220 / 280），
    输出槽位间距因此比屏幕上看到的宽了 70px。
    """
    return resource_size(r, settings)[1]


def slot_origin(proc: dict, manifest: dict, settings: dict | None) -> tuple[float, float]:
    """输出槽位的左上起点：(x, y)。

    横向按**处理节点的实际宽度**让开（不写死偏移量）；纵向的累加见
    `resource_height()` —— 每个槽位从上**一个节点的实际高度**往下排。

    吸附开着时用**向上**吸附：横向的 `+SLOT_GAP_X`、纵向的 `+SLOT_GAP_Y`
    都是下限，就近吸附会把它们啃掉几个像素。
    """
    proc_w, _ = process_size(proc, manifest, settings)
    g = grid_size(settings)
    return (
        snap_up(proc.get("x", 0) + proc_w + SLOT_GAP_X, g),
        snap_up(proc.get("y", 0), g),
    )


# --------------------------------------------------------------------------
# 网格吸附
# --------------------------------------------------------------------------
GRID_SIZE_RANGE = (1, 200)   # 吸附格的允许区间（px），前后端保持一致


# --------------------------------------------------------------------------
# 缩放吸附（拖节点右下角改大小时，宽高吸到指定值的整数倍）
# --------------------------------------------------------------------------
# **0 或负数 = 关闭**（统一归到 0），所以下界是 0 而不是 1。上限与 GRID_SIZE_RANGE
# 取同一个 200 —— 再大就超过大部分节点的尺寸了，吸附会退化成「只有一档」。
# 前后端保持一致：随 /api/state 的 `scaleSnapRange` 下发，前端表单的 min/max 用它。
#
# 注意它和网格吸附**不是一个东西**：网格吸附管**位置**（拖节点），在落盘时统一收口
# （snap_graph）；缩放吸附管**尺寸**（拖右下角手柄），只在拖动那一刻生效 —— 见
# main.persist 那段的说明。
SCALE_SNAP_RANGE = (0, 200)


def grid_size(settings: dict | None) -> float:
    """当前生效的吸附格大小；没开吸附（或值不合法）返回 0。"""
    s = settings or {}
    if not s.get("snapToGrid"):
        return 0.0
    try:
        g = float(s.get("gridSize") or 0)
    except (TypeError, ValueError):
        return 0.0
    return g if g >= GRID_SIZE_RANGE[0] else 0.0


def snap_value(v: float, g: float) -> float:
    """**就近**吸附：吸到最近的格点，位移最大半个格。"""
    return round(v / g) * g if g > 0 else v


def snap_up(v: float, g: float) -> float:
    """**向上**吸附：吸到不小于 v 的那个格点。

    给「程序算出来的布局」用（输出槽位、整图排版）：这类位置有一条**下限**约束
    （相邻间距不能小于 `SLOT_GAP_Y` / `ROW_GAP_Y`，否则节点会压在一起），就近吸附
    会把间距啃掉最多半个格。向上取整则保证间距只会变大一点点、绝不会变小。
    """
    if g <= 0:
        return v
    return math.ceil(v / g - 1e-9) * g


def snap_graph(graph: dict, settings: dict | None) -> int:
    """把画布上所有节点（数据节点 + 处理节点）的坐标**就近**对齐到吸附格。

    返回被移动的节点数。只动**不对齐**的那些，所以连点两次是幂等的。
    吸附关着的时候是空操作。

    这里刻意做成「落盘前的统一收口」（见 main.persist）：只要吸附开着，
    凡是被写进 graph.json 的坐标都满足吸附规则。因此不光是用户拖动的节点，
    连「脚本执行时引擎自己摆的输出槽位」「整图排版」「导入图片」落下
    的位置也一并收齐 —— 不会出现画布上一半对齐、一半没对齐的状态。
    """
    g = grid_size(settings)
    if g <= 0:
        return 0
    moved = 0
    for node in [*graph.get("resources", []), *graph.get("processes", [])]:
        hit = False
        for axis in ("x", "y"):
            raw = node.get(axis)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            snapped = snap_value(v, g)
            if abs(snapped - v) > 1e-9:
                node[axis] = snapped
                hit = True
        if hit:
            moved += 1
    return moved


# --------------------------------------------------------------------------
# 整图自动布局（画布右键 →「格式化所有节点」/「撤销格式化」）
# --------------------------------------------------------------------------
COL_GAP_X = SLOT_GAP_X        # 列间距：沿用输出节点那 64px 的横向留白，观感一致
ROW_GAP_Y = 20                # 列内相邻节点的纵向留白（比 SLOT_GAP_Y=10 松一点：
                              # 整图重排后上下相邻的往往不是同一组输出，10 会显得糊在一起）
COMPONENT_GAP_Y = 80          # 互不相连的分量之间的空隙
LAYOUT_SWEEPS = 2             # 重心法迭代轮数（每轮 = 一次下行 + 一次上行）


class _FmtNode:
    """排版时用的节点快照：显存大小、种类，以及一个「兄弟组」的归属。

    `gid` 用来把**同一个处理节点的多个输出**绑成一组 —— 它们在列内必须
    连排、且按槽位序号从上到下，不然重执行之后就认不出谁是谁了。
    """

    __slots__ = ("id", "node", "kind", "w", "h", "gid", "index")

    def __init__(self, node: dict, kind: str, w: float, h: float):
        self.id = node["id"]
        self.node = node
        self.kind = kind
        self.w = w
        self.h = h
        self.index = _as_float(node.get("index"), 0.0) if kind == "resource" else 0.0
        if kind == "resource" and node.get("producer"):
            self.gid = f"out:{node['producer']}"
        else:
            self.gid = f"self:{node['id']}"


def _fmt_collect(graph: dict, registry: dict, settings: dict | None) -> dict[str, _FmtNode]:
    """收集所有节点 + 它们的**实际尺寸**（口径同 §3.5.1，前端按同一个数渲染）。"""
    out: dict[str, _FmtNode] = {}
    for r in graph.get("resources", []):
        w, h = nodespec.size_of(r, kind="resource", settings=settings)
        out[r["id"]] = _FmtNode(r, "resource", w, h)
    for p in graph.get("processes", []):
        m = (registry or {}).get(p.get("defId"))
        w, h = nodespec.size_of(p, kind="process", manifest=m, settings=settings)
        out[p["id"]] = _FmtNode(p, "process", w, h)
    return out


def _fmt_links(items: dict[str, _FmtNode], graph: dict) -> tuple[dict, dict]:
    """前驱 / 后继。

    两类边：画布上的显式连线（数据节点 → 处理节点），以及处理节点 → 它产出的
    输出数据节点（"隐式边"，前端也是这么画的）。合起来就是这张图的真实依赖。
    """
    succ: dict[str, list[str]] = {i: [] for i in items}
    pred: dict[str, list[str]] = {i: [] for i in items}
    for e in graph.get("edges", []):
        a, b = e.get("from"), e.get("to")
        if a in items and b in items:
            succ[a].append(b)
            pred[b].append(a)
    for r in graph.get("resources", []):
        pid = r.get("producer")
        if pid in items and r["id"] in items:
            succ[pid].append(r["id"])
            pred[r["id"]].append(pid)
    return succ, pred


def _fmt_layers(ids: list[str], succ: dict, pred: dict) -> dict[str, int]:
    """分层：`层(v) = 1 + max(层(前驱))`，没有前驱就是第 0 层（最长路径分层）。

    这样每条边都从第 L 列指向第 L' 列且 L' > L（永远向右），输出节点也自然紧贴
    在自己处理节点的右边一列。做法是 Kahn 拓扑排序，顺带算最长路径。
    万一有环（设计上禁止，见 §2.2.3），剩下的节点留在第 0 层 —— 难看但不会死循环。
    """
    idset = set(ids)
    indeg = {i: sum(1 for p in pred[i] if p in idset) for i in ids}
    layer = {i: 0 for i in ids}
    queue = deque([i for i in ids if indeg[i] == 0])
    while queue:
        i = queue.popleft()
        for j in succ[i]:
            if j not in indeg:
                continue
            if layer[i] + 1 > layer[j]:
                layer[j] = layer[i] + 1
            indeg[j] -= 1
            if indeg[j] == 0:
                queue.append(j)
    return layer


def _fmt_components(ids: list[str], succ: dict, pred: dict) -> list[list[str]]:
    """弱连通分量：互不相连的几张子图分开摆，别混在同一列里。"""
    seen: set[str] = set()
    out: list[list[str]] = []
    for start in ids:
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        comp: list[str] = []
        while stack:
            v = stack.pop()
            comp.append(v)
            for nb in (*succ[v], *pred[v]):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        out.append(comp)
    return out


def _fmt_center(i: str, items: dict[str, _FmtNode], ys: dict[str, float]) -> float:
    return ys[i] + items[i].h / 2.0


def _fmt_reorder(
    col: list[str], nbrs: dict, items: dict[str, _FmtNode], ys: dict[str, float]
) -> list[str]:
    """列内排序：按上游（或下游）节点的重心排，**同组输出绑在一起不可拆散**。

    没有可参考邻居的组（比如只在上行阶段、又没有后继的处理节点）就以自己当前的
    中心当键 —— 相当于待在原地，不会因为「没邻居」被甩到列首或列尾。
    """
    groups: dict[str, list[str]] = {}
    for i in col:
        groups.setdefault(items[i].gid, []).append(i)

    ranked: list[tuple[float, list[str]]] = []
    for members in groups.values():
        # 同一处理节点的输出：严格按槽位序号（重执行后靠序号重新绑定，见 §2.2.1 R4）
        members.sort(key=lambda i: (items[i].index, items[i].id))
        centers = []
        for i in members:
            nb = [j for j in nbrs[i] if j in ys]
            centers.append(
                sum(_fmt_center(j, items, ys) for j in nb) / len(nb) if nb else None
            )
        known = [c for c in centers if c is not None]
        key = (
            sum(known) / len(known)
            if known
            else sum(_fmt_center(i, items, ys) for i in members) / len(members)
        )
        ranked.append((key, members))
    ranked.sort(key=lambda t: t[0])   # 稳定排序：键相同时保持上一轮的相对次序
    return [i for _, ms in ranked for i in ms]


def _fmt_place(
    col: list[str],
    items: dict[str, _FmtNode],
    ys: dict[str, float],
    desired,
    g: float,
    snap: bool,
) -> None:
    """把一列的节点落位：**每个"组"从锚点往下挂**。

    「组」= 同一个处理节点的输出（`gid` 相同）；组内按"上一个的底 + `ROW_GAP_Y`"
    等距往下排，**组的起点 = 它的期望位置**（对输出块来说就是处理节点自己的顶边，
    于是输出块从处理节点的顶边开始往下排，与 `slot_origin` 一致）。

    组起点还受"间距下限"约束：压到上一组时往后让（`max(期望, 游标)`）—— 间距只会
    变大不会变小，所以同列永不重叠。这一条是必要的：一列里常常混着别的处理节点的
    输出，整列共用一个锚点的话，第二组就会被前一组的高度顺延出去（实测差 20px）。

    `snap=True` 时每个落点**向上**吸到格点（§3.10：程序算出来的布局先把吸附满足掉，
    间距只大不小）。
    """
    prev: str | None = None
    cursor: float | None = None
    prev_gid: str | None = None
    for i in col:
        gid = items[i].gid
        if prev is None:                       # 列首：直接落在它的期望位置
            y = desired(i)
        elif gid == prev_gid:                  # 同组：跟着上一个往下，保持组内等距
            y = ys[prev] + items[prev].h + ROW_GAP_Y
        else:                                  # 换组：可以顶到自己的期望位置
            y = max(desired(i), cursor)
        if snap:
            y = snap_up(y, g)
        ys[i] = y
        cursor = y + items[i].h + ROW_GAP_Y
        if snap:
            cursor = snap_up(cursor, g)
        prev, prev_gid = i, gid


def _fmt_layout_component(
    comp: list[str],
    items: dict[str, _FmtNode],
    succ: dict,
    pred: dict,
    y0: dict[str, float],
    g: float,
) -> tuple[dict[str, float], dict[str, float]]:
    """给一个连通分量排版，返回 `({id: 列起点 x}, {id: y})`（分量内坐标）。

    两阶段：先用**不带吸附**的几何值迭代排序（只为决定上下次序），再走一遍带吸附的
    落位。分开做是因为「排序看的是相对关系、落位要满足格点」，混在一起会让每轮迭代
    的键都带上取整噪声。
    """
    layer = _fmt_layers(comp, succ, pred)
    cols: dict[int, list[str]] = {}
    for i in comp:
        cols.setdefault(layer[i], []).append(i)

    # ---- 阶段 1：排序（用当前 y 当种子，等于"先尊重用户现在的上下关系"）----
    ys = dict(y0)
    base = min(y0[i] for i in comp)

    def want(i: str, ymap: dict[str, float]) -> float:
        """节点想待在哪儿：**跟它的上游的「顶」对齐**。

        - 处理节点的输出、单输入的节点：跟着那个上游的顶 → 链条拉成一条水平线。
        - **多输入**的节点（例如 5 路图一起进）跟**最上面那个输入**走，而不是取
          所有输入的均值。取均值的话每级都会往下漂半块 —— 实测同一张画布，
          跟最上面那个是 1892 高，取均值是 3310 高（见 §3.11）。
          代价是其余几路输入连进来的线是斜的（向上收拢）。
        - 源节点（没有上游）保留它原来的相对高低。
        """
        ps = [p for p in pred[i] if p in ymap]
        if not ps:
            return y0[i] - base
        return min(ymap[p] for p in ps)

    for _ in range(LAYOUT_SWEEPS):
        for L in sorted(cols):                                    # 下行：看上游
            cols[L] = _fmt_reorder(cols[L], pred, items, ys)
            _fmt_place(cols[L], items, ys, lambda i: want(i, ys), g, snap=False)
        for L in reversed(sorted(cols)):                          # 上行：看下游
            cols[L] = _fmt_reorder(cols[L], succ, items, ys)
            _fmt_place(cols[L], items, ys, lambda i: want(i, ys), g, snap=False)

    # ---- 阶段 2：正式落位（列起点 + 列内 y 都吸到格点）----
    colx: dict[str, float] = {}
    x = 0.0
    for L in sorted(cols):
        for i in cols[L]:
            colx[i] = x
        x = snap_up(x + max(items[i].w for i in cols[L]) + COL_GAP_X, g)

    placed: dict[str, float] = {}
    for L in sorted(cols):
        _fmt_place(cols[L], items, placed, lambda i: want(i, placed), g, snap=True)
    return colx, placed


def format_all(graph: dict, registry: dict, settings: dict | None) -> int:
    """把整张画布重新排版（画布右键 →「格式化所有节点」）。

    采用**分层列布局**（和蓝图那类自动整理插件是同一路数）：

    1. **分层**：`层(v) = 1 + max(层(前驱))` → 每一层是一列，边永远从左指向右，
       输出节点天然落在自己处理节点的右边一列（和 `slot_origin` 的语义一致）。
    2. **列宽**：一列里最宽的节点决定这一列，下一列从 `列起点 + 列宽 + COL_GAP_X`
       开始 —— 列与列横向永不相交，这是「零重叠」的一半保证。
    3. **列内排序**：按上游/下游节点的重心迭代排（减少连线交叉）；同一个处理节点的
       多个输出**绑成一组**，连排 + 序号递增。
    4. **列内落位**：一列里逐个按"上一个的底 + `ROW_GAP_Y`"排开，整块**从锚点往下挂**
       （锚点 = 这一列最上面那个节点的期望位置，见 `_fmt_place`）。同列永不重叠 ——
       这是「零重叠」的另一半保证。
    5. **分量**：互不相连的子图分开排，再按原来的上下顺序纵向堆叠，全部左对齐。
    6. **锚点**：整体左上角对齐到**原来的包围盒左上角**，视野不会跳到别处；
       吸附开着时所有坐标都吸到格点（向上吸，间距只大不小，见 §3.10）。

    你担心的「第 n 个输出会不会压住第 n-1 个的下游」在这里不会发生：同一处理节点的
    输出永远在同一列（纵向只要按各自高度累加），而它们的下游永远在更右边的列里，
    横向就把地方让开了。真正会挤的只有「同一个处理节点下挂多棵子树」，那由第 3、4
    步一起分摊掉。

    返回被移动的节点数。
    """
    items = _fmt_collect(graph, registry, settings)
    if len(items) < 2:
        return 0
    succ, pred = _fmt_links(items, graph)
    comps = _fmt_components(list(items), succ, pred)
    # 分量顺序：照它们现在的位置从上往下、从左往右 —— 尽量贴近用户本来的排布
    comps.sort(
        key=lambda c: (
            min(_as_float(items[i].node.get("y")) for i in c),
            min(_as_float(items[i].node.get("x")) for i in c),
        )
    )
    # 原来整块画布的左上角（"锚点"），排版后要对回这里
    anchor_x = min(_as_float(n.node.get("x")) for n in items.values())
    anchor_y = min(_as_float(n.node.get("y")) for n in items.values())

    g = grid_size(settings)
    pos: dict[str, tuple[float, float]] = {}
    cursor = anchor_y
    for comp in comps:
        y0 = {i: _as_float(items[i].node.get("y")) for i in comp}
        colx, ys = _fmt_layout_component(comp, items, succ, pred, y0, g)
        top = min(ys[i] for i in comp)
        bottom = max(ys[i] + items[i].h for i in comp)
        for i in comp:
            pos[i] = (anchor_x + colx[i], snap_up(cursor + (ys[i] - top), g))
        cursor = snap_up(cursor + (bottom - top) + COMPONENT_GAP_Y, g)

    moved = 0
    for i, (x, y) in pos.items():
        node = items[i].node
        if _as_float(node.get("x")) != x or _as_float(node.get("y")) != y:
            node["x"], node["y"] = x, y
            moved += 1
    return moved


def snapshot_positions(graph: dict) -> list[dict]:
    """记下所有节点的坐标，供「撤销格式化」用（§3.11）。"""
    return [
        {"id": n["id"], "x": n.get("x"), "y": n.get("y")}
        for n in (*graph.get("resources", []), *graph.get("processes", []))
    ]


def restore_positions(graph: dict, snapshot: list[dict]) -> int:
    """把快照写回图里（只认还活着的节点；期间被删掉的直接跳过）。

    返回真正被挪回去的节点数。
    """
    by_id = {n["id"]: n for n in (*graph.get("resources", []), *graph.get("processes", []))}
    moved = 0
    for it in snapshot or []:
        node = by_id.get(it.get("id"))
        if node is None:
            continue
        if node.get("x") != it.get("x") or node.get("y") != it.get("y"):
            node["x"], node["y"] = it.get("x"), it.get("y")
            moved += 1
    return moved


# --------------------------------------------------------------------------
# 并行判据：两个节点能不能同时动
# --------------------------------------------------------------------------
def dep_maps(graph: dict) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """整张图的 前驱 / 后继，**含隐式边**（处理节点 → 它产出的数据节点）。

    ⚠️ 画布上的 `graph["edges"]` 只有「数据节点 → 处理节点」一种。光看它，
    任意两个**处理节点**之间都不存在路径 —— 拿来做可达性判断会把所有节点都
    当成互不相关（包括直接上下游）。所以必须把 producer 那条边补上。
    排版用的 `_fmt_links()` 是同一份口径，两处要一起改。
    """
    succ: dict[str, list[str]] = {}
    pred: dict[str, list[str]] = {}
    for n in (*graph.get("resources", []), *graph.get("processes", [])):
        succ.setdefault(n["id"], [])
        pred.setdefault(n["id"], [])
    for e in graph.get("edges", []):
        a, b = e.get("from"), e.get("to")
        if a in succ and b in succ:
            succ[a].append(b)
            pred[b].append(a)
    for r in graph.get("resources", []):
        pid = r.get("producer")
        if pid in succ and r["id"] in succ:
            succ[pid].append(r["id"])
            pred[r["id"]].append(pid)
    return succ, pred


def closure(graph: dict, ids) -> set[str]:
    """`ids` 自己 + 它们的**全部上游** + **全部下游**（DESIGN.md §2.2.5）。

    这就是「`ids` 正在执行时，哪一堆节点不许动」。两个节点能同时动，当且仅当
    **谁都不在对方的闭包里** —— 也就是互不为上下游。

    ⚠️ 这不等于「闭包不相交」：兄弟节点共用同一个输入图，闭包在**那个输入**上
    必然相交，但兄弟本来就是能并行的（都只读那张图）。相交只说明「那张输入图
    不许动」，不说明兄弟不能同时跑。

    注意**包含自己**：上游/下游是严格关系，判不出「自己」。而自己正在跑的时候
    尤其不能重入（会抢同一批输出名、互相覆盖文件）。
    """
    succ, pred = dep_maps(graph)
    seeds = {i for i in ids if i in succ}
    out = set(seeds)
    for nb in (succ, pred):          # 分别往上、往下，不能交叉走（斜角不算相关）
        stack = list(seeds)
        while stack:
            cur = stack.pop()
            for nxt in nb.get(cur, ()):
                if nxt not in out:
                    out.add(nxt)
                    stack.append(nxt)
    return out


# --------------------------------------------------------------------------
# 运行状态（供 SSE 使用）
# --------------------------------------------------------------------------
class RunState:
    def __init__(self, run_id: str, process_id: str, canvas_id: str = ""):
        self.id = run_id
        self.process_id = process_id
        self.canvas_id = canvas_id
        self.lines: list[str] = []
        self.status = "running"          # running | done | error
        self.error: str | None = None
        self.finished = False
        self._lock = threading.Lock()

    def emit(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    def snapshot(self, start: int = 0) -> tuple[list[str], bool, str, str | None]:
        with self._lock:
            return list(self.lines[start:]), self.finished, self.status, self.error


class RunManager:
    """所有正在跑 / 刚跑完的执行。

    并行度只有一处依据，就是这里：`active()` 里还在跑的节点，它们的上下游闭包
    才是「不许动」的范围（见 `closure`）。判据一律走**内存**，不看画布上那个
    持久化的 `status` —— 服务崩一次，磁盘上的 status 会永远停在 running。
    """

    KEEP_FINISHED = 64        # 跑完的留着供日志用，超过这个数就丢掉最老的

    def __init__(self):
        self.runs: dict[str, RunState] = {}

    def create(self, canvas_id: str, process_id: str) -> RunState:
        rid = uuid.uuid4().hex[:12]
        st = RunState(rid, process_id, canvas_id)
        self.runs[rid] = st
        done = [r for r in self.runs.values() if r.finished]
        if len(done) > self.KEEP_FINISHED:
            for r in done[: len(done) - self.KEEP_FINISHED]:
                self.runs.pop(r.id, None)
        return st

    def get(self, rid: str) -> RunState | None:
        return self.runs.get(rid)

    def drop(self, rid: str) -> None:
        self.runs.pop(rid, None)

    # ---------------- 并行判定 ----------------
    def active(self) -> list[RunState]:
        return [r for r in self.runs.values() if not r.finished]

    def count(self) -> int:
        return len(self.active())

    def active_pids(self, canvas_id: str) -> set[str]:
        """这个画布上正在跑的节点 id。"""
        return {r.process_id for r in self.active() if r.canvas_id == canvas_id}

    def is_active(self, canvas_id: str, process_id: str) -> bool:
        """这个节点自己正在跑（重入判定 —— 上下游判据判不出自己）。"""
        return any(
            not r.finished and r.canvas_id == canvas_id and r.process_id == process_id
            for r in self.runs.values()
        )

    def active_in(self, canvas_id: str) -> bool:
        """这个画布上有没有任何执行中的动作（画布改名/删除的判据）。"""
        return any(r.canvas_id == canvas_id for r in self.active())



# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _resource(graph: dict, rid: str) -> dict | None:
    return next((r for r in graph["resources"] if r["id"] == rid), None)


def _process(graph: dict, pid: str) -> dict | None:
    return next((p for p in graph["processes"] if p["id"] == pid), None)


def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def connected_inputs(graph: dict, specs: list[dict], pid: str) -> dict[str, str | None]:
    """返回 {端口key: 数据节点id 或 None}。specs 来自 registry.port_specs()。"""
    out: dict[str, str | None] = {}
    for port in specs:
        edge = next(
            (e for e in graph["edges"] if e["to"] == pid and e["port"] == port["key"]),
            None,
        )
        out[port["key"]] = edge["from"] if edge else None
    return out


def _remove_resource(graph: dict, rid: str) -> None:
    graph["resources"] = [r for r in graph["resources"] if r["id"] != rid]
    graph["edges"] = [e for e in graph["edges"] if e["from"] != rid and e["to"] != rid]


def remove_process(graph: dict, pid: str, ws: CanvasView) -> None:
    """删除处理节点：连带删除它的输出数据节点、文件、以及所有相关连线。"""
    for r in [r for r in graph["resources"] if r.get("producer") == pid]:
        ws.delete_image(r["name"])
        _remove_resource(graph, r["id"])
    graph["processes"] = [p for p in graph["processes"] if p["id"] != pid]
    graph["edges"] = [e for e in graph["edges"] if e["from"] != pid and e["to"] != pid]


def remove_resource_node(graph: dict, rid: str, ws: CanvasView) -> None:
    r = _resource(graph, rid)
    if r is None:
        return
    ws.delete_image(r["name"])
    _remove_resource(graph, rid)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
class _Prepared:
    """准备阶段备好的东西。

    跑脚本只要 `run_dir` / `script` / `payload`；收尾还要 `proc` / `manifest`。
    中间那段（子进程全程）**不碰 graph**，所以才能不持锁。
    """

    __slots__ = (
        "proc", "manifest", "specs", "params", "inputs", "run_dir", "payload", "script",
        "t0", "t_first", "t_exit",
    )

    def __init__(self, proc, manifest, specs, params, inputs, run_dir, payload, script):
        self.proc = proc
        self.manifest = manifest
        self.specs = specs
        self.params = params
        self.inputs = inputs
        self.run_dir = run_dir
        self.payload = payload
        self.script = script
        # 三个时刻，把「界面上那个耗时」拆成 启动 / 子进程 / 收尾 三段：
        #   t0      —— 拉起子进程之前（lastDuration 从这儿算）
        #   t_first —— 子进程吐出第一行日志（= 解释器冷启动 + import + 读 payload）
        #   t_exit  —— 子进程退出（之后只剩扫产出、搬文件、写状态）
        # 脚本内部各段由脚本自己打 `[计时]`，这边只负责它量不到的两头（见 _emit_timing）。
        self.t0 = 0.0
        self.t_first = 0.0
        self.t_exit = 0.0


def execute(
    ws: CanvasView,
    graph: dict,
    registry: dict[str, dict],
    pid: str,
    st: RunState,
    settings: dict | None = None,
    lock=None,
    alive=None,
) -> tuple[bool, str | None]:
    """跑一个处理节点。**锁只包住前后两段，不包住子进程。**

    这是并行度的全部关键（DESIGN.md §2.2.5）：准备阶段要读图（校验输入、算参数、
    挑尺寸），收尾阶段要写图（重绑槽位、搬文件、标脏），两段都必须持锁 ——
    但都是毫秒级的；中间等子进程的 10~60 秒**一点锁都不拿**，互不相关的节点
    才可能同时跑。

    `lock` 由主程序传进来（可重入的全局锁），不传就是不加锁（纯单测用）。
    `alive()` 在收尾前确认这个节点所在的画布还在 —— 画布被删了就别往里面写。
    """
    gate = lock if lock is not None else contextlib.nullcontext()

    t_enter = time.perf_counter()
    with gate:
        prep, err = _prepare(ws, graph, registry, pid, st, settings)
    if prep is None:
        return False, err
    st.emit(
        f"[engine][计时] 准备（持锁：校验输入 / 算尺寸 / 开临时目录）: "
        f"{time.perf_counter() - t_enter:.2f} 秒 —— 发生在节点耗时起算之前，不在那个数里"
    )

    produced, err = _run(prep, st)          # ← 这一段不持锁
    if produced is None:
        return False, err

    with gate:
        return _commit(ws, graph, prep, produced, st, settings, alive)


def _prepare(
    ws: CanvasView,
    graph: dict,
    registry: dict[str, dict],
    pid: str,
    st: RunState,
    settings: dict | None = None,
) -> tuple[_Prepared | None, str | None]:
    """持锁阶段一：读图 + 校验 + 算参数 + 开临时目录。只读，不外写。"""
    emit = st.emit

    proc = _process(graph, pid)
    if proc is None:
        return None, "处理节点不存在"
    manifest = registry.get(proc.get("defId", ""))
    if manifest is None:
        return None, f"未注册的脚本: {proc.get('defId')}"

    ws.ensure()
    specs = registry_mod.port_specs(manifest, proc)
    inputs_map = connected_inputs(graph, specs, pid)

    # ---- 校验必填输入 ----
    missing: list[str] = []
    for port in specs:
        rid = inputs_map.get(port["key"])
        label = port.get("label") or port["key"]
        if not rid:
            if port.get("required"):
                missing.append(label)
            continue
        res = _resource(graph, rid)
        if res is None or ws.image_path(res["name"]) is None:
            missing.append(label)
    if missing:
        return None, "输入缺失: " + "、".join(missing)

    # ---- 组装参数 ----
    # 提示词原样传给脚本，不做任何隐式改写：用户在提示词里直接写「参考图1」
    # 「参考图2」，与节点上的端口标签一一对应（见 DESIGN.md §2.4）。
    params = dict(proc.get("params") or {})

    # ---- 组装输入绝对路径 ----
    inputs: dict[str, str | None] = {}
    for port in specs:
        rid = inputs_map.get(port["key"])
        if not rid:
            inputs[port["key"]] = None
            continue
        res = _resource(graph, rid)
        p = ws.image_path(res["name"]) if res else None
        inputs[port["key"]] = str(p) if p else None

    # ---- 「尺寸 = 自动」在这里落地 ----
    # 空值**不是**「不发尺寸」，而是「按最后一张参考图就近挑一个预设」，然后
    # 当成用户**显式选的**发给脚本 —— 脚本因此完全不需要知道「自动」这回事，
    # 它那边只有一条「有 size 就发、没有就不发」的老逻辑（见 autosize.py 的模块说明）。
    # 同一份解析也在 /api/state 里跑（main._auto_size_hint），两处口径必须一致。
    # 尺寸参数可以按后端分两套（size / size_gpt），所以先问 model 属于哪个后端。
    backend = registry_mod.backend_of(manifest, params.get("model"))
    auto_key = autosize.auto_key(manifest, backend)
    if auto_key and not str(params.get(auto_key) or "").strip():
        ref_dims: tuple[int, int] | None = None
        ref_label: str | None = None
        # 倒序 = 从最后一张参考图往前找，与 DashScope「多图输入取最后一张」一致
        for port in reversed(specs):
            p_str = inputs.get(port["key"])
            if not p_str:
                continue
            dims = autosize.dims_of_path(p_str)
            if dims:
                ref_dims, ref_label = dims, port.get("label") or port["key"]
                break
        value, info = autosize.resolve(manifest, params, ref_dims, backend)
        if value:
            params[auto_key] = value
        if info:
            if ref_dims:
                emit(
                    f"[尺寸] 自动 → {info['label']}"
                    f"（依据 {ref_label} {ref_dims[0]}×{ref_dims[1]}）"
                )
            else:
                emit("[尺寸] 自动 → 无参考图，交回模型自选")

    # ---- 临时目录 ----
    run_dir = ws.tmp_dir / uuid.uuid4().hex[:12]
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"params": params, "inputs": inputs, "output_dir": str(run_dir)}

    script = Path(manifest["_dir"]) / manifest.get("entry", "")
    if not script.is_file():
        drop_tree(run_dir)
        return None, f"脚本文件不存在: {script}"

    emit(f"[engine] 执行「{manifest.get('name', proc['defId'])}」")
    # 列出每个输入口挂的是哪张图，方便对照提示词里写的「参考图N」
    linked = []
    for port in specs:
        rid = inputs_map.get(port["key"])
        res = _resource(graph, rid) if rid else None
        if res:
            linked.append(f"{port['label']} = {res['name']}")
    if linked:
        emit(f"[engine] 输入: {'，'.join(linked)}")
    emit(f"[engine] 参数: {json.dumps(params, ensure_ascii=False)}")
    emit("")

    return (
        _Prepared(proc, manifest, specs, params, inputs, run_dir, payload, script),
        None,
    )


def _run(prep: _Prepared, st: RunState) -> tuple[list[Path] | None, str | None]:
    """**不持锁**阶段：起子进程、收日志、等它退出、收集产出。

    这一段只碰自己那次执行的临时目录，所以可以放心地跑几十秒。
    """
    emit = st.emit
    script = prep.script
    run_dir = prep.run_dir
    payload = prep.payload

    # 从这里开始计时，覆盖子进程全程（含模型调用与图片下载）
    prep.t0 = time.perf_counter()
    # ---- 启动子进程 ----
    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        p = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(run_dir),
            env=dict(os.environ),
            creationflags=creationflags,
        )
    except Exception as e:  # noqa: BLE001
        drop_tree(run_dir)
        return None, f"无法启动脚本: {e}"

    try:
        p.stdin.write(json.dumps(payload, ensure_ascii=False))
        p.stdin.close()
    except Exception:  # noqa: BLE001
        pass

    err_lines: list[str] = []

    def drain_stderr() -> None:
        try:
            for line in p.stderr:  # type: ignore[union-attr]
                err_lines.append(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    t_err = threading.Thread(target=drain_stderr, daemon=True)
    t_err.start()

    deadline = time.time() + RUN_TIMEOUT_SEC
    try:
        for line in p.stdout:  # type: ignore[union-attr]
            if not prep.t_first:
                # 首行到达 = 解释器起来了、模块 import 完了、payload 读进去了。
                # 这一段本地开销（Windows 上尤其明显）脚本自己量不到，只能从外面掐。
                prep.t_first = time.perf_counter()
                emit(
                    f"[engine][计时] 启动（spawn → 首行日志）: "
                    f"{prep.t_first - prep.t0:.2f} 秒"
                )
            emit(line.rstrip())
            if time.time() > deadline:
                p.kill()
                raise TimeoutError
    except TimeoutError:
        drop_tree(run_dir)
        return None, f"执行超时（>{RUN_TIMEOUT_SEC}s）"
    except Exception:  # noqa: BLE001
        pass

    rc = p.wait()
    prep.t_exit = time.perf_counter()
    t_err.join(timeout=5)

    if rc != 0:
        drop_tree(run_dir)
        tail = "\n".join(err_lines[-30:]).strip()
        return None, tail or f"脚本以退出码 {rc} 结束"

    # ---- 收集产出 ----
    produced = sorted(
        [f for f in run_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS],
        key=lambda f: _natural_key(f.name),
    )
    if not produced:
        drop_tree(run_dir)
        err = "\n".join(err_lines[-10:]).strip()
        return None, ("脚本成功退出但没有产出图片。" + ("\n" + err if err else ""))

    return produced, None


def _emit_timing(emit, prep: "_Prepared", t_done: float) -> None:
    """把 `lastDuration` 拆成三段打出来，和脚本内那张时间轴对账。

    三段是**互斥且刚好拼满**的（都是同一把 `perf_counter` 上的时刻相减）：

        启动   t0 → t_first   拉起进程 + 解释器冷启动 + import + 读 payload
        子进程 t_first → t_exit  脚本内部各段（生成请求 / 下载 / 写盘…）
        收尾   t_exit → t_done   扫产出 + 搬文件进图片池 + 写状态

    子进程那段能再往下拆，看脚本自己打的 `[计时]` —— 引擎不解析日志，
    也正因为不解析，这段对任何脚本都通用（DESIGN.md §2.2.2）。
    """
    t_first = prep.t_first or prep.t0
    t_exit = prep.t_exit or t_first
    launch = t_first - prep.t0
    child = t_exit - t_first
    settle = t_done - t_exit
    emit(
        f"[engine][计时] 合计 {settle + child + launch:.1f} 秒 = "
        f"启动 {launch:.2f} + 子进程 {child:.1f} + 收尾 {settle:.2f}"
    )


def _commit(
    ws: CanvasView,
    graph: dict,
    prep: _Prepared,
    produced: list[Path],
    st: RunState,
    settings: dict | None = None,
    alive=None,
) -> tuple[bool, str | None]:
    """持锁阶段二：重绑槽位、搬文件、标脏、写状态。

    跑完到写回之间隔了几十秒，这期间节点可能被删掉、画布可能没了 —— 提交前
    先确认它还在，不在就把结果丢掉，绝不要把已经删掉的节点「复活」出来。
    """
    emit = st.emit
    proc = prep.proc
    manifest = prep.manifest
    pid = proc["id"]
    run_dir = prep.run_dir

    if (alive is not None and not alive()) or _process(graph, pid) is None:
        drop_tree(run_dir)
        return False, "节点或画布已被删除，本次结果丢弃"

    # ---- 槽位重绑定 ----
    existing = {r.get("index"): r for r in graph["resources"] if r.get("producer") == pid}

    # 多出来的槽位：直接移除（文件永久删除，连线断开）
    for idx in sorted([i for i in existing if i >= len(produced)], reverse=True):
        r = existing.pop(idx)
        ws.delete_image(r["name"])
        _remove_resource(graph, r["id"])
        emit(f"[engine] 输出变少，第 {idx + 1} 个槽位已被移除")

    base_x, cursor_y = slot_origin(proc, manifest, settings)

    for i, src in enumerate(produced):
        r = existing.get(i)
        if r is None:
            r = {
                "id": new_id("r"),
                "name": ws.alloc_name(graph),
                "kind": "output",
                "producer": pid,
                "index": i,
                "x": base_x,
                "y": cursor_y,
            }
            graph["resources"].append(r)
            emit(f"[engine] 新增输出槽位 #{i} → 图片「{r['name']}」")
        else:
            ws.delete_image(r["name"])  # 原地刷新：删掉旧文件
        # 下一个槽位从**这一个节点自己的位置和高度**往下让开一点点：
        # 已有槽位的位置不会被改动（用户手动拖过就尊重用户），但间距得按它的实际
        # 高度算，否则节点调矮之后中间会空出一大截。吸附开着时向上吸到格点，
        # 保证间距不小于 SLOT_GAP_Y。
        cursor_y = snap_up(
            _as_float(r.get("y"), cursor_y) + resource_height(r, settings) + SLOT_GAP_Y,
            grid_size(settings),
        )
        dest = ws.image_dir(create=True) / f"{r['name']}{src.suffix.lower()}"
        shutil.move(str(src), str(dest))

    drop_tree(run_dir)

    proc["status"] = "done"
    proc["error"] = None
    proc["dirty"] = False
    proc["lastRun"] = time.strftime("%Y-%m-%d %H:%M:%S")
    t_done = time.perf_counter()
    proc["lastDuration"] = round(t_done - prep.t0, 1)
    _emit_timing(emit, prep, t_done)

    # ---- R5：直接下游处理节点标脏（不递归）----
    produced_ids = {r["id"] for r in graph["resources"] if r.get("producer") == pid}
    for e in graph["edges"]:
        if e["from"] in produced_ids:
            d = _process(graph, e["to"])
            if d is not None:
                d["dirty"] = True

    emit("")
    emit(f"[engine] 完成，共 {len(produced)} 张输出")
    return True, None
