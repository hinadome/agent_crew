"""`kiro-crew` command line."""

from __future__ import annotations

import argparse
import asyncio
import sys
from importlib import resources
from pathlib import Path

DEFAULT_CONFIG = Path("kiro-crew.yaml")


def _apply_cli_logging(args: argparse.Namespace) -> None:
    """Configure logging from CLI flags when any were given; otherwise leave it to config (via App).

    When flags are present we install handlers now so they win over config and cover every command
    (including the offline admin ones). ``init`` needs no logging; it returns before anything logs.
    """
    from .core.logging import configure, parse_area_overrides

    level = args.log_level or ("DEBUG" if args.verbose else None)
    has_flags = bool(level or args.log or args.log_file or args.log_json or args.log_content)
    if not has_flags:
        return  # config-driven; App.configure_logging_from will handle it
    configure(level or "INFO", areas=parse_area_overrides(args.log), file=args.log_file,
              json_format=args.log_json, console=True, include_content=True if args.log_content else None)


def _admin(args: argparse.Namespace) -> None:
    """Offline administration: operates directly on the database; no channels are started."""
    from .core.config import load_config

    cfg = load_config(args.config)
    from .app import build_context

    ctx = build_context(cfg)
    try:
        if args.cmd == "prune":
            from .memory.maintenance import run_once

            print(run_once(ctx))
        elif args.cmd == "pairing":
            p = ctx.pairing
            if args.action == "list":
                for r in p.pending():
                    print(f"pending   {r.channel}:{r.sender_id} ({r.display_name or '?'})  code {r.code}")
                for c, sid in p.approved():
                    print(f"approved  {c}:{sid}")
            elif args.action == "approve":
                approved = p.approve(args.code, approved_by="cli")
                print(f"approved {approved.channel}:{approved.sender_id}" if approved else "unknown or expired code")
            else:
                print("revoked" if p.revoke(args.channel, args.sender_id) else "not found")
        elif args.cmd == "task":
            if args.action == "list":
                for t in ctx.tasks.list(limit=30):
                    print(t.summary())
            else:
                ok = ctx.tasks.answer(args.id, " ".join(args.text))
                print("answered; the running bot resumes the task within seconds" if ok
                      else "not found, or the task is not waiting for an answer")
        elif args.cmd == "schedule":
            if args.action == "list":
                for s in ctx.schedules.list(include_system=True):
                    print(s.summary())
            else:
                print("cancelled" if ctx.schedules.delete(args.id) else "not found")
        else:  # memory
            from .memory.factory import make_store

            store = make_store(ctx, args.user, agent=args.agent)
            if args.action == "list":
                print(f"{store.count()} memories for {args.user}")
                for i in store.list(limit=100):
                    print(f"[{i['id']}] {i['content']}")
            elif args.action == "search":
                for h in asyncio.run(store.search(args.query)):
                    print(f"[{(h.metadata or {}).get('id')}] {h.content}")
            elif args.action == "forget":
                print("forgotten" if store.delete(args.id) else "not found")
            elif args.action == "clear":
                print(f"deleted {store.clear()}" if args.yes else "refusing without --yes")
            else:
                if not hasattr(store, "reindex"):
                    sys.exit("reindex needs memory.backend: hybrid")
                print(f"embedded {asyncio.run(store.reindex())} facts")
    finally:
        ctx.storage.close()


