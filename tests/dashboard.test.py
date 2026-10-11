#!/usr/bin/env python3
"""Exercise multi-profile rate limits dashboard, formatting, and web endpoints."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dashboard", ROOT / "bin" / "codex-switch-dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class DashboardUnitTests(unittest.TestCase):
    def test_window_label_formatting(self):
        self.assertEqual(dashboard.format_window_label(300), "5-Hour Limit")
        self.assertEqual(dashboard.format_window_label(10080), "Weekly Limit")
        self.assertEqual(dashboard.format_window_label(60), "1-Hour Limit")
        self.assertEqual(dashboard.format_window_label(2880), "2-Day Limit")
        self.assertEqual(dashboard.format_window_label(45), "45-Min Limit")

    def test_countdown_formatting(self):
        now = 1000000
        self.assertEqual(dashboard.format_countdown(now - 10, now), "Ready now")
        self.assertEqual(dashboard.format_countdown(now + 120, now), "in 2m")
        self.assertEqual(dashboard.format_countdown(now + 7300, now), "in 2h 1m")
        self.assertEqual(dashboard.format_countdown(now + 90000, now), "in 1d 1h")

    def test_progress_bar_rendering(self):
        bar_no_color = dashboard.render_progress_bar(50, width=10, color=False)
        self.assertIn("50%", bar_no_color)
        self.assertEqual(bar_no_color, "[█████░░░░░]  50%")

        bar_zero = dashboard.render_progress_bar(0, width=10, color=False)
        self.assertEqual(bar_zero, "[░░░░░░░░░░]   0%")

        bar_hundred = dashboard.render_progress_bar(100, width=10, color=False)
        self.assertEqual(bar_hundred, "[██████████] 100%")

    def test_valid_profile_names(self):
        self.assertTrue(dashboard.valid_profile_name("work"))
        self.assertTrue(dashboard.valid_profile_name("client.a"))
        self.assertTrue(dashboard.valid_profile_name("user-2"))
        self.assertFalse(dashboard.valid_profile_name("dashboard"))
        self.assertFalse(dashboard.valid_profile_name("home"))
        self.assertFalse(dashboard.valid_profile_name("switch"))
        self.assertFalse(dashboard.valid_profile_name(""))
        self.assertFalse(dashboard.valid_profile_name(".dot"))

    def test_data_normalization_and_badges(self):
        now = 1791690000
        raw_ready = {
            "status": "ok",
            "email": "user@example.com",
            "plan_type": "plus",
            "rate_limits": {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "primary": {"usedPercent": 15, "windowDurationMins": 300, "resetsAt": now + 3600},
                    "secondary": {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": now + 86400},
                },
                "rateLimitResetCredits": {"availableCount": 1},
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            norm = dashboard.normalize_profile_data("work", raw_ready, "work", tmpdir, now)
            self.assertEqual(norm["name"], "work")
            self.assertTrue(norm["is_active"])
            self.assertEqual(norm["readiness"], "ready")
            self.assertEqual(norm["badge_text"], "READY")
            self.assertEqual(norm["badge_color"], "green")
            self.assertEqual(norm["primary"]["used_percent"], 15)
            self.assertEqual(norm["primary"]["remaining_percent"], 85)
            self.assertEqual(norm["reset_credits"], 1)

    def test_data_normalization_rate_limited(self):
        now = 1791690000
        raw_limited = {
            "status": "ok",
            "email": "user@example.com",
            "plan_type": "plus",
            "rate_limits": {
                "ordinaryUsageAllowed": False,
                "rateLimits": {
                    "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": now + 1800},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": now + 86400},
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            norm = dashboard.normalize_profile_data("work", raw_limited, "personal", tmpdir, now)
            self.assertFalse(norm["is_active"])
            self.assertEqual(norm["readiness"], "rate_limited")
            self.assertEqual(norm["badge_text"], "RATE LIMITED")
            self.assertEqual(norm["badge_color"], "red")

    def test_data_normalization_high_usage(self):
        now = 1791690000
        raw_high = {
            "status": "ok",
            "email": "user@example.com",
            "plan_type": "plus",
            "rate_limits": {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "primary": {"usedPercent": 85, "windowDurationMins": 300, "resetsAt": now + 1800},
                    "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": now + 86400},
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            norm = dashboard.normalize_profile_data("personal", raw_high, "personal", tmpdir, now)
            self.assertEqual(norm["readiness"], "high_usage")
            self.assertEqual(norm["badge_text"], "HIGH USAGE")
            self.assertEqual(norm["badge_color"], "yellow")

    def test_web_server_endpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Set up mock profiles in tmpdir
            os.makedirs(os.path.join(tmpdir, ".codex"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex", ".codex-switch-active"), "w") as f:
                f.write("work\n")

            os.makedirs(os.path.join(tmpdir, ".codex-work"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex-work", "auth.json"), "w") as f:
                f.write('{"auth_mode":"chatgpt"}\n')

            os.makedirs(os.path.join(tmpdir, ".codex-personal"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex-personal", "auth.json"), "w") as f:
                f.write('{"auth_mode":"chatgpt"}\n')

            # Start temporary server on ephemeral port
            dashboard.DashboardHandler.home_dir = tmpdir
            dashboard.DashboardHandler.codex_bin = "nonexistent-codex"

            import http.server
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()

            try:
                base_url = f"http://127.0.0.1:{port}"
                # Test GET /
                with urllib.request.urlopen(f"{base_url}/", timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                    html = resp.read().decode("utf-8")
                    self.assertIn("codex-switch", html)
                    self.assertIn("Profile Dashboard", html)

                # Test GET /api/status
                with urllib.request.urlopen(f"{base_url}/api/status", timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(data.get("active_profile"), "work")
                    names = [p["name"] for p in data.get("profiles", [])]
                    self.assertIn("work", names)
                    self.assertIn("personal", names)

                # Test POST /api/switch
                switch_payload = json.dumps({"profile": "personal"}).encode("utf-8")
                req = urllib.request.Request(
                    f"{base_url}/api/switch",
                    data=switch_payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                    switch_res = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(switch_res.get("success"))
                    self.assertEqual(switch_res.get("active"), "personal")

                # Verify marker changed on disk
                with open(os.path.join(tmpdir, ".codex", ".codex-switch-active")) as f:
                    self.assertEqual(f.read().strip(), "personal")

            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
