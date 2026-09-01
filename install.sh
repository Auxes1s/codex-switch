#!/bin/sh

set -eu

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)

if [ -n "${CODEX_SWITCH_INSTALL_DIR-}" ]; then
    install_dir=$CODEX_SWITCH_INSTALL_DIR
else
    case "${HOME-}" in
        /*) install_dir="${HOME}/.local/bin" ;;
        *)
            printf '%s\n' 'install.sh: HOME must be set to an absolute path' >&2
            exit 1
            ;;
    esac
fi

umask 022
mkdir -p "$install_dir"

install_tmp="${install_dir}/.codex-switch.install.$$"
cleanup() {
    rm -f "$install_tmp"
}
trap 'cleanup' EXIT HUP INT TERM

install -m 755 "${script_dir}/bin/codex-switch" "$install_tmp"
mv -f "$install_tmp" "${install_dir}/codex-switch"

printf 'Installed codex-switch to %s\n' "${install_dir}/codex-switch"
printf '%s\n' 'For transparent profile routing, add this to your shell config:'
printf '%s\n' '  eval "$(codex-switch shell-init)"'
