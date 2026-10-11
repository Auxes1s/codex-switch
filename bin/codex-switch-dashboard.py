#!/usr/bin/env python3
"""Interactive dashboard, rate limits breakdown, and web homepage for codex-switch."""

import argparse
import base64
import concurrent.futures
from datetime import datetime
import glob
import http.server
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import webbrowser

RESERVED_COMMANDS = {
    "add", "auth", "current", "dashboard", "help", "home",
    "list", "login", "logout", "remove", "run", "shell-init",
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

    cmd = [codex_bin, "-c", 'cli_auth_credentials_store="file"', "app-server", "--stdio"]
    try:
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

    try:
        send_rpc({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-dashboard", "version": "1"}}})
        send_rpc({"method": "initialized"})
        send_rpc({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
        send_rpc({"id": 3, "method": "account/rateLimits/read", "params": {}})

        deadline = time.time() + timeout
        while time.time() < deadline:
            line_bytes = process.stdout.readline()
            if not line_bytes:
                break
            try:
                msg = json.loads(line_bytes.decode("utf-8", errors="replace"))
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
        process.terminate()
        try:
            process.wait(timeout=0.6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
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
    is_link = os.path.islink(profile_home)
    link_target = os.readlink(profile_home) if is_link else None
    alias_of = None
    if link_target:
        m = re.search(r"\.codex-([A-Za-z0-9._-]+)", link_target)
        if m:
            alias_of = m.group(1)

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
        "is_alias": is_link,
        "alias_of": alias_of,
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


def fetch_all_profiles_dashboard(home_dir, codex_bin, timeout=7):
    now_epoch = time.time()
    active_profile = get_active_profile(home_dir)
    profile_dirs = glob.glob(os.path.join(home_dir, ".codex-*"))
    profile_names = []
    for pdir in sorted(profile_dirs):
        if not os.path.isdir(pdir):
            continue
        pname = os.path.basename(pdir).replace(".codex-", "")
        if valid_profile_name(pname):
            profile_names.append(pname)

    results_raw = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(profile_names)))) as executor:
        future_to_prof = {
            executor.submit(query_codex_profile, pname, home_dir, codex_bin, timeout): pname
            for pname in profile_names
        }
        for future in concurrent.futures.as_completed(future_to_prof):
            pname = future_to_prof[future]
            try:
                results_raw[pname] = future.result()
            except Exception as e:
                results_raw[pname] = {"status": "error", "error": str(e)}

    profiles_data = []
    for pname in profile_names:
        raw = results_raw.get(pname, {})
        profiles_data.append(normalize_profile_data(pname, raw, active_profile, home_dir, now_epoch))

    ready_count = sum(1 for p in profiles_data if p["readiness"] == "ready")
    high_count = sum(1 for p in profiles_data if p["readiness"] == "high_usage")
    limited_count = sum(1 for p in profiles_data if p["readiness"] == "rate_limited")
    signed_out_count = sum(1 for p in profiles_data if p["readiness"] == "signed_out")

    # Determine recommended profile (highest remaining quota)
    candidates = [
        p for p in profiles_data
        if p["readiness"] in ("ready", "high_usage") and not p["is_alias"]
    ]
    candidates.sort(
        key=lambda x: (
            0 if x["primary"] is not None else 1,
            -(x["primary"]["remaining_percent"]) if x["primary"] else 0,
            -(x["secondary"]["remaining_percent"]) if x["secondary"] else 0,
        )
    )
    best_profile = candidates[0]["name"] if candidates else None

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
        lines.append(f"  {c_bold}Recommended    :{c_reset} Switch to {c_green}{c_bold}{summ.get('recommended')}{c_reset} (highest remaining quota)")
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
            alias_note = f" {c_dim}(alias of {p['alias_of']}){c_reset}" if p.get("is_alias") and p.get("alias_of") else ""

            b_color = c_green if p["badge_color"] == "green" else c_yellow if p["badge_color"] == "yellow" else c_red if p["badge_color"] == "red" else c_dim
            badge = f"[{b_color}{c_bold}{p['badge_text']}{c_reset}]"

            p_title = f"{act_icon} {c_bold}{p['name']}{c_reset}{act_badge}{alias_note}"
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
    lines.append(f"    Switch profile : {c_cyan}codex-switch <profile>{c_reset}")
    lines.append(f"    Web dashboard  : {c_cyan}codex-switch dashboard --web{c_reset}")
    lines.append(f"    Live watch     : {c_cyan}codex-switch dashboard --watch{c_reset}")
    lines.append(f"{c_bold}{c_cyan}================================================================================{c_reset}\n")

    return "\n".join(lines)


# Web UI Single Page Template
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>codex-switch Dashboard</title>
  <style>
    :root {
      --bg: #090b10;
      --surface: #12151e;
      --surface-hover: #171b26;
      --border: #232838;
      --border-focus: #3b82f6;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --green: #10b981;
      --green-bg: rgba(16, 185, 129, 0.12);
      --yellow: #f59e0b;
      --yellow-bg: rgba(245, 158, 11, 0.12);
      --red: #ef4444;
      --red-bg: rgba(239, 68, 68, 0.12);
      --blue: #3b82f6;
      --blue-bg: rgba(59, 130, 246, 0.12);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      padding: 24px;
      line-height: 1.5;
    }
    .container { max-width: 1100px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
      flex-wrap: wrap;
      gap: 16px;
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
    .brand .badge {
      background: var(--blue-bg);
      color: var(--blue);
      border: 1px solid rgba(59, 130, 246, 0.3);
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
    }
    .header-actions { display: flex; align-items: center; gap: 12px; }
    button {
      background: var(--surface);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 8px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      transition: all 0.15s ease;
    }
    button:hover { background: var(--surface-hover); border-color: #3b4256; }
    button.primary { background: #2563eb; border-color: #3b82f6; }
    button.primary:hover { background: #1d4ed8; }
    button.switch-btn { width: 100%; justify-content: center; margin-top: 12px; }
    .refresh-spinner { display: inline-block; transition: transform 0.6s ease; }
    .refreshing .refresh-spinner { transform: rotate(360deg); }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 28px;
    }
    .summary-card {
      background: var(--surface);
      border: 1px solid var(--border);
      padding: 16px 20px;
      border-radius: 12px;
    }
    .summary-card .label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
    .summary-card .val { font-size: 24px; font-weight: 700; margin-top: 4px; display: flex; align-items: center; gap: 8px; }

    .profiles-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 20px;
    }
    .profile-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      position: relative;
      transition: border-color 0.15s ease, transform 0.15s ease;
    }
    .profile-card:hover { border-color: #3b4256; transform: translateY(-2px); }
    .profile-card.active { border-color: var(--green); box-shadow: 0 0 0 1px var(--green); }
    .card-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
    .prof-name { font-size: 18px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
    .active-pill {
      background: var(--green);
      color: #000;
      font-size: 10px;
      font-weight: 800;
      padding: 2px 6px;
      border-radius: 4px;
      letter-spacing: 0.5px;
    }
    .status-badge {
      font-size: 11px;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 6px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .status-badge.green { background: var(--green-bg); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.3); }
    .status-badge.yellow { background: var(--yellow-bg); color: var(--yellow); border: 1px solid rgba(245, 158, 11, 0.3); }
    .status-badge.red { background: var(--red-bg); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.3); }
    .status-badge.gray { background: rgba(156, 163, 175, 0.12); color: var(--text-muted); border: 1px solid rgba(156, 163, 175, 0.2); }

    .account-meta { font-size: 13px; color: var(--text-muted); margin-bottom: 16px; word-break: break-all; }
    .plan-chip {
      background: rgba(255, 255, 255, 0.08);
      color: var(--text);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
      margin-left: 6px;
    }

    .limit-block { margin-top: 14px; }
    .limit-header { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 6px; }
    .limit-header .name { font-weight: 600; color: var(--text); }
    .limit-header .reset { color: var(--text-muted); }
    .progress-bar-bg {
      background: #1e2230;
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      margin-bottom: 4px;
    }
    .progress-bar-fill { height: 100%; border-radius: 999px; transition: width 0.3s ease; }
    .progress-bar-fill.green { background: linear-gradient(90deg, #10b981, #059669); }
    .progress-bar-fill.yellow { background: linear-gradient(90deg, #f59e0b, #d97706); }
    .progress-bar-fill.red { background: linear-gradient(90deg, #ef4444, #dc2626); }
    .limit-sub { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-muted); }

    .credits-banner {
      background: var(--yellow-bg);
      border: 1px solid rgba(245, 158, 11, 0.3);
      color: #fbbf24;
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 600;
      margin-top: 14px;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #1e2230;
      color: var(--text);
      border: 1px solid var(--border);
      padding: 12px 20px;
      border-radius: 10px;
      font-size: 14px;
      font-weight: 600;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
      display: none;
      z-index: 100;
    }
    .toast.show { display: block; animation: slideUp 0.2s ease; }
    @keyframes slideUp { from { transform: translateY(10px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

    .footer {
      margin-top: 40px;
      padding-top: 20px;
      border-top: 1px solid var(--border);
      text-align: center;
      font-size: 13px;
      color: var(--text-muted);
    }
    .cli-chip {
      background: #171b26;
      border: 1px solid var(--border);
      padding: 4px 10px;
      border-radius: 6px;
      font-family: monospace;
      font-size: 12px;
      color: #93c5fd;
      cursor: pointer;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <h1>⚡ codex-switch</h1>
        <span class="badge">Profile Dashboard</span>
      </div>
      <div class="header-actions">
        <label style="display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-muted); cursor: pointer;">
          <input type="checkbox" id="autoRefresh" checked> Auto-refresh (30s)
        </label>
        <button id="refreshBtn" onclick="loadDashboard()">
          <span class="refresh-spinner">↻</span> Refresh
        </button>
      </div>
    </header>

    <div class="summary-grid" id="summaryGrid">
      <div class="summary-card">
        <div class="label">Active Profile</div>
        <div class="val" id="activeProfileVal">-</div>
      </div>
      <div class="summary-card">
        <div class="label">Ready for Use</div>
        <div class="val" style="color: var(--green);" id="readyProfilesVal">-</div>
      </div>
      <div class="summary-card">
        <div class="label">Recommended Next</div>
        <div class="val" style="color: var(--blue);" id="recommendedVal">-</div>
      </div>
      <div class="summary-card">
        <div class="label">Total Profiles</div>
        <div class="val" id="totalProfilesVal">-</div>
      </div>
    </div>

    <div class="profiles-grid" id="profilesGrid">
      <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted);">
        Loading profiles and rate limits...
      </div>
    </div>

    <div class="footer">
      Run in terminal: <span class="cli-chip" onclick="copyText('codex-switch dashboard')">codex-switch dashboard</span>
      &nbsp;•&nbsp; Switch: <span class="cli-chip" onclick="copyText('codex-switch <profile>')">codex-switch &lt;profile&gt;</span>
    </div>
  </div>

  <div class="toast" id="toast">Profile switched!</div>

  <script>
    let timer = null;

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 3000);
    }

    function copyText(txt) {
      navigator.clipboard.writeText(txt);
      showToast('Copied: ' + txt);
    }

    async function switchProfile(name) {
      try {
        const res = await fetch('/api/switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile: name })
        });
        const data = await res.json();
        if (data.success) {
          showToast('Switched active profile to "' + name + '"');
          loadDashboard();
        } else {
          alert('Failed to switch: ' + (data.error || 'Unknown error'));
        }
      } catch (err) {
        alert('Network error switching profile: ' + err);
      }
    }

    async function loadDashboard() {
      const btn = document.getElementById('refreshBtn');
      btn.classList.add('refreshing');
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        renderDashboard(data);
      } catch (err) {
        console.error('Failed to load status:', err);
      } finally {
        setTimeout(() => btn.classList.remove('refreshing'), 600);
      }
    }

    function renderDashboard(data) {
      const summ = data.summary || {};
      document.getElementById('activeProfileVal').innerText = data.active_profile || 'None';
      document.getElementById('readyProfilesVal').innerText = `${summ.ready || 0} / ${summ.total || 0}`;
      document.getElementById('recommendedVal').innerText = summ.recommended || 'None';
      document.getElementById('totalProfilesVal').innerText = summ.total || 0;

      const grid = document.getElementById('profilesGrid');
      grid.innerHTML = '';

      (data.profiles || []).forEach(p => {
        const card = document.createElement('div');
        card.className = 'profile-card' + (p.is_active ? ' active' : '');

        let primaryHtml = '';
        if (p.primary) {
          const left = p.primary.remaining_percent;
          const used = p.primary.used_percent;
          const fillClass = left >= 30 ? 'green' : (left >= 10 ? 'yellow' : 'red');
          primaryHtml = `
            <div class="limit-block">
              <div class="limit-header">
                <span class="name">${p.primary.name}</span>
                <span class="reset">Resets ${p.primary.resets_in}</span>
              </div>
              <div class="progress-bar-bg">
                <div class="progress-bar-fill ${fillClass}" style="width: ${Math.min(100, left)}%;"></div>
              </div>
              <div class="limit-sub">
                <span style="font-weight: 600; color: var(--text);">${left}% left</span>
                <span>(${used}% used)</span>
              </div>
            </div>
          `;
        }

        let secondaryHtml = '';
        if (p.secondary) {
          const left = p.secondary.remaining_percent;
          const used = p.secondary.used_percent;
          const fillClass = left >= 30 ? 'green' : (left >= 10 ? 'yellow' : 'red');
          secondaryHtml = `
            <div class="limit-block">
              <div class="limit-header">
                <span class="name">${p.secondary.name}</span>
                <span class="reset">Resets ${p.secondary.resets_in}</span>
              </div>
              <div class="progress-bar-bg">
                <div class="progress-bar-fill ${fillClass}" style="width: ${Math.min(100, left)}%;"></div>
              </div>
              <div class="limit-sub">
                <span style="font-weight: 600; color: var(--text);">${left}% left</span>
                <span>(${used}% used)</span>
              </div>
            </div>
          `;
        }

        let creditsHtml = '';
        if (p.reset_credits > 0) {
          creditsHtml = `<div class="credits-banner">✨ ${p.reset_credits} Rate Limit Reset(s) Available</div>`;
        }

        let actionBtn = '';
        if (p.is_active) {
          actionBtn = `<button disabled style="opacity: 0.7; cursor: default; width: 100%; justify-content: center; margin-top: 14px;">✓ Currently Active</button>`;
        } else if (p.readiness === 'signed_out') {
          actionBtn = `<button onclick="copyText('codex-switch login ${p.name}')" class="switch-btn">Sign In: codex-switch login ${p.name}</button>`;
        } else {
          actionBtn = `<button onclick="switchProfile('${p.name}')" class="primary switch-btn">Switch to ${p.name}</button>`;
        }

        card.innerHTML = `
          <div>
            <div class="card-top">
              <div class="prof-name">
                ${p.name}
                ${p.is_active ? '<span class="active-pill">ACTIVE</span>' : ''}
              </div>
              <span class="status-badge ${p.badge_color}">${p.badge_text}</span>
            </div>
            <div class="account-meta">
              ${p.email} <span class="plan-chip">${p.plan_label}</span>
              ${p.is_alias ? `<div style="font-size:11px; margin-top:3px;">Alias of ${p.alias_of}</div>` : ''}
            </div>
            ${primaryHtml}
            ${secondaryHtml}
            ${creditsHtml}
          </div>
          <div>
            ${actionBtn}
          </div>
        `;
        grid.appendChild(card);
      });
    }

    document.getElementById('autoRefresh').addEventListener('change', e => {
      if (e.target.checked) {
        timer = setInterval(loadDashboard, 30000);
      } else {
        clearInterval(timer);
      }
    });

    loadDashboard();
    timer = setInterval(loadDashboard, 30000);
  </script>
</body>
</html>
"""


def switch_profile_action(profile_name, home_dir, codex_bin):
    if not valid_profile_name(profile_name):
        return False, f"Invalid profile name: {profile_name}"
    profile_home = os.path.join(home_dir, f".codex-{profile_name}")
    if not os.path.isdir(profile_home):
        return False, f"Profile directory does not exist: {profile_name}"
    auth_file = os.path.join(profile_home, "auth.json")
    if not os.path.isfile(auth_file) or os.path.getsize(auth_file) == 0:
        return False, f"Profile is not signed in: {profile_name}"

    cmd_path = os.path.join(os.path.dirname(__file__), "codex-switch.cmd")
    if os.name == "nt" and os.path.isfile(cmd_path):
        env = os.environ.copy()
        if codex_bin:
            env["CODEX_SWITCH_CODEX_BIN"] = codex_bin
        res = subprocess.run([cmd_path, "switch", profile_name], env=env, capture_output=True, text=True)
        if res.returncode == 0:
            return True, None
        return False, res.stderr.strip() or res.stdout.strip()

    script_path = os.path.join(os.path.dirname(__file__), "codex-switch")
    if os.path.isfile(script_path):
        env = os.environ.copy()
        env["HOME"] = home_dir
        if codex_bin:
            env["CODEX_SWITCH_CODEX_BIN"] = codex_bin
        res = subprocess.run([script_path, "switch", profile_name], env=env, capture_output=True, text=True)
        if res.returncode == 0:
            return True, None
        return False, res.stderr.strip() or res.stdout.strip()

    # Fallback to direct marker write if shell script is unavailable
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


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    home_dir = None
    codex_bin = "codex"

    def log_message(self, format, *args):
        # Suppress noisy HTTP request logging
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/status":
            data = fetch_all_profiles_dashboard(self.home_dir, self.codex_bin)
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path in ("/", "/index.html"):
            payload = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/switch":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            try:
                req_json = json.loads(body.decode("utf-8"))
                target_prof = req_json.get("profile")
            except Exception:
                target_prof = None

            if not target_prof:
                resp = {"success": False, "error": "Profile name is required"}
            else:
                ok, err = switch_profile_action(target_prof, self.home_dir, self.codex_bin)
                resp = {"success": ok, "error": err, "active": target_prof if ok else None}

            payload = json.dumps(resp).encode("utf-8")
            self.send_response(200 if resp["success"] else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()


def start_web_server(home_dir, codex_bin, host="127.0.0.1", port=8420, open_browser=True):
    DashboardHandler.home_dir = home_dir
    DashboardHandler.codex_bin = codex_bin

    chosen_port = port
    server = None
    for p in range(port, port + 20):
        try:
            server = http.server.ThreadingHTTPServer((host, p), DashboardHandler)
            chosen_port = p
            break
        except OSError:
            continue

    if not server:
        sys.stderr.write(f"codex-switch: could not bind web server to {host} on ports {port}-{port+20}\n")
        sys.exit(1)

    url = f"http://{host}:{chosen_port}"
    print(f"\n⚡ Codex Profile Dashboard running at: {url}")
    print("Press Ctrl+C to stop the dashboard server.\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        server.server_close()


def run_watch_loop(home_dir, codex_bin, interval=5, focus_profile=None):
    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    try:
        while True:
            data = fetch_all_profiles_dashboard(home_dir, codex_bin)
            output = render_terminal(data, use_color=use_color, focus_profile=focus_profile)
            if use_color:
                sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(output)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nExiting watch mode.")


def main():
    parser = argparse.ArgumentParser(description="Codex Profile Usage & Status Dashboard")
    parser.add_argument("profile", nargs="?", help="Specific profile to inspect")
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_SWITCH_CODEX_BIN", "codex"), help="Codex binary path")
    parser.add_argument("--home-dir", default=os.environ.get("HOME", os.path.expanduser("~")), help="User home directory")
    parser.add_argument("--dashboard", "-d", action="store_true", help="Display profile limits dashboard")
    parser.add_argument("--all", "-a", action="store_true", help="Display all profiles in dashboard")
    parser.add_argument("--web", "-w", action="store_true", help="Launch interactive browser dashboard / homepage")
    parser.add_argument("--port", type=int, default=8420, help="Web server port")
    parser.add_argument("--host", default="127.0.0.1", help="Web server host")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser in web mode")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--watch", "-W", action="store_true", help="Live auto-refresh terminal dashboard")
    parser.add_argument("--interval", type=int, default=5, help="Refresh interval in seconds for watch mode")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output")

    args = parser.parse_args()

    if args.web:
        start_web_server(args.home_dir, args.codex_bin, host=args.host, port=args.port, open_browser=not args.no_browser)
        return

    if args.watch:
        run_watch_loop(args.home_dir, args.codex_bin, interval=args.interval, focus_profile=args.profile)
        return

    data = fetch_all_profiles_dashboard(args.home_dir, args.codex_bin)

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
