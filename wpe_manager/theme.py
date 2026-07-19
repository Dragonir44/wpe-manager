"""WPE-inspired dark theme: a fixed dark palette + QSS so the app looks like
Wallpaper Engine regardless of the user's Breeze/system theme.

Built on the Fusion style (predictable across distros; honours the QPalette),
with QSS on top for rounded borders, hover states and the blue accent. Colour
constants are shared with the grid card delegate (see gui.WallpaperCardDelegate)
so the whole UI reads as one system.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# --- WPE-sampled palette ---------------------------------------------------- #
# Colours sampled from Wallpaper Engine's own UI (neutral dark greys, a vivid
# azure accent #2d75f4, a coral destructive red #d9534f).
BG        = "#26272b"   # window background
PANEL     = "#202124"   # side panels, popups
SURFACE   = "#34363b"   # inputs, buttons
SURFACE_HI = "#3e4046"  # hovered surface
GRID_BG   = "#1c1d20"   # the thumbnail grid canvas
STRIP_BG  = "#2b2c30"   # card title strip (below the thumbnail)
BORDER    = "#3c3e44"
BORDER_HI = "#4c4f56"
TEXT      = "#e7e7e8"
MUTED     = "#9a9ba0"
ACCENT    = "#2d75f4"   # WPE azure
ACCENT_HI = "#4785f6"
ACCENT_LO = "#1f5fd0"
ON_ACCENT = "#ffffff"
DANGER    = "#d9534f"   # destructive actions
DANGER_HI = "#e0645f"

_QSS = f"""
QToolTip {{
    background: #0f1114; color: {TEXT};
    border: 1px solid {BORDER}; padding: 4px 6px;
}}

/* --- buttons --- */
QPushButton {{
    background: {SURFACE}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 6px;
    padding: 5px 12px;
}}
QPushButton:hover {{ background: {SURFACE_HI}; border-color: {BORDER_HI}; }}
QPushButton:pressed {{ background: {PANEL}; }}
QPushButton:disabled {{ color: {MUTED}; background: {PANEL}; border-color: #2f333a; }}

/* primary / accent buttons (setProperty("accent", True)) */
QPushButton[accent="true"] {{
    background: {ACCENT}; color: {ON_ACCENT};
    border: 1px solid {ACCENT}; font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton[accent="true"]:pressed {{ background: {ACCENT_LO}; border-color: {ACCENT_LO}; }}
QPushButton[accent="true"]:disabled {{ background: #29405f; border-color: #29405f; color: #8aa0c0; }}

/* destructive buttons (setProperty("danger", True)) */
QPushButton[danger="true"] {{
    background: {DANGER}; color: {ON_ACCENT};
    border: 1px solid {DANGER}; font-weight: 600;
}}
QPushButton[danger="true"]:hover {{ background: {DANGER_HI}; border-color: {DANGER_HI}; }}
QPushButton[danger="true"]:disabled {{ background: #5a3a39; border-color: #5a3a39; color: #b78d8b; }}

/* --- inputs --- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {SURFACE}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 6px;
    padding: 4px 8px; selection-background-color: {ACCENT};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {BORDER_HI};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {PANEL}; color: {TEXT};
    border: 1px solid {BORDER}; selection-background-color: {ACCENT};
    outline: 0;
}}

/* --- panels --- */
QFrame#playlistPanel {{
    background: {PANEL}; border: 1px solid #33373e; border-radius: 10px;
}}
QListWidget {{
    background: #1e2126; color: {TEXT};
    border: 1px solid #33373e; border-radius: 8px; padding: 4px;
    outline: 0;
}}
QListWidget::item {{ padding: 6px 8px; border-radius: 6px; }}
QListWidget::item:hover {{ background: {SURFACE_HI}; }}
QListWidget::item:selected {{ background: {ACCENT}; color: {ON_ACCENT}; }}

/* --- the thumbnail grid --- */
QListView#grid {{
    background: {GRID_BG}; border: 1px solid #2a2d33; border-radius: 8px;
}}

/* --- right-side properties panel --- */
QFrame#propsPanel {{
    background: {PANEL}; border: 1px solid #33373e; border-radius: 10px;
}}
QLabel#ppPreview {{
    background: {GRID_BG}; border: 1px solid {BORDER};
    border-radius: 6px; color: {MUTED};
}}
QLabel#ppTitle {{ font-size: 15px; font-weight: 600; padding: 2px 0; }}
QLabel#ppMeta {{ color: {MUTED}; }}
QLabel#ppSection {{ color: {MUTED}; font-weight: 600; padding-top: 2px; }}
QLabel#ppPlaceholder {{ color: {MUTED}; padding: 8px 2px; }}

/* --- menus --- */
QMenu {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; color: {ON_ACCENT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

/* --- scrollbars --- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {BORDER_HI}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: {BORDER_HI}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* --- splitter handles between panels --- */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:hover {{ background: {BORDER}; border-radius: 2px; }}

/* --- misc --- */
QStatusBar {{ background: {GRID_BG}; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}
"""


def apply(app: QApplication) -> None:
    """Force the Fusion style + a fixed WPE-like dark palette and QSS."""
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(PANEL))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(SURFACE))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#0f1114"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(MUTED))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(ON_ACCENT))
    pal.setColor(QPalette.ColorRole.Link, QColor(ACCENT_HI))
    disabled = QColor(MUTED)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    app.setPalette(pal)
    app.setStyleSheet(_QSS)
