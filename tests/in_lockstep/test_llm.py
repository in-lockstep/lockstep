"""Gates over the transport layer.

These are structural assertions, deliberately: they hold without network, keys, or SDKs installed,
which is what lets them run in CI from day one. The behavioural gates (GATE-ASYNC-2/4,
GATE-RETRY-1/4/6) need a stub provider and land with the invoker in Phase 2.
"""

from __future__ import annotations

import ast
import inspect
import typing
from pathlib import Path

import pytest

from in_lockstep.llm import (
    ContextLengthError,
    Credentials,
    DataPolicy,
    LLMInput,
    LLMProvider,
    Model,
    ProviderRegistrationError,
    ProviderRegistry,
    ProviderSettings,
    RateLimitError,
    SecretStr,
    TokenUsage,
    TransientError,
)
from in_lockstep.llm._errors import classify

LLM_ROOT = Path(__file__).resolve().parents[2] / "src" / "in_lockstep" / "llm"
PROVIDERS = sorted((LLM_ROOT / "providers").glob("*.py"))


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


@pytest.mark.parametrize("path", PROVIDERS, ids=[p.stem for p in PROVIDERS])
def test_gate_async_1_every_client_is_async(path: Path) -> None:
    """GATE-ASYNC-1 — no provider may construct a synchronous client.

    Five of six upstream providers called a blocking SDK client inside `async def`, which freezes
    the event loop: `max_parallel` serialises, KillSwitch is unreachable mid-call, and asyncio
    cannot cancel a thread-blocking call at all.
    """
    tree = ast.parse(path.read_text())
    sync_clients = {"Anthropic", "AnthropicBedrock", "AnthropicVertex", "OpenAI", "Client"}
    offenders = [
        _callee(c) for c in _calls(tree) if _callee(c) in sync_clients and not _callee(c).startswith("Async")
    ]
    # genai.Client is the one legitimate sync constructor: the async surface hangs off `.aio`.
    if path.stem == "google_gemini":
        offenders = [o for o in offenders if o != "Client"]
    assert not offenders, f"{path.name} constructs synchronous client(s): {offenders}"


# SDK clients whose constructor takes a retry count. These are the ones that silently add
# attempts underneath us: both anthropic and openai default to DEFAULT_MAX_RETRIES = 2.
RETRY_CAPABLE = {"AsyncAnthropic", "AsyncAnthropicBedrock", "AsyncAnthropicVertex", "AsyncOpenAI"}


@pytest.mark.parametrize("path", PROVIDERS, ids=[p.stem for p in PROVIDERS])
def test_gate_retry_2_sdk_retries_disabled(path: Path) -> None:
    """GATE-RETRY-2 — one retry layer only.

    Both SDKs default to DEFAULT_MAX_RETRIES = 2. Composed with the upstream with_retry(3) that
    reached ~12 HTTP attempts per logical call, and ~48 with a Retry middleware on top — per
    tool-loop turn, so a 20-turn agent could reach ~240 requests.
    """
    tree = ast.parse(path.read_text())
    constructions = [c for c in _calls(tree) if _callee(c) in RETRY_CAPABLE]
    if not constructions:
        pytest.skip(f"{path.name} constructs no retry-capable SDK client")
    for call in constructions:
        kwargs = {kw.arg for kw in call.keywords if kw.arg}
        starred = any(kw.arg is None for kw in call.keywords)
        if starred:
            # Constructed from a kwargs dict built above; assert on the dict literal instead.
            flat = path.read_text().replace(" ", "").replace("\n", "")
            assert '"max_retries":0' in flat, f"{path.name} builds client kwargs without max_retries=0"
            continue
        assert "max_retries" in kwargs, f"{path.name} constructs {_callee(call)} without max_retries"


def test_gate_retry_2_with_retry_is_not_imported() -> None:
    """GATE-RETRY-2 — one retry layer, not two.

    A caller-layer retry helper composing with SDK retries is how one logical call becomes ~12
    HTTP attempts, and ~48 once middleware retries too. `RetryPolicy` is the only layer, and every
    SDK client is constructed with max_retries=0.
    """
    for path in LLM_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "with_retry" not in [a.name for a in node.names], f"{path} imports it"
            if isinstance(node, ast.Call) and _callee(node) == "with_retry":
                pytest.fail(f"{path} calls with_retry")


