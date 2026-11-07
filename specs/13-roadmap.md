# 13. Roadmap and open questions

Status: current as of the autonomy work (spec 21). The candidate features below are proposals, not commitments.

## Delivered since the first version

| Area | Delivered | Spec |
|---|---|---|
| Automation | scheduler (reminders, recurring jobs, cron), heartbeat | 14 |
| Access control | pairing flow, admin commands, approvals for sensitive tools | 15 |
| Capabilities | `web_fetch` with SSRF guard, pluggable `web_search`, Docker command sandbox | 16 |
| Memory | list/forget/clear (chat + CLI), hybrid semantic+keyword backend, retention/housekeeping | 06 |
| Quality | 493 hermetic tests (as of spec 21) + 30 integration tests, mypy-clean source, CI workflow, in-place DB migrations | 17 |
| Scheduler | at-least-once firing via a firing lease (a crash no longer drops a run) | 14 |
| Security | `web_fetch` pins the connection to the vetted IP, closing the DNS-rebinding window | 16 |
| Observability | structured per-turn logging (levels/areas/file/JSON) and a read-only live web dashboard | 18 |
| Safety of logs | content hidden by default, secrets always masked (verified with canaries), third-party DEBUG capped | 18 |
| Storage | persistence behind protocols + registry; second (in-memory) backend; shared behavioural contract tests | 19 |
| Multi-agent | **isolation** (no owner persona/core memory, private files + memory), drop-in agent files, hot reload (watcher / SIGHUP / `/reload`), routing rules, user `/agent` switching, `kiro-crew agent` CLI, stateful delegates, pooled MCP servers, name and reference validation | 08, 20 |
| Verification | real-infrastructure integration tests; found and fixed real issues (see spec 17) | 17 |
| Autonomy | task completion criteria + verifier with fix rounds; tool-call, repeated-call and token budgets; tasks that ask the user and wait (no worker held, survive restarts); retries that explain the failure; plan + notes re-injected every model call; parallel one-level sub-tasks | 21 |
| Setup | `kiro-crew init --provider ... [--base-url --model-id]` writes a config for your LLM (incl. the pinned `coder` agent) | 02 |
| Quality | tests for memory auto-extraction, the real provider builders, plugin modules and entry points, and the Telegram/Slack/Discord inbound rules; fixed a dashboard 500 caused by non-JSON log fields and a Telegram double space | 17 |

## Known gaps

| Area | Gap | Impact |
|---|---|---|
| Verification | **Still never run for real:** LLM providers, Discord, Slack, Telegram, live WhatsApp, Docker containers, real search/embedding APIs. The tests exist (spec 17) and skip until you supply credentials/Docker. Already run for real: HTTPS/TLS/DNS fetching and an MCP stdio server | integration bugs possible (the suite already caught a Slack start-up crash that only a live run would have shown) |
| CI | workflow (incl. the integration job) not yet run on GitHub; only Python 3.11 verified locally | |
| Channels | text only: no attachments, images, voice, reactions, edits, streaming | limited media use |
| Channels | 5 platforms (others such as iMessage, Teams, Signal, Matrix, Google Chat are not built) | add via the `Channel` contract |
| Channels | WhatsApp 24 h window / templates not handled | late task results may fail |
| Interfaces | no TUI; no onboarding wizard / `doctor` command (a read-only web dashboard exists, see spec 18) | CLI + YAML + dashboard |
| Tasks | progress not pushed to chat while running; single process; the verifier sees only the final text; the autonomy features have only been run against a scripted model | users poll with `/tasks`; no HA |
| Scheduler | no per-job timezone (uses the global `scheduler.timezone`) | |
| Memory | vector scan is in-process (thousands of facts per user); no consolidation/decay of facts | use a vector DB backend for scale |
| Security | no rate limits or money budgets (token budgets exist, off by default); prompt-injection defences are mitigations, not guarantees | |
| Observability | logging only; no metrics, tracing export or cost tracking | |
| Storage | only `sqlite` and a non-persistent `memory` backend exist; no Postgres/DynamoDB backend yet | single node (the abstraction and contract tests are ready for one) |
| Packaging | no Dockerfile, systemd unit or published wheel | |
| Skills | no SKILL.md-style on-demand instruction packages or shared catalog | |
| Models | no auth-profile rotation or provider sign-in flows | static keys |
| Agents | no external coding-agent workers; no chat-driven agent creation (deliberate); channel settings still need a restart; inbound/`is_group` rules are unit-tested but not against the live Discord/Slack/Telegram services | |
| Devices | no browser control, voice, camera/screen or companion apps | |

## Candidate features (rough priority)

1. **Run the integration tests with real credentials and Docker** and fix what they find (the tests are written; see spec 17).
2. **Observability and budgets:** OpenTelemetry (Strands already emits spans), per-user token counters, rate limits, calibrated default token budgets.
3. **Packaging:** Dockerfile, systemd unit, published wheel, `kiro-crew doctor` and an onboarding wizard.
4. **Skills:** on-demand instruction packages loaded by name, with a local catalog.
5. **Channel breadth and media:** attachments/images, streaming or edited replies, more platforms.
6. **Browser tool** running inside the sandbox image.
7. **A Postgres (or DynamoDB) storage backend** to allow multiple instances; the protocols and contract tests already exist.
8. **Web dashboard** for config, sessions, tasks, schedules, pairing and memory.
9. **Deeper sub-tasks:** dependencies between tasks, nested fan-out, per-task tool permission scopes.
10. **Memory consolidation:** merge and decay facts, summarise clusters.

## Open questions

- Should long-term memory default to `shared` for single-owner deployments to avoid cross-channel identity setup?
- Should unauthorised senders on `allowlist` channels get a one-time hint containing their id (better UX, slight presence leak)?
- Which vector/KB backend should be the first-class option beyond the in-process hybrid store?
- Should approvals support "approve for this session" to cut prompt fatigue?
