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
`CODEX_HOME`.

When shared session state is first enabled, existing profile session files are
merged into `~/.codex`. Existing SQLite metadata—including titles, projects,
goals, queues, and history—is also merged, preferring the newest conflicting
record. The original profile files and database snapshots are preserved under
`~/.codex/.codex-switch-legacy-state/` until you remove them yourself. Close
running Codex sessions before this one-time migration. Python 3 is required for
the migration only, not for normal profile use afterward.

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
Invoke-Expression (codex-switch shell-init | Out-String)
```

## Manage profiles

```sh
codex-switch list
codex-switch current
codex-switch status
codex-switch status work
codex-switch add client
codex-switch remove client
```

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
helper under `%LOCALAPPDATA%\Programs\codex-switch\bin` by default.

## Test

```sh
./tests/codex-switch.test.sh
```

Run `./tests/codex-switch.test.ps1` from PowerShell for the Windows suite.
