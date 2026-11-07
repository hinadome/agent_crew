# TEST_PROCEDURE.md

How to test kiro-crew, from "no accounts needed" to "everything live".

Every command below was run against this repository. Where a step could **not** be run (it needs your credentials, Docker, or a chat account) it is marked **[NOT RUN]** and you are the first to run it.

| Level | What | Needs | Time |
|---|---|---|---|
| 0 | Install and sanity check | Python 3.11+, uv | 2 min |
| 1 | Automated hermetic suite | nothing | ~25 s |
| 2 | Integration tests without credentials | local network | ~10 s |
| 3 | Integration tests with real services (opt-in) | your keys/Docker/bot tokens | varies [NOT RUN] |
| 4 | Manual smoke test with a **mock LLM** | nothing | 10 min |
| 5 | Manual feature scripts | Level 4 setup | 30 min |
| 6 | Real chat channels | bot accounts [NOT RUN] | varies |

Start with Levels 0, 1, 2 and 4. They need no API key.

## Level 0 - Setup

```bash
cd kiro_crew
uv sync --all-extras
uv run ruff check src tests      # expect: All checks passed!
uv run mypy                      # expect: Success: no issues found in 59 source files
```

## Level 1 - Hermetic suite

```bash
uv run pytest -q -rs
```
Expected: `493 passed, 30 deselected` (about 25 s). No network, no keys. The 30 deselected tests are the integration tests (Level 2/3).

Useful variants:
```bash
uv run pytest tests/test_channels_and_cli.py -q      # one file
uv run pytest -k approval -q                         # by keyword
uv run pytest -x -q                                  # stop at first failure
uv run pytest tests/test_autonomy.py tests/test_task_questions.py tests/test_task_state.py tests/test_subtasks.py -q   # autonomy features
uv run pytest tests/test_gaps.py -q                  # memory extraction, real provider builders, plugins, channel rules
```
A guard test (`test_repository_contains_no_secret_shaped_strings`) fails if any file in the repo contains something shaped like a real key. This protects `git push` from GitHub push protection.

## Level 2 - Integration tests without credentials

```bash
uv run pytest -m integration -q -rs
```
Expected: `10 passed, 20 skipped`. The 10 exercise the real web-fetch path (TLS, DNS pinning) and a real MCP server over stdio. The 20 skips list the env var each one needs.

## Level 3 - Integration tests with real services [NOT RUN]

These read credentials **only** from `KIRO_IT_*` variables. They never use your normal AWS or OpenAI config. Set only what you want to test; the rest skip.

| Area | Variables |
|---|---|
| LLM | `KIRO_IT_PROVIDER`, `KIRO_IT_MODEL_ID`, `KIRO_IT_API_KEY`, `KIRO_IT_BASE_URL`, `KIRO_IT_REGION`, `KIRO_IT_HOST` |
| Web search | `KIRO_IT_BRAVE_KEY` or `KIRO_IT_TAVILY_KEY` or `KIRO_IT_SEARXNG_URL` |
| Embeddings | `KIRO_IT_EMBED_PROVIDER`, `KIRO_IT_EMBED_MODEL`, `KIRO_IT_EMBED_API_KEY`, `KIRO_IT_EMBED_BASE_URL`, `KIRO_IT_EMBED_HOST`, `KIRO_IT_EMBED_REGION` |
| Telegram | `KIRO_IT_TELEGRAM_TOKEN` (+ `KIRO_IT_TELEGRAM_CHAT`) |
| Discord | `KIRO_IT_DISCORD_TOKEN` (+ `KIRO_IT_DISCORD_CHANNEL`) |
| Slack | `KIRO_IT_SLACK_BOT_TOKEN`, `KIRO_IT_SLACK_APP_TOKEN` (+ `KIRO_IT_SLACK_CHANNEL`) |
| WhatsApp | `KIRO_IT_WHATSAPP_TOKEN`, `KIRO_IT_WHATSAPP_PHONE_ID` |
| Docker sandbox | `KIRO_IT_DOCKER_IMAGE` (Docker daemon must run) |

Example (OpenAI-compatible endpoint):
```bash
export KIRO_IT_PROVIDER=openai KIRO_IT_MODEL_ID=gpt-4o-mini KIRO_IT_API_KEY=...   # your key
uv run pytest -m integration -q -rs
```
Pass criteria: the tests for the variables you set pass; the others show `SKIPPED` with the missing variable named.

Config generator check:
```bash
cd "$(mktemp -d)" && OPENAI_API_KEY=x uv run --project /path/to/kiro_crew kiro-crew init --provider openai \
  --base-url https://llm.example.com/v1 --model-id my-model && OPENAI_API_KEY=x uv run --project /path/to/kiro_crew kiro-crew check
```
Expect `Wrote kiro-crew.yaml (default LLM: openai)`, then `config OK`; the file has `default: gpt`, the URL and model on the `gpt`
profile, and `model: gpt` on the `coder` agent.

