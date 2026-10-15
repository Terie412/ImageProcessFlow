# -*- coding: utf-8 -*-
"""处理节点的**实时预览**：把脚本当模块加载进来，直接调它的纯函数画一张图。

对应 DESIGN.md §3.17。给「程序化描边圆角框」这类**按参数凭空造图**的节点用：
调一次参数就想看见结果，而不是「执行 → 长出一张输出图 → 再回头调」。

## 为什么是 import，不是起子进程

执行一个节点走的是脚本契约（§2.5）：`python <entry>` + stdin/stdout + 输出目录。
预览**故意不走那条路** —— 起一个解释器再把 Pillow 导进来，每次 200ms 上下，
而缩略图是跟着控件走的（前端 150ms 防抖），这笔开销付不起；预览也不该往图片池里
塞文件、不该占输出槽位。

所以这里直接 `import` 脚本模块调纯函数。**脚本侧要守一条约定**：

    模块里提供一个 `render(params: dict) -> PIL.Image.Image` 这样的纯函数，
    import 时不能有副作用 —— 读 stdin、写文件、打印都必须留在
    `if __name__ == "__main__":` 后面。

好处是**预览与执行是同一份绘制代码**，改脚本两边一起变，不会长出「预览长得对、
产出不对」。代价是这条约定靠自觉 —— 脚本作者要不是这么写的，坏的是他自己那个节点的预览。

## 缓存

按 `(路径, mtime)` 缓存模块对象：改完脚本**重启服务**照旧（脚本注册表本来就是启动时
扫一次），但即使不重启，mtime 一变也会重新加载，不会拿着旧模块当新代码用。
"""
from __future__ import annotations

import importlib.util
import io
import threading
from pathlib import Path

_LOCK = threading.Lock()
# path -> (mtime, module)；一条路径只留最新一份
_CACHE: dict[str, tuple[float, object]] = {}


def load_module(entry: str | Path):
    """按路径加载脚本模块（带 mtime 缓存）。加载失败抛 RuntimeError。"""
    path = Path(entry)
    try:
        stamp = path.stat().st_mtime
    except OSError as e:
        raise RuntimeError(f"预览脚本不存在或读不到：{path}") from e

    key = str(path)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]

    # 模块名带目录名，只为了报错信息好看；**不塞进 sys.modules** ——
    # 预览是「看一眼就走」的，没必要让脚本长期占着全局命名空间
    spec = importlib.util.spec_from_file_location(f"_wb_preview_{path.parent.name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"加载不了预览脚本：{path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"预览脚本导入失败：{type(e).__name__}: {e}") from e

    with _LOCK:
        _CACHE[key] = (stamp, mod)
    return mod


def render_png(entry: str | Path, func: str, params: dict) -> bytes:
    """调脚本的纯函数画一张 PNG，返回字节。出错抛 RuntimeError（消息直接给用户看）。"""
    callable_name = func or "render"
    fn = getattr(load_module(entry), callable_name, None)
    if not callable(fn):
        raise RuntimeError(f"这个脚本没有提供预览函数 {callable_name}()（见 DESIGN.md §3.17）")
    try:
        img = fn(params or {})
    except Exception as e:  # noqa: BLE001
        # ValueError 之类是脚本自己给用户的说明（颜色不合法…），原样透出去更有用
        raise RuntimeError(str(e) if isinstance(e, ValueError) and str(e) else
                           f"预览失败：{type(e).__name__}: {e}") from e

    buf = io.BytesIO()
    try:
        img.save(buf, "PNG", optimize=True)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"预览结果存不成 PNG：{type(e).__name__}: {e}") from e
    return buf.getvalue()
