#!/usr/bin/env python3
"""Shinken as a uni-agent sandbox backend — the swerex-shaped deployment seam.

uni-agent (https://github.com/verl-project/uni-agent) drives every sandbox through the
SWE-ReX deployment/runtime protocol; `shinken.integrations.swerex.ShinkenDeployment`
implements that shape over a Shinken provider, so its `AgentEnv` (and the verl rollout
stack above it) can point at Shinken sandboxes. This script makes the exact calls
uni-agent's AgentEnv makes, in order — scripted, no model API needed.

Run (needs Docker + the local sandbox image, see images/linux/):
    python examples/uniagent_shinken.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python" / "src"))

from shinken.integrations.swerex import ShinkenDeployment  # noqa: E402
from shinken.providers import DockerLocalProvider  # noqa: E402


class BashAction:  # the swerex action shape uni-agent sends (duck-typed)
    def __init__(self, command, timeout=60, check="ignore", session="default"):
        self.command, self.timeout, self.check, self.session = command, timeout, check, session


class Req:  # Command / ReadFileRequest / WriteFileRequest attribute bags
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def main():
    # uni-agent: AgentEnvConfig.deployment.get_deployment(run_id) -> here, directly.
    # Fork-native option: ShinkenDeployment(provider, checkpoint="<golden ckpt id>")
    # makes every start() materialize from a committed golden state instead of cold-booting.
    dep = ShinkenDeployment(DockerLocalProvider())

    await dep.start()  # AgentEnv.start(): create sandbox + handshake + default bash session
    try:
        rt = dep.runtime

        # AgentEnv.communicate() -> run_in_session(BashAction): env + cwd persist
        await rt.run_in_session(BashAction("export GREETING=hello && cd /tmp"))
        obs = await rt.run_in_session(BashAction('echo "$GREETING from $(pwd)"'))
        print("session says:", obs.output.strip(), f"(exit {obs.exit_code})")

        # AgentEnv.copy_to_container() -> execute(mkdir -p) + upload/write_file
        await rt.execute(Req(command=["mkdir", "-p", "/tmp/task"], shell=False, check=True))
        await rt.write_file(Req(content="state from the trainer", path="/tmp/task/input.txt"))
        read = await rt.read_file(Req(path="/tmp/task/input.txt", encoding=None, errors=None))
        print("file round-trip:", read.content)

        # Shinken stays a full computer-use runtime beside the swerex surface:
        shot = dep.sandbox.screenshot()
        print("screenshot:", len(shot["png"]), "bytes of real desktop pixels")
    finally:
        await dep.stop()  # AgentEnv.close(): session closed, sandbox destroyed


if __name__ == "__main__":
    asyncio.run(main())
