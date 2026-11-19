# Dashboard visual pass — fixes

Reviewed 2026-11-19 against `2026-11-19-dashboard-design.md`, on the live templates rendered with made-up data, at 1360 px and 390 px. The structure holds up; these are the concrete fixes.

1. **Phone navigation takes two rows.** Nine links wrap under the name. Make the rail a single horizontally scrollable row on narrow screens (`overflow-x: auto`, no wrap, a faded right edge as the scroll cue), with the current section's link scrolled into view.
2. **Empty space under the dial.** On wide screens the dial column ends well above the proposals. Add a small "Today" list under the legend: messages received, replies sent, spend today, and the next reminder (or "No reminders"). Plain rows, Fog labels, Ink values, tabular figures. No new data sources beyond what the usage and reminders pages already read.
3. **Proposal labels use a middle dot** ("Soul edit · reflection"), which the design rules out. Write them as sentences: "Soul edit, suggested in reflection", "Memory imported from ChatGPT", "Skill proposed after task #12", "Plugin draft".
4. **Preset needs two controls.** The preset select plus a separate "Set preset" button doubles every chat row's actions. Submit on change (htmx `hx-trigger="change"`) and drop the button. Use a visually hidden label that says the preset changes on selection.
5. **Zero unread counts shout.** "0" renders at the same weight as "40". Show zero in Fog, as the mock did.
6. **Filters need a button press.** The proposals "Filter" and turn-log "Filter" buttons can go the same way as item 4: apply on change, keep a `<noscript>` button.
7. **Online sessions are hard to see on the dial** when they're short. Give each session arc a minimum drawn length of 15 minutes, so a 3-minute wake still reads as a mark.
8. **Focus states.** Check every interactive element (selects, the disclosure summaries "Traits", "Sticker sets", "Today by model", the files list) shows the 2 px Arc outline with offset. Summaries currently rely on the browser default.
9. **Empty pages.** Plugins and Briefs say only "No plugins are installed." / "No core-change briefs yet." Add one line of direction each: "Ask Dom in your DM to write a plugin for something he does often. You'll review the code here before it runs." and "When Dom thinks his own code should change, he writes a brief here for a coding agent."

Out of scope: colours, type, the dial's construction and the layout grid stay as they are.
