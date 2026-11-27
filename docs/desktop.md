# Desktop network

Local mode uses an IPv4-only Docker bridge. Dom publishes the sandbox API on
loopback and mounts the browser profile outside the instance home. The profile
keeps browser logins between sessions.

Choose a subnet that does not overlap your LAN or other Docker networks. The
default bridge is `dom-desktop` with subnet `172.30.0.0/24`. Configure both under
`[desktop]` in `config.toml`.

On the Linux Docker host, use Docker's iptables firewall backend. Run the setup
with permission to manage Docker and the host firewall. Preview with
`dom desktop-setup <home>`, then pass `--yes` to apply the changes. The command
prints each rule it inserts and can be rerun.

```sh
sudo /path/to/dom desktop-setup /path/to/dom-home --yes
dom desktop-check <home> <lan-ip> --lan-port N --public-address 1.1.1.1
```

Replace `<home>` with your instance home and `<lan-ip>` and `N` with the IPv4
address and port of a LAN service known to answer from the host.
`desktop-check` runs Dom’s startup probe once against that service. The LAN
connection must time out, and the public control address must connect.
The `--public-address` flag overrides the IPv4 address in `probe_control` and
uses port `443`. The probe image (`probe_image`, default `alpine:3.22`) and
the AIO Sandbox image must already be on the host.

Setup builds a dedicated chain, `DOM-DESKTOP`, that holds every rule. `DOCKER-USER`
and `INPUT` each get one jump to it for the desktop subnet. The chain returns
established and same-bridge traffic to Docker's normal forwarding, accepts
established replies to the host, drops new connections to the host itself (host
input does not pass through `DOCKER-USER`) and drops new connections to private,
carrier-grade NAT and link-local ranges. Setup checks that the jump is rule 1 of
both parent chains and moves it back to rule 1 if another tool displaced it, so a
rerun after Docker restarts is always safe. Rules from an older Dom that sit
directly in `DOCKER-USER` or `INPUT` are redundant and can be deleted.

## Chromium and startup

Local mode passes the packaged `seccomp-chromium.json` profile to Docker.
Docker’s default profile blocks namespace operations Chromium needs to start
its sandbox. The packaged profile permits `unshare`, `setns`, `clone3` and
the user, PID and network namespace flags on `clone`. It keeps the default
deny action, and Dom keeps Docker’s `no-new-privileges`, process and memory
limits. [Profile provenance and rule changes](../dom/desktop/README.md)

Dom disables Jupyter for local containers. `run_python` writes a temporary
script and runs it with `python3` through the Bash API. Each call runs in a
fresh interpreter and returns stdout, stderr and the exit code.

Dom waits up to `[desktop] start_timeout_seconds` (default `180`) after the
container starts for the API to respond and then for Chromium’s tab list to
be available. A timeout reports `API not healthy` or `browser did not start`.

On a Raspberry Pi running Debian 13 (aarch64) with Docker’s iptables backend
and `ghcr.io/agent-infra/sandbox:latest`, the 2026-11-19 bring-up measured
about 22–67 seconds for the API with Jupyter disabled and about 4–5 minutes per
start with Jupyter enabled. An unconfined diagnostic run reached browser
readiness in about 59 seconds and measured about 8 seconds for a page load and
1.7 seconds for a screenshot. These timings vary by host and workload.

## Reapply at boot

On a systemd host, put a unit like this in
`/etc/systemd/system/dom-desktop-network.service`. Set the paths to the installed
Dom executable and instance home.

