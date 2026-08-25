# Security

## What this container holds

proxyseerr is configured with the **admin API keys** for up to four Sonarr/Radarr
instances, and it attaches them to every request it forwards. Anything that can
reach its ports can therefore do anything those API keys can do — add and delete
media, read your library, change server settings. Treat the proxy's ports with
the same care as the servers behind it.

## How it defends itself

- **Authentication is required by default.** The proxy refuses to start unless
  `PROXY_API_KEY` is set (minimum 16 characters, compared in constant time) or
  `PROXY_ALLOW_ANONYMOUS=true` is set deliberately. Requests without the key get
  401 before anything is forwarded.
- **The upstream API keys never leave the container.** Whatever key a caller
  presents is discarded and replaced with the target instance's own key.
- **Request headers are allowlisted.** Only `Content-Type`, `Accept` and
  `User-Agent` are relayed; cookies, `Authorization`, and forwarding headers are
  dropped rather than passed to a server that trusts this proxy.
- **Response headers are allowlisted too.** No `Set-Cookie`, auth challenges, or
  upstream server banners are relayed back.
- **The forwarded path surface is narrow.** Only `/api/...` and `/ping` are
  forwarded. Traversal segments, backslashes and control characters are refused
  without contacting the upstream.
- **Redirects are not followed**, so an upstream cannot steer the proxy at a
  third-party host.
- **Errors do not leak topology.** Internal URLs and upstream exception text go
  to the log; callers get a generic message. `/proxy/health` reports only
  reachability unless the caller is authenticated.
- **Credentials are redacted from logs**, including any `apikey=` in a URL that
  appears inside an upstream exception message.
- **Request bodies are capped** at `MAX_BODY_BYTES` (8 MB default).
- **The container runs as a non-root user (uid 1000).**

## Deployment guidance

- Keep the proxy on your LAN. It is not built to face the internet; do not port
  forward it. If you must expose it, put it behind a reverse proxy that does TLS
  and its own authentication.
- Talk to it over your trusted network. Traffic between Seerr and the proxy is
  plain HTTP and carries `PROXY_API_KEY`.
- Give each Sonarr/Radarr instance its own API key so one can be rotated alone.
- Never commit a real `.env`, `docker-compose.override.yml`, or an Unraid
  template with your keys filled in. All three are gitignored.

## Reporting a vulnerability

Open a security advisory at
https://github.com/RonFBurgundy/proxyseerr/security/advisories/new rather than a
public issue.
