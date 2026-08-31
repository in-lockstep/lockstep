"""The receipt: what a configuration does, derived from it rather than described about it.

A marketplace listing is normally prose an author wrote about their own package, and prose is not
checkable. This framework has the parts to make it a computation instead: `capabilities` is a
load-bearing frozenset checked at class creation, `PromptLayers.projection()` says which guardrails
survived and in what order, the policy stack knows what it merged, and a corpus is files on disk.
Nothing here asks anybody what their code does. It reads what the objects already declare.

The subject today is **a repository** — `in-lockstep pack describe`, run against your own module,
where it answers "what did we actually configure" without reading a container by eye. That is
useful on its own and it is deliberately the same shape a pack's receipt will have
(`design/extension-packs.md` §3.2), so the format is exercised by the person who wrote the
configuration long before it is trusted about somebody else's.

Two properties matter for what comes later, and both are cheap to hold now:

**It is canonical.** A receipt exists to be compared — published against re-derived, before against
after — so the JSON is sorted and the digest is over exactly what is printed. A format that
serialises differently on two machines cannot be a comparison.

**It reports absence as absence.** A repository with no corpus gets `null`, never the framework's
own case count. Borrowing the shipped evidence into a repository's receipt would be the reassuring
number this project keeps refusing to compute.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__
from .ai.prompt import Inspectable
from .core.verbs import capabilities_of, verb_of
from .lockstep import Lockstep

#: Bumped when a field changes meaning. A consumer that cannot read this version must say so
#: rather than guess at the fields it recognises.
RECEIPT_VERSION = 1


def receipt_for(lockstep: Lockstep, *, root: Path) -> dict[str, Any]:
    """Derive the receipt for a configured `Lockstep`.

    Pure with respect to the model: no key, no network, no spend. Everything here is either
    declared on an object or a file this process can read, which is what makes a receipt something
    you can demand before trusting code rather than after running it.
    """
    receipt: dict[str, Any] = {
        "receipt": RECEIPT_VERSION,
        "subject": {
            "kind": "repository",
            "root": str(root),
            "config": lockstep.config_source or "none (detected defaults)",
        },
        "requires": {"in-lockstep": __version__},
        "bindings": _bindings(lockstep),
        "prompts": _prompts(lockstep),
        "policy": _policy(lockstep),
        "standards": list(getattr(lockstep, "standards", None) or []),
        "models": _models(lockstep),
        "egress": _egress(lockstep),
        "corpus": _corpus(root),
        "cassettes": _cassettes(root),
    }
    receipt["digest"] = digest(receipt)
    return receipt


def digest(receipt: dict[str, Any]) -> str:
    """A content hash over the canonical form, excluding any digest already present.

    Excluded rather than zeroed, because a receipt that hashes its own hash cannot be verified by
    recomputation — which is the only thing a digest is for here.
    """
    body = {k: v for k, v in receipt.items() if k != "digest"}
    return "sha256:" + hashlib.sha256(canonical(body).encode()).hexdigest()


def canonical(receipt: dict[str, Any]) -> str:
    """The one serialisation. Sorted keys, no incidental whitespace, newline-terminated."""
    return json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


# -- the derivations -----------------------------------------------------------------


def _bindings(lockstep: Lockstep) -> list[dict[str, Any]]:
    """What serves each interface, and what it admits to being able to do.

    `capabilities` is read off the bound object rather than the class, because that is where every
    gate reads it — `ApprovalGate`, the budget refusal and `Retry` all key on this set, so a
    receipt keyed on anything else would describe a different adapter than the one that runs.
    """
    out: list[dict[str, Any]] = []
    for binding in lockstep.container.resolved():
        impl = binding.impl
        verb = verb_of(impl)
        out.append(
            {
                "interface": binding.iface.__name__,
                "name": binding.name or "",
                "implementation": (impl if isinstance(impl, type) else type(impl)).__name__,
                "tier": binding.tier.name.lower(),
                "scope": binding.scope.value,
                "verb": verb.value if verb is not None else "",
                "capabilities": sorted(c.value for c in capabilities_of(impl)),
            }
        )
    return sorted(out, key=lambda b: (b["interface"], b["name"]))


def _prompts(lockstep: Lockstep) -> list[dict[str, Any]]:
    """Every prompt a bound AI adapter would compose, with the projection that says how.

    The projection is the field worth publishing: whether `guardrail:baseline` still leads is a
    question no README settles and this answers exactly. `guardrails_intact` states the answer
    rather than leaving a reader to scan the list — a flag, not a refusal, which is the same
    posture the standards layer takes about removal.
    """
    out: list[dict[str, Any]] = []
    for binding in lockstep.container.resolved():
        if isinstance(binding.impl, type) or not isinstance(binding.impl, Inspectable):
            continue
        for label, composed in binding.impl.compositions().items():
            projection = composed.projection()
            out.append(
                {
                    "label": label,
                    "source": composed.source,
                    "prompt": type(composed.prompt).__name__,
                    "version": composed.prompt.version,
                    "projection": projection,
                    "guardrails_intact": bool(projection) and projection[0] == "guardrail:baseline",
                }
            )
    return sorted(out, key=lambda p: (p["label"], p["source"]))


def _policy(lockstep: Lockstep) -> dict[str, Any]:
    """The contributed layers with their sources, and what they merged to.

    Both halves, because they answer different questions: the layers say who asked for what — a
    plugin's contribution carries `plugin:<name>` — and the resolution says what a run is actually
    held to. A receipt with only the second could not show that an organisation's floor applied.
    """
    resolved = lockstep.policy.resolve()
    return {
        "layers": [
            {"name": layer.name, "source": layer.source or "local"} for layer in lockstep.policy.layers
        ],
        "resolved": {
            "network": resolved.network,
            "scan_input": resolved.scan_input,
            "deny_tools": sorted(resolved.deny_tools),
            "max_turns": resolved.max_turns,
            "permissions": resolved.permissions,
        },
    }


def _models(lockstep: Lockstep) -> list[dict[str, Any]]:
    """Routes, and whether each is priced — the check `doctor` makes before a run spends.

    `priced` is `None` rather than `False` when the registry cannot be built at all: unknown is
    not the same as unpriced, and a receipt that flattened them would report a machine without
    credentials as a repository with a broken route.
    """
    routes = dict(getattr(lockstep.models, "routes", None) or {})
    if not routes:
        return []

    from .ai.pricing import CostTable

    priced: dict[str, bool | None] = {}
    try:
        from .ai.auth import Auth
        from .ai.bootstrap import Model, default_registry, table_for

        registry = default_registry(Auth())
        bound = lockstep.container.resolve(CostTable) if lockstep.container.has(CostTable) else None
        for verb, model_id in routes.items():
            selected = Model(model_id)
            if not selected.provider or selected.provider not in registry.names():
                priced[verb] = None
                continue
            priced[verb] = table_for(registry, selected, bound).knows(selected.name)
    except Exception:
        priced = dict.fromkeys(routes, None)

    return [
        {"verb": verb, "model": model_id, "priced": priced.get(verb)}
        for verb, model_id in sorted(routes.items())
    ]


def _egress(lockstep: Lockstep) -> list[str]:
    """The hosts a run may dial, the same list `egress-manifest` hands the proxy.

    Empty when the registry cannot be built, which is honest: this is what the *default* registry
    knows, and a module that registers its own provider through `invoker_factory=` is ahead of
    what any static read can see — the limit `doctor`'s route checks already state.
    """
    try:
        from .ai.auth import Auth
        from .ai.bootstrap import Model, default_registry
        from .privileged.egress import EgressPolicy

        registry = default_registry(Auth())
        policy = (
            lockstep.container.resolve(EgressPolicy)
            if lockstep.container.has(EgressPolicy)
            else EgressPolicy.detect()
        )
        routes = dict(getattr(lockstep.models, "routes", None) or {})
        if routes:
            endpoints = [
                registry.registration_for(selected).endpoint
                for selected in (Model(model_id) for model_id in routes.values())
                if selected.provider in registry.names()
            ]
        else:
            endpoints = list(registry.endpoints())
        return sorted(policy.manifest(endpoints))
    except Exception:
        return []


def _corpus(root: Path) -> dict[str, Any] | None:
    """This repository's own eval cases, or `None` — never the framework's.

    A repository with no corpus has no evidence, and reporting the shipped cases here would be
    counting somebody else's. `None` is the whole point of the field: it is what tells a reader
    that an approach bound in this module has never been measured.
    """
    for candidate in (root / "corpus", root / ".lockstep" / "corpus"):
        if not candidate.is_dir():
            continue
        from .evaluation import load_cases

        cases = load_cases(candidate)
        if not cases:
            continue
        families: dict[str, int] = {}
        for case in cases:
            family = case.path.parent.parent.name if case.path else "?"
            families[family] = families.get(family, 0) + 1
        return {
            "path": str(candidate.relative_to(root)),
            "cases": len(cases),
            "deterministic": sum(1 for c in cases if c.deterministic),
            "rubric": sum(1 for c in cases if c.rubric),
            "families": dict(sorted(families.items())),
        }
    return None


def _cassettes(root: Path) -> list[str]:
    """Recorded exchanges this repository can replay offline, by name.

    Counted because a cassette is what makes an approach measurable for nothing: it replays at the
    `LLMInput`/`LLMOutput` seam with no key and no spend. A configuration with none can be read
    but not exercised.
    """
    directory = root / ".lockstep" / "cassettes"
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


# -- rendering -----------------------------------------------------------------------


def render(receipt: dict[str, Any]) -> list[str]:
    """The human form. The JSON is the artifact; this is the one a person reads over."""
    subject = receipt["subject"]
    lines = [
        f"subject       {subject['kind']}  {subject['root']}",
        f"config        {subject['config']}",
        f"requires      in-lockstep {receipt['requires']['in-lockstep']}",
        "",
    ]

    lines.append("bindings")
    for binding in receipt["bindings"]:
        caps = ", ".join(binding["capabilities"]) or "(declares none)"
        verb = f"  [{binding['verb']}]" if binding["verb"] else ""
        lines.append(
            f"  {binding['interface']:<20} -> {binding['implementation']:<18} ({binding['tier']}){verb}"
        )
        lines.append(f"  {'':<20}    {caps}")

    if receipt["prompts"]:
        lines += ["", "prompts"]
        for prompt in receipt["prompts"]:
            flag = "" if prompt["guardrails_intact"] else "   <- baseline does not lead"
            lines.append(
                f"  {prompt['label']:<28} {prompt['prompt']} v{prompt['version']} <- {prompt['source']}{flag}"
            )
            lines.append(f"  {'':<28} {', '.join(prompt['projection'])}")

    lines += ["", "policy"]
    for layer in receipt["policy"]["layers"] or [{"name": "(nothing contributed)", "source": ""}]:
        source = f"  <- {layer['source']}" if layer["source"] else ""
        lines.append(f"  {layer['name']}{source}")
    resolved = receipt["policy"]["resolved"]
    lines.append(
        f"  = network={resolved['network'] or '(unset)'} scan={resolved['scan_input'] or '(unset)'} "
        f"deny_tools={len(resolved['deny_tools'])} max_turns={resolved['max_turns']}"
    )

    lines += ["", "standards"]
    for label in receipt["standards"] or ["(none installed)"]:
        lines.append(f"  {label}")

    if receipt["models"]:
        lines += ["", "models"]
        for route in receipt["models"]:
            state = {True: "priced", False: "UNPRICED", None: "unknown"}[route["priced"]]
            lines.append(f"  {route['verb']:<12} {route['model']:<44} {state}")

    lines += ["", "egress"]
    for host in receipt["egress"] or ["(none derivable from the default registry)"]:
        lines.append(f"  {host}")

    lines += ["", "evidence"]
    corpus = receipt["corpus"]
    if corpus is None:
        # Said in words, because this is the field most worth noticing and a bare `0` reads as a
        # measurement rather than as its absence.
        lines.append("  corpus      none in this repository — nothing bound here has been measured")
    else:
        lines.append(
            f"  corpus      {corpus['cases']} case(s) in {corpus['path']} "
            f"({corpus['deterministic']} deterministic, {corpus['rubric']} rubric)"
        )
    cassettes = receipt["cassettes"]
    lines.append(
        f"  cassettes   {len(cassettes)}"
        + (f"  ({', '.join(cassettes)})" if cassettes else "  — nothing replays offline")
    )

    lines += ["", f"digest        {receipt['digest']}"]
    return lines
