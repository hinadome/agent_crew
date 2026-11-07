# 03. LLM providers

Source: `providers/__init__.py`.

## Contract

- **PRV-1** A provider is a builder `ProviderBuilder = (ModelProfile) -> strands.models.Model`,
  registered under a string key in the `providers` registry.
- **PRV-2** `ModelFactory` resolves profile names to `Model` instances and caches them (one instance
  per profile name, shared across agents).
- **PRV-3** `ModelFactory.resolve(name=None)` returns:
  - a `ModelRouter` wrapping `[default, *fallback]` when `name` is the default **and** `fallback` is
    non-empty;
  - otherwise the single `Model` for `name` (or the default when `name` is `None`).
- **PRV-4** `ModelFactory.profile(name)` always returns a single concrete `Model` (never a router);
  used for the aux model and for delegate/sub-agent models.

## Built-in providers

| Key | Strands model | `client` keys | Notes |
|---|---|---|---|
| `bedrock` | `BedrockModel` | `region_name`, `boto_session`, `api_key`, ... | default; uses AWS credential chain; `aws login` works |
| `anthropic` | `AnthropicModel` | `api_key` (via `client_args`) | `params.max_tokens` is required by the API; builder defaults it to 4096 |
| `openai` | `OpenAIModel` | `api_key`, `base_url`, ... (via `client_args`) | also drives any OpenAI-compatible server (OpenRouter, vLLM, Groq, Together, LM Studio) |
| `ollama` | `OllamaModel` | `host` (default `http://localhost:11434`) | local models |
| `litellm` | `LiteLLMModel` | `client_args` | 100+ providers via prefixed `model_id`, e.g. `gemini/gemini-2.0-flash` |

- **PRV-5** `client` carries connection settings and `params` carries generation settings; the builder
  routes each to the right place for that SDK.
- **PRV-6** The `anthropic` builder moves `params.max_tokens` to the constructor argument and passes the
  remaining `params` through.

## Failover (PRV-7)

With `fallback` set, the default model becomes a Strands `ModelRouter` using the default
`FallbackStrategy`: ordered failover that prefers the candidates failing least, re-arming all
candidates after any success. Limits inherited from Strands:

- Routing applies to the main model call only. `Agent.structured_output()` bypasses it.
- A model that fails after streaming partial output emits that partial text before the replacement's
  full response.
- `agent.model` reports the first declared candidate; subsystems reading it (e.g. proactive
  compression sizing) reason about that model.

Recommendation: list candidates with comparable context windows.

## Switching models

| Mechanism | Scope | Source |
|---|---|---|
| `models.default` | process | config |
| `agents.<name>.model` | one agent | config |
| `/model <profile>` | one conversation, persisted | `gateway.py` + `conversation_settings` |
| `models.fallback` | automatic on error | router |
| `models.aux` | background chores | factory |

- **PRV-8** A per-conversation override is stored in `conversation_settings` and reapplied when the
  agent is rebuilt; conversation history is preserved across the switch.

## Adding a provider

```python
from kiro_crew.providers import providers

@providers.register("my-llm")
def build(profile):
    return MyModel(model_id=profile.model_id, **profile.client, **profile.params)
```

Reference it as `provider: my-llm` in a profile. See spec 10 for packaging as an entry point.

## Acceptance criteria

- Resolving the default with a `fallback` yields a `ModelRouter`; without one yields a single model *(verified)*.
- An agent whose primary model raises completes using the next candidate *(verified)*.
- Shared cached model instances behave correctly across multiple agents and routers *(verified)*.
- `/model` switch changes the model while keeping history *(verified with scripted models)*.
- Real Bedrock/OpenAI/Anthropic/Ollama/LiteLLM calls *(unverified — no credentials in the build env)*.
