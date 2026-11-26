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

### Over Tailscale

Tailscale is the recommended way to reach the studio when the bot host is on another network, or to avoid tying the studio to home-network addresses that change. Put the studio host and every bot host on one tailnet. Set `bind_host` to the studio host's Tailscale address (`tailscale ip -4`). List each bot host in `allowed_sources` by its MagicDNS name (`<bot-host>.<tailnet>.ts.net`) or its Tailscale address. Names ending in `.ts.net` are looked up again regularly, like `.local` names. In Dom's config, set `[studio] base_url` to `http://<studio host's MagicDNS name or Tailscale address>:<port>`. The bearer token is still required, and traffic between hosts is encrypted by Tailscale. A tailnet access rule limiting the studio's port to the bot hosts is a sensible extra layer.

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

## Wardrobe portraits

Each outfit needs its own approved portrait, and only the reference outfit (`default_outfit`) is real at first.
`python -m studio_service.portraits generate studio.toml` makes one candidate portrait per other outfit from the
reference outfit's portrait and turnaround, using the image worker and each outfit's optional `description`
(for example `description = "a plain grey hoodie"`; the name is used when it is empty). Candidates are saved under
`candidates/<outfit>/` beside the reference portrait; nothing configured is touched. Name outfits with `--outfit`,
fix a render with `--seed`. Look at the candidates, then
`python -m studio_service.portraits approve studio.toml <outfit> <candidate.png>` copies one over that outfit's
portrait and keeps the old one as `<portrait>.previous`. Stop the studio service first: the image worker needs the
memory the service holds. For the cloud backups, also copy each approved portrait to the bot's
`DOM_HOME/studio-portraits/<outfit>.png`.

## Video notes from a still

`POST /v1/video_note` takes an optional `still`, a base64 PNG of at most 3 MiB, to animate in place of the outfit's
portrait. `GET /v1/health` reports `"video_still": true` when the studio takes one; the bot sends it only then.
