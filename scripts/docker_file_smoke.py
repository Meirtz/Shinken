"""Docker guest file-transfer smoke (#154): round-trip a file through the *actual*
container filesystem via the DockerLocalProvider's guest transport (`docker cp`), not the
host-local reference store. Used by the Docker CI job against the running `shk` container.

Env: SHK_TOKEN (bearer token); optional SHK_ADDR (default 127.0.0.1:8765),
SHK_CONTAINER (default shk).
"""

import os
import pathlib
import tempfile

from shinken.providers import DockerLocalProvider
from shinken.providers.base import SandboxHandle

addr = os.environ.get("SHK_ADDR", "127.0.0.1:8765")
container = os.environ.get("SHK_CONTAINER", "shk")
handle = SandboxHandle(
    provider="docker-local",
    sandbox_id=container,
    addr=addr,
    token=os.environ["SHK_TOKEN"],
    metadata={"container_id": container},
)

env = DockerLocalProvider().connect(handle)
try:
    payload = b"shinken guest file-transfer smoke"
    with tempfile.TemporaryDirectory() as d:
        src = pathlib.Path(d) / "up.txt"
        src.write_bytes(payload)
        put = env.put_file(str(src), "/tmp/shk-xfer.txt")
        print(f"put -> guest: {put}")

        out = pathlib.Path(d) / "down.txt"
        got = env.get_file("/tmp/shk-xfer.txt", str(out), expect_sha256=put["sha256"])
        assert out.read_bytes() == payload, "guest round-trip payload mismatch"
        print(f"get <- guest: {got}")
    print("docker guest file-transfer smoke OK")
finally:
    env.close()
