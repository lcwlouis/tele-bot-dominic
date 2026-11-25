# Memory file formats

Everything Dom remembers is Markdown in the instance home. This page documents the line format the notes use and the
journal archive, for people editing files by hand and for the dashboard.

## Note lines

People notes (`memory/people/<id>.md`), chat notes (`memory/chats/<id>.md`), `memory/MEMORY.md` and the owner's
`private/USER.md` and `private/NOTES.md` are bullet lists. A line may end with metadata groups in braces:

```
- Works as a nurse (as of 2026-11) {chat -100123}
- Hates surprises {any}
- Plans a trip to Japan (until 2027-04-30) {chat -100123} {core dom 2026-11-25: she talks about it all the time}
- Loves tea {core owner 2026-11-25}
```

| Group | Meaning |
|---|---|
| `{chat <id>}` | Told in that chat. Shown only in that chat's prompt (the owner's DM sees every public line). |
| `{any}` | General and not sensitive: the person would share it anywhere. Shown wherever the person is. |
| none | An older line, or one typed by hand: counts as `{any}`. |
| `{core owner DATE}` | A core memory the owner pinned. Dom can't unpin it. |
| `{core dom DATE: why}` | A core memory Dom chose, with the day and his reason. |

Tags never count toward a file's size limit and the model never sees them in a chat prompt. `MEMORY.md` is read in every
chat, so it refuses `{chat ...}` tags. The owner can change any tag by editing the file.

`(as of YYYY-MM)` and `(until YYYY-MM-DD)` are plain text in the line. The daily upkeep job moves a line whose `until`
date has passed to the former file. Core lines never expire.

## Core memories

A core memory is a pinned line. It never fades, the memory pass never rewrites or drops it, and a period summary links
it. Lines the owner adds or changes on the Memory page are pinned automatically (editing only a tag doesn't pin).
Dom pins and unpins his own in the background memory pass, which runs after his reply, never before. The cap is
`core_memory_cap` (default 30, the owner's pins included). The owner deletes a core memory by deleting its line or
its `{core ...}` group in the file; `GET /api/memory/core` lists them all as `{"cap": n, "items": [{file, text, source, by, day, why}]}`.

## Former files

When the memory pass replaces or removes a line, or a line expires, the old line is appended to `<file>.former.md`
(`memory/people/7.md` becomes `memory/people/7.former.md`, `private/USER.md` becomes `private/USER.former.md`) with the
day and the reason:

```
- Works at the bakery (as of 2025-06) {any} [former 2026-03-01: replaced]
```

Former files are not in any prompt, have no size limit, and are searchable by the owner.

## Journal archive

`memory/JOURNAL.md` and `private/JOURNAL.md` keep their last 30 entries. Older entries move, one at a time, into
`memory/journal/YYYY-MM.md` and `private/journal/YYYY-MM.md`, each holding every entry (`## YYYY-MM-DD`) of its month,
oldest first. The archive is written before the live file is trimmed, so an interrupted move leaves an entry in both
places, never neither.

A background job adds summaries above the entries of a month file:

```
# Journal 2025-12
Week 2025-W49: <a few sentences>
Core 2025-W49: <a core memory pinned that week>
Month 2025-12: <a short paragraph, rolled up from its weeks>
```

A week lives in the file of the month its Monday falls in. `YYYY.md` holds a year's summary (`Year 2025: ...`).
A period is summarised once it ended and every entry in it has been archived. Older periods get longer spans in shorter
text; the entries below the summaries are never changed. `RECENT.md` holds the "recent life" block built from the last
4 weeks, 6 months and 3 years. It reaches the owner's DM only, after the chat window.
