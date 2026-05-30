# Permissions — the 3-layer capability-unlock model

> **Status:** drafting · **Date:** 2026-05-30 · **Owner:** the maintainers
> Working note feeding [`docs/05-tech-decisions.md`](../docs/05-tech-decisions.md) (D6 as an ADR), [`docs/02-architecture.md`](../docs/02-architecture.md), and [`docs/08-threat-model.md`](../docs/08-threat-model.md).
> Reconciles to canon **D6** (3-layer capability-unlock permission), and touches **D2** (`tool_runner` boundary, code-as-action), **D5** (approvals as replay events), **D9** (Action Gateway choke point).
> Siblings: [sandbox-infra.md](sandbox-infra.md) · [ai-native-interface.md](ai-native-interface.md) · [replay.md](replay.md) · [streaming-bandwidth.md](streaming-bandwidth.md) · [open-questions.md](open-questions.md) · [sources.md](sources.md).

The **Permission Panel** is one of Shinken's four headline features: a human-facing UI that lets an operator *unlock advanced image features* for a running agent — network access, broader filesystem scope, the clipboard, a GPU, privileged install / `sudo`, persistence, brokered credentials, peripherals — without ever handing the agent ambient authority it can abuse. The architectural claim that distinguishes a real boundary from security theater is that **a permission decision is not a permission boundary.** A policy engine returns a verdict; if nothing below it physically enforces that verdict, a jailbroken or prompt-injected model ignores it. So Shinken splits permissions into **three layers that do three different jobs**:

1. **Decision layer — Cedar.** Answers "is this grant permitted by policy?" Declarative, sub-millisecond, and *statically analyzable*, so we can **prove** a grant or a policy edit never widens authority beyond intent.
2. **Caretaker layer — object-capability handles.** The *live on/off switch* the panel toggles: an unforgeable, attenuable, revocable reference that revokes in O(1) by flipping one indirection bit, checked at use time — not by waiting for a policy cache to expire.
3. **OS-enforcement layer.** The wall the model cannot talk past: bubblewrap + seccomp + Landlock + cgroups + an out-of-VM egress proxy on Linux; Seatbelt + TCC on macOS; a restricted token + per-workspace capability SID + WFP on Windows.

**Cedar decides, the handle is the switch, the OS is the wall.** This note specifies all three, resolves Cedar-vs-OPA, pins the OS primitives per guest, defines the **8 capability classes / 4 risk tiers / taint** grammar, details the **forced egress proxy + Vault/KMS secret brokering**, lays out the **escalation-on-failure** HITL loop, and ties it to the generic **`tool_runner` policy boundary**. Vendor numbers are marked *(vendor-published, unverified)* and go on the first-party measurement backlog in [open-questions.md](open-questions.md).

---

## 0. Why three layers (the deeper insight)

The decisive correction over the abstract layered-defense landscape (OS primitives + ocap theory + a policy engine) is that **a policy engine alone cannot do live revocation.** Decision-cache TTLs are real: managed Cedar deployments (e.g. Amazon Verified Permissions' API-linked policy stores) run an authorizer with a **~120 s** cache and agents refresh policy roughly **every 2 minutes** *(vendor-published, unverified)*. That leaves a stale-authorization window in which a *revoked* agent keeps acting on dead authority — unacceptable for a privilege-escalation surface.

The fix is to never use "delete the policy" as the revoke mechanism. After Cedar permits a grant, the control plane **issues a capability handle** (the caretaker/membrane pattern) that the enforcement points hold. Revoke flips the caretaker's enabled bit; the next use of the handle fails closed (`RevokedException`), synchronously, no policy re-query. The membrane extends this transitively, so any capability *derived from* a revoked one (a sub-agent's attenuated handle) also dies.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  PERMISSION PANEL  (Control Panel UI / Action Gateway)                      │
│  request → live grant card → approve/deny/escalate/takeover → revoke        │
└───────────────┬─────────────────────────┬──────────────────────────────────┘
                │ "is this permitted?"    │ "make it live / kill it now"
                ▼                          ▼
   ┌─────────────────────────┐   ┌──────────────────────────────────────┐
   │ LAYER 1  CEDAR           │   │ LAYER 2  OCAP CARETAKER / MEMBRANE     │
   │ deny-by-default          │   │ unforgeable · attenuable · revocable  │
   │ forbid-overrides         │──▶│ O(1) bit-flip revoke, checked AT USE  │
   │ SMT/Lean-proved          │   │ (no ~120s cache wait)                  │
   └─────────────────────────┘   └────────────────┬─────────────────────┘
                                                    │ drives on grant/revoke
                                                    ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │ LAYER 3  OS ENFORCEMENT  (per guest OS — the wall a jailbroken model      │
   │                            cannot cross)                                  │
   │  Linux:   bwrap + seccomp(net-gate) + Landlock + cgroups + egress proxy   │
   │  macOS:   Seatbelt (sandbox-exec) + TCC                                   │
   │  Windows: restricted token + per-workspace capability SID + WFP           │
   └────────────────────────────────────────────────────────────────────────┘
