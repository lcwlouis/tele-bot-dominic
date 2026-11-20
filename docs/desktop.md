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
handoff message. On the Desktop page, use the live view and tap Done when the
computer is ready for Dom to continue.

The handoff message names the site the browser was on and when Dom looked, for
example "browser was on login.example.org at 14:05" in the configured timezone.
If the sandbox marks no tab as active it says "an unknown site". Treat the host as
a hint: check the live view before you enter anything.
