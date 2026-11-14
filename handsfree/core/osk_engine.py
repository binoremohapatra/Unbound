"""On-Screen Keyboard (OSK) with Dwell Typing, Smart Prediction & Accessibility.

Abstracted and synthesized from OptiKey:
  - Accessible QWERTY & control key layout designed for head/eye tracking
  - Dwell-time key actuation engine with dwell progress feedback (0.0 -> 1.0)
  - Real-time AI smart prediction: prefix autocomplete, context-aware next-word suggestions
  - Quick phrase library & persistent personal dictionary
  - Text-to-Speech (TTS) readout integration with multi-backend fallback
  - Text injection into active Windows foreground applications
"""

from __future__ import annotations

from collections import Counter
import json
import logging
from pathlib import Path
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple

logger = logging.getLogger("handsfree.osk")

# High-frequency dictionary for word prediction
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
    "handsfree", "accessible", "mouse", "click", "turn", "speed", "drive", "pause",
    "urgently", "ready", "doctor", "medicine", "open", "close", "fine", "call"
]

QUICK_PHRASES = [
    "I need help.",
    "Please call someone.",
    "I am fine.",
    "I need water.",
    "Please open this.",
    "I want to communicate.",
    "Thank you.",
    "Yes please.",
    "No thank you.",
    "Can you assist me?"
]

# Context-aware n-gram transitions
CONTEXT_PREDICTIONS = {
    ("how", "are"): ["you", "we", "things", "they"],
    ("how",): ["are", "is", "do", "can", "much"],
    ("i", "need", "help"): ["please", "urgently", "now", "here"],
    ("i", "need"): ["help", "water", "food", "rest", "medicine", "someone"],
    ("i", "am"): ["fine", "okay", "tired", "ready", "here", "good"],
    ("i", "want"): ["to", "water", "help", "food", "this"],
    ("please"): ["help", "call", "open", "come", "wait", "give"],
    ("thank"): ["you", "so", "very"],
    ("can", "you"): ["help", "please", "call", "open", "read"],
    ("open"): ["google", "youtube", "notepad", "keyboard", "settings"],
}


class WordPredictor:
    """Trie-based prefix predictive text engine (Original OptiKey-style, maintained for compat)."""

    def __init__(self, vocab: Optional[List[str]] = None):
        self.words = list(vocab) if vocab else list(COMMON_WORDS)
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


