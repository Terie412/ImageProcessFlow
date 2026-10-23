# -*- coding: utf-8 -*-
"""AI 生图 / 编辑节点脚本 —— 一个节点，两套后端。

走哪一套由 `model` 的取值决定（判定见 `backend_of`）：

| | Qwen（DashScope） | GPT Image（OpenAI 兼容） |
| --- | --- | --- |
| 端点 | 一个，multimodal-generation | **两个**：无参考图 → generations，有参考图 → edits |
| 请求体 | JSON | 有参考图时是 **multipart/form-data** |
| 参考图 | 塞进 messages.content | generations 不收参考图；edits 用 image 字段（可重复） |
| 返回 | 图片 URL，还得再下载一次 | **只有 b64_json，没有 URL** |
| size 分隔符 | `720*1280`（星号） | `720x1280`（字母 x） |
| 尺寸参数 key | `size` | `size_gpt` |
| 鉴权环境变量 | `DASHSCOPE_API_KEY` | `GPT-IMAGE-2-KEY` |

节点脚本契约（见 DESIGN.md §2.5）：
  stdin  ← 一行 JSON: {"params": {...}, "inputs": {"image1": 路径或 null, ...}, "output_dir": 目录}
  stdout → 日志（原样显示在节点上）
  产出   → 把图片写进 output_dir，命名为 0.png、1.png…
  成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr
"""
from __future__ import annotations

import time

# 进程内能取到的**最早**时刻，故意放在其余 import 之前 ——
# 「脚本总计」从这里起算，才把 import 这些开销也纳进来（见 `print_timeline()`）。
_T_BOOT = time.perf_counter()

import base64  # noqa: E402
import json  # noqa: E402
import mimetypes  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import unicodedata  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

# --------------------------------------------------------------------------
# 后端常量
# --------------------------------------------------------------------------
DASHSCOPE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
DASHSCOPE_KEY_ENV = "DASHSCOPE_API_KEY"

# OpenAI 兼容端点。中转站换了改这里，或设环境变量 GPT_IMAGE_BASE_URL 覆盖。
OPENAI_BASE_URL = (os.environ.get("GPT_IMAGE_BASE_URL") or "https://xxapi.info/v1").rstrip("/")
OPENAI_KEY_ENV = "GPT-IMAGE-2-KEY"
OPENAI_MODEL_PREFIX = "gpt-image-"

# Cloudflare 按客户端指纹拦人：python-urllib 的默认 UA 直接吃 403（`error code: 1010`），
# 必须装成浏览器。实测踩出来的，别删（2026-10-14）。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

BACKEND_LABEL = {"qwen": "Qwen / DashScope", "gpt": "GPT Image / OpenAI 兼容"}

# 两套尺寸参数各自的 key、以及各自要求的宽高分隔符
SIZE_KEY = {"qwen": "size", "gpt": "size_gpt"}
SIZE_SEP = {"qwen": "*", "gpt": "x"}

DEFAULT_MODEL = "qwen-image-2.1-turbo"

# 透明底：**两个后端都靠提示词**，不是参数 —— 实测（2026-10-14）这个中转站会把
# `background="transparent"` 静默丢掉（传了返回的仍是 RGB、无 alpha），
# 改在提示词里要求反而能拿到真正的 alpha（PNG colorType=6）。
TRANSPARENT_SUFFIX = (
    "。输出带有透明背景的 PNG，背景 alpha 通道为全透明，"
    "主体以外的所有内容全部删除。"
)


