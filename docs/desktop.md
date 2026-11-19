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
PartOf=docker.service

[Service]
Type=oneshot
ExecStart=/path/to/dom desktop-setup /path/to/dom-home
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable it with `sudo systemctl enable --now dom-desktop-network.service`.
