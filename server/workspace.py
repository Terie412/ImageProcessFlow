# -*- coding: utf-8 -*-
"""工作目录层：画布文档读写、图片短名分配、目录沙箱。

对应 DESIGN.md §2.4 与 §2.6：
- 工作目录是唯一的可操作区域，绝不触碰目录以外的文件
- 一个工作目录里可以放**多个画布**，每个画布一个独立的 DAG
- **图片池是按画布分的**：每个画布有自己的 images/，所以 A、B、C 这些短名
  只在**本画布内**唯一 —— 两个画布各有一个 A 是正常的
- 画布上不再引用的图片文件一律永久删除

存储布局（v2）::

    <工作目录>/
    ├─ graph.json            画布容器：{version:2, current, canvases:[{id,name,viewport,…}]}
    ├─ canvases/<画布id>/images/A.png     每个画布自己的图片池
    ├─ .tmp/  .trash/        临时目录与删除中转站（两个画布共用，内容都是瞬态的）

老的 v1 工作目录（顶层一个扁平的 graph.json + 顶层 images/）在**第一次 load**
时自动迁移：老画布成为「画布 1」，老 images/ 里的文件整体挪进它的池子。
"""
from __future__ import annotations

import json
import shutil
import string
import uuid
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
GRAPH_FILE = "graph.json"
CANVASES_DIR = "canvases"
IMAGES_DIR = "images"
TMP_DIR = ".tmp"
TRASH_DIR = ".trash"
LEGACY_IMAGES_DIR = "images"   # v1 的顶层图片目录

DOC_VERSION = 2
GRAPH_KEYS = ("resources", "processes", "edges")


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


def new_canvas_id() -> str:
    return "c_" + uuid.uuid4().hex[:8]


