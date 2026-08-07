# ADR-010 — Permissionless gateway, no key custody

**Status:** Accepted
**Implementation:** Implemented in the reference services (`services/`): the gateway relays
and never holds keys or funds.

## Context

The original specification called for a gateway translating OpenAI-style calls into chain
transactions. A single gateway holding user keys would be a **point of centralisation** and a
**chokepoint**, contrary to the decentralisation objective.

## Decision

The gateway is **permissionless and replicable**, with **no key custody**: **transaction signing
happens client-side**. Anyone can run a gateway instance.

## Consequences

- No single point of failure or censorship.
- The gateway only translates and relays; it never holds funds or keys.
- UX: wallet integration and client-side WebSockets.
