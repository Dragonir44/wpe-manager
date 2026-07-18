"""Qt (PySide6) GUI: a fast, lazily-thumbnailed grid of wallpapers, a playlist
panel (checkbox selection + WPE import), and per-screen assignment."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
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
    QColor,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import config, engine, library, steam
from .library import AGE_LABELS, Wallpaper
from .rotation import RotationController

THUMB = QSize(240, 135)  # 16:9 thumbnails
WALLPAPER_ROLE = Qt.ItemDataRole.UserRole + 1


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
        for i in range(self.screen_combo.count()):
            screen = self.screen_combo.itemData(i)
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
        central = QWidget()
        outer = QHBoxLayout(central)
        outer.addWidget(self._build_playlist_panel())
        outer.addLayout(self._build_main_area(), 1)
        self.setCentralWidget(central)
        self.status = self.statusBar()

    def _build_playlist_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel.setFixedWidth(280)
        v = QVBoxLayout(panel)
        v.addWidget(QLabel("<b>Playlists</b>"))

        self.pl_list = QListWidget()
        self.pl_list.currentTextChanged.connect(self._on_playlist_selected)
        v.addWidget(self.pl_list, 1)

        self.new_pl_btn = QPushButton("Nouvelle (depuis cochés)")
        self.new_pl_btn.clicked.connect(self._create_playlist)
        v.addWidget(self.new_pl_btn)

        self.update_pl_btn = QPushButton("MàJ items (depuis cochés)")
        self.update_pl_btn.clicked.connect(self._update_playlist_items)
        v.addWidget(self.update_pl_btn)

        self.del_pl_btn = QPushButton("Supprimer")
        self.del_pl_btn.clicked.connect(self._delete_playlist)
        v.addWidget(self.del_pl_btn)

        self.import_btn = QPushButton("Importer depuis Wallpaper Engine")
        self.import_btn.clicked.connect(self._import_wpe)
        v.addWidget(self.import_btn)

        v.addWidget(self._hline())
        v.addWidget(QLabel("Réglages de la playlist :"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Intervalle (min) :"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setValue(30)
        self.interval_spin.valueChanged.connect(self._on_pl_settings_changed)
        row.addWidget(self.interval_spin)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Ordre :"))
        self.order_combo = QComboBox()
        self.order_combo.addItems(["Séquentiel", "Aléatoire"])
        self.order_combo.currentIndexChanged.connect(self._on_pl_settings_changed)
        row2.addWidget(self.order_combo)
        v.addLayout(row2)

        self.pl_count = QLabel("—")
        v.addWidget(self.pl_count)
        return panel

    def _build_main_area(self) -> QVBoxLayout:
        area = QVBoxLayout()

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Écran :"))
        self.screen_combo = QComboBox()
        self._populate_screens()
        self.screen_combo.currentIndexChanged.connect(self._on_screen_changed)
        bar.addWidget(self.screen_combo)

        self.apply_single_btn = QPushButton("Fond sélectionné → écran")
        self.apply_single_btn.clicked.connect(self._apply_single)
        bar.addWidget(self.apply_single_btn)

        self.apply_pl_combo = QComboBox()
        bar.addWidget(self.apply_pl_combo)
        self.apply_pl_btn = QPushButton("Playlist → écran")
        self.apply_pl_btn.clicked.connect(self._apply_playlist)
        bar.addWidget(self.apply_pl_btn)

        self.clear_btn = QPushButton("Vider l'écran")
        self.clear_btn.clicked.connect(self._clear_selected)
        bar.addWidget(self.clear_btn)
        bar.addStretch(1)
        self.autostart_check = QCheckBox("Démarrer avec la session")
        self.autostart_check.setToolTip(
            "Relance l'app dans la barre système et restaure les fonds "
            "d'écran (rotation comprise) à l'ouverture de session."
        )
        self.autostart_check.setChecked(config.is_autostart_enabled())
        self.autostart_check.toggled.connect(self._set_autostart)
        bar.addWidget(self.autostart_check)
        self.launcher_check = QCheckBox("Menu applications")
        self.launcher_check.setToolTip(
            "Ajoute une entrée dans le menu des applications pour lancer "
            "l'app comme n'importe quel programme."
        )
        self.launcher_check.setChecked(config.is_launcher_installed())
        self.launcher_check.toggled.connect(self._set_launcher)
        bar.addWidget(self.launcher_check)
        self.stop_btn = QPushButton("Tout arrêter")
        self.stop_btn.clicked.connect(self.controller.stop_all)
        bar.addWidget(self.stop_btn)
        area.addLayout(bar)

        bar2 = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Rechercher…")
        self.search.textChanged.connect(self._on_search)
        bar2.addWidget(self.search, 1)
        self.silent_combo = QComboBox()
        self.silent_combo.addItems(["🔊 Son", "🔇 Muet"])
        self.silent_combo.setCurrentIndex(1 if self.cfg.silent else 0)
        self.silent_combo.currentIndexChanged.connect(self._on_silent_changed)
        bar2.addWidget(self.silent_combo)
        bar2.addWidget(QLabel("FPS :"))
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(5, 240)
        self.fps_spin.setValue(self.cfg.fps)
        self.fps_spin.valueChanged.connect(self._on_fps_changed)
        bar2.addWidget(self.fps_spin)
        bar2.addWidget(QLabel("Transition (ms) :"))
        self.overlap_spin = QSpinBox()
        self.overlap_spin.setRange(0, 5000)
        self.overlap_spin.setSingleStep(100)
        self.overlap_spin.setValue(self.cfg.overlap_ms)
        self.overlap_spin.setToolTip("Recouvrement entre l'ancien et le nouveau fond (0 = coupure nette).")
        self.overlap_spin.valueChanged.connect(self._on_overlap_changed)
        bar2.addWidget(self.overlap_spin)
        self.paths_btn = QPushButton("Chemins…")
        self.paths_btn.clicked.connect(self._edit_paths)
        bar2.addWidget(self.paths_btn)
        area.addLayout(bar2)

        bar3 = QHBoxLayout()
        bar3.addWidget(QLabel("Filtres :"))
        self.genre_btn = QPushButton("Genre")
        self.type_btn = QPushButton("Type")
        self.age_btn = QPushButton("Âge")
        self.res_btn = QPushButton("Résolution")
        for b in (self.genre_btn, self.type_btn, self.age_btn, self.res_btn):
            b._base_label = b.text()
            bar3.addWidget(b)
        self.compat_check = QCheckBox("Compatible écran")
        self.compat_check.setToolTip(
            "N'affiche que les fonds dont le ratio correspond à l'écran "
            "sélectionné en haut (idéal pour l'ultrawide)."
        )
        self.compat_check.toggled.connect(self._on_compat_toggled)
        bar3.addWidget(self.compat_check)
        self.reset_filters_btn = QPushButton("Réinitialiser")
        self.reset_filters_btn.clicked.connect(self._reset_filters)
        bar3.addWidget(self.reset_filters_btn)
        bar3.addStretch(1)
        bar3.addWidget(QLabel("Tri :"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("Titre A→Z", "title")
        self.sort_combo.addItem("Taille ↑", "size_asc")
        self.sort_combo.addItem("Taille ↓", "size_desc")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        bar3.addWidget(self.sort_combo)
        self.count_label = QLabel("—")
        bar3.addWidget(self.count_label)
        self.sync_btn = QPushButton("Sync Steam")
        self.sync_btn.setToolTip(
            "Récupère la résolution des fonds depuis le Workshop Steam "
            "(sans clé, mise en cache)."
        )
        self.sync_btn.clicked.connect(lambda: self._sync_metadata(force=True))
        bar3.addWidget(self.sync_btn)
        area.addLayout(bar3)

        self.view = QListView()
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setWordWrap(True)
        self.view.setSpacing(8)
        self.view.setIconSize(THUMB)
        self.view.setGridSize(QSize(THUMB.width() + 24, THUMB.height() + 46))
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
        self.screen_combo.clear()
        for screen in QGuiApplication.screens():
            geo = screen.geometry()
            self.screen_combo.addItem(f"{screen.name()}  ({geo.width()}×{geo.height()})", screen.name())

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
        self._populate_filter_menus()
        self._update_count()

    # Filters --------------------------------------------------------------- #
    def _populate_filter_menus(self) -> None:
        items = getattr(self.model, "_items", [])
        genres = sorted({t for w in items for t in w.tags}, key=str.casefold)
        types = sorted({w.type for w in items if w.type})
        ages = [a for a in ("everyone", "questionable", "mature")
                if any(w.age == a for w in items)]
        self._install_menu(self.genre_btn, [(g, g) for g in genres], self.proxy.genres)
        self._install_menu(self.type_btn,
                           [(t, t.capitalize()) for t in types], self.proxy.types)
        self._install_menu(self.age_btn,
                           [(a, AGE_LABELS.get(a, a)) for a in ages], self.proxy.ages)
        self._build_resolution_menu()

    def _build_resolution_menu(self) -> None:
        """The resolution menu is data-driven: it lists the exact resolutions
        actually present (grouped by family, with counts), so it stays useful
        whatever the library holds. Rebuilt after a Steam sync; the current
        selection is preserved across the rebuild."""
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
        self._install_menu(self.res_btn, pairs, self.proxy.resolutions)
        if keep:  # restore selection across the rebuild
            for act in self.res_btn.menu().actions():
                if act.data() in keep:
                    act.setChecked(True)

    def _install_menu(self, button, pairs, target_set) -> None:
        target_set.clear()
        button.setText(button._base_label)
        menu = QMenu(button)
        for value, label in pairs:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setData(value)
            act.toggled.connect(
                lambda on, v=value, s=target_set, b=button: self._toggle_filter(s, v, on, b)
            )
        if not pairs:
            menu.addAction("(aucun)").setEnabled(False)
        button.setMenu(menu)

    def _toggle_filter(self, target_set: set, value: str, on: bool, button) -> None:
        target_set.add(value) if on else target_set.discard(value)
        n = len(target_set)
        button.setText(f"{button._base_label} ({n})" if n else button._base_label)
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
        self._update_count()

    def _reset_filters(self) -> None:
        self.search.clear()
        self.proxy.text = ""
        self.compat_check.setChecked(False)
        for btn, s in ((self.genre_btn, self.proxy.genres),
                       (self.type_btn, self.proxy.types),
                       (self.age_btn, self.proxy.ages),
                       (self.res_btn, self.proxy.resolutions)):
            s.clear()
            btn.setText(btn._base_label)
            for act in (btn.menu().actions() if btn.menu() else []):
                act.setChecked(False)
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
        self._build_resolution_menu()  # new resolutions are now known
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
        current = self.pl_list.currentItem().text() if self.pl_list.currentItem() else None
        self.pl_list.blockSignals(True)
        self.pl_list.clear()
        self.pl_list.addItems(names)
        self.pl_list.blockSignals(False)
        self.apply_pl_combo.clear()
        self.apply_pl_combo.addItems(names)
        if current and current in names:
            items = self.pl_list.findItems(current, Qt.MatchFlag.MatchExactly)
            if items:
                self.pl_list.setCurrentItem(items[0])

    def _selected_playlist_name(self) -> str | None:
        item = self.pl_list.currentItem()
        return item.text() if item else None

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
        # Reflect the playlist's contents as checked items in the grid.
        self.model.set_checked(pl.get("ids", []))

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
        return self.screen_combo.currentData()

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
        for i in range(self.screen_combo.count()):
            name = self.screen_combo.itemData(i)
            parts.append(f"{name} → {self.controller.describe(name)}")
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
    def _on_screen_changed(self, _i: int) -> None:
        # Keep the "compatible with screen" filter tied to the selected screen.
        if self.compat_check.isChecked():
            self.proxy.compat_ratio = self._screen_ratio()
            self.proxy.invalidate()
            self._update_count()
        screen = self._current_screen()
        wid = self.controller.current_id(screen) if screen else None
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
