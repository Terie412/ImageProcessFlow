# -*- coding: utf-8 -*-
"""执行引擎：子进程调用脚本、临时目录、原子搬运、槽位重绑定、脏标记。

对应 DESIGN.md §2.2 与 §2.5：
- 执行 P 只跑 P（无级联），全部手动执行
- 数据节点是输出槽位的投影；原地刷新（id / 坐标不变）；槽位变少则直接移除文件
- 失败时保留旧输出（因为走临时目录，成功后才搬运）
- 执行后把直接下游处理节点标脏，不递归传递
"""
from __future__ import annotations

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
from pathlib import Path

from workspace import IMAGE_EXTS, Workspace, drop_tree
import registry as registry_mod

RUN_TIMEOUT_SEC = 900

# 输出槽位落点：**按节点实际尺寸推算**，不能写死偏移量。
# 早期写死 380/210，那是按 280 宽的处理节点算的；节点一旦被调大（默认已到 600 宽、
# 数据节点 280 高），输出节点就会横向压住上游、纵向互相压。
# 纵向的间距同样不能写死 —— 见 `resource_height()`：必须按**数据节点的实际渲染高**
# 逐个累加，否则用户把数据节点调矮之后，每两个节点之间会空出一大截。
PROCESS_DEFAULT_W = 600      # 与前端 PROCESS_SIZE.w 一致
RESOURCE_DEFAULT_H = 280     # 与前端 RESOURCE_SIZE.h 一致
SLOT_GAP_X = 64              # 输出节点与处理节点之间的横向留白
SLOT_GAP_Y = 10              # 相邻输出槽位之间的纵向留白（用户手动排布时用的就是这个量级）


