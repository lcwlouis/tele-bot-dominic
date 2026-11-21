# T3 Code bridge

Dom can install a dedicated T3 Code user service on Linux. Through it, the
Telegram owner can inspect projects and recent coding activity on linked machines
from their DM. Thread messages are treated as untrusted text. Sending a message
to a thread requires a tap that confirms the destination and the exact message.

## Install the user service

Extract the Linux ARM64 Node.js 24 tarball into `~/.local/node` so that
`~/.local/node/bin/node` and `~/.local/node/bin/npm` exist. Use the
[official Node.js downloads](https://nodejs.org/en/download).

Set the T3 Code version, Node path and local port in the Dom home's `config.toml`.
Set `enabled = true` after `dom t3-connect` has signed in (see below).

```toml
[t3]
enabled = false
port = 39731
version = "0.1.10"
node = "~/.local/node/bin/node"
```

Install the package at the matching version-specific prefix. The command
function below also sets the runtime cache location used by the service.

```sh
export PATH="$HOME/.local/node/bin:$PATH"
t3_version=0.1.10
npm install --prefix "$HOME/.local/t3/$t3_version" --save-exact "t3@$t3_version"

t3_dom() {
    T3CODE_HOME="$HOME/.t3-dom" "$HOME/.local/node/bin/node" \
        "$HOME/.local/t3/$t3_version/node_modules/.bin/t3" "$@"
}
t3_dom --version
```

Preview the unit, then use `--yes` to write, reload and enable it. Substitute
your Dom home's path.

```sh
dom t3-service path/to/dom-home
dom t3-service path/to/dom-home --yes
```

The service binds to loopback and uses `~/.t3-dom` for T3 Code state. It
disables project creation from the startup directory and caps the service
at 600 MiB. Keep the Pi dedicated to the bridge. Leave its project list
empty and coding providers disabled. To change the port or version of a
running service, reinstall its unit and restart `t3.service`.

## Link or unlink a machine

A headless server starts with linked environments switched off. Turn the Labs
switch on before linking: with `t3.service` stopped, write
`{"linkedEnvironmentsEnabled": true}` to `~/.t3-dom/userdata/settings.json`, then
start the service again. Linking needs a single-use pairing code from the other
machine; keep its scopes narrow:

```sh
# on the other machine
t3 pair --scope orchestration:read --scope orchestration:operate --ttl 10m --label "Dom"
```

Run the link command on the Pi as the service user, using the other machine's
T3 Connect address. Approve the request on that machine in T3 Code under
**Settings → Sharing & access**. Use `approval-required` (Supervised) so Dom can
pass on messages you confirm, or `read-only` if you only want him to read.

```sh
t3_dom environment link --base-dir "$HOME/.t3-dom" --access approval-required --pairing-code '<code>' '<environment-url>'
t3_dom environment list --base-dir "$HOME/.t3-dom" --json
```

To remove a link, also revoke it under **Authorized clients** on the
other machine so its session ends.

```sh
t3_dom environment unlink --base-dir "$HOME/.t3-dom" '<environment-id>'
```

## CLI compatibility

T3 Code v0.1.10 exposes the following commands. Each supported command
accepts `--base-dir DIR` to select a local T3 Code instance.

| Operation | Command and result |
| --- | --- |
| List links | `t3 environment list --json` returns an array with environment IDs, labels, access and reachability status. |
| List projects | `t3 project list --json` is rejected. The `project` group contains `add`, `remove` and `rename`. |
| List threads | `t3 thread list --project PROJECT --json` returns an array for a local project, ordered newest first. `PROJECT` is an ID or workspace path. Any display limit would be applied in Dom. |
| Read a thread | `t3 thread show THREAD --json --messages N` returns a `thread` object and a `messages` array. |
| Send to a thread | `t3 thread send --json THREAD -` takes text on stdin. This syntax is from CLI help. |

Links report remote reachability. Thread commands use the local server and
reject remote selectors such as `--environment` and `--environment-id`.
The CLI has no project discovery and cannot select a linked machine, so the
owner-DM bridge does both through T3 Code's MCP interface instead. T3 Code documents a
separate [MCP interface for outside agents](https://github.com/pingdotgg/t3code/blob/v0.1.10/docs/user/outside-agents.md).

## Sign in

Run `dom t3-connect <home>` to preview sign-in. Add `--yes` to register Dom with
the local gateway and authorize it using Supervised access. The command
stores the sign-in in `<home>/t3-token.json` with mode 0600. Sign-in lasts
30 days. Set `[t3] base_url` to the matching loopback origin if you changed
the service port.

## Use the bridge from Telegram

Enable `[t3] enabled` and restart Dom. In the owner's private chat, ask Dom
to summarize coding work, search for a thread, or read its latest messages.
Use a machine or thread ID if more than one matches a name. Dom sends one DM
note after a turn that accessed T3. Thread content is treated as untrusted
and shortened before reaching the model.

When you ask Dom to pass on a message, review the thread, machine, and exact
text in the confirmation. Tap **Send** to submit it in queue mode or
**Cancel** to discard it. The button expires after 10 minutes and can be
used once. Dom reports the result in the DM.

Use `/t3` to see the bridge status, sign-in expiry, reachable machines, and
the latest 15 audit entries. Dom keeps 180 days of audit history, including
failed operations and the exact text of staged and submitted messages.
Tap **Reconnect** to renew Supervised access. Dom sends one expiry reminder
per sign-in when three days or fewer remain.
