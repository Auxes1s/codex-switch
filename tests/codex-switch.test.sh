#!/bin/sh

set -eu
unset CODEX_HOME CODEX_SQLITE_HOME

repo_dir=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
command_path="${repo_dir}/bin/codex-switch"
installer_path="${repo_dir}/install.sh"
test_root=$(mktemp -d "${TMPDIR:-/tmp}/codex-switch-tests.XXXXXX")

cleanup() {
    rm -rf "$test_root"
}
trap 'cleanup' EXIT HUP INT TERM

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

assert_file_equals() {
    expected=$1
    actual=$2
    message=$3
    cmp -s "$expected" "$actual" || fail "$message"
}

assert_text_equals() {
    expected=$1
    actual=$2
    message=$3
    [ "$expected" = "$actual" ] || fail "$message (expected '$expected', got '$actual')"
}

assert_arg() {
    expected=$1
    line=$2
    message=$3
    actual=$(sed -n "${line}p" "${capture_base}.args")
    assert_text_equals "$expected" "$actual" "$message"
}

new_home() {
    case_name=$1
    test_home="${test_root}/${case_name}"
    mkdir -p "$test_home/.codex" \
        "$test_home/.codex-personal" \
        "$test_home/.codex-work" \
        "$test_home/.codex-gig" \
        "$test_home/.codex/sessions/2026/01" \
        "$test_home/.codex-personal/sessions/2026/02" \
        "$test_home/.codex-work/sessions/2026/03" \
        "$test_home/.codex/skills" \
        "$test_home/.codex/plugins" \
        "$test_home/.codex/packages"
    printf '%s\n' 'central-auth-must-not-change' > "$test_home/.codex/auth.json"
    printf '%s\n' 'shared-config' > "$test_home/.codex/config.toml"
    printf '%s\n' 'personal-auth' > "$test_home/.codex-personal/auth.json"
    printf '%s\n' 'work-auth' > "$test_home/.codex-work/auth.json"
    printf '%s\n' 'gig-auth' > "$test_home/.codex-gig/auth.json"
    printf '%s\n' 'central-session' > "$test_home/.codex/sessions/2026/01/central.jsonl"
    printf '%s\n' 'personal-session' > "$test_home/.codex-personal/sessions/2026/02/personal.jsonl"
    printf '%s\n' 'work-session' > "$test_home/.codex-work/sessions/2026/03/work.jsonl"
    printf '%s\n' '{"id":"central-session","thread_name":"central","updated_at":"2026-01-01T00:00:00Z"}' > "$test_home/.codex/session_index.jsonl"
    printf '%s\n' '{"id":"personal-session","thread_name":"personal","updated_at":"2026-02-01T00:00:00Z"}' > "$test_home/.codex-personal/session_index.jsonl"
    printf '%s\n' '{"id":"work-session","thread_name":"work","updated_at":"2026-03-01T00:00:00Z"}' > "$test_home/.codex-work/session_index.jsonl"
}

codex_stub="${test_root}/codex-stub"
cat > "$codex_stub" <<'EOF'
#!/bin/sh
set -eu
printf '%s\n' "$CODEX_HOME" > "${CODEX_SWITCH_CAPTURE}.home"
printf '%s\n' "$CODEX_SQLITE_HOME" > "${CODEX_SWITCH_CAPTURE}.sqlite-home"
printf '%s\n' "$@" > "${CODEX_SWITCH_CAPTURE}.args"
if [ "${OPENAI_API_KEY+x}${CODEX_API_KEY+x}${CODEX_ACCESS_TOKEN+x}${OPENAI_IDENTITY_TOKEN_FILE+x}${OPENAI_FEDERATION_RULE_ID+x}" ]; then
    printf '%s\n' 'present' > "${CODEX_SWITCH_CAPTURE}.auth-env"
else
    printf '%s\n' 'absent' > "${CODEX_SWITCH_CAPTURE}.auth-env"
fi
if [ -n "${CODEX_SWITCH_WRITE_AUTH-}" ]; then
    printf '%s\n' "$CODEX_SWITCH_WRITE_AUTH" > "$CODEX_HOME/auth.json"
fi
printf '%s\n' 'codex-stub-ok'
EOF
chmod 755 "$codex_stub"

new_home isolated
cp "$test_home/.codex/auth.json" "$test_home/central-auth-original"
cp "$test_home/.codex-personal/auth.json" "$test_home/personal-auth-original"

