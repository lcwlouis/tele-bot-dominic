# GIFs

Dom's `gif` action lets him answer with a reaction GIF. He writes a short search query (at most 50
characters). Dom's code searches [KLIPY](https://klipy.com/developers), picks one result at random from
the first ten and sends it as a Telegram animation, after the bubbles and any sticker, in the same place
a sticker goes. The action is optional per turn, like stickers, and is not offered in a bot's final turn.

## Turning it on

1. Create a KLIPY account and an API key in the [partner panel](https://partner.klipy.com/api-keys).
   A new key is in testing mode, limited to 100 requests an hour. That is enough for one friend group.
   Unlimited use needs a production-access request in the same panel.
2. Put the key in the instance `.env` as `KLIPY_API_KEY=...`, then restart Dom.
3. Ask Dom in the owner DM what he can do: `self_inspect` reports `gifs: on`.
4. Read the note about approval below before you rely on it.

With no key nothing changes: the action is not offered to the model and `self_inspect` says GIFs are off.

## Why KLIPY

Checked on 2026-11-20 against the providers' own documentation.

- **Tenor is gone.** Google closed new Tenor API keys on 2026-01-13 and shut the API down on
  2026-06-30 (reported by GIGAZINE, July 2026).
- **GIPHY** has a free beta key (100 calls an hour) and a `rating` filter, but its
  [endpoint docs](https://developers.giphy.com/docs/api/endpoint) say the search call must be made from
  the client side and the query must be the user's own words. Both rule out a bot that writes the query.
- **KLIPY** has a free test key (100 calls an hour, production access on request, per its
  [overview](https://klipy.com/api-overview)), a `content_filter` parameter (`off`, `low`, `medium`,
  `high`), a `format_filter` that returns only mp4, and a per-file `size` field. Its
  [GIF search](https://docs.klipy.com/gifs-api) answers with `data.data[].file.{md,sm}.mp4.{url,size}`.
- Imgur has no curated GIF search, so it was not considered further.

## Limits of this integration

- Dom asks for `content_filter=high`, the strictest level, so some searches return nothing and Dom sends
  no GIF.
- Requests time out after 5 seconds. Only `https` mp4 files on `klipy.com` hosts and at most 5 MB are
  used. Dom sends the URL; Telegram downloads the file itself, so Dom keeps no copy.
- Any failure (timeout, HTTP error, empty or odd answer, Telegram refusing the file) means no GIF. The
  rest of the turn is sent as usual.
- Dom sends the fixed `customer_id=dom`, never a chat, user or owner identifier. The API key is part of
  KLIPY's request URL, so Dom lowers the `httpx` log level to keep it out of the logs.
- KLIPY's [integration requirements](https://docs.klipy.com/integration-requirements) say that server-side
  requests, proxying and caching need prior approval from KLIPY (contact address in those requirements), and that results
  should not be re-ordered or filtered by the integrator. Dom searches from the server and picks one
  result, so **the owner should ask KLIPY for approval before relying on this beyond testing**. Dom does
  not show KLIPY branding in Telegram; ask KLIPY whether that is acceptable for a private bot.
