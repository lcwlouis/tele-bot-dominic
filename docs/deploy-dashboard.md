# Serve the dashboard through an existing Cloudflare tunnel

The dashboard runs inside `dom run` and is off by default. Use the existing
self-hosted Cloudflare Access application with an allow policy for the owner.
The tunnel validates Access before forwarding the request. Dom independently
validates the signed assertion against the same team and application audience.
[Cloudflare Access JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/).

## Docker bridge

Use this layout when Dom runs on the host and `cloudflared` runs in Docker on
an existing private bridge. The example public hostname is
`dashboard.example.org` and the tunnel is `<tunnel>`. Keep the existing ingress
service `http://<forwarder>:8080` and its Access validation.

Find the IPv4 gateway of the bridge used by `cloudflared`.

```sh
docker network inspect '<tunnel-network>' --format '{{json .IPAM.Config}}'
```

Set these keys in `DOM_HOME/config.toml`. Replace `<bridge-gateway>` with that
gateway address, `<team>` with the bare Cloudflare Access team name, and
`<aud>` with the dashboard application's audience tag.

```toml
[web]
enabled = true
bind = "<bridge-gateway>"
port = 8080
access_team = "<team>"
access_aud = "<aud>"
dev_token_env = ""
```

Optionally add `access_emails = ["<owner-email>"]` to also require the
verified assertion's `email` claim to be on that list (compared
case-insensitively). Without it, Dom accepts anyone the Access policy admits,
which is right while the policy allows only the owner. Use the list as a
second lock if the policy is ever widened.

Restart Dom with its existing service or `dom run '<DOM_HOME>'`.
The listening address is the bridge gateway. Keep TCP port 8080 reachable
from the dashboard bridge and block it at the LAN interface if the host
routes LAN traffic to the bridge.

Build the forwarder with Alpine and socat, then run it on the same network
as `cloudflared`. Use the same gateway value as `web.bind`.

```sh
docker build -t dom-dashboard-forwarder - <<'DOCKERFILE'
FROM alpine:3
RUN apk add --no-cache socat
ENTRYPOINT ["socat"]
DOCKERFILE

docker run -d --name '<forwarder>' \
  --network '<tunnel-network>' \
  --restart unless-stopped --memory 16m \
  dom-dashboard-forwarder TCP-LISTEN:8080,reuseaddr,fork 'TCP:<bridge-gateway>:8080'
```

The container listens on the Docker network with no published host port.
The ingress can continue to use the same service name. Keep the original
public `Host` and Cloudflare's `X-Forwarded-Proto` through the proxy so
the browser origin matches on dashboard actions.
[Cloudflare tunnel origin parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/),
[Cloudflare request headers](https://developers.cloudflare.com/fundamentals/reference/http-headers/#x-forwarded-proto).

## Loopback alternative

When `cloudflared` and Dom share the host's network namespace, set
`bind = "127.0.0.1"` and point the existing Access-protected ingress at
`http://127.0.0.1:8080`. The loopback address in a bridge-mode container
belongs to that container, so use the bridge setup in that case.

## Check the deployment

- From the origin host, request the configured address and port without an
  assertion. Dom should respond with HTTP 403.
- Open the public hostname in a private browser window and confirm that
  the Access sign-in appears. Sign in as the owner, open the dashboard, and
  check a same-origin action such as saving an edit to a memory file.
- In the bridge setup, check from a LAN device that the host's dashboard
  listener is unreachable.

## Google's OAuth callback

Connecting Google (see [Google Calendar and Gmail](google.md)) sends the browser back to
`<web.public_url>/api/integrations/google/callback`. That is a top-level navigation, so the browser sends its Access
session cookie and the callback authenticates like any dashboard request: do not add an Access bypass for it. Allow
that exact URI in the Google OAuth client. On a bearer-token dashboard a browser cannot send the token on that redirect, so
this one `GET` path skips the token check and is authenticated by its single-use `state` matching the cookie the
authenticated connect request set; every other path still needs the token.

## Local testing with a bearer token

For a local check without Access, set `enabled = true`, use
`bind = "127.0.0.1"` and `port = 8080`, leave `access_team` and
`access_aud` empty, and set `dev_token_env = "DOM_DASHBOARD_TOKEN"`. Put a
random value of at least 32 characters under `DOM_DASHBOARD_TOKEN` in the
environment or `DOM_HOME/.env`. Dom refuses to start if the token is shorter,
if `bind` is not a loopback address, or if `access_team` or `access_aud` is
also set. Each local request supplies that value as an
`Authorization: Bearer <token>` header. Every write is a JSON `POST` to an `/api`
route (or the desktop viewer's two JSON routes), and needs
`Content-Type: application/json` and a same-origin `Origin` header (`Referer`
alone is rejected). Every page is the app under `/app`; the old addresses
(`/`, `/chats`, `/memory`, `/desktop`, `/own-work`, `/reminders`, `/briefs`,
`/settings`, `/plugins`, `/usage`, `/turns`, `/proposals`) redirect there, so
bookmarks keep working.
