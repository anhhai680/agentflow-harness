# Spec: `greet`

**Status: approved** (2026-11-01)

## Objective

Say hello to a name: `GET /greet?name=<name>` answers `Hello, <name>!`.

## Contracts this module provides

- `GET /greet` returns `{"greeting": "Hello, <name>!"}`.
- `toy.greet(name: str = "world") -> str`.

## Open Questions

None.

**Resolved (2026-11-01, API review):**
- **Q1 — Default name.** `world` when no name is given.
