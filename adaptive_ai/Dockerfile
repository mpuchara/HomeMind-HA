ARG BUILD_VERSION=0.7.12
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
COPY patches/v0.7.3-main.patch.part* /tmp/adaptive-patches/
COPY patches/v0.7.3-options-migration.patch /tmp/adaptive-patches/
COPY patches/v0.7.3-appjs.patch /tmp/adaptive-patches/
COPY patches/v0.7.4-main.patch.part* /tmp/adaptive-patches/
COPY patches/v0.7.4-appjs.patch /tmp/adaptive-patches/
COPY patches/v0.7.12-app.patch.gz.b64.part* /tmp/adaptive-patches/
RUN apk add --no-cache patch \
    && cat /tmp/adaptive-runtime/runtime.zip.b64.part* | base64 -d > /tmp/runtime.zip \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/runtime.zip').extractall('/tmp/runtime')" \
    && cp -R /tmp/runtime/rootfs/app/. /app/ \
    && cat /tmp/adaptive-patches/v0.7.3-main.patch.part* > /tmp/adaptive-patches/main-073.patch \
    && cat /tmp/adaptive-patches/v0.7.4-main.patch.part* > /tmp/adaptive-patches/main-074.patch \
    && cat /tmp/adaptive-patches/v0.7.12-app.patch.gz.b64.part* | base64 -d | gzip -dc > /tmp/adaptive-patches/app-0712.patch \
    && cd /app \
    && patch -p0 < /tmp/adaptive-patches/main-073.patch \
    && patch -p0 < /tmp/adaptive-patches/v0.7.3-options-migration.patch \
    && patch -p0 < /tmp/adaptive-patches/v0.7.3-appjs.patch \
    && patch -p0 < /tmp/adaptive-patches/main-074.patch \
    && patch -p0 < /tmp/adaptive-patches/v0.7.4-appjs.patch \
    && patch -p1 < /tmp/adaptive-patches/app-0712.patch \
    && python -m py_compile /app/main.py /app/control.py \
    && pip install --no-cache-dir websockets==15.0.1 \
    && chmod +x /app/run.sh \
    && rm -rf /tmp/runtime.zip /tmp/runtime /tmp/adaptive-runtime /tmp/adaptive-patches

EXPOSE 8099
CMD ["/app/run.sh"]
