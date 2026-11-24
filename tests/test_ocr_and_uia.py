"""
Unit & Integration Tests for Chirkoot MK-V OCR Normalization, UIA Engine & Set-of-Mark Grounding.
Validates RapidOCR coordinate normalization, UIA active window inspection, and
stale token detection in Set-of-Mark (SoM) visual grounding.
"""

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from tools import _format_ocr_results
from desktop_automation import (
    UiaEngine,
    SetOfMarkEngine,
    MarkedElement,
    SoMObservation,
    GLOBAL_SOM_ENGINE
)


class TestOcrAndUia(unittest.TestCase):

    def test_ocr_result_formatting_and_normalization(self):
        """Test formatting OCR boxes and normalizing centers to 0-1000 coordinates."""
        # Simulated OCR result from RapidOCR: list of [box_points, text, confidence]
        # Box is 4 points: top-left, top-right, bottom-right, bottom-left
        # In a 1920x1080 screen:
        # Box center at x=960 (norm=500), y=540 (norm=500)
        box = [[910, 520], [1010, 520], [1010, 560], [910, 560]]
        mock_results = [
            (box, "Submit Order", 0.985)
        ]

        formatted = _format_ocr_results(mock_results, img_w=1920, img_h=1080)
        self.assertIn("Submit Order", formatted)
        self.assertIn("conf: 0.98", formatted)
        self.assertIn("Center: x=500, y=500", formatted)
        self.assertIn("<UNTRUSTED_OBSERVED_CONTENT", formatted)

    def test_uia_active_window_handle_query(self):
        """Test querying active window HWND via Win32 API."""
        hwnd = UiaEngine.get_active_window_handle()
        # On interactive desktop, active window handle is non-negative int
        if hwnd is not None:
            self.assertIsInstance(hwnd, int)
            self.assertGreater(hwnd, 0)

    def test_set_of_mark_token_validation_and_stale_rejection(self):
        """Test SetOfMarkEngine token binding and rejection of stale or wrong observation tokens."""
        engine = SetOfMarkEngine()

        marked_el = MarkedElement(
            mark_id=1,
            name="Save Button",
            control_type="ButtonControl",
            bounding_box=(100, 200, 200, 250),
            center=(150, 225),
            normalized_center=(150, 225),
            source="uia"
        )
        obs = SoMObservation(
            observation_token="som_fresh_token_123",
            timestamp=1000.0,
            hwnd=12345,
            window_rect=(0, 0, 800, 600),
            window_title="Active Window",
            marks={1: marked_el}
        )
        engine.active_observation = obs

        # 1. Valid token resolution
        valid, mark, msg = engine.validate_and_resolve_mark(1, "som_fresh_token_123")
        self.assertTrue(valid)
        self.assertIsNotNone(mark)
        self.assertEqual(mark.name, "Save Button")

        # 2. Stale / wrong token rejection
        invalid, mark_none, msg_stale = engine.validate_and_resolve_mark(1, "som_stale_old_token_999")
        self.assertFalse(invalid)
        self.assertIsNone(mark_none)
        self.assertIn("STALE OBSERVATION", msg_stale)

        # 3. Missing mark ID rejection
        invalid_mid, _, msg_missing = engine.validate_and_resolve_mark(99, "som_fresh_token_123")
        self.assertFalse(invalid_mid)
        self.assertIn("not found in observation", msg_missing)


if __name__ == "__main__":
    unittest.main()
