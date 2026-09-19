ARG BUILD_VERSION=0.14.35
ARG BUILD_ARCH=amd64
FROM python:3.13-alpine
ARG BUILD_VERSION
ARG BUILD_ARCH
ENV ADAPTIVE_AI_BUILD_VERSION="${BUILD_VERSION}"
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="${BUILD_ARCH}"
WORKDIR /app
COPY src/ /app/
RUN python -m compileall -q /app \
    && pip install --no-cache-dir websockets==15.0.1 \
    && chmod +x /app/run.sh
EXPOSE 8099
CMD ["/app/run.sh"]
