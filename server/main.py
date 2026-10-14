# -*- coding: utf-8 -*-
"""本地服务：FastAPI 路由 + SSE 日志推送。

对应 DESIGN.md §2.3：纯本地，只监听 127.0.0.1。
开发期用浏览器直连本服务；交付期可用 pywebview 套壳（同一份后端）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

import autosize
import detect as detect_mod
import engine as engine_mod
import nodespec
import polish as polish_mod
import registry as registry_mod
from workspace import IMAGE_EXTS, CanvasView, Workspace, drop_tree

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
    # 执行日志面板：打开时执行节点会在右下角显示实时日志（默认关，界面偏好）
    "showLog": False,
    # 提示词润色用的文本模型：这里存的是「环境变量名」，真正的 key 运行时从环境变量取
    "textModelKeyEnv": "DEEPSEEK_API_KEY",
    # 润色用的模型名（DeepSeek 里只有它支持图片输入）
    "textModelName": "deepseek-v4-flash-vision-exp",
    # 上次打开过的工作目录：下次启动自动打开，省得每次都要重选一遍
    "lastWorkspace": "",
    # 上次「下载」到的**目录**（应用级、跨节点、跨工作目录）。给那些还没下载过的
    # 数据节点用：它们点「下载」时对话框默认落到最近保存过的地方，而不是工作目录根
    # —— 图片往往就存在同一个目录下的不同文件里。
    "lastExportDir": "",
    # 网格吸附：开启后拖动节点会吸到格点上，落盘的坐标也一律对齐到格点
    "snapToGrid": True,
    # 吸附格大小（px）
    "gridSize": 5,
    # 缩放吸附：拖节点右下角改大小时，宽高吸到它的整数倍。
    # **0 或负数 = 关闭**（负数统一归到 0）；单位是 px，默认 10。
    "scaleSnap": 10,
    # 节点尺寸：**唯一一份**配置，见 server/nodespec.py。
    # 两个表形状一致，key 都是 resource / process:<defId> / process：
    #   nodeSize → **初始尺寸**（节点没被拖过右下角手柄时该多大）
    #   nodeMin  → **最小尺寸**（拖手柄时的下限）
    # 放在设置里而不是 manifest 里：这是用户的个人偏好，不是脚本的属性。
    # 注意 nodeSize 里**不写**各处理节点类型的条目 —— 那些由 manifest.size 声明
    # （脚本作者最清楚自己的节点该多大），写在这里等于多养一份会漂移的副本。
    "nodeSize": {
        "resource": {"w": 200, "h": 200},
        "process": {"w": 600, "h": 340},
    },
    # 各处理节点类型的默认下限 —— 界面上一行一行调出来之后固化的值
    # （2026-10-14）。绝大多数是 200×200；裁剪要放下它的两个数字框，所以是 220×200。
    # `process` 那条是**兜底**（脚本没声明、设置里也没有它那一行时用），所以保持大一点。
    "nodeMin": {
        "resource": {"w": 200, "h": 200},
        "process": {"w": 560, "h": 280},
        "process:alpha": {"w": 200, "h": 200},
        "process:alpha_erase": {"w": 200, "h": 200},
        "process:color_balance": {"w": 200, "h": 200},
        "process:crop": {"w": 220, "h": 200},
        "process:ai_image": {"w": 200, "h": 200},
        "process:recolor": {"w": 200, "h": 200},
        "process:region_erase": {"w": 200, "h": 200},
        "process:resize": {"w": 200, "h": 200},
        "process:square_pad": {"w": 200, "h": 200},
    },
}

# 尺寸的合理区间，防止填 0 或天文数字把节点搞没。
# 唯一的定义在 nodespec.SIZE_RANGE（随 /api/state 的 sizeRange 下发给前端）。
NODE_SIZE_RANGE = nodespec.SIZE_RANGE


def _valid_size_key(k: str) -> bool:
    if k in ("resource", "process"):
        return True
    if not k.startswith("process:"):
        return False
    tail = k[len("process:"):]
    return 0 < len(tail) <= 64 and "/" not in tail and ":" not in tail


def _coerce_size_map(value, current: dict, dflt: dict) -> dict | None:
    """校验 nodeSize / nodeMin：只认已知的 key，宽高取整并夹到合理区间。

    某个 `process:<defId>` 传 `null` 表示**删掉这一项**（回到通用 `process` 兜底）。
    `resource` / `process` 这两个基准项不允许删。
    """
    if not isinstance(value, dict):
        return None
    out = {k: dict(v) for k, v in (current or dflt).items()}
    for kind, spec in value.items():
        if not _valid_size_key(kind):
            continue
        if spec is None:
            if kind.startswith("process:"):
                out.pop(kind, None)
            continue
        if not isinstance(spec, dict):
            continue
        spec_out = dict(out.get(kind) or dflt["process"])
        for axis in ("w", "h"):
            if spec.get(axis) is None:
                continue
            try:
                n = int(round(float(spec[axis])))
            except (TypeError, ValueError):
                continue
            spec_out[axis] = max(NODE_SIZE_RANGE[0], min(NODE_SIZE_RANGE[1], n))
        out[kind] = spec_out
    return out


def _coerce_setting(key: str, value, current: dict):
    """校验/规范化一个设置项；不合法返回 None（调用方跳过这一项）。"""
    if key in ("nodeSize", "nodeMin"):
        return _coerce_size_map(value, current.get(key), DEFAULT_SETTINGS[key])
    if key == "gridSize":
        # 允许 "5" / 5 / 5.0 这类写法，取整后夹到合理区间 —— 0 会让吸附退化成空操作
        try:
            n = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        lo, hi = engine_mod.GRID_SIZE_RANGE
        return max(lo, min(hi, n))
    if key == "scaleSnap":
        # 允许 "10" / 10 / 10.4 这类写法，取整后夹到区间内。
        # **0 与负数都是「关闭」**，统一归到 0（下界就是 0），免得状态有两种写法
        try:
            n = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        lo, hi = engine_mod.SCALE_SNAP_RANGE
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
    "doc": None,         # 画布文档 {version, current, canvases:[…]}
    "graph": None,       # **当前画布那个 dict 本身**（doc.canvases 里的同一对象）
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


# 节点改名 / 重命名后，旧画布上的 defId 在这里跟着迁移 —— 不迁的话那些节点会
# 显示成「未注册的脚本」，等于把用户画布打坏。加一条就是一次迁移；旧的**不要删**，
# 用户手上可能还留着更早的工作区。
DEF_ID_RENAMES = {
    # 2026-10-14：Qwen 生图 / 编辑 → AI 生图 / 编辑（同一个节点并入 OpenAI 后端）
    "qwen_image": "ai_image",
}


def _migrate(g: dict) -> dict:
    """老画布兼容：迁移改过名的 defId、补齐新增参数、补齐动态输入口数量。

    已有连线指向的序号不会被凭空吃掉。
    """
    for p in g.get("processes", []):
        renamed = DEF_ID_RENAMES.get(p.get("defId"))
        if renamed:
            p["defId"] = renamed

        m = STATE["registry"].get(p.get("defId"))
        if not m:
            continue

        # 老节点缺的参数（manifest 后来新增的）补上默认值；enum 参数的值若已不在
        # 选项里（manifest 改过，比如尺寸换了一批预设），就地回落到默认值 ——
        # 否则界面上的下拉会是一片空白。参数按后端分组之后这两件事更要紧：
        # 切到另一个后端时，那一整套参数必须已经在 params 里躺着。
        params = p.get("params")
        if isinstance(params, dict):
            for spec in m.get("params", []):
                params.setdefault(spec["key"], spec.get("default"))
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


# --------------------------------------------------------------------------
# 画布文档：一个工作目录里有多个画布
#
# 磁盘上只有一个 graph.json（{version:2, current, canvases:[…]}），一次原子写。
# 内存里 STATE["doc"] 是整份文档，STATE["graph"] 指向**当前画布那个 dict 本身**
# （同一个对象，不是副本）—— 所以到处在用的 `g = graph()` + 就地改字段照旧生效，
# 切画布只是换个引用。图片池按画布分，引擎只跟 `view()`（当前画布的文件面）打交道。
# --------------------------------------------------------------------------
def doc() -> dict:
    """画布文档；第一次访问时从磁盘读（顺带完成 v1→v2 迁移）。"""
    if STATE["doc"] is None:
        d = ws().load_doc()
        for c in d["canvases"]:
            _migrate(c)
        STATE["doc"] = d
        _focus(d)
    return STATE["doc"]


def _focus(d: dict, canvas_id: str | None = None) -> dict:
    """把当前画布切到 canvas_id（不传就沿用文档里记的那个），并让 STATE["graph"] 指向它。"""
    if canvas_id and any(c["id"] == canvas_id for c in d["canvases"]):
        d["current"] = canvas_id
    cur = next((c for c in d["canvases"] if c["id"] == d.get("current")), d["canvases"][0])
    d["current"] = cur["id"]
    STATE["graph"] = cur
    return cur


def graph() -> dict:
    return STATE["graph"] if STATE["graph"] is not None else _focus(doc())


def canvas() -> dict:
    """当前画布（id / name / viewport 与图本身都在里面）。"""
    return _focus(doc())


def view() -> CanvasView:
    """当前画布的**文件面**（图片池 + 临时目录）。引擎要的就是它。"""
    return ws().view(canvas()["id"])


def canvas_list(d: dict) -> list[dict]:
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "nodeCount": len(c.get("resources", [])) + len(c.get("processes", [])),
            "viewport": c.get("viewport"),
        }
        for c in d["canvases"]
    ]


def _open_workspace(path: str | Path) -> Workspace:
    """打开工作目录，并把它记进设置 —— 下次启动直接用这个路径。

    工作目录只活在内存里，重启就没了；不记下来的话每次都得在界面上重选一遍。
    """
    w = Workspace(path)
    w.ensure()
    STATE["workspace"] = w
    STATE["doc"] = None      # 丢掉上一个工作目录的文档
    STATE["graph"] = None
    STATE["settings"]["lastWorkspace"] = str(w.root)
    save_settings(STATE["settings"])
    doc()                    # 读盘 + 迁移 + 定位到上次用的那个画布
    # 开着网格吸附的话，打开时顺手把没对齐的坐标收齐（老项目/别人传过来的画布）
    if STATE["settings"].get("snapToGrid"):
        persist()
    return w


def persist() -> None:
    """落盘画布文档。**所有**坐标写入都收口在这里，所以网格吸附也在这里统一执行。

    排在 save 之前，是为了让「存下去的就是画布上看到的」：开着吸附时，
    内存里的图与磁盘上的 json 都是对齐过的坐标。

    这里是**就近**吸附（最近的格点）—— 手动拖、导入图片这类「用户指哪打哪」
    的位置用就近；而程序算出来的布局（输出槽位、整图排版）在算的时候就用
    `engine.snap_up` 向上吸附，保证间距下限不被破坏（见 engine.snap_graph）。

    文档里的**所有画布一起收** —— 它们都在内存里，吸附规则又是全局的，
    没道理只收当前这一个。
    """
    d = doc()
    moved = sum(engine_mod.snap_graph(c, STATE["settings"]) for c in d["canvases"])
    if moved:
        print(f"[画布] 网格吸附：{moved} 个节点已就近对齐到格点")
    ws().save_doc(d)


def reload_registry() -> None:
    STATE["registry"] = registry_mod.load_manifests(SCRIPTS_DIR)


def _auto_size_hint(out: dict, proc: dict, manifest: dict) -> dict | None:
    """给「尺寸 = 自动」的处理节点算一条界面提示：它到底会选哪个预设。

    这是「自动」能被用户看见的**唯一来源** —— 否则要等生成完才发现模型挑了别的分辨率。

    依据必须是**最后一张连了线的参考图**：与执行时（`engine.execute`）、
    以及 DashScope 的官方口径（「多图输入时取最后一张」）三处一致。
    任何一处改口径，界面显示的和真正发出去的就会是两个值。

    ⚠️ 这里**不管用户当前选了什么** —— 用 `autosize.pick()` 而不是 `resolve()`。
    显不显示是前端的事：用户在下拉里切回「自动」是纯本地操作（参数走防抖 PATCH，
    不回灌 state），要是服务端按「用户已显式选择」就不下发，切回来那一下提示会是空的。

    `out` 是 `_decorate` 里那份**已经补过宽高**的图，所以这里直接读
    `resources[*].width/height` 即可，不用再开一次图片文件。
    """
    backend = registry_mod.backend_of(manifest, (proc.get("params") or {}).get("model"))
    key = autosize.auto_key(manifest, backend)
    if key is None:
        return None  # 这个脚本没有「自动」语义

    edges = out.get("edges") or []
    resources = out.get("resources") or []
    ref_dims = None
    ref = None
    ref_port = None
    # 倒序 = 从最后一张参考图往前找，第一个找到的就是「最后一张连了线的」
    for port in reversed(proc.get("portSpecs") or []):
        edge = next(
            (e for e in edges if e.get("to") == proc["id"] and e.get("port") == port["key"]),
            None,
        )
        if edge is None:
            continue
        r = next((x for x in resources if x["id"] == edge.get("from")), None)
        if r is None or not r.get("width") or not r.get("height"):
            continue
        ref_dims = (r["width"], r["height"])
        ref, ref_port = r, port
        break

    _value, info = autosize.pick(manifest, ref_dims, backend)
    if info is None:
        return None
    if ref is not None and ref_port is not None:
        info["from"] = ref_port.get("label") or ref_port["key"]
        info["image"] = ref.get("name")
    return info


def _decorate(g: dict, canvas_id: str | None = None) -> dict:
    """给节点补上「算出来的」字段（图片文件信息 + **最终尺寸** + 「自动」尺寸的落点）。

    图片池是按画布分的，同一个短名（比如 A）在不同画布下是**不同的文件** ——
    所以这里必须带上画布 id，否则会把别的画布的 A 的尺寸显示过来。

    **尺寸的口径只在 `nodespec.size_of()` 里有一份**：这里把每个节点的最终尺寸
    落到 `w` / `h` 上（`max(最小尺寸, 存的 ?? 初始尺寸)`），前端 `buildNodes` 直接
    用它渲染，不再自己推算 —— 原先前端 `App.jsx` 和服务端各算一遍，实测两边对
    「数据节点没存过高时算多高」给出的答案是 220 与 280，输出槽位就是这么被摆歪的。
    """
    w = STATE["workspace"]
    settings = STATE["settings"]
    out = json.loads(json.dumps(g))
    for r in out["resources"]:
        r["hasImage"] = False
        r["mtime"] = 0
        r["width"] = None
        r["height"] = None
        r["format"] = None
        r["alpha"] = None
        if w is not None and canvas_id:
            p = w.image_path(canvas_id, r.get("name", ""))
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
                            # 有没有**实际的**透明通道 —— 大图查看器的像素读数据此
                            # 决定显示 RGBA 还是 RGB（判据与 recolor/resize 一致：
                            # 看 mode，不看文件格式 —— RGB 模式的 PNG 也算「没有」）
                            r["alpha"] = im.mode in ("RGBA", "LA") or (
                                im.mode == "P" and "transparency" in im.info
                            )
                    except Exception:  # noqa: BLE001
                        pass
        r["w"], r["h"] = nodespec.size_of(r, kind="resource", settings=settings)

    for p in out["processes"]:
        m = STATE["registry"].get(p.get("defId"))
        p["w"], p["h"] = nodespec.size_of(p, kind="process", manifest=m, settings=settings)
        # 「尺寸 = 自动」会选哪个预设 —— 生成前就显示出来（见 _auto_size_hint）
        p["autoSize"] = None
        if m is None:
            p["portSpecs"] = []
            p["portGroups"] = []
            continue
        p["portSpecs"] = registry_mod.port_specs(m, p)
        p["portGroups"] = registry_mod.port_groups(m, p)
        p["autoSize"] = _auto_size_hint(out, p, m)
    return out


def state_payload() -> dict:
    w = STATE["workspace"]
    d = doc()
    return {
        "workspace": str(w.root) if w is not None else None,
        "workspaceDir": str(w.root) if w is not None else None,
        # 资源目录（工作目录之外的只读素材来源，跟随工作目录存在 graph.json 里）。
        # 空串 = 还没指定，前端据此显示「选择资源目录」。
        "resourceDir": d.get("resourceDir") or "",
        "canvasId": d["current"],
        "canvases": canvas_list(d),
        "graph": _decorate(graph(), d["current"]),
        # 这个画布还有没有「格式化所有节点」可撤销（右键菜单据此置灰那一项）
        "canUndoFormat": d["current"] in FMT_UNDO,
        "manifests": {k: registry_mod.public_manifest(v) for k, v in STATE["registry"].items()},
        # 节点尺寸的唯一读取入口：每个已知类型的 {w,h,minW,minH}（见 nodespec）
        "sizeSpec": nodespec.resolve_types(STATE["registry"], STATE["settings"]),
        # 宽高的允许区间，前端表单的 min/max 直接用它，不再自己写
        "sizeRange": list(nodespec.SIZE_RANGE),
        # 「缩放吸附」步长的允许区间（同一个理由：只留服务端一份）
        "scaleSnapRange": list(engine_mod.SCALE_SNAP_RANGE),
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
    # 脚本换了以后，画布上可能残留已经失效的参数值，顺手修正 —— 所有画布都要修
    with LOCK:
        for c in doc()["canvases"]:
            _migrate(c)
        persist()
    return state_payload()


# --------------------------------------------------------------------------
# 画布
# --------------------------------------------------------------------------
@app.post("/api/canvases")
def api_create_canvas(payload: dict):
    """新建一个画布并切过去。"""
    with LOCK:
        d = doc()
        c = ws().create_canvas(d, (payload or {}).get("name"))
        _focus(d, c["id"])
        persist()
    return state_payload()


@app.post("/api/canvases/{cid}/open")
def api_open_canvas(cid: str):
    """切换到指定画布。"""
    with LOCK:
        d = doc()
        if not any(c["id"] == cid for c in d["canvases"]):
            raise HTTPException(404, "画布不存在")
        _focus(d, cid)
        persist()
    return state_payload()


@app.patch("/api/canvases/{cid}")
def api_update_canvas(cid: str, payload: dict):
    """改画布名 / 记住它的视野（前端切走时把当前的缩放平移发过来）。"""
    with LOCK:
        d = doc()
        c = next((x for x in d["canvases"] if x["id"] == cid), None)
        if c is None:
            raise HTTPException(404, "画布不存在")
        name = (payload or {}).get("name")
        if isinstance(name, str) and name.strip():
            c["name"] = name.strip()[:60]
        vp = (payload or {}).get("viewport")
        if isinstance(vp, dict):
            try:
                c["viewport"] = {k: float(vp[k]) for k in ("x", "y", "zoom")}
            except (KeyError, TypeError, ValueError):
                pass
        persist()
    return state_payload()


@app.delete("/api/canvases/{cid}")
def api_delete_canvas(cid: str):
    """删掉一个画布 —— 连它的图片池一起永久删除。最后一个画布不给删。"""
    with LOCK:
        d = doc()
        if len(d["canvases"]) <= 1:
            raise HTTPException(400, "至少要保留一个画布")
        if not ws().delete_canvas(d, cid):
            raise HTTPException(404, "画布不存在")
        _focus(d)
        persist()
    return state_payload()


# --------------------------------------------------------------------------
# 剪贴板：跨画布复制 / 粘贴节点
#
# 剪贴板放在服务端内存里，而不是让浏览器揣着图片数据来回传 —— 粘贴时必须做一件
# 浏览器做不了的事：把图片文件从**源画布**的池子复制一份进**目标画布**，并在目标
# 画布重新分配短名（图片池是按画布分的，同名在不同画布是不同文件）。
# --------------------------------------------------------------------------
CLIP: dict = {"src": None, "resources": [], "processes": [], "edges": [], "anchor": (0.0, 0.0)}


def _clip_empty() -> bool:
    return not CLIP["resources"] and not CLIP["processes"]


@app.post("/api/clipboard/copy")
def api_clip_copy(payload: dict):
    """把当前画布上选中的节点（+ 它们之间的连线）抄进剪贴板。"""
    ids = [i for i in ((payload or {}).get("ids") or []) if isinstance(i, str)]
    if not ids:
        raise HTTPException(400, "没有选中任何节点")
    with LOCK:
        g = graph()
        idset = set(ids)
        resources = [json.loads(json.dumps(r)) for r in g["resources"] if r["id"] in idset]
        processes = [json.loads(json.dumps(p)) for p in g["processes"] if p["id"] in idset]
        if not resources and not processes:
            raise HTTPException(400, "选中的节点不在当前画布上")
        edges = [
            json.loads(json.dumps(e))
            for e in g["edges"]
            if e["from"] in idset and e["to"] in idset
        ]
        nodes = resources + processes
        CLIP.update({
            "src": canvas()["id"],
            "srcName": canvas()["name"],
            "resources": resources,
            "processes": processes,
            "edges": edges,
            "anchor": (
                min(float(n.get("x", 0)) for n in nodes),
                min(float(n.get("y", 0)) for n in nodes),
            ),
        })
    return {"ok": True, "nodes": len(nodes), "edges": len(edges)}


@app.post("/api/clipboard/paste")
def api_clip_paste(payload: dict):
    """把剪贴板里的节点重建到**当前画布**。

    - 数据节点：图片文件从源画布**复制**一份过来，短名在目标画布重新分配
    - 连线：只保留两端都被复制了的那些，端口不变
    - 位置：整体平移到粘贴点（不传 x/y 就相对原位挪 40px），最终由吸附/落盘收口
    - 「谁产出了谁」：生产者也一起粘过来的输出图才认领它的槽位；否则退化成
      一张普通的输入图（不然会留下一个指向不存在处理节点的 producer）
    """
    if _clip_empty():
        raise HTTPException(400, "剪贴板是空的")
    with LOCK:
        d = doc()
        dst = canvas()
        src_id = CLIP["src"]
        if not any(c["id"] == src_id for c in d["canvases"]):
            raise HTTPException(400, "剪贴板里的节点来自另一个工作目录，粘不过来")
        v_src = ws().view(src_id)
        v_dst = view()

        ax, ay = CLIP["anchor"]
        px, py = (payload or {}).get("x"), (payload or {}).get("y")
        dx = (float(px) - ax) if px is not None else 40.0
        dy = (float(py) - ay) if py is not None else 40.0

        idmap: dict[str, str] = {}
        # ---- 数据节点：连图片文件一起搬 ----
        created: list[tuple[dict, dict]] = []
        missing: list[str] = []
        for src_r in CLIP["resources"]:
            nid = new_id("r")
            idmap[src_r["id"]] = nid
            # 边分配边 append：alloc_name 是靠画布上的名字判重的，
            # 不先落进列表里，下一次分配会拿到同一个名字
            name = v_dst.alloc_name(dst)
            node = {
                "id": nid,
                "name": name,
                "kind": src_r.get("kind", "input"),
                "x": float(src_r.get("x", 0)) + dx,
                "y": float(src_r.get("y", 0)) + dy,
            }
            for k in ("w", "h"):
                if src_r.get(k) is not None:
                    node[k] = float(src_r[k])
            dst["resources"].append(node)
            created.append((src_r, node))
            if src_r.get("name") and not v_src.copy_image_from(src_r["name"], v_dst, name):
                missing.append(src_r["name"])

        for src_r, node in created:
            prod = src_r.get("producer")
            if prod and prod in idmap:
                node["producer"] = idmap[prod]
                node["index"] = src_r.get("index", 0)
            else:
                node.pop("producer", None)
                node["kind"] = "input"

        # ---- 处理节点：参数/端口照抄，运行状态清零 ----
        for src_p in CLIP["processes"]:
            nid = new_id("p")
            idmap[src_p["id"]] = nid
            node = {
                "id": nid,
                "defId": src_p["defId"],
                "x": float(src_p.get("x", 0)) + dx,
                "y": float(src_p.get("y", 0)) + dy,
                "params": json.loads(json.dumps(src_p.get("params") or {})),
                "ports": dict(src_p.get("ports") or {}),
                "status": "idle",
                "dirty": False,
                "error": None,
                "lastRun": None,
            }
            for k in ("w", "h"):
                if src_p.get(k) is not None:
                    node[k] = float(src_p[k])
            dst["processes"].append(node)

        # ---- 连线：两端都在剪贴板里才重建 ----
        for e in CLIP["edges"]:
            if e["from"] in idmap and e["to"] in idmap:
                dst["edges"].append({
                    "id": new_id("e"),
                    "from": idmap[e["from"]],
                    "to": idmap[e["to"]],
                    "port": e["port"],
                })

        persist()
        out = state_payload()
    schedule_purge()
    out["paste"] = {
        "nodes": len(created) + len(CLIP["processes"]),
        "missing": missing,     # 源文件不在的（理论上有 hasImage 就不会发生）
        "from": CLIP.get("srcName"),
    }
    return out


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


def _nudge_foreground() -> None:
    """让紧接着弹出的原生对话框能真正拿到键盘焦点。

    服务端是个**长期在后台跑**的进程，而用户点「下载」那一刻输入焦点在浏览器上。
    这种情形下 Windows 会按「防焦点窃取」的规则拦掉对话框成为前台窗口的请求 ——
    对话框照样显示出来，但里面的文件名输入框收不到键盘输入，必须先拿鼠标点一下。

    发一对 Alt 按下 / 抬起能让系统认为「用户刚操作过」，从而放行（社区里的老办法）。
    它只争取「能不能成为前台窗口」，不产生任何实际输入，也不改变别的窗口状态。

    非 Windows 直接跳过（`ctypes.windll` 不存在），所以整段包在 try 里。
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.keybd_event(0x12, 0, 0, 0)       # Alt 按下
        user32.keybd_event(0x12, 0, 0x0002, 0)  # Alt 抬起
    except Exception:  # noqa: BLE001
        pass