## Level 4 - Keyless smoke test with a mock LLM

The mock (Appendix A) is a small OpenAI-compatible server. It answers `[<model_id>] you said: <text>`. Writing `[[tool:NAME:{json}]]` in a message makes it call that tool, so tools can be tested deterministically.

```bash
mkdir -p /tmp/tp/ws && cd /tmp/tp
# save Appendix A as mock_llm.py and Appendix B as kiro-crew.yaml in /tmp/tp
python3 mock_llm.py &                       # needs aiohttp (installed by --all-extras; use `uv run python` from the repo if not)
K="uv run --project /path/to/kiro_crew kiro-crew -c /tmp/tp/kiro-crew.yaml"
$K check
$K agent list
```
Expected: `check` prints the config as OK with providers/tools/backends/channels. `agent list` prints a table:
```
NAME        SOURCE  MODEL           FLAGS                               REACHED FROM
assistant   yaml    mock (default)                                      entry_agent
researcher  yaml    mock (default)  selectable delegate-of:assistant    -
```

Interactive session: `$K chat`. Piped input also works, for scripting. Ctrl-D quits (running turns finish first).

## Level 5 - Feature scripts

Run inside `$K chat` unless stated. "Expect" lines are real outputs (times and ids differ).

### 5.1 Chat and models
| Type | Expect |
|---|---|
| `hello there` | `[mock-main] you said: hello there` |
| `/model` | `Current: mock (default)` and `Available: mock, mock2` |
| `/model mock2` then `hi` | `Switched this conversation to mock2.` then `[mock-alt] you said: hi` |
| `/help` | list of commands |

### 5.2 Tools
| Type | Expect |
|---|---|
| `[[tool:current_time:{}]]` | `[mock-main] tool finished: <ISO time>` |
| `[[tool:remember_core:{"note":"prefers tea"}]]` | `Saved to core memory.`; `/tmp/tp/ws/MEMORY.md` gains `- prefers tea` |

### 5.3 Long-running tasks
| Type | Expect |
|---|---|
| `[[tool:start_task:{"title":"demo job","goal":"say done"}]]` | `Started task <id>. The user will be notified on completion.` |
| `/tasks` | `[id] RUNNING - <title>` |
| (wait) | `Task [id] done: <title>` plus the result |
| `/cancel <id>` | task stops |
Tasks live in the DB and resume after a restart.

Completion criteria (hermetic tests cover the verifier; the mock LLM cannot judge): `[[tool:start_task:{"title":"demo job","goal":"say done","done_when":"says done"}]]` stores the criteria; with the mock as reviewer the result is delivered with `(Completion could not be verified.)` because the mock never returns the required JSON. Guard rails: `uv run pytest tests/test_autonomy.py -q` (26 tests: stuck loops, budgets, verification). With a real LLM [NOT RUN], give a task a criterion it cannot meet and expect two fix rounds, then a `WARNING - not confirmed complete` message.

Asking questions: covered by `uv run pytest tests/test_task_questions.py -q` (waiting without holding the worker, `/answer`, timeout default, owner-only, cancel, limits, retry prompts). The mock LLM cannot drive this manually, because its tool-marker syntax cannot nest a marker inside `start_task`'s arguments. With a real LLM [NOT RUN]: ask for something deliberately ambiguous as a background task; expect `Task [id] needs your input: ...`, `/tasks` showing `WAITING ... asks:`, then `/answer <id> <text>` -> `Thanks, the task continues.` and later `Task [id] done`. From a second terminal: `$K task list`, `$K task answer <id> <text>` (resumes within ~5 s). Set `tasks.ask_timeout_s: 20` to watch the timeout default.

Plans, sub-tasks and token budgets are covered by the hermetic suite (above). With a real LLM [NOT RUN]: give a multi-part goal
("compare Lisbon, Porto and Madrid for a weekend trip, one section each"). Expect `/tasks` to show `plan x/y` and, if it fans out,
three sub-tasks marked `sub-task of <id>`, then one final message from the parent. Set `limits.task_tokens: 20000` and repeat to see a
budget stop.

### 5.4 Reminders and schedules (verified)
```
[[tool:remind_me:{"when":"in 3s","text":"stretch now"}]]
/schedules
```
Expect `Scheduled. [id] once at ... | message: stretch now`, the `/schedules` row, and within seconds `Reminder: stretch now`. From the shell: `$K schedule list`, `$K schedule cancel <id>`. Restart mid-schedule: the job stays (at-least-once delivery, lease based).

