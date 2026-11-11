"""Unit tests for CursorTracker & Tracky-Mouse DwellClicker (Gameface + Tracky Mouse)."""

import unittest
from handsfree.core.cursor_tracker import CursorTracker, DwellClicker, DwellAction, DwellState


class TestCursorTracker(unittest.TestCase):

    def setUp(self):
        self.tracker = CursorTracker(
            sensitivity_x=30.0,
            sensitivity_y=30.0,
            curve_exponent=1.5,
            deadzone_deg=1.0,
            screen_width=1920,
            screen_height=1080,
            enable_dwell=True,
            dwell_time_ms=500.0,
        )

    def test_deadzone_filtering(self):
        # Within deadzone (<= 1.0 deg), cursor movement should be zero
        dx, dy, (cx, cy), dwell = self.tracker.process_head_pose(raw_yaw=0.8, raw_pitch=-0.5, timestamp=1.0)
        self.assertEqual(dx, 0)
        self.assertEqual(dy, 0)

    def test_acceleration_curve(self):
        # Small angle above deadzone
        dx1, _, _, _ = self.tracker.process_head_pose(raw_yaw=2.0, raw_pitch=0.0, timestamp=1.0)
        self.tracker.filter_yaw.reset()

        # Large angle above deadzone
        dx2, _, _, _ = self.tracker.process_head_pose(raw_yaw=8.0, raw_pitch=0.0, timestamp=2.0)
        self.assertGreater(abs(dx2), abs(dx1) * 3)

    def test_screen_clamping(self):
        # Move far right
        for i in range(50):
            self.tracker.process_head_pose(raw_yaw=30.0, raw_pitch=0.0, timestamp=float(i))
        self.assertLessEqual(self.tracker.cursor_x, 1919.0)

    def test_recenter_neutral(self):
        self.tracker.set_neutral(yaw=15.0, pitch=10.0)
        dx, dy, _, _ = self.tracker.process_head_pose(raw_yaw=15.0, raw_pitch=10.0, timestamp=100.0)
        self.assertEqual(dx, 0)
        self.assertEqual(dy, 0)


class TestDwellClicker(unittest.TestCase):

    def setUp(self):
        self.triggered_actions = []

        def on_action(act, x, y):
            self.triggered_actions.append((act, x, y))

        self.dwell = DwellClicker(
            dwell_time_ms=400.0,
            dwell_radius=15.0,
            cooldown_ms=200.0,
            on_action=on_action,
        )

    def test_dwell_progresses_and_triggers(self):
        t0 = 1000.0
        # First point sets anchor
        s1 = self.dwell.update(100.0, 100.0, timestamp=t0)
        self.assertEqual(s1.state, DwellState.DWELLING)
        self.assertEqual(s1.progress, 0.0)

        # Hovering at same point after 200ms -> 50%
        s2 = self.dwell.update(101.0, 100.0, timestamp=t0 + 0.2)
        self.assertEqual(s2.state, DwellState.DWELLING)
        self.assertAlmostEqual(s2.progress, 0.5, places=1)

        # Reaching 400ms -> triggers click
        s3 = self.dwell.update(100.5, 100.0, timestamp=t0 + 0.41)
        self.assertEqual(len(self.triggered_actions), 1)
        self.assertEqual(self.triggered_actions[0][0], DwellAction.LEFT_CLICK)
        self.assertEqual(s3.state, DwellState.COOLDOWN)

    def test_moving_outside_radius_resets_dwell(self):
        t0 = 1000.0
        self.dwell.update(100.0, 100.0, timestamp=t0)
        self.dwell.update(102.0, 100.0, timestamp=t0 + 0.2)

        # Move outside 15px radius -> resets anchor
        s = self.dwell.update(150.0, 100.0, timestamp=t0 + 0.3)
        self.assertEqual(s.progress, 0.0)
        self.assertEqual(len(self.triggered_actions), 0)

    def test_drag_toggle_mode(self):
        self.dwell.set_action(DwellAction.DRAG_TOGGLE)
        self.assertFalse(self.dwell.is_dragging)

        # Trigger dwell
        self.dwell.update(50.0, 50.0, timestamp=1.0)
        self.dwell.update(50.0, 50.0, timestamp=1.5)
        self.assertTrue(self.dwell.is_dragging)


if __name__ == "__main__":
    unittest.main()

