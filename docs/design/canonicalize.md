# Design: `canonicalize` — move a like to the version the artist page lists

Status: **implemented** (2026-11-15; proposed the same day). A separate command next to `compare-likes` / `sync`; it reuses their YouTube write machinery (confirm, LM-wide check, restore) and their safety invariants unchanged. Builds on the LL → LM model in [ll-lm-alignment.md](ll-lm-alignment.md).

The same discovery runs per playlist in `canonicalize-playlists`, which moves playlist items rather than likes: [playlist-canonicalize.md](playlist-canonicalize.md).

## TL;DR

- A song is often published as several identical audio tracks (ATV): on a single, an album, a compilation. The user liked one; YT Music's artist page lists another. `canonicalize` finds liked ATVs (**A**) missing from their artist's "Show all" songs list or its Top songs while **exactly one** ATV in that list (**B**) has the same title and the same length (±1 s).
- Report mode (default) reads Liked songs (LM) and each liked song's artist pages and saves one finding per such A. Nothing is written. The user reviews it, excludes findings with `skip`, then runs `canonicalize --apply`.
- `--apply` moves each reviewed like from A to B on YouTube: **repoint** (like B, confirm, unlike A) when B isn't liked yet, **merge** (unlike A) when it is. Each action is followed by the same LM-wide check as `sync`; a mismatch re-likes A (restore) and stops the run.
- This is the one place a title picks the target of a write: only in this mode, only inside the liked song's own artist list, only by exact equality after normalization. (Alignment's same-recording check also compares titles, but only to verify pairs that order has already fixed.)

## 1. Model

**A user preference, not drift repair.** `compare-likes` / `sync` fix the backing map: a like YT Music shows as another track. Here nothing is broken. A is liked, playable and shown in LM as itself. The user would just rather hold the like on the version the artist page lists (the album track rather than the single, or the original album rather than a compilation), so the song shows up as liked on the artist page, album page and Top songs.

- **A**: a liked `MUSIC_VIDEO_TYPE_ATV` LM entry whose `videoId` is in none of its artists' songs lists (Show-all list or Top songs).
- **B**: the same recording as it appears in that list: another ATV with an equal title key and a duration within 1 s (§2).
- **Order change, accepted.** Liking B moves it to the top of both Liked videos (LL) and LM (spec ll-lm-alignment §2). A repoint therefore changes the song's position; a merge leaves B where it is and removes A's entry. The plan print says so before anything is written.
- **Out of scope.** Likes on official MVs (OMV) and user uploads (UGC): they aren't ATVs, and an MV shown as its audio track is alignment's business (`rendered_as_other`, `shadow_duplicate`). No LLM or fuzzy judgment in the tool: discovery is deterministic, and the user's review lands as `skip`.

## 2. Discovery

1. **Fresh LM read** (`get_liked_songs`, full list), stored as a snapshot before anything else is fetched (§4.3).
2. **Artist ids**: the distinct `artists[].id` of every liked song, first-seen order.
3. **Per artist id**, the artist page (§3): Top songs ids and the "Show all" songs list (about 150 songs; §7).
4. **Candidate rule**, per distinct liked `videoId` A (A shown more than once in LM is still one candidate):
   - A is `MUSIC_VIDEO_TYPE_ATV`, has at least one artist id and a `duration_seconds`. Liked rows without a `videoId` (unplayable) are ignored.
   - Every one of A's artists was read. If any of them couldn't be (error, §3), A gets **no finding**: B might be in the unread list, or A might be listed there.
   - A is in none of its artists' Show-all lists or Top songs ids. Otherwise A is **listed** (already canonical) and counted as such.
   - The B's: entries of those Show-all lists with a `videoId` ≠ A, `MUSIC_VIDEO_TYPE_ATV`, a `duration_seconds`, title key equal to A's, and `|duration(B) − duration(A)| ≤ 1 s`. The same B reached through two of A's artists, or listed twice, counts once.
   - Exactly one B → a `canonical_version` candidate. Two or more → `canonical_ambiguous`. None → nothing.
   - A shown more than once in LM → the finding is report-only (§4.1): another like also renders as A, and §5.1 would skip it at run time anyway.
   - An artist with no songs list (a plain channel, §3) contributes no B's and doesn't block A.
