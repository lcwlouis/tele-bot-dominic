# Dom Studio

The studio runs local media inference for a personal bot on an Apple Silicon Mac. The [studio design](../docs/superpowers/specs/2026-11-24-studio-design.md) describes its HTTP API, memory reservation, model terms and live acceptance checks.

## Prepare the host

Use a logged-in GUI session with `uv`, Git, the Swift toolchain, FFmpeg with libopus and libx264, and ffprobe. Choose a private directory for weights, approved assets and configuration. The worker preparation script builds the pinned Swift runner, obtains its verified Metal library, prepares separate Python runtimes and downloads and verifies the pinned snapshots. Run these commands from the `studio/` checkout:

```sh
scripts/prepare-workers.sh "$STUDIO_RUNTIME"
python3 scripts/create-token.py "$STUDIO_TOKEN_FILE"
```

The model download prints each snapshot path. To verify existing snapshots against their pinned metadata:

```sh
uv run --no-project --with 'huggingface_hub==1.33.0' scripts/models.py "$STUDIO_RUNTIME/models" --verify-only
```

Use the same token in the bot's private environment. Keep the token path, reference WAV and transcript, portraits and outfit references outside the checkout. Keep the dedicated snapshot cache out of automatic detached-snapshot pruning.

## Configure the studio

Copy [studio.example.toml](studio.example.toml) to the private directory as `studio.toml`. Replace the sample addresses and paths. Use the real LAN interface address for `bind_host` and prefer one address per allowed interface of the bot host. Include local container and VM CIDRs in `denied_sources`, even if they overlap an allowed range. The built-in denies cover loopback, the host's interfaces and the standard desktop VM and bridge ranges. If the host also runs desktop sandboxes, copy its maximum container count, per-container memory in MiB and minimum free memory into `[desktop]`.

Supply all worker paths as absolute paths. For the video worker, use the prepared `flash-src` checkout as its working directory. The special worker uses the `ltx-2-mlx` executable in the prepared checkout's virtual environment. Put local `ffmpeg` and `ffprobe` on the same search path for the LTX runtime. The worker commands receive only local snapshot and asset paths.

```sh
scripts/install.sh "$STUDIO_CONFIG"
scripts/uninstall.sh
```

The LaunchAgent logs to `studio.log` beside the private config. Uninstall stops the agent and removes its plist. The private assets and model cache are retained.

## Local tests

```sh
uv run --locked pytest -q
```

The tests use fake JSON-line workers, an encoder stub and fake memory readings.
