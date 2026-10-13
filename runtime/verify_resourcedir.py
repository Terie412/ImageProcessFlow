# -*- coding: utf-8 -*-
"""验证「资源目录」——工作目录之外的素材来源（只读 + 新建目录 / 改名 / 删除）。

纯本地：不起服务、不调用任何模型、不需要 API key。直接驱动 `server/main.py` 里的
路由函数（它们就是普通函数），断言：

  ① graph.json 里 resourceDir 默认空串、老文档缺字段时被补上（跟随工作目录）
  ② 路径穿越（`..` / 绝对路径 / 指向目录外的符号链接）一律被拒
  ③ list：目录在前文件在后、字段齐全、只有图片 isImage=true
  ④ raw：能取到文件、取不存在的 404
  ⑤ import-from-resource：复制进当前画布图片池 + 建数据节点 + 原文件不动（只读）
  ⑥ 非图片不给导
  ⑦ 写操作：新建目录 / 重命名 / 删除（含名字校验、越界、根目录不给动）
  ⑧ 空串 = 清除资源目录

> 「删除」在测试里把系统回收站换成了假实现（直接删），免得跑一次测试就往真回收站里
> 丢东西；真机上的回收站那条路见下面注释。

    runtime\\venv\\Scripts\\python.exe runtime\\verify_resourcedir.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import main as m  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from workspace import Workspace, drop_tree  # noqa: E402

TMP = ROOT / "runtime" / "_resdir_tmp"
WS = TMP / "ws"
SRC = TMP / "src"

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def expect_http(fn, *a, **kw):
    """跑一个应当抛 HTTPException 的调用，返回状态码；没抛返回 None。"""
    try:
        fn(*a, **kw)
    except HTTPException as e:
        return e.status_code
    except Exception as e:  # noqa: BLE001
        return f"其他异常 {type(e).__name__}: {e}"
    return None


def setup() -> None:
    drop_tree(TMP)
    WS.mkdir(parents=True, exist_ok=True)
    (SRC / "sub").mkdir(parents=True, exist_ok=True)
    (SRC / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    (SRC / "b.txt").write_text("hello", encoding="utf-8")
    (SRC / "sub" / "c.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
    # 指向资源目录**之外**的符号链接（能建就建，权限不够就跳过越界那条）
    try:
        os.symlink(ROOT / "README.md", SRC / "escape.md", target_is_directory=False)
    except (OSError, NotImplementedError):
        pass

    m.STATE["workspace"] = Workspace(WS)
    m.STATE["workspace"].ensure()
    m.STATE["doc"] = None
    m.STATE["graph"] = None
    m.STATE["registry"] = {}
    m.STATE["settings"] = json.loads(json.dumps(m.DEFAULT_SETTINGS))

    # 删除测试别真往系统回收站里丢东西 —— 把「进回收站」换成直接删（其余逻辑照跑）
    def fake_trash(p):
        if Path(p).is_dir():
            drop_tree(p)
        else:
            Path(p).unlink()
        return True

    m._win_send_to_trash = fake_trash


def run() -> None:
    print("=" * 66)
    print("[1] 文档级字段：resourceDir 跟随工作目录，默认空串")
    print("=" * 66)
    d = m.doc()
    check("新文档 resourceDir 默认空串", d.get("resourceDir") == "", repr(d.get("resourceDir")))
    check("state_payload 里带 resourceDir", m.state_payload().get("resourceDir") == "")

    old = {"version": 2, "current": "c_x", "canvases": [{"id": "c_x", "name": "画布 1"}]}
    norm = m.Workspace(WS).normalize_doc(json.loads(json.dumps(old)))
    check("老文档缺 resourceDir 被补空串", norm.get("resourceDir") == "", repr(norm.get("resourceDir")))

    print("=" * 66)
    print("[2] 指定资源目录 → 写进 graph.json")
    print("=" * 66)
    sp = m.api_set_resourcedir({"path": str(SRC)})
    check("state 下发新路径", sp.get("resourceDir") == str(SRC.resolve()), sp.get("resourceDir"))
    saved = json.loads((WS / "graph.json").read_text(encoding="utf-8"))
    check("路径落进 graph.json", saved.get("resourceDir") == str(SRC.resolve()))
    check("目录不存在时报 400", expect_http(m.api_set_resourcedir, {"path": str(SRC / "nope")}) == 400)

    print("=" * 66)
    print("[3] 路径穿越防护")
    print("=" * 66)
    check("`..` 上跳被拒", expect_http(m._resource_path, "../x") == 400)
    check("混在中间的 `..` 被拒", expect_http(m._resource_path, "sub/../../x") == 400)
    check("绝对路径（盘符）被拒", expect_http(m._resource_path, "C:/Windows/win.ini") == 400)
    check("正斜杠绝对路径被拒", expect_http(m._resource_path, "/etc/passwd") == 400)
    check("正常相对路径通过", m._resource_path("sub/c.jpg").name == "c.jpg")
    if (SRC / "escape.md").exists():
        check("指向目录外的符号链接被拒", expect_http(m._resource_path, "escape.md") == 400)
    else:
        print("  [SKIP] 符号链接（本机不允许创建）")

    print("=" * 66)
    print("[4] 列目录：懒加载一层，目录在前、图片可拖")
    print("=" * 66)
    r = m.api_list_resourcedir("")
    names = [e["name"] for e in r["entries"]]
    check("目录排在最前", names and names[0] == "sub", str(names))
    by = {e["name"]: e for e in r["entries"]}
    check("a.png 判定为图片", by["a.png"]["type"] == "file" and by["a.png"]["isImage"] is True)
    check("b.txt 不是图片", by["b.txt"]["isImage"] is False)
    check("文件带 rel / size / mtime", by["a.png"]["rel"] == "a.png" and by["a.png"]["size"] > 0)
    check("子目录带 hasChildren", by["sub"]["hasChildren"] is True)
    check("列子目录只给直接子项", [e["name"] for e in m.api_list_resourcedir("sub")["entries"]] == ["c.jpg"])
    check("列一个文件当目录 → 400", expect_http(m.api_list_resourcedir, "b.txt") == 400)

    print("=" * 66)
    print("[5] raw：只读取文件")
    print("=" * 66)
    fr = m.api_resourcedir_raw("b.txt")
    check("能取到文件", Path(str(fr.path)).name == "b.txt", str(fr.path))
    check("取不存在的 → 404", expect_http(m.api_resourcedir_raw, "nope.txt") == 404)
    check("越界取文件 → 400", expect_http(m.api_resourcedir_raw, "../README.md") == 400)

    print("=" * 66)
    print("[6] 拖进画布 = 复制一份 + 建数据节点（原文件不动）")
    print("=" * 66)
    sp = m.api_import_from_resource({"path": "a.png", "x": 10, "y": 20})
    g = sp["graph"]
    check("新建了一个数据节点", len(g["resources"]) == 1, str(len(g["resources"])))
    r0 = g["resources"][0]
    check("分配短名 A", r0.get("name") == "A", str(r0.get("name")))
    check("落在指定坐标", (r0.get("x"), r0.get("y")) == (10, 20))
    copied = m.view().image_path(r0["name"])
    check("图片复制进了画布池", copied is not None and copied.suffix == ".png", str(copied))
    check("副本内容与原文件一致", copied.read_bytes() == (SRC / "a.png").read_bytes())
    check("原文件仍在（资源目录只读）", (SRC / "a.png").is_file())
    # 再拖一张 → 名字递增，且能取子目录里的图（相对路径带子目录）
    sp = m.api_import_from_resource({"path": "sub/c.jpg"})
    check("第二张分配 B 且保留 .jpg", sp["graph"]["resources"][1]["name"] == "B")
    check("非图片被拒", expect_http(m.api_import_from_resource, {"path": "b.txt"}) == 400)
    check("越界路径被拒", expect_http(m.api_import_from_resource, {"path": "../README.md"}) == 400)

    print("=" * 66)
    print("[7] 写操作：新建目录 / 重命名 / 删除（面板右键那三样）")
    print("=" * 66)
    # --- 新建目录 ---
    r = m.api_resourcedir_mkdir({"parent": "", "name": "newdir"})
    check("根下新建目录，这一层列表里有它", any(e["name"] == "newdir" and e["type"] == "dir" for e in r["entries"]))
    check("磁盘上真建出来了", (SRC / "newdir").is_dir())
    check("返回的就是这一层（path=''）", r.get("path") == "")
    check("重名 → 400", expect_http(m.api_resourcedir_mkdir, {"parent": "", "name": "newdir"}) == 400)
    r = m.api_resourcedir_mkdir({"parent": "sub", "name": "inner"})
    check("在子目录里新建 → 返回 sub 这层", r.get("path") == "sub" and any(e["name"] == "inner" for e in r["entries"]))
    check("嵌套目录建出来了", (SRC / "sub" / "inner").is_dir())
    check("parent 是文件 → 400", expect_http(m.api_resourcedir_mkdir, {"parent": "a.png", "name": "x"}) == 400)
    check("parent 越界 → 400", expect_http(m.api_resourcedir_mkdir, {"parent": "../x", "name": "y"}) == 400)

    # --- 名字校验（新建与重命名共用同一个 _safe_child_name）---
    for bad in ["", "   ", ".", "..", "a/b", "a\\b", "a:b", "a.", "CON", "nul.txt", "x" * 201]:
        check(f"非法名字被拒：{bad[:10]!r}", expect_http(m._safe_child_name, bad) == 400)
    check("正常名字（含中文、首尾空格）通过", m._safe_child_name(" 我的图集 ") == "我的图集")

    # --- 重命名 ---
    r = m.api_resourcedir_rename({"path": "b.txt", "name": "c.txt"})
    check("文件改名成功", (SRC / "c.txt").is_file() and not (SRC / "b.txt").exists())
    check("返回父目录（根）这层", r.get("path") == "" and any(e["name"] == "c.txt" for e in r["entries"]))
    m.api_resourcedir_rename({"path": "newdir", "name": "renamed"})
    check("目录改名成功", (SRC / "renamed").is_dir() and not (SRC / "newdir").exists())
    check("改成已存在的名字 → 400", expect_http(m.api_resourcedir_rename, {"path": "a.png", "name": "c.txt"}) == 400)
    check("重命名资源目录根 → 400", expect_http(m.api_resourcedir_rename, {"path": "", "name": "x"}) == 400)
    check("重命名不存在的 → 404", expect_http(m.api_resourcedir_rename, {"path": "nope.txt", "name": "x"}) == 404)
    check("改成同名 = 原地不动，仍成功", m.api_resourcedir_rename({"path": "a.png", "name": "a.png"}).get("ok") is True)
    check("_parent_rel 取父目录", m._parent_rel("sub/c.jpg") == "sub" and m._parent_rel("a.png") == "")

    # --- 删除（测试里回收站是假的；真机上会进系统回收站）---
    (SRC / "sub" / "inner" / "deep.txt").write_text("x", encoding="utf-8")
    r = m.api_resourcedir_delete({"path": "sub"})
    check("删目录：连同里面的内容一起没了", not (SRC / "sub").exists())
    check("报告「进了回收站」", r.get("toTrash") is True)
    r = m.api_resourcedir_delete({"path": "c.txt"})
    check("删文件：从磁盘消失", not (SRC / "c.txt").exists())
    check("删完回的是父目录这层", all(e["name"] != "c.txt" for e in r["entries"]))
    check("删资源目录根 → 400", expect_http(m.api_resourcedir_delete, {"path": ""}) == 400)
    check("删越界路径 → 400", expect_http(m.api_resourcedir_delete, {"path": "../README.md"}) == 400)
    check("删不存在的 → 404", expect_http(m.api_resourcedir_delete, {"path": "nope"}) == 404)

    print("=" * 66)
    print("[8] 空串 = 清除资源目录")
    print("=" * 66)
    sp = m.api_set_resourcedir({"path": ""})
    check("清除后 state 为空串", sp.get("resourceDir") == "")
    check("清除后 list 报 400", expect_http(m.api_list_resourcedir, "") == 400)
    check("清除后 raw 报 400", expect_http(m.api_resourcedir_raw, "a.png") == 400)

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
