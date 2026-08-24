#!/bin/sh

set -eu

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

new_home() {
    case_name=$1
    test_home="${test_root}/${case_name}"
    mkdir -p "$test_home/.codex" \
        "$test_home/.codex-personal" \
        "$test_home/.codex-work" \
        "$test_home/.codex-gig"
    printf '%s\n' 'default-auth' > "$test_home/.codex/auth.json"
    printf '%s\n' 'keep-config' > "$test_home/.codex/config.toml"
    printf '%s\n' 'keep-history' > "$test_home/.codex/history.jsonl"
    printf '%s\n' 'personal-auth' > "$test_home/.codex-personal/auth.json"
    printf '%s\n' 'work-auth' > "$test_home/.codex-work/auth.json"
    printf '%s\n' 'gig-auth' > "$test_home/.codex-gig/auth.json"
}

new_home persistent
output=$(HOME="$test_home" "$command_path" personal)
assert_text_equals 'Active Codex profile: personal' "$output" 'switch confirmation is incorrect'
assert_file_equals "$test_home/.codex-personal/auth.json" "$test_home/.codex/auth.json" 'personal credentials were not activated'
assert_text_equals 'personal' "$(sed -n '1p' "$test_home/.codex/.codex-switch-active")" 'active marker is incorrect'
assert_text_equals 'keep-config' "$(sed -n '1p' "$test_home/.codex/config.toml")" 'config was changed'
assert_text_equals 'keep-history' "$(sed -n '1p' "$test_home/.codex/history.jsonl")" 'history was changed'

printf '%s\n' 'personal-refreshed' > "$test_home/.codex/auth.json"
HOME="$test_home" "$command_path" work >/dev/null
assert_text_equals 'personal-refreshed' "$(sed -n '1p' "$test_home/.codex-personal/auth.json")" 'outgoing credentials were not synced'
assert_text_equals 'work-auth' "$(sed -n '1p' "$test_home/.codex/auth.json")" 'work credentials were not activated'

printf '%s\n' 'work-refreshed' > "$test_home/.codex/auth.json"
HOME="$test_home" "$command_path" work >/dev/null
assert_text_equals 'work-refreshed' "$(sed -n '1p' "$test_home/.codex-work/auth.json")" 'same-account credentials were not synced'
assert_text_equals 'work-refreshed' "$(sed -n '1p' "$test_home/.codex/auth.json")" 'same-account switch changed active credentials'

mode=$(stat -f '%Lp' "$test_home/.codex/auth.json" 2>/dev/null || stat -c '%a' "$test_home/.codex/auth.json")
assert_text_equals '600' "$mode" 'active credentials do not have mode 600'
temp_count=$(find "$test_home/.codex" -maxdepth 1 -name '.*codex-switch.*' ! -name '.codex-switch-active' | wc -l | tr -d ' ')
assert_text_equals '0' "$temp_count" 'temporary files were left behind'

printf '%s\n' 'gig2-auth' > "$test_home/.codex/auth.json"
output=$(HOME="$test_home" "$command_path" add gig2)
assert_text_equals 'Added and activated Codex profile: gig2' "$output" 'add confirmation is incorrect'
assert_text_equals 'gig2-auth' "$(sed -n '1p' "$test_home/.codex-gig2/auth.json")" 'add did not save active credentials'
assert_text_equals 'gig2' "$(sed -n '1p' "$test_home/.codex/.codex-switch-active")" 'add did not reconcile the active marker'

if HOME="$test_home" "$command_path" add gig2 >/dev/null 2>&1; then
    fail 'adding a duplicate profile succeeded'
fi
for invalid_name in '../escape' '_leading' 'bad name' 'add'; do
    if HOME="$test_home" "$command_path" add "$invalid_name" >/dev/null 2>&1; then
        fail "adding an invalid profile succeeded: $invalid_name"
    fi
done

list_output=$(HOME="$test_home" "$command_path" list)
printf '%s\n' "$list_output" | grep -F 'gig2	(active)' >/dev/null || fail 'list did not mark gig2 active'
printf '%s\n' "$list_output" | grep -x 'personal' >/dev/null || fail 'list omitted a saved profile'

printf '%s\n' 'replacement-auth' > "$test_home/.codex/auth.json"
output=$(HOME="$test_home" "$command_path" auth personal)
assert_text_equals 'Updated auth for Codex profile: personal' "$output" 'auth confirmation is incorrect'
assert_text_equals 'replacement-auth' "$(sed -n '1p' "$test_home/.codex-personal/auth.json")" 'auth did not update saved credentials'
assert_text_equals 'personal' "$(sed -n '1p' "$test_home/.codex/.codex-switch-active")" 'auth did not update the active marker'
if HOME="$test_home" "$command_path" auth unknown >/dev/null 2>&1; then
    fail 'updating an unknown profile succeeded'
fi

printf '%s\n' 'preserve-me' > "$test_home/.codex-gig2/notes.txt"
HOME="$test_home" "$command_path" remove gig2 >/dev/null
[ ! -e "$test_home/.codex-gig2/auth.json" ] || fail 'remove retained profile credentials'
[ -f "$test_home/.codex-gig2/notes.txt" ] || fail 'remove deleted unrelated profile contents'
if HOME="$test_home" "$command_path" remove personal >/dev/null 2>&1; then
    fail 'removing the active profile succeeded'
fi
mkdir "$test_home/.codex-temporary"
printf '%s\n' 'temporary-auth' > "$test_home/.codex-temporary/auth.json"
HOME="$test_home" "$command_path" remove temporary >/dev/null
[ ! -e "$test_home/.codex-temporary" ] || fail 'remove retained an empty profile directory'

new_home invalid
cp "$test_home/.codex/auth.json" "$test_home/original-auth"
for invalid_args in '' 'unknown' 'personal extra'; do
    if HOME="$test_home" "$command_path" $invalid_args >/dev/null 2>&1; then
        fail "invalid arguments succeeded: ${invalid_args:-<none>}"
    fi
    assert_file_equals "$test_home/original-auth" "$test_home/.codex/auth.json" 'invalid arguments changed credentials'
done

new_home missing
rm -f "$test_home/.codex-work/auth.json"
cp "$test_home/.codex/auth.json" "$test_home/original-auth"
if HOME="$test_home" "$command_path" work >/dev/null 2>&1; then
    fail 'missing target credentials succeeded'
fi
assert_file_equals "$test_home/original-auth" "$test_home/.codex/auth.json" 'missing target changed credentials'
[ ! -e "$test_home/.codex/.codex-switch-active" ] || fail 'missing target created an active marker'

install_dir="${test_root}/installed/bin"
CODEX_SWITCH_INSTALL_DIR="$install_dir" "$installer_path" >/dev/null
assert_file_equals "$command_path" "$install_dir/codex-switch" 'installer copied different content'
[ -x "$install_dir/codex-switch" ] || fail 'installed command is not executable'

printf '%s\n' 'ok - all codex-switch tests passed'
