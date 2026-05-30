# P0 deep-dive — action backends, focused capture, cross-OS replay, harness compat

Consolidated findings from the Phase-0 deep-dive (2026-05-30), feeding the ACI spec ([docs/11-aci-spec.md](../docs/11-aci-spec.md)) and M1. Vendor-neutral; public NVIDIA product facts (NVENC/NICE DCV) appear only as cited technology options. Numbers are vendor-published and unverified unless first-party.

## Linux action-execution backends for a computer-use Guest Runtime (shinkend), beyond pyautogui

There is no single Linux input-injection API; the right backend depends on the display server (X11 vs Wayland), whether you run headless, and whether you can target by element instead of coordinate. On X11 the mature, low-friction path is the XTEST extension (used by xdotool/PyAutoGUI/xte): it injects events into the server's input pipeline so apps accept them, but it is coordinate-and-focus-bound — keystrokes land on whatever window has focus (focus-steal), it cannot reliably target an unfocused window, it has keymap/Unicode quirks, and it does not touch the clipboard (X selections are a separate protocol). XSendEvent can target a specific window without focus but carries a "synthetic" flag (SendEvent mark of shame) that many apps reject, so it is unreliable for real automation. Wayland deliberately removed X-era emulation, fragmenting the replacement space: the kernel uinput path (ydo…

