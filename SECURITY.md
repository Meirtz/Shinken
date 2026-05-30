# Security Policy

Shinken runs untrusted, AI-generated actions against real desktops. Security is a first-class
concern — see [`docs/design/threat-model.md`](docs/design/threat-model.md).

## Reporting a vulnerability

**Please do not open a public issue for security problems.** Instead, use GitHub's
[private vulnerability reporting](https://github.com/Meirtz/Shinken/security/advisories/new)
("Report a vulnerability" under the Security tab). We aim to acknowledge reports within a few days.

When reporting, include: affected version/commit, a description and impact, reproduction steps,
and any logs or a `.skn` replay bundle that helps.

## Scope

In scope: sandbox escape, cross-tenant isolation breaks, the egress/credential broker, the
permission/capability engine, and the replay store. Out of scope (pre-release): the design docs
themselves and not-yet-implemented components.

## Disclosure

We follow coordinated disclosure: we will work with you on a fix and a disclosure timeline before
any public advisory.
