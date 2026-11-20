# Docker deployment on a VPS

The image includes the default local OpenAlex CPU classifier. Python dependencies
are installed during the build; the pinned embedding model downloads on the first
classification and is reused from a Docker volume. No GPU is needed. Allow several
GB of free RAM above the roughly 2.38 GB of model weights, plus disk space for the
image, build layers, model download and publication cache. Benchmark your intended
query size on the VPS before opening access to other users.

## First start

These commands assume Git, Docker Engine and the Docker Compose plugin are installed
on the VPS. Use `sudo docker` if Docker requires it on your server.

```bash
git clone https://github.com/jmiba/publication-sdg-classifier.git
cd publication-sdg-classifier
cp .streamlit/secrets.sample.toml .streamlit/secrets.toml
nano .streamlit/secrets.toml
```

Set a real contact `http_user_agent` and your OpenAlex API key. Remove unused
placeholder API keys; enable only the enrichment providers you intend to use.
Secrets are mounted read-only at runtime and excluded from the image and build
context. The container runs as UID/GID 10001:10001. Make the secrets file readable
by that identity, for example on Linux:

```bash
sudo chown root:10001 .streamlit/secrets.toml
sudo chmod 640 .streamlit/secrets.toml
sudo docker compose up -d --build --wait
sudo docker compose logs --tail=100 app
```

The service binds only to `127.0.0.1:8501`. To try it without a public hostname,
forward the port from your own computer:

```bash
ssh -L 8501:127.0.0.1:8501 YOUR_USER@YOUR_VPS
```

Open `http://localhost:8501`. Run a small fetch with the default local classifier
to download the model and verify real inference. The HTTP health check verifies
Streamlit availability; it does not load the model or validate API credentials.
Outbound HTTPS is needed for sources, enrichment providers and Hugging Face.
The optional `scholarly` free-proxy profile is excluded; SerpApi enrichment remains
available. No existing local cache is imported automatically.

## HTTPS through an existing Caddy container

For Caddy running directly on the host, proxy to `127.0.0.1:8501`. For a Caddy
container already connected to the Docker network `frontend`, enable the overlay
once by creating an untracked `.env` file in this checkout:

```dotenv
COMPOSE_FILE=compose.yaml:compose.caddy.yaml
```

Then run `sudo docker compose up -d --wait` and add a block to your existing Caddy
configuration, replacing the example hostname:

```caddyfile
publications.example.org {
    reverse_proxy publication-sdg:8501
}
```

Point the hostname's DNS to the VPS and validate/reload your existing Caddy
configuration. [Caddy handles WebSocket proxying](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) and automatic HTTPS. The `frontend` network
must already exist and Caddy must be attached to it. This setup does not change
your existing Caddy or other services, and needs no public port 8501.

The app has no built-in login and uses shared server API credentials and cache.
For personal/team use, protect this hostname with your existing access control or
[Caddy `basic_auth`](https://caddyserver.com/docs/caddyfile/directives/basic_auth) before exposing it. Generate password hashes with Caddy's
`hash-password` command; do not put plaintext passwords in the Caddyfile.

## Update

Run from the same checkout so Compose retains the same project and volumes.
Wait for active fetches to finish: recreating the container ends in-memory sessions
and background jobs. First make a consistent SQLite backup (see below), then:

```bash
sudo docker image tag publication-sdg:local publication-sdg:rollback
git pull --ff-only
sudo docker compose build --pull
sudo docker compose up -d --wait
sudo docker compose logs --tail=100 app
```

Building happens while the old container continues to serve. Code-only changes
reuse the dependency layer. Model files and SQLite data survive recreation;
API keys remain in the host secrets file. Dependencies follow the repository's
existing pins/ranges; a fresh build can resolve newer versions within those ranges.
For an exact runtime rollback, keep the previous image tag. After a secrets-file
edit, use `sudo docker compose up -d --force-recreate --wait` to remount the file
and reload the process-level configuration.

To run the saved previous image without rebuilding:

```bash
sudo env SDG_IMAGE_TAG=rollback docker compose up -d --no-build --wait
```

An older image does not undo database migrations. Restore a matching pre-update
backup if a future release makes incompatible schema changes. The `rollback` tag
stores only the most recent saved image. Do not prune it while you need it.

## Data and backups

Compose maintains `publication-sdg_data` for SQLite (including WAL/SHM files) and
`publication-sdg_model-cache` for Hugging Face downloads. Keep one app container;
this deployment is not configured for horizontal scaling. `SDG_CACHE_PATH` selects
the database at process startup, defaulting to `cache.sqlite3` outside Docker.

Use SQLite's backup API to copy a consistent live database, including committed
WAL transactions. After the first fetch has initialized the cache:

```bash
mkdir -p backups
sudo docker compose exec -T app python -c "import sqlite3; source = sqlite3.connect('/data/cache.sqlite3'); target = sqlite3.connect('/tmp/cache-backup.sqlite3'); source.backup(target); target.close(); source.close()"
sudo docker compose cp app:/tmp/cache-backup.sqlite3 "backups/cache-$(date +%Y%m%d-%H%M%S).sqlite3"
```

Copy backups off the VPS as well. Back up the host secrets file separately and
securely. Downloaded models can be downloaded again. Never copy only a live main
SQLite file while ignoring its WAL.

For restore or importing an existing cache, first create a consistent backup of
the source with SQLite's backup API. Stop the app, retain a separate backup of the
current data volume, and replace the database in that volume using a one-off
container. Remove old WAL/SHM companions only as part of that stopped restore,
set ownership to 10001:10001, then start the app. Do not overwrite a live database.

`sudo docker compose down` retains data. **`docker compose down -v` deletes both
persistent volumes** and must not be used for routine updates. Health status can
be checked with `sudo docker compose ps`; Docker restart policy restarts exited
processes, not containers that merely become unhealthy.

## Validation scope

The image was built and tested on Linux ARM64 through Docker Desktop. Streamlit
health and app rendering, non-root secrets/volume access, volume persistence after
container recreation, and one real offline CPU classification with the pinned
model passed. The source test suite also passed. The Contabo Linux AMD64 deployment additionally
passed real CPU inference, public HTTPS, whole-site password authentication
(missing and incorrect credentials rejected), and authenticated WebSocket upgrade.
Live publication-provider requests and a full browser fetch still need checking
with a small query before wider use.