def _tk_pick_directory(initial: str | None) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    _nudge_foreground()
    root = tk.Tk()
    root.withdraw()
    # Windows 上**必须**先让 Tk 跑完一轮事件处理再弹对话框，否则对话框是在
    # 「主窗口还没画出来」的状态下创建的，里面的输入框拿不到键盘焦点 ——
    # 表现就是「必须先用鼠标点一下才能改名字」。见 CPython issue 34253。
    root.update_idletasks()
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

    _nudge_foreground()
    root = tk.Tk()
    root.withdraw()
    # 同 _tk_pick_directory：弹之前先 update_idletasks，否则文件名输入框拿不到
    # 键盘焦点（CPython issue 34253）—— 这正是「点了下载、还得先用鼠标点一下
    # 文件名才能改名」的成因。
    root.update_idletasks()
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


def _export_remember_path(node: dict | None) -> str:
    """取这个数据节点记住的上次下载路径；没记过返回空串。

    记忆**只有一个字段** `lastExportPath`（完整路径，含文件名）—— 早期版本只记目录
    (`lastExportDir`)，那会儿前端拿不到完整路径，也就没法做「覆盖」。两套字段并存
    会导致「旧代码写了目录、新前端只认路径」这种看不见的空档，所以**节点上的**目录
    字段已删除。

    > 别和 `settings.lastExportDir` 搞混：那个是**应用级**的「上次下载到哪个目录」，
    > 一份、跨节点，专门给「还没有位置记忆」的节点兜底（见 `_export_fallback_dir`）。
    """
    return ((node or {}).get("lastExportPath") or "").strip()