5. **Evidence flags** on each candidate, for the review: B is in Top songs; B is already liked (B's `videoId` is in LM); how many times LM shows A; a version keyword in A's album (§4.2).

**Title key**: `normalize.normalize_for_match` (NFKC, wave dash → `~`, trimmed, casefolded) with every whitespace run collapsed to one space. Equality only; no substring, no fuzzy score. Example: `キズナミュージック♪ (instrumental)` at 317 s sits in the same list as the 316 s vocal track and is excluded only because its title key differs.

**The scoped exception to "never match by title".** `CLAUDE.md` forbids title matching because the alignment infers *which YouTube like backs an LM entry*, and a title guess there unlikes the wrong video. Here the backing is never inferred: A is A's own like (LM shows A as itself and `getRating(A)` is `like`, §5.1). The title only picks the target B, and only within the liked song's own artist list (about 150 songs by the same artist), never across the library. Both are ATVs with equal title keys and lengths within 1 s. Outside this mode the rule stands.

## 3. Artist page

ytmusicapi's `get_artist` fails on two kinds of pages seen in the dogfood (ytmusicapi 1.12.0 and 1.12.3), so the page is read raw:

```
yt._send_request("browse", {"browseId": channelId})
  ["contents"]["singleColumnBrowseResultsRenderer"]["tabs"][0]
  ["tabRenderer"]["content"]["sectionListRenderer"]["contents"]
```

- **Songs shelf** = the first `musicShelfRenderer` anywhere in that section list (not necessarily the first section).
- **Top songs ids** = `musicShelfRenderer.contents[].musicResponsiveListItemRenderer.playlistItemData.videoId`.
- **Show-all browse id** = `musicShelfRenderer.title.runs[0].navigationEndpoint.browseEndpoint.browseId` → `get_playlist(browseId, limit=None)`; its raw track dicts are the songs list. A shelf without that endpoint yields Top songs ids and an empty songs list.
- **No songs shelf on a plain channel** (`musicVisualHeaderRenderer`) = "no songs list". Not an error: the artist is counted, contributes nothing, and doesn't block A.
- **No songs shelf on any other page** (an artist page's `musicImmersiveHeaderRenderer`, no header, or an unknown one) = the songs list didn't come back. YT Music omits the shelf from artist pages intermittently (ytmusicapi's `get_artist`: "API sometimes does not return songs"); on 2026-11-15 it was missing from 8 of 8 reads of real artist pages (logged in and anonymous; the carousels and description came back), hours after the same pages had it. The page is read once more after 1 s; still missing → a per-artist error (`MissingSongsShelfError`), so A gets no finding rather than being judged against an empty list. After three such retries in a row have failed, the run stops retrying (an outage, where each retry only adds a second) unless a page reads cleanly: once a page returns its songs shelf, retries resume.
- A response without the tabs path, or a failing request, is a per-artist error: recorded with its message, reported as a warning, and the run continues with the other artists. A missing or unusable auth file stops the whole run. The report prints the first 10 artist warnings and a count of the rest (the JSON report lists every error), plus one "re-run later" hint when a songs list was missing.

**Header renderers.** A real artist page has a `musicImmersiveHeaderRenderer`. A plain YouTube channel credited as an artist has a `musicVisualHeaderRenderer` and only carousels (Videos, Playlists), no songs shelf. The parser keys on the shelf and looks at the header only when the shelf is missing. Only the plain-channel header is allow-listed as "no songs list": a missing or renamed header falls on the unreadable side, because "no songs list" doesn't block A and could let a dropped list produce a write-eligible finding.

**The two `get_artist` failures:**
1. Plain channel: `KeyError: 'musicImmersiveHeaderRenderer'`. The raw read gives "no songs list".
2. A headerless `musicCarouselShelfRenderer` ("Watch the new music video" promo) before the songs shelf: `get_artist` reads every carousel's header and raises `KeyError: 'header'`. The raw read skips it and finds the shelf.

Artists are read one after another with a 0.2 s pause and progress on stderr (`Reading artist pages: n/total`, overwritten in place on a terminal; a pipe or log gets only the last line).

## 4. Findings and review

### 4.1 Issue types

| Finding | Source / related | Confidence | Acted on |
|---|---|---|---|
| `canonical_version` | A / B | **1.0** write-eligible; **0.5** report-only (version keyword, or A shown more than once in LM) | 1.0 by `--apply`; 0.5 for the version keyword alone only with `--include-flagged` (§6) |
| `canonical_ambiguous` | A / none | 0.5 | never |

`canonical_version` is one pair type whether or not B is liked: the action is chosen at run time (§5.4), and a `skip` keeps carrying over when B's like state changes. `DiagnosisItem.reason` holds the evidence: both ids, titles, albums and durations, the artist whose list holds B, B in Top songs, B already liked, and any report-only cause (the keyword hit, or "A shown N times in Liked songs"). For `canonical_ambiguous` it lists every B instead. Like everywhere else, `sync` never modifies it.

### 4.2 Version-keyword net

Same title and length can still be a different recording: a live take, a remix, a TV-size edit. When **A's album name** contains one of `Version`, `Ver.`, `Live`, `Remix`, `Acoustic`, `Instrumental`, `Off Vocal`, `TV Size`, the finding is report-only (0.5). Matching runs on the NFKC-normalized album name, is case-insensitive, and lets the words of `Off Vocal` / `TV Size` be joined by spaces, hyphens or nothing (`Off-Vocal`, `TVsize`); a keyword embedded in a longer Latin word doesn't count: no ASCII letter may sit directly before or after it. So `LoveLive! …` and `Alive` stay eligible, while `LIVE2019`, `(Artist Version)` and `Best Live! collection` hit.

A plain substring match was rejected: it catches compilation albums such as `LoveLive! …`, the compilation-to-single case this feature exists for. Even the word-boundary net hits best-of albums (`Best Live! collection` is no live recording), so a finding report-only for the keyword alone can be applied after review with `--apply --include-flagged` (§6); unwanted ones are kept out with `skip`. Dogfood numbers in §7.

### 4.3 Storage

- A report is a `Diagnosis` with `kind = "canonicalize"` (new nullable column; `NULL` = a `compare-likes` run). `latest_diagnosis` defaults to `kind IS NULL`, so `sync`, `doctor` and `issues` without `--type` keep reading the alignment diagnosis; `skip` / `unskip` also accept ids from the latest canonicalize report (§4.4).
- `ytmusic_snapshot_id` = the LM snapshot this run stored; `youtube_snapshot_id` is empty (no YouTube scan is involved, and report mode builds no YouTube client).
- **The LM snapshot** is a regular `ytmusic_liked_songs` snapshot, as `scan ytmusic` would store, committed right after the LM read and **before any artist page is fetched**. Its timestamp is then when LM was observed, so a write made during the minutes of artist-page reads still counts as newer than it. That matters because the snapshot is also the latest YT Music scan: a later `compare-likes` may build on it, and `sync` must then refuse on such a write (§6). It also gives every A a `Track` and an LM position. Being a newer `ytmusic_liked_songs` snapshot, it makes `sync` on an existing alignment diagnosis warn that a newer scan exists and suggest re-running `compare-likes`.
- **B tracks**: each B gets a `Track` (source `ytmusic_liked_songs`, keyed by `videoId`) through `snapshot.py`, which stays the only `Track` writer, so B's row is the one a later scan would use.

### 4.4 Review

- The report prints a summary (liked songs, artist pages read, A's already listed, A's with an unreadable artist, `canonical_version` open write-eligible / total, `canonical_ambiguous`) and the findings table, then points to `skip` and `--apply`.
- `likesurgeon issues --type canonical_version` (or `canonical_ambiguous`) lists the latest canonicalize report under the title "Canonicalize report #N — issues"; without such a report it says to run `canonicalize` first. `issues` without a type still lists the alignment diagnosis.
- `canonicalize --json` prints one JSON document on stdout: `diagnosis_id`, `summary` (`songs`, `listed`, `candidates`, `ambiguous`, `unreadable`), `artist_errors` (artist id → message), `items` (the `issues --format json` rows). Progress, the snapshot line, artist warnings and the carry-over note go to stderr.
- `skip` / `unskip` accept finding ids from the latest alignment diagnosis or the latest canonicalize report, reject ids from older reports, and stay all-or-nothing.
- Carry-over: a new report re-applies earlier `skipped` statuses by `(issue_type, source_track, related_track)` (`carry_over_skipped`, as for `compare-likes`) and says how many it kept.

## 5. Actions

Both run through `sync_dispatch.execute`, YouTube only. One `SyncAttempt` per call, check and skip decision, each write's row committed before any wait.

| Action | When | Calls | Quota |
|---|---|---|---|
| **repoint** | B not liked | getRating A, getRating B, like B, getRating B, unlike A, getRating A | 104 |
| **merge** | B already liked | getRating A, getRating B, unlike A, getRating A | 53 |

A restore (~51) and a B rollback (~51) only run after a failure and aren't estimated.

### 5.1 Prechecks (no writes)

Against the LM read taken before the action (the previous action's matching post-check read, else a fresh read), then live ratings:

1. **LM shows A exactly once**, else `canonical_precheck_lm` skipped, no rating read. Zero means A's like renders as something else; two means another like also renders as A, so "one A fewer" couldn't tell which entry went. Discovery already makes the second case report-only; this is the live guard.
2. **`getRating(A)` = `like`** (`canonical_precheck_a`), else skipped. A may be shown in LM while backed by another video's like. Unreadable → `failed`, item stays `open`, nothing written.
3. **`getRating(B)`** (`canonical_precheck_b`) picks the flow. The row records which: not liked → repoint, liked → merge. Unreadable → `failed`, item stays `open`, nothing written.
4. Merge only: **LM shows B** at least once, else `canonical_precheck_lm` skipped. B's like must render as B for "one A fewer, B still shown" to hold. The row's reason says which check failed (A shown N times, or B not shown).

A stale finding (A unliked since the report, LM changed) ends `skipped`. Because LM shows A as itself, A's availability is known. The "never act on unknown availability" rule, which guards order-derived pairs whose A is only known from a YouTube scan, doesn't arise.

### 5.2 Repoint (B not liked)

1. `videos.rate(B, like)` (`canonical_like`); wait 5 s; `getRating(B)` = `like` (`canonical_verify`).
   - Reads not liked → `skipped` (terminal), A never touched.
   - Unread → B rollback (below), `failed`. The like call errored → B rollback unless B reads not liked, `failed`. A is never touched.
   - Rejected by the quota (nothing sent) → `failed`.
2. Unlike A (§5.4).
3. LM check (`lm_check`). **Expected: one A fewer and one B more** (LM entry count unchanged).

**B rollback.** Whenever A stays liked after B's like may have landed (A restored, A's unlike quota-rejected, B's like erroring or unread), `videos.rate(B, none)` (`canonical_rollback`) and `getRating(B)` = `none` (`canonical_rollback_verify`). If it fails or can't be confirmed, B is reported in `left_liked` and the run stops with `ROLLBACK_STOP`, the stop reason `sync`'s repoint uses too (worded "a B like couldn't be undone" for both).

### 5.3 Merge (B already liked)

1. Unlike A (§5.4).
2. LM check (`lm_check`). **Expected: one A fewer**, B still shown. No `canonical_like` row and no B rollback: this run never liked B.

### 5.4 Unlike, LM check, restore and the run-time flow choice

- **Unlike A.** `videos.rate(A, none)` (`canonical_unlike`); wait 5 s; `getRating(A)` = `none` (`canonical_unlike_verify`). Failures follow `sync_dispatch` (ll-lm-alignment §4.1):
  - Quota-rejected (nothing sent) → `failed`, no restore; repoint: B rollback. The quota stop ends the run after this action.
  - Still reads liked (the call errored without landing, or a 2xx that didn't take) → A is re-liked (`restore_like` / `restore_verify`), LM is checked against "A never unliked", and the run stops as after any restore.
  - Call errored but A reads unliked, or the verify read failed → the LM check decides: as expected → `failed` (A unliked, LM as planned, item `open`), no stop and no B rollback; otherwise → restore and stop.
  - LM unreadable after the unlike → counted as a mismatch: restore (repoint: B rollback), and the run stops. LM unreadable before an action → the run stops before any write; the rest stay `open`, unattempted.
- **LM check.** The full LM is read and compared as a multiset of `videoId` with the expected one, re-read once after 15 s on a mismatch. Counts, not order: B landing on top after a repoint is expected and not asserted. An A that still reads liked after its unlike is re-liked first (a pending unlike can't win later), and LM is then expected to look as if A had never been unliked: repoint `before` + one B, merge `before`.
- **Restore.** On a mismatch, A is re-liked (`restore_like`) and confirmed (`restore_verify`); in the repoint flow, B is then rolled back. The item ends `skipped`, counted in `restored`. A restore that can't be confirmed leaves A in `left_unliked` (stranded). Anything stopping the action once A's unlike call started, Ctrl-C included, leaves an `interrupted` row.
- **Stop on restore.** Any action that needed a restore, whether or not it worked, stops the run: `ExecResult.aborted` = `CANONICALIZE_STOP` (the canonical counterpart of `RESTORED_STOP`, set unless another stop reason already is). The mismatch means LM didn't behave as this model predicts (A's like backed another entry, or B doesn't render as B), so the next action shouldn't run on the same assumption. The rest stay `open`, unattempted.
- **Flow chosen at run time.** The planned kind (`canonical_repoint` / `canonical_merge`) is a forecast from the report's LM for the plan print and quota estimate. The dispatcher runs one flow for both and decides from B's live rating (§5.1 step 3). That is how two A's sharing one B resolve: the first repoints (likes B), the second finds B liked and merges, and LM ends one entry shorter with B shown once. A planned repoint whose B the user has liked since runs as a merge, and vice versa.
- `DiagnosisItem.status`: `applied` only when every call succeeded and the LM check passed; `skipped` is terminal (a precheck failed, B's like didn't land, A was restored); anything else stays `open`.
- **Never** call YT Music `rate_song`: it is the same rating as YouTube's and only duplicates or reverts.

### 5.5 Row kinds

| Row | Read / write | Flow |
|---|---|---|
| `canonical_precheck_lm` | read (LM) | both |
| `canonical_precheck_a` | read | both |
| `canonical_precheck_b` | read | both |
| `canonical_like` | **write** | repoint |
| `canonical_verify` | read | repoint |
| `canonical_unlike` | **write** | both |
| `canonical_unlike_verify` | read | both |
| `canonical_rollback` | **write** | repoint |
| `canonical_rollback_verify` | read | repoint |
| `lm_check`, `restore_like` (**write**), `restore_verify`, `interrupted` (**write**) | shared with `sync` | both |

## 6. Operational rules

1. **`--apply` acts on the saved report**: the latest canonicalize diagnosis, so what was reviewed is what runs. No report → exit 1, naming `canonicalize`. Every action re-reads live state first (§5.1). Rediscovering instead would add minutes of artist-page reads to each write run and could apply candidates nobody reviewed.
2. **Planner.** Open `canonical_version` findings at 1.0 become actions. 0.5, `canonical_ambiguous`, `applied` and `skipped` findings don't, except that `--include-flagged` (opt-in) also takes an open 0.5 `canonical_version` whose A, in the report's LM snapshot, appears exactly once and has a version keyword in its album: the keyword is then its only report-only cause. The cause comes from that snapshot, not from parsing `reason`. The forecast is `canonical_merge` when B is in the report's LM (or an earlier action of this plan likes it), else `canonical_repoint`.
3. **Oldest first** (highest LM position first): each repoint moves B to the top, so the oldest goes first and the moved songs keep their relative order. `--limit N` takes the N oldest.
4. **Gate.** The plan table (id, forecast action, A, B, title, and a Flagged column naming the keyword when `--include-flagged` added an action), counts, quota estimate and order note are printed, plus any stranded videos. Then `--yes` or a prompt, the YouTube write-scope check, and the YT Music auth probe (a failed check exits 1). With nothing to apply, it exits 0 without building a YouTube client. `--json` with `--apply`, and `--limit` / `--yes` / `--include-flagged` without it, exit 2. The run exits 1 when any action failed or it stopped early.
5. **Quota stop.** Quota exhaustion stops the run once the in-flight action has finished its check and any restore.
6. **Stranded reporting per video id.** `canonical_unlike` joins the unlike kinds; with the shared `lm_check`, `restore_like`, `restore_verify` and `interrupted` it is the only canonical row that settles stranded state (`canonical_verify`, `canonical_unlike_verify` and the rollback rows don't). A canonical A's track is a `ytmusic_liked_songs` track, while an alignment A is a `youtube_liked_videos` track, so `stranded_unliked_video_ids` resolves each settling row to its video id and keeps the newest one **per video id across alignment and canonical findings**. A video is stranded only when its newest settling row says it may be unliked, and dropped once a later YouTube scan contains it.
7. **`sync` refuses, `compare-likes` warns.** `canonical_like`, `canonical_unlike` and `canonical_rollback` join `_WRITE_KINDS` (`restore_like` and `interrupted` already are). A canonical write after the older scan of an alignment diagnosis makes `sync` refuse and `compare-likes` warn, exactly like a `sync` write. After `--apply`, re-scan both sources and re-run `compare-likes` before the next `sync`; the result output says so.
8. **`canonicalize --apply` itself doesn't refuse** after earlier writes, its own or `sync`'s. Its findings don't depend on order or alignment; each is about one song. Each action re-checks A's and B's live state and a live LM read before writing.

## 7. Evidence and known limits

**Dogfood** (one account, read-only dogfood scripts, 2026-11-14; 1211 liked songs, 313 distinct artist ids):
- 748 liked songs already canonical (in their artist's list).
- 71 candidates at ±1 s, all ATV: 70 through `get_artist`, plus 1 from a manual parse of the five broken pages. 6 had B already liked (merge), 21 had B in Top songs. Exactly one B was shared by two A's. The second A's album ("μ's Best Album Best Live! Collection Ⅱ") is report-only under the keyword net, so the two-A's-one-B case (repoint then merge) didn't occur among write-eligible findings in this data. The number of ambiguous songs wasn't measured: the script kept only the first B per A. Example: the single "Kizuna Music♪" (ATV, 316 s) → the same track on the album "Breakthrough!" (ATV, 316 s, Top songs #1).
- Version-keyword net (§4.2): 7 of the 71 become report-only, 5 `… Best Live! collection` and 2 `(… Version)`. A plain substring match would catch 17, 10 of them `LoveLive! Sunshine!! Aqours CHRONICLE` rows.
- 63 "artists" were plain YouTube channels (`musicVisualHeaderRenderer`, Videos/Playlists carousels only) behind 140 UGC liked rows: no songs list.
- 5 real artists (HoneyWorks, TOKINOSORA, Mariah Carey, Nogizaka46, Minori Suzuki) broke `get_artist` with `KeyError: 'header'` (headerless promo carousel). The raw read covers them.
- The dogfood script passed `location="KR"`; the client doesn't, so lists may differ slightly by region.

**Known limits:**
- **About 150 songs per Show-all list** (HoneyWorks 150, Mariah Carey 149, Poppin'Party 149). For a larger catalog, A or B may sit past the cap: A may get a finding although listed further down, or B may be missed. Top songs ids count as listed as a small cushion. The review is the guard.
- **Position isn't asserted.** The LM check compares counts; B's move to the top is documented, not verified.
- **A wrong B passes the LM check.** If B were a different recording, "one A fewer, one B more" would still hold. The guards are the candidate rule (same artist list, ATV, equal title key, ±1 s), the version-keyword net and the review. The LM check guards A, the like that gets removed.
- **The songs shelf comes and goes.** An artist page without it is unreadable (§3), so during an omission most artists can be unreadable and the report has few or no findings. The summary's "Audio tracks with an unreadable artist" and "Artist pages read / total" rows show it; re-run later.
- **ytmusicapi's private `_send_request`** carries the artist-page read; an upstream rename breaks it. Every failure is per artist (no finding for its songs) except a missing auth file.

## 8. Docs and code impact

| Area | Change |
|---|---|
| New `canonicalize.py` | title key, `VERSION_KEYWORDS`, `artist_ids`, pure `find_candidates`; `persist_report`; planner `plan` / `summarize`, `flagged_keywords` for `--include-flagged` |
| `ytmusic_client.py` | pure `parse_artist_page`, `ArtistSongs`, `fetch_artist_songs` (raw `browse` + `get_playlist`, per-artist errors) |
| `snapshot.py` | `upsert_ytmusic_track` for B tracks; stays the only `Track` writer |
| `models.py` / `db.py` | nullable `Diagnosis.kind` with an in-place migration; `Diagnosis` / `DiagnosisItem` / `SyncAttempt` docstrings |
| `diagnosis.py` | `DIAGNOSIS_KIND_CANONICALIZE`, the two issue types; `latest_diagnosis(kind=None)` → `kind IS NULL` |
| `sync.py` | `canonical_repoint` / `canonical_merge` in `ActionKind` and `QUOTA_COST` |
| `sync_dispatch.py` | repoint steps shared through a row-kind prefix; `_SyncRun.canonicalize` (flow from B's live rating); `CANONICALIZE_STOP` |
| `sync_preflight.py` | `canonical_unlike` in `_UNLIKE_KINDS`; the three canonical writes in `_WRITE_KINDS`; `stranded_unliked_video_ids` keyed per video id |
| `cli.py` | `canonicalize` (report, `--json`, `--apply`, `--limit`, `--yes`, `--include-flagged`); `issues --type canonical_*`; `skip` / `unskip` across both report kinds |
| `CLAUDE.md` | doc map entry; the title-matching rule scoped with this exception; canonical actions in the write-action list, quota and write kinds |
| README | "Canonicalize (tidy up releases and duplicates)" section; Safety bullets name both writing commands |
| ARCHITECTURE / DEVELOPMENT | two writing commands, data flow, module rows, `diagnoses.kind`, the new issue types; test strategy and the latest-diagnosis snippet (`kind IS NULL`) |

Old diagnoses read as `compare-likes` runs (`kind` NULL). One schema change: the nullable column.
