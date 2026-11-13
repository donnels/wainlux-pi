# Build on the Deck, run on the Pi Zero W (armv6). See ADR-022.
# From the repository root, run ./scripts/build_laser.sh.

# Pinned multi-platform index for rust-musl-cross:arm-musleabihf.
FROM --platform=$BUILDPLATFORM ghcr.io/rust-cross/rust-musl-cross@sha256:90fc4876d17c21346e55dfd4a344924b72c3bb38380749647461cd9595174789 AS build
WORKDIR /w
COPY . .
RUN RUSTFLAGS="-C target-cpu=arm1176jzf-s" cargo build --release --locked -p app --target arm-unknown-linux-musleabihf

# Runtime: COPY only, so the workstation never runs ARM code.
# Pinned multi-platform Alpine 3.21 index, including linux/arm/v6.
FROM docker.io/library/alpine@sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab
COPY --from=build /w/target/arm-unknown-linux-musleabihf/release/laser-app /usr/local/bin/laser-app
COPY materials.toml /etc/laser/materials.toml
COPY tests.toml /etc/laser/tests.toml
COPY use-cases /etc/laser/use-cases
COPY compose/assets/DejaVuSansMono-LICENSE /usr/share/licenses/laser-app/DejaVuSansMono-LICENSE
ENV LISTEN=0.0.0.0:8081 \
    DATA_DIR=/data \
    MACHINE=k6 \
    LASER_PORT=/dev/ttyUSB0 \
    MATERIALS=/etc/laser/materials.toml \
    TEST_CATALOG=/etc/laser/tests.toml \
    USE_CASES=/etc/laser/use-cases
VOLUME /data
EXPOSE 8081
CMD ["laser-app"]
