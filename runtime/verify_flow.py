# -*- coding: utf-8 -*-
"""规则验证：原地刷新 / 多输出 / 槽位收缩 / 脏标记 / 文件清理。

在 demo 工作目录已有 A（输入）与 p1（生图节点）的基础上运行。
预计消耗 4 次生图调用。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8770"
WS = ROOT / "demo"
OK, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((OK if cond else FAIL, name, detail))
    print(f"   [{OK if cond else FAIL}] {name}" + (f"  — {detail}" if detail else ""))


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def run(pid: str, label: str) -> dict:
    print(f"\n>> 执行 {label}")
    rid = call("POST", f"/api/processes/{pid}/execute")["runId"]
    state = None
    with urllib.request.urlopen(f"{BASE}/api/runs/{rid}/stream", timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            m = json.loads(line[6:])
            if m["type"] == "log" and m["line"].strip():
                print("   |", m["line"])
            elif m["type"] == "end":
                state = m.get("state")
                print(f"   --> {m['status']}" + (f"  {m['error']}" if m.get("error") else ""))
                break
    return state


def snap(state):
    g = state["graph"]
    res = {r["id"]: r for r in g["resources"]}
    procs = {p["id"]: p for p in g["processes"]}
    files = {p.name for p in (WS / "images").glob("*")}
    return res, procs, files


def main() -> int:
    st = call("GET", "/api/state")
    g = st["graph"]
    input_res = next(r for r in g["resources"] if r["kind"] == "input")
    p1 = g["processes"][0]["id"]
    print(f"输入图 {input_res['name']} ({input_res['id']})，处理节点 {p1}")

    # ---------------- 1. 更好的提示词 + 原地刷新 ----------------
    print("\n" + "=" * 62)
    print("规则 1：重执行 → 数据节点原地刷新（id / 名字 / 坐标不变）")
    print("=" * 62)
    before = snap(st)
    b_before = next(r for r in before[0].values() if r.get("producer") == p1 and r["index"] == 0)

    call("PATCH", f"/api/processes/{p1}", {
        "params": {
            "prompt": "抠图编辑：只保留右侧的橙色圆形。删除左侧的蓝色矩形，删除下方的绿色三角形。",
            "transparent": True, "n": 1, "prompt_extend": False,
        }
    })
    st = run(p1, "① n=1（明确枚举的提示词）")
    after = snap(st)
    b_after = next(r for r in after[0].values() if r.get("producer") == p1 and r["index"] == 0)
    check("数据节点 id 不变", b_before["id"] == b_after["id"], f"{b_before['id']}")
    check("图片名不变", b_before["name"] == b_after["name"], f"{b_before['name']}")
    check("图片内容已刷新", b_after["mtime"] > b_before["mtime"], f"mtime {b_before['mtime']} → {b_after['mtime']}")
    check("工作目录里只有 A/B 两张图", after[2] == {f"{r['name']}.png" for r in after[0].values()}, str(sorted(after[2])))

    # ---------------- 2. 多输出 ----------------
    print("\n" + "=" * 62)
    print("规则 2：n=2 → 输出槽位增加，新增节点拿到新名字")
    print("=" * 62)
    call("PATCH", f"/api/processes/{p1}", {"params": {"n": 2}})
    st = run(p1, "② n=2（多输出）")
    after2 = snap(st)
    outs = sorted([r for r in after2[0].values() if r.get("producer") == p1], key=lambda r: r["index"])
    check("输出槽位数量 = 2", len(outs) == 2, f"{[r['name'] for r in outs]}")
    check("槽位 0 仍是原来的 B", outs[0]["id"] == b_before["id"] and outs[0]["name"] == b_before["name"])
    check("新增槽位有自己的名字", outs[1]["name"] not in ("A", "B"), f"新名 = {outs[1]['name']}")
    check("两张图都落盘", after2[2] == {f"{r['name']}.png" for r in after2[0].values()}, str(sorted(after2[2])))

    # 给第 2 个槽位接一个下游处理节点，用于验证「槽位收缩时连线断开」
    p2 = call("POST", "/api/processes", {"defId": "qwen_image", "x": 900, "y": 480})["graph"]["processes"][-1]["id"]
    st = call("POST", "/api/edges", {"from": outs[1]["id"], "to": p2, "port": "image1"})
    check("已把第 2 个槽位接到下游节点", any(
        e["from"] == outs[1]["id"] and e["to"] == p2 for e in st["graph"]["edges"]))

    # 也把槽位 0 接给 p2 的 image2 —— 顺便测「一个节点多个输入口」
    st = call("POST", "/api/edges", {"from": outs[0]["id"], "to": p2, "port": "image2"})
    check("同一节点的两个输入口各接一张", len([e for e in st["graph"]["edges"] if e["to"] == p2]) == 2)

    # ---------------- 3. 槽位收缩 ----------------
    print("\n" + "=" * 62)
    print("规则 3：n 回到 1 → 多余槽位被移除，文件永久删除，连线断开")
    print("=" * 62)
    call("PATCH", f"/api/processes/{p1}", {"params": {"n": 1}})
    st = run(p1, "③ n=1（槽位收缩）")
    after3 = snap(st)
    outs3 = [r for r in after3[0].values() if r.get("producer") == p1]
    check("输出槽位回到 1 个", len(outs3) == 1, f"{[r['name'] for r in outs3]}")
    check("被移除槽位的图片文件已永久删除", not (WS / "images" / f"{outs[1]['name']}.png").exists(),
          f"{outs[1]['name']}.png 不存在")
    check("指向它的连线已断开", not any(e["from"] == outs[1]["id"] for e in st["graph"]["edges"]))
    check("文件与画布严格对应", after3[2] == {f"{r['name']}.png" for r in after3[0].values()},
          str(sorted(after3[2])))

    # ---------------- 4. 脏标记 ----------------
    print("\n" + "=" * 62)
    print("规则 4：执行上游 → 直接下游处理节点被标脏（且不递归）")
    print("=" * 62)
    # 注意：p2 刚在上一步被连线，按设计「连边即标脏」，它此刻本就是脏的
    p2_before = snap(call("GET", "/api/state"))[1][p2]
    check("连线即标脏（p2 此刻应为脏）", p2_before.get("dirty") is True, f"dirty={p2_before.get('dirty')}")
    st = run(p1, "④ 再跑一次上游")
    p2_after = snap(st)[1][p2]
    check("执行后 p2 被标脏", p2_after.get("dirty") is True, f"dirty={p2_after.get('dirty')}")
    check("p2 的输出没有被牵连（脏标记不递归）",
          not any(r.get("producer") == p2 for r in st["graph"]["resources"]))

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 62)
    print("汇总")
    print("=" * 62)
    bad = [r for r in results if r[0] == FAIL]
    for tag, name, detail in results:
        print(f"  [{tag}] {name}" + (f"  — {detail}" if detail else ""))
    print(f"\n  共 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
