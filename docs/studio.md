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

When the chat model writes a bubble that promises media ("sending you a pic",
"here's a voice note") without setting its field, nothing is sent. Dom puts the
reply back to the model once, to set the field or drop the promise (only to drop
it when that media isn't on offer), and the turn log shows a `media promise`
step. A promise still standing after that one nudge is cut out of the bubbles, never sent, and a reply left empty
becomes "Can't send that right now." Only a direct promise to the reader counts: not a question, a negation, a
third party ("to her"), "take a look", a link, or words in quotes.

A failed, refused or limited generation leaves one short, rate-limited event in
that chat's history; it never crosses into another chat.

Dom keeps what he sends: each photo, voice note and video note is saved under
the home's `media/` folder (size-capped by `studio.keep_cap_mb`), and he looks at
a generated photo before sending it when his chat model takes images. See
[Media Dom sees and sends](privacy.md#media-dom-sees-and-sends).

## Where the studio runs

`dom setup <home>` (see [Set up Dom](setup.md#where-the-studio-runs)) offers three paths:

- **On this machine.** The studio runs beside Dom (default address `http://127.0.0.1:8080`).
- **On a rented cloud GPU.** The studio runs on a machine you rent by the hour. Join that
  machine and Dom's host to one Tailscale network and use its MagicDNS name or Tailscale
  address as `base_url`, as [the studio guide](../studio/README.md) describes for any
  studio on another network. Never open the studio's port to the internet; the bearer
  token is still required. Paste the token at the setup prompt or in Settings → Services:
  it goes to `.env` as `STUDIO_TOKEN` and is never shown again.
- **External APIs only.** No studio: `[studio] enabled = false` and the cloud backups below
  make the media.

Settings → Services edits the address and token later, and tests them with a harmless
health request.

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

**A gap per kind.** `gap_minutes` is the gap for every kind that has none of its
own. A kind can have its own with `voice_gap_minutes`, `video_gap_minutes`,
`image_gap_minutes` or `special_gap_minutes` (minutes, up to a week; `false`
unsets one). A kind with its own gap is spaced only from the last item *of that
kind*; the kinds without one share `gap_minutes` and space each other as before.
So with no per-kind gaps nothing changes, and with `voice_gap_minutes = 0` and
`gap_minutes = 30` a voice note and a photo can arrive together, two photos stay
30 minutes apart, and a voice note never holds back a photo. Settings → What Dom
may do → Media limits edits all of them (the per-kind gaps sit under "Gaps for
one kind"; empty uses the general gap).

## Event days

On a special day in his own life (a trip, a concert, his birthday) Dom may raise
his media limits for that day. He declares it himself with the owner-DM tool
`declare_event_day` (a title, and optionally a date up to two weeks ahead and a
multiplier). On that calendar day (Dom's time zone) every per-chat limit and the
studio-wide daily total are multiplied by up to `[studio] event_day_max_multiplier`
(default `2`; `0` turns event days off). At most `event_days_per_week` (default
`1`) may fall within any rolling week; a cancelled one does not count.

The spend cap still holds. Every item beyond the normal limits must pass
`usage.may_spend_one_off`: it uses only the month's unused budget under
`monthly_spend_cap_usd` (and the media area's monthly limit, if you set one) and
never passes it. Each extra item is held to an assumed cost before its real cost is
known (voice $0.02, photo $0.05, round video $0.50, special $1.00); the real cost is
recorded as usual when a cloud backup makes it. With no monthly cap there is no
budget to run out, so the multiplier alone limits it. Gaps are not raised.

Dom is told in that day's turn note. `/studio` lists today's and the coming event
days, and Settings → What Dom may do → Event days lists them with a Cancel button
(the limits go back to normal at once). The API is `GET /api/studio/event-days` and
`POST /api/studio/event-days/cancel` with `{"id": ...}`.

The `[studio.timeouts]` keys are `health` (3 s), `voice` (65 s),
`video_notes` (185 s), `special` (425 s), `images` (125 s), and
`portrait` (10 s). Run `/studio` in the owner DM to see status and counts.

## A scene for every video note

A video note used to animate the outfit's wardrobe portrait, so even the
background never changed. With `[studio] video_scenes = true` (the default) each
round video starts from a fresh still of Dom in a scene that fits the moment. Dom
writes the scene in the note's `scene` field (up to 200 characters); without one
the still is made from what he says. The image model makes the still with the
outfit's portrait as the identity reference (the local studio's image worker, else
a cloud image backup that takes a reference), Dom looks at it before it is
animated when `look_owner_dm`, `look_friend_dm` or `look_group` is on for that
chat (cheaper than looking at the video, so the video itself is then not looked
at), and it is animated in place of the portrait. If the still can't be made, or
the studio is older than 0.8.4 and cannot take one, the note uses the portrait
as before.

A round video note is a talking head, so the still has to give the animator a
face to move. Every still's prompt, a retake after the look included, ends with
"Facing the camera, face clearly visible, nothing covering it." (the scene is
shortened to keep the whole prompt within 300 characters), which rules out a
mirror selfie with the phone over the face. The local studio's FlashHead worker
then crops each still to the face with head and shoulders around it (2.5 times
the face's width) before animating, so the mouth and eyes fill enough of the
frame to move while the scene still shows behind him. A still with no face, or
with several (the crop could land on someone else), is animated whole. Special
notes use their own worker and are not cropped. `prepare-workers.sh` fetches the
face detector's files, pinned and checked, so a render never downloads them.

**What it costs.** The still is made inside the video note's reservation, so one
note can never be refused for lack of a photo allowance, and a refused note makes
no still. It counts as one more item toward the studio-wide `daily` total (the
note counts twice there); it does not use the chat's photo allowance. A local
still costs nothing. A cloud image backup records its cost as `studio_image`
usage and obeys the spend cap, so a cloud-only video note costs the image plus
the avatar video (and the voice): if the video then fails, the still's cost is
already spent. The still is kept beside the video in the home's `media/` folder
as `<message id>-still.png` (kept and cleaned up with the video, counted with it
against `keep_cap_mb`) and shown in the library.

**Provenance.** Each kept item records where it came from: the local studio or a cloud backup (provider host and
model id), the outfit and preset, generation time, duration, whether the look before sending redid it (and which try
was sent), its voice and still, and the usage-row ids of its paid calls. The Library page (Work → Library) shows it in
an item's details, and `GET /api/media/files` returns it as `made`. Cost is read from those rows each time, so it is
`local, free` for the studio, and `cost pending` until a late-reported cost lands. A cost the provider settled at $0 (a failed video job bills nothing) is final, not pending; a redo shows its own cost, or `local, free` when the studio made it. A studio worker can name its model
for this with `model = "…"` under `[workers.<name>]`; the studio then lists it in its health reply, and local usage
rows and the library show it instead of `local-studio`.

The studio service takes the still as an optional `still` field (base64 PNG, at
most 3 MiB) on `POST /v1/video_note` and reports `"video_still": true` on
`/v1/health`.

## Reference photos of known places

A place Dom knows (a note under `memory/places/`) may have one reference photo, added by the owner on the dashboard's
World page. It is saved as `media/places/<slug>.jpg` (a JPEG of at most 1024 px, decoded and written again, so a phone's
location data is dropped, in a private folder and moved into place atomically, like kept media) and is not counted
against `keep_cap_mb`.

When Dom's photo, or the still of a video note, is made by a cloud image backup that takes a reference
(`supports_reference`) and Dom names an offered place by its key (`image_place` on a photo, `video_note.place` on a video
note) that has a photo, the photo goes as a second reference, with a line telling the model to keep how that place
looks. It is never chosen from the scene's words: no key, no photo. A chat only sees a place's key, and so only gets its
photo, when it has a line in the place's note (the owner's DM always does). The local studio's image
worker takes one reference, the outfit's portrait, so local photos are not changed.

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

A voice backup takes an optional `voice`, the provider's voice name (`Achird`
when unset), also in Settings → Models. A direct Gemini backup, and OpenRouter's
`google/` speech models, accept only Gemini's prebuilt voices (for example `Kore`,
`Puck`, `Charon`, `Achird`; the 30 names are listed in
[Gemini's docs](https://ai.google.dev/gemini-api/docs/speech-generation#voices)),
and Dom refuses to save another name. Other OpenRouter speech models name their own
voices; Dom checks only the format (a letter, then letters, digits, `-` or `_`),
so a wrong name there fails when the provider answers and the next backup is tried.

```toml
[[studio.voice_fallbacks]]
provider = "openrouter_tts"
model = "google/gemini-3.8-flash-lite-tts"
api_key_env = "OPENROUTER_MEDIA_API_KEY"
voice = "Kore"
```

Voice and video conversion require `ffmpeg` with `libopus`, `libx264` and `aac` on the **bot host**
(Debian's and Raspberry Pi OS's `ffmpeg` package has them). Copy each
approved reference to `DOM_HOME/studio-portraits/<outfit>.png` so the bot can
use it when the local Studio is unreachable. References must be PNG files no
larger than 4 MiB. Direct Gemini (`provider = "gemini_tts"`) requires a direct
Gemini model ID, a separate key variable and `paid = true`. See the
[configuration reference](configuration.md) and [provider data handling](privacy.md).
