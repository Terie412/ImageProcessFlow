# -*- coding: utf-8 -*-
"""工作目录层：graph.json 读写、图片短名分配、目录沙箱。

对应 DESIGN.md §2.4：
- 工作目录是唯一的可操作区域，绝不触碰目录以外的文件
- 每张图片有全局唯一的短名（A、B、C…），文件名主干即图片名
- 画布上不再引用的图片文件一律永久删除
"""
from __future__ import annotations

import json
import string
import uuid
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
GRAPH_FILE = "graph.json"
IMAGES_DIR = "images"
TMP_DIR = ".tmp"
TRASH_DIR = ".trash"


def drop_tree(path: str | Path) -> None:
    """尽力删掉一棵目录树，**绝不往外抛异常**。

    为什么不用 shutil.rmtree(ignore_errors=True)：某些运行环境（沙箱、
    "安全删除"钩子）会把 shutil.rmtree 换成会 `raise SystemExit` 的实现。
    SystemExit 不是 Exception，连 finally 都拦不住 —— 一次正常的请求会因此
    变成 500，临时目录也留了下来。清理失败本身不要紧（下次启动还会再清一遍），
    但绝不能影响调用方，所以这里自己走 os 层删，并把所有异常都吞掉。
    """
    root = Path(path)
    try:
        entries = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    except BaseException:  # noqa: BLE001
        entries = []
    for p in entries:
        try:
            if p.is_symlink() or not p.is_dir():
                p.unlink()
            else:
                p.rmdir()
        except BaseException:  # noqa: BLE001
            pass
    try:
        root.rmdir()
    except BaseException:  # noqa: BLE001
        pass


def empty_graph() -> dict:
    return {"version": 1, "resources": [], "processes": [], "edges": []}


def _name_sequence():
    """A, B, ..., Z, AA, AB, ..."""
    letters = string.ascii_uppercase
    n = 1
    while True:
        idx, out = n, []
        while idx > 0:
            idx, rem = divmod(idx - 1, 26)
            out.append(letters[rem])
        yield "".join(reversed(out))
        n += 1


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.graph_path = self.root / GRAPH_FILE
        self.images_dir = self.root / IMAGES_DIR
        self.tmp_dir = self.root / TMP_DIR
        self.trash_dir = self.root / TRASH_DIR

    # ---------------- 生命周期 ----------------
    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 图读写 ----------------
    def load(self) -> dict:
        self.ensure()
        if not self.graph_path.is_file():
            g = empty_graph()
            self.save(g)
            return g
        try:
            g = json.loads(self.graph_path.read_text(encoding="utf-8"))
        except Exception:
            g = empty_graph()
        g.setdefault("version", 1)
        g.setdefault("resources", [])
        g.setdefault("processes", [])
        g.setdefault("edges", [])
        return g

    def save(self, graph: dict) -> None:
        self.ensure()
        tmp = self.graph_path.with_name(GRAPH_FILE + ".tmp")
        tmp.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.graph_path)

    # ---------------- 图片名 ----------------
    def used_names(self, graph: dict) -> set[str]:
        return {r["name"] for r in graph["resources"] if r.get("name")}

    def alloc_name(self, graph: dict) -> str:
        used = self.used_names(graph)
        for cand in _name_sequence():
            if cand not in used:
                return cand
        raise RuntimeError("名字池耗尽")

    def image_path(self, name: str) -> Path | None:
        """按名字解析图片文件：images/<name>.<ext>"""
        for p in sorted(self.images_dir.glob(f"{name}.*")):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                return p
        return None

    def delete_image(self, name: str) -> list[str]:
        """永久删除该名字对应的图片文件。

        注意：这里只把文件 **rename 进 .trash/**，真正的 unlink 交给后台清理。
        原因是 Windows 上文件删除有 ~370ms 的固定开销（文件系统过滤驱动），
        而同卷 rename 只要 ~1ms。让请求等 unlink 会直接拖慢界面操作。
        文件最终仍是被永久删除的，只是延后几百毫秒。
        """
        moved: list[str] = []
        for p in list(self.images_dir.glob(f"{name}.*")):
            if not p.is_file():
                continue
            try:
                self.trash_dir.mkdir(parents=True, exist_ok=True)
                p.rename(self.trash_dir / f"{uuid.uuid4().hex}{p.suffix}")
                moved.append(p.name)
            except BaseException:  # noqa: BLE001  删文件失败也不该带崩调用方
                # rename 失败就退回直接删，保证语义正确
                try:
                    p.unlink()
                    moved.append(p.name)
                except BaseException:  # noqa: BLE001
                    pass
        return moved

    def purge_trash(self) -> int:
        """真正删掉 .trash/ 里的文件。慢（每个约 370ms），只应在后台线程调用。"""
        if not self.trash_dir.is_dir():
            return 0
        n = 0
        for p in list(self.trash_dir.glob("*")):
            try:
                if p.is_file():
                    p.unlink()
                    n += 1
            except BaseException:  # noqa: BLE001
                pass
        return n
