"""theme.py - HazGuard / OmniBand dark-mode Qt stylesheet constants.

Cinematic light industrial aesthetic: Carbon background, Burnt Orange accents,
Warm Graphite secondary text. NO cyan, teal, or generic blue.
"""
from __future__ import annotations

# ── Colour tokens ──────────────────────────────────────────────────────────
CARBON          = "#252525"
WARM_GRAPHITE   = "#716D67"
INDUSTRIAL_IVORY = "#F7F5F0"
PURE_WHITE      = "#FFFFFF"
WARM_STONE      = "#E9E5DE"
BURNT_ORANGE    = "#F07842"
DARK_BURNT_ORANGE = "#A94E29"
SOFT_SAND       = "#D8C9BA"

# Surface layers
SURFACE_0 = "#181a1b"   # deepest background
SURFACE_1 = "#1e2022"   # panel background
SURFACE_2 = "#252729"   # card / group-box
SURFACE_3 = "#2e3133"   # hover, selected row
BORDER    = "#3a3d40"   # subtle separator

# Accent
ACCENT      = BURNT_ORANGE
ACCENT_DARK = DARK_BURNT_ORANGE
ACCENT_DIM  = "#7a3c21"

# Status
STATUS_OK    = "#4caf50"
STATUS_WARN  = BURNT_ORANGE
STATUS_ERR   = "#e0303d"
STATUS_INFO  = SOFT_SAND

# Text
TEXT_PRIMARY   = INDUSTRIAL_IVORY
TEXT_SECONDARY = WARM_GRAPHITE
TEXT_DISABLED  = "#4a4845"

# Font stacks
FONT_MONO = "Cascadia Code, Consolas, Courier New, monospace"
FONT_UI   = "Segoe UI, Arial, sans-serif"

FONT_SIZE_XS = 9
FONT_SIZE_SM = 10
FONT_SIZE_MD = 12
FONT_SIZE_LG = 14
FONT_SIZE_XL = 18

MAIN_STYLESHEET = f"""
QMainWindow, QDialog, QWidget {{
    background-color: {SURFACE_0};
    color: {TEXT_PRIMARY};
    font-family: {FONT_UI};
    font-size: {FONT_SIZE_MD}px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    background: {SURFACE_1};
}}
QTabBar::tab {{
    background: {SURFACE_2};
    color: {TEXT_SECONDARY};
    padding: 8px 20px;
    border: 1px solid {BORDER};
    border-bottom: none;
    min-width: 100px;
}}
QTabBar::tab:selected {{
    background: {SURFACE_1};
    color: {ACCENT};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover {{
    color: {TEXT_PRIMARY};
    background: {SURFACE_3};
}}
QPushButton {{
    background-color: {SURFACE_2};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 16px;
    min-height: 36px;
    min-width: 80px;
}}
QPushButton:hover {{
    background-color: {SURFACE_3};
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {ACCENT_DIM};
    border-color: {ACCENT_DARK};
}}
QPushButton:disabled {{
    color: {TEXT_DISABLED};
    border-color: {BORDER};
}}
QPushButton#accent_btn {{
    background-color: {ACCENT};
    color: {SURFACE_0};
    font-weight: bold;
    border: none;
}}
QPushButton#accent_btn:hover {{
    background-color: {DARK_BURNT_ORANGE};
}}
QPushButton#danger_btn {{
    background-color: {STATUS_ERR};
    color: {PURE_WHITE};
    font-weight: bold;
    border: none;
}}
QLabel {{
    color: {TEXT_PRIMARY};
    background: transparent;
}}
QLabel#secondary {{
    color: {TEXT_SECONDARY};
    font-size: {FONT_SIZE_SM}px;
}}
QLabel#accent {{
    color: {ACCENT};
    font-weight: bold;
}}
QLabel#mono {{
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_MD}px;
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {SURFACE_3};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 16px; height: 16px;
    background: {ACCENT};
    border-radius: 8px;
    margin: -6px 0;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    margin-top: 12px;
    padding-top: 8px;
    color: {TEXT_SECONDARY};
    font-size: {FONT_SIZE_SM}px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}}
QComboBox {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 4px 8px;
    color: {TEXT_PRIMARY};
    min-height: 30px;
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    selection-background-color: {SURFACE_3};
    color: {TEXT_PRIMARY};
}}
QScrollBar:vertical {{
    background: {SURFACE_1};
    width: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {SURFACE_3};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QTextEdit, QPlainTextEdit {{
    background: {SURFACE_1};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    font-family: {FONT_MONO};
    font-size: {FONT_SIZE_SM}px;
}}
QMenuBar {{
    background: {SURFACE_0};
    color: {TEXT_PRIMARY};
}}
QMenuBar::item:selected {{
    background: {SURFACE_2};
    color: {ACCENT};
}}
QMenu {{
    background: {SURFACE_2};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
}}
QMenu::item:selected {{
    background: {SURFACE_3};
    color: {ACCENT};
}}
QToolTip {{
    background: {SURFACE_2};
    color: {TEXT_PRIMARY};
    border: 1px solid {ACCENT};
    padding: 4px;
}}
QStatusBar {{
    background: {SURFACE_0};
    color: {TEXT_SECONDARY};
    border-top: 1px solid {BORDER};
}}
QSplitter::handle {{
    background: {BORDER};
}}
"""


def apply_theme(app) -> None:
    """Apply OmniBand dark theme to a QApplication."""
    from PySide6.QtGui import QFont, QPalette, QColor
    from PySide6.QtCore import Qt

    app.setStyleSheet(MAIN_STYLESHEET)

    palette = QPalette()
    palette.setColor(QPalette.Window,          QColor(SURFACE_0))
    palette.setColor(QPalette.WindowText,      QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.Base,            QColor(SURFACE_1))
    palette.setColor(QPalette.AlternateBase,   QColor(SURFACE_2))
    palette.setColor(QPalette.ToolTipBase,     QColor(SURFACE_2))
    palette.setColor(QPalette.ToolTipText,     QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.Text,            QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.Button,          QColor(SURFACE_2))
    palette.setColor(QPalette.ButtonText,      QColor(TEXT_PRIMARY))
    palette.setColor(QPalette.BrightText,      QColor(ACCENT))
    palette.setColor(QPalette.Highlight,       QColor(ACCENT))
    palette.setColor(QPalette.HighlightedText, QColor(SURFACE_0))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT_DISABLED))
    app.setPalette(palette)

    font = QFont("Segoe UI", FONT_SIZE_MD)
    app.setFont(font)

