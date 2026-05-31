# Sandbox Capabilities

Audience: users and agent developers who need to understand Shinken's safety boundary.

The full design is D6 in [`../design/tech-decisions.md`](../design/tech-decisions.md). The v0.0.1 plan is in
[`../engineering/v0.0.1-plan.md`](../engineering/v0.0.1-plan.md).

## What A Capability Is

A capability is a sandbox power that crosses a meaningful boundary. Examples:

- Network egress.
- Filesystem scope or host mounts.
- Credentials.
- Clipboard.
- GPU.
- Persistence.
- Privileged install or sudo.
- OS automation / peripheral access.

Ordinary GUI actions inside the already-provisioned Sandbox should not ask on every click. Boundary
changes should be explicit and recorded.

## v0.0.1 Scope

v0.0.1 should implement the capability semantics locally:

- A session capability envelope.
- A local Action Gateway shim.
- Grant / deny / narrow decisions.
- Basic file/artifact scope checks where applicable.

This is not yet the full production enforcement stack.

## Later Production Enforcement

Later releases add:

- Cedar policy decisions.
- Object-capability handles and synchronous revoke.
- OS/substrate enforcement.
- Out-of-VM egress proxy.
- Secret broker integration.
- Control Panel capability cards.

## Security Rule Of Thumb

The model can be prompt-injected by any untrusted observation: pixels, DOM, a11y text, files, web
pages, emails, PDFs, tool output. Capability checks must be enforced by the runtime/control plane,
not by model prompts alone.