```

Each layer is independently testable and degrades honestly: if a Cedar decision is somehow wrong, the OS wall still holds; if the OS primitive is unavailable on a host (and several are — see §4), the panel must *say so* rather than imply a guarantee it cannot keep.

---

## 1. Layer 1 — Cedar as the decision layer (and why not OPA/Rego)

**Decision: Cedar for the capability-unlock decision layer. Not OPA/Rego.** OPA stays only as an optional *outer* fleet/org-rule layer that can further restrict but never widen.

The unlock panel is a **privilege-escalation surface**: the property that matters most is being able to *prove* that a grant — or an edit to the policy text itself — "never grants more than before." Cedar's engine and symbolic compiler are formally verified in Lean, and **Cedar Analysis** compiles policies to SMT (CVC5) to answer equivalence and permissiveness-shift queries with soundness and completeness (a negative result comes with a concrete counterexample). Rego is Turing-flexible by design, which makes that proof *impossible* — and over-grant here is a breach, not a bug.

Cedar's model maps 1:1 onto the domain:

| Cedar concept | Maps to in Shinken |
|---|---|
| **Principal** | `Agent::"id" in Session::"id"` (entity hierarchy via the `in` operator) |
| **Action** | `Action::"UseCapability"` |
| **Resource** | the `Capability` / `Feature` entity being unlocked (`net.egress`, `fs.scope`, …) |
| **Context** | `{ trust_tier, approval_state, now (datetime), request: { domain, cidr, path, device_id, … } }` |
| **permit / forbid** | the grant / the un-overridable guardrail |
| **forbid-overrides + deny-by-default** | the never-overridable hard-deny tier |
| **policy templates** (`?principal` / `?resource` slots) | per-session/per-agent scoped grants without writing fresh policy text each session |
| **entity tags** (`.hasTag` / `.getTag`) | per-image manifest attributes |

A **live grant is a template-linked policy** whose `when` clause encodes scope and a time-box using Cedar operators — e.g. `context.request.domain like "*.github.com" && context.now < session.expires`, egress CIDRs via `context.request.ip.isInRange(...)`, presence via `has`. Cedar ships the operators we need: `like` with `*` wildcards, set ops, and extension functions `ip()` / `datetime()` / `duration()` with `.isInRange()` / `.isLoopback()` — directly useful for egress-CIDR and time-box conditions.

The **hard-deny tier** is admin-level `forbid` policies no permit can override: forbid `sudo` for any `principal.trust_tier == "untrusted"`; forbid `fs.write` to `~/.ssh`, `~/.aws`, `$PATH` dirs and shell rc files *even when broad read is granted*; forbid `net.egress` without TLS inspection on high-risk sessions. Because forbid-overrides is a language property, these are structurally un-bypassable. A direct production precedent exists for the whole shape: a major cloud agent-runtime gates every tool call with Cedar in **enforce mode** (deny-by-default, a single forbid overrides all permits, tool inputs inspected via `context.input`, un-permitted tools hidden from the tool list).

| Property | Cedar | OPA / Rego |
|---|---|---|
| Statically analyzable ("never grants more")? | **Yes** (SMT + Lean) | No (Turing-flexible) |
| Decision latency | sub-ms; reportedly **42–60× faster** than Rego *(vendor-published, unverified)* | ~1–10 ms in-memory |
| Independent security benchmark robustness | passes most cases | error-prone; failed ~**13/27** cases *(vendor-published, unverified)* |
| Model fit (PARC + forbid-overrides + deny-by-default) | native | needs hand-rolling |
| Right role in Shinken | **the unlock decision layer** | optional outer org/fleet rules only |

**Borrow, don't adopt, the enforcement ladder.** A third policy framework contributes one idea worth stealing: graduated enforcement *levels* — advisory / soft-mandatory (override allowed but logged) / hard-mandatory (no override). We attach this to each capability class as a **risk tier** on top of Cedar (§3), not as a competing engine.

Cedar docs: <https://docs.cedarpolicy.com/policies/syntax-policy.html>, <https://docs.cedarpolicy.com/policies/syntax-operators.html>, <https://docs.cedarpolicy.com/policies/templates.html>. Cedar Analysis: <https://aws.amazon.com/blogs/opensource/introducing-cedar-analysis-open-source-tools-for-verifying-authorization-policies/>. OPA-vs-Cedar migration & benchmarks: <https://aws.amazon.com/blogs/security/migrating-from-open-policy-agent-to-amazon-verified-permissions/>, <https://goteleport.com/blog/benchmarking-policy-languages/>, <https://www.osohq.com/learn/opa-vs-cedar-vs-zanzibar>.

**Pre-grant analyzability gate.** Before the panel commits *any* grant, and before any policy edit ships, run Cedar Analysis as a permissiveness-shift query and *block on regression*. This catches accidental escalation at author time, in CI and at the moment of grant — the property Rego cannot give us.

---

## 2. Layer 2 — the ocap caretaker / membrane handle

Cedar said "yes." The OS will eventually let the syscall through. Between those two moments sits the **capability handle** — the live switch the panel actually toggles.

A handle is an **unforgeable, attenuable, revocable reference** that *is* the authority (object-capability / POLA). The caretaker pattern wraps the real authority behind a proxy that forwards only while an `enabled` bit is true; flip the bit and every future use is dead instantly, fail-closed. Properties Shinken relies on:

- **O(1) synchronous revoke** — a bit-flip checked at *use time*, closing the ~120 s policy-cache stale-revocation window.
- **Attenuation, never widening** — a broad capability narrows when delegated (`fs.write[/work]` → `fs.write[/work/out]` for a sub-agent), never broadens. Defeats privilege creep.
- **Use-time checks kill TOCTOU** — the egress proxy, FS supervisor, and device-cgroup manager consult the live handle at the moment of the connection/syscall, so a capability revoked mid-flight fails the *very next* use. The confused-deputy and many TOCTOU classes go away by construction.
- **Replay-native** — issuing, attenuating, and revoking a handle are each first-class trajectory events (D5).

The **grant** and **revoke** wire protocols are explicit and both fail-closed:

```
GRANT(capability, scope):                 REVOKE(handle):
  1. Cedar IsAuthorized (cap + scope)        1. flip caretaker bit  → handle dead at next use
  2. Cedar Analysis permissiveness proof     2. delete/archive the template-linked Cedar policy
  3. issue capability handle                 3. tear down OS delta (drop CIDR, re-narrow
  4. push OS delta:                             Landlock, deny device node, drop ACL/SID)
       add CIDR to proxy allowlist           4. emit replay event
       extend Landlock ruleset             (synchronous; fail-closed if any step fails;
       device-cgroup allow                   revoke is idempotent)
  5. emit replay event
