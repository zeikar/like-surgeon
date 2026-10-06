# Finding likes the YouTube Data API hides

The like count the YouTube app shows (and the likes playlist's `itemCount` from
`playlists.list`) can be higher than what `playlistItems.list` returns. The
missing likes read `getRating = none` through the Data API, are mostly videos
marked made for kids, and still appear in the YouTube web "Liked videos" list
(no "show unavailable videos" option is involved). Scans never see them.
`likesurgeon scan youtube-likes` warns with the difference and links here.

Un-liking and re-liking such a video on youtube.com makes the API list it
again (and YT Music may then show it). The web list can be read through the
InnerTube `browse` endpoint, which tells you which videos to touch.

## Steps

1. Read the web list. POST to
   `https://www.youtube.com/youtubei/v1/browse?prettyPrint=false` with body
   `{"context": {"client": {"clientName": "WEB", "clientVersion": "2.20261001.00.00", "hl": "en"}}, "browseId": "VLLL"}`.
   Send `Cookie` and `User-Agent` from `~/.like-surgeon/browser.json`, plus
   `Origin` / `X-Origin: https://www.youtube.com`, `X-Goog-AuthUser: 0`, and an
   `Authorization: SAPISIDHASH <ts>_<sha1("<ts> <SAPISID> https://www.youtube.com")>`
   computed fresh from the `__Secure-3PAPISID` (or `SAPISID`) cookie. The
   `Authorization` stored in `browser.json` is for the music.youtube.com origin
   and does not work here.
2. Collect every `lockupViewModel` object; its `contentId` is the video id
   (100 per page).
3. Paginate. The continuation token is at
   `continuationItemRenderer ... continuationCommand.token` (in practice the
   last `continuationCommand` that has a `token` in the response; some
   have none). POST the same
   endpoint with the same `context` block plus `"continuation": token`; a
   request without `context` fails. New items arrive under
   `onResponseReceivedActions[].appendContinuationItemsAction.continuationItems`.
   Stop when no token comes back or a page adds no new videos.
4. Diff against the latest YouTube snapshot: find its id with
   `likesurgeon snapshots` (source `youtube_liked_videos`), then
   `likesurgeon export <id> --format json > snap.json`. Web ids missing from
   the snapshot are the hidden likes.
5. For each one, open it on youtube.com, un-like it, then like it again.
6. Re-run `likesurgeon scan youtube-likes`; the warning should be gone.

## Sketch

```python
import hashlib, json, os, time, urllib.request

h = {k.lower(): v for k, v in json.load(open(os.path.expanduser("~/.like-surgeon/browser.json"))).items()}
cookie = h["cookie"]
ua = h.get("user-agent", "Mozilla/5.0")
sapisid = next(
    p.split("=", 1)[1] for p in (c.strip() for c in cookie.split(";"))
    if p.startswith("__Secure-3PAPISID=")
)
URL = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
CTX = {"client": {"clientName": "WEB", "clientVersion": "2.20261001.00.00", "hl": "en"}}


def post(body):
    ts = int(time.time())
    digest = hashlib.sha1(f"{ts} {sapisid} https://www.youtube.com".encode()).hexdigest()
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Cookie": cookie, "User-Agent": ua,
        "Origin": "https://www.youtube.com", "X-Origin": "https://www.youtube.com",
        "X-Goog-AuthUser": "0", "Authorization": f"SAPISIDHASH {ts}_{digest}",
    })
    return json.load(urllib.request.urlopen(req))


def walk(node, key):
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        node = node.values()
    if isinstance(node, (list, type({}.values()))):
        for v in node:
            yield from walk(v, key)


web, body = set(), {"context": CTX, "browseId": "VLLL"}
while True:
    resp = post(body)
    before = len(web)
    web |= {v["contentId"] for v in walk(resp, "lockupViewModel")}
    tokens = [
        t["token"] for t in walk(resp, "continuationCommand")
        if isinstance(t, dict) and "token" in t
    ]
    if not tokens or len(web) == before:
        break
    body = {"context": CTX, "continuation": tokens[-1]}

snap = {t["video_id"] for t in json.load(open("snap.json"))["tracks"]}
print("\n".join(sorted(web - snap)))  # open each at youtube.com/watch?v=<id>
```

## Caveats

- This is an unofficial InnerTube call. The response shape can change; if
  `lockupViewModel` stops matching, look for the video ids elsewhere in the
  JSON.
- Browser cookies go stale within about an hour; re-run
  `likesurgeon auth ytmusic --from-browser chrome` (or your browser) right before trying.
- `browser.json` holds a live session token. Never share it or paste it into
  an issue.