```ini
[Unit]
Description=Dom desktop network
Requires=docker.service
After=docker.service
PartOf=docker.service

[Service]
Type=oneshot
ExecStart=/path/to/dom desktop-setup /path/to/dom-home --yes
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable it with `sudo systemctl enable --now dom-desktop-network.service`.

## Startup firewall check

Dom proves isolation before **every** container start. Nothing is cached: a pass
for one start says nothing about the next. First it runs `docker network inspect`
and refuses to start unless the network is an IPv4-only bridge whose subnet equals
`[desktop] subnet`. Then it runs a throwaway `probe_image` container
(`--pull=never --rm`) on that network. The container uses busybox
`nc -z -w 3` against each target on its own:

- every `[desktop] lan_probes` entry,
- the network's gateway on the dashboard port (`[web] port`) and on port 22.

A target passes only if its single connect **times out**. A connect that succeeds
or is refused means the target is reachable, and Dom refuses to start, sets the
status to `error: LAN reachable from the sandbox — run dom desktop-setup` and sends
the owner one DM.

The probe also connects to `probe_control`, a public address that must answer. If
it does not, the probe container had no working networking and its timeouts prove
nothing, so the result is "could not verify" and Dom refuses to start (fail
closed).

`probe_control` is an IPv4 literal with a port. Each `lan_probes` entry is an IPv4
literal or an mDNS `<name>.local` host name with a port, for example
`lan_probes = ["192.0.2.1:443"]`; other DNS names are rejected when the config loads.
The probe container cannot resolve mDNS, so Dom resolves each `.local` name on the
Docker host before every probe and checks every IPv4 address it returns. A name that
does not resolve fails closed. The firewall rules block the private ranges whatever
the names resolve to. In local mode an empty `lan_probes` also refuses to start.

## Remote mode

Dom cannot inspect a remote host's firewall. Remote mode therefore refuses to
start the desktop unless you set `[desktop] remote_isolation_confirmed = true`,
which asserts that you checked the remote host cannot reach your LAN or the Dom
host.

Run the firewall unit on the Docker host before requesting a desktop task.
The firewall unit runs in the system manager, while `dom install-service` uses
the user manager. In local mode Dom checks the rules before each container start.
The firewall commands use `iptables -w`.

## Reading Dom's own code

The desktop worker knows the public repository URL (`[update] repo_url`, shown by
`self_inspect`) and may `git clone` it inside the sandbox to read code and history. It is
read-only: the worker never pushes, never commits to it and uses no credentials. The clone
needs the sandbox image to include `git`.

## Several containers

Dom can run more than one desktop container, for example one per project, so a
login or a long task in one does not block the others. `start_desktop_task` takes an
optional container name (lowercase letters, digits and hyphens, up to 24). Without
one the task runs on `default`, the container that existed before. Each container has
its own task slot, Docker name (`dom-desktop` for `default`, `dom-desktop-<name>`
otherwise), loopback port (the next free one from `[desktop] port`), browser profile
(`desktop-profile`, then `desktop-profile-<name>`, so logins last per container) and
idle timer. A named container is created on first use and forgotten once it is stopped
with no task; its profile stays on disk.

Hosts are the machines that run containers. With no `[[desktop.hosts]]` table, the
`[desktop] mode` settings are one host named `default`, so existing configs work
unchanged. To set limits or add machines, list hosts:

```toml
[desktop]
lan_probes = ["192.0.2.1:443"]

[[desktop.hosts]]
name = "pi"
mode = "local"          # Docker on this machine; at most one local host
max_containers = 1      # the Pi's default
min_free_memory_mb = 512  # kept free besides the container's own 1600 MB limit

