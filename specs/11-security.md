# 11. Security

## Assets

LLM and platform credentials; the user's conversations and long-term memory; files in the agent
sandbox; the host (via tools); LLM spend.

## Trust boundaries

```
Internet/platforms ─▶ [channel adapter] ─▶ [gateway auth] ─▶ agent ─▶ tools / MCP / LLM API
                                                         └▶ SQLite + workspace on local disk
```

## Controls

| Id | Control | Where |
|---|---|---|
| SEC-1 | Deny-by-default allow-list for every non-local channel | `gateway._authorised` |
| SEC-2 | Unauthorised senders get no response (no bot-presence leak); the id is logged | gateway |
| SEC-3 | Group/server chat requires an explicit mention (Discord, Telegram) or thread (Slack) | adapters |
| SEC-4 | WhatsApp webhook: verify-token handshake and HMAC-SHA256 body signature, constant-time | `whatsapp_channel` |
| SEC-5 | Webhook retry de-duplication | `whatsapp_channel` |
| SEC-6 | File tools confined to `workspace/files`; `..`, absolute paths, escaping symlinks rejected | `Workspace.resolve` |
| SEC-7 | Secrets via `${ENV}`; `kiro-crew.yaml` git-ignored; secrets not logged by this project | config, `.gitignore` |
| SEC-8 | Per-user memory namespaces | `memory.longterm`, `gateway._user_key` |
| SEC-9 | FTS queries are tokenised and quoted, preventing operator injection | `longterm._fts_query` |
| SEC-10 | All SQL uses bound parameters; the only interpolated identifiers are fixed table names | `memory/`, `tasks/` |
| SEC-11 | Delegation depth cap; task agents cannot create tasks | factory |
| SEC-12 | Per-attempt task timeout and bounded retries limit runaway spend | `TaskRunner` |
| SEC-13 | Session ids are filesystem-safe (sanitised + hash) | `Address.key` |
| SEC-14 | Pairing: random unambiguous one-time codes, 1 h expiry, single use, capped queue, rate-limited replies, admin-only approval | `security/pairing.py` |
| SEC-15 | Human approval for sensitive tools; unanswered or unaddressable requests are denied (fail closed) | `security/approval.py` |
| SEC-16 | `web_fetch` SSRF guard: public addresses only, every redirect hop re-validated, size/time caps, content marked untrusted | `tools/web.py` |
| SEC-17 | Command sandbox: container with no network, no capabilities, read-only root, resource limits, only the files folder mounted; **fails closed** without Docker | `sandbox/` |
| SEC-18 | Scheduler limits: per-user schedule cap, 60 s minimum interval, owner-only cancel | `scheduler/` |
| SEC-19 | Admin commands restricted to `admins` / CLI | `gateway.py` |
| SEC-20 | Automatic retention of old conversations and finished tasks | `memory/maintenance.py` |
| SEC-21 | Web dashboard is read-only and token-gated; binds localhost by default; warns if enabled without a token | `web/__init__.py`, `core/config.py` |
| SEC-22 | **Logs hide message content by default** (text, tool args/results, commands become `<N chars hidden>`); opt in with `include_content` / `--log-content` | `core/logging.py` |
| SEC-23 | **Secrets are masked in every log line from any logger**: token shapes (OpenAI/Anthropic, AWS, Slack, Discord, Telegram, GitHub, JWT, Bearer), `key=value` credentials, tracebacks, and the exact secret values found in the config | `core/logging.py` |
| SEC-24 | Third-party loggers that print message bodies at DEBUG (Strands, httpx, openai, ...) are held at WARNING while content hiding is on | `core/logging.py` |
| SEC-25 | The dashboard's aiohttp access log is disabled (the request line carries `?token=`) | `web/__init__.py` |
| SEC-26 | **Agent isolation**: an `isolated` agent never receives the owner's `SOUL.md`/`MEMORY.md`, works in its own files folder and uses its own long-term memory namespace; it cannot be given the `core_memory` tool | `agents/factory.py`, `core/context.py`, `memory/factory.py` |
| SEC-27 | Agent switching is opt-in: only agents with `selectable: true` (narrowed by `agents_allowed`) can be chosen with `/agent`; admins may pick any | `routing.py`, `gateway.py` |
| SEC-28 | `/reload` is admin-only; a reload validates fully first and never applies a broken configuration | `agents/reload.py` |
| SEC-29 | The pid file is written only after the SIGHUP handler exists, and `kiro-crew agent reload` refuses to signal a pid that does not look like kiro-crew (a stale pid file could name a reused pid; SIGHUP kills a process that does not handle it) | `app.py`, `agents/reload.py` |
| SEC-30 | Agent names are validated (they become tool names) and drop-in file names are validated before use | `core/agentfiles.py` |