output=$(HOME="$test_home" "$command_path" personal)
assert_text_equals 'Selected Codex profile: personal' "$output" 'switch confirmation is incorrect'
assert_text_equals 'personal' "$(sed -n '1p' "$test_home/.codex/.codex-switch-active")" 'active marker is incorrect'
assert_file_equals "$test_home/central-auth-original" "$test_home/.codex/auth.json" 'switch changed central credentials'
assert_file_equals "$test_home/personal-auth-original" "$test_home/.codex-personal/auth.json" 'switch changed profile credentials'
assert_text_equals "$test_home/.codex/config.toml" "$(readlink "$test_home/.codex-personal/config.toml")" 'profile does not share config'
assert_text_equals "$test_home/.codex/skills" "$(readlink "$test_home/.codex-personal/skills")" 'profile does not share skills'
assert_text_equals "$test_home/.codex/sessions" "$(readlink "$test_home/.codex-personal/sessions")" 'profile does not share sessions'
assert_file_equals "$test_home/.codex/.codex-switch-legacy-state/personal/sessions/2026/02/personal.jsonl" "$test_home/.codex/sessions/2026/02/personal.jsonl" 'personal session was not merged'
grep -F 'personal-session' "$test_home/.codex/session_index.jsonl" >/dev/null || fail 'personal session index was not merged'
assert_text_equals 'personal-auth' "$(sed -n '1p' "$test_home/.codex-personal/auth.json")" 'sharing sessions changed profile credentials'

capture_base="${test_root}/run-work"
output=$(HOME="$test_home" CODEX_SWITCH_CODEX_BIN="$codex_stub" \
    CODEX_SWITCH_CAPTURE="$capture_base" CODEX_SWITCH_WRITE_AUTH='work-refreshed' OPENAI_API_KEY='test-key' CODEX_API_KEY='test-key' \
    "$command_path" run work exec 'do the thing')
assert_text_equals 'codex-stub-ok' "$output" 'run did not execute Codex'
assert_text_equals "$test_home/.codex-work" "$(sed -n '1p' "${capture_base}.home")" 'run used the wrong CODEX_HOME'
assert_text_equals "$test_home/.codex" "$(sed -n '1p' "${capture_base}.sqlite-home")" 'run used the wrong shared SQLite home'
assert_text_equals 'absent' "$(sed -n '1p' "${capture_base}.auth-env")" 'run inherited an overriding auth variable'
assert_arg '-c' 1 'run omitted the config override flag'
assert_arg 'cli_auth_credentials_store="file"' 2 'run did not force file credential storage'
assert_arg 'exec' 3 'run lost the Codex subcommand'
assert_arg 'do the thing' 4 'run lost a Codex argument'
assert_text_equals 'work-refreshed' "$(sed -n '1p' "$test_home/.codex-work/auth.json")" 'Codex did not refresh the selected profile directly'
assert_file_equals "$test_home/central-auth-original" "$test_home/.codex/auth.json" 'run changed central credentials'
assert_file_equals "$test_home/personal-auth-original" "$test_home/.codex-personal/auth.json" 'run changed another profile'
assert_text_equals 'work' "$(sed -n '1p' "$test_home/.codex/.codex-switch-active")" 'explicit run did not select its profile'
assert_text_equals "$test_home/.codex/sessions" "$(readlink "$test_home/.codex-work/sessions")" 'work profile does not share sessions'
grep -F 'work-session' "$test_home/.codex/session_index.jsonl" >/dev/null || fail 'work session index was not merged'

capture_base="${test_root}/run-active"
HOME="$test_home" CODEX_SWITCH_CODEX_BIN="$codex_stub" \
    CODEX_SWITCH_CAPTURE="$capture_base" "$command_path" run -- review >/dev/null
assert_text_equals "$test_home/.codex-work" "$(sed -n '1p' "${capture_base}.home")" 'active run used the wrong CODEX_HOME'
assert_arg 'review' 3 'active run lost the Codex argument'

capture_base="${test_root}/status"
HOME="$test_home" CODEX_SWITCH_CODEX_BIN="$codex_stub" \
    CODEX_SWITCH_CAPTURE="$capture_base" "$command_path" status personal >/dev/null
assert_text_equals "$test_home/.codex-personal" "$(sed -n '1p' "${capture_base}.home")" 'status used the wrong CODEX_HOME'
assert_arg 'login' 3 'status omitted the login subcommand'
assert_arg 'status' 4 'status omitted the status subcommand'

capture_base="${test_root}/login-new"
HOME="$test_home" CODEX_SWITCH_CODEX_BIN="$codex_stub" \
    CODEX_SWITCH_CAPTURE="$capture_base" CODEX_SWITCH_WRITE_AUTH='new-auth' \
    "$command_path" login new --device-auth >/dev/null
assert_text_equals "$test_home/.codex-new" "$(sed -n '1p' "${capture_base}.home")" 'login used the wrong CODEX_HOME'
assert_arg 'login' 3 'login omitted the Codex login subcommand'
assert_arg '--device-auth' 4 'login lost its argument'
assert_text_equals 'new-auth' "$(sed -n '1p' "$test_home/.codex-new/auth.json")" 'login did not store auth in the profile'
mode=$(stat -c '%a' "$test_home/.codex-new/auth.json" 2>/dev/null || stat -f '%Lp' "$test_home/.codex-new/auth.json")
assert_text_equals '600' "$mode" 'profile credentials do not have mode 600'
assert_text_equals 'new' "$(HOME="$test_home" "$command_path" current)" 'login did not select its profile'