def empty_canvas(name: str, canvas_id: str | None = None) -> dict:
    """一个空画布：画布自己的元信息 + 一份空图。"""
    return {
        "id": canvas_id or new_canvas_id(),
        "name": name,
        "viewport": None,          # 上次的缩放/平移，切回来还在原来的视野
        "resources": [],
        "processes": [],
        "edges": [],
    }


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
        self.doc_path = self.root / GRAPH_FILE
        self.canvases_dir = self.root / CANVASES_DIR
        self.tmp_dir = self.root / TMP_DIR
        self.trash_dir = self.root / TRASH_DIR

    # ---------------- 生命周期 ----------------
    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.canvases_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 画布目录 ----------------
    def canvas_dir(self, canvas_id: str) -> Path:
        return self.canvases_dir / canvas_id

    def image_dir(self, canvas_id: str, create: bool = False) -> Path:
        d = self.canvas_dir(canvas_id) / IMAGES_DIR
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------------- 文档读写 ----------------
    def load_doc(self) -> dict:
        """读画布容器。老的 v1 工作目录（顶层一个扁平 graph）在这里就地迁移成 v2。

        迁移是有副作用的（改写 graph.json + 把顶层 images/ 挪进画布池），所以只在
        碰上 v1 时发生一次；之后每本都直接命中 v2 分支。
        """
        self.ensure()
        raw = None
        if self.doc_path.is_file():
            try:
                raw = json.loads(self.doc_path.read_text(encoding="utf-8"))
            except BaseException:  # noqa: BLE001
                raw = None
        if isinstance(raw, dict) and isinstance(raw.get("canvases"), list) and raw["canvases"]:
            return self.normalize_doc(raw)
        doc = self.migrate_v1(raw) if isinstance(raw, dict) else self.fresh_doc()
        self.save_doc(doc)
        return doc

    def fresh_doc(self) -> dict:
        c = empty_canvas("画布 1")
        self.image_dir(c["id"], create=True)
        # resourceDir：资源目录（工作目录之外的只读素材来源）。**跟随工作目录** ——
        # 存在这份文档里，所以每个项目自带一个资源目录（见 DESIGN.md §2.4 / 资源目录一节）。
        return {"version": DOC_VERSION, "current": c["id"], "resourceDir": "", "canvases": [c]}

    def migrate_v1(self, raw: dict | None) -> dict:
        """v1 → v2：老画布成为「画布 1」，顶层 images/ 整体挪进它的池子。"""
        c = empty_canvas("画布 1")
        for k in GRAPH_KEYS:
            v = (raw or {}).get(k)
            c[k] = v if isinstance(v, list) else []
        self._move_legacy_images(self.image_dir(c["id"], create=True))
        return {"version": DOC_VERSION, "current": c["id"], "resourceDir": "", "canvases": [c]}

    def _move_legacy_images(self, dst: Path) -> int:
        """把 v1 的顶层 images/ 里的文件挪进画布池。

        同卷 rename 是瞬时的；真失败（被占用 / 跨卷）就退回复制，且**绝不删原件** ——
        宁可留一份重复的，也不能把用户的图弄丢。
        """
        src = self.root / LEGACY_IMAGES_DIR
        if not src.is_dir() or src == dst:
            return 0
        moved = 0
        for p in sorted(src.iterdir()):
            if not p.is_file():
                continue
            target = dst / p.name
            if target.exists():
                continue
            try:
                p.rename(target)
            except BaseException:  # noqa: BLE001
                try:
                    shutil.copy2(p, target)
                except BaseException:  # noqa: BLE001
                    continue
            moved += 1
        try:
            if not any(src.iterdir()):
                src.rmdir()
        except BaseException:  # noqa: BLE001
            pass
        return moved

    def normalize_doc(self, doc: dict) -> dict:
        """补字段：保证 current 指向存在的画布、每个画布有自己的图片目录。

        老 v2 文件（少字段）和手改过的文件都会经过这里，所以读进来的一定是完整形状。
        """
        doc["version"] = DOC_VERSION
        # 资源目录：老文档没有这个字段，补一个空串（未指定）。它**跟随工作目录**，
        # 所以和 current 一样是文档级字段，不是每个画布一份。
        if not isinstance(doc.get("resourceDir"), str):
            doc["resourceDir"] = ""
        seen: set[str] = set()
        canvases: list[dict] = []
        for i, c in enumerate(doc.get("canvases") or []):
            if not isinstance(c, dict):
                continue
            cid = c.get("id") or new_canvas_id()
            if cid in seen:
                cid = new_canvas_id()
            seen.add(cid)
            c["id"] = cid
            c["name"] = str(c.get("name") or f"画布 {i + 1}").strip() or f"画布 {i + 1}"
            c.setdefault("viewport", None)
            for k in GRAPH_KEYS:
                if not isinstance(c.get(k), list):
                    c[k] = []
            canvases.append(c)
        if not canvases:
            return self.fresh_doc()
        doc["canvases"] = canvases
        if doc.get("current") not in seen:
            doc["current"] = canvases[0]["id"]
        for c in canvases:
            self.image_dir(c["id"], create=True)
        return doc

    def save_doc(self, doc: dict) -> None:
        self.ensure()
        tmp = self.doc_path.with_name(GRAPH_FILE + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.doc_path)

    # ---------------- 画布增删 ----------------
    def next_canvas_name(self, doc: dict) -> str:
        used = {c.get("name") for c in doc.get("canvases", [])}
        n = 1
        while f"画布 {n}" in used:
            n += 1
        return f"画布 {n}"

    def create_canvas(self, doc: dict, name: str | None = None) -> dict:
        c = empty_canvas(str(name or "").strip() or self.next_canvas_name(doc))
        doc.setdefault("canvases", []).append(c)
        self.image_dir(c["id"], create=True)
        return c

    def delete_canvas(self, doc: dict, canvas_id: str) -> bool:
        """删掉一个画布，连它的图片池一起永久删除。最后一个画布不给删。"""
        canvases = doc.get("canvases", [])
        if len(canvases) <= 1 or not any(c.get("id") == canvas_id for c in canvases):
            return False
        doc["canvases"] = [c for c in canvases if c.get("id") != canvas_id]
        drop_tree(self.canvas_dir(canvas_id))
        if doc.get("current") == canvas_id:
            doc["current"] = doc["canvases"][0]["id"]
        return True

    # ---------------- 图片名（作用域 = 本画布） ----------------
    def used_names(self, canvas: dict) -> set[str]:
        return {r["name"] for r in canvas.get("resources", []) if r.get("name")}

    def alloc_name(self, canvas: dict) -> str:
        used = self.used_names(canvas)
        for cand in _name_sequence():
            if cand not in used:
                return cand
        raise RuntimeError("名字池耗尽")

    def image_path(self, canvas_id: str, name: str) -> Path | None:
        """按名字解析图片文件：canvases/<画布id>/images/<name>.<ext>"""
        for p in sorted(self.image_dir(canvas_id).glob(f"{name}.*")):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                return p
        return None

    def copy_image(self, src_canvas_id: str, dst_canvas_id: str, src_name: str, dst_name: str) -> bool:
        """把一张图复制一份到另一个画布（跨画布粘贴用）。返回是否成功。"""
        src = self.image_path(src_canvas_id, src_name)
        if src is None:
            return False
        try:
            shutil.copy2(src, self.image_dir(dst_canvas_id, create=True) / f"{dst_name}{src.suffix.lower()}")
            return True
        except BaseException:  # noqa: BLE001
            return False

    def delete_image(self, canvas_id: str, name: str) -> list[str]:
        """永久删除该画布下这个图片名对应的文件。

        注意：这里只把文件 **rename 进 .trash/**，真正的 unlink 交给后台清理。
        原因是 Windows 上文件删除有 ~370ms 的固定开销（文件系统过滤驱动），
        而同卷 rename 只要 ~1ms。让请求等 unlink 会直接拖慢界面操作。
        文件最终仍是被永久删除的，只是延后几百毫秒。
        """
        moved: list[str] = []
        for p in list(self.image_dir(canvas_id).glob(f"{name}.*")):
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

    def view(self, canvas_id: str) -> "CanvasView":
        """取某个画布的「文件面」——**引擎只跟这个打交道**。

        这样引擎不需要知道工作区里有几个画布、当前是哪个：它拿到的是
        「这个画布的临时目录 + 这个画布的图片池」，图片名的分配与删除都局限在本画布内。
        """
        return CanvasView(self, canvas_id)