def _agent_cli(args: argparse.Namespace) -> None:
    """`kiro-crew agent ...`: needs only the config (no database, no channels)."""
    from .agents import manage
    from .core.config import load_config

    def fail(msg: str) -> None:
        sys.exit(f"error: {msg}")

    if args.action == "validate":
        res = manage.validate(args.config)
        print(res.message)
        for w in res.warnings:
            print(f"warning: {w}")
        if not res.ok:
            sys.exit(1)
        return
    if args.action == "reload":
        from .agents.reload import signal_running_process

        try:
            pidfile = load_config(args.config).workspace / "kiro-crew.pid"
        except Exception as e:  # noqa: BLE001
            fail(str(e))
        ok, msg = signal_running_process(pidfile)
        print(msg)
        if not ok:
            sys.exit(1)
        return
    try:
        cfg = load_config(args.config)
    except Exception as e:  # noqa: BLE001  (surface any config problem as a clean message)
        fail(str(e))
    if args.action == "list":
        listing = manage.overview(cfg)
        width = max(len(r["name"]) for r in listing)
        print(f"{'NAME':<{width}}  {'SOURCE':<6}  {'MODEL':<22}  {'FLAGS':<34}  REACHED FROM")
        for r in listing:
            flags = [f for f, on in (("isolated", r["isolated"]), ("selectable", r["selectable"]),
                                     ("stateful", r["stateful"]), ("private-memory", r["private_memory"] and not r["isolated"])) if on]
            if r["delegate_of"]:
                flags.append("delegate-of:" + ",".join(r["delegate_of"]))
            print(f"{r['name']:<{width}}  {r['source']:<6}  {r['model']:<22}  {' '.join(flags):<34}  "
                  f"{', '.join(r['routed_from']) or '-'}")
    elif args.action == "show":
        by_name = {r["name"]: r for r in manage.overview(cfg)}
        if args.name not in by_name:
            fail(f"no such agent {args.name!r}. Known: {', '.join(by_name)}")
        import yaml

        print(yaml.safe_dump(by_name[args.name], sort_keys=False, allow_unicode=True).rstrip())
        shown = cfg.agents[args.name].system_prompt.strip().splitlines()
        print("prompt: |")
        for line in shown[:12]:
            print(f"  {line}")
        if len(shown) > 12:
            print(f"  ... ({len(shown) - 12} more lines)")
    elif args.action == "add":
        prompt = args.prompt or ""
        if args.prompt_file:
            prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
        split = lambda v: [x.strip() for x in v.split(",") if x.strip()] if v else None
        try:
            path = manage.scaffold(args.config, args.name, prompt=prompt, description=args.description,
                                   model=args.model, tools=split(args.tools), delegate_of=split(args.delegate_of),
                                   isolated=args.isolated, selectable=args.selectable, stateful=args.stateful,
                                   template=args.template)
        except ValueError as e:
            fail(str(e))
        print(f"created {path}")
        print("A running bot picks it up within a few seconds (or run: kiro-crew agent reload).")
    elif args.action == "remove":
        try:
            path = manage.remove(args.config, args.name)
        except ValueError as e:
            fail(str(e))
        print(f"removed {path}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="kiro-crew", description=__doc__)
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="path to YAML config")
    p.add_argument("-v", "--verbose", action="store_true", help="shortcut for --log-level DEBUG")
    p.add_argument("--log-level", help="base log level: DEBUG|INFO|WARNING|ERROR (overrides config)")
    p.add_argument("--log", action="append", metavar="AREA=LEVEL",
                   help="per-area level, repeatable, e.g. --log gateway=debug --log memory=info")
    p.add_argument("--log-file", help="also write logs to this file (rotating)")
    p.add_argument("--log-json", action="store_true", help="emit JSON log lines")
    p.add_argument("--log-content", action="store_true",
                   help="include message text, tool arguments/results and commands in logs (hidden by default)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="write a starter kiro-crew.yaml here")
    run = sub.add_parser("run", help="start all enabled channels + task workers")
    run.add_argument("--only", nargs="*", help="only start these channels (e.g. --only cli)")
    sub.add_parser("chat", help="shortcut for: run --only cli")
    sub.add_parser("check", help="validate config and list registered plugins")
    sub.add_parser("prune", help="run housekeeping once (delete old sessions and finished tasks)")

    pr = sub.add_parser("pairing", help="manage who may talk to the bot")
    prs = pr.add_subparsers(dest="action", required=True)
    prs.add_parser("list")
    pa = prs.add_parser("approve")
    pa.add_argument("code")
    pv = prs.add_parser("revoke")
    pv.add_argument("channel")
    pv.add_argument("sender_id")

    mem = sub.add_parser("memory", help="inspect and manage long-term memory")
    mem.add_argument("--user", default="shared", help="memory owner (default: shared; else e.g. discord:123 or a name)")
    mem.add_argument("--agent", default=None, help="an agent with private memory (default: the shared memory)")
    mems = mem.add_subparsers(dest="action", required=True)
    mems.add_parser("list")
    ms = mems.add_parser("search")
    ms.add_argument("query")
    mf = mems.add_parser("forget")
    mf.add_argument("id", type=int)
    mems.add_parser("clear").add_argument("--yes", action="store_true", help="required to actually delete")
    mems.add_parser("reindex", help="(hybrid backend) embed facts that are missing vectors")

    ag = sub.add_parser("agent", help="list, add, validate, remove or reload agents")
    ags = ag.add_subparsers(dest="action", required=True)
    ags.add_parser("list", help="all agents, where they come from and what reaches them")
    ags.add_parser("show", help="one agent's effective settings").add_argument("name")
    ags.add_parser("validate", help="load the config + agents/ folder exactly as the app would and report problems")
    ags.add_parser("remove", help="delete a drop-in agent file").add_argument("name")
    ags.add_parser("reload", help="ask a running `kiro-crew run` to reload agents now (sends SIGHUP)")
    add = ags.add_parser("add", help="create agents/<name>.md (picked up by a running bot within seconds)")
    add.add_argument("name")
    add.add_argument("--description", default="")
    add.add_argument("--model", help="a model profile name")
    add.add_argument("--tools", help="comma-separated tool names, e.g. web,files")
    add.add_argument("--prompt", help="the system prompt text")
    add.add_argument("--prompt-file", help="read the system prompt from this file")
    add.add_argument("--delegate-of", help="comma-separated parent agents that should get an ask_<name> tool")
    add.add_argument("--template", default="basic", choices=["basic", "public", "specialist"],
                     help="public = isolated + selectable (low-trust, public-facing)")
    add.add_argument("--isolated", action="store_true", default=None)
    add.add_argument("--selectable", action="store_true", default=None)
    add.add_argument("--stateful", action="store_true", default=None)

    tk = sub.add_parser("task", help="list background tasks or answer a waiting task's question")
    tks = tk.add_subparsers(dest="action", required=True)
    tks.add_parser("list")
    ans = tks.add_parser("answer")
    ans.add_argument("id")
    ans.add_argument("text", nargs="+")
    sc = sub.add_parser("schedule", help="list or cancel reminders/scheduled jobs")
    scs = sc.add_subparsers(dest="action", required=True)
    scs.add_parser("list")
    scs.add_parser("cancel").add_argument("id")
    args = p.parse_args(argv)

    _apply_cli_logging(args)

    if args.cmd == "init":
        dest = Path(args.config)
        if dest.exists():
            sys.exit(f"{dest} already exists")
        dest.write_text((resources.files("kiro_crew") / "default_config.yaml").read_text())
        print(f"Wrote {dest}. Edit it, then run: kiro-crew chat")
        return

    if args.cmd == "agent":
        _agent_cli(args)
        return

    from .app import load_app

    if args.cmd == "check":
        from .channels import _BUILTIN, channel_registry
        from .memory.longterm import memory_backends
        from .providers import providers
        from .tools import tool_factories

        app = load_app(args.config, only_channels=[])
        print("config OK")
        print("providers:", providers.names())
        print("tools:", tool_factories.names())
        print("memory backends:", memory_backends.names())
        print("channels:", sorted(set(_BUILTIN) | set(channel_registry.names())))
        print("enabled channels:", [k for k, v in app.cfg.channels.items() if v.enabled])
        return

    if args.cmd in ("pairing", "memory", "schedule", "task", "prune"):
        _admin(args)
        return

    only = ["cli"] if args.cmd == "chat" else args.only
    asyncio.run(load_app(args.config, only).run())


if __name__ == "__main__":
    main()