output=$(HOME="$test_home" "$command_path" add unsigned)
printf '%s\n' "$output" | grep -F 'Added and selected Codex profile: unsigned' >/dev/null || fail 'add confirmation is incorrect'
[ ! -e "$test_home/.codex-unsigned/auth.json" ] || fail 'add copied credentials into a new profile'
assert_text_equals "$test_home/.codex/plugins" "$(readlink "$test_home/.codex-unsigned/plugins")" 'add did not share plugins'
assert_text_equals "$test_home/.codex/sessions" "$(readlink "$test_home/.codex-unsigned/sessions")" 'add did not share sessions'

if HOME="$test_home" "$command_path" add unsigned >/dev/null 2>&1; then
    fail 'adding a duplicate profile succeeded'
fi
for invalid_name in '../escape' '_leading' 'bad name' 'run'; do
    if HOME="$test_home" "$command_path" add "$invalid_name" >/dev/null 2>&1; then
        fail "adding an invalid profile succeeded: $invalid_name"
    fi
done

list_output=$(HOME="$test_home" "$command_path" list)
printf '%s\n' "$list_output" | grep -F 'unsigned	(signed out)	(active)' >/dev/null || fail 'list did not mark unsigned signed out and active'
printf '%s\n' "$list_output" | grep -x 'personal' >/dev/null || fail 'list omitted a saved profile'

capture_base="${test_root}/logout"
HOME="$test_home" CODEX_SWITCH_CODEX_BIN="$codex_stub" \
    CODEX_SWITCH_CAPTURE="$capture_base" "$command_path" logout personal >/dev/null
assert_arg 'logout' 3 'logout omitted the Codex logout subcommand'

HOME="$test_home" "$command_path" switch new >/dev/null
printf '%s\n' 'preserve-me' > "$test_home/.codex-unsigned/notes.txt"
HOME="$test_home" "$command_path" remove unsigned >/dev/null
[ ! -e "$test_home/.codex-unsigned/auth.json" ] || fail 'remove retained profile credentials'
[ ! -e "$test_home/.codex-unsigned/config.toml" ] || fail 'remove retained a managed config link'
[ -f "$test_home/.codex-unsigned/notes.txt" ] || fail 'remove deleted unrelated profile contents'
if HOME="$test_home" "$command_path" remove new >/dev/null 2>&1; then
    fail 'removing the active profile succeeded'
fi

new_home invalid
cp "$test_home/.codex/auth.json" "$test_home/central-auth-original"
for invalid_args in '' 'unknown' 'personal extra'; do
    if HOME="$test_home" "$command_path" $invalid_args >/dev/null 2>&1; then
        fail "invalid arguments succeeded: ${invalid_args:-<none>}"
    fi
    assert_file_equals "$test_home/central-auth-original" "$test_home/.codex/auth.json" 'invalid arguments changed central credentials'
done

rm -f "$test_home/.codex-work/auth.json"
if HOME="$test_home" "$command_path" work >/dev/null 2>&1; then
    fail 'switching to a signed-out profile succeeded'
fi
assert_file_equals "$test_home/central-auth-original" "$test_home/.codex/auth.json" 'missing profile auth changed central credentials'

shell_init=$(HOME="$test_home" "$command_path" shell-init)
printf '%s\n' "$shell_init" | grep -F 'command codex-switch run -- "$@"' >/dev/null || fail 'shell-init did not wrap Codex'

dashboard_output=$(HOME="$test_home" CODEX_SWITCH_CODEX_BIN="$codex_stub" "$command_path" dashboard --json)
printf '%s\n' "$dashboard_output" | grep -F '"profiles":' >/dev/null || fail 'dashboard --json failed to emit JSON profiles'

status_dash_output=$(HOME="$test_home" CODEX_SWITCH_CODEX_BIN="$codex_stub" "$command_path" status --json)
printf '%s\n' "$status_dash_output" | grep -F '"profiles":' >/dev/null || fail 'status --json failed to emit JSON profiles'

install_dir="${test_root}/installed/bin"
CODEX_SWITCH_INSTALL_DIR="$install_dir" "$installer_path" >/dev/null
assert_file_equals "$command_path" "$install_dir/codex-switch" 'installer copied different content'
assert_file_equals "$repo_dir/bin/codex-switch-migrate.py" "$install_dir/codex-switch-migrate.py" 'installer omitted the migration helper'
assert_file_equals "$repo_dir/bin/codex-switch-diagnose.py" "$install_dir/codex-switch-diagnose.py" 'installer omitted the diagnostic helper'
assert_file_equals "$repo_dir/bin/codex-switch-dashboard.py" "$install_dir/codex-switch-dashboard.py" 'installer omitted the dashboard helper'
[ -x "$install_dir/codex-switch" ] || fail 'installed command is not executable'

python3 "$repo_dir/tests/migration.test.py"
python3 "$repo_dir/tests/diagnostics.test.py"
python3 "$repo_dir/tests/dashboard.test.py"

printf '%s\n' 'ok - all codex-switch tests passed'
