"""Unit tests for VirtualGamepad abstraction and OptiKey OSK Engine."""
import unittest
import time
from handsfree.core.vgamepad_gateway import VirtualGamepad, X360Button
from handsfree.core.osk_engine import OnScreenKeyboardEngine, WordPredictor, KEYBOARD_LAYOUT


class TestVirtualGamepad(unittest.TestCase):
    def setUp(self):
        self.pad = VirtualGamepad(dry_run=True)

    def test_button_press_and_release(self):
        self.pad.press_button("A")
        self.assertIn("A", self.pad.mock_state.buttons)
        self.pad.release_button("A")
        self.assertNotIn("A", self.pad.mock_state.buttons)

    def test_analog_stick_clamping(self):
        self.pad.set_left_joystick(1.5, -2.0)
        self.assertEqual(self.pad.mock_state.left_x, 32767)
        self.assertEqual(self.pad.mock_state.left_y, -32767)

    def test_triggers_clamping(self):
        self.pad.set_triggers(left=1.5, right=-0.5)
        self.assertEqual(self.pad.mock_state.left_trigger, 255)
        self.assertEqual(self.pad.mock_state.right_trigger, 0)

    def test_release_all(self):
        self.pad.press_button("X")
        self.pad.press_button("RB")
        self.pad.set_left_joystick(0.8, -0.5)
        self.pad.set_triggers(left=0.0, right=0.9)

        self.pad.release_all()
        self.assertEqual(len(self.pad.mock_state.buttons), 0)
        self.assertEqual(self.pad.mock_state.left_x, 0)
        self.assertEqual(self.pad.mock_state.right_trigger, 0)


class TestWordPredictor(unittest.TestCase):
    def setUp(self):
        self.predictor = WordPredictor(["hello", "help", "helmet", "world", "work", "worker"])

    def test_predict_prefix(self):
        res = self.predictor.predict("hel", max_results=3)
        self.assertEqual(res, ["hello", "help", "helmet"])

    def test_predict_empty(self):
        res = self.predictor.predict("", max_results=3)
        self.assertTrue(len(res) <= 3)

    def test_predict_no_match(self):
        res = self.predictor.predict("xyz", max_results=3)
        self.assertEqual(res, [])


class TestOnScreenKeyboardEngine(unittest.TestCase):
    def setUp(self):
        self.osk = OnScreenKeyboardEngine(dwell_time_ms=300.0, cooldown_ms=100.0)

    def test_direct_key_actuation(self):
        self.osk.actuate_key("H")
        self.osk.actuate_key("I")
        self.assertEqual(self.osk.text_buffer, "hi")

    def test_space_and_backspace(self):
        self.osk.actuate_key("A")
        self.osk.actuate_key("SPACE")
        self.osk.actuate_key("B")
        self.assertEqual(self.osk.text_buffer, "a b")
        self.osk.actuate_key("BKSP")
        self.assertEqual(self.osk.text_buffer, "a ")

    def test_clear(self):
        self.osk.actuate_key("A")
        self.osk.actuate_key("CLEAR")
        self.assertEqual(self.osk.text_buffer, "")

    def test_dwell_key_hover_and_trigger(self):
        t0 = 100.0
        # Hover begins on key 'E'
        p1 = self.osk.hover_key("E", timestamp=t0)
        self.assertEqual(p1, 0.0)

        # Hover halfway (0.15s into 0.3s)
        p2 = self.osk.hover_key("E", timestamp=t0 + 0.15)
        self.assertAlmostEqual(p2, 0.5, places=1)

        # Hover reaches dwell threshold (0.31s >= 0.30s)
        p3 = self.osk.hover_key("E", timestamp=t0 + 0.31)
        self.assertEqual(self.osk.text_buffer, "e")
        # After actuation, key resets
        self.assertIsNone(self.osk.active_key)

    def test_word_prediction_selection(self):
        self.osk.actuate_key("H")
        self.osk.actuate_key("E")
        self.osk.actuate_key("L")
        preds = self.osk.predictions
        self.assertTrue(len(preds) > 0)
        self.assertIn("hello", preds)
        self.osk.select_prediction("hello")
        self.assertEqual(self.osk.text_buffer, "hello ")


if __name__ == "__main__":
    unittest.main()
