"""Gateway: the single choke point between channels and agents.

Responsibilities (and nothing else): authorise senders, map conversations to sessions, serialise turns
per conversation, handle slash-commands, run the agent, and deliver chunked replies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from collections import OrderedDict

from strands import Agent

from .agents.budget import find_budget_error
from .agents.factory import AgentFactory
from .channels import Channel, chunk_text
from .core.context import AppContext
from .core.logging import current_turn, end_turn, log, start_turn
from .core.types import Address, InboundMessage, OutboundMessage
from .routing import Route, resolve_agent, switchable_agents

logger = logging.getLogger(__name__)
MAX_CACHED_AGENTS = 64

HELP = """Commands:
/new                    start a fresh conversation (long-term memory is kept)
/model [name]           show or switch the LLM profile for this conversation
/agent [name|reset]     show or switch the agent for this conversation (/agents lists them)
/tasks                  list your background tasks
/cancel <id>            cancel a background task
/schedules              list your reminders and scheduled jobs
/unschedule <id>        cancel a reminder or scheduled job
/memory                 list what I remember about you (/memory search <text>)
/memory forget <id>     delete one remembered fact (/memory clear confirm: delete all)
/approve, /deny         answer a pending approval request
/pair [list|approve <code>|revoke <channel> <sender>]   (admins) manage access
/reload                 (admins) reload agents from the config and agents/ folder
/help                   this message"""


class Gateway:
    def __init__(self, ctx: AppContext, factory: AgentFactory, channels: dict[str, Channel]) -> None:
        self.ctx, self.factory, self.channels = ctx, factory, channels
        self._agents: OrderedDict[str, Agent] = OrderedDict()
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    # -- lifecycle ---------------------------------------------------------------------------
    async def start(self) -> None:
        for ch in self.channels.values():
            await ch.start(self.handle)
            logger.info("Channel started: %s", ch.name)

    async def stop(self) -> None:
        for ch in self.channels.values():
            with contextlib.suppress(Exception):
                await ch.stop()

    # -- outbound (also used for proactive messages, e.g. task results) -----------------------
    async def send(self, address: Address, text: str) -> None:
        ch = self.channels.get(address.channel)
        if ch is None:
            logger.warning("No active channel %r to deliver message", address.channel)
            return
        for part in chunk_text(text, ch.max_len):
            await ch.send(OutboundMessage(address, part))

    # -- inbound -----------------------------------------------------------------------------
    def _authorised(self, ch: Channel, msg: InboundMessage) -> bool:
        if ch.local:
            return True
        allow = ch.config.allow_from
        return ("*" in allow or msg.user_id in allow
                or self.ctx.pairing.is_approved(msg.address.channel, msg.user_id))

    def _is_admin(self, ch: Channel, msg: InboundMessage) -> bool:
        return ch.local or f"{msg.address.channel}:{msg.user_id}" in self.ctx.config.admins

    async def _unauthorised(self, ch: Channel, msg: InboundMessage) -> None:
        if ch.config.access != "pairing":
            logger.warning("Ignored message from unauthorised sender %r on %s. To allow, add it to "
                           "channels.%s.allow_from (or set access: pairing)", msg.user_id, ch.name, ch.name)
            return
        issued = self.ctx.pairing.request(ch.name, msg.user_id, msg.user_name)
        if issued is None:
            logger.warning("Pairing queue full; ignoring %r on %s", msg.user_id, ch.name)
            return
        code, notify = issued
        logger.warning("Pairing request from %r on %s: code %s", msg.user_id, ch.name, code)
        if notify:
            await self.send(msg.address, f"You are not authorised yet. Ask the owner to approve code {code} "
                                         f"(valid 1 hour).")

    def _user_key(self, msg: InboundMessage) -> str:
        """Memory namespace owner: canonical identity if linked, else per-sender, or one shared bucket."""
        if self.ctx.config.memory.scope == "shared":
            return "shared"
        native = f"{msg.address.channel}:{msg.user_id}"
        for person, ids in self.ctx.config.identities.items():
            if native in ids:
                return person
        return native

    async def handle(self, msg: InboundMessage) -> None:
        ch = self.channels[msg.address.channel]
        token = start_turn("chat", channel=msg.address.channel, conversation=msg.address.conversation_id,
                           user=msg.user_id)
        try:
            log(logger, logging.DEBUG, "inbound message", sender=msg.user_id, name=msg.user_name,
                text=msg.text, attachments=len(msg.attachments))
            if not self._authorised(ch, msg):
                log(logger, logging.INFO, "message rejected (unauthorised)", sender=msg.user_id, channel=ch.name)
                await self._unauthorised(ch, msg)
                return
            key = msg.address.key
            route = resolve_agent(self.ctx.config, ch.config, msg, self.ctx.settings.get_agent(key))
            if (turn := current_turn()) is not None:
                turn.agent = route.agent
            log(logger, logging.DEBUG, "agent selected", agent=route.agent, reason=route.reason)
            # /approve and /deny answer a turn that is itself waiting, so they must bypass the turn lock.
            approvals = self.ctx.services.get("approvals")
            if approvals is not None and approvals.has_pending(key) and msg.text.strip().lower() in ("/approve", "/deny"):
                approved = msg.text.strip().lower() == "/approve"
                log(logger, logging.INFO, "approval answered", decision="approve" if approved else "deny")
                approvals.resolve(key, approved)
                await self.send(msg.address, "Approved." if approved else "Denied.")
                return
            lock = self._locks.get(key)
            if lock is None:  # weakly held: freed automatically once no turn is running or queued
                lock = self._locks[key] = asyncio.Lock()
            async with lock:  # one turn at a time per conversation; later messages queue in order
                try:
                    await self._process(ch, msg, key, route)
                except Exception as e:
                    if (stop := find_budget_error(e)) is not None:
                        logger.warning("Turn stopped on %s: %s", key, stop)
                        await self.send(msg.address, f"I had to stop: {stop}. Try rephrasing, or break the request "
                                                     "into smaller steps (or ask me to run it as a background task).")
                        return
                    logger.exception("Error handling message on %s", key)
                    await self.send(msg.address, "Sorry, something went wrong handling that. Please try again.")
        finally:
            end_turn(token)

    async def _process(self, ch: Channel, msg: InboundMessage, key: str, route: Route) -> None:
        text = msg.text.strip()
        if text.startswith("/"):
            reply = await self._command(text, msg, key, route)
            if reply is not None:
                log(logger, logging.DEBUG, "command handled", command=text.split()[0])
                await self.send(msg.address, reply)
                return
        typing = asyncio.create_task(self._keep_typing(ch, msg.address))
        started = time.monotonic()
        try:
            agent = self._agent_for(ch, msg, key, route.agent)
            log(logger, logging.DEBUG, "invoking agent", agent=agent.name,
                model=self._model_override(key) or self.ctx.config.models.default)
            result = await agent.invoke_async(text)
        finally:
            typing.cancel()
        reply = str(result)
        log(logger, logging.DEBUG, "agent reply", chars=len(reply), ms=int((time.monotonic() - started) * 1000))
        await self.send(msg.address, reply)

    async def _keep_typing(self, ch: Channel, address: Address) -> None:
        while True:
            with contextlib.suppress(Exception):
                await ch.typing(address)
            await asyncio.sleep(8)

    # -- agents ------------------------------------------------------------------------------
    def _model_override(self, key: str) -> str | None:
        return self.ctx.settings.get_model_profile(key)

    def _agent_for(self, ch: Channel, msg: InboundMessage, key: str, agent_name: str) -> Agent:
        cache_key = f"{key}|{agent_name}"  # one cached agent per (conversation, agent): switching never mixes them
        if cache_key in self._agents:
            self._agents.move_to_end(cache_key)
            return self._agents[cache_key]
        agent = self.factory.create(agent_name, session_id=key, user_id=self._user_key(msg),
                                    origin=msg.address, model_profile=self._model_override(key))
        self._agents[cache_key] = agent
        while len(self._agents) > MAX_CACHED_AGENTS:
            self._agents.popitem(last=False)  # evicted agents are rebuilt from their persisted session
        return agent

    # -- commands ----------------------------------------------------------------------------
    def evict(self, key: str | None = None) -> None:
        """Drop cached agents for one conversation (or all). They are rebuilt from their persisted sessions."""
        if key is None:
            self._agents.clear()
        else:
            for k in [k for k in self._agents if k.startswith(f"{key}|")]:
                del self._agents[k]

    async def _command(self, text: str, msg: InboundMessage, key: str, route: Route) -> str | None:
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        match cmd.lower():
            case "/help":
                return HELP
            case "/new":
                self.evict(key)
                self.factory.reset_conversation(key)
                return "Started a fresh conversation. (Long-term memory is kept.)"
            case "/model":
                names = self.ctx.models.profile_names
                if not arg:
                    cur = self._model_override(key) or f"{self.ctx.config.models.default} (default)"
                    return f"Current: {cur}\nAvailable: {', '.join(names)}"
                if arg not in names:
                    return f"Unknown profile {arg!r}. Available: {', '.join(names)}"
                self.ctx.settings.set_model_profile(key, arg)
                self.evict(key)  # rebuilt on next turn; history is restored from the session
                return f"Switched this conversation to {arg}."
            case "/agent" | "/agents":
                return self._agent_command(arg, msg, key, route, listing=cmd.lower() == "/agents")
            case "/reload":
                ch = self.channels[msg.address.channel]
                if not self._is_admin(ch, msg):
                    return "Admins only."
                reload = self.ctx.services.get("reload_agents")
                return reload().render() if reload else "Reload is not available."
            case "/tasks":
                tasks = self.ctx.tasks.list(user_id=self._user_key(msg), limit=10)
                return "\n".join(t.summary() for t in tasks) or "No tasks yet."
            case "/schedules":
                items = self.ctx.schedules.list(self._user_key(msg))
                return "\n".join(x.summary() for x in items) or "No reminders or scheduled jobs."
            case "/unschedule":
                if not arg:
                    return "Usage: /unschedule <id>"
                return "Cancelled." if self.ctx.schedules.delete(arg, self._user_key(msg)) else "No such schedule."
            case "/approve" | "/deny":
                return "Nothing is waiting for approval."
            case "/memory":
                return await self._memory_command(arg, msg, route.agent)
            case "/pair":
                ch = self.channels[msg.address.channel]
                return self._pair_command(arg, msg) if self._is_admin(ch, msg) else "Admins only."
            case "/cancel":
                runner = self.ctx.services.get("task_runner")
                if not arg or runner is None:
                    return "Usage: /cancel <task id>"
                return "Cancelled." if runner.cancel(arg) else "Task not found or already finished."
        return None  # unknown slash text goes to the agent as a normal message

    def _agent_command(self, arg: str, msg: InboundMessage, key: str, route: Route, *, listing: bool) -> str:
        ch = self.channels[msg.address.channel]
        cfg = self.ctx.config
        admin = self._is_admin(ch, msg)
        options = switchable_agents(cfg, ch.config, is_admin=admin)
        if listing or not arg:
            lines = [f"Current agent: {route.agent} ({route.reason})"]
            for name in options:
                a = cfg.agents[name]
                flags = ", ".join(f for f in (("isolated" if a.isolated else ""), ("private memory" if a.private_memory else ""))
                                  if f)
                lines.append(f"  {name}" + (f" - {a.description}" if a.description else "") + (f" [{flags}]" if flags else ""))
            lines.append("Switch with /agent <name>; /agent reset restores the default." if options
                         else "No other agents are available to switch to.")
            return "\n".join(lines)
        if arg.lower() in ("reset", "default"):
            self.ctx.settings.set_agent(key, None)
            self.evict(key)
            return "Back to the default agent for this chat."
        if arg not in cfg.agents or arg not in options:
            return f"You can't switch to {arg!r}. Available: {', '.join(options) or 'none'}"
        self.ctx.settings.set_agent(key, arg)
        self.evict(key)
        log(logger, logging.INFO, "agent switched", agent=arg, by=msg.user_id)
        return f"Switched this conversation to {arg}."

    # -- admin helpers -----------------------------------------------------------------------
    def _pair_command(self, arg: str, msg: InboundMessage) -> str:
        pairing = self.ctx.pairing
        sub, _, rest = arg.partition(" ")
        rest = rest.strip()
        who = f"{msg.address.channel}:{msg.user_id}"
        match sub.lower():
            case "" | "list":
                pend = pairing.pending()
                lines = [f"pending: {r.channel}:{r.sender_id} ({r.display_name or '?'}) code {r.code}" for r in pend]
                lines += [f"approved: {c}:{sid}" for c, sid in pairing.approved()]
                return "\n".join(lines) or "No pairing requests."
            case "approve":
                req = pairing.approve(rest, approved_by=who) if rest else None
                return (f"Approved {req.channel}:{req.sender_id}." if req
                        else "Unknown or expired code. Usage: /pair approve <code>")
            case "revoke":
                parts = rest.split(None, 1)
                ok = len(parts) == 2 and pairing.revoke(parts[0], parts[1])
                return "Revoked." if ok else "Usage: /pair revoke <channel> <sender_id> (must be approved)"
        return "Usage: /pair [list|approve <code>|revoke <channel> <sender>]"

    async def _memory_command(self, arg: str, msg: InboundMessage, agent: str) -> str:
        from .memory.factory import make_store

        store = make_store(self.ctx, self._user_key(msg), agent=agent)
        if not hasattr(store, "list"):
            return "This memory backend does not support listing."
        sub, _, rest = arg.partition(" ")
        rest = rest.strip()
        match sub.lower():
            case "" | "list":
                items = store.list(limit=20)
                if not items:
                    return "I have no stored memories about you yet."
                return f"{store.count()} memories (newest first):\n" + "\n".join(
                    f"[{i['id']}] {i['content'][:200]}" for i in items)
            case "search":
                hits = await store.search(rest) if rest else []
                return "\n".join(f"[{h.metadata and h.metadata.get('id')}] {h.content[:200]}" for h in hits) \
                    or "No matches."
            case "forget":
                if not rest.isdigit():
                    return "Usage: /memory forget <id>"
                return "Forgotten." if store.delete(int(rest)) else "No such memory."
            case "clear":
                if rest != "confirm":
                    return "This deletes ALL your stored memories. Run '/memory clear confirm' to proceed."
                return f"Deleted {store.clear()} memories."
        return "Usage: /memory [list|search <text>|forget <id>|clear confirm]"
