"""Retrying a rejected request with fields the pipeline declared.

The version this replaces carried a table of one application's domain model — `schema_version:
"2.0.0"`, `role_name: "admin"`, `resource_type: "organization"` — plus special cases for that
application's workflow documents and the casing of its `effect` field. Against anything else it
patched nothing and cost a round trip to find that out.

The mechanism is worth keeping: a server that answers 422 usually says which field it wanted, and a
test that only omitted a required-but-uninteresting field should not fail for it. What it cannot do
is invent the value. So the values are declared, and a pipeline that declares none gets a plain
failure — which is the correct report for a request the target refused.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..errors import ExecError
from ..logging import log

# What a rejected-field message looks like. Overridable, because it is a convention of whichever
# framework serves the API rather than anything the HTTP spec settles.
DEFAULT_PATTERN = r"(\w+): Field required"


@dataclass
class Recovery:
    """Values to supply when the target says a field was required."""

    defaults: dict[str, Any] = field(default_factory=dict)
    pattern: str = DEFAULT_PATTERN

    @classmethod
    def load(cls, path: Path) -> Recovery:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ExecError(f"{path} should be a mapping with a `defaults:` block")
        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, dict) or not defaults:
            raise ExecError(f"{path} declares no `defaults:`; without values there is nothing to retry")
        return cls(defaults=dict(defaults), pattern=str(raw.get("pattern") or DEFAULT_PATTERN))

    def value_for(self, name: str, runtime_vars: dict[str, str]) -> Any:
        """`{random}` and `{SOME_VAR}` are expanded; everything else is used as written."""
        raw = self.defaults[name]
        if not isinstance(raw, str):
            return raw
        if raw == "{random}":
            return f"auto-{uuid.uuid4().hex[:8]}"
        match = re.fullmatch(r"\{([A-Z][A-Z0-9_]*)\}", raw)
        if match:
            return runtime_vars.get(match.group(1), raw)
        return raw

    def patch(self, body: dict[str, Any], message: str, runtime_vars: dict[str, str]) -> bool:
        """Fill in every field the message named that this pipeline has a value for."""
        patched = False
        for found in re.finditer(self.pattern, message):
            name = found.group(1)
            if name in body or name not in self.defaults:
                continue
            body[name] = self.value_for(name, runtime_vars)
            patched = True
            log.debug(f"      422 recovery: added {name}={json.dumps(body[name], default=str)}")
        return patched
