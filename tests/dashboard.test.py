#!/usr/bin/env python3
"""Exercise multi-profile rate limits dashboard, formatting, and quota caching."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

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
        self.assertIn("50% left", bar_no_color)
        self.assertEqual(bar_no_color, "[█████░░░░░]  50% left")

        bar_zero = dashboard.render_progress_bar(0, width=10, color=False)
        self.assertEqual(bar_zero, "[░░░░░░░░░░]   0% left")

        bar_hundred = dashboard.render_progress_bar(100, width=10, color=False)
        self.assertEqual(bar_hundred, "[██████████] 100% left")

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

    def test_quota_caching_and_ttl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".codex"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex", ".codex-switch-active"), "w") as f:
                f.write("work\n")

            os.makedirs(os.path.join(tmpdir, ".codex-work"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex-work", "auth.json"), "w") as f:
                f.write('{"auth_mode":"chatgpt"}\n')

            cache_file = os.path.join(tmpdir, ".codex", ".quota-cache.json")
            self.assertFalse(os.path.isfile(cache_file))

            # Pre-seed cache to simulate fresh query
            dashboard.save_quota_cache(cache_file, {
                "work": {
                    "cached_at": time.time(),
                    "data": {
                        "status": "ok",
                        "email": "test@example.com",
                        "plan_type": "plus",
                        "rate_limits": {
                            "ordinaryUsageAllowed": True,
                            "rateLimits": {
                                "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": time.time() + 3600},
                            },
                        },
                    },
                }
            })
            self.assertTrue(os.path.isfile(cache_file))

            # Query should use cache without invoking nonexistent codex binary
            data = dashboard.fetch_all_profiles_dashboard(tmpdir, "nonexistent-codex", timeout=1, cache_ttl=60)
            work_prof = next(p for p in data["profiles"] if p["name"] == "work")
            self.assertEqual(work_prof["email"], "test@example.com")
            self.assertEqual(work_prof["primary"]["used_percent"], 10)

            # Query with no_cache=True should bypass cache
            data_fresh = dashboard.fetch_all_profiles_dashboard(tmpdir, "nonexistent-codex", timeout=1, no_cache=True)
            work_fresh = next(p for p in data_fresh["profiles"] if p["name"] == "work")
            self.assertEqual(work_fresh["readiness"], "offline")

    def test_symlink_profiles_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".codex"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex", ".codex-switch-active"), "w") as f:
                f.write("work\n")
            os.makedirs(os.path.join(tmpdir, ".codex-work"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex-work", "auth.json"), "w") as f:
                f.write('{"auth_mode":"chatgpt"}\n')
            os.symlink(os.path.join(tmpdir, ".codex-work"), os.path.join(tmpdir, ".codex-alias"))

            data = dashboard.fetch_all_profiles_dashboard(tmpdir, "nonexistent-codex")
            names = [p["name"] for p in data.get("profiles", [])]
            self.assertIn("work", names)
            self.assertNotIn("alias", names)

    def test_effective_reset_epoch(self):
        # Primary limited only
        p1 = {
            "primary": {"used_percent": 100, "resets_at": 1000},
            "secondary": {"used_percent": 20, "resets_at": 5000},
        }
        self.assertEqual(dashboard.get_effective_reset_epoch(p1), 1000)

        # Secondary limited only
        p2 = {
            "primary": {"used_percent": 50, "resets_at": 1000},
            "secondary": {"used_percent": 100, "resets_at": 5000},
        }
        self.assertEqual(dashboard.get_effective_reset_epoch(p2), 5000)

        # Both limited
        p3 = {
            "primary": {"used_percent": 100, "resets_at": 1000},
            "secondary": {"used_percent": 100, "resets_at": 5000},
        }
        self.assertEqual(dashboard.get_effective_reset_epoch(p3), 5000)

    def test_recommendation_when_all_rate_limited(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".codex"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex", ".codex-switch-active"), "w") as f:
                f.write("work\n")

            os.makedirs(os.path.join(tmpdir, ".codex-work"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex-work", "auth.json"), "w") as f:
                f.write('{"auth_mode":"chatgpt"}\n')

            os.makedirs(os.path.join(tmpdir, ".codex-personal"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex-personal", "auth.json"), "w") as f:
                f.write('{"auth_mode":"chatgpt"}\n')

            cache_file = os.path.join(tmpdir, ".codex", ".quota-cache.json")
            dashboard.save_quota_cache(cache_file, {
                "work": {
                    "cached_at": now,
                    "data": {
                        "status": "ok",
                        "rate_limits": {
                            "ordinaryUsageAllowed": False,
                            "rateLimits": {
                                "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": now + 3600},
                            },
                        },
                    },
                },
                "personal": {
                    "cached_at": now,
                    "data": {
                        "status": "ok",
                        "rate_limits": {
                            "ordinaryUsageAllowed": False,
                            "rateLimits": {
                                "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": now + 600},
                            },
                        },
                    },
                },
            })

            data = dashboard.fetch_all_profiles_dashboard(tmpdir, "nonexistent-codex", timeout=1)
            summ = data["summary"]
            self.assertEqual(summ["recommended"], "personal")
            self.assertEqual(summ["recommended_reason"], "reset_time")
            self.assertTrue("in 10m" in summ["recommended_detail"] or "in 9m" in summ["recommended_detail"])

    def test_switch_profile_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".codex"), exist_ok=True)
            active_file = os.path.join(tmpdir, ".codex", ".codex-switch-active")
            with open(active_file, "w") as f:
                f.write("work\n")

            os.makedirs(os.path.join(tmpdir, ".codex-personal"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex-personal", "auth.json"), "w") as f:
                f.write('{"auth_mode":"chatgpt"}\n')

            ok, err = dashboard.switch_profile_action("personal", tmpdir, "nonexistent-codex")
            self.assertTrue(ok)
            self.assertIsNone(err)

            with open(active_file, "r") as f:
                self.assertEqual(f.read().strip(), "personal")

            ok, err = dashboard.switch_profile_action("ghost", tmpdir, "nonexistent-codex")
            self.assertFalse(ok)
            self.assertIn("profile directory does not exist", err)

    def test_auto_rotate_handler(self):
        import io
        from contextlib import redirect_stdout

        now = time.time()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".codex"), exist_ok=True)
            active_file = os.path.join(tmpdir, ".codex", ".codex-switch-active")
            with open(active_file, "w") as f:
                f.write("work\n")

            os.makedirs(os.path.join(tmpdir, ".codex-work"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex-work", "auth.json"), "w") as f:
                f.write('{"auth_mode":"chatgpt"}\n')

            os.makedirs(os.path.join(tmpdir, ".codex-personal"), exist_ok=True)
            with open(os.path.join(tmpdir, ".codex-personal", "auth.json"), "w") as f:
                f.write('{"auth_mode":"chatgpt"}\n')

            cache_file = os.path.join(tmpdir, ".codex", ".quota-cache.json")
            dashboard.save_quota_cache(cache_file, {
                "work": {
                    "cached_at": now,
                    "data": {
                        "status": "ok",
                        "rate_limits": {
                            "ordinaryUsageAllowed": True,
                            "rateLimits": {
                                "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": now + 3600},
                            },
                        },
                    },
                },
                "personal": {
                    "cached_at": now,
                    "data": {
                        "status": "ok",
                        "rate_limits": {
                            "ordinaryUsageAllowed": True,
                            "rateLimits": {
                                "primary": {"usedPercent": 80, "windowDurationMins": 300, "resetsAt": now + 3600},
                            },
                        },
                    },
                },
            })

            data = dashboard.fetch_all_profiles_dashboard(tmpdir, "nonexistent-codex", timeout=1)
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    dashboard.handle_auto_rotate(data, tmpdir, "nonexistent-codex", as_json=False)
                self.assertEqual(cm.exception.code, 0)
            self.assertIn("already on best profile 'work'", buf.getvalue())

            with open(active_file, "w") as f:
                f.write("personal\n")
            data["active_profile"] = "personal"

            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    dashboard.handle_auto_rotate(data, tmpdir, "nonexistent-codex", as_json=False)
                self.assertEqual(cm.exception.code, 0)
            self.assertIn("Switched to Codex profile: work", buf.getvalue())
            with open(active_file, "r") as f:
                self.assertEqual(f.read().strip(), "work")

            with open(active_file, "w") as f:
                f.write("personal\n")
            data["active_profile"] = "personal"
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    dashboard.handle_auto_rotate(data, tmpdir, "nonexistent-codex", as_json=True)
                self.assertEqual(cm.exception.code, 0)
            res = json.loads(buf.getvalue())
            self.assertTrue(res["success"])
            self.assertTrue(res["switched"])
            self.assertEqual(res["active_profile"], "work")
            self.assertEqual(res["reason"], "quota")

    def test_recommendation_tie_breaks_favor_active_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".codex"), exist_ok=True)
            active_file = os.path.join(tmpdir, ".codex", ".codex-switch-active")
            with open(active_file, "w") as f:
                f.write("personal\n")

            for pname in ("gig", "personal", "work"):
                pdir = os.path.join(tmpdir, f".codex-{pname}")
                os.makedirs(pdir, exist_ok=True)
                with open(os.path.join(pdir, "auth.json"), "w") as f:
                    f.write('{"auth_mode":"chatgpt"}\n')

            now = time.time()
            cache_file = os.path.join(tmpdir, ".codex", ".quota-cache.json")
            dashboard.save_quota_cache(cache_file, {
                pname: {
                    "cached_at": now,
                    "data": {"status": "ok", "error": "workspace routing discovery timed out"},
                }
                for pname in ("gig", "personal", "work")
            })

            data = dashboard.fetch_all_profiles_dashboard(tmpdir, "nonexistent-codex", timeout=1)
            self.assertEqual(data["summary"]["recommended"], "personal")


if __name__ == "__main__":
    unittest.main()
