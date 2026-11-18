# Official PlantUML command-line image, pinned by immutable digest.
FROM ghcr.io/plantuml/plantuml@sha256:2cf2f097652701319e29b9e6a809e68d4ddb06fbeeb696705c097da8fa6d0507

# This CLI renderer does not need the package manager at runtime.
USER root
RUN dpkg --purge apt libapt-pkg6.0
USER appuser