(fail-closed if ANY step fails)
```

Note the ordering on revoke: the bit flips *first* (instant effect), then the slower OS/policy teardown follows. The handle is the source of truth for "is this still allowed right now"; Cedar and the OS deltas are eventually-consistent around it.

Object-capability references: <https://en.wikipedia.org/wiki/Object-capability_model>, <https://tersesystems.github.io/ocaps/guide/management.html>.

---

## 3. The capability grammar — 8 classes, 4 risk tiers, taint

The unified abstraction the panel exposes is a **typed capability grammar**: 8 classes, each a record of `{ scope fields, risk_tier, lifecycle, enforcement_binding-per-OS }`. **Every class defaults to empty/false** — zero ambient authority — and the panel only ever *adds* scoped handles.

### 3.1 The 8 capability classes

| # | Class | Scope fields | Default | Notes |
|---|-------|--------------|---------|-------|
| 1 | `net.egress` | `domains[]`, `cidrs[]`, `ports[]`, `tls_inspect:bool` | none | enforced by the out-of-VM egress proxy (§5) |
| 2 | `fs.scope` | `read[]`, `write[]`, `deny[]` (path-specificity ordered) | write = working tree only; deny credential dirs | bind-mounts / Seatbelt subpaths / capability-SID ACLs |
| 3 | `clipboard` | `paste_in:bool`, `copy_out:bool` | false / false | `copy_out` is the exfil-relevant half |
| 4 | `gpu` | `device_ids[]`, `mode: compute \| render` | none | cgroup device controller + substrate routing |
| 5 | `install.privileged` / `sudo` | `scope: pkg \| any` | none | **the "unlock"** — minimal capabilities only, never `CAP_SYS_ADMIN` |
| 6 | `persistence` | `paths[]`, `survives_session:bool` | false | overlay/bind-mount scope |
| 7 | `credentials` | `broker_scopes[]`, `never_raw: true` (invariant) | none | brokered at the proxy; model never sees plaintext (§5) |
| 8 | `peripheral` | `camera`, `mic`, `usb[]`, `input_inject` | all false | device cgroup + seccomp; `input_inject` is hard-tier |

Scope is expressed *precisely* — domains/CIDRs/ports for egress, read/write/deny path lists for FS, device-ids + mode for GPU. Each image ships a declarative **capability manifest** (what it *may* ever request, and at which tier) analogous to entitlements / an Android manifest / a `Permissions-Policy` header; the Cedar schema validates every request against it, so an agent can never request a capability the image doesn't declare.

### 3.2 The 4 risk tiers (and the enforcement ladder)

The grammar attaches one of four tiers to each requested capability. The lower three map to the borrowed advisory / soft-mandatory / hard-mandatory enforcement ladder; the fourth is the un-overridable Cedar `forbid` tier.

| Tier | Behavior | Enforcement-ladder mapping | Example capabilities |
|------|----------|----------------------------|----------------------|
| **Auto** | run silently, still logged | advisory / auto-grant from a fixed safe allowlist | read scoped FS, query a11y tree, loopback net, idempotent reads, clipboard paste-in |
| **Notify** | run, but stream a non-blocking notice the human can veto | advisory (with veto) | low-risk writes *inside* the working tree |
| **Ask** | pause; require live human or reviewer approval | soft-mandatory (logged human override) | egress to a new domain, FS write outside workspace, install, GPU render, clipboard copy-out, send/post/pay/delete |
| **Block** | hard-deny, **no in-session override** | hard-mandatory + Cedar forbid-overrides | `sudo`/root, raw-device GPU, credential broker, persistence to system paths, peripheral input-injection, disabling the sandbox, writing the policy store |

Block-tier capabilities are not in-session unlockable at all: they require *pre-authorization in admin-set Cedar policy*. This is the line a compromised agent and a fatigued human both cannot cross.

### 3.3 Taint-aware promotion (the prompt-injection backstop)

Tier assignment is **not tool-name-only**. The core lesson of the prompt-injection literature is that once untrusted input enters an agent it can drive *any* downstream action, so the guardrail layer — not the model — must be the boundary, and **tainted data must not silently drive consequential actions.** Shinken therefore tracks a trust label on every input source (user instruction = trusted; web page, file from an untrusted repo, tool output = untrusted/tainted) and applies a hard rule:

> **Any action whose parameters derive from tainted data is promoted up at least one risk tier (Auto → Ask), regardless of the tool's base risk.**

This operationalizes the *lethal trifecta* / *Agents Rule of Two*: of the three ingredients {untrusted-content ingestion, sensitive-data/credential access, external communication}, allow at most **two** unattended; if a session needs all three, force human-in-the-loop for the exfil-capable action. We taint the turn on untrusted ingestion and downgrade later egress accordingly. A **tool-output sanitizer / injection probe runs *before* the agent consumes content**, and its verdict (`clean` / `possible injection`) is surfaced on the approval card. This is defense-in-depth, not a guarantee — adaptive prompt-injection attacks succeed **80–100%** of the time against model-level defenses *(vendor-published, unverified; "The Attacker Moves Second")* — which is exactly why the taint rule promotes to a *human/OS* gate rather than trusting a classifier.

Taint / trifecta references: <https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/>, <https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/>, <https://simonw.substack.com/p/new-prompt-injection-papers-agents>, CaMeL <https://arxiv.org/pdf/2503.18813>.

---

## 4. Layer 3 — OS enforcement primitives, per guest OS

The biggest implementation lesson from tearing down a production agent sandbox: **don't try to express domain allowlists in OS primitives.** The OS layer enforces only a *small enum* — `fs.read[roots]`, `fs.write[roots]`, `fs.deny[globs]`, `network ∈ {off, proxy-only, full}`, plus resource quotas — and *all* per-domain/per-method/credential logic lives in the OS-agnostic egress proxy (§5). seccomp sees the socket *family* not the destination IP; macOS SBPL network rules are host/port literals at best; Windows WFP scopes by account/protocol/port. None do domain allowlisting; forcing domain logic into the OS layer is the classic mistake.

### 4.1 Linux (default, v1 — deepest and most composable)

The production pattern is a **two-stage pipeline**, which Shinken adopts verbatim:

1. **Outer stage — bubblewrap (`bwrap`) builds the filesystem view and namespaces.** `--unshare-user --unshare-pid` (+ `--unshare-net` when network is fully restricted), `--ro-bind / /` for a read-only root, `--bind <root> <root>` per writable carve-out, protected subpaths (`.git`, agent dirs) re-bound read-only, `/dev/null` mounted over symlinks/missing protected paths to defeat symlink escapes. Overlapping FS entries apply **narrowest-last** (path-specificity), so `/repo=write, /repo/a=deny, /repo/a/b=write` nests correctly.
2. **Inner stage — re-exec self with `PR_SET_NO_NEW_PRIVS` + a narrow seccomp-bpf filter.** Two stages because setuid-`bwrap` breaks if `NO_NEW_PRIVS` is applied first. **seccomp is a network *switch*, not a giant allowlist:** default-allow, deny the socket syscalls, gate `socket()`/`socketpair()` by address family — `AF_UNIX`-only in restricted mode, `AF_INET`/`AF_INET6`-only in proxy-routed mode (to reach the local proxy bridge). Plus an **always-on hardening deny** of `ptrace` / `process_vm_readv`/`process_vm_writev` / `io_uring_*` — `io_uring` does network I/O without classic syscalls, so a "deny connect()" filter that forgets it is bypassable.

The layers a typical teardown *omits* and **Shinken must add**: **cgroups v2** for per-session cpu/mem/pids quotas (anti-DoS at high concurrency) and the **device controller** for the `gpu.device` unlock; the **Linux capabilities(7) bounding set** as the ceiling for any `sudo` unlock — minimal specific capabilities only, never the catch-all `CAP_SYS_ADMIN` (near-root, an escape vector, as are `CAP_SYS_MODULE`/`CAP_SYS_PTRACE`); and **Landlock as an optional *secondary* self-restriction tier** (not the primary FS enforcer — bind mounts express read-only-root + nested carve-outs more naturally) using ABI-6 **IPC scoping** (kernel 6.12) for `isolate-ipc` and ABI-7 **audit logging** (6.15) as denial telemetry (Landlock network rules landed in ABI 4 / kernel 6.7).

**Honest degradation is mandatory.** Unprivileged user namespaces are *disabled by default* on some hosts (a hardened Ubuntu 24.04 AppArmor restriction) and absent on WSL1; Landlock gates on kernel version. Feature-detect at runtime, warn/fallback, and surface the actual enforced level in the panel — never silently run unsandboxed.

Landlock refs: <https://docs.kernel.org/userspace-api/landlock.html>, <https://landlock.io/news/5/>. capabilities(7): <https://man7.org/linux/man-pages/man7/capabilities.7.html>.

### 4.2 macOS (v1, scarce premium tier)

Enforce with **Seatbelt via `/usr/bin/sandbox-exec`** (path **hardcoded** to resist PATH injection), generating an SBPL profile with a `(deny default)` base, then dynamically appending `(allow file-read*/file-write* (subpath (param "ROOT_n")))` rules with `(require-not (subpath …))` carve-outs for protected metadata, glob→anchored-regex deny rules for unreadable paths, and a **3-state network section** — fully open, fully closed, or loopback-only-to-the-proxy. Pass paths as `-D` *parameters*, not interpolated strings, to kill quoting attacks. **TCC** (camera/mic/Full-Disk-Access/Accessibility/Screen-Recording consent) is an *orthogonal user-consent axis*, not a behavior sandbox — it informs the panel's consent vocabulary and the per-image manifest, while Seatbelt does the actual restriction (and the macOS substrate pre-grants TCC, per [sandbox-infra.md](sandbox-infra.md)).

**Standing risk to track:** `sandbox-exec` is *officially deprecated* yet remains the only documented way to sandbox an arbitrary CLI process (still true through macOS 26.3, early 2026) with no Apple-sanctioned replacement. Isolate the SBPL generator behind an interface so it can be swapped, and watch Apple's `containerization` project for a successor. Refs: <https://github.com/apple/containerization/issues/737>, <https://hacktricks.wiki/en/macos-hardening/macos-security-and-privilege-escalation/macos-security-protections/macos-sandbox/index.html>.

### 4.3 Windows (v1, heavier tier)

The production reality diverges from the textbook "use AppContainer" advice. The pragmatic choice for *non-packaged* agent processes is a **restricted token** — `CreateRestrictedToken(WRITE_RESTRICTED | LUA_TOKEN | DISABLE_MAX_PRIVILEGE)` — plus **random per-workspace capability SIDs** (`S-1-5-21-<rand×4>`) as restricting SIDs, with file ACLs keyed to those SIDs for the writable carve-out. This is the cleanest realization of the ocap primitive on Windows: the capability SID is literally an **unforgeable, per-session, kernel-enforced, revocable write-authority handle** (delete SID + ACLs at session end). `WRITE_RESTRICTED` keeps *reads* broad (build tools work) while hard-gating writes; confidentiality therefore relies on explicit deny-read ACLs (authoritative only on the elevated backend) — a footgun to flag.

Network egress is **not** done by the token (a `WRITE_RESTRICTED` token only gives all-or-nothing network); it's done by the **Windows Filtering Platform (WFP)**: persistent block filters scoped by `FWPM_CONDITION_ALE_USER_ID` to a dedicated sandbox account, installed transactionally from an elevated helper. Session lifecycle uses a **job object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (guaranteed process-tree teardown — essential, and easy to forget) — and Shinken should *also* set job-object memory/active-process limits as the Windows analog of cgroups. UI isolation uses a **private desktop/window-station**. AppContainer/LPAC stays an optional *stronger* tier only where finer per-capability network (a network-client capability SID) is needed. Refs: <https://openai.com/index/building-codex-windows-sandbox/>, <https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation>.

### 4.4 Cross-OS capability → enforcement binding (the table the panel must surface)

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| `fs.read` / `fs.write` / `fs.deny` | bwrap `--ro-bind` / `--bind` / `/dev/null` mask + (optional) Landlock | Seatbelt `(subpath (param …))` + `(require-not …)` + glob→regex deny | restricted token + per-workspace capability-SID ACLs (deny-read elevated-only) |
| `net.egress` (gate) | netns + seccomp family-gate → proxy bridge | SBPL loopback-to-proxy section | WFP block on sandbox account → permit proxy |
| `net.egress` (which domains) | **egress proxy (§5)** | **egress proxy (§5)** | **egress proxy (§5)** |
| `gpu` | cgroup device controller (+ VFIO/vGPU substrate, see [sandbox-infra.md](sandbox-infra.md)) | n/a (premium tier; no GPU passthrough) | device assignment per substrate |
| `sudo` / `install.privileged` | minimal capabilities in the bounding set (never `CAP_SYS_ADMIN`) | n/a / app-layer | elevated dedicated-account backend |
| resource quotas | cgroups v2 | — | job-object limits |
| session teardown | `PR_SET_PDEATHSIG` + process-group kill | — | job object `KILL_ON_JOB_CLOSE` |
| denial telemetry | Landlock ABI-7 audit + proxy OTel | — | Windows audit + proxy OTel |

**Cross-OS divergence is sharp and asymmetric** — Linux is most expressive, macOS Seatbelt is expressive but deprecated/undocumented, Windows is token+WFP+ACL with coarse network. The unified grammar **degrades to the weakest per-OS enforcement and the panel must say so** (e.g. "on this Windows host without AppContainer, network egress is all-or-nothing at the OS layer; per-domain control is enforced only at the proxy"). The Linux/macOS/Windows substrate model is documented across the Claude Code sandboxing docs (<https://code.claude.com/docs/en/sandboxing>) and the Codex sandboxing docs (<https://developers.openai.com/codex/concepts/sandboxing>).

---

## 5. Network egress + credential/secret brokering

This is where the `net.egress` and `credentials` capabilities become physically real, and it is **out of the guest, shared across all three OSes.**

### 5.1 Forced out-of-VM egress proxy

The dominant production pattern: the guest runs **`network = none`**, and an external **loopback HTTP + SOCKS5 proxy** is the only path out. Two enforcement seams, because either alone is bypassable:

1. **Forced proxy env.** Override `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`WS(S)_PROXY` plus ~30 tool-specific keys (npm, yarn, pip, bundler, docker, electron, node…) in the child env, *force-overriding* any caller value so a command-level env cannot bypass the proxy; `NO_PROXY` keeps only loopback + RFC1918 direct.
2. **OS netns/firewall backstop.** Because env-var proxying alone is bypassable by a tool that ignores `*_PROXY` (raw sockets, statically linked binaries), back it with the OS network gate from §4 so an agent ignoring env *still* cannot reach the internet. **The proxy, not the agent loop, is the boundary**, and it must hold when the model is jailbroken.

