# 17. Testing and CI

## Running

```bash
uv sync --all-extras                 # all extras so no adapter test is skipped
uv run ruff check src tests
uv run mypy                          # type-checks src/kiro_crew (clean on all source files)
uv run pytest -q -rs                 # hermetic: ~17 s, no network, keys or Docker
uv run pytest -m integration -rs     # real infrastructure; every skip prints its reason
```

## Design

- **No external services.** A scripted fake LLM (`ScriptedModel`) and a fake chat channel (`FakeChannel`) in
  `tests/conftest.py` drive the real gateway, agent factory, memory, task and scheduler code.
- The fake model can **call a tool on demand**: a prompt containing `[[tool:NAME:{json}]]` makes it emit that tool call
  once, which exercises real Strands tool execution, hooks and state.
- HTTP behaviour (web fetch, search backends, embeddings) is tested with `httpx.MockTransport`; the WhatsApp webhook is
  tested against a real local aiohttp server.
- Databases are temporary SQLite files/in-memory; time-dependent paths use real short timeouts (largest ~1 s).

## Coverage by area

| File | Covers |
|---|---|
| `test_gateway.py` | authorisation, chunking, history restore, `/new`, `/model`, identities, serialisation, pairing, `/memory`, approvals |
| `test_config_and_providers.py` | shipped config, env expansion, validation errors, registry, router and failover |
| `test_tasks.py` | lifecycle, recovery, retry/backoff, timeout, cancel, progress, quiet results |
| `test_scheduler.py` | expression parsing, claiming, missed jobs, agent tools, heartbeat |
| `test_memory.py` | keyword and hybrid stores, embedding outage, reindex, retention, DB migration, workspace |
| `test_tools.py` | file sandbox, tool resolution, SSRF, fetch, search, command sandbox |
| `test_channels_and_cli.py` | chunking, adapter construction, WhatsApp webhook, admin CLI |
| `test_logging.py` | levels, per-area overrides, turn context, reserved-name guard, JSON/file sinks, ring buffer |
| `test_web_ui.py` | token auth, index, logs/state APIs, live SSE stream |
| `test_redaction.py` | secret shapes, config secrets, content hiding, third-party cap, tracebacks, end-to-end canary leak test |
| `test_storage_contract.py` | the behavioural contract, run against **both** storage backends (+ both fact stores) |
| `test_memory_storage_e2e.py` | the whole app on the non-persistent `memory` backend, no SQLite |
| `test_agent_isolation.py` | name validation, isolated agents' prompt/files/memory, end-to-end canary of the owner's secret, delegate/task inheritance |
| `test_agent_files.py` | drop-in parsing and merging, `delegate_of`, prompt files, cross-reference validation, cycle warnings, `agent add/remove/validate` helpers |
| `test_routing.py` | routing precedence and matching, `/agent` list/switch/reset/refusals, per-agent history, restart persistence, stale overrides |
| `test_reload.py` | add/change/remove, five invalid-edit cases, refusals, restart-needed reporting, watcher (incl. a regression for a race it found), real SIGHUP, pidfile safety |
| `test_delegates.py` | stateless vs stateful delegates, persistence, serialisation, `/new`, nesting cap, hermetic MCP pool |
| `integration/` | real infrastructure (below) |

## Properties checked on purpose

- **Security regressions are caught.** Disabling the SSRF guard, the pairing-code expiry, the authorisation check, or
  the sandbox network isolation each makes at least one test fail (verified by fault injection during development).
