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
from .core.verbs import SHIPPED_VERBS, capabilities_of, verb_of
from .lockstep import Lockstep
from .packs import Pack, PackError

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
        counted = _corpus_at(candidate, root)
        if counted is not None:
            return counted
    return None


def _corpus_at(directory: Path | None, base: Path | None) -> dict[str, Any] | None:
    """Count what a corpus directory holds, deterministic and rubric apart.

    Apart, because they are not the same evidence: a rubric nobody judged is outstanding rather
    than passed, and a single total would let a suite of unjudged rubrics read as measurement.
    """
    if directory is None or not directory.is_dir():
        return None

    from .evaluation import load_cases

    cases = load_cases(directory)
    if not cases:
        return None
    families: dict[str, int] = {}
    for case in cases:
        family = case.path.parent.parent.name if case.path else "?"
        families[family] = families.get(family, 0) + 1
    try:
        path = str(directory.relative_to(base)) if base is not None else str(directory)
    except ValueError:  # pragma: no cover - a corpus outside the subject's own tree
        path = str(directory)
    return {
        "path": path,
        "cases": len(cases),
        "deterministic": sum(1 for c in cases if c.deterministic),
        "rubric": sum(1 for c in cases if c.rubric),
        "families": dict(sorted(families.items())),
    }


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
        # Two counts over the same cases, not a partition of them: a case may carry both a
        # deterministic expectation and a rubric, and phrasing it as "A deterministic, B rubric"
        # invited the reading that they add up to the total.
        lines.append(
            f"  corpus      {corpus['cases']} case(s) in {corpus['path']} — "
            f"{corpus['deterministic']} with checks a machine settles, "
            f"{corpus['rubric']} needing a judge"
        )
    cassettes = receipt["cassettes"]
    lines.append(
        f"  cassettes   {len(cassettes)}"
        + (f"  ({', '.join(cassettes)})" if cassettes else "  — nothing replays offline")
    )

    lines += ["", f"digest        {receipt['digest']}"]
    return lines


# -- the other subject: a pack ---------------------------------------------------------


def receipt_for_pack(subject: Pack, *, load: bool = True) -> dict[str, Any]:
    """Derive the receipt for an installed extension pack.

    The order of operations is the security story rather than an implementation detail. `imports`
    is computed from the AST of the files the distribution recorded, before anything is imported —
    so a pack that reports `none` has been shown to be inert by a path that never ran it. Only
    then, and only when there is something to import, is the module loaded to see what it offers.

    `load=False` keeps it a pure metadata read at the cost of the `offers` list, which is the
    right trade when the caller has not decided to trust the pack yet.
    """
    imports = subject.imports()
    declared: dict[str, Any] | None = None
    problems: list[str] = []
    try:
        manifest = subject.manifest()
        declared = {"kind": manifest.kind, "summary": manifest.summary}
    except PackError as e:
        problems.append(str(e))

    offers: list[dict[str, Any]] = []
    imported = False
    if load and imports == "modules":
        try:
            offers = _offers(subject.module)
            imported = True
        except Exception as e:  # a pack that cannot be imported offers nothing, and says why
            problems.append(f"importing {subject.module!r} failed: {e}")

    receipt: dict[str, Any] = {
        "receipt": RECEIPT_VERSION,
        "subject": {
            "kind": "pack",
            "name": subject.name,
            "module": subject.module,
            "distribution": subject.distribution,
            "version": subject.version,
        },
        "requires": {"in-lockstep": __version__},
        "declares": declared,
        "imports": imports,
        "imported": imported,
        "offers": offers,
        "corpus": _corpus_at(subject.corpus(), subject.root),
        "cassettes": subject.cassettes(),
        "problems": problems,
    }
    receipt["kind_matches"] = _kind_matches(declared, offers, imports)
    receipt["digest"] = digest(receipt)
    return receipt


def _kind_matches(declared: dict[str, Any] | None, offers: list[dict[str, Any]], imports: str) -> bool | None:
    """Whether what a pack calls itself agrees with what it turned out to hold.

    Declared *and* cross-checked, which is the only arrangement that is honest about both halves:
    `kind` is an author's intent and a derivation cannot read intent, but an intent nothing checks
    is a claim. `None` where the answer is unknown — an unresolvable distribution, or a pack whose
    modules were not loaded — because a mismatch nobody could have detected is not a match.
    """
    if declared is None or imports == "unknown":
        return None
    kind = declared["kind"]
    kinds_offered = {offer["offers"] for offer in offers}
    if kind == "prompt":
        return imports == "none" or kinds_offered <= {"prompt"}
    if not offers:
        return None
    return kind in kinds_offered


