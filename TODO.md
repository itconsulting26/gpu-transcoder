# TODO

## Security
- [ ] Encrypt SSH passwords stored in the database (currently plain text in `SourceServer.password`); prefer SSH keys
- [ ] Enable SSH host key verification (`StrictHostKeyChecking=no` is used today) and store known hosts per server
- [ ] Bind the web port to `127.0.0.1` by default in `docker-compose.yml` and document a reverse proxy setup (Nginx / Apache / Caddy with HTTPS)
- [ ] Default `DJANGO_ALLOWED_HOSTS` to `localhost` instead of `*`
- [ ] Re-enable Django password validators (`AUTH_PASSWORD_VALIDATORS` is empty)
- [ ] Add login rate limiting / lockout
- [ ] MCP admin: add TLS, IP allow-list, and an option to disable dangerous tools (`run_shell`, `write_file`)

## Features
- [ ] Translate the UI to English (Django i18n) and keep French as an option
- [ ] Scheduled scans (watch folders and queue new large files automatically)
- [ ] Notifications when a job finishes or fails (email, Telegram, webhook)
- [ ] Statistics page: space saved per server and over time
- [ ] Support several GPUs and pick the least busy one
- [ ] Per-server or per-folder encoding profiles
- [ ] Optional AV1 output on GPUs that support it (RTX 40 series and newer)
- [ ] REST API to queue jobs from other tools

## Reliability
- [ ] Unit tests for `probe.py` and the verification step in `transcode.py`
- [ ] Integration test with a small sample video
- [ ] Automatic retry of failed jobs with a configurable limit
- [ ] Clean up leftover `.gpu-tmp` files after a crash
- [ ] Healthchecks in `docker-compose.yml`
- [ ] Option to use PostgreSQL instead of SQLite for larger libraries

## Project
- [x] Add a LICENSE file (MIT)
- [ ] Add screenshots to the README
- [ ] GitHub Actions: lint (ruff) and build the Docker image
- [ ] Publish the image to GitHub Container Registry
