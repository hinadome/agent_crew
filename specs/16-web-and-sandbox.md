# 16. Web tools and command sandbox

Source: `tools/web.py`, `sandbox/__init__.py`, `tools/sandbox_tools.py`.

## Web tools (tool group `web`)

`web_fetch(url)` is always provided. `web_search(query, max_results)` is provided only when
`tool_options.web.search` names a backend.

### Configuration (`tool_options.web`)

| Key | Meaning |
|---|---|
| `search` | `brave`, `tavily` or `searxng` (omit to disable `web_search`) |
| `api_key` | Brave subscription token / Tavily key |
| `url` | SearXNG base URL (JSON output must be enabled on the instance) |
| `allow_private` | default `false`; `true` permits private/loopback targets (SSRF risk) |

### `web_fetch` behaviour

- **WEB-1** Only `http` and `https` with a host.
- **WEB-2** **SSRF guard.** The hostname is resolved and **every** returned address must be globally routable
  (`ipaddress.is_global`, not multicast; IPv4-mapped IPv6 is unwrapped). Loopback, RFC 1918, link-local (including
  cloud metadata `169.254.169.254`), CGNAT `100.64/10`, `0.0.0.0` and IPv6 equivalents are blocked. Literal IPs are
  checked directly.
- **WEB-2b** **Anti-rebinding pinning.** Validation returns the vetted IP, and the request is made to **that IP**
  (via a custom transport) while the `Host` header and TLS SNI/verification keep the original hostname. Because the
  hostname is resolved once and the connection is pinned to the checked address, DNS cannot flip to an internal
  address between the check and the connect. Validation and pinning run per request, so every redirect hop is covered.
- **WEB-3** Redirects are followed manually (max 5) and **each hop is re-validated and IP-pinned** (see WEB-2b), so a
  public page cannot bounce the agent to an internal address, even across the redirect.
- **WEB-4** Limits: 15 s timeout, 2 MB body, 20,000 characters returned; only `text/*`, JSON and XML content types.
- **WEB-5** HTML is reduced to readable text (title kept; script/style/head/svg removed).
- **WEB-6** Output is prefixed with a notice that the content is **untrusted data**, to blunt prompt injection.
- **WEB-7** Failures are returned to the model as text (`Fetch failed: ...`); they do not raise.

### Search backends

| Backend | Request | Result fields |
|---|---|---|
| `brave` | `GET api.search.brave.com/res/v1/web/search?q&count`, header `X-Subscription-Token` | `web.results[].title/url/description` |
| `tavily` | `POST api.tavily.com/search`, `Authorization: Bearer` | `results[].title/url/content` |
| `searxng` | `GET <url>/search?q&format=json` | `results[].title/url/content` |

- **WEB-8** `max_results` is clamped to 1-10. Backend errors become `Search failed: <ErrorType>`.
- **WEB-9** Add a backend by registering a class in `search_backends`.

Defence in depth: DNS rebinding is mitigated by IP pinning (WEB-2b), but `allow_private: true` and the `host`
sandbox can still reach whatever the process can, so also restrict the process's outbound network (egress firewall,
no route to internal ranges) for sensitive environments.

## Command sandbox (tool group `sandbox`)

Tools: `run_shell(command)` (`sh -c`) and `run_python(code)` (`python3 -c`). Working directory is
`workspace/files`.

### Backends (`sandbox.backend`)

| Backend | Isolation |
|---|---|
| `docker` (default) | one throw-away container per command: `--rm`, `--network none` (unless `sandbox.network`), `--memory`, `--cpus`, `--pids-limit 128`, `--cap-drop ALL`, `no-new-privileges`, `--read-only` root, `/tmp` tmpfs 64 MB, runs as the host uid:gid, only `workspace/files` mounted at `/work` |
| `host` | **NONE.** Runs on the host in its own process group with a minimal environment and a timeout. Must be chosen explicitly; logs a warning on every run |
| `none` | tools refuse |

- **SBX-1** **Fail closed.** If the selected backend is unavailable (for example Docker not installed), the tool returns
  `Sandbox unavailable: ...` and **never falls back** to running on the host.
- **SBX-2** Timeout `sandbox.timeout_s` (default 60): the container is killed (`docker kill`) / the process group is
  killed; the result reports `exit code: 124 (timed out)`.
- **SBX-3** Output (stdout/stderr/exit code) is truncated to `sandbox.max_output_chars` (default 20,000).
- **SBX-4** Pair with `require_approval: [run_shell, run_python]` for human sign-off (spec 15). The sample config does this
  for the `coder` agent.
- **SBX-5** Add a backend by registering a `SandboxBackend` subclass in `sandbox_backends` (for example Firecracker,
  gVisor, or a remote runner).

Notes: the image must contain `sh` and `python3` for the respective tools (`python:3.12-slim` does). Files written to
`/work` persist in `workspace/files` and are visible to the `files` tools.

## Acceptance criteria

- SSRF: loopback, private, link-local, metadata, IPv4-mapped IPv6, CGNAT, non-http schemes and hostnames that resolve to
  any private address are blocked; redirects to private targets are blocked; public IPs allowed *(verified)*.
- Anti-rebinding: the connection dials the vetted IP with the hostname preserved in `Host`/SNI; a resolver that would
  flip to a private address on a second lookup never gets one; redirect hops are re-validated and pinned *(verified with a mock transport)*.
- HTML extraction, redirect following, redirect-loop limit, binary/HTTP-error rejection and size cap *(verified with a
  mock transport)*.
- Brave/Tavily/SearXNG request shape and parsing, and graceful failure *(verified with a mock transport; not against the
  live services)*.
- Docker command line contains every hardening flag *(verified by inspecting the generated command)*.
- Missing Docker never falls back to the host *(verified)*; host backend runs, captures output, times out and truncates
  *(verified)*.
- **Actually running a container** *(unverified: Docker is not available in the build environment)*.
