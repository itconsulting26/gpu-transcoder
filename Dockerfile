# ffmpeg 6.1 compilé avec NVENC/NVDEC (nv-codec-headers compatibles pilote 545 / GTX 1080)
FROM jrottenberg/ffmpeg:6.1-nvidia2204

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
    TZ=Europe/Paris

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip sshfs openssh-client sshpass \
        ca-certificates tzdata procps \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -encoders | grep -q nvenc

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
