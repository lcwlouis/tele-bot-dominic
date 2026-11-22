# Writing plugins

A plugin is a small folder of Python code that Dom runs in a sandbox. It can offer
three kinds of surface:

- **Tools** that Dom calls during a chat turn.
- **Slash commands** that people type in Telegram, such as `/weather Berlin`.
- **Dashboard panels** that show data on the Plugins page and the dashboard home.

Dom drafts plugins in the owner DM with `draft_plugin`, or the owner places one
under `<home>/plugins/<name>/`. Staging runs an isolated trial. Live calls need
the owner to review the exact code on the dashboard and approve that version.
Any change to the files, including the commands, panels, tools, network access or storage declared in
`plugin.toml`, makes it a new version that needs a fresh review and approval.

## Files

A plugin holds `plugin.toml`, `main.py` and up to eight more Python files, 64 KB
in total. Code uses the Python standard library and the `_dom` helper only.
This is the starter's `plugin.toml`:

```toml
name = "units"                 # [a-z][a-z0-9_]{1,31}, same as the folder
description = "Convert units"  # up to 200 characters
version = "1"
# Optional, and they must come before any [[...]] table:
# network = false              # true allows outgoing internet connections
# storage = false              # true lets an offline plugin keep one JSON blob
# timeout_seconds = 10         # 1 to 30, wall time and CPU time
# memory_mb = 64               # 16 to 256

[[tools]]
name = "length"
description = "Convert a length between units"
parameters = {type = "object", properties = {value = {type = "number"}, unit = {type = "string"}}}
scopes = ["owner_dm"]          # where Dom may call it: owner_dm, friend_dm, group

[[commands]]
name = "km"                    # typed as /km; [a-z][a-z0-9_]{0,31}
description = "Convert miles to kilometres"  # shown in the owner's / menu, up to 100 characters
scopes = ["owner_dm", "group"] # default ["owner_dm"]

[[panels]]
id = "summary"                 # [a-z][a-z0-9_]{0,31}
title = "Unit conversions"     # up to 80 characters
```

`main.py` decorates one function per declared name with `tool`, `command` or
`panel` from `_dom` and ends with `run()`. The complete starter is in
[`examples/plugins/units/`](examples/plugins/units/main.py); copy the folder to
`<home>/plugins/units/` to try it.

A plugin needs at least one tool, command or panel. Function names match the
declared names. A plugin can declare up to ten commands and five panels.

## Plugin storage

With `storage = true`, an offline plugin can import `load_storage` and
`save_storage` from `_dom`. `load_storage()` returns the current JSON value or
`None` on the first call. Pass the full replacement to `save_storage(value)`.
Dom limits the JSON to 64 KiB of UTF-8 and 100 levels of objects or arrays.
The replacement is saved after the sandbox finishes and returns a valid response.
The plugin's tools and commands must be scoped to `owner_dm`, and its panels run
on the owner dashboard. These calls share the blob across approved versions.
The database stays outside the sandbox.

## Trial runs

Define a zero-argument function decorated with `self_test` from `_dom` to run
checks on staging. Return a short string or `None` on success and raise an
exception on failure. If there is no self-test, Dom tries the first declared
panel, command or tool in that order. A command receives empty text and its
first scope. A tool receives an empty argument object.

The trial uses the live sandbox limits with networking off and empty, disposable
storage. The owner's approval card and the Plugins page show the result and
elapsed time. Approval remains available while a trial is pending or after it
fails. Refresh the Plugins page to see the latest result.

