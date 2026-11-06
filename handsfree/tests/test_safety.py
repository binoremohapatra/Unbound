"""Tests for safety mechanisms: Watchdog heartbeat timeouts and Gateway release_all."""

import time
import unittest
from handsfree.core.gateway import OutputGateway
from handsfree.core.mapping import OutputCommand
from handsfree.core.watchdog import Watchdog


class TestSafetyMechanisms(unittest.TestCase):

    def setUp(self):
        self.gateway = OutputGateway(dry_run=True)

    def test_gateway_tracks_and_releases_all(self):
        self.gateway.dispatch(OutputCommand(device="gamepad", command_type="button", target="A", value=1.0))
        self.gateway.dispatch(OutputCommand(device="gamepad", command_type="button", target="X", value=1.0))
        self.assertIn("A", self.gateway.pressed_buttons)
        self.assertIn("X", self.gateway.pressed_buttons)

        self.gateway.release_all()
        self.assertEqual(len(self.gateway.pressed_buttons), 0)
        self.assertEqual(len(self.gateway.pressed_keys), 0)

    def test_watchdog_triggers_emergency_release_on_timeout(self):
        timeout_called = False

        def on_timeout():
            nonlocal timeout_called
            timeout_called = True

        watchdog = Watchdog(gateway=self.gateway, timeout_seconds=0.1, on_timeout=on_timeout)
        self.gateway.dispatch(OutputCommand(device="gamepad", command_type="button", target="A", value=1.0))
        self.assertIn("A", self.gateway.pressed_buttons)

        watchdog.start()
        try:
            watchdog.beat()
            time.sleep(0.04)
            watchdog.beat()
            self.assertFalse(watchdog.is_tripped)
            self.assertIn("A", self.gateway.pressed_buttons)

            time.sleep(0.18)

            self.assertTrue(watchdog.is_tripped)
            self.assertTrue(timeout_called)
            self.assertEqual(len(self.gateway.pressed_buttons), 0)
        finally:
            watchdog.stop()


if __name__ == "__main__":
    unittest.main()

