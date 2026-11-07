# Getting started with kiro-crew

A beginner's guide: what this is, how to run it in 10 minutes, and five real scenarios to try.

> Honesty note: the commands and the keyless path (section 3) were run. The scenarios in section 5 use a
> real LLM, which I have not run here because that needs your credentials. The prompts are examples; the
> model's wording will differ. Anything that depends on a real account is marked **(needs your account)**.

## 1. What is it?

kiro-crew is a personal AI assistant that lives where you chat (terminal, Discord, Slack, Telegram, WhatsApp).
Think of it as a small team ("crew"):

| Word | Meaning |
|---|---|
| Agent | One AI role: a prompt + a model + some tools. Example: `main` (lead), `researcher`, `coder`. |
| Crew | Agents that work together. The lead (`entry_agent`) can hand work to specialists ("delegates"). |
| Tool | Something an agent can do: tell time, read/write files, fetch web pages, run code, set reminders. |
| Channel | Where you talk to it: `cli` (terminal), Discord, Slack, Telegram, WhatsApp. |
| Memory | What it remembers: chat history, long-term facts about you, and two files you can edit (`SOUL.md` = its personality, `MEMORY.md` = standing facts). |
| Task | A long job that runs in the background so the chat stays free. |
| Schedule | A reminder or recurring job ("every weekday at 8"). |
| Workspace | A folder (default `~/.kiro-crew`) holding the files, memory and database. |

You do not need to write code. You edit one YAML file and chat.

## 2. Install (5 minutes)

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
cd kiro_crew
uv sync --all-extras
uv run kiro-crew init --provider openai      # writes ./kiro-crew.yaml; pick: bedrock (default), openai, anthropic, ollama, litellm
```

## 3. Pick an LLM, then run

The brain is an LLM. Choose ONE of these. In `kiro-crew.yaml`, set `models.default` to the profile name.

| Option | Cost | What to do |
|---|---|---|
| A. Amazon Bedrock (the sample default, profile `claude`) | AWS usage | `aws login` or set AWS credentials; check Bedrock model access in your region **(needs your account)** |
| B. OpenAI (`gpt`), or any OpenAI-compatible URL (see below) | API usage | `export OPENAI_API_KEY=...` and set `models.default: gpt` **(needs your account)** |
| C. Anthropic (`claude-direct`) | API usage | `export ANTHROPIC_API_KEY=...` and set `models.default: claude-direct` **(needs your account)** |
| D. Ollama, local (`local`) | free | install Ollama, `ollama pull llama3.2`, set `models.default: local` |
| E. No LLM at all | free | the mock server in [TEST_PROCEDURE.md](TEST_PROCEDURE.md) (Level 4). Good for learning the commands, not for smart answers. |

### Using an OpenAI-compatible server at your own URL

Works for vLLM, LM Studio, a LiteLLM proxy, OpenRouter, a company gateway, or anything that speaks the OpenAI
chat-completions API. Use `provider: openai` and set `client.base_url`:

```yaml
models:
  default: mygw
  profiles:
    mygw:
      provider: openai
      model_id: my-model-name                  # exactly the name your server expects
      client:
        base_url: "https://llm.example.com/v1" # use the path your server documents (usually ends in /v1)
        api_key: "${MYGW_API_KEY}"             # env var; use "not-needed" if the server has no auth
      params: {temperature: 0.3}               # optional
```

Or let `init` write it: `uv run kiro-crew init --provider openai --base-url https://llm.example.com/v1 --model-id my-model-name`
(then `export OPENAI_API_KEY=...`, any value if the server has no auth).

```bash
export MYGW_API_KEY=...
uv run kiro-crew check
uv run kiro-crew chat
```

- Everything under `client:` is passed to the OpenAI client, so options such as `default_headers` (custom auth
  header) or `timeout` also go there.
- kiro-crew does not add `/v1` for you. A 404 usually means a wrong path.
- Several servers: one profile each, switch with `/model NAME`; `models.fallback: [other]` gives automatic failover.
- Change any agent that still says `model: claude` (the sample `coder`), or it will still try Bedrock.
- `memory.backend: hybrid` has its own embedder URL under `memory.options.embedder` (`base_url`); your server must
  support `/embeddings`.
- Agents with tools need a server and model that support OpenAI-style tool calling. Plain chat does not.
- Verified only against a local mock server (including tool calls), not your real endpoint. If chat fails, run
  `uv run kiro-crew -v chat` and read the error line.

