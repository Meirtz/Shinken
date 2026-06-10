"""Concurrency scaling: sync-facade (thread/session) vs SharedLoop (1 thread/N sessions),
against N local mock shinkend servers. Isolates the CLIENT-side cost of managing many
sandboxes from one process — the substrate-agnostic half of the 'manage 1024 sandboxes'
question. Emits CSV to docs/benchmarks/data/concurrency.csv.
"""
from __future__ import annotations
import asyncio, base64, gc, json, os, resource, socket, sys, threading, time
from websockets.asyncio.server import serve

_PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d76360000002000154a24f9f0000000049454e44ae426082")).decode()

async def _handler(ws):
    async for raw in ws:
        m = json.loads(raw); t = m.get("type")
        if t == "hello":
            await ws.send(json.dumps({"type":"welcome","v":0,"server":{"platform":"linux"},
                "capabilities":{"schema_version":0,"verbs":["click","screenshot"],
                "targets":["point_px"],"observation_types":["screenshot"],"max_long_edge":2576}}))
        elif t == "action":
            cid = m.get("call_id")
            if (m.get("action") or {}).get("verb") == "screenshot":
                await ws.send(json.dumps({"type":"observation","obs_id":"o","cause":cid,
                    "image":{"ref":_PNG,"w":1,"h":1,"scope":"screen","format":"png"}}))
            else:
                await ws.send(json.dumps({"type":"ack","call_id":cid,"ok":True}))

def _servers(n):
    ports=[]
    for _ in range(n):
        s=socket.socket(); s.bind(("127.0.0.1",0)); ports.append(s.getsockname()[1]); s.close()
    loop=asyncio.new_event_loop(); ready=threading.Event()
    async def boot():
        for p in ports: await serve(_handler,"127.0.0.1",p)
    def run():
        asyncio.set_event_loop(loop); loop.run_until_complete(boot()); ready.set(); loop.run_forever()
    th=threading.Thread(target=run,daemon=True); th.start(); ready.wait(10)
    return [f"127.0.0.1:{p}" for p in ports], loop

def _threads(): return sum(1 for t in threading.enumerate() if t.name=="shinken-loop" and t.is_alive())
def _rss(): 
    r=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r/(1024*1024 if sys.platform=="darwin" else 1024)

def main():
    import shinken
    Ns=[1,2,4,8,16,32,64,128,256,512]
    rows=[]
    for n in Ns:
        addrs,loop=_servers(n)
        # SharedLoop path
        gc.collect(); rss0=_rss()
        with shinken.SharedLoop() as sh:
            envs=[shinken.connect(a,loop=sh) for a in addrs]
            t=time.time()
            for e in envs: e.screenshot()
            wall=time.time()-t
            thr=_threads(); rss=_rss()
            for e in envs: e.close()
        rows.append({"n":n,"mode":"shared_loop","loop_threads":thr,
                     "rss_mb":round(rss,1),"rss_delta_mb":round(rss-rss0,1),
                     "observe_all_s":round(wall,3),"ms_per_sandbox":round(wall/n*1000,2)})
        loop.call_soon_threadsafe(loop.stop); time.sleep(0.2)
        print(f"N={n:4d} shared_loop: threads={thr} rss={rss:.0f}MB wall={wall*1000:.0f}ms", flush=True)
    # sync-facade path: only up to 256 (512 threads is unkind); measure thread growth
    for n in [1,4,16,64,256]:
        addrs,loop=_servers(n)
        gc.collect(); rss0=_rss(); base=_threads()
        envs=[shinken.connect(a) for a in addrs]
        t=time.time()
        for e in envs: e.screenshot()
        wall=time.time()-t
        thr=_threads(); rss=_rss()
        for e in envs: e.close()
        rows.append({"n":n,"mode":"sync_facade","loop_threads":thr-base,
                     "rss_mb":round(rss,1),"rss_delta_mb":round(rss-rss0,1),
                     "observe_all_s":round(wall,3),"ms_per_sandbox":round(wall/n*1000,2)})
        loop.call_soon_threadsafe(loop.stop); time.sleep(0.2)
        print(f"N={n:4d} sync_facade: threads={thr-base} rss={rss:.0f}MB", flush=True)
    out=os.path.join("docs/benchmarks/data","concurrency.csv")
    import csv
    with open(out,"w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print("wrote",out,f"({len(rows)} rows)")

if __name__=="__main__":
    raise SystemExit(main())
