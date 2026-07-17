# Design: security architecture

**Status:** current **Related:** [`frontend-framework.md`](frontend-framework.md) (§Auth — the web request-auth
chokepoint), [`spike-infrastructure.md`](spike-infrastructure.md) (sandbox egress allowlist),
[`workspace-model.md`](workspace-model.md) (cross-Project default-deny), [`../PRODUCT.md`](../PRODUCT.md) §9 (security
posture this implements)

## Overview

Cross-cutting rules for *how* security checks are built in themis, distinct from the per-area docs that own each
mechanism. The load-bearing one: every critical security check runs at a single **default-on chokepoint** — never as a
step each call site opts into.

## Background

A security check that each call site must remember to invoke is a latent hole. The default is *unprotected*, and an
omission — a new route, a new tool, a new egress path — is indistinguishable from a deliberate exemption. The system is
only as safe as its least careful addition, and that addition is reviewed by someone who has to notice an *absent* line.
Request auth, egress restriction, data-boundary enforcement, and sandbox tool-gating all share this shape, so the rule
is stated once here rather than re-argued per area.

## Design

A critical security check MUST be a single chokepoint that is **enforced by default**, with exemptions **explicitly
allowlisted** in one place.

- **Default-deny.** Absence of an explicit decision denies. A new code path inherits the check for free; exempting it is
  a visible, reviewable edit to the allowlist — not a matter of remembering to add a line. The reviewer reads a present
  entry, never has to spot a missing one.
- **One implementation.** The check's logic lives once and is shared. Call sites neither re-implement nor re-decide it.
- **At the resource, not only the perimeter.** The authoritative check sits where the protected resource is actually
  accessed. A perimeter gate (an HTTP proxy, an API gateway) is defense-in-depth *on top*, never the sole authority: a
  framework bug (e.g. Next.js CVE-2025-29927 skipped middleware via a request header) or a routing change that silently
  drops coverage can bypass a perimeter while the resource-side check still holds.
- **Fail loud.** A check that cannot run — missing config, unverifiable input — denies and raises. It never degrades to
  allow ([`../style/general.md`](../style/general.md) "Fail loud").

"If at all possible" is a real qualifier. Where a language or framework offers no default-on seam, the fallback is the
narrowest shared wrapper *plus* a lint/CI rule that fails when a call site skips it — the allowlist enforced
mechanically, not by convention. A per-call-site check guarded only by reviewer discipline does not satisfy this rule.

## Alternatives considered

- **Per-call-site enforcement** (each route/tool invokes the check itself). Rejected: opt-in, so the default is
  unprotected and an omission reads the same as an intentional exemption — the Background failure mode.
- **Perimeter-only enforcement** (one gate in front, nothing at the resource). Rejected as the *sole* layer: bypassable
  by a framework bug (CVE-2025-29927) or a routing/matcher change that drops coverage unnoticed. It is a valid added
  layer, not the authority.

## Implementation state

- **Web request auth** — a proxy default-deny perimeter plus a shared, request-scoped accessor that re-verifies the IAP
  assertion at the data seam; public routes (`healthz`, the HMAC-verified webhook receiver) are the allowlisted
  exemptions. Owned by [`frontend-framework.md`](frontend-framework.md) §Auth. In flight.
- **Sandbox egress** — deny-by-default VPC egress plus a curated allowlist, and a binary allowlist in the sandbox image.
  Owned by [`spike-infrastructure.md`](spike-infrastructure.md).
- **Cross-Project data** — default-deny sharing; case-level content never crosses Project boundaries implicitly. Owned
  by [`workspace-model.md`](workspace-model.md) and [`../PRODUCT.md`](../PRODUCT.md) §7.
