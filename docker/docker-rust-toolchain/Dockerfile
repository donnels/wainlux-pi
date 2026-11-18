# Shared native toolchain for locked Rust checks and tests on the development host.
# Update the digest deliberately and record the change in docker/IMAGES.adoc.
FROM docker.io/library/rust@sha256:9daf2b244ccc30523e55cafe32edf8465671ba567d0101620e5612ccb76ea1a7

RUN rustup component add rustfmt

WORKDIR /repo
