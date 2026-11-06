"""Tests for hierarchical StateMachine, layers, latches, and intent filtering."""

import unittest
from handsfree.core.events import Intent
from handsfree.core.state_machine import StateMachine, SystemState


class TestStateMachine(unittest.TestCase):

    def setUp(self):
        self.sm = StateMachine(initial_state=SystemState.ACTIVE)

    def test_state_transitions(self):
        self.assertEqual(self.sm.current_state, SystemState.ACTIVE)
        self.sm.transition_to(SystemState.PAUSED)
        self.assertEqual(self.sm.current_state, SystemState.PAUSED)

    def test_layer_management(self):
        self.assertEqual(self.sm.active_layer, "base")
        self.sm.set_layer("shift")
        self.assertEqual(self.sm.active_layer, "shift")

        intent = Intent(action="jawOpen", value=1.0)
        filtered = self.sm.filter_intent(intent)
        self.assertIsNotNone(filtered)
        self.assertEqual(filtered.layer, "shift")

    def test_latching(self):
        self.assertFalse(self.sm.is_latched("cruise_control"))
        toggled = self.sm.toggle_latch("cruise_control")
        self.assertTrue(toggled)
        self.assertTrue(self.sm.is_latched("cruise_control"))

        toggled = self.sm.toggle_latch("cruise_control")
        self.assertFalse(toggled)
        self.assertFalse(self.sm.is_latched("cruise_control"))

    def test_allowed_intent_table(self):
        steer_intent = Intent(action="stick_x", value=0.5)
        accelerate_intent = Intent(action="accelerate", value=1.0)

        self.assertTrue(self.sm.is_intent_allowed(steer_intent))
        self.assertTrue(self.sm.is_intent_allowed(accelerate_intent))

        self.sm.transition_to(SystemState.PAUSED)
        self.assertFalse(self.sm.is_intent_allowed(steer_intent))
        self.assertFalse(self.sm.is_intent_allowed(accelerate_intent))
        self.assertIsNone(self.sm.filter_intent(steer_intent))

        unpause_intent = Intent(action="unpause", value=1.0)
        self.assertTrue(self.sm.is_intent_allowed(unpause_intent))
        self.assertIsNotNone(self.sm.filter_intent(unpause_intent))


if __name__ == "__main__":
    unittest.main()

