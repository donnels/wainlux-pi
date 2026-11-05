# Copilot Instructions

Current laser service: [docker/docker-wainlux-rs](../docker/docker-wainlux-rs). Read its [API and timing contract](../docker/docker-wainlux-rs/README.adoc), then [project docs](../documentation/README.adoc).

Keep protocol documentation clean-room: describe observed bytes and behavior, not vendor implementation. Follow [CLEAN_ROOM.md](../CLEAN_ROOM.md).

Run checks from the repository root:

- Docs: `./scripts/test_docs.sh`
- Rust: `cargo test --workspace` from `docker/docker-wainlux-rs`
- UI: `node app/tests/ui.cjs` from `docker/docker-wainlux-rs` (requires jsdom)

Use AsciiDoc for documentation and PlantUML for diagrams. Keep changes focused, state uncertainty, and do not interrupt a physical burn when working with the Pi.
