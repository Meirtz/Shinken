# Shinken Linux Sandbox image (M0 skeleton).
#
# A minimal headless Linux desktop (Xvfb + Openbox) running a few GUI apps, with
# the shinkend Guest Runtime built and launched on the ACI WebSocket port. This is
# the proven OSWorld/E2B-style stack; the structured-first observation + action
# executor backends land in M1 (#4).
#
# Build (from repo root):  docker build -f images/linux/Dockerfile -t shinken/sandbox-linux .
# Run (bind to HOST loopback + require a dev token — shinkend refuses a non-loopback
# bind without SHINKEND_TOKEN). This local reference image is NOT the production isolation
# boundary (that is a microVM, see docs/design/threat-model.md — the isolation & capability
# note); for a tighter container, add the usual run flags:
#   docker run --rm -p 127.0.0.1:8765:8765 \
#     --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp \
#     -e SHINKEND_TOKEN="$(openssl rand -hex 16)" shinken/sandbox-linux
# Then: shinken.connect("127.0.0.1:8765", token="<that token>")

# Both stages pin the SAME Debian release (bookworm) so the build-stage glibc the shinkend
# binary links against matches the runtime stage — floating rust:1-slim / debian:stable-slim
# can drift across a Debian major bump and yield a confusing GLIBC version error at start.
# (dependabot's `docker` ecosystem bumps these; see .github/dependabot.yml.)

# --- build shinkend ---
FROM rust:1-slim-bookworm AS build
WORKDIR /src
COPY shinkend/ ./shinkend/
RUN cargo build --release --locked --manifest-path shinkend/Cargo.toml

# --- runtime: headless Linux desktop + shinkend ---
FROM debian:bookworm-slim
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

# Run the desktop + shinkend as a non-root user: standard container hygiene, and shinkend
# needs no privileges anyway. shinkend binds 8765 (>1024, no privilege needed); Xvfb/openbox/
# xterm and the /tmp X-lock cleanup all work unprivileged. The token still arrives via env
# (dev-only; the production isolation boundary is the microVM, D1).
RUN useradd --create-home --uid 1000 shinken
USER shinken

EXPOSE 8765
ENV SHINKEND_ADDR=0.0.0.0:8765
CMD ["/usr/local/bin/start.sh"]
