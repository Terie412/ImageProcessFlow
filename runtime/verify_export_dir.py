# -*- coding: utf-8 -*-
"""验证「下载」的落点记忆：节点自己记 + 全局记（settings.lastExportDir）。

需求：每个数据节点自己记上次下载的**完整路径**；另外再有一份**全局**的上次下载
**目录**，专门给「还没下载过任何东西」的节点用 —— 它们点「下载」时默认落到最近
保存过的地方，而不是每次都从工作目录根翻起。

纯本地：把 `_tk_save_as`（原生对话框）换成假的，不起服务、不开窗口、不调模型。

    runtime\\venv\\Scripts\\python.exe runtime\\verify_export_dir.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import main as m  # noqa: E402
from workspace import Workspace, drop_tree  # noqa: E402

TMP = ROOT / "runtime" / "_exportdir_tmp"
WS = TMP / "ws"
OUT1 = TMP / "out1"      # 第一次保存到的目录
OUT2 = TMP / "out2"      # 另一个目录

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


# ---- 假的「另存为」：记下它收到的初始目录/文件名，返回我们指定的落点 ----
saves: list[tuple[str, str]] = []
save_dir = {"d": OUT1}


def fake_save(initial_dir, initial_file):
    saves.append((str(initial_dir), str(initial_file)))
    if save_dir["d"] is None:
        return None                      # 模拟用户取消
    return str(Path(save_dir["d"]) / (initial_file or "x.png"))


def setup() -> None:
    drop_tree(TMP)
    for d in (WS, OUT1, OUT2):
        d.mkdir(parents=True, exist_ok=True)
    (TMP / "A.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)

    m._tk_save_as = fake_save
    m.save_settings = lambda s: None     # 别动真实的 settings.json
    m.STATE["workspace"] = Workspace(WS)
    m.STATE["workspace"].ensure()
    m.STATE["doc"] = None
    m.STATE["graph"] = None
    m.STATE["registry"] = {}
    m.STATE["settings"] = json.loads(json.dumps(m.DEFAULT_SETTINGS))


def src() -> Path:
    return TMP / "A.png"


def run() -> None:
    src_p = src()
    d = m.doc()
    g = d["canvases"][0]
    m.STATE["graph"] = g

    print("=" * 66)
    print("[1] 没记过位置时，默认目录 = 全局记录，其次工作目录根")
    print("=" * 66)
    check("没有任何记录 → 工作目录根", m._export_fallback_dir() == WS.resolve(), str(m._export_fallback_dir()))
    m.STATE["settings"]["lastExportDir"] = str(OUT1)
    check("有全局记录 → 用它", m._export_fallback_dir() == OUT1.resolve())
    m.STATE["settings"]["lastExportDir"] = str(TMP / "gone")
    check("全局记的目录不存在 → 退回工作目录根", m._export_fallback_dir() == WS.resolve())
    m.STATE["settings"]["lastExportDir"] = "   "
    check("全局记录是空白 → 退回工作目录根", m._export_fallback_dir() == WS.resolve())

    print("=" * 66)
    print("[2] 第一次下载：落到工作目录根，并同时写两处记忆")
    print("=" * 66)
    m.STATE["settings"]["lastExportDir"] = ""
    n1 = {"id": "r1", "name": "A", "kind": "input"}
    g["resources"].append(n1)
    saves.clear()
    save_dir["d"] = OUT1
    dest = m._export_save_as(n1, "", src_p, m._export_fallback_dir())
    check("对话框初始目录 = 工作目录根", saves[-1][0] == str(WS.resolve()), str(saves[-1]))
    check("初始文件名 = 图片名", saves[-1][1] == "A.png", str(saves[-1]))
    check("文件真的写出去了", Path(dest).is_file(), str(dest))
    check("节点记住完整路径", n1.get("lastExportPath") == dest, str(n1.get("lastExportPath")))
    check("全局记住目录", m.STATE["settings"]["lastExportDir"] == str(OUT1), str(m.STATE["settings"]["lastExportDir"]))

    print("=" * 66)
    print("[3] 第二个「没下载过」的节点：默认落到全局目录（这就是需求本身）")
    print("=" * 66)
    check("全局目录已生效", m._export_fallback_dir() == OUT1.resolve(), str(m._export_fallback_dir()))
    n2 = {"id": "r2", "name": "B", "kind": "input"}
    g["resources"].append(n2)
    saves.clear()
    dest2 = m._export_save_as(n2, "", src_p, m._export_fallback_dir())
    check("对话框初始目录 = 上次保存的目录，而不是工作目录根", saves[-1][0] == str(OUT1.resolve()), str(saves[-1]))
    check("新节点也记住了自己的完整路径", n2.get("lastExportPath") == dest2, str(dest2))
    check("节点记忆写进了 graph.json", any(
        r.get("lastExportPath") == dest2 for r in json.loads((WS / "graph.json").read_text(encoding="utf-8"))["canvases"][0]["resources"]
    ))

    print("=" * 66)
    print("[4] 有自己记忆的节点：用自己的目录，并顺带刷新全局记录")
    print("=" * 66)
    n3 = {"id": "r3", "name": "C", "kind": "input", "lastExportPath": str(OUT2 / "C.png")}
    g["resources"].append(n3)
    saves.clear()
    save_dir["d"] = OUT2
    m._export_save_as(n3, m._export_remember_path(n3), src_p, m._export_fallback_dir())
    check("用自己的目录（不是全局的）", saves[-1][0] == str(OUT2.resolve()), str(saves[-1]))
    check("文件名沿用记忆里的", saves[-1][1] == "C.png", str(saves[-1]))
    check("全局记录跟着更新", m.STATE["settings"]["lastExportDir"] == str(OUT2), str(m.STATE["settings"]["lastExportDir"]))

    print("=" * 66)
    print("[5] 记忆里的目录已不存在 → 落到全局目录")
    print("=" * 66)
    gone = str(TMP / "old" / "C.png")
    saves.clear()
    save_dir["d"] = OUT1
    m._export_save_as(n3, gone, src_p, m._export_fallback_dir())
    check("退回全局目录", saves[-1][0] == str(OUT2.resolve()), str(saves[-1]))

    print("=" * 66)
    print("[6] 用户取消：不写任何记忆")
    print("=" * 66)
    before_glob = m.STATE["settings"]["lastExportDir"]
    n4 = {"id": "r4", "name": "D", "kind": "input"}
    g["resources"].append(n4)
    save_dir["d"] = None
    r = m._export_save_as(n4, "", src_p, m._export_fallback_dir())
    check("取消返回 None", r is None)
    check("节点没被写记忆", "lastExportPath" not in n4)
    check("全局记录没变", m.STATE["settings"]["lastExportDir"] == before_glob)

    print("=" * 66)
    print("[7] 设置项本身：默认空串、能被 load 回来")
    print("=" * 66)
    check("DEFAULT_SETTINGS 里有 lastExportDir 且是空串", m.DEFAULT_SETTINGS.get("lastExportDir") == "")
    check("_coerce_setting 接受字符串并 strip",
          m._coerce_setting("lastExportDir", "  D:/x  ", {}) == "D:/x")

    drop_tree(TMP)


def main() -> int:
    setup()
    run()
    ok = sum(1 for r in results if r[0])
    print("=" * 66)
    print(f"结果：{ok}/{len(results)} 通过")
    for good, name, detail in results:
        if not good:
            print(f"  FAIL  {name}  {detail}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
