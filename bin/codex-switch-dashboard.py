#!/usr/bin/env python3
"""Interactive dashboard, rate limits breakdown, and auth diagnostics for codex-switch."""

import argparse
import base64
import concurrent.futures
from datetime import datetime
import glob
import json
import os
import re
import queue
import shutil
import threading
import subprocess
import sys
import time

RESERVED_COMMANDS = {
    "add", "auth", "auto", "current", "dashboard", "help", "home",
    "list", "login", "logout", "next", "remove", "run", "shell-init",
    "status", "switch",
}


def valid_profile_name(name):
    if not name or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name) or name.endswith("."):
        return False
    return name.lower() not in RESERVED_COMMANDS


def get_active_profile(home_dir):
    active_marker = os.path.join(home_dir, ".codex", ".codex-switch-active")
    if os.path.isfile(active_marker):
        try:
            with open(active_marker, "r", encoding="utf-8") as f:
                name = f.readline().strip()
                if valid_profile_name(name):
                    return name
        except OSError:
            pass
    return None


def format_window_label(minutes):
    if minutes == 300:
        return "5-Hour Limit"
    if minutes == 10080:
        return "Weekly Limit"
    if minutes >= 1440 and minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days}-Day Limit"
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours}-Hour Limit"
    return f"{minutes}-Min Limit"


def format_countdown(epoch_seconds, now_epoch):
    if not epoch_seconds:
        return "N/A"
    diff = int(epoch_seconds - now_epoch)
    if diff <= 0:
        return "Ready now"
    if diff < 3600:
        return f"in {diff // 60}m"
    if diff < 86400:
        hours = diff // 3600
        mins = (diff % 3600) // 60
        return f"in {hours}h {mins}m"
    days = diff // 86400
    hours = (diff % 86400) // 3600
    return f"in {days}d {hours}h"


def format_reset_time(epoch_seconds, now_epoch):
    if not epoch_seconds:
        return ""
    try:
        dt = datetime.fromtimestamp(epoch_seconds)
        now_dt = datetime.fromtimestamp(now_epoch)
        if dt.date() == now_dt.date():
            return dt.strftime("%H:%M")
        return dt.strftime("%b %d %H:%M")
    except Exception:
        return ""


def parse_jwt_claims(id_token):
    if not id_token or "." not in id_token:
        return {}
    try:
        parts = id_token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            padded = payload + "=" * ((4 - len(payload) % 4) % 4)
            data = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            return json.loads(data)
    except Exception:
        pass
    return {}


