# Vendored: `pipeline-framework/src/llm/`

**Origin:** `/Users/tpouyer/Projects/pipeline-framework`, commit `6ac3cde908b0b6230f06d33210d39ef352dafd20`
(GPG-signed, subtree clean at capture time).
**Direction:** one-way. There is no re-sync obligation and no upstream trust relationship after
import. Changes here are ordinary in-tree changes reviewed like any other code.

**Why vendored rather than depended on:** the package is unpublished. (An earlier draft claimed the
subtree was dirty — it was not; that described a different part of that repository.)

The recorded hashes below are of the **origin files**, as a provenance record. They are *not* a
drift gate: the async rewrite invalidates them by design, and `make fmt` would invalidate any hash
of the vendored copies. The vendored tree is excluded from `ruff format` and held to relaxed mypy
settings — the same treatment the extracted `pipeline_exec` executors had, before that package was
deleted; this is now the only code in the tree carrying it.

| Origin file | LOC | sha256 (origin) |
|---|---|---|
| `src/llm/__init__.py` | 26 | see `vendor.lock` |
| `src/llm/interface.py` | 43 | see `vendor.lock` |
| `src/llm/resolver.py` | 53 | **dropped — not vendored** |
| `src/llm/types.py` | 54 | see `vendor.lock` |
| `src/llm/providers/anthropic.py` | 76 | see `vendor.lock` |
| `src/llm/providers/bedrock.py` | 72 | see `vendor.lock` |
| `src/llm/providers/google_gemini.py` | 110 | see `vendor.lock` |
| `src/llm/providers/ollama.py` | 67 | see `vendor.lock` |
| `src/llm/providers/openai_compat.py` | 123 | see `vendor.lock` |
| `src/llm/providers/vertex_claude.py` | 126 | see `vendor.lock` |

## Deliberately not vendored

- **`resolver.py`** — `get_provider(config)` selects one provider per process from an ambient
  `config.llm_provider`, which makes the design's per-verb routing inexpressible. Replaced by
  `registry.ProviderRegistry`.
- **`utils/token_tracker.py`** — a module-global mutable singleton that accounts but never
  refuses, is not run-scoped, would cross-stamp branch labels under fan-out, and prices any
  unrecognised model at Sonnet rates via `DEFAULT_COST_PER_M`. Replaced by a run-scoped `Spend`.
- **`utils/retry.py`** — retried only `RateLimitError` and sat at the caller layer, composing
  multiplicatively with SDK retries. Replaced by a single `RetryPolicy`.

## Defects fixed on the way in

| # | Defect | Fix |
|---|---|---|
| 1 | 5 of 6 providers made blocking SDK calls inside `async def`, freezing the event loop | Async clients throughout (`AsyncAnthropic`, `AsyncAnthropicBedrock`, `AsyncAnthropicVertex`, `AsyncOpenAI`, `genai.Client.aio`, `httpx.AsyncClient`) |
| 2 | Error classification by substring: `"rate" in msg.lower()` matches "gene**rate**d" | `_errors.classify` — typed exceptions and HTTP status only |
| 3 | ~12 HTTP attempts per logical call (SDK 3 × `with_retry` 4), ~48 with middleware | `max_retries=0` on every SDK client; one retry layer |
| 4 | Gemini dropped `ToolDefinition.parameters`, so models got no argument schema | `parameters` passed to `FunctionDeclaration` |
| 5 | Ollama had no error mapping at all — its 429s were never retried | `classify` over `httpx.HTTPStatusError` |
| 6 | Ollama discarded tool structure and sent `role="tool_result"` verbatim (invalid) | Tool calls preserved both directions; role mapped to `"tool"` |
| 7 | `ContextLengthError` declared but never raised by anyone | Raised on a 400/413/422 with an explicit context-length signal; routes to repair, not retry |
| 8 | `ToolCall.id` empty (Ollama) or `f"gemini-{name}"` (Gemini), colliding when one tool is called twice in a turn | Index-qualified ids |
| 9 | Bedrock ignored config entirely (`AnthropicBedrock()` with no arguments), so Auth never saw its credentials | Region and credentials passed explicitly when supplied |
| 10 | Providers read credentials from ambient `Config`, leaving nothing for `Redact` to be seeded with | Constructor injection of `Credentials`; providers may not read `os.environ` (GATE-AUTH-1) |
| 11 | `TokenUsage` had no cache accounting, so any budget is wrong once prompt caching lands | `cache_read_tokens` / `cache_write_tokens` added at vendoring, before anything serialized the shape |
