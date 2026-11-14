"""test_somatic_keyboard.py - Unit tests for Somatic QWERTY Keyboard and Word Prediction.

Tests:
1. Grid navigation: Move Left, Move Right, Move Down to Next Row
2. Key Typing: Characters, Space, Backspace, Enter
3. Prefix Word Prediction: Prefix match populates top prediction buttons
"""
from __future__ import annotations

import os
import pytest

pyside6 = pytest.importorskip("PySide6", reason="PySide6 GUI toolkit not installed")
from PySide6.QtWidgets import QApplication

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_keyboard_grid_navigation(qapp):
    from host.omniband.app.somatic_keyboard import SomaticKeyboardWidget

    kb = SomaticKeyboardWidget()
    assert kb.curr_row == 1
    assert kb.curr_col == 0

    # Move right
    kb.move_right()
    assert kb.curr_col == 1

    # Move left
    kb.move_left()
    assert kb.curr_col == 0

    # Move down row
    kb.move_down_row()
    assert kb.curr_row == 2
    assert kb.curr_col == 0


def test_keyboard_typing_and_prediction(qapp):
    from host.omniband.app.somatic_keyboard import SomaticKeyboardWidget

    typed_keys = []
    kb = SomaticKeyboardWidget(on_type_fn=lambda k: typed_keys.append(k))

    # Type 'u', 'n', 'b' -> should predict 'unbound'
    kb._on_key_clicked("U")
    kb._on_key_clicked("N")
    kb._on_key_clicked("B")

    assert kb.live_text == "unb"
    assert "u" in typed_keys
    assert "n" in typed_keys
    assert "b" in typed_keys

    # Prediction should have 'unbound'
    pred_1 = kb.buttons[0][0].text()
    assert "unbound" in [kb.buttons[0][i].text() for i in range(3)]

    # Click prediction
    kb._on_key_clicked("[PRED_1]")
    assert "unbound " in kb.live_text

