# Google Calendar and Gmail

Dom can read the owner's calendar, add, move and delete events, search and read mail, and save mail drafts. It is
owner-only: none of it exists in a group or a friend's DM. Every change goes through an approval card, and Dom never
sends mail: there is no send in the code.

## What Dom can do

| Switch (Settings → Services → Google) | Scope | Dom can |
| --- | --- | --- |
| Calendar: read events | `calendar.events` | List upcoming events and free/busy in the owner DM. A compact "today and tomorrow" block also rides in the owner DM's turn note, fetched in the background and cached for about 15 minutes. |
| Calendar: add, move, delete | `calendar.events` | Propose an event change. An owner-DM card shows the title, time and attendees, with Reject, Allow once and Allow always, **per kind** (add, move and delete each have their own policy in System → Access). |
| Gmail: search and read | `gmail.readonly` | Search (a Gmail query, newest first, snippets) and read one message (text only, capped; attachments are named, never opened). |
| Gmail: save drafts | `gmail.compose` | Save a draft after a card shows the recipients, subject and the start of the text. The draft is not sent. |

Each switch starts **off**. A switch whose scope Google did not grant (you can untick a permission on Google's consent
screen) cannot be turned on. Off means the tool is not offered and the handler refuses it in code too.

`gmail.compose` technically also lets a token send mail; Google has no drafts-only scope. Dom's code exposes no send,
and the tool has no such action.

To turn an event into a reminder, ask Dom: he reads the event and uses his ordinary `remind` tool. Nothing is synced
automatically.

## Create the OAuth client (once)

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable the **Google Calendar
   API** and the **Gmail API**.
2. Configure the OAuth consent screen. A personal Google account can keep the app **unverified in production** for
   owner-only use (the consent screen then says the app is unverified; continue past it). Do not leave it in
   *Testing*: Google expires a Testing app's refresh token after seven days.
3. Add the three scopes:
   - `https://www.googleapis.com/auth/calendar.events`
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
4. Create an OAuth client of type **Web application** with exactly this authorized redirect URI:

   ```text
   <web.public_url>/api/integrations/google/callback
   ```

   `web.public_url` is the dashboard's https address, for example `https://dom.example.org`, so the URI is
   `https://dom.example.org/api/integrations/google/callback`. Google needs https and a hostname, not an IP address.
5. Put the client in the instance's `.env`:

   ```text
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   ```

   The dashboard shows each as "set" or "not set" and never prints a value. Restart Dom after editing `.env`.

## Connect

Settings → Services → **Google** → **Connect Google**. Dom starts an authorization-code flow with PKCE (S256), a random
`state`, `access_type=offline` and `prompt=consent` so Google returns a refresh token. The `state` is single-use, lasts
ten minutes and is tied to the browser that started it by a short-lived cookie (`HttpOnly`, `Secure`, `SameSite=Lax`,
scoped to the callback path). After you consent, Google sends the browser to the callback, Dom exchanges the code on the
server and stores the tokens in the encrypted secret store (namespace `google`), never in plain text in the database
and never in a log, trace or turn log. The granted scopes are recorded.

**Disconnect** revokes the token at Google (`https://oauth2.googleapis.com/revoke`) and deletes it here, even if Google
cannot be reached.

If Google later says the refresh token is expired or revoked (`invalid_grant`: you removed Dom in your Google account,
or you changed your password, which kills Gmail-scoped tokens), Dom marks Google as needing a reconnect, deletes the
dead token and sends one quiet notice in the owner DM with a link to Settings. It does not repeat the notice until you
connect again.

## The callback behind Cloudflare Access

The callback is a top-level `GET` navigation from `accounts.google.com`. Behind Cloudflare Access it works because the
browser sends its Access session cookie on a top-level navigation, so Access injects the signed assertion and Dom
authenticates the request like any other. If the Access session expired while you were on Google's consent screen,
Access redirects to its own sign-in and the code is lost: press Connect again.

The route is deliberately not one of the dashboard's `Sec-Fetch-Site: same-origin` guarded reads, since Google's
redirect is cross-site by nature.

**With a token-only dashboard** (`web.dev_token_env`, loopback) a browser cannot send the token header on Google's
redirect, so that one `GET` path alone skips the token check. It authenticates itself instead: the connect request is
authenticated (and sets the `HttpOnly`, `Secure`, `SameSite=Lax` binding cookie for exactly one single-use `state`), and
the callback succeeds only when the unexpired, unused `state` in the URL matches that cookie. A request without the
cookie, with a wrong, expired or replayed `state`, or that is not a navigation (`Sec-Fetch-Mode` other than `navigate`),
is redirected to a generic failure with no side effect at all (no setting, cookie or state is touched); every other path and every other method still needs the token. Behind
Cloudflare Access the same checks apply on top of Access's own gate.

## Approvals

Add, move, delete and draft each go through an owner-DM card, and the tool returns at once ("asked the owner"); the
action runs in the background after the tap and its outcome is written to the DM history for Dom's next turn.

- **The card shows everything that matters:** the title or subject, every attendee or recipient, the times with the
  timezone and, for a move, the old and the new time. If that cannot be shown whole in a Telegram card without cutting
  anything (say, ten long addresses), the card only summarises ("10 recipients, too long to list here") and has just
  **Reject** and a link: only the dashboard's **Waiting for you** or **System → Access** view, which shows the complete
  details, can approve it.
- **Before it runs,** Dom checks again that nothing moved since the card was made: each switch and the connection
  carry a counter, so a switch turned off and on again, a disconnect or a different account makes the older card
  stale. The request is registered before anything is scheduled, so a cancel always finds it; the check is repeated
  right before the write itself. Turning a switch off, disconnecting, or connecting another account cancels the
  waiting cards ("Cancelled").
- **Nothing is cut:** every attendee or recipient, the whole title or subject and the times are stored with the
  request and shown, scrollable, on the dashboard. A title over 200 characters or a place over 120 is refused instead
  of shortened, and an approval whose details are too large to store whole (hundreds of long addresses) is not created
  ("too many attendees or recipients to review").
- **A tap that lands as the wait runs out still counts:** a committed approval always runs or is reported as failed.
  Allow always is saved only after the action ran.
- **Writes are not repeated blindly.** A calendar insert carries its own event id, so a retry cannot make a second
  event. A draft that got no clear answer (a timeout or a 5xx after Google may have made it) is reported as "may or may
  not have been created, check Gmail's Drafts folder" and not sent again.
- **A slow Google never holds a reply.** Inside a turn Dom spends at most 8 seconds on Google in all and then says it
  is slow; refreshes and approved actions in the background keep the longer retries.
- **Free/busy** reads up to four pages of events; if the range holds more it says the answer is incomplete and never calls
  that range free.

## Quotas and failures

Google API calls are free. Dom allows at most 60 Google requests a minute, retries 429 and 5xx answers with backoff
(honouring `Retry-After`, capped at 8 seconds), and refreshes the token once when Google says it is stale. Every model
call a Google tool triggers is attributed and capped like any other.

For what goes to which model, see [Privacy](privacy.md#google-calendar-and-gmail).
