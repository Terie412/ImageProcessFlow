# -*- coding: utf-8 -*-
"""验证并行执行：互不为上下游的节点能同时跑，上下游的会被挡住。

纯本地、不起服务、不联网、不调模型。三组检查：

 ① **闭包判据**（`engine.closure`）：自己算、上下游算、**兄弟不算**
    （兄弟共用同一个输入图，闭包在那张图上相交，但它们本来就该能并行）
 ② **真并行**：两个无关节点同时执行 —— 墙钟时间接近单个的耗时，
    并且子进程跑着的时候全局锁是**空的**（这是整件事的关键）
 ③ **守卫接线**（直接调路由函数，不起 HTTP）：正在执行时上下游被 409 挡住、
    兄弟放行、画布本身也挡（改名 / 删除），但记视野那个 PATCH 放行

    runtime\\venv\\Scripts\\python.exe runtime\\verify_parallel.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import engine  # noqa: E402
import registry as reg  # noqa: E402
import workspace  # noqa: E402
from fastapi import HTTPException  # noqa: E402

fails: list[str] = []

# 假脚本：睡一会儿、往输出目录扔一张（内容随便的）png。
# 引擎只按后缀收文件，不解码，所以不需要真图片。
FAKE_SCRIPT = """
import json, sys, time, pathlib
p = json.load(sys.stdin)
time.sleep(float(p["params"].get("secs", 1)))
pathlib.Path(p["output_dir"], "out.png").write_bytes(b"\\x89PNG\\r\\n\\x1a\\nfake")
print("跑完了")
"""


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def expect_409(fn, label: str) -> None:
    try:
        fn()
    except HTTPException as e:
        check(e.status_code == 409, f"{label} → 拒绝（{e.detail}）")
        return
    check(False, f"{label} → 本该拒绝，却放行了")


def expect_ok(fn, label: str) -> None:
    try:
        fn()
    except HTTPException as e:
        check(False, f"{label} → 本该放行，却报了 {e.status_code}（{e.detail}）")
        return
    check(True, f"{label} → 放行")


# --------------------------------------------------------------------------
# 拓扑（两组检查共用同一种形状）
#
#   R0 ──→ pA ──→ rA ──→ pC
#    └───→ pB ──→ rB
#
# pA / pB 是兄弟（共用 R0）；pC 是 pA 的下游。
# --------------------------------------------------------------------------
def build_graph(cid: str) -> dict:
    def res(rid, name, producer=None):
        r = {"id": rid, "name": name, "kind": "output" if producer else "input", "x": 0, "y": 0}
        if producer:
            r["producer"] = producer
            r["index"] = 0
        return r

    def proc(pid, x=0):
        return {
            "id": pid, "defId": "fake", "x": x, "y": 0, "params": {"secs": 1},
            "ports": {}, "status": "idle", "dirty": False, "error": None, "lastRun": None,
        }

    return {
        "id": cid,
        "resources": [res("r0", "图片1"), res("rA", "图片2", "pA"), res("rB", "图片3", "pB")],
        "processes": [proc("pA"), proc("pB", 800), proc("pC", 1600)],
        "edges": [
            {"id": "e1", "from": "r0", "to": "pA", "port": "image"},
            {"id": "e2", "from": "r0", "to": "pB", "port": "image"},
            {"id": "e3", "from": "rA", "to": "pC", "port": "image"},
        ],
    }


def make_workspace(tmp: Path) -> tuple[workspace.Workspace, str, dict]:
    """临时工作目录 + 一个假脚本节点。返回 (workspace, canvas_id, graph)。"""
    scripts = tmp / "scripts" / "fake"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "manifest.json").write_text(
        json.dumps(
            {
                "id": "fake",
                "name": "测试节点",
                "entry": "main.py",
                "inputs": [{"key": "image", "required": True}],
                "params": [{"key": "secs", "type": "text", "default": 1}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (scripts / "main.py").write_text(FAKE_SCRIPT, encoding="utf-8")

    w = workspace.Workspace(tmp / "ws")
    w.ensure()
    doc = w.load_doc()
    cid = doc["canvases"][0]["id"]
    v = w.view(cid)
    v.image_dir(create=True)
    # 输入图只要「文件在」—— 引擎只按后缀认，不解码
    (v.image_dir() / "图片1.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return w, cid, build_graph(cid)


# --------------------------------------------------------------------------
# ① 闭包判据
# --------------------------------------------------------------------------
def check_closure(g: dict) -> None:
    print("\n① 闭包判据（谁是上下游）")
    c_pa = engine.closure(g, ["pA"])
    check("pA" in c_pa, "自己算在内（重入要能被挡住）")
    check("r0" in c_pa, "上游输入图算在内（不能边跑边换图）")
    check("rA" in c_pa and "pC" in c_pa, "下游（含间接的 pC）算在内")
    check("pB" not in c_pa and "rB" not in c_pa, "**兄弟 pB 不在里面** —— 它俩能并行")
    check("r0" in engine.closure(g, ["pB"]), "兄弟共用输入图 → 闭包在那张图上相交（允许）")
    check("rB" in engine.closure(g, ["pA", "pB"]), "两个同时跑 → 两边下游都冻结")
    check("pB" not in engine.closure(g, ["pC"]), "反过来：pC 的闭包也不含兄弟 pB")


# --------------------------------------------------------------------------
# ② 真并行
# --------------------------------------------------------------------------
def check_parallelism(tmp: Path) -> None:
    print("\n② 两个无关节点同时执行（各睡 1 秒）")
    w, cid, g = make_workspace(tmp)
    registry = reg.load_manifests(tmp / "scripts")
    lock = threading.RLock()
    results: dict[str, tuple] = {}
    lock_free: list[bool] = []

    def go(pid: str) -> None:
        st = engine.RunState(pid, pid, cid)
        results[pid] = engine.execute(w.view(cid), g, registry, pid, st, None, lock=lock)

    t0 = time.perf_counter()
    ts = [threading.Thread(target=go, args=(p,)) for p in ("pA", "pB")]
    for t in ts:
        t.start()
    time.sleep(0.3)
    # 子进程正跑着的时候，全局锁必须是空的 —— 空了才说明「锁没包住子进程」
    got = lock.acquire(timeout=0.3)
    lock_free.append(got)
    if got:
        lock.release()
    for t in ts:
        t.join(timeout=30)
    elapsed = time.perf_counter() - t0

    check(all(ok for ok, _ in results.values()), f"两个都成功：{results}")
    check(lock_free[0], "执行期间全局锁是空的（锁只包准备与收尾）")
    check(elapsed < 1.8, f"墙钟 {elapsed:.2f}s（串行要 2s 以上）")
    for pid in ("pA", "pB"):
        outs = [r for r in g["resources"] if r.get("producer") == pid]
        exists = all(w.view(cid).image_path(r["name"]) for r in outs)
        check(len(outs) == 1 and exists, f"{pid} 产出落到本画布：{outs and outs[0]['name']}")


# --------------------------------------------------------------------------
# ③ 守卫接线（直接调路由函数）
# --------------------------------------------------------------------------
def check_guards(tmp: Path) -> None:
    print("\n③ 正在执行时的写操作（直接调路由函数）")
    import main as srv  # noqa: PLC0415  延迟导入：它 import 时会读 settings.json

    w, cid, g = make_workspace(tmp)
    srv.STATE["workspace"] = w
    srv.STATE["doc"] = None
    srv.STATE["graph"] = None
    srv.STATE["registry"] = reg.load_manifests(tmp / "scripts")
    srv.STATE["settings"] = dict(srv.DEFAULT_SETTINGS)
    srv.RUNS.runs.clear()
    d = srv.doc()
    d["canvases"][0].update({k: g[k] for k in ("resources", "processes", "edges")})
    w.create_canvas(d, "第二张")     # 删画布要留一张，不然先撞上「至少要保留一个」
    srv.persist()

    st = srv.RUNS.create(cid, "pA")          # 假装 pA 正在执行
    try:
        expect_409(lambda: srv.api_delete_node("pA"), "删掉正在执行的自己")
        expect_409(lambda: srv.api_delete_node("rA"), "删掉它的输出图")
        expect_409(lambda: srv.api_delete_node("r0"), "删掉它的输入图（上游）")
        expect_409(lambda: srv.api_update_process("pC", {"params": {"secs": 2}}), "改下游参数")
        expect_409(lambda: srv.api_delete_node("pC"), "删下游节点")
        expect_409(
            lambda: srv.api_create_edge({"from": "rB", "to": "pC", "port": "image"}),
            "给下游连一条新输入",
        )
        expect_409(lambda: srv.api_update_canvas(cid, {"name": "改名"}), "画布改名")
        expect_409(lambda: srv.api_delete_canvas(cid), "删掉整个画布")
        expect_409(
            lambda: srv.api_set_workspace({"path": str(tmp / "elsewhere")}),
            "换工作目录",
        )

        # 放行的：兄弟节点、纯坐标、记视野
        expect_ok(lambda: srv.api_update_process("pB", {"params": {"secs": 3}}), "改兄弟节点参数")
        expect_ok(lambda: srv.api_update_process("pA", {"x": 10, "y": 20}), "拖正在跑的节点（纯坐标）")
        expect_ok(
            lambda: srv.api_update_canvas(cid, {"viewport": {"x": 0, "y": 0, "zoom": 1}}),
            "记视野的 PATCH（切画布时每次都会发）",
        )
        expect_ok(lambda: srv.api_delete_node("rB"), "删兄弟的输出图")
    finally:
        st.finished = True
        srv.RUNS.runs.clear()


def wait_idle(srv, timeout: float = 20.0) -> bool:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if not srv.RUNS.active():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------
# ④ 真走一遍执行接口（还是不起 HTTP，直接调路由函数）
# --------------------------------------------------------------------------
def check_execute_api(tmp: Path) -> None:
    print("\n④ 执行接口：兄弟并行 / 重入被拒 / 下游被拒 / 结果落在自己的画布")
    import main as srv  # noqa: PLC0415

    w, cid, g = make_workspace(tmp)
    srv.STATE["workspace"] = w
    srv.STATE["doc"] = None
    srv.STATE["graph"] = None
    srv.STATE["registry"] = reg.load_manifests(tmp / "scripts")
    srv.STATE["settings"] = dict(srv.DEFAULT_SETTINGS)
    srv.RUNS.runs.clear()
    d = srv.doc()
    d["canvases"][0].update({k: g[k] for k in ("resources", "processes", "edges")})
    for p in d["canvases"][0]["processes"]:
        p["params"] = {"secs": 0.6}
    other = w.create_canvas(d, "另一张")["id"]
    srv.persist()

    srv.api_execute("pA")
    srv.api_execute("pB")                      # 兄弟 → 应该被放行
    check(len(srv.RUNS.active()) == 2, "两个执行同时在跑（RUNS 里同时有 2 个）")
    expect_409(lambda: srv.api_execute("pA"), "同一个节点再点一次")
    expect_409(lambda: srv.api_execute("pC"), "上游还在跑时执行下游")
    srv.api_open_canvas(other)                 # 执行期间切画布（这个不冻结）
    check(wait_idle(srv), "两个执行都结束了")

    ca = next(c for c in srv.doc()["canvases"] if c["id"] == cid)
    cb = next(c for c in srv.doc()["canvases"] if c["id"] == other)
    status = {p["id"]: p["status"] for p in ca["processes"]}
    check(status.get("pA") == "done" and status.get("pB") == "done", f"状态都写成 done：{status}")
    outs = [r for r in ca["resources"] if r.get("producer") in ("pA", "pB")]
    check(
        all(w.view(cid).image_path(r["name"]) for r in outs) and len(outs) >= 2,
        f"产出文件在**节点自己的画布**里：{[r['name'] for r in outs]}",
    )
    check(
        not any(r.get("producer") in ("pA", "pB") for r in cb["resources"]),
        "没有写进执行中切过去的那个画布",
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ipf-parallel-"))
    w, cid, g = make_workspace(tmp / "closure")
    check_closure(g)
    check_parallelism(tmp / "parallel")
    check_guards(tmp / "guards")
    check_execute_api(tmp / "api")

    print()
    if fails:
        print(f"失败 {len(fails)} 项：")
        for f in fails:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