def _export_fallback_dir() -> Path:
    """「没记过位置」的数据节点点「下载」时，对话框默认落在哪。

    优先用**全局记录**的上次下载目录（`settings.lastExportDir`）—— 图片往往存在
    同一个目录下的不同文件里，所以新节点直接落到最近保存过的地方就够了。
    没记录过、或记的目录已经不在（被删 / 外置盘拔了），才退回工作目录根。
    """
    glob = (STATE["settings"].get("lastExportDir") or "").strip()
    if glob and Path(glob).is_dir():
        return Path(glob)
    return ws().root


def _export_save_as(
    node: dict | None, remembered: str, src: Path, fallback_dir: Path
) -> str | None:
    """弹另存为，写回记忆。返回落点；用户取消返回 None。

    对话框的初始目录：先看这个节点自己记的父目录；没有就落到 `fallback_dir`
    （= `_export_fallback_dir()`：全局记录的上次下载目录，最后才是工作目录根）。
    初始文件名带上记忆里的文件名 —— 用户可能把下载出去的那份改过名，再点「下载」时
    默认回到那个名字。

    保存成功后**两处都更新**：节点自己的 `lastExportPath`（完整路径），以及全局的
    `settings.lastExportDir`（目录，供以后那些还没有位置记忆的节点用）。
    """
    mem_dir = Path(remembered).parent if remembered else None
    initial_dir = str(mem_dir) if (mem_dir and mem_dir.is_dir()) else str(fallback_dir)
    initial_file = Path(remembered).name if remembered else src.name
    try:
        dest = _tk_save_as(initial_dir, initial_file or src.name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"无法打开系统对话框：{e}") from e
    if not dest:
        return None
    shutil.copy2(src, dest)
    # 全局记录：下次任何「还没记过位置」的节点下载，默认落到这个目录
    STATE["settings"]["lastExportDir"] = str(Path(dest).parent)
    save_settings(STATE["settings"])
    if node is not None:
        node["lastExportPath"] = str(dest)
        persist()
    return dest


