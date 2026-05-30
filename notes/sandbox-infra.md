# Sandbox Infrastructure — the substrate tier

> **Status:** drafting · **Date:** 2026-05-30 · **Owner:** the maintainers
> Working note feeding [`../docs/design/architecture.md`](../docs/design/architecture.md) and [`../docs/design/tech-decisions.md`](../docs/design/tech-decisions.md).
> Reconciles to **D1** (tiered, substrate-pluggable isolation), **D9** (Fleet Manager / control plane), **D10** (cross-platform), **D11** (optional GPU tier).
> Siblings: [streaming-bandwidth.md](streaming-bandwidth.md) · [replay.md](replay.md) · [permissions.md](permissions.md) · [ai-native-interface.md](ai-native-interface.md) · [sources.md](sources.md).

A Shinken **Sandbox** is one isolated guest computer; the **Substrate / Provider** is the pluggable virtualization backend beneath it. The single most important conclusion from the substrate research is blunt: **there is no one substrate.** The three desktop OSes diverge so sharply on graphics, snapshot semantics, licensing, and hardware that forcing one virtual-machine monitor (VMM) to cover all of them is a category error. Shinken therefore runs a **federated, per-OS fleet behind one control plane** and routes each Sandbox by the triple **(OS × needs-GPU × needs-fast-fork)** — exactly **D1**. The killer feature — sub-millisecond copy-on-write (CoW) fork, where *instant reset between tasks* and *replay branching* are the **same primitive** — is **Linux-only and CPU-only**. Everything else is a heavier, longer-lived, snapshot-light tier, and the platform must be honest about that asymmetry.

