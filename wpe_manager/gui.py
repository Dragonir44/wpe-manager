"""Qt (PySide6) GUI: a fast, lazily-thumbnailed grid of wallpapers, a playlist
panel (checkbox selection + WPE import), and per-screen assignment."""
from __future__ import annotations

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
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
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
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import config, engine, library
from .library import Wallpaper
from .rotation import RotationController

THUMB = QSize(240, 135)  # 16:9 thumbnails
WALLPAPER_ROLE = Qt.ItemDataRole.UserRole + 1


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
    def __init__(self, wallpapers: list[Wallpaper]):
        super().__init__()
        self._items = wallpapers
        self._by_id = {w.id: i for i, w in enumerate(wallpapers)}
        self._pixmaps: dict[int, QPixmap] = {}
        self._pending: set[int] = set()
        self._checked: list[str] = []  # wallpaper ids, selection order preserved
        self._pool = QThreadPool.globalInstance()
        self._signals = _ThumbSignals()
        self._signals.done.connect(self._on_thumb)
        self._fallback = _placeholder()

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
            return f"{wp.title}\nType : {wp.type}\nID : {wp.id}"
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
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, cfg: config.Config):
        super().__init__()
        self.cfg = cfg
        self.controller = RotationController(cfg)
        self.controller.changed.connect(self._refresh_status)
        self._loading_pl = False  # guard against edit feedback loops

        self.setWindowTitle("Wallpaper Engine Manager")
        self.resize(1180, 720)

        self._build_ui()
        self._reload_library()
        self._refresh_playlists()
        self._refresh_status()

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
        self.model = WallpaperModel(wallpapers)
        self._install_model()
        self.status.showMessage(f"{len(wallpapers)} fonds d'écran chargés", 4000)

    def _install_model(self) -> None:
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.view.setModel(self.proxy)

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

    # Options --------------------------------------------------------------- #
    def _on_screen_changed(self, _i: int) -> None:
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
        self.proxy.setFilterFixedString(text)

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
