# -*- coding: utf-8 -*-
"""本地服务：FastAPI 路由 + SSE 日志推送。

对应 DESIGN.md §2.3：纯本地，只监听 127.0.0.1。
开发期用浏览器直连本服务；交付期可用 pywebview 套壳（同一份后端）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import detect as detect_mod
import engine as engine_mod
import polish as polish_mod
import registry as registry_mod
from workspace import IMAGE_EXTS, Workspace, drop_tree

try:
    from PIL import Image as _PILImage
except Exception:  # noqa: BLE001
    _PILImage = None

APP_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = APP_ROOT / "scripts"
STATIC_DIR = APP_ROOT / "server" / "static"

LOCK = threading.RLock()
RUNS = engine_mod.RunManager()

# --------------------------------------------------------------------------
# 应用级设置
# --------------------------------------------------------------------------
# 与工作目录无关，存在项目根目录的 settings.json 里 —— 换工作目录也保持同一套偏好。
SETTINGS_FILE = APP_ROOT / "settings.json"

DEFAULT_SETTINGS: dict = {
    # 删除数据节点（会永久删掉磁盘上的图片）前是否弹确认框
    "confirmDeleteDataNode": True,
    # 提示词润色用的文本模型：这里存的是「环境变量名」，真正的 key 运行时从环境变量取
    "textModelKeyEnv": "DEEPSEEK_API_KEY",
    # 润色用的模型名（DeepSeek 里只有它支持图片输入）
    "textModelName": "deepseek-v4-flash-vision-exp",
    # 上次打开过的工作目录：下次启动自动打开，省得每次都要重选一遍
    "lastWorkspace": "",
    # 网格吸附：开启后拖动节点会吸到格点上，落盘的坐标也一律对齐到格点
    "snapToGrid": False,
    # 吸附格大小（px）
    "gridSize": 5,
    # 每种节点的最小尺寸（画布坐标），按**具体节点类型**分：
    #   resource              → 数据节点
    #   process:<defId>       → 某个具体的处理节点类型（如 process:qwen_image）
    #   process               → 处理节点的兜底值（某类型还没有自己的条目时用它）
    # 放在设置里而不是 manifest 里：这是用户的个人偏好，不是脚本的属性。
    "nodeMin": {
        "resource": {"w": 200, "h": 180},
        "process": {"w": 560, "h": 280},
    },
}

# 最小尺寸的合理区间，防止填 0 或天文数字把节点搞没
NODE_MIN_RANGE = (80, 4000)


def _valid_node_min_key(k: str) -> bool:
    if k in ("resource", "process"):
        return True
    if not k.startswith("process:"):
        return False
    tail = k[len("process:"):]
    return 0 < len(tail) <= 64 and "/" not in tail and ":" not in tail


def _coerce_node_min(value, current: dict) -> dict | None:
    """校验 nodeMin：只认已知的 key，宽高取整并夹到合理区间。

    某个 `process:<defId>` 传 `null` 表示**删掉这一项**（回到通用 `process` 兜底）。
    `resource` / `process` 这两个基准项不允许删。
    """
    if not isinstance(value, dict):
        return None
    out = {k: dict(v) for k, v in (current or DEFAULT_SETTINGS["nodeMin"]).items()}
    for kind, spec in value.items():
        if not _valid_node_min_key(kind):
            continue
        if spec is None:
            if kind.startswith("process:"):
                out.pop(kind, None)
            continue
        if not isinstance(spec, dict):
            continue
        spec_out = dict(out.get(kind) or DEFAULT_SETTINGS["nodeMin"]["process"])
        for axis in ("w", "h"):
            if spec.get(axis) is None:
                continue
            try:
                n = int(round(float(spec[axis])))
            except (TypeError, ValueError):
                continue
            spec_out[axis] = max(NODE_MIN_RANGE[0], min(NODE_MIN_RANGE[1], n))
        out[kind] = spec_out
    return out


def _coerce_setting(key: str, value, current: dict):
    """校验/规范化一个设置项；不合法返回 None（调用方跳过这一项）。"""
    if key == "nodeMin":
        return _coerce_node_min(value, current.get("nodeMin"))
    if key == "gridSize":
        # 允许 "5" / 5 / 5.0 这类写法，取整后夹到合理区间 —— 0 会让吸附退化成空操作
        try:
            n = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        lo, hi = engine_mod.GRID_SIZE_RANGE
        return max(lo, min(hi, n))
    if isinstance(value, type(DEFAULT_SETTINGS[key])):
        return value.strip() if isinstance(value, str) else value
    return None


def load_settings() -> dict:
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # 只认已知键，且值要能通过校验，避免被写坏的值带崩前端
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    for k, v in raw.items():
        if k in DEFAULT_SETTINGS:
            clean = _coerce_setting(k, v, out)
            if clean is not None:
                out[k] = clean
    return out


def save_settings(s: dict) -> None:
    SETTINGS_FILE.write_text(
        json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


STATE: dict = {
    "workspace": None,   # type: Workspace | None
    "graph": None,
    "registry": {},
    "settings": load_settings(),
}

app = FastAPI(title="ImageProcessFlow", docs_url=None, redoc_url=None)


@app.exception_handler(Exception)
async def _unhandled(request, exc):  # noqa: ANN001, ANN201
    """兜底：没被接住的异常，把「哪一行炸的」直接回给界面。

    默认的 500 正文只有一句 Internal Server Error，弹到界面上等于没说，
    要查原因得回服务端控制台翻 traceback。这里把异常名 + 最后几帧塞进 detail，
    同时在控制台留一份完整堆栈。
    """
    tbs = traceback.format_exception(type(exc), exc, exc.__traceback__)
    print("[未捕获异常] " + "".join(tbs), flush=True)
    tail = "".join(tbs[-4:]).strip()[-1200:]
    return JSONResponse(
        {"detail": f"服务端异常 {type(exc).__name__}: {exc}\n{tail}"}, status_code=500
    )


# --------------------------------------------------------------------------
# 通用
# --------------------------------------------------------------------------
def ws() -> Workspace:
    w = STATE["workspace"]
    if w is None:
        raise HTTPException(409, "尚未选择工作目录")
    return w


def _migrate(g: dict) -> dict:
    """老画布兼容：补齐动态输入口的数量；已有连线指向的序号不会被凭空吃掉。"""
    for p in g.get("processes", []):
        m = STATE["registry"].get(p.get("defId"))
        if not m:
            continue

        # enum 参数的值若已不在选项里（manifest 改过，比如尺寸换了一批预设），
        # 就地回落到默认值 —— 否则界面上的下拉会是一片空白
        params = p.get("params")
        if isinstance(params, dict):
            for spec in m.get("params", []):
                if spec.get("type") != "enum":
                    continue
                allowed = [
                    (o if isinstance(o, str) else o.get("value"))
                    for o in spec.get("options", [])
                ]
                if params.get(spec["key"]) not in allowed:
                    params[spec["key"]] = spec.get("default")

        dyn = [s for s in m.get("inputs", []) if s.get("dynamic")]
        if not dyn:
            continue
        ports = p.setdefault("ports", {})
        for s in dyn:
            prefix = s["key"]
            ports.setdefault(prefix, int(s.get("count", 1)))
            n = int(ports[prefix])
            for e in g.get("edges", []):
                if e["to"] != p["id"] or not e["port"].startswith(prefix):
                    continue
                tail = e["port"][len(prefix):]
                if tail.isdigit():
                    n = max(n, int(tail))
            ports[prefix] = min(n, int(s.get("max", 99)))
    return g


def graph() -> dict:
    if STATE["graph"] is None:
        STATE["graph"] = _migrate(ws().load())
    return STATE["graph"]


def _open_workspace(path: str | Path) -> Workspace:
    """打开工作目录，并把它记进设置 —— 下次启动直接用这个路径。

    工作目录只活在内存里，重启就没了；不记下来的话每次都得在界面上重选一遍。
    """
    w = Workspace(path)
    w.ensure()
    STATE["workspace"] = w
    STATE["graph"] = _migrate(w.load())
    STATE["settings"]["lastWorkspace"] = str(w.root)
    save_settings(STATE["settings"])
    # 开着网格吸附的话，打开时顺手把没对齐的坐标收齐（老项目/别人传过来的画布）
    if STATE["settings"].get("snapToGrid"):
        persist()
    return w


def persist() -> None:
    """落盘画布。**所有**坐标写入都收口在这里，所以网格吸附也在这里统一执行。

    排在 save 之前，是为了让「存下去的就是画布上看到的」：开着吸附时，
    内存里的图与磁盘上的 json 都是对齐过的坐标。

    这里是**就近**吸附（最近的格点）—— 手动拖、导入图片这类「用户指哪打哪」
    的位置用就近；而程序算出来的布局（输出槽位、重排）在算的时候就用
    `engine.snap_up` 向上吸附，保证间距下限不被破坏（见 engine.snap_graph）。
    """
    n = engine_mod.snap_graph(STATE["graph"], STATE["settings"])
    if n:
        print(f"[画布] 网格吸附：{n} 个节点已就近对齐到格点")
    ws().save(STATE["graph"])


def reload_registry() -> None:
    STATE["registry"] = registry_mod.load_manifests(SCRIPTS_DIR)


def _decorate(g: dict) -> dict:
    """给数据节点补上图片文件信息（供前端做缓存失效）。"""
    w = STATE["workspace"]
    out = json.loads(json.dumps(g))
    for r in out["resources"]:
        r["hasImage"] = False
        r["mtime"] = 0
        r["width"] = None
        r["height"] = None
        r["format"] = None
        if w is not None:
            p = w.image_path(r.get("name", ""))
            if p is not None:
                r["hasImage"] = True
                r["mtime"] = int(p.stat().st_mtime)
                r["ext"] = p.suffix.lower()
                if _PILImage is not None:
                    try:
                        # Pillow 打开图片只读文件头，拿到尺寸和格式就关闭
                        with _PILImage.open(p) as im:
                            r["width"], r["height"] = im.size
                            r["format"] = (im.format or "").upper() or None
                    except Exception:  # noqa: BLE001
                        pass

    for p in out["processes"]:
        m = STATE["registry"].get(p.get("defId"))
        if m is None:
            p["portSpecs"] = []
            p["portGroups"] = []
            continue
        p["portSpecs"] = registry_mod.port_specs(m, p)
        p["portGroups"] = registry_mod.port_groups(m, p)
    return out


def state_payload() -> dict:
    w = STATE["workspace"]
    g = graph()
    return {
        "workspace": str(w.root) if w is not None else None,
        "workspaceDir": str(w.root) if w is not None else None,
        "graph": _decorate(g),
        "manifests": {k: registry_mod.public_manifest(v) for k, v in STATE["registry"].items()},
        "settings": dict(STATE["settings"]),
    }


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# 后台清理 .trash/
# --------------------------------------------------------------------------
_purge_lock = threading.Lock()
_purge_running = False


def schedule_purge() -> None:
    """把 .trash/ 的真正删除丢到后台线程。

    Windows 上 unlink 一个文件有 ~370ms 的固定开销（文件系统过滤驱动），
    而同卷 rename 只要 ~1ms。让请求等 unlink 会直接拖慢界面操作，
    所以 Workspace.delete_image 只做 rename，真删在这里延后完成。
    文件最终仍被永久删除，只是慢几百毫秒。
    """
    global _purge_running
    w = STATE["workspace"]
    if w is None:
        return
    with _purge_lock:
        if _purge_running:
            return
        _purge_running = True

    def run() -> None:
        global _purge_running
        try:
            for _ in range(200):  # 清理过程中可能又进来新的，最多连续跑 200 轮
                if w.purge_trash() == 0:
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            with _purge_lock:
                _purge_running = False

    threading.Thread(target=run, daemon=True, name="purge-trash").start()


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
@app.get("/api/state")
def api_state():
    return state_payload()


@app.post("/api/registry/reload")
def api_registry_reload():
    reload_registry()
    # 脚本换了以后，画布上可能残留已经失效的参数值，顺手修正
    with LOCK:
        STATE["graph"] = _migrate(graph())
        persist()
    return state_payload()


@app.patch("/api/settings")
def api_update_settings(payload: dict):
    with LOCK:
        s = STATE["settings"]
        for k, v in (payload or {}).items():
            if k not in DEFAULT_SETTINGS:
                continue
            clean = _coerce_setting(k, v, s)
            if clean is not None:
                s[k] = clean
        save_settings(s)
        # 吸附开着就顺手把整个画布收一遍 —— 这就是「开启之后检查所有节点」那一步
        if s.get("snapToGrid"):
            persist()
    return state_payload()


# --------------------------------------------------------------------------
# 工作目录
# --------------------------------------------------------------------------
@app.post("/api/workspace")
def api_set_workspace(payload: dict):
    path = (payload or {}).get("path", "").strip()
    if not path:
        raise HTTPException(400, "路径为空")
    with LOCK:
        _open_workspace(path)
    schedule_purge()
    return state_payload()


def _tk_pick_directory(initial: str | None) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass
    try:
        return filedialog.askdirectory(initialdir=initial or "") or None
    finally:
        root.destroy()


def _tk_save_as(initial_dir: str, initial_file: str) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass
    try:
        return filedialog.asksaveasfilename(
            initialdir=initial_dir, initialfile=initial_file, defaultextension=Path(initial_file).suffix
        ) or None
    finally:
        root.destroy()


@app.post("/api/workspace/pick")
def api_pick_workspace():
    initial = str(STATE["workspace"].root) if STATE["workspace"] else str(APP_ROOT)
    try:
        chosen = _tk_pick_directory(initial)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"无法打开系统对话框（可手动填写路径）：{e}") from e
    if not chosen:
        return {"cancelled": True}
    with LOCK:
        _open_workspace(chosen)
    schedule_purge()
    return state_payload()


@app.post("/api/workspace/export")
def api_export(payload: dict):
    """把工作目录里的图片复制到用户指定的位置（UI 上叫「下载」）。"""
    name = (payload or {}).get("name", "")
    w = ws()
    src = w.image_path(name)
    if src is None:
        raise HTTPException(404, f"图片不存在: {name}")
    try:
        dest = _tk_save_as(str(w.root), src.name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"无法打开系统对话框：{e}") from e
    if not dest:
        return {"cancelled": True}
    shutil.copy2(src, dest)
    return {"ok": True, "saved": dest}


# --------------------------------------------------------------------------
# 图片
# --------------------------------------------------------------------------
@app.get("/api/images/{name}")
def api_image(name: str):
    p = ws().image_path(name)
    if p is None:
        raise HTTPException(404, "图片不存在")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------
# 数据节点
# --------------------------------------------------------------------------
@app.post("/api/resources/import")
async def api_import_resource(
    file: UploadFile = File(...),
    resource_id: str = Form(""),
    x: float = Form(0),
    y: float = Form(0),
):
    """从外部拖入 / 选择图片 → 复制一份到工作目录，成为数据节点。

    若给了 resource_id，则表示「替换某个输入图的图片」（见 DESIGN.md §2.2.2 C8）。
    """
    w = ws()
    ext = Path(file.filename or "img.png").suffix.lower()
    if ext not in IMAGE_EXTS:
        ext = ".png"

    with LOCK:
        g = graph()
        if resource_id:
            r = next((r for r in g["resources"] if r["id"] == resource_id), None)
            if r is None:
                raise HTTPException(404, "数据节点不存在")
            w.delete_image(r["name"])
        else:
            r = {
                "id": new_id("r"),
                "name": w.alloc_name(g),
                "kind": "input",
                "x": x,
                "y": y,
            }
            g["resources"].append(r)

        dest = w.images_dir / f"{r['name']}{ext}"
        with dest.open("wb") as fh:
            while chunk := await file.read(1 << 20):
                fh.write(chunk)

        # R5 / C8：给直接下游处理节点打脏标记
        for e in g["edges"]:
            if e["from"] == r["id"]:
                d = engine_mod._process(g, e["to"])  # noqa: SLF001
                if d is not None:
                    d["dirty"] = True

        persist()
    schedule_purge()
    return state_payload()


@app.delete("/api/nodes/{node_id}")
def api_delete_node(node_id: str):
    with LOCK:
        g = graph()
        engine_mod.remove_process(g, node_id, ws())
        engine_mod.remove_resource_node(g, node_id, ws())
        persist()
    schedule_purge()
    return state_payload()


# --------------------------------------------------------------------------
# 处理节点
# --------------------------------------------------------------------------
@app.post("/api/processes")
def api_create_process(payload: dict):
    def_id = (payload or {}).get("defId")
    m = STATE["registry"].get(def_id)
    if m is None:
        raise HTTPException(404, f"未注册的脚本: {def_id}")
    with LOCK:
        g = graph()
        p = {
            "id": new_id("p"),
            "defId": def_id,
            "x": float(payload.get("x", 0)),
            "y": float(payload.get("y", 0)),
            "params": registry_mod.default_params(m),
            "ports": registry_mod.default_ports(m),
            "status": "idle",
            "dirty": False,
            "error": None,
            "lastRun": None,
        }
        g["processes"].append(p)
        persist()
    return state_payload()


@app.patch("/api/processes/{pid}")
def api_update_process(pid: str, payload: dict):
    with LOCK:
        g = graph()
        p = engine_mod._process(g, pid)  # noqa: SLF001
        if p is None:
            raise HTTPException(404, "处理节点不存在")
        if "params" in payload:
            p["params"] = {**(p.get("params") or {}), **(payload["params"] or {})}
            # R1：改参数不触发任何重算，只清掉上次的错误提示
            p["status"] = "idle" if p.get("status") == "error" else p.get("status", "idle")
            p["error"] = None
        if "ports" in payload:
            # 动态输入口数量（「＋」新增用；删除走 DELETE .../ports/{key}，要处理前移）
            p.setdefault("ports", {})
            for k, v in (payload["ports"] or {}).items():
                p["ports"][k] = int(v)
            m = STATE["registry"].get(p.get("defId")) or {}
            valid = {s["key"] for s in registry_mod.port_specs(m, p)}
            drop = {e["id"] for e in g["edges"] if e["to"] == pid and e["port"] not in valid}
            if drop:
                g["edges"] = [e for e in g["edges"] if e["id"] not in drop]
            # 输入集合变了 → 该节点自己变脏
            p["dirty"] = True
        if "x" in payload:
            p["x"] = float(payload["x"])
        if "y" in payload:
            p["y"] = float(payload["y"])
        persist()
    return state_payload()


@app.delete("/api/processes/{pid}/ports/{port_key}")
def api_delete_port(pid: str, port_key: str):
    """删除指定的动态输入口，其后的端口依次前移（参考图3 → 参考图2）。

    见 DESIGN.md §2.1：端口是按数量展开的，所以“删掉中间一个”等价于
    “数量 -1，且它后面的整体前移一格”。提示词里写的序号不会跟着改。
    """
    with LOCK:
        g = graph()
        p = engine_mod._process(g, pid)  # noqa: SLF001
        if p is None:
            raise HTTPException(404, "处理节点不存在")
        m = STATE["registry"].get(p.get("defId")) or {}
        specs = registry_mod.port_specs(m, p)
        hit = next((s for s in specs if s["key"] == port_key), None)
        if hit is None:
            raise HTTPException(404, f"该节点没有输入口 {port_key}")
        group = hit.get("group")
        if not group:
            raise HTTPException(400, f"{port_key} 是固定输入口，不能删除")

        seq = [s["key"] for s in specs if s.get("group") == group]
        idx = seq.index(port_key)
        floor = next(
            (int(s.get("min", 0)) for s in m.get("inputs", []) if s.get("key") == group), 0
        )
        if len(seq) - 1 < floor:
            raise HTTPException(400, f"至少要保留 {floor} 个输入口")

        # 1) 摘掉挂在这个口上的连线
        g["edges"] = [e for e in g["edges"] if not (e["to"] == pid and e["port"] == port_key)]
        # 2) 其后的端口整体前移一格
        shift = {f"{group}{j + 1}": f"{group}{j}" for j in range(idx + 1, len(seq))}
        if shift:
            for e in g["edges"]:
                if e["to"] == pid and e["port"] in shift:
                    e["port"] = shift[e["port"]]
        # 3) 数量减一
        p.setdefault("ports", {})
        p["ports"][group] = len(seq) - 1
        p["dirty"] = True
        persist()
    return state_payload()


@app.post("/api/processes/{pid}/polish")
def api_polish(pid: str):
    """用文本模型润色该节点的提示词。

    只返回结果，不写回画布 —— 由前端填进输入框（这样可以撤销）。
    见 DESIGN.md §3.7。
    """
    with LOCK:
        g = graph()
        p = engine_mod._process(g, pid)  # noqa: SLF001
        if p is None:
            raise HTTPException(404, "处理节点不存在")
        m = STATE["registry"].get(p.get("defId")) or {}
        specs = registry_mod.port_specs(m, p)
        inputs_map = engine_mod.connected_inputs(g, specs, pid)
        w = ws()
        image_paths: list[str] = []
        for port in specs:
            rid = inputs_map.get(port["key"])
            res = engine_mod._resource(g, rid) if rid else None  # noqa: SLF001
            path = w.image_path(res["name"]) if res else None
            if path is not None:
                image_paths.append(str(path))
        params = p.get("params") or {}
        prompt = (params.get("prompt") or "").strip()
        model_id = params.get("model") or ""

    if not prompt:
        raise HTTPException(400, "提示词是空的，没什么可润色的")

    key_env = (STATE["settings"].get("textModelKeyEnv") or "").strip()
    if not key_env:
        raise HTTPException(400, "还没配置文本模型的环境变量名（顶栏 ⚙ → 设置）")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise HTTPException(400, f"环境变量 {key_env} 未设置或为空，请先把它写进系统环境变量")

    notes = polish_mod.read_notes(APP_ROOT / "MODEL_NOTES.md", model_id)
    model_name = (STATE["settings"].get("textModelName") or polish_mod.DEFAULT_MODEL).strip()
    try:
        new_prompt = polish_mod.polish(api_key, model_name, prompt, image_paths, notes)
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e

    return {
        "prompt": new_prompt,
        "model": model_name,
        "images": len(image_paths),
        "notesMatched": bool(notes),
    }


@app.post("/api/processes/{pid}/detect-regions")
def api_detect_regions(pid: str):
    """「自动识别」：让模型挑脚本参数 → 跑几何切分脚本 → 返回区域数组。

    链路见 DESIGN.md §3.8：模型**只挑参数**，像素边界由确定性算法算出来。
    只返回结果、不写回画布 —— 由前端填进参数（这样可以撤销）。
    """
    with LOCK:
        g = graph()
        p = engine_mod._process(g, pid)  # noqa: SLF001
        if p is None:
            raise HTTPException(404, "处理节点不存在")
        m = STATE["registry"].get(p.get("defId")) or {}
        cfg = m.get("detect") or {}
        if not cfg:
            raise HTTPException(400, "这个节点没有配置「自动识别」")
        specs = registry_mod.port_specs(m, p)
        inputs_map = engine_mod.connected_inputs(g, specs, pid)
        w = ws()
        image_path: str | None = None
        for port in specs:
            rid = inputs_map.get(port["key"])
            res = engine_mod._resource(g, rid) if rid else None  # noqa: SLF001
            path = w.image_path(res["name"]) if res else None
            if path is not None:
                image_path = str(path)
                break
        script = (Path(m["_dir"]) / str(cfg.get("script", ""))).resolve()

    if image_path is None:
        raise HTTPException(400, "还没连接输入图，没什么可识别的")

    key_env = (STATE["settings"].get("textModelKeyEnv") or "").strip()
    if not key_env:
        raise HTTPException(400, "还没配置文本模型的环境变量名（顶栏 ⚙ → 设置）")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise HTTPException(400, f"环境变量 {key_env} 未设置或为空，请先把它写进系统环境变量")
    if not script.is_file():
        raise HTTPException(400, f"切分脚本不存在：{script}")

    model_name = (STATE["settings"].get("textModelName") or detect_mod.DEFAULT_MODEL).strip()
    try:
        args, reason, warns = detect_mod.detect(
            api_key, model_name, script, image_path,
            cfg.get("args") or {}, cfg.get("notes") or "",
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e

    # ---- 按模型挑的参数跑一遍几何切分 ----
    run_dir = w.tmp_dir / f"detect_{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    cli = [sys.executable, str(script), image_path, "--outdir", str(run_dir), "--debug"]
    for k, v in args.items():
        cli += [f"--{k.replace('_', '-')}", str(v)]
    try:
        try:
            proc = subprocess.run(  # noqa: S603
                cli, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300, cwd=str(run_dir),
                creationflags=(0x08000000 if os.name == "nt" else 0),
            )
        except subprocess.TimeoutExpired as e:
            raise HTTPException(502, "切分脚本执行超时（>300s）") from e
        log = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0:
            raise HTTPException(502, "切分脚本执行失败：\n" + log[-800:])
        js = next(run_dir.glob("*_elements.json"), None)
        if js is None:
            raise HTTPException(502, "切分脚本没有产出结果文件：\n" + log[-800:])
        data = json.loads(js.read_text(encoding="utf-8"))
    finally:
        drop_tree(run_dir)

    # ---- 区域 JSON → 节点参数（应用 padding，并夹到图像范围内）----
    try:
        with _PILImage.open(image_path) as im:  # type: ignore[union-attr]
            W, H = im.size
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "读不出输入图的尺寸")

    pad = int(args.get("pad") or 0)
    regions: list[dict] = []
    for el in data.get("elements") or []:
        x0, y0 = int(el["x0"]) - pad, int(el["y0"]) - pad
        x1, y1 = int(el["x1"]) + 1 + pad, int(el["y1"]) + 1 + pad
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(W, x1), min(H, y1)
        if x1 - x0 < 1 or y1 - y0 < 1:
            continue
        regions.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})

    # 只切出一个、而且几乎盖满整张图 —— 十有八九是掩膜退化了（阈值/方法不合适），
    # 结果虽然"成功"，但对用户毫无用处，得说清楚
    warns = list(warns)
    if len(regions) == 1 and W * H > 0:
        cover = (regions[0]["w"] * regions[0]["h"]) / float(W * H)
        if cover > 0.9:
            warns.append(
                f"只识别出 1 个区域，而且覆盖了整张图（{cover * 100:.0f}%）——"
                "多半是掩膜没分干净，可以再点一次「自动识别」，或手动编辑区域"
            )

    return {
        "regions": regions,
        "count": len(regions),
        "method": data.get("method"),
        "args": args,
        "reason": reason,
        "warnings": warns,
        "imageSize": [W, H],
        "log": log.splitlines()[-8:],
    }


@app.post("/api/tidy")
def api_tidy():
    """把所有输出数据节点重排到各自处理节点右侧 —— 收拾历史上被写死偏移量摆歪的画布。"""
    with LOCK:
        n = engine_mod.tidy_outputs(graph(), STATE["registry"], STATE["settings"])
        persist()
    print(f"[画布] 重排输出节点：移动了 {n} 个")
    return state_payload()


@app.patch("/api/positions")
def api_positions(items: list[dict]):
    """保存节点几何：位置（拖拽）与尺寸（拖右下角改大小）。两个都可选。"""
    with LOCK:
        g = graph()
        for it in items or []:
            node = engine_mod._process(g, it["id"]) or engine_mod._resource(g, it["id"])  # noqa: SLF001
            if node is None:
                continue
            if "x" in it:
                node["x"] = float(it["x"])
            if "y" in it:
                node["y"] = float(it["y"])
            # 节点尺寸用 w / h —— 千万别用 width / height，
            # 那两个名字在 /api/state 里是「图片像素分辨率」（由 _decorate 现算）
            if "w" in it:
                node["w"] = max(1.0, float(it["w"]))
            if "h" in it:
                node["h"] = max(1.0, float(it["h"]))
        persist()
    return {"ok": True}


# --------------------------------------------------------------------------
# 连线
# --------------------------------------------------------------------------
def _downstream_processes(g: dict, pid: str) -> list[str]:
    prod_ids = {r["id"] for r in g["resources"] if r.get("producer") == pid}
    return [e["to"] for e in g["edges"] if e["from"] in prod_ids]


def _reaches(g: dict, start: str, target: str) -> bool:
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(_downstream_processes(g, cur))
    return False


@app.post("/api/edges")
def api_create_edge(payload: dict):
    src = payload.get("from")
    dst = payload.get("to")
    port = payload.get("port")
    with LOCK:
        g = graph()
        if engine_mod._resource(g, src) is None or engine_mod._process(g, dst) is None:  # noqa: SLF001
            raise HTTPException(404, "节点不存在")

        dst_proc = engine_mod._process(g, dst)  # noqa: SLF001
        manifest = STATE["registry"].get(dst_proc["defId"])
        valid = {s["key"] for s in registry_mod.port_specs(manifest or {}, dst_proc)}
        if port not in valid:
            raise HTTPException(400, f"该节点没有输入口 {port}")

        # 一个输入口只能接一个来源
        for e in list(g["edges"]):
            if e["to"] == dst and e["port"] == port:
                g["edges"].remove(e)

        # DAG 检查
        prod = engine_mod._resource(g, src).get("producer")  # noqa: SLF001
        if prod and _reaches(g, dst, prod):
            raise HTTPException(400, "这条连线会造成环")

        edge = {"id": new_id("e"), "from": src, "to": dst, "port": port}
        g["edges"].append(edge)
        engine_mod._process(g, dst)["dirty"] = True  # noqa: SLF001
        persist()
    return state_payload()


@app.delete("/api/edges/{edge_id}")
def api_delete_edge(edge_id: str):
    with LOCK:
        g = graph()
        g["edges"] = [e for e in g["edges"] if e["id"] != edge_id]
        persist()
    return state_payload()


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
@app.post("/api/processes/{pid}/execute")
def api_execute(pid: str):
    with LOCK:
        g = graph()
        p = engine_mod._process(g, pid)  # noqa: SLF001
        if p is None:
            raise HTTPException(404, "处理节点不存在")
        p["status"] = "running"
        p["error"] = None
        persist()
    st = RUNS.create(pid)

    def worker() -> None:
        ok, err = False, None
        try:
            with LOCK:
                ok, err = engine_mod.execute(
                    ws(), STATE["graph"], STATE["registry"], pid, st, STATE["settings"]
                )
                persist()
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
        finally:
            with LOCK:
                p2 = engine_mod._process(STATE["graph"], pid)  # noqa: SLF001
                if p2 is not None:
                    if ok:
                        p2["status"] = "done"
                        p2["error"] = None
                    else:
                        p2["status"] = "error"
                        p2["error"] = err
                persist()
            st.status = "done" if ok else "error"
            st.error = err
            st.finished = True
            schedule_purge()  # 执行过程中被淘汰的旧输出文件

    threading.Thread(target=worker, daemon=True).start()
    return {"runId": st.id}


@app.get("/api/runs/{rid}/stream")
def api_run_stream(rid: str):
    st = RUNS.get(rid)
    if st is None:
        raise HTTPException(404, "run 不存在")

    def gen():
        idx = 0
        while True:
            lines, finished, status, error = st.snapshot(idx)
            for line in lines:
                yield "data: " + json.dumps({"type": "log", "line": line}, ensure_ascii=False) + "\n\n"
            idx += len(lines)
            if finished:
                yield "data: " + json.dumps(
                    {"type": "end", "status": status, "error": error, "state": state_payload()},
                    ensure_ascii=False,
                ) + "\n\n"
                break
            time.sleep(0.12)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# 静态前端
# --------------------------------------------------------------------------
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:
    @app.get("/")
    def _no_frontend():
        return JSONResponse(
            {"error": "前端尚未构建", "hint": "在 web/ 目录执行 npm run build"},
            status_code=503,
        )


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="ImageProcessFlow 本地服务")
    ap.add_argument("--workspace", "-w", default=None, help="工作目录（也可稍后在界面里选择）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()

    reload_registry()
    print(f"[启动] 已注册脚本: {', '.join(STATE['registry']) or '（无）'}")

    # 命令行给了就用给的（目录不存在就建出来 —— start.bat -w <新目录> 一直是这个语义）；
    # 没给才自动打开「上次用过的」，那个可能已经被删掉了，所以要判存在
    if args.workspace:
        w = _open_workspace(args.workspace)
        print(f"[启动] 工作目录: {w.root}")
    else:
        last = STATE["settings"].get("lastWorkspace") or ""
        if last and Path(last).is_dir():
            w = _open_workspace(last)
            print(f"[启动] 工作目录: {w.root}（上次用过的）")
        elif last:
            print(f"[启动] 上次的工作目录已不存在，忽略: {last}")

    # 清理上次残留的临时目录
    if STATE["workspace"] is not None:
        for d in STATE["workspace"].tmp_dir.glob("*"):
            if d.is_dir():
                drop_tree(d)
        schedule_purge()  # 上次没来得及真删的 .trash/

    import uvicorn

    print(f"[启动] http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
