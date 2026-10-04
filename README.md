# Nexus Mobile

Standalone FastAPI acquisition service for mobile raw evidence. It contains no Stage 0 scoring, Stage 1 ranking, Stage 2 trading analysis, order execution, or Stage 3 engine.

## Routes

- `GET /mobile`
- `POST /mobile/stage0/raw`
- `POST /mobile/stage1/raw`
- `POST /mobile/stage2/raw`

No production trading routes or Zerodha callback/login routes are registered. The API is intended to be reachable only through a private network such as Tailscale.

## Local Development

Requires Python 3.12 or 3.13.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
python -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
```

Open `http://127.0.0.1:8000/mobile` locally. Set the Kite API key and secret in the untracked `secrets/` files (or `.env` for local non-Docker development), then use the local Zerodha login control to authenticate. Stage 1 needs outbound NSE access but no Kite session.

## Configuration and Secrets

### Non-Secret Configuration (`.env`)

Supported variables:

- `MOBILE_AUTH_SESSION_PATH` (default `data/auth/zerodha_session.json` locally; Compose uses `/var/lib/nexus-mobile/auth/zerodha_session.json`)
- `STAGE1_ARCHIVE_PATH` (default `data/stage1/runs` locally; container path `/var/lib/nexus-mobile/stage1/runs`)
- `LOG_LEVEL` (default `INFO`)
- `HOST` (default `0.0.0.0`)
- `PORT` (default `8000`)
- `HOST_PORT` (Compose host-side localhost port, default `8000`)
- `NEXUS_BIND_ADDRESS` (default `127.0.0.1`; do not change to a public interface)
- `REQUEST_TIMEOUT_SECONDS` (default `10`)
- `NSE_RETRIES` (default `2`)

### Secret Credentials (`secrets/` or Host Environment)

- `kite_api_key` (injected via Docker Compose secret `secrets/kite_api_key.txt` or `KITE_API_KEY` env var)
- `kite_api_secret` (injected via Docker Compose secret `secrets/kite_api_secret.txt` or `KITE_API_SECRET` env var)

Production runtime does NOT depend on a static `KITE_ACCESS_TOKEN` or any Google/Gemini cloud variables. Access tokens are generated via Zerodha login and stored in the persistent volume at `/var/lib/nexus-mobile/auth/zerodha_session.json`.

`.env.example` contains non-secret defaults only. Never commit `.env` or `secrets/`, copy an Original Nexus session, or put credentials in an image. On a hardened VPS, inject the API key and secret through Docker Compose secrets with `chmod 600` file permissions. Mobile stores only its own Kite session in its protected session file; callback request tokens are never persisted, and tokens are never returned by an endpoint or written to logs.

### Daily Zerodha Token Renewal

Kite access tokens are short-lived and are not refreshed automatically by Kite. Open the local/private Mobile UI and start Zerodha login; Kite redirects to `/api/callback`, which validates and atomically replaces the Mobile session. A stored session is verified at startup with Kite's profile endpoint. An invalid stored token is marked unavailable; complete a new login instead. Authentication status exposes only safe state and token fingerprint metadata. Uvicorn access logging is disabled so the callback query string is not logged.

## Docker Build and Compose

Create the protected secret files and `.env` beside `compose.yaml`:

```bash
mkdir -p secrets
echo -n "YOUR_KITE_API_KEY" > secrets/kite_api_key.txt
echo -n "YOUR_KITE_API_SECRET" > secrets/kite_api_secret.txt
chmod 600 secrets/kite_api_key.txt secrets/kite_api_secret.txt
docker compose build
docker compose up -d
```

Compose binds the service to `127.0.0.1:8000` only. There is no public host port binding, and authentication routes reject non-private source addresses. Stage 1 archives and Mobile Kite state use separate Docker-managed volumes: `nexus-mobile-stage1-archives` and `nexus-mobile-kite-auth`. Both survive container recreation. The auth volume is mounted only at `/var/lib/nexus-mobile/auth` and is not populated from any Original Nexus path.

## Linux VPS and Tailscale

1. Install Docker Engine and the Docker Compose plugin on a supported Ubuntu VPS.
2. Install and authenticate Tailscale on the VPS and Android phone into the same tailnet. Apply least-privilege ACLs so only approved devices/users can reach this host.
3. Do not open port 8000 in the VPS firewall or cloud security group. Allow only the required Tailscale transport and outbound HTTPS for Kite/NSE.
4. Deploy this directory independently, create a protected `.env`, then start Compose. Compose creates persistent named archive and Kite-auth volumes automatically.
5. Publish the private mobile UI through Tailscale Serve, forwarding to localhost:

```bash
sudo tailscale serve --bg http://127.0.0.1:8000
```

Use the HTTPS Tailscale hostname shown by `tailscale serve status` from the phone, ending in `/mobile`. Tailscale Serve is private to the tailnet unless explicitly configured otherwise; do not enable Funnel or any public ingress.

## Health, Logs, and Operations

The container health probe requests `GET /mobile`; no extra HTTP route is registered. It performs no market-data request and exposes no configuration values.

```bash
docker compose ps
docker compose logs --tail=100 -f nexus-mobile
docker compose restart nexus-mobile
docker compose down
```

Do not use `docker compose down -v` unless archive and Kite-session deletion is explicitly intended.

Request logs include request ID, path, HTTP status, and duration. They do not include payloads, cookies, headers, or secrets. Application errors are summarized by exception category.

## Tests

```bash
python -m pytest -q
```

The tests cover route inventory, raw serialization, valid empty NSE datasets, archives, exact NSE equity identity, mapping/alias contracts, Stage 2 context behavior, and an import-isolation check that verifies the application modules are loaded from this project.

## Troubleshooting

- **Kite-backed routes unavailable:** check `/auth/zerodha/status` locally/private. If login is required, complete Zerodha login from the Mobile UI; inspect logs for error category only.
- **Exact stock unresolved:** the current Kite instrument master may not include an exact NSE cash-equity row. The service intentionally does not substitute another ticker or BSE listing.
- **NSE 401/403 or request failures:** the client refreshes cookies and retries. NSE may rate-limit or block a VPS egress address; check outbound DNS/HTTPS and retry policy before changing source behavior.
- **No phone access:** verify both devices are in the same tailnet, Tailscale ACLs allow access, `tailscale serve status` is healthy, and public port 8000 remains closed.
- **Archive permission errors:** ensure the mounted archive volume is writable by UID/GID 10001.
- **Container healthy but acquisition failing:** health indicates only that the application/UI is serving. It deliberately does not probe external market-data providers.

## Security Notes

The expected network boundary is Android phone -> Tailscale private network -> localhost-bound Compose service -> outbound HTTPS to Kite/NSE. FastAPI must not be exposed directly to the public internet. Never enable Tailscale Funnel for this service. Kite credentials stay on the server; mobile responses contain only the requested acquisition evidence.
