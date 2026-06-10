"""Asyncio TCP delay proxy — WAN emulation for the local benchmark suites (S11).

macOS has no netem/tc, so this stands in: a tiny TCP relay that forwards bytes both
ways between a client and one upstream, holding every chunk for a configurable
ONE-WAY delay before forwarding it. A connection through the proxy therefore sees
~2×delay of added RTT (each direction crosses the proxy once). Per-direction
ordering is preserved via a timestamped FIFO — the delay shifts bytes in time, it
does not serialize throughput (chunks are "in flight" concurrently, like a long
pipe), so bandwidth-heavy replies are not artificially throttled.

Library use (the bench suites)::

    proxy = DelayProxy("127.0.0.1", 32768, delay_ms=75.0)  # one-way; +150 ms RTT
    addr = proxy.start_in_thread()  # "127.0.0.1:<port>", own loop on a daemon thread
    env = shinken.connect(addr, token=token)
    ...
    proxy.stop()

CLI use::

    python benchmarks/_wan_proxy.py --upstream 127.0.0.1:8765 --delay-ms 75

Validation: ``bench_step_pipeline.py`` measures ACI ``ping`` RTT through the proxy at
every tier and records nominal-vs-measured in the results JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import threading

_CHUNK = 64 * 1024


class DelayProxy:
    """Relay every connection to ``(upstream_host, upstream_port)``, delaying each
    direction by ``delay_ms`` one-way."""

    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        delay_ms: float = 0.0,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
    ) -> None:
        self.upstream = (upstream_host, int(upstream_port))
        self.delay_s = float(delay_ms) / 1000.0
        self.listen_host = listen_host
        self.listen_port = int(listen_port)
        self.port: int | None = None
        self._server: asyncio.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """One direction: forward each chunk ``delay_s`` after it arrived. The reader
        keeps reading while earlier chunks wait in the queue, so the delay adds
        latency without capping throughput."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        async def drain() -> None:
            while True:
                due, data = await queue.get()
                wait = due - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                if data is None:
                    with contextlib.suppress(Exception):
                        writer.write_eof()  # half-close: the peer sees EOF in order
                    return
                writer.write(data)
                await writer.drain()

        drainer = asyncio.ensure_future(drain())
        try:
            while True:
                data = await reader.read(_CHUNK)
                if not data:
                    break
                queue.put_nowait((loop.time() + self.delay_s, data))
        except Exception:
            pass  # a reset on either side just ends this direction
        finally:
            queue.put_nowait((loop.time() + self.delay_s, None))
            with contextlib.suppress(Exception):
                await drainer

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_connection(*self.upstream)
        except Exception:
            cw.close()
            return
        await asyncio.gather(
            self._pump(cr, uw), self._pump(ur, cw), return_exceptions=True
        )
        for w in (cw, uw):
            with contextlib.suppress(Exception):
                w.close()

    async def start(self) -> int:
        """Bind and start serving on the current loop; returns the bound port."""
        self._server = await asyncio.start_server(
            self._handle, self.listen_host, self.listen_port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    def start_in_thread(self) -> str:
        """Run the proxy on its own event loop in a daemon thread; return ``host:port``."""
        ready = threading.Event()
        self._loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self.start())
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="wan-proxy", daemon=True)
        self._thread.start()
        if not ready.wait(5):
            raise RuntimeError("WAN proxy failed to start")
        return f"{self.listen_host}:{self.port}"

    def stop(self) -> None:
        loop, self._loop = self._loop, None
        if loop is None:
            return

        async def shutdown() -> None:
            # Stop accepting, then cancel the relay tasks of any live connections so
            # the loop stops clean (no pending-task warnings at interpreter exit).
            if self._server is not None:
                self._server.close()
            pending = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=2)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        with contextlib.suppress(Exception):
            loop.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TCP delay proxy (WAN emulation)")
    ap.add_argument("--upstream", required=True, help="host:port to relay to")
    ap.add_argument(
        "--delay-ms",
        type=float,
        default=0.0,
        help="ONE-WAY delay; the RTT gains ~2x this",
    )
    ap.add_argument("--listen-host", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=0, help="0 = pick a free port")
    args = ap.parse_args(argv)
    host, _, port = args.upstream.rpartition(":")
    proxy = DelayProxy(
        host, int(port), args.delay_ms, args.listen_host, args.listen_port
    )

    async def serve_forever() -> None:
        bound = await proxy.start()
        print(
            f"relaying {args.listen_host}:{bound} -> {args.upstream} "
            f"(+{args.delay_ms:g} ms one-way, ~+{2 * args.delay_ms:g} ms RTT)",
            flush=True,
        )
        await asyncio.Event().wait()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
