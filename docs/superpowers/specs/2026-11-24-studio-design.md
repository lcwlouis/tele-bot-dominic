# Studio design for v0.8.3

Base v0.8.2. Release v0.8.3. The E59 gate is two fresh reviews, Gemini 4 Neon at high and Fable 5.2 at medium, repeated until both find zero drift. Neon drops to medium only if the weekly quota is still tight when the gate starts.

## 1. Goal

Dom can send voice notes, round video notes and images from a local Apple Silicon studio. Dom is a personal bot. The studio is optional and falls back to the v0.8.2 experience when it is down. Incoming voice transcription continues to use the configured media model. Semantic memory, live voice calls, cloud media generation, automated image scoring and generated media reuse are outside this release.

## 2. Runtime choices

| Job | Engine | Measured memory and render time |
|---|---|---|
| Voice | Higgs TTS 3, Swift MLX BF16, with an approved voice reference and transcript | 8.6 GiB idle, 11.5–11.8 GiB peak, about 5.5 s for a 6.6 s line |
| Everyday video | SoulX-FlashHead Lite via MPS at 512 × 512 | 5.4 GiB idle, 8.4 GiB peak, about 63 s for 6.6 s |
| Image | FLUX.2 Klein 4B Q4 via mflux, 512 px reference and 768 px output | 4.6 GiB idle, 6.4 GiB peak, about 23 s |
| Special video | LTX-2.3 distilled Q4 via MLX block streaming | About 23 GiB peak and 237 s for 6.6 s, measured with other models unloaded |

The three everyday models were measured at 18.6 GiB settled idle and 21.7 GiB peak together. The lowest available memory in the residency sequence was 22.5 GiB. Studio admission preserves a 16 GiB desktop reserve plus any sandbox reservation.