Policy semantics (the safe defaults, implemented):

- **Deny-overrides, deny-by-default.** Per-host lattice `None < Allow < Deny`; empty allowlist blocks everything; structured audit event per decision.
- **Scoped wildcards only.** `host` (exact), `*.host` (subdomains, excludes apex), `**.host` (apex + subdomains). **Reject a bare global `*` in denylists / managed allowlists.**
- **Anti-domain-fronting.** Reject when the absolute-form `Host` header doesn't match the connect target; strip hop-by-hop headers including `Proxy-Authorization`.
- **Canonicalize at the trust seam.** Reject hostnames with NUL/control/percent-encoded/CRLF/non-DNS chars *before* the allowlist check. Real bug class: a JS `endsWith` allowlist passes a NUL-suffixed host while libc `getaddrinfo` truncates at the NUL and dials the attacker's host (the published SOCKS5 null-byte parser-differential). Naïve normalizers don't strip control chars — add it explicitly rather than copy the gap.
- **SSRF / non-public-IP block, at two points.** Block loopback, RFC1918, CGNAT (100.64/10), link-local 169.254/16 (incl. metadata 169.254.169.254), TEST-NET, multicast, IPv4-mapped IPv6 — both at policy time (resolve the host, block if *any* IP is non-public, **fail-closed on lookup failure**) *and* at TCP-connect time (re-check) to defeat DNS rebinding between check and dial.
- **DNS is a first-class egress channel.** Block raw outbound port 53 and force a controlled allowlisting resolver; otherwise an agent leaks via subdomain labels even with HTTP locked (a published PoC reconstructed SSNs from DNS subdomain queries when port 53 was open). Anomaly-detect high-info-per-domain query volume.
- **TLS-terminating MITM, fail-closed.** SNI/Host filtering can't enforce method/path/body or scrub/inject credentials — only MITM can. Make it available and **fail closed** for high-risk sessions (block, don't tunnel blind, when no CA is present); offer a *limited mode* of `GET`/`HEAD`/`OPTIONS` only. SNI/Host filtering is **not a hard boundary** — pin resolved IPs or add a firewall/VPC backstop, keep allowlists narrow, warn on broad entries (`github.com`, `*.amazonaws.com` accept data in URLs/query params).
- **Tightly scope IPC escape hatches.** Unix-socket proxying explicit-allowlist-only, loopback-bind-only; treat `docker.sock` as a full-host unlock at the Block tier.

