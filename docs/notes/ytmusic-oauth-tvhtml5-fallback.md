# YT Music OAuth → TVHTML5 fallback (research note)

**Status:** observed, not implemented. Park here for revival if a future cookie-based
fallback (e.g. `browser-cookie3` extraction → ytmusicapi `browser` auth) also fails.

**Date observed:** 2026-05-07

## Problem

The 0.2.1 plan migrated YT Music auth from ytmusicapi's manual browser-header
flow to its OAuth Device Authorization Grant flow. Auth itself worked (tokens
issue and refresh cleanly), but every subsequent `youtubei/v1/browse?…` call
returned **HTTP 400 "Request contains an invalid argument"**, including:

- `get_liked_songs(limit=…)` (the actual scan target)
- `get_account_info()`
- `get_library_playlists(limit=…)`

The 400 is independent of the request body content — it's the `client.clientName`
in the request context being incompatible with the OAuth token's client type.
ytmusicapi 1.12 (current PyPI latest as of 2026-05-07) hardcodes
`clientName="WEB_REMIX"` in `helpers.initialize_context()`. Tokens issued for a
"TVs and Limited Input devices" OAuth client (the only Google Cloud client
type that supports the device-code grant ytmusicapi uses) cannot impersonate
WEB_REMIX, so Google rejects.

## clientName probe results

Same OAuth token, same `browseId="VLLM"`, varying `client.clientName`:

| clientName                       | Status | Response shape                              |
| -------------------------------- | ------ | ------------------------------------------- |
| `WEB_REMIX`                      | 400    | —                                           |
| `WEB`                            | 400    | —                                           |
| `ANDROID`                        | 400    | —                                           |
| `ANDROID_MUSIC`                  | 400    | —                                           |
| `IOS_MUSIC`                      | 400    | —                                           |
| `TVHTML5`                        | 200    | `tvBrowseRenderer` (TV-shaped, paginated)   |
| `TVHTML5_SIMPLY_EMBEDDED_PLAYER` | 200    | `sectionListRenderer` (20-track hard cap)   |

`as_mobile()` (ytmusicapi's built-in switch to ANDROID_MUSIC) does **not** help
— the same 400 fires.

## Why TVHTML5 works but ytmusicapi can't read it

`TVHTML5` returns the user's full Liked Music playlist with proper pagination,
but in YouTube TV web app shape rather than YouTube Music shape:

```
contents
  .tvBrowseRenderer
    .content.tvSurfaceContentRenderer.content.twoColumnRenderer
      .rightColumn.playlistVideoListRenderer
        .contents[]                       ← tileRenderer items (15 per page)
        .continuations[0].nextContinuationData.continuation   ← page token
```

Each `tileRenderer` exposes:

| Field           | Path                                                                                       |
| --------------- | ------------------------------------------------------------------------------------------ |
| `videoId`       | `metadata.tileMetadataRenderer.title.runs[0].navigationEndpoint.watchEndpoint.videoId`     |
| `title`         | `metadata.tileMetadataRenderer.title.runs[0].text`                                         |
| `artist`        | `metadata.tileMetadataRenderer.lines[0].lineRenderer.items[0].lineItemRenderer.text.simpleText` |
| `duration`      | `header.tileHeaderRenderer.thumbnailOverlays[0].thumbnailOverlayTimeStatusRenderer.text.simpleText` (e.g. `"4:25"`) |
| `thumbnails`    | `header.tileHeaderRenderer.thumbnail.thumbnails[]`                                         |
| `musicVideoType`| `metadata.tileMetadataRenderer.title.runs[0].navigationEndpoint.watchEndpoint.watchEndpointMusicSupportedConfigs.watchEndpointMusicConfig.musicVideoType` |

Pagination is via `?ctoken=…&continuation=…` query params on the same
`youtubei/v1/browse?alt=json` endpoint. Continuation responses live at
`continuationContents.playlistVideoListContinuation.{contents,continuations}`.

ytmusicapi's `get_playlist` parser walks `contents.twoColumnBrowseResultsRenderer.…`
(YT Music web shape) and bails with `KeyError 'twoColumnBrowseResultsRenderer'`
on this TV shape, which is why we observed `KeyError`s on the manual override
attempts during the original probe.

## What it would take to ship

A custom client that bypasses ytmusicapi for the data path while still
delegating OAuth to it:

- New module (~150 lines) with HTTP client + tile-renderer parser + continuation
  loop. Lives next to `ytmusic_client.py`; ytmusicapi is kept only for
  `OAuthCredentials` / `RefreshingToken` token plumbing.
- Adapter in `YTMusicClient.fetch_liked_songs` so the rest of the snapshot
  pipeline keeps consuming the same dict shape.
- Tests against captured tile fixtures (~50 lines).

Information loss vs ytmusicapi's web shape:

- **Album** is not exposed by TV API → field stays empty.
- **Multiple artists** flatten to a single byline string → list-shaped artists
  collapse to one entry. Cross-source compare's `canonical_key` still works.
- We take responsibility for tracking TV API shape changes (ytmusicapi has
  been updated against this surface in the past, e.g. `as_mobile` exists for
  similar reasons).

## When to revisit

Consider this fallback if the next planned approach hits a wall:

1. **`browser-cookie3` cookie extraction → ytmusicapi `browser` auth.** Pulls
   live cookies from the user's logged-in browser each scan, sidestepping
   manual `browser.json` setup *and* OAuth's clientName mismatch. Adds
   `browser-cookie3` dep but reuses ytmusicapi's mature data-path parser.
2. If (1) breaks because the user can't grant filesystem access to a
   browser cookie store (sandboxed environment, headless server, etc.) or
   the browser DB schema flips, this TVHTML5 path is the next-cleanest
   option that keeps OAuth.

## Reproduction script

```python
import time, requests
from likesurgeon.config import Config
from likesurgeon.ytmusic_client import load_oauth_credentials
from ytmusicapi import YTMusic

cfg = Config.load()
creds = load_oauth_credentials(cfg.ytmusic_oauth_client_path)
yt = YTMusic(str(cfg.ytmusic_oauth_token_path), oauth_credentials=creds)

CLIENT_CTX = {"clientName": "TVHTML5", "clientVersion": "7.20240101.13.00", "hl": "en"}
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:88.0) Gecko/20100101 Firefox/88.0",
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://music.youtube.com",
    "authorization": yt._token.as_auth(),
    "X-Goog-Request-Time": str(int(time.time())),
}
URL = "https://music.youtube.com/youtubei/v1/browse?alt=json"

# First page
body = {"browseId": "VLLM", "context": {"client": CLIENT_CTX, "user": {}}}
resp = requests.post(URL, json=body, headers=HEADERS, timeout=20).json()
plr = (
    resp["contents"]["tvBrowseRenderer"]["content"]
        ["tvSurfaceContentRenderer"]["content"]["twoColumnRenderer"]
        ["rightColumn"]["playlistVideoListRenderer"]
)
tiles = plr["contents"]
cont = plr.get("continuations", [{}])[0].get("nextContinuationData", {}).get("continuation")

# Continuation page
while cont:
    cont_body = {"context": {"client": CLIENT_CTX, "user": {}}}
    cd = requests.post(
        f"{URL}&ctoken={cont}&continuation={cont}",
        json=cont_body, headers=HEADERS, timeout=20,
    ).json()
    page = cd["continuationContents"]["playlistVideoListContinuation"]
    tiles.extend(page["contents"])
    cont = page.get("continuations", [{}])[0].get("nextContinuationData", {}).get("continuation")
```

The above pulled all 1,141 tracks for the test account in ~76 round-trips.
