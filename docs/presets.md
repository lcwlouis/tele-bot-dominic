# Presets and Dom's daily vibe

Presets are files in the instance's `presets/` folder (`name.md`, with optional `title`, `description` and `traits` front matter and a short overlay). A chat uses, in order: its own preset, then the "Everywhere" setting, then the soul alone. Set them with `/preset` in the owner DM or on the dashboard Chats panel.

"Everywhere" can be a fixed preset, `None`, or **Auto (Dom picks daily)**:

- In the nightly reflection Dom chooses tomorrow's preset from his mood and recent journal, with a short reason. It takes effect from his wake-up time. The `assistant` preset is never picked: assistant mode is for tasks, not a vibe.
- If the model call fails or the answer is not one of the installed presets, he gets a weighted random pick instead: `classic` counts 6, `fboy` counts 1 and any other preset counts 3.
- Once per pick (so once a day) Dom may switch with the `switch_vibe` tool when a conversation clearly calls for it. The tool is offered only under Auto, does nothing in a chat that has its own preset, and each switch is logged as an owner event with its reason.
- A chat's own preset always wins over Auto. Choosing a fixed preset Everywhere locks it and Dom stops picking.
- The Chats panel shows the current vibe, Dom's reason, and whether Everywhere is Auto or locked. `self_inspect` and the prompt tell Dom which preset is his own pick.

## Turning Auto on

Send `/preset`, tap **Everywhere**, then **Auto (Dom picks daily)**. Or choose **Auto (Dom picks daily)** in the Everywhere selector on the dashboard Chats panel. Turning it on picks a first preset at random if there is none from the last day.

To stop pinning a chat to a preset, choose "Same as everywhere" for it on the dashboard, or **None** in its `/preset` list.

Dom's picks are kept in the `kv` table under `preset:auto`; nothing else changes on disk.
