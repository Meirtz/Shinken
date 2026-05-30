# Shinken Linux Sandbox image (M0 skeleton).
#
# A minimal headless Linux desktop (Xvfb + Openbox) running a few GUI apps, with
# the shinkend Guest Runtime built and launched on the ACI WebSocket port. This is
# the proven OSWorld/E2B-style stack; the structured-first observation + action
# executor backends land in M1 (#4).
#
# Build (from repo root):  docker build -f images/linux/Dockerfile -t shinken/sandbox-linux .
# Run (bind to HOST loopback + require a dev token — shinkend refuses a non-loopback
# bind without SHINKEND_TOKEN):
#   docker run --rm -p 127.0.0.1:8765:8765 \
#     -e SHINKEND_TOKEN="$(openssl rand -hex 16)" shinken/sandbox-linux
# Then: shinken.connect("127.0.0.1:8765", token="<that token>")

# --- build shinkend ---
FROM rust:1-slim AS build
WORKDIR /src
COPY shinkend/ ./shinkend/
RUN cargo build --release --manifest-path shinkend/Cargo.toml

# --- runtime: headless Linux desktop + shinkend ---
FROM debian:stable-slim
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:0

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11-utils openbox xdotool x11-apps \
        at-spi2-core dbus-x11 \
        python3 python3-gi gir1.2-atspi-2.0 \
        zenity \
        xterm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/shinkend/target/release/shinkend /usr/local/bin/shinkend
COPY images/linux/start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

EXPOSE 8765
ENV SHINKEND_ADDR=0.0.0.0:8765
CMD ["/usr/local/bin/start.sh"]
