# Subscription agents (pilot)

Dom can use the coding agents you already pay for, Codex (ChatGPT), Claude and Antigravity (Google AI), as a model for two jobs: **escalation** (the bigger model you approve for one task) and **drafting** (skill and plugin drafts). They talk to Dom over the [Agent Client Protocol](https://agentclientprotocol.com) (ACP), and each is a pilot: off until you switch it on, and easy to drop.

## Read this first

- **It may break the provider's terms.** Anthropic's consumer terms bar access through "automated or non-human means, whether through a bot, script, or otherwise" except with an API key; Google's Antigravity terms call using third-party software to reach the service a breach; OpenAI documents unattended Codex on a ChatGPT login only for trusted private automation. Dom runs them for you, on your own accounts, and you accept that risk. An account could be limited or closed. The research behind this is in `docs/superpowers/notes/v0.8.3-acp-agents.md`.
- **It is not zero data retention.** Each provider keeps your conversations under its consumer terms and may train on them unless you opt out in its settings (Claude: data privacy controls; ChatGPT: Settings → Data controls; Google: your activity settings). Everything in the prompt goes to it, so it sees what Dom sends: your DM's window, notes and tool results for an escalated task, or the brief for a draft.
- **Your DM only.** An agent never serves chat, triage, photos and voice, friends or groups. Dom checks the chat in code before it starts one: a call from any chat but your own DM is refused and goes on to the next provider.

## Switch one on

Settings → Models → Subscription agents has one switch per agent, each off by default. A switch applies at once and is saved as `[acp.<name>] enabled = true` in `config.toml` (see [Configuration](configuration.md#subscription-agents-pilot)). An agent that is on is tried **first** in the escalation and drafting chains; if it fails or is not allowed, Dom goes on to the providers configured for that role. Limit one to a single role with `roles = ["escalation"]`.

When you approve a model switch for a task, the card names the agent ("Codex subscription") and says it is billed to your subscription. Each call is recorded as usage of kind `subscription` with cost 0, so the spend caps never count it and never stop it. Turn logs and traces show the agent, how long it took, any failure and that it was billed to a subscription. The subscription's own allowance is the only limit; Dom adds none beyond a per-call timeout.

## Set up an agent

Install the adapter, sign in once on the machine that runs it, and switch it on.

| Agent | Adapter | Login | Dom turns its tools off by |
| --- | --- | --- | --- |
| Codex | `codex-acp` (npm, from the Agent Client Protocol project), needs Node | `codex login` (ChatGPT) or an API key | `INITIAL_AGENT_MODE=read-only`, `CODEX_CONFIG` with `features.shell_tool = false` and web search disabled |
| Claude | `claude-agent-acp` (npm, or the standalone executable from its releases) | `claude` login; a long-lived token from `claude setup-token` for a headless machine | `tools: []` sent with the session, and every permission refused |
| Antigravity | `agy acp` | sign in with `agy` | every permission refused (it has no documented tools-off switch) |

For all three Dom also refuses every permission request the agent makes, never offers file or terminal access, starts the agent in an empty temporary directory, and passes it only `PATH`, `HOME` and a few locale variables, never Dom's own keys. The agent starts fresh for every call and is killed when the call ends, times out or is cancelled.

If an adapter's command is somewhere unusual, set `command` (and `args`) in its table.

### Running it on another machine

An adapter that does not run on the Pi (Codex's arm64 support is not documented, so assume it does not) can run on a Mac you can reach. Give the Mac an SSH name in `~/.ssh/config` with key login, sign the agent in there, and set:

```toml
[acp.codex]
enabled = true
host = "studio-mac"        # the name in ~/.ssh/config
command = "/opt/homebrew/bin/codex-acp"  # use a full path: ssh gives the command a minimal PATH
```

Dom runs `ssh <host> -- env … <command>` and speaks ACP over it. Secrets are never sent to a host (`auth_env` is refused with `host`): sign in on that machine. Without `host`, the agent runs on the machine Dom runs on; the Pi can run Claude and Antigravity from their arm64 builds.

### An API key instead of a login

`auth_env = ["CODEX_API_KEY"]` hands the named variables from `.env` to the agent's process. That is API billing, not a subscription, and not what this pilot is about; it is there for agents that can only sign in that way on a headless machine.

## How it answers

The agent is used as a plain text model. Dom puts the conversation, and the tools Dom offers, in one prompt and asks for the actions JSON it always expects, or a `{"tool_calls": [...]}` object for a tool. Dom runs the tools itself, with its usual scope checks and approval cards, so the agent never runs anything. Photos and voice notes reach it as their descriptions.

## Check an agent works

1. Switch it on and approve a model switch (or ask for a skill draft) in your DM. The card or the Turn log's trace names the agent and its time.
2. Confirm its tools are off: ask for something only a tool could do ("run `ls` and tell me what is in the folder"). It should answer that it cannot, and the agent's own history must show no command ran.
3. Look at Usage: calls appear as `Subscription agents` with no cost.

If it fails, Dom's log has one line, `<agent> agent failed after N s: <reason>` ("is it installed here?", "answered with an error (-32000): Authentication required", "did not answer within 120 s"), and Dom falls through to the next provider.

## Drop it

Switch the agent off. Nothing else keeps running: there is no background process between calls.
