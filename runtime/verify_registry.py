# -*- coding: utf-8 -*-
"""验证脚本注册表的**两个根**：公开的 `scripts/` 与私有的 `scripts_private/`。

纯本地、不起服务、不调模型、不联网。

 ① 单个 Path 仍然能扫（向后兼容：老测试都这么调）
 ② 多个根按顺序合并；**后面的覆盖前面的**（私有同名 id 可以盖掉公开的）
 ③ 不存在的根直接跳过 —— 新克隆的仓库没有 `scripts_private/`，不能因此报错
 ④ 真仓库里：两个根都扫得到，私有那些节点确实被注册进来
 ⑤ **`scripts_private/` 里的文件全部被 git 排除**（拿 `git check-ignore` 问 git 本身，
    不是自己重新解释一遍 .gitignore）—— 这条是「私有节点不进 git」的守卫：
    哪天有人删了 .gitignore 里那行，这里会红

    runtime\\venv\\Scripts\\python.exe runtime\\verify_registry.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import registry as reg  # noqa: E402

fails: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def make_node(root: str, node_id: str, name: str) -> None:
    d = os.path.join(root, node_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"id": node_id, "name": name, "entry": "x.py", "_note": "内部备注"}, f, ensure_ascii=False)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="registry_test_")
    pub = os.path.join(tmp, "pub")
    priv = os.path.join(tmp, "priv")
    make_node(pub, "alpha", "公开的 alpha")
    make_node(pub, "blur", "公开的 blur")
    make_node(priv, "alpha", "私有的 alpha")
    make_node(priv, "secret", "私有 secret")

    print("[1] 单个根（向后兼容）")
    one = reg.load_manifests(pub)
    check(sorted(one) == ["alpha", "blur"], f"扫到 {sorted(one)}")
    check(one["alpha"]["name"] == "公开的 alpha", "名字对")

    print("[2] 两个根：合并 + 后者覆盖前者")
    both = reg.load_manifests([pub, priv])
    check(sorted(both) == ["alpha", "blur", "secret"], f"扫到 {sorted(both)}")
    check(both["alpha"]["name"] == "私有的 alpha", "同 id 时私有那份胜出")
    check(both["blur"]["name"] == "公开的 blur", "没冲突的原样保留")
    check(both["secret"]["_dir"].startswith(priv), "私有节点的 _dir 指向私有根（启动日志靠它数）")

    print("[3] 不存在的根直接跳过")
    missing = os.path.join(tmp, "nope")
    r = reg.load_manifests([pub, missing])
    check(sorted(r) == ["alpha", "blur"], "缺一个根不影响另一个")
    check(reg.load_manifests([missing]) == {}, "全都不存在 = 空表，不抛异常")

    print("[4] 缺省字段被补齐")
    m = one["blur"]
    check(m.get("inputs") == [] and m.get("params") == [] and m.get("outputs") == [], "inputs/params/outputs 补空表")
    check(reg.backend_of(m, None) is None, "没有 model 参数时 backend_of 是 None")

    print("[5] 下发前剥掉内部字段")
    pubm = reg.public_manifest(m)
    check(not any(k.startswith("_") for k in pubm), f"没有 _ 开头的键（{sorted(pubm)}）")
    check("id" in pubm and "name" in pubm, "该留的都还在")

    print("[6] 真仓库：两个根都扫得到")
    real = reg.load_manifests(
        [os.path.join(ROOT, "scripts"), os.path.join(ROOT, "scripts_private")]
    )
    check("crop" in real and "rounded_box" in real, f"公开节点在（共 {len(real)} 个）")
    priv_root = os.path.join(ROOT, "scripts_private").replace("\\", "/")
    priv_ids = [k for k, v in real.items() if str(v.get("_dir", "")).replace("\\", "/").startswith(priv_root)]
    if os.path.isdir(os.path.join(ROOT, "scripts_private")):
        check(len(priv_ids) > 0, f"私有节点也在（{priv_ids}）")
    else:
        print("  [SKIP] 本机没有 scripts_private/（新克隆的仓库就是这样）")

    print("[7] 守卫：scripts_private/ 里的文件必须全部被 git 排除")
    priv_dir = os.path.join(ROOT, "scripts_private")
    if not os.path.isdir(priv_dir):
        print("  [SKIP] 没有 scripts_private/")
    else:
        files = [
            os.path.relpath(os.path.join(dp, f), ROOT)
            for dp, _dn, fn in os.walk(priv_dir)
            for f in fn
            if "__pycache__" not in dp
        ]
        if not files:
            print("  [SKIP] scripts_private/ 是空的")
        else:
            # 直接问 git：这些路径算不算被忽略（不自己重新解释 .gitignore）
            r = subprocess.run(
                ["git", "check-ignore", "--stdin"],
                cwd=ROOT, input="\n".join(files), capture_output=True, text=True, encoding="utf-8",
            )
            ignored = [x for x in r.stdout.splitlines() if x.strip()]
            check(
                len(ignored) == len(files),
                f"{len(files)} 个文件全部被忽略（实测 {len(ignored)} 个）",
            )
            if len(ignored) != len(files):
                print("        没被忽略的：", [f for f in files if f not in ignored][:5])
            # 再确认一遍 git 没把它们当成未跟踪文件
            st = subprocess.run(
                ["git", "status", "--porcelain", "--", "scripts_private"],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            )
            check(st.stdout.strip() == "", f"git status 里看不到它们（{st.stdout.strip()[:120]!r}）")

    print("=" * 66)
    ok = 0 if fails else 1
    print("全部通过" if ok else f"{len(fails)} 项失败")
    for m in fails:
        print("  FAIL " + m)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
