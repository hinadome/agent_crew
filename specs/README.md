# Specifications

Detailed functional specifications for kiro-crew. Each document describes behaviour as implemented,
with numbered requirements (e.g. `MEM-4`) so they can be referenced from issues, reviews and tests.

| # | Spec | Covers |
|---|---|---|
| 01 | [Overview and requirements](01-overview-and-requirements.md) | goals, scope, actors, glossary, non-functional requirements |
| 02 | [Configuration](02-configuration.md) | YAML schema, env expansion, validation rules |
| 03 | [LLM providers](03-providers.md) | provider contract, built-ins, profiles, failover, switching |
| 04 | [Channels](04-channels.md) | channel contract, each adapter, message limits |
| 05 | [Gateway](05-gateway.md) | auth, sessions, concurrency, commands, replies |
| 06 | [Memory](06-memory.md) | four memory layers, scope, extraction, injection |
| 07 | [Background tasks](07-tasks.md) | queue, lifecycle, recovery, tools, notifications |
| 08 | [Agents and crew](08-agents-and-crew.md) | agent assembly, **isolation**, delegation (stateless/stateful), pooled MCP |
| 09 | [Tools](09-tools.md) | tool resolution, built-in tools, sandboxing |
| 10 | [Extensibility](10-extensibility.md) | registries, plugin loading, extension contracts and examples |
| 11 | [Security](11-security.md) | threat model, controls, residual risks |
| 12 | [CLI and operations](12-cli-and-operations.md) | commands, logging, files, deployment, shutdown |
| 13 | [Roadmap and open questions](13-roadmap.md) | delivered work, known gaps and candidate features |
| 14 | [Scheduler and heartbeat](14-scheduler-and-heartbeat.md) | reminders, recurring jobs, cron, proactive heartbeat |
| 15 | [Access control and approvals](15-access-control-and-approvals.md) | allow-lists, pairing flow, admins, tool approvals |
| 16 | [Web tools and command sandbox](16-web-and-sandbox.md) | web fetch/search with SSRF guard, Docker sandbox |
| 17 | [Testing and CI](17-testing-and-ci.md) | test design, coverage map, fault-injection checks, CI |
| 18 | [Logging and the web dashboard](18-logging-and-web-ui.md) | structured logs, per-turn tracing, levels/areas, redaction, live web UI |
| 19 | [Storage backends](19-storage.md) | storage protocols, registry, sqlite/memory backends, behavioural contract |
| 20 | [Agent management](20-agent-management.md) | drop-in agent files, routing rules, `/agent` switching, hot reload, `kiro-crew agent` |

## Conventions

- **MUST / SHOULD / MAY** follow RFC 2119.
- Requirement ids use a prefix per spec: `CFG`, `PRV`, `CHN`, `GW`, `MEM`, `TSK`, `AGT`, `TLS`, `EXT`,
  `SEC`, `OPS`, `SCH`, `ACC`, `WEB`, `SBX`, `LOG`, `WEB-UI`, `STO`, `AGT`, `AGF`, `RTE`, `SWT`, `RLD`, `CLI`.
- Each spec ends with **Acceptance criteria**. Criteria marked *(verified)* are exercised by the automated suite
  (`uv run pytest`, scripted fake LLM, no external services); *(unverified)* means implemented but not yet exercised against
  real external services.
- Source paths are relative to `src/kiro_crew/`.
