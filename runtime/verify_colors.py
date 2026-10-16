# -*- coding: utf-8 -*-
"""验证「颜色库」——文档级字段 `colors` 的增删改（见 DESIGN.md §3.20）。

纯本地：不起服务、不调用任何模型、不需要 API key。直接驱动 `server/main.py` 里的
路由函数（它们就是普通函数），断言：

  ① 新文档 colors 默认空列表；老文档缺字段被补上、脏数据被丢掉（读坏值不报错）
  ② 新增：排在末尾、id 唯一、值被规范成小写 `#rrggbb`（手写 #ABC / 无 # 都认）
  ③ 校验：名字空 / 太长、值不是 6 位 hex（含带 alpha 的 8 位）→ 400，且**不动文档**
  ④ 改：只改名 / 只改值 / 两个一起改；不存在的 id → 404；空请求 → 400
  ⑤ 删：删掉 + 后面的前移；不存在的 id → 404
  ⑥ 落盘：graph.json 里 colors 是**文档级**字段（不在任何画布里面），
     重读文档（等价于重启服务）后一模一样；随便换个画布颜色库不变
  ⑦ 颜色库与画布内容互不干扰

    runtime\\venv\\Scripts\\python.exe runtime\\verify_colors.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import main as m  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from workspace import Workspace, drop_tree  # noqa: E402

TMP = ROOT / "runtime" / "_colors_tmp"
WS = TMP / "ws"

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
    m.STATE["workspace"] = Workspace(WS)
    m.STATE["workspace"].ensure()
    m.STATE["doc"] = None
    m.STATE["graph"] = None
    m.STATE["registry"] = {}
    m.STATE["settings"] = json.loads(json.dumps(m.DEFAULT_SETTINGS))


def colors() -> list[dict]:
    return m.state_payload()["colors"]


def names() -> list[str]:
    return [c["name"] for c in colors()]


def run() -> None:
    print("=" * 66)
    print("[1] 文档级字段：colors 默认空，老文档缺字段被补上、脏数据被丢掉")
    print("=" * 66)
    check("新文档 colors 是空列表", m.doc().get("colors") == [], repr(m.doc().get("colors")))
    check("state_payload 里带 colors", m.state_payload().get("colors") == [])

    old = {"version": 2, "current": "c_x", "canvases": [{"id": "c_x", "name": "画布 1"}]}
    norm = Workspace(WS).normalize_doc(json.loads(json.dumps(old)))
    check("老文档缺 colors 被补空列表", norm.get("colors") == [], repr(norm.get("colors")))

    dirty = {
        "version": 2,
        "current": "c_x",
        "canvases": [{"id": "c_x", "name": "画布 1"}],
        "colors": [
            {"id": "c1", "name": "  主色  ", "value": "#ABC"},   # 手写简写 → 归一
            {"id": "c2", "name": "", "value": "#ffffff"},        # 名字空 → 丢
            {"id": "c9", "name": "坏值", "value": "nope"},        # 值不合法 → 丢
            {"id": "c3", "name": "重复 id", "value": "#112233"},  # 这一条留着（见下）
            {"id": "c3", "name": "也重复", "value": "#445566"},   # id 撞了 → 换一个
            "不是字典",                                            # 类型不对 → 丢
            {"name": "没有 id", "value": "ff8800"},               # 没 id → 补一个
        ],
    }
    norm = Workspace(WS).normalize_doc(dirty)
    got = norm["colors"]
    check("只留下 4 条合法的", len(got) == 4, str(got))
    check("名字首尾空白被去掉、值归一成小写 6 位", got[0] == {"id": "c1", "name": "主色", "value": "#aabbcc"}, str(got[0]))
    check("重复的 id 只留第一个，后一个换新 id", got[1]["id"] == "c3" and got[2]["id"] != "c3", str([c["id"] for c in got]))
    check("留下的 id 互不相同", len({c["id"] for c in got}) == 4, str([c["id"] for c in got]))
    check("缺 id 的被补上、无 # 的值也认", got[3]["id"].startswith("color_") and got[3]["value"] == "#ff8800", str(got[3]))

    print("=" * 66)
    print("[2] 新增：排在末尾 + 值被规范化")
    print("=" * 66)
    m.api_color_add({"name": "主色", "value": "#4F9CF9"})
    m.api_color_add({"name": "强调色 · 深", "value": "1E2127"})
    m.api_color_add({"name": "简写", "value": "#abc"})
    cs = colors()
    check("三条都在、顺序 = 添加顺序", names() == ["主色", "强调色 · 深", "简写"], str(names()))
    check("大写归一成小写", cs[0]["value"] == "#4f9cf9", cs[0]["value"])
    check("没有 # 也认", cs[1]["value"] == "#1e2127", cs[1]["value"])
    check("3 位简写展开成 6 位", cs[2]["value"] == "#aabbcc", cs[2]["value"])
    check("每条的 id 唯一且非空", len({c["id"] for c in cs}) == 3 and all(c["id"] for c in cs), str([c["id"] for c in cs]))

    print("=" * 66)
    print("[3] 校验：坏输入报 400，且不动已有数据")
    print("=" * 66)
    before = json.dumps(colors(), sort_keys=True)
    check("名字空 → 400", expect_http(m.api_color_add, {"name": "   ", "value": "#ffffff"}) == 400)
    check("没给名字 → 400", expect_http(m.api_color_add, {"value": "#ffffff"}) == 400)
    check(
        f"名字超长（{m.COLOR_NAME_MAX} 上限）→ 400",
        expect_http(m.api_color_add, {"name": "x" * (m.COLOR_NAME_MAX + 1), "value": "#ffffff"}) == 400,
    )
    check("带 alpha 的 8 位十六进制 → 400", expect_http(m.api_color_add, {"name": "半透明", "value": "#11223344"}) == 400)
    check("不是十六进制 → 400", expect_http(m.api_color_add, {"name": "坏", "value": "red"}) == 400)
    check("空值 → 400", expect_http(m.api_color_add, {"name": "空", "value": ""}) == 400)
    check("缺值 → 400", expect_http(m.api_color_add, {"name": "缺"}) == 400)
    check("一路 400 之后，颜色库一个字没动", json.dumps(colors(), sort_keys=True) == before)

    print("=" * 66)
    print("[4] 改：只改名 / 只改值 / 一起改")
    print("=" * 66)
    cid = colors()[1]["id"]
    m.api_color_update(cid, {"name": "深色底"})
    check("只改名：名字换了、值没动", colors()[1]["name"] == "深色底" and colors()[1]["value"] == "#1e2127", str(colors()[1]))
    m.api_color_update(cid, {"value": "#FFEE00"})
    check("只改值：值换了、名字没动", colors()[1]["name"] == "深色底" and colors()[1]["value"] == "#ffee00", str(colors()[1]))
    m.api_color_update(cid, {"name": "高亮", "value": "#0a0b0c"})
    check("两个一起改", colors()[1] == {"id": cid, "name": "高亮", "value": "#0a0b0c"}, str(colors()[1]))
    check("改不存在的 id → 404", expect_http(m.api_color_update, "color_nope", {"name": "x"}) == 404)
    check("空请求 → 400", expect_http(m.api_color_update, cid, {}) == 400)
    check("改成空名字 → 400", expect_http(m.api_color_update, cid, {"name": "  "}) == 400)
    check(
        "改成非法值 → 400 且值没动",
        expect_http(m.api_color_update, cid, {"value": "#12345"}) == 400 and colors()[1]["value"] == "#0a0b0c",
    )

    print("=" * 66)
    print("[5] 删：删掉 + 后面的前移")
    print("=" * 66)
    left = m.api_color_delete(colors()[0]["id"])
    check("state 里少了一条", len(left["colors"]) == 2, str(names()))
    check("后面的前移（顺序保持）", names() == ["高亮", "简写"], str(names()))
    check("删不存在的 id → 404", expect_http(m.api_color_delete, "color_nope") == 404)
    while colors():  # 剩下的逐条删干净
        m.api_color_delete(colors()[0]["id"])
    check("删到空", colors() == [], str(colors()))
    check("删空之后再删同一条 → 404", expect_http(m.api_color_delete, cid) == 404)

    print("=" * 66)
    print("[6] 落盘 + 重读：colors 在文档级，跟画布无关")
    print("=" * 66)
    m.api_color_add({"name": "主色", "value": "#4f9cf9"})
    m.api_color_add({"name": "底", "value": "#16181d"})
    raw = json.loads((WS / "graph.json").read_text(encoding="utf-8"))
    check("graph.json 顶层有 colors", isinstance(raw.get("colors"), list) and len(raw["colors"]) == 2, str(raw.get("colors")))
    check(
        "colors 不在画布里面",
        all("colors" not in c for c in raw["canvases"]),
        str([sorted(c) for c in raw["canvases"]]),
    )

    # 重读文档 = 重启服务（内存全丢，只剩磁盘）
    m.STATE["doc"] = None
    m.STATE["graph"] = None
    check("重读后颜色还在、值不变", names() == ["主色", "底"], str(names()))

    # 换个画布：颜色库是文档级的，不该跟着画布走
    sp = m.api_create_canvas({"name": "画布 2"})
    check("切到新画布后颜色库原封不动", [c["name"] for c in sp["colors"]] == ["主色", "底"], str(names()))
    check("新画布是空的（颜色不在画布里）", not sp["graph"]["resources"] and not sp["graph"]["processes"])

    print("=" * 66)
    print("[7] 与画布内容互不干扰")
    print("=" * 66)
    n_before = len(m.state_payload()["graph"]["resources"])
    m.api_color_add({"name": "不碰画布", "value": "#ffffff"})
    check("加颜色不往画布上添节点", len(m.state_payload()["graph"]["resources"]) == n_before)
    check("state_payload 每次都返回独立的副本", m.state_payload()["colors"] is not m.doc()["colors"])

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
