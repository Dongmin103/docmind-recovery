#!/usr/bin/env bash
set -euo pipefail

if [[ "${SURYA_MODEL_LICENSE_ACCEPTED:-0}" != "1" ]]; then
  printf '%s\n' \
    'Review the Surya MODEL_LICENSE, then set SURYA_MODEL_LICENSE_ACCEPTED=1.' >&2
  exit 2
fi

target_dir="${1:-./models/surya}"
revision="6a3a4c30e5e74446d4f8b6afd05b2f2da970f470"
repo_url="https://huggingface.co/datalab-to/surya-ocr-2-gguf/resolve/${revision}"

mkdir -p "${target_dir}"

download_and_verify() {
  local filename="$1"
  local expected_sha="$2"
  local destination="${target_dir}/${filename}"

  if [[ ! -f "${destination}" ]]; then
    curl --retry 5 --retry-delay 2 --retry-all-errors -fL \
      "${repo_url}/${filename}?download=true" \
      -o "${destination}.partial"
    mv "${destination}.partial" "${destination}"
  fi

  echo "${expected_sha}  ${destination}" | sha256sum -c -
}

download_and_verify \
  "surya-2.gguf" \
  "1f18abe17b1ed8b4e47ee9b1ad0e274c93daf5efbb6b29a04ff1712e37051e05"
download_and_verify \
  "surya-2-mmproj.gguf" \
  "98c0563673b1657ff6d021d1e5f04af06cbf61bb40c63ac613e8bb71b42fb2c0"

printf 'Surya model files verified in %s\n' "${target_dir}"
