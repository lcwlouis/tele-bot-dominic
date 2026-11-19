# Dashboard design direction

The dashboard is the owner's view into Dom: where he is, what's waiting for a yes, what he remembers, and what he costs. The owner asked for JARVIS/FRIDAY. The direction keeps the parts of that look that carry meaning (a dark cockpit, light that marks what is live, one instrument at the centre) and drops the parts that are only decoration (scan lines, grids of fake readouts, glow on everything).

Mock: [`2026-11-19-dashboard-mock.html`](2026-11-19-dashboard-mock.html), a static page with made-up data. Open it in a browser; it is responsive down to 360 px.

## The one bold thing: the day dial

A JARVIS screen is built around one round instrument. Dom's is a **24-hour dial** at the top of the home page, and every mark on it is real data:

- the dark arc is his sleep window (from `HABITS.md`)
- teal arcs are today's online sessions
- short ticks around the rim are messages per hour (the same counts habit learning uses)
- one hand points at the current time
- the centre says what he is doing right now, in words ("Online in Book Club until 21:40")

Nothing else on the page is circular except the two small meters that echo it (memory file fill, spend against cap). Everything else is quiet, flat rows.

## Tokens

### Colour

| Name | Hex | Use |
|---|---|---|
| Hull | `#0C1726` | Page background. A deep navy, not black: the room is lit. |
| Panel | `#13233A` | Raised surfaces: the dial plate, panels. |
| Seam | `#24405F` | Borders and dividers. |
| Arc | `#5CE1E6` | Live things: online, the clock hand, primary actions, focus rings. |
| Amber | `#F2B544` | Things waiting for the owner: proposals, warnings, spend near the cap. |
| Signal | `#FF6B6B` | Errors, leaks held back, Reject. |
| Ink | `#D8E6F3` | Body text. |
| Fog | `#7C93AD` | Secondary text, timestamps, axis labels. |

Rules: teal means *live*, amber means *waiting on you*. A screen with no amber has nothing to do. Contrast: Ink on Hull 12.9:1, Fog on Hull 5.6:1, Arc on Hull 10.4:1, all above WCAG AA.

### Type

- **Saira Semi Condensed** (600/500) for headings, the dial centre and numbers. Its squared, slightly narrow letters read as instrument lettering without being a novelty face.
- **Atkinson Hyperlegible** (400/700) for everything people read: chat names, memory text, proposal bodies. Built for legibility, which matters on a phone at night.
- No monospace except inside the memory editor's text area and diffs, where alignment matters.
- Scale (1.25 ratio, 16 px base): 12.8 / 16 / 20 / 25 / 31.25 / 39. Line height 1.5 for body, 1.15 for headings. Sentence case everywhere; no all-caps labels.
- Fonts are vendored as woff2 under `dom/web/static/fonts/` (the CSP allows only `self`).

### Space, shape, motion

- Spacing steps: 4, 8, 12, 16, 24, 32, 48.
- Panels have one **chamfered corner** (top-right, 14 px, via `clip-path`), the cockpit cue. Rows inside panels are flat with a 1 px Seam divider. Buttons have a 6 px radius. Nothing else gets rounded corners or shadows.
- Motion: one orchestrated moment. On page load the dial's arcs draw clockwise from midnight in 700 ms. After that, motion only answers the owner (a proposal card collapses when approved; a row flashes Arc once when a wake succeeds). `prefers-reduced-motion` turns all of it off.
- Focus: 2 px Arc outline with 2 px offset on every interactive element.

## Layout

Phone first: one column, in this order: dial, "Waiting for you" (proposals), chats, usage, memory, turn log. On wide screens (≥ 960 px) a left rail holds the section links and the dial sits beside the proposals:

```
Phone (360–959 px)          Wide (≥ 960 px)
┌──────────────────┐        ┌──────┬────────────────────────────┐
│   day dial       │        │ rail │  dial      │ waiting for   │
├──────────────────┤        │      │            │ you (cards)   │
│ waiting for you  │        │ Now  ├────────────┴───────────────┤
├──────────────────┤        │ Chats│  chats (table)             │
│ chats (rows)     │        │ Mem. ├──────────────┬─────────────┤
├──────────────────┤        │ Usage│  usage       │ turn log    │
│ usage            │        │ Log  ├──────────────┴─────────────┤
├──────────────────┤        │      │  memory editor             │
│ memory · log     │        └──────┴────────────────────────────┘
└──────────────────┘
```

Text is left-aligned throughout; numbers in tables are right-aligned with tabular figures.

## Components

- **Chat row:** presence glyph (a short arc: full teal = online, thin Fog = offline, dark with a dot = asleep), chat name, preset, "until 21:40", unread count. Actions (Wake, Sleep, Leave) appear in a row below on phones and inline on wide screens. Leave is Signal-coloured text, never a filled button.
- **Proposal card:** amber left edge; kind ("Soul edit", "Memory from ChatGPT"); a diff for soul edits (removed lines struck through in Signal, added in Arc); Approve (filled Arc) and Reject (Signal outline). Imported memories group into one card per import with "Approve all" and per-fact toggles.
- **Memory editor:** file list on the left (private files under a "Only in your DM" heading), editor on the right, a small arc meter showing characters used against the limit, Save changes. `HABITS.md` is shown read-only with a note that Dom writes it from message counts.
- **Usage:** a semicircle meter for today's spend against the cap (Arc under 70 %, Amber above, Signal when hit), then a 30-day bar strip by kind. Fallback events appear as Amber marks on the strip.
- **Turn log:** compact rows: time, chat, outcome ("replied", "reacted 🤣", "stayed quiet", "held back: possible leak"), model, calls as small chips ("search", "final").

## Copy

- Plain verbs: "Wake Dom", "Send to sleep", "Leave group", "Approve", "Reject", "Save changes".
- Empty states give direction: "Nothing is waiting for you." / "No turns yet today. Dom replies when someone messages him."
- Errors say what happened and what to do: "Couldn't reach Telegram. Dom will retry; nothing was lost."