class SmartKeyboardEngine:
    """Upgraded AI Smart Prediction Engine with context-awareness, autocomplete,

    frequently-used phrases, and persistent personal vocabulary.
    """

    PERSONAL_DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "personal_dict.json"

    def __init__(self, vocab: Optional[List[str]] = None):
        self.base_vocab: List[str] = list(vocab) if vocab else list(COMMON_WORDS)
        self.personal_words: List[str] = []
        self.quick_phrases: List[str] = list(QUICK_PHRASES)
        self.usage_counts: Counter[str] = Counter()
        self._load_personal_dictionary()

    def _load_personal_dictionary(self) -> None:
        try:
            if self.PERSONAL_DICT_PATH.exists():
                data = json.loads(self.PERSONAL_DICT_PATH.read_text(encoding="utf-8"))
                self.personal_words = [w.lower().strip() for w in data.get("custom_words", []) if w]
                saved_phrases = data.get("quick_phrases", [])
                if saved_phrases:
                    self.quick_phrases = saved_phrases
        except Exception as e:
            logger.warning(f"Could not load personal dictionary: {e}")

    def save_personal_dictionary(self) -> None:
        try:
            self.PERSONAL_DICT_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "custom_words": sorted(list(set(self.personal_words))),
                "quick_phrases": self.quick_phrases,
            }
            self.PERSONAL_DICT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error saving personal dictionary: {e}")

    def add_word(self, word: str) -> bool:
        clean = word.strip().lower()
        if clean and clean not in self.personal_words:
            self.personal_words.append(clean)
            self.save_personal_dictionary()
            return True
        return False

    def remove_word(self, word: str) -> bool:
        clean = word.strip().lower()
        if clean in self.personal_words:
            self.personal_words.remove(clean)
            self.save_personal_dictionary()
            return True
        return False

    def record_selection(self, word: str) -> None:
        clean = word.strip().lower()
        if clean:
            self.usage_counts[clean] += 1

    def predict(self, text_or_prefix: str, max_results: int = 5) -> List[str]:
        """Provides dynamic 3-5 word suggestions considering context & current word prefix."""
        text = text_or_prefix.lower()
        tokens = text.strip().split()
        is_trailing_space = text.endswith(" ") or not text.strip()
        current_token = "" if is_trailing_space else (tokens[-1] if tokens else "")

        results: List[str] = []

        # 1. Autocomplete current prefix if actively typing a word
        all_vocab = self.personal_words + self.base_vocab
        if current_token:
            prefix_matches = [
                w for w in all_vocab
                if w.startswith(current_token) and w != current_token
            ]
            # Prioritize by usage frequency then length
            prefix_matches.sort(key=lambda w: (-self.usage_counts[w], len(w)))
            results.extend(prefix_matches)

        # 2. Context-aware next-word suggestions
        # Check trigram/bigram context
        context_words = tokens if is_trailing_space else tokens[:-1]
        if len(context_words) >= 3:
            trigram = tuple(context_words[-3:])
            if trigram in CONTEXT_PREDICTIONS:
                results.extend(CONTEXT_PREDICTIONS[trigram])
        if len(context_words) >= 2:
            bigram = tuple(context_words[-2:])
            if bigram in CONTEXT_PREDICTIONS:
                results.extend(CONTEXT_PREDICTIONS[bigram])
        if len(context_words) >= 1:
            unigram = (context_words[-1],)
            if unigram in CONTEXT_PREDICTIONS:
                results.extend(CONTEXT_PREDICTIONS[unigram])

        # 3. Frequency-based fallbacks
        fallbacks = ["the", "i", "help", "water", "please", "yes", "no", "thank", "you"]
        results.extend(fallbacks)

        # Deduplicate while preserving order
        deduped: List[str] = []
        seen = set()
        for r in results:
            if r and r not in seen and r != current_token:
                seen.add(r)
                deduped.append(r)
                if len(deduped) >= max_results:
                    break

        return deduped


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
    """Dwell-time accessible typing engine with AI smart prediction, quick phrases and TTS."""

    def __init__(
        self,
        dwell_time_ms: float = 1500.0,
        cooldown_ms: float = 350.0,
        on_text_updated: Optional[Callable[[str], None]] = None,
        on_key_actuated: Optional[Callable[[str], None]] = None,
    ):
        self.dwell_time_s = dwell_time_ms / 1000.0
        self.cooldown_s = cooldown_ms / 1000.0
        self.on_text_updated = on_text_updated
        self.on_key_actuated = on_key_actuated

        self.text_buffer = ""
        self.smart_predictor = SmartKeyboardEngine()
        # Keep predictor attribute pointing to SmartKeyboardEngine for full compatibility
        self.predictor = self.smart_predictor
        self.predictions: List[str] = self.smart_predictor.predict("")

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
        self.predictions = self.smart_predictor.predict(self.text_buffer)

    def select_prediction(self, word: str) -> None:
        """Applies word prediction completion."""
        clean_word = word.strip()
        current = self.get_current_word()
        if current:
            # Replace prefix with full word
            self.text_buffer = self.text_buffer[:-len(current)] + clean_word + " "
        else:
            if self.text_buffer and not self.text_buffer.endswith(" "):
                self.text_buffer += " "
            self.text_buffer += clean_word + " "

        self.smart_predictor.record_selection(clean_word)
        self._update_predictions()
        if self.on_text_updated:
            self.on_text_updated(self.text_buffer)

    def apply_quick_phrase(self, phrase: str) -> None:
        """Appends a quick preset phrase to the text buffer."""
        clean = phrase.strip()
        if not clean:
            return
        if self.text_buffer and not self.text_buffer.endswith(" "):
            self.text_buffer += " "
        self.text_buffer += clean + " "
        self._update_predictions()
        if self.on_text_updated:
            self.on_text_updated(self.text_buffer)

    def add_personal_word(self, word: str) -> bool:
        ok = self.smart_predictor.add_word(word)
        self._update_predictions()
        return ok

    def remove_personal_word(self, word: str) -> bool:
        ok = self.smart_predictor.remove_word(word)
        self._update_predictions()
        return ok

    def get_quick_phrases(self) -> List[str]:
        return self.smart_predictor.quick_phrases

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
        """Speaks text aloud via TTS with multi-tier fallback."""
        if not text.strip():
            return
        clean_text = text.strip()
        logger.info(f"[TTS SPEAK]: {clean_text}")

        # Attempt 1: pyttsx3
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.say(clean_text)
            engine.runAndWait()
            return
        except Exception:
            pass

        # Attempt 2: Windows PowerShell SAPI (Standard on Windows 10/11)
        try:
            safe_text = clean_text.replace("'", "''").replace('"', '`"')
            cmd = f"$voice = New-Object -ComObject SAPI.SpVoice; $voice.Speak('{safe_text}')"
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            pass

    def inject_text(self, text: str) -> None:
        """Injects text into active Windows application via SendInput / pydirectinput."""
        if not text:
            return
        logger.info(f"[INJECT TEXT TO APP]: {text}")
        try:
            import pydirectinput
            pydirectinput.write(text, interval=0.01)
            return
        except Exception:
            pass

        # Native Windows SendInput fallback
        try:
            import ctypes
            for char in text:
                vk = ctypes.windll.user32.VkKeyScanW(ord(char))
                if vk != -1:
                    code = vk & 0xFF
                    ctypes.windll.user32.keybd_event(code, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(code, 0, 2, 0)
        except Exception as e:
            logger.debug(f"Native SendInput fallback: {e}")