- **Upgrades.** A v0.1-schema database is opened by the current code and keeps working (migration test).
- **Stability.** The suite was run repeatedly with identical results.
- **Test helpers can lie.** The first version of the stateful-delegate tests passed vacuously (the helper matched the *parent's* model calls, whose history naturally contains the earlier request). Fault-injecting the feature exposed it; the helper now selects only the delegate's own calls, and breaking `stateful` fails four tests.
- **Reliability fixes are pinned by tests.** Scheduler firing is at-least-once via a lease (a simulated crash re-claims
  the job; an immediate-retry path exists); `web_fetch` pins the connection to the vetted IP (a resolver that would flip
  to a private address is proven never to be dialed). Disabling either makes tests fail (fault injection).

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests and manual dispatch, for Python 3.11, 3.12 and 3.13:
frozen install with all extras → ruff → **mypy** → pytest → `kiro-crew init && kiro-crew check`. A separate `integration` job
(not on pull requests, so forks never see secrets) runs `pytest -m integration` on the runner, which has network and Docker;
provider/channel tests run only if you add the matching `KIRO_IT_*` repository secrets.
*The workflow file has not been executed on GitHub; it mirrors the local commands that pass. Local runs used Python 3.11 only; 3.12 and 3.13 are in the matrix but not yet verified.*

## Integration tests (`tests/integration`, marker `integration`)

Deselected by default (`addopts = -m 'not integration'`). They use real sockets, TLS, DNS, subprocesses and (if you provide
them) real services. Credentials come **only** from `KIRO_IT_*` variables, never from your normal AWS/OpenAI/... config, so
the suite cannot spend money or post messages by surprise. Each test skips with the exact variables it needs.

| Module | Needs | What it proves |
|---|---|---|
| `test_web_real.py` | outbound HTTPS | real fetch through the IP-pinned transport (DNS -> vetted IP -> TLS with the right SNI); **certificate verification still rejects expired / wrong-host / self-signed certs under pinning**; real `localhost` and a public name resolving to 127.0.0.1 are blocked; with real sockets the connection goes to the pinned IP and the `Host` header is preserved |
| `test_mcp_real.py` | nothing (spawns a local MCP server) | a real MCP stdio server is discovered and called through the app's agent factory; **delegate calls and different agent instances share one pooled server process** (before pooling: 5 calls = 5 processes), kept alive between calls and stopped on `close()` |
| `test_docker_real.py` | Docker daemon | no network, read-only root, `/work` shared with the host, runs as the host uid, all capabilities dropped, timeout kills the container (no orphan), tool end to end |
| `test_providers_real.py` | `KIRO_IT_PROVIDER`, `KIRO_IT_MODEL_ID` (+key/region) | a one-turn reply, a real tool call, the gateway end to end, and failover from a broken profile to a real one |
| `test_search_embeddings_real.py` | search key / embedding provider | real Brave/Tavily/SearXNG results; embeddings are semantically sensible; hybrid memory finds a paraphrase |
| `test_channels_live.py` | bot tokens | Telegram/Discord/Slack authenticate and connect (optionally send one message); WhatsApp token and phone id valid (read-only) |

Integration tests found something the mocks could not: removing the TLS SNI hint from the pinned transport breaks every
real HTTPS fetch, while the mock-transport test only checked that the hint was *present*.

### Manual checklist (needs a human; no automation can DM your bot)

For each channel you enable: (1) DM the bot from an allow-listed account and get a reply; (2) message from a non-listed
account and confirm silence (or a pairing code if `access: pairing`); (3) in a group/server, confirm it only answers when
mentioned; (4) send a message longer than the channel limit and confirm it arrives in chunks; (5) `/model`, `/new`, `/tasks`;
(6) start a background task and confirm the completion message arrives in the same chat/thread; (7) set a reminder and
confirm delivery; (8) for WhatsApp, confirm webhook verification in the Meta console and a signed delivery.

## Not covered

- Verified against real infrastructure **in the build environment**: HTTPS/TLS/DNS, a real MCP server. Written and
  gated but **not yet run** here: Docker, real LLM providers, real search/embedding APIs, live chat platforms (these
  need Docker or your credentials; see the table above).
- Load/concurrency stress beyond a handful of parallel turns.

Add these as opt-in integration tests (skipped unless credentials/Docker are present) when you have accounts.
