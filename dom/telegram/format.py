"""Render CommonMark to Telegram text and UTF-16 entities."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, replace
import logging
import re
import unicodedata
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import MessageEntity
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

from dom.actions import TELEGRAM_TEXT_LIMIT, utf16_len

log = logging.getLogger(__name__)

_MARKDOWN = MarkdownIt().enable(("strikethrough", "table"))
_INLINE_STYLES = {"strong": "bold", "em": "italic", "s": "strikethrough"}
_FORMATTED_INLINE = {*_INLINE_STYLES, "link", "code_inline"}
# Only text with clear structure is rendered. A casual line such as "lol *sigh*" or "5*6*7" goes out exactly as
# written, because a lone pair of asterisks or underscores in chat is rarely meant as Markdown.
_STRUCTURED = re.compile(
    r"```|~~~|`[^`\n]+`|\*\*\S[^\n]*?\*\*|__\S[^\n]*?__|^#{1,6}[ \t]+\S|\[[^\]\n]+\]\([^)\s]+\)|^>[ \t]",
    re.MULTILINE,
)
_BLANK_LINE = re.compile(r"(?:^[ \t]*\r?\n)+|(?:\r?\n[ \t]*)+$")
_BREAKS = (
    re.compile(r"\r?\n(?:[ \t]*\r?\n)+"),
    re.compile(r"\r?\n"),
    re.compile(r"[^\S\r\n]+"),
)

TextChunk = tuple[str, list[MessageEntity]]


@dataclass(frozen=True)
class _Span:
    kind: str
    start: int
    end: int
    url: str | None = None
    language: str | None = None


def _http_url(href: str) -> str | None:
    try:
        url = urlsplit(href)
        return href if url.scheme in ("http", "https") and url.hostname else None
    except ValueError:
        return None


class _Render:
    def __init__(self, source: str):
        self.lines = re.split(r"\r\n|\r|\n", source)
        self.line_starts = [0, *(m.end() for m in re.finditer(r"\r\n|\r|\n", source))]
        self.source_starts: dict[int, int] = {}
        self.parts: list[str] = []
        self.spans: list[_Span] = []
        self.pos = 0

    def write(self, text: str) -> None:
        self.parts.append(text)
        self.pos += len(text)

    def mark(self, line: int, position: int) -> None:
        self.source_starts.setdefault(position, self.line_starts[line])

    def raw(self, node: SyntaxTreeNode) -> str:
        start, end = node.map
        return "\n".join(self.lines[start:end])

    def inline(self, node: SyntaxTreeNode) -> None:
        start = self.pos
        first_span = len(self.spans)
        kind = _INLINE_STYLES.get(node.type)
        url = None
        if node.type in ("text", "html_inline", "code_inline"):
            self.write(node.content)
            if node.type == "code_inline":
                kind = "code"
        elif node.type in ("softbreak", "hardbreak"):
            self.write("\n")
        elif node.type == "link":
            url = _http_url(node.attrGet("href") or "")
            if url:
                kind = "text_link"
        for child in node.children:
            self.inline(child)
        if node.type == "link" and not url:
            del self.spans[first_span:]
        if kind and start < self.pos:
            self.spans.append(_Span(kind, start, self.pos, url=url))

    def blocks(self, nodes: Sequence[SyntaxTreeNode], depth: int = 0, quoted: bool = False) -> None:
        for index, node in enumerate(nodes):
            if index:
                self.write("\n\n")
            start = self.pos
            if node.map is not None:
                self.mark(node.map[0], start)
            if node.type in ("paragraph", "heading"):
                inline = node.children[0]
                if node.type == "paragraph" and not any(n.type in _FORMATTED_INLINE for n in inline.children):
                    # A top-level ordinary paragraph retains its spacing, escapes and character references.
                    self.write(self.raw(node) if depth == 0 and not quoted else inline.content)
                else:
                    self.inline(inline)
                if node.type == "heading":
                    self.spans.append(_Span("bold", start, self.pos))
            elif node.type in ("fence", "code_block", "table"):
                code = self.raw(node) if node.type == "table" else node.content
                if _unclosed_fence(node):
                    self.write(self.raw(node))
                    continue
                info = node.info.split() if node.type == "fence" else []
                # Code lines correspond to source lines, including when CommonMark removes list indentation.
                visible = _BLANK_LINE.sub("", code)
                first_line = node.map[0] + (node.type == "fence")
                if leading := _BLANK_LINE.match(code):
                    first_line += leading.group().count("\n")
                for index, match in enumerate(re.finditer(r"(?m)^(?=[\s\S])", visible)):
                    self.mark(first_line + index, start + match.start())
                self.write(visible)
                if start < self.pos:
                    self.spans.append(_Span("pre", start, self.pos, language=info[0] if info else None))
                elif node.type == "fence":
                    self.write(self.raw(node))
            elif node.type == "blockquote":
                self.blocks(node.children, depth, quoted=True)
                if not quoted:
                    self.spans.append(_Span("blockquote", start, self.pos))
            elif node.type in ("bullet_list", "ordered_list"):
                self.items(node, depth, quoted)
            else:
                self.write(self.raw(node))

    def items(self, node: SyntaxTreeNode, depth: int, quoted: bool) -> None:
        first = int(node.attrGet("start") or 1)
        for index, item in enumerate(node.children):
            if index:
                self.write("\n")
            self.mark(item.map[0], self.pos)
            self.write("  " * depth + (f"{first + index}. " if node.type == "ordered_list" else "• "))
            for block_index, block in enumerate(item.children):
                if block_index:
                    self.write("\n" if block.type.endswith("_list") else "\n\n")
                    if not block.type.endswith("_list"):
                        self.write("  " * (depth + 1))
                if block_index == 0 and block.type == "fence":
                    self.write("\n")
                self.blocks([block], depth + 1, quoted)


def _exclude(span: _Span, blocked: Sequence[_Span]) -> list[_Span]:
    """Give code and links priority where Telegram cannot nest entities."""
    result = []
    start = span.start
    for other in blocked:
        if other.end <= start:
            continue
        if other.start >= span.end:
            break
        if start < other.start:
            result.append(replace(span, start=start, end=other.start))
        start = other.end
    if start < span.end:
        result.append(replace(span, start=start))
    return result


def _compatible_spans(text: str, spans: list[_Span]) -> list[_Span]:
    code = sorted((s for s in spans if s.kind in ("code", "pre")), key=lambda s: s.start)
    links = [
        s for s in spans if s.kind == "text_link"
        and not any(c.start < s.end and s.start < c.end for c in code)
    ]
    exclusive = sorted([*code, *links], key=lambda s: s.start)
    quotes = [part for span in spans if span.kind == "blockquote" for part in _exclude(span, exclusive)]
    quote_edges = sorted({edge for quote in quotes for edge in (quote.start, quote.end)})
    compatible = [*exclusive, *quotes]
    for span in spans:
        if span.kind not in _INLINE_STYLES.values():
            continue
        for part in _exclude(span, code):
            # A bold or italic run may straddle where a link interrupted a quote.
            boundaries = [part.start, *(edge for edge in quote_edges if part.start < edge < part.end), part.end]
            compatible.extend(replace(part, start=a, end=b) for a, b in zip(boundaries, boundaries[1:]))

    result: list[_Span] = []
    for span in sorted(compatible, key=lambda s: (s.start, -s.end)):
        if span.kind not in ("code", "pre"):
            content = text[span.start:span.end]
            span = replace(
                span, start=span.start + len(content) - len(content.lstrip()),
                end=span.start + len(content.rstrip()),
            )
        if span.start < span.end and not any(
            s.kind == span.kind and s.start <= span.start and span.end <= s.end
            for s in result if span.kind in _INLINE_STYLES.values()
        ):
            result.append(span)
    return result


def _unclosed_fence(node: SyntaxTreeNode) -> bool:
    if node.type != "fence":
        return False
    first, after = node.map
    content = node.content
    lines = content.count("\n") + bool(content and not content.endswith("\n"))
    return after - first == lines + 1


def _is_plain(node: SyntaxTreeNode) -> bool:
    return node.type in ("html_block", "hr") or _unclosed_fence(node) or (
        node.type == "paragraph" and not any(n.type in _FORMATTED_INLINE for n in node.children[0].children)
    )


def _render(text: str) -> tuple[str, list[_Span], dict[int, int]]:
    if not _STRUCTURED.search(text):
        return text, [], {}
    nodes = SyntaxTreeNode(_MARKDOWN.parse(text)).children
    if all(_is_plain(node) for node in nodes):
        return text, [], {}
    renderer = _Render(text)
    renderer.blocks(nodes)
    rendered = "".join(renderer.parts)
    if not rendered.strip():
        return text, [], {}
    return rendered, _compatible_spans(rendered, renderer.spans), renderer.source_starts


def _is_emoji_continuation(ch: str) -> bool:
    code = ord(ch)
    return (
        code == 0x200D  # ZWJ
        or 0xFE00 <= code <= 0xFE0F  # Variation Selectors
        or 0xE0100 <= code <= 0xE01EF  # Variation Selectors Supplement
        or 0x1F3FB <= code <= 0x1F3FF  # Emoji skin-tone modifiers
        or 0xE0020 <= code <= 0xE007F  # Tag characters
        or unicodedata.category(ch) in ("Mn", "Mc", "Me")  # Combining marks (incl. keycap U+20E3)
    )


def _safe_hard_cut(text: str, max_end: int) -> int:
    """Back off from `max_end` so a hard split never cuts inside an emoji or combining sequence."""
    cut = max_end
    while cut > 0 and cut < len(text):
        prev, nxt = text[cut - 1], text[cut]
        if prev == "\u200d" or _is_emoji_continuation(nxt):
            cut -= 1
            continue
        if 0x1F1E6 <= ord(prev) <= 0x1F1FF and 0x1F1E6 <= ord(nxt) <= 0x1F1FF:
            run = 0
            i = cut - 1
            while i >= 0 and 0x1F1E6 <= ord(text[i]) <= 0x1F1FF:
                run += 1
                i -= 1
            if run % 2 == 1:
                cut -= 1
                continue
        break
    return cut if cut > 0 else max_end


def _split(text: str, spans: Sequence[_Span], limit: int) -> list[tuple[int, TextChunk]]:
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + utf16_len(char))

    chunks = []
    start = 0
    while start < len(text):
        end = bisect_right(offsets, offsets[start] + limit) - 1
        if end < len(text):
            protected = [
                s for s in spans if s.start < end and start < s.end
                and offsets[s.end] - offsets[s.start] <= limit
            ]

            def legal(cut: int, resume: int) -> bool:
                return (start < cut and bool(text[start:cut].strip())
                        and not any(s.start < resume and cut < s.end for s in protected))

            # An oversized code block may split at newlines, then at a hard boundary for a single long line.
            pre = next((s for s in spans if s.kind == "pre" and s.start <= start < s.end), None)
            if pre and pre.end > end:
                breaks = (_BREAKS[1],)
            else:
                breaks = _BREAKS
            resume = end
            for pattern in breaks:
                candidates = [
                    (match.start(), match.end()) for match in pattern.finditer(text, start, end + 1)
                    if legal(match.start(), match.end())
                ]
                if candidates:
                    end, resume = candidates[-1]
                    break
            else:
                # Back off from entities that fit in a fresh chunk, and from split graphemes.
                while end > start:
                    cut = min((s.start for s in protected if s.start < end < s.end), default=end)
                    cut = start + _safe_hard_cut(text[start:end + 1], cut - start)
                    if cut == end:
                        break
                    end = cut
                if end <= start or not text[start:end].strip():
                    end = bisect_right(offsets, offsets[start] + limit) - 1
                resume = end

            # Drop a run of newline separators between chunks while preserving code indentation.
            newline = _BREAKS[0].match(text, resume) or _BREAKS[1].match(text, resume)
            if newline:
                resume = newline.end()
        else:
            resume = end

        # Avoid stray blank lines at chunk edges. Do not strip spaces inside source code or list indentation.
        part = text[start:end]
        if offsets[-1] > limit:
            if not part.strip():
                start = resume
                continue
            for match in _BLANK_LINE.finditer(part):
                if match.start() == 0:
                    start += match.end()
                else:
                    end -= len(part) - match.start()
            part = text[start:end]
        if part:
            entities = [
                MessageEntity(
                    type=s.kind, offset=offsets[max(start, s.start)] - offsets[start],
                    length=offsets[min(end, s.end)] - offsets[max(start, s.start)],
                    url=s.url, language=s.language,
                )
                for s in spans if max(start, s.start) < min(end, s.end)
            ]
            chunks.append((start, (part, entities)))
        start = resume
    return chunks


def _render_or_plain(text: str) -> tuple[str, list[_Span], dict[int, int]]:
    try:
        return _render(text)
    except Exception:
        log.warning("could not render Telegram Markdown, sending original text", exc_info=True)
        return text, [], {}


def format_text(
    text: str, *, limit: int = TELEGRAM_TEXT_LIMIT, literal: bool = False, pre: bool = False,
) -> list[TextChunk]:
    """Render once, then split. Literal notices skip parsing; pre treats the entire source as code."""
    if limit < 2:
        raise ValueError("Telegram chunk limit must fit a UTF-16 surrogate pair")
    if pre:
        text = _BLANK_LINE.sub("", text)
        spans = [_Span("pre", 0, len(text))]
    elif literal:
        spans = []
    else:
        text, spans, _ = _render_or_plain(text)
    return [chunk for _, chunk in _split(text, spans, limit)]


def format_delivery(source: str) -> list[tuple[TextChunk, str]]:
    """Return each Telegram chunk with the original Markdown to record against its message id."""
    text, spans, source_starts = _render_or_plain(source)
    pieces = _split(text, spans, TELEGRAM_TEXT_LIMIT)
    if not pieces:
        return []

    # Block and code line boundaries map directly to source lines. A hard cut inside a transformed
    # line maps within the surrounding anchors. In both cases, the stored slices reassemble exactly.
    if text == source:
        cuts = [position for position, _ in pieces[1:]]
    else:
        source_starts.update({0: 0, len(text): len(source)})
        anchors = sorted(source_starts)
        cuts = []
        for position, _ in pieces[1:]:
            right = bisect_right(anchors, position)
            left_pos, right_pos = anchors[right - 1], anchors[right]
            left_source, right_source = source_starts[left_pos], source_starts[right_pos]
            cuts.append(left_source + (position - left_pos) * (right_source - left_source) // (right_pos - left_pos))

    boundaries = [0, *cuts, len(source)]
    return [
        (chunk, source[begin:end])
        for (_, chunk), begin, end in zip(pieces, boundaries, boundaries[1:])
    ]


async def with_entities(send, text: str, entities: list[MessageEntity], **options):
    """Call a Telegram text method and fall back once on an entity-specific rejection."""
    if not entities:
        return await send(text, **options)
    try:
        return await send(text, entities=entities, **options)
    except TelegramBadRequest as error:
        reason = error.message.lower()
        if not any(marker in reason for marker in ("entity", "entities", "wrong http url")):
            raise
        log.warning("Telegram rejected text entities, retrying as plain text")
        return await send(text, **options)
