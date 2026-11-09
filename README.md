# GPU Transcoder

A self-hosted web platform that shrinks large video files stored on **remote servers** using an **NVIDIA GPU (NVENC)**, then safely replaces the originals in place.

Built with Django + a background worker, packaged with Docker. The UI is in French.

## What it does

- **Keeps the original codec**: H.264 → H.264, HEVC → HEVC, capped at a configurable bitrate (4 Mb/s by default).
- **Works on remote storage**: add source servers over SSH (key or password), browse them over SFTP, and mount them with SSHFS for encoding. Videos never need to be copied to the GPU box first.
- **Finds what is worth shrinking**: scanning a folder runs `ffprobe` on every video and flags files above a size or bitrate threshold (1.5 GB / 4 Mb/s by default).
- **Safe in-place replacement**: encodes to a hidden temp file in the same folder (`.name.gpu-tmp.ext`), verifies it (duration, streams, readability, end of file), then atomically renames it over the original, keeping the same name, extension, permissions and timestamps.
  - On any failure the original is never touched and the job is marked as failed with its log.
  - If the output is not smaller, the original is kept ("no gain").
- **Other formats on demand**: AVI, AV1, etc. are not skipped silently. They are transcoded to H.264 only when you force them.
- **Live dashboard**: progress, fps, speed, bitrate, size, ETA and GPU status in real time.
- **Settings from the UI**: max/target bitrate, NVENC preset and CQ, audio re-encoding, number of parallel jobs, detection thresholds, queue pause.
- **Multi-user** with an admin role.

## Requirements

- Linux host with an NVIDIA GPU that supports NVENC (developed on a GTX 1080, driver 545)
- Docker + Docker Compose and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
- SSH access to the servers that hold your videos

## Quick start

```bash
git clone https://github.com/itconsulting26/gpu-transcoder.git
cd gpu-transcoder

cp .env.example .env        # then edit .env: secret key, admin password, hosts
mkdir -p data keys && chmod 700 keys
# Put the SSH private keys of your source servers in ./keys/

docker compose up -d --build
```

Open `http://<server>:8080` and log in with `ADMIN_USER` / `ADMIN_PASSWORD`.

> **Security:** the web UI listens on `0.0.0.0:8080`. Put it behind a reverse proxy with HTTPS, or restrict the port with a firewall. Do not expose it to the internet as-is.

## Everyday commands

```bash
docker compose logs -f worker   # follow the worker
docker compose restart worker   # after a code change (code is mounted as a volume, no rebuild needed)
docker compose build            # only when Dockerfile or requirements.txt change
docker compose down             # stop (data stays in ./data)
```

## How to use it

1. **Servers** → add an SSH server (key file name from `./keys/`, or a password), then test the connection.
2. **Browse** → navigate the remote folders and click "Scan this folder".
3. **Files** → tick the files, then "Add to queue" (or "Force" for AVI/AV1… → H.264).
4. **Dashboard** → watch the jobs in real time.

## Project layout

```
core/            Django app: models, views, scanner (probe.py), SSH/SSHFS (ssh.py),
                 encoding + verification (transcode.py), job loop (worker.py)
transcoder/      Django project settings
templates/       Web UI
mcp-admin/       Optional: tiny MCP server to let an AI assistant administer the GPU box
Dockerfile       Based on jrottenberg/ffmpeg:6.1-nvidia2204 (ffmpeg with NVENC/NVDEC)
docker-compose.yml   web (gunicorn) + worker (GPU, FUSE for SSHFS)
```

## Optional: MCP admin server

`mcp-admin/` contains a small [MCP](https://modelcontextprotocol.io) server (shell, file read/write, directory listing, `nvidia-smi`) used to let Claude install and maintain the platform remotely. `bootstrap.sh` installs it as a systemd service and generates a random token.

> **Warning:** this gives **root shell access** to whoever holds the URL. It runs over plain HTTP with the token in the URL path. Keep it bound to localhost behind a TLS reverse proxy, or firewall it, and stop it when you don't need it.

## Configuration

All secrets come from environment variables (`.env`), never from the code:

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django secret key |
| `DJANGO_DEBUG` | `1` to enable debug mode (never in production) |
| `DJANGO_ALLOWED_HOSTS` | Allowed host names, comma-separated |
| `DJANGO_CSRF_ORIGINS` | Trusted origins when behind a proxy |
| `ADMIN_USER` / `ADMIN_PASSWORD` | First admin account |
| `TZ` | Time zone |

See [TODO.md](TODO.md) for the roadmap.

## License

[MIT](LICENSE)

## Author

Chafik Lounici — ITC Consulting
