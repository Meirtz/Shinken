# Spike — macOS sandbox automation readiness (entitlements + TCC)

**Issue:** #43 · **Decisions it grounds:** [D6](../design/tech-decisions.md) (capabilities) and
[D10](../design/tech-decisions.md) (substrate/provider) · **Status:** historical research spike.
At the time of this analysis the native macOS runtime was not built. It has since been superseded
by a capture+input v1 with local-only proof; AX observation and managed-pool readiness remain
designed. See [status.md](status.md) for current truth. This document preserves the public-source
readiness analysis #43 required.

## The question

On macOS the hard part is not asking the agent before it acts — it is making a macOS Sandbox
*actually automation-ready*. Synthetic input, screen/window capture, AX-tree reads, and app
automation are each gated by **TCC** (Transparency, Consent, and Control) grants and code-signing
state that can silently block the runtime. A clone of a "ready" image is only ready if its TCC
state survives the clone. This spike answers: which grants are needed, which can be pre-baked into a
pool image, how clones preserve them, and what readiness probe `shinkend` should expose.

A "many grants cannot be silently pre-authorized" result is a *valid* outcome: it scopes the macOS
provider and the preflight UX; it does not change v0.0.1 (Linux/X11) scope.

## Capability readiness matrix

Each automation need maps to a distinct TCC service / API gate. "Pre-grantable?" = can a standing
pool image grant it **without an interactive click**, i.e. via an MDM **PPPC** profile (see below).

| Need | API / mechanism | TCC service / gate | Runtime probe | Pre-grantable via PPPC? |
|------|-----------------|--------------------|---------------|--------------------------|
| Synthesize input into other apps | `CGEventPost` / `CGEventCreate*` | **Accessibility** | `AXIsProcessTrusted()` | ✅ (PPPC `Accessibility`) |
| Read other apps' AX trees | `AXUIElement*` APIs | **Accessibility** | `AXIsProcessTrusted()` | ✅ (same grant as above) |
| Capture screen / display | ScreenCaptureKit `SCStream`/`SCScreenshotManager`, `CGDisplayStream` | **Screen Recording** | `CGPreflightScreenCaptureAccess()` | ✅ (PPPC `ScreenCapture`, macOS 11+) |
| Capture a specific window | ScreenCaptureKit `SCShareableContent` (per-window `SCWindow`) | **Screen Recording** | `CGPreflightScreenCaptureAccess()` | ✅ (same grant) |
| Observe global keyboard/mouse | `CGEventTap` (listen-only) | **Input Monitoring** | `IOHIDCheckAccess(kIOHIDRequestTypeListenEvent)` | ⚠️ partial — PPPC `ListenEvent` exists but coverage varies by OS version |
| Automate apps via Apple Events | `NSAppleScript` / `osascript` / `AESendMessage` | **Automation** — *per (source app → target app) pair* | dry-run Apple Event; check `errAEEventNotPermitted` | ⚠️ PPPC `AppleEvents` can pre-allow a specific source→target identity pair, but it is enumerated per target |
| Read protected files / other apps' data | filesystem | **Full Disk Access** | attempt read of a known TCC-protected path | ⚠️ PPPC `SystemPolicyAllFiles` (managed only; not all subservices) |

Notes:
- **Accessibility is the load-bearing grant** for a GUI agent: it gates *both* synthetic input and
  AX-tree reads. With Accessibility + Screen Recording, Shinken can do the v0.0.1 act-and-observe
  loop (pointer/keyboard + screenshot/screencast/window capture) on macOS.
- `CGWindowListCreateImage` and other legacy capture paths return blank/placeholder content without
  Screen Recording (macOS 10.15+), so the probe must gate capture, not assume it.
- **Apple Events automation is the awkward one**: TCC tracks it per source→target pair, so a fresh
  target app triggers a new consent unless an MDM PPPC profile enumerated it. Treat app-level
  scripting as an explicit, separately-gated capability, not part of the baseline GUI loop.

## How TCC binds — and what that means for pre-baking

TCC decisions live in `TCC.db` (a user store under `~/Library/Application Support/com.apple.TCC/`
and a system store under `/Library/Application Support/com.apple.TCC/`), protected by **SIP** — an
app cannot write its own grant. A grant is keyed to the requesting client's **bundle identifier +
code-signing Designated Requirement** (for signed apps) or to the **executable path** (for unsigned
/ ad-hoc). Consequences for a Sandbox image:

- **Sign the runtime with a stable identity.** A Developer ID (or stable internal) signature + fixed
  bundle id makes a grant survive rebuilds and moves. An ad-hoc/unsigned `shinkend` binds its grant
  to a path and loses it whenever the binary is replaced — unusable for a pool.
- **The deterministic pre-grant path is MDM + PPPC.** A **Privacy Preferences Policy Control**
  configuration profile, delivered by an MDM to a *supervised* device/VM, pre-authorizes
  Accessibility / Screen Recording / Apple Events / etc. for a named code requirement — no
  interactive toggling. This is how a standing macOS pool becomes automation-ready. Unsupervised
  devices cannot be PPPC-pre-granted for the high-risk services; they require a manual first run.
- **What cannot be baked silently** without MDM: any grant on an unmanaged image, and (depending on
  OS version) some Input Monitoring / Full Disk Access subservices. The fallback is an interactive
  preflight (below).

## Image / clone behavior