**Recommendations**
- Architecture fit: this slots into shinkend's per-OS handler factory (D2/D10) behind the canonical typed action union, and into the structured-first observation engine (D3). Build a Linux executor with a Backend trait/ABC {move, click, double_click, drag, scroll, key, type/insert_text, set_clipboard} plus capability fl…
- P0 (v1, Linux headless+desktop fork tiers, which are X11 by design): (1) XTEST as the universal pixel/coordinate backend (via libxdo or direct libXtst, not shelling out to xdotool per call) — drives every point_px/point_norm action; pin a keyboard layout on the XTEST virtual keyboard and bake a transient-keymap path f…
- Why X11-first for v1: Shinken's Linux fork tier renders software/virtio-gpu and pixel-streams the framebuffer (D1/D3) — you control the session, so choosing X11 (Xvfb/Xorg-dummy) sidesteps the entire Wayland input-fragmentation tax while keeping CoW-fork compatibility (XTEST/AT-SPI/CDP are pure userspace, nothing to l…
- Later (when a Wayland guest is required, e.g. testing Wayland-only apps or GNOME/KDE fidelity): add a Wayland backend group. Prefer libei/libeis via the XDG RemoteDesktop portal (the cross-compositor, unprivileged, GNOME/KDE/wlroots-converging path; supports absolute pointer, keyboard, touch) — for an unattended guest…
- Explicitly DO NOT: make PyAutoGUI/raw-XTEST the sole strategy (coordinate-only, focus-stealing, X11-only — exactly pyautogui's ceiling); rely on XSendEvent for general input (apps reject the synthetic flag); or assume one Wayland API works everywhere (Mutter/KWin reject the wlr protocols; bare uinput is relative-only …
- Priority order at actuation time (encode as router preference): for a browser target -> CDP; for an element_ref with a valid a11y action -> AT-SPI2 do_action; otherwise pixel coordinate -> XTEST (X11) or libei/uinput (Wayland); clipboard -> X selections / wl-clipboard. Always record the chosen backend and the resolved…

**Numbers (vendor-published, unverified)**
- PyAutoGUI screenshot() ~100 ms at 1920x1080; locate() ~1-2 s — screenshot latency argues for structured-first observation over per-step pixels
- XTEST events enter the server input pipeline directly (sub-ms injection), accepted by apps because they lack the synthetic flag
- ydotool needs ydotoold to hold the virtual device persistently because a freshly created uinput device takes time for X11/Wayland to recognize (recognition delay)
- CDP/Playwright-class in-browser injection avoids any display-server round-trip — lowest-latency path for the browser workload
- D-Bus per-call overhead makes AT-SPI do_action slower than direct XTEST injection for high-rate input, but it is element-stable and resolution-independent

**Sources**
- [XTEST Extension Protocol (X.Org)](https://www.x.org/releases/X11R7.7/doc/xextproto/xtest.html)
- [XTEST Extension Library (X.Org)](https://www.x.org/releases/X11R7.7/doc/libXtst/xtestlib.html)
- [X's two ways to send events to X clients (XTEST vs XSendEvent, the 'mark of sha…](https://utcc.utoronto.ca/~cks/space/blog/unix/XTwoWaysToSendEvents)
- [xdotool — fake keyboard/mouse input, window management](https://github.com/jordansissel/xdotool)
- [xdotool manpage (XTEST usage, type/key, window targeting)](https://manpages.ubuntu.com/manpages/trusty/man1/xdotool.1.html)
- [Exploring the Fragmentation of Wayland, an xdotool adventure (xdotool author)](https://www.semicomplete.com/blog/xdotool-and-exploring-wayland-fragmentation/)
- [xdotool key/type breaks with multiple keyboards / different layouts (XTEST keym…](https://github.com/jordansissel/xdotool/issues/150)
- [PyAutoGUI (Linux uses Xlib XTEST; screenshots via PIL/scrot; focus limitation)](https://github.com/asweigart/pyautogui)
- [PyAutoGUI documentation (cheat sheet / screenshot functions)](https://pyautogui.readthedocs.io/en/latest/quickstart.html)
- [ydotool — generic command-line automation tool (uinput, root/permissions)](https://github.com/ReimuNotMoe/ydotool)
- [evemu — create a virtual input device and replay an event sequence (manpage)](https://manpages.ubuntu.com/manpages/bionic/man1/evemu-device.1.html)
- [Multitouch/Testing/Evemu — Ubuntu Wiki](https://wiki.ubuntu.com/Multitouch/Testing/Evemu)
- [wlr-virtual-pointer-unstable-v1 protocol (wlroots)](https://github.com/swaywm/wlroots/blob/master/protocol/wlr-virtual-pointer-unstable-v1.xml)
- [wlr virtual pointer protocol — Wayland Explorer (KWin/Mutter do not implement)](https://wayland.app/protocols/wlr-virtual-pointer-unstable-v1)
- [libwldevices-go (zwlr_virtual_pointer + zwp_virtual_keyboard bindings, composit…](https://github.com/bnema/libwldevices-go)
- [Who-T: libei — a library to support emulated input (Peter Hutterer)](http://who-t.blogspot.com/2020/08/libei-library-to-support-emulated-input.html)
- [libei 1.0 Released For Better Supporting Emulated Input On Wayland — Phoronix](https://www.phoronix.com/news/libei-1.0-Emulated-Input)
- [Input emulation on Wayland via libei and RemoteDesktop portal (RustDesk discuss…](https://github.com/rustdesk/rustdesk/discussions/4515)
- [XDG Desktop Portal — RemoteDesktop interface (Notify* + ConnectToEIS)](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
- [gnome-remote-desktop (PipeWire capture + libei input + Mutter RemoteDesktop API)](https://github.com/GNOME/gnome-remote-desktop)
- [Atspi.Action.do_action (AT-SPI2 element action invocation)](https://docs.gtk.org/atspi2/method.Action.do_action.html)
- [Atspi.Accessible (AT-SPI2 accessible tree)](https://docs.gtk.org/atspi2/class.Accessible.html)
- [pyatspi2 — Python bindings for AT-SPI](https://github.com/GNOME/pyatspi2)
- [AT-SPI2 architecture (a11y D-Bus bus; GTK/Qt/WebKit bridges; headless setup)](https://gnome.pages.gitlab.gnome.org/at-spi2-core/devel-docs/architecture.html)
- [AT-SPI on D-Bus (toolkit support, enabling env vars)](https://wiki.linuxfoundation.org/accessibility/atk/at-spi/at-spi_on_d-bus)
- [Chrome DevTools Protocol — Input domain (dispatch*, insertText, imeSetCompositi…](https://chromedevtools.github.io/devtools-protocol/tot/Input/)
- [X11: How does 'the' clipboard work? (selections PRIMARY/CLIPBOARD, not input in…](http://www.uninformativ.de/blog/postings/2017-04-02/0/POSTING-en.html)
- [e2b desktop environment overview (Xvfb + XFCE + xdotool headless sandbox)](https://deepwiki.com/e2b-dev/desktop/5.1-environment-overview)

## macOS and Windows action-execution backends and background-window injection for Shinken ACI executor

Two modes: in an isolated headless guest the agent is the only user so focus-steal is moot (use a system-wide synthesizer plus a semantic accessibility path); on a shared host drive one app without moving the cursor or stealing focus (the Codex/cua-driver pattern). macOS background: AXUIElement actions plus per-pid CGEventPostToPid and private SkyLight SLEventPostToPid with yabai focus-without-raise. Windows background: UIAutomation Invoke patterns plus PostMessage; SendInput is the foreground path. Cross-platform libs (pyautogui/pynput, nut.js, RobotGo, autopy) are system-wide with no per-window targeting; SikuliX is vision-only. Headless: macOS CGWarpMouseCursorPosition/capture need a display; Windows input is dead in Session 0 / disconnected RDP (autologon WinSta0); Linux needs a virtual display then XTest works. trycua cua-driver (MIT, ~33 tools) is the reference background implemen…

**Recommendations**
- macOS in-guest: AXUIElement default + CGEventPostToPid fallback; avoid CGWarpMouseCursorPosition; pre-grant TCC.
- Windows in-guest: UIA Invoke default + SendInput fallback; autologon to WinSta0. Linux: XTest over AT-SPI on a virtual display.
- P1 background mode via SkyLight+yabai (macOS) and UIA+dispatch (Windows); vendor cua-driver (MIT).

**Numbers (vendor-published, unverified)**
- cua-driver: kAXSelectedText one atomic AX call; 33 tools; macOS cap 2 VMs/host (vendor-published, unverified).

**Sources**
- [trycua/cua - Inside macOS window internals (SkyLight, yabai)](https://github.com/trycua/cua/blob/main/blog/inside-macos-window-internals.md)
- [Microsoft - UI Automation Control Patterns Overview](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controlpatternsoverview)
- [FireDaemon KB - Windows Session 0 Isolation](https://kb.firedaemon.com/support/solutions/articles/4000086228-microsoft-windows-session-0-isolation-and-interactive-services-detection)

## Cross-OS screen/window/region capture and continuous video, including focused-app (occluded-window) capture and the encoder hand-off, for Shinken's P0 capture layer

Modern per-OS capture has converged on compositor-backed APIs that can grab a single app/window even when it is occluded or in the background — the focused-app capture pattern that trycua/cua's cua-driver exploits (per-window `screencapture -l <windowID> -x -o` on macOS, no permission dialog, captures background windows) and that neko does NOT do (neko captures the whole X11 root via GStreamer ximagesrc). On macOS the modern API is ScreenCaptureKit: an SCContentFilter(desktopIndependentWindow:) always includes full window content even when the source window is off-screen or occluded, is display/Space independent, and delivers IOSurface-backed CMSampleBuffers with dirty-rects and showsCursor cursor compositing; the legacy CGWindowListCreateImage is obsoleted in macOS 15 (per-window still possible but one-shot, slow). On Windows, Windows.Graphics.Capture (WGC, 1903+) captures a single win…

**Recommendations**
- P0 capture contract: expose ONE schema with three operations sharing a single capture source per OS — (a) screenshot(target, region?, format, quality, max_dim), (b) start_video(target, fps, codec) returns stream, (c) focused-app screenshot AND focused-app video(window_id/pid). target in {display:N, region, window:id, …
- macOS P0: ScreenCaptureKit for everything. SCScreenshotManager (macOS 14+) for screenshot-per-step (display, region via contentRect, per-window incl. occluded via desktopIndependentWindow). SCStream for continuous video and focused-app video — the desktopIndependentWindow filter is the focused/occluded capability (ful…
- Windows P0: Windows.Graphics.Capture (WGC) as the primary for ALL of screenshot, screen video, and focused-app screenshot/video — it is the only Windows API that does per-window AND occluded/background-window capture, outputs GPU D3D11 textures, and works cross-GPU. Use CreateForWindow for focused-app, CreateForMonito…
- Linux P0: detect session type. Wayland uses xdg-desktop-portal ScreenCast + PipeWire as the portable primary; request restore_token + persist_mode=until-revoked so the agent never re-prompts after first consent; use cursor_mode=metadata or embedded; consume dmabuf into GStreamer. Probe AvailableSourceTypes for WINDOW …
- Encoder hand-off (all OS): keep frames on the GPU and feed GStreamer's nvcodec (nvh264enc/nvh265enc/NVENC) exactly as neko does, with realtime tuning (NVENC rc-mode=cbr low-latency; x264 tune=zerolatency speed-preset=veryfast bframes=0; vp8 deadline=1 cpu-used=4). Hand-off per OS: macOS IOSurface to VideoToolbox (h264…
- Two-path strategy (the core architectural call): run BOTH paths off the SAME capture source. (1) Screenshot-per-step for the AGENT observation loop — on-demand, downscaled to the model's true vision resolution (cua caps at 1568px long-side / 1920px width so the click coordinate frame equals the image the model sees), …

**Numbers (vendor-published, unverified)**
- macOS SCK: minimumFrameInterval caps fps (e.g. CMTime(1,60) approx <=60fps) and SCK never delivers faster than genuine content changes; queueDepth default 3, range 3-8 (more surfaces = better fps but…
- cua computer-server screenshot: full screen, downscaled to max width 1920 (LANCZOS), PNG or JPEG (quality 1-95), base64 (~31KB inline noted). Pull-per-request, one full frame per observe — high per-o…
- neko ABR/encoder defaults: GCC estimator initial 1 Mbit, read interval 2s, stable 12s / unstable 6s / stalled 24s, downgrade backoff 10s, upgrade backoff 5s, diff threshold 0.15 — conservative (slow …
- WGC requires Win10 1903 (SDK 18362)+; borderless requires Win11. DXGI DDA accumulates updates between AcquireNextFrame calls (not every-frame), so effective fps tracks change rate, not a fixed clock.

**Sources**
- [Take ScreenCaptureKit to the next level - WWDC22 (SCContentFilter per-window, o…](https://developer.apple.com/videos/play/wwdc2022/10155/)
- [SCContentFilter | Apple Developer Documentation](https://developer.apple.com/documentation/screencapturekit/sccontentfilter)
- [SCStreamConfiguration.minimumFrameInterval | Apple Developer Documentation](https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/minimumframeinterval)
- [What's new in ScreenCaptureKit - WWDC23 (SCScreenshotManager replacing CGWindow…](https://developer.apple.com/videos/play/wwdc2023/10136/)
- [MacPorts ticket 71136: CGWindowListCreateImage unavailable/obsoleted in macOS 1…](https://trac.macports.org/ticket/71136)
- [Screen capture - UWP applications | Microsoft Learn (Windows.Graphics.Capture o…](https://learn.microsoft.com/en-us/windows/uwp/audio-video-camera/screen-capture)
- [IGraphicsCaptureItemInterop::CreateForWindow - Win32 apps | Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow)
- [New Ways to do Screen Capture - Windows Developer Blog (WGC, minimized windows …](https://blogs.windows.com/windowsdeveloper/2019/09/16/new-ways-to-do-screen-capture/)
- [GraphicsCaptureSession.IsBorderRequired (borderless capture, Win11, RequestAcce…](https://learn.microsoft.com/en-us/answers/questions/108678/how-to-remove-yellow-boarder-capture-indicator-fro)
- [Windows Graphics Capture vs DXGI Desktop Duplication (occluded/background, cros…](https://obsproject.com/forum/threads/windows-graphics-capture-vs-dxgi-desktop-duplication.149320/)
- [Desktop Duplication API - Win32 apps | Microsoft Learn (full-screen only, accum…](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api)
- [IDXGIOutputDuplication::AcquireNextFrame + GetFramePointerShape (cursor shape/p…](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi1_2-idxgioutputduplication-acquirenextframe)
- [ScreenCast - XDG Desktop Portal documentation (SelectSources types/cursor_mode/…](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
- [xdg-desktop-portal-hyprland (window sharing as compositor-specific extra functi…](https://wiki.hypr.land/Hypr-Ecosystem/xdg-desktop-portal-hyprland/)
- [Releases - emersion/xdg-desktop-portal-wlr (ext_image_copy_capture_v1 toplevel,…](https://github.com/emersion/xdg-desktop-portal-wlr/releases)
- [Composite Extension Version 0.4 / XCompositeNameWindowPixmap (offscreen redirec…](https://www.x.org/archive/X11R7.5/doc/compositeproto/compositeproto.txt)
- [NVIDIA: Using the X Composite Extension (redirect + texture-from-pixmap)](https://download.nvidia.com/XFree86/Linux-x86_64/435.17/README/xcompositeextension.html)
- [NVIDIA Capture SDK (NvFBC desktop-to-GPU-buffer capture, NVENC handoff)](https://developer.nvidia.com/capture-sdk)
- [NVFBC Windows 10 Support / Deprecation Technical Bulletin (frozen at Capture SD…](https://developer.download.nvidia.com/designworks/capture-sdk/docs/NVFBC_Win10_Deprecation_Tech_Bulletin.pdf)
- [GStreamer nvh264enc documentation (NVCODEC NVENC H.264 encode)](https://gstreamer.freedesktop.org/documentation/nvcodec/nvh264enc.html)
- [Zero copy pipeline on Nvidia (dmabuf/GPU-surface zero-copy to NVENC) - GStreame…](https://discourse.gstreamer.org/t/zero-copy-pipeline-on-nvidia/4856)
- [m1k1o/neko GitHub repository (X11 ximagesrc to GStreamer to WebRTC, NVENC, shar…](https://github.com/m1k1o/neko)
- [neko documentation - capture configuration (GStreamer pipelines, ABR qualities,…](https://neko.m1k1o.net/docs/v3/configuration/capture)

## An efficient, scientific, cross-OS REPLAY architecture for Shinken (the layered .skn model: event-log + periodic STATE snapshots + on-demand VIDEO sidecar, with content-addressed/delta/fMP4 storage and O(nearest-snapshot) seek) PLUS a separate qcow2-backed deterministic-eval-VM design for OSWorld-style evaluation in which agent-trajectory replay is OPTIONAL because snapshot-revert alone gives reproducibility.

The state of the art converges on a layered, NOT bit-deterministic, replay model and Shinken already encodes it in D5/notes/replay.md: a self-contained .skn ZIP (Playwright trace.zip lineage) whose source of truth is an append-only events.jsonl (rrweb two-level kind+src envelope, asciicast-v3 interval dt + monotonic seq, OTel-GenAI decision channel), with periodic BISECTED STATE snapshots (env half = microVM/VM/process image; agent half = a versioned orchestrator checkpoint) pinned to exact event-log offsets, content-addressed media in resources/<sha1>, an on-demand fragmented-MP4 (CMAF) video sidecar keyed to the same clock via a keyframe index, and a sidecar index.json giving O(log n) seeks so scrub-to-T and fork-from-T cost O(nearest snapshot + tail), never O(whole history). Per-OS capture differs sharply: Linux gets AT-SPI a11y + NVFBC/x11grab and is the only tier with sub-second Co…

**Recommendations**
- SPLIT the two concerns explicitly into two artifacts with two contracts. (A) The cross-OS .skn LAYERED REPLAY bundle (event-log + bisected STATE snapshots + on-demand VIDEO sidecar) is the capture/debug/branch/training format (D5). (B) A SEPARATE qcow2 DETERMINISTIC-EVAL-VM design is the reproducible-scoring substrate…
- P0 (ship first, smallest surface): the qcow2 deterministic-eval-VM needing ONLY snapshot-revert. A read-only golden qcow2 base per task-suite SKU + per-task qcow2 backing-file overlays (redirect-on-write) and/or savevm/loadvm internal snapshots; reset() = loadvm('init_state') -> run agent LIVE -> typed verifier DAG gr…
- P0 (parallel): the .skn event-log as source of truth with the storage levers wired from day one — content-addressed resources/<sha1> dedup, full-snapshot+typed-delta observations (a11y_delta / png_diff), interval-dt + monotonic seq, and index.json for O(log n) seek. Make 'pure replay (all events recorded) reproduces t…
- LATER (layer on): the on-demand VIDEO sidecar as fragmented MP4 / CMAF, IDR-aligned, forced keyframes every ~1-2s, with a keyframe->seq->byte-offset index so video scrubbing snaps to event seq. Record server-side at the encoder/SFU via a GStreamer tee (never in the browser) so a viewer drop never corrupts the record; …
- LATER (Linux fork tier only): promote .skn replay to true mid-execution BRANCHING via Firecracker MAP_PRIVATE CoW memory-fork + a versioned (NOT pickle) agent-half checkpoint, immutable checkpoint DAG, per-fork uniqueness reseed (VMGenID/RNG/MAC/boot_id/tokens), and side-effect idempotency keys / record-mock proxy. On…
- Reserve the 'scientific' determinism layer for the AGENT CORE only: recorded LLM/tool inputs (seed + response.id + tool.call.id) replayed via stubs; CRIU/cuda-checkpoint for the Linux process/GPU agent half if needed. Do NOT pursue full-desktop bit-determinism (rr/Hermit/Antithesis are single-core/Linux-x86/simulation…
- Commit measurement spikes before locking defaults (all current figures are vendor-published/unverified): a11y-tree fidelity on Electron/Qt/canvas; qcow2 loadvm/revert latency vs CoW-overlay reset vs Firecracker memory-fork; fMP4 keyframe cadence (seek granularity vs bitrate); snapshot/delta-chain flatten cadence (read…

**Numbers (vendor-published, unverified)**
- Event-log/storage: a 5-min DOM session compresses to ~100-500 KB (~5-13 kbps); a11y-tree diffs at agent cadence ~16-80 kbps; AV1-SCC pixels ~100 kbps (rarely >500 kbps under motion, >80% below x264, …
- Codec sizes for the video sidecar: HEVC ~40-50% smaller than H.264 at equal quality; AV1 ~40% smaller (42 dB PSNR at 7 Mbps AV1 vs 11 Mbps H.264, 1080p60), +1.5-2 dB PSNR; AV1 single-stream ~500 fps …
- Seek/cadence: fragmented-MP4/CMAF with forced keyframes every ~1-2s bounds video seek granularity; structured scrub is O(log n) via index.json regardless of session length; snapshot cadence at semant…
- qcow2 eval reset: OSWorld VMware/VirtualBox full snapshot-revert is seconds-to-minutes, I/O-bound on disk-delta size; CoW backing-file overlays give instant children with near-zero per-task disk (onl…
- Firecracker memory-fork (Linux tier, for branching not eval): VMM restore 5-30 ms (28 ms warm); ~93% of pages stay shared; ~50 clones share most pages; sub-ms CoW fork (~0.79 ms ZeroBoot; Morph P99 ~…
- LLM determinism: temperature=0/seed do NOT guarantee identical output (Anthropic/OpenAI); observed accuracy variance up to ~15%, best-vs-worst gap up to ~70% — hence record-and-stub for the agent cor…
- rr-style full record overhead ~10-20x slowdown (reason to record inputs at the agent boundary, not full-syscall-record the desktop).
- DIVERT counterfactual fork: branches share 34.8-58.4% exact token prefixes (vs ~0.5% independent rollouts) -> direct KV-cache reuse for cheap eval fan-out.

**Sources**
- [Shinken D5 replay note — .skn bundle, bisected snapshot+event-log model, branch…](file:///path/to/shinken/notes/replay.md)
- [Shinken Technical Decisions — D1 isolation tiers, D5 replay, D7 eval golden-sna…](file:///path/to/shinken/docs/05-tech-decisions.md)
- [Shinken streaming-bandwidth note — fMP4/CMAF IDR-aligned recording, keyframe ca…](file:///path/to/shinken/notes/streaming-bandwidth.md)
- [Shinken sandbox-infra note — CoW disk (overlayfs/qcow2 backing chains/dm-thin/b…](file:///path/to/shinken/notes/sandbox-infra.md)
- [OSWorld desktop_env — reset() -> _revert_to_snapshot('init_state') per task (de…](file:///path/to/shinken/references/OSWorld/desktop_env/desktop_env.py)
- [OSWorld VMware provider — save_state/revert_to_snapshot via vmrun snapshot/reve…](file:///path/to/shinken/references/OSWorld/desktop_env/providers/vmware/provider.py)
- [OSWorld server — in-guest recording via ffmpeg x11grab libx264 (server/main.py:…](https://github.com/xlang-ai/OSWorld/blob/main/desktop_env/server/main.py)
- [OSWorld Docker manager — golden qcow2 images Ubuntu.qcow2 / Windows-10-x64.qcow…](https://github.com/xlang-ai/OSWorld/blob/main/desktop_env/providers/docker/manager.py)
- [QEMU — Documentation/CreateSnapshot (savevm/loadvm, internal vs external, redir…](https://wiki.qemu.org/Documentation/CreateSnapshot)
- [QEMU — Disk Images (qcow2 backing files, copy-on-write overlays)](https://www.qemu.org/docs/master/system/images.html)
- [Backing File and Snapshot in QEMU/KVM (redirect-on-write branch tree)](https://techpiezo.com/linux/use-and-implementation-of-backing-file-and-snapshot-in-qemu-kvm/)
- [Playwright — Trace Viewer + trace.zip packaging (JSONL channels + content-addre…](https://playwright.dev/docs/trace-viewer)
- [rrweb types — EventType / IncrementalSource / eventWithTime envelope](https://github.com/rrweb-io/rrweb/blob/master/packages/types/src/index.ts)
- [asciinema — asciicast v3 (interval timing, markers, tags)](https://docs.asciinema.org/manual/asciicast/v3/)
- [OpenTelemetry — GenAI semantic conventions (gen_ai.request.seed/response.id, ex…](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/)
- [OpenAdapt recording schema — png_data/png_diff_data delta-encoded screenshots, …](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/legacy/openadapt/models.py)
- [LangGraph Persistence — StateSnapshot, checkpoint DAG, update_state fork (immut…](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Firecracker — Snapshotting support (memory file + vmstate, diff snapshots, MAP_…](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)
- [REAP working-set record-and-prefetch (ASPLOS'21) — ~97% restore page-fault elim…](https://marioskogias.github.io/docs/reap.pdf)
- [Morph Infinibranch — parentless CoW VM fork / live branching (sub-ms fork, ~93%…](https://www.morph.so/blog/infinibranch/)
- [rr — lightweight deterministic record/replay (single-core; recorded-input bound…](https://rr-project.org/)
- [CRIU — Checkpoint/Restore (process tree, pre-dump incremental, --lazy-pages)](https://criu.org/Checkpoint/Restore)
- [NVIDIA — Checkpointing CUDA Applications with CRIU (cuda-checkpoint; x64-only, …](https://developer.nvidia.com/blog/checkpointing-cuda-applications-with-criu/)
- [Antithesis — deterministic hypervisor (full-machine determinism only by simulat…](https://antithesis.com/blog/deterministic_hypervisor/)
- [Improving Video Quality and Performance with AV1 and NVIDIA Ada Lovelace (~40% …](https://developer.nvidia.com/blog/improving-video-quality-and-performance-with-av1-and-nvidia-ada-lovelace-architecture/)
- [GStreamer isomp4/mp4mux — fragmented MP4 (crash-safe, no moov-at-end, IDR-align…](https://gstreamer.freedesktop.org/documentation/isomp4/mp4mux.html)
- [Visionular — AV1 screen-content coding (~100 kbps class, intra-block-copy savin…](https://visionular.ai/av1-screen-content-coding/)
- [DIVERT — branch-based agent eval via complete-state snapshot + parent/child tre…](https://arxiv.org/html/2604.21480)
- [LLM determinism limits — temperature/seed do not guarantee identical output (ob…](https://medium.com/@2nick2patel2/llm-determinism-in-prod-temperature-seeds-and-replayable-results-8f3797583eb1)

## Shinken AGENT/HARNESS-layer compatibility: a small async Env/Operator core contract + thin adapters (Gym/Gymnasium shim, MCP server, native SDK, OSWorld-DesktopEnv shim) to be simply and efficiently compatible with every harness

Every harness surveyed — OpenAI Gym/Gymnasium, OSWorld DesktopEnv, BrowserGym, HUD's MCP setup/run/evaluate contract, Anthropic and OpenAI computer-use agent loops, ByteDance UI-TARS's Operator(screenshot+execute+advertised action space), LangGraph, and trycua/cua's ComputerAgent loop (plus cua-bench's make/reset/step/evaluate gym) — collapses to the same observe -> act -> reward shape over an isolated environment. So Shinken does NOT need to pick one; it needs one small CORE contract that all of them are thin adapters over. Plain synchronous Gym (reset/step returning obs,reward,done,info with fixed observation_space/action_space) is necessary but insufficient for computer use: it assumes a single agent, a fixed-shape Box/Discrete space, lock-step turn-taking, a scalar reward at every step, and no notion of streaming observations, async/long-horizon runs, mid-step permission interrupts,…

**Recommendations**
- Build a small ASYNC core contract, not plain Gym. Plain sync Gym is necessary as a compatibility veneer but insufficient as the core: it cannot express streaming observations, async/long-horizon runs, mid-step permission interrupts, capability negotiation, or deterministic replay/branching — exactly the feature set ev…
- P0 CORE — Env contract (environment side, async): async create(spec)/connect(id)/ephemeral(spec) lifecycle (cua's three modes); async reset(task_config?) -> Observation (DesktopEnv/HUD setup); async capabilities() -> CapabilityDescriptor {schema_version semver, supported_verbs, supported_targets, coordinate_modes, obs…
- P0 CORE — Operator contract (actuation side, async, UI-TARS-shaped): async screenshot()/observe() -> Observation; async execute(actions: Action[]) -> ExecuteResult{status, executed_target_logical_px, state_delta, error?} where Action is the typed verb union with target=oneof{point_px|point_norm|element_ref}; supported…
- P0 CORE — control + permission channel: a needs_approval{capability, token} event (generalizing OpenAI pending_safety_checks and LangGraph interrupt()) pauses the run until the controller (human panel or policy engine) replies grant/deny; universal control sentinels done/fail/call_user as first-class events; global ab…
- P0 ADAPTERS — ship four thin adapters generated from one IDL: (1) Gym/Gymnasium SHIM wraps the async core so reset()->(obs,info) and step(action)->(obs,reward,terminated,truncated,info) work for RL users; it batches the event stream into one step return and calls verify() to populate reward at episode end; expose a Di…
- P0 ADAPTERS — vendor agent-loop adapters over the core so any off-the-shelf CUA model drives Shinken unmodified: AnthropicComputerAdapter (computer_20241022/0124/1124 + bash + text_editor, tool_use/tool_result, prompt-cache-stable history, image-resize math to avoid ~14% click drift), OpenAICuaAdapter (computer_call.a…
- Keep the hot path and media OFF MCP and off the Gym step return: action/observation streaming + video/frame-deltas ride Shinken's own bidirectional stream (WebSocket for browser reach / gRPC bidi server-side) plus a separate hardware-accelerated media plane (NVENC over WebRTC), exactly as cua keeps H.265 off MCP and A…
- LATER (P1+): pass@k/Pass^k + ICC reporting via N forked replicas (cheap because of CoW fork); a verifier calibration/auto-repair loop (OpenComputer 94.1% vs 79.2% LLM-judge); a stateless INIT/RUN/VERIFY eval-service over a warm fork pool (ProRL); LangGraph-style agent checkpoint paired with env snapshot for full count…

**Numbers (vendor-published, unverified)**
- Anthropic image scaling: scale = min(1, 1568/long_edge, sqrt(1_150_000/total_px)) for older models; Opus 4.7/4.8 1:1 to 2576px long edge; macOS Retina DPR=2; ~1,000-1,800 input tokens per screenshot …
- Anthropic canonical loop: hardcoded _screenshot_delay=2.0s settle before every capture; prompt caching with 3 rolling breakpoints; image truncation disabled when caching on (cached reads ~10% cost).
- OpenAI CUA: 9 action types, batched actions[]; computer_screenshot up to ~10.24M px detail:'original'; wait in ms (vs Anthropic seconds); launch benchmarks OSWorld 38.1 / WebArena 58.1 / WebVoyager 8…
- HUD: run_dataset max_concurrent=30 default, max_steps default 50, group_size for pass@k; LOCAL stdio/Docker not parallelizable vs REMOTE HTTP spawnable; telemetry upload via ThreadPoolExecutor(4); ho…
- UI-TARS: maxLoopCount default 25, MAX_SNAPSHOT_ERR_CNT=10; 0-1000 normalized coords (rescale x*image_w/1000); max ~5 in-context images, JPEG-75; clipboard-paste type.
- cua: screenshot-poll-per-step over SSE (one frame per /cmd) with default screenshot_delay 0.5s; ~25 registered agent loops; Computer Server default HTTP port 8000; CuaBot live view uses H.265 OFF MCP…
- OSWorld flake/RCE: sleep(60) after reset + sleep(20) before evaluate; reward hardcoded 0 each step then 0/1 at end; in-VM Flask debug=True on 0.0.0.0:5000, unauthenticated /execute (120s) /run_python…
- Eval methodology: programmatic verifiers 94.1% human agreement vs 79.2% LLM-judge (OpenComputer); single-run pass@1 hides 10-30pt variance, up to 24.9pp best-vs-worst, tau-bench 80% pass@1 collapses …

**Sources**
- [hud-evals/hud-python — MCP setup_tool/run/evaluate_tool->reward[0,1], LOCAL vs …](https://github.com/hud-evals/hud-python)
- [HUD Documentation — Environment/Task/Scenario/Traces, model gateway, parallel e…](https://docs.hud.ai/)
- [xlang-ai/OSWorld — DesktopEnv gym-like reset/step/evaluate, computer_13 action …](https://github.com/xlang-ai/OSWorld)
- [XLANG Lab — Introducing OSWorld-Verified (300+ grader/task fixes, AWS ~50x para…](https://xlang.ai/blog/osworld-verified)
- [Farama Gymnasium — reset/step/observation_space/action_space, terminated/trunca…](https://gymnasium.farama.org/)
- [BrowserGym (ServiceNow Research) — gym interface over a real browser, DOM/AXTre…](https://github.com/ServiceNow/BrowserGym)
- [Anthropic — Computer use tool (computer_20241022/0124/1124, zoom, coordinate sc…](https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/computer-use-tool)
- [anthropic-quickstarts/computer-use-demo — sampling loop, hosted tool schema, To…](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo)
- [OpenAI — Computer use (CUA): computer_call/computer_call_output, actions[], cal…](https://developers.openai.com/api/docs/guides/tools-computer-use)
- [openai/openai-cua-sample-app — responses-loop, typed SSE RunEvent stream, zod r…](https://github.com/openai/openai-cua-sample-app)
- [Operator System Card | OpenAI — confirmations, takeover mode, watch mode, real-…](https://openai.com/index/operator-system-card/)
- [bytedance/UI-TARS-desktop — Operator(screenshot+execute+advertised action space…](https://github.com/bytedance/UI-TARS-desktop)
- [bytedance/UI-TARS — GUI agent action space (normalized 0-1000 coordinate DSL)](https://github.com/bytedance/UI-TARS)
- [trycua/cua — Computer SDK + ComputerAgent async-generator loop + cua-bench gym …](https://github.com/trycua/cua)
- [Cua Docs — MCP Server usage (high-level run/task tool, CUA_MODEL_NAME, CUA_USE_…](https://cua.ai/docs/cua/reference/mcp-server/usage)
- [LangGraph — Persistence (checkpoints, threads, time travel, fork) and interrupt…](https://docs.langchain.com/oss/python/langgraph/persistence)
- [MCP spec 2025-11-25 — Transports (stdio + Streamable HTTP/SSE; no WebSocket/med…](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [MCP spec — Authorization (OAuth 2.1 Resource Server, RFC9728 Protected Resource…](https://modelcontextprotocol.io/specification/draft/basic/authorization)
- [OpenComputer: Verifiable Software Worlds for Computer-Use Agents (programmatic …](https://arxiv.org/html/2605.19769v1)
- [ProRL Agent: Rollout-as-a-Service — AgentHandler init/run/eval, INIT/RUN/EVAL w…](https://arxiv.org/html/2603.18815v1)
- [On Randomness in Agentic Evals — Pass@k vs Pass^k, k>=5-10 repeats, confidence …](https://arxiv.org/pdf/2602.07150)
- [Morph Cloud — Infinibranch sub-millisecond CoW VM fork / time-travel (cheap k-r…](https://cloud.morph.so/docs/documentation/instances/creating-snapshot)
- [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering (ACI…](https://arxiv.org/abs/2405.15793)
