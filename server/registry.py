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


# 「这个节点当前在跟哪个后端说话」由 model 参数决定 —— 见 backend_of 与 DESIGN.md §3.4。
MODEL_PARAM_KEY = "model"


def backend_of(manifest: dict | None, model: str | None) -> str | None:
    """当前选中的 `model` 属于哪个后端（`"qwen"` / `"openai"`…）。

    一个节点可以同时挂多个后端（`AI 生图 / 编辑` 就是 Qwen + GPT Image 两套），
    参数上标 `"backend": "qwen"` 表示「只在选中该后端的模型时显示 / 生效」。
    这个函数是**判定后端归属的唯一入口** —— `autosize` 和 `/api/state` 都走它，
    否则「界面显示哪套尺寸参数」和「执行时读哪个 key」会各算各的。

    判据是 model 参数每个 option 上的 `backend` 字段；没标 backend 的 option、
    或者 model 值不在选项里（例如 manifest 刚改过），一律返回 None ——
    调用方按「没有后端区分」处理，回退到不标 backend 的那份参数。
    """
    for p in (manifest or {}).get("params", []) or []:
        if p.get("key") != MODEL_PARAM_KEY:
            continue
        for o in p.get("options", []) or []:
            if isinstance(o, dict) and o.get("value") == model:
                return o.get("backend")
    return None


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
