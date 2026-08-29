"""Scanning untrusted content for planted instructions.

Ported from the compiler-era runtime, where it ran as a pre-agent step over files an agent was
about to read. It runs in two places now: over untrusted context items before the first model
call, and over every tool result before it re-enters the loop — because a tool result is content
that arrived after the package was assembled, and a `git log` carries whatever anyone wrote in a
commit message.

The original's framing is worth keeping verbatim: this is not a safety filter and it is not a
substitute for the constraints that hold whatever the model decides to do — read-only tool sets,
deny lists, egress rules. Those are what actually bound a successful injection. This closes the
gap between "we told it not to" and "we checked", and reports what it found so a human can look.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

OVERRIDE = "instruction_override"
EXFIL = "credential_exfil"
HIDDEN = "hidden_content"
UNICODE_TRICK = "unicode"

CRITICAL, HIGH, MEDIUM = "critical", "high", "medium"


@dataclass(frozen=True)
class Pattern:
    name: str
    category: str
    severity: str
    regex: re.Pattern[str]


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        "ignore_previous",
        OVERRIDE,
        CRITICAL,
        re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?\b"),
    ),
    Pattern(
        "disregard_above",
        OVERRIDE,
        CRITICAL,
        re.compile(r"(?i)\bdisregard\s+(?:everything|all|the)\s+(?:above|previous)\b"),
    ),
    Pattern(
        "new_instructions", OVERRIDE, CRITICAL, re.compile(r"(?i)\bnew\s+(?:system\s+)?instructions?\s*:")
    ),
    Pattern("you_are_now", OVERRIDE, HIGH, re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an|the)\b")),
    Pattern("system_prompt_marker", OVERRIDE, HIGH, re.compile(r"(?i)<\s*/?\s*(?:system|assistant)\s*>")),
    Pattern("end_of_context", OVERRIDE, HIGH, re.compile(r"(?i)\bend\s+of\s+(?:context|document|input)\b")),
    Pattern(
        "exfil_env_file",
        EXFIL,
        CRITICAL,
        re.compile(r"(?i)\b(?:cat|read|print|send|post|upload)\b[^\n]{0,40}\.env\b"),
    ),
    Pattern("exfil_ssh", EXFIL, CRITICAL, re.compile(r"(?i)(?:\.ssh/|id_rsa|id_ed25519|authorized_keys)")),
    Pattern(
        "exfil_cloud_creds",
        EXFIL,
        CRITICAL,
        re.compile(r"(?i)(?:~/\.aws|\.aws/credentials|/etc/shadow|\.netrc)"),
    ),
    Pattern(
        "exfil_token_names",
        EXFIL,
        HIGH,
        re.compile(r"(?i)\b(?:GITHUB_TOKEN|ANTHROPIC_API_KEY|OPENAI_API_KEY|AWS_SECRET)\b"),
    ),
    Pattern("html_comment", HIDDEN, MEDIUM, re.compile(r"<!--.*?-->", re.DOTALL)),
)

# Characters that render as nothing but carry payload.
_INVISIBLE = {
    "​",
    "‌",
    "‍",
    "⁠",
    "﻿",
    "‪",
    "‫",
    "‬",
    "‭",
    "‮",
}


@dataclass(frozen=True)
class Finding:
    name: str
    category: str
    severity: str
    excerpt: str
    offset: int

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "category": self.category,
            "severity": self.severity,
            "excerpt": self.excerpt,
            "offset": self.offset,
        }


def strip_invisible(text: str) -> str:
    return "".join(c for c in text if c not in _INVISIBLE and unicodedata.category(c) not in ("Cf",))


def scan(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(text):
            excerpt = match.group(0)[:120].replace("\n", " ")
            findings.append(
                Finding(
                    name=pattern.name,
                    category=pattern.category,
                    severity=pattern.severity,
                    excerpt=excerpt,
                    offset=match.start(),
                )
            )
    stripped = strip_invisible(text)
    if stripped != text:
        findings.append(
            Finding(
                name="invisible_characters",
                category=UNICODE_TRICK,
                severity=HIGH,
                excerpt=f"{len(text) - len(stripped)} invisible character(s) removed",
                offset=0,
            )
        )
    return findings


def summarize(findings: list[Finding]) -> dict[str, object]:
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for finding in findings:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        by_category[finding.category] = by_category.get(finding.category, 0) + 1
    return {
        "count": len(findings),
        "by_severity": by_severity,
        "by_category": by_category,
        "worst": (
            CRITICAL
            if by_severity.get(CRITICAL)
            else HIGH
            if by_severity.get(HIGH)
            else MEDIUM
            if by_severity.get(MEDIUM)
            else ""
        ),
    }
