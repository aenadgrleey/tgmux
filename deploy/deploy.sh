#!/usr/bin/env bash
# Deploy a git ref of tgmux to /opt/tgmux and restart the gateway.
# Run on the server; installed as /usr/local/bin/tgmux-deploy.
#
#   tgmux-deploy                 deploy the latest origin/prod
#   tgmux-deploy REF             deploy a tag or commit: checkout, install, validate, restart;
#                                  roll back on failure; nothing happens if REF is already deployed
#   tgmux-deploy --dry-run [REF] print what would happen
#   tgmux-deploy --check [REF]   validate the currently deployed commit without restarting
#   tgmux-deploy --auto          what tgmux-deploy.timer runs every minute: deploy origin/prod if it
#                                  moved; silent otherwise; skips a commit that already failed
#
# Validation is `tgmux serve --check`: offline, never opens a second Telegram
# connection with the session the running gateway uses. Results go to the
# reporter bot (`tgmux reporter send`) when it is configured.
set -Eeuo pipefail

# The checkout below rewrites this very file; bash reads scripts lazily, so run from a copy.
if [[ -z ${TGMUX_DEPLOY_COPY-} ]]; then
    copy=$(mktemp "${TMPDIR:-/tmp}/tgmux-deploy.XXXXXX")
    cp -- "$0" "$copy"
    TGMUX_DEPLOY_COPY=$copy exec bash "$copy" "$@"
fi
trap 'rm -f -- "$TGMUX_DEPLOY_COPY"' EXIT

usage() {
    printf 'Usage: tgmux-deploy [--check|--dry-run|--auto] [REF]   (REF defaults to origin/prod)\n' >&2
}

mode=activate
case "${1-}" in
    --check|--dry-run|--auto) mode=${1#--}; shift ;;
    -h|--help) usage; exit 0 ;;