def _offers(module_name: str) -> list[dict[str, Any]]:
    """What a pack's module exports that this framework recognises.

    Derived by walking the imported namespace rather than read from a manifest, for the reason the
    whole receipt exists: a list of classes an author wrote down is a claim, and a list of classes
    the interpreter found is a fact. Only names defined in the pack's own package count — a pack
    that imports `TDD` to subclass it is not offering `TDD`.

    Recognition goes through `core`'s vocabulary rather than through `AiStrategy`, and not only to
    keep this module out of `adapters`. "A verb is an interface; anything satisfying it can serve
    it" is the documented contract, so `verb` plus `capabilities` IS the interface — and a pack
    offering a deterministic adapter is described by the same code that describes an AI one, which
    an `AiStrategy` check would have silently skipped.
    """
    import importlib
    import inspect

    from .ai.prompt import Prompt

    module = importlib.import_module(module_name)
    top = module_name.split(".")[0]
    out: list[dict[str, Any]] = []
    for name, value in sorted(vars(module).items()):
        if not inspect.isclass(value) or getattr(value, "__module__", "").split(".")[0] != top:
            continue
        verb = verb_of(value)
        if verb is not None:
            request = getattr(value, "request", None)
            out.append(
                {
                    "name": name,
                    # A verb the framework does not ship is the thing a "verb" pack offers, and it
                    # is worth distinguishing here: such a pack owes a route, a price, a prompt and
                    # a corpus that a strategy for a shipped verb inherits.
                    "offers": "strategy" if verb.value in SHIPPED_VERBS else "verb",
                    "id": str(getattr(value, "id", "") or ""),
                    "verb": verb.value,
                    "request": getattr(request, "__name__", ""),
                    "capabilities": sorted(c.value for c in capabilities_of(value)),
                }
            )
        elif issubclass(value, Prompt):
            out.append({"name": name, "offers": "prompt", "body": value().body_label()})
    return out


def namespace_problems(receipt: dict[str, Any]) -> list[str]:
    """Ids that would land in a ledger under a name nothing ties back to this pack.

    A strategy's `id` is what an eval subject and a ledger record key on, so two packs shipping
    `implement/tdd` produce records that cannot be told apart afterwards. Reported rather than
    refused here, and an entry criterion for a published index — the same split the guardrail
    projection gets.
    """
    name = receipt["subject"]["name"]
    return [
        f"{offer['name']} declares id={offer['id']!r}, which is not namespaced by {name!r}"
        for offer in receipt["offers"]
        if offer["offers"] == "strategy" and offer["id"] and not offer["id"].startswith(f"{name}/")
    ]


def render_pack(receipt: dict[str, Any]) -> list[str]:
    """The human form of a pack receipt."""
    subject = receipt["subject"]
    declared = receipt["declares"]
    lines = [
        f"pack          {subject['name']}  {subject['version'] or '(version unknown)'}",
        f"distribution  {subject['distribution'] or '(unresolved)'}  module {subject['module']}",
        f"kind          {declared['kind'] if declared else '(undeclared)'}",
    ]
    if declared and declared["summary"]:
        lines.append(f"summary       {declared['summary']}")

    explain = {
        "none": "nothing importable — every module is a docstring, so installing it runs nothing",
        "modules": "importable code — installing puts it in your import graph",
        "unknown": "NOT CHECKED — the distribution could not be resolved to files",
    }[receipt["imports"]]
    lines += ["", f"imports       {receipt['imports']}  ({explain})"]
    if receipt["imports"] == "modules":
        lines.append(
            f"              {'imported to read what it offers' if receipt['imported'] else 'not imported'}"
        )

    match = receipt["kind_matches"]
    if match is False:
        lines.append(
            f"              <- what it offers does not match kind={declared['kind'] if declared else '?'}"
        )
    elif match is None:
        lines.append("              <- kind could not be checked against what it offers")

    lines += ["", "offers"]
    for offer in receipt["offers"] or []:
        if offer["offers"] == "strategy":
            lines.append(
                f"  {offer['name']:<24} strategy  {offer['request'] or '(no request)'}  "
                f"[{offer['verb']}]  id={offer['id'] or '(unset)'}"
            )
            lines.append(f"  {'':<24} {', '.join(offer['capabilities']) or '(declares none)'}")
        else:
            lines.append(f"  {offer['name']:<24} prompt    body:{offer['body']}")
    if not receipt["offers"]:
        lines.append("  (nothing importable to offer — resources only)")

    lines += ["", "evidence"]
    corpus = receipt["corpus"]
    if corpus is None:
        lines.append("  corpus      none — this pack ships no cases, so it cannot be measured")
    else:
        # Two counts over the same cases, not a partition of them: a case may carry both a
        # deterministic expectation and a rubric, and phrasing it as "A deterministic, B rubric"
        # invited the reading that they add up to the total.
        lines.append(
            f"  corpus      {corpus['cases']} case(s) in {corpus['path']} — "
            f"{corpus['deterministic']} with checks a machine settles, "
            f"{corpus['rubric']} needing a judge"
        )
    cassettes = receipt["cassettes"]
    lines.append(
        f"  cassettes   {len(cassettes)}"
        + (f"  ({', '.join(cassettes)})" if cassettes else "  — nothing replays offline")
    )

    problems = list(receipt["problems"]) + namespace_problems(receipt)
    if problems:
        lines += ["", "problems"]
        lines += [f"  {problem}" for problem in problems]

    lines += ["", f"digest        {receipt['digest']}"]
    return lines
