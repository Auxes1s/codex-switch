#!/usr/bin/env python3
"""Find shared local conversations and resume one with isolated profile auth."""

import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unicodedata
import uuid


HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("dashboard", HERE / "codex-switch-dashboard.py")
dashboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dashboard)


def clean_text(value):
    """Conversation titles are untrusted terminal text."""
    return " ".join("".join(" " if unicodedata.category(c).startswith("C") else c
                            for c in str(value or "")).split())


def list_conversations(active_home, profile, codex_bin, timeout=20):
    profile_home = Path(str(active_home) + "-" + profile)
    if not dashboard.protect_profile_auth(str(profile_home)):
        raise ValueError("Profile is not signed in; run: codex-switch login " + profile)
    env = os.environ.copy()
    env.update(CODEX_HOME=str(profile_home), CODEX_SQLITE_HOME=str(active_home))
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
                 "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_FEDERATION_RULE_ID"):
        env.pop(name, None)
    process = subprocess.Popen(dashboard.codex_app_server_command(codex_bin), env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    lines = None
    conversations = {}
    cursors = set()
    pending_id = 1

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()

    def request_page(cursor=None):
        params = {"currentAccountOnly": False, "modelProviders": [], "limit": 100,
                  "sortKey": "updated_at", "archived": False}
        if cursor:
            params["cursor"] = cursor
        send({"id": pending_id, "method": "thread/list", "params": params})

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex-switch-resume", "version": "1"}}})
        lines = dashboard.process_lines(process, timeout)
        for stream, line in lines:
            if stream is process.stderr:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != pending_id:
                continue
            if "error" in message:
                raise RuntimeError("Codex could not read shared conversations; check the profile and configuration")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Codex returned an invalid conversation response")
            if pending_id == 1:
                send({"method": "initialized"})
                pending_id += 1
                request_page()
                continue
            if not isinstance(result.get("data"), list):
                raise RuntimeError("Codex returned an invalid conversation list")
            for thread in result["data"]:
                try:
                    thread_id = str(uuid.UUID(thread["id"]))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                conversations[thread_id] = {
                    "id": thread_id,
                    "name": clean_text(thread.get("name") or thread.get("preview") or "Untitled conversation")[:200],
                    "cwd": clean_text(thread.get("cwd")),
                    "updated_at": thread.get("updatedAt"),
                }
            cursor = result.get("nextCursor")
            if not cursor:
                return sorted(conversations.values(), key=lambda t: t["updated_at"] or 0, reverse=True)
            if not isinstance(cursor, str) or cursor in cursors:
                raise RuntimeError("Codex returned a repeated or invalid conversation cursor")
            cursors.add(cursor)
            pending_id += 1
            request_page(cursor)
        raise RuntimeError("Shared conversation lookup timed out; retry or increase --timeout")
    finally:
        if lines is not None:
            lines.close()
        dashboard.stop_process(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def search_conversations(conversations, query):
    words = query.casefold().split()
    return [t for t in conversations if all(word in (t["name"] + " " + t["cwd"] + " " + t["id"]).casefold()
                                           for word in words)]


def render(conversations, offset=0, page_size=20):
    for number, thread in enumerate(conversations[offset:offset + page_size], offset + 1):
        try:
            stamp = datetime.fromtimestamp(thread["updated_at"]).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OverflowError, OSError):
            stamp = "Unknown date"
        print(f"{number:>3}. {thread['name'][:120]}")
        print(f"     {stamp}  {thread['cwd']}")
        print(f"     {thread['id']}")


def pick_conversation(conversations, query=""):
    offset = 0
    while True:
        matches = search_conversations(conversations, query)
        label = "match" if len(matches) == 1 else "matches"
        print(f"\nShared local conversations — {len(matches)} {label} across all profiles")
        render(matches, offset)
        print("Enter a number to resume, /text to search, n/p for pages, or q to cancel.")
        try:
            answer = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if answer.lower() == "q":
            return None
        if answer.startswith("/"):
            query, offset = answer[1:], 0
        elif answer.lower() == "n" and offset + 20 < len(matches):
            offset += 20
        elif answer.lower() == "p":
            offset = max(0, offset - 20)
        elif answer.isdecimal() and 1 <= int(answer) <= len(matches):
            return matches[int(answer) - 1]["id"]


def launch_resume(active_home, profile, codex_bin, thread_id, native_args):
    # Re-enter the launcher by explicit ID, which bypasses the picker. This
    # preserves domain guards, hooks, environment scrubbing and native output.
    env = os.environ.copy()
    env["CODEX_HOME"] = str(active_home)
    env["CODEX_SWITCH_CODEX_BIN"] = codex_bin
    if os.name == "nt":
        shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if not shell:
            raise RuntimeError("PowerShell is required to resume a conversation")
        command = [shell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(HERE / "codex-switch.ps1")]
    else:
        env["HOME"] = str(active_home.parent)
        command = [str(HERE / "codex-switch")]
    return subprocess.call(command + ["run", profile, "resume", thread_id] + native_args, env=env)


def main(argv=None):
    if os.name == "nt" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    native_args = []
    if "--" in argv:
        split = argv.index("--")
        argv, native_args = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(prog="codex-switch resume", description="Find and resume local conversations from every Codex profile")
    parser.add_argument("--active-home", required=True, type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--profile", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--codex-bin", required=True, help=argparse.SUPPRESS)
    parser.add_argument("thread_id", nargs="?", help="Conversation UUID, if already known")
    parser.add_argument("--search", default="", help="Search titles, project folders and UUIDs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="List matches without resuming")
    mode.add_argument("--json", action="store_true", help="Print matches as JSON without resuming")
    mode.add_argument("--last", action="store_true", help="Resume the most recently updated match")
    parser.add_argument("--all", action="store_true", help="All project folders are already included by default")
    parser.add_argument("--timeout", type=float, default=20, help="Lookup timeout in seconds")
    args = parser.parse_args(argv)
    if not dashboard.valid_profile_name(args.profile):
        parser.error("invalid profile name")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.thread_id:
        if args.list or args.json or args.last or args.search:
            parser.error("a conversation UUID cannot be combined with lookup options")
        try:
            thread_id = str(uuid.UUID(args.thread_id))
        except ValueError:
            parser.error("expected a conversation UUID")
    else:
        conversations = list_conversations(args.active_home, args.profile, args.codex_bin, args.timeout)
        matches = search_conversations(conversations, args.search)
        if args.json:
            print(json.dumps({"profile": args.profile, "conversations": matches}, indent=2))
            return 0
        if args.list or (not args.last and not (sys.stdin.isatty() and sys.stdout.isatty())):
            render(matches, page_size=len(matches))
            if not matches:
                print("No matching shared local conversations.")
            return 0
        if args.last:
            if not matches:
                raise ValueError("No matching shared local conversations")
            thread_id = matches[0]["id"]
        else:
            thread_id = pick_conversation(conversations, args.search)
            if thread_id is None:
                return 0
    return launch_resume(args.active_home, args.profile, args.codex_bin, thread_id, native_args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as error:
        print("codex-switch: " + str(error), file=sys.stderr)
        sys.exit(1)
    except OSError:
        print("codex-switch: shared conversation lookup or launch failed; check the selected profile and Codex configuration", file=sys.stderr)
        sys.exit(1)
