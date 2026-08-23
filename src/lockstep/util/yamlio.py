"""Deterministic YAML emission.

The drift gate byte-compares committed output against a fresh compile, so emission must be stable
across runs and machines: no sorted keys (we choose the order), no anchors/aliases, literal block
scalars for multi-line `run:` bodies, and an unquoted top-level `on:` key.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_USES_SHA = re.compile(r"^(?P<prefix>\s*(?:- )?uses: \S+@)(?P<sha>[0-9a-f]{7,40})\s*$")


class _Dumper(yaml.SafeDumper):
    """SafeDumper that never emits anchors, so repeated sub-objects stay readable."""

    def ignore_aliases(self, data: Any) -> bool:  # noqa: ARG002
        return True


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _represent_str)


def dump(data: Any) -> str:
    """Dump a workflow mapping to YAML with our conventions applied."""
    text = yaml.dump(
        data,
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=False,
        width=10**9,  # never fold: a wrapped ${{ }} expression is unreadable and diff-noisy
        allow_unicode=True,
    )
    # PyYAML quotes the string key "on" because YAML 1.1 would read it as a boolean. GitHub reads
    # workflows as YAML 1.2 where it is plainly a string, and `'on':` is noise in every diff.
    return re.sub(r"^'on':", "on:", text, count=1, flags=re.MULTILINE)


def annotate_pins(text: str, sha_tags: dict[str, str]) -> str:
    """Append `  # <tag>` to every `uses: …@<sha>` line, so pins stay legible to reviewers."""
    out: list[str] = []
    for line in text.splitlines():
        match = _USES_SHA.match(line)
        if match:
            tag = sha_tags.get(match.group("sha"))
            if tag:
                line = f"{line}  # {tag}"
        out.append(line)
    return "\n".join(out) + "\n"


def with_header(text: str, header: list[str]) -> str:
    """Prepend provenance comment lines."""
    if not header:
        return text
    return "\n".join(f"# {line}" if line else "#" for line in header) + "\n" + text
