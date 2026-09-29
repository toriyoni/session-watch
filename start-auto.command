#!/bin/zsh
# Run from Finder or a terminal; keep the Mac awake while polling.
set -e
cd -- "${0:A:h}"
uv_binary="$(command -v uv || true)"
if [[ -z "$uv_binary" && -x /opt/homebrew/bin/uv ]]; then
    uv_binary=/opt/homebrew/bin/uv
fi
if [[ -z "$uv_binary" ]]; then
    print -u2 'uv is required. Install uv, then run this launcher again.'
    exit 1
fi
run_args=(run --frozen)
if [[ -f .env ]]; then
    run_args+=(--env-file .env)
fi
exec /usr/bin/caffeinate -i "$uv_binary" "${run_args[@]}" tui --auto "$@"
