"""Reject app bundles which can draw Qt widgets but cannot receive macOS keys."""
import unittest
from scripts.smoke_app import validate_macos_gui


class MacBundleTests(unittest.TestCase):
    def test_console_worker_must_not_make_gui_background_only(self):
        broken = {"CFBundleExecutable": "Benchmark", "LSBackgroundOnly": True}
        with self.assertRaisesRegex(AssertionError, "keyboard focus"):
            validate_macos_gui(broken)
        validate_macos_gui({**broken, "LSBackgroundOnly": False, "LSUIElement": False})

    def test_hidden_agent_or_console_entrypoint_is_not_regular_gui(self):
        with self.assertRaises(AssertionError):
            validate_macos_gui({"CFBundleExecutable": "Benchmark", "LSUIElement": True})
        with self.assertRaises(AssertionError):
            validate_macos_gui({"CFBundleExecutable": "Benchmark-worker"})