@app.post("/api/workspace/export")
def api_export(payload: dict):
    """把当前画布里的一张图复制到用户指定的位置（UI 上叫「下载」）。

    每个数据节点**自己记住**上次下载到了**哪个完整路径**（存进 graph.json 里该节点的
    lastExportPath 字段），下次点「下载」时对话框直接定位到那个文件所在目录。

    **没记过**的节点（或记的目录已不存在）：落到**全局记录**的上次下载目录
    （`settings.lastExportDir`）—— 图片往往存在同一个目录下的不同文件里，所以新节点
    默认落在最近保存过的地方，最后才退回工作目录根。见 `_export_fallback_dir`。

    节点删了记忆也就没了，跨画布复制粘贴不会把这条记忆带过去（粘贴出来的是新节点）。
    """
    body = payload or {}
    name = body.get("name", "")
    resource_id = body.get("resource_id", "")
    src = view().image_path(name)
    if src is None:
        raise HTTPException(404, f"图片不存在: {name}")
    node = engine_mod._resource(graph(), resource_id) if resource_id else None  # noqa: SLF001
    dest = _export_save_as(node, _export_remember_path(node), src, _export_fallback_dir())
    if dest is None:
        return {"cancelled": True}
    return {"ok": True, "saved": dest, "modified": False}


