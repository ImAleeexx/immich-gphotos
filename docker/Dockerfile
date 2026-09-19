FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    IGP_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app /data

# Deliberately NOT `USER app`. A bind-mounted ./data arrives owned by whoever
# created it on the host -- root, when Docker created it because it did not
# exist -- and that ownership replaces the chown above entirely. Starting as
# root lets the entrypoint claim the directory; it drops to uid 1000 (or
# PUID/PGID) before exec'ing the app, which never runs as root.
ENV HOME=/home/app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8080/healthz').status_code==200 else 1)"

CMD ["python", "-m", "immich_gphotos.entrypoint"]
