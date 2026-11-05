# Build on the Deck, run on the Pi Zero W (armv6). See ADR-022.
# podman build --platform linux/arm/v5 -t laser-app:arm .

FROM --platform=$BUILDPLATFORM ghcr.io/rust-cross/rust-musl-cross:arm-musleabihf AS build
WORKDIR /w
COPY . .
RUN cargo build --release --locked -p app --target arm-unknown-linux-musleabihf

# Runtime: COPY only, so the Deck never runs ARM code. Shell and apt kept for debugging.
FROM debian:stable-slim
COPY --from=build /w/target/arm-unknown-linux-musleabihf/release/laser-app /usr/local/bin/laser-app
COPY materials.toml /etc/laser/materials.toml
COPY use-cases /etc/laser/use-cases
COPY compose/assets/DejaVuSansMono-LICENSE /usr/share/licenses/laser-app/DejaVuSansMono-LICENSE
ENV LISTEN=0.0.0.0:8081 \
    DATA_DIR=/data \
    MACHINE=k6 \
    LASER_PORT=/dev/ttyUSB0 \
    MATERIALS=/etc/laser/materials.toml \
    USE_CASES=/etc/laser/use-cases
VOLUME /data
EXPOSE 8081
CMD ["laser-app"]
