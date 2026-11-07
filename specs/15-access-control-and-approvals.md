# 15. Access control: pairing and approvals

Source: `security/pairing.py`, `security/approval.py`, `gateway.py`.

## Who may talk to the bot

Authorisation (`Gateway._authorised`), evaluated for every inbound message on a non-local channel:

1. `allow_from` contains `"*"` or the sender id → allowed.
2. The sender has an **approved pairing** for that channel → allowed.
3. Otherwise the sender is unauthorised and handled according to `channels.<name>.access`.

| `access` | Behaviour for unauthorised senders |
|---|---|
| `allowlist` (default) | message dropped silently; WARNING logged with the sender id |
| `pairing` | sender receives a one-time code (see below) |

- **ACC-1** The local CLI channel is always trusted.
- **ACC-2** Approval takes effect immediately; no restart or YAML edit.

## Pairing

Tables: `pairing_requests(channel, sender_id, code UNIQUE, display_name, created_at, expires_at, last_notified)`,
`pairing_approved(channel, sender_id, approved_at, approved_by)`.

- **ACC-3** Codes are 8 characters from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no look-alikes), generated with
  `secrets.choice`.
- **ACC-4** A code expires after 1 hour. A sender with a live request is **re-shown the same code**, at most once per
  10 minutes (other messages in between are ignored silently).
- **ACC-5** At most 20 requests are pending; further strangers are ignored (and logged) until some expire or are handled.
- **ACC-6** `approve(code)` is case-insensitive, single-use, only valid before expiry.
- **ACC-7** Approve/revoke/list are admin-only: in chat `/pair list|approve <code>|revoke <channel> <sender>` for senders
  listed in `admins` (`"<channel>:<sender_id>"`), always available on the CLI; offline via
  `kiro-crew pairing list|approve <code>|revoke <channel> <sender>`.
- **ACC-8** A non-admin using `/pair` receives "Admins only."
- **ACC-9** Revoking removes access immediately; the next message from that sender is treated as unauthorised again.

Trade-off: unlike `allowlist`, `pairing` replies to strangers, which reveals that a bot exists on that account.

## Approvals for sensitive tools

Configure per agent: `require_approval: [run_shell, run_python]` (any tool names).

Flow (`ApprovalHook` on `BeforeToolCallEvent`):

1. The model calls a listed tool. The call is **paused**.
2. The user is asked in the originating chat: `Approval needed: run <tool> with <args> ... Reply /approve or /deny`.
3. `/approve` runs the tool; `/deny` cancels it (the model is told the user did not approve); no answer within
   `approval_timeout_s` (default 300) is a denial and the user is told.

- **ACC-10** Only one approval question is outstanding per conversation (a per-conversation lock serialises concurrent
  tool calls).
- **ACC-11** `/approve` and `/deny` bypass the per-conversation turn lock (the waiting turn holds it) and are only
  honoured for a conversation that has a pending request; otherwise `/approve` replies "Nothing is waiting for approval."
- **ACC-12** **Fail closed:** if the agent has no origin chat (for example a delegate without an address) the call is
  cancelled rather than executed.
- **ACC-13** Arguments shown in the prompt are truncated to 500 characters.
- **ACC-14** Approval applies to the tool call as a whole, not to its contents beyond what is displayed; the user
  should read the shown arguments.

Delegates inherit the parent's origin so their sensitive calls ask in the same chat. Scheduled and heartbeat
tasks ask in their target chat and are denied after the timeout if unattended.

## Acceptance criteria *(verified by the automated suite)*

- Unauthorised senders get no reply under `allowlist`; under `pairing` they get a code once, not repeatedly.
- Admin approval authorises the sender at once; revocation removes access; non-admins cannot approve.
- Codes are single-use, case-insensitive, expire after an hour, and the pending queue is capped at 20.
- A gated tool runs only after `/approve`; `/deny` and timeout both leave no side effect; a call with no chat to
  ask is refused.
- Disabling the authorisation check or the code-expiry check makes tests fail (checked by deliberate fault injection).
