# -*- coding: utf-8 -*-
"""验证「删除某个输入口」：删中间一个之后，后面的端口依次前移。

用完即走，全程在一次性临时工作目录里，不碰用户的真实工作目录，也不调用在线模型。
"""
import json
import mimetypes
import shutil
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8770"
TMPWS = ROOT / "runtime" / "portdel_ws"
SEED = ROOT / "runtime" / "seed.png"


def call(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, {}


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


def ports_of(st, pid):
    p = next(x for x in st["graph"]["processes"] if x["id"] == pid)
    return [s["key"] for s in p["portSpecs"]], p.get("ports")


def links_of(st, pid):
    """返回 {端口key: 数据节点名}"""
    names = {r["id"]: r["name"] for r in st["graph"]["resources"]}
    return {e["port"]: names.get(e["from"]) for e in st["graph"]["edges"] if e["to"] == pid}


orig = call("GET", "/api/state")[1]["workspace"]
shutil.rmtree(TMPWS, ignore_errors=True)
call("POST", "/api/workspace", {"path": str(TMPWS)})

try:
    # 准备：3 张图 + 1 个节点 + 3 个输入口
    rids = []
    for i in range(3):
        st = upload(SEED, 60, 60 + i * 160)
        rids.append(st["graph"]["resources"][-1]["id"])
    st = call("POST", "/api/processes", {"defId": "qwen_image", "x": 600, "y": 120})[1]
    pid = st["graph"]["processes"][-1]["id"]
    st = call("PATCH", f"/api/processes/{pid}", {"ports": {"image": 3}})[1]

    for i, rid in enumerate(rids):
        st = call("POST", "/api/edges", {"from": rid, "to": pid, "port": f"image{i + 1}"})[1]

    names = {r["id"]: r["name"] for r in st["graph"]["resources"]}
    print(f"  三张图: {[names[r] for r in rids]}")
    keys, raw = ports_of(st, pid)
    print(f"  端口  : {keys}   连线: {links_of(st, pid)}")

    print("\n===== 1. 删除中间的「参考图2」=====")
    st_code, st = call("DELETE", f"/api/processes/{pid}/ports/image2")
    keys, raw = ports_of(st, pid)
    links = links_of(st, pid)
    print(f"  端口  : {keys}   连线: {links}   (ports={raw})")
    check("端口数量减到 2", keys == ["image1", "image2"], str(keys))
    check("参考图1 的图没变", links.get("image1") == names[rids[0]], str(links.get("image1")))
    check("原参考图3 的图前移到参考图2", links.get("image2") == names[rids[2]], str(links.get("image2")))
    check("被删的口不再有连线", "image3" not in links and len(links) == 2, str(links))

    print("\n===== 2. 再删「参考图1」（首部）=====")
    st = call("DELETE", f"/api/processes/{pid}/ports/image1")[1]
    keys, raw = ports_of(st, pid)
    links = links_of(st, pid)
    print(f"  端口  : {keys}   连线: {links}   (ports={raw})")
    check("端口数量减到 1", keys == ["image1"], str(keys))
    check("剩下的图整体前移到参考图1", links.get("image1") == names[rids[2]], str(links.get("image1")))

    print("\n===== 3. 异常输入 =====")
    code, body = call("DELETE", f"/api/processes/{pid}/ports/image9")
    print(f"  删除不存在的口 -> HTTP {code}")
    check("不存在的口被拒绝（404/400）", code in (400, 404), str(code))

    code, body = call("DELETE", f"/api/processes/{pid}/ports/nope1")
    print(f"  删除不存在的组 -> HTTP {code}")
    check("不存在的组被拒绝（404/400）", code in (400, 404), str(code))

    print("\n===== 4. 减到 0 之后再删 =====")
    st = call("DELETE", f"/api/processes/{pid}/ports/image1")[1]
    keys, raw = ports_of(st, pid)
    print(f"  端口  : {keys}   (ports={raw})")
    check("可以减到 0 个输入口（min=0）", keys == [], str(keys))
    code, _ = call("DELETE", f"/api/processes/{pid}/ports/image1")
    check("0 个时再删被拒绝", code in (400, 404), str(code))
finally:
    call("POST", "/api/workspace", {"path": orig})
    shutil.rmtree(TMPWS, ignore_errors=True)
    print(f"\n已恢复工作目录: {orig}")

bad = [r for r in results if not r]
print(f"\n共 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
sys.exit(1 if bad else 0)