This note specifies: the per-OS substrate matrix; the Firecracker-no-GPU/display reality and what QEMU-microvm and crosvm add; the fork-from-snapshot mechanism (MAP_PRIVATE CoW, userfaultfd, CoW disk) and its mandatory uniqueness hook; the Windows and macOS hard constraints; the optional GPU virtualization tier (vGPU / MIG / VFIO); and how all of it builds on the OSS [`kubernetes-sigs/agent-sandbox`](https://github.com/kubernetes-sigs/agent-sandbox) CRD rather than reinventing fleet orchestration.

---

## 1. The per-OS substrate matrix

The routing key is **(OS × needs-GPU × needs-fast-fork)**. Below is the authoritative matrix; each cell names the VMM, the display path, the reset mechanism, and the realistic density. Every speed/density figure here is **vendor-published, unverified** unless noted — a first-party measurement plan is a prerequisite (see [open-questions.md](open-questions.md)).

| Tier | OS | Substrate (VMM) | Display / observation | Reset = | Fast-fork? | Density |
|------|-----|-----------------|----------------------|---------|-----------|---------|
| **L0 — headless Linux** (code/agent, no GUI) | Linux | **Firecracker** (KVM) | none (vsock + a11y/CDP) | fork-from-snapshot | **Yes — sub-ms** | thousands/host |
| **L1 — Linux desktop** (default v1) | Linux | **QEMU-microvm** (or **crosvm** PoC) + virtio-gpu | Xorg+dummy / Xvfb + Xfce + WebRTC/VNC; software render (llvmpipe/lavapipe) | fork-from-snapshot | **Yes** (no VFIO state to lose) | hundreds/host |
| **G — GPU-accelerated Linux** | Linux | **Cloud Hypervisor / QEMU + VFIO or vGPU/MIG** | virtio-gpu accel or VFIO + pixel stream | full reboot / app-layer; recycle-on-TTL | **No** (VFIO/vGPU state non-snapshottable) | 1–48 / GPU |
| **W — Windows** (v1, heavier) | Windows | **Cloud Hypervisor / QEMU + virtio-win** from sysprep golden image | UIA tree + screenshot + pixel stream | quiesced (VSS) snapshot revert / golden re-clone | **No** (no Firecracker-class fork) | density gated by **licensing** |
| **W-dev — Windows single-tenant** | Windows | **Windows Sandbox** (Hyper-V kernel isolation) | UIA + screenshot | clean-slate every launch | n/a | **1 per host** |
| **M — macOS** (v1, scarce premium) | macOS | **Apple Virtualization.framework** on Apple HW (tart / lume style) | AXUIElement tree + ScreenCaptureKit + pixel stream | APFS clonefile re-clone from golden | **No** (clone, not memory-fork) | **2 VMs / Mac** (hard cap) |
| **A — Android** (roadmap) | Android | **Cuttlefish / crosvm** or redroid | adb + screencap | quick-boot snapshot | partial | — |

The single control plane, one Guest Runtime (`shinkend`) contract, and one ACI sit **above** this matrix (**D10**); the per-OS handler-factory and substrate router sit beneath it. The scheduler must surface **"no fast-fork" as a first-class property** of the G / W / M tiers in the API — callers cannot assume sub-second reset everywhere. This is the honest constraint to design SLAs around: high density (Firecracker) and rich GPU/Windows desktops (Cloud Hypervisor / QEMU) are *different VMMs with different boot/snapshot/density profiles*, so per-tier SLAs must reflect the split rather than promising one number everywhere.

```
                         ┌──────────────────────────────────────────────┐
   create(os, gpu?,      │            Shinken Control Plane              │
   fast_reset?) ───────► │  Fleet Manager · substrate router · scheduler│
                         └───────┬───────────┬────────────┬─────────────┘
                                 │           │            │
                    (Linux, ─────┘  (Linux,  │  (Win/Linux+GPU,  (macOS,
                     no GPU)         GPU)     │   no fork)         2/Mac)
                         ▼            ▼       ▼                    ▼
                  Firecracker   QEMU-microvm  CLH/QEMU+VFIO   Apple VZ.framework
                  (L0 headless) /crosvm (L1)  (G + Windows W)  (M, Apple HW only)
                         │            │            │               │
                         └─ fast-fork ┘            └─ longer-lived, snapshot-light ┘
```

---

## 2. Firecracker has no GPU and no display — accept it

The decisive substrate fact: **Firecracker emulates exactly five virtio devices** — net, block, vsock, a UART serial console, and an i8042/PS2 keyboard controller used only to signal shutdown. **Zero graphics devices, zero PCIe, zero VFIO.** A GUI literally has no surface to draw on. Its community GPU/PCIe initiative (launched Oct 2024) was **paused in February 2026 for lack of internal resources**, and even the planned MVP was cold-plug-only, single-GPU physical-function only, **with no GPU snapshot** and an 8–20% boot-time regression (vendor-published, unverified; [discussion #4845](https://github.com/firecracker-microvm/firecracker/discussions/4845)). **Treat Firecracker-GPU and Firecracker-display as permanently out of scope for planning.** Anyone who hears "microVM" and assumes "can show a desktop" has fallen into the trap.

That is fine, because Firecracker is the headless king and we keep it exactly there (tier L0): ~125 ms boot to userspace, <5 MiB per-VM overhead, 28–33 ms snapshot restore for concurrent spawns, thousands of microVMs/host, up to ~150 microVMs/s/host create rate, 10× memory/CPU oversubscription in production (all vendor-published, unverified — [Firecracker snapshot docs](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md), [NSDI'20 summary](https://blog.acolyer.org/2020/03/02/firecracker/), [Seven Years of Firecracker](https://brooker.co.za/blog/2025/09/18/firecracker.html)). E2B and Morph are the production proof points that this is a real, copyable blueprint, not a research demo.

The defense-in-depth story matters for [permissions.md](permissions.md) and [`../docs/design/threat-model.md`](../docs/design/threat-model.md): Firecracker is wrapped by the **jailer** companion (chroot, dedicated cgroup, mount/PID/network namespaces, dropped privileges, a seccomp-BPF filter allowing only ~24 syscalls), so a guest+VMM escape lands in a near-empty jail with no host filesystem and only the configured TAP. Networking is one host TAP device per microVM with iptables/nftables NAT (typically a /30 per VM); **namespaced NAT lets many clones of one snapshot run with identical guest IPs** ([network-setup docs](https://github.com/firecracker-microvm/firecracker/blob/main/docs/network-setup.md)). The control surface is a REST API over a per-VM Unix socket; no daemon broker. Fly.io's eBPF/XDP UDP-steering + anti-spoof pattern ([fly.io: BPF, XDP and UDP](https://fly.io/blog/bpf-xdp-packet-filters-and-udp/)) is the model for routing media at line rate to the right microVM.

### 2.1 The Linux *desktop* answer: decouple "display" from "GPU"

The crucial insight for tier L1 is that **a Linux desktop needs a *surface*, not a physical GPU.** You run a headless compositor inside the guest and software-render, then pixel-stream the framebuffer out:

- **Xorg + the `dummy` video driver** (`xserver-xorg-video-dummy`) or **Xvfb** with a forced modeline (e.g. 1920×1080), an Xfce4/openbox session, and a pixel server scraping the framebuffer. This is **exactly the OSWorld stack** (`xfce4 + x11vnc + Xvfb + xdotool + pyautogui` on `:0`/5900, screenshots up to 1920×1080 RGB, AT-SPI tree as XML — [OSWorld](https://github.com/xlang-ai/OSWorld)) and what E2B Desktop ships (`Xvfb :0 ... + startxfce4 + x11vnc + noVNC`, [e2b-dev/desktop](https://github.com/e2b-dev/desktop)). It is the lowest-risk, highest-compatibility path and gives mature **AT-SPI** accessibility-tree grounding for **D3** Rung-0 observation — see [ai-native-interface.md](ai-native-interface.md).
- Render with Mesa **llvmpipe** (OpenGL) / **lavapipe** (Vulkan) software rasterizers, or **virtio-gpu virgl/venus** when a host GPU is present — no passthrough required ([Mesa Venus docs](https://docs.mesa3d.org/drivers/venus.html), [Collabora 2025 on virglrenderer](https://www.collabora.com/news-and-blog/blog/2025/01/15/the-state-of-gfx-virtualization-using-virglrenderer/)).
- **Wayland-headless** (wlroots: sway/labwc/cage with `WLR_BACKENDS=headless` + `WLR_LIBINPUT_NO_DEVICES=1`, streamed by [wayvnc](https://github.com/any1/wayvnc/blob/master/FAQ.md)) is viable but rougher and has a **weaker AT-SPI story** and less-universal input injection than X11+xdotool; virtual outputs come from VKMS or EVDI kernel modules. Reach for it only on the GPU-accelerated tier or to future-proof; do not pay its operational roughness on the bread-and-butter CPU tier.

> **Watch out:** virtio-gpu/virgl can silently drop to llvmpipe when host GL/Vulkan or guest Mesa is misconfigured, tanking performance without erroring. Guest images must **detect and assert the active renderer.** And switching the Linux default to Wayland silently degrades agent grounding/input vs the mature X11 AT-SPI + xdotool stack — this is a hidden a11y-coverage risk and one of the load-bearing unverified assumptions flagged in [open-questions.md](open-questions.md).

### 2.2 Which VMM presents that surface?

Only some VMMs ship a paravirtual display device. The comparison that drives the L1 choice:

| VMM | virtio-gpu? | VFIO/vGPU? | Windows guest? | Fast snapshot/fork? | Verdict for Shinken |
|-----|-------------|------------|----------------|---------------------|---------------------|
| **Firecracker** | **No** (5 devices, no graphics) | No | No | **Yes** (28–33 ms restore) | L0 headless Linux only |
| **QEMU** (incl. `microvm` machine type) | **Yes** — mature in-tree (2D, virgl, gfxstream/rutabaga, Venus); also new Rust vhost-device-gpu | Yes — VFIO **and** vGPU (most mature) | Yes (virtio-win) | heavier; not 5–30 ms class; virtio-gpu/VFIO state complicates snapshot | **L1 desktop default**; backstop for W and G |
| **crosvm** | **Yes** — real, maintained (virgl + gfxstream + Wayland) | partial | No | less proven for server multi-tenancy | dark-horse L1; already the Android (Cuttlefish) substrate |
| **Cloud Hypervisor (CLH)** | **No mainline** (only unmaintained out-of-tree Spectrum-OS patches) | **Yes** — production VFIO (Turing/Ampere/Hopper/Lovelace, up to 8 GPUs) | Yes | snapshot experimental **and mutually exclusive with VFIO** | G + W tiers, **not** the fast-fork desktop |

The takeaway: **QEMU with the `microvm` machine type is the safe, supported L1 default** — virtio-mmio + qboot + in-tree virtio-gpu trims attack surface toward Firecracker territory while still presenting a display ([QEMU microvm](https://www.qemu.org/docs/master/system/i386/microvm.html), [QEMU virtio-gpu](https://qemu-project.gitlab.io/qemu/system/devices/virtio-gpu.html)). **crosvm is worth a head-to-head PoC** because virtio-gpu+Wayland is native and microVM-light ([crosvm gpu book](https://crosvm.dev/book/devices/gpu.html), [Cuttlefish](https://source.android.com/docs/devices/cuttlefish)). **Avoid Cloud Hypervisor for desktops:** its only virtio-gpu is the unmaintained out-of-tree [Spectrum-OS patch set](https://spectrum-os.org/software/cloud-hypervisor/) that shells out to a crosvm GPU process over non-standard vhost-user, and the maintainers are [explicitly uninterested](https://spectrum-os.org/lists/archives/spectrum-discuss/87bk1gdfwl.fsf@alyssa.is/) in upstreaming it — never build a product tier on that.

Standardize on **`rutabaga_gfx` as the rendering mental model** (virgl for OpenGL, gfxstream for GLES+Vulkan), and run the renderer **out-of-process** ([vhost-device-gpu](https://crates.io/crates/vhost-device-gpu), [Kernel Recipes 2025](https://kernel-recipes.org/en/2025/schedule/modernizing-virtio-gpu/)) to keep the huge GL/Vulkan attack surface out of the VMM. Because QEMU and crosvm share that library, an L1 desktop image stays portable between them. (Caveat: vhost-device-gpu is at v0.2.x and still lacks blob-resource/Venus support pending QEMU API stabilization — Vulkan-heavy desktop apps may not run out-of-process yet.)

If operational surface must be cut to one VMM, **QEMU-`microvm` is the single unifier**: it covers headless Linux (slower fork than Firecracker but functional), Linux desktop (virtio-gpu), Windows (virtio-win + guest agent), and GPU (VFIO + vGPU) — at the cost of Firecracker-class fork speed. The recommended posture keeps Firecracker as a **performance fast-path for the headless tier only** and QEMU-microvm as the desktop default.

---

## 3. Fork-from-snapshot: the killer primitive (Linux, tiers L0/L1)

This is where Shinken differentiates against OSWorld's terminate-and-reboot revert (tens of seconds to minutes, I/O-bound on disk-delta size — the AWS provider literally `terminate_instances` then `RunInstances` from an AMI with 15 s boto3 waiters; the VMware provider does `vmrun revertToSnapshot` + fixed sleeps). **Make fork-from-snapshot, not restore-once, the core reset primitive** — and expose the same mechanism two ways (reset = re-fork one child; branch = fork N children). See [replay.md](replay.md) for how this maps onto the `.skn` checkpoint DAG (**D5**).

### 3.1 The mechanism stack

```
   golden microVM ──boot once, warm deps──► PAUSE ──► snapshot artifacts:
                                                       ├─ memory.bin  (guest RAM, the CoW source)
                                                       └─ vmstate     (vCPU regs + virtio/device state)

   each child  ═══►  NEW KVM VMM process  ═══►  mmap(memory.bin, MAP_PRIVATE)
                                                  │
                       reads → shared clean pages (the immutable snapshot)
                       writes → private anonymous CoW copies (kernel page-level CoW)
```

1. **Two-file VM snapshot.** Pause the microVM; serialize guest RAM to a memory file (the CoW backing store) plus a tiny vmstate file (vCPU registers + device state). Restore does **not** boot a kernel — it re-mmaps the memory file.
2. **MAP_PRIVATE CoW restore.** With `MAP_PRIVATE`, the guest reads shared clean pages and writes fault into private anonymous copies (kernel page-level CoW). N VMs share one read-only memory file; only **written (dirty) pages cost RAM**. Restore is O(1) in apparent RAM size. AWS quotes VMM-side restore **<30 ms** (vendor-published, unverified).
3. **Parentless fork.** Boot a golden image once with the agent stack warm, snapshot it to an *immutable* memory.bin+vmstate template (not a live parent), then spawn every task/branch as a new KVM VM mapping that same file MAP_PRIVATE. Morph's Infinibranch and the OSS [`forkd`](https://github.com/deeplethe/forkd) reference implementation both demonstrate this exact model.
4. **CoW disk underneath.** A shared read-only base rootfs (built from a Dockerfile as an artifact) plus per-sandbox writable deltas via overlayfs / qcow2 backing chains / device-mapper-thin / btrfs|ZFS clones, so thousands of sandboxes share one base image.

### 3.2 The numbers to design against (all vendor-published, unverified)

| System | Metric | Value |
|--------|--------|-------|
| Firecracker | VMM-side restore | <30 ms (AWS-quoted; VMM setup only, **excludes page-fault population**) |
| Firecracker | per-VM memory overhead | <5 MiB |
| Morph Infinibranch | CoW mmap itself | ~4 µs |
| Morph Infinibranch | end-to-end fork P99 @ 1000 concurrent | ~1.3 ms (cost is ~99.5% KVM VM-create, not the mmap) |
| Morph Infinibranch | shared pages / private overhead | ~93% shared; ~265 KB pre-exec, ~1.75 MB per numpy VM (100 numpy VMs share 2.4 GB) |
| Morph Infinibranch | snapshot / branch / restore | <250 ms |
| `forkd` (OSS) | spawn 100 children | ~101 ms (~1 ms/child) |
| `forkd` (OSS) | BRANCH a live VM | ~150 ms; idle-source pause window ~200 ms |
| `forkd` (OSS) | diff-snapshot win | source downtime 29.3 s → 205 ms (143×) |
| `forkd` (OSS) | density | ~50 idle-pooled agents per 8 GB; CoW metadata ~0.12 MiB/child |
| E2B | snapshot restore / warm-pool ready | ~150 ms / <200 ms; ~128 MB per sandbox |

Sources: [Morph Infinibranch](https://www.morph.so/blog/infinibranch/), [forkd](https://github.com/deeplethe/forkd), [E2B persistence](https://e2b.dev/docs/sandbox/persistence). These are the bar Shinken's Linux runtime must match or beat.

### 3.3 The real cost is page faults, not VMM setup

The most expensive pitfall: **restore latency is dominated by lazily faulting thousands of guest pages off disk one-by-one**, not by VMM setup. REAP measured restore as ~95% slower than warm purely from serial page faults ([REAP, ASPLOS'21](https://marioskogias.github.io/docs/reap.pdf)). If you only measure "VMM restore <30 ms," you ship a system that is actually hundreds of ms slow on first touch. The fixes:

- **Record-and-prefetch (REAP).** On the first run, record (via userfaultfd) the exact working set a snapshot touches (8–99 MB, ~24 MB avg), then prefetch the whole set in **one sequential read** before resuming vCPUs — eliminates ~97% of critical-path faults, ~3.7× faster restore. ~200 LoC added to Firecracker.
- **userfaultfd page-fault delegation.** Hand the guest-memory fault FD to a userspace handler so faults can be batched, prefetched, or fetched **remotely** from a page server for disaggregated pools ([Firecracker UFFD docs](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/handling-page-faults-on-snapshot-resume.md), [FaaSnap EuroSys'22](https://www.sysnet.ucsd.edu/~voelker/pubs/faasnap-eurosys22.pdf)). FaaSnap adds mincore()+madvise(WILLNEED)+concurrent region loading to handle input-varying workloads where a fixed working set drifts.
- E2B's production stack is the reference: UFFD lazy memory, memfd background streaming, 2 MB HugePages, virtio free-page hinting, **4 KiB-page dedup of memory diffs against base templates** ([E2B Firecracker integration](https://deepwiki.com/e2b-dev/infra/3.2-firecracker-integration), [E2B overlayfs blog](https://e2b.dev/blog/scaling-firecracker-using-overlayfs-to-save-disk-space)).

> **Caveat:** working-set prefetch assumes a *stable* working set (REAP: 97% page reuse). When agent inputs vary widely (different files/screenshots/datasets), off-working-set faults reappear — so the restore daemon needs a fallback concurrent userfaultfd loader, not just a fixed prefetch list.

### 3.4 CoW disk: pick by write pattern

| Mechanism | Granularity | Best for | Failure mode |
|-----------|-------------|----------|--------------|
| **overlayfs** (RO lower + RW upper) | whole-file copy-up on first write | shared rootfs + per-task delta; shared page cache (default for Linux microVM tier) | large-file edits incur big copy latency; reported E2B multi-resume persistence edge cases |
| **device-mapper thin** | block-level | sharing one RO rootfs across many microVMs with per-VM deltas; avoids whole-file copy-up | operationally fiddly (pool sizing, low-space failure modes) |
| **qcow2 backing chains** | image-format level | the QEMU / CLH (W/G) tier — instant child clones | read amplification + metadata lookups grow down long chains (periodic flatten/rebase) |
| **btrfs / ZFS clones** | block-level CoW | host-side base-image fan-out; large mutable files; integrity/replication | VM-image random-overwrite is a CoW worst case; qcow2-on-CoW-fs is double-CoW (set nodatacow/preallocation) |

**Track per-sandbox PRIVATE (dirty) page RSS, not snapshot size, as the density metric and the scheduler bin-packing key** — the ~93% shared-page advantage erodes as a workload writes memory. Bin-pack and admission-control on private RSS or fork-density projections will be wildly optimistic.

### 3.5 The uniqueness hook is mandatory, day one

Forked clones share PRNG/CSPRNG state, MAC/IP, hostname, `boot_id`, clock, saved random-seed files, and TLS/session tokens. **Resuming the same snapshot state more than once without reseeding is a crypto/security vulnerability, not a correctness nit.** Firecracker's VMGenID device auto-reseeds the in-kernel CSPRNG on guest Linux ≥ 5.18 ([random-for-clones docs](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/random-for-clones.md), [VMGenID spec](https://uapi-group.org/specifications/specs/vmgenid/), [Restoring Uniqueness in MicroVM Snapshots](https://arxiv.org/abs/2102.12892)) — but that covers **only** the kernel pool, with a race window between vCPU resume and reseed.

The Guest Runtime's **post-fork hook** must, on every fork: reseed userspace PRNGs (numpy/openssl), regenerate MAC/IP/hostname/`machine-id`/SSH host keys/`boot_id`, resync the clock, delete `/var/lib/systemd/random-seed` before snapshotting, rotate any TLS/session tokens, and **re-register the Guest Runtime with the control plane**. This is the *same problem on every OS*: Linux reseeds CSPRNG + regenerates IDs; Windows relies on sysprep `/generalize` for fresh SID + cloudbase-init for hostname/keys; macOS regenerates host identifiers + MAC + machine-identifier after the APFS clone. The uniqueness step is non-negotiable across all three.

> Also: a resumed/forked VM must keep its memory backing file alive for its **entire lifetime** (it is the CoW source). You cannot GC the snapshot while any fork still references it — this constrains storage layout and snapshot lifecycle. And keep fork-tier snapshots **single-vCPU-friendly**: multi-vCPU forks multiply fork time and complicate consistent capture of in-flight vCPU state. For parallel agent rollouts, prefer many single-vCPU forks over a few fat VMs.

### 3.6 Process-level analogues (for the GPU/process-granular tier)

Where the isolation boundary is an outer microVM or gVisor, cheaper finer-grained snapshots are available: **CRIU** (process-tree checkpoint/restore, `--lazy-pages` page-server for userfaultfd-style on-demand paging — [criu(8)](https://manpages.ubuntu.com/manpages/bionic/man8/criu.8.html)) and **gVisor native checkpoint/restore** (Sentry C/R + FUSE lazy-loading lower layer + prioritized background page loading; ~2.5× faster than cold, GPU memory snapshots up to ~10× — [Modal mem-snapshots](https://modal.com/blog/mem-snapshots), [gVisor C/R](https://gvisor.dev/docs/user_guide/checkpoint_restore/)). These are the basis for process-level RL tree-search rollouts ([Tree-GRPO](https://arxiv.org/abs/2509.21240)) and the only practical fast-restore path on the GPU tier where Firecracker cannot passthrough. CRIU is brittle on external state (open sockets, GPU contexts, some namespaces) and cross-CPU-feature restore — do not assume it "just works" for arbitrary agent processes.

---

## 4. Windows — a licensing-and-isolation problem more than a tech problem

Windows is virtualizable on commodity x86 cloud hardware (Hyper-V / Cloud Hypervisor / QEMU+KVM), and Microsoft itself ships the reference agent stacks. The constraint is licensing, not the hypervisor.

**Scale tier (W).** Full Windows VMs from a **sysprep golden-image pipeline**: install + patch + apps + cloudbase-init, then `sysprep /generalize /oobe /shutdown` with a cloudbase-init `Unattend.xml` so each clone gets a fresh SID/hostname/networking and runs first-boot config. **cloudbase-init is cloud-init for Windows** ([guide](https://xen-orchestra.com/blog/windows-templates-with-cloudbase-init-step-by-step-guide-best-practices/)) — sysprep `/generalize` *is* the Windows analogue of the Linux uniqueness step. Use **virtio-win + the QEMU guest agent** for quiesced (VSS) snapshots and qcow2/dm-thin for CoW disk clones. There is **no sub-second CoW-fork class reset for Windows in practice** — design this tier around warm pools and full reboots, not memory fork. The de-facto reference control model is [WindowsAgentArena](https://github.com/microsoft/WindowsAgentArena) (Win11-in-Docker via QEMU/KVM, in-guest Python command server, 154 tasks, Azure-ML fan-out) and OmniTool/OmniParser; the native structured channel is **UIAutomation (UIA)** via [pywinauto](https://github.com/pywinauto/pywinauto) — stream UIA deltas alongside frames to cut bandwidth (see [streaming-bandwidth.md](streaming-bandwidth.md)).

**Dev / single-tenant tier (W-dev).** [Windows Sandbox](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/) gives genuine Hyper-V kernel isolation, clean-slate on every launch, ~35 s boot, scriptable via `.wsb` (LogonCommand bootstrap). But it is **strictly one instance per host** with no snapshots/persistence by design — a developer / low-density tier, **not** a scale substrate. trycua/cua already drives it via a WINSANDBOX provider, proving the agent-bootstrap pattern ([cua: Windows Sandbox](https://cua.ai/blog/windows-sandbox)).

**Do NOT use Microsoft Dev Box** as the untrusted-agent substrate — it is a managed Entra/Intune-joined corporate developer desktop (now in maintenance mode), not an ephemeral hostile-workload sandbox boundary. The managed competitor to benchmark against is **Windows 365 for Agents** (announced Jan 2026: ephemeral check-out/check-in Cloud PCs with create/control/observe APIs — [Windows Experience blog](https://blogs.windows.com/windowsexperience/2026/01/22/windows-365-for-agents-the-cloud-pcs-next-chapter/)). Shinken's wedge is **cross-platform unification** (one API spanning Windows AND macOS), which Microsoft does not offer.

**Licensing is the gating constraint — resolve it before scaling.** Get procurement to confirm the buyable path first:

| Path | Rule |
|------|------|
| Windows Server, unlimited VMs/host | **Datacenter, licensed per physical core** (min 8 cores/proc, 16/server) via SPLA/CSP |
| Windows Server, Standard | 2 VMs/host, stackable |
| Windows Server BYOL on AWS | forces **EC2 Dedicated Hosts** (no License Mobility for Windows Server; license by physical core) |
| Windows 11 *client* guests | need **Multitenant Hosting Rights** (Win11 Ent E3/E5 or AVD per-user) |
| SPLA reporting on Listed Providers (AWS/GCP/Azure/Alibaba) | allowed only until **Sept 30, 2025** (changed) |
| Windows-11-on-Arm (Azure marketplace) | currently lacks Trusted Launch / secure-boot / TPM — not a drop-in |

(Public licensing facts: [Windows Server 2025 licensing](https://www.microsoft.com/licensing/guidance/Windows-Server-2025), [Win11 virtual-desktop licensing](https://www.microsoft.com/licensing/guidance/Windows-11-Licensing-for-Virtual-Desktops), [AWS BYOL on Dedicated Hosts](https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/byol-ded-hosts.html). Windows Server PAYG cloud-billing ~$33.58/core/month, ~$0.046/core/hour — vendor-published, unverified.)

---

## 5. macOS — the hard wall

macOS is the immovable constraint, and **no software trick removes it.** Apple's SLA permits macOS virtualization **only on Apple-branded hardware** via Apple's Virtualization.framework (VZVirtualMachine), and the framework (enforced in closed-source XNU) **hard-caps a single physical Mac at 2 concurrent macOS guests** — the third returns `VZErrorDomain` code 6 ([Apple macOS Sequoia SLA](https://www.apple.com/legal/sla/docs/macOSSequoia.pdf), [Eclectic Light: how Apple limits VMs](https://eclecticlight.co/2022/08/04/virtualisation-on-apple-silicon-macs-8-how-apple-limits-vms/), [VZVirtualMachine](https://developer.apple.com/documentation/virtualization/vzvirtualmachine)). The full constraint set:

- **Apple hardware only** — no commodity-cloud path exists; you cannot legally run macOS guests on x86/ARM Linux/KVM hosts.
- **2 macOS VMs per physical Mac**, regardless of CPU/RAM. Density scales **only by adding Macs**: capacity = (number of Macs × 2) − host overhead, and this must be a **first-class scheduler constraint.**
- **arm64 guests only; no nested virtualization** you can rely on for density.
- Guests **cannot use Apple ID / iCloud / App Store / FairPlay-DRM video** (no Secure Enclave access) — any task needing Apple sign-in or App Store apps will fail. **Validate target tasks against this before promising macOS coverage.**
- **TCC pre-grant required:** Accessibility (read+act on the [AXUIElement](https://developer.apple.com/documentation/applicationservices/axuielement_h) tree, ~50 ms reads, far cheaper than pixel parsing) and Screen Recording (ScreenCaptureKit). Bake these grants into the golden image; ephemeral guests otherwise stall on prompts.

**Substrate and orchestration.** The legal substrate is Apple's Virtualization.framework, driven by open tooling: **tart** (+ Orchard for fleet scheduling) or **lume** (OCI-registry VM images, APFS copy-on-write fast clone, localhost HTTP control API) — the [tart](https://github.com/cirruslabs/tart)/[cua lume](https://github.com/trycua/cua) model. Commercial options are **Anka** (Registry + Controller, ephemeral VMs, per-host license) and **MacStadium Orka** (managed Mac cloud, [Orka](https://macstadium.com/orka)). The "reset" primitive is an **APFS `clonefile(2)` instant CoW clone** of the base disk + MAC/machine-identifier regeneration (cua's `Home.cloneVMDirectory` does exactly this) — **not** a live memory-fork. There is no Firecracker-class sub-second live fork on macOS; replay degrades gracefully to disk-snapshot + deterministic event-replay (see [replay.md](replay.md)).

**Economics.** macOS is the expensive, low-density, lowest-elasticity tier. [AWS EC2 Mac](https://aws.amazon.com/ec2/instance-types/mac/) (`mac2-m2.metal`: M2, 8 CPU / 10 GPU cores, 24 GiB) is a **Dedicated Host with a 24-hour minimum allocation** (Apple SLA) at ~$6.50/hr (~$4,700/mo on-demand; vendor-published, unverified) — bursty/ephemeral macOS is economically punishing, so **pre-provision a steady Mac pool** (or use MacStadium/colo bare metal where you control reuse cadence) rather than promising commodity elasticity.

---

## 6. The optional GPU tier (D11) — opt-in, snapshot-hostile

**Make GPU opt-in, not default.** The vast majority of agent/code/browser tasks are CPU-only and should ride the Firecracker / QEMU-microvm CoW-fork tier. Only route a task to a GPU node when it explicitly needs hardware 3D/WebGL rendering, CUDA/ML inference, or hardware-accelerated encode. This keeps the expensive, snapshot-hostile GPU pool small. The public NVIDIA mechanisms (framed as technology options) trade isolation against density:

| Mechanism | Isolation | Density | Snapshot/fork? | Use |
|-----------|-----------|---------|----------------|-----|
| **MIG** (Multi-Instance GPU) | HW memory + fault isolation + QoS; dedicated SMs/L2/mem **and dedicated NVENC/NVDEC per slice** | coarse: up to 7 slices/A100·H100, up to 4 on RTX PRO 6000 Blackwell; min slice ~5–10 GB | **No** (non-migratable device state) | a handful of isolated heavyweight tenants |
| **Time-slicing** | **none** (no per-replica mem cap, no fault isolation) | highest; any GPU incl. L4/L40S/A40 | No | trusted, bursty, *inside* a VM/MIG boundary only |
| **MPS** | weak (fatal fault can propagate) | high; concurrent kernels | No | trusted same-tenant batch packing |
| **vGPU** | per-VM frame-buffer partition (mem isolation); MIG-backed adds HW isolation | double-bounded by frame buffer **and** a context-switch cap | **No** (device state non-snapshottable) | **the agent-desktop tier** (rendering + encode into the guest) |
| **VFIO full passthrough** | strongest single-tenant | **1 GPU = 1 VM** (worst density) | **No** (`State blocked by non-migratable device vfio-pci`) | rare max-perf single-tenant |

(Public NVIDIA facts: [MIG](https://www.nvidia.com/en-us/technologies/multi-instance-gpu/), [vGPU scheduling policies](https://docs.nvidia.com/ai-enterprise/release-8/latest/infra-software/vgpu/features/scheduling.html), [Kubernetes GPU sharing](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html), [Colfax: time-sliced vs MIG-backed](https://research.colfax-intl.com/sharing-nvidia-gpus-at-the-system-level-time-sliced-and-mig-backed-vgpus/).)

**Run two distinct GPU pools.** *Pool A* = ultra-high-concurrency light desktops on a no-MIG large-VRAM card (L4 24 GB / L40·L40S 48 GB) running **time-sliced vGPU** with small (~1–2 GB) frame-buffer profiles. *Pool B* = isolation-sensitive or heavy agents on a **MIG-capable** card (A100/H100/RTX PRO 6000 Blackwell), optionally MIG-backed vGPU and **Confidential Containers** for the trusted variant (GPU-TEE + NRAS attestation). For untrusted GPU agents that need hardware isolation, prefer **MIG-backed vGPU or Kata GPU passthrough with a MIG slice** over raw time-slicing/MPS (which let one tenant OOM or crash the whole GPU).

**Size desktop concurrency by frame buffer first, then validate against the context-switch ceiling.** A ~1–2 GB profile suggests an L40/L40S 48 GB card holds ~24–48 sessions *by memory*, but NVIDIA documents a hard **~32-user context-switch cap on the 48 GB A40** despite memory headroom ([A40](https://www.nvidia.com/en-us/data-center/a40/)) — so plan ~24–32 concurrent light desktops per 48 GB card and benchmark before pushing higher. **Pick the scheduler policy deliberately:** Equal Share (fair, multi-tenant) or Fixed Share (per-agent SLA), **never** Best Effort for untrusted multi-tenant. Instrument per-session DCGM metrics (per-vGPU/MIG VRAM, SM occupancy, NVENC session count, context-switch latency) and set a hard max-sessions-per-card guardrail.

**Two NVENC facts for the encode tier** (cross-reference [streaming-bandwidth.md](streaming-bandwidth.md) and **D11**): (1) the **8-concurrent-session NVENC cap is consumer-GeForce-only** — qualified professional/datacenter GPUs are limited only by encoder hardware throughput (up to 3 NVENC engines/chip; an L4/L40 node sustains 1000+ AV1 720p30 streams — [L4](https://www.nvidia.com/en-us/data-center/l4/), [NVENC app note](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html); vendor-published, unverified). (2) Per **D11**, the **encode tier NEVER runs on A100/H100/H200/B200 (zero NVENC engines)** — use **Ada L4** (density) or **L40S** (premium 4K/AV1+render). NVENC will not be the concurrency bottleneck; frame buffer and SM context-switching will be.

> **The architectural asymmetry to design around:** VFIO passthrough and vGPU device state **cannot be snapshotted or live-migrated**, so the sub-second VM-fork reset that defines the Linux CPU tier **does not exist on the GPU tier** ([Proxmox: VFIO non-migratable](https://forum.proxmox.com/threads/cannot-snapshot-vm-vfio-migration-not-supported.179190/)). GPU agent desktops are **longer-lived, recycled-on-TTL** instances; reset state at the application/filesystem layer or via full VM reboot from a golden image. The only viable fast-GPU-restore path is gVisor-GPU + CRIU GPU memory snapshots (Modal-style), accepting software (not KVM) isolation. Also note vGPU is mid-transition from mdev to a vendor-specific VFIO/SR-IOV framework (kernel 6.8+): pin known-good vGPU+kernel+hypervisor combos, provision devices **before** VM start (no hotplug), and budget for fragile guest-driver installs (especially on Cloud Hypervisor).

---

## 7. Building on the OSS `kubernetes-sigs/agent-sandbox` CRD

Shinken does **not** reinvent fleet orchestration. The Fleet Manager (**D9**) is shaped like the standard Kubernetes agent-sandbox pattern: the OSS [`kubernetes-sigs/agent-sandbox`](https://github.com/kubernetes-sigs/agent-sandbox) **CRD** declares a Sandbox resource; pods run under hardened **runtime classes** (gVisor / Kata) with pre-warmed pools per image/region/tier. This gives the container fast-path for the Linux tiers and a clean CRD shape the control plane reconciles against, while the VM tiers (Firecracker / QEMU-microvm / CLH / Apple VZ) plug in beneath the same Sandbox abstraction.

The substrate must be a **strategy, not a single binary** — a VMM abstraction (in the spirit of Kata's pluggable backend) routing by the **(OS × needs-GPU × needs-fast-fork)** triple to the right pool. The lifecycle API copies the proven E2B/cua shape: `create(template, os, gpu?, fast_reset?, timeout, metadata, allow_internet_access)` + `connect(id)` (auto-resume a paused box) + auto-pause-on-idle + `kill`, with timeout reset on connect, and **`snapshot / fork / branch / restore` as first-class verbs** stored as a checkpoint DAG (**D5**). **Dual-timer sessions** per **D9** (idle ~15 min reset-on-activity; max-lifetime ~4–8 h; **auto-suspend-to-snapshot on idle**, since idle cost dominates) and storage/compute separation (the Morph/Blaxel pattern: idle states at ~zero compute, resumable in tens of ms — [Blaxel standby](https://blaxel.ai/blog/sandbox-management-for-ai-coding-agents)) are the backing store for the scrubbable, forkable timeline.

**The unified in-guest bootstrap and transport.** Standardize **one Guest Runtime (`shinkend`) contract** — screenshot + input + shell/file ops + the ACI event stream — packaged three ways, started by the OS-native first-boot mechanism:

| OS | First-boot mechanism | Control transport |
|----|----------------------|-------------------|
| Linux | cloud-init / systemd unit baked in the rootfs | **virtio-vsock** (host↔guest, never HTTP polling) |
| Windows | cloudbase-init RunOnce / sysprep specialize | **guest-initiated outbound TCP/WebSocket** (no vsock guest driver) |
| macOS | LaunchDaemon baked into the tart/lume image | **guest-initiated outbound TCP/WebSocket** (VZ has no vsock for this) |

The transport is **not uniform**: virtio-vsock is the low-overhead host↔guest channel on Linux/QEMU (and carries structured observation/action + frame deltas — the bandwidth win in [streaming-bandwidth.md](streaming-bandwidth.md)), but **Windows has no vsock guest driver and Apple's VZ lacks it**, so the portable fallback is a **guest-initiated outbound TCP/WebSocket callback** to the control plane (works through the per-sandbox egress firewall). Every guest must **re-register after each fork/clone/sysprep** so identities stay unique (§3.5). **Bake the Guest Runtime into golden images with pinned versions** — do not `pip/uv add` on every boot (cua's first-boot install is a hard network dependency and a cold-start tax to avoid). Gate "VM ready" on the Guest Runtime's `/status`, not just power-on, and gate the display client on a non-black framebuffer.

The host knobs the **Permission Panel** (**D6**) actuates per session live on this substrate: TAP-per-VM + iptables/eBPF egress gating (the out-of-VM egress proxy), per-VM token-bucket bandwidth/ops rate limiters (Firecracker/E2B), VFIO/MIG GPU attach/detach, and the privileged-install "unlock." Sandbox health is a **circuit-breakable dependency** in the control plane (**D9**). See [permissions.md](permissions.md) for the full capability model.

---

## 8. Reconciliation to D1–D11, and the carried gaps

| Decision | How this note lands it |
|----------|------------------------|
| **D1** — tiered, substrate-pluggable, routed by (OS × GPU × fast-fork) | The §1 matrix *is* D1. Firecracker (L0 headless) + QEMU-microvm/crosvm (L1 desktop, virtio-gpu, fork-capable) + CLH/QEMU+VFIO (G + Windows, no fork) + Apple VZ (M, 2/Mac) + Cuttlefish/crosvm (Android roadmap). Reset = fork-from-snapshot on Linux; instant reset and replay-branch are the same primitive. |
| **D5** — replay = event stream + bisected snapshots, branchable `.skn` | §3 fork-from-snapshot is the branch mechanism; checkpoint DAG node = immutable memory+disk snapshot; "fork down a new path" = CoW child resuming warmed state. Degrades to disk-snapshot + event-replay on W/M. |
| **D6** — capability-unlock permission panel | §7 enumerates the host knobs (egress proxy, rate limiters, GPU attach, privileged unlock) the panel actuates per session; uniqueness/egress are first-class. |
| **D9** — Fleet Manager + Action Gateway + dual-timer | §7: warm pools + fork-on-demand on the `agent-sandbox` CRD shape; auto-suspend-to-snapshot on idle; circuit-breakable health. |
| **D10** — one control plane, one Guest Runtime, one ACI; per-OS factory beneath | §1 + §7: control plane/Guest Runtime/ACI above the matrix; per-OS handler-factory + substrate router beneath. |
| **D11** — optional GPU tier; encode never on A100/H100/H200/B200 | §6: opt-in, two pools (time-sliced vGPU density / MIG-backed + Confidential Containers trusted); encode on Ada L4 / L40S only; GPU tier has no fast-fork. |

**Carried gaps (do not paper over — tracked in [open-questions.md](open-questions.md)):**

1. **a11y-coverage on Electron/Qt/canvas/games** is the load-bearing unverified assumption for the structured fast-path observation model (and for choosing X11+AT-SPI over Wayland on L1) — needs a measurement spike.
2. **No first-party perf numbers.** Every fork/restore/density figure in §3 is vendor-published; Shinken needs its own measurement plan before publishing SLAs, and must measure *page-fault-bound* restore, not just VMM setup.
3. **macOS / Windows fast-reset is largely infeasible today** — replay must degrade gracefully there; the platform asymmetry is permanent.
4. **Windows-in-cloud licensing and the macOS 2-VM/host + 24 h Dedicated-Host minimum** shape cost and roadmap — procurement must confirm the buyable Windows path; macOS is premium/scarce capacity, not commodity.
5. **GPU tier non-snapshottability** is an architectural asymmetry, not a bug to fix; design GPU sessions around TTL recycling.
6. **crosvm vs QEMU-microvm for L1** is an open PoC; standardize on `rutabaga_gfx` so the desktop image stays portable between them.
