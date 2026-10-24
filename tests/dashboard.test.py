#!/usr/bin/env python3
"""Exercise multi-profile rate limits dashboard, formatting, and quota caching."""

import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dashboard", ROOT / "bin" / "codex-switch-dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class DashboardUnitTests(unittest.TestCase):
    def test_live_rpc_and_silent_timeout(self):
        with tempfile.TemporaryDirectory(prefix="codex rpc spaces ") as tmpdir:
            root = Path(tmpdir)
            (root / ".codex-work").mkdir()
            (root / ".codex-work" / "auth.json").write_text('{}')
            stub = root / "rpc.py"
            stub.write_text('''import json, os, sys, time
if os.environ.get("RPC_TEST_SILENT"):
    time.sleep(60)
assert not os.environ.get("OPENAI_API_KEY")
assert os.environ["CODEX_HOME"].endswith(".codex-work")
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialized":
        initialized = True
    if "id" not in request:
        continue
    ident = request["id"]
    if ident == 1:
        result = {}
    elif ident == 2:
        assert initialized
        result = {"account": {"email": "test@example.com", "planType": "plus"}}
    else:
        assert initialized
        if os.environ.get("RPC_TEST_SECRET"):
            print(json.dumps({"id": ident, "error": {"message": "unauthorized 401 refresh_token=SECRET"}}), flush=True)
            continue
        result = {"rateLimits": {"primary": {"usedPercent": 25}}}
    print(json.dumps({"id": ident, "result": result}), flush=True)
''', encoding="utf-8")
            if os.name == "nt":
                launcher = root / "codex.cmd"
                launcher.write_text(f'@echo off\n"{sys.executable}" "{stub}" %*\n')
            else:
                launcher = root / "codex"
                launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n')
                launcher.chmod(0o755)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "must-be-scrubbed"}):
                result = dashboard.query_codex_profile("work", tmpdir, str(launcher), timeout=5)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["email"], "test@example.com")
            self.assertEqual(result["rate_limits"]["rateLimits"]["primary"]["usedPercent"], 25)
            with patch.dict(os.environ, {"RPC_TEST_SECRET": "1"}):
                result = dashboard.query_codex_profile("work", tmpdir, str(launcher), timeout=0.2)
                self.assertEqual(result["status"], "auth_expired")
                self.assertNotIn("SECRET", json.dumps(result))
            with patch.dict(os.environ, {"RPC_TEST_SILENT": "1"}):
                start = time.monotonic()
                result = dashboard.query_codex_profile("work", tmpdir, str(launcher), timeout=0.2)
                self.assertLess(time.monotonic() - start, 5)
                self.assertEqual(result["status"], "partial")
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    start = time.monotonic()
                    dashboard.diagnose(str(launcher), "work", timeout=0.2)
                self.assertLess(time.monotonic() - start, 5)
                self.assertEqual(stderr.getvalue(), "")

    def test_queries_wait_for_initialize_response(self):
        process = Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()

        def lines(*args):
            sent = process.stdin.getvalue().decode().splitlines()
            self.assertEqual([json.loads(line)["method"] for line in sent], ["initialize"])
            yield process.stdout, '{"id":1,"result":{}}'
            sent = process.stdin.getvalue().decode().splitlines()
            self.assertEqual([json.loads(line)["method"] for line in sent],
                             ["initialize", "initialized", "account/read", "account/rateLimits/read"])
            yield process.stdout, '{"id":2,"result":{"account":{"email":"test@example.com"}}}'
            yield process.stdout, '{"id":3,"result":{"rateLimits":{}}}'

        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, ".codex-work").mkdir()
            Path(tmpdir, ".codex-work", "auth.json").write_text('{}')
            with patch.object(dashboard.subprocess, "Popen", return_value=process), \
                    patch.object(dashboard, "process_lines", side_effect=lines), \
                    patch.object(dashboard, "stop_process"):
                result = dashboard.query_codex_profile("work", tmpdir, "codex")
            self.assertEqual(result["status"], "ok")

    def test_unverified_auth_is_never_ready_or_recommended(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, ".codex-work").mkdir()
            with patch.object(dashboard, "query_codex_profile", return_value={"status": "partial"}):
                data = dashboard.fetch_all_profiles_dashboard(tmpdir, "codex", no_cache=True)
            self.assertIsNone(data["summary"]["recommended"])
            profile = data["profiles"][0]
            self.assertEqual(profile["readiness"], "offline")
            self.assertEqual(profile["plan_label"], "Plan unknown")

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
