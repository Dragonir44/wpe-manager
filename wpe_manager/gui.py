"""Qt (PySide6) GUI: a fast, lazily-thumbnailed grid of wallpapers, a playlist
panel (checkbox selection + WPE import), and per-screen assignment."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QRunnable,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import config, engine, library, steam, theme
from .library import AGE_LABELS, Wallpaper
from .rotation import RotationController

THUMB = QSize(240, 135)  # 16:9 thumbnails
WALLPAPER_ROLE = Qt.ItemDataRole.UserRole + 1
RESOLUTION_ROLE = Qt.ItemDataRole.UserRole + 2  # compact "W×H" for the card badge
_TYPE_LABELS = {"scene": "Scène", "video": "Vidéo", "web": "Web",
                "application": "Application"}


class FlowLayout(QLayout):
    """A layout that lays widgets out left-to-right and wraps to a new line when
    it runs out of width. Used for the toolbars so the central area can shrink
    (a single QHBoxLayout row would pin a huge minimum width and block the
    splitter from resizing the side panels)."""

    def __init__(self, parent=None, margin=0, hspacing=6, vspacing=6):
        super().__init__(parent)
        self._items: list = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only: bool) -> int:
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right = rect.right() - m.right()
        line_h = 0
        for it in self._items:
            hint = it.sizeHint()
            w, h = hint.width(), hint.height()
            if x + w - 1 > right and line_h > 0:
                x = rect.x() + m.left()
                y += line_h + self._vspace
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += w + self._hspace
            line_h = max(line_h, h)
        return y + line_h + m.bottom() - rect.y()


def app_icon() -> QIcon:
    """The app/tray icon: the KDE wallpaper theme icon, or a drawn fallback."""
    themed = QIcon.fromTheme("preferences-desktop-wallpaper")
    if not themed.isNull():
        return themed
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(72, 118, 214))
    p.drawRoundedRect(6, 6, 52, 52, 12, 12)
    p.setBrush(QColor(245, 210, 90))
    p.drawEllipse(16, 14, 16, 16)  # a little "sun"
    p.setBrush(QColor(255, 255, 255, 210))
    p.drawEllipse(30, 34, 22, 22)  # a "moon/cloud"
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- #
# Thumbnail loading (background threads → never blocks the UI)
# --------------------------------------------------------------------------- #
class _ThumbSignals(QObject):
    done = Signal(int, QImage)


class _ThumbTask(QRunnable):
    def __init__(self, row: int, path: Path, signals: _ThumbSignals):
        super().__init__()
        self._row = row
        self._path = path
        self._signals = signals

    def run(self) -> None:
        image = QImage(str(self._path))
        if image.isNull():
            self._signals.done.emit(self._row, QImage())
            return
        scaled = image.scaled(
            THUMB,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._signals.done.emit(self._row, scaled)


def _placeholder() -> QPixmap:
    pm = QPixmap(THUMB)
    pm.fill(QColor(52, 54, 60))
    return pm


class WallpaperModel(QAbstractListModel):
    checked_changed = Signal()  # emitted whenever the set of checked ids changes

    def __init__(self, wallpapers: list[Wallpaper], metadata: dict[str, dict] | None = None):
        super().__init__()
        self._items = wallpapers
        self._by_id = {w.id: i for i, w in enumerate(wallpapers)}
        self._meta = metadata or {}
        self._pixmaps: dict[int, QPixmap] = {}
        self._pending: set[int] = set()
        self._checked: list[str] = []  # wallpaper ids, selection order preserved
        self._pool = QThreadPool.globalInstance()
        self._signals = _ThumbSignals()
        self._signals.done.connect(self._on_thumb)
        self._fallback = _placeholder()

    # Steam metadata (resolution) ------------------------------------------ #
    def meta_of(self, wid: str) -> dict:
        return self._meta.get(wid, {})

    def resolutions_of(self, wid: str) -> list[str]:
        return self._meta.get(wid, {}).get("resolutions", [])

    def resolution_label(self, wid: str) -> str:
        return ", ".join(self.resolutions_of(wid))

    def set_metadata(self, metadata: dict[str, dict]) -> None:
        self._meta = metadata
        if self._items:  # refresh tooltips
            self.dataChanged.emit(
                self.index(0), self.index(len(self._items) - 1),
                [Qt.ItemDataRole.ToolTipRole],
            )

    # Qt model API ---------------------------------------------------------- #
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if index.isValid():
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        wp = self._items[row]
        if role == Qt.ItemDataRole.DisplayRole:
            return wp.title
        if role == Qt.ItemDataRole.ToolTipRole:
            res = self.resolution_label(wp.id)
            size = f"{wp.size_bytes / 1e6:.0f} Mo" if wp.size_bytes else "?"
            lines = [wp.title, f"Type : {wp.type}", f"Taille : {size}"]
            if res:
                lines.append(f"Résolution : {res}")
            if wp.tags:
                lines.append(f"Tags : {', '.join(wp.tags)}")
            lines.append(f"ID : {wp.id}")
            return "\n".join(lines)
        if role == WALLPAPER_ROLE:
            return wp
        if role == RESOLUTION_ROLE:
            res = self.resolutions_of(wp.id)
            return res[0].replace(" ", "").replace("x", "×") if res else ""
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if wp.id in self._checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DecorationRole:
            if row in self._pixmaps:
                return self._pixmaps[row]
            self._request_thumb(row, wp)
            return self._fallback
        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if role == Qt.ItemDataRole.CheckStateRole and index.isValid():
            wid = self._items[index.row()].id
            checked = Qt.CheckState(value) == Qt.CheckState.Checked
            if checked and wid not in self._checked:
                self._checked.append(wid)
            elif not checked and wid in self._checked:
                self._checked.remove(wid)
            self.dataChanged.emit(index, index, [role])
            self.checked_changed.emit()
            return True
        return False

    # Check helpers --------------------------------------------------------- #
    def checked_ids(self) -> list[str]:
        return list(self._checked)

    def set_checked(self, ids: list[str]) -> None:
        self._checked = [i for i in ids if i in self._by_id]
        if self._items:
            self.dataChanged.emit(
                self.index(0), self.index(len(self._items) - 1),
                [Qt.ItemDataRole.CheckStateRole],
            )
        self.checked_changed.emit()

    def clear_checks(self) -> None:
        self.set_checked([])

    # Lazy thumbnail plumbing ---------------------------------------------- #
    def _request_thumb(self, row: int, wp: Wallpaper) -> None:
        if row in self._pending:
            return
        if not wp.has_preview:
            self._pixmaps[row] = self._fallback
            return
        self._pending.add(row)
        self._pool.start(_ThumbTask(row, wp.preview, self._signals))

    def _on_thumb(self, row: int, image: QImage) -> None:
        self._pending.discard(row)
        self._pixmaps[row] = self._fallback if image.isNull() else QPixmap.fromImage(image)
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


# --------------------------------------------------------------------------- #
# Combined filtering / sorting
# --------------------------------------------------------------------------- #
class WallpaperFilterProxy(QSortFilterProxyModel):
    """Filters the grid by title, genre, type, age and resolution at once.

    Resolution comes from the source model's Steam metadata (family bucket or a
    per-screen aspect-ratio match). Items with no known resolution are hidden
    only while a resolution filter is active — they simply can't match it."""

    _COMPAT_TOL = 0.12  # relative aspect-ratio tolerance for "matches screen"
    NO_RES = "\x00none"  # sentinel filter value: wallpapers with no resolution tag

    def __init__(self):
        super().__init__()
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.text = ""
        self.genres: set[str] = set()
        self.types: set[str] = set()
        self.ages: set[str] = set()
        self.resolutions: set[str] = set()   # exact "W x H" strings (+ NO_RES)
        self.compat_ratio: float | None = None
        self.sort_mode = "title"  # "title" | "size_asc" | "size_desc"

    def filterAcceptsRow(self, source_row: int, parent) -> bool:
        model = self.sourceModel()
        idx = model.index(source_row, 0, parent)
        wp = idx.data(WALLPAPER_ROLE)
        if wp is None:
            return False
        if self.text and self.text.casefold() not in wp.title.casefold():
            return False
        if self.types and wp.type not in self.types:
            return False
        if self.ages and (wp.age or "") not in self.ages:
            return False
        if self.genres and not (self.genres & set(wp.tags)):
            return False
        wres = model.resolutions_of(wp.id)
        if self.resolutions:
            hit = bool(self.resolutions & set(wres))
            if not hit and self.NO_RES in self.resolutions and not wres:
                hit = True
            if not hit:
                return False
        if self.compat_ratio is not None:
            if not any(self._matches_ratio(r) for r in wres):
                return False
        return True

    def _matches_ratio(self, res: str) -> bool:
        w, h = steam.parse_wh(res)
        if not w or not h:
            return False
        return abs(w / h - self.compat_ratio) / self.compat_ratio <= self._COMPAT_TOL

    def lessThan(self, left, right) -> bool:
        lw = left.data(WALLPAPER_ROLE)
        rw = right.data(WALLPAPER_ROLE)
        if lw is None or rw is None:
            return False
        if self.sort_mode == "size_asc":
            return lw.size_bytes < rw.size_bytes
        if self.sort_mode == "size_desc":
            return lw.size_bytes > rw.size_bytes
        return lw.title.casefold() < rw.title.casefold()

    def apply_sort(self) -> None:
        # Title order == the source order (scan() already sorts by title), so a
        # -1 sort column restores it without our lessThan; size uses lessThan.
        self.sort(-1 if self.sort_mode == "title" else 0)


