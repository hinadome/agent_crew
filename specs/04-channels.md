# 04. Channels

Source: `channels/`.

## Contract

- **CHN-1** A channel subclasses `Channel` and implements `start(on_message)`, `stop()`, `send(msg)`,
  and optionally `typing(address)`. It is registered under a key in `channel_registry`.
- **CHN-2** A channel only translates between platform events and `InboundMessage` /
  `OutboundMessage`. It performs no auth, session, agent or memory logic (those belong to the gateway).
- **CHN-3** `start` MUST return promptly; long-running receive loops run as background tasks.
- **CHN-4** A channel reports a request to shut the process down by setting `self.quit` (an
  `asyncio.Event`). Used by the CLI on EOF.
- **CHN-5** Replies are split to `max_len` by the gateway before `send`; `chunk_text` prefers
  paragraph, then line, then word boundaries, and never emits an empty message.
- **CHN-6** Channel adapter modules are imported lazily, so a channel's third-party dependency is only
  required when that channel is enabled.
- **CHN-6b** Adapters MUST NOT create event-loop-bound objects (aiohttp sessions etc.) in `__init__`: the app constructs channels *before* the loop starts (`asyncio.run(app.run())`). Create them in `start()`. (Slack previously violated this; found by the test suite and fixed.)
- **CHN-6c** All adapters validate required credentials at construction, so a bad config fails at startup.
- **CHN-7** `Channel.local = True` marks a trusted channel that bypasses the allow-list (CLI only).

## `InboundMessage` / `OutboundMessage`

`InboundMessage(address, user_id, text, user_name?, attachments?, raw?, is_group=False)`; `OutboundMessage(address,
text, reply_to?)`. `Address(channel, conversation_id, thread_id?)` has a `key` property producing a
stable, filesystem-safe session key (sanitised prefix + hash suffix).

## Adapters

### CLI (`cli`)
- **CHN-7b** Each adapter sets `is_group` (Discord: server message; Slack: not an IM; Telegram: chat type not private; WhatsApp and CLI: never), which `routes` use to tell groups from direct messages (spec 20).
- **CHN-8** Trusted local console (`local = True`, `max_len = 100_000`). Reads stdin on a thread, feeds
  each non-empty line as an `InboundMessage` from `cli/local`, prints replies. EOF sets `quit`.

### Discord (`discord`)
- Requires `token`; needs the *Message Content* privileged intent.
- **CHN-9** Replies in DMs always; in servers only when the bot is mentioned. The mention is stripped.
- `max_len = 1900`.

### Slack (`slack`)
- Requires `bot_token` (`xoxb-`) and `app_token` (`xapp-`); uses Socket Mode (no public URL).
- **CHN-10** The Slack client/app is created in `start()` (see CHN-6b). Handles `message.im` (DMs) and `app_mention`. Channel replies go in a thread
  (`thread_ts`); DMs are flat. Bot and subtype messages are ignored; mentions are stripped.
- `max_len = 3500`.

### Telegram (`telegram`)
- Requires `token`; uses httpx long-polling (`getUpdates`), no extra dependency.
- **CHN-11** Replies in private chats always; in groups only when `@mentioned` or when replying to the
  bot. Bot authors are ignored; the mention is stripped. Poll errors back off ~5 s.
- `max_len = 4000`.

### WhatsApp (`whatsapp`)
- Meta WhatsApp Cloud API webhook. Requires `access_token`, `phone_number_id`, `verify_token`,
  `app_secret`; optional `host` (`0.0.0.0`), `port` (`8080`), `path` (`/whatsapp`).
- **CHN-12** `GET` verifies the subscription using `verify_token` (constant-time compare); mismatch → 403.
- **CHN-13** `POST` verifies `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with `app_secret`,
  constant-time compare); mismatch → 403.
- **CHN-14** Text messages are de-duplicated by message id (window of 2000) because Meta retries.
- **CHN-15** Only `type == "text"` messages are handled; the webhook acks fast and processes in the
  background. `allow_from` entries are phone numbers without `+`.
- `max_len = 3800`. Free-form replies only within 24 h of the user's last message (platform rule).

## Message length summary

| Channel | `max_len` | Platform hard cap |
|---|---|---|
| cli | 100000 | — |
| discord | 1900 | 2000 |
| slack | 3500 | ~40000 (kept conservative) |
| telegram | 4000 | 4096 |
| whatsapp | 3800 | 4096 |

## Acceptance criteria

- `chunk_text` never exceeds `max_len` and never yields empty output *(verified)*.
- Enabling a channel without its dependency fails only when that channel is constructed *(by design)*.
- WhatsApp: GET verify passes/fails by token; POST rejects bad signatures; duplicate deliveries are
  processed once *(verified against a local server)*.
- Discord/Slack/Telegram adapters construct from config *(verified)*; live platform behaviour *(unverified)*.
