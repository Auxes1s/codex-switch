# codex-switch

`codex-switch` keeps each Codex account in its own authentication home so you can
move between personal, work, and client profiles without copying live OAuth tokens.

## Why authentication used to get revoked

The original command copied `auth.json` into and out of `~/.codex`. ChatGPT login
uses automatically refreshed credentials, so every copy could become a competing
refresh-token cache. A Codex process left open during a switch could refresh one
copy while the saved profile retained another, stale copy. The
[official Codex authentication docs](https://developers.openai.com/codex/auth)
describe automatic token refresh and the `CODEX_HOME`-scoped file credential store.

The new flow runs Codex with a profile-specific `CODEX_HOME` and forces
`cli_auth_credentials_store="file"`:

```text
~/.codex-personal/auth.json
~/.codex-work/auth.json
~/.codex-gig/auth.json
```

Tokens are never copied during switching or normal use. Codex refreshes the one
file belonging to the active profile. Configuration, skills, plugins, and the
Codex package installation are shared from `~/.codex`. Session transcripts and
history are also shared from `~/.codex` so every profile can browse and resume
all local sessions. Codex's runtime databases use the shared
`CODEX_SQLITE_HOME=~/.codex`, while credentials remain under the profile-specific
`CODEX_HOME`. Ambient credential variables (`OPENAI_API_KEY`, `CODEX_API_KEY`,
`CODEX_ACCESS_TOKEN`, and workload-identity settings) are cleared before Codex
starts so they cannot override the selected profile. Verified with Codex CLI
0.162.1 (and 0.155.0).

When shared session state is first enabled, existing profile session files are
merged into `~/.codex`. Existing SQLite metadata—including titles, projects,
goals, queues, and history—is also merged. Records with the same primary key
prefer the newer timestamp; unresolved conflicts stop the merge for review. The
original profile files and database snapshots are preserved under
`~/.codex/.codex-switch-legacy-state/` until you remove them yourself. Close
running Codex sessions before this one-time migration. Python 3 is required for
the migration and optional authentication diagnostics after a failed launch;
successful launches do not require Python.

## Set up profiles

Log in once for each account. `login` creates a missing profile automatically:

```sh
codex-switch login personal
codex-switch login work
codex-switch login gig
```

Complete each browser flow with the intended ChatGPT account. For a remote or
headless machine, pass the Codex device-login option:

```sh
codex-switch login work --device-auth
```

Existing `~/.codex-<profile>/auth.json` files are used in place, but a profile
whose token was already revoked must be logged out and authenticated once more:

```sh
codex-switch logout work
codex-switch login work
```

`auth` remains as an alias for `login`.
On macOS/Linux, a failed launch keeps Codex's original output and exit status.
When Python 3 is available, the launcher also asks Codex for authentication
details. A confirmed authentication failure names the profile and its login
command; revoked sessions include Codex's native refresh error. This diagnostic
has a six-second deadline and does not run after successful launches.
On Windows, `login` authenticates in a secured staging home and publishes its
credentials only after a successful login. To guard a profile by email domain:

```powershell
codex-switch login work --expect-domain example.com
```

The guard is checked by `switch`, `run`, and `status`. A failed publication
restores the profile's previous credentials and selection.

## Use profiles

Run Codex directly under a named profile:

```sh
codex-switch run personal
codex-switch run work exec "review the current changes"
```

Or select a default profile, then run through it:

```sh
codex-switch work
codex-switch run --
```

For the most fluid workflow, add this line to `~/.zshrc`:

```sh
eval "$(codex-switch shell-init)"
```

After opening a new shell, ordinary `codex` commands use the selected profile:

```sh
codex-switch personal
codex

codex-switch work
codex exec "run the test suite"
```

The shell integration affects the Codex CLI in that shell. The desktop app and IDE
extension keep using their own standard `~/.codex` login.

In PowerShell, add the equivalent wrapper to your profile:

```powershell
Invoke-Expression (codex-switch.ps1 shell-init | Out-String)
```

Windows `run` passes `--no-daemon` to avoid an unnecessary console process.
If hooks cause a visible window on your setup, set
`CODEX_SWITCH_DISABLE_HOOKS=1`; `run --with-hooks` overrides that setting.

## Manage profiles & usage dashboard

```sh
codex-switch list
codex-switch current
codex-switch status
codex-switch status work
codex-switch dashboard
codex-switch dashboard --web
codex-switch add client
codex-switch remove client
```

`dashboard` (or `home`) queries Codex for live rate limits, 5-hour and weekly window usage percentages, reset countdowns, and available free reset credits across all configured profiles:

- `codex-switch dashboard`: clean terminal view with progress bars, reset timers, and profile recommendations so you know which accounts have quota available right now.
- `codex-switch dashboard --web` (or `-w`): launches an interactive browser dashboard / homepage with live auto-refresh, visual progress meters, and one-click profile switching.
- `codex-switch dashboard --watch`: live-refreshing terminal dashboard.
- `codex-switch dashboard --json`: machine-readable JSON report of all profiles and limits.
- `codex-switch status --dashboard`: launches the limits dashboard via `status`.

`add` creates an unsigned profile without cloning credentials. `remove` refuses to
remove the active profile, deletes its credential file and links managed by this
tool, and preserves unrelated files in the profile directory. Removing a profile
does not delete shared sessions.

Do not commit, paste, or share any profile's `auth.json`; it contains access tokens.

## Install

```sh
./install.sh
```

This installs the command at `~/.local/bin/codex-switch`. Set
`CODEX_SWITCH_INSTALL_DIR` to install into a different directory.

On Windows, run `./install.ps1`; it installs the PowerShell launcher and migration
helper under `%LOCALAPPDATA%\Programs\codex-switch\bin` by default and adds that
directory to your user `PATH`. Open a new shell after the first installation.

## Test

```sh
./tests/codex-switch.test.sh
python3 ./tests/migration.test.py
```

From PowerShell, run `./tests/codex-switch.test.ps1` and
`python ./tests/migration.test.py` for the Windows suites.