def test_gate_cost_4_no_default_cost_table() -> None:
    """GATE-COST-4 — an unpriced model must be BLOCKED, never silently priced as Sonnet."""
    root = LLM_ROOT.parents[1]
    for path in root.rglob("*.py"):
        assert "DEFAULT_COST_PER_M" not in path.read_text(), f"{path} carries DEFAULT_COST_PER_M"


@pytest.mark.parametrize("path", PROVIDERS, ids=[p.stem for p in PROVIDERS])
def test_gate_auth_1_providers_never_read_the_environment(path: Path) -> None:
    """GATE-AUTH-1 — credentials arrive through the constructor, or Redact cannot be seeded.

    An env-scraping fallback structurally cannot see an OIDC- or vault-derived short-lived token
    that was never in the environment — which is exactly what §5.2 prefers.
    """
    source = path.read_text()
    for forbidden in ("os.environ", "getenv", "load_dotenv"):
        assert forbidden not in source, f"{path.name} reads the environment via {forbidden}"


def test_gate_retry_3_generated_is_not_a_rate_limit() -> None:
    """GATE-RETRY-3 — the exact upstream defect: `"rate" in msg.lower()` matches "gene*rate*d"."""

    class Boom(Exception):
        status_code = 400

    err = classify(Boom("the model generated an invalid response"), provider="anthropic")
    assert err is not None
    assert not isinstance(err, RateLimitError), "a 400 mentioning 'generated' is not a rate limit"
    assert not err.retryable, "a 400 content error must not be retried"


def test_classification_is_by_status_not_text() -> None:
    class Rated(Exception):
        status_code = 429

    class Broke(Exception):
        status_code = 503

    assert isinstance(classify(Rated("slow down"), provider="p"), RateLimitError)
    transient = classify(Broke("upstream unavailable"), provider="p")
    assert isinstance(transient, TransientError)
    assert transient.retryable, "5xx is the ERRORED class Retry targets"


def test_context_length_error_is_actually_raised() -> None:
    """Declared upstream, never raised by anyone. It must not be retryable."""

    class TooLong(Exception):
        status_code = 400

    err = classify(TooLong("prompt is too long: 300000 tokens > maximum context"), provider="p")
    assert isinstance(err, ContextLengthError)
    assert not err.retryable, "re-sending an over-long prompt fails again by construction"


def test_unrecognised_error_is_reraised_not_guessed() -> None:
    assert classify(ValueError("something local went wrong"), provider="p") is None


def test_secret_str_does_not_render_itself() -> None:
    secret = SecretStr("sk-ant-supersecret")
    assert "supersecret" not in str(secret)
    assert "supersecret" not in repr(secret)
    assert "supersecret" not in f"{secret}"
    assert secret.reveal() == "sk-ant-supersecret"


def test_credentials_expose_exactly_what_redact_is_seeded_with() -> None:
    creds = Credentials(values={"api_key": SecretStr("abc123")}, source="env:TEST")
    assert creds.secret_values() == frozenset({"abc123"})
    assert creds.get("api_key") == "abc123"
    assert Credentials.none().secret_values() == frozenset()


def test_token_usage_carries_cache_accounting() -> None:
    """Cache accounting is part of the shape: this type serializes into checkpoints and the
    ledger (§4.2), so adding a field later changes a persisted layout.
    """
    usage = TokenUsage(input_tokens=10, output_tokens=5, cache_read_tokens=100)
    assert usage.total_tokens == 15
    assert usage.cache_read_tokens == 100
    assert usage.cache_write_tokens == 0


def test_generate_signature_is_preserved_byte_identically() -> None:
    """The substitution this pivot committed to. Changing it would break the whole premise."""
    from in_lockstep.llm.types import LLMOutput

    sig = inspect.signature(LLMProvider.generate)
    assert list(sig.parameters) == ["self", "input"]
    # `from __future__ import annotations` makes these strings; resolve them properly.
    hints = typing.get_type_hints(LLMProvider.generate)
    assert hints["input"] is LLMInput
    assert hints["return"] is LLMOutput


class _Stub(LLMProvider):
    def __init__(self, settings: ProviderSettings, creds: Credentials) -> None:
        self.settings = settings
        self.creds = creds

    async def generate(self, input: LLMInput):  # type: ignore[override]
        raise NotImplementedError

    def name(self) -> str:
        return "stub"

    def base_url(self) -> str:
        return self.settings.base_url


def _registry(endpoint: str = "https://api.example.test") -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        "acme",
        _Stub,
        settings=ProviderSettings(base_url=endpoint),
        data_policy=DataPolicy.EXTERNAL,
        endpoint=endpoint,
    )
    return registry


