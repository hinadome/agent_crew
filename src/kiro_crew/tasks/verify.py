"""Completion check: a separate, tool-less model call judges a task result against its criteria."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from strands import Agent

from ..core.context import AppContext

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 12_000

SYSTEM = (
    "You are a strict reviewer. You are given a goal, completion criteria and the result an agent produced. "
    "Decide whether the result satisfies the criteria, judging only from the result text shown. If the result "
    "only claims something that you cannot see evidence for, it is NOT met. Reply with ONLY a JSON object: "
    '{"met": true or false, "feedback": "what is missing or wrong, concrete and short"}.'
)


@dataclass
class Verdict:
    met: bool | None  # None = the reviewer gave no usable verdict
    feedback: str = ""


def parse_verdict(text: str) -> Verdict:
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            data, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("met"), bool):
            return Verdict(data["met"], str(data.get("feedback", "")).strip())
    return Verdict(None, "the reviewer returned no usable verdict")


async def verify(ctx: AppContext, goal: str, criteria: str, result: str) -> Verdict:
    cfg = ctx.config
    model = ctx.models.profile(cfg.models.aux) if cfg.models.aux else ctx.models.resolve()
    reviewer = Agent(model=model, system_prompt=SYSTEM, tools=[], callback_handler=None)
    shown = result if len(result) <= MAX_RESULT_CHARS else result[:MAX_RESULT_CHARS] + "\n[truncated]"
    try:
        out = await reviewer.invoke_async(f"GOAL:\n{goal}\n\nCOMPLETION CRITERIA:\n{criteria}\n\nRESULT:\n{shown}")
    except Exception as e:  # noqa: BLE001 - a broken reviewer must never lose a finished task
        logger.warning("verification call failed: %s: %s", type(e).__name__, e)
        return Verdict(None, "the reviewer call failed")
    return parse_verdict(str(out))
