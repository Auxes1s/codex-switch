# codex-switch

`codex-switch` manages authentication profiles and persistently selects which one
the normal Codex home uses.

```sh
codex-switch personal
codex-switch work
codex-switch gig
```

The original one-argument syntax remains shorthand for `switch`. Profiles are
discovered dynamically, and can be managed with subcommands:

```sh
codex-switch list
codex-switch switch work
codex-switch add gig2
codex-switch auth gig2
codex-switch remove gig2
```

`add` copies the current Codex `auth.json` into a new profile. `auth` replaces
an existing profile's saved credentials with that same current auth. Both commands
mark the named profile active, keeping the marker consistent with the active
credentials. This makes it possible to log in through Codex normally and then save
the resulting credentials as a profile.

The command copies credentials from a saved profile to the active Codex home and
exits. Future ordinary `codex` commands therefore use the selected profile.
Configuration, plugins, history, sessions, and other files in the active Codex
home are not changed. On macOS and Linux the paths are
`~/.codex-<profile>/auth.json` and `~/.codex/auth.json`; the Windows paths are
described below.

Before switching again, the current credentials are synced back to the previously
selected profile. This preserves refreshed tokens. The first switch deliberately
overwrites an untracked `~/.codex/auth.json` without creating a backup.

`remove` refuses to remove the active profile. For an inactive profile it deletes
only `auth.json`, and removes the profile directory only when the directory is then
empty; unrelated files are preserved.

## Install on macOS or Linux

```sh
./install.sh
```

This installs the command at `~/.local/bin/codex-switch`. Set
`CODEX_SWITCH_INSTALL_DIR` to install into a different directory.

## Install on Windows

From PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The installer copies the PowerShell command and its `cmd.exe` launcher to
`%LOCALAPPDATA%\Programs\codex-switch\bin`, then adds that directory to your user
`PATH`. Open a new terminal after the first installation, then use the same
commands as on macOS and Linux:

```powershell
codex-switch list
codex-switch personal
codex-switch switch work
```

Set `CODEX_SWITCH_INSTALL_DIR` to choose another installation directory. Pass
`-NoPathUpdate` if the installer should not change your user `PATH`.

Native Codex on Windows stores its files in `%USERPROFILE%\.codex`. If
`CODEX_HOME` is set, `codex-switch` uses that directory instead and stores saved
profiles beside it as `<CODEX_HOME>-<profile>`.

WSL has a separate Linux home by default. Run the existing shell version inside
WSL to manage WSL-only profiles. If WSL's `CODEX_HOME` points at the Windows Codex
directory, use the native Windows `codex-switch` command to manage those shared
credentials as well.

## Test

```sh
./tests/codex-switch.test.sh
```

On Windows, run:

```powershell
.\tests\codex-switch.test.ps1
```
