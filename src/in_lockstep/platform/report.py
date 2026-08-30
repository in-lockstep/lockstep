"""Rendering a review's outcome for the surface a human actually reads: the pull request.

A review whose findings die in a CI job log or an orphan-branch ledger is spend without value —
the PR page is the only place a contributor or a phone-bound maintainer looks. Every number this
renders already exists on the `Outcome`; this is the last mile that puts it where it is seen.

The marker is what makes a review comment *sticky*: an upsert keys off it to edit its own prior
comment rather than adding one per run, so a re-review updates in place instead of burying the
thread. It is an HTML comment, invisible in the rendered markdown.

The implement verb's own provenance travels the same way. Its change is opened in a privileged job
that never held the run's `Outcome`, so what that job can say about the change has to ride the
changeset artifact. `implement_body` renders the one piece that now does: the test verdict the
unprivileged job recorded when it ran the suite against the staged change (item 13).
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


#: The warning every implement PR body carries. The change came from a model that read untrusted
#: ticket text while holding write tools, and the reviewer has to know that before reading a line.
_UNTRUSTED_WARNING = (
    "The ticket body is untrusted input to a model that held write tools, so review this as you "
    "would a change from a stranger who had read your repository — the controls bound where it "
    "could write, not what it thought."
)


def implement_body(changeset: Any, verdict: Any) -> str:
    """The PR body for a change an implement run staged: the untrusted-input warning it must always
    carry, plus what the run's own test said about the change.

    `verdict` is a `TestVerdict` or None. None means no Test verb was bound, so the change arrives
    unverified and the body says exactly that rather than implying a green it never earned.
    """
    lines = [
        _UNTRUSTED_WARNING,
        "",
        f"**Tests:** {_verdict_line(verdict)}",
        "",
        marker("implement"),
    ]
    return "\n".join(lines)


def _verdict_line(verdict: Any) -> str:
    if verdict is None:
        return "not run — no test verb is bound, so this change is unverified."
    if not verdict.decided:
        return "ran, but collected nothing — neither red nor green."
    if verdict.green:
        extra = f", {verdict.skipped} skipped" if verdict.skipped else ""
        return f"✅ {verdict.passed} passed{extra}, run against the staged change before it was proposed."
    if not verdict.red:
        # ERRORED, and it must not render as the red branch below. That branch reads "0 of 0
        # failed", which is a sentence about the change; what happened is a sentence about the
        # runner, and a reviewer told the wrong one goes looking in the wrong place.
        return (
            f"⚠️ could not be run ({verdict.status}) — the runner did not start, so nothing here "
            f"is evidence about this change."
        )
    return (
        f"🛑 {verdict.failed} of {verdict.total} failed, run against the staged change before it "
        f"was proposed."
    )


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