def _as_float(v, dflt: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _node_min(settings: dict | None, key: str, dflt: tuple[float, float]) -> tuple[float, float]:
    """读设置里的最小尺寸；处理节点没有专属条目时退回通用的 `process`。"""
    nm = (settings or {}).get("nodeMin") or {}
    spec = nm.get(key)
    if not isinstance(spec, dict):
        spec = nm.get("process") if key.startswith("process:") else None
    if not isinstance(spec, dict):
        return dflt
    return (
        _as_float(spec.get("w")) or dflt[0],
        _as_float(spec.get("h")) or dflt[1],
    )


def resource_height(r: dict | None, settings: dict | None) -> float:
    """数据节点的**实际渲染高度**。

    必须和前端 App.jsx 的算法保持一致：`max(设置里的下限, 存的 h ?? RESOURCE_SIZE.h)`
    —— 存的 h 是用户拖右下角改过的大小，没存过才用默认 280。

    这个值决定相邻输出槽位的纵向间距。**不能写死 280**：用户把数据节点调矮（例如
    180）之后，还按 280 排就会在每个节点之间空出一大截（310 - 180 = 130）。
    """
    min_h = _node_min(settings, "resource", (0.0, RESOURCE_DEFAULT_H))[1]
    return max(min_h, _as_float((r or {}).get("h")) or RESOURCE_DEFAULT_H)


def slot_origin(proc: dict, manifest: dict, settings: dict | None) -> tuple[float, float]:
    """输出槽位的左上起点：(x, y)。

    横向按**处理节点的实际宽度**让开（不写死偏移量）；纵向的累加见
    `resource_height()` —— 每个槽位从上**一个节点的实际高度**往下排。

    吸附开着时用**向上**吸附：横向的 `+SLOT_GAP_X`、纵向的 `+SLOT_GAP_Y`
    都是下限，就近吸附会把它们啃掉几个像素。
    """
    min_w, _ = _node_min(
        settings, f"process:{proc.get('defId')}", (PROCESS_DEFAULT_W, RESOURCE_DEFAULT_H)
    )
    sz = manifest.get("size") or {}
    proc_w = max(
        min_w,
        _as_float(proc.get("w")) or _as_float(sz.get("defaultWidth")) or PROCESS_DEFAULT_W,
    )
    g = grid_size(settings)
    return (
        snap_up(proc.get("x", 0) + proc_w + SLOT_GAP_X, g),
        snap_up(proc.get("y", 0), g),
    )


# --------------------------------------------------------------------------
# 网格吸附
# --------------------------------------------------------------------------
GRID_SIZE_RANGE = (1, 200)   # 吸附格的允许区间（px），前后端保持一致


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

    给「程序算出来的布局」用（输出槽位、重排）：这类位置有一条**下限**约束
    （相邻间距不能小于 SLOT_GAP_Y，否则节点会压在一起），就近吸附会把间距
    啃掉最多半个格。向上取整则保证间距只会变大一点点、绝不会变小。
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
    连「脚本执行时引擎自己摆的输出槽位」「重排输出节点」「导入图片」落下
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


def tidy_outputs(graph: dict, registry: dict, settings: dict | None) -> int:
    """把所有输出数据节点重新摆到各自处理节点的右侧、按槽位序号纵向排开。

    纵向间距按**每个节点自己的高度**逐个累加，所以高低不一的一组也能紧凑排好。

    吸附开着时，累加出来的位置**向上**吸到格点：先满足吸附规则，再尽量让间距
    不小于 `SLOT_GAP_Y`（就近吸附可能把 10px 的缝啃成 7px，向上取整则只会变成
    12~14px）。**所有会挪动节点的功能都按这个优先级来**，见 DESIGN.md §3.10。

    返回被移动的节点数。用于收拾历史上被写死偏移量摆歪的画布。
    """
    g = grid_size(settings)
    procs = {p["id"]: p for p in graph.get("processes", [])}
    groups: dict[str, list[dict]] = {}
    for r in graph.get("resources", []):
        if r.get("producer"):
            groups.setdefault(r["producer"], []).append(r)

    moved = 0
    for pid, items in groups.items():
        proc = procs.get(pid)
        if proc is None:
            continue
        base_x, y = slot_origin(proc, registry.get(proc.get("defId")) or {}, settings)
        for r in sorted(items, key=lambda x: x.get("index", 0)):
            if _as_float(r.get("x")) != base_x or _as_float(r.get("y")) != y:
                r["x"], r["y"] = base_x, y
                moved += 1
            y = snap_up(y + resource_height(r, settings) + SLOT_GAP_Y, g)
    return moved


# --------------------------------------------------------------------------
# 运行状态（供 SSE 使用）
# --------------------------------------------------------------------------
class RunState:
    def __init__(self, run_id: str, process_id: str):
        self.id = run_id
        self.process_id = process_id
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
    def __init__(self):
        self.runs: dict[str, RunState] = {}

    def create(self, process_id: str) -> RunState:
        rid = uuid.uuid4().hex[:12]
        st = RunState(rid, process_id)
        self.runs[rid] = st
        return st

    def get(self, rid: str) -> RunState | None:
        return self.runs.get(rid)

    def drop(self, rid: str) -> None:
        self.runs.pop(rid, None)


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


def remove_process(graph: dict, pid: str, ws: Workspace) -> None:
    """删除处理节点：连带删除它的输出数据节点、文件、以及所有相关连线。"""
    for r in [r for r in graph["resources"] if r.get("producer") == pid]:
        ws.delete_image(r["name"])
        _remove_resource(graph, r["id"])
    graph["processes"] = [p for p in graph["processes"] if p["id"] != pid]
    graph["edges"] = [e for e in graph["edges"] if e["from"] != pid and e["to"] != pid]


def remove_resource_node(graph: dict, rid: str, ws: Workspace) -> None:
    r = _resource(graph, rid)
    if r is None:
        return
    ws.delete_image(r["name"])
    _remove_resource(graph, rid)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def execute(
    ws: Workspace,
    graph: dict,
    registry: dict[str, dict],
    pid: str,
    st: RunState,
    settings: dict | None = None,
) -> tuple[bool, str | None]:
    emit = st.emit

    proc = _process(graph, pid)
    if proc is None:
        return False, "处理节点不存在"
    manifest = registry.get(proc.get("defId", ""))
    if manifest is None:
        return False, f"未注册的脚本: {proc.get('defId')}"

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
        return False, "输入缺失: " + "、".join(missing)

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

    # ---- 临时目录 ----
    run_dir = ws.tmp_dir / uuid.uuid4().hex[:12]
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"params": params, "inputs": inputs, "output_dir": str(run_dir)}

    script = Path(manifest["_dir"]) / manifest.get("entry", "")
    if not script.is_file():
        drop_tree(run_dir)
        return False, f"脚本文件不存在: {script}"

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

    # 从这里开始计时，覆盖子进程全程（含模型调用与图片下载）
    t0 = time.perf_counter()

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
        return False, f"无法启动脚本: {e}"

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
            emit(line.rstrip())
            if time.time() > deadline:
                p.kill()
                raise TimeoutError
    except TimeoutError:
        drop_tree(run_dir)
        return False, f"执行超时（>{RUN_TIMEOUT_SEC}s）"
    except Exception:  # noqa: BLE001
        pass

    rc = p.wait()
    t_err.join(timeout=5)

    if rc != 0:
        drop_tree(run_dir)
        tail = "\n".join(err_lines[-30:]).strip()
        return False, tail or f"脚本以退出码 {rc} 结束"

    # ---- 收集产出 ----
    produced = sorted(
        [f for f in run_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS],
        key=lambda f: _natural_key(f.name),
    )
    if not produced:
        drop_tree(run_dir)
        err = "\n".join(err_lines[-10:]).strip()
        return False, ("脚本成功退出但没有产出图片。" + ("\n" + err if err else ""))

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
        dest = ws.images_dir / f"{r['name']}{src.suffix.lower()}"
        shutil.move(str(src), str(dest))

    drop_tree(run_dir)

    proc["status"] = "done"
    proc["error"] = None
    proc["dirty"] = False
    proc["lastRun"] = time.strftime("%Y-%m-%d %H:%M:%S")
    proc["lastDuration"] = round(time.perf_counter() - t0, 1)

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