[[desktop.hosts]]
name = "studio"
mode = "remote"         # one sandbox API Dom connects to; the container is named "studio"
url = "https://studio.example.org"
remote_isolation_confirmed = true
min_free_memory_mb = 16384
```

A remote host is one container because Dom cannot start containers there; add a host
entry per sandbox. The first listed host runs `default`.

**Resource checks.** Starts on a host are serialized. Before starting a container Dom
checks, and refuses with a message that says why instead of starting:

- the host already holds `max_containers` containers that are running, starting or
  have a task queued (the refusal names them);
- free memory is below the container's 1600 MB limit plus `min_free_memory_mb`. A local
  host reads `MemAvailable` from `/proc/meminfo`. A remote host reads it through the
  sandbox's shell (`/proc/meminfo` inside the sandbox), which shows the machine's memory
  on Linux hosts but the Docker VM's on Docker Desktop. If the figure cannot be read and
  a floor is set, Dom refuses. A remote floor of `0` (the default) is not checked.

A refusal is not a failure: the container stays stopped, the task is not queued and the
reason goes back to the model and the dashboard. Idle containers stop on their own
`[desktop] idle_minutes` timer, which frees their slot.

**Isolation.** Every local container joins the same `[desktop] network` bridge, so the
firewall rules above cover all of them, and Dom runs the LAN probe before **every**
container start, not once. Containers on that bridge can reach each other (not your
LAN); each has its own random API key.

**Dashboard.** The Tasks page (Work, then Tasks) lists every container with its host, state and
task, and each host's container limit, above one newest-first list of desktop tasks and Dom's own
work. Open a task for its steps, its result (marked as unchecked web text), files and screenshots, and to
cancel or delete it. `/app/work?task=<id>` opens one desktop task and `/app/work?own=<id>` one of
Dom's own. `/app/work?container=<name>` shows one container; the live viewer
and the Open Desktop button in a handoff message carry the name. Handoff and report messages, `/tasks` and `/status` name
a non-default container, and screenshot files are prefixed with it.

## Documents

Send a document to the owner DM with the caption `/desktop` (or `/desktop@<botname>`)
followed by a space or nothing. Other captions, such as `/desktopx`, are ordinary
chat. Staged documents are bound to the next desktop task when it is queued and
uploaded to a fresh `/home/gem/inbox-<task id>/` in the sandbox. If the upload
fails, those documents are dropped and the task report says so. After the task Dom
removes that directory and logs a warning if the removal does not exit with 0.

## The shared workspace

`<home>/workspace` (created owner-only) is one folder that Dom's `files` tool, the owner's desktop and the
dashboard's Files page (Work → Files) all use. A local desktop container mounts it read-write at `/workspace`, so
a file Dom writes is a file the desktop sees, and a file the desktop saves there (a download, a result) is one Dom can
read. The default container and every other local one get the mount; Dom's own self-directed `self-work`
container and a remote desktop host never do. The browser profile stays outside the home, as before.

- **What lands there.** A document sent in the owner DM, with any caption, is saved to `workspace/inbox/` under a plain
  name (a second `report.pdf` becomes `report-2.pdf`), at most 20 MB, downloaded in the background while Dom's reply
  waits for it like any other media. Dom is told the file's name, type and path in the message's `<media>` tag, and
  reads it with `files`. A `/desktop` document is staged for the task as before and also saved there, from the same
  download. The desktop's own task prompt says `/workspace` is shared.
- **File tools.** In the owner DM only, `files` has `list`, `read`, `write`, `move` and `delete`. `read` returns
  text, Markdown, CSV, JSON and similar text files, and PDF text (extracted with
  [`pypdf`](https://pypi.org/project/pypdf/), a small pure-Python library added for this: nothing else in Dom reads
  PDFs), twelve thousand characters at a time with the offset of the next part. What it returns is fenced as untrusted data.
  Writes are at most a million characters, a workspace holds at most 500 MB, and `private/`, `.env`, `state.db` and the
  rest of the home are not under the folder, so they cannot be named.
- **Links and tricks.** The desktop runs untrusted web content and can leave anything in `/workspace`, so every path
  is walked one directory at a time without following links (`O_NOFOLLOW` against the directory just opened), and only
  regular files and directories are touched. `..`, absolute and hidden paths are refused; links, pipes and devices are
  neither listed nor opened.
- **Sharing with chats.** `[workspace] share_with_chats` (off by default; Settings → What Dom may do) lets groups and
  friends' DMs list and read `workspace/shared/`, and nothing else, never write. A document sent in such a chat
  then lands in `workspace/shared/inbox/<chat id>/`, readable only by that chat (and the owner). Off, documents in
  groups and friends' DMs are not downloaded.
- **Leaving the sandbox.** Sending a file from the workspace to a website still asks the `upload` permission
  (the desktop may now upload from `/workspace`), and anything the desktop sends to Telegram still asks `download`.
  The dashboard's own download and upload are behind its login.
- **Backups.** `dom backup` includes the workspace (without links or pipes the desktop may have left in it).
- **File ownership.** The container writes as its own user. Where that user is not the one running Dom, files the
  desktop saves may not be readable by Dom (Linux hosts normally match at user 1000).

## Sandbox responses

Dom asks the sandbox for `Accept-Encoding: identity` and refuses any response
that carries another `Content-Encoding` before reading its body, so the size
limits count the bytes on the wire and a compressed bomb cannot inflate past them.

## Spend

Worker model calls count toward `daily_spend_cap_usd`, `monthly_spend_cap_usd` and the
`desktop` limit in `[area_spend_caps_usd]`. When any of them is reached, a
running task stops with "paused by a spend cap". `[desktop] task_cost_cap_usd`
(default `0.5`, `0` turns it off) stops a single task once its calls cost that much.

The model call that writes the report to you is one more call of the task. It is
skipped, and the plain report is sent instead, when the daily cap or the task
ceiling has been reached. A provider that reports no cost for a call is priced
from its tokens at `[desktop] fallback_usd_per_1k_tokens` (default `0.002`) for the
task ceiling only; the daily cap counts reported cost. Set the price above what
your model really costs, so the ceiling errs towards stopping. A call to a model
endpoint on your own network (a private or loopback address, or a `.local`, `.lan`
or `.home.arpa` name, such as a home-lab vLLM) costs nothing, so a task on a local
model is bounded by its step budget (`max_steps`), not by the dollar ceiling.

## Dashboard handoff

Set `[web].public_url` in `config.toml` to the HTTPS base URL of the
authenticated dashboard. Dom uses it for the Open Desktop button in the owner's
handoff message (it adds `?container=<name>` for a container other than `default`).
That link opens the full-page live viewer (`/desktop/vnc/index.html`) directly; its
back link returns to the Tasks page, whose Open live viewer button opens it again. The viewer shows the live browser host, handoff countdown,
Done button, local zoom and pan, one-finger scroll mode, a remote key bar,
explicit Copy/Paste buttons, fullscreen, and Phone mode (`800x600` via the
sandbox API, automatically restored to `1280x1024` server-side at Done, cancel,
timeout, or restart). The remote clipboard is never pushed automatically to your
device and is cleared server-side when the handoff ends. When the remote browser
focuses a password field, the viewer switches its Type field into password mode
automatically; typed passwords are sent only as VNC keystrokes, never through
the sandbox API.

The handoff message names the site the browser was on and when Dom looked, for
example "browser was on login.example.org at 14:05" in the configured timezone.
If the sandbox marks no tab as active it says "an unknown site". Treat the host as
a hint: check the live view before you enter anything.

## Desktop permissions and saved logins

Desktop actions are governed by per-action policies (`Ask`, `Allow`, or `Never`)
and per-site rules under **System → Access** (`/app/system/access`):

- **Visit unfamiliar sites** (default `Allow`): gates `browse`, and any page a click, key, form, redirect or late navigation reaches: each task remembers the sites it was cleared for, and a page tool that finds the browser on any other site gets the same check (`Ask` raises a card, `Never` and a rejection close the page). A permission card's wait does not count against the task's time.
- **Fill saved logins** (default `Ask`): every new site asks before filling, with **Allow once**, **Allow always**, and **Reject** buttons in the owner DM and on Waiting for you.
- **Download files from desktop** (default `Ask`): gates exporting non-screenshot files out of the sandbox.
- **Upload files** (default `Ask`): gates uploading sandbox files to a web page (`Never` also discards staged `/desktop` inbox attachments).
- **Background work** (default `Allow`): gates Dom's self-directed Own work tasks (`Ask` pauses before each task launches; `Never` turns planning and execution off).

Saved logins are encrypted at rest in SQLite with AES-256-GCM using `DOM_MASTER_KEY`
in `DOM_HOME/.env` (mode `0600`, auto-generated on first write when no encrypted rows
exist, rotatable from **System → Access** while Dom runs or via `dom rotate-key <home>`
while Dom is stopped — the CLI refuses if `ready.json` points to a running Dom process; stop Dom, or delete
a stale `ready.json` if Dom is not running).
When approved, Dom re-reads the active tab's URL from the sandbox browser's tab list
immediately before typing the username and again before the password, and requires it to
be `https://` on the saved site and to name the same host as the page's own report. The tab
list is the trust anchor: a page's JavaScript can forge anything `/v1/browser/page/evaluate`
returns. As a second layer, Dom verifies via `/v1/browser/page/evaluate` at the same two
points that `document.hasFocus()` is
true, the top-level `window.location.href` is `https://` (default port 443) on the saved
login's site, the active element is the expected top-level input, and any enclosing form's
`action` stays on the matched site. Keystrokes go strictly as RFB `KeyEvent` frames over
`/websockify` with `websockets` frame logging silenced. Dom checks after typing that focus
did not leave the same password input (aborting, clearing, and alerting the owner DM if
stolen), always submits with `Enter`, clears any leftover password value over VNC if the
field remains populated after submit, makes a best-effort attempt to erase the typed
password over VNC if a check fails or the task is cancelled after typing and before the
submit (it cannot if the sandbox is unreachable), and registers the username and password in a per-run
redaction set applied to all tool results, `read_page`/`look` text, `finish`/wrap-up
reports, turn logs, traces, and usage rows for the rest of the task. What is guaranteed:
Dom never places the plaintext password into model prompts, tool arguments/results, traced
sandbox HTTP requests, turn logs, traces, usage rows, or Python logs, and self-directed
tasks never see or use saved logins. Residual risk: a malicious page script can lie about focus and field state to the
second-layer check (the tab list still pins the origin), and a compromised or malicious page on the
matched origin itself (or a compromised sandbox container/shell tool during an owner task)
still sees keystrokes typed into Chromium and could transform/render them visually or steal
session cookies after login.

