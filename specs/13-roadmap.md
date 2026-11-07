# 13. Roadmap and open questions

Status after the second iteration. Nothing here is committed.

## Delivered since the first version

| Area | Delivered | Spec |
|---|---|---|
| Automation | scheduler (reminders, recurring jobs, cron), heartbeat | 14 |
| Access control | pairing flow, admin commands, approvals for sensitive tools | 15 |
| Capabilities | `web_fetch` with SSRF guard, pluggable `web_search`, Docker command sandbox | 16 |
| Memory | list/forget/clear (chat + CLI), hybrid semantic+keyword backend, retention/housekeeping | 06 |
| Quality | 390 hermetic tests + 30 integration tests, mypy-clean source, CI workflow, in-place DB migrations | 17 |
| Scheduler | at-least-once firing via a firing lease (a crash no longer drops a run) | 14 |
| Security | `web_fetch` pins the connection to the vetted IP, closing the DNS-rebinding window | 16 |
| Observability | structured per-turn logging (levels/areas/file/JSON) and a read-only live web dashboard | 18 |
| Safety of logs | content hidden by default, secrets always masked (verified with canaries), third-party DEBUG capped | 18 |
| Storage | persistence behind protocols + registry; second (in-memory) backend; shared behavioural contract tests | 19 |
| Multi-agent | **isolation** (no owner persona/core memory, private files + memory), drop-in agent files, hot reload (watcher / SIGHUP / `/reload`), routing rules, user `/agent` switching, `kiro-crew agent` CLI, stateful delegates, pooled MCP servers, name and reference validation | 08, 20 |
| Verification | real-infrastructure integration tests; found and fixed real issues (see spec 17) | 17 |

## Known gaps

| Area | Gap | Impact |
|---|---|---|
| Verification | **Still never run for real:** LLM providers, Discord, Slack, Telegram, live WhatsApp, Docker containers, real search/embedding APIs. The tests exist (spec 17) and skip until you supply credentials/Docker. Already run for real: HTTPS/TLS/DNS fetching and an MCP stdio server | integration bugs possible (the suite already caught a Slack start-up crash that only a live run would have shown) |
| CI | workflow (incl. the integration job) not yet run on GitHub; only Python 3.11 verified locally | |
| Channels | text only: no attachments, images, voice, reactions, edits, streaming | limited media use |
| Channels | 5 platforms (others such as iMessage, Teams, Signal, Matrix, Google Chat are not built) | add via the `Channel` contract |
| Channels | WhatsApp 24 h window / templates not handled | late task results may fail |
| Interfaces | no TUI; no onboarding wizard / `doctor` command (a read-only web dashboard exists, see spec 18) | CLI + YAML + dashboard |
| Tasks | progress not pushed to chat while running; single process | users poll with `/tasks`; no HA |
| Scheduler | no per-job timezone (uses the global `scheduler.timezone`) | |
| Memory | vector scan is in-process (thousands of facts per user); no consolidation/decay of facts | use a vector DB backend for scale |
| Security | no rate limits or token/cost budgets; prompt-injection defences are mitigations, not guarantees | |
| Observability | logging only; no metrics, tracing export or cost tracking | |
| Storage | only `sqlite` and a non-persistent `memory` backend exist; no Postgres/DynamoDB backend yet | single node (the abstraction and contract tests are ready for one) |
| Packaging | no Dockerfile, systemd unit or published wheel | |
| Skills | no SKILL.md-style on-demand instruction packages or shared catalog | |
| Models | no auth-profile rotation or provider sign-in flows | static keys |
| Agents | no external coding-agent workers; no chat-driven agent creation (deliberate); channel settings still need a restart; `is_group` is untested against the live Discord/Slack/Telegram services | |
| Devices | no browser control, voice, camera/screen or companion apps | |

## Candidate features (rough priority)

1. **Run the integration tests with real credentials and Docker** and fix what they find (the tests are written; see spec 17).
2. **Observability and budgets:** OpenTelemetry (Strands already emits spans), per-user token/cost counters, rate limits.
3. **Packaging:** Dockerfile, systemd unit, published wheel, `kiro-crew doctor` and an onboarding wizard.
4. **Skills:** on-demand instruction packages loaded by name, with a local catalog.
5. **Channel breadth and media:** attachments/images, streaming or edited replies, more platforms.
6. **Browser tool** running inside the sandbox image.
7. **A Postgres (or DynamoDB) storage backend** to allow multiple instances; the protocols and contract tests already exist.
8. **Web dashboard** for config, sessions, tasks, schedules, pairing and memory.
9. **Spawnable sub-agents** (ad-hoc, with their own history and budget).
10. **Memory consolidation:** merge and decay facts, summarise clusters.

## Open questions

- Should long-term memory default to `shared` for single-owner deployments to avoid cross-channel identity setup?
- Should unauthorised senders on `allowlist` channels get a one-time hint containing their id (better UX, slight presence leak)?
- Which vector/KB backend should be the first-class option beyond the in-process hybrid store?
- Should approvals support "approve for this session" to cut prompt fatigue?