## Operator guidance

1. Put `allow_from` ids in config before exposing a bot; avoid `"*"`.
2. Run the process as an unprivileged user; restrict permissions on the workspace directory and
   `kiro-crew.db` (they contain conversations).
3. Grant agents the fewest tools. Do not give an agent that faces many users a shell, network-mutating
   or cloud-mutating tool.
4. Use least-privilege cloud credentials (for Bedrock: `bedrock:InvokeModel*` on the needed models).
5. Put the WhatsApp webhook behind HTTPS; keep `app_secret` and tokens in the environment.
6. Review third-party MCP servers and plugins as you would any dependency; they run with your privileges.

## Residual risks (not mitigated)

| Risk | Note |
|---|---|
| Prompt injection via tool results, fetched content or other users in shared chats | no content filtering; web content is labelled untrusted and risky tools can require approval, but a determined injection may still steer the model |
| DNS rebinding against `web_fetch` | resolution is checked before connecting but not pinned; also restrict egress at the network level |
| Approval prompt shows truncated arguments | the user must read what is displayed; approval covers the whole call |
| `sandbox.backend: host` or vended `shell` / `python_repl` tools | run unsandboxed on the host; do not enable for untrusted users |
| Pairing replies to strangers | reveals a bot exists on that account; use `allowlist` if that matters |
| Docker image supply chain | `sandbox.image` is pulled by tag; pin by digest for stricter control |
| `isolated` is not a sandbox | it protects the owner's private notes and files from that agent; it does not restrict the tools you grant it (web, shell, MCP), the network, or what the LLM provider sees |
| `selectable` widens who can use an agent's tools | any allowed user can switch to a selectable agent and get its capabilities; keep powerful agents non-selectable and give selectable ones only safe tools |
| Anyone who can write `workspace/agents/*.md` controls the bot | the folder is as sensitive as the config file: a new file can add an agent with tools and (via `delegate_of`) attach it to existing agents; protect it with filesystem permissions. Auto-reload applies edits within seconds |
| A shared (trusted) agent can read `files/agents/<name>/` | isolation is one-directional (owner-trusted agents are not restricted from isolated agents' folders) |
| Web dashboard exposure | read-only but shows conversation metadata and message text; keep on localhost or behind your own TLS/auth; set a token |
| Logs may contain message text and tool arguments | **only if you opt in** (`include_content`); by default content is hidden. When enabled, protect log files and the dashboard like the database. Secret scrubbing is best-effort pattern matching: an unusual credential format with no recognisable shape and not in the config can still appear |
| Poisoned long-term memory | extraction may store attacker-influenced text; review `memories` if exposed to untrusted input |
| Spend abuse by an allow-listed user | no per-user rate limits or token budgets |
| Shared-scope memory | `memory.scope: shared` lets every allowed user see all facts |
| Sensitive data at rest | SQLite and workspace files are unencrypted |
| Channel-level spoofing | trust in platform-provided sender ids (Slack, Discord, Telegram APIs) |
| Unbounded task text/progress in DB | progress capped at 50 notes; results unbounded |

## Acceptance criteria

- Unauthorised sender produces no reply *(verified)*.
- WhatsApp bad signature → 403, bad verify token → 403, replay ignored *(verified locally)*.
- Sandbox escapes rejected including symlinks *(verified)*.
- Cross-namespace memory isolation *(verified)*.
- Fault injection: disabling the SSRF guard, pairing expiry, the authorisation check or sandbox network isolation each makes the test suite fail *(verified)*.
- Agent isolation: a canary owner secret never reaches an isolated agent's prompt (default channel still gets it); isolated agents cannot read the owner's files, cannot climb out of their folder (also via `run_shell`), and have invisible-both-ways memory; fault injection of each protection fails tests *(verified)*.
- Real container isolation strength *(unverified: no Docker in the build environment)*.