`init --provider` sets your choice as the default for every agent, including the sample `coder` agent (which otherwise
points at Bedrock). If you edit the file by hand instead, change `coder`'s `model:` line too.

Then:

```bash
uv run kiro-crew check     # validates the config; fix anything it reports
uv run kiro-crew chat      # talk in the terminal; Ctrl-D quits
```

First try: type `hello, what can you do?`. Type `/help` for the commands.

### Chat commands you will use

| Command | What it does |
|---|---|
| `/help` | list commands |
| `/new` | start a fresh conversation |
| `/model` , `/model NAME` | show / switch the LLM for this chat |
| `/agents` , `/agent NAME` , `/agent reset` | list / switch / return to the default agent |
| `/tasks` , `/cancel ID` | background tasks |
| `/answer ID text` | answer a question a waiting background task asked you |
| `/schedules` , `/unschedule ID` | reminders and recurring jobs |
| `/memory` , `/memory search Q` , `/memory forget ID` | see or delete what it remembers |
| `/approve` , `/deny` | answer a "may I run this?" question |

## 4. Where things live

```
./kiro-crew.yaml            your config (git-ignored; keep secrets in env vars, use ${VAR})
~/.kiro-crew/               the workspace (change with `workspace:` in the config)
  SOUL.md                   personality and rules. Edit freely.
  MEMORY.md                 standing facts it must always know. Edit freely.
  files/                    the only folder agents may read/write
  agents/*.md               drop-in agents (section 5.5)
  HEARTBEAT.md              checklist for the optional heartbeat
  (database)                chat history, tasks, schedules, memory
```

## 5. Real scenarios

Each scenario: goal, setup, what to type, what should happen, how to check.

### 5.1 Personal assistant that remembers you

Goal: it knows your preferences next week.

Type:
```
Remember that I prefer short answers, I work in the Berlin timezone, and my manager is Sam.
```
Expected: it confirms it saved this (core memory tool or long-term memory).
Check:
```bash
cat ~/.kiro-crew/MEMORY.md          # standing facts, if it used core memory
uv run kiro-crew memory list        # long-term facts extracted from chats
```
Then `/new` and ask `Who is my manager?`. It should answer from memory.
Fix a wrong fact: `/memory list`, then `/memory forget <id>`.
Tip: set `scheduler.timezone: Europe/Berlin` so "at 9" means 9 in your time.

### 5.2 Reminders and recurring jobs

Type:
```
Remind me in 10 minutes to stretch.
Every weekday at 08:30, give me a short list of what I should focus on today.
```
Expected: it replies with a confirmation like `Scheduled. [id] ...`. The first becomes a plain message
at the time. The second runs the AI each weekday and sends the result.
Check: `/schedules` in chat, or `uv run kiro-crew schedule list`. Remove with `/unschedule ID`.
Note: `kiro-crew chat` only fires reminders while it is running. For always-on use run `kiro-crew run` (section 6).

### 5.3 A long job in the background

Type:
```
Research the pros and cons of SQLite vs PostgreSQL for a small web app. Take your time and write the result to a file called db-notes.md.
```
Expected: the lead agent starts a background task ("Started task ...") and the chat stays usable. When it finishes
you get `Task [id] done: ...`.
Check: `/tasks`; the output file appears in `~/.kiro-crew/files/db-notes.md`. Cancel with `/cancel ID`.
Tip: tell it what "done" means, for example `... Done when the file lists at least 3 sources.` The lead agent passes this as completion criteria; when the task ends, a reviewer checks the result and sends the agent back to fix gaps (twice at most), and you get a `WARNING` if it still falls short. A stuck agent (repeating the same call, or using far too many tool calls) is stopped; see `limits:` in the config.
If the task hits a real fork it may pause and ask: `Task [id] needs your input: Which dates?`. The task shows as `WAITING` in `/tasks` and uses no worker. Reply `/answer <id> 12-15 December` (or `uv run kiro-crew task answer <id> 12-15 December` from a shell). No reply within 30 minutes: it carries on with the default assumption it told you. It asks at most 3 times (`tasks.max_questions`; `tasks.ask_user: false` turns asking off). If an attempt fails, the retry is told what failed and to try a different approach.
For bigger jobs the agent writes a plan and keeps notes; `/tasks` shows progress like `plan 2/5`, and the plan survives restarts. If the
work splits into independent parts (say, one per city), it can run them as parallel sub-tasks and then combine the results;
you only get the final message.
The `researcher` agent needs the `web` tool (already in the sample config) to browse. Add a search key to enable
search, not just page fetching: set `tool_options.web.search: brave` and `api_key` **(needs your account)**.

