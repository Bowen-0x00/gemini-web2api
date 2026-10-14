"""Unit tests for ChromeDriver bridge module."""

import os
import unittest
from unittest.mock import MagicMock, patch

from bridge.chrome_driver import ChromeDriverBridge, find_chromedriver


class TestChromeDriverBridge(unittest.TestCase):

    def test_find_chromedriver_locates_binary(self):
        """Verify that find_chromedriver finds the project or system driver."""
        driver_path = find_chromedriver()
        self.assertTrue(os.path.exists(driver_path))
        self.assertTrue(os.path.isfile(driver_path))

    def test_detect_model_fallback(self):
        """Test detect_model returns preferred model when driver is None or errors."""
        bridge = ChromeDriverBridge(server_url="http://127.0.0.1:38765", preferred_model="gemini-4-argon")
        # Without initialized driver, it should safely return the preferred model
        self.assertEqual(bridge.detect_model(), "gemini-4-argon")

    def test_bridge_initialization_options(self):
        """Verify ChromeDriverBridge attributes and options."""
        bridge = ChromeDriverBridge(
            server_url="http://127.0.0.1:38765",
            debugger_address="127.0.0.1:9222",
            preferred_model="gemini-4-argon",
        )
        self.assertEqual(bridge.server_url, "http://127.0.0.1:38765")
        self.assertEqual(bridge.debugger_address, "127.0.0.1:9222")
        self.assertEqual(bridge.preferred_model, "gemini-4-argon")


if __name__ == "__main__":
    unittest.main()
