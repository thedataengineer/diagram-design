#!/usr/bin/env bash
# Run .woodpecker/ci.yml on this machine with woodpecker-cli on the Docker
# backend. It does not call GitHub Actions or the Woodpecker server.
#
#   scripts/ci-local.sh                 # push event (version gate: --current-only)
#   scripts/ci-local.sh pull_request    # PR event (version gate vs origin/main)
#
# Uses woodpecker-cli from PATH when present, otherwise the pinned release,
# downloaded once into ~/.cache/diagram-design/.
set -euo pipefail

WOODPECKER_CLI_VERSION="v3.18.1"
event="${1:-push}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cli="$(command -v woodpecker-cli || true)"
if [ -z "$cli" ]; then
  cli="${HOME}/.cache/diagram-design/woodpecker-cli-${WOODPECKER_CLI_VERSION}"
  if [ ! -x "$cli" ]; then
    case "$(uname -s)-$(uname -m)" in
      Darwin-arm64) asset=darwin_arm64 ;;
      Darwin-x86_64) asset=darwin_amd64 ;;
      Linux-aarch64 | Linux-arm64) asset=linux_arm64 ;;
      Linux-x86_64) asset=linux_amd64 ;;
      *) echo "ci-local: unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
    esac
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL -o "$tmp/cli.tgz" \
      "https://github.com/woodpecker-ci/woodpecker/releases/download/${WOODPECKER_CLI_VERSION}/woodpecker-cli_${asset}.tar.gz"
    tar -xzf "$tmp/cli.tgz" -C "$tmp"
    mkdir -p "$(dirname "$cli")"
    install -m 755 "$tmp/woodpecker-cli" "$cli"
  fi
fi

cd "$root"
exec "$cli" exec --backend-engine docker --pipeline-event "$event" .woodpecker/ci.yml
