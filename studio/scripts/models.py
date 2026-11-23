"""Fetch and hash-check only the pinned model files used by the studio."""

import argparse
from fnmatch import fnmatchcase
import hashlib
import os
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.hf_api import RepoFile

MODELS = {
    "higgs": ("aufklarer/Higgs-TTS-3-4B-MLX-bf16", "88ea5e087fa30f0e22a56cdedc919854fdbe9436", ("*",)),
    "klein": ("black-forest-labs/FLUX.2-klein-4B", "e7b7dc27f91deacad38e78976d1f2b499d76a294",
              ("*.json", "*.safetensors", "*.txt", "*.jinja", "LICENSE*", "README.md")),
    "flash": ("Soul-AILab/SoulX-FlashHead-1_3B", "74d81686a7bd750b276ec1ade295bc4077c5868a",
              ("Model_Lite/*", "VAE_LTX/*", "*.json", "README.md")),
    "wav2vec": ("facebook/wav2vec2-base-960h", "22aad52d435eb6dbaf354bdad9b0da84ce7d6156",
                ("*.json", "model.safetensors", "pytorch_model.bin", "README.md")),
    "ltx": ("dgrauet/ltx-2.3-mlx-q4", "f306259444116d71927188af2fbbf7fdc1a8089a",
            ("*.json", "LICENSE", "README.md", "transformer-distilled.safetensors",
             "connector.safetensors", "vae_decoder.safetensors", "vae_encoder.safetensors",
             "audio_vae.safetensors", "vocoder.safetensors", "spatial_upscaler_x2_v1_1.safetensors")),
    "gemma": ("mlx-community/gemma-3-12b-it-4bit", "86cc6a8dedbc456dd0e4af01a9d09f396f77e558",
              ("*.json", "*.safetensors", "*.model", "*.jinja", "README.md")),
}


def verify(path, entry):
    if path.stat().st_size != entry.size:
        raise ValueError(f"wrong size: {entry.path}")
    if entry.lfs:
        digest, expected = hashlib.sha256(), entry.lfs.sha256
    else:
        digest, expected = hashlib.sha1(usedforsecurity=False), entry.blob_id
        digest.update(f"blob {entry.size}\0".encode())
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError(f"wrong hash: {entry.path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cache", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    cache = args.cache.resolve()
    api = HfApi()
    for name, (repo, revision, patterns) in MODELS.items():
        files = [
            entry for entry in api.list_repo_tree(repo, revision=revision, recursive=True)
            if isinstance(entry, RepoFile) and any(fnmatchcase(entry.path, pattern) for pattern in patterns)
        ]
        if not files:
            raise ValueError(f"no files selected for {repo}")
        expected = cache / ("models--" + repo.replace("/", "--")) / "snapshots" / revision
        if not args.verify_only:
            snapshot = snapshot_download(repo, revision=revision, cache_dir=cache,
                                         allow_patterns=[entry.path for entry in files])
            if Path(snapshot) != expected:
                raise ValueError(f"unexpected snapshot for {repo}")
        for entry in files:
            verify(expected / entry.path, entry)
        print(f"{name}: {expected}")


if __name__ == "__main__":
    os.umask(0o077)
    main()
