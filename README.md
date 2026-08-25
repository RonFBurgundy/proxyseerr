# proxyseerr

A small API routing proxy that lets **Seerr** (or Overseerr / Jellyseerr) drive **two Sonarr
instances** through a single server connection — typically a standard English TV instance and a
dedicated Anime instance configured per the [TRaSH Guides](https://trash-guides.info/).

## The problem

Seerr's override rules (`Settings → Services → Rules`) can change a request's root folder, quality
profile and tags, but they **cannot change which Sonarr instance the request is sent to**. Every TV
request is evaluated against whichever server is flagged as the default, so a rule that rewrites the
path to `/data/media/anime` still posts that path to the *English* instance's database. Once a title
has been tracked by the default instance, Seerr caches that instance's series ID and ignores the
override entirely.

Running two Sonarr instances properly — separate release profiles for dual-audio, separate naming
rules, separate download clients — needs real instance switching.

## What proxyseerr does

Seerr sees one Sonarr. The proxy sits behind it and splits the traffic:

| Seerr asks for | proxyseerr does |
| --- | --- |
| the series library | merges both libraries into one list |
| root folders / quality profiles / language profiles / tags | merges both, so the anime options are selectable in Seerr's rules |
| a series add | routes it to the instance that owns the chosen root folder or quality profile |
| a lookup, refresh, search, queue check, episode list | routes it to the instance the series actually lives on |

The trick is a single merged ID space. Sonarr hands out per-instance auto-increment IDs, so English
series 42 and Anime series 42 both exist. Everything the proxy returns from the anime instance is
shifted by `ANIME_ID_OFFSET` (default one billion), and every ID coming back from Seerr is decoded to
`(instance, real id)` — which *is* the routing decision:

```
Seerr sees                       proxyseerr forwards
─────────────────────────────    ──────────────────────────────
rootfolder      1  /data/tv      → ENGLISH  rootfolder 1
rootfolder 1000000001 /data/anime → ANIME    rootfolder 1
qualityprofile  6  HD-1080p      → ENGLISH  profile 6
qualityprofile 1000000006 [Anime] Dual → ANIME profile 6

POST /api/v3/series {rootFolderPath: /data/anime,
                     qualityProfileId: 1000000006}
                                 → ANIME  {qualityProfileId: 6}
GET  /api/v3/series/1000000017   → ANIME  /api/v3/series/17
POST /api/v3/command {seriesId: 1000000017}
                                 → ANIME  {seriesId: 17}
```

External metadata IDs (`tvdbId`, `tmdbId`, `imdbId`) are never touched, so Seerr's matching keeps
working. Tags are resolved by **label**: if a request routed to the anime instance carries a tag that
only exists on the English instance, the same label is created on the anime side rather than dropped.

No dummy root folder in the English instance is required — the merged lists give Seerr the real
anime folders and profiles to pick from.

## Install

### Unraid

Add this as a template repository under **Docker → Template Repositories**:

```
https://github.com/RonFBurgundy/proxyseerr
```

Then add the container from **Add Container → Template → proxyseerr**, or apply
`unraid/proxyseerr.xml` directly. Fill in both Sonarr URLs and API keys. The WebUI button opens
`/proxy/health`, which reports whether both instances are reachable.

### Docker Compose

```bash
cp docker-compose.yml docker-compose.override.yml   # edit URLs and keys
docker compose up -d
```

### Image

```
ghcr.io/ronfburgundy/proxyseerr:latest
```

Multi-arch (`linux/amd64`, `linux/arm64`), built and published by GitHub Actions on every push to
`main` and on `v*` tags.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENGLISH_SONARR_URL` | `http://localhost:8989` | Standard TV instance. This is the fallback for anything not matched as anime. |
| `ENGLISH_SONARR_API_KEY` | — | Required. |
| `ANIME_SONARR_URL` | `http://localhost:8987` | Anime instance. |
| `ANIME_SONARR_API_KEY` | — | Required. |
| `PROXY_PORT` | `5000` | Port Seerr connects to. |
| `PROXY_API_KEY` | *(unset)* | If set, requests must carry this key. Use the same value as the API key in Seerr's Sonarr settings. |
| `ANIME_ID_OFFSET` | `1000000000` | Size of the anime ID shift. Changing it after titles are tracked invalidates the IDs stored in Seerr. |
| `ANIME_ROOT_FOLDER_MATCH` | `anime` | Fallback keyword for paths not owned by a known instance. |
| `ANIME_LABEL_PREFIX` | `[Anime] ` | Prefix on anime profile names in Seerr's dropdowns. Root folder paths are never decorated. |
| `UPSTREAM_TIMEOUT` | `20` | Seconds before an upstream call is abandoned. |
| `LOG_LEVEL` | `INFO` | `INFO` logs one line per routing decision. |

## Seerr setup

1. **Start the container** and check `http://<host>:5000/proxy/health` — both instances should report
   `"reachable": true`.
2. **In Seerr → Settings → Services**, delete the second (anime) Sonarr server. Only one server
   entry should remain.
3. **Edit the remaining server** to point at the proxy (`<host>` / port `5000`). If you set
   `PROXY_API_KEY`, enter it as the API key; otherwise any value works. Save — the root folder and
   quality profile dropdowns should now list entries from *both* instances, with the anime ones
   prefixed `[Anime]`.
4. **Set the server defaults** to your English root folder and profile.
5. **Add override rules** under `Settings → Services → Rules`:
   - *Original Language is Japanese* → set root folder to the anime path **and** quality profile to
     an `[Anime] …` profile.
   - *Keyword contains anime* → the same targets.

   Either field alone is enough to route the request; setting both is belt-and-braces.
6. **Test with a fresh title** and watch the log:
   `Routing ADD 'One Piece' to ANIME Sonarr (root folder /data/media/anime exists only on the anime instance)`.

### Titles already tracked on the wrong instance

Seerr caches the instance-side series ID, so anything previously added keeps pointing at the English
instance. For each affected title: delete the series from the English Sonarr (keep the files if you
plan to re-import them on the anime side), delete the request in Seerr, then request it again.

## Routing rules, in order

1. A namespaced `qualityProfileId`, `languageProfileId`, `rootFolderId` or `id` in the payload.
2. A `rootFolderPath` that exists on exactly one of the two instances.
3. `seriesType: "anime"` in the payload.
4. `rootFolderPath` containing `ANIME_ROOT_FOLDER_MATCH`.
5. Otherwise the English instance.

Every decision is logged with its reason, so a misrouted title can be diagnosed from the container
log alone.

## Behaviour notes

- If one instance is unreachable, merged reads return what the other one gave rather than failing —
  a temporarily offline anime instance shows as a partial library, not an empty one.
- A title present on **both** instances appears twice in the merged library, and a warning naming its
  `tvdbId` is logged. Keep each show on one instance.
- Commands with no series or episode ID (e.g. `RefreshMonitoredDownloads`) are sent to both.
- `POST /api/v3/tag` creates the tag on the English instance; the anime side gets the same label the
  first time a request carrying that tag is routed there.
- Anything not explicitly handled falls through to a catch-all that still decodes IDs in the path and
  query string, so unknown Sonarr endpoints route correctly.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m proxyseerr          # serves on PROXY_PORT with waitress
```

Or without a local Python:

```bash
docker run --rm -v "$PWD":/src -w /src python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

| Module | Responsibility |
| --- | --- |
| `proxyseerr/config.py` | environment → `Settings` |
| `proxyseerr/namespace.py` | ID encode/decode, per-resource field lists |
| `proxyseerr/upstream.py` | HTTP to Sonarr, header hygiene |
| `proxyseerr/routing.py` | which instance owns this request, tag translation |
| `proxyseerr/service.py` | merge, translate, forward |
| `proxyseerr/app.py` | the Sonarr v3 route surface |
