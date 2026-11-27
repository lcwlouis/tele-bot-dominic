# Google Calendar and Gmail

Dom can read the owner's calendar, keep calendars of his own and add, move and delete events on them, search and read
mail, save mail drafts, work with Google Drive files (Docs and Sheets included) and look up Google Contacts. It is
owner-only: none of it exists in a group or a friend's DM. The one thing a friend can reach is
[booking](#booking-within-your-windows): times inside windows you set, and nothing else. Every change goes through an
approval card, and Dom never sends mail: there is no send in the code. He never writes to the owner's own calendars, and
in Drive he sees only the files he made and the files the owner hands him.

## What Dom can do

| Switch (Settings → Services → Google) | Scope | Dom can |
| --- | --- | --- |
| Calendar: read events | `calendar.events` | List upcoming events and free/busy in the owner DM. A compact "today and tomorrow" block also rides in the owner DM's turn note, fetched in the background and cached for about 15 minutes. |
| Dom's own calendars | `calendar.app.created` | Make calendars of his own (for example "Dom - Planning") and add, move and delete events on **those only**. An owner-DM card shows the calendar, title, time and attendees, with Reject, Allow once and Allow always, **per kind** (add, move and delete each have their own policy in System → Access). |
| Gmail: search and read | `gmail.readonly` | Search (a Gmail query, newest first, snippets) and read one message (text only, capped; attachments are named, never opened). |
| Gmail: save drafts | `gmail.compose` | Save a draft after a card shows the recipients, subject and the start of the text. The draft is not sent. |
| Files in Google Drive | `drive.file` | List, search and read the files he made and the files you handed him; make Docs and Sheets in his own folder; add to a Doc, add rows to a Sheet or replace cells in one, each after a card. See [Drive, Docs and Sheets](#drive-docs-and-sheets). |
| Contacts | `contacts.readonly` | Look up a name and get the email address and phone number, read only. See [Contacts](#contacts). |

Each switch starts **off**. A switch whose scope Google did not grant (you can untick a permission on Google's consent
screen, and the Files and Contacts scopes are asked for only when you want them, below) cannot be turned on. Off means the tool is not offered and the handler refuses it in code too.

`gmail.compose` technically also lets a token send mail; Google has no drafts-only scope. Dom's code exposes no send,
and the tool has no such action.

## Dom's own calendars

`calendar.app.created` is Google's narrow scope for an app that keeps calendars of its own: it can make secondary
calendars and see, add, change and delete events on the ones **it made**, and nothing else. That is why Dom's writes
no longer go to your primary calendar (v0.8.6): reading your calendars keeps working as before under `calendar.events`,
and writing happens only on calendars Dom made.

- **Making one asks every time.** `create_calendar` sends a card (Reject, Allow once). There is no Allow always for it,
  and Allow is not a policy you can set for it: each new calendar is asked once, when Dom first wants it. Dom keeps at
  most five calendars and refuses a name he already has. The cap and the name are checked again when a card is approved
  (two cards waiting together cannot make a sixth calendar or the same one twice). The names `primary` and an email
  address are refused: they name your own calendar.
- **Code checks the target.** Dom's calendar tool takes the calendar by name or id. Add, move and delete refuse
  anything that is not in the list of calendars he made ("Dom writes only to calendars he made himself"), including your
  primary calendar, before any card is sent. Google enforces the same under this scope. The ids of his calendars are
  kept in the encrypted secret store (namespace `google`, key `calendars`), not in the database as text. A name is looked
  up by id first, and `primary` always means your own calendar.
- **Tied to the Google account.** Google is asked for `openid` only to learn which account connected; Dom keeps a hash of
  its `sub`, never the ID token. The list of calendars and nudges is kept per account and only the connected account's is
  shown, counted or written. When a different account connects, the earlier one's calendars are set aside (kept for when
  it connects again), its nudges are cancelled and an owner event is recorded.
- **Writes are serialised per calendar.** An approved write runs with that calendar locked, so a move and a delete of one
  event cannot leave a reminder behind, and it first checks the calendar is still Dom's: removing a calendar cancels the
  cards still waiting to write to it, and one already approved finds it gone and changes nothing.
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
  deletes it from your Google account after a confirmation, and cancels its nudges. It works with the write switch off
  (it is your own action) but needs a live connection with the scope. Colour appears when Google lets Dom read it.
- **Disconnecting** revokes the token but keeps the list of calendars, so connecting the same account again picks them
  up (nothing shows while disconnected). The calendars themselves stay in your Google account until you remove them here.
- **If Google loses the answer** to "make a calendar" (a timeout), Dom does not try again, and tells you it may or may
  not exist: check Google Calendar before asking again.

## Asking for a newer permission only when it is wanted

A plain **Connect Google** asks Google for the calendar and Gmail permissions only. The Files and Contacts permissions are
asked for when you want them: a switch whose permission was never granted shows a **Reconnect to allow** button, which
sends you through Google's consent screen once more asking for just that permission on top of what you already granted
(`include_granted_scopes`). The switch unlocks when Google grants it. Until then Dom is not offered the tool, and if the
model asks anyway the handler answers that Google has not granted it and to press Reconnect. Disconnecting and connecting
again starts from the base permissions.

## Drive, Docs and Sheets

Dom's connection holds `https://www.googleapis.com/auth/drive.file` and nothing wider. Under that scope Google itself
shows Dom only the files **he made** and the files **you opened with him through Google Picker**: not your other files,
not your folders, not what other people shared with you. Dom does not request `drive`, `documents` or `spreadsheets`;
adding them to the consent screen is not needed and Dom never asks for them.

The Docs and Sheets APIs work on those files under `drive.file`. Google's reference lists `drive.file` among the accepted
scopes of [`documents.get`](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/get),
[`documents.create`](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/create) and
[`documents.batchUpdate`](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate),
[`spreadsheets.create`](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/create),
[`values.get`](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/get),
[`values.update`](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/update),
[`values.append`](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append) and
Drive's [`files.export`](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export). Dom calls
these Google REST APIs directly (not through an MCP server), so approval cards, scopes and the owner-DM rule stay in his
own code.

| `drive` action | What it does |
| --- | --- |
| `list` | The files he may see, newest first, or a search by name or text (`query`). Names are fenced as untrusted. |
| `read` | A Google Doc or Slides (as plain text), a Sheet (the first sheet as CSV, or a `range` like `Sheet2!A1:D20`) or a text file (`text/*`, JSON, XML, YAML), by id or by link. |
| `create` | A Doc (`name`, `text`) or a Sheet (`name`, `rows`) in his own folder, **Dom**, made the first time it is needed. |
| `append` | Text at the end of a Doc, or rows below the data of a Sheet (`sheet` names the tab; the first by default). |
| `update` | Replace the cells from `range` with `rows` in a Sheet. |

- **Reading is fenced and bounded.** What he reads was written by other people, so the result is wrapped as untrusted
  data and he is told never to follow it or copy it into notes or other chats. One read returns at most 8000 characters
  (`offset` pages through the rest); a plain file over 1 MB is not fetched; Google caps an export at 10 MB. A Sheets
  `range` is cut to 200 rows by 26 columns from its top-left cell before Google is asked (the result says so, and he
  reads a later range for the rest), and an answer over 2 MB is refused while it arrives, never parsed. He reads Docs,
  Sheets, Slides and text files; anything else (images, PDFs) he refuses by name.
- **Writing goes through a card.** Each of `create` (Doc, Sheet), `append` (Doc), `append` (Sheet rows) and `update`
  (Sheet cells) is its own kind in System → Access with Reject, Allow once and Allow always, so allowing document
  appends does not allow overwriting cells. The card shows the file's name and the exact text or rows; for `update` it
  also shows what the cells hold now ("Before") and what they will hold ("After"), and the range is spelled out for the
  rows actually written (`B2` with two rows of two cells shows `B2:C3`). The text or cells to be written count as material,
  like a recipient or an attendee: a card that cannot list all of them uncut in Telegram (more than eight lines, or a line
  over 200 characters) shows no part of them, says so, and becomes dashboard-only, where everything is shown, as for
  calendar and mail.
- **Cells are text.** Values are written as typed (`valueInputOption=RAW`), so a cell that starts with `=` stays text and
  never runs as a formula. Size limits: 8000 characters of doc text; at most 100 rows of 20 cells, 500 characters a cell
  and 20,000 characters in all.
- **His folder.** Files he creates go into a folder called **Dom**, found by name among the folders he made (a folder of
  yours with the same name is invisible to him under this scope, so it is never written to) and made if it is not
  there. Two files made at once share one find-or-make, so there is no second folder. Writes that get no clear answer
  from Google are never repeated: he says the file may or may not exist.
- **Files he cannot see.** A file shared with you but not handed to Dom looks, to Google, like a file that does not
  exist. The tool says so and carries the one-tap link that hands it over (next section).
- **Switching off** cancels the cards still waiting, as for the other switches.
- **On the dashboard,** Settings → Services → Google lists the Files switch with its permission state ("Reconnect to
  allow" when missing), a link to Dom's folder and the names of the files he may see (names only, read from Google when
  the page opens), and a link to the picker.

## Handing Dom a file by link

A shared link alone gives an app nothing under `drive.file`: Google grants Dom a file only when you open it with Dom's
OAuth client through Google Picker. So when you send a Drive, Docs, Sheets or Slides link in your DM, Dom's turn note
carries a one-tap link to a small dashboard page (`<web.public_url>/google/picker?file=<id>`) and he answers with it. On
that page you tap **Choose the file in Google Drive**, sign in to Google if asked, and select the file (the picker opens on
the linked file, with the rest of your Drive behind a tab). That grants Dom that file and no other. The page then tells
Dom's server, which asks Google whether Dom can now see each file and, for each one he can, records an owner event
("Handed Dom the Google Doc … [file id …]") so he can read it right away. Nothing is claimed that Google did not confirm.

- **Only file links.** Folder links and other addresses make no link: the Picker cannot hand Dom a folder's contents.
  A message with more than three links gets links for the first three.
- **Behind the dashboard login.** `/google/picker` is a normal dashboard page: Cloudflare Access (or the dev token) must
  let you in, like every other page. No token of Dom's ever reaches the page: the browser asks Google for its own
  `drive.file`-only token with Google Identity Services, and the server asks Google (with its own connection) whether the
  pick worked. The page carries the OAuth client id (public by design), the browser API key and the project number.
- **Its own CSP.** Only this page may load Google's scripts and frames: `script-src` adds `https://apis.google.com` and
  `https://accounts.google.com/gsi/client`; `frame-src` adds `https://docs.google.com` and `https://accounts.google.com/gsi/`;
  `connect-src`, `style-src` and `img-src` add Google's own hosts. Every other dashboard page keeps the strict default
  (`default-src 'self'`). The page's `Referrer-Policy` is `strict-origin`, because your API key's website restriction
  reads the origin (never the path) from the Referer.
- **If the page cannot work** (no key or project number, the switch off) it says so and loads nothing from Google, and
  Dom's turn note says the picker is not set up instead of offering a dead link.

### Set up the picker (once)

1. In the same Google Cloud project, enable the **Google Picker API**, the **Google Drive API**, the **Google Docs API**,
   the **Google Sheets API** and the **People API**.
2. Create an **API key** (APIs & Services → Credentials). Restrict it: **Websites** with `<web.public_url>/*` and
   `https://docs.google.com/*` (the Picker renders in an iframe on docs.google.com), and **API restrictions** to the
   Picker API.
3. On the existing **Web application** OAuth client add `<web.public_url>` (for example `https://dom.example.org`, no path)
   under **Authorized JavaScript origins**. The Picker's token request runs in your browser from that address.
4. Note the project's **number** (Cloud console → project settings; digits only).
5. Add both to the instance's `.env` and restart Dom:

   ```text
   GOOGLE_PICKER_API_KEY=...
   GOOGLE_PROJECT_NUMBER=...
   ```

   Neither is ever printed in a log, the dashboard (only "set" or "not set") or Dom's prompt; they appear only in the
   picker page itself, which sits behind the dashboard login.
6. Turn on **Files in Google Drive** (press **Reconnect to allow** first if it is locked).

## Contacts

The **Contacts** switch (`contacts.readonly`) lets Dom look up a name and get the email address and phone number from
your Google Contacts, to find a friend's address for a booking or a draft. It is read only (`people:searchContacts`),
owner DM only, and the answer is fenced as untrusted data because contact text was typed by other people. Dom asks for at
most five matches. Google's People API wants one empty search before the first real one to fill its cache; Dom does that
once.

The same lookup is available to Dom's own code as `contact_email(google, name)` in `dom/google_contacts.py`: it returns
one address, or `None` when nothing matches, when two people could be meant (it never guesses) or when the switch is off.
Nothing calls it yet: booking invites a friend only by the email they give in the chat.

## What Dom can and cannot see

| Dom can see | Dom cannot see |
| --- | --- |
| Files he made, in his folder **Dom** | Any other file, folder or shared drive of yours |
| Files you handed him with the picker | Files shared with you that you did not hand him |
| Contacts' names, emails and phones (Contacts on) | Anything about contacts beyond that, or any edit |
| Docs, Sheets, Slides and text files, as text | Images, PDFs and other binary files |

Turning a switch off, or disconnecting Google, takes the access away at once. Files already handed to Dom stay granted at
Google until you remove Dom at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Booking within your windows

A friend, in a DM or in a group, can ask Dom to meet. Your calendar does not hold your whole life, so Dom first needs to
know **when you are willing to meet**, and he offers only times inside that.

- **Set the windows** in your DM ("weekday evenings 7 to 10, Saturday afternoons"): Dom turns it into exact windows (days,
  start and end, in your timezone) with the `booking` tool's `set_windows`, saves them and repeats them back so you can
  correct him. Or edit them in Settings → Services → Google, under "Meeting windows": days, times, the least notice
  (default 12 hours), the furthest ahead (default 14 days) and whether a friend whose email is known is invited to the
  event. **No windows means no booking:** Dom tells the friend he will check and asks you to set them. Windows cannot cross
  midnight; use two. Saving windows the first time asks you (the usual calendar card) to let Dom make the calendar
  **Dom - Bookings**, where bookings go.
- **Offers.** The friend asks; Dom's `offer` finds up to three slots of the length asked (default 60 minutes) that lie wholly
  inside a window, are at least the notice away and within the horizon, and are free on **every calendar he reads**: your
  primary calendar and each of his own. Slots start on the window's start and every half hour after it, are spread over
  different days first, and a slot already offered to someone else is not offered again. An all-day event, or any event
  you are not marked free for, blocks its time. If the calendar cannot be read whole (more events than he can fetch, or Google
  failing) he offers nothing and tells the friend only that he will ask you for times; the reason (no free time, or what could not be read) goes to your DM history. An offer lasts four hours, and a chat holds at most three open.
- **The pick.** When the friend chooses one, Dom asks you with one card: the friend's name, the chat (their DM or the
  group), the time, the calendar, whether the friend is invited, and what they said it is about, all quoted as the
  friend's own words. The friend is told Dom will check. The card has **Allow once** and **Reject** only. To book without
  a card, set "Confirm a booking" to Allow in System → Access; Never makes Dom decline every request kindly.
- **On approval** Dom checks again, against how things are by then, that the time has not started, is still inside your
  windows and your notice (counted from when it was offered), and is still free, adds "Meet <name>" to **Dom - Bookings**,
  and tells the friend it is set. If any check fails, nothing is booked and you get a short notice saying which. The
  friend is added as a guest, and Google emails them, **only if** you allowed invites and they gave their email in the
  chat; otherwise the event is on your calendar alone. Tapping a card twice books once. **On every decline, whatever the
  cause** (a rejection, an expiry, a switch turned off, a time taken or no longer allowed, a failed write), the friend
  gets the same kind line ("Sorry, I can't make that one work. Maybe another time?") and never a reason, so nothing
  about your calendar can be read from it.
- **Two friends at once** are never offered the same time: the slots are chosen and held in one step, and a time stays
  held while it waits for your tap, while it is written, and after it is booked. Times are worked out in your timezone's
  wall clock but added and compared as real instants, so a daylight-saving change neither offers a time that does not
  exist nor shortens a meeting, and the end time shown is the real end.
- **Restarts.** Offers are kept in the database, so a pick finds the slot Dom really offered after a restart. A pick that
  was waiting for your tap when Dom stopped has lost its card; when he starts again he closes it and tells the friend. A booking that was being written when Dom stopped may or may not
  have reached Google, so at start he asks Google for the event (its id was saved first): if it is there the booking is
  settled as booked and the friend told, if not it is declined, and if Google cannot be asked it waits for the next start.
- **What leaves your DM** is the offered start and end times, and the friend's own confirmation. No title, place,
  attendee or busy block is ever sent: Dom asks Google for event times only, and the result a friend's chat sees is built
  from the slots alone. See [Privacy](privacy.md#booking).

## Create the OAuth client (once)

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable the **Google Calendar
   API** and the **Gmail API** (and, for Files and Contacts, the APIs listed under [Set up the picker](#set-up-the-picker-once)).
2. Configure the OAuth consent screen. A personal Google account can keep the app **unverified in production** for
   owner-only use (the consent screen then says the app is unverified; continue past it). Do not leave it in
   *Testing*: Google expires a Testing app's refresh token after seven days.
3. Add the scopes (`openid` needs no setup: Google always allows it):
   - `https://www.googleapis.com/auth/calendar.events`
   - `https://www.googleapis.com/auth/calendar.app.created`
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
   - `https://www.googleapis.com/auth/drive.file` (Files; non-sensitive)
   - `https://www.googleapis.com/auth/contacts.readonly` (Contacts)

   Do not add `documents`, `spreadsheets` or `drive`: Dom never requests them.
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

Create calendar, add, move, delete, draft and each kind of Drive write (create a doc, add to a doc, create a sheet, add rows, replace cells) each go through an owner-DM card, and the tool returns at once ("asked the owner"); the
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