@app.post("/api/workspace/overwrite")
def api_overwrite(payload: dict):
    """「覆盖」：把当前画布里的这张图**直接写回**上次下载的完整路径，不弹对话框。

    与「下载」的区别只有一个 —— 不挑位置、不打字，直接盖掉 `lastExportPath` 指向的
    那个文件（同名覆盖）。典型场景：输出节点重跑出新图后，之前手工下载过一份给
    别的软件用，现在懒得再「下载 → 另存为 → 一路点确定」。

    记忆文件**已经不在**时，退回「下载」的语义：弹另存为对话框重新选一次位置
    （返回 `modified: true` 让前端提示换成「已保存到」）。父目录不存在则报错，
    绝不悄悄替用户造目录。
    """
    body = payload or {}
    name = body.get("name", "")
    resource_id = body.get("resource_id", "")
    src = view().image_path(name)
    if src is None:
        raise HTTPException(404, f"图片不存在: {name}")
    node = engine_mod._resource(graph(), resource_id) if resource_id else None  # noqa: SLF001
    remembered = _export_remember_path(node)

    # 记忆失效（没记过 / 文件被删被移 / 外置盘拔了）→ 按「下载」走另存为
    if not remembered or not Path(remembered).is_file():
        dest = _export_save_as(node, remembered, src, _export_fallback_dir())
        if dest is None:
            return {"cancelled": True}
        return {"ok": True, "saved": dest, "modified": True}

    dest = Path(remembered)
    if not dest.parent.is_dir():
        raise HTTPException(400, f"目标目录已不存在：{dest.parent}")
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        raise HTTPException(500, f"覆盖失败：{e}") from e
    return {"ok": True, "saved": str(dest), "modified": False}


# --------------------------------------------------------------------------
# 图片
#
# 图片池是按画布分的，所以**取图必须带画布 id**：两个画布各有一个 A 是完全正常的，
# 少了这个 id 就会把别的画布的 A 送出去。前端一律用带 id 的那条路由。
# --------------------------------------------------------------------------
@app.get("/api/images/{cid}/{name}")
def api_image_in_canvas(cid: str, name: str):
    p = ws().image_path(cid, name)
    if p is None:
        raise HTTPException(404, "图片不存在")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


@app.get("/api/images/{name}")
def api_image(name: str):
    """不带画布 id 的兜底：在当前画布里找（老前端 / 手工点链接用）。"""
    p = view().image_path(name)
    if p is None:
        raise HTTPException(404, "图片不存在")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------
