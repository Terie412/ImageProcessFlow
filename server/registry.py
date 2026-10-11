# -*- coding: utf-8 -*-
"""脚本注册表：扫描 scripts/*/manifest.json。

对应 DESIGN.md §2.3「脚本注册」：新增脚本 = 放一个 .py + 更新对应 manifest。
"""
from __future__ import annotations

import json
from pathlib import Path


def load_manifests(scripts_dir: str | Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    d = Path(scripts_dir)
    if not d.is_dir():
        return out
    for sub in sorted(d.iterdir()):
        mf = sub / "manifest.json"
        if not (sub.is_dir() and mf.is_file()):
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[registry] 跳过 {mf}: {e}")
            continue
        m.setdefault("id", sub.name)
        m["_dir"] = str(sub)
        m.setdefault("inputs", [])
        m.setdefault("params", [])
        m.setdefault("outputs", [])
        out[m["id"]] = m
    return out


def default_params(manifest: dict) -> dict:
    return {p["key"]: p.get("default") for p in manifest.get("params", [])}


def default_ports(manifest: dict) -> dict:
    """动态输入口的默认数量，例如 {"image": 1}"""
    return {
        s["key"]: int(s.get("count", 1))
        for s in manifest.get("inputs", [])
        if s.get("dynamic")
    }


def port_groups(manifest: dict, proc: dict | None = None) -> list[dict]:
    """动态输入口的分组信息，供前端渲染 +/- 控件。"""
    cur = (proc or {}).get("ports") or {}
    out = []
    for s in manifest.get("inputs", []):
        if not s.get("dynamic"):
            continue
        out.append(
            {
                "key": s["key"],
                "label": s.get("label") or s["key"],
                "count": int(cur.get(s["key"], s.get("count", 1))),
                "min": int(s.get("min", 0)),
                "max": int(s.get("max", 99)),
            }
        )
    return out


def port_specs(manifest: dict, proc: dict | None = None) -> list[dict]:
    """把 manifest 的 inputs 展开成实际的端口列表。

    固定端口直接用 key；动态端口按当前数量展开成 image1 / image2 / …
    见 DESIGN.md §3.4。
    """
    cur = (proc or {}).get("ports") or {}
    out: list[dict] = []
    for s in manifest.get("inputs", []):
        if not s.get("dynamic"):
            out.append(
                {
                    "key": s["key"],
                    # 显式写 "label": "" 就表示这个口不要标签（界面只画一个连线圈）
                    "label": s["label"] if "label" in s else s["key"],
                    "type": s.get("type", "image"),
                    "required": bool(s.get("required", False)),
                }
            )
            continue
        n = int(cur.get(s["key"], s.get("count", 1)))
        n = max(int(s.get("min", 0)), min(int(s.get("max", 99)), n))
        for i in range(1, n + 1):
            out.append(
                {
                    # 标签与提示词里的写法保持一致（「参考图1」而不是「参考图 1」），
                    # 这样用户照着端口标签写进提示词即可，没有隐式转换
                    "key": f"{s['key']}{i}",
                    "label": f"{s.get('label') or s['key']}{i}",
                    # 界面上输入口只显示序号（「1」「2」…），省地方；完整名字放 label 里当 tooltip
                    "num": i,
                    "type": s.get("type", "image"),
                    "required": bool(s.get("required", False)),
                    "group": s["key"],
                }
            )
    return out


def public_manifest(manifest: dict) -> dict:
    """去掉内部字段，用于返回给前端。"""
    return {k: v for k, v in manifest.items() if not k.startswith("_")}
