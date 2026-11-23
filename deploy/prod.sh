#!/usr/bin/env bash
# Deploy a git ref of tgmux to /opt/tgmux and restart the prod gateway.
#
#   deploy/prod.sh --dry-run REF   print what would happen
#   deploy/prod.sh --check REF     validate the currently deployed REF without restarting
#   deploy/prod.sh REF             checkout REF, install, validate, restart; roll back on failure
#
# Validation is `tgmux serve --check`: offline, never opens a second Telegram
# connection with the session the running gateway uses.
set -Eeuo pipefail

usage() {
    printf 'Usage: %s [--check|--dry-run] REF\n' "${0##*/}" >&2
}

mode=activate
case "${1-}" in
    --check|--dry-run) mode=${1#--}; shift ;;
    -h|--help) usage; exit 0 ;;
esac
[[ $# -eq 1 ]] || { usage; exit 2; }
ref=$1
[[ $ref != -* ]] || { printf 'error: REF must not begin with a dash\n' >&2; exit 2; }

prod_root=/opt/tgmux
python="$prod_root/.venv/bin/python"
tgmux="$prod_root/.venv/bin/tgmux"
unit=tgmux-prod.service
export TGMUX_INSTANCE=prod
settle=10   # seconds the gateway must stay up after a restart

if [[ $mode == dry-run ]]; then
    printf 'DRY RUN: git -C %q fetch, checkout --detach %q\n' "$prod_root" "$ref"
    printf 'DRY RUN: %q -m pip install -e %q, then %q serve --check (instance %s)\n' \
        "$python" "$prod_root" "$tgmux" "$TGMUX_INSTANCE"
    printf 'DRY RUN: systemctl restart %q, require it active after %ss; on failure restore the previous commit\n' \
        "$unit" "$settle"
    exit 0
fi

[[ -d $prod_root/.git ]] || { printf 'error: %s is not a git work tree\n' "$prod_root" >&2; exit 1; }
[[ -x $python ]] || { printf 'error: missing %s (python3 -m venv %s/.venv)\n' "$python" "$prod_root" >&2; exit 1; }

git_prod=(git -C "$prod_root")
"${git_prod[@]}" fetch --quiet --tags origin || printf 'warning: git fetch failed, using local refs\n' >&2
target=$("${git_prod[@]}" rev-parse --verify --end-of-options "${ref}^{commit}") || {
    printf 'error: REF does not resolve to a commit: %s\n' "$ref" >&2
    exit 1
}
old_ref=$("${git_prod[@]}" rev-parse --verify HEAD)

"${git_prod[@]}" diff --quiet -- && "${git_prod[@]}" diff --cached --quiet -- || {
    printf 'error: %s has local modifications\n' "$prod_root" >&2
    exit 1
}

install() {
    "$python" -m pip install --quiet --disable-pip-version-check -e "$prod_root"
}

if [[ $mode == check ]]; then
    [[ $target == "$old_ref" ]] || {
        printf 'error: --check REF must be the deployed commit (%s)\n' "$old_ref" >&2
        exit 1
    }
    "$tgmux" serve --check
    exit 0
fi

(( EUID == 0 )) || { printf 'error: activation must run as root\n' >&2; exit 1; }

restore() {
    "${git_prod[@]}" checkout --detach --quiet "$old_ref"
    install
}

started_ok() {
    systemctl restart "$unit" || return 1
    sleep "$settle"
    systemctl is-active --quiet "$unit"
}

"${git_prod[@]}" checkout --detach --quiet "$target"
if ! install || ! "$tgmux" serve --check; then
    printf 'error: candidate validation failed; restoring %s\n' "$old_ref" >&2
    restore
    exit 1
fi

if started_ok; then
    printf '%s is active at %s\n' "$unit" "$target"
    exit 0
fi

printf 'error: %s did not stay up; restoring %s and restarting once\n' "$unit" "$old_ref" >&2
journalctl -u "$unit" -n 20 --no-pager >&2 || true
restore
if started_ok; then
    printf 'rollback restored %s\n' "$old_ref" >&2
else
    printf 'error: rollback restored code but the service did not recover\n' >&2
fi
exit 1
