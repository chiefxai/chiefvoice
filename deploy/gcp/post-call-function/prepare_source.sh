#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
destination="${1:?usage: prepare_source.sh DESTINATION}"

rm -rf "$destination"
mkdir -p "$destination"
cp "$(dirname "${BASH_SOURCE[0]}")/main.py" "$destination/main.py"
cp "$(dirname "${BASH_SOURCE[0]}")/requirements.txt" "$destination/requirements.txt"
cp -R "$repo_root/src/chiefvoice" "$destination/chiefvoice"
