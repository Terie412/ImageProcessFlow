# -*- coding: utf-8 -*-
"""Qwen 生图 / 编辑节点脚本（阿里云百炼 DashScope）。

节点脚本契约（见 DESIGN.md §2.5）：
  stdin  ← 一行 JSON: {"params": {...}, "inputs": {"image1": 路径或 null, ...}, "output_dir": 目录}
  stdout → 日志（原样显示在节点上）
  产出   → 把图片写进 output_dir，命名为 0.png、1.png…
  成败   → 退出码 0 = 成功；非 0 = 失败，错误写 stderr

参考自 QwenImageGen skill 的 scripts/qwen_image_gen.py，从命令行参数改为 stdin JSON。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request

DASHSCOPE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)

TRANSPARENT_SUFFIX = (
    "。输出带有透明背景的 PNG，背景 alpha 通道为全透明，"
    "主体以外的所有内容全部删除。"
)

INPUT_ORDER = ("image1", "image2", "image3")


def log(msg: str = "") -> None:
    print(msg, flush=True)


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


def build_body(params: dict, images: list[str]) -> dict:
    content: list[dict] = []
    for img in images:
        content.append({"image": to_image_ref(img)})

    prompt = params.get("prompt", "") or ""
    if params.get("transparent"):
        prompt += TRANSPARENT_SUFFIX
    content.append({"text": prompt})

    # DashScope 要求的分隔符是「*」（如 720*1280），写成 x / × 都会被拒：
    #   Expected format: '<width>*<height>'
    # 所以这里统一转一下。面积需在 512x512 ~ 2560x2560 之间（图生图上限 2048x2048）。
    raw_size = (params.get("size") or "").strip()
    size = re.sub(r"\s*[xX×*]\s*", "*", raw_size) if raw_size else ""

    parameters = {
        "n": max(1, int(params.get("n") or 1)),
        "prompt_extend": bool(params.get("prompt_extend")),
        "watermark": bool(params.get("watermark")),
    }
    if size:
        parameters["size"] = size

    return {
        "model": params.get("model") or "qwen-image-2.1-turbo",
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": parameters,
    }


def call_api(api_key: str, body: dict) -> dict:
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
    log(f"[计时] 生成请求耗时: {time.perf_counter() - t0:.2f} 秒")
    return result


def extract_urls(result: dict) -> list[str]:
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


def download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        f.write(r.read())


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

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

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("环境变量 DASHSCOPE_API_KEY 未设置", file=sys.stderr)
        return 1

    images: list[str] = []
    for key in INPUT_ORDER:
        p = inputs.get(key)
        if not p:
            continue
        if not os.path.isfile(p):
            print(f"输入图片不存在: {p}", file=sys.stderr)
            return 1
        images.append(p)

    if images:
        log(f"[输入] 共 {len(images)} 张参考图：" + ", ".join(os.path.basename(x) for x in images))
    else:
        log("[输入] 无参考图 → 文生图")

    log(f"[模型] {params.get('model') or 'qwen-image-2.1-turbo'}")
    log(f"[提示词] {prompt}")
    if params.get("transparent"):
        log("[提示词] 已追加透明背景要求")
    log("")

    t_all = time.perf_counter()
    try:
        body = build_body(params, images)
        result = call_api(api_key, body)
        urls = extract_urls(result)
    except Exception as e:  # noqa: BLE001
        print(str(e), file=sys.stderr)
        return 1

    log(f"[结果] 收到 {len(urls)} 张图片，开始下载…")
    os.makedirs(output_dir, exist_ok=True)
    for i, url in enumerate(urls):
        dest = os.path.join(output_dir, f"{i}.png")
        try:
            download(url, dest)
        except Exception as e:  # noqa: BLE001
            print(f"下载第 {i + 1} 张失败: {e}", file=sys.stderr)
            return 1
        log(f"[结果] {i}.png  ({os.path.getsize(dest) / 1024:.0f} KB)")

    log("")
    log(f"[计时] 总耗时: {time.perf_counter() - t_all:.2f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
