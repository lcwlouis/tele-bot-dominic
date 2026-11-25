"""Dom sees the real photo and hears the real voice note: which media goes into a prompt, and what the chat route takes.

For a turn, the newest unanswered incoming media and Dom's own latest generated media are attached to the prompt's
user message (dom.modalities turns them into content parts after the text, so the cached prefix is unchanged), as far
as the chat route's model accepts that type and the per-prompt limits allow. Media the route can't take is described
by the media route instead, as before. Learning what a model accepts (a list fetch, a one-off probe) runs in the
background or when the owner asks, never on the reply path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from functools import partial
from typing import TYPE_CHECKING

from dom import media as media_mod
from dom import model_lists, turn_log
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


def _media(row: StoredMessage) -> dict:
    try:
        media = json.loads(row.media_json) if row.media_json else None
    except ValueError:
        return {}
    return media if isinstance(media, dict) else {}


class NativeMedia:
    def __init__(self, dispatcher: Dispatcher):
        self.d = dispatcher
        self.modalities = Modalities(dispatcher.app.store)
        self._bytes: OrderedDict[tuple[int, int], tuple[str, bytes]] = OrderedDict()
        self._learning: dict[tuple, float] = {}
        self.last: frozenset[str] = frozenset()  # what the chat route took the last time it was asked
        self._unknown: list[ChatClient] = []  # providers whose input types nothing has told yet

    # --- what the chat route takes ---

    def _chat_providers(self) -> list[ChatClient]:
        client = self.d.app.client
        providers = getattr(client, "providers", None)
        if providers is None:
            providers = (client,) if isinstance(client, ChatClient) else ()
        return [provider for provider in providers if isinstance(provider, ChatClient)]

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
        modality = MODALITY_OF.get(media.get("kind"))
        accepted = await self.accepted()
        for provider in self._unknown:  # a photo arrived and nobody knows if his model sees: find out, off the reply path
            self._learn_later(provider)
        if modality is None or modality not in accepted:
            return False
        size = media.get("file_size")
        if size is not None and size > min(self.max_bytes(), media_mod.MAX_BYTES.get(media["kind"], 0)):
            return False
        if media.get("kind") == "sticker" and (media.get("mime") or media_mod.DEFAULT_MIME["sticker"]) != "image/webp":
            return bool(media.get("preview_file_id"))
        return True

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

    async def attach(self, messages: list[dict], chat_id: int, window: list[StoredMessage]) -> None:
        """Put this turn's media on the prompt's user message. Everything comes from this chat's own window, so
        nothing from another chat can reach the prompt."""
        accepted = await self.accepted()
        if not accepted:
            return
        left = {kind: count for kind, count in self._limits().items() if kind in accepted}
        last_reply = max((i for i, row in enumerate(window) if row.role == "assistant"), default=-1)
        picks: dict[int, tuple[StoredMessage, dict, str, bool]] = {}
        for index in range(len(window) - 1, last_reply, -1):  # newest incoming first
            row = window[index]
            media = _media(row)
            modality = MODALITY_OF.get(media.get("kind"))
            if row.role != "user" or row.is_bot or modality is None or row.msg_id is None or left.get(modality, 0) <= 0:
                continue
            if not await self.takes(media):
                continue
            left[modality] -= 1
            picks[index] = (row, media, modality, False)
        for index in range(len(window) - 1, -1, -1):  # Dom's own latest generated media, once
            row = window[index]
            media = _media(row)
            modality = MODALITY_OF.get(media.get("kind"))
            if row.role == "assistant" and media.get("file") and modality is not None:
                if left.get(modality, 0) > 0 and index not in picks:
                    left[modality] -= 1
                    picks[index] = (row, media, modality, True)
                break
        attachments: list[Attachment] = []
        for index in sorted(picks):
            row, media, modality, own = picks[index]
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
                if found is None:
                    continue
                mime, data = found
                who = row.sender_name or "someone"
                ref = f"message {row.msg_id}, {media['kind'].replace('_', ' ')} from {who}"
                describe = partial(self._describe_now, chat_id, row.msg_id, media)
            attachments.append(Attachment(modality, mime, data, ref, describe))
        if attachments:
            messages[-1]["attachments"] = attachments
            counts = {kind: sum(1 for a in attachments if a["modality"] == kind) for kind in MODALITIES}
            turn_log.step("sees " + ", ".join(f"{n} {kind}" for kind, n in counts.items() if n))


async def _text(value: str | None) -> str | None:
    return value
