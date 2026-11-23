#!/usr/bin/env bash
# From the laptop: deploy to the prod VPS in one command.
#
#   deploy/remote.sh              push master, deploy the latest origin/master on the server
#   deploy/remote.sh v0.2.0       deploy a tag or commit (pushed first if it is a local tag)
#   deploy/remote.sh --dry-run    show what the server would do
#
# Host: $TGMUX_DEPLOY_HOST, default "aenaut" (alias in ~/.ssh/config).
set -Eeuo pipefail

host=${TGMUX_DEPLOY_HOST:-aenaut}
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)

if [[ ${1-} != --dry-run && ${1-} != --check ]]; then
    git -C "$repo" diff --quiet HEAD -- || printf 'warning: uncommitted changes are not deployed\n' >&2
    git -C "$repo" push --quiet origin master
    ref=${1-}
    if [[ -n $ref ]] && git -C "$repo" show-ref --verify --quiet "refs/tags/$ref"; then
        git -C "$repo" push --quiet origin "refs/tags/$ref"
    fi
fi

remote_args=
(( $# == 0 )) || printf -v remote_args ' %q' "$@"   # bash 3.2 (macOS): no empty "${a[@]}" under set -u
exec ssh -t "$host" "/opt/tgmux/deploy/prod.sh$remote_args"
