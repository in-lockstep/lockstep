"""The standing instructions a repository writes for whatever agent works in it.

`AGENTS.md`, `CLAUDE.md`, `.cursorrules` — the file where a project records the things an agent
cannot infer from the code it happens to have open. `Lockstep.detect` has always found them and
`ls` has always listed them, and until now that was the whole of it: the framework reported
finding a CLAUDE.md and never opened it. A model implementing in this repository was told nothing
that file says.

That is not a cosmetic gap. This repository's own CLAUDE.md leads with `python_classes = ["*Tests"]`
— test classes must end in `Tests`, not begin with `Test` — precisely because a `Test*` class is
collected by nothing, the suite runs green having executed no new test, and a test-first run
refuses with `tdd.not_red`. It cost $21 the first time and $31 the second, and both runs were
working in a checkout containing the file that explains it.

WHY THIS IS A SYSTEM LAYER AND NOT CONTEXT. Standing conventions are instructions, not material:
they hold for the whole session rather than describing the task. `PromptLayers.contexts` places
them AFTER the framework's guardrails and the strategy body, which is the property that matters —
a repository can tell a model how its tests are named; it cannot displace the guardrail that says
what the model may not do.

WHY ONLY THE WRITING VERBS READ THEM. `reads_house_rules` is off by default and set on the
implement and fix bases alone. Those run from `issue_comment` and `issues` events, which GitHub
executes on the DEFAULT branch, so the file is the reviewed one. Review is a `pull_request` event,
where `actions/checkout` gives you the merge ref — the contributor's content. Reading instructions
out of that tree would let anyone who can open a pull request write text into the system prompt of
the model reviewing it, which is the injection this framework spends most of its design refusing.
The trust boundary is the checkout, so the flag is per-verb rather than global.
"""

from __future__ import annotations

from pathlib import Path

from ...core.context import AGENT_INSTRUCTION_FILES

#: A ceiling on one instruction file, in characters. Not a budget — the curator does budgeting for
#: context, and this is a system layer, which nothing curates. It is a bound on the damage a
#: checked-in file can do to every prompt in the repository: at four characters per token this is
#: roughly 8k tokens per file, and a project whose conventions do not fit in that has written a
#: manual rather than a set of rules. Truncated rather than dropped, because the top of such a file
#: is where the rules are and the bottom is where the appendices are.
MAX_INSTRUCTION_CHARS = 32_000


def house_rules(root: str | Path) -> tuple[tuple[str, str], ...]:
    """Every instruction file present, as `(name, text)` layers, in a fixed order.

    ALL of them, not the first match. `AGENTS.md` is the vendor-neutral spelling other clients
    read and `CLAUDE.md` is Claude Code's; a repository that supports both keeps both, and their
    contents are frequently not the same. Choosing a winner would drop half of what somebody
    deliberately wrote down, silently, in the direction of the model knowing less.

    Order is fixed rather than directory order so the composed prompt is deterministic — a cassette
    is keyed on the whole prompt, and a set that reordered itself would be a set that never
    replayed.

    Unreadable is not empty: a file that exists and cannot be decoded is skipped rather than
    guessed at, and skipping it costs the model one convention while failing the run over it would
    cost a person a working pipeline for a file that was probably a stray binary.
    """
    base = Path(root)
    layers: list[tuple[str, str]] = []
    for name in AGENT_INSTRUCTION_FILES:
        path = base / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip():
            continue
        if len(text) > MAX_INSTRUCTION_CHARS:
            text = text[:MAX_INSTRUCTION_CHARS] + f"\n\n[truncated at {MAX_INSTRUCTION_CHARS} characters]"
        # Named after the file, so the model can say which rule it is following and a reader of the
        # rendered prompt can see whose text it is. `PromptLayers` renders the name in the comment
        # marker above the block.
        layers.append((f"house-rules/{name}", text.strip()))
    return tuple(layers)
