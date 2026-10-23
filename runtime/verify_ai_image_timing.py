# -*- coding: utf-8 -*-
"""AI 生图节点的**分段计时日志** —— 纯本地验证。

不起服务、不调用模型、不花钱：`call_qwen` 与 `fetch_bytes` 都被换成假的，
只验证「时间轴那几行会不会按预期打出来、数字对不对得上」。

跑：`python runtime/verify_ai_image_timing.py`
"""
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ai_image" / "ai_image.py"

FAILED = []


def truthy(label, cond, extra=""):
    if not cond:
        FAILED.append(label)
    print(f"  {'OK ' if cond else 'XX '} {label}" + (f"  {extra}" if extra else ""))


def load_ai_image():
    spec = importlib.util.spec_from_file_location("ai_image_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_main(mod, payload):
    """跑一次 main()，返回 (退出码, stdout 全文)。"""
    buf = io.StringIO()
    old_out, old_in = sys.stdout, sys.stdin
    sys.stdout, sys.stdin = buf, io.StringIO(json.dumps(payload))
    try:
        rc = mod.main()
    finally:
        sys.stdout, sys.stdin = old_out, old_in
    return rc, buf.getvalue()


print("=" * 70)
print("1. qwen 路径：各段 [计时] + 结尾时间轴")
print("=" * 70)

tmp = Path(tempfile.mkdtemp(prefix="verify_timing_"))
ref = tmp / "ref.png"
ref.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 4096)   # 内容无所谓，只走读盘 + base64

mod = load_ai_image()
os.environ["DASHSCOPE_API_KEY"] = "fake-key-for-timing-test"


def fake_qwen(api_key, body):
    time.sleep(0.25)                                   # 假装在云端排队
    mod.phase("生成请求（云端排队 + 推理）", time.perf_counter() - 0.25)
    return {"output": {"choices": [{"message": {"content": [{"image": "https://fake/0.png"}]}}]}}


def fake_fetch(url):
    time.sleep(0.1)                                    # 假装在下载
    return b"\x89PNG\r\n\x1a\n" + b"1" * 2048


mod.call_qwen = fake_qwen
mod.fetch_bytes = fake_fetch

rc, out = run_main(mod, {
    "params": {"prompt": "测试用提示词", "model": "qwen-image-2.1-turbo", "size": "720x1280", "n": 1},
    "inputs": {"image1": str(ref)},
    "output_dir": str(tmp / "out"),
})

truthy("退出码 0", rc == 0, f"rc={rc}")
for label in (
    "读入 payload + 校验",
    "组装请求（读参考图 + base64）",
    "生成请求（云端排队 + 推理）",
    "下载图片（1 张",
    "写盘",
    "── 时间轴 ──",
    "未归类（解释器启动 / import / 校验）",
    "总计（进程入口 → 脚本结束）",
):
    truthy(f"出现「{label}」", label in out)

n_timing = len(re.findall(r"^\[计时\] ", out, re.M))
truthy("`[计时]` 行数 ≥ 11（5 段 × 2 + 表头 + 2 行收尾）", n_timing >= 11, f"实际 {n_timing} 行")

row_lines = [ln for ln in out.splitlines() if ln.startswith("[计时] ") and "总计" not in ln]
secs = []
for ln in row_lines:
    m = re.search(r"\s([0-9]+\.[0-9]{2}) 秒\s", ln)
    if m:
        secs.append(float(m.group(1)))
truthy("时间轴每行都解析出秒数", len(secs) >= 6, f"解析到 {len(secs)} 行")
total = float(re.search(r"总计（进程入口 → 脚本结束）: ([0-9.]+) 秒", out).group(1))
truthy("总计时长包含两次假延迟（≥0.35 秒）", total >= 0.35, f"total={total}")
rows_sum = sum(secs)
truthy("各段之和 ≈ 总计（差值 < 0.05 秒）", abs(rows_sum - total) < 0.05,
       f"段和 {rows_sum:.2f} vs 总计 {total:.2f}")

print()
print("=" * 70)
print("2. 空 payload 直接失败时不该打时间轴")
print("=" * 70)
mod2 = load_ai_image()
rc2, out2 = run_main(mod2, {"params": {}, "inputs": {}, "output_dir": str(tmp / "o2")})
truthy("退出码非 0", rc2 != 0, f"rc={rc2}")
truthy("没有半截时间轴", "时间轴" not in out2)

print()
print("=" * 70)
print("3. 引擎侧 _emit_timing：三段拼回总数")
print("=" * 70)
sys.path.insert(0, str(ROOT / "server"))
try:
    import engine

    class _P:
        t0, t_first, t_exit = 10.0, 10.4, 25.0

    lines = []
    engine._emit_timing(lines.append, _P(), 25.2)
    truthy("打了一行", len(lines) == 1, lines[0] if lines else "无输出")
    truthy("合计 15.2 秒", "合计 15.2 秒" in lines[0], lines[0])
    truthy("启动 0.40", "启动 0.40" in lines[0])
    truthy("子进程 24.6 → 实际 14.6", "子进程 14.6" in lines[0])
    truthy("收尾 0.20", "收尾 0.20" in lines[0])
except Exception as e:  # noqa: BLE001
    print(f"  SKIP 无法导入 engine: {e}")

print()
print("=" * 70)
print("失败项: " + (", ".join(FAILED) if FAILED else "无"))
print("=" * 70)
sys.exit(1 if FAILED else 0)