### 5.5 Approvals (verified, fixed during writing of this document)
```
[[tool:run_shell:{"command":"echo APPROVED-AND-RAN"}]]
```
Expect `Approval needed: run run_shell with {...}  Reply /approve or /deny (auto-deny in 8s).` Then type `/approve` within 8 s:
`Approved.` followed by `tool finished: exit code: 0 --- stdout --- APPROVED-AND-RAN`.
Do nothing for 8 s: `No answer - action denied.` and the tool result says the user did not approve. `/deny` denies at once. A later `/approve` answers `Nothing is waiting for approval.`

Scripted form needs the pause, because piped lines arrive instantly:
```bash
(echo '[[tool:run_shell:{"command":"echo APPROVED-AND-RAN"}]]'; sleep 3; echo /approve; sleep 3) | $K chat
```
(Bug fixed: the CLI used to read input one line at a time, so `/approve` was never read while a turn waited. Regression test: `test_cli_channel_reads_input_while_a_turn_is_pending`.)
`sandbox.backend: host` logs a warning that commands run unsandboxed. Use `docker` for isolation [NOT RUN].

### 5.6 Memory
Shell: `$K memory list`, `$K memory search tea`, `$K memory forget <id>`, `$K memory clear --yes`, `$K memory reindex`. Add `--user U` / `--agent A` to scope. Without `--yes`, `clear` prints `refusing without --yes`. In chat: `/memory list|search Q|forget ID|clear confirm`. Fresh install prints `0 memories for shared`.
Auto-extraction (`memory.auto_extract: true`) needs a real LLM [NOT RUN]; the mock cannot extract facts.

### 5.7 Agents (verified)
| Action | Expect |
|---|---|
| `/agents` | assistant and `researcher - Looks things up.` |
| `/agent researcher` | `Switched this conversation to researcher.` |
| `/agent reset` | `Back to the default agent for this chat.` |
| `$K agent add scribe --template basic --description "Writes"` | `created .../ws/agents/scribe.md` and "A running bot picks it up within a few seconds" |
| `$K agent validate` | `OK: 3 agents (2 in YAML, 1 drop-in files)` |
| `$K agent list` | `scribe  file  ...` row |
| `$K agent remove scribe` | `removed .../scribe.md` |
Hot reload: keep `chat` running, add an agent from a second terminal; within seconds `/agents` shows it (only if `selectable`), or run `/reload` / `$K agent reload`. A broken agent file is rejected and the old set stays active; changes outside agents/routes/mcp_servers/entry_agent report "restart needed".
Delegation: `[[tool:ask_researcher:{"request":"look this up"}]]`.

### 5.8 Pairing
Set a chat channel to `access: pairing`. A stranger's first message gets a code. `$K pairing list`, `$K pairing approve CODE`, `$K pairing revoke CHANNEL ID`; in chat `/pair ...` as an admin. With nothing pending, `list` prints nothing and `approve ZZZZ` prints `unknown or expired code`. The full flow needs a real channel [NOT RUN].

### 5.9 Logging, redaction, dashboard (verified)
```bash
# add to a copy of the config:  web_ui: {enabled: true, host: 127.0.0.1, port: 8787, token: tok123}
SECRET="sk-""abcdefghijklmnopqrstuvwx"       # split so this file holds no key-shaped string
(echo "my password is hunter2 $SECRET"; sleep 5; echo hi; sleep 6) | $K -v --log-file /tmp/tp/app.log chat
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/api/state                 # 401 (no token)
curl -s "http://127.0.0.1:8787/api/state?token=tok123" | head -c 300                     # JSON: agents, routes, entry_agent
curl -s "http://127.0.0.1:8787/api/logs?token=tok123" | head -c 300                      # JSON log lines
grep -c "hunter2\|$SECRET" /tmp/tp/app.log                                               # expect 0
```
Expected: 401 without token, 200 with, and **0** matches: message content and secrets never reach logs unless you pass `--log-content`. Browser: open `http://127.0.0.1:8787/?token=tok123` (live stream via `/api/stream`). Other flags: `--log-level`, `--log AREA=LEVEL` (areas: gateway, agents, tasks, scheduler, memory, tools, channels, security, web_ui, strands), `--log-json`.

### 5.10 Web fetch
Needs the web tool enabled in config. Fetching `http://127.0.0.1/` or any private address must be refused (`allow_private: false`). Covered automatically by Level 2. A live search needs a key [NOT RUN].

### 5.11 Storage backends
Set `storage: memory` (or the SQL default) and re-run 5.1-5.6. The in-memory backend loses data on exit. The `hybrid` memory backend requires SQL storage.

## Level 6 - Real channels [NOT RUN]
For each platform: create a bot, put the token in env/config, set `allow_from` to your own id (empty means nobody is allowed), run `kiro-crew run`, then check:
- [ ] message from an allowed user gets a reply
- [ ] message from a non-allowed user is ignored and its id is logged
- [ ] long reply is split into chunks
- [ ] a group/channel message is handled per `is_group` rules
- [ ] `/approve` works from the chat
- [ ] a reminder arrives in the same chat
- [ ] restart keeps the conversation history