### 5.4 Safe code running with approval

Goal: let the coder run code, but only after you say yes.

The sample `coder` has the `sandbox` tool and `require_approval: [run_shell, run_python]`. The sandbox runs code
in Docker with no network. Without Docker it refuses (it never falls back to your machine).

1. Install Docker and start it (or, for a throwaway test ONLY, set `sandbox.backend: host`, which is unsandboxed).
2. Type: `Ask the coder to write and run a Python snippet that prints the first 10 squares.`
3. Expected: `Approval needed: run run_python with {...}  Reply /approve or /deny`.
4. Type `/approve` (within `approval_timeout_s`, default 300 s). You get the output. `/deny` refuses.
Check: nothing runs until you approve; if you wait too long it says `No answer - action denied.`
Verified with the mock LLM: approve gives `Approved.` and the command output; timeout gives a denial.

### 5.5 Add your own specialist in 30 seconds

Goal: an agent that only does one job.

```bash
uv run kiro-crew agent add trip-planner --template basic \
  --description "Plans trips" --delegate-of main \
  --prompt "You plan short city trips. Give a 3-day itinerary with a budget."
uv run kiro-crew agent validate      # expect: OK: ... agents
uv run kiro-crew agent list
```
If `chat` or `run` is already running, it picks the agent up within a few seconds (or type `/reload`).
The lead agent now has an `ask_trip_planner` tool, so `Plan a weekend in Lisbon` can be handed to it.
Make it directly selectable by users: add `--selectable`, then `/agent trip-planner` in chat and `/agent reset` to go back.
Remove it: `uv run kiro-crew agent remove trip-planner`.
Files are plain markdown in `~/.kiro-crew/agents/`, so you can also write them by hand (format in
[README.md](README.md#agents-add-isolate-route-switch)). A broken file is rejected and the old agents keep running.

### 5.6 A safe public-facing helper

Goal: a support bot others can talk to without seeing your private notes.

```bash
uv run kiro-crew agent add faq --template public --prompt "You answer questions about our product politely and briefly." --selectable
```
The `public` template is `isolated`: it never sees your `SOUL.md` / `MEMORY.md` and gets its own files and memory.
To make a channel use it, add a route in the config (example for a Discord server):
```yaml
routes:
  - {agent: faq, channel: discord, group: true}
```
Remember: isolation protects your notes, not tools. Only give it tools you are happy for strangers to trigger.

## 6. Put it on Discord / Slack / Telegram / WhatsApp  (needs your account)

Run `uv run kiro-crew run` to start every channel with `enabled: true`. Keep it running (tmux, systemd, a container).
Secrets go in environment variables, never in the file.

Deny by default: `allow_from` must list who may talk to the bot. Empty = nobody. (If someone unknown messages the
bot, their id appears in the log, so you can copy it.)

Easiest first: Telegram (no extra install).
1. In Telegram, talk to @BotFather, create a bot, copy the token.
2. `export TELEGRAM_BOT_TOKEN=...`
3. Config: `channels.telegram: {enabled: true, allow_from: ["<your numeric id>"]}` (message the bot once, run with
   `-v`, read your id from the "unauthorised sender" log line if you do not know it).
4. `uv run kiro-crew run -v`, then message your bot.

Other channels: Discord needs `uv sync --extra discord` and the Message Content intent; Slack needs
`--extra slack` with a bot token and an app token (Socket Mode, no public URL); WhatsApp needs `--extra whatsapp`
and a public HTTPS URL for the webhook. Details: [README.md](README.md) and `specs/`.

Let strangers ask for access instead of editing the list: set `access: pairing` on the channel. They get a code;
you run `uv run kiro-crew pairing approve CODE`. `uv run kiro-crew pairing list` shows pending codes.

## 7. See what it is doing

```bash
uv run kiro-crew -v chat                          # detailed logs
uv run kiro-crew --log tools=debug --log agents=debug run
```
(Use `--log AREA=LEVEL`; areas: gateway, agents, tasks, scheduler, memory, tools, channels, security, web_ui.)
Your message text is hidden in logs and secrets are masked, unless you pass `--log-content`.

### Web dashboard

A read-only page in your browser: live logs (streamed as they happen), current tasks, schedules, pairing
requests, and the agents/routes in use. It cannot change anything.

**1. Turn it on** in `kiro-crew.yaml` (it is off by default):
```yaml
web_ui:
  enabled: true
  host: 127.0.0.1        # this machine only (keep it this way)
  port: 8787
  token: "${KIRO_WEB_TOKEN:-}"   # the password for the page
```
Pick a long random token and keep it out of the file:
```bash
export KIRO_WEB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
echo "$KIRO_WEB_TOKEN"            # you need it for the URL
```
(Or write the token directly in the file instead of `${...}`. If the token is empty the dashboard has NO password
and logs a warning `UNAUTHENTICATED`; only do that on a machine only you use.)

**2. Start the app.** The dashboard runs inside the app, so it is up whenever the app is, with either
`uv run kiro-crew chat` or `uv run kiro-crew run`. Look for the startup line `Web UI on http://127.0.0.1:8787`.
It stops when the app stops. Use `kiro-crew run` for an always-on dashboard.

**3. Open it** in a browser on the same machine:
```
http://127.0.0.1:8787/?token=<your token>
```
The page shows a connection status, a live log list (follow mode; filter by level or text), and tasks,
schedules and pairing requests. Wrong or missing token: the page itself still loads (it is only a layout), but it shows `unauthorised` and no data, because the data endpoints (`/api/...`) return HTTP 401. Test the password with `/api/state`, not `/`.
For more detail in the log view, start with `-v`, or `--log AREA=debug` (section above).

**4. Quick check from the terminal** (no browser needed):
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/api/state                       # 401 = password required; 200 = NO token is set (check $KIRO_WEB_TOKEN)
curl -s "http://127.0.0.1:8787/api/state?token=$KIRO_WEB_TOKEN" | head -c 300                  # JSON
curl -s "http://127.0.0.1:8787/api/logs?token=$KIRO_WEB_TOKEN"  | head -c 300                  # recent log lines
```
You can also send the token as a header, `X-Auth-Token: <token>`, which keeps it out of URLs and shell history.

**Using it from another computer.** Do not change `host` to `0.0.0.0`: the page carries conversation
metadata and the token travels in plain HTTP. Use an SSH tunnel instead:
```bash
ssh -L 8787:127.0.0.1:8787 user@the-server       # then open http://127.0.0.1:8787/?token=... locally
```

**What the logs show.** Message text and tool arguments are hidden and secrets are masked, also in the
dashboard, unless you passed `--log-content`. Tokens in the URL are never written to logs.

Dashboard problems: see the troubleshooting table below (port busy, 401, blank page).

## 8. Troubleshooting

| Problem | Likely cause / fix |
|---|---|
| Errors about AWS / credentials / model access | You are on the Bedrock default. Switch `models.default`, or run `aws login` and enable the model in your region. Also check the `coder` agent's `model:` line. |
| `check` reports a config error | Read the message; it names the field. |
| Bot ignores you on Discord/Slack/etc. | Your id is not in `allow_from`. Run with `-v` and look for the unauthorised-sender line. |
| Dashboard says `unauthorised` / 401 | Token in the URL does not match `web_ui.token`; check `echo $KIRO_WEB_TOKEN` in the same shell that started the app. |
| Dashboard page does not load | `web_ui.enabled` is false, the app is not running, or the port is in use (change `web_ui.port`). Look for `Web UI on http://...` in the startup output. |
| Reminder never arrived | The process was not running. Use `kiro-crew run` for always-on. |
| "Nothing is waiting for approval" | The approval timed out, or you typed it before the request appeared. |
| Code tools refuse to run | Docker is not running (by design). Start Docker. |
| Answers are poor | The model is the limit. Try a stronger profile with `/model NAME`. |

## 9. Safe-use checklist

- [ ] Secrets only in environment variables
- [ ] `allow_from` lists only people you trust
- [ ] Code-running agents keep `require_approval` and use the Docker sandbox
- [ ] Public-facing agents are `isolated: true` and have few tools
- [ ] Dashboard keeps `host: 127.0.0.1` and a token
- [ ] Optional: set `limits.task_tokens` (and `chat_tokens`) once you know what a typical task uses (`/tasks` shows tokens per task)

## 10. Where next

- [TEST_PROCEDURE.md](TEST_PROCEDURE.md): verify everything works, including a no-API-key mock
- [README.md](README.md): full feature reference
- [ARCHITECTURE.md](ARCHITECTURE.md): how it fits together
- `specs/`: detailed design per feature
