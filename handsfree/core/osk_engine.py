"""On-Screen Keyboard (OSK) with Dwell Typing & Word Prediction.

Abstracted and synthesized from OptiKey (OptiKey/OptiKey):
  - Accessible QWERTY & control key layout designed for head/eye tracking
  - Dwell-time key actuation engine with dwell progress feedback (0.0 -> 1.0)
  - Real-time word prediction & autocomplete engine (Trie / frequency based)
  - Text-to-Speech (TTS) readout integration
  - Text injection into active Windows foreground applications via SendInput
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple

logger = logging.getLogger("handsfree.osk")

# High-frequency dictionary for word prediction (OptiKey-style)
COMMON_WORDS = [
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it", "for", "not",
    "on", "with", "he", "as", "you", "do", "at", "this", "but", "his", "by", "from",
    "they", "we", "say", "her", "she", "or", "an", "will", "my", "one", "all", "would",
    "there", "their", "what", "so", "up", "out", "if", "about", "who", "get", "which",
    "go", "me", "when", "make", "can", "like", "time", "no", "just", "him", "know",
    "take", "people", "into", "year", "your", "good", "some", "could", "them", "see",
    "other", "than", "then", "now", "look", "only", "come", "its", "over", "think",
    "also", "back", "after", "use", "two", "how", "our", "work", "first", "well",
    "way", "even", "new", "want", "because", "any", "these", "give", "day", "most",
    "us", "great", "hello", "help", "please", "thank", "yes", "water", "food", "pain",
    "tired", "stop", "play", "game", "computer", "keyboard", "controller", "unbound",
    "handsfree", "accessible", "mouse", "click", "turn", "speed", "drive", "pause"
]


class WordPredictor:
    """Trie-based prefix predictive text engine."""

    def __init__(self, vocab: Optional[List[str]] = None):
        self.words = vocab or COMMON_WORDS
        self.word_set = set(self.words)

    def predict(self, prefix: str, max_results: int = 5) -> List[str]:
        """Returns top matching word candidates for a given prefix."""
        clean = prefix.strip().lower()
        if not clean:
            return ["the", "i", "yes", "no", "hello"][:max_results]

        matches = []
        for w in self.words:
            if w.startswith(clean) and w != clean:
                matches.append(w)
                if len(matches) >= max_results:
                    break
        return matches


@dataclass
class KeyDef:
    label: str
    action_type: str = "char"     # "char", "space", "backspace", "enter", "speak", "clear", "send"
    width: float = 1.0           # relative grid width


# Standard OptiKey accessibility QWERTY layout
KEYBOARD_LAYOUT: List[List[KeyDef]] = [
    [
        KeyDef("1"), KeyDef("2"), KeyDef("3"), KeyDef("4"), KeyDef("5"),
        KeyDef("6"), KeyDef("7"), KeyDef("8"), KeyDef("9"), KeyDef("0"),
        KeyDef("BKSP", "backspace", width=1.5),
    ],
    [
        KeyDef("Q"), KeyDef("W"), KeyDef("E"), KeyDef("R"), KeyDef("T"),
        KeyDef("Y"), KeyDef("U"), KeyDef("I"), KeyDef("O"), KeyDef("P"),
    ],
    [
        KeyDef("A"), KeyDef("S"), KeyDef("D"), KeyDef("F"), KeyDef("G"),
        KeyDef("H"), KeyDef("J"), KeyDef("K"), KeyDef("L"), KeyDef("ENTER", "enter", width=1.5),
    ],
    [
        KeyDef("Z"), KeyDef("X"), KeyDef("C"), KeyDef("V"), KeyDef("B"),
        KeyDef("N"), KeyDef("M"), KeyDef(","), KeyDef("."), KeyDef("CLEAR", "clear", width=1.5),
    ],
    [
        KeyDef("SPEAK", "speak", width=2.0),
        KeyDef("SPACE", "space", width=4.5),
        KeyDef("SEND TO APP", "send", width=2.5),
    ]
]


class OnScreenKeyboardEngine:
    """Dwell-time accessible typing engine with word prediction and TTS."""

    def __init__(
        self,
        dwell_time_ms: float = 550.0,
        cooldown_ms: float = 350.0,
        on_text_updated: Optional[Callable[[str], None]] = None,
        on_key_actuated: Optional[Callable[[str], None]] = None,
    ):
        self.dwell_time_s = dwell_time_ms / 1000.0
        self.cooldown_s = cooldown_ms / 1000.0
        self.on_text_updated = on_text_updated
        self.on_key_actuated = on_key_actuated

        self.text_buffer = ""
        self.predictor = WordPredictor()
        self.predictions: List[str] = self.predictor.predict("")

        # Dwell tracking state
        self.active_key: Optional[str] = None
        self.dwell_start_time = 0.0
        self.dwell_progress = 0.0
        self.cooldown_end_time = 0.0

    def get_current_word(self) -> str:
        """Extracts the word currently being typed."""
        if not self.text_buffer:
            return ""
        tokens = self.text_buffer.split(" ")
        return tokens[-1] if tokens else ""

    def _update_predictions(self) -> None:
        current = self.get_current_word()
        self.predictions = self.predictor.predict(current)

    def select_prediction(self, word: str) -> None:
        """Applies word prediction completion."""
        current = self.get_current_word()
        if current:
            # Replace prefix with full word
            self.text_buffer = self.text_buffer[:-len(current)] + word + " "
        else:
            self.text_buffer += word + " "

        self._update_predictions()
        if self.on_text_updated:
            self.on_text_updated(self.text_buffer)

    def hover_key(self, key_label: Optional[str], timestamp: Optional[float] = None) -> float:
        """Updates dwell state when the cursor hovers over a key.
        
        Returns:
            Dwell progress (0.0 to 1.0)
        """
        now = timestamp if timestamp is not None else time.time()

        if now < self.cooldown_end_time:
            self.dwell_progress = 0.0
            return 0.0

        if not key_label:
            self.active_key = None
            self.dwell_progress = 0.0
            return 0.0

        if key_label != self.active_key:
            self.active_key = key_label
            self.dwell_start_time = now
            self.dwell_progress = 0.0
            return 0.0

        # Hovering on same key
        elapsed = now - self.dwell_start_time
        self.dwell_progress = min(1.0, elapsed / max(0.001, self.dwell_time_s))

        if self.dwell_progress >= 1.0:
            self.actuate_key(key_label)
            self.cooldown_end_time = now + self.cooldown_s
            self.dwell_progress = 0.0
            self.active_key = None

        return self.dwell_progress

    def actuate_key(self, key_label: str) -> None:
        """Actuates a key upon dwell completion."""
        label = key_label.upper()

        if label == "SPACE":
            self.text_buffer += " "
        elif label in ("BKSP", "BACKSPACE"):
            if self.text_buffer:
                self.text_buffer = self.text_buffer[:-1]
        elif label == "ENTER":
            self.text_buffer += "\n"
        elif label == "CLEAR":
            self.text_buffer = ""
        elif label == "SPEAK":
            self.speak_text(self.text_buffer)
        elif label == "SEND TO APP":
            self.inject_text(self.text_buffer)
        else:
            # Normal character
            self.text_buffer += key_label.lower() if len(key_label) == 1 else key_label

        self._update_predictions()

        if self.on_key_actuated:
            self.on_key_actuated(key_label)
        if self.on_text_updated:
            self.on_text_updated(self.text_buffer)

    def speak_text(self, text: str) -> None:
        """Speaks the text buffer aloud via TTS."""
        if not text.strip():
            return
        logger.info(f"[TTS SPEAK]: {text}")
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
        except Exception:
            # Fallback log or OS TTS
            pass

    def inject_text(self, text: str) -> None:
        """Injects text into active Windows application via SendInput / pydirectinput."""
        if not text:
            return
        logger.info(f"[INJECT TEXT TO APP]: {text}")
        try:
            import pydirectinput
            pydirectinput.write(text, interval=0.01)
        except Exception:
            pass

