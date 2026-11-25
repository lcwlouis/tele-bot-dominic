# Voice notes, round videos and photos

Dom is a personal bot. The optional studio generates its voice notes, round
videos and photos on a separate machine you run. Enable it in the instance's
`config.toml` and put the matching bearer token in `.env` as `STUDIO_TOKEN`
(or the name in `token_env`). Set `base_url` to the server origin and restart
Dom after changing the connection settings. Under **Settings → What Dom may
do**, you can change the five feature switches while Dom runs. All five start
off. Special videos are available in the owner DM only.

Dom sends ordinary bubbles first. When generation is unavailable or a quota
blocks a note, the words can arrive as text. A failed video job can use a voice
note first if enabled and within quota. Generated photos are skipped when they
cannot be sent. See [Privacy](privacy.md#what-leaves-the-host) for what Dom shares
with the generation service and Jev.

## Limits

These are rolling windows per chat. The whole studio allows 12 delivered items
in 24 hours. A special video uses its own allowance and counts toward the gap
and studio-wide total.

| Chat | Voice | Round video | Photo | Special | Gap |
| --- | --- | --- | --- | --- | --- |
| Owner DM | 3 per day | 1 per day | 2 per day | 1 per day | 30 min |
| Friend DM | 1 per day | 2 per week | 1 per day | — | 3 h |
| Group | 1 per day | 1 per week | 1 per week | — | 6 h |

Customize these via `[studio.limits]` (`daily`) and
`[studio.limits.owner_dm]`, `[studio.limits.friend_dm]` or
`[studio.limits.group]`. Tier keys are `voice_daily`, `video_daily`,
`video_weekly`, `image_daily`, `image_weekly`, `special_daily` and
`gap_minutes`. A daily setting takes precedence over the corresponding
weekly setting.

The `[studio.timeouts]` keys are `health` (3 s), `voice` (65 s),
`video_notes` (185 s), `special` (425 s), `images` (125 s), and
`portrait` (10 s). Run `/studio` in the owner DM to see status and counts.

## Preset outfits

Add an inline list of your server's wardrobe IDs to a preset's front matter,
for example `outfits: [casual, formal]`. The first ID is the default for that
preset. Presets without a list use `[studio] default_outfit` if configured.
With `profile_photo_follows_preset` on, Dom uses the default portrait of the
Everywhere preset. Telegram photo updates are attempted at most once per `profile_photo_every_days`
(1, 7, 14 or 30 days, default 14). A refused update is tried again after an hour.

## Model licences

The generation models have separate terms from Dom's source code. Review the
terms for the checkpoints and the voice and portrait references you install.

- [Higgs Audio v3 TTS](https://github.com/boson-ai/higgs-audio) uses the Boson
  Higgs Audio v3 Research and Non-Commercial License for its weights.
- [FLUX.2 Klein 4B](https://github.com/black-forest-labs/flux2) uses Apache 2.0.
- [SoulX-FlashHead](https://github.com/Soul-AILab/SoulX-FlashHead) uses Apache 2.0
  for its repository. Check the terms of the Lite model's upstream dependencies.
- [LTX-2.3](https://huggingface.co/Lightricks/LTX-2.3) uses the LTX-2 Community
  License.

## Optional cloud fallbacks

Configure only the providers you want to use when the local Studio cannot
complete a request. Put the key value in the instance's `.env`. Keep the
corresponding media switches on. A backup can also serve on its own, with no
local studio configured.

```toml
[[studio.voice_fallbacks]]
provider = "openrouter_tts"
model = "google/gemini-3.8-flash-lite-tts"
api_key_env = "OPENROUTER_MEDIA_API_KEY"

[[studio.image_fallbacks]]
provider = "openrouter_image"
model = "google/gemini-2.5-flash-image"
api_key_env = "OPENROUTER_MEDIA_API_KEY"
```

OpenRouter speech usage is recorded immediately. A background check updates the
same row when the provider reports the cost. Seedream 5.5 photos request 1K
square output. Cloud photos above 1280 px on either side or 2 MiB are encoded
as JPEG at quality 90 before delivery.
[OpenRouter Image API](https://openrouter.ai/docs/guides/overview/multimodal/image-generation) ·
[Seedream 5.5](https://openrouter.ai/bytedance-seed/seedream-5-5/).

An optional video backup makes round video notes through OpenRouter when the
local studio can't. It must not be zero data retention (OpenRouter keeps each
video for download), so it takes `no_training` or `any`:

```toml
[[studio.video_fallbacks]]
provider = "openrouter_video"
model = "heygen/heygen-avatar-6-turbo"
api_key_env = "OPENROUTER_MEDIA_API_KEY"
data_policy = "no_training"
```

Dom makes the audio through the voice note path, then sends the voice track and
the outfit's approved portrait to the avatar for lip-sync. The supported models
are `heygen/heygen-avatar-6-turbo` and `heygen/heygen-avatar-6`, with 1080p
as their smallest advertised resolution. In the 2026-11-24 live test, Avatar IV
rejected inline WAV with an HTTPS-only error, while Avatar 6 Turbo accepted it.
Replace existing Avatar IV backups in Settings → Models. The cloud job shares
one deadline from speech through encoding.
Dom downloads the clip through OpenRouter's authenticated content URL and
maps his own audio into the Telegram video note. If the avatar fails, eligible
voice-note fallback can reuse that audio. See [provider data handling](privacy.md#separate-media-key)
for the required separate media key.

Voice and video conversion require `ffmpeg` with `libopus`, `libx264` and `aac` on the **bot host**
(Debian's and Raspberry Pi OS's `ffmpeg` package has them). Copy each
approved reference to `DOM_HOME/studio-portraits/<outfit>.png` so the bot can
use it when the local Studio is unreachable. References must be PNG files no
larger than 4 MiB. Direct Gemini (`provider = "gemini_tts"`) requires a direct
Gemini model ID, a separate key variable and `paid = true`. See the
[configuration reference](configuration.md) and [provider data handling](privacy.md).
