# proxyseerr

A small API routing proxy that lets **Seerr** drive **two Sonarr instances and two Radarr
instances** through a single server connection each — typically a standard
English library and a dedicated Anime library configured per the
[TRaSH Guides](https://trash-guides.info/).

> **Compatibility.** Built and verified against [Seerr](https://github.com/seerr-team/seerr)'s
> current Sonarr/Radarr API client, talking to Sonarr and Radarr **v4**. Overseerr and Jellyseerr
> use closely related clients and may well work, but they are untested — if you try one, an issue
> saying what happened is welcome.

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

## Setup

Follow these in order. Each **Check** step tells you what you should see — if you
don't see it, stop there rather than continuing, and the Troubleshooting section
below names the likely cause.

### Before you start

- Two Sonarr instances, two Radarr instances, or both pairs. One pair alone is fine.
- The **API key** for each instance: its *Settings → General → Security → API Key*.
- The **LAN IP of the machine each instance runs on**, and each one's port.
- Distinct root folders per pair, e.g. `/data/media/tv` and `/data/media/anime`. Two
  instances sharing a root folder path cannot be told apart by path.

### Step 1 — Generate the proxy's own API key

```bash
openssl rand -hex 24
```

Keep it somewhere you can paste from twice. The proxy holds admin keys for every
instance, so it refuses to start without one of its own.

### Step 2 — Install the container

**Unraid.** Recent versions removed the *Template Repositories* field, so fetch the
template onto the box. From the Unraid terminal:

```bash
curl -o /boot/config/plugins/dockerMan/templates-user/my-proxyseerr.xml \
  https://raw.githubusercontent.com/RonFBurgundy/proxyseerr/main/unraid/proxyseerr.xml
```

Then **Docker → Add Container → Template → my-proxyseerr**, and fill in:

| Field | Value |
| --- | --- |
| Proxy API Key | what Step 1 printed |
| English Sonarr URL / API Key | your standard TV instance |
| Anime Sonarr URL / API Key | your anime TV instance |
| English Radarr URL / API Key | your standard movie instance, or leave all four blank |
| Anime Radarr URL / API Key | your anime movie instance, or leave all four blank |

> **The mistake everyone makes:** the container is on a bridge network, so `localhost`
> inside it means *the container*, not your server. Every URL must use the host's LAN
> IP — `http://192.168.x.x:8989`, not `http://localhost:8989`.

Leave everything else at its defaults. **No volumes are needed**: the proxy holds no
state, so there is nothing to map and nothing to back up.

Passing the raw URL to `…/Docker/AddContainer?xmlTemplate=<url>` does *not* work; that
parameter only reads templates already on the server.

**Docker Compose.** Alternatively:

```bash
cp .env.example .env      # fill in your keys; .env is gitignored
docker compose up -d
```

### Step 3 — Check: is the proxy up?

Open `http://<your-server>:5000/proxy/health?apikey=<your key>`. You want:

```json
{"service": "sonarr", "status": "ok",
 "instances": {"english": {"reachable": true, ...},
               "anime":   {"reachable": true, ...}}}
```

Then read the container log. Every instance should be listed with its name and version:

```
[INFO] Authentication: API key required | request log: errors | anime ID offset: 1000000000
[INFO] ENGLISH Sonarr -> http://sonarr.example.lan:8989 (Sonarr, v4.0.19.2979)
[INFO] ANIME Sonarr   -> http://sonarr-anime.example.lan:8987 (Sonarr Anime, v4.0.19.2979)
[INFO] sonarr proxy listening on 0.0.0.0:5000 (anime ID offset 1000000000)
```

**Do not continue** until every instance says `reachable` and no line says *is NOT
responding*. If the container exited instead of starting, the log's last line says
exactly which setting is wrong.

### Step 4 — Point Seerr at the proxy

In **Seerr → Settings → Services**:

1. **Delete the second Sonarr server** and the second Radarr server. Exactly one of
   each must remain, flagged as default.
2. **Edit the remaining Sonarr**: hostname = your server's LAN IP, port **5000**, SSL
   off, API key = the key from Step 1. Save.
3. **Edit the remaining Radarr** the same way on port **5001**, if you configured that pair.

### Step 5 — Check: did the merge work?

Still in that server's settings, open the **Root Folder** and **Quality Profile**
dropdowns. You should see entries from *both* instances, with the anime ones prefixed
`[Anime]`.

This is the step that proves the whole thing works. If you only see one instance's
entries, stop — the anime instance is not answering, and the container log will name it.

Then set the server's **defaults** to your English root folder and profile.

### Step 6 — Add your override rules

Under **Settings → Services → Rules**, add:

| Condition | Set root folder to | Set quality profile to |
| --- | --- | --- |
| Original Language **is** Japanese | your anime path | an `[Anime] …` profile |
| Keyword **contains** anime | your anime path | an `[Anime] …` profile |

Either field alone is enough to route the request. Setting both is belt-and-braces.

### Step 7 — Check: request something

Request an anime title, then a normal English one, and watch the container log:

```
Routing ADD 'Sailor Moon' to ANIME Sonarr (qualityProfileId is in the anime ID range)
Routing ADD 'Family Guy' to ENGLISH Sonarr (root folder /data/media/tv exists only on the english instance)
```

Both lines name the instance *and* why it was chosen. If a title went the wrong way,
that reason tells you which rule to fix.

### Moving titles already on the wrong instance

Seerr caches the instance-side ID, so anything requested before the proxy existed keeps
pointing at whichever instance had it. The proxy cannot see or fix that — it only sees
new requests. Per affected title: delete it from the wrong Sonarr/Radarr (keep the files
if you plan to re-import them), delete the request in Seerr, then request it again.

## Configuration reference

Each service gets its own port, because Seerr configures them as separate servers and
both speak the same `/api/v3/...` paths:

| Service | Default port | Configure with |
| --- | --- | --- |
| Sonarr pair | 5000 | `ENGLISH_SONARR_URL` / `_API_KEY`, `ANIME_SONARR_URL` / `_API_KEY` |
| Radarr pair | 5001 | `ENGLISH_RADARR_URL` / `_API_KEY`, `ANIME_RADARR_URL` / `_API_KEY` |

Configure one pair or both. A pair with all four variables blank simply isn't served; a
pair with *some* of them set is a startup error rather than a half-working proxy.

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
| `ANIME_ROOT_FOLDER_MATCH` | `anime` | Fallback keyword for paths not owned by a known instance. Set empty to disable the fallback. |
| `ANIME_LABEL_PREFIX` | `[Anime] ` | **Cosmetic only.** Prefixes anime profile *names* in Seerr's dropdowns. Set empty to turn off — see below. |
| `UPSTREAM_TIMEOUT` | `20` | Seconds to wait for an instance to *respond*. Raise it on slow arrays — see Troubleshooting. |
| `UPSTREAM_CONNECT_TIMEOUT` | `5` | Seconds to wait to *connect*. Kept short so a wrong host or port fails fast instead of holding a thread; capped at `UPSTREAM_TIMEOUT`. |
| `MAX_BODY_BYTES` | `8388608` | Largest request body accepted. |
| `REQUEST_LOG` | `errors` | `errors` logs every failed request, `all` logs every request, `off` disables the access log. |
| `LOG_LEVEL` | `INFO` | `INFO` logs one line per routing decision. |

### Image tags

Multi-arch (`linux/amd64`, `linux/arm64`), built and published by GitHub Actions.

| Tag | Moves when | Use it if |
| --- | --- | --- |
| `latest` | a new release is tagged, and weekly for base-image fixes | you want releases automatically |
| `0.1.5` | never | you want a fixed version you can roll back to |
| `0.1` | a patch release in that line | you want fixes but not feature changes |
| `main` | every push to `main` | you are testing unreleased work |

`latest` deliberately does **not** follow `main`. A push to `main` publishes only the
`main` tag, so pulling `latest` can never hand you a development build.

### What `ANIME_LABEL_PREFIX` does and does not do

It rewrites the `name` field of the anime instance's quality and language profiles in the
response Seerr renders, and nothing else. It is **never sent upstream**: adds carry
`qualityProfileId`, an integer, so a profile's name cannot reach Sonarr or Radarr, become a
tag, or influence naming, download clients or release matching. Root folder paths are
excluded on purpose, because Seerr echoes those back verbatim in the add payload.

Set it to an empty value to switch it off — worth doing if your anime profiles already have
names you can tell apart at a glance.

Sonarr/Radarr **tags** are a separate thing entirely: the proxy merges `/api/v3/tag` without
decorating anything, and only ever creates a tag when Seerr sends one that exists on the other
instance. Configure no tags in Seerr and nothing is tagged.

## Security

**The proxy holds admin API keys for every instance and attaches them to each forwarded
request.** Anything that can reach its ports can do anything those keys can do. So:

- `PROXY_API_KEY` is **required** — the container refuses to start without it. To run open
  on a trusted LAN anyway, set `PROXY_ALLOW_ANONYMOUS=true` explicitly.
- Keep it on your LAN. Don't port forward it.

`SECURITY.md` lists the rest: header allowlists in both directions, the restricted forwarding
surface, credential redaction in logs, and what `/proxy/health` will and won't tell an
unauthenticated caller.

## How the proxy picks an instance

Not to be confused with **Seerr's override rules**, which are a different layer. Those decide
*what parameters Seerr sends* — a root folder, a quality profile, tags. They cannot choose a
server, which is the reason this proxy exists.

The proxy then takes whatever arrives and decides *which instance receives it*, using the first
of these checks that answers:

| # | Check | Typical source |
| --- | --- | --- |
| 1 | A namespaced `qualityProfileId`, `languageProfileId`, `rootFolderId` or `id` | a Seerr rule that sets an `[Anime]` profile |
| 2 | A `rootFolderPath` that exists on exactly one instance | a Seerr rule that sets the anime root folder, or your server defaults |
| 3 | `seriesType: "anime"` (Sonarr only) | Seerr's own metadata |
| 4 | `rootFolderPath` containing `ANIME_ROOT_FOLDER_MATCH` | fallback for a path neither instance claims |
| 5 | Otherwise, the English instance | nothing matched |

Check 2 needs to read both instances' root folders. If either cannot be read, it is skipped with
a warning rather than guessed at — an instance that cannot be asked is not an instance that
answered "no".

Every decision is logged with the check that produced it, so a misrouted title can be diagnosed
from the container log alone:

```
Routing ADD 'Sailor Moon' to ANIME Sonarr (qualityProfileId is in the anime ID range)
Routing ADD 'Family Guy' to ENGLISH Sonarr (root folder /data/media/tv exists only on the english instance)
```

## Operations

**The proxy holds no state.** No volumes, no database, nothing to back up or
corrupt. Its only memory is a 60-second cache of root folders and tags. A restart —
clean, unclean, or a full host reboot — loses nothing, and the ID mapping is a pure
function of `ANIME_ID_OFFSET`, so the IDs stored in Seerr keep meaning the same thing
across restarts.

**Start order does not matter.** On a reboot everything comes up at once and the proxy
will usually win the race. It probes each instance at startup and warns about any that
are not answering, then carries on serving; requests succeed as soon as the instance is
up. A failed lookup is never cached, so nothing is stuck waiting out a TTL.

**When one instance is down, behaviour is deliberately asymmetric:**

| Request | One instance down | Both down |
| --- | --- | --- |
| Library (`/series`, `/movie`) | **502** — a half library tells Seerr that everything on the missing instance is gone | 502 |
| Root folders, profiles, tags | 200 with what the reachable instance has, logged as incomplete | 502 |
| An add routed to the *up* instance | works normally | 502 |
| An add routed to the *down* instance | 502, naming the instance | 502 |
| Root-folder routing | falls back to the keyword rule, with a warning — "only on the English instance" is not a safe conclusion when the anime instance cannot be asked | as one down |

The one rule to take from that table: the proxy never turns "I cannot see an instance"
into "that instance does not have it".

**Two things must not change after titles are tracked.** Seerr stores the namespaced IDs
in its own database, so changing `ANIME_ID_OFFSET`, or swapping which URL is English and
which is anime, silently repoints every stored ID. The startup log prints each
instance's name from its own settings so a swap is visible immediately:

```
[INFO] ENGLISH Sonarr -> http://sonarr.example.lan:8989 (Sonarr, v4.0.19.2979)
[INFO] ANIME Sonarr   -> http://sonarr-anime.example.lan:8987 (Sonarr Anime, v4.0.19.2979)
```

**Staying current.** Releases are built from a fresh base image, and the newest release is
rebuilt weekly so security fixes to Debian and Python reach you without waiting for new
code. Version tags stay immutable — `0.1.4` is always the same image — so `latest` is the
tag that receives those rebuilds. Dependabot keeps the Python dependencies and GitHub
Actions current, and CI gates every release.

## Troubleshooting

**A title went to the wrong instance.** Every add logs its reasoning — find the
`Routing ADD '<title>' to …` line and it names which rule fired. If it says
`default instance`, none of your rules matched: check that the Seerr rule sets the
anime root folder *or* an `[Anime]` quality profile.

**Timeouts, or a library that comes back partial.** On an Unraid array with spun-down
disks, or a library of several thousand titles, an instance can take longer to answer
than the 20 second default — and each merged read waits on both instances in turn. The
proxy warns you before it becomes a failure:

```
[WARNING] ANIME Sonarr took 11.4s for GET /api/v3/series (timeout is 20s). If this
          keeps climbing, raise UPSTREAM_TIMEOUT - a spun-down array or a large
          library can outlast the default.
```

If you see that, set `UPSTREAM_TIMEOUT` to `45`. Connecting is timed separately and
stays short, so raising it does not slow down detection of a wrong URL.

**`[Anime]` profiles missing from Seerr's dropdowns.** The anime instance did not
answer the profile read. Check `/proxy/health`, then the log for a line naming it.

**Seerr says the server is down but the anime instance is fine.** Seerr's connection
test hits `/api/v3/system/status`, which goes to the English instance only.

**`/api/v3/languageprofile` returning 404 on Sonarr v4.** Expected — v4 replaced
language profiles with Custom Formats, so both instances legitimately 404 and the proxy
passes that status through. Nothing is broken, and an add payload without a
`languageProfileId` is forwarded untouched.

## Logging

The proxy is built so that nothing degrades quietly — every path that drops,
truncates or rejects something writes a line naming the instance involved.

At startup it reports the auth mode, the ID offset, and probes every configured
instance, so an unreachable server is visible immediately rather than at the
first failed request:

```
[INFO ] Authentication: API key required | request log: errors | anime ID offset: 1000000000
[INFO ] ENGLISH Sonarr -> http://sonarr.example.lan:8989 (v4.0.15.2941)
[WARNING] ANIME Sonarr -> http://sonarr-anime.example.lan:8987 is NOT responding. Requests routed there
          will fail and merged reads will be missing its titles until it returns.
[INFO ] sonarr proxy listening on 0.0.0.0:5000 (anime ID offset 1000000000)
```

While running, these are logged:

| Event | Level |
| --- | --- |
| Routing decision for an add or command, with the reason | INFO |
| **A delete**, naming the instance and whether `deleteFiles` was set — always, unless `REQUEST_LOG=off` | INFO |
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

- Partial and total outages are handled differently per endpoint — see **Operations** above.
- A title present on **both** instances appears twice in the merged library, and a warning naming its
  `tvdbId`/`tmdbId` is logged. Keep each title on one instance.
- Commands with no title ID (e.g. `RefreshMonitoredDownloads`) are sent to both instances.
- `POST /api/v3/tag` creates the tag on the English instance; the anime side gets the same label the
  first time a request carrying that tag is routed there.
- Seerr's connection test hits `/api/v3/system/status`, which goes to the English instance. If that
  instance is down, Seerr reports the server as down even when the anime instance is healthy.
- Paths are matched case-insensitively, because Sonarr and Radarr are (they run on ASP.NET)
  and Seerr relies on it — it asks for `/qualityProfile` but `/rootfolder`.
- **Queue pagination is per-instance.** Each instance paginates its own queue, so the merged
  result is page *n* of both stitched together. With a queue of a few dozen items — the normal
  case — this is invisible; with hundreds, later pages will not line up.
- **If both instances share the same root folder path**, ownership is ambiguous: routing falls
  through to the `ANIME_ROOT_FOLDER_MATCH` keyword and then to the English instance. Give the two
  instances distinct root folders.
- Each merged read makes two upstream calls **sequentially**, so its latency is roughly the sum of
  both instances' response times. At home-library scale that is tens of milliseconds; the proxy
  itself holds no state and caches only root folders and tags, for 60 seconds.
- Anything not explicitly handled falls through to a catch-all that still decodes IDs in the path and
  query string, and consults the request body for ID fields when neither carries one — that last part
  is what makes Seerr's `PUT /episode/monitor` reach the right instance.

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

## License

MIT — see [LICENSE](LICENSE). Provided as is, with no warranty; it talks to your
media servers with admin credentials, so read `SECURITY.md` before exposing it.
