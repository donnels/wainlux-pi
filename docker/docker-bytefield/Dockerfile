FROM docker.io/library/node@sha256:a34ab6d7f6f4813f4d0c5469d74685604b962fbdbf5793a7bc7b4998ecb867c8

# Pin the build-time SVG background helper and the SVG-to-PNG renderer.
RUN apk add --no-cache \
    python3=3.14.10-r1 \
    rsvg-convert=2.62.3-r0 \
    font-dejavu=2.37-r6

# Copy background gradient script and wrapper
COPY docker/add_svg_background.py /usr/local/bin/add_svg_background.py
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/add_svg_background.py /usr/local/bin/entrypoint.sh

COPY docker/package.json docker/package-lock.json /opt/bytefield/
RUN cd /opt/bytefield \
    && npm ci --ignore-scripts --omit=dev \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /usr/local/bin/yarn /usr/local/bin/yarnpkg
ENV PATH="/opt/bytefield/node_modules/.bin:${PATH}"
WORKDIR /diagrams

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--help"]
