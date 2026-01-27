# Copilot Instructions

- Scope: Pi Zero W Dockerized Flask driving Wainlux K6 over USB (CP2102); browser → Flask API → serial driver.
- Read first: [README.adoc](../README.adoc); diagrams in [images](../images).
- Core files (/app in container): [docker-wainlux/docker/app/main.py](../docker-wainlux/docker/app/main.py) routes/image prep, uses k6 library from [docker-wainlux/docker/k6/](../docker-wainlux/docker/k6/), UI in [docker-wainlux/docker/templates/index.html](../docker-wainlux/docker/templates/index.html).
- API: / (UI), POST /api/connect|disconnect, /api/test/home, /api/test/bounds (320x320 frame), /api/engrave (upload → 1-bit), GET /api/status.
- Image prep: thumbnail 800x800 → grayscale → threshold 128 to 1-bit → depth 1-255 (default 100) → driver.
- Protocol: 115200 on /dev/ttyUSB0, ACK byte 9, limits 1600x1520, 9-byte header (opcode 9, size, depth, power=1000, line idx) + packed pixels; 100 ms pause after move.
- Run on Pi: in [docker-wainlux](../docker-wainlux) `docker compose build` (needs 1GB swap, ~15-20m) then `docker compose up -d`; config in [docker-wainlux/compose.yaml](../docker-wainlux/compose.yaml); logs `docker compose logs -f`.
- Docs: AsciiDoc with includes; see [documentation/structure.adoc](../documentation/structure.adoc); PlantUML in images.

## Style and behaviour Guidelines
- DRY
- YAGNI
- KISS
- Extreme Hemingway
- No Magic
- Docs in asciidoc (main README as .asciidoc includes as .adoc)
- Images in plantuml
- Point out false statements or ones that are not defensible
- Provide, and compare, when there are multiple options

## Clean-Room Protocol
- Protocol yes. Vendor code NO.
- No vendor function names.
- No vendor variable names.
- No Chinese pinyin names.
- No line numbers from decompiled code.
- No code snippets from vendor.
- Document what bytes do. Not how vendor does it.
- Discovery method: "Observed via USB capture", "Ghidra analysis (via LLM)", etc.
- Observed behavior. Not implementation.
- Protocol structure. Not code structure.
- See CLEAN_ROOM.md for full policy.

## Organization of files
- /docker-wainlux/ compose files and README
- /docker-wainlux/docker Docker context
- /docker-wainlux/docker/app Flask app
- /documentation Docs in asciidoc
- /docs placeholder for output of documentation build for github pages (do not put stuff in here manually)
- /images PlantUML and other images
- /scripts Helper scripts
- /tests Unit and integration tests (as we're not yet flying it's been removed)
- README.adoc Top-level readme
