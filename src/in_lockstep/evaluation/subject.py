"""What a measurement is *of*.

The key decides which runs are comparable, and getting it wrong is not a small error: too coarse
and genuine behavioural differences are averaged into the noise floor, too fine and no subject
ever accumulates enough runs to have a floor at all.

Hence a content hash rather than a declared version. Someone editing a prompt without bumping its
version is not a hypothetical; it is the normal way a prompt gets edited.

The composed hash covers the *static* layer flatten — guardrails, body, skills, contexts — and not
the rendered prompt with the diff in it. Hashing the rendered prompt makes every subject N=1 and
no baseline can ever accumulate.

Skills are hashed separately, and the reason written here was wrong for as long as nothing called
this. It said skill bodies load by progressive disclosure and are therefore not in the composed
text. They are: `PromptLayers.trailing_texts` inlines every skill body verbatim, so editing one
already moves `composed_prompt_sha256`. The separate hash is belt and braces rather than the only
thing standing between a skill edit and a subject that does not notice it.

Kept anyway, and not as a hedge. It is what makes the property survive a composition that stops
inlining them — progressive disclosure is a real design this framework may yet adopt for large
skills, and on the day it does, the identity does not quietly get worse. What changed is the
claim, not the code: a comment asserting a mechanism that is not in force is the failure this
repository's gate ledger exists to catch, and this one sat in the module the ledger cites.
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
        """What a human reads in a report.

        `strategy_id` already carries the verb — the composition labels are `review/security`,
        `triage/analyst` — so joining the two rendered `review/review/security`. It read as a typo
        rather than as a bug for as long as nothing produced one, which is what a display-only
        field with no caller looks like.
        """
        version = f"@{self.prompt_version}" if self.prompt_version else ""
        strategy = (
            self.strategy_id
            if self.strategy_id.startswith(f"{self.verb}/")
            else (f"{self.verb}/{self.strategy_id}")
        )
        return f"{strategy} {self.prompt_id}{version} on {self.model_id}".replace("  ", " ").strip()


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