See [the credentials and permissions security spec](superpowers/specs/2026-11-27-credentials-and-permissions.md)
for the full threat model, key-rotation protocol, and E101 secure secret request flow.

## Self-directed work

With `[self_work] enabled = true` (the default) and a local desktop host, Dom may
plan up to three small tasks of his own during his nightly daydream, in the hour
before he sleeps: research a topic by reading up to four named public sites in the
browser, keeping notes in files if he likes. Each task records its goal, why, the
tools it will use, the sites it will open, and a time and step budget. Every task can read the pages it opens
(`browse`, `read_page`, `list_elements`, `scroll`, `look` and `save_screenshot`), and gets at least 10 steps, or
`max_steps` if that is lower. Its report may only say what the pages said, not facts from the model's memory.
Set `enabled = false` to turn it off; see
[Configuration](configuration.md#self-directed-work) for the delay and budget limits.

**You see the plan first.** Dom sends a short notice to your DM ("Tomorrow's own work: 3
things", one bullet per task, and a Review on dashboard button when `[web].public_url`
is set). The reasons, tools, sites and budgets are in the dashboard's task list (Work,
then Tasks; and the home view, while anything is planned or running), each task with
Cancel in its details; `/ownwork` lists and cancels them in Telegram too. A
task starts no sooner than `delay_hours` after the announcement, and only while Dom
is awake and idle: your DM is not asleep, no chat is online or mid-reply, and no
desktop task is running. A task that has not started within a day expires, and the
next night's plan replaces any leftovers. Results, with any screenshots, appear in
the task list; your DM gets one short notice ("Done: <task>" or "Stopped: <task>", the first sentence of the result tagged as an unchecked web summary, and a link). Finished tasks (and their
saved screenshots) can be deleted from the task list or with `/ownwork`
after moving them into the deletion archive; planned or running tasks must be
cancelled first. A task that runs out of steps
gets one last call to report what it found; with a report it ends as done, marked
partial, and keeps the screenshots it names. Self-directed tasks never message
anyone else.