def test_registry_routes_per_model_not_per_process() -> None:
    """The R5-STAFF-1 defect: get_provider() bound ONE provider for the whole process."""
    registry = _registry()
    registry.register(
        "local",
        _Stub,
        settings=ProviderSettings(base_url="http://localhost:1234/v1"),
        data_policy=DataPolicy.INTERNAL,
        endpoint="http://localhost:1234/v1",
    )
    cheap = registry.provider_for(Model("local:qwen3-8b"))
    smart = registry.provider_for(Model("acme:big-model"))
    assert cheap is not smart, "two verbs must be able to route to two providers in one process"
    assert registry.data_policy_for(Model("local:qwen3-8b")) is DataPolicy.INTERNAL
    assert registry.data_policy_for(Model("acme:big-model")) is DataPolicy.EXTERNAL


def test_residency_keys_on_destination_not_provider_class() -> None:
    """Two registrations of the SAME class with different endpoints get different policies."""
    registry = ProviderRegistry()
    registry.register(
        "local-compat",
        _Stub,
        settings=ProviderSettings(base_url="http://localhost:1234/v1"),
        data_policy=DataPolicy.INTERNAL,
        endpoint="http://localhost:1234/v1",
    )
    registry.register(
        "hosted-compat",
        _Stub,
        settings=ProviderSettings(base_url="https://api.example.test/v1"),
        data_policy=DataPolicy.EXTERNAL,
        endpoint="https://api.example.test/v1",
    )
    assert registry.data_policy_for(Model("local-compat:m")) is DataPolicy.INTERNAL
    assert registry.data_policy_for(Model("hosted-compat:m")) is DataPolicy.EXTERNAL


def test_gate_auth_2_endpoint_must_match_what_the_client_dials() -> None:
    """GATE-AUTH-2 — a declared endpoint that diverges from the destination is a lie."""
    registry = ProviderRegistry()
    registry.register(
        "drifted",
        _Stub,
        settings=ProviderSettings(base_url="https://elsewhere.test"),
        data_policy=DataPolicy.INTERNAL,
        endpoint="http://localhost:1234/v1",
    )
    with pytest.raises(ProviderRegistrationError, match="dials"):
        registry.provider_for(Model("drifted:m"))


def test_unqualified_model_id_is_refused() -> None:
    with pytest.raises(ProviderRegistrationError, match="unqualified"):
        _registry().provider_for(Model("just-a-name"))


def test_unknown_provider_names_what_is_registered() -> None:
    with pytest.raises(ProviderRegistrationError, match="acme"):
        _registry().provider_for(Model("nope:m"))


def test_an_anthropic_workspace_id_travels_as_a_header_not_a_credential() -> None:
    """An identity-linked key acts in a workspace, and the API 400s without the id.

    It goes in `ProviderSettings.extra` rather than `Credentials` deliberately: `Credentials`
    seeds `Redact`, so putting an identifier there would mask the workspace id out of the very
    error messages that name it.
    """
    from in_lockstep.llm.interface import Credentials, ProviderSettings, SecretStr
    from in_lockstep.llm.providers.anthropic import AnthropicProvider

    captured: dict = {}

    class Fake(AnthropicProvider):
        def _make_client(self, settings, creds):
            captured["headers"] = {
                n: v for n, v in settings.extra.items() if n.startswith("anthropic-") and v
            }
            captured["secrets"] = creds.secret_values()
            return object()

    Fake(
        ProviderSettings(extra={"anthropic-workspace-id": "wrk_123"}),
        Credentials(values={"api_key": SecretStr("sk-test-value-long-enough")}),
    )
    assert captured["headers"] == {"anthropic-workspace-id": "wrk_123"}
    assert "wrk_123" not in captured["secrets"], "an identifier must not be seeded into Redact"


def test_no_workspace_id_sends_no_header() -> None:
    """A key that is not identity-linked must not be handed an empty header."""
    import os

    from in_lockstep.ai.bootstrap import default_registry
    from in_lockstep.llm.registry import Model

    had = os.environ.pop("ANTHROPIC_WORKSPACE_ID", None)
    try:
        registration = default_registry().registration_for(Model("anthropic:claude-haiku-4-5"))
        assert registration.settings.extra == {}
    finally:
        if had is not None:
            os.environ["ANTHROPIC_WORKSPACE_ID"] = had
