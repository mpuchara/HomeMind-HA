ARG BUILD_VERSION=0.7.2
ARG BUILD_ARCH=amd64
FROM python:3.13-alpine

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

WORKDIR /app
COPY runtime.zip.b64.part* /tmp/adaptive-runtime/
RUN cat /tmp/adaptive-runtime/runtime.zip.b64.part* | base64 -d > /tmp/runtime.zip \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/runtime.zip').extractall('/tmp/runtime')" \
    && cp -R /tmp/runtime/rootfs/app/. /app/ \
    && python -m py_compile /app/main.py /app/control.py \
    && pip install --no-cache-dir websockets==15.0.1 \
    && chmod +x /app/run.sh \
    && rm -rf /tmp/runtime.zip /tmp/runtime /tmp/adaptive-runtime

EXPOSE 8099
CMD ["/app/run.sh"]