Shinken's macOS substrate (D10) targets Apple **Virtualization.framework** guests, including the
tooling built on it (e.g. Tart, lume). macOS virtualization is Apple-Silicon-on-Apple-Silicon and
bounded by the macOS license terms — a constraint on where the pool can run, not on the readiness
model. TCC state is **per-guest-user**, in that user's `TCC.db`:

- **A golden image preserves readiness across clones** when the cloned guest keeps the *same* user
  account and the *same* signed `shinkend` identity that the grants (or PPPC profile) were issued
  for. Post-clone uniqueness hooks (hostname / boot-id / RNG reseed) do not disturb TCC because they
  do not change the user or the code signature.
- **Resetting** is `tccutil reset <service> [bundle-id]` (per service / per app). Use it to scrub a
  guest before returning it to a cold pool if a session granted ad-hoc consent.
- A **PPPC profile baked into the golden image (MDM-enrolled guest)** is the cleanest model: every
  clone boots automation-ready with no per-clone TCC work.

## Proposed readiness probe + capability descriptor (D6/D10)

`shinkend` on macOS should expose a **readiness query** that the Control Plane consults before
dispatching a session, so it can say "this Sandbox can / cannot automate macOS" up front (mirroring
the honest capability negotiation the Linux runtime already does in the handshake):

```jsonc
// query: macos_readiness  →  result.value
{
  "accessibility":    true,   // AXIsProcessTrusted()
  "screen_recording": true,   // CGPreflightScreenCaptureAccess()
  "input_monitoring": false,  // IOHIDCheckAccess(listen)
  "full_disk_access": false,  // probe read of a known protected path
  "automation": { "com.apple.finder": true },  // per-target Apple Events dry-run
  "code_signed":  true,       // stable Designated Requirement present
  "pre_granted_via": "pppc"   // "pppc" | "interactive" | "none"
}
```

Proposed D6 capability descriptor — a macOS automation-readiness power the Capability Manager can
require/grant, composed from the matrix above:

```jsonc
"macos.automation": {
  "requires": ["accessibility", "screen_recording"],   // baseline GUI act+observe
  "optional": ["input_monitoring", "automation:<bundle-id>", "full_disk_access"],
  "preflight": "block-until-ready" // | "degrade" (see fallback)
}
```

The Control Plane treats a Sandbox whose probe lacks a `requires` grant as **not automation-ready**
and routes around it (cold-pool replenish / re-image), exactly as Sandbox health is a
circuit-breakable dependency elsewhere (D9).

## Preflight / first-run checklist (macOS Sandbox)

1. `shinkend` is **code-signed** with the expected Designated Requirement and bundle id.
2. Managed path: an **MDM PPPC profile** for that requirement is installed (Accessibility + Screen
   Recording at minimum). Unmanaged path: the operator completes a one-time interactive grant.
3. Run `macos_readiness`; require `accessibility` + `screen_recording` true before marking ready.
4. For app-scripting sessions, pre-enumerate target bundle ids in the PPPC `AppleEvents` payload, or
   accept a per-target interactive consent on first use.
5. On return-to-pool, `tccutil reset` any session-granted ad-hoc consents if the guest is reused.

## Non-goals and fallback

- **Non-goals at the time of the spike (v0.0.1 planning):** this was readiness design only; the
  later capture+input v1 is tracked in [status.md](status.md). No attempt to bypass SIP/TCC or to
  self-grant — Shinken is honest about the boundary, not a tool to
  defeat it. Unmanaged, unsigned silent pre-granting is out of scope (it is not possible by design).
- **Fallback when a permission cannot be pre-granted:**
  - Missing **Accessibility** or **Screen Recording** → the Sandbox reports *not automation-ready*;
    the Control Plane does not dispatch GUI sessions to it (route to a ready/managed pool or surface
    the preflight to a human). No silent degraded driving.
  - Missing **Input Monitoring** → input *synthesis* still works (that is Accessibility); only global
    input *observation* is unavailable. Degrade: skip input-event observation, keep the GUI loop.
  - Missing **Automation** for a target app → that app's Apple-Events scripting capability is denied
    and recorded as a permission event; pixel/AX driving of the same app still works.
  - Missing **Full Disk Access** → file operations in protected locations fail closed and are
    recorded; unprotected paths are unaffected.

## References (public Apple documentation)

- Accessibility trust: [`AXIsProcessTrusted` / `AXIsProcessTrustedWithOptions`](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrusted)
- Screen capture authorization: [`CGPreflightScreenCaptureAccess` / `CGRequestScreenCaptureAccess`](https://developer.apple.com/documentation/coregraphics/3656523-cgpreflightscreencaptureaccess) · [ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit)
- Input access: [`IOHIDCheckAccess`](https://developer.apple.com/documentation/iokit/3753406-iohidcheckaccess)
- Apple Events automation consent: [Apple Events / scripting privacy](https://developer.apple.com/documentation/security/updating_your_app_to_use_the_apple_events_sandbox_temporary_exception) · `NSAppleEventsUsageDescription`
- PPPC / MDM configuration profiles: [Privacy Preferences Policy Control payload](https://support.apple.com/guide/deployment/privacy-preferences-policy-control-payload-dep38df53c2a/web)
- TCC reset tooling: `man tccutil`
- macOS virtualization: [Virtualization framework](https://developer.apple.com/documentation/virtualization)
