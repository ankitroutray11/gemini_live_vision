"""
Unit tests for Chirkoot DPI & Multi-Monitor Coordinate Mapping System.
Validates 0-1000 normalized to physical pixel coordinate mapping,
window-relative mapping, roundtrip consistency, and multi-monitor boundaries.
"""

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from desktop_automation import DpiCoordinateManager, VirtualScreenMetrics


class TestCoordinateMapping(unittest.TestCase):

    def test_window_relative_coordinate_mapping(self):
        """Test mapping 0-1000 within a specific target window bounding box."""
        # Window at left=100, top=200, right=900 (width=800), bottom=800 (height=600)
        target_rect = (100, 200, 900, 800)

        # Top-left corner
        px, py = DpiCoordinateManager.normalized_to_physical(0, 0, target_rect=target_rect)
        self.assertEqual((px, py), (100, 200))

        # Center
        px, py = DpiCoordinateManager.normalized_to_physical(500, 500, target_rect=target_rect)
        self.assertEqual((px, py), (500, 500))

        # Bottom-right corner
        px, py = DpiCoordinateManager.normalized_to_physical(1000, 1000, target_rect=target_rect)
        self.assertEqual((px, py), (900, 800))

        # 25% X, 75% Y
        px, py = DpiCoordinateManager.normalized_to_physical(250, 750, target_rect=target_rect)
        self.assertEqual((px, py), (300, 650))

    def test_clamping_out_of_bounds_inputs(self):
        """Test that negative and out-of-range coordinates are clamped to [0, 1000]."""
        target_rect = (0, 0, 1920, 1080)

        # Negative should clamp to 0
        px, py = DpiCoordinateManager.normalized_to_physical(-100, -500, target_rect=target_rect)
        self.assertEqual((px, py), (0, 0))

        # > 1000 should clamp to 1000
        px, py = DpiCoordinateManager.normalized_to_physical(1500, 2500, target_rect=target_rect)
        self.assertEqual((px, py), (1920, 1080))

    def test_roundtrip_coordinate_conversion(self):
        """Test physical -> normalized -> physical roundtrip precision."""
        target_rect = (50, 50, 1970, 1130)  # 1920x1080 window offset by 50px

        for norm_x in [0, 125, 333, 500, 750, 999, 1000]:
            for norm_y in [0, 250, 500, 750, 1000]:
                px, py = DpiCoordinateManager.normalized_to_physical(norm_x, norm_y, target_rect=target_rect)
                calc_nx, calc_ny = DpiCoordinateManager.physical_to_normalized(px, py, target_rect=target_rect)
                # Max 1 unit deviation due to integer rounding
                self.assertAlmostEqual(norm_x, calc_nx, delta=2)
                self.assertAlmostEqual(norm_y, calc_ny, delta=2)

    def test_degenerate_zero_size_rect_protection(self):
        """Test that degenerate 0-width or 0-height rects do not trigger division by zero."""
        degenerate_rect = (100, 100, 100, 100)
        px, py = DpiCoordinateManager.normalized_to_physical(500, 500, target_rect=degenerate_rect)
        self.assertEqual((px, py), (100, 100))

        nx, ny = DpiCoordinateManager.physical_to_normalized(100, 100, target_rect=degenerate_rect)
        self.assertEqual((nx, ny), (0, 0))

    def test_virtual_screen_metrics_query(self):
        """Test querying live Windows virtual screen metrics."""
        metrics = DpiCoordinateManager.get_virtual_screen_metrics()
        self.assertIsInstance(metrics, VirtualScreenMetrics)
        self.assertGreater(metrics.width, 0)
        self.assertGreater(metrics.height, 0)
        self.assertGreater(metrics.primary_width, 0)
        self.assertGreater(metrics.primary_height, 0)


if __name__ == "__main__":
    unittest.main()