# 资源目录：工作目录之外的**只读**素材来源
#
# 与工作目录的沙箱规则（§2.4「绝不操作工作目录以外的文件」）的唯一例外：
# 这里**只读** —— 只允许列目录 / 读文件，绝不写、改、删资源目录里的任何东西。
# 把它拖进画布 = 把文件**复制一份**进当前画布的图片池（原文件不动），
# 与「从外部拖入图片」走同一条语义。
#
# 路径存进工作目录的 graph.json（`resourceDir`），所以它**跟随工作目录** ——
# 先有工作目录、再在其中指定资源目录，换项目就换一套素材来源。
# --------------------------------------------------------------------------
def _resource_root() -> Path:
    """资源目录根（已解析）。没指定 / 目录不存在时抛 400。"""
    raw = (doc().get("resourceDir") or "").strip()
    if not raw:
        raise HTTPException(400, "尚未指定资源目录")
    root = Path(raw)
    if not root.is_dir():
        raise HTTPException(400, f"资源目录不存在：{raw}")
    return root.resolve()


def _resource_path(rel: str) -> Path:
    """把资源目录内的**相对路径**解析成绝对路径，并挡住越界。

    解析（`resolve()`）之后再检查是否真在根目录之下 —— `..` 上跳、绝对路径、
    以及指向目录外的符号链接，都会在这里被拒。这是这块唯一的防线，别绕过它。
    """
    root = _resource_root()
    rel = (rel or "").strip().replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise HTTPException(400, "只接受资源目录内的相对路径")
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "路径越界：只允许访问资源目录内的文件")
    return target


def _rel_of(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _dir_has_child(p: str) -> bool:
    """这个目录里有没有东西 —— 前端据此决定要不要画展开箭头。"""
    try:
        with os.scandir(p) as it:
            return next(it, None) is not None
    except OSError:
        return False


def _list_dir(rel: str) -> dict:
    """列一个目录的**直接子项**（懒加载一层）。

    目录在前、文件在后，各按名字排序；图片文件带 `isImage`（只有它能拖进画布）。
    `list` / `mkdir` / `rename` / `delete` 都回这一份 —— 写完之后前端就地把这一层刷新掉，
    不用再单独拉一次列表。
    """
    root = _resource_root()
    target = _resource_path(rel)
    if not target.is_dir():
        raise HTTPException(400, "不是一个目录")
    try:
        items = list(os.scandir(target))
    except OSError as e:
        raise HTTPException(400, f"无法读取目录：{e}") from e

    entries: list[dict] = []
    for it in items:
        try:
            is_dir = it.is_dir()
        except OSError:
            continue
        entry = {
            "name": it.name,
            "rel": _rel_of(root, Path(it.path)),
            "type": "dir" if is_dir else "file",
        }
        if is_dir:
            entry["hasChildren"] = _dir_has_child(it.path)
        else:
            ext = Path(it.name).suffix.lower()
            entry["ext"] = ext
            entry["isImage"] = ext in IMAGE_EXTS
            try:
                st = it.stat()
                entry["size"] = st.st_size
                entry["mtime"] = int(st.st_mtime)
            except OSError:
                pass
        entries.append(entry)

    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
    return {"root": str(root), "path": (rel or "").strip("/"), "entries": entries}


@app.post("/api/resourcedir")
def api_set_resourcedir(payload: dict):
    """指定 / 更换资源目录（存进当前工作目录的 graph.json）。空串 = 清除。"""
    raw = ((payload or {}).get("path") or "").strip()
    with LOCK:
        d = doc()
        if not raw:
            d["resourceDir"] = ""
            persist()
            return state_payload()
        p = Path(raw)
        if not p.is_dir():
            raise HTTPException(400, f"目录不存在：{raw}")
        d["resourceDir"] = str(p.resolve())
        persist()
    return state_payload()


@app.post("/api/resourcedir/pick")
def api_pick_resourcedir():
    """弹原生目录对话框选资源目录（与选工作目录同一套 tkinter）。"""
    initial = (doc().get("resourceDir") or "").strip() or str(ws().root)
    try:
        chosen = _tk_pick_directory(initial)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"无法打开系统对话框（可手动填写路径）：{e}") from e
    if not chosen:
        return {"cancelled": True}
    with LOCK:
        doc()["resourceDir"] = str(Path(chosen).resolve())
        persist()
    return state_payload()


@app.get("/api/resourcedir/list")
def api_list_resourcedir(path: str = ""):
    """列一个子目录的**直接子项**（懒加载：前端展开某目录时才请求它这一层）。"""
    return _list_dir(path)


# --------------------------------------------------------------------------
# 资源目录：写操作（新建目录 / 重命名 / 删除）
#
# 这**打破**了资源目录原本「只读」的约定（见 DESIGN.md §3.15）：现在可以在里面
# 新建目录、重命名、删除。三条纪律没变：
#   · 只动资源目录**内**的东西 —— 一切路径仍走 `_resource_path()` 那道防线；
#   · **不创建文件**，只能新建目录（文件从哪来是用户自己的事）；
#   · 删除走**回收站**，不永久删除 —— 资源目录往往就是用户自己的「下载」「图片」，
#     删错了必须能找回来。
# --------------------------------------------------------------------------
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _parent_rel(rel: str) -> str:
    """取一个相对路径的父目录（同样用正斜杠、相对资源目录根）。"""
    r = (rel or "").strip("/")
    return r.rsplit("/", 1)[0] if "/" in r else ""


def _safe_child_name(name: str) -> str:
    """校验「新建 / 重命名」里的**单段名字**。不合规直接 400。"""
    n = (name or "").strip()
    if not n:
        raise HTTPException(400, "名字不能为空")
    if n in (".", ".."):
        raise HTTPException(400, "名字不合法")
    if any(c in n for c in "/\\") or any(ord(c) < 32 for c in n):
        raise HTTPException(400, "名字里不能有路径分隔符或控制字符")
    if any(c in n for c in '<>:"|?*'):
        raise HTTPException(400, '名字里不能有这些字符：< > : " | ? *')
    if n[-1] in (".", " "):
        raise HTTPException(400, "名字不能以点或空格结尾")
    if n.split(".")[0].upper() in _WIN_RESERVED:
        raise HTTPException(400, f"{n} 是 Windows 保留名，换一个")
    if len(n) > 200:
        raise HTTPException(400, "名字太长了（超过 200 个字符）")
    return n


