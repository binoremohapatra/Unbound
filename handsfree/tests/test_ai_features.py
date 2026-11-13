"""Unit and integration tests for Unbound Handsfree AI Features:
  - Feature 1: AI Voice Command Engine & Command Router
  - Feature 2: AI Screen Understanding Assistant
  - Feature 3: AI Software Guardian & Auto-Recovery
  - Feature 4: AI Adaptive Personalization
  - Feature 5: AI Smart Suggested Keyboard
"""

import unittest
import time
from unittest.mock import MagicMock

from handsfree.core.osk_engine import SmartKeyboardEngine, OnScreenKeyboardEngine
from handsfree.core.command_router import CommandRouter
from handsfree.core.voice_engine import VoiceCommandEngine
from handsfree.core.screen_understanding import ScreenUnderstandingService, ScreenElement
from handsfree.core.guardian import SoftwareGuardian
from handsfree.core.personalization import AdaptivePersonalizationService, UserProfile


class TestAISmartKeyboard(unittest.TestCase):
    def setUp(self):
        self.engine = SmartKeyboardEngine()

    def test_prefix_autocomplete(self):
        res = self.engine.predict("unb")
        self.assertIn("unbound", res)

    def test_context_aware_bigrams(self):
        res = self.engine.predict("how are ")
        self.assertIn("you", res)

    def test_context_aware_trigrams(self):
        res = self.engine.predict("i need help ")
        self.assertIn("please", res)

    def test_personal_dictionary_add_remove(self):
        test_word = "zephyrcortex"
        self.engine.add_word(test_word)
        self.assertIn(test_word, self.engine.personal_words)
        preds = self.engine.predict("zephyr")
        self.assertIn(test_word, preds)

        self.engine.remove_word(test_word)
        self.assertNotIn(test_word, self.engine.personal_words)

    def test_osk_quick_phrase(self):
        osk = OnScreenKeyboardEngine()
        osk.apply_quick_phrase("I need water.")
        self.assertTrue(osk.text_buffer.startswith("I need water."))


class TestAICommandRouter(unittest.TestCase):
    def setUp(self):
        self.spoken_feedback = []
        self.router = CommandRouter(
            tts_speaker=lambda msg: self.spoken_feedback.append(msg),
            cooldown_seconds=1.0,
        )

    def test_allowlist_browser_command(self):
        ok, feedback, key = self.router.route("open google")
        self.assertTrue(ok)
        self.assertEqual(key, "open google")
        self.assertIn("Opening Google.", self.spoken_feedback)

    def test_search_pattern_extraction(self):
        ok, feedback, key = self.router.route("search for quantum computing")
        self.assertTrue(ok)
        self.assertEqual(key, "search")
        self.assertIn("Searching for quantum computing.", feedback)

    def test_dedup_prevention(self):
        ok1, _, _ = self.router.route("open youtube")
        self.assertTrue(ok1)
        ok2, feedback2, _ = self.router.route("open youtube")
        self.assertFalse(ok2)
        self.assertIn("Duplicate suppressed", feedback2)

    def test_disallow_arbitrary_commands(self):
        ok, feedback, _ = self.router.route("format c: /fs:ntfs")
        self.assertFalse(ok)
        self.assertIn("Command not recognized", feedback)


class TestAIScreenUnderstanding(unittest.TestCase):
    def setUp(self):
        self.spoken = []
        self.service = ScreenUnderstandingService(tts_speaker=lambda msg: self.spoken.append(msg))

    def test_element_quadrant_description(self):
        elem_top_right = ScreenElement("Search", "button", x=1600, y=50, width=80, height=30)
        desc = elem_top_right.get_quadrant_description(screen_w=1920, screen_h=1080)
        self.assertIn("top-right", desc)

        elem_center = ScreenElement("Confirm", "button", x=900, y=500, width=120, height=40)
        desc_center = elem_center.get_quadrant_description(screen_w=1920, screen_h=1080)
        self.assertIn("center", desc_center)

    def test_read_screen_fallback(self):
        summary = self.service.read_screen()
        self.assertIsInstance(summary, str)
        self.assertTrue(len(summary) > 0)


class TestAISoftwareGuardian(unittest.TestCase):
    def test_heartbeat_and_disconnect_recovery(self):
        gateway_mock = MagicMock()
        spoken = []
        guardian = SoftwareGuardian(
            gateway_ref=gateway_mock,
            tts_speaker=lambda msg: spoken.append(msg),
            hardware_timeout_s=0.2,
            recovery_stability_s=0.1,
        )
        guardian.start()
        try:
            # 1. Feed stable heartbeats
            guardian.record_hardware_heartbeat()
            time.sleep(0.12)
            guardian.record_hardware_heartbeat()
            self.assertTrue(guardian.hardware_connected)
            self.assertEqual(guardian.active_mode, "wearable_primary")

            # 2. Let hardware stall past timeout
            time.sleep(0.25)
            # Guardian loop should detect stall, trigger release_all and engage fallback
            self.assertFalse(guardian.hardware_connected)
            self.assertEqual(guardian.active_mode, "webcam_fallback")
            gateway_mock.release_all.assert_called()
            self.assertTrue(any("interrupted" in s for s in spoken))
        finally:
            guardian.stop()


class TestAIAdaptivePersonalization(unittest.TestCase):
    def setUp(self):
        self.service = AdaptivePersonalizationService()
        self.service.pending_recommendations.clear()

    def test_dwell_miss_heuristic(self):
        self.service.profile.dwell_time_ms = 1500.0
        self.service._recent_dwell_misses = 0

        # Record 4 misses
        for _ in range(4):
            self.service.record_dwell_miss()

        self.service._last_analysis_time = 0.0
        self.service._evaluate_heuristics()

        self.assertTrue(len(self.service.pending_recommendations) > 0)
        rec = self.service.pending_recommendations[0]
        self.assertEqual(rec["setting"], "dwell_time_ms")
        self.assertGreater(rec["recommended"], 1500.0)

        # User approval
        ok = self.service.apply_recommendation(rec["id"])
        self.assertTrue(ok)
        self.assertGreater(self.service.profile.dwell_time_ms, 1500.0)


if __name__ == "__main__":
    unittest.main()

