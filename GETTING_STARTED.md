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
uv run kiro-crew init      # writes ./kiro-crew.yaml (a sample config)
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

Important: the sample config pins the `coder` agent to `model: claude` (Bedrock). If you are not using Bedrock,
change it to your profile name (or delete that line), or `coder` will try Bedrock.

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

Web dashboard (live logs, tasks, schedules): add to the config
```yaml
web_ui: {enabled: true, token: "pick-a-long-random-string"}
```
then open `http://127.0.0.1:8787/?token=pick-a-long-random-string` while `kiro-crew run` is running.

## 8. Troubleshooting

| Problem | Likely cause / fix |
|---|---|
| Errors about AWS / credentials / model access | You are on the Bedrock default. Switch `models.default`, or run `aws login` and enable the model in your region. Also check the `coder` agent's `model:` line. |
| `check` reports a config error | Read the message; it names the field. |
| Bot ignores you on Discord/Slack/etc. | Your id is not in `allow_from`. Run with `-v` and look for the unauthorised-sender line. |
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

## 10. Where next

- [TEST_PROCEDURE.md](TEST_PROCEDURE.md): verify everything works, including a no-API-key mock
- [README.md](README.md): full feature reference
- [ARCHITECTURE.md](ARCHITECTURE.md): how it fits together
- `specs/`: detailed design per feature
