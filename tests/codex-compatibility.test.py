#!/usr/bin/env python3
"""Opt-in real Codex resume check, using only temporary homes and fake tokens.

Run with CODEX_SWITCH_TEST_CODEX_BIN=/absolute/path/to/codex python3
tests/codex-compatibility.test.py. No model turn is submitted. The test provider
points to a closed loopback port; real credentials are never loaded or copied.
"""

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid


REPO = Path(__file__).resolve().parents[1]


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "e30." + payload + ".test-signature"


class Server:
    def __init__(self, command, env, cwd):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.errors, text=True,
                                        start_new_session=True)
        self.events = queue.Queue()
        self.request_id = 0
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for line in self.process.stdout:
            try:
                self.events.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        self.events.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.request_id += 1
        self.send({"id": self.request_id, "method": method, "params": params})
        deadline = time.monotonic() + 25
        while True:
            message = self.events.get(timeout=max(0, deadline - time.monotonic()))
            assert message is not None, "Codex exited before replying to " + method
            if message.get("id") == self.request_id:
                return message

    def initialize(self):
        response = self.request("initialize", {
            "clientInfo": {"name": "codex-tui", "version": "0.0.0"},
        })
        assert "result" in response, response
        self.send({"method": "initialized"})

    def close(self):
        # The launcher is a shell waiting on Codex. Stop the whole isolated test
        # process group so neither Codex nor its helpers outlive temporary homes.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=3)
        self.process.stdin.close()
        self.reader.join(timeout=3)
        self.process.stdout.close()
        self.errors.close()


def fixture(home, cwd, mode):
    thread_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    timestamp = "2026-01-05T12:00:00Z"
    path = home / "sessions" / "2026" / "01" / "05" / (
        "rollout-2026-01-05T12-00-00-" + thread_id + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"timestamp": timestamp, "type": "session_meta", "payload": {
            "id": thread_id, "timestamp": timestamp, "cwd": str(cwd),
            "originator": "codex", "cli_version": "0.169.0", "source": "cli",
            "model_provider": "test_provider", "history_mode": mode,
            "creator_user_id": "original-user", "creator_account_id": "original-account",
            "account_scoped": True,
        }},
        {"timestamp": timestamp, "type": "event_msg", "payload": {
            "type": "task_started", "turn_id": turn_id,
            "model_context_window": None, "collaboration_mode_kind": "default",
        }},
        {"timestamp": timestamp, "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Synthetic shared conversation"}],
        }},
        {"timestamp": timestamp, "type": "event_msg", "payload": {
            "type": "user_message", "message": "Synthetic shared conversation", "kind": "plain",
        }},
        {"timestamp": timestamp, "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": turn_id, "last_agent_message": None,
        }},
    ]
    if mode == "paginated":
        # Paginated history projects canonical items, rather than the legacy
        # user_message event above. Keep actual content in the resume check.
        records.insert(-1, {"timestamp": timestamp, "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": thread_id, "turn_id": turn_id,
            "item": {"type": "UserMessage", "id": str(uuid.uuid4()), "client_id": None,
                     "content": [{"type": "text", "text": "Synthetic shared conversation",
                                  "text_elements": []}]},
        }})
        for ordinal, record in enumerate(records):
            record["ordinal"] = ordinal
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return thread_id, path, records[0]


