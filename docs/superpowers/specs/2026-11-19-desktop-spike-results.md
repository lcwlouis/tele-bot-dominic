# Tiny computer spike — results (2026-11-19)

Ran on a Podman VM sized like the Pi 4B: arm64, 4 CPUs, 4GB RAM. This is a Mac M-series host, so **memory numbers transfer to the Pi; speed doesn't.** The Pi still needs a confirmation run.

**Workload:**
- open Google Maps (Singapore) plus Wikipedia
- run Python
- for AIO: run a numpy 1000×1000 matmul in Jupyter

| | AIO Sandbox (full) | AIO Sandbox (lean¹) | Cua `linux:24.04` | Cua `24.04-slim` |
|---|---|---|---|---|
| Image size | 8.4 GB | 8.4 GB | 3.3 GB | 1.5 GB |
| Idle RAM | 665 MB | **369 MB** | **199 MB** | 203 MB |
| Busy RAM | 1.23 GB | **799 MB** | **890 MB** | — |
| Browser | Chromium + CDP | same | Chromium on an XFCE desktop | Chromium |
| Python / Jupyter | 3.10–3.12, numpy, Jupyter | same | Python 3.12 only (no numpy or Jupyter) | **no Python** |
| Agent API | HTTP/JSON: `/v1/browser/*` (navigate, click, fill, markdown, screenshot), `/v1/bash/*`, `/v1/jupyter/*`, `/v1/file/*`, MCP | same | gRPC `cua.env.v1` on 3211; needs the Cua SDK | same |
| Human takeover | noVNC at `/vnc/index.html` | same | via the Cua viewer/SDK; no plain VNC page | same |
| Login handoff help | `/v1/browser/captcha/detect`, `captcha/wait`, cookie and state save/load | same | none built in | none |
| Auth | optional `SANDBOX_API_KEY` | same | token required (`CUA_ENV_TOKEN`) | same |

¹ Lean means `DISABLE_CODE_SERVER=true DISABLE_NODEJS_REPL=true DISABLE_MCP_BROWSER=true`. Jupyter and VNC stay on.

## Recommendation

**AIO Sandbox, run lean,** for `local` (Pi) mode.
- **It meets the acceptance bar** from spec §9: idle 0.37GB against the 1.2GB limit, peak 0.8GB against the 1.6GB limit.
- **The API is plain HTTP and maps directly onto `desktop_*` tools.** Its browser endpoints return markdown and element lists, so Dom doesn't need screenshots for every step.
- **It already has captcha and login-wait endpoints**, which suit the handoff flow.
- **Takeover is a VNC page** we can proxy behind the UI's auth.

Cua's desktop idles lower and feels like a real computer. Choosing it would mean adopting their gRPC SDK, installing a Python toolchain ourselves, and building the takeover path. That's more for us to own, for a smaller idle saving.

## Pi risks to confirm

- **Pull and storage.** The 8.4GB image on the SD card is the risk. The first Pi attempt (two parallel pulls) ran past 30 minutes, and the Pi then dropped off the network.
  - Pull one image at a time, detached.
  - Check SD wear and space. Consider a USB SSD.
  - Or put the image on the homelab for `remote` mode.
- **Speed.** Chromium on Pi 4 cores will be slower. Measure page load and screenshot latency there.