Platforms: Discord (bot token, message-content intent), Slack (bot token + app token, Socket Mode), Telegram (BotFather token, numeric user id), WhatsApp (Cloud API token, phone id, webhook). The detailed checklist is in `specs/17`.

## Negative and security checks
- [ ] Unlisted sender: no reply (Level 6)
- [ ] Private-address fetch refused (Level 2)
- [ ] `memory clear` without `--yes` refuses (5.6)
- [ ] Unapproved `run_shell` is denied after the timeout (5.5)
- [ ] Dashboard without token returns 401 (5.9)
- [ ] Secrets and content absent from logs (5.9)
- [ ] Isolated agents do not see another agent's core memory/files (covered by `uv run pytest -k isolat -q`)

## CI
`.github/workflows` runs ruff, mypy and the hermetic suite. It has **not** been run on GitHub yet; check the first run after pushing.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `Connection refused 8099` | start `mock_llm.py` |
| `/approve` says `Nothing is waiting for approval.` | it came too late (timeout) or before the request; pause between lines when scripting |
| Port 8787 busy | change `web_ui.port` |
| Integration test skipped | the `-rs` output names the missing variable |
| `git push` rejected for secrets | run Level 1; the guard test finds the file |
| Agent file ignored | `$K agent validate` prints the reason |

## Results log
| Level | Date | Pass/Fail | Notes |
|---|---|---|---|
| 0 | | | |
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |
| 6 | | | |

## Cleanup
```bash
pkill -f mock_llm.py; rm -rf /tmp/tp
```

## Appendix A - mock_llm.py
```python
"""A tiny OpenAI-compatible mock LLM for testing kiro-crew without any API key.

Run:   python mock_llm.py            (listens on http://127.0.0.1:8099/v1)
Chat:  replies "[<model>] you said: <your message>"
Tools: put a marker in your message and the mock "decides" to call that tool:
           [[tool:NAME:{"arg": "value"}]]
       e.g.   remind me [[tool:remind_me:{"when": "in 1m", "text": "stand up"}]]
Needs:  pip install aiohttp   (already installed with kiro-crew's channel extras)
"""
import json
import re
import sys

from aiohttp import web

MARKER = re.compile(r"\[\[tool:(\w+):(\{.*?\})\]\]")


def text_of(content):
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if isinstance(p, dict))


async def completions(req):
    body = await req.json()
    msgs = body["messages"]
    last = msgs[-1]
    user_texts = [text_of(m["content"]) for m in msgs if m["role"] == "user"]
    just_got_tool_result = last["role"] == "tool"
    marker = None if just_got_tool_result else MARKER.search(text_of(last["content"]) if last["role"] == "user" else "")
    model = body["model"]
    print(f"[mock] {model}: {user_texts[-1][:70]!r}" + ("  -> tool call" if marker else ""), flush=True)

    if marker:
        chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": marker.group(1), "arguments": marker.group(2)}}]}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
    else:
        reply = f"[{model}] you said: {MARKER.sub('', user_texts[-1]).strip()[:200]}"
        if just_got_tool_result:
            reply = f"[{model}] tool finished: {text_of(last['content'])[:160]}"
        chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
    chunks.append({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}})

    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(req)
    for c in chunks:
        c.update({"id": "mock", "object": "chat.completion.chunk", "created": 1, "model": model})
        await resp.write(f"data: {json.dumps(c)}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    return resp


app = web.Application()
app.add_routes([web.post("/v1/chat/completions", completions)])
if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    print(f"mock LLM on http://127.0.0.1:{port}/v1  (Ctrl-C to stop)", flush=True)
    web.run_app(app, host="127.0.0.1", port=port, print=None, access_log=None)
```

## Appendix B - kiro-crew.yaml for the mock
```yaml
workspace: /tmp/tp/ws
entry_agent: assistant
models:
  default: mock
  aux: mock
  profiles:
    mock:  {provider: openai, model_id: mock-main,  client: {api_key: "not-needed", base_url: "http://127.0.0.1:8099/v1"}}
    mock2: {provider: openai, model_id: mock-alt,   client: {api_key: "not-needed", base_url: "http://127.0.0.1:8099/v1"}}
memory: {auto_extract: false}
scheduler: {enabled: true, tick_s: 1}
agents:
  assistant:
    system_prompt: You are a helpful test assistant.
    tools: [time, tasks, schedule, files, core_memory, sandbox]
    require_approval: [run_shell]
    delegates: [researcher]
  researcher:
    description: Looks things up.
    selectable: true
    system_prompt: You are a researcher.
channels:
  cli: {enabled: true}
sandbox: {backend: host, timeout_s: 10}
approval_timeout_s: 8
```