**What is enforced in code, not just asked for in the prompt:**

- Tasks run only in a local desktop container named `self-work`, never on the Dom
  host and never on a remote host. Its browser profile is deleted before every task,
  so there are no logins to use, and the worker has no `handoff` tool, so it cannot
  ask for one. The default container's profile (your logins) is never touched. If you
  run your own desktop task in the `self-work` container, any logins you make there
  are wiped before Dom's next self-directed task; use another container for those.
- Containers share the desktop bridge, so a self-directed task starts only while
  every other local container is stopped, and no other container may start while it
  runs (cancel it in the task list to free the desktop).
- The worker gets only the desktop tools the plan named, from the browser tools,
  `read_file`, `write_file` and `finish`. There is no shell and no Python: those run
  as the same user, with the same files and screen as the browser, so they could
  drive it past every rule below. Plugins, the T3 bridge, skills, skill proposals and
  core-change briefs are not available either.
- A task names at most four sites, shown in full in the DM and on the dashboard. They
  are public hostnames: no IP addresses, LAN names or public suffixes shared by
  unrelated owners (`co.uk`, `me.uk`, `eu.org`, `github.io`, `blogspot.*` and the like). The browser may open
  only those sites and their subdomains, on ports 80 and 443, and only through URLs
  that browsers and Dom read the same way (no `\`, `@`, `%` or whitespace in the
  address part). A page that leaves the plan, by a click or a redirect, is closed; if
  it cannot be closed, the task stops.
- The only form is a search. `fill` takes up to 120 characters through one simple
  selector (no lists, pseudo-classes or combinators) that reads as a search field and
  not a login field, and only on a page whose element list shows a search box.
  `press` takes scrolling keys and Escape, and Enter only right after a search was
  typed. `click` takes only the index of an element that the page's element list,
  read just before the click, shows as a link to a web address; never a selector,
  a button or a `javascript:` link. A link click may still run the page's own
  JavaScript; with an empty profile and no logins that is accepted as low impact.
- `read_file` and `write_file` work only under `/tmp` and `/home/gem/outbox`, after
  resolving `..` and symlinks. Only a path that already reads as under those folders
  is resolved, quoted, by the sandbox.
- The step and time budgets stop the task when exceeded.
- The plan runs only once its announcement reached your DM. If the message cannot be
  sent, the tasks are refused. If another container starts just before a task does,
  the task goes back to waiting (and still expires after a day). The summary Dom
  sends you when a task finishes is labelled as a summary of web content and has its
  links and user mentions removed; the full result is on the dashboard.

### Network guard

Before every self-directed task, right after the fresh container starts and before
any page loads, Dom checks the network **from inside that container** through its
own shell. The check must show that:

- the public `probe_control` address connects,
- every `lan_probes` service, the Docker host's own LAN address and the container's
  gateway (on port 22 and the dashboard port) time out,
- no sampled private address (the first address of each private, shared and
  link-local range) answers.

A timeout counts as proof only for a target that answers from the Docker host itself:
a switched-off `lan_probes` device times out whether or not the firewall is there. Every
host address and at least one `lan_probes` service must prove the block, so probe a
service that is always on, such as the router. The startup firewall check for every
local container applies the same rule to `lan_probes`.

If any of this fails, or the check cannot run, the task is refused, nothing else
runs, and Dom tells you why in your DM. The result of the last check is stored and
`dom doctor` reports it under "self-directed work". Owner-requested desktop tasks are
unchanged.

The blocking rules are the same `DOM-DESKTOP` firewall the local desktop already
needs. Dom runs unprivileged and cannot install it; install it once as root and
reapply it at boot (see [Reapply at boot](#reapply-at-boot)):

```sh
sudo /path/to/dom desktop-setup /path/to/dom-home --yes
dom doctor /path/to/dom-home
```

The planning call sends the public soul, the last few public journal entries and
the goals of earlier self-directed tasks to the chat model. Nothing from `private/`
is used, and the worker's brief holds only the plan.

### Studio on the desktop host

If this host also runs the studio, include the studio port in its isolation sign-off. From a sandbox, verify that the studio is unreachable through the host LAN address, container host alias, VM gateway and local DNS name. Test from an unlisted LAN peer as well. Set `remote_isolation_confirmed = true` only after all the isolation checks pass. Match the studio's desktop reservation to the host's container limit, memory per container and minimum free memory.
