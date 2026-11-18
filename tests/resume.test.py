#!/usr/bin/env python3
"""Shared discovery, picker and safe launch behavior without real credentials."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("resume", ROOT / "bin/codex-switch-resume.py")
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)
FIRST = "01a1d6f2-6db7-76b0-a8dc-0c68fdbea1bf"
SECOND = "01a1d6cc-c948-7112-b236-cb02006ecb71"


class ResumeTests(unittest.TestCase):
    def test_discovery_pages_across_accounts_and_scrubs_environment(self):
        with tempfile.TemporaryDirectory() as root:
            active = Path(root, ".codex")
            profile = Path(root, ".codex-work")
            profile.mkdir()
            (profile / "auth.json").write_text('{"tokens":{"refresh_token":"SECRET"}}')
            process = Mock(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())

            def sent():
                return [json.loads(line) for line in process.stdin.getvalue().splitlines()]

            def lines(*args):
                self.assertEqual(sent()[-1]["method"], "initialize")
                yield process.stdout, '{"id":1,"result":{}}'
                self.assertEqual(sent()[-2]["method"], "initialized")
                for request in (sent()[-1],):
                    self.assertEqual(request["method"], "thread/list")
                    self.assertFalse(request["params"]["currentAccountOnly"])
                    self.assertNotIn("cwd", request["params"])
                    self.assertEqual(request["params"]["modelProviders"], [])
                yield process.stdout, json.dumps({"id": 2, "result": {"data": [
                    {"id": FIRST, "name": "Review B10 file bindings", "cwd": "/research", "updatedAt": 2},
                ], "nextCursor": "page-two"}})
                self.assertEqual(sent()[-1]["params"]["cursor"], "page-two")
                yield process.stdout, json.dumps({"id": 3, "result": {"data": [
                    {"id": SECOND, "name": "Other conversation", "cwd": "/other", "updatedAt": 1},
                ], "nextCursor": None}})

            ambient = {name: "SECRET" for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
                       "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_FEDERATION_RULE_ID")}
            with patch.dict(os.environ, ambient), \
                    patch.object(resume.subprocess, "Popen", return_value=process) as spawn, \
                    patch.object(resume.dashboard, "process_lines", side_effect=lines), \
                    patch.object(resume.dashboard, "stop_process"):
                results = resume.list_conversations(active, "work", "codex")
                environment = spawn.call_args.kwargs["env"]
                self.assertEqual(environment["CODEX_HOME"], str(profile))
                self.assertEqual(environment["CODEX_SQLITE_HOME"], str(active))
                for name in ambient:
                    self.assertNotIn(name, environment)
            self.assertEqual([r["id"] for r in results], [FIRST, SECOND])
            self.assertNotIn("SECRET", json.dumps(results))

    def test_linked_credentials_are_rejected_before_spawn(self):
        if os.name == "nt":
            self.skipTest("POSIX symlink creation")
        with tempfile.TemporaryDirectory() as root:
            active = Path(root, ".codex")
            profile = Path(root, ".codex-work")
            profile.mkdir()
            target = Path(root, "original-auth")
            target.write_text("SECRET")
            (profile / "auth.json").symlink_to(target)
            with patch.object(resume.subprocess, "Popen") as spawn:
                with self.assertRaises(ValueError):
                    resume.list_conversations(active, "work", "codex")
                spawn.assert_not_called()

    def test_timeout_and_rpc_errors_cannot_become_partial_success(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".codex-work").mkdir()
            Path(root, ".codex-work/auth.json").write_text("{}")
            for messages in ([], [{"id": 1, "error": {"message": "SECRET"}}]):
                process = Mock(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
                events = [(process.stdout, json.dumps(m)) for m in messages]
                with patch.object(resume.subprocess, "Popen", return_value=process), \
                        patch.object(resume.dashboard, "process_lines", return_value=(event for event in events)), \
                        patch.object(resume.dashboard, "stop_process"):
                    with self.assertRaises(RuntimeError) as caught:
                        resume.list_conversations(Path(root, ".codex"), "work", "codex")
                    self.assertNotIn("SECRET", str(caught.exception))

    def test_search_picker_and_cancel(self):
        conversations = [
            {"id": FIRST, "name": "Review B10 file bindings", "cwd": "/research", "updated_at": 2},
            {"id": SECOND, "name": "Another review", "cwd": "/other", "updated_at": 1},
        ]
        self.assertEqual(resume.search_conversations(conversations, "B10 BINDINGS"), conversations[:1])
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=["/B10 bindings", "1"]):
            self.assertEqual(resume.pick_conversation(conversations), FIRST)
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", return_value="q"):
            self.assertIsNone(resume.pick_conversation(conversations))
        self.assertNotIn("\x1b", resume.clean_text("Malicious\x1b[2J\nTitle"))

    def test_json_does_not_launch_or_change_selection(self):
        conversations = [{"id": FIRST, "name": "Review B10 file bindings", "cwd": "/research", "updated_at": 2}]
        with tempfile.TemporaryDirectory() as root:
            active = Path(root, ".codex")
            active.mkdir()
            marker = active / ".codex-switch-active"
            marker.write_text("personal\n")
            output = io.StringIO()
            with patch.object(resume, "list_conversations", return_value=conversations), \
                    patch.object(resume, "launch_resume") as launch, contextlib.redirect_stdout(output):
                self.assertEqual(resume.main(["--active-home", str(active), "--profile", "work",
                    "--codex-bin", "codex", "--search", "B10", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["conversations"][0]["id"], FIRST)
            self.assertEqual(marker.read_text(), "personal\n")
            launch.assert_not_called()

    def test_last_uses_latest_match_and_preserves_native_arguments(self):
        conversations = [{"id": FIRST, "name": "Review B10 file bindings", "cwd": "/research", "updated_at": 2}]
        with patch.object(resume, "list_conversations", return_value=conversations), \
                patch.object(resume, "launch_resume", return_value=7) as launch:
            result = resume.main(["--active-home", "/test/.codex", "--profile", "work",
                "--codex-bin", "codex", "--last", "--search", "bindings", "--", "--model", "test-model"])
        self.assertEqual(result, 7)
        self.assertEqual(launch.call_args.args[3:], (FIRST, ["--model", "test-model"]))

    def test_resume_reenters_guarded_launcher_without_a_shell(self):
        with patch.object(resume.subprocess, "call", return_value=3) as launch:
            result = resume.launch_resume(Path("/test/.codex"), "work", "codex", FIRST, [])
        self.assertEqual(result, 3)
        command = launch.call_args.args[0]
        self.assertEqual(command[-4:], ["run", "work", "resume", FIRST])
        self.assertEqual(launch.call_args.kwargs["env"]["CODEX_HOME"], str(Path("/test/.codex")))
        self.assertNotIn("shell", launch.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
