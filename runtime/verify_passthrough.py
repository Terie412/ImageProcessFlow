# -*- coding: utf-8 -*-
"""验证提示词原样透传：不再有任何「图片名 -> 第 N 张参考图」的隐式替换。

用 scripts/_echo_test 这个只回显参数、不调用在线模型的临时脚本跑，零成本。
全程在一次性临时工作目录里，不碰用户的真实工作目录。
"""
import json
import mimetypes
import shutil
import sys
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8770"
TMPWS = ROOT / "runtime" / "echo_ws"
SEED = ROOT / "runtime" / "seed.png"

PROMPT = "只保留参考图1中的人物，其余全部删除。"
NOTE = "顺手验证非主文本参数"


def call(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def upload(path, x=0, y=0):
    b = "----b" + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = b"".join(
        [
            f'--{b}\r\nContent-Disposition: form-data; name="resource_id"\r\n\r\n\r\n'.encode(),
            f'--{b}\r\nContent-Disposition: form-data; name="x"\r\n\r\n{x}\r\n'.encode(),
            f'--{b}\r\nContent-Disposition: form-data; name="y"\r\n\r\n{y}\r\n'.encode(),
            f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode(),
            path.read_bytes(),
            f"\r\n--{b}--\r\n".encode(),
        ]
    )
    req = urllib.request.Request(
        BASE + "/api/resources/import",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={b}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


orig = call("GET", "/api/state")["workspace"]
shutil.rmtree(TMPWS, ignore_errors=True)
call("POST", "/api/workspace", {"path": str(TMPWS)})

try:
    print("===== 1. 端口标签的写法 =====")
    st = upload(SEED, 80, 120)
    rid = st["graph"]["resources"][-1]["id"]
    st = call("POST", "/api/processes", {"defId": "_echo_test", "x": 520, "y": 120})
    pid = st["graph"]["processes"][-1]["id"]
    st = call("POST", "/api/edges", {"from": rid, "to": pid, "port": "image1"})
    # 撑到 2 个口，看第二个标签
    st = call("PATCH", f"/api/processes/{pid}", {"ports": {"image": 2}})
    labels = [s["label"] for s in next(p for p in st["graph"]["processes"] if p["id"] == pid)["portSpecs"]]
    print(f"  端口标签: {labels}")
    check("标签是「参考图1 / 参考图2」（无空格）", labels == ["参考图1", "参考图2"], str(labels))

    print("\n===== 2. 写进提示词，执行 =====")
    call("PATCH", f"/api/processes/{pid}", {"params": {"prompt": PROMPT, "note": NOTE}})
    rid_run = call("POST", f"/api/processes/{pid}/execute")["runId"]
    logs = []
    with urllib.request.urlopen(f"{BASE}/api/runs/{rid_run}/stream", timeout=120) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            msg = json.loads(line[6:])
            if msg["type"] == "log" and msg["line"].strip():
                logs.append(msg["line"])
                print("  |", msg["line"])
            elif msg["type"] == "end":
                print(f"  >>> 执行结果: {msg['status']} {msg.get('error') or ''}")
                break
    blob = "\n".join(logs)

    print("\n===== 3. 断言 =====")
    check("提示词原样出现在参数里", f'"prompt": {json.dumps(PROMPT, ensure_ascii=False)}' in blob)
    check("没有发生任何替换（日志不含「第1张参考图」）", "第1张参考图" not in blob)
    check("非主文本参数也原样透传", json.dumps(NOTE, ensure_ascii=False) in blob)
    check("日志列出了「参考图1 = 图片名」的对应", "参考图1 = A" in blob, "便于对照提示词")
    check("脚本自己收到的也是原文", f'"prompt": {json.dumps(PROMPT, ensure_ascii=False)}' in blob)
finally:
    call("POST", "/api/workspace", {"path": orig})
    shutil.rmtree(TMPWS, ignore_errors=True)
    print(f"\n已恢复工作目录: {orig}")

bad = [r for r in results if not r]
print(f"\n共 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
sys.exit(1 if bad else 0)
