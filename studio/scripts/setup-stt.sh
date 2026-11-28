#!/bin/bash
set -euo pipefail
umask 077
runtime=${1:?Usage: setup-stt.sh RUNTIME_DIR [MODEL_REPO]}
model=${2:-mlx-community/whisper-large-v3-turbo}
[[ $(uname -s) = Darwin && $(uname -m) = arm64 ]]
mkdir -p "$runtime"
runtime=$(cd "$runtime" && pwd -P)

uv venv --allow-existing --python 3.12 "$runtime/stt"
uv pip install --python "$runtime/stt/bin/python" "mlx-whisper>=0.4.2,<1"
"$runtime/stt/bin/python" - "$model" <<'PY'
import sys
import mlx_whisper

model = sys.argv[1]
print(f"Downloading / verifying {model}...")
mlx_whisper.load_models.load_model(model)
print("Ready.")
PY
