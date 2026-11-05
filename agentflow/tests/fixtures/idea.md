# Idea: Support Assistant (agentflow test fixture)

A small stand-in for a product's idea doc, with the same sections the doc checks read.

## Problem Statement

Customers ask the same questions again and again.

## MVP Scope

### In

- The agent asks before it escalates.

| Area | Owner |
|---|---|
| Console | Staff |

## Not Doing (and Why)

- **Phone support:** out of scope, every metric comes from conversations inside the product.

## Decisions Log

| Question | Decision |
|---|---|
| Which customer identifier does the web app provide? | CRM **customer code** |
| CRM sandbox / rate limit | Sandbox API key available; defensive caching |

## Open Questions

**MVP**
- Ngưỡng làm rõ: how many clarifying rounds before the agent escalates?
- Is zero data retention available with each LLM provider we shortlist?

**Phase 2**
- IdentityServer4 claim names and access-token lifetime.
