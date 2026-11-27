"""Dom sees the real photo and hears the real voice note: which media goes into a prompt, and what the chat route takes.

For a turn, the newest unanswered incoming media and Dom's own latest generated media are attached to the prompt's
user message (dom.modalities turns them into content parts after the text, so the cached prefix is unchanged), as far
as the chat route's model accepts that type and the per-prompt limits allow. Media the route can't take is described
by the media route instead, as before. Learning what a model accepts (a list fetch, a one-off probe) runs in the
background or when the owner asks, never on the reply path.

A clip (video, animation, video note) his chat model can't take as video goes the same way when it takes images: the
clip is cut into frame strips (dom.video) and its sound is transcribed, both on arrival in the background, and the turn
carries the strip as an image with the transcript beside it. A clip long enough for several strips is described part by
part and joined, since a prompt can't carry them all. The media model describes the whole clip only when no strips can
be made (no ffmpeg, or a chat model that takes no images).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import OrderedDict
from functools import partial
from typing import TYPE_CHECKING, AsyncContextManager

from dom import media as media_mod
from dom import model_lists, turn_log, video
from dom.llm import ChatClient, openrouter_host
from dom.media_files import MIME
from dom.modalities import MODALITIES, MODALITY_OF, Attachment, Modalities, probe_image
from dom.models import StoredMessage
from dom.usage import ModelCallSkipped

if TYPE_CHECKING:
    from dom.dispatch import Dispatcher

log = logging.getLogger("dom.native_media")
KEPT_BYTES = 4  # incoming files held in memory between arrival and the turn
RETRY_LEARNING_SECONDS = 1800  # before an endpoint whose capabilities could not be learned is tried again
LOAD_SECONDS = 15  # for one attachment's download before the turn goes ahead without it
AGED_TRIED = 200  # attachments remembered as already offered for a late description, so a failed one isn't retried
KEPT_STRIPS = 4  # clips whose strips are held in memory (they are also on disk, unless the media cap is zero)
VIDEO_KINDS = frozenset({"video", "animation", "video_note"})
TRANSCRIPT_CHARS = 4000
TRANSCRIBE_PROMPT = (
    "Transcribe the speech in this audio word for word, in the language spoken. If there is no speech, "
    "answer exactly: (no speech)"
)
STRIP_PROMPT = (
    "This image is a grid of frames from part {number} of {total} of a video clip ({start} to {end}), read left to "
    "right, top to bottom, each frame labelled with its time. Say what happens in this part, any text on screen and "
    "the mood, in at most 60 words."
)
SOUND_LINES = {
    "no speech": "There is no speech in it.", "no sound": "It has no sound.",
    "not transcribed": "Its sound could not be transcribed.",
}


def _media(row: StoredMessage) -> dict:
    try:
        media = json.loads(row.media_json) if row.media_json else None
    except ValueError:
        return {}
    return media if isinstance(media, dict) else {}


def _providers(client: object) -> list[ChatClient]:
    providers = getattr(client, "providers", None)
    if providers is None:
        providers = (client,) if isinstance(client, ChatClient) else ()
    return [provider for provider in providers if isinstance(provider, ChatClient)]


def sound_line(watched: dict) -> str:
    """What was heard in a watched clip, or why nothing was."""
    if watched.get("sound") == "heard" and watched.get("transcript"):
        return f'Heard: "{watched["transcript"]}"'
    return SOUND_LINES.get(str(watched.get("sound")), "")


LOST_PICTURE = "(The picture of this video is no longer available.)"


def missing_line(watched: dict) -> str:
    """Which stretches of a watched clip had frames that could not be read."""
    stretches = ", ".join(f"{video.clock(a)} to {video.clock(b)}" for a, b in watched.get("missing") or ())
    return f"Frames from {stretches} could not be read." if stretches else ""


def cut_line(watched: dict) -> str:
    return (
        f"Only the first {video.clock(watched['seconds'])} of {video.clock(watched['total'])} was looked at."
        if watched.get("cut") else ""
    )


class NativeMedia:
    def __init__(self, dispatcher: Dispatcher):
        self.d = dispatcher
        self.modalities = Modalities(dispatcher.app.store)
        self._bytes: OrderedDict[tuple[int, int], tuple[str, bytes]] = OrderedDict()
        self._learning: dict[tuple, float] = {}
        self.last: frozenset[str] = frozenset()  # what the chat route took the last time it was asked
        self._unknown: list[ChatClient] = []  # providers whose input types nothing has told yet
        self._aged: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._strips: OrderedDict[tuple[int, int], list[bytes]] = OrderedDict()
        self._watching = asyncio.Semaphore(1)  # one clip downloaded and in ffmpeg at a time: the Pi has 4 GB
        self._watches: dict[tuple[int, int], asyncio.Task] = {}  # clips being watched now, shared by whoever needs them
        self._summaries: dict[tuple[int, int], asyncio.Task] = {}  # clips being described from their strips now
        self._descriptions: dict[tuple[int, int], asyncio.Task] = {}  # clips being described in the background now

    # --- what the chat route takes ---

    def _chat_providers(self) -> list[ChatClient]:
        return _providers(self.d.app.client)

    def _limits(self) -> dict[str, int]:
        config = self.d.app.config
        return {
            "image": config.media_input_images, "audio": config.media_input_audio, "video": config.media_input_video,
        }

    def max_bytes(self) -> int:
        return int(self.d.app.config.media_input_max_mb * 1_000_000)

    async def accepted(self) -> frozenset[str]:
        """The types the chat route's first usable provider takes, marking every provider's own types on it so a
        backup that can't take one is sent a description instead. Unknown endpoints are learned in the background."""
        chosen: frozenset[str] | None = None
        self._unknown = []
        for provider in self._chat_providers():
            found = await self.modalities.lookup(provider.endpoint)
            provider.inputs = found or frozenset()
            if found is None:
                self._unknown.append(provider)
            if chosen is None and provider.available:
                chosen = provider.inputs
        limits = self._limits()
        self.last = frozenset(kind for kind in (chosen or ()) if limits.get(kind, 0) > 0)
        return self.last

    async def takes(self, media: dict | None) -> bool:
        """Whether this attachment would go to the chat model itself: its type is accepted and it fits the limits."""
        if not media:
            return False
        modality = self._modality(media)
        accepted = await self.accepted()
        for provider in self._unknown:  # a photo arrived and nobody knows if his model sees: find out, off the reply path
            self._learn_later(provider)
        if modality is None or modality not in accepted:
            return False
        if media.get("watched"):  # frame strips: the chat model gets them as images (video.fit squeezes them to the slots)
            return True
        size = media.get("file_size")
        if size is not None and size > min(self.max_bytes(), media_mod.MAX_BYTES.get(media["kind"], 0)):
            return False
        if media.get("kind") == "sticker" and (media.get("mime") or media_mod.DEFAULT_MIME["sticker"]) != "image/webp":
            return bool(media.get("preview_file_id"))
        return True

    @staticmethod
    def _slots(media: dict, free: int) -> int:
        """The image slots a prompt gives an attachment: one, or for a watched clip one per strip, squeezed into what
        is free (video.fit)."""
        return min(int(media["watched"].get("strips") or 1), free) if media.get("watched") else 1

    @staticmethod
    def _modality(media: dict) -> str | None:
        """What the chat model is given for an attachment: a watched clip is shown as an image (its frame strip)."""
        return "image" if media.get("watched") else MODALITY_OF.get(media.get("kind"))

    def _learn_later(self, provider: ChatClient) -> None:
        key = Modalities._identity(provider.endpoint)
        now = self.d.now().timestamp()
        if now - self._learning.get(key, -RETRY_LEARNING_SECONDS) < RETRY_LEARNING_SECONDS:
            return
        self._learning[key] = now
        self.d._spawn(self.learn(provider))

    async def learn(self, provider: ChatClient) -> frozenset[str] | None:
        """Find out what one endpoint takes: fetch its model list when OpenRouter lists input types, else probe it once
        with a generated image. The answer is cached. Never called on the reply path."""
        endpoint, app = provider.endpoint, self.d.app
        try:
            if openrouter_host(provider.host):
                location = model_lists.list_location("openrouter", "chat", endpoint.base_url)
                if location is not None:
                    listed = await model_lists.fetch(
                        provider.http, location[0], provider.api_key, path=location[1],
                        secrets=model_lists.secret_values(app.config),
                    )
                    await model_lists.remember(app.store, endpoint.base_url, endpoint.api_key_env, listed)
                    self.modalities.forget(endpoint)
                    found = await self.modalities.lookup(endpoint)
                    if found is not None:
                        return found
            if endpoint.input is not None:
                return await self.modalities.lookup(endpoint)
            probe = ChatClient(
                endpoint, provider.api_key, provider.http, usage=provider.usage, kind="media",
            )
            async with self.d.sticker_media_request():
                answer = await probe_image(probe)
            if answer is None:
                return None
            await self.modalities.remember_probe(endpoint, answer)
            log.info("probed %s: %s", endpoint.model, "takes images" if answer else "text only")
            return frozenset({"image"}) if answer else frozenset()
        except ModelCallSkipped:
            return None
        except Exception as exc:
            log.warning("could not learn what %s accepts (%s)", endpoint.model, type(exc).__name__)
            return None

    async def reprobe(self) -> list[dict]:
        """Learn again what every chat provider takes (the owner's request), ignoring cached answers."""
        results = []
        for provider in self._chat_providers():
            endpoint = provider.endpoint
            if endpoint.input is None:
                await self.modalities.reset(endpoint)
                await self.learn(provider)
            results.append(await self.modalities.explain(endpoint))
        return results

    # --- bytes in hand before the turn ---

    def _remember(self, key: tuple[int, int], mime: str, data: bytes) -> None:
        self._bytes[key] = (mime, data)
        self._bytes.move_to_end(key)
        while len(self._bytes) > KEPT_BYTES:
            self._bytes.popitem(last=False)

    async def prefetch(self, chat_id: int, msg_id: int, media: dict) -> None:
        """Download an arriving attachment now, so the turn doesn't wait for it. No model is called."""
        try:
            mime, data = await media_mod.download(self.d.app.bot, media, limit=self.max_bytes())
        except media_mod.PermanentMediaSkip:
            return
        except Exception as exc:
            log.warning("could not fetch an attachment in chat %s (%s)", chat_id, type(exc).__name__)
            return
        self._remember((chat_id, msg_id), mime, data)

    def forget_chat(self, chat_id: int) -> None:
        for key in [key for key in self._bytes if key[0] == chat_id]:
            del self._bytes[key]

    async def _incoming_bytes(self, chat_id: int, msg_id: int, media: dict) -> tuple[str, bytes] | None:
        if (found := self._bytes.get((chat_id, msg_id))) is not None:
            return found
        try:
            async with asyncio.timeout(LOAD_SECONDS):
                mime, data = await media_mod.download(self.d.app.bot, media, limit=self.max_bytes())
        except media_mod.PermanentMediaSkip:
            return None
        except Exception as exc:
            log.warning("could not fetch an attachment in chat %s (%s)", chat_id, type(exc).__name__)
            return None
        self._remember((chat_id, msg_id), mime, data)
        return mime, data

    async def _describe_now(self, chat_id: int, msg_id: int, media: dict) -> str | None:
        store = self.d.app.store
        row = await store.get_message(chat_id, msg_id)
        if row is not None and (text := _media(row).get("description")):
            return text
        await self.d._describe(chat_id, msg_id, media, self.d._media_request(chat_id))
        row = await store.get_message(chat_id, msg_id)
        return _media(row).get("description") if row is not None else None

    # --- picking the media for one prompt ---

    async def undescribed(self, pending: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
        """The (msg_id, media) among `pending`, oldest first, that need a description: what the chat model can't take,
        and what would not fit its per-prompt limits, since only the newest items go to it as they are."""
        accepted = await self.accepted()
        left = {kind: count for kind, count in self._limits().items() if kind in accepted}
        need = []
        for msg_id, media in reversed(pending):  # newest first, as attach() picks
            modality = self._modality(media)
            if modality is not None and left.get(modality, 0) > 0 and await self.takes(media):
                left[modality] -= self._slots(media, left[modality])
            else:
                need.append((msg_id, media))
        return need[::-1]

    async def prepare(self, chat_id: int, window: list[StoredMessage]) -> tuple[list[Attachment], dict[int, str]]:
        """This turn's media for the prompt's user message, and a note for each attachment that could not be loaded
        (its description, or that it couldn't be loaded) to show in its `<media>` tag. Everything comes from this
        chat's own window, so nothing from another chat can reach the prompt."""
        accepted = await self.accepted()
        last_reply = max((i for i, row in enumerate(window) if row.role == "assistant"), default=-1)
        if "image" not in accepted and any(
            row.role == "user" and not row.is_bot and _media(row).get("watched") for row in window[last_reply + 1:]
        ):  # a clip was watched, but his chat model can't be shown the frames: the media model's description stands in
            turn_log.step("clip described by the media model: his chat model takes no images")
        if not accepted:
            return [], {}
        left = {kind: count for kind, count in self._limits().items() if kind in accepted}
        picks: dict[int, tuple[StoredMessage, dict, str, bool, int]] = {}
        unseated: list[tuple[StoredMessage, dict]] = []  # watched clips with no image slot left this turn
        for index in range(len(window) - 1, last_reply, -1):  # newest incoming first
            row = window[index]
            media = _media(row)
            modality = self._modality(media)
            if row.role != "user" or row.is_bot or modality is None or row.msg_id is None:
                continue
            if left.get(modality, 0) <= 0:
                if media.get("watched") and "image" in accepted:
                    unseated.append((row, media))
                continue
            if not await self.takes(media):
                continue
            slots = self._slots(media, left[modality])
            left[modality] -= slots
            picks[index] = (row, media, modality, False, slots)
        for index in range(len(window) - 1, -1, -1):  # Dom's own latest generated media, once
            row = window[index]
            media = _media(row)
            modality = MODALITY_OF.get(media.get("kind"))
            if row.role == "assistant" and media.get("file") and modality is not None:
                if left.get(modality, 0) > 0 and index not in picks:
                    left[modality] -= 1
                    picks[index] = (row, media, modality, True, 1)
                break
        attachments: list[Attachment] = []
        notes: dict[int, str] = {}
        heard: dict[int, str] = {}  # for a clip shown as a frame strip: what it is, and what was heard in it
        for index in sorted(picks):
            row, media, modality, own, slots = picks[index]
            if media.get("watched") and not own:
                images = await self._strip_images(chat_id, row.msg_id, media)
                if not images:  # the strips are gone (the media cap, or a restart with nothing kept): say so
                    notes[row.msg_id] = (
                        await self._describe_now(chat_id, row.msg_id, media)
                        or " ".join(filter(None, (LOST_PICTURE, sound_line(media["watched"]), cut_line(media["watched"]))))
                    )
                    continue
                watched = media["watched"]
                kind, who = str(media["kind"]).replace("_", " "), row.sender_name or "someone"
                spans = watched.get("spans") or []
                shown = await asyncio.to_thread(video.fit, images, slots)
                for number, group in enumerate(video.groups(len(images), slots)):
                    first, last = group[0], group[-1]
                    window_text = (
                        f", {video.clock(spans[first][0])} to {video.clock(spans[last][1])}"
                        if len(spans) > last else ""
                    )
                    of = f" {number + 1} of {len(shown)}" if len(shown) > 1 else ""
                    ref = f"message {row.msg_id}, frame strip{of} of a {kind} from {who}{window_text} (each frame labelled with its time)"
                    attachments.append(Attachment(
                        "image", "image/jpeg", shown[number], ref, partial(self._describe_now, chat_id, row.msg_id, media),
                    ))
                heard[row.msg_id] = " ".join(filter(None, (
                    f"({kind}, {video.clock(watched['total'])}, shown as a frame strip)",
                    sound_line(watched), cut_line(watched), missing_line(watched),
                )))
                continue
            if own:
                data = await self.d.app.media_files.read(chat_id, str(media["file"]))
                if data is None or len(data) > self.max_bytes():
                    continue
                mime = MIME.get(str(media["file"]).rpartition(".")[2], "application/octet-stream")
                text = media.get("scene") or media.get("text") or None
                ref = f"message {row.msg_id}, the {media['kind'].replace('_', ' ')} you sent"
                describe = partial(_text, text)
            else:
                found = await self._incoming_bytes(chat_id, row.msg_id, media)
                if found is None:  # no bytes, and nothing was described on arrival: say what we can instead
                    kind = str(media["kind"]).replace("_", " ")
                    notes[row.msg_id] = await self._describe_now(chat_id, row.msg_id, media) or f"({kind} couldn't be loaded)"
                    continue
                mime, data = found
                who = row.sender_name or "someone"
                ref = f"message {row.msg_id}, {media['kind'].replace('_', ' ')} from {who}"
                describe = partial(self._describe_now, chat_id, row.msg_id, media)
            attachments.append(Attachment(modality, mime, data, ref, describe))
        pending: list[tuple[StoredMessage, asyncio.Task]] = []
        for row, media in unseated:  # over the turn's image budget: the media model describes it, and the turn says so
            turn_log.step("clip described by the media model: no free image slot")
            watched = media["watched"]
            heard[row.msg_id] = " ".join(filter(None, (
                f"({str(media['kind']).replace('_', ' ')}, {video.clock(watched['total'])}; its frames don't fit this "
                "turn's images)", sound_line(watched), cut_line(watched),
            )))
            if media.get("description") is None:
                pending.append((row, self._description_task(chat_id, row.msg_id, media)))
        if pending:  # the picture, as text, if it comes within the catch-up wait; else the note stands and the job goes on
            from dom.dispatch import MEDIA_WAIT_SECONDS

            await asyncio.wait([task for _, task in pending], timeout=MEDIA_WAIT_SECONDS)
            for row, task in pending:
                if task.done() and not task.cancelled() and task.exception() is None and task.result():
                    heard[row.msg_id] = task.result()
        if attachments:
            counts = {kind: sum(1 for a in attachments if a["modality"] == kind) for kind in MODALITIES}
            turn_log.step("sees " + ", ".join(f"{n} {kind}" for kind, n in counts.items() if n))
        if notes:
            turn_log.step("media not loaded")
        return attachments, heard | notes


    # --- a clip the chat model can't take as video: frame strips and a transcript ---

    async def route(self, media: dict) -> video.Route:
        """How a clip reaches Dom (see dom.video.route): as the clip, as frame strips for his chat model, as frame
        strips the media model describes (his chat model takes no images), or described whole."""
        accepted = await self.accepted()
        return video.route(
            takes_video=await self.takes(media), takes_images="image" in accepted, has_ffmpeg=video.ffmpeg() is not None,
        )

    async def arrive(self, chat_id: int, msg_id: int, media: dict) -> bool:
        """A clip arriving that his chat model won't take as video: cut it into strips and transcribe it now, in the
        background. True when that was done (the turn will show the strips, or, when his chat model takes no images,
        the media model describes them now); False when the media model should describe the whole clip instead."""
        if media.get("kind") not in VIDEO_KINDS or (route := await self.route(media)) not in ("strips", "described"):
            return False
        seen = await self._watch_task(chat_id, msg_id, media, False)
        if seen is None:
            return False
        if route == "described":
            await self.d._describe(chat_id, msg_id, seen, self.d._media_request(chat_id))
        return True

    def _watch_task(self, chat_id: int, msg_id: int, media: dict, background: bool) -> asyncio.Task:
        """The one job watching this clip: whoever needs it while it runs (arrival, triage, a catch-up) shares its
        result instead of downloading the clip and starting ffmpeg again."""
        key = (chat_id, msg_id)
        task = self._watches.get(key)
        if task is None:
            task = self._watches[key] = self.d._spawn(self.watch(chat_id, msg_id, media, background=background))
            task.add_done_callback(lambda _: self._watches.pop(key, None))
        return task

    async def describe(self, chat_id: int, msg_id: int, media: dict, request: AsyncContextManager[None]) -> str | None:
        """The description of one file, for the dispatcher. A clip shown as strips is described from them (made now if
        they aren't yet, joining a job already running for it, for at most the catch-up wait); anything else, or a clip
        that can't be watched, is described whole by the media model."""
        describer = self.d.app.media_describer
        if media.get("kind") in VIDEO_KINDS:
            row = await self.d.app.store.get_message(chat_id, msg_id)
            current = _media(row) if row is not None and _media(row) else media
            if not current.get("watched") and await self.route(current) in ("strips", "described"):
                from dom.dispatch import MEDIA_WAIT_SECONDS  # the reply path never waits longer than catch-up does

                try:
                    current = await asyncio.wait_for(
                        asyncio.shield(self._watch_task(chat_id, msg_id, current, True)), MEDIA_WAIT_SECONDS,
                    )
                except TimeoutError:
                    log.info("a clip in chat %s is still being watched; described later", chat_id)
                    return None
            if current and current.get("watched"):
                return await self.summarise(chat_id, msg_id, current, request)
        return await describer.describe(media, request=request)

    def _description_task(self, chat_id: int, msg_id: int, media: dict) -> asyncio.Task:
        """The one job describing this clip in the background (under the media budget and spend cap); a turn that
        wants the picture and an aged-media pass share it. Its result is the stored description."""
        key = (chat_id, msg_id)
        task = self._descriptions.get(key)
        if task is None:
            task = self._descriptions[key] = self.d._spawn(self._describe_one(chat_id, msg_id, media))
            task.add_done_callback(lambda _: self._descriptions.pop(key, None))
        return task

    async def _describe_one(self, chat_id: int, msg_id: int, media: dict) -> str | None:
        await self.d._describe(chat_id, msg_id, media, self.d._media_request(chat_id, background=True))
        row = await self.d.app.store.get_message(chat_id, msg_id)
        return _media(row).get("description") if row is not None else None

    async def watch(self, chat_id: int, msg_id: int, media: dict, *, background: bool = False) -> dict | None:
        """Cut a clip into strips, transcribe its sound, keep the strips beside the media files and record what was
        made on the message. Returns the message's media as stored, or None when the clip could not be watched (too
        big for Telegram to hand over, no ffmpeg, unreadable, too few frames). One clip at a time is downloaded and
        in ffmpeg, so a burst of clips never holds several 20 MB files in memory on the Pi; the model calls and the
        saves happen after, with nothing held."""
        store = self.d.app.store
        try:
            async with self._watching:
                try:
                    _, clip = await media_mod.download(self.d.app.bot, media, ceiling=video.DOWNLOAD_BYTES)
                except media_mod.PermanentMediaSkip:
                    log.info("a clip in chat %s is too big to download; not watched", chat_id)
                    return None
                except Exception as exc:
                    log.warning("could not fetch a clip in chat %s (%s)", chat_id, type(exc).__name__)
                    return None
                try:
                    seen = await video.watch(clip, media.get("duration"))
                finally:
                    del clip
        except video.Unwatchable as exc:
            log.info("could not watch a clip in chat %s (%s)", chat_id, exc)
            return None
        sound, transcript = await self._hear(chat_id, seen.audio, background)
        files = self.d.app.media_files
        kept = await files.keep_strips(chat_id, msg_id, [strip.image for strip in seen.strips], self.d.now())
        watched = {
            "strips": len(seen.strips), "spans": [[strip.start, strip.end] for strip in seen.strips],
            "seconds": round(seen.seconds, 1), "total": round(seen.total, 1), "cut": seen.cut,
            "missing": [[round(a, 1), round(b, 1)] for a, b in seen.missing],
            "sound": sound, "transcript": transcript,
        }
        if not await store.update_media(chat_id, msg_id, {**kept, "watched": watched}):
            return None
        if kept:
            await files.evict()
        self._strips[(chat_id, msg_id)] = [strip.image for strip in seen.strips]
        self._strips.move_to_end((chat_id, msg_id))
        while len(self._strips) > KEPT_STRIPS:
            self._strips.popitem(last=False)
        row = await store.get_message(chat_id, msg_id)
        return _media(row) if row is not None else None

    async def _hears(self, client: object) -> bool:
        """Whether the first usable model of a route takes audio."""
        for provider in _providers(client):
            if provider.available:
                return "audio" in await self.modalities.accepts(provider.endpoint)
        return False

    async def _hear(self, chat_id: int, audio: bytes | None, background: bool) -> tuple[str, str | None]:
        """A clip's sound as (what happened, the transcript): the media route writes it when it takes audio, else the
        chat model when it does; with neither the clip's sound goes unheard, and its note says so."""
        if audio is None:
            return "no sound", None
        describer = self.d.app.media_describer
        who = video.listener(
            describer is not None and await self._hears(describer.client), "audio" in await self.accepted(),
        )
        if who is None or not self.d._media_budget(chat_id):
            return "not transcribed", None
        client = describer.client if who == "media" else self.d.app.client
        messages = [{"role": "user", "content": [
            {"type": "text", "text": TRANSCRIBE_PROMPT},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": "m4a"}},
        ]}]
        try:
            async with self.d._media_request(chat_id, background=background):
                reply = await client.complete(messages, [])
        except ModelCallSkipped:
            return "not transcribed", None
        except Exception as exc:
            log.warning("could not transcribe a clip in chat %s (%s)", chat_id, type(exc).__name__)
            return "not transcribed", None
        text = reply.get("content")
        text = text.strip() if isinstance(text, str) else ""
        if not text or text.lower().startswith("(no speech"):
            return "no speech", None
        return "heard", text[:TRANSCRIPT_CHARS]

    async def _strip_images(self, chat_id: int, msg_id: int, media: dict) -> list[bytes]:
        """A watched clip's strips, from memory or the media folder (only through this chat's own id). Empty when they
        are gone: the media cap removed them, or the cap is zero and the process restarted."""
        if (held := self._strips.get((chat_id, msg_id))) is not None:
            return held
        images = []
        for rel in media.get("strips") or ():
            data = await self.d.app.media_files.read(chat_id, str(rel))
            if data is None:
                return []
            images.append(data)
        return images

    async def summarise(self, chat_id: int, msg_id: int, media: dict, request: AsyncContextManager[None]) -> str | None:
        """What happens in a watched clip, from its strips (one media-model call each, in turn, every part told what
        came before) joined with what was heard in it. None when the strips exist but none could be described.
        Callers that come while it is running (arrival, triage, a catch-up) share its one result, so the media model
        is asked, and paid, once; `request` is the first caller's."""
        key = (chat_id, msg_id)
        task = self._summaries.get(key)
        if task is None:
            task = self._summaries[key] = self.d._spawn(self._summarise(chat_id, msg_id, media, request))
            task.add_done_callback(lambda _: self._summaries.pop(key, None))
        return await asyncio.shield(task)

    async def _summarise(self, chat_id: int, msg_id: int, media: dict, request: AsyncContextManager[None]) -> str | None:
        watched = media["watched"]
        images = await self._strip_images(chat_id, msg_id, media)
        describer = self.d.app.media_describer
        if not images:  # evicted, or the media cap is zero and Dom restarted: the clip whole if it can still be fetched
            whole = await describer.describe(media, request=request)
            return " ".join(filter(None, (
                whole or LOST_PICTURE, sound_line(watched), "" if whole else cut_line(watched),
            )))
        client = describer.client
        spans, parts, before = watched.get("spans") or [], [], ""
        transcript = watched.get("transcript")
        if images:
            async with request:
                for number, image in enumerate(images, 1):
                    start, end = spans[number - 1] if number <= len(spans) else (0.0, watched.get("seconds", 0.0))
                    prompt = STRIP_PROMPT.format(
                        number=number, total=len(images), start=video.clock(start), end=video.clock(end),
                    )
                    if before:
                        prompt += f" The part before: {before}"
                    if transcript:
                        prompt += f' What was said in the whole clip: "{transcript[:1500]}"'
                    messages = [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")}},
                    ]}]
                    try:
                        reply = await client.complete(messages, [])
                    except ModelCallSkipped:
                        break
                    except Exception as exc:
                        log.warning("could not describe part %d of a clip in chat %s (%s)", number, chat_id, type(exc).__name__)
                        continue
                    text = reply.get("content")
                    if isinstance(text, str) and text.strip():
                        before = text.strip()
                        parts.append(f"{video.clock(start)}-{video.clock(end)}: {before}" if len(images) > 1 else before)
            if not parts:
                return None
        return " ".join(filter(None, (*parts, sound_line(watched), cut_line(watched), missing_line(watched)))) or None

    # --- a description once the media is no longer attached ---

    def describe_aged(self, chat_id: int, window: list[StoredMessage]) -> None:
        """Dom has answered, so media he saw as it was in this window is not attached again. Give each such file the
        description a later prompt shows in its `<media>` tag (otherwise the tag stays empty and he forgets what it
        showed). Only media his model takes as it is: anything else was described when it arrived. Runs in the
        background under the media budget and spend cap, once per file. Only the chat's own window is read, so
        nothing crosses chats."""
        todo = []
        for row in window:
            media = _media(row)
            key = (chat_id, row.msg_id)
            if (row.role != "user" or row.is_bot or row.msg_id is None or row.is_deleted or key in self._aged
                    or media.get("kind") not in MODALITY_OF or media.get("kind") == "sticker"
                    or media.get("description") is not None):
                continue
            self._aged[key] = None
            todo.append((row.msg_id, media))
        while len(self._aged) > AGED_TRIED:
            self._aged.popitem(last=False)
        if todo:
            self.d._spawn(self._describe_aged(chat_id, todo))

    async def _describe_aged(self, chat_id: int, todo: list[tuple[int, dict]]) -> None:
        for index, (msg_id, media) in enumerate(todo):
            try:
                # what the model can't take was described on arrival, or tried then
                if await self.takes(media) and not await self.d._describe(
                    chat_id, msg_id, media, self.d._media_request(chat_id, background=True),
                ):
                    for left_over, _ in todo[index:]:  # the budget is used up: offer these again on a later turn
                        self._aged.pop((chat_id, left_over), None)
                    return
            except Exception:
                log.warning("could not describe aged media in chat %s", chat_id, exc_info=True)


async def _text(value: str | None) -> str | None:
    return value