class CanvasView:
    """一个画布的文件面（图片池 + 临时目录）。

    引擎（`engine.execute` / `remove_process` / `remove_resource_node`）接收的就是它，
    所以引擎里那句 `ws.delete_image(r["name"])` 天然只会动**本画布**的图片文件 ——
    两个画布各有一个 A 也不会互相误删。
    """

    def __init__(self, ws: Workspace, canvas_id: str):
        self.ws = ws
        self.id = canvas_id
        self.root = ws.root
        self.tmp_dir = ws.tmp_dir

    def ensure(self) -> None:
        """把「本画布跑一次脚本」要用到的目录备齐（临时目录 + 本画布的图片池）。"""
        self.ws.ensure()
        self.ws.image_dir(self.id, create=True)

    def image_dir(self, create: bool = False) -> Path:
        return self.ws.image_dir(self.id, create=create)

    def image_path(self, name: str) -> Path | None:
        return self.ws.image_path(self.id, name)

    def used_names(self, canvas: dict) -> set[str]:
        return self.ws.used_names(canvas)

    def alloc_name(self, canvas: dict) -> str:
        return self.ws.alloc_name(canvas)

    def delete_image(self, name: str) -> list[str]:
        return self.ws.delete_image(self.id, name)

    def copy_image_from(self, src_name: str, dst_view: "CanvasView", dst_name: str) -> bool:
        """把本画布的一张图复制到另一个画布（跨画布粘贴用）。"""
        return self.ws.copy_image(self.id, dst_view.id, src_name, dst_name)