def read_local_auth_metadata(profile_dir):
    auth_file = os.path.join(profile_dir, "auth.json")
    if not os.path.isfile(auth_file) or os.path.getsize(auth_file) == 0:
        return {"exists": False, "email": None, "auth_mode": None}
    try:
        with open(auth_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        auth_mode = data.get("auth_mode", "chatgpt")
        tokens = data.get("tokens") or {}
        id_token = tokens.get("id_token")
        claims = parse_jwt_claims(id_token) if id_token else {}
        email = claims.get("email")
        return {"exists": True, "email": email, "auth_mode": auth_mode}
    except Exception:
        return {"exists": True, "email": None, "auth_mode": None}


def codex_app_server_command(codex_bin):
    executable = shutil.which(codex_bin) or codex_bin
    if os.name == "nt" and executable.lower().endswith(".ps1"):
        shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if not shell:
            raise OSError("PowerShell is required to launch a .ps1 Codex shim")
        return [shell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", executable, "-c", 'cli_auth_credentials_store="file"', "app-server", "--stdio"]
    return [executable, "-c", 'cli_auth_credentials_store="file"', "app-server", "--stdio"]


def stop_process(process):
    if process.poll() is None:
        if os.name == "nt":
            # npm .cmd and PowerShell shims spawn children that also own the pipes.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        else:
            process.terminate()
        try:
            process.wait(timeout=0.6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def process_lines(process, timeout, drain_until=None):
    """Read both pipes within a deadline, including on Windows (no pipe select)."""
    events = queue.Queue()

    def read_stream(stream):
        try:
            for line in iter(stream.readline, b""):
                events.put((stream, line))
        finally:
            events.put((stream, None))

    readers = []
    for stream in (process.stdout, process.stderr):
        reader = threading.Thread(target=read_stream, args=(stream,), daemon=True)
        reader.start()
        readers.append(reader)
    deadline = time.monotonic() + timeout
    remaining_streams = 2
    try:
        while remaining_streams and time.monotonic() < deadline:
            if drain_until is not None:
                deadline = min(deadline, drain_until[0])
            try:
                stream, line = events.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                remaining_streams -= 1
            else:
                yield stream, line.decode("utf-8", errors="replace")
    finally:
        stop_process(process)
        for reader in readers:
            reader.join(timeout=1)


def query_codex_profile(profile, home_dir, codex_bin, timeout=7):
    profile_home = os.path.join(home_dir, f".codex-{profile}")
    sqlite_home = os.path.join(home_dir, ".codex")
    auth_file = os.path.join(profile_home, "auth.json")

    local_meta = read_local_auth_metadata(profile_home)
    if not local_meta["exists"]:
        return {
            "status": "signed_out",
            "email": None,
            "plan_type": None,
            "rate_limits": None,
            "error": "Profile is not signed in",
        }

    env = os.environ.copy()
    env["CODEX_HOME"] = profile_home
    env["CODEX_SQLITE_HOME"] = sqlite_home
    for k in [
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_IDENTITY_TOKEN_FILE",
        "OPENAI_FEDERATION_RULE_ID",
    ]:
        env.pop(k, None)

    try:
        cmd = codex_app_server_command(codex_bin)
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        return {
            "status": "offline",
            "email": local_meta["email"],
            "plan_type": None,
            "rate_limits": None,
            "error": f"Failed to execute Codex CLI: {e}",
        }

    account_info = None
    rate_limits_info = None
    auth_failed = False
    error_msg = None

    def send_rpc(obj):
        try:
            line = json.dumps(obj) + "\n"
            process.stdin.write(line.encode("utf-8"))
            process.stdin.flush()
        except OSError:
            pass

    lines = None
    try:
        send_rpc({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-dashboard", "version": "1"}}})
        send_rpc({"method": "initialized"})
        send_rpc({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
        send_rpc({"id": 3, "method": "account/rateLimits/read", "params": {}})

        lines = process_lines(process, timeout)
        for stream, line in lines:
            if stream is process.stderr:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue

            msg_id = msg.get("id")
            if msg_id == 2:
                if "error" in msg:
                    err = msg.get("error", {})
                    err_text = str(err.get("message", ""))
                    if "401" in err_text or "unauthorized" in err_text.lower():
                        auth_failed = True
                    error_msg = err_text
                else:
                    res = msg.get("result") or {}
                    account_info = res.get("account")
                    if account_info is None and res.get("requiresOpenaiAuth"):
                        auth_failed = True
            elif msg_id == 3:
                if "error" in msg:
                    error_msg = str(msg.get("error", {}).get("message", ""))
                else:
                    rate_limits_info = msg.get("result") or {}

            if (account_info is not None or auth_failed) and rate_limits_info is not None:
                break
    finally:
        if lines is not None:
            lines.close()
        stop_process(process)
        try:
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
        except Exception:
            pass

    if auth_failed:
        return {
            "status": "auth_expired",
            "email": local_meta["email"],
            "plan_type": None,
            "rate_limits": None,
            "error": "Session token expired or revoked. Run: codex-switch login " + profile,
        }

    email = None
    plan_type = None
    if account_info and isinstance(account_info, dict):
        email = account_info.get("email") or local_meta["email"]
        plan_type = account_info.get("planType")
    else:
        email = local_meta["email"]

    return {
        "status": "ok" if rate_limits_info else "partial",
        "email": email,
        "plan_type": plan_type,
        "rate_limits": rate_limits_info,
        "error": error_msg,
    }


def normalize_profile_data(profile_name, raw_data, active_profile, home_dir, now_epoch):
    profile_home = os.path.join(home_dir, f".codex-{profile_name}")
    is_active = (profile_name == active_profile)
    status_raw = raw_data.get("status", "unknown")
    email = raw_data.get("email") or "Unknown Account"
    plan_type = raw_data.get("plan_type") or "plus"
    plan_label = {
        "plus": "ChatGPT Plus",
        "pro": "ChatGPT Pro",
        "team": "ChatGPT Team",
        "enterprise": "ChatGPT Enterprise",
        "free": "ChatGPT Free",
    }.get(plan_type.lower(), f"ChatGPT {plan_type.capitalize()}")

    rl_data = raw_data.get("rate_limits") or {}
    rate_limits = rl_data.get("rateLimits") or {}
    primary_rl = rate_limits.get("primary") or {}
    secondary_rl = rate_limits.get("secondary") or {}
    reset_credits_info = rl_data.get("rateLimitResetCredits") or {}

    pri_used = primary_rl.get("usedPercent")
    pri_window = primary_rl.get("windowDurationMins", 300)
    pri_resets_at = primary_rl.get("resetsAt")

    sec_used = secondary_rl.get("usedPercent")
    sec_window = secondary_rl.get("windowDurationMins", 10080)
    sec_resets_at = secondary_rl.get("resetsAt")

    reset_credits_count = reset_credits_info.get("availableCount", 0)
    allowed = rl_data.get("ordinaryUsageAllowed", True)
    spend_reached = rate_limits.get("spendControlReached", False)

    # Compute status and badge
    if status_raw == "signed_out":
        readiness = "signed_out"
        badge_text = "SIGNED OUT"
        badge_color = "gray"
    elif status_raw == "auth_expired":
        readiness = "auth_expired"
        badge_text = "AUTH EXPIRED"
        badge_color = "red"
    elif not allowed or (pri_used is not None and pri_used >= 100) or (sec_used is not None and sec_used >= 100):
        readiness = "rate_limited"
        badge_text = "RATE LIMITED"
        badge_color = "red"
    elif (pri_used is not None and pri_used >= 80) or (sec_used is not None and sec_used >= 85):
        readiness = "high_usage"
        badge_text = "HIGH USAGE"
        badge_color = "yellow"
    elif status_raw in ("ok", "partial"):
        readiness = "ready"
        badge_text = "READY"
        badge_color = "green"
    else:
        readiness = "offline"
        badge_text = "OFFLINE"
        badge_color = "gray"

    primary_dict = None
    if pri_used is not None:
        primary_dict = {
            "name": format_window_label(pri_window),
            "window_minutes": pri_window,
            "used_percent": pri_used,
            "remaining_percent": max(0, 100 - pri_used),
            "resets_at": pri_resets_at,
            "resets_in": format_countdown(pri_resets_at, now_epoch),
            "resets_time": format_reset_time(pri_resets_at, now_epoch),
        }

    secondary_dict = None
    if sec_used is not None:
        secondary_dict = {
            "name": format_window_label(sec_window),
            "window_minutes": sec_window,
            "used_percent": sec_used,
            "remaining_percent": max(0, 100 - sec_used),
            "resets_at": sec_resets_at,
            "resets_in": format_countdown(sec_resets_at, now_epoch),
            "resets_time": format_reset_time(sec_resets_at, now_epoch),
        }

    return {
        "name": profile_name,
        "is_active": is_active,
        "path": profile_home,
        "readiness": readiness,
        "badge_text": badge_text,
        "badge_color": badge_color,
        "email": email,
        "plan_type": plan_type,
        "plan_label": plan_label,
        "primary": primary_dict,
        "secondary": secondary_dict,
        "reset_credits": reset_credits_count,
        "error": raw_data.get("error"),
    }


def load_quota_cache(cache_path):
    if not os.path.isfile(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data.get("profiles", {})
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_quota_cache(cache_path, profiles_cache):
    try:
        parent = os.path.dirname(cache_path)
        os.makedirs(parent, exist_ok=True)
        tmp_path = cache_path + f".tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({
                "version": 1,
                "timestamp": time.time(),
                "profiles": profiles_cache,
            }, f, indent=2)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, cache_path)
    except OSError:
        pass


def get_effective_reset_epoch(profile_dict):
    pri = profile_dict.get("primary")
    sec = profile_dict.get("secondary")
    pri_resets = pri.get("resets_at") if pri else None
    sec_resets = sec.get("resets_at") if sec else None

    pri_exhausted = pri and (pri.get("used_percent") is not None and pri.get("used_percent") >= 100)
    sec_exhausted = sec and (sec.get("used_percent") is not None and sec.get("used_percent") >= 100)

    if pri_exhausted and sec_exhausted:
        if pri_resets is not None and sec_resets is not None:
            return max(pri_resets, sec_resets)
        return pri_resets if pri_resets is not None else (sec_resets if sec_resets is not None else float("inf"))
    elif sec_exhausted:
        return sec_resets if sec_resets is not None else float("inf")
    elif pri_exhausted:
        return pri_resets if pri_resets is not None else float("inf")
    else:
        epochs = [t for t in (pri_resets, sec_resets) if t is not None]
        return min(epochs) if epochs else float("inf")


def fetch_all_profiles_dashboard(home_dir, codex_bin, timeout=7, no_cache=False, cache_ttl=60):
    now_epoch = time.time()
    active_profile = get_active_profile(home_dir)
    cache_path = os.path.join(home_dir, ".codex", ".quota-cache.json")
    cached_profiles = {} if no_cache else load_quota_cache(cache_path)

    profile_dirs = glob.glob(os.path.join(home_dir, ".codex-*"))
    profile_names = []
    for pdir in sorted(profile_dirs):
        if not os.path.isdir(pdir) or os.path.islink(pdir):
            continue
        pname = os.path.basename(pdir).replace(".codex-", "")
        if valid_profile_name(pname):
            profile_names.append(pname)

    results_raw = {}
    needed = []
    for pname in profile_names:
        entry = cached_profiles.get(pname)
        if (
            not no_cache
            and entry
            and isinstance(entry, dict)
            and "cached_at" in entry
            and (now_epoch - entry["cached_at"]) < cache_ttl
            and "data" in entry
        ):
            results_raw[pname] = entry["data"]
        else:
            needed.append(pname)

    if needed:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(needed)))) as executor:
            future_to_prof = {
                executor.submit(query_codex_profile, pname, home_dir, codex_bin, timeout): pname
                for pname in needed
            }
            for future in concurrent.futures.as_completed(future_to_prof):
                pname = future_to_prof[future]
                try:
                    res = future.result()
                    results_raw[pname] = res
                    cached_profiles[pname] = {
                        "cached_at": now_epoch,
                        "data": res,
                    }
                except Exception as e:
                    results_raw[pname] = {"status": "error", "error": str(e)}

        clean_cache = {k: v for k, v in cached_profiles.items() if k in profile_names}
        save_quota_cache(cache_path, clean_cache)

    profiles_data = []
    for pname in profile_names:
        raw = results_raw.get(pname, {})
        profiles_data.append(normalize_profile_data(pname, raw, active_profile, home_dir, now_epoch))

    ready_count = sum(1 for p in profiles_data if p["readiness"] == "ready")
    high_count = sum(1 for p in profiles_data if p["readiness"] == "high_usage")
    limited_count = sum(1 for p in profiles_data if p["readiness"] == "rate_limited")
    signed_out_count = sum(1 for p in profiles_data if p["readiness"] == "signed_out")
    # Determine recommended profile
    candidates = [
        p for p in profiles_data
        if p["readiness"] in ("ready", "high_usage")
    ]
    best_profile = None
    best_reason = None
    best_detail = None

    if candidates:
        candidates.sort(
            key=lambda x: (
                0 if x["primary"] is not None else 1,
                -(x["primary"]["remaining_percent"]) if x["primary"] else 0,
                -(x["secondary"]["remaining_percent"]) if x["secondary"] else 0,
                0 if x["is_active"] else 1,
            )
        )
        best = candidates[0]
        best_profile = best["name"]
        best_reason = "quota"
        pri = best.get("primary")
        sec = best.get("secondary")
        if pri and pri.get("remaining_percent") is not None:
            best_detail = f"{pri['remaining_percent']}% quota left"
        elif sec and sec.get("remaining_percent") is not None:
            best_detail = f"{sec['remaining_percent']}% quota left"
        else:
            best_detail = "quota available"
    else:
        # Fallback when all accounts are rate-limited: shortest time until reset
        limited_candidates = [
            p for p in profiles_data
            if p["readiness"] == "rate_limited"
        ]
        if limited_candidates:
            limited_candidates.sort(
                key=lambda x: (
                    get_effective_reset_epoch(x),
                    0 if x["is_active"] else 1,
                )
            )
            best = limited_candidates[0]
            eff_epoch = get_effective_reset_epoch(best)
            if eff_epoch != float("inf"):
                best_profile = best["name"]
                best_reason = "reset_time"
                countdown = format_countdown(eff_epoch, now_epoch)
                reset_time = format_reset_time(eff_epoch, now_epoch)
                time_str = f" at {reset_time}" if reset_time else ""
                best_detail = f"resets {countdown}{time_str}"

    return {
        "timestamp": now_epoch,
        "active_profile": active_profile,
        "summary": {
            "total": len(profiles_data),
            "ready": ready_count,
            "high_usage": high_count,
            "rate_limited": limited_count,
            "signed_out": signed_out_count,
            "recommended": best_profile,
            "recommended_reason": best_reason,
            "recommended_detail": best_detail,
        },
        "profiles": profiles_data,
    }


def render_progress_bar(remaining_percent, width=14, color=True):
    if remaining_percent is None:
        return "[?             ]"
    pct = max(0, min(100, int(remaining_percent)))
    filled = int(round(pct * width / 100))
    bar = "█" * filled + "░" * (width - filled)
    if not color:
        return f"[{bar}] {pct:>3}% left"
    if pct >= 30:
        c_code = "\033[32m"  # Green: healthy quota left
    elif pct >= 10:
        c_code = "\033[33m"  # Yellow: low quota left
    else:
        c_code = "\033[31m"  # Red: critical / exhausted
    return f"[{c_code}{bar}\033[0m] {pct:>3}% left"


def render_terminal(data, use_color=True, focus_profile=None):
    c_bold = "\033[1m" if use_color else ""
    c_green = "\033[32m" if use_color else ""
    c_yellow = "\033[33m" if use_color else ""
    c_red = "\033[31m" if use_color else ""
    c_cyan = "\033[36m" if use_color else ""
    c_dim = "\033[2m" if use_color else ""
    c_reset = "\033[0m" if use_color else ""

    lines = []
    lines.append(f"{c_bold}{c_cyan}================================================================================{c_reset}")
    lines.append(f"{c_bold}  ⚡ CODEX-SWITCH DASHBOARD • REMAINING QUOTA & LIMITS STATUS{c_reset}")
    lines.append(f"{c_bold}{c_cyan}================================================================================{c_reset}")

    active = data.get("active_profile") or "None"
    summ = data.get("summary") or {}
    lines.append(f"  {c_bold}Active Profile :{c_reset} {c_green}{active}{c_reset}")
    lines.append(
        f"  {c_bold}Readiness      :{c_reset} {c_green}{summ.get('ready', 0)} ready{c_reset} • "
        f"{c_yellow}{summ.get('high_usage', 0)} low quota{c_reset} • "
        f"{c_red}{summ.get('rate_limited', 0)} exhausted{c_reset} • "
        f"{c_dim}{summ.get('signed_out', 0)} signed out{c_reset}"
    )
    if summ.get("recommended"):
        rec = summ.get("recommended")
        if rec == active:
            if summ.get("recommended_reason") == "reset_time":
                detail = summ.get("recommended_detail", "resets earliest")
                lines.append(f"  {c_bold}Recommended    :{c_reset} Active profile {c_green}{c_bold}{rec}{c_reset} resets earliest ({detail})")
            else:
                detail = summ.get("recommended_detail", "optimal")
                lines.append(f"  {c_bold}Recommended    :{c_reset} Active profile {c_green}{c_bold}{rec}{c_reset} is optimal ({detail})")
        else:
            if summ.get("recommended_reason") == "reset_time":
                detail = summ.get("recommended_detail", "resets soonest")
                lines.append(f"  {c_bold}Recommended    :{c_reset} Switch to {c_yellow}{c_bold}{rec}{c_reset} (all limited; {detail})")
            else:
                detail = summ.get("recommended_detail", "highest remaining quota")
                lines.append(f"  {c_bold}Recommended    :{c_reset} Switch to {c_green}{c_bold}{rec}{c_reset} ({detail})")
    lines.append(f"{c_dim}--------------------------------------------------------------------------------{c_reset}")

    profiles = data.get("profiles", [])
    if focus_profile:
        profiles = [p for p in profiles if p["name"] == focus_profile]

    if not profiles:
        lines.append("  No profiles found matching criteria.")
    else:
        for p in profiles:
            is_act = p["is_active"]
            act_icon = f"{c_green}●{c_reset}" if is_act else f"{c_dim}○{c_reset}"
            act_badge = f" {c_green}{c_bold}(ACTIVE){c_reset}" if is_act else ""

            b_color = c_green if p["badge_color"] == "green" else c_yellow if p["badge_color"] == "yellow" else c_red if p["badge_color"] == "red" else c_dim
            badge = f"[{b_color}{c_bold}{p['badge_text']}{c_reset}]"

            p_title = f"{act_icon} {c_bold}{p['name']}{c_reset}{act_badge}"
            lines.append(f"\n{p_title}  {badge}")
            lines.append(f"  Account     : {p['email']} {c_dim}({p['plan_label']}){c_reset}")

            if p["primary"]:
                pri = p["primary"]
                bar = render_progress_bar(pri["remaining_percent"], width=14, color=use_color)
                resets_str = f"Resets {pri['resets_in']}" + (f" ({pri['resets_time']})" if pri["resets_time"] else "")
                lines.append(f"  {pri['name']:11} : {bar} ({pri['used_percent']}% used) • {resets_str}")
            if p["secondary"]:
                sec = p["secondary"]
                bar = render_progress_bar(sec["remaining_percent"], width=14, color=use_color)
                resets_str = f"Resets {sec['resets_in']}" + (f" ({sec['resets_time']})" if sec["resets_time"] else "")
                lines.append(f"  {sec['name']:11} : {bar} ({sec['used_percent']}% used) • {resets_str}")

            if p["reset_credits"] > 0:
                lines.append(f"  Credits     : {c_yellow}{c_bold}✨ {p['reset_credits']} free rate limit reset(s) available{c_reset}")

            if p["error"] and p["readiness"] != "signed_out":
                lines.append(f"  Note        : {c_dim}{p['error']}{c_reset}")

    lines.append(f"\n{c_dim}--------------------------------------------------------------------------------{c_reset}")
    lines.append(f"  {c_bold}Quick Actions :{c_reset}")
    lines.append(f"    Auto-rotate    : {c_cyan}codex-switch next{c_reset}")
    lines.append(f"    Switch profile : {c_cyan}codex-switch <profile>{c_reset}")
    lines.append(f"    Live watch     : {c_cyan}codex-switch dashboard --watch{c_reset}")
    lines.append(f"{c_bold}{c_cyan}================================================================================{c_reset}\n")

    return "\n".join(lines)


def has_saved_credentials():
    """True when the profile's CODEX_HOME holds a non-empty auth.json."""
    home = os.environ.get("CODEX_HOME")
    if not home:
        return False
    try:
        return os.path.getsize(os.path.join(home, "auth.json")) > 0
    except OSError:
        return False


def diagnose(codex, profile, timeout=6):
    process = subprocess.Popen(
        codex_app_server_command(codex),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    drain_until = [float('inf')]
    lines = process_lines(process, timeout, drain_until)
    auth_failed = False
    reason = None
    fallback_reason = None
    code = None

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex-switch-diagnostic", "version": "1"},
        }})
        for stream, line in lines:
            if stream is process.stderr:
                if "Failed to refresh token" in line:
                    match = re.search(r'error_message: Some\("((?:\\.|[^"\\])*)"\)', line)
                    if match:
                        reason = json.loads('"' + match[1] + '"')
                    match = re.search(r'error_code: Some\("([a-z0-9_]+)"\)', line)
                    if match:
                        code = match[1]
                    match = re.search(r"Failed to refresh token: ([A-Za-z ,.']{10,200})$", line.rstrip())
                    if match and not fallback_reason:
                        fallback_reason = match[1].strip()
                continue
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") == 1:
                if "error" in response:
                    break
                send({"method": "initialized"})
                send({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
            elif response.get("id") == 2:
                message = response.get("error", {}).get("message", "").lower()
                auth_failed = "401" in message and "unauthorized" in message
                result = response.get("result")
                if (not auth_failed and isinstance(result, dict) and result.get("account") is None
                        and result.get("requiresOpenaiAuth") and has_saved_credentials()):
                    auth_failed = True
                if not auth_failed:
                    break
                send({"id": 3, "method": "account/read", "params": {"refreshToken": True}})
            elif response.get("id") == 3:
                result = response.get("result", {})
                if result.get("account"):
                    auth_failed = False
                # stderr and stdout have independent readers; allow the native
                # refresh reason emitted before this response to reach the queue.
                drain_until[0] = time.monotonic() + 0.1
        if auth_failed:
            print(f"codex-switch: Codex authentication failed for profile '{profile}'.", file=sys.stderr)
            if reason:
                detail = " ".join(reason.splitlines())
                print(f"codex-switch: Codex reports: {detail}" + (f" ({code})" if code else ""), file=sys.stderr)
            elif fallback_reason:
                print(f"codex-switch: Codex reports: {fallback_reason}", file=sys.stderr)
            print(f"codex-switch: Sign in again: codex-switch login {profile}", file=sys.stderr)
    finally:
        lines.close()
        stop_process(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def run_watch_loop(home_dir, codex_bin, interval=5, focus_profile=None):
    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    try:
        while True:
            data = fetch_all_profiles_dashboard(home_dir, codex_bin, no_cache=True)
            output = render_terminal(data, use_color=use_color, focus_profile=focus_profile)
            if use_color:
                sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(output)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nExiting watch mode.")


def switch_profile_action(profile_name, home_dir, codex_bin):
    if not valid_profile_name(profile_name):
        return False, f"invalid profile name: {profile_name}"
    profile_home = os.path.join(home_dir, f".codex-{profile_name}")
    if not os.path.isdir(profile_home) or os.path.islink(profile_home):
        return False, f"profile directory does not exist: {profile_name}"
    auth_file = os.path.join(profile_home, "auth.json")
    if not os.path.isfile(auth_file) or os.path.getsize(auth_file) == 0:
        return False, f"profile is not signed in: {profile_name}"

    cmd_path = os.path.join(os.path.dirname(__file__), "codex-switch.cmd")
    if os.name == "nt" and os.path.isfile(cmd_path):
        env = os.environ.copy()
        env["CODEX_HOME"] = os.path.join(home_dir, ".codex")
        if codex_bin:
            env["CODEX_SWITCH_CODEX_BIN"] = codex_bin
        res = subprocess.run([cmd_path, "switch", profile_name], env=env, capture_output=True, text=True)
        return (res.returncode == 0, res.stderr.strip() or res.stdout.strip() if res.returncode != 0 else None)

    script_path = os.path.join(os.path.dirname(__file__), "codex-switch")
    if os.path.isfile(script_path):
        env = os.environ.copy()
        env["HOME"] = home_dir
        env["CODEX_HOME"] = os.path.join(home_dir, ".codex")
        if codex_bin:
            env["CODEX_SWITCH_CODEX_BIN"] = codex_bin
        res = subprocess.run([script_path, "switch", profile_name], env=env, capture_output=True, text=True)
        return (res.returncode == 0, res.stderr.strip() or res.stdout.strip() if res.returncode != 0 else None)

    marker_path = os.path.join(home_dir, ".codex", ".codex-switch-active")
    tmp_path = marker_path + f".tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(f"{profile_name}\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, marker_path)
        return True, None
    except Exception as e:
        return False, str(e)


def handle_auto_rotate(data, home_dir, codex_bin, as_json=False):
    summ = data.get("summary") or {}
    recommended = summ.get("recommended")
    reason = summ.get("recommended_reason")
    detail = summ.get("recommended_detail")
    active_profile = data.get("active_profile")

    if not recommended:
        if as_json:
            print(json.dumps({
                "success": False,
                "error": "No usable profiles available to rotate",
                "active_profile": active_profile,
            }, indent=2))
        else:
            print("codex-switch: no usable profiles available to rotate (all profiles signed out or offline)", file=sys.stderr)
        sys.exit(1)

    if recommended == active_profile:
        if as_json:
            print(json.dumps({
                "success": True,
                "switched": False,
                "active_profile": active_profile,
                "reason": reason,
                "detail": detail,
            }, indent=2))
        else:
            if reason == "reset_time":
                print(f"codex-switch: all profiles are rate-limited; already on '{active_profile}' ({detail})")
            else:
                print(f"codex-switch: already on best profile '{active_profile}' ({detail})")
        sys.exit(0)

    ok, err = switch_profile_action(recommended, home_dir, codex_bin)
    if not ok:
        if as_json:
            print(json.dumps({
                "success": False,
                "error": err or "Switch action failed",
                "active_profile": active_profile,
                "target_profile": recommended,
            }, indent=2))
        else:
            print(f"codex-switch: could not switch to profile '{recommended}': {err}", file=sys.stderr)
        sys.exit(1)

    if as_json:
        print(json.dumps({
            "success": True,
            "switched": True,
            "active_profile": recommended,
            "previous_profile": active_profile,
            "reason": reason,
            "detail": detail,
        }, indent=2))
    else:
        if reason == "reset_time":
            print(f"All profiles rate-limited. Switched to Codex profile: {recommended} ({detail})")
        else:
            print(f"Switched to Codex profile: {recommended} ({detail})")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Codex Profile Usage & Status Dashboard")
    parser.add_argument("profile", nargs="?", help="Specific profile to inspect")
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_SWITCH_CODEX_BIN", "codex"), help="Codex binary path")
    parser.add_argument("--home-dir", default=os.environ.get("HOME", os.path.expanduser("~")), help="User home directory")
    parser.add_argument("--dashboard", "-d", action="store_true", help="Display profile limits dashboard")
    parser.add_argument("--all", "-a", action="store_true", help="Display all profiles in dashboard")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--watch", "-W", action="store_true", help="Live auto-refresh terminal dashboard")
    parser.add_argument("--interval", type=int, default=5, help="Refresh interval in seconds for watch mode")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    parser.add_argument("--no-cache", "-f", "--refresh", action="store_true", help="Bypass quota cache and query fresh limits")
    parser.add_argument("--cache-ttl", type=int, default=60, help="Quota cache TTL in seconds (default: 60)")
    parser.add_argument("--diagnose", nargs=2, metavar=("CODEX_BIN", "PROFILE"), help="Diagnose auth failure for profile")
    parser.add_argument("--next", "--auto", dest="auto_rotate", action="store_true", help="Automatically switch to the profile with highest quota (or shortest reset time)")

    args = parser.parse_args()

    if args.profile in ("next", "auto"):
        args.auto_rotate = True
        args.profile = None

    if args.diagnose:
        try:
            diagnose(args.diagnose[0], args.diagnose[1])
        except (OSError, ValueError, TypeError):
            pass
        return

    if args.watch:
        run_watch_loop(args.home_dir, args.codex_bin, interval=args.interval, focus_profile=args.profile)
        return

    data = fetch_all_profiles_dashboard(
        args.home_dir,
        args.codex_bin,
        no_cache=args.no_cache,
        cache_ttl=args.cache_ttl,
    )

    if args.auto_rotate:
        handle_auto_rotate(data, args.home_dir, args.codex_bin, as_json=args.json)
        return

    if args.json:
        if args.profile:
            filtered = [p for p in data.get("profiles", []) if p["name"] == args.profile]
            data["profiles"] = filtered
        print(json.dumps(data, indent=2))
        return

    use_color = (sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR"))
    output = render_terminal(data, use_color=use_color, focus_profile=args.profile)
    print(output)


if __name__ == "__main__":
    main()