Higgs weights are for personal or hobbyist use under the [Boson model licence](https://huggingface.co/bosonai/higgs-tts-3-4b/blob/main/LICENSE). Klein 4B is [Apache 2.0](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B). FlashHead Lite's DiT and wav2vec2 are Apache 2.0, and its LTX VAE is OpenRAIL-M. The Apple Silicon port describes personal and educational use. LTX-2.3 uses the LTX Community License, with a separately licensed Gemma encoder. See the pinned model directories for the applicable terms. [FlashHead components](https://huggingface.co/Soul-AILab/SoulX-FlashHead-1_3B) · [Mac port](https://github.com/manmastersg/soulX-flashHead-local) · [LTX weights](https://huggingface.co/dgrauet/ltx-2.3-mlx-q4) · [Gemma](https://huggingface.co/mlx-community/gemma-3-12b-it-4bit)

## 3. Owner decisions

- D6 ships voice, everyday video and images together with independently controlled switches. Special notes and the opt-in global profile photo follow the Phase 9 plan.
- D7 keeps the current outfit references. The wardrobe catalogue and all source media live on the studio host, with their locations configured in private `studio.toml`.
- All three chat scopes can receive everyday studio media within the hard caps. Special notes are owner DM only, capped at one a day for milestones, celebrations and owner requests.
- Incoming voice stays with the configured media model. FTS5 remains the memory retrieval method.

## 4. Architecture

The bot validates actions and screens outbound content before asking the studio to render. The studio checks the socket peer and bearer token, then admits work to a single serial render lane. Temporary files are scoped to one render. Each surrounding client operation can fall back independently.

### 4.1 Studio HTTP contract

One Python service under `studio/` has its own environment. The bot environment has no studio runtime dependencies. A GUI-session LaunchAgent keeps the service running. The studio may be unavailable while the host is asleep or the user is logged out.

The service binds one configured LAN interface over plain HTTP. Connections from other peers are closed at the transport before token authentication. Static source addresses, CIDRs and names ending in `.local` are configurable, with DNS refreshed every 60 seconds. Each name keeps its last successful address set during lookup errors or timeouts. Loopback, local interface addresses, built-in container and VM ranges, and locally configured VM or bridge ranges are always denied. Forwarded peer headers do not participate. Allowed peers send one `Authorization` header using the `Bearer` scheme and the token from a mode-0600 file. The service uses constant-time token comparison. Plain HTTP exposes request bodies and tokens to network listeners with access to the traffic, so deploy on the intended private LAN.

| Endpoint | Request | Success |
|---|---|---|
| `GET /v1/health` | — | JSON with `workers` (list of loaded worker names), `available_memory_bytes` (latest integer reading, or null), `queue_depth` (waiting jobs) and `version` |
| `POST /v1/voice` | `{text, seed?}` | `audio/ogg`, Opus mono 48 kHz |
| `POST /v1/video_note` | `{text, special?: bool, outfit?}` | `video/mp4`, square 512 px H.264 with mono AAC and a 60 s maximum |
| `POST /v1/image` | `{scene, outfit?, size?: 768}` | `image/png`, square 768 px |
| `GET /v1/portrait/{outfit}` | Configured outfit name | `image/png`, approved outfit portrait |

POST bodies use JSON with strict types, no unknown fields and a 16 KiB limit. Voice text is 1–600 characters, everyday video text is 1–200, special video text is 1–120, and image scenes are 1–300. The tighter video caps leave time for speech synthesis, rendering and encoding within the video deadlines. The special-video memory budget was measured on a 6.6 s line, so longer clips still depend on the memory watchdog. A voice seed can be an unsigned 64-bit integer (0 through 18446744073709551615 inclusive). An omitted or null seed is generated by the service. Unknown, empty, overlong (above 128 characters), null or omitted render outfits use the studio default. Health and portrait access avoid the render queue.

Up to three jobs may wait in addition to the active job. Synchronous deadlines including queue time are 60 s voice, 180 s everyday video, 420 s special video and 120 s image.

| Status | Response body | When |
|---|---|---|
| 401 | `{"error":"unauthorized"}` | Missing authorization header, duplicate headers or invalid token |
| 403 | Empty | The HTTP handler rejects a disallowed peer. Production closes these connections before HTTP. |
| 404 | `{"error":"unknown-outfit"}` | A portrait was requested for an unknown outfit |
| 408 | `{"error":"request-timeout"}` | The request body took more than 5 s to read |
| 413 | `{"error":"body-too-large"}` | The request body exceeds 16 KiB |
| 415 | `{"error":"expected-json"}` | The request has no `application/json` media type |
| 422 | `{"error":"invalid-request"}` | Malformed JSON or a payload that fails validation |
| 499 | Empty | The client disconnects while the body is being read or a job is pending or active |
| 503 | `{"error":"busy"}` | The waiting queue is full |
| 503 | `{"error":"low-memory"}` | Admission or the watchdog refuses the render |
| 503 | `{"error":"worker-failed"}` | The worker, encoder or render output fails |
| 503 | `{"error":"portrait-unavailable"}` | The configured portrait cannot be read or has an invalid size |
| 504 | `{"error":"timeout"}` | The job deadline expires |

A disconnected client cancels the job and its active worker process group. Media files are deleted when the response is prepared and media responses are non-cacheable. Logs contain job kind, duration, sampled memory and outcome. Logs omit spoken text, scenes, credentials and raw worker diagnostics.

Resident workers use one JSON object per line on stdin and stdout. They emit a ready event, then a done or error event per request. The supervisor restarts a failed worker on subsequent demand with crash backoff. LTX runs a fresh exclusive CLI invocation for each special render. Each worker and encoder process group has a guard that kills the group if the supervisor exits.

### 4.2 Admission and watchdog

Available memory is the sum of free, inactive and purgeable pages from `vm_stat`, multiplied by its page size. The supervisor also records the physical footprint of the service and active studio process groups.

Reserve 16 GiB plus `max_containers × container_memory_mb + min_free_memory_mb`, in MiB. The studio configuration mirrors the desktop host reservation. For loaded workers, admit only if subtracting the expected growth leaves the reserve intact. Growth is 3 GiB for voice, 2 GiB for image and 3 GiB for everyday video. Cold voice, image and everyday video workers have load budgets of 12, 7 and 9 GiB respectively. A cold voice worker needed by a video is accounted for separately.

When space is tight, evict idle image and video workers in least-recently-used order and re-read memory. Higgs remains loaded for routine jobs. For a special render, unload both image and FlashHead, keep Higgs and require 23 GiB plus 2 GiB margin above the reserve. Reload evicted workers on their next job. The 250 ms watchdog cancels the active render at readings below reserve plus 1.5 GiB or if telemetry becomes unavailable.

### 4.3 Bot integration

- `[studio]` defaults off. It contains the base URL, token environment variable, feature switches, default outfit, timeout settings and rolling caps. The token goes in the bot's private environment. Use the existing GIF action pattern for conditional availability and fallbacks.
- The client caches health for 60 seconds and marks the studio away for 5 minutes on connection failure. Render errors become a fallback and a content-free log. Client timeouts allow time for the service's deadlines.
- The response schema can offer a capped `voice` string, a `video_note` object with text and optional boolean `special`, and a capped image scene. Video and image can select an outfit from the active preset. Ephemeral replies use text only. Every spoken line and image scene passes the same outbound leak screen as bubbles before any render call.
- Deliver after bubbles. Voice shows the voice chat action and falls back to a text bubble. Video runs in a tracked background task and refreshes the video-note chat action every four seconds. Video falls back to voice, then text. Image shows the upload chat action and silently skips on render failure. A refused send uses the corresponding fallback. Record successful media and its text or scene through the media history path. Mark specials for cap queries.
- Include the studio turn note only when available and in budget. Show the allowed actions and remaining counts. Media stays rare and should add something to the conversation. Names of tools and engines stay out of chats. Expose studio status and enabled capabilities through self-inspection and available capabilities.

| Scope | Voice | Video | Images | Special | Minimum gap |
|---|---|---|---|---|---|
| Owner DM | 3/day | 1/day | 2/day | 1/day | 30 min |
| Approved friend DM | 1/day | 2/week | 1/day | — | 3 h |
| Group | 1/day | 1/week | 1/week | — | 6 h |

The global cap is 12 studio items per rolling day. Counts use existing `messages.media_json` rows and rolling 24-hour and seven-day windows. No schema change is required. If a supporting index becomes necessary, use the next available migration after 0023.

Presets list approved outfit names in `outfits` front matter. The first valid outfit is the preset default. The bot uses its configured default when the preset lacks outfits. The studio uses a turnaround sheet and face portrait for Klein and a front portrait for video. The wardrobe can include casual and mildly flirty clothing suitable for public settings. Scenes may use real-world or fictional settings and a fixed instruction preserves the outfit. Current references remain in use.

When enabled, the global profile photo follows the Everywhere preset's default outfit through the portrait endpoint and Telegram's profile-photo API. Rate-limit changes to once per hour. Retry a change when health recovers. An owner-only status command can show readiness, memory, loaded workers and counts.

## 5. Delivery and live acceptance

S1–S4 cover the HTTP service, memory guard, pinned worker runtimes and LaunchAgent. S5–S7 and S9 cover the bot client, actions, limits, presets, profile photos, status and docs. S10 is the release team's live test.

1. Back up the database and retain the previous bot release for rollback.
2. In the owner DM, verify approved voice identity, a round everyday video with moving lips within 90 s, a special video within about five minutes, and an image wearing the active outfit. Verify the opted-in profile photo update.
3. Stop the studio and verify text fallback, removal of the turn note, away status and uninterrupted chat. Restart it and check recovery within five minutes.
4. Render an everyday video while a desktop task runs and verify that the configured reserve is preserved in sampled logs. Exercise the special exclusive path.
5. From a desktop sandbox, try the studio interface address, the host alias, VM gateway and local DNS name. Repeat from an unlisted LAN machine. Verify denial before auth and record the result in the desktop host isolation sign-off.
6. Verify outbound leak screening with a private fixture and run the public-repository scan. Complete both E59 reviews before tagging v0.8.3.

Performance and quality depend on speech duration, host load, portrait and model. FlashHead's lip alignment can lag and LTX can drift. The watchdog samples periodically, so a brief excursion can fall between readings. Model revisions, source pins and dependency locks prevent unreviewed component upgrades. The image, FlashHead and model downloader environments sync from hash-checked Python 3.12 wheel locks generated on macOS arm64. Keep the pinned studio snapshots when cleaning shared caches.

## Cloud fallbacks

Cloud routes are opt-in. Configure ordered `[[studio.voice_fallbacks]]` and
`[[studio.image_fallbacks]]` tables. With either list empty, that kind uses
local generation alone. Every render uses the existing screening, feature
switches, chat approval and quota reservation. When the local Studio is ready,
Dom awaits its response before considering cloud. Busy, offline, timed-out and
failed requests can move to the next provider. Cloud generation also checks the
daily spend cap in every chat, including the owner DM. Provider names stay out
of the replies.

- **OpenRouter images** use `POST /api/v1/images` with the model ID, scene,
  `provider: {"zdr": true}` and an inline `input_references` image when available.
  The response carries inline `data[0].b64_json` and any reported
  `usage.cost`. Dom accepts PNG. The image catalog includes Gemini Flash Image,
  GPT Image, Seedream and Flux models. Check each endpoint's reference and output
  capabilities before choosing it. The public endpoint catalogs list an
  image-reference endpoint and ZDR eligibility for
  `google/gemini-2.5-flash-image`. [Image API](https://openrouter.ai/docs/guides/overview/multimodal/image-generation) ·
  [Model endpoints](https://openrouter.ai/api/v1/images/models/google/gemini-2.5-flash-image/endpoints) ·
  [ZDR endpoints](https://openrouter.ai/api/v1/endpoints/zdr).
- **OpenRouter speech** uses `POST /api/v1/audio/speech` with `input`,
  `voice: "Achird"`, `response_format: "pcm"`, and `provider: {"zdr": true}`.
  The speech catalog for `google/gemini-3.8-flash-lite-tts` lists Achird, and
  the public ZDR catalog includes the model. Dom encodes the returned 24 kHz
  mono PCM to OGG/Opus using ffmpeg. When the response supplies a generation
  ID, Dom makes a bounded metadata lookup for the reported `total_cost`.
  [Speech API](https://openrouter.ai/docs/guides/overview/multimodal/tts) ·
  [Speech model](https://openrouter.ai/api/v1/audio/speech/models/google/gemini-3.8-flash-lite-tts) ·
  [Generation metadata](https://openrouter.ai/docs/api/api-reference/generations/get-generation).
- **Direct Gemini speech** is an optional paid route. It uses
  `POST /v1beta/models/{model}:generateContent` with `x-goog-api-key`,
  the screened text, an AUDIO response modality and a prebuilt Achird voice.
  The response carries base64 audio in `candidates[].content.parts[].inlineData`.
  Dom extracts PCM samples from the WAV data chunk, dropping the header and
  any trailing C2PA metadata before encoding. Gemini reports token usage.
  Google's unpaid terms permit product improvement and human review. Paid
  Services exclude product improvement and allow limited logging for safety
  and legal requirements. The owner must confirm a paid project explicitly
  with `paid = true`. [Speech generation](https://ai.google.dev/gemini-api/docs/speech-generation) ·
  [Gemini API terms](https://ai.google.dev/gemini-api/terms).
- **Everyday and special video notes** use the local renderer first, then an
  optional `[[studio.video_fallbacks]]` chain (`provider = "openrouter_video"`,
  E66). OpenRouter's job API: `POST /api/v1/videos` returns 202 with a job `id`;
  `GET /api/v1/videos/{id}` reports `pending`, `in_progress`, `completed` (with
  `usage.cost`) or `failed`; `GET /api/v1/videos/{id}/content?index=0` with the
  API key downloads the MP4. Dom builds both URLs from the fixed origin and the
  validated job ID and never follows the job's own URLs. Image-to-video takes
  `frame_images` with `frame_type: "first_frame"`; most current models list
  `supported_frame_images` (Grok Imagine Video 1.5 Lite, Seedance, Wan, Kling,
  Veo, Hailuo), so Dom sends the outfit's approved portrait as the first frame
  under the photo reference rules. `generate_audio` (default on for models with
  audio) makes the model's own voice from the quoted line. Dom asks for 480p,
  1:1, 4/6/8 s by line length. Cost: Grok Imagine Video 1.5 Lite lists 2¢ per
  output second at 480p plus 1¢ per input image, about $0.09–$0.17 a note; Veo
  3.1 Fast lists $0.10–$0.12 per second (and no 1:1). Latency is about 30 s to a
  few minutes, inside the 185 s and 425 s video deadlines that also cover polling.
  OpenRouter keeps outputs for download, so "No video endpoint currently offers
  Zero Data Retention, so `zdr: true` fails routing"; `data_collection: "deny"`
  works. Config validation refuses a video backup set to `zdr`. The clip is
  re-encoded with ffmpeg as a Telegram video note (square, at most 640 px, H.264
  and AAC, under 60 s). Without a backup, failed video still becomes a voice note
  through the voice chain, then text.
  [Video generation guide](https://openrouter.ai/docs/guides/overview/multimodal/video-generation) ·
  [Video request API](https://openrouter.ai/docs/api/api-reference/video-generation/create-videos) ·
  [Video models](https://openrouter.ai/api/v1/videos/models) ·
  [Provider selection: `zdr` and `data_collection`](https://openrouter.ai/docs/guides/routing/provider-selection) ·
  [ZDR and guardrails](https://openrouter.ai/docs/guides/features/zdr).
- **Data policies (E66).** Every OpenRouter route and backup has
  `data_policy = "zdr" | "no_training" | "any"` (default `zdr`), sent as
  `provider.zdr = true`, `provider.data_collection = "deny"` or nothing. The
  account's "ZDR endpoints only" setting and ZDR guardrails apply on top and win.
  See [privacy](../../privacy.md#openrouter-data-policies) for the separate media
  key setup.
- **Switches.** A studio switch can be turned on only when something can serve
  it: the local studio (enabled with a base URL) or a backup for that kind. The
  profile photo needs the local studio's portraits. It changes at most every
  `profile_photo_every_days` (1, 7, 14 or 30; default 14).

Store approved cloud reference PNGs as `studio-portraits/<outfit>.png` on
the bot host, outside the memory directories. This keeps the reference available
during a complete Studio outage. Set `supports_reference = false` for a model
that lacks image input. A missing or unsupported reference skips that provider
unless `allow_without_reference = true`.

Credentials stay in `.env` and each variable is paired with its provider's
fixed HTTPS origin. Cloud requests use per-kind deadlines, exact MIME checks,
bounded response bodies and inline media, with redirects disabled. Limits are
25 MiB for decoded photos, 4 MiB for references and 3 MiB for voice PCM. Usage
is recorded by media kind and chat with costs when supplied. Live acceptance
covers one synthetic request per enabled provider, the routed OpenRouter ZDR
endpoints, Telegram playback, the owner's Achird audition, and photo identity
during a Studio outage.
