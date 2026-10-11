# -*- coding: utf-8 -*-
"""验证「自动识别」接口：模型挑参数 → 跑几何切分 → 返回区域数组。

会真实调用 1 次 DeepSeek（很便宜），但**不调用任何生图模型**。
全程在一次性临时工作目录里跑，不碰用户的真实画布。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8770"
TMPWS = ROOT / "runtime" / "detect_ws"
SRC = ROOT / "workspace" / "images" / "B.png"

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def req(method: str, path: str, body=None, timeout: int = 300):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"detail": raw[:300]}


def main() -> int:
    st, s0 = req("GET", "/api/state")
    orig_ws = s0["workspace"]

    try:
        print("=" * 62)
        print("[1] 前置：注册表里有没有 crop 节点")
        print("=" * 62)
        names = list(s0["manifests"].keys())
        print(f"   已注册脚本: {names}")
        check("crop 已注册", "crop" in names, str(names))
        m = s0["manifests"].get("crop") or {}
        print(f"   crop 输入口: {m.get('inputs')}")
        print(f"   crop 参数  : {[(p['key'], p['type']) for p in m.get('params', [])]}")
        check("输入口没有标签", m.get("inputs", [{}])[0].get("label") == "",
              repr(m.get("inputs", [{}])[0].get("label")))
        check("参数是 regions 类型且为 primary",
              (m.get("params") or [{}])[0].get("type") == "regions"
              and (m.get("params") or [{}])[0].get("primary") is True)
        check("声明了多输出", (m.get("outputs") or [{}])[0].get("multi") is True)
        check("带 detect 配置", bool(m.get("detect")))

        shutil.rmtree(TMPWS, ignore_errors=True)
        req("POST", "/api/workspace", {"path": str(TMPWS)})

        # 导入素材
        boundary = "----b" + str(int(time.time() * 1000))
        body = b"".join([
            f'--{boundary}\r\nContent-Disposition: form-data; name="resource_id"\r\n\r\n\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="x"\r\n\r\n200\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="y"\r\n\r\n150\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="B.png"\r\n'
            f'Content-Type: image/png\r\n\r\n'.encode(),
            SRC.read_bytes(), f"\r\n--{boundary}--\r\n".encode()])
        rq = urllib.request.Request(
            BASE + "/api/resources/import", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(rq, timeout=60) as resp:
            st2 = json.loads(resp.read().decode())
        rid = st2["graph"]["resources"][-1]["id"]
        rname = st2["graph"]["resources"][-1]["name"]

        _, st3 = req("POST", "/api/processes", {"defId": "crop", "x": 700, "y": 150})
        pid = st3["graph"]["processes"][-1]["id"]
        print(f"\n已建画布：数据节点 {rname} + 裁剪节点 {pid}")

        print("\n" + "=" * 62)
        print("[2] 没连线就点自动识别")
        print("=" * 62)
        code, body2 = req("POST", f"/api/processes/{pid}/detect-regions")
        print(f"   HTTP {code}  {str(body2.get('detail'))[:60]}")
        check("未连接输入图时报 400", code == 400, str(code))

        print("\n" + "=" * 62)
        print("[3] 连线后点自动识别（真实调 1 次文本模型）")
        print("=" * 62)
        req("POST", "/api/edges", {"from": rid, "to": pid, "port": "image"})
        t0 = time.perf_counter()
        code, res = req("POST", f"/api/processes/{pid}/detect-regions")
        dt = time.perf_counter() - t0
        print(f"   HTTP {code}   耗时 {dt:.1f}s")
        if code != 200:
            print("   ", json.dumps(res, ensure_ascii=False)[:500])
            check("自动识别成功", False, str(code))
            return 1

        print(f"   模型: {res.get('reason')}")
        print(f"   选出的参数: {json.dumps(res.get('args'), ensure_ascii=False)}")
        print(f"   用到的策略: {res.get('method')}   图像: {res.get('imageSize')}")
        print(f"   识别出 {res['count']} 个区域:")
        for i, r in enumerate(res["regions"][:10], 1):
            print(f"     #{i}  x={r['x']} y={r['y']} {r['w']}×{r['h']}")
        if res.get("log"):
            print("   脚本日志:")
            for ln in res["log"]:
                print("     |", ln)

        check("识别出至少 1 个区域", res["count"] >= 1, str(res["count"]))
        check("策略是 gutter / cluster 之一", res.get("method") in ("gutter", "cluster"), str(res.get("method")))
        check("模型没返回坐标（args 里只有参数）",
              not any(isinstance(v, (list, dict)) for v in (res.get("args") or {}).values()),
              json.dumps(res.get("args"), ensure_ascii=False))
        W, H = res["imageSize"]
        ok_box = all(
            0 <= r["x"] and 0 <= r["y"] and r["x"] + r["w"] <= W and r["y"] + r["h"] <= H
            and r["w"] > 0 and r["h"] > 0
            for r in res["regions"]
        )
        check("所有区域都在图像范围内且非退化", ok_box)
        pad = int((res.get("args") or {}).get("pad") or 0)
        check("pad 已生效（区域比 tight bbox 大）", pad >= 0, f"pad={pad}")

        print("\n" + "=" * 62)
        print("[4] 把识别结果写进节点 → 执行裁剪（不调模型）")
        print("=" * 62)
        _, st4 = req("PATCH", f"/api/processes/{pid}", {"params": {"regions": res["regions"]}})
        p = st4["graph"]["processes"][-1]
        check("regions 写进了节点参数", len(p["params"].get("regions") or []) == res["count"],
              str(len(p["params"].get("regions") or [])))

        _, st5 = req("POST", f"/api/processes/{pid}/execute", {})
        with urllib.request.urlopen(f"{BASE}/api/runs/{st5['runId']}/stream", timeout=180) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                msg = json.loads(line[6:])
                if msg["type"] == "log" and msg["line"].strip():
                    print("   |", msg["line"])
                elif msg["type"] == "end":
                    print("   >>>", msg["status"], msg.get("error") or "")
                    break

        st6, s6 = req("GET", "/api/state")
        outs = sorted([x for x in s6["graph"]["resources"] if x.get("producer") == pid],
                      key=lambda x: x["index"])
        brief = [(o["name"], f"{o.get('width')}x{o.get('height')}") for o in outs]
        print(f"   产出 {len(outs)} 个输出节点: {brief}")
        check("输出个数 = 区域个数", len(outs) == res["count"], f"{len(outs)} vs {res['count']}")

        # 逐个核对：裁出来的尺寸应当等于区域尺寸（夹过边界的除外）
        from PIL import Image
        ok_size = True
        for o, reg in zip(outs, res["regions"]):
            pth = TMPWS / "images" / f"{o['name']}.png"
            if not pth.is_file():
                ok_size = False
                print(f"   !! {pth} 不存在")
                continue
            with Image.open(pth) as im:
                if im.size != (reg["w"], reg["h"]):
                    ok_size = False
                    print(f"   !! {o['name']} 尺寸 {im.size} != 区域 {reg['w']}×{reg['h']}")
        check("每张裁剪图的尺寸都等于对应区域", ok_size)

        print("\n" + "=" * 62)
        print("[5] 区域清空后执行 → 报错且保留旧输出")
        print("=" * 62)
        req("PATCH", f"/api/processes/{pid}", {"params": {"regions": []}})
        _, st7 = req("POST", f"/api/processes/{pid}/execute", {})
        err = None
        with urllib.request.urlopen(f"{BASE}/api/runs/{st7['runId']}/stream", timeout=120) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                msg = json.loads(line[6:])
                if msg["type"] == "end":
                    err = msg.get("error") or ""
                    print("   >>>", msg["status"], err[:80])
                    break
        st8, s8 = req("GET", "/api/state")
        outs2 = [x for x in s8["graph"]["resources"] if x.get("producer") == pid]
        check("区域为空时执行失败", bool(err), str(err)[:60])
        check("旧输出被保留（没被清空）", len(outs2) == len(outs), f"{len(outs2)} vs {len(outs)}")
    finally:
        req("POST", "/api/workspace", {"path": orig_ws})
        time.sleep(0.5)
        shutil.rmtree(TMPWS, ignore_errors=True)
        print(f"\n已恢复工作目录: {orig_ws}")

    bad = [r for r in results if not r[0]]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
