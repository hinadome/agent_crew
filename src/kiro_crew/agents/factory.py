"""Builds Strands agents from config. The only place that knows how the pieces fit together."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from strands import Agent, tool
from strands.agent.conversation_manager import (
    SlidingWindowConversationManager,
    SummarizingConversationManager,
)
from strands.memory import ExtractionConfig, IntervalTrigger, MemoryManager
from strands.session import RepositorySessionManager

from ..core.config import MCPServerConfig
from ..core.context import AppContext
from ..core.types import Address
from ..memory.factory import make_store
from ..security.approval import ApprovalHook
from ..tasks.state import TaskStateHook, task_state_tools
from ..tasks.subtasks import subtask_tools
from ..tasks.tools import ask_user_tool, report_progress_tool
from ..tools import resolve_tools
from .budget import BudgetHook
from .trace import TraceHook

logger = logging.getLogger(__name__)
MAX_DELEGATION_DEPTH = 2
_POOL_PIN = "kiro-crew:mcp-pool"  # permanent consumer id that keeps pooled MCP servers alive
DELEGATE_SEP = "~"  # session id of a stateful delegate: <parent session>~<delegate name>


class AgentFactory:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.cfg = ctx.config
        self.repo = ctx.storage.sessions
        # One MCP client per (server, config) shared by every agent that uses it. Strands ref-counts consumers and
        # would stop a server whenever its last agent goes away; the pool keeps a permanent consumer so short-lived
        # delegate agents do not respawn the server process on every call.
        self._mcp_pool: dict[tuple[str, str], Any] = {}

    # -- public ------------------------------------------------------------------------------
    def create(self, agent_name: str | None = None, *, session_id: str | None, user_id: str,
               origin: Address | None = None, model_profile: str | None = None, extra_prompt: str = "",
               task_id: str | None = None, _depth: int = 0) -> Agent:
        name = agent_name or self.cfg.entry_agent
        acfg = self.cfg.agents[name]

        tool_specs = [t for t in acfg.tools if not (task_id and t in ("tasks", "schedule"))]  # no recursion
        tools: list[Any] = resolve_tools(tool_specs, self.ctx.for_agent(name))
        tools += [self._mcp_client(m) for m in self.cfg.mcp_servers if m.name in acfg.mcp_servers]
        if _depth < MAX_DELEGATION_DEPTH:
            tools += [self._delegate_tool(d, user_id, _depth, origin, session_id) for d in acfg.delegates]
        if task_id:
            tools.append(report_progress_tool(self.ctx, task_id))
            if self.cfg.tasks.ask_user:
                tools.append(ask_user_tool(self.ctx, task_id))
            tools += task_state_tools(self.ctx, task_id)
            parent = self.ctx.tasks.get(task_id)
            if self.cfg.tasks.max_subtasks > 0 and parent is not None and parent.parent_id is None:  # one level only
                tools += subtask_tools(self.ctx, task_id)

        owner_context = self.ctx.workspace.system_context(soul=acfg.use_soul, core_memory=acfg.use_core_memory)
        prompt = "\n\n".join(p for p in (acfg.system_prompt, owner_context, extra_prompt) if p)
        persistent = session_id is not None  # delegates are ephemeral: no session, no memory writes

        agent = Agent(
            agent_id=name,
            name=name,
            description=acfg.description or None,
            model=self.ctx.models.resolve(model_profile or acfg.model),
            aux_model=self.ctx.models.profile(self.cfg.models.aux) if self.cfg.models.aux else None,
            system_prompt=prompt,
            tools=tools,
            conversation_manager=self._conversation_manager(),
            memory_manager=self._memory_manager(user_id, name, extract=persistent and _depth == 0),
            session_manager=(RepositorySessionManager(session_id=session_id, session_repository=self.repo)
                             if session_id is not None else None),
            hooks=self._hooks(acfg, task_id) + ([TaskStateHook(self.ctx, task_id, prompt)] if task_id else []),
            callback_handler=None,
        )
        # Set after construction so a restored session can never override routing info.
        agent.state.set("user_id", user_id)
        agent.state.set("agent_name", name)
        agent.state.set("origin", {"channel": origin.channel, "conversation_id": origin.conversation_id,
                                   "thread_id": origin.thread_id} if origin else None)
        return agent

    def reset_session(self, session_id: str) -> None:
        self.repo.delete_session(session_id)

    def reset_conversation(self, key: str) -> None:
        """Forget a conversation: its own session plus the sessions of stateful delegates it spawned."""
        self.repo.delete_session(key)
        self.repo.delete_prefix(f"{key}{DELEGATE_SEP}")

    # -- parts -------------------------------------------------------------------------------
    def _conversation_manager(self) -> Any:
        c = self.cfg.conversation
        if c.strategy == "sliding_window":
            return SlidingWindowConversationManager(window_size=c.window_size)
        return SummarizingConversationManager(summary_ratio=c.summary_ratio,
                                              preserve_recent_messages=c.preserve_recent_messages)

    def _memory_manager(self, user_id: str, agent: str, *, extract: bool) -> MemoryManager:
        m = self.cfg.memory
        extraction: Any = False
        if extract and m.auto_extract:
            extraction = ExtractionConfig(trigger=IntervalTrigger(turns=m.extract_every_n_turns))
        store = make_store(self.ctx, user_id, extraction=extraction, agent=agent)
        return MemoryManager(stores=[store], add_tool_config=m.allow_agent_writes and extract,
                             injection={"max_entries": m.inject_max_entries})

    def _hooks(self, acfg: Any, task_id: str | None = None) -> list[Any]:
        manager = self.ctx.services.get("approvals")
        lim = self.cfg.limits
        hooks: list[Any] = [TraceHook()]  # DEBUG-level tracing of tool/model/message activity
        task = self.ctx.tasks.get(task_id) if task_id else None
        hooks.append(BudgetHook(lim.task_tool_calls if task_id else lim.chat_tool_calls, lim.repeat_limit,
                                lim.grace_calls, per_invocation=task_id is None,
                                max_tokens=lim.task_tokens if task_id else lim.chat_tokens,
                                prior_tokens=task.tokens if task else 0))
        if acfg.require_approval and manager is not None:
            hooks.append(ApprovalHook(manager, set(acfg.require_approval)))
        return hooks

    def _mcp_client(self, m: MCPServerConfig) -> Any:
        key = (m.name, json.dumps(m.model_dump(), sort_keys=True, default=str))
        client = self._mcp_pool.get(key)
        if client is None:
            client = self._mcp_pool[key] = self._build_mcp_client(m)
            client.add_consumer(_POOL_PIN)  # never reaches zero consumers -> the server stays up between agents
        return client

    def refresh_mcp(self) -> None:
        """After a config reload: stop pooled servers whose definition changed or was removed."""
        live = {(m.name, json.dumps(m.model_dump(), sort_keys=True, default=str)) for m in self.cfg.mcp_servers}
        for key in [k for k in self._mcp_pool if k not in live]:
            self._release_mcp(self._mcp_pool.pop(key))

    def close(self) -> None:
        """Stop every pooled MCP server (call at shutdown)."""
        for client in list(self._mcp_pool.values()):
            self._release_mcp(client)
        self._mcp_pool.clear()

    @staticmethod
    def _release_mcp(client: Any) -> None:
        try:
            client.remove_consumer(_POOL_PIN)
            client.stop(None, None, None)
        except Exception:
            logger.warning("could not stop an MCP server cleanly", exc_info=True)

    def _build_mcp_client(self, m: MCPServerConfig) -> Any:
        from strands.tools.mcp import MCPClient

        if m.url:
            return MCPClient(url=m.url, headers=m.headers or None, prefix=m.name)
        from mcp import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=m.command or "", args=m.args, env=m.env or None)
        # mcp's stdio_client and strands' MCPClient declare structurally-equal but nominally different stream
        # types (anyio vs mcp wrappers); they interoperate at runtime, so silence only this call.
        return MCPClient(lambda: stdio_client(params), prefix=m.name)  # type: ignore[arg-type,return-value]

    def _delegate_tool(self, delegate: str, user_id: str, depth: int, origin: Address | None,
                       parent_session: str | None = None) -> Any:
        """Expose another configured agent as a tool (the 'agents as tools' crew pattern).

        A delegate marked ``stateful: true`` keeps its own conversation: it has a persisted session scoped to the
        parent conversation (``<parent session>~<delegate>``) and one agent instance reused across calls, so it can
        answer follow-ups. Otherwise every call gets a fresh, sessionless agent. Calls to a stateful delegate are
        serialised (an agent cannot be invoked concurrently).
        """
        desc = self.cfg.agents[delegate].description or f"Delegate a sub-task to the '{delegate}' specialist."
        stateful = self.cfg.agents[delegate].stateful and parent_session is not None
        holder: dict[str, Agent] = {}
        lock = asyncio.Lock()

        @tool(name=f"ask_{delegate}", description=desc)
        async def delegate_to(request: str) -> str:
            """Delegate work to a specialist agent.

            Args:
                request: Complete, self-contained instructions for the specialist.
            """
            if not stateful:
                sub = self.create(delegate, session_id=None, user_id=user_id, origin=origin, _depth=depth + 1)
                return str(await sub.invoke_async(request))
            async with lock:
                if "agent" not in holder:
                    holder["agent"] = self.create(delegate, session_id=f"{parent_session}{DELEGATE_SEP}{delegate}",
                                                  user_id=user_id, origin=origin, _depth=depth + 1)
                return str(await holder["agent"].invoke_async(request))

        return delegate_to
