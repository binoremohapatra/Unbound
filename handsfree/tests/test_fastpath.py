"""Unit tests for FastPath head pose to analog stick translation."""

import unittest
from handsfree.core.calibration import CalibrationManager
from handsfree.core.fastpath import FastPath


class TestFastPath(unittest.TestCase):

    def setUp(self):
        self.cal = CalibrationManager()
        self.fastpath = FastPath(calibration=self.cal)

    def test_neutral_pose_outputs_zero(self):
        intent_x, intent_y = self.fastpath.process_pose(raw_yaw=0.0, raw_pitch=0.0, timestamp=1.0)
        self.assertEqual(intent_x.value, 0.0)
        self.assertEqual(intent_y.value, 0.0)

    def test_deadzone_suppression(self):
        # Within deadzone (3.0 degrees), should output 0.0
        intent_x, intent_y = self.fastpath.process_pose(raw_yaw=2.0, raw_pitch=-1.5, timestamp=1.0)
        self.assertEqual(intent_x.value, 0.0)
        self.assertEqual(intent_y.value, 0.0)

    def test_steering_direction(self):
        # Large yaw to the right
        intent_x, _ = self.fastpath.process_pose(raw_yaw=20.0, raw_pitch=0.0, timestamp=1.0)
        self.assertGreater(intent_x.value, 0.5)


if __name__ == "__main__":
    unittest.main()

