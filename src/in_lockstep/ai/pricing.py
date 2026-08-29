"""Model pricing.

An unpriced model is refused, not guessed at. The upstream tracker fell back to a default rate
for anything it did not recognise, which prices a local model at frontier rates and a frontier
model at whatever the default happened to be — and produces a number that looks like evidence.
The existing metering code already got this right for credits ("an unpriced model is named, never
treated as free"); this is the same discipline applied to tokens.

Rates are per million tokens, (input, output). Cache reads are priced separately where a provider
charges differently for them.

Where a rate declares no cache price, the input rate stands in — conservative, because a cache
read is cheaper than an input token wherever it is priced separately, and a ceiling that
under-estimates is not a ceiling. The resulting figure is therefore an upper bound rather than a
measurement, and `Cost.priced_fraction` is what keeps the difference visible: this module refuses
to guess a *model's* price, and reporting a partly-substituted total as though it were exact would
be the same fabrication one level down.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.outcome import Cost
from ..core.spend import Unpriced


@dataclass(frozen=True)
class Rate:
    input_per_m: float
    output_per_m: float
    cache_read_per_m: float | None = None
    cache_write_per_m: float | None = None


@dataclass
class CostTable:
    """Exact model ids only.

    Prefix matching is deliberately not offered: "claude-" matching every future Claude means the
    day a new tier ships at a different price, every run is silently mispriced and the ledger
    records it as fact.
    """

    rates: dict[str, Rate] = field(default_factory=dict)

    def add(self, model_id: str, rate: Rate) -> None:
        self.rates[model_id] = rate

    def knows(self, model_id: str) -> bool:
        return model_id in self.rates

    def rate_for(self, model_id: str) -> Rate:
        try:
            return self.rates[model_id]
        except KeyError:
            known = ", ".join(sorted(self.rates)) or "(empty)"
            raise Unpriced(
                f"no rate for model {model_id!r}. A run cannot be budgeted against a model whose "
                f"price is unknown, and defaulting the rate would record a fabricated cost. "
                f"Add it to the CostTable. Known: {known}"
            ) from None

    def price(
        self,
        model_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        wall_seconds: float = 0.0,
    ) -> Cost:
        rate = self.rate_for(model_id)
        usd = (input_tokens * rate.input_per_m + output_tokens * rate.output_per_m) / 1_000_000
        # Input and output always come from a declared rate: `rate_for` refuses otherwise.
        priced = input_tokens + output_tokens

        # Cache tokens are the one place a rate can be partial. Substituting the input rate is
        # conservative for a ceiling — a cache read is cheaper than an input token everywhere it
        # is priced separately, so this over-estimates rather than under — and a budget that
        # under-estimates is not a ceiling. But the resulting dollar figure is not a measurement,
        # and `priced_tokens` is what stops it being read as one.
        if cache_read_tokens:
            declared_read = rate.cache_read_per_m
            per_m = declared_read if declared_read is not None else rate.input_per_m
            usd += cache_read_tokens * per_m / 1_000_000
            priced += cache_read_tokens if declared_read is not None else 0
        if cache_write_tokens:
            declared_write = rate.cache_write_per_m
            per_m = declared_write if declared_write is not None else rate.input_per_m
            usd += cache_write_tokens * per_m / 1_000_000
            priced += cache_write_tokens if declared_write is not None else 0
        return Cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            usd=usd,
            wall_seconds=wall_seconds,
            priced_tokens=priced,
        )

    def project(self, model_id: str, *, input_tokens: int, max_output_tokens: int) -> Cost:
        """The pre-flight estimate.

        Output is bounded by the request's `max_tokens`, never by an expected value: a single turn
        that returns its full allowance would otherwise overshoot a ceiling checked against an
        average.
        """
        return self.price(model_id, input_tokens=input_tokens, output_tokens=max_output_tokens)


def default_table() -> CostTable:
    """Shipped rates. A repository overrides or extends this like any other binding."""
    table = CostTable()
    table.add("claude-sonnet-4-6", Rate(3.0, 15.0, cache_read_per_m=0.30, cache_write_per_m=3.75))
    table.add("claude-opus-4-6", Rate(15.0, 75.0, cache_read_per_m=1.50, cache_write_per_m=18.75))
    table.add("claude-haiku-4-5", Rate(0.80, 4.0, cache_read_per_m=0.08, cache_write_per_m=1.0))
    table.add("gemini-2.5-pro", Rate(1.25, 10.0))
    table.add("gemini-2.5-flash", Rate(0.15, 0.60))
    table.add("gemini-2.5-flash-lite", Rate(0.075, 0.30))
    return table
