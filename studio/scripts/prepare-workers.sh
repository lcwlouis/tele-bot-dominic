#!/bin/bash
set -euo pipefail
umask 077
studio=$(cd "$(dirname "$0")/.." && pwd -P)
runtime=${1:?Usage: prepare-workers.sh RUNTIME_DIR}
[[ $# -eq 1 ]]
[[ $(uname -s) = Darwin && $(uname -m) = arm64 ]]
mkdir -p "$runtime"
runtime=$(cd "$runtime" && pwd -P)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

checkout() {
    local repo=$1 revision=$2 target=$3
    if [[ ! -e "$target" ]]; then
        local staging
        staging=$(mktemp -d "$runtime/.checkout.XXXXXX")
        git -C "$staging" init --quiet
        git -C "$staging" fetch --quiet --depth 1 "$repo" "$revision"
        git -C "$staging" checkout --quiet --detach FETCH_HEAD
        mv "$staging" "$target"
    fi
    [[ "$(git -C "$target" rev-parse HEAD)" = "$revision" ]]
}

checkout https://github.com/soniqo/speech-swift.git 7f382bb4bf24d560e927893a64d8ffb9cf30bc89 "$runtime/speech-swift"
cp -R "$studio/swift" "$scratch/higgs"
for module in AudioCommon MLXCommon HiggsTTS; do
    # Copy the pinned source into the build tree so SwiftPM can validate package paths.
    cp -R "$runtime/speech-swift/Sources/$module" "$scratch/higgs/Sources/$module"
done
swift build --package-path "$scratch/higgs" --build-system native --force-resolved-versions -c release
higgs_bin=$(swift build --package-path "$scratch/higgs" --build-system native -c release --show-bin-path)
curl -fsSL https://github.com/soniqo/speech-swift/releases/download/v0.0.29/speech-macos-arm64.tar.gz -o "$scratch/speech.tar.gz"
(cd "$scratch" && shasum -a 256 -c <<<"ff426a060004dd39b3d00ad014a648747d48cb78b800672aa17221ada2453406  speech.tar.gz")
mkdir "$scratch/release"
tar -xzf "$scratch/speech.tar.gz" -C "$scratch/release"
metal=$(find "$scratch/release" -name mlx.metallib -type f -print -quit)
test -n "$metal"
mkdir -p "$runtime/bin"
install -m 644 "$metal" "$runtime/bin/mlx.metallib"
install -m 755 "$higgs_bin/DomHiggs" "$runtime/bin/DomHiggs"
for bundle in "$higgs_bin"/*.bundle; do
    if [[ -d "$bundle" ]]; then
        cp -R "$bundle" "$runtime/bin/"
    fi
done

sync_worker() {
    local kind=$1
    uv venv --allow-existing --python 3.12 "$runtime/$kind"
    uv pip sync --require-hashes --only-binary :all: --python "$runtime/$kind/bin/python" \
        "$studio/locks/$kind.txt"
}

sync_worker image

checkout https://github.com/manmastersg/soulX-flashHead-local.git e7dc9cbe563684b97670f66d19dcf8d2aa2c9045 "$runtime/flash-src"
checkout https://github.com/Soul-AILab/SoulX-FlashHead.git 9bc03de06bb0de82cd6bc477804512ae06144bf2 "$runtime/flash-upstream"
# Fill in the VAE source omitted by the Apple Silicon port.
/usr/bin/rsync -r --ignore-existing "$runtime/flash-upstream/flash_head/ltx_video/" "$runtime/flash-src/flash_head/ltx_video/"
sync_worker flash
"$runtime/flash/bin/python" - "$runtime/flash-src" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
port = (root / "gradio_app.py").read_text()
adapter = port.split("# ── Apple Silicon MPS Support", 1)[1]
adapter = adapter.split("\nimport torch as _torch", 1)[1]
adapter = "import torch as _torch" + adapter.split("\n# ── PyTorch SDPA", 1)[0]
(root / "mps_adapter.py").write_text(adapter)
PY

checkout https://github.com/dgrauet/ltx-2-mlx.git 747b30ef10e92a011e85b2aa0f6c4ee86ced5218 "$runtime/ltx-src"
(cd "$runtime/ltx-src" && uv sync --no-dev --locked --python 3.12 --package ltx-pipelines-mlx)

sync_worker hub
"$runtime/hub/bin/python" "$studio/scripts/models.py" "$runtime/models"
