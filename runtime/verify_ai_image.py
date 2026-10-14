# -*- coding: utf-8 -*-
"""AI 生图 / 编辑 —— backend 参数机制与 defId 迁移的本地验证。

纯本地：不起服务、不调用模型、不写盘（除了 py_compile 的缓存）。
"""
import json
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import autosize  # noqa: E402
import registry as registry_mod  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILED.append(label)
    print(f"  {'OK ' if ok else 'XX '} {label}: {got!r}" + ("" if ok else f"  期望 {want!r}"))


def truthy(label, cond, extra=""):
    if not cond:
        FAILED.append(label)
    print(f"  {'OK ' if cond else 'XX '} {label}" + (f"  {extra}" if extra else ""))


print("=" * 70)
print("1. manifest 结构")
print("=" * 70)
MF = ROOT / "scripts" / "ai_image" / "manifest.json"
mf_raw = json.loads(MF.read_text(encoding="utf-8"))
check("id", mf_raw["id"], "ai_image")
check("name", mf_raw["name"], "AI 生图 / 编辑")
truthy("entry 文件存在", (MF.parent / mf_raw["entry"]).is_file(), str(mf_raw["entry"]))
truthy("旧目录已消失", not (ROOT / "scripts" / "qwen_image").exists())

reg = registry_mod.load_manifests(ROOT / "scripts")
truthy("注册表里有 ai_image", "ai_image" in reg)
truthy("注册表里没有 qwen_image", "qwen_image" not in reg)
m = reg["ai_image"]

print()
print("=" * 70)
print("2. registry.backend_of")
print("=" * 70)
check("qwen 模型", registry_mod.backend_of(m, "qwen-image-2.1-turbo"), "qwen")
check("qwen 3.0", registry_mod.backend_of(m, "qwen-image-3.0"), "qwen")
check("gpt flare", registry_mod.backend_of(m, "gpt-image-2.5-flare"), "openai")
check("gpt sunburst", registry_mod.backend_of(m, "gpt-image-2.5-sunburst"), "openai")
check("未知模型 → None", registry_mod.backend_of(m, "dall-e-9"), None)
check("None 模型 → None", registry_mod.backend_of(m, None), None)

print()
print("=" * 70)
print("3. 参数按 backend 分组")
print("=" * 70)
by_key = {p["key"]: p for p in m["params"]}
check("size.backend", by_key["size"].get("backend"), "qwen")
check("size_gpt.backend", by_key["size_gpt"].get("backend"), "openai")
check("prompt_extend.backend", by_key["prompt_extend"].get("backend"), "qwen")
check("watermark.backend", by_key["watermark"].get("backend"), "qwen")
check("prompt 无 backend（通用）", by_key["prompt"].get("backend"), None)
check("model 无 backend（通用）", by_key["model"].get("backend"), None)
check("n 无 backend（通用）", by_key["n"].get("backend"), None)
check("transparent 无 backend（通用）", by_key["transparent"].get("backend"), None)

wanted_keys = {p["key"] for p in m["params"]}
check("参数全集", wanted_keys,
      {"prompt", "model", "size", "size_gpt", "n", "transparent", "prompt_extend", "watermark"})

# 两套尺寸的选项不重叠（除了「自动」这个空值）
q_opts = {o["value"] for o in by_key["size"]["options"] if o["value"]}
g_opts = {o["value"] for o in by_key["size_gpt"]["options"] if o["value"]}
print(f"     qwen 预设 {len(q_opts)} 个: {sorted(q_opts)}")
print(f"     gpt  预设 {len(g_opts)} 个: {sorted(g_opts)}")

print()
print("=" * 70)
print("4. autosize 按 backend 取 key")
print("=" * 70)
check("auto_key(qwen)", autosize.auto_key(m, "qwen"), "size")
check("auto_key(openai)", autosize.auto_key(m, "openai"), "size_gpt")
check("auto_key(不传) → 回退第一个", autosize.auto_key(m), "size")

print()
print("=" * 70)
print("5. 同一个参考图，两套预设各挑各的")
print("=" * 70)
# 941x1672 是既有断言里的经典反例（比例 0.5628，一张 9:16 竖图）
REF = (941, 1672)
v_q, info_q = autosize.resolve(m, {"size": "", "size_gpt": ""}, REF, "qwen")
v_g, info_g = autosize.resolve(m, {"size": "", "size_gpt": ""}, REF, "openai")
check("qwen 自动 →", v_q, "720x1280")
check("gpt  自动 →", v_g, "1152x2048")
truthy("qwen info.key", info_q and info_q.get("key") == "size", str(info_q and info_q.get("key")))
truthy("gpt  info.key", info_g and info_g.get("key") == "size_gpt", str(info_g and info_g.get("key")))

# 显式选过就尊重用户，且只读自己那套
v_q2, _ = autosize.resolve(m, {"size": "1536x1536", "size_gpt": "2048x2048"}, REF, "qwen")
v_g2, _ = autosize.resolve(m, {"size": "1536x1536", "size_gpt": "2048x2048"}, REF, "openai")
check("qwen 显式", v_q2, "1536x1536")
check("gpt  显式", v_g2, "2048x2048")

# 没有参考图 → 不发 size（交回模型）
v_q3, info_q3 = autosize.resolve(m, {"size": "", "size_gpt": ""}, None, "qwen")
v_g3, info_g3 = autosize.resolve(m, {"size": "", "size_gpt": ""}, None, "openai")
check("qwen 无参考图 → None", v_q3, None)
check("gpt  无参考图 → None", v_g3, None)
check("info.source", info_g3 and info_g3.get("source"), "model")

print()
print("=" * 70)
print("6. 服务端 defId 迁移（main._migrate）")
print("=" * 70)
import main  # noqa: E402

main.STATE["registry"] = reg
g = {
    "resources": [],
    "edges": [],
    "processes": [
        {"id": "p1", "defId": "qwen_image",
         "params": {"prompt": "旧的", "model": "qwen-image-2.1-turbo", "size": "720x1280"}},
        {"id": "p2", "defId": "crop", "params": {}},
    ],
}
main._migrate(g)
p1 = g["processes"][0]
check("defId 迁移", p1["defId"], "ai_image")
check("旧值保留", p1["params"]["size"], "720x1280")
truthy("补齐 size_gpt", "size_gpt" in p1["params"], repr(p1["params"].get("size_gpt")))
truthy("补齐 prompt_extend", "prompt_extend" in p1["params"])
truthy("补齐 watermark", "watermark" in p1["params"])
truthy("补齐 transparent", "transparent" in p1["params"])
truthy("补齐 n", "n" in p1["params"])
check("无关节点不动", g["processes"][1]["defId"], "crop")
truthy("迁移是幂等的", (main._migrate(g), g["processes"][0]["defId"])[1] == "ai_image")

print()
print("=" * 70)
print("7. 脚本可编译")
print("=" * 70)
try:
    py_compile.compile(str(ROOT / "scripts" / "ai_image" / "ai_image.py"), doraise=True)
    truthy("ai_image.py 语法", True)
except py_compile.PyCompileError as e:
    truthy("ai_image.py 语法", False, str(e))

print()
print("=" * 70)
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过")
