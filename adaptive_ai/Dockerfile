ARG BUILD_VERSION=0.6.0
ARG BUILD_ARCH=amd64
FROM python:3.13-alpine

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

WORKDIR /app
COPY rootfs/app/ /app/
RUN if ls /app/main.py.gz.b64.part* >/dev/null 2>&1; then \
      cat /app/main.py.gz.b64.part* | base64 -d > /tmp/main.py.gz \
      && gunzip -c /tmp/main.py.gz > /app/main.py; \
    elif [ -f /app/main.py.gz ]; then \
      gunzip -f /app/main.py.gz; \
    fi \
    && pip install --no-cache-dir websockets==15.0.1 \
    && chmod +x /app/run.sh

EXPOSE 8099
CMD ["/app/run.sh"]
