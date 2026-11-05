# Idea: Deflection Engine (agentflow test fixture)

A small stand-in for `docs/ideas/agentic-rag-customer-support.md`, with the same sections the doc checks read.

## Problem Statement

Customers ask the same questions again and again.

## MVP Scope

### In

- The agent asks before it escalates.

| Area | Owner |
|---|---|
| Console | Staff |

## Not Doing (and Why)

- **Phone support:** out of scope, every metric comes from conversations inside Tiva Care.

## Decisions Log

| Question | Decision |
|---|---|
| Which customer identifier does the web app provide? | MISA **customer code** |
| MISA sandbox / rate limit | Sandbox AppID available; defensive caching |

## Open Questions

**MVP**
- Ngưỡng làm rõ: how many clarifying rounds before the agent escalates?
- Is zero data retention available with each LLM provider we shortlist?

**Phase 2**
- IdentityServer4 claim names and access-token lifetime.
