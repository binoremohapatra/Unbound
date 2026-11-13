"""somatic_keyboard.py - Hands-Free Somatic QWERTY Keyboard with Grid Scanning & Dwell.

Features:
- QWERTY + Numbers + Punctuation + Special keys
- Row 0: Top-3 live word prediction + Space, Backspace, Enter
- Mode A (4-Channel Grid Scanner):
    CH1 (Head Left / F7)  -> Move Left
    CH2 (Head Right / F8) -> Move Right
    CH3 (Head Up / F9)    -> Move Down to Next Row
    CH4 Single (Jaw Drop / F10) -> Type Highlighted Key
    CH4 Double (F10 Double)     -> Type Spacebar
    CH4 Hold   (F10 Hold >700ms)-> Type Backspace
- Mode B (Webcam Head-Mouse Dwell Mode):
    Hover on key for 0.9s -> Dwell ring animation -> Clicks key!
- Target window integration: sends directly into active window or live text preview.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QGridLayout
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QColor

from host.omniband.app.theme import (
    SURFACE_1, SURFACE_2, SURFACE_3, BURNT_ORANGE, DARK_BURNT_ORANGE,
    STATUS_OK, STATUS_ERR, INDUSTRIAL_IVORY, WARM_GRAPHITE, PURE_WHITE, FONT_MONO
)

# Common 200 English / Coding Words dictionary for real-time word prediction
DICTIONARY = [
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it", "for", "not", "on", "with",
    "he", "as", "you", "do", "at", "this", "but", "his", "by", "from", "they", "we", "say", "her",
    "she", "or", "an", "will", "my", "one", "all", "would", "there", "their", "what", "so", "up",
    "out", "if", "about", "who", "get", "which", "go", "me", "when", "make", "can", "like", "time",
    "no", "just", "him", "know", "take", "people", "into", "year", "your", "good", "some", "could",
    "them", "see", "other", "than", "then", "now", "look", "only", "come", "its", "over", "think",
    "also", "back", "after", "use", "two", "how", "our", "work", "first", "well", "way", "even",
    "new", "want", "because", "any", "these", "give", "day", "most", "us", "def", "class", "import",
    "return", "self", "true", "false", "none", "print", "while", "for", "in", "break", "continue",
    "try", "except", "finally", "async", "await", "function", "const", "let", "var", "window",
    "document", "console", "log", "error", "event", "string", "number", "boolean", "null", "undefined",
    "cyberpunk", "omnicontrol", "unbound", "somatic", "engine", "copilot", "hardware", "sendinput",
]


class SomaticKeyboardWidget(QWidget):
    """Hands-Free Somatic Keyboard with Grid Scanning & Dwell."""

    sig_key_typed = Signal(str)
    sig_dismiss = Signal()

    def __init__(
        self,
        on_type_fn: Optional[Callable[[str], None]] = None,
        on_dismiss_fn: Optional[Callable[[], None]] = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.on_type_fn = on_type_fn
        self.on_dismiss_fn = on_dismiss_fn

        # Layout definitions
        self.grid_rows = [
            ["[PRED_1]", "[PRED_2]", "[PRED_3]", "SPACE", "BACKSPACE", "ENTER"],
            ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "="],
            ["Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "[", "]"],
            ["A", "S", "D", "F", "G", "H", "J", "K", "L", ";", "'"],
            ["Z", "X", "C", "V", "B", "N", "M", ",", ".", "/", "SHIFT", "CLEAR", "▼ HIDE"],
        ]

        # Scanner cursor position
        self.curr_row = 1
        self.curr_col = 0
        self.shift_active = False

        # Live buffer
        self.live_text = ""
        self.curr_word = ""

        # Key buttons storage
        self.buttons: List[List[QPushButton]] = []

        self._build_ui()
        self._update_highlight()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # Header instructions
        hdr = QLabel("⌨️ SOMATIC ZERO-HAND KEYBOARD (DIRECT IN-PLACE TYPING)")
        hdr.setFont(QFont("Segoe UI", 10, QFont.Bold))
        hdr.setStyleSheet(f"color: {BURNT_ORANGE};")
        layout.addWidget(hdr)

        # Non-focusable status banner (NO internal QLineEdit or QTextEdit trap!)
        self.status_pill = QLabel("🟢 DIRECT INJECTION ACTIVE — KEYSTROKES ENTER TARGET APPLICATION CARET")
        self.status_pill.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.status_pill.setStyleSheet(
            f"background: {SURFACE_1}; color: {INDUSTRIAL_IVORY}; "
            "border: 1px solid #444; border-radius: 4px; padding: 4px 8px;"
        )
        self.status_pill.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_pill)

        # Keyboard Grid
        grid_widget = QWidget()
        grid_widget.setFocusPolicy(Qt.NoFocus)
        grid_layout = QVBoxLayout(grid_widget)
        grid_layout.setSpacing(4)
        grid_layout.setContentsMargins(0, 4, 0, 0)

        for r_idx, row in enumerate(self.grid_rows):
            row_layout = QHBoxLayout()
            row_layout.setSpacing(4)
            row_btns = []
            for c_idx, key in enumerate(row):
                btn = QPushButton(key)
                btn.setFocusPolicy(Qt.NoFocus)
                btn.setMinimumHeight(44)
                btn.setFont(QFont("Cascadia Code", 10, QFont.Bold))
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {SURFACE_2};
                        color: {INDUSTRIAL_IVORY};
                        border: 1px solid #444;
                        border-radius: 4px;
                    }}
                    QPushButton:hover {{
                        background: {SURFACE_3};
                        border-color: {BURNT_ORANGE};
                    }}
                """)
                btn.clicked.connect(lambda _, k=key: self._on_key_clicked(k))
                row_layout.addWidget(btn)
                row_btns.append(btn)
            self.buttons.append(row_btns)
            grid_layout.addLayout(row_layout)

        layout.addWidget(grid_widget)
        self._refresh_predictions()

    def _update_highlight(self) -> None:
        """Update visual highlight cursor on active grid key."""
        for r_idx, row in enumerate(self.buttons):
            for c_idx, btn in enumerate(row):
                is_selected = (r_idx == self.curr_row and c_idx == self.curr_col)
                if is_selected:
                    btn.setStyleSheet(f"""
                        QPushButton {{
                            background: {BURNT_ORANGE};
                            color: #111;
                            font-weight: bold;
                            border: 2px solid {PURE_WHITE};
                            border-radius: 4px;
                        }}
                    """)
                else:
                    btn.setStyleSheet(f"""
                        QPushButton {{
                            background: {SURFACE_2};
                            color: {INDUSTRIAL_IVORY};
                            border: 1px solid #444;
                            border-radius: 4px;
                        }}
                    """)

    # --- Grid Navigation (Mode A) ---
    def move_left(self) -> None:
        max_col = len(self.grid_rows[self.curr_row])
        self.curr_col = (self.curr_col - 1) % max_col
        self._update_highlight()

    def move_right(self) -> None:
        max_col = len(self.grid_rows[self.curr_row])
        self.curr_col = (self.curr_col + 1) % max_col
        self._update_highlight()

    def move_down_row(self) -> None:
        self.curr_row = (self.curr_row + 1) % len(self.grid_rows)
        max_col = len(self.grid_rows[self.curr_row])
        if self.curr_col >= max_col:
            self.curr_col = max_col - 1
        self._update_highlight()

    def type_selected(self) -> None:
        key = self.grid_rows[self.curr_row][self.curr_col]
        self._on_key_clicked(key)

    def _on_key_clicked(self, key: str) -> None:
        if key.startswith("[PRED_"):
            pred_text = self.buttons[0][int(key[6]) - 1].text()
            if pred_text and not pred_text.startswith("["):
                # If prediction completes current prefix, send only remaining characters + space
                if self.curr_word and pred_text.lower().startswith(self.curr_word.lower()):
                    suffix = pred_text[len(self.curr_word):] + " "
                    self.live_text += suffix
                    if self.on_type_fn:
                        self.on_type_fn(suffix)
                    self.sig_key_typed.emit(suffix)
                else:
                    # Otherwise backspace the current prefix and inject full word + space
                    for _ in range(len(self.curr_word)):
                        if self.on_type_fn:
                            self.on_type_fn("backspace")
                    if self.curr_word:
                        self.live_text = self.live_text[:-len(self.curr_word)]
                    full = pred_text + " "
                    self.live_text += full
                    if self.on_type_fn:
                        self.on_type_fn(full)
                    self.sig_key_typed.emit(full)
                self.curr_word = ""
                self._refresh_predictions()
            return
        elif key == "SPACE":
            self.live_text += " "
            self.curr_word = ""
            self._refresh_predictions()
            if self.on_type_fn:
                self.on_type_fn(" ")
            self.sig_key_typed.emit(" ")
            return
        elif key == "BACKSPACE":
            if self.live_text:
                self.live_text = self.live_text[:-1]
            if self.curr_word:
                self.curr_word = self.curr_word[:-1]
            self._refresh_predictions()
            if self.on_type_fn:
                self.on_type_fn("backspace")
            self.sig_key_typed.emit("backspace")
            return
        elif key == "ENTER":
            self.live_text += "\n"
            self.curr_word = ""
            self._refresh_predictions()
            if self.on_type_fn:
                self.on_type_fn("\n")
            self.sig_key_typed.emit("\n")
            return
        elif key == "CLEAR":
            self.live_text = ""
            self.curr_word = ""
            self._refresh_predictions()
            return
        elif key == "SHIFT":
            self.shift_active = not self.shift_active
            return
        elif key in ("HIDE", "▼ HIDE"):
            self.sig_dismiss.emit()
            if self.on_dismiss_fn:
                self.on_dismiss_fn()
            return
        else:
            char = key.lower() if not self.shift_active else key.upper()
            self.live_text += char
            if char.isalnum():
                self.curr_word += char.lower()
            else:
                self.curr_word = ""
            self._refresh_predictions()
            if self.on_type_fn:
                self.on_type_fn(char)
            self.sig_key_typed.emit(char)

    def _refresh_predictions(self) -> None:
        """Update Row 0 with top-3 matching prefix predictions."""
        preds = []
        if self.curr_word:
            for w in DICTIONARY:
                if w.startswith(self.curr_word) and w != self.curr_word:
                    preds.append(w)
                    if len(preds) == 3:
                        break

        for i in range(3):
            btn = self.buttons[0][i]
            if i < len(preds):
                btn.setText(preds[i])
                btn.setEnabled(True)
            else:
                btn.setText(f"[P{i+1}]")
                btn.setEnabled(False)
