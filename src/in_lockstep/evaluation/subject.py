"""What a measurement is *of*.

The key decides which runs are comparable, and getting it wrong is not a small error: too coarse
and genuine behavioural differences are averaged into the noise floor, too fine and no subject
ever accumulates enough runs to have a floor at all.

Hence a content hash rather than a declared version. Someone editing a prompt without bumping its
version is not a hypothetical; it is the normal way a prompt gets edited.

The composed hash covers the *static* layer flatten — guardrails, body, skills, contexts — and not
the rendered prompt with the diff in it. Hashing the rendered prompt makes every subject N=1 and
no baseline can ever accumulate.

Skills are hashed separately because skill bodies load by progressive disclosure and are therefore
not in the composed text. Without that, editing a skill leaves the subject key unchanged and its
effect is measured as noise — the same failure the content hash was chosen to prevent, arriving
from the other side.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def _sha(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode())
        digest.update(b"\x00")
    return digest.hexdigest()


@dataclass(frozen=True)
class EvalSubject:
    verb: str
    strategy_id: str
    composed_prompt_sha256: str
    skillset_hash: str
    context_recipe_hash: str
    model_id: str
    # Carried for display and slicing, never for identity.
    prompt_id: str = ""
    prompt_version: str = ""

    @property
    def key(self) -> str:
        return _sha(
            self.verb,
            self.strategy_id,
            self.composed_prompt_sha256,
            self.skillset_hash,
            self.context_recipe_hash,
            self.model_id,
        )[:32]

    def label(self) -> str:
        """What a human reads in a report."""
        version = f"@{self.prompt_version}" if self.prompt_version else ""
        return f"{self.verb}/{self.strategy_id} {self.prompt_id}{version} on {self.model_id}"


def subject_for(
    *,
    verb: str,
    strategy_id: str,
    composed_prompt: str,
    skills: tuple[str, ...] = (),
    context_recipe: tuple[str, ...] = (),
    model_id: str,
    prompt_id: str = "",
    prompt_version: str = "",
) -> EvalSubject:
    return EvalSubject(
        verb=verb,
        strategy_id=strategy_id,
        composed_prompt_sha256=_sha(composed_prompt),
        skillset_hash=_sha(*skills),
        context_recipe_hash=_sha(*context_recipe),
        model_id=model_id,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
    )