Dom uses the `[models.drafting]` endpoint for plugin and skill drafts. The
starter uses Claude Haiku 5.5 on OpenRouter with low thinking. When the drafting
key is missing, Dom uses the chat model. See
[Configuration](configuration.md#models) for endpoint and thinking settings.

## Commands

- The function receives `text`, everything after `/name` with outer spaces
  removed (at most 1024 characters), and `scope`: `owner_dm`, `friend_dm` or
  `group`. Nothing else is sent: no chat ID, no sender, no names, no history.
- It returns plain text. Dom drops control and invisible characters, cuts the
  reply to 2000 characters and sends it without Markdown, HTML or link
  previews. Neither the command nor the reply is added to chat history, so a
  plugin's output never becomes part of Dom's prompt.
- A command runs only in a chat whose scope it declares, in an approved chat,
  for a human sender. Messages from bots and channels never trigger one.
- While Dom is asleep, a command in a group or friend DM does not run: it is an
  ordinary message he sees when he wakes. In the owner's DM it runs, and then Dom
  adds one line in his own voice about being woken (grumpy if it could have
  waited). That line is composed from the command's name and arguments only,
  never the plugin's output, and he goes back to sleep.
- Core commands win. Names such as `status`, `claim`, `desktop`, `update`,
  `start` and `help` are reserved. Two plugins cannot declare the same command:
  a draft that would collide is refused, and if two approved plugins on disk
  still collide, neither offers it.
- Outside the owner DM a chat can run one plugin command every 10 seconds.
  Extra commands in that window are ignored without a reply.
- Network plugins can only declare `owner_dm` commands, like their tools.
- Approved `owner_dm` commands appear in the owner's `/` menu after the core
  commands. Dom shows no menu in other chats, so commands for friends and groups
  are typed by name.

## Panels

- The function takes no arguments and returns an object with only these keys:

  | Key | Shape | Limits |
  | --- | --- | --- |
  | `title` | string | 120 characters, one line |
  | `paragraphs` | list of strings | 10 paragraphs of 1000 characters |
  | `items` | list of `[key, value]` | 30 pairs; key 80, value 500 characters |
  | `table` | `{"columns": [...], "rows": [[...], ...]}` | 8 columns, 50 rows, 200 characters a cell |

  Values and cells may be strings or finite numbers. Anything else, including
  unknown keys, booleans, nested objects and invisible characters, is refused and
  the panel shows an error. Dom never tries to clean bad data.
- Dom renders the data with its own Jinja template, with autoescaping on. A
  plugin cannot send HTML, CSS or JavaScript to the dashboard: markup in a value
  is shown as text.
- Panels load after the page, one request per panel, so a slow plugin never
  delays the dashboard. Each load and each Refresh runs the plugin once.

## Limits shared by every surface

Tools, commands and panels run the same way:

- In a Bubblewrap sandbox with an empty environment, no access to Dom's home,
  a read-only `/plugin`, an 8 MB `/tmp`, no new processes and restricted system
  calls. Network access only if the manifest asks for it and the owner approved.
- Live calls run the exact approved bytes. Dom re-reads and re-hashes the files
  before every live call. Trials run the exact staged bytes.
- One plugin process at a time. A call while another plugin runs gets "busy"
  instead of waiting.
- The manifest's `timeout_seconds` and `memory_mb`, 16 KB of output, 1 MB files
  and 32 open files.

## Security notes

These are the risks considered for commands and panels, and what Dom does.

- **Injection through command text.** The text is user input. It reaches the
  plugin as a JSON string inside one JSON line, so it cannot change the request.
  It is screened for instance credentials and verbatim lines of the owner's
  private notes before it is sent. What the plugin does with it is the plugin's
  code, which the owner reviewed.
- **Injection through replies.** Replies go out as plain text with no parse mode
  and no link previews, after control and bidirectional-override characters are
  removed. They are not stored in chat history, so plugin output cannot steer
  Dom's later turns.
- **Injection through panel data.** Panels are data only, checked against a
  strict schema and escaped by the template. Plugins have no way to add markup,
  styles, scripts, links or images to the dashboard.
- **Cross-site requests.** Running a panel is a POST, so it needs the same
  same-origin htmx request as every other dashboard action. A link or image on
  another site cannot make Dom run a plugin.
- **Context leaks.** Commands get the text and the scope only. Panels get
  nothing. Neither sees chat IDs, people, notes, memory, transcripts or secrets.
  A network plugin could send what it receives elsewhere, which is why its
  commands are limited to the owner DM.
- **Resource use.** Every call has the sandbox limits above. Outside the owner
  DM a chat can trigger at most one command every 10 seconds, so a busy group
  cannot keep the sandbox or Telegram's rate limits occupied.
- **Shadowing.** Core commands are handled before plugin commands, reserved
  names are refused in the manifest, and colliding plugin commands are dropped.
- **Unnoticed widening.** The declared surfaces are part of the hashed
  `plugin.toml`. Adding a command, widening its scopes or adding a panel is a
  new version that needs a fresh review, and the review page says when the
  declared surfaces changed since the approved version.

## Retiring and deleting plugins

- **Retire (disable).** From the Plugins page on the dashboard or `/plugins` in
  the owner DM, retiring a plugin marks it disabled, keeps its files on disk,
  removes its Telegram commands from the owner's `/` menu, hides its dashboard
  panels, and stops Dom from offering its tools.
- **Delete.** Deleting a plugin (with confirmation from the Plugins page or
  `/plugins`) refuses if the plugin is currently mid-call or changed since
  confirmation, moves the plugin folder and writes its database rows and stored
  state into `<backups>/deleted/`, removes the database row and stored state,
  settles any pending proposal for it, refreshes the Telegram command menu
  (retrying on the next background tick if Telegram fails), and records an
  owner event.
