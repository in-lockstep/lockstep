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

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

#: What `marker` writes, as its readers match it. One alphabet, defined once, beside the writer:
#: `comment` finds a body's own marker with this rather than with a pattern of its own, because
#: the copy it kept excluded `-` and so refused the body a hyphenated lens had written as carrying
#: no marker at all (#275). Exactly what `quote` emits -- alphanumerics, `_.-~`, the `:` that joins
#: kind to lens, and percent-escapes -- so the reader cannot disagree with the writer again.
MARKER = re.compile(r"<!-- in-lockstep:[A-Za-z0-9_.~:%-]+ -->")


def marker(kind: str) -> str:
    """The hidden anchor an upsert finds its own comment by. Stable per kind, so a security review
    edits the security comment and a performance review its own, side by side.

    Escaped, because the kind carries a lens name and the anchor is an HTML comment built by
    interpolation. A `-->` inside it closed the comment early: the rest rendered as visible text on
    the pull request, and the anchor the next run finds its own comment by was gone, so it posted a
    second comment beside the first -- the one thing `upsert_comment` exists to prevent (#275).
    The closed set `chatops.resolve_aspect` checks keeps such a name out of the command's paths;
    this keeps it out of the marker for whoever calls `review_comment` directly, because a defence
    that is only an ordering is one reordering away from gone.

    Percent-encoded rather than stripped, for two properties stripping lacks: it is injective, so
    two lenses cannot share one anchor and edit each other's comments; and it leaves every shipped
    kind byte-identical. `-` stays literal, so a hyphenated lens keeps the anchor its earlier
    comments already carry. What cannot survive is `<`, `>` and `!`, which is every character
    HTML needs to close, open or interrupt a comment.
    """
    return f"<!-- in-lockstep:{quote(kind, safe=':')} -->"


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

    Control refusals are split out for the same reason and were not (#256). A budget ceiling or an
    approval gate attaches a blocking ERROR-severity finding, and they rendered in the findings
    table indistinguishably from something the reviewer noticed about the code — so `blocked` read
    as `we found a serious problem` in the artefact people actually read.

    Which findings those are is decided by the outcome's **status**, not by a list of ids. A
    refusal is what a BLOCKED outcome carries, by construction, so a control added later is
    partitioned without anybody remembering to add it — the argument `test_sinks.py` makes about
    listing primitives rather than sinks, after an enumerated list of sinks had already missed
    five.
    """
    status = outcome.status.value
    decided = "" if outcome.decided else " · decided nothing"
    lines = [
        f"## in-lockstep review — {aspect}",
        "",
        f"**{status}**{decided} · {_cost_line(outcome.cost)}",
        "",
    ]

    rest = [f for f in outcome.findings if not f.id.startswith(("injection.", "review.not_reviewed"))]
    injections = [f for f in outcome.findings if f.id.startswith("injection.")]
    omitted = [f for f in outcome.findings if f.id == "review.not_reviewed"]
    refused = status == "blocked"
    findings = [] if refused else rest

    if refused:
        lines += ["### Refused before it could review", ""]
        lines += [f"- `{f.id}` — {_cell(f.message)}" for f in rest] or ["- (no reason recorded)"]
        lines += ["", "_A control stopped this run. Nothing here is a finding about the change._", ""]
    elif findings:
        lines += ["| | location | finding |", "|---|---|---|"]
        for f in findings:
            loc = f"{f.path}:{f.line}" if f.path and f.line else f.path
            where = _code(loc) if loc else ""
            lines.append(f"| {_icon(f)} | {where} | {_cell(f.message)} |")
        lines.append("")
    elif status == "succeeded" and outcome.decided:
        # A sentence only a review that actually read the diff can earn. It was gated on
        # `decided`, which defaults to True, so the killswitch path -- which builds a blocked
        # outcome carrying nothing -- posted "No findings." on the pull request (#256).
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
        *_cover_note(changeset),
        _UNTRUSTED_WARNING,
        "",
        f"**Tests:** {_verdict_line(verdict)}",
        "",
        marker("implement"),
    ]
    return "\n".join(lines)


def fix_body(changeset: Any, verdict: Any) -> str:
    """The PR body for a change a fix run staged: what it did, and what the suite said about it.

    Two sentences of provenance rather than `implement_body`'s one, because a fix arrives having
    already proved something — its reproducer was confirmed red and then green — and a reviewer who
    is not told that will go looking for the evidence. What matters is that the two claims stay
    distinct: the reproducer passing is a fact about the bug, and the verdict is a fact about the
    rest of the repository. A change can honestly have the first and fail the second, which is
    exactly the run this function was written after.
    """
    lines = [
        *_cover_note(changeset),
        "A reproducer for this bug was written, confirmed red, and this change makes it pass.",
        "",
        _UNTRUSTED_WARNING,
        "",
        f"**Tests:** {_verdict_line(verdict)}",
        "",
        marker("fix"),
    ]
    return "\n".join(lines)


def _cover_note(changeset: Any) -> list[str]:
    """The model's own account of the change, first: its summary as the opening paragraph and its
    notes as a list, or nothing when it wrote neither.

    First rather than after the provenance lines because a reviewer opens a pull request to learn
    what it does, and until #343 a framework-opened one never said. The two provenance sentences
    that follow are the framework's claims; these are the model's, which is why they are set under
    a heading that says whose they are — and why they stay after the untrusted-input warning in
    the reader's mind, if not on the page. Model prose, so the artifact masked it already, and a
    cover note that was not JSON arrives as up to a thousand characters of the reply, which is
    the bound on how long this paragraph can be.
    """
    summary = " ".join(str(getattr(changeset, "summary", "") or "").split())
    notes = [str(n).strip() for n in getattr(changeset, "notes", ()) or () if str(n).strip()]
    if not summary and not notes:
        return []
    lines: list[str] = []
    if summary:
        lines += [summary, ""]
    if notes:
        lines += ["**Notes from the run:**", *(f"- {note}" for note in notes), ""]
    return lines


def write_review_comments(directory: str | Path, outcomes: Mapping[str, Any]) -> tuple[Path, ...]:
    """One sticky-comment body per lens into `directory`, the shape `review --comment-out <dir>`
    writes and `comment --body-file <dir>` posts: `<lens>.md`, each ending in its own marker.
    Through the sink, like every write that leaves the process; the body carries a model's words
    about the diff it was given."""
    from ..privileged import sink

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for lens, outcome in outcomes.items():
        path = target / f"{lens}.md"
        sink.write_text_atomic(path, review_comment(lens, outcome))
        written.append(path)
    return tuple(written)


def _verdict_line(verdict: Any) -> str:
    if verdict is None:
        return "not run — no test verb is bound, so this change is unverified."
    if verdict.status == "blocked":
        # Before the `decided` branch, which would call this "collected nothing": nothing was
        # collected because nothing was run, and the reason is a control (#308), not the suite.
        return (
            "🚫 not run — the bound Test runner would have put the staged files on the host "
            "(sandbox.host_fallback), so this change is unverified."
        )
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


def scorecard_lines(scorecard: Any) -> list[str]:
    """The measurement as terminal lines: both arms over one denominator, then the verdict.

    Returned rather than printed, like `as_trend_text`, so the workflow that has the stream
    writes them. `—` where a column has no subject: a scorecard with no rubric has nothing
    outstanding to report, and `0` would read as "judged, and all passed".
    """
    before, after = scorecard.before, scorecard.after
    out = [f"measured  {before.measured} case(s), both arms"]
    for name, arm in (("before", before), ("after", after)):
        outstanding = f"{arm.outstanding} outstanding" if arm.outstanding else "—"
        judged = f", {arm.judged} judged" if getattr(arm, "judged", 0) else ""
        out.append(f"{name:<9} {arm.passed} passed, {arm.failed} failed, {outstanding}{judged}")
        for failure in arm.failures:
            out.append(f"          {failure.case}: {failure.check} {failure.detail}".rstrip())
    for case, why in scorecard.dropped:
        out.append(f"dropped   {case}: {why}")
    out.append(f"verdict   {scorecard.verdict}")
    return out


def _judged_by(before: Any, after: Any) -> str:
    """Who settled what. The deterministic checks are the grader's; a rubric is the judge's where
    one answered and the person's where none did, and the sentence says how many of each."""
    judged = getattr(before, "judged", 0) + getattr(after, "judged", 0)
    outstanding = before.outstanding + after.outstanding
    if judged and not outstanding:
        return (
            f"**Judged by:** the deterministic checks above were settled by the grader `eval run` "
            f"uses, and the {judged} rubric expectation(s) by a judge whose verdicts ride the run's "
            f"record; this pull request is still a person's to accept."
        )
    if judged:
        return (
            f"**Judged by:** the grader for the deterministic checks, a judge for {judged} rubric "
            f"expectation(s), and a person — this pull request — for the {outstanding} still `—`."
        )
    return (
        "**Judged by:** a person — this pull request. The deterministic checks above were settled "
        "by the grader `eval run` uses; no judge answered, so a rubric expectation is `—` "
        "rather than passed."
    )


def improve_body(scorecard: Any, rationale: str, *, run_id: str, label: str) -> str:
    """The pull-request body for a prompt proposal: the numbers, their denominators, who judged.

    Every figure carries the population it was counted over, and the judgement column says
    `—` where nobody judged: the deterministic checks were settled by a grader, the rubric
    checks by nobody, and the person reading this is the judge the loop declares.
    """
    before, after = scorecard.before, scorecard.after
    lines = [
        f"A prompt change to `{label}`, drafted by a model from the recorded cases the current "
        f"body fails, and measured against the promoted corpus before it was opened.",
        "",
        "| | measured | passed | failed | outstanding |",
        "|---|---|---|---|---|",
        f"| before | {before.measured} | {before.passed} | {before.failed} | {before.outstanding or '—'} |",
        f"| after | {after.measured} | {after.passed} | {after.failed} | {after.outstanding or '—'} |",
        "",
        f"**Verdict:** {scorecard.verdict}, over {len(scorecard.cases)} case(s) both arms answered"
        + (f"; {len(scorecard.dropped)} dropped" if scorecard.dropped else "")
        + ".",
        "",
        _judged_by(before, after),
        "",
        f"**Measurement run:** `{run_id}` — `in-lockstep history --explain {run_id}` prints its "
        "record, and the paid arm's inferences were recorded on that run's tape.",
        "",
        "**After merging:** `in-lockstep report --around <this pull request's number>` compares the "
        f"runs on `{label}` after the merge with the runs before it, each window with its run count "
        "and no verdict (`GATE-LEDGER-2`).",
    ]
    if rationale:
        lines += ["", "**The drafter's rationale**", "", rationale.strip()]
    if scorecard.dropped:
        lines += ["", "**Not measured**", ""]
        lines += [f"- `{case}`: {why}" for case, why in scorecard.dropped]
    lines += [
        "",
        "The body was written by a model reading recorded answers to somebody else's diffs. Read "
        "the whole diff as you would a change from a stranger; the controls bounded which file "
        "it could touch, not what it said.",
        "",
        marker("improve"),
    ]
    return "\n".join(lines)
