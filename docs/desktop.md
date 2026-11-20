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

`lan_probes` and `probe_control` are IPv4 literals with ports, for example
`lan_probes = ["192.0.2.1:443"]`; DNS names are rejected when the config loads. In
local mode an empty `lan_probes` also refuses to start.

## Remote mode

Dom cannot inspect a remote host's firewall. Remote mode therefore refuses to
start the desktop unless you set `[desktop] remote_isolation_confirmed = true`,
which asserts that you checked the remote host cannot reach your LAN or the Dom
host.

Run the firewall unit on the Docker host before requesting a desktop task.
The firewall unit runs in the system manager, while `dom install-service` uses
the user manager. In local mode Dom checks the rules before each container start.
The firewall commands use `iptables -w`.

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

**Dashboard.** The Desktop page lists every container with its host, state and task.
`/desktop?container=<name>` shows one; the live viewer and the Open Desktop button in a
handoff message carry the name. Handoff and report messages, `/tasks` and `/status` name
a non-default container, and screenshot files are prefixed with it.

## Documents

Send a document to the owner DM with the caption `/desktop` (or `/desktop@<botname>`)
followed by a space or nothing. Other captions, such as `/desktopx`, are ordinary
chat. Staged documents are bound to the next desktop task when it is queued and
uploaded to a fresh `/home/gem/inbox-<task id>/` in the sandbox. If the upload
fails, those documents are dropped and the task report says so. After the task Dom
removes that directory and logs a warning if the removal does not exit with 0.

## Sandbox responses

Dom asks the sandbox for `Accept-Encoding: identity` and refuses any response
that carries another `Content-Encoding` before reading its body, so the size
limits count the bytes on the wire and a compressed bomb cannot inflate past them.

## Spend

Worker model calls count toward `daily_spend_cap_usd`. When the cap is reached, a
running task stops with "paused by today's spend cap". `[desktop] task_cost_cap_usd`
(default `0.5`, `0` turns it off) stops a single task once its calls cost that much.

The model call that writes the report to you is one more call of the task. It is
skipped, and the plain report is sent instead, when the daily cap or the task
ceiling has been reached. A provider that reports no cost for a call is priced
from its tokens at `[desktop] fallback_usd_per_1k_tokens` (default `0.002`) for the
task ceiling only; the daily cap counts reported cost. Set the price above what
your model really costs, so the ceiling errs towards stopping.

## Dashboard handoff

Set `[web].public_url` in `config.toml` to the HTTPS base URL of the
authenticated dashboard. Dom uses it for the Open Desktop button in the owner's
handoff message (it adds `?container=<name>` for a container other than `default`). On the Desktop page, use the live view and tap Done when the
computer is ready for Dom to continue.

The handoff message names the site the browser was on and when Dom looked, for
example "browser was on login.example.org at 14:05" in the configured timezone.
If the sandbox marks no tab as active it says "an unknown site". Treat the host as
a hint: check the live view before you enter anything.
