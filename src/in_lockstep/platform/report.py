"""Rendering a review's outcome for the surface a human actually reads: the pull request.

A review whose findings die in a CI job log or an orphan-branch ledger is spend without value —
the PR page is the only place a contributor or a phone-bound maintainer looks. Every number this
renders already exists on the `Outcome`; this is the last mile that puts it where it is seen.

The marker is what makes a review comment *sticky*: an upsert keys off it to edit its own prior
comment rather than adding one per run, so a re-review updates in place instead of burying the
thread. It is an HTML comment, invisible in the rendered markdown.

The implement verb's own provenance belongs in its PR body too, but that runs across the
trampoline's job split — the change is opened in a privileged job that never held the run's
`Outcome` — so it waits on the changeset artifact carrying that provenance, alongside item 13.
"""

from __future__ import annotations

from typing import Any


def marker(kind: str) -> str:
    """The hidden anchor an upsert finds its own comment by. Stable per kind, so a security review
    edits the security comment and a performance review its own, side by side."""
    return f"<!-- in-lockstep:{kind} -->"


def _cost_line(cost: Any) -> str:
    usd = f"${cost.usd:.4f}"
    tokens = f"{cost.input_tokens} in / {cost.output_tokens} out"
    billed = getattr(cost, "billed_fraction", None)
    if billed == 0:
        return f"{usd} — replayed, nothing billed · {tokens} tokens"
    return f"{usd} · {tokens} tokens"


def review_comment(aspect: str, outcome: Any) -> str:
    """A sticky PR comment for a review: verdict, cost, and the findings as a table.

    Injection signals are split out because they are a fact about the *change* (a diff that tried
    to talk to the reviewer), not a review finding about the code, and conflating the two would let
    a real security note hide in a list of style nits.
    """
    status = outcome.status.value
    decided = "" if outcome.decided else " · decided nothing"
    lines = [
        f"## in-lockstep review — {aspect}",
        "",
        f"**{status}**{decided} · {_cost_line(outcome.cost)}",
        "",
    ]

    findings = [f for f in outcome.findings if not f.id.startswith(("injection.", "review.not_reviewed"))]
    injections = [f for f in outcome.findings if f.id.startswith("injection.")]
    omitted = [f for f in outcome.findings if f.id == "review.not_reviewed"]

    if findings:
        lines += ["| | location | finding |", "|---|---|---|"]
        for f in findings:
            loc = f"{f.path}:{f.line}" if f.path and f.line else f.path
            where = _code(loc) if loc else ""
            lines.append(f"| {_icon(f)} | {where} | {_cell(f.message)} |")
        lines.append("")
    elif outcome.decided:
        lines += ["No findings.", ""]

    if injections:
        lines += ["### ⚠️ Prompt-injection signals in the diff", ""]
        lines += [f"- {_cell(f.message)}" for f in injections]
        lines.append("")

    if omitted:
        lines += ["_Not reviewed (too large to fit): " + ", ".join(_cell(f.path) for f in omitted) + "._", ""]

    lines.append(marker(f"review:{aspect}"))
    return "\n".join(lines)


def _icon(finding: Any) -> str:
    sev = getattr(finding.severity, "value", "")
    return {"error": "🛑", "warning": "⚠️", "note": "ℹ️"}.get(sev, "·")


def _cell(text: str) -> str:
    """One finding message, safe inside a markdown table cell: pipes escaped, newlines flattened,
    bounded so one pathological finding cannot blow out the comment."""
    flat = " ".join(str(text).split())
    return flat.replace("|", "\\|")[:300]


def _code(text: str) -> str:
    """A path or location as an inline code span, safe in a table cell. A path is untrusted model
    output too, and a pipe would split the row (GFM splits on `|` before parsing code spans) while
    a backtick would tear the span open — so both are removed/escaped, not just the message's."""
    inner = " ".join(str(text).split()).replace("`", "").replace("|", "\\|")
    return f"`{inner[:200]}`"
