# Desktop network

Local mode uses an IPv4-only Docker bridge. Dom publishes the sandbox API on
loopback and mounts the browser profile outside the instance home. The profile
keeps browser logins between sessions.

Choose a subnet that does not overlap your LAN or other Docker networks. The
default bridge is `dom-desktop` with subnet `172.30.0.0/24`. Configure both under
`[desktop]` in `config.toml`.

On the Linux Docker host, run the setup with permission to manage Docker and the
host firewall. The command prints each rule it inserts and can be rerun.

```sh
sudo /path/to/dom desktop-setup /path/to/dom-home
/path/to/dom desktop-check /path/to/dom-home 192.0.2.1 --lan-port 443
```

Replace the example address and port with a LAN service known to answer from the
host. The probe runs in a throwaway container and should time out for the LAN
service and connect to a public address on TCP 443. Use `--public-address` to
choose a different public endpoint. The AIO Sandbox image must already be on the
host.

The rules return established traffic and same-bridge traffic to Docker's normal
forwarding policy, then drop new connections to private, carrier-grade NAT and
link-local ranges. Host input is filtered separately because connections to the
Docker host do not pass through `DOCKER-USER`.

## Reapply at boot

On a systemd host, put a unit like this in
`/etc/systemd/system/dom-desktop-network.service`. Set the paths to the installed
Dom executable and instance home.

```ini
[Unit]
Description=Dom desktop network
Requires=docker.service
After=docker.service
Before=dom.service
PartOf=docker.service

[Service]
Type=oneshot
ExecStart=/path/to/dom desktop-setup /path/to/dom-home
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable it with `sudo systemctl enable --now dom-desktop-network.service`.

## Startup firewall check

Before Dom starts the sandbox for the first time in a process, and again after any
failed start, it runs a throwaway container on the desktop network. The container
tries TCP connects to every `[desktop] lan_probes` entry and to the network's
gateway on the dashboard port. Only a dropped (timed out) connection passes. If
any target answers or refuses, Dom refuses to start the sandbox, sets the status to
`error: LAN reachable from the sandbox — run dom desktop-setup` and sends the
owner one DM.

Set `lan_probes` to `host:port` addresses of LAN services that are up, for
example `lan_probes = ["192.0.2.1:443"]`. In local mode an empty list also
refuses to start.

Start the firewall unit before Dom so the rules exist when it starts. Add this to
the Dom service:

```ini
[Unit]
After=dom-desktop-network.service
Requires=dom-desktop-network.service
```

and keep `Before=dom.service` in the `[Unit]` section of
`dom-desktop-network.service`. The rules use the iptables backend with `-w`; the
nftables backend is not supported.

## Dashboard handoff

Set `[web].public_url` in `config.toml` to the HTTPS base URL of the
authenticated dashboard. Dom uses it for the Open Desktop button in the owner's
handoff message. On the Desktop page, use the live view and tap Done when the
computer is ready for Dom to continue.
