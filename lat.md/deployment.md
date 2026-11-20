# Container deployment

A single non-root Streamlit container supports VPS hosting while separating application releases from persistent data and host-managed credentials.

The Docker image includes the base dependencies and pinned local OpenAlex ML
profile with CPU PyTorch wheels. It copies only runtime modules, public source
registries, Streamlit configuration and the released head. An allowlist build
context excludes secrets, caches, virtual environments and development artifacts.
See `DEPLOYMENT.md` for operator commands and proxy integration.

## Persistent runtime data

SQLite and downloaded embedding files live in separate named volumes so rebuilding or replacing the image preserves expensive cached results and model downloads.

`SDG_CACHE_PATH` selects [[cache_db.py#DB_PATH]] once at import, with the existing
`cache.sqlite3` default for local runs. Compose sets `/data/cache.sqlite3` through
the image; the entire directory persists, including WAL/SHM. `HF_HOME=/models`
redirects Hugging Face downloads to the other volume. Fresh volumes inherit
ownership for UID/GID 10001. Credentials are a required read-only bind mount;
missing host files fail rather than creating directories.

## Service and update boundaries

The service binds to loopback for local tunnels or host proxies, with an optional external frontend network for an existing containerized Caddy proxy.

One app process owns the model, background jobs and cache connection. Container
replacement drops active sessions/jobs; updates must wait for fetches to finish.
Compose uses a stable project name, exit restart policy and HTTP health check.
Health does not establish real inference or external credential validity. The app
has no login; access control belongs at the proxy when hosting shared credentials.

Updates build before recreation, retain named volumes, and can retain a previous
image for rollback. SQLite's backup API provides consistent backups while WAL is
active. Image rollback does not roll back schema migrations; matching backups may
be required. Existing dependency version ranges are preserved.

## VPS qualification

The Contabo AMD64 deployment validates CPU inference and authenticated Caddy access, while live publication-provider requests remain a separate end-to-end check.

The deployment reuses the separate reverse-proxy project and frontend network.
Whole-site Caddy authentication rejects unauthenticated and incorrect credentials
for the root and health route, while valid credentials allow HTTP and WebSocket
connections. Host secrets use root ownership and group 10001 read access (0640);
initial login details are kept separately in an account-private file (0600).
Only existing public HTTP/HTTPS ports are needed; port 8501 remains loopback-bound.