def main():
    binary = os.environ.get("CODEX_SWITCH_TEST_CODEX_BIN")
    if not binary:
        raise SystemExit("Set CODEX_SWITCH_TEST_CODEX_BIN to the Codex binary to test")
    if os.name == "nt":
        raise SystemExit("This real-binary launcher check currently requires POSIX")
    with tempfile.TemporaryDirectory(prefix="codex-switch-compatibility-") as root_text:
        root = Path(root_text)
        central = root / ".codex"
        central.mkdir(mode=0o700)
        (central / "config.toml").write_text(
            'model = "test-model"\nmodel_provider = "test_provider"\n'
            '[features]\ncli_resume_account_check = true\n'
            '[model_providers.test_provider]\nname = "Test provider"\n'
            'base_url = "http://127.0.0.1:1/v1"\nwire_api = "responses"\n'
            'requires_openai_auth = true\n')
        env = os.environ.copy()
        for key in ("CODEX_HOME", "CODEX_SQLITE_HOME", "OPENAI_API_KEY", "CODEX_API_KEY",
                    "CODEX_ACCESS_TOKEN", "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_FEDERATION_RULE_ID"):
            env.pop(key, None)
        env.update(HOME=str(root), CODEX_SWITCH_CODEX_BIN=binary)
        profiles = ("personal", "work", "gig")
        credentials = {}
        for profile in profiles:
            profile_home = root / (".codex-" + profile)
            profile_home.mkdir(mode=0o700)
            auth = {"auth_mode": "chatgpt", "last_refresh": datetime.now(timezone.utc).isoformat(),
                    "tokens": {"id_token": jwt({"sub": profile, "email": profile + "@example.test",
                        "https://api.openai.com/auth": {"chatgpt_user_id": profile,
                            "chatgpt_account_id": profile, "chatgpt_plan_type": "plus"}}),
                        "access_token": jwt({"exp": 4102444800}),
                        "refresh_token": "fake-refresh-" + profile, "account_id": profile}}
            auth_path = profile_home / "auth.json"
            auth_path.write_text(json.dumps(auth))
            auth_path.chmod(0o600)
            credentials[auth_path] = auth_path.read_bytes()
            subprocess.run([str(REPO / "bin/codex-switch"), profile], env=env,
                           stdout=subprocess.DEVNULL, check=True, timeout=10)
        for mode in ("legacy", "paginated"):
            thread_id, path, original_meta = fixture(central, root, mode)
            direct_env = dict(env, CODEX_HOME=str(root / ".codex-personal"),
                              CODEX_SQLITE_HOME=str(central))
            server = Server([binary, "--no-daemon", "app-server", "--stdio"], direct_env, root)
            try:
                server.initialize()
                blocked = server.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                assert "different ChatGPT account" in blocked.get("error", {}).get("message", ""), blocked
            finally:
                server.close()
            for profile in profiles:
                marker = (central / ".codex-switch-active").read_bytes()
                listing = subprocess.run([str(REPO / "bin/codex-switch"), "resume", profile,
                                          "--search", "Synthetic shared", "--json"],
                                         env=env, cwd=root, capture_output=True, text=True,
                                         check=True, timeout=30)
                shared = json.loads(listing.stdout)["conversations"]
                assert thread_id in {t["id"] for t in shared}, shared
                assert (central / ".codex-switch-active").read_bytes() == marker
                server = Server([str(REPO / "bin/codex-switch"), "run", profile,
                                 "app-server", "--stdio"], env, root)
                try:
                    server.initialize()
                    listing = server.request("thread/list", {"currentAccountOnly": False,
                                                               "modelProviders": [], "limit": 100})
                    assert thread_id in {t["id"] for t in listing.get("result", {}).get("data", [])}, listing
                    response = server.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                    assert response.get("result", {}).get("thread", {}).get("id") == thread_id, response
                    history = server.request("thread/turns/list", {"threadId": thread_id,
                                                                   "itemsView": "full", "limit": 10})
                    assert "Synthetic shared conversation" in json.dumps(history.get("result")), history
                    assert json.loads(path.read_text().splitlines()[0]) == original_meta
                    print("ok -", mode, "cross-account resume through", profile, flush=True)
                finally:
                    server.close()
        assert all(path.read_bytes() == content for path, content in credentials.items())
        assert not (central / "auth.json").exists()
        print("ok - synthetic credentials remain isolated and creator metadata is preserved")


if __name__ == "__main__":
    main()
