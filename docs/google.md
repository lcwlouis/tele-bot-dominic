# Google Calendar and Gmail

Dom can read the owner's calendar, keep calendars of his own and add, move and delete events on them, search and read
mail, and save mail drafts. It is owner-only: none of it exists in a group or a friend's DM. Every change goes through an
approval card, and Dom never sends mail: there is no send in the code. He never writes to the owner's own calendars.

## What Dom can do

| Switch (Settings → Services → Google) | Scope | Dom can |
| --- | --- | --- |
| Calendar: read events | `calendar.events` | List upcoming events and free/busy in the owner DM. A compact "today and tomorrow" block also rides in the owner DM's turn note, fetched in the background and cached for about 15 minutes. |
| Dom's own calendars | `calendar.app.created` | Make calendars of his own (for example "Dom - Planning") and add, move and delete events on **those only**. An owner-DM card shows the calendar, title, time and attendees, with Reject, Allow once and Allow always, **per kind** (add, move and delete each have their own policy in System → Access). |
| Gmail: search and read | `gmail.readonly` | Search (a Gmail query, newest first, snippets) and read one message (text only, capped; attachments are named, never opened). |
| Gmail: save drafts | `gmail.compose` | Save a draft after a card shows the recipients, subject and the start of the text. The draft is not sent. |

Each switch starts **off**. A switch whose scope Google did not grant (you can untick a permission on Google's consent
screen) cannot be turned on. Off means the tool is not offered and the handler refuses it in code too.

`gmail.compose` technically also lets a token send mail; Google has no drafts-only scope. Dom's code exposes no send,
and the tool has no such action.

## Dom's own calendars

`calendar.app.created` is Google's narrow scope for an app that keeps calendars of its own: it can make secondary
calendars and see, add, change and delete events on the ones **it made**, and nothing else. That is why Dom's writes
no longer go to your primary calendar (v0.8.6): reading your calendars keeps working as before under `calendar.events`,
and writing happens only on calendars Dom made.

- **Making one asks every time.** `create_calendar` sends a card (Reject, Allow once). There is no Allow always for it,
  and Allow is not a policy you can set for it: each new calendar is asked once, when Dom first wants it. Dom keeps at
  most five calendars and refuses a name he already has.
- **Code checks the target.** Dom's calendar tool takes the calendar by name or id. Add, move and delete refuse
  anything that is not in the list of calendars he made ("Dom writes only to calendars he made himself"), including your
  primary calendar, before any card is sent. Google enforces the same under this scope. The ids of his calendars are
  kept in the encrypted secret store (namespace `google`, key `calendars`), not in the database as text.
- **Reading.** Dom can list the events of one of his calendars (`calendar` on `list` or `freebusy`) as well as your
  primary calendar, and the `calendars` action lists the calendars he made.
- **Nudges.** An event Dom adds or moves can carry `nudge_minutes` (1 to 10080). Dom then reminds you that long before it
  through the ordinary reminder system (`dom/reminders.py`), in your DM only, as a normal reminder: nothing about it
  reaches a group or a friend. Moving the event moves the reminder, `nudge_minutes: 0` on a move takes it off, and
  deleting the event (or removing its calendar) cancels it. A nudge whose time has already passed is refused before a card
  is shown, and if the reminder list is full the event is still made and Dom says no nudge was set.
- **Existing connections.** A connection made before v0.8.6 lacks the new scope. Everything else keeps working; the
  Google card says "Reconnect to let Dom keep his own calendars", the "Dom's own calendars" switch stays locked, and the
  tool offers only reading until you press **Connect again** and Google grants it. Add `calendar.app.created` to the
  consent screen first (below).
- **On the dashboard.** Settings → Services → Google lists Dom's calendars with their colour and the number of upcoming
  events (read from Google when the page opens, so the card itself never waits on Google). The trash button next to one
  deletes it from your Google account after a confirmation, and cancels its nudges. Colour appears when Google lets Dom
  read it.
- **Disconnecting** revokes the token but keeps the list of calendars, so connecting the same account again picks them
  up. The calendars themselves stay in your Google account until you remove them here.
- **If Google loses the answer** to "make a calendar" (a timeout), Dom does not try again, and tells you it may or may
  not exist: check Google Calendar before asking again.

## Create the OAuth client (once)

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable the **Google Calendar
   API** and the **Gmail API**.
2. Configure the OAuth consent screen. A personal Google account can keep the app **unverified in production** for
   owner-only use (the consent screen then says the app is unverified; continue past it). Do not leave it in
   *Testing*: Google expires a Testing app's refresh token after seven days.
3. Add the four scopes:
   - `https://www.googleapis.com/auth/calendar.events`
   - `https://www.googleapis.com/auth/calendar.app.created`
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
is redirected to a generic failure (a request that is not a top-level navigation gets a plain 403) with no side effect at all (no setting, cookie or state is touched); every other path and every other method still needs the token. Behind
Cloudflare Access the same checks apply on top of Access's own gate.

## Approvals

Create calendar, add, move, delete and draft each go through an owner-DM card, and the tool returns at once ("asked the owner"); the
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
