"""Read-only web dashboard for live visibility: streaming logs, turns, tasks, schedules, pairing.

Token-gated (``web_ui.token``). It exposes conversation metadata and log lines, so bind it locally or put
it behind your own auth/proxy. It never mutates state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from ..agents.manage import overview
from ..core.context import AppContext
from ..core.logging import iter_recent, ring

logger = logging.getLogger("kiro_crew.web")


class WebUI:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.cfg = ctx.config.web_ui
        self._runner: web.AppRunner | None = None

    # -- auth --------------------------------------------------------------------------------
    def _authorised(self, request: web.Request) -> bool:
        token = self.cfg.token
        if not token:
            return True  # explicitly unauthenticated (config load already warned)
        supplied = request.headers.get("X-Auth-Token") or request.query.get("token")
        return supplied == token

    def _guard(self, request: web.Request) -> None:
        if not self._authorised(request):
            raise web.HTTPUnauthorized(text="missing or bad token")

    # -- lifecycle ---------------------------------------------------------------------------
    async def start(self) -> None:
        r = ring()
        if r is not None:
            r.bind_loop(asyncio.get_running_loop())
        app = web.Application()
        app.add_routes([
            web.get("/", self.index),
            web.get("/api/logs", self.api_logs),
            web.get("/api/stream", self.api_stream),
            web.get("/api/state", self.api_state),
        ])
        # The access log records the request line, which includes ?token=... -> never enable it.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg.host, self.cfg.port)
        await site.start()
        logger.info("Web UI on http://%s:%d%s", self.cfg.host, self.cfg.port,
                    "" if self.cfg.token else "  (UNAUTHENTICATED)")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # -- endpoints ---------------------------------------------------------------------------
    async def index(self, request: web.Request) -> web.Response:
        return web.Response(text=_PAGE, content_type="text/html")

    async def api_logs(self, request: web.Request) -> web.Response:
        self._guard(request)
        limit = min(int(request.query.get("limit", 500)), 2000)
        level = request.query.get("level", "0")
        events = list(iter_recent(limit, level if level != "0" else 0))
        return web.json_response(events)

    async def api_state(self, request: web.Request) -> web.Response:
        self._guard(request)
        tasks = [t.__dict__ | {"address": None} for t in self.ctx.tasks.list(limit=50)]
        for t in tasks:
            t.pop("address", None)
        schedules = [{"id": s.id, "summary": s.summary(), "mode": s.mode, "next_run": s.next_run,
                      "run_count": s.run_count, "user_id": s.user_id}
                     for s in self.ctx.schedules.list(include_system=True)]
        pending = [{"channel": p.channel, "sender_id": p.sender_id, "code": p.code,
                    "display_name": p.display_name, "expires_at": p.expires_at}
                   for p in self.ctx.pairing.pending()]
        approved = [{"channel": c, "sender_id": s} for c, s in self.ctx.pairing.approved()]
        return web.json_response({
            "tasks": [_task_view(t) for t in self.ctx.tasks.list(limit=50)],
            "schedules": schedules,
            "pairing": {"pending": pending, "approved": approved},
            "channels": list(self.ctx.services["gateway"].channels),
            "agents": list(self.ctx.config.agents),
            "agent_details": overview(self.ctx.config),
            "routes": [r.model_dump(exclude_none=True) for r in self.ctx.config.routes],
            "entry_agent": self.ctx.config.entry_agent,
        })

    async def api_stream(self, request: web.Request) -> web.StreamResponse:
        self._guard(request)
        r = ring()
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        if r is None:
            await resp.write(b"event: error\ndata: logging ring not initialised\n\n")
            return resp
        q = r.subscribe()
        try:
            await resp.write(b": connected\n\n")
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(f"data: {json.dumps(event.as_dict(), default=str)}\n\n".encode())
                except TimeoutError:
                    await resp.write(b": keep-alive\n\n")  # keep proxies from closing idle streams
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            r.unsubscribe(q)
        return resp


def _task_view(t: Any) -> dict[str, Any]:
    return {"id": t.id, "title": t.title, "status": t.status, "attempts": t.attempts,
            "user_id": t.user_id, "progress": t.progress[-3:], "updated_at": t.updated_at}


_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<title>kiro-crew</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--fg:#d7dae0;--mut:#8b93a1;--card:#171a21;--line:#262b36;--acc:#5aa9e6}
*{box-sizing:border-box}body{margin:0;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;background:var(--bg);color:var(--fg)}
header{display:flex;gap:12px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);flex-wrap:wrap}
h1{font-size:14px;margin:0;color:#fff}.pill{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1px 8px;color:var(--mut)}
select,input,button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font:inherit}
main{display:grid;grid-template-columns:1fr 340px;gap:12px;padding:12px;height:calc(100vh - 52px)}
#logwrap{overflow:auto;background:#0b0d11;border:1px solid var(--line);border-radius:8px;padding:8px}
.row{white-space:pre-wrap;word-break:break-word;padding:1px 0;border-bottom:1px solid #12151b}
.lvl{display:inline-block;width:46px;font-weight:700}.DEBUG{color:#6b7280}.INFO{color:#8fc8ff}.WARNING{color:#f0c674}.ERROR,.CRITICAL{color:#ff7b72}.EXCEPTION{color:#ff7b72}
.meta{color:var(--mut)}.msg{color:var(--fg)}.turn{color:#b48ead}
aside{overflow:auto;display:flex;flex-direction:column;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px}
.card h2{font-size:12px;margin:0 0 6px;color:#fff;text-transform:uppercase;letter-spacing:.04em}
.item{border-bottom:1px solid var(--line);padding:3px 0}.item:last-child{border:0}
.s-succeeded{color:#7ee787}.s-failed{color:#ff7b72}.s-running{color:#8fc8ff}.s-queued,.s-cancelled{color:var(--mut)}
a{color:var(--acc)}
</style></head><body>
<header>
<h1>kiro-crew</h1>
<span class=pill id=conn>connecting…</span>
<label>level <select id=level><option>DEBUG<option selected>INFO<option>WARNING<option>ERROR</select></label>
<input id=filter placeholder="filter text / turn / user" size=22>
<label><input type=checkbox id=follow checked> follow</label>
<button id=clear>clear</button>
<span class=pill id=count>0</span>
</header>
<main>
 <div id=logwrap><div id=log></div></div>
 <aside>
  <div class=card><h2>Channels / agents</h2><div id=meta class=mut></div></div>
  <div class=card><h2>Agents</h2><div id=agents></div></div>
  <div class=card><h2>Tasks</h2><div id=tasks></div></div>
  <div class=card><h2>Schedules</h2><div id=sched></div></div>
  <div class=card><h2>Pairing</h2><div id=pair></div></div>
 </aside>
</main>
<script>
const tok=new URLSearchParams(location.search).get('token')||'';
const q=s=>document.querySelector(s), ORDER={DEBUG:10,INFO:20,WARNING:30,ERROR:40,CRITICAL:50,EXCEPTION:40};
let rows=[], follow=true;
const esc=s=>(s??'').toString().replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function visible(e){
  const min=ORDER[q('#level').value]||0; if((ORDER[e.level]||0)<min) return false;
  const f=q('#filter').value.trim().toLowerCase(); if(!f) return true;
  return [e.message,e.logger,e.turn_id,e.user,e.conversation,e.agent,JSON.stringify(e.fields)].join(' ').toLowerCase().includes(f);
}
function fmtTurn(e){ if(!e.turn_id) return ''; let t=' <span class=turn>['+e.turn_id+' '+(e.kind||'');
  if(e.conversation) t+=' '+esc(e.channel||'')+':'+esc(e.conversation); if(e.user) t+=' '+esc(e.user);
  if(e.agent) t+=' @'+esc(e.agent); return t+']</span>'; }
function fmtFields(f){ if(!f||!Object.keys(f).length) return ''; return ' <span class=meta>'+esc(Object.entries(f).map(([k,v])=>k+'='+(typeof v==='object'?JSON.stringify(v):v)).join(' '))+'</span>'; }
function render(){
  const log=q('#log'); const shown=rows.filter(visible);
  log.innerHTML=shown.map(e=>{const t=new Date(e.ts*1000).toLocaleTimeString();
    return '<div class=row><span class=meta>'+t+'</span> <span class="lvl '+e.level+'">'+e.level+'</span> '+
      '<span class=meta>'+esc(e.logger.replace('kiro_crew.',''))+'</span> <span class=msg>'+esc(e.message)+'</span>'+fmtFields(e.fields)+fmtTurn(e)+'</div>';}).join('');
  q('#count').textContent=shown.length+' / '+rows.length;
  if(follow){const w=q('#logwrap'); w.scrollTop=w.scrollHeight;}
}
function push(e){ rows.push(e); if(rows.length>5000) rows=rows.slice(-4000); render(); }
async function load(){
  const r=await fetch('/api/logs?limit=1000&token='+encodeURIComponent(tok)); if(!r.ok){q('#conn').textContent='unauthorised';return false;}
  rows=await r.json(); render(); return true;
}
function stream(){
  const es=new EventSource('/api/stream?token='+encodeURIComponent(tok));
  es.onopen=()=>q('#conn').textContent='live';
  es.onerror=()=>{q('#conn').textContent='reconnecting…';};
  es.onmessage=ev=>{try{push(JSON.parse(ev.data));}catch(e){}};
}
async function state(){
  const r=await fetch('/api/state?token='+encodeURIComponent(tok)); if(!r.ok) return; const s=await r.json();
  q('#meta').innerHTML='channels: '+esc((s.channels||[]).join(', ')||'none')+'<br>agents: '+esc((s.agents||[]).join(', '))+'<br>entry: '+esc(s.entry_agent);
  q('#agents').innerHTML=(s.agent_details||[]).map(a=>{
    const flags=[a.isolated?'isolated':'',a.selectable?'selectable':'',a.stateful?'stateful':'',a.private_memory&&!a.isolated?'private-memory':''].filter(Boolean).join(' ');
    return '<div class=item><b>'+esc(a.name)+'</b> <span class=meta>'+esc(a.source)+' · '+esc(a.model)+'</span>'+(flags?'<br><span class=turn>'+esc(flags)+'</span>':'')+
      (a.routed_from.length?'<br><span class=meta>← '+esc(a.routed_from.join(', '))+'</span>':'')+(a.delegates.length?'<br><span class=meta>asks: '+esc(a.delegates.join(', '))+'</span>':'')+'</div>';}).join('')||'<span class=meta>none</span>';
  q('#tasks').innerHTML=(s.tasks||[]).slice(0,12).map(t=>'<div class=item><span class="s-'+t.status+'">'+t.status+'</span> ['+t.id+'] '+esc(t.title)+(t.progress&&t.progress.length?'<br><span class=meta>'+esc(t.progress[t.progress.length-1])+'</span>':'')+'</div>').join('')||'<span class=meta>none</span>';
  q('#sched').innerHTML=(s.schedules||[]).map(x=>'<div class=item>'+esc(x.summary)+'</div>').join('')||'<span class=meta>none</span>';
  const p=s.pairing||{pending:[],approved:[]};
  q('#pair').innerHTML=(p.pending.map(x=>'<div class=item>pending '+esc(x.channel)+':'+esc(x.sender_id)+' code <b>'+esc(x.code)+'</b></div>').join('')+
    p.approved.map(x=>'<div class=item class=meta>approved '+esc(x.channel)+':'+esc(x.sender_id)+'</div>').join(''))||'<span class=meta>none</span>';
}
q('#level').onchange=render; q('#filter').oninput=render;
q('#follow').onchange=e=>follow=e.target.checked;
q('#clear').onclick=()=>{rows=[];render();};
(async()=>{ if(await load()){stream(); state(); setInterval(state,4000);} })();
</script></body></html>"""
