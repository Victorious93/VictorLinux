#!/usr/bin/env bash
# Download the VM's tiny CPU fixture on the host. The live guest stays offline.
set -euo pipefail

destination=${1:?Usage: fetch_vm_model.sh OUTPUT.gguf}
revision=499bc8821c6b12b4e53c5bffcb21ec206f212d81
digest=66967fbece6dbe97886593fdbb73589584927e29119ec31f08090732d1861739
url="https://huggingface.co/ggml-org/models/resolve/$revision/tinyllamas/stories15M-q4_0.gguf"

mkdir -p "$(dirname "$destination")"
temporary=$(mktemp "${destination}.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 20 --max-time 120 --retry 3 --output "$temporary" "$url"
printf '%s  %s\n' "$digest" "$temporary" | sha256sum --check --status
mv -- "$temporary" "$destination"
