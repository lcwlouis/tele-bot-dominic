# Memory file formats

Everything Dom remembers is Markdown in the instance home. This page documents the line format the notes use and the
journal archive, for people editing files by hand and for the dashboard.

## Note lines

People notes (`memory/people/<id>.md`), chat notes (`memory/chats/<id>.md`), entity notes under `memory/places/`,
`memory/projects/` and `memory/goals/`, `memory/MEMORY.md` and the owner's
`private/USER.md` and `private/NOTES.md` are bullet lists. A line may end with metadata groups in braces:

```
- Works as a nurse (as of 2026-11) {chat -100123}
- Hates surprises {any}
- Plans a trip to Japan (until 2027-04-30) {chat -100123} {entities person:123|place:japan|goal:take-a-break} {core dom 2026-11-25: she talks about it all the time}
- Loves tea {core owner 2026-11-25}
```

| Group | Meaning |
|---|---|
| `{chat <id>}` | Told in that chat. Shown only in that chat's prompt (the owner's DM sees every public line). |
| `{any}` | General and not sensitive: the person would share it anywhere. Shown wherever the person is. |
| none | An older line, or one typed by hand: counts as `{any}`. |
| `{core owner DATE}` | A core memory the owner pinned. Dom can't unpin it. |
| `{core dom DATE: why}` | A core memory Dom chose, with the day and his reason. |
| `{entities person:ID\|place:slug\|project:slug\|goal:slug}` | The people, places, projects and goals this fact links. Slugs use lowercase letters, digits and dashes. |

A group counts wherever it stands in the line (`- Secret {chat -100123} (confirmed)` is tagged to that chat), and every
prompt, search result and summary shows the line without it. Source, entity and core groups never count toward a file's size limit. `MEMORY.md` is
read in every chat, so it refuses `{chat ...}` tags. The owner can change any tag by editing the file.

People, chat and entity notes carry source tags. When the memory pass writes anywhere else (`MEMORY.md`, the owner's
`private/` notes) the tags in its text are dropped, and it can never write a `{core ...}` group in any file: a replace
takes the new line's tag from the new text alone (the old line's `{any}` or `{chat ...}` does not carry over).

`(as of YYYY-MM)` and `(until YYYY-MM-DD)` are plain text in the line. The daily upkeep job moves a line whose `until`
date has passed to the former file. Core lines never expire.

## Entity files and the graph index

People keep their existing numeric files. When the background memory pass first needs a durable place, project or
goal, it may create `memory/places/<slug>.md`, `memory/projects/<slug>.md` or `memory/goals/<slug>.md`. Each fact line
records every entity it is about; entities on the same line are linked. The source chat tag remains the privacy
boundary, so a group or friend's DM can retrieve only its own lines and `{any}` lines, while the owner DM can retrieve
all public lines. Two rules close the gaps a tag cannot: an untagged line in `memory/chats/<id>.md` belongs to that chat
alone, and another chat sees a person's note only when that person is in the chat, the same reach as the notes prompt.
The filter is SQL in code on every mode (keyword, meaning, entity) and on `related`, never prompt text.

SQLite is a rebuildable index, not the source of truth. It records each fact's file, line, source chat, dates, entities
and links, and indexes messages for retrieval. Messages are read incrementally past a cursor; an edit or delete drops
the message's facts in the same transaction, and a rare chunked sweep catches removals from any other path. A vector
belongs to a line's text, so a line that survives an edit of its file is never embedded again. `dom rebuild-index HOME`
rebuilds it from Markdown and message history.
Search mode `keyword` is the fast default; `meaning` blends BM25, embeddings and the same recency weighting; `entity`
returns everything visible about an entity and the entities linked on those facts. Entity and core groups are stripped
before a model sees a note.

## Core memories

A core memory is a pinned line. It never fades, the memory pass never rewrites or drops it, and a period summary links
it. Lines the owner adds or changes on the Memory page are pinned automatically (editing only a tag doesn't pin).
Dom pins and unpins his own in the background memory pass, which runs after his reply, never before. The cap is
`core_memory_cap` (default 30, the owner's pins included). The owner deletes a core memory by deleting its line or
its `{core ...}` group in the file; `GET /api/memory/core` lists them all as `{"cap": n, "items": [{file, text, source, by, day, why}]}`.

## On the Memory page

Files open as a preview first; Edit shows the source. The preview hides every metadata group and shows it as a small
chip instead: `from: <chat name>` for `{chat <id>}`, `shareable` for `{any}`, and `core, pinned by you` or `core,
pinned by Dom` for a pin. A journal keeps each line on its own. People's notes are listed by the person's name, and
the journal archive and every former file sit read-only under a folded Archive group. Core memories lists every pin
with who made it, the day and Dom's reason; deleting one offers to unpin it (the line stays, without its `{core ...}`
group) or to delete the line, saved like any other edit on the page.

## Former files

When the memory pass replaces or removes a line, or a line expires, the old line is appended to `<file>.former.md`
(`memory/people/7.md` becomes `memory/people/7.former.md`, `private/USER.md` becomes `private/USER.former.md`) with the
day and the reason:

```
- Works at the bakery (as of 2025-06) {any} [former 2026-03-01: replaced]
```

Former files are not in any prompt, have no size limit, and are searchable by the owner. They are read-only to the
memory pass, the owner-DM edit tool and the pin tools; they are the record of what changed.

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
A period is summarised the day after it ended (so a late entry still lands), whether its entries are in the archive or
still in the live `JOURNAL.md`; the job only reads the live file. A week or month needs all its weeks or months
summarised first, and a month with no entries in a week (the private journal only has days with private messages)
simply has no summary for that week. Older periods get longer spans in shorter text; the entries below the summaries are
never changed.

## The recent-life block

`RECENT.md` next to the month files holds what a reader needs, finest first, with no period left out:

```
Through 2026-02-15
Weeks:
- 2026-W06: ...
Months:
- 2026-01: ...
Years:
- 2025: ...
```

- Weeks: the last 4 weeks, plus any older week whose month or months have no summary yet.
- Months: the last 6 months, plus any older month of a year with no summary yet.
- Years: the last 3 summarised years.
- `Through` is the last day a week summary covers. It is for the code and is never shown to the model.

A coarser summary takes a period over only once it exists, so nothing falls into a gap while the next summary waits to be
written. The text changes about weekly: when a week completes, or one ages out of the window. The block reaches the
owner's DM only, after the chat window, and starts with `Latest days, not summarised yet`: each live journal entry
dated after `Through` (at most 9, each cut to 200 characters), read straight from `JOURNAL.md` with no model call.