def log(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# 分段计时
# --------------------------------------------------------------------------
# 节点右上角那个「28.8s」是引擎从**拉起子进程**量到**文件搬进图片池**的墙钟时间，
# 中间发生的事它一概不知。所以脚本这边把能自己量的段逐段打出来，末尾再给一张
# 对齐的时间轴 —— 「慢在排队、慢在下载、还是慢在本地」一眼能看出来。
#
# 量不到的两段由引擎补：`spawn → 首行日志`（解释器冷启动 + import）和
# `子进程退出 → 文件搬完`（收尾）。两边合起来正好是界面上的那个数。
_PHASES: list[tuple[str, float]] = []


def phase(label: str, since: float) -> float:
    """收一段耗时：打一行 `[计时]`、记进时间轴，返回当前时刻给下一段接着用。"""
    now = time.perf_counter()
    secs = now - since
    _PHASES.append((label, secs))
    log(f"[计时] {label}: {secs:.2f} 秒")
    return now


def _disp_width(s: str) -> int:
    """中文/全角按 2 格算 —— 不然时间轴那一列歪得没法看。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def print_timeline(total: float) -> None:
    """把各段耗时打成一张时间轴。

    `total` = 从进程入口到**现在**的墙钟时间（`_T_BOOT` 起算）。
    各段之和通常略小于它：差值是解释器起来、import、以及各段之间的校验逻辑 ——
    统一归到「未归类」那一行，不硬凑。
    """
    if not _PHASES:
        return
    rows = list(_PHASES)
    rows.append(("未归类（解释器启动 / import / 校验）", max(0.0, total - sum(s for _, s in rows))))
    name_w = max(_disp_width(label) for label, _ in rows)

    log("")
    log("[计时] ────── 时间轴 ──────")
    for label, secs in rows:
        share = f"{secs / total * 100:5.1f}%" if total > 0 else "    - "
        pad = " " * (name_w - _disp_width(label))
        log(f"[计时] {label}{pad}  {secs:6.2f} 秒  {share}")
    log(f"[计时] 总计（进程入口 → 脚本结束）: {total:.2f} 秒")


# --------------------------------------------------------------------------
# 公共小工具
# --------------------------------------------------------------------------
def backend_of(model: str) -> str:
    """这个 model 走哪套后端。

    ⚠️ 判定规则必须和 `server/registry.backend_of()` 保持一致 —— 那边按 manifest 里
    model option 上的 `backend` 字段读，这边按 model 名前缀推。两者只在这份 manifest
    的 model 取值范围内需要一致：加新模型时两处一起改（manifest 的 option.backend
    决定界面显示哪套参数，这里决定请求发去哪）。
    """
    return "gpt" if (model or "").startswith(OPENAI_MODEL_PREFIX) else "qwen"


def collect_images(inputs: dict) -> list[str]:
    """按端口序号把所有非空的参考图收拢成有序列表（image1、image2…）。

    顺序 = 提示词里「参考图N」的顺序，所以必须按数字排，不能靠 dict 的插入序。
    """
    found: list[tuple[int, str]] = []
    for key, path in (inputs or {}).items():
        m = re.fullmatch(r"image(\d+)", str(key))
        if m and path:
            found.append((int(m.group(1)), str(path)))
    found.sort(key=lambda x: x[0])
    return [p for _, p in found]


def build_prompt(params: dict) -> str:
    """提示词 + （勾了透明底时的）附加要求。原样透传，不做改写。"""
    prompt = (params.get("prompt") or "").strip()
    if params.get("transparent"):
        prompt += TRANSPARENT_SUFFIX
    return prompt


def normalize_size(raw, backend: str) -> str:
    """把宽高统一成目标后端要的分隔符。

    DashScope 只认星号（写成 `720x1280` 会 400：`Expected format: '<width>*<height>'`），
    OpenAI 只认字母 x。manifest 里统一写成人眼友好的 `x`，发送前在这里转。
    """
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not s:
        return ""
    return re.sub(r"\s*[xX×*]\s*", SIZE_SEP[backend], s)


def _n_of(params: dict) -> int:
    try:
        return max(1, int(params.get("n") or 1))
    except (TypeError, ValueError):
        return 1


def write_outputs(output_dir: str, blobs: list[bytes]) -> int:
    os.makedirs(output_dir, exist_ok=True)
    for i, blob in enumerate(blobs):
        dest = os.path.join(output_dir, f"{i}.png")
        with open(dest, "wb") as f:
            f.write(blob)
        log(f"[结果] {i}.png  ({len(blob) / 1024:.0f} KB)")
    return 0


# --------------------------------------------------------------------------
# 后端一：Qwen / DashScope
# --------------------------------------------------------------------------
def encode_local_image(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        raise ValueError(f"不支持的图片格式: {path}")
    with open(path, "rb") as f:
        return f"data:{mime};base64,{base64.b64encode(f.read()).decode('utf-8')}"


def to_image_ref(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return encode_local_image(os.path.abspath(path_or_url))


def build_qwen_body(params: dict, images: list[str]) -> dict:
    content: list[dict] = []
    for img in images:
        content.append({"image": to_image_ref(img)})
    content.append({"text": build_prompt(params)})

    # 面积须在 512x512 ~ 2560x2560 之间（图生图上限 2048x2048）。
    parameters = {
        "n": _n_of(params),
        "prompt_extend": bool(params.get("prompt_extend")),
        "watermark": bool(params.get("watermark")),
    }
    size = normalize_size(params.get(SIZE_KEY["qwen"]), "qwen")
    if size:
        parameters["size"] = size

    return {
        "model": params.get("model") or DEFAULT_MODEL,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": parameters,
    }


def call_qwen(api_key: str, body: dict) -> dict:
    req = urllib.request.Request(
        DASHSCOPE_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}\n{detail[:2000]}") from e
    phase("生成请求（云端排队 + 推理）", t0)
    return result


def extract_qwen_urls(result: dict) -> list[str]:
    urls: list[str] = []
    try:
        choices = result["output"]["choices"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"响应结构异常: {json.dumps(result, ensure_ascii=False)[:1500]}") from e
    for ch in choices:
        for item in ch.get("message", {}).get("content", []) or []:
            if isinstance(item, dict) and item.get("image"):
                urls.append(item["image"])
    if not urls:
        raise RuntimeError(f"响应中没有图片: {json.dumps(result, ensure_ascii=False)[:1500]}")
    return urls


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


# --------------------------------------------------------------------------
# 后端二：GPT Image / OpenAI 兼容
# --------------------------------------------------------------------------
def openai_fields(params: dict) -> dict:
    """OpenAI 两端点共用的字段。

    `n` 保持**数字**类型：JSON 端点（generations）按 uint 解析，发字符串会直接 400
    （`json: cannot unmarshal string into Go struct field Alias.n of type uint`）。
    multipart 那端只能是字符串，由 `encode_multipart` 的 f-string 自动转，不用管。
    """
    fields = {
        "model": params.get("model") or DEFAULT_MODEL,
        "prompt": build_prompt(params),
        "n": _n_of(params),
    }
    size = normalize_size(params.get(SIZE_KEY["gpt"]), "gpt")
    if size:
        fields["size"] = size
    # 刻意**不发** background / quality / output_format：
    #   实测（2026-10-14）这个中转站会把 background 丢掉 —— 传了返回的仍是 RGB 无 alpha，
    #   响应顶层也不回显该字段；quality 同样不生效（max 与默认档的 output tokens 基本一致）。
    #   透明底因此改由提示词实现（见 build_prompt）。将来中转站支持了再加回来。
    return fields


def encode_multipart(fields: dict, files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    """手工拼 multipart/form-data（脚本不依赖 requests，只用标准库）。

    `files` 每项是 (字段名, 文件名, 内容)；参考图要重复用同一个字段名 `image`，
    这是 OpenAI edits 端点收多图的方式。
    """
    boundary = "----wbnode" + os.urandom(10).hex()
    out: list[bytes] = []
    for k, v in fields.items():
        out.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
        )
    for name, filename, blob in files:
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
            ).encode("utf-8")
            + blob
            + b"\r\n"
        )
    out.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(out), boundary


def call_openai(api_key: str, params: dict, images: list[str]) -> list[bytes]:
    """有参考图走 edits（multipart），没有走 generations（JSON）。"""
    t = time.perf_counter()
    fields = openai_fields(params)

    if images:
        files: list[tuple[str, str, bytes]] = []
        for p in images:
            with open(p, "rb") as f:
                files.append(("image", os.path.basename(p), f.read()))
        body, boundary = encode_multipart(fields, files)
        url = f"{OPENAI_BASE_URL}/images/edits"
        ctype = f"multipart/form-data; boundary={boundary}"
    else:
        body = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        url = f"{OPENAI_BASE_URL}/images/generations"
        ctype = "application/json"
    t = phase("组装请求（读参考图 + multipart）" if images else "组装请求（JSON）", t)

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": ctype,
            "Authorization": f"Bearer {api_key}",
            "User-Agent": BROWSER_UA,   # 少这个就吃 Cloudflare 403，见文件头注释
            "Accept": "application/json, text/plain, */*",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}\n{detail[:2000]}") from e
    t = phase("生成请求（中转站排队 + 推理）", t0)

    blobs = decode_b64_images(result)
    phase("解码 base64", t)
    return blobs


def decode_b64_images(result: dict) -> list[bytes]:
    """GPT Image 只返回 base64（`response_format` 该模型不支持，没有 URL 可下）。"""
    items = result.get("data") or []
    blobs: list[bytes] = []
    for it in items:
        if isinstance(it, dict) and it.get("b64_json"):
            blobs.append(base64.b64decode(it["b64_json"]))
    if not blobs:
        raise RuntimeError(f"响应中没有图片: {json.dumps(result, ensure_ascii=False)[:1500]}")
    return blobs


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    t = time.perf_counter()
    raw = sys.stdin.read()
    if not raw.strip():
        print("stdin 没有收到 JSON payload", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"payload 不是合法 JSON: {e}", file=sys.stderr)
        return 1

    params = payload.get("params") or {}
    inputs = payload.get("inputs") or {}
    output_dir = payload.get("output_dir")
    if not output_dir:
        print("payload 缺少 output_dir", file=sys.stderr)
        return 1

    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        print("提示词为空，请先填写 prompt", file=sys.stderr)
        return 1

    model = params.get("model") or DEFAULT_MODEL
    backend = backend_of(model)
    key_env = OPENAI_KEY_ENV if backend == "gpt" else DASHSCOPE_KEY_ENV

    api_key = os.environ.get(key_env)
    if not api_key:
        print(f"环境变量 {key_env} 未设置", file=sys.stderr)
        return 1

    images = collect_images(inputs)
    for p in images:
        if not os.path.isfile(p):
            print(f"输入图片不存在: {p}", file=sys.stderr)
            return 1

    size_value = normalize_size(params.get(SIZE_KEY[backend]), backend)
    t = phase("读入 payload + 校验", t)

    log(f"[后端] {BACKEND_LABEL[backend]}")
    log(f"[模型] {model}")
    if images:
        log(f"[输入] 共 {len(images)} 张参考图：" + ", ".join(os.path.basename(x) for x in images))
    else:
        log("[输入] 无参考图 → 文生图")
    log(f"[尺寸] {size_value or '未指定（模型自选）'}")
    log(f"[提示词] {prompt}")
    if params.get("transparent"):
        log("[提示词] 已追加透明背景要求")
    log("")

    try:
        if backend == "gpt":
            blobs = call_openai(api_key, params, images)   # 内部各段自测
        else:
            t_req = time.perf_counter()
            body = build_qwen_body(params, images)
            # 参考图越大这段越久：请求体里塞的是整张图的 base64（约 1.37 倍原体积）
            phase("组装请求（读参考图 + base64）", t_req)
            urls = extract_qwen_urls(call_qwen(api_key, body))
            t_dl = time.perf_counter()
            blobs = [fetch_bytes(u) for u in urls]
            kb = sum(len(b) for b in blobs) / 1024
            phase(f"下载图片（{len(blobs)} 张，{kb:.0f} KB）", t_dl)
    except Exception as e:  # noqa: BLE001
        print_timeline(time.perf_counter() - _T_BOOT)   # 失败也给出时间分布：卡在哪一段一眼可见
        print(str(e), file=sys.stderr)
        return 1

    log(f"[结果] 收到 {len(blobs)} 张图片")
    t = time.perf_counter()
    write_outputs(output_dir, blobs)
    phase("写盘", t)

    print_timeline(time.perf_counter() - _T_BOOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