def _win_send_to_trash(path: Path) -> bool:
    """Windows：把一项丢进**回收站**（SHFileOperationW + FOF_ALLOWUNDO，静默）。"""
    import ctypes
    from ctypes import wintypes

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040          # 有它才进回收站；没有就是永久删除
    FOF_NOERRORUI = 0x0400

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    # pFrom 是**双 null 结尾**的多字符串（这里只有一项）
    op = SHFILEOPSTRUCTW(
        None, FO_DELETE, str(path) + "\0\0", None,
        FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
        False, None, None,
    )
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted


def _delete_resource_item(p: Path) -> bool:
    """删掉资源目录里的一项，返回「是否进了回收站」。

    Windows 上回收站那条路走不通就**报错，绝不退化成永久删除** —— 这是用户自己的
    文件，不能因为一个 Shell 调用失败就悄悄销毁。
    """
    if os.name == "nt":
        if _win_send_to_trash(p):
            return True
        raise HTTPException(500, f"没能移入回收站，未删除任何东西：{p.name}")
    try:
        from send2trash import send2trash  # type: ignore

        send2trash(str(p))
        return True
    except Exception:  # noqa: BLE001  没装就退化成永久删除（返回 False 让界面说清楚）
        pass
    if p.is_dir():
        drop_tree(p)
    else:
        p.unlink()
    return False


@app.post("/api/resourcedir/mkdir")
def api_resourcedir_mkdir(payload: dict):
    """在 `parent` 指定的目录里新建一个子目录，回**这一层**的新列表。

    `parent` 是相对资源目录根的路径，空串 = 资源目录根（面板空白处右键就是它）。
    """
    body = payload or {}
    parent_rel = (body.get("parent") or "").strip("/")
    name = _safe_child_name(body.get("name", ""))
    parent = _resource_path(parent_rel)
    if not parent.is_dir():
        raise HTTPException(400, "目标不是一个目录")
    target = parent / name
    if target.exists():
        raise HTTPException(400, f"已经存在同名项：{name}")
    try:
        target.mkdir()
    except OSError as e:
        raise HTTPException(500, f"新建目录失败：{e}") from e
    return {"ok": True, **_list_dir(parent_rel)}


@app.post("/api/resourcedir/rename")
def api_resourcedir_rename(payload: dict):
    """把资源目录里的一项（文件或目录）**改名** —— 不移动，只在同一个父目录内。"""
    body = payload or {}
    rel = (body.get("path") or "").strip("/")
    if not rel:
        raise HTTPException(400, "不能重命名资源目录本身")
    name = _safe_child_name(body.get("name", ""))
    src = _resource_path(rel)
    if not src.exists():
        raise HTTPException(404, "目标不存在")
    if name == src.name:
        # 改成同名 = 什么都没做，直接回这一层（也躲开 Windows 上同名 rename 的边界行为）
        return {"ok": True, **_list_dir(_parent_rel(rel))}
    dst = src.parent / name
    if dst.exists():
        raise HTTPException(400, f"已经存在同名项：{name}")
    try:
        src.rename(dst)
    except OSError as e:
        raise HTTPException(500, f"重命名失败：{e}") from e
    return {"ok": True, **_list_dir(_parent_rel(rel))}


@app.post("/api/resourcedir/delete")
def api_resourcedir_delete(payload: dict):
    """删除资源目录里的一项（**目录会连同里面的内容一起**），默认进回收站。"""
    rel = ((payload or {}).get("path") or "").strip("/")
    if not rel:
        raise HTTPException(400, "不能删除资源目录本身")
    target = _resource_path(rel)
    if not target.exists():
        raise HTTPException(404, "目标不存在")
    to_trash = _delete_resource_item(target)
    return {"ok": True, "toTrash": to_trash, **_list_dir(_parent_rel(rel))}


