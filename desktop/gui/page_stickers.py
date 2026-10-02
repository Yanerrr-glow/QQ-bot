"""表情包页：可调列数的缩略图网格 + 选中项的元数据 + 删除。

两个技术要点：

1. **缩略图必须经认证会话取字节**（`ApiClient.sticker_bytes`），不能用
   `QNetworkAccessManager` 直接拿 URL —— 服务端配了 token 时那样会拿到 401，
   而图片控件又不方便带自定义请求头。所以这里在后台线程里取字节，
   再用 `QImage.fromData` 在内存里构造图像。
2. **取图不能卡界面**：每个瓦片一个后台任务，一次最多 8 个并发（QThreadPool 默认），
   取不回来的瓦片显示占位文字而不是白框。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.util import one_line
from .base import Page, StateSlice
from .widgets import PALETTE, badge, esc, label

THUMB_SIZE = 96


class StickerTile(QtWidgets.QFrame):
    """一个表情包瓦片。点击即选中，右键可删。"""

    clicked = QtCore.Signal(str)

    def __init__(self, item: dict[str, Any], parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.digest = str(item.get("hash") or "")
        self.item = item
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setToolTip(_tooltip(item))
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        self.image = QtWidgets.QLabel("…")
        self.image.setFixedSize(THUMB_SIZE, THUMB_SIZE)
        self.image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.image.setStyleSheet(f"background:{PALETTE['panel2']};border-radius:4px;color:{PALETTE['dim']}")
        layout.addWidget(self.image, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

        score = item.get("score")
        uses = item.get("uses")
        caption = f"{_fmt(score)} · {int(uses or 0)}次"
        text = QtWidgets.QLabel(caption)
        text.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        text.setStyleSheet(f"color:{PALETTE['dim']}")
        layout.addWidget(text)

    def set_image(self, data: bytes) -> None:
        image = QtGui.QImage.fromData(data)
        if image.isNull():
            self.image.setText("坏图")
            return
        pixmap = QtGui.QPixmap.fromImage(image).scaled(
            THUMB_SIZE, THUMB_SIZE,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self.image.setPixmap(pixmap)

    def set_failed(self, reason: str) -> None:
        self.image.setText("取图失败")
        self.image.setToolTip(reason)
        self.image.setStyleSheet(
            f"background:{PALETTE['panel2']};border-radius:4px;color:{PALETTE['err']}"
        )

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit(self.digest)
        super().mousePressEvent(event)


class StickersPage(Page):
    title = "表情包库"
    subtitle = "缩略图网格、元数据与删除"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._items: list[dict[str, Any]] = []
        self._tiles: dict[str, StickerTile] = {}
        self._selected = ""
        self._pending: set[str] = set()
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("刷新")
        self.btn_refresh.clicked.connect(lambda: self.load(force=True))
        self.btn_load_thumbs = QtWidgets.QPushButton("加载缩略图")
        self.btn_load_thumbs.setToolTip("经认证会话逐张取图（服务端慢时不会卡界面）")
        self.btn_load_thumbs.clicked.connect(self._load_thumbnails)
        self.btn_delete = QtWidgets.QPushButton("删除选中")
        self.btn_delete.clicked.connect(self._delete)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_load_thumbs)
        bar.addWidget(self.btn_delete)
        bar.addWidget(QtWidgets.QLabel("列数："))
        self.columns = QtWidgets.QSpinBox()
        self.columns.setRange(2, 12)
        self.columns.setValue(6)
        self.columns.valueChanged.connect(lambda _value: self._relayout())
        bar.addWidget(self.columns)
        bar.addStretch(1)
        outer.addLayout(bar)

        self.hint = label("正在读取表情包库…")
        outer.addWidget(self.hint)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.grid_host = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(self.grid_host)
        self.grid.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.grid_host)
        splitter.addWidget(area)

        detail = QtWidgets.QWidget()
        detail_layout = QtWidgets.QVBoxLayout(detail)
        detail_layout.addWidget(QtWidgets.QLabel("选中项"))
        self.preview = QtWidgets.QLabel("（未选中）")
        self.preview.setFixedHeight(180)
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet(f"background:{PALETTE['panel']};border-radius:6px")
        detail_layout.addWidget(self.preview)
        self.detail = QtWidgets.QPlainTextEdit()
        self.detail.setReadOnly(True)
        detail_layout.addWidget(self.detail, 1)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        outer.addWidget(splitter, 1)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(
            lambda: StateSlice(self.client().state()),
            on_done=self._apply,
            busy_text="正在读取表情包库…",
        )

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.hint.setText(badge("读取失败", level="err"))
            return
        state: StateSlice = result.value
        self._items = state.stickers
        stats = dict(state.status.get("stickers") or {})
        self._rebuild_grid()
        self.hint.setText(
            f"{esc(int(stats.get('count') or 0))} 张 · 占用 {esc(_bytes(stats.get('total_bytes')))} · "
            f"被用过 {esc(int(stats.get('used') or 0))} 次 · 疑似重复组 {esc(int(stats.get('duplicates') or 0))} · "
            f"以文件发来的 {esc(int(stats.get('file_sent') or 0))} 张"
        )
        self.toast(f"已载入 {len(self._items)} 张表情包（缩略图按需加载）", level="ok")
        self.refreshed.emit()

    def _rebuild_grid(self) -> None:
        while self.grid.count():
            entry = self.grid.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._tiles.clear()
        for item in self._items:
            tile = StickerTile(item)
            tile.clicked.connect(self._select)
            self._tiles[tile.digest] = tile
        self._relayout()

    def _relayout(self) -> None:
        while self.grid.count():
            self.grid.takeAt(0)
        columns = max(1, self.columns.value())
        for index, digest in enumerate(self._tiles):
            tile = self._tiles[digest]
            tile.setParent(self.grid_host)
            self.grid.addWidget(tile, index // columns, index % columns)
        self.grid_host.update()

    def _select(self, digest: str) -> None:
        self._selected = digest
        item = next((x for x in self._items if str(x.get("hash")) == digest), None)
        if item is None:
            return
        self.detail.setPlainText(_detail_text(item))
        self.preview.setText("（点「加载缩略图」后显示）")
        self._fetch_one(digest, preview=True)

    # ------------------------------------------------------------ 取图
    def _load_thumbnails(self) -> None:
        pending = [d for d in self._tiles if d not in self._pending]
        if not pending:
            self.toast("缩略图都取过了", level="info")
            return
        self.toast(f"开始加载 {len(pending)} 张缩略图（后台进行，不影响操作）", level="info")
        for digest in pending:
            self._fetch_one(digest)

    def _fetch_one(self, digest: str, *, preview: bool = False) -> None:
        if digest in self._pending:
            return
        self._pending.add(digest)

        def _done(result) -> None:  # noqa: ANN001
            self._pending.discard(digest)
            tile = self._tiles.get(digest)
            if not result.ok:
                if tile is not None:
                    tile.set_failed(one_line(result.error, 60))
                return
            data: bytes = result.value
            if tile is not None:
                tile.set_image(data)
            if preview and digest == self._selected:
                image = QtGui.QImage.fromData(data)
                if not image.isNull():
                    self.preview.setPixmap(
                        QtGui.QPixmap.fromImage(image).scaled(
                            260, 180,
                            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                            QtCore.Qt.TransformationMode.SmoothTransformation,
                        )
                    )

        # 取图失败**不弹错**：服务端忙的时候几十张图一起 404/超时会刷屏，
        # 瓦片上标一下就够了。
        self.async_.run(lambda: self.client().sticker_bytes(digest), on_done=_done)

    # ------------------------------------------------------------ 动作
    def _delete(self) -> None:
        if not self._selected:
            self.toast("先在网格里点一张图", level="warn")
            return
        if not QtWidgets.QMessageBox.question(
            self, "删除表情包",
            f"删除表情包 {self._selected}？\n\n文件会从服务端删除，此操作不可撤销。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        digest = self._selected
        self.run_task(
            lambda: self.client().sticker_delete(digest),
            on_done=lambda r: self._after_delete(r, digest),
            busy_text="正在删除表情包…",
        )

    def _after_delete(self, result, digest: str) -> None:  # noqa: ANN001
        if not result.ok:
            return
        payload = dict(result.value or {})
        if payload.get("ok"):
            self._selected = ""
            self.preview.setText("（未选中）")
            self.detail.clear()
            self.toast("已删除", level="ok")
            self.load(force=True)
        else:
            self.toast("删除未成功（服务端返回 ok=false）", level="warn")


def _tooltip(item: dict[str, Any]) -> str:
    return (
        f"hash: {item.get('hash')}\n"
        f"喜好分: {_fmt(item.get('score'))}　使用次数: {item.get('uses')}\n"
        f"尺寸: {item.get('width')}×{item.get('height')}　大小: {_bytes(item.get('size'))}\n"
        f"来源: {item.get('from_conv') or '-'} / {item.get('from_name') or '-'}\n"
        f"类型: {item.get('sub_type_label') or '-'}　加入: {item.get('added_at') or '-'}"
    )


def _detail_text(item: dict[str, Any]) -> str:
    lines = [f"{key}: {value}" for key, value in sorted(item.items()) if not isinstance(value, (dict, list))]
    for key, value in item.items():
        if isinstance(value, (dict, list)):
            lines.append(f"{key}: {one_line(value, 200)}")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _bytes(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
