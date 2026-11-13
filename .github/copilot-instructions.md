# Copilot Instructions

Current laser service: [docker/docker-wainlux-rs](../docker/docker-wainlux-rs). Read its [API and timing contract](../docker/docker-wainlux-rs/README.adoc), then [project docs](../documentation/README.adoc).

Keep protocol documentation clean-room: describe observed bytes and behavior, not vendor implementation. Follow [CLEAN_ROOM.md](../CLEAN_ROOM.md).

Run checks from the repository root:

- Rust tests and ARM image build on the workstation: `./scripts/build_laser.sh` (Docker or Podman auto-detected; Flatpak bridge handled; no deployment)
- Explicitly transfer and deploy to an idle Pi Zero W: `./scripts/deploy_laser.sh --target USER@PI-HOST --confirm` (SSH preflight; no laser job)
- Docs: `./scripts/test_docs.sh`
- UI: `node app/tests/ui.cjs` from `docker/docker-wainlux-rs` (requires jsdom)
- Audio/run correlation: `./scripts/analyze_laser_audio.sh RECORDING.m4a --attempt-id ATTEMPT-ID --expected-id DDDDDD` (containerized; no host audio packages)

The canonical build and transfer instructions are in [Getting Started](../documentation/GETTING_STARTED.adoc#build-and-test-the-rust-service). Never assume the Pi SSH hostname; require an explicit target.

Use AsciiDoc for documentation and PlantUML for diagrams. Keep changes focused, state uncertainty, and do not interrupt a physical burn when working with the Pi.
