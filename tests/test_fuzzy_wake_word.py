"""
Unit & Regression Tests for Chirkoot Fuzzy Wake-Word Engine.
Validates Indian English, Hindi, and Hinglish pronunciation matching,
rejection of everyday conversational words as standalone triggers,
confidence thresholding, cooldown gating, and mute controls.
"""

import sys
import unittest
from pathlib import Path

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from wake_word_engine import FuzzyWakeWordMatcher, LocalWakeWordDetector


class TestFuzzyWakeWord(unittest.TestCase):

    def setUp(self):
        self.matcher = FuzzyWakeWordMatcher

    def test_distinctive_english_and_hinglish_pronunciations(self):
        """Test variations of Chirkoot and Chitrakoot pronunciations in English & Hinglish."""
        phrases = [
            "Hey Chirkoot",
            "chirkoot",
            "CHITRAKOOT",
            "Chirkut",
            "OK Chirkoot",
            "Suno Chirkoot",
            "Hey Chitrakoot",
            "Jarvis",
            "hey jarvis"
        ]
        for phrase in phrases:
            res = self.matcher.match_utterance(phrase, threshold=0.70)
            self.assertTrue(
                res["matched"],
                f"Expected '{phrase}' to match authorized keyword, got: {res}"
            )
            self.assertGreaterEqual(res["confidence"], 0.70)

    def test_devanagari_and_hindi_wake_words(self):
        """Test Hindi / Devanagari wake word phrases."""
        hindi_phrases = [
            ("चिरकूट", "CHIRKOOT"),
            ("चित्रकूट", "CHITRAKOOT"),
            ("चिरकुट", "CHIRKUT"),
            ("जार्विस", "JARVIS")
        ]
        for dev, expected_kw in hindi_phrases:
            res = self.matcher.match_utterance(dev, threshold=0.70)
            self.assertTrue(
                res["matched"],
                f"Expected Devanagari '{dev}' to match '{expected_kw}', got: {res}"
            )
            self.assertEqual(res["keyword"], expected_kw)

    def test_strict_rejection_of_everyday_words(self):
        """Test that everyday conversational words are rejected as standalone triggers."""
        rejected = [
            "Hey", "Hi", "Hello", "OK", "Check out", "Circuit", "Short circuit",
            "Suno", "Namaste", "Arey", "Oye", "Bhai", "Sure", "Chair",
            "सुनो", "नमस्ते", "अरे", "ओए", "भाई"
        ]
        for word in rejected:
            res = self.matcher.match_utterance(word, threshold=0.70)
            self.assertFalse(
                res["matched"],
                f"Everyday word '{word}' was incorrectly accepted as a wake trigger!"
            )
            self.assertIsNotNone(res["rejected_reason"])

    def test_unrelated_speech_rejected(self):
        """Test that normal English vocabulary does not false-trigger."""
        unrelated = [
            "open visual studio code",
            "can you help me with this python script",
            "what is the weather today",
            "turn on dark mode",
            "download the file from git",
            "elephant",
            "computer"
        ]
        for phrase in unrelated:
            res = self.matcher.match_utterance(phrase, threshold=0.70)
            self.assertFalse(
                res["matched"],
                f"Unrelated phrase '{phrase}' should not match wake-word, got: {res}"
            )

    def test_cooldown_and_duplicate_suppression(self):
        """Test refractory period suppression on LocalWakeWordDetector."""
        detector = LocalWakeWordDetector(cooldown_sec=1.5)
        detector.reset()

        # Simulate first trigger
        detector.simulate_keyword_spot("CHIRKOOT")
        dummy_pcm = b"\x00" * 3200
        first_evt = detector.accept_waveform(dummy_pcm)
        self.assertIsNotNone(first_evt, "First wake event should succeed")
        self.assertEqual(first_evt["keyword"], "CHIRKOOT")

        # Immediate second trigger should be suppressed by cooldown
        detector.simulate_keyword_spot("CHIRKOOT")
        second_evt = detector.accept_waveform(dummy_pcm)
        self.assertIsNone(second_evt, "Immediate re-trigger must be suppressed during cooldown")

    def test_mute_control(self):
        """Test that muting suppresses all wake-word triggers."""
        detector = LocalWakeWordDetector()
        detector.reset()
        detector.set_mute(True)

        detector.simulate_keyword_spot("CHIRKOOT")
        dummy_pcm = b"\x00" * 3200
        evt = detector.accept_waveform(dummy_pcm)
        self.assertIsNone(evt, "Muted detector must return None even if keyword was spotted")

        detector.set_mute(False)
        detector.simulate_keyword_spot("CHIRKOOT")
        evt_unmuted = detector.accept_waveform(dummy_pcm)
        self.assertIsNotNone(evt_unmuted, "Unmuted detector must resume spotting")


if __name__ == "__main__":
    unittest.main()
