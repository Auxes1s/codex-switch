#!/usr/bin/env python3
"""Add native Codex auth details after a failed launch, without exposing tokens."""

import json
import os
import re
import selectors
import subprocess
import sys
import time


def diagnose(codex, profile, timeout=6):
    process = subprocess.Popen(
        [codex, '-c', 'cli_auth_credentials_store="file"', 'app-server', '--stdio'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    selector = selectors.DefaultSelector()
    buffers = {}
    auth_failed = False
    reason = None
    code = None

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    try:
        for stream in (process.stdout, process.stderr):
            selector.register(stream, selectors.EVENT_READ)
            buffers[stream] = b""
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex-switch-diagnostic", "version": "1"},
        }})
        deadline = time.monotonic() + timeout
        finished = False
        while selector.get_map() and not finished and time.monotonic() < deadline:
            for key, _ in selector.select(max(0, deadline - time.monotonic())):
                stream = key.fileobj
                data = os.read(stream.fileno(), 65536)
                if not data:
                    selector.unregister(stream)
                    continue
                buffers[stream] += data
                while b"\n" in buffers[stream]:
                    line, buffers[stream] = buffers[stream].split(b"\n", 1)
                    line = line.decode(errors="replace")
                    if stream is process.stderr:
                        # Print only the native refresh failure's message/code,
                        # never complete logs, account JSON, or credentials.
                        if "Failed to refresh token" in line:
                            match = re.search(r'error_message: Some\("((?:\\.|[^"\\])*)"\)', line)
                            if match:
                                reason = json.loads('"' + match[1] + '"')
                            match = re.search(r'error_code: Some\("([a-z0-9_]+)"\)', line)
                            if match:
                                code = match[1]
                        continue
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if response.get("id") == 1:
                        if "error" in response:
                            finished = True
                            break
                        send({"method": "initialized"})
                        send({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
                    elif response.get("id") == 2:
                        message = response.get("error", {}).get("message", "").lower()
                        auth_failed = "401" in message and "unauthorized" in message
                        if not auth_failed:
                            finished = True
                            break
                        send({"id": 3, "method": "account/read", "params": {"refreshToken": True}})
                    elif response.get("id") == 3:
                        result = response.get("result", {})
                        if result.get("account"):
                            # Codex refreshed successfully; don't claim that a
                            # new browser login is required.
                            auth_failed = False
                        finished = True
                # A native stderr line can become readable alongside the final
                # RPC response; consume all events in this batch before exiting.
        if auth_failed:
            print(f"codex-switch: Codex authentication failed for profile '{profile}'.", file=sys.stderr)
            if reason:
                detail = " ".join(reason.splitlines())
                print(f"codex-switch: Codex reports: {detail}" + (f" ({code})" if code else ""), file=sys.stderr)
            print(f"codex-switch: Sign in again: codex-switch login {profile}", file=sys.stderr)
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


if __name__ == "__main__":
    try:
        diagnose(*sys.argv[1:])
    except (OSError, ValueError, TypeError):
        # Optional diagnostics must not obscure Codex's original failure.
        pass