Egress refs: Codex network-proxy source (<https://github.com/openai/codex/blob/main/codex-rs/network-proxy/src/policy.rs>, `.../http_proxy.rs`, `.../config.rs`); E2B default-none egress (<https://e2b.dev/docs/sandbox/internet-access>); SOCKS5 null-byte bypass (<https://oddguan.com/blog/second-time-same-sandbox-anthropic-claude-code-network-allowlist-bypass-data-exfiltration/>); DNS-tunnel PoC (<https://aurascape.ai/resources/auralabs-research/silent-leak-dns-tunneling-aws-agentcore-code-interpreter/>); sandbox network-isolation bypass (<https://unit42.paloaltonetworks.com/bypass-of-aws-sandbox-network-isolation-mode/>); managed/programmable egress (<https://blog.cloudflare.com/sandbox-auth/>).

### 5.2 Secret brokering — the model never sees plaintext

The `credentials` capability carries the invariant `never_raw: true`. Three complementary mechanisms, none of which exposes plaintext to the model:

1. **MITM header injection at the proxy.** Per-host hooks match exact host+method+path, **strip the agent's `Authorization` header**, and inject a server-held secret (from an env var or absolute-path file, exactly one source) with an optional prefix like `"Bearer "`. The agent only ever issues an *authless* request; the credential is added *downstream of the agent*. Requires TLS-MITM.
2. **A dedicated `mlock`'d token broker** for the model/API endpoint. Read the secret once from stdin via raw `read(2)`, `mlock(2)` it so it never swaps, mark the header sensitive, zeroize the stack buffer, forward on a *single* allowed route. The agent gets only a localhost proxy URL.
3. **Encrypted-at-rest store** with the key in the OS keyring (age/scrypt, 32-byte OS-RNG passphrase, atomic writes) for any secret that must persist locally — never plaintext config.

**Prefer just-in-time short-lived credentials over durable keys.** The control plane mints, and the broker injects, **per-session SPIFFE X.509/JWT SVIDs (via SPIRE), Vault dynamic secrets, or cloud workload-identity-federation tokens** — minutes-to-hours TTL, so a leaked credential self-expires and lifetime ties to the session/grant lifecycle for instant revocation. This is the **Vault/KMS brokering** path Shinken integrates (Vault, any cloud KMS, or SPIFFE/SPIRE).

**Secrets must never enter the replay/audit trail.** Mark brokered tokens, injected headers, and takeover-mode keystrokes (§6) sensitive; the trajectory records only "credential X brokered for scope Y." Scrub child-process env; a regex output-redactor (`sk-` keys, AWS `AKIA…`, bearer tokens, `api_key/token/secret = …` assignments) is a best-effort backstop, not the primary control. A naïve audit store that captures secrets becomes the top exfil target.

Secret-broker refs: Codex MITM hook + token broker (<https://github.com/openai/codex/blob/main/codex-rs/network-proxy/src/mitm_hook.rs>, `.../responses-api-proxy/src/read_api_key.rs`); SPIFFE/SPIRE (<https://www.hashicorp.com/en/blog/spiffe-securing-the-identity-of-agentic-ai-and-non-human-actors>); Vault Enterprise 1.21 SPIFFE/SVID (<https://www.hashicorp.com/en/blog/vault-enterprise-1-21-spiffe-auth-fips-140-3-level-1-compliance-granular-secret-recovery>); Claude Code credential-broker/secure-deployment pattern (<https://code.claude.com/docs/en/agent-sdk/secure-deployment>).

```
   agent (authless request)            egress proxy (out of guest)            upstream
   ───────────────────────►   ┌──────────────────────────────────┐   ─────────────────►
   GET api.github.com/...      │  1. canonicalize host             │   GET api.github.com
   (no Authorization)          │  2. allowlist + deny-overrides    │   Authorization:
                               │  3. SSRF / DNS-rebind check       │     Bearer <JIT SVID>
                               │  4. TLS-MITM terminate            │   (injected here, never
                               │  5. strip client Authorization    │    seen by the agent)
   broker mints per-session ──▶│  6. inject brokered secret        │
   short-lived SVID (Vault/    │  7. OTel audit (no secret, no URL │
   KMS/SPIFFE), TTL-bound      │     query logged)                 │
                               └──────────────────────────────────┘
```

---

## 6. Human-in-the-loop — escalation-on-failure, the live approval card

The HITL loop sits *on top of* the OS substrate, in strict order: **(1) the deterministic Cedar-backed allow/ask/deny matrix is the always-on boundary; (2) an optional reviewer-classifier may auto-grant only Ask-tier actions; (3) the OS enforcement holds regardless.** The classifier is an accelerator, never the sole gate — a real-world auto-grant classifier showed a **17% false-negative rate on dangerous actions** (and **0.4% false-positive on n=10,000**) *(vendor-published, unverified)*, so it sits *behind* the deterministic matrix and the OS wall.

### 6.1 The deterministic matrix

`deny → ask → allow`, **first-match-wins**, with **deny-wins at any scope** and **managed > project > session** precedence so **an agent can never widen its own policy**. The policy store and panel config are themselves resources the sandbox is *forbidden to write*. Rules use a `Tool(specifier)` grammar with compound-command/wrapper-aware matching (a `safe && evil` chain must match *each* sub-command; wrappers like `timeout`/`nice`/`nohup` are stripped first). But **argument-constraining string rules give false safety** — trivially bypassed by flags/redirects/env-vars/env-runners — so command strings are used only for *UX auto-approve*, and every boundary is backed by the OS sandbox and the proxy.

### 6.2 Escalation-on-failure (the D6 default)

Separate the **"what triggers a prompt"** dial (approval policy) from the **"what the OS physically permits"** dial (sandbox/capability), then **start every session at least-authority** and surface a context-rich **Ask at the exact boundary an action hits**, not front-loaded prompts. A sandbox-denied action does **not** auto-escalate — it falls back to a prompt asking the human to approve a retry *with* the specific elevation. The three-way decision is **Run / Escalate / Deny**:

- **Run** — execute in-place inside the current sandbox.
- **Escalate** — run with the requested additional capability (an *additional-permission overlay*: only the specific `network` / `fs.read` / `fs.write` grant, merged onto the active turn, never a blanket unlock), at `Turn` or `Session` scope.
- **Deny** — return the reason to the agent as a tool result instructing it to respect the boundary, not a silent failure.

This minimizes total prompts (fighting the **~93%** rubber-stamp rate *(vendor-published, unverified)*) and gives the human concrete context at decision time; combine with a fixed safe-by-default Auto allowlist so harmless actions never reach the panel. Escalation refs: <https://developers.openai.com/codex/agent-approvals-security>, <https://developers.openai.com/codex/concepts/sandboxing>; auto-mode classifier <https://www.anthropic.com/engineering/claude-code-auto-mode>; permission grammar <https://code.claude.com/docs/en/permissions>.

### 6.3 The live approval card

On an Ask, **pause the session** and stream a typed action card showing:

- **actor** (agent id + model version), **action verb**, **resource**;
- **computed blast-radius / preview** — exact command, files touched, domain + bytes, email recipient + draft, diff;
- the **policy decision path** (which rule/tier fired) and the **taint / injection-probe verdict**;
- **grant options** following a three-action model — **Approve-once / Approve-this-session (expires at session end) / Approve-pattern ("don't ask again for `Tool(specifier)`") / Deny / Cancel / Takeover** — distinguishing an explicit *deny* from a mere *dismissal* (so we neither re-prompt forever nor wrongly record a denial).

**Batch sibling requests** (three writes under one dir, a multi-domain egress unlock) into one card to cut prompt count — but never batch across risk tiers, and never auto-persist Block/credential actions. **Lifecycle:** `once` (one-shot, handle dies after the action) / `session` (expires at session end, enforced by both `context.now < session.expires` in Cedar *and* a handle TTL) / `standing` (admin-set in policy only).

### 6.4 Side-effect gating, Takeover, and Watch Mode

- **Action-type / reversibility gating.** Show a concrete preview and require explicit confirm before *irreversible* side effects (send/post/pay/delete) — gated by deterministic product rules, not model training alone.
- **Takeover Mode + URL-mode credential entry.** When an action needs a credential/payment/OAuth, switch to an **out-of-band secure surface** the agent cannot screenshot or keylog; secrets bypass the model context and the recording entirely (the broker injects the scoped token at the proxy, §5.2). Copy the anti-phishing rules: show the full URL, highlight the domain, never auto-open/pre-fetch, bind the consent to the verified user identity. The trajectory records only "credential brokered," never keystrokes.
- **Watch Mode** for the most dangerous edge of the Ask tier (operating on a production system, bulk deletes): require an *active, present* human, pause if they disengage. For autonomous/headless fleets, anything that would require Watch Mode is **hard-denied unless pre-authorized** — never silently auto-approved.

### 6.5 Fail-closed circuit breakers

- Unmatched action ⇒ **Ask**. Reviewer/human timeout ⇒ **action does NOT run** (record `timed_out`). Block/Critical ⇒ **auto-deny**.
- **Denial-threshold breaker:** after a threshold (e.g. **3 consecutive or 20 total** denials in a session) hard-escalate to a human or terminate a headless session, so a prompt-injected agent cannot grind against the boundary indefinitely.
- **Judge actions, not narration.** Feed any auto-grant reviewer a *stripped* record (user request + actor + tool + resource only — **no** assistant explanation, **no** tool outputs), and run the injection probe on tool output *before* the agent (and the reviewer) sees it, so a jailbroken agent cannot rationalize its way to approval.

The just-in-time consent transport, if Shinken exposes its ACI/panel over MCP, maps cleanly: **form-mode elicitation** (flat typed schema → rendered approval card → accept/decline/cancel) for the live Ask, **URL-mode elicitation** (error `-32042`) for any credential/payment/OAuth step that must bypass the client/LLM context. Servers MUST NOT request secrets via form mode. Refs: <https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation>, Operator/CUA safety primitives (<https://help.openai.com/en/articles/11752874-chatgpt-agent>, <https://openai.com/index/prompt-injections/>).

---

## 7. The generic `tool_runner` boundary pattern

D6's permission model is the realization of a single architectural principle that runs through D2 and D9: **the agent loop runs *outside* the sandbox, and every tool call routes through a controlled API that enforces policy and capability *before* anything executes.** This is the generic **`tool_runner` policy boundary** — vendor-neutral, and the shape every modern agent runtime is converging on. Permissions are enforced by the *harness*, never by the model; prompt and system instructions only shape *intent*.

```
┌────────────────────────────────────────────────────────────────────────────┐
│  AGENT LOOP (provider-agnostic, OUTSIDE the sandbox)                          │
│     model → proposes tool call (verb + args, possibly tainted)                │
└───────────────────────────────────┬──────────────────────────────────────--─┘
                                     │  tool_runner boundary  (Action Gateway, D9)
                                     ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  tenant-auth → rate-limit/budget → TAINT check → CEDAR (decide) →          │
   │  handle issue/lookup → HITL Ask card if needed → dispatch                  │
   └───────────────────────────────────┬──────────────────────────────────────┘
                                        ▼  (only now does anything run)
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  SANDBOX (OS-enforced) + egress proxy (out of VM) + secret broker          │
   │  handle consulted AT USE TIME; the wall holds even if the model is jailbroken│
   └──────────────────────────────────────────────────────────────────────────┘
```

Concretely the boundary funnels through **one place** (the **Action Gateway**, D9: tenant-auth → token-bucket/WFQ rate-limit → budget → **Cedar policy** → dispatch), so policy selection is auditable and centralized. **Code-as-action** (`exec`/`bash`/`edit`) is, per **D2**, a *separate off-by-default capability class* that lives behind exactly this boundary — it is never an ambient affordance. The three-axis taxonomy worth adopting wholesale: (1) **command admissibility** (allow/prompt/forbidden), (2) **sandbox confinement mode** (read-only / workspace-write / full-access), (3) **approval policy** (never / on-failure / on-request / unless-trusted) — kept independent, with **strictest-wins** merge (`Allow < Prompt < Forbidden`) and **paths-as-parameters** (never string-interpolated policy) to eliminate quoting/injection bugs. Reference implementations of this boundary: <https://github.com/anthropic-experimental/sandbox-runtime>, <https://developers.openai.com/codex/concepts/sandboxing>, <https://code.claude.com/docs/en/sandboxing>.

The first user-visible payoff is the headline feature itself: the panel *unlocks advanced image features* (GPU, broader network, install/sudo) on demand, each unlock flowing decision → handle → OS-delta and recorded as a first-class, forkable replay event — turning what competitors treat as a static, error-prone config file into a live, provable, auditable authority timeline.

---

## 8. Replay & audit integration (folds into D5)

Every step of the authority lifecycle is a **first-class, signed, hash-chained replay event** (D5), carrying the **triple identity** (authorizing human UID, agent id + model version, tool), the params **SHA-256** (not raw values), the policy decision + rule id, risk tier, taint/injection verdict, outcome, and the grant's **scope + lifetime + revocation**. Because LLM behavior is non-deterministic, replay is *forensic reconstruction* from this trace, not a re-run — so the record must be self-sufficient. Align field names to the OpenTelemetry GenAI semantic conventions / ISO 42001. Events captured: **request, auto-grant, human approve/deny/cancel, override (with overriding actor), timeout, attenuation, revocation**, plus every proxy egress decision and broker grant. **Excluded:** takeover-mode keystrokes and brokered secrets (mark sensitive, redact, never persist plaintext). OS-layer denial telemetry (Landlock ABI-7 audit, WFP audit, the proxy's per-decision OTel events) feeds the *same* timeline — turning the "auditable authority timeline" from aspiration into telemetry these primitives already emit. Audit/OTel refs: <https://www.loginradius.com/blog/engineering/auditing-and-logging-ai-agent-activity>, <https://galileo.ai/blog/ai-agent-compliance-governance-audit-trails-risk-management>.

---

## 9. Open risks / spikes (carry into [open-questions.md](open-questions.md))

1. **Cedar–handle–OS consistency.** The handle is the live truth; Cedar and OS deltas are eventually-consistent around it. Specify and test the convergence semantics and the fail-closed behavior when an OS-delta teardown lags a revoke.
2. **Unprivileged-userns / Landlock availability.** Feature-detection + honest panel degradation is load-bearing; validate the matrix across target host kernels (Ubuntu 24.04 AppArmor restriction, WSL1 rejection, Landlock ABI gating).
3. **macOS `sandbox-exec` deprecation.** A standing strategic risk; keep the SBPL generator swappable and track Apple's `containerization` successor.
4. **TLS-MITM at scale.** CA distribution into guests, cert-pinned-client breakage, and the latency/throughput cost of terminating TLS for high-risk sessions need a first-party measurement (numbers above are vendor-published, unverified).
5. **DNS-channel hardening.** Validate that the controlled resolver + port-53 block + anomaly detection actually closes the subdomain-label exfil channel under adversarial DNS.
6. **Auto-grant classifier ROC.** The 0.4% FP / 17% FN figures are from one vendor's traffic; Shinken needs its own measurement before letting the reviewer auto-grant anything beyond the fixed safe allowlist.
7. **Capability-state GC.** Per-workspace capability SIDs/ACLs (Windows) and persistent WFP filters survive reboots; revocation must remove SIDs, ACLs, and filters or stale authority re-widens later sessions.

---

### Reconciliation to canon

- **D6 (this note's spine):** 3-layer model (Cedar / ocap caretaker / OS enforcement) — §0–§4; Cedar-not-OPA — §1; 8 capability classes + 4 risk tiers + taint — §3; forced out-of-VM egress proxy + Vault/KMS secret brokering, header-injection so the model never sees plaintext — §5; escalation-on-failure (Run/Escalate/Deny) HITL with the live approval card — §6; approvals/denials as first-class replay events — §8; aligns with the `tool_runner` boundary — §7.
- **D2:** code-as-action as a separate off-by-default capability class behind the `tool_runner` boundary — §7.
- **D5:** the authority lifecycle as signed, hash-chained, forkable replay events; secrets excluded — §8.
- **D9:** the Action Gateway as the single choke point where Cedar policy is evaluated — §7.
- **D1 / D11:** the GPU capability binds to the cgroup device controller and the substrate router (VFIO/vGPU/MIG); GPU-TEE + attestation for the trusted tier — see [sandbox-infra.md](sandbox-infra.md).