# --------------------------------------------------------------------------- #
# WPE-style card delegate
# --------------------------------------------------------------------------- #
class WallpaperCardDelegate(QStyledItemDelegate):
    """Draws each grid item as a Wallpaper-Engine-style card: a near-square
    thumbnail with the title on a strip below it, sharp corners, and a blue
    border + blue title strip when selected. To stay as clean as WPE at rest,
    the check badge and resolution tag are only shown on hover (the check badge
    also stays visible when the wallpaper is checked)."""

    CARD_W = 168
    IMG_H = 122
    STRIP_H = 24
    _MARGIN = 5
    _RADIUS = 3
    _BADGE = 20              # check badge side (px)

    def sizeHint(self, option, index) -> QSize:
        return QSize(self.CARD_W + self._MARGIN * 2,
                     self.IMG_H + self.STRIP_H + self._MARGIN * 2)

    def _card_rect(self, opt_rect: QRect) -> QRect:
        return QRect(opt_rect.x() + self._MARGIN, opt_rect.y() + self._MARGIN,
                     self.CARD_W, self.IMG_H + self.STRIP_H)

    def _img_rect(self, opt_rect: QRect) -> QRect:
        c = self._card_rect(opt_rect)
        return QRect(c.x(), c.y(), c.width(), self.IMG_H)

    def _badge_rect(self, opt_rect: QRect) -> QRect:
        img = self._img_rect(opt_rect)
        return QRect(img.x() + 6, img.y() + 6, self._BADGE, self._BADGE)

    def paint(self, painter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(self._card_rect(option.rect))
        img = QRectF(self._img_rect(option.rect))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        checked = (index.data(Qt.ItemDataRole.CheckStateRole)
                   == Qt.CheckState.Checked)
        base_font = painter.font()

        # rounded clip; fill so the strip area gets its background
        path = QPainterPath()
        path.addRoundedRect(card, self._RADIUS, self._RADIUS)
        painter.setClipPath(path)
        painter.fillRect(card, QColor(theme.STRIP_BG))

        # thumbnail (cover-scaled, centre-cropped into the image area)
        pm = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pm, QPixmap) and not pm.isNull():
            scaled = pm.scaled(img.size().toSize(),
                               Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
            px = img.x() + (img.width() - scaled.width()) / 2
            py = img.y() + (img.height() - scaled.height()) / 2
            painter.save()
            painter.setClipRect(img)
            painter.drawPixmap(int(px), int(py), scaled)
            painter.restore()

        if hover:
            painter.fillRect(img, QColor(255, 255, 255, 18))

        # title strip below the thumbnail
        strip = QRectF(card.x(), img.bottom(), card.width(), self.STRIP_H)
        painter.fillRect(strip, QColor(theme.ACCENT if selected else theme.STRIP_BG))
        title = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        title_font = QFont(base_font)
        title_font.setPointSizeF(base_font.pointSizeF() * 0.92)
        painter.setFont(title_font)
        painter.setPen(QColor(theme.ON_ACCENT if selected else theme.TEXT))
        tw = int(strip.width()) - 14
        text = QFontMetrics(title_font).elidedText(
            title, Qt.TextElideMode.ElideRight, tw)
        painter.drawText(strip.adjusted(7, 0, -7, 0),
                         Qt.AlignmentFlag.AlignCenter, text)

        # resolution tag (top-right, on hover only)
        res = index.data(RESOLUTION_ROLE)
        if hover and res:
            badge_font = QFont(base_font)
            badge_font.setPointSizeF(max(7.0, base_font.pointSizeF() * 0.78))
            painter.setFont(badge_font)
            rfm = QFontMetrics(badge_font)
            bw = rfm.horizontalAdvance(res) + 12
            br = QRectF(img.right() - bw - 6, img.y() + 6, bw, 17)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawRoundedRect(br, 3, 3)
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(br, Qt.AlignmentFlag.AlignCenter, res)

        # check badge (top-left) — shown on hover, or always when checked
        if hover or checked:
            badge = QRectF(self._badge_rect(option.rect))
            painter.setPen(Qt.PenStyle.NoPen)
            if checked:
                painter.setBrush(QColor(theme.ACCENT))
                painter.drawRoundedRect(badge, 3, 3)
                pen = QPen(QColor(theme.ON_ACCENT))
                pen.setWidthF(2.0)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                x, y, w, h = badge.x(), badge.y(), badge.width(), badge.height()
                painter.drawLine(QPointF(x + w * 0.26, y + h * 0.52),
                                 QPointF(x + w * 0.44, y + h * 0.70))
                painter.drawLine(QPointF(x + w * 0.44, y + h * 0.70),
                                 QPointF(x + w * 0.76, y + h * 0.32))
            else:
                painter.setBrush(QColor(0, 0, 0, 120))
                painter.drawRoundedRect(badge, 3, 3)
                pen = QPen(QColor(255, 255, 255, 180))
                pen.setWidthF(1.0)
                painter.setPen(pen)
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.drawRoundedRect(badge.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)

        painter.setClipping(False)

        # selection border around the whole card
        if selected:
            pen = QPen(QColor(theme.ACCENT))
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawRoundedRect(card.adjusted(1, 1, -1, -1),
                                    self._RADIUS, self._RADIUS)

        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        # Clicking the check badge toggles the wallpaper without disturbing the
        # current selection; clicks elsewhere fall through to the view.
        if (event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self._badge_rect(option.rect).contains(
                    event.position().toPoint())):
            checked = (index.data(Qt.ItemDataRole.CheckStateRole)
                       == Qt.CheckState.Checked)
            model.setData(
                index,
                Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked,
                Qt.ItemDataRole.CheckStateRole)
            return True
        return False


# --------------------------------------------------------------------------- #
# Steam metadata sync (background)
# --------------------------------------------------------------------------- #
class _SyncSignals(QObject):
    progress = Signal(int, int)
    done = Signal(dict)


class _MetaSyncTask(QRunnable):
    def __init__(self, ids: list[str], signals: _SyncSignals):
        super().__init__()
        self._ids = ids
        self._signals = signals

    def _progress(self, done: int, total: int) -> None:
        try:
            self._signals.progress.emit(done, total)
        except RuntimeError:
            pass  # window/signals went away (app quit mid-sync)

    def run(self) -> None:
        try:
            result = steam.fetch_metadata(self._ids, self._progress)
            self._signals.done.emit(result)
        except RuntimeError:
            pass


# --------------------------------------------------------------------------- #
# Per-wallpaper properties editor
# --------------------------------------------------------------------------- #
class PropertyForm(QWidget):
    """A reusable form of a wallpaper's editable properties. Builds a widget per
    property type and exposes only the values that differ from the defaults, so
    the launch command carries the minimum set of --set-property overrides."""

    def __init__(self, props: list[library.Property], overrides: dict,
                 color_parent=None):
        super().__init__()
        self._getters: dict[str, tuple[library.Property, object]] = {}
        self._color_parent = color_parent or self
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        # long labels (e.g. "Browse properties scheme color") otherwise squeeze
        # the value; wrap the field onto its own full-width row when needed.
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for p in props:
            current = overrides.get(p.key, p.default)
            widget, getter = self._make_widget(p, current)
            self._getters[p.key] = (p, getter)
            form.addRow(p.label + " :", widget)

    # -- widget factory ---------------------------------------------------- #
    def _make_widget(self, p: library.Property, current):
        if p.type == "bool":
            cb = QCheckBox()
            cb.setChecked(bool(current))
            return cb, cb.isChecked
        if p.type == "slider":
            spin = QDoubleSpinBox()
            lo = float(p.minimum) if p.minimum is not None else 0.0
            hi = float(p.maximum) if p.maximum is not None else 100.0
            spin.setRange(min(lo, hi), max(lo, hi))
            spin.setDecimals(0 if max(abs(lo), abs(hi)) > 3 else 3)
            spin.setSingleStep((hi - lo) / 100 if hi > lo else 1)
            try:
                spin.setValue(float(current))
            except (TypeError, ValueError):
                pass
            return spin, spin.value
        if p.type == "combo":
            combo = QComboBox()
            for label, value in p.options:
                combo.addItem(label, value)
            idx = combo.findData(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            return combo, combo.currentData
        if p.type == "color":
            return self._color_widget(current)
        # textinput (and any stray text)
        edit = QLineEdit("" if current is None else str(current))
        return edit, edit.text

    def _color_widget(self, current):
        rgb = self._parse_color(current)
        btn = QPushButton()
        state = {"rgb": rgb}

        def refresh():
            r, g, b = state["rgb"]
            btn.setStyleSheet(
                f"background-color: rgb({int(r * 255)},{int(g * 255)},{int(b * 255)});"
                " min-height: 22px; border: 1px solid palette(mid);"
            )
            btn.setText(f"{r:.2f}  {g:.2f}  {b:.2f}")

        def pick():
            r, g, b = state["rgb"]
            chosen = QColorDialog.getColor(
                QColor.fromRgbF(r, g, b), self._color_parent, "Couleur")
            if chosen.isValid():
                state["rgb"] = (chosen.redF(), chosen.greenF(), chosen.blueF())
                refresh()

        btn.clicked.connect(pick)
        refresh()
        return btn, lambda: "{:.6f} {:.6f} {:.6f}".format(*state["rgb"])

    @staticmethod
    def _parse_color(value) -> tuple[float, float, float]:
        try:
            parts = [float(x) for x in str(value).split()][:3]
        except (TypeError, ValueError):
            parts = []
        while len(parts) < 3:
            parts.append(0.0)
        return tuple(parts[:3])  # type: ignore[return-value]

    # -- result ------------------------------------------------------------ #
    def _equal(self, p: library.Property, a, b) -> bool:
        if p.type == "color":
            return self._parse_color(a) == self._parse_color(b)
        if p.type == "slider":
            try:
                return abs(float(a) - float(b)) < 1e-9
            except (TypeError, ValueError):
                return a == b
        if p.type == "bool":
            return bool(a) == bool(b)
        return a == b

    def changed_values(self) -> dict:
        """Only the values that differ from each property's default."""
        out: dict = {}
        for key, (p, getter) in self._getters.items():
            value = getter()
            if not self._equal(p, value, p.default):
                out[key] = value
        return out


# --------------------------------------------------------------------------- #
# Bottom playlist strip
# --------------------------------------------------------------------------- #
class _StripThumb(QLabel):
    """A clickable thumbnail in the playlist strip; clicking removes it."""
    clicked = Signal(str)

    def __init__(self, wid: str, pm: QPixmap):
        super().__init__()
        self._wid = wid
        if isinstance(pm, QPixmap) and not pm.isNull():
            self.setPixmap(pm)
        self.setObjectName("stripThumb")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._wid)


class ChecksStrip(QScrollArea):
    """Horizontal strip of the currently checked wallpapers (the working
    playlist), WPE-style. Click a thumbnail to remove it from the selection."""

    THUMB_H = 58

    def __init__(self):
        super().__init__()
        self._model: WallpaperModel | None = None
        self._thumbs: dict[str, _StripThumb] = {}
        self.setObjectName("checksStrip")
        self.setWidgetResizable(True)
        self.setFixedHeight(self.THUMB_H + 16)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._inner = QWidget()
        self._row = QHBoxLayout(self._inner)
        self._row.setContentsMargins(6, 4, 6, 4)
        self._row.setSpacing(5)
        self._row.addStretch(1)
        self.setWidget(self._inner)

    def set_model(self, model: WallpaperModel) -> None:
        self._model = model
        model.checked_changed.connect(self.refresh)
        model.dataChanged.connect(self._on_data)
        self.refresh()

    def wheelEvent(self, event) -> None:
        # a vertical wheel scrolls the strip horizontally
        bar = self.horizontalScrollBar()
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar.setValue(bar.value() - delta)
        event.accept()

    def _scaled(self, row: int) -> QPixmap:
        pm = self._model.data(self._model.index(row),
                              Qt.ItemDataRole.DecorationRole)
        if isinstance(pm, QPixmap) and not pm.isNull():
            return pm.scaledToHeight(
                self.THUMB_H, Qt.TransformationMode.SmoothTransformation)
        return QPixmap()

    def _on_data(self, tl, br, roles) -> None:
        # a checked thumbnail finished loading: update just that image in place
        # (never rebuild here — a full rebuild per arrival is O(n²) and froze
        # the UI when loading big playlists).
        if self._model is None:
            return
        if roles and Qt.ItemDataRole.DecorationRole not in roles:
            return
        for wid, thumb in self._thumbs.items():
            row = self._model._by_id.get(wid)
            if row is not None and tl.row() <= row <= br.row():
                pm = self._scaled(row)
                if not pm.isNull():
                    thumb.setPixmap(pm)

    def refresh(self) -> None:
        """Rebuild the strip to match the checked set. Called once per selection
        change (not per thumbnail arrival)."""
        if self._model is None:
            return
        self._thumbs.clear()
        while self._row.count() > 1:  # keep the trailing stretch
            item = self._row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        ids = self._model.checked_ids()
        if not ids:
            empty = QLabel("Aucun fond coché — coche des fonds dans la grille "
                           "pour composer une playlist.")
            empty.setObjectName("stripEmpty")
            self._row.insertWidget(0, empty)
            return
        for wid in ids:
            row = self._model._by_id.get(wid)
            if row is None:
                continue
            thumb = _StripThumb(wid, self._scaled(row))
            wp = self._model.data(self._model.index(row), WALLPAPER_ROLE)
            thumb.setToolTip(
                (f"{wp.title}\n" if wp else "") + "Cliquer pour retirer")
            thumb.clicked.connect(self._remove)
            self._thumbs[wid] = thumb
            self._row.insertWidget(self._row.count() - 1, thumb)

    def _remove(self, wid: str) -> None:
        if self._model is not None:
            self._model.set_checked(
                [i for i in self._model.checked_ids() if i != wid])


# --------------------------------------------------------------------------- #
# Visual screen picker
# --------------------------------------------------------------------------- #
class ScreenPicker(QWidget):
    """A compact visual monitor selector: draws the connected screens as
    rectangles laid out by their real geometry (like Wallpaper Engine's screen
    picker); click one to select it. Hover for each screen's resolution and
    current assignment."""

    selected = Signal(str)

    def __init__(self):
        super().__init__()
        self._screens: list[tuple[str, QRect]] = []
        self._assign: dict[str, str] = {}
        self._current: str | None = None
        self._rects: dict[str, QRectF] = {}
        self.setMinimumSize(200, 76)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_screens(self, screens) -> None:
        self._screens = [(s.name(), s.geometry()) for s in screens]
        names = [n for n, _ in self._screens]
        if self._current not in names:
            self._current = names[0] if names else None
        self._update_tooltip()
        self.updateGeometry()
        self.update()

    def set_assignments(self, mapping: dict) -> None:
        self._assign = dict(mapping)
        self._update_tooltip()
        self.update()

    def current(self) -> str | None:
        return self._current

    def set_current(self, name: str) -> None:
        if name != self._current and any(n == name for n, _ in self._screens):
            self._current = name
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(max(200, 66 * max(1, len(self._screens))), 76)

    def _update_tooltip(self) -> None:
        lines = ["Cliquer un écran pour le sélectionner :"]
        for name, g in self._screens:
            a = self._assign.get(name, "")
            lines.append(f"  {name} — {g.width()}×{g.height()}"
                         + (f"  →  {a}" if a else ""))
        self.setToolTip("\n".join(lines))

    def _compute_rects(self) -> dict:
        if not self._screens:
            return {}
        minx = min(g.x() for _, g in self._screens)
        miny = min(g.y() for _, g in self._screens)
        maxx = max(g.x() + g.width() for _, g in self._screens)
        maxy = max(g.y() + g.height() for _, g in self._screens)
        bw, bh = maxx - minx, maxy - miny
        if bw <= 0 or bh <= 0:
            return {}
        area = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        scale = min(area.width() / bw, area.height() / bh)
        offx = area.x() + (area.width() - bw * scale) / 2
        offy = area.y() + (area.height() - bh * scale) / 2
        return {name: QRectF(offx + (g.x() - minx) * scale,
                             offy + (g.y() - miny) * scale,
                             g.width() * scale, g.height() * scale)
                for name, g in self._screens}

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._rects = self._compute_rects()
        f = painter.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() * 0.82))
        painter.setFont(f)
        fm = QFontMetrics(f)
        for name, g in self._screens:
            r = self._rects.get(name)
            if r is None:
                continue
            sel = name == self._current
            painter.setPen(QPen(QColor(theme.ACCENT if sel else theme.BORDER_HI),
                                2 if sel else 1))
            painter.setBrush(QColor(theme.ACCENT_LO if sel else theme.SURFACE))
            inner = r.adjusted(1.5, 1.5, -1.5, -1.5)
            painter.drawRoundedRect(inner, 3, 3)
            painter.setPen(QColor(theme.ON_ACCENT if sel else theme.TEXT))
            painter.drawText(
                inner, Qt.AlignmentFlag.AlignCenter,
                fm.elidedText(name, Qt.TextElideMode.ElideRight,
                              max(10, int(inner.width()) - 4)))

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        for name, r in self._rects.items():
            if r.contains(pos):
                if name != self._current:
                    self._current = name
                    self.update()
                    self.selected.emit(name)
                break


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, cfg: config.Config):
        super().__init__()
        self.cfg = cfg
        self.controller = RotationController(cfg)
        self.controller.changed.connect(self._refresh_status)
        self._loading_pl = False  # guard against edit feedback loops
        self._really_quitting = False
        self._tray_notified = False
        self.metadata = config.load_metadata()
        self._syncing = False
        self._sync_signals = _SyncSignals()
        self._sync_signals.progress.connect(self._on_sync_progress)
        self._sync_signals.done.connect(self._on_sync_done)

        self.setWindowTitle("Wallpaper Engine Manager")
        self.setWindowIcon(app_icon())
        self.resize(1180, 720)

        self._build_ui()
        self._build_tray()
        self._reload_library()
        self._refresh_playlists()
        self._refresh_status()
        self._on_screen_changed()  # load the active screen's playlist at startup

    # System tray ---------------------------------------------------------- #
    def _build_tray(self) -> None:
        """A tray icon that keeps the app (and its rotation) alive when the
        window is closed, and lets the user switch playlists per screen."""
        self._tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(app_icon(), self)
        self._tray.setToolTip("Wallpaper Engine Manager")
        menu = QMenu()
        menu.aboutToShow.connect(self._rebuild_tray_menu)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._rebuild_tray_menu()
        self._tray.show()

    def _on_tray_activated(self, reason) -> None:
        # Left-click / double-click toggles the window.
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_window()

    def _toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _rebuild_tray_menu(self) -> None:
        if self._tray is None:
            return
        menu = self._tray.contextMenu()
        menu.clear()

        title = menu.addAction("Wallpaper Engine Manager")
        title.setEnabled(False)
        menu.addSeparator()

        names = self.controller.playlist_names()
        for screen in self._screen_names():
            sub = menu.addMenu(f"{screen} — {self.controller.describe(screen)}")
            group = QActionGroup(sub)
            group.setExclusive(True)
            assigned = self.controller.assignments.get(screen, {})
            for name in names:
                act = sub.addAction(name)
                act.setCheckable(True)
                act.setChecked(
                    assigned.get("mode") == "playlist"
                    and assigned.get("playlist") == name
                )
                act.triggered.connect(
                    lambda _c, s=screen, n=name: self.controller.assign_playlist(s, n)
                )
                group.addAction(act)
            if not names:
                empty = sub.addAction("(aucune playlist)")
                empty.setEnabled(False)
            sub.addSeparator()
            clear = sub.addAction("Vider l'écran")
            clear.triggered.connect(lambda _c, s=screen: self.controller.clear(s))

        menu.addSeparator()
        show = menu.addAction("Ouvrir la fenêtre")
        show.triggered.connect(self._toggle_window)
        stop = menu.addAction("Tout arrêter")
        stop.triggered.connect(self.controller.stop_all)
        auto = menu.addAction("Démarrer avec la session")
        auto.setCheckable(True)
        auto.setChecked(config.is_autostart_enabled())
        auto.toggled.connect(self._set_autostart)
        launcher = menu.addAction("Ajouter au menu des applications")
        launcher.setCheckable(True)
        launcher.setChecked(config.is_launcher_installed())
        launcher.toggled.connect(self._set_launcher)
        menu.addSeparator()
        quit_act = menu.addAction("Quitter")
        quit_act.triggered.connect(self._quit_app)

    def _quit_app(self) -> None:
        self._really_quitting = True
        if self._tray is not None:
            self._tray.hide()
        QApplication.quit()

    # UI construction ------------------------------------------------------ #
    def _build_ui(self) -> None:
        self._build_settings_dialog()
        central = QWidget()
        root = QVBoxLayout(central)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_filter_panel())
        main = QWidget()
        main.setLayout(self._build_main_area())
        splitter.addWidget(main)
        splitter.addWidget(self._build_properties_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)  # the grid takes the slack
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([210, 760, 320])
        root.addWidget(splitter, 1)
        root.addWidget(self._build_playlist_bar())
        self.setCentralWidget(central)
        self.status = self.statusBar()

    def _build_playlist_bar(self) -> QWidget:
        """WPE-style bottom bar: playlist controls on top, a horizontal strip of
        the currently checked wallpapers (the working playlist) below."""
        bar = QFrame()
        bar.setObjectName("playlistBar")
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 6, 8, 6)

        controls = FlowLayout()
        self.pl_head = QLabel("Sélection (0)")
        self.pl_head.setObjectName("plHead")
        controls.addWidget(self.pl_head)

        controls.addWidget(QLabel("Playlist :"))
        self.pl_combo = QComboBox()
        self.pl_combo.setMinimumWidth(160)
        self.pl_combo.currentTextChanged.connect(self._on_playlist_selected)
        controls.addWidget(self.pl_combo)
        self.pl_count = QLabel("—")
        self.pl_count.setObjectName("plCount")
        controls.addWidget(self.pl_count)

        self.load_pl_btn = QPushButton("Charger")
        self.load_pl_btn.setToolTip(
            "Coche dans la grille les fonds de la playlist sélectionnée "
            "(pour l'éditer via « MàJ items » ou la forker via « Nouvelle »).")
        self.load_pl_btn.clicked.connect(self._load_playlist_into_checks)
        controls.addWidget(self.load_pl_btn)

        self.new_pl_btn = QPushButton("Nouvelle")
        self.new_pl_btn.setProperty("accent", True)
        self.new_pl_btn.setToolTip("Créer une playlist depuis les fonds cochés.")
        self.new_pl_btn.clicked.connect(self._create_playlist)
        controls.addWidget(self.new_pl_btn)

        self.update_pl_btn = QPushButton("MàJ items")
        self.update_pl_btn.setToolTip(
            "Remplacer les fonds de la playlist sélectionnée par les cochés.")
        self.update_pl_btn.clicked.connect(self._update_playlist_items)
        controls.addWidget(self.update_pl_btn)

        self.del_pl_btn = QPushButton("Supprimer")
        self.del_pl_btn.setProperty("danger", True)
        self.del_pl_btn.clicked.connect(self._delete_playlist)
        controls.addWidget(self.del_pl_btn)

        controls.addWidget(QLabel("Intervalle (min) :"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setValue(30)
        self.interval_spin.valueChanged.connect(self._on_pl_settings_changed)
        controls.addWidget(self.interval_spin)

        controls.addWidget(QLabel("Ordre :"))
        self.order_combo = QComboBox()
        self.order_combo.addItems(["Séquentiel", "Aléatoire"])
        self.order_combo.currentIndexChanged.connect(self._on_pl_settings_changed)
        controls.addWidget(self.order_combo)

        self.import_btn = QPushButton("Importer WPE")
        self.import_btn.setToolTip("Importer les playlists depuis Wallpaper Engine.")
        self.import_btn.clicked.connect(self._import_wpe)
        controls.addWidget(self.import_btn)

        self.uncheck_btn = QPushButton("Tout décocher")
        self.uncheck_btn.setEnabled(False)
        self.uncheck_btn.clicked.connect(lambda: self.model.clear_checks())
        controls.addWidget(self.uncheck_btn)
        v.addLayout(controls)

        self.checks_strip = ChecksStrip()
        v.addWidget(self.checks_strip)
        return bar

    # -- right-side properties panel (WPE-style) --------------------------- #
    def _build_properties_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("propsPanel")
        panel.setMinimumWidth(250)
        v = QVBoxLayout(panel)

        self.pp_preview = QLabel("Aucun fond sélectionné")
        self.pp_preview.setObjectName("ppPreview")
        self.pp_preview.setFixedHeight(168)
        self.pp_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pp_preview.installEventFilter(self)  # rescale preview on panel resize
        self._pp_preview_pm = QPixmap()
        v.addWidget(self.pp_preview)

        self.pp_title = QLabel("—")
        self.pp_title.setObjectName("ppTitle")
        self.pp_title.setWordWrap(True)
        self.pp_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.pp_title)

        self.pp_meta = QLabel("")
        self.pp_meta.setObjectName("ppMeta")
        self.pp_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.pp_meta)

        v.addWidget(self._hline())
        hdr = QLabel("Propriétés")
        hdr.setObjectName("ppSection")
        v.addWidget(hdr)

        self.pp_scroll = QScrollArea()
        self.pp_scroll.setWidgetResizable(True)
        self.pp_scroll.setFrameShape(QFrame.Shape.NoFrame)
        v.addWidget(self.pp_scroll, 1)

        row = QHBoxLayout()
        self.pp_reset = QPushButton("Réinitialiser")
        self.pp_reset.clicked.connect(self._reset_props)
        row.addWidget(self.pp_reset)
        self.pp_apply = QPushButton("Appliquer")
        self.pp_apply.setProperty("accent", True)
        self.pp_apply.clicked.connect(self._apply_props)
        row.addWidget(self.pp_apply)
        v.addLayout(row)

        self._pp_wp: Wallpaper | None = None
        self._pp_form: PropertyForm | None = None
        self._update_props_panel(None)
        return panel

    def _set_props_placeholder(self, text: str) -> None:
        lbl = QLabel(text)
        lbl.setObjectName("ppPlaceholder")
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.pp_scroll.setWidget(lbl)

    def _rescale_preview(self) -> None:
        pm = getattr(self, "_pp_preview_pm", QPixmap())
        if pm is None or pm.isNull():
            return
        w = max(80, self.pp_preview.width() - 2)
        self.pp_preview.setPixmap(pm.scaled(
            w, self.pp_preview.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def eventFilter(self, obj, event):
        if obj is self.pp_preview and event.type() == QEvent.Type.Resize:
            self._rescale_preview()
        return super().eventFilter(obj, event)

    def _update_props_panel(self, wp: Wallpaper | None) -> None:
        self._pp_wp = wp
        self._pp_form = None
        if wp is None:
            self._pp_preview_pm = QPixmap()
            self.pp_preview.setPixmap(QPixmap())
            self.pp_preview.setText("Aucun fond sélectionné")
            self.pp_title.setText("—")
            self.pp_meta.setText("")
            self._set_props_placeholder(
                "Sélectionne un fond pour voir et éditer ses propriétés.")
            self.pp_apply.setEnabled(False)
            self.pp_reset.setEnabled(False)
            return

        self._pp_preview_pm = QPixmap(str(wp.preview)) if wp.has_preview else QPixmap()
        if not self._pp_preview_pm.isNull():
            self.pp_preview.setText("")
            self._rescale_preview()
        else:
            self.pp_preview.setPixmap(QPixmap())
            self.pp_preview.setText("(pas d'aperçu)")
        self.pp_title.setText(wp.title)
        type_label = _TYPE_LABELS.get(wp.type, wp.type.capitalize() if wp.type else "")
        age_label = AGE_LABELS.get(wp.age, "")
        self.pp_meta.setText("  ·  ".join(x for x in (type_label, age_label) if x))

        props = library.read_properties(wp.folder)
        if not props:
            self._set_props_placeholder(
                "Ce fond n'expose aucune propriété personnalisable.")
            self.pp_apply.setEnabled(False)
            self.pp_reset.setEnabled(False)
            return
        overrides = config.load_properties().get(wp.id, {})
        self._pp_form = PropertyForm(props, overrides, color_parent=self)
        self.pp_scroll.setWidget(self._pp_form)
        self.pp_apply.setEnabled(True)
        self.pp_reset.setEnabled(bool(overrides))

    def _apply_props(self) -> None:
        wp = self._pp_wp
        if wp is None or self._pp_form is None:
            return
        new = self._pp_form.changed_values()
        all_overrides = config.load_properties()
        if new:
            all_overrides[wp.id] = new
        else:
            all_overrides.pop(wp.id, None)
        config.save_properties(all_overrides)
        self.controller.refresh_wallpaper(wp.id)  # live if currently displayed
        self.pp_reset.setEnabled(bool(new))
        self.status.showMessage(
            f"{len(new)} propriété(s) appliquée(s) à « {wp.title} »." if new
            else f"Propriétés de « {wp.title} » remises par défaut.", 5000)

    def _reset_props(self) -> None:
        wp = self._pp_wp
        if wp is None:
            return
        all_overrides = config.load_properties()
        if wp.id in all_overrides:
            all_overrides.pop(wp.id, None)
            config.save_properties(all_overrides)
            self.controller.refresh_wallpaper(wp.id)
        self._update_props_panel(wp)  # rebuild the form at defaults
        self.status.showMessage(f"Propriétés de « {wp.title} » réinitialisées.", 5000)

    # -- left filter sidebar (WPE-style) ----------------------------------- #
    def _build_filter_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("filterPanel")
        panel.setMinimumWidth(200)
        v = QVBoxLayout(panel)
        v.setContentsMargins(8, 8, 8, 8)

        head = QHBoxLayout()
        title = QLabel("Filtres")
        title.setObjectName("filterTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.reset_filters_btn = QPushButton("Réinitialiser")
        self.reset_filters_btn.clicked.connect(self._reset_filters)
        head.addWidget(self.reset_filters_btn)
        v.addLayout(head)

        self.compat_check = QCheckBox("Compatible écran")
        self.compat_check.setToolTip(
            "N'affiche que les fonds dont le ratio correspond à l'écran "
            "sélectionné (idéal pour l'ultrawide).")
        self.compat_check.toggled.connect(self._on_compat_toggled)
        v.addWidget(self.compat_check)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 4, 0)
        self._type_boxes: list[QCheckBox] = []
        self._age_boxes: list[QCheckBox] = []
        self._res_boxes: list[QCheckBox] = []
        self._genre_boxes: list[QCheckBox] = []
        self._type_layout = self._add_filter_section(cv, "Type")
        self._age_layout = self._add_filter_section(cv, "Âge")
        self._res_layout = self._add_filter_section(cv, "Résolution")
        self._genre_layout = self._add_filter_section(cv, "Genre", with_all_none=True)
        cv.addStretch(1)
        scroll.setWidget(content)
        v.addWidget(scroll, 1)

        self.sync_btn = QPushButton("Sync Steam")
        self.sync_btn.setToolTip(
            "Récupère la résolution des fonds depuis le Workshop Steam "
            "(sans clé, mise en cache).")
        self.sync_btn.clicked.connect(lambda: self._sync_metadata(force=True))
        v.addWidget(self.sync_btn)
        return panel

    def _add_filter_section(self, parent_layout, title: str,
                            with_all_none: bool = False):
        header = QHBoxLayout()
        lbl = QLabel(title)
        lbl.setObjectName("filterSection")
        header.addWidget(lbl)
        if with_all_none:
            header.addStretch(1)
            all_btn = QPushButton("Tout")
            all_btn.setObjectName("miniBtn")
            all_btn.clicked.connect(lambda: self._set_all_genres(True))
            none_btn = QPushButton("Aucun")
            none_btn.setObjectName("miniBtn")
            none_btn.clicked.connect(lambda: self._set_all_genres(False))
            header.addWidget(all_btn)
            header.addWidget(none_btn)
        parent_layout.addLayout(header)
        box = QVBoxLayout()
        box.setContentsMargins(6, 0, 0, 8)
        box.setSpacing(1)
        parent_layout.addLayout(box)
        return box

    # -- settings dialog (declutters the top bar) -------------------------- #
    def _build_settings_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Réglages")
        dlg.setModal(False)
        form = QFormLayout(dlg)

        self.silent_combo = QComboBox()
        self.silent_combo.addItems(["🔊 Son", "🔇 Muet"])
        self.silent_combo.setCurrentIndex(1 if self.cfg.silent else 0)
        self.silent_combo.currentIndexChanged.connect(self._on_silent_changed)
        form.addRow("Audio :", self.silent_combo)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(5, 240)
        self.fps_spin.setValue(self.cfg.fps)
        self.fps_spin.valueChanged.connect(self._on_fps_changed)
        form.addRow("FPS :", self.fps_spin)

        self.overlap_spin = QSpinBox()
        self.overlap_spin.setRange(0, 5000)
        self.overlap_spin.setSingleStep(100)
        self.overlap_spin.setValue(self.cfg.overlap_ms)
        self.overlap_spin.setToolTip(
            "Recouvrement entre l'ancien et le nouveau fond (0 = coupure nette).")
        self.overlap_spin.valueChanged.connect(self._on_overlap_changed)
        form.addRow("Transition (ms) :", self.overlap_spin)

        self.autostart_check = QCheckBox("Démarrer avec la session")
        self.autostart_check.setToolTip(
            "Relance l'app dans la barre système et restaure les fonds "
            "d'écran (rotation comprise) à l'ouverture de session.")
        self.autostart_check.setChecked(config.is_autostart_enabled())
        self.autostart_check.toggled.connect(self._set_autostart)
        form.addRow(self.autostart_check)

        self.launcher_check = QCheckBox("Menu applications")
        self.launcher_check.setToolTip(
            "Ajoute une entrée dans le menu des applications pour lancer "
            "l'app comme n'importe quel programme.")
        self.launcher_check.setChecked(config.is_launcher_installed())
        self.launcher_check.toggled.connect(self._set_launcher)
        form.addRow(self.launcher_check)

        paths_btn = QPushButton("Chemins de la bibliothèque…")
        paths_btn.clicked.connect(self._edit_paths)
        form.addRow(paths_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.hide)
        form.addRow(buttons)
        self._settings_dialog = dlg

    def _open_settings(self) -> None:
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _build_main_area(self) -> QVBoxLayout:
        area = QVBoxLayout()

        bar = FlowLayout()
        bar.addWidget(QLabel("Écrans :"))
        self.screen_picker = ScreenPicker()
        self.screen_picker.selected.connect(lambda _n: self._on_screen_changed())
        self._populate_screens()
        bar.addWidget(self.screen_picker)

        self.apply_single_btn = QPushButton("Fond sélectionné → écran")
        self.apply_single_btn.setProperty("accent", True)
        self.apply_single_btn.clicked.connect(self._apply_single)
        bar.addWidget(self.apply_single_btn)

        self.apply_pl_combo = QComboBox()
        bar.addWidget(self.apply_pl_combo)
        self.apply_pl_btn = QPushButton("Playlist → écran")
        self.apply_pl_btn.setProperty("accent", True)
        self.apply_pl_btn.clicked.connect(self._apply_playlist)
        bar.addWidget(self.apply_pl_btn)

        self.clear_btn = QPushButton("Vider l'écran")
        self.clear_btn.clicked.connect(self._clear_selected)
        bar.addWidget(self.clear_btn)
        self.stop_btn = QPushButton("Tout arrêter")
        self.stop_btn.clicked.connect(self.controller.stop_all)
        bar.addWidget(self.stop_btn)
        self.settings_btn = QPushButton("⚙ Réglages")
        self.settings_btn.setToolTip("Audio, FPS, transition, autostart, chemins…")
        self.settings_btn.clicked.connect(self._open_settings)
        bar.addWidget(self.settings_btn)
        area.addLayout(bar)

        bar2 = FlowLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Rechercher…")
        self.search.setMinimumWidth(220)
        self.search.textChanged.connect(self._on_search)
        bar2.addWidget(self.search)
        bar2.addWidget(QLabel("Tri :"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("Titre A→Z", "title")
        self.sort_combo.addItem("Taille ↑", "size_asc")
        self.sort_combo.addItem("Taille ↓", "size_desc")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        bar2.addWidget(self.sort_combo)
        self.count_label = QLabel("—")
        bar2.addWidget(self.count_label)
        area.addLayout(bar2)

        self.view = QListView()
        self.view.setObjectName("grid")
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(6)
        self.view.setMouseTracking(True)  # so the delegate gets hover state
        self.view.setItemDelegate(WallpaperCardDelegate(self.view))
        self.view.doubleClicked.connect(lambda _idx: self._apply_single())
        area.addWidget(self.view, 1)
        return area

    @staticmethod
    def _hline() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _populate_screens(self) -> None:
        self.screen_picker.set_screens(QGuiApplication.screens())

    def _screen_names(self) -> list[str]:
        return [s.name() for s in QGuiApplication.screens()]

    # Library --------------------------------------------------------------- #
    def _reload_library(self) -> None:
        if not self.cfg.is_usable():
            self.model = WallpaperModel([])
            self._install_model()
            QMessageBox.warning(
                self, "Bibliothèque introuvable",
                "Aucune bibliothèque Wallpaper Engine détectée.\n\n"
                "Bouton « Chemins… » pour indiquer workshop/content/431960 et les assets.",
            )
            return
        wallpapers = library.scan(self.cfg.library_path)
        self.model = WallpaperModel(wallpapers, self.metadata)
        self._install_model()
        self.status.showMessage(f"{len(wallpapers)} fonds d'écran chargés", 4000)
        # First run (no cached resolutions): fetch them in the background so the
        # resolution filters work without the user hunting for the Sync button.
        missing = [w.id for w in wallpapers if w.id not in self.metadata]
        if missing and not self.metadata:
            self._sync_metadata(force=False)

    def _install_model(self) -> None:
        self.proxy = WallpaperFilterProxy()
        self.proxy.setSourceModel(self.model)
        self.view.setModel(self.proxy)
        self.proxy.rowsInserted.connect(self._update_count)
        self.proxy.rowsRemoved.connect(self._update_count)
        self.proxy.modelReset.connect(self._update_count)
        self.model.checked_changed.connect(self._on_checked_changed)
        self.checks_strip.set_model(self.model)
        self.view.selectionModel().selectionChanged.connect(
            lambda *_: self._on_selection_changed()
        )
        self._populate_filters()
        self._update_count()
        self._on_checked_changed()
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        self._update_props_panel(self._selected_wallpaper())

    def _on_checked_changed(self) -> None:
        n = len(self.model.checked_ids())
        self.pl_head.setText(f"Sélection ({n})")
        self.uncheck_btn.setText(f"Tout décocher ({n})" if n else "Tout décocher")
        self.uncheck_btn.setEnabled(n > 0)

    # Filters --------------------------------------------------------------- #
    def _populate_filters(self) -> None:
        items = getattr(self.model, "_items", [])
        genres = sorted({t for w in items for t in w.tags}, key=str.casefold)
        types = sorted({w.type for w in items if w.type})
        ages = [a for a in ("everyone", "questionable", "mature")
                if any(w.age == a for w in items)]
        self._fill_filter_section(self._type_layout,
                                  [(t, t.capitalize()) for t in types],
                                  self.proxy.types, self._type_boxes)
        self._fill_filter_section(self._age_layout,
                                  [(a, AGE_LABELS.get(a, a)) for a in ages],
                                  self.proxy.ages, self._age_boxes)
        self._fill_filter_section(self._genre_layout, [(g, g) for g in genres],
                                  self.proxy.genres, self._genre_boxes)
        self._build_resolution_section()

    def _build_resolution_section(self) -> None:
        """Data-driven: lists the exact resolutions actually present (grouped by
        family, with counts). Rebuilt after a Steam sync; the current selection
        is preserved across the rebuild."""
        items = getattr(self.model, "_items", [])
        counts: Counter = Counter()
        no_res = 0
        for w in items:
            rs = set(self.model.resolutions_of(w.id))
            if rs:
                counts.update(rs)
            else:
                no_res += 1

        def sort_key(res: str):
            w, h = steam.parse_wh(res)
            fam = steam.aspect_family(w, h)
            fam_rank = steam.FAMILY_ORDER.index(fam) if fam in steam.FAMILY_ORDER else 99
            return (fam_rank, -(w * h))

        pairs = []
        for res in sorted(counts, key=sort_key):
            fam = steam.aspect_family(*steam.parse_wh(res))
            label = f"{steam.FAMILY_LABELS.get(fam, '?')} — {res}  ({counts[res]})"
            pairs.append((res, label))
        if no_res:
            pairs.append((WallpaperFilterProxy.NO_RES, f"(sans résolution) ({no_res})"))

        keep = set(self.proxy.resolutions)
        self._fill_filter_section(self._res_layout, pairs,
                                  self.proxy.resolutions, self._res_boxes)
        if keep:  # restore selection across the rebuild
            for cb in self._res_boxes:
                if cb.property("fvalue") in keep:
                    cb.setChecked(True)

    def _fill_filter_section(self, box, pairs, target_set, store) -> None:
        store.clear()
        target_set.clear()
        while box.count():
            item = box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for value, label in pairs:
            cb = QCheckBox(label)
            cb.setProperty("fvalue", value)
            cb.toggled.connect(
                lambda on, v=value, s=target_set: self._toggle_filter(s, v, on))
            box.addWidget(cb)
            store.append(cb)
        if not pairs:
            empty = QLabel("(aucun)")
            empty.setObjectName("filterEmpty")
            box.addWidget(empty)

    def _toggle_filter(self, target_set: set, value: str, on: bool) -> None:
        target_set.add(value) if on else target_set.discard(value)
        self.proxy.invalidate()
        self._update_count()

    def _set_all_genres(self, on: bool) -> None:
        s = self.proxy.genres
        for cb in self._genre_boxes:
            cb.blockSignals(True)
            cb.setChecked(on)
            cb.blockSignals(False)
        s.clear()
        if on:
            s.update(cb.property("fvalue") for cb in self._genre_boxes)
        self.proxy.invalidate()
        self._update_count()

    def _screen_ratio(self) -> float | None:
        name = self._current_screen()
        for screen in QGuiApplication.screens():
            if screen.name() == name:
                g = screen.geometry()
                return g.width() / g.height() if g.height() else None
        return None

    def _on_compat_toggled(self, on: bool) -> None:
        self.proxy.compat_ratio = self._screen_ratio() if on else None
        self.proxy.invalidate()
        self._prune_incompatible_checks()
        self._update_count()

    def _prune_incompatible_checks(self) -> None:
        """While "Compatible écran" is on, drop any checked wallpaper that no
        longer fits the selected screen — so switching screens doesn't carry
        over incompatible picks."""
        if not self.compat_check.isChecked() or self.proxy.compat_ratio is None:
            return
        checked = self.model.checked_ids()
        keep = [wid for wid in checked
                if any(self.proxy._matches_ratio(r)
                       for r in self.model.resolutions_of(wid))]
        if len(keep) != len(checked):
            self.model.set_checked(keep)
            self.status.showMessage(
                f"{len(checked) - len(keep)} fond(s) incompatible(s) décoché(s).", 4000
            )

    def _reset_filters(self) -> None:
        self.search.clear()
        self.proxy.text = ""
        self.compat_check.setChecked(False)
        for boxes, s in ((self._genre_boxes, self.proxy.genres),
                         (self._type_boxes, self.proxy.types),
                         (self._age_boxes, self.proxy.ages),
                         (self._res_boxes, self.proxy.resolutions)):
            for cb in boxes:
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)
            s.clear()
        self.proxy.compat_ratio = None
        self.proxy.invalidate()
        self._update_count()

    def _on_sort_changed(self, _i: int) -> None:
        self.proxy.sort_mode = self.sort_combo.currentData()
        self.proxy.apply_sort()

    def _update_count(self, *args) -> None:
        shown = self.proxy.rowCount()
        total = self.model.rowCount()
        items = getattr(self.model, "_items", [])
        no_res = sum(1 for w in items if not self.model.resolutions_of(w.id))
        extra = f"  ·  {no_res} sans résolution" if no_res else ""
        self.count_label.setText(f"{shown} / {total}{extra}")

    # Steam metadata sync --------------------------------------------------- #
    def _sync_metadata(self, force: bool) -> None:
        if self._syncing:
            return
        items = getattr(self.model, "_items", [])
        ids = ([w.id for w in items] if force
               else [w.id for w in items if w.id not in self.metadata])
        if not ids:
            if force:
                self.status.showMessage("Aucun fond à synchroniser.", 4000)
            return
        self._syncing = True
        self.sync_btn.setEnabled(False)
        self.sync_btn.setText("Sync…")
        self.status.showMessage(f"Sync Steam : 0/{len(ids)}…")
        QThreadPool.globalInstance().start(_MetaSyncTask(ids, self._sync_signals))

    def _on_sync_progress(self, done: int, total: int) -> None:
        self.status.showMessage(f"Sync Steam : {done}/{total}…")

    def _on_sync_done(self, result: dict) -> None:
        self.metadata.update(result)
        config.save_metadata(self.metadata)
        self.model.set_metadata(self.metadata)
        self._build_resolution_section()  # new resolutions are now known
        self.proxy.invalidate()
        self._syncing = False
        self.sync_btn.setEnabled(True)
        self.sync_btn.setText("Sync Steam")
        got = sum(1 for v in result.values() if v.get("resolutions"))
        self.status.showMessage(
            f"Sync terminée : {got}/{len(result)} fonds avec résolution taggée.", 6000
        )
        self._update_count()

    # Playlists panel ------------------------------------------------------- #
    def _refresh_playlists(self) -> None:
        names = self.controller.playlist_names()
        current = self.pl_combo.currentText() if self.pl_combo.count() else None
        self.pl_combo.blockSignals(True)
        self.pl_combo.clear()
        self.pl_combo.addItems(names)
        if current and current in names:
            self.pl_combo.setCurrentText(current)
        self.pl_combo.blockSignals(False)
        self.apply_pl_combo.clear()
        self.apply_pl_combo.addItems(names)
        self._on_playlist_selected(self.pl_combo.currentText())

    def _selected_playlist_name(self) -> str | None:
        return self.pl_combo.currentText() or None

    def _on_playlist_selected(self, name: str) -> None:
        pl = self.controller.playlists.get(name)
        if not pl:
            self.pl_count.setText("—")
            return
        self._loading_pl = True
        self.interval_spin.setValue(int(pl.get("interval_min", 30)))
        self.order_combo.setCurrentIndex(1 if pl.get("order") == "random" else 0)
        self._loading_pl = False
        self.pl_count.setText(f"{len(pl.get('ids', []))} fonds")

    def _load_playlist_into_checks(self) -> None:
        """Load the selected playlist's wallpapers into the grid checkboxes, so
        it can be edited (« MàJ items ») or forked (« Nouvelle »)."""
        name = self._selected_playlist_name()
        pl = self.controller.playlists.get(name) if name else None
        if not pl:
            self.status.showMessage("Sélectionne d'abord une playlist.", 4000)
            return
        ids = pl.get("ids", [])
        self.model.set_checked(ids)
        self.status.showMessage(
            f"{len(ids)} fonds de « {name} » cochés — édite puis « MàJ items » "
            "ou « Nouvelle ».", 6000
        )

    def _create_playlist(self) -> None:
        ids = self.model.checked_ids()
        if not ids:
            self.status.showMessage("Coche d'abord des fonds dans la grille.", 5000)
            return
        name, ok = QInputDialog.getText(self, "Nouvelle playlist", "Nom :")
        name = name.strip()
        if not ok or not name:
            return
        if name in self.controller.playlists:
            QMessageBox.warning(self, "Nom existant", "Une playlist porte déjà ce nom.")
            return
        self.controller.upsert_playlist(
            {"name": name, "ids": ids, "interval_min": 30, "order": "sequential"}
        )
        self._refresh_playlists()
        self.status.showMessage(f"Playlist « {name} » créée ({len(ids)} fonds).", 5000)

    def _update_playlist_items(self) -> None:
        name = self._selected_playlist_name()
        if not name:
            return
        ids = self.model.checked_ids()
        if not ids:
            self.status.showMessage("Aucun fond coché.", 4000)
            return
        pl = self.controller.playlists[name]
        pl["ids"] = ids
        self.controller.upsert_playlist(pl)
        self.pl_count.setText(f"{len(ids)} fonds")
        self.controller.apply()  # refresh live rotation if this playlist is active
        self.status.showMessage(f"« {name} » mise à jour ({len(ids)} fonds).", 5000)

    def _delete_playlist(self) -> None:
        name = self._selected_playlist_name()
        if not name:
            return
        self.controller.delete_playlist(name)
        self._refresh_playlists()
        self.status.showMessage(f"Playlist « {name} » supprimée.", 4000)

    def _on_pl_settings_changed(self, _v=None) -> None:
        if self._loading_pl:
            return
        name = self._selected_playlist_name()
        if not name:
            return
        pl = self.controller.playlists[name]
        pl["interval_min"] = self.interval_spin.value()
        pl["order"] = "random" if self.order_combo.currentIndex() == 1 else "sequential"
        self.controller.upsert_playlist(pl)
        self.controller.apply()  # re-arm timers with the new interval

    def _import_wpe(self) -> None:
        path = config.wpe_config_path(self.cfg)
        if path is None:
            path_str, _ = QFileDialog.getOpenFileName(
                self, "config.json de Wallpaper Engine", str(Path.home()), "config.json (config.json)"
            )
            if not path_str:
                return
            path = Path(path_str)
        imported = config.import_wpe_playlists(path)
        if not imported:
            QMessageBox.information(self, "Import", "Aucune playlist trouvée dans ce config.json.")
            return
        added = 0
        for pl in imported:
            name = pl["name"]
            while name in self.controller.playlists:
                name += " (import)"
            pl["name"] = name
            self.controller.upsert_playlist(pl)
            added += 1
        self._refresh_playlists()
        QMessageBox.information(self, "Import", f"{added} playlist(s) importée(s) depuis Wallpaper Engine.")

    # Selection helpers ----------------------------------------------------- #
    def _selected_wallpaper(self) -> Wallpaper | None:
        sm = self.view.selectionModel()
        idxs = sm.selectedIndexes() if sm else []
        return idxs[0].data(WALLPAPER_ROLE) if idxs else None

    def _current_screen(self) -> str | None:
        return self.screen_picker.current()

    # Actions --------------------------------------------------------------- #
    def _apply_single(self) -> None:
        wp = self._selected_wallpaper()
        screen = self._current_screen()
        if wp is None:
            self.status.showMessage("Sélectionne un fond (clic simple).", 4000)
            return
        if screen is None:
            self.status.showMessage("Aucun écran sélectionné.", 4000)
            return
        self.controller.assign_single(screen, wp.id)
        self.status.showMessage(f"« {wp.title} » → {screen}", 5000)

    def _apply_playlist(self) -> None:
        screen = self._current_screen()
        name = self.apply_pl_combo.currentText()
        if not screen or not name:
            self.status.showMessage("Choisis un écran et une playlist.", 4000)
            return
        self.controller.assign_playlist(screen, name)
        self.status.showMessage(f"Playlist « {name} » → {screen}", 5000)

    def _clear_selected(self) -> None:
        screen = self._current_screen()
        if screen:
            self.controller.clear(screen)
            self.status.showMessage(f"Écran {screen} vidé.", 4000)

    def _refresh_status(self) -> None:
        parts = []
        assignments = {}
        for name in self._screen_names():
            desc = self.controller.describe(name)
            assignments[name] = desc
            parts.append(f"{name} → {desc}")
        if hasattr(self, "screen_picker"):
            self.screen_picker.set_assignments(assignments)
        running = "▶ en cours" if engine.is_running() else "■ arrêté"
        self.setWindowTitle(f"Wallpaper Engine Manager — {running}   [{'  |  '.join(parts)}]")
        if getattr(self, "_tray", None) is not None:
            self._tray.setToolTip(
                "Wallpaper Engine Manager\n" + "\n".join(parts)
            )

    def _set_autostart(self, enabled: bool) -> None:
        config.set_autostart(bool(enabled))
        # Keep the GUI checkbox and the tray menu in sync with each other.
        if hasattr(self, "autostart_check") and self.autostart_check.isChecked() != enabled:
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(enabled)
            self.autostart_check.blockSignals(False)

    def _set_launcher(self, enabled: bool) -> None:
        config.set_launcher(bool(enabled))
        if hasattr(self, "launcher_check") and self.launcher_check.isChecked() != enabled:
            self.launcher_check.blockSignals(True)
            self.launcher_check.setChecked(enabled)
            self.launcher_check.blockSignals(False)

    def closeEvent(self, event) -> None:
        """Closing the window hides it to the tray so rotation keeps running.

        A real exit goes through the tray's « Quitter » entry. If there's no
        system tray, closing quits normally (otherwise the app would be
        unreachable)."""
        if self._really_quitting or getattr(self, "_tray", None) is None:
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self._tray_notified:
            self._tray_notified = True
            self._tray.showMessage(
                "Wallpaper Engine Manager",
                "L'app reste dans la barre système ; la rotation continue. "
                "Clic sur l'icône pour rouvrir, clic droit → Quitter pour fermer.",
                app_icon(),
                5000,
            )

    # Options --------------------------------------------------------------- #
    def _on_screen_changed(self, _i=None) -> None:
        # Keep the "compatible with screen" filter tied to the selected screen.
        if self.compat_check.isChecked():
            self.proxy.compat_ratio = self._screen_ratio()
            self.proxy.invalidate()
            self._prune_incompatible_checks()
            self._update_count()
        screen = self._current_screen()
        if screen is None:
            return
        # Reflect what's on this screen (like WPE): load its playlist into the
        # bottom bar, or select its single wallpaper in the grid.
        a = self.controller.assignments.get(screen)
        if a and a.get("mode") == "playlist":
            name = a.get("playlist")
            if name in self.controller.playlists:
                if self.pl_combo.currentText() != name:
                    self.pl_combo.setCurrentText(name)  # loads interval/order
                self.model.set_checked(
                    self.controller.playlists[name].get("ids", []))
                return
        wid = self.controller.current_id(screen)
        if not wid:
            return
        for row in range(self.proxy.rowCount()):
            idx = self.proxy.index(row, 0)
            wp = idx.data(WALLPAPER_ROLE)
            if wp and wp.id == wid:
                self.view.setCurrentIndex(idx)
                self.view.scrollTo(idx)
                break

    def _on_search(self, text: str) -> None:
        self.proxy.text = text
        self.proxy.invalidate()
        self._update_count()

    def _on_silent_changed(self, index: int) -> None:
        self.cfg.silent = index == 1
        config.save_config(self.cfg)
        self.controller.apply()

    def _on_fps_changed(self, value: int) -> None:
        self.cfg.fps = value
        config.save_config(self.cfg)

    def _on_overlap_changed(self, value: int) -> None:
        self.cfg.overlap_ms = value
        config.save_config(self.cfg)

    def _edit_paths(self) -> None:
        lib = QFileDialog.getExistingDirectory(
            self, "Dossier des wallpapers (…/workshop/content/431960)",
            self.cfg.library_dir or str(Path.home()),
        )
        if lib:
            self.cfg.library_dir = lib
        assets = QFileDialog.getExistingDirectory(
            self, "Dossier des assets (…/common/wallpaper_engine/assets)",
            self.cfg.assets_dir or lib or str(Path.home()),
        )
        if assets:
            self.cfg.assets_dir = assets
        config.save_config(self.cfg)
        self._reload_library()
