# 05. Gateway

Source: `gateway.py`, `app.py`.

The gateway is the only component that receives inbound messages and the only one that sends replies.

## Responsibilities

Authorise senders, map conversations to sessions, serialise turns per conversation, handle slash
commands, run the agent, and deliver chunked replies (including proactive messages from tasks).

## Inbound pipeline

1. **GW-1 Authorisation.** If the channel is `local`, allow. Otherwise allow if `allow_from` contains `"*"` or the
   sender id, **or** the sender has an approved pairing (spec 15). Otherwise: with `access: allowlist` the message is
   dropped silently and logged at WARNING with the sender id; with `access: pairing` the sender is issued a code.
1b. **GW-1b Approval answers.** `/approve` and `/deny` for a conversation with a pending approval are resolved
   *before* the turn lock (the waiting turn holds it).
2. **GW-2 Session key.** `Address.key` (channel + conversation + optional thread). Slack channel threads
   therefore get independent sessions; DMs do not.
3. **GW-3 Serialisation.** A per-session `asyncio.Lock` ensures one turn at a time; later messages wait
   in arrival order. Locks are held weakly and freed when idle. Different sessions run concurrently.
4. **GW-4 Commands.** Text beginning with `/` is matched against the command table; a match is answered
   without an LLM call. An unrecognised `/...` text is passed to the agent as ordinary input.
5. **GW-5 Typing.** While the agent runs, `channel.typing()` is called every 8 s (errors suppressed).
5b. **GW-5b Routing.** After authorisation the agent is chosen: conversation override (`/agent`) > first matching `routes` rule >
   channel `agent:` > `entry_agent` (spec 20). The choice is tagged on the turn and logged at DEBUG.
6. **GW-6 Agent.** Cached per *(session key, agent)* in an LRU of 64 agents (so switching agents never mixes them); on a miss the agent is rebuilt by the
   factory and its history is restored from the session store.
7. **GW-7 Reply.** The result text is split by `chunk_text(max_len)` and sent chunk by chunk.
8. **GW-8 Errors.** Any exception during a turn is logged with traceback and the user receives
   "Sorry, something went wrong handling that. Please try again." The lock is released.

## Identity and memory owner (GW-9)

`_user_key(msg)`:

1. `memory.scope == "shared"` → `"shared"`.
2. Else if `"<channel>:<sender_id>"` is listed under `identities.<person>` → `<person>`.
3. Else `"<channel>:<sender_id>"`.

The result becomes the agent's `user_id` and therefore the long-term memory namespace `user:<key>` and
the owner recorded on tasks.

## Commands

| Command | Behaviour |
|---|---|
| `/help` | prints the command list |
| `/new` | evicts the cached agent and deletes the conversation's session (history). Long-term memory, core memory, and tasks are untouched. A model override persists. |
| `/agent`, `/agents` | the agent in use (and why) and the agents the sender may switch to |
| `/agent <name>` | switch this conversation to a `selectable` agent (admins: any); persisted |
| `/agent reset` | back to default routing |
| `/reload` | admins only: reload agents from the config and `agents/` folder (spec 20) |
| `/model` | shows the current override or `<default> (default)` and the available profiles |
| `/model <name>` | validates the name; upserts `conversation_settings`; evicts the cached agent so the next turn rebuilds with that model and restored history |
| `/tasks` | lists up to 10 of the sender's tasks |
| `/cancel <id>` | cancels a queued/running task; usage message if no id or tasks disabled |
| `/schedules` | the sender's active reminders and scheduled jobs |
| `/unschedule <id>` | cancels one of the sender's schedules |
| `/memory` / `/memory list` | up to 20 newest stored facts with ids, and the total count |
| `/memory search <text>` | search the sender's stored facts |
| `/memory forget <id>` | delete one fact |
| `/memory clear confirm` | delete all of the sender's facts (without `confirm`, explains how) |
| `/approve`, `/deny` | answer a pending tool-approval request (spec 15) |
| `/pair [list\|approve <code>\|revoke <channel> <sender>]` | admins only |

Command names are case-insensitive.

## Outbound API (GW-10)

`Gateway.send(address, text)` is the public delivery function, also given to the `TaskRunner` as its
notifier. If the channel for `address.channel` is not running, a warning is logged and nothing is sent.

## Startup and shutdown (`App.run`)

- **GW-11** Startup order: build context → construct enabled channels → start channels → recover and
  start task workers.
- **GW-12** Shutdown triggers: SIGINT, SIGTERM, or any channel setting `quit`. Order: stop task workers
  (running tasks stay `running` for recovery) → stop channels → close database.
- **GW-13** Starting with no enabled channel exits with an explanatory message.

## Acceptance criteria *(verified with a scripted model and a fake channel unless noted)*

- Unauthorised sender gets no reply and a WARNING is logged.
- A conversation restarted in a new `App` instance on the same database sees prior history.
- `/new` clears every agent's history in the conversation (and stateful delegates); the `/agent` and `/model` choices are kept.
- `/new` clears history; `/model b` switches the next reply to model B.
- Two senders linked in `identities` share one memory namespace.
- Messages arriving during a running turn are processed afterwards in order *(by construction; not
  stress-tested)*.
- Pairing, `/memory` commands and approvals behave as specified in specs 06 and 15 *(verified)*.
- Real-platform delivery and typing indicators *(unverified)*.