@app.get("/api/resourcedir/raw")
def api_resourcedir_raw(path: str = ""):
    """只读地取资源目录里的一个文件（面板里的缩略图用）。"""
    p = _resource_path(path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


@app.get("/api/resourcedir/info")
def api_resourcedir_info(path: str = ""):
    """只读一个图片文件的尺寸 / 格式 / 有没有 alpha —— 面板里双击预览时用。

    口径与 `/api/state` 里数据节点的 `width`/`height`/`format`/`alpha` **完全一致**：
    只读文件头（Pillow 的 `open` 不解码整张），而且同样按**实际的像素模式**判断有没有
    透明通道，不看文件后缀（一张 RGB 模式的 PNG 照样算「没有」）。
    """
    p = _resource_path(path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    if p.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(400, "不是图片文件")
    if _PILImage is None:
        raise HTTPException(500, "服务端没有 Pillow，读不了图片信息")
    try:
        with _PILImage.open(p) as im:
            return {
                "width": im.size[0],
                "height": im.size[1],
                "format": (im.format or "").upper() or None,
                "alpha": im.mode in ("RGBA", "LA")
                or (im.mode == "P" and "transparency" in im.info),
            }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"读不了这张图：{e}") from e


@app.post("/api/resources/import-from-resource")
def api_import_from_resource(payload: dict):
    """把资源目录里的一个图片文件**复制一份**进当前画布的图片池，成为数据节点。

    与 `/api/resources/import` 的区别只有一个：源文件在本地磁盘上，服务端直接
    复制 —— 不经浏览器上传，大图不会在前后端之间来回搬。原文件**不动**（资源目录只读）。
    """
    body = payload or {}
    rel = body.get("path", "")
    src = _resource_path(rel)
    if not src.is_file():
        raise HTTPException(404, f"文件不存在：{rel}")
    ext = src.suffix.lower()
    if ext not in IMAGE_EXTS:
        raise HTTPException(400, f"不是图片文件：{src.name}")

    v = view()
    with LOCK:
        g = graph()
        r = {
            "id": new_id("r"),
            "name": v.alloc_name(g),
            "kind": "input",
            "x": float(body.get("x", 0)),
            "y": float(body.get("y", 0)),
        }
        g["resources"].append(r)
        dest = v.image_dir(create=True) / f"{r['name']}{ext}"
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            # 复制失败就把刚加的节点撤掉 —— 别在画布上留一个指向不存在文件的空壳
            g["resources"].remove(r)
            raise HTTPException(500, f"复制失败：{e}") from e
        persist()
    schedule_purge()
    return state_payload()


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
    """从外部拖入 / 选择图片 → 复制一份到**当前画布的图片池**，成为数据节点。

    若给了 resource_id，则表示「替换某个输入图的图片」（见 DESIGN.md §2.2.2 C8）。
    """
    v = view()
    ext = Path(file.filename or "img.png").suffix.lower()
    if ext not in IMAGE_EXTS:
        ext = ".png"

    with LOCK:
        g = graph()
        if resource_id:
            r = next((r for r in g["resources"] if r["id"] == resource_id), None)
            if r is None:
                raise HTTPException(404, "数据节点不存在")
            v.delete_image(r["name"])
        else:
            r = {
                "id": new_id("r"),
                "name": v.alloc_name(g),
                "kind": "input",
                "x": x,
                "y": y,
            }
            g["resources"].append(r)

        dest = v.image_dir(create=True) / f"{r['name']}{ext}"
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
        v = view()
        engine_mod.remove_process(g, node_id, v)
        engine_mod.remove_resource_node(g, node_id, v)
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
        v = view()
        image_paths: list[str] = []
        for port in specs:
            rid = inputs_map.get(port["key"])
            res = engine_mod._resource(g, rid) if rid else None  # noqa: SLF001
            path = v.image_path(res["name"]) if res else None
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
        v = view()
        image_path: str | None = None
        for port in specs:
            rid = inputs_map.get(port["key"])
            res = engine_mod._resource(g, rid) if rid else None  # noqa: SLF001
            path = v.image_path(res["name"]) if res else None
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
    run_dir = v.tmp_dir / f"detect_{uuid.uuid4().hex[:8]}"
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


# 「格式化所有节点」的撤销快照：**每个画布一份**，一直留到被撤销为止。
# 放内存里不落盘 —— 重启就没了，但重启之后画布还在，只是撤不回上一次格式化，
# 这个代价可以接受（坐标本身已经写进 graph.json 了）。
FMT_UNDO: dict[str, list[dict]] = {}


@app.post("/api/format")
def api_format():
    """整张画布重新排版（分层列布局，见 DESIGN.md §3.11）。

    动的是**所有**节点的坐标，所以顺手记一份快照供「撤销格式化」用。

    快照用 `setdefault` —— **只要没撤销过就一直留着，不会被后面的格式化冲掉**。
    这样「连点两次格式化 → 撤销」是回到**最初**的排布（而不是回到上一次格式化的
    结果，那等于什么都没撤销）。
    """
    with LOCK:
        g = graph()
        cid = canvas()["id"]
        before = engine_mod.snapshot_positions(g)
        n = engine_mod.format_all(g, STATE["registry"], STATE["settings"])
        FMT_UNDO.setdefault(cid, before)
        persist()
    print(f"[画布] 格式化所有节点：移动了 {n} 个")
    payload = state_payload()
    payload["moved"] = n
    return payload


@app.post("/api/format/undo")
def api_format_undo():
    """把画布坐标还原到格式化之前（**只能用一次**，用完就得重新格式化才有）。"""
    with LOCK:
        cid = canvas()["id"]
        snap = FMT_UNDO.pop(cid, None)
        if not snap:
            raise HTTPException(400, "没有可撤销的格式化（每个画布只留最近一次，重启后也清空）")
        n = engine_mod.restore_positions(graph(), snap)
        persist()
    print(f"[画布] 撤销格式化：还原了 {n} 个")
    payload = state_payload()
    payload["restored"] = n
    return payload


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


@app.post("/api/processes/{pid}/fill-inputs")
def api_fill_inputs(pid: str, payload: dict):
    """把一批数据节点按「离本节点左上角的距离」填进这个处理节点的输入口。

    规则（2026-10-14 与用户确认）：

    - **端口多删少补**：动态口的数量直接改成选中数量（夹在 manifest 的 min/max 之间）——
      选了 4 个而当前只展开 2 个就补到 4，当前展开 8 个就砍到 4（砍掉的口上的连线一并断开）。
      固定单口节点（裁剪 / 区域抹除）没有数量概念，只填第一个。
    - **覆盖**：本节点输入口上的旧连线先全部摘掉，再按新顺序重连。
    - **排除本节点自己产出的**输出型数据节点（连上就是自环）。
    - **顺序** = 到本节点左上角的欧氏距离，越近越靠前 —— 这决定谁是「参考图1」。
      在前端算不了：节点坐标的权威副本在服务端这里。
    """
    ids = [i for i in ((payload or {}).get("resources") or []) if isinstance(i, str)]
    with LOCK:
        g = graph()
        p = engine_mod._process(g, pid)  # noqa: SLF001
        if p is None:
            raise HTTPException(404, "处理节点不存在")
        m = STATE["registry"].get(p.get("defId")) or {}
        inputs = m.get("inputs") or []
        if not inputs:
            raise HTTPException(400, "这个节点没有输入口")

        # 候选：得在本画布上，且不是本节点自己产出的图；同一个图只算一次
        seen: set[str] = set()
        picked: list[dict] = []
        for rid in ids:
            if rid in seen:
                continue
            r = engine_mod._resource(g, rid)  # noqa: SLF001
            if r is None or r.get("producer") == pid:
                continue
            seen.add(rid)
            picked.append(r)
        if not picked:
            raise HTTPException(400, "选中的数据节点里没有可作输入的（本节点自己的产出会被排除）")

        # 顺序：到本节点左上角的距离，越近越靠前
        px, py = float(p.get("x") or 0), float(p.get("y") or 0)
        picked.sort(
            key=lambda r: (
                (float(r.get("x") or 0) - px) ** 2 + (float(r.get("y") or 0) - py) ** 2,
                float(r.get("y") or 0),
                float(r.get("x") or 0),
            )
        )

        # 端口数量：多删少补（当前所有节点都只有一个输入声明，取 inputs[0]）
        spec = inputs[0]
        if spec.get("dynamic"):
            group = spec["key"]
            lo = int(spec.get("min", 0))
            hi = int(spec.get("max", 99))
            count = max(lo, min(hi, len(picked)))
            p.setdefault("ports", {})
            p["ports"][group] = count
            keys = [f"{group}{i + 1}" for i in range(count)]
        else:
            keys = [spec["key"]]
            picked = picked[:1]

        # 覆盖：本节点输入口上的旧连线**全部**摘掉，随后按新顺序重连。
        # 不能只删「新旧端口有交集」的那部分 —— 缩口时失效端口上的线会留下来，
        # 变成既不在端口表里、又挂在节点上的孤儿边（实测踩到过）。
        g["edges"] = [e for e in g["edges"] if e["to"] != pid]

        # 按顺序重连；会成环的跳过（直接产出已在上面排除，这里兜住更深的下游）
        for key, r in zip(keys, picked):
            prod = r.get("producer")
            if prod and _reaches(g, pid, prod):
                continue
            g["edges"].append({"id": new_id("e"), "from": r["id"], "to": pid, "port": key})

        p["dirty"] = True
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
        # 先把画布钉住：执行期间图片要写进**这个画布**的池子，即便中途切了画布也不跑偏
        v = view()
        persist()
    st = RUNS.create(pid)

    def worker() -> None:
        ok, err = False, None
        try:
            with LOCK:
                ok, err = engine_mod.execute(
                    v, STATE["graph"], STATE["registry"], pid, st, STATE["settings"]
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