esac
[[ $# -le 1 ]] || { usage; exit 2; }
[[ $mode != auto || $# -eq 0 ]] || { printf 'error: --auto always deploys origin/prod\n' >&2; exit 2; }
ref=${1:-origin/prod}
[[ $ref != -* ]] || { printf 'error: REF must not begin with a dash\n' >&2; exit 2; }

root=/opt/tgmux
python="$root/.venv/bin/python"
tgmux="$root/.venv/bin/tgmux"
unit=tgmux.service
units=(tgmux.service tgmux-deploy.service tgmux-deploy.timer)
unset TGMUX_INSTANCE   # the gateway runs the default instance
settle=10              # seconds the gateway must stay up after a restart
failed_file=/root/.local/state/tgmux/deploy-failed   # commit the timer must not retry

if [[ $mode == dry-run ]]; then
    printf 'DRY RUN: git -C %q fetch, checkout --detach %q\n' "$root" "$ref"
    printf 'DRY RUN: %q -m pip install -e %q, install changed systemd units, then %q serve --check\n' \
        "$python" "$root" "$tgmux"
    printf 'DRY RUN: systemctl restart %q, require it active after %ss; on failure restore the previous commit\n' \
        "$unit" "$settle"
    exit 0
fi

[[ -d $root/.git ]] || { printf 'error: %s is not a git work tree\n' "$root" >&2; exit 1; }
[[ -x $python ]] || { printf 'error: missing %s (python3 -m venv %s/.venv)\n' "$python" "$root" >&2; exit 1; }

# One deploy at a time: the timer gives way, a manual run waits for it.
exec 9>/run/lock/tgmux-deploy.lock
if [[ $mode == auto ]]; then
    flock -n 9 || exit 0
else
    flock -w 300 9 || { printf 'error: another deploy is still running\n' >&2; exit 1; }
fi

notify() {
    "$tgmux" reporter send "$1" >/dev/null 2>&1 || true
}

git_root=(git -C "$root")
if ! "${git_root[@]}" fetch --quiet --prune --tags origin; then
    [[ $mode == auto ]] && exit 0   # network blip: try again next minute
    printf 'warning: git fetch failed, using local refs\n' >&2
fi
target=$("${git_root[@]}" rev-parse --verify --end-of-options "${ref}^{commit}") || {
    printf 'error: REF does not resolve to a commit: %s\n' "$ref" >&2
    exit 1
}
old_ref=$("${git_root[@]}" rev-parse --verify HEAD)

if [[ $mode == auto ]]; then
    [[ $target != "$old_ref" ]] || exit 0
    if [[ -f $failed_file && $(<"$failed_file") == "$target" ]]; then
        exit 0   # already failed and rolled back; waits for the next push to prod
    fi
    if ! systemctl is-active --quiet "$unit"; then
        printf 'skip: %s is not running (stopped on purpose?); deploy by hand: tgmux-deploy\n' "$unit"
        exit 0
    fi
fi

# Work lands on dev and is promoted with `git push origin dev:prod`; show what is still waiting.
if [[ $mode != auto && $ref == origin/prod ]] \
        && waiting=$("${git_root[@]}" rev-list --count origin/prod..origin/dev 2>/dev/null) \
        && (( waiting > 0 )); then
    printf 'note: dev is %s commit(s) ahead of prod (promote: git push origin dev:prod)\n' "$waiting" >&2
fi

"${git_root[@]}" diff --quiet -- && "${git_root[@]}" diff --cached --quiet -- || {
    printf 'error: %s has local modifications\n' "$root" >&2
    exit 1
}

install_units() {
    local changed=0 name
    for name in "${units[@]}"; do
        [[ -f $root/deploy/$name ]] || continue
        if ! cmp -s "$root/deploy/$name" "/etc/systemd/system/$name"; then
            cp -- "$root/deploy/$name" "/etc/systemd/system/$name"
            changed=1
        fi
    done
    if (( changed )); then
        systemctl daemon-reload
        systemctl enable --quiet tgmux-deploy.timer 2>/dev/null || true
        systemctl start tgmux-deploy.timer 2>/dev/null || true
    fi
}

install() {
    "$python" -m pip install --quiet --disable-pip-version-check -e "$root" && install_units
}

if [[ $mode == check ]]; then
    [[ $# -eq 0 || $target == "$old_ref" ]] || {
        printf 'error: --check REF must be the deployed commit (%s)\n' "$old_ref" >&2
        exit 1
    }
    "$tgmux" serve --check
    exit 0
fi

(( EUID == 0 )) || { printf 'error: activation must run as root\n' >&2; exit 1; }

if [[ $target == "$old_ref" ]] && systemctl is-active --quiet "$unit"; then
    printf '%s is already at %s (%s) and running; nothing to do\n' "$unit" "${target:0:7}" "$ref"
    exit 0
fi
changes=$("${git_root[@]}" log --oneline --no-decorate "$old_ref..$target" -- 2>/dev/null | head -10 || true)
printf 'deploying %s: %s -> %s\n' "$ref" "${old_ref:0:7}" "${target:0:7}"
[[ -z $changes ]] || printf '%s\n' "$changes" | sed 's/^/  /'

restore() {
    "${git_root[@]}" checkout --detach --quiet "$old_ref"
    install
}

started_ok() {
    systemctl restart "$unit" || return 1
    sleep "$settle"
    systemctl is-active --quiet "$unit"
}

failed() {   # $1: what failed
    printf '%s\n' "$target" > "$failed_file"
    notify "❌ деплой ${target:0:7} (${ref}) не прошёл: $1. Откат на ${old_ref:0:7}. Автодеплой ждёт следующего push в prod."
}

"${git_root[@]}" checkout --detach --quiet "$target"
if ! install || ! "$tgmux" serve --check; then
    printf 'error: candidate validation failed; restoring %s\n' "$old_ref" >&2
    restore
    failed "проверка (install / serve --check)"
    exit 1
fi

if started_ok; then
    rm -f -- "$failed_file"
    printf '%s is active at %s\n' "$unit" "$target"
    notify "🚀 выкачено ${target:0:7} (${ref}, было ${old_ref:0:7})${changes:+$'\n'$changes}"
    exit 0
fi

printf 'error: %s did not stay up; restoring %s and restarting once\n' "$unit" "$old_ref" >&2
journalctl -u "$unit" -n 20 --no-pager >&2 || true
restore
if started_ok; then
    printf 'rollback restored %s\n' "$old_ref" >&2
    failed "шлюз не поднялся"
else
    printf 'error: rollback restored code but the service did not recover\n' >&2
    failed "шлюз не поднялся, и после отката тоже не работает — нужна ручная проверка"
fi
exit 1
