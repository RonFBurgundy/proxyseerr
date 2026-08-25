# proxyseerr

A small API routing proxy that lets **Seerr** (or Overseerr / Jellyseerr) drive **two Sonarr
instances and two Radarr instances** through a single server connection each — typically a standard
English library and a dedicated Anime library configured per the
[TRaSH Guides](https://trash-guides.info/).

## The problem

Seerr's override rules (`Settings → Services → Rules`) can change a request's root folder, quality
profile and tags, but they **cannot change which instance the request is sent to**. Every request is
evaluated against whichever server is flagged as the default, so a rule that rewrites the path to
`/data/media/anime` still posts that path to the *English* instance's database. Once a title has been
tracked by the default instance, Seerr caches that instance's ID and ignores the override entirely.

Running two instances properly — separate release profiles for dual-audio, separate naming rules,
separate download clients — needs real instance switching.

## What proxyseerr does

Seerr sees one Sonarr and one Radarr. The proxy sits behind them and splits the traffic:

| Seerr asks for | proxyseerr does |
| --- | --- |
| the library | merges both instances into one list |
| root folders / quality profiles / language profiles / tags | merges both, so the anime options are selectable in Seerr's rules |
| an add | routes it to the instance that owns the chosen root folder or quality profile |
| a lookup, refresh, search, queue check, episode or file list | routes it to the instance the title actually lives on |

The trick is a single merged ID space. Sonarr and Radarr hand out per-instance auto-increment IDs, so
English series 42 and Anime series 42 both exist. Everything the proxy returns from the anime
instance is shifted by `ANIME_ID_OFFSET` (default one billion), and every ID coming back from Seerr
is decoded to `(instance, real id)` — which *is* the routing decision:

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

No dummy root folder in the English instance is required — the merged lists give Seerr the real anime
folders and profiles to pick from.

## Sonarr and Radarr in one container

Each service gets its own port, because Seerr configures them as separate servers and both speak the
same `/api/v3/...` paths:

| Service | Default port | Configure with |
| --- | --- | --- |
| Sonarr pair | 5000 | `ENGLISH_SONARR_URL` / `_API_KEY`, `ANIME_SONARR_URL` / `_API_KEY` |
| Radarr pair | 5001 | `ENGLISH_RADARR_URL` / `_API_KEY`, `ANIME_RADARR_URL` / `_API_KEY` |

Configure one pair or both. A service with all four variables blank simply isn't served; a service
with *some* of them set is a startup error rather than a half-working proxy.

## Security

**The proxy holds admin API keys for every instance and attaches them to each forwarded request.**
Anything that can reach its ports can do anything those keys can do. So:

- `PROXY_API_KEY` is **required** — the container refuses to start without it. Generate one with
  `openssl rand -hex 24` and enter the same value as the API key for both servers in Seerr. To run
  open on a trusted LAN anyway, set `PROXY_ALLOW_ANONYMOUS=true` explicitly.
- Keep it on your LAN. Don't port forward it.

`SECURITY.md` lists the rest: header allowlists in both directions, the restricted forwarding
surface, credential redaction in logs, and what `/proxy/health` will and won't tell an
unauthenticated caller.

## Install

### Unraid

Add this as a template repository under **Docker → Template Repositories**:

```
https://github.com/RonFBurgundy/proxyseerr
```

Then add the container from **Add Container → Template → proxyseerr**, or apply
`unraid/proxyseerr.xml` directly. Fill in the proxy API key plus whichever service pairs you use. The
WebUI button opens `/proxy/health`.

### Docker Compose

```bash
cp .env.example .env      # fill in keys; .env is gitignored
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
| `PROXY_API_KEY` | — | **Required** (min 16 chars) unless `PROXY_ALLOW_ANONYMOUS=true`. The key Seerr must present. |
| `PROXY_ALLOW_ANONYMOUS` | `false` | Deliberately accept unauthenticated callers. |
| `ENGLISH_SONARR_URL` / `_API_KEY` | — | Standard TV instance; the fallback for anything not matched as anime. |
| `ANIME_SONARR_URL` / `_API_KEY` | — | Anime TV instance. |
| `ENGLISH_RADARR_URL` / `_API_KEY` | — | Standard movie instance. |
| `ANIME_RADARR_URL` / `_API_KEY` | — | Anime movie instance. |
| `SONARR_PROXY_PORT` | `5000` | Also accepts the legacy `PROXY_PORT`. |
| `RADARR_PROXY_PORT` | `5001` | Must differ from the Sonarr port. |
| `ANIME_ID_OFFSET` | `1000000000` | Size of the anime ID shift. Changing it after titles are tracked invalidates the IDs stored in Seerr. |
| `ANIME_ROOT_FOLDER_MATCH` | `anime` | Fallback keyword for paths not owned by a known instance. |
| `ANIME_LABEL_PREFIX` | `[Anime] ` | Prefix on anime profile names in Seerr's dropdowns. Root folder paths are never decorated. |
| `UPSTREAM_TIMEOUT` | `20` | Seconds before an upstream call is abandoned. |
| `MAX_BODY_BYTES` | `8388608` | Largest request body accepted. |
| `REQUEST_LOG` | `errors` | `errors` logs every failed request, `all` logs every request, `off` disables the access log. |
| `LOG_LEVEL` | `INFO` | `INFO` logs one line per routing decision. |

## Seerr setup

1. **Start the container** and open `http://<host>:5000/proxy/health?apikey=<your key>` — every
   instance should report `"reachable": true`.
2. **In Seerr → Settings → Services**, delete the second (anime) Sonarr server and the second Radarr
   server. One entry each should remain.
3. **Edit the remaining servers**: Sonarr at `<host>:5000`, Radarr at `<host>:5001`, both with
   `PROXY_API_KEY` as the API key. Save — the root folder and quality profile dropdowns should now
   list entries from *both* instances, with the anime ones prefixed `[Anime]`.
4. **Set the server defaults** to your English root folder and profile.
5. **Add override rules** under `Settings → Services → Rules`:
   - *Original Language is Japanese* → set root folder to the anime path **and** quality profile to
     an `[Anime] …` profile.
   - *Keyword contains anime* → the same targets.

   Either field alone is enough to route the request; setting both is belt-and-braces.
6. **Test with a fresh title** and watch the log:
   `Routing ADD 'One Piece' to ANIME Sonarr (root folder /data/media/anime exists only on the anime instance)`.

### Titles already tracked on the wrong instance

Seerr caches the instance-side ID, so anything previously added keeps pointing at the English
instance. For each affected title: delete it from the English Sonarr/Radarr (keep the files if you
plan to re-import them on the anime side), delete the request in Seerr, then request it again.

## Routing rules, in order

1. A namespaced `qualityProfileId`, `languageProfileId`, `rootFolderId` or `id` in the payload.
2. A `rootFolderPath` that exists on exactly one of the two instances.
3. `seriesType: "anime"` in the payload (Sonarr only).
4. `rootFolderPath` containing `ANIME_ROOT_FOLDER_MATCH`.
5. Otherwise the English instance.

Every decision is logged with its reason, so a misrouted title can be diagnosed from the container
log alone.

## Logging

The proxy is built so that nothing degrades quietly — every path that drops,
truncates or rejects something writes a line naming the instance involved.

At startup it reports the auth mode, the ID offset, and probes every configured
instance, so an unreachable server is visible immediately rather than at the
first failed request:

```
[INFO ] Authentication: API key required | request log: errors | anime ID offset: 1000000000
[INFO ] ENGLISH Sonarr -> http://10.0.0.5:8989 (v4.0.15.2941)
[WARNING] ANIME Sonarr -> http://10.0.0.5:8987 is NOT responding. Requests routed there
          will fail and merged reads will be missing its titles until it returns.
[INFO ] sonarr proxy listening on 0.0.0.0:5000 (anime ID offset 1000000000)
```

While running, these are logged:

| Event | Level |
| --- | --- |
| Routing decision for an add or command, with the reason | INFO |
| **An instance rejecting a forwarded request**, with the status and its own error message | WARNING |
| An instance unreachable or answering a merge read with an error | WARNING |
| A merge read getting an unexpected payload shape | WARNING |
| A tag that could not be created on the target instance and was dropped | WARNING |
| A title present on both instances | WARNING |
| A request body that claimed to be JSON but was not | WARNING |
| A rejected unauthenticated request, refused path, or oversized body | WARNING |
| Any unhandled error, with traceback | ERROR |
| Every failed request, one line: `POST /api/v3/series -> 400 via ANIME Sonarr in 42ms` | WARNING |
| Every *successful* request, same format | only when `REQUEST_LOG=all` |

The one that matters most is the second row. An add rejected by Sonarr — wrong
quality profile, a root folder that instance does not have — is passed back to
Seerr with its original status, and it is also recorded here with the upstream's
own explanation:

```
[WARNING] ANIME Sonarr rejected POST /api/v3/series with HTTP 400: Invalid quality profile
[WARNING] POST /api/v3/series -> 400 via ANIME Sonarr in 61ms
```

API keys are redacted from every log line, including inside upstream exception
messages that embed the request URL. Set `REQUEST_LOG=all` while setting things
up, then drop back to `errors`.

## Behaviour notes

- If one instance is unreachable, merged reads return what the other one gave rather than failing —
  a temporarily offline anime instance shows as a partial library, not an empty one, and the log
  says the result is incomplete.
- If *both* instances are unreachable, merged reads answer **502 rather than an empty list**. An
  empty library is a lie Seerr would act on; an error makes it keep what it already knows.
- A title present on **both** instances appears twice in the merged library, and a warning naming its
  `tvdbId`/`tmdbId` is logged. Keep each title on one instance.
- Commands with no title ID (e.g. `RefreshMonitoredDownloads`) are sent to both instances.
- `POST /api/v3/tag` creates the tag on the English instance; the anime side gets the same label the
  first time a request carrying that tag is routed there.
- Seerr's connection test hits `/api/v3/system/status`, which goes to the English instance. If that
  instance is down, Seerr reports the server as down even when the anime instance is healthy.
- Anything not explicitly handled falls through to a catch-all that still decodes IDs in the path and
  query string, so unknown endpoints route correctly — within the `/api/...` surface the proxy is
  willing to forward at all.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m proxyseerr          # serves each configured pair on its port
```

Or without a local Python:

```bash
docker run --rm -v "$PWD":/src -w /src python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

| Module | Responsibility |
| --- | --- |
| `proxyseerr/kinds.py` | what differs between a Sonarr pair and a Radarr pair |
| `proxyseerr/config.py` | environment → validated `Settings` |
| `proxyseerr/namespace.py` | ID encode/decode |
| `proxyseerr/upstream.py` | HTTP to the instances, header allowlists, redaction |
| `proxyseerr/routing.py` | which instance owns this request, tag translation |
| `proxyseerr/service.py` | merge, translate, forward |
| `proxyseerr/app.py` | the v3 route surface, auth, path allowlist |
| `proxyseerr/healthcheck.py` | container healthcheck entrypoint |
