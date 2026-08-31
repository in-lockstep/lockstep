"""Extension packs: a distribution that OFFERS, and never applies itself.

There are two entry-point groups now, and the difference between them is the whole design.

`in_lockstep.standards` applies itself inside `Lockstep.detect()`. That is right for standards:
they can only tighten, so the real risk is a repository forgetting one, and installing the package
IS applying it.

`in_lockstep.extensions` — this group — does discovery and nothing else. A strategy hands a model
write and execute tools and pays for a model call, so its arrival must be a diff somebody read.
Nothing here binds anything. `installed()` lists what is available; a line in `.lockstep/lockstep.py`
is what puts it in force, and that file is loaded from a trusted ref, which is what keeps a
strategy from being selectable by a string a ticket body could eventually reach.

Two properties are worth knowing before reading the code.

**Listing does not import.** `installed()` reads entry-point metadata, and every resource this
module reaches — `pack.toml`, corpus cases, cassettes, the AST behind `imports` — is located
through the distribution's own file list rather than through `importlib.resources`, which would
import the package to answer. So `in-lockstep pack ls` runs a stranger's package through no code
path at all, and `pack describe` imports only when it has already reported that there is something
to import.

**A broken pack is quiet, where a broken standard is loud.** `load_standards` raises, because
running without standards somebody installed is the silently-dropped control this framework exists
to refuse. A pack that fails to parse has applied nothing — nothing is in force until a bind line
exists — so the failure belongs beside the pack in a listing, not in an exception that hides every
other pack behind it.
"""

from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ai.prompt import Body, parse_frontmatter

GROUP = "in_lockstep.extensions"

#: What a pack may call itself. Checked against what `pack describe` derives, so a declaration
#: that disagrees with the code is reported rather than believed.
KINDS = ("prompt", "strategy", "verb")

#: Every key `pack.toml` may carry. Anything else is refused — the file names a pack and says what
#: kind of thing it is, and the moment it can carry a binding, a policy or a model route it has
#: become the alternate configuration surface this framework does not have. `Frontmatter` says the
#: same thing about prompt bodies, in the same words, for the same reason.
MANIFEST_KEYS = frozenset({"kind", "summary"})


class PackError(RuntimeError):
    """A pack's declaration cannot mean what it says."""


class PackNotFound(LookupError):
    """No installed pack answers to that name."""


@dataclass(frozen=True)
class Manifest:
    """What `pack.toml` declares. Two fields, neither of which changes what runs."""

    kind: str
    summary: str = ""


@dataclass(frozen=True)
class Pack:
    """An installed extension pack, addressed by its entry-point name.

    Holds no imported module. `module` is a name this has not resolved, and `root` is where the
    distribution says its files are — which is what lets every read below happen without running
    anything the pack ships.
    """

    #: The entry-point name, which is how a person and the index address it.
    name: str
    #: The importable package holding its resources. Not imported by anything in this class.
    module: str
    distribution: str = ""
    version: str = ""
    #: The module's directory, located through distribution metadata. `None` when the distribution
    #: cannot be resolved to files on disk — a zipped install, or an injected test entry.
    root: Path | None = None

    # -- the authoring surface, used from a lockstep.py at bind time ------------------

    @property
    def package(self) -> str:
        """For `Body.from_file(..., package=pack.package)`.

        Resolution through `importlib.resources` imports the package, which is correct *here*:
        this is the seam a repository reaches for in its own module, at bind time, having already
        decided to trust the pack. The inspection paths below deliberately do not use it.
        """
        return self.module

    def body(self, resource: str) -> Body:
        """A prompt body from this pack, resolved at render time like every shipped one."""
        return Body.from_file(resource, package=self.module)

    def guardrails(self, *names: str) -> tuple[tuple[str, str], ...]:
        """Guardrail fragments, labelled by pack so a projection says where they came from.

        `prompts/<name>.md`, frontmatter stripped, in the order asked for. The label is
        `<pack>/<name>` rather than `<name>` because a projection is read to answer "whose rule is
        this" — two packs contributing `house` would otherwise be indistinguishable in the one
        artifact meant to tell them apart.
        """
        fragments: list[tuple[str, str]] = []
        for name in names:
            text = self.read(f"prompts/{name}.md")
            if text is None:
                raise PackError(f"pack {self.name!r} has no prompts/{name}.md")
            _, body = parse_frontmatter(text)
            fragments.append((f"{self.name}/{name}", body.strip()))
        return tuple(fragments)

    # -- inspection, none of which imports the pack -----------------------------------

    def read(self, relative: str) -> str | None:
        """A file inside the pack, by path, or `None`. Located, never imported."""
        path = self.file(relative)
        return path.read_text() if path is not None and path.is_file() else None

    def file(self, relative: str) -> Path | None:
        if self.root is None:
            return None
        return self.root / relative

    def manifest(self) -> Manifest:
        """Parse `pack.toml`, refusing anything it may not carry.

        The refusal is the point rather than the parse. A manifest that silently ignored an
        unknown key would let one arrive, get documented somewhere, and become load-bearing —
        which is how a declaration file becomes a configuration language.
        """
        raw = self.read("pack.toml")
        if raw is None:
            raise PackError(
                f"pack {self.name!r} ships no pack.toml, so it does not say what it is. "
                f"Add one: [pack] with kind = one of {', '.join(KINDS)}."
            )
        try:
            data = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as e:
            raise PackError(f"pack {self.name!r} has an unreadable pack.toml: {e}") from e

        section = data.get("pack")
        if not isinstance(section, dict):
            raise PackError(f"pack {self.name!r} has a pack.toml with no [pack] table")
        unknown = sorted(set(section) - MANIFEST_KEYS)
        if unknown:
            raise PackError(
                f"pack {self.name!r} declares {', '.join(unknown)} in pack.toml, which may only "
                f"carry {', '.join(sorted(MANIFEST_KEYS))}. A pack offers code and resources; what "
                f"runs is decided by a line in lockstep.py, not by a key in a manifest."
            )
        kind = str(section.get("kind", ""))
        if kind not in KINDS:
            raise PackError(f"pack {self.name!r} declares kind={kind!r}; expected one of {', '.join(KINDS)}")
        return Manifest(kind=kind, summary=str(section.get("summary", "")))

    def imports(self) -> str:
        """`none`, `modules`, or `unknown` — what installing this puts in the import graph.

        Derived by walking the AST of every `.py` the pack ships: `none` means each module holds a
        docstring and nothing else, so importing it can run no code of the pack's own. That is the
        property a prompt pack is worth having, and it is a fact on every pack rather than a tier,
        because a tier would have to lie about the pack that ships one small `Prompt` subclass.

        `unknown` is not `modules` and not `none`. A distribution this cannot resolve to files —
        zipped, or an entry injected by a test — has not been checked, and reporting an unchecked
        pack as inert would be the reassuring answer computed from nothing.
        """
        if self.root is None or not self.root.is_dir():
            return "unknown"
        for path in sorted(self.root.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text())
            except (OSError, SyntaxError):
                return "unknown"
            if any(not _is_docstring(node) for node in tree.body):
                return "modules"
        return "none"

    def corpus(self) -> Path | None:
        directory = self.file("corpus")
        return directory if directory is not None and directory.is_dir() else None

    def cassettes(self) -> list[str]:
        directory = self.file("cassettes")
        if directory is None or not directory.is_dir():
            return []
        return sorted(p.stem for p in directory.glob("*.json"))


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def installed(entries: Any = None) -> list[Pack]:
    """Every `in_lockstep.extensions` entry point, in name order, with nothing loaded.

    `entry.load()` is never called — that would import a stranger's package to answer a question
    about whether it is worth importing. The module name comes out of the entry point's value and
    the files come from distribution metadata, so listing is a metadata read from end to end.
    """
    if entries is None:
        from importlib.metadata import entry_points

        entries = list(entry_points(group=GROUP))

    packs: list[Pack] = []
    for entry in sorted(entries, key=lambda e: str(e.name)):
        module = str(getattr(entry, "value", "") or "").split(":", 1)[0].strip()
        dist = getattr(entry, "dist", None)
        root = getattr(entry, "root", None)
        if root is None and dist is not None and module:
            root = _module_root(dist, module)
        packs.append(
            Pack(
                name=str(entry.name),
                module=module,
                distribution=str(getattr(dist, "name", "") or ""),
                version=str(getattr(dist, "version", "") or ""),
                root=Path(root) if root is not None else None,
            )
        )
    return packs


def _module_root(dist: Any, module: str) -> Path | None:
    """Where a distribution put a module's files, without importing it.

    `dist.files` plus `locate_file` is the metadata answer to a question `importlib.resources`
    answers by importing, and it is the first thing tried because it is the most direct.

    It does not always answer. An **editable** install records a `.pth` and its dist-info and
    nothing else — which is how a pack author installs their own pack while writing it, so the
    population most likely to hit this is the one least able to explain it. Their pack reported
    `imports: unknown` and no `pack.toml`, and the honest-but-useless answer looked like a broken
    framework. Found by installing the worked example for the first time, which is what
    `design/extension-packs.md` §11 said would happen.

    So the fallback is `find_spec`, which resolves a top-level name to its directory through the
    ordinary path finders and **does not execute the module** — asserted in the tests, because
    "listing a pack runs no code it ships" is the property this whole module is arranged around
    and a fallback that quietly broke it would be worse than reporting `unknown`.
    """
    top = module.split(".")[0]
    try:
        for recorded in dist.files or ():
            parts = Path(str(recorded)).parts
            if parts and parts[0] == top:
                located = Path(str(dist.locate_file(recorded))).resolve()
                # Walk back up to the package directory the file sits under.
                return located.parents[len(parts) - 2] if len(parts) > 1 else located.parent
    except Exception:  # pragma: no cover - defensive: metadata shapes vary by installer
        return None
    return _located_without_importing(top)


def _located_without_importing(top: str) -> Path | None:
    """A top-level package's directory, via the import machinery but not the import."""
    import importlib.util

    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):  # a name that is not importable at all
        return None
    for location in (spec.submodule_search_locations or ()) if spec else ():
        return Path(str(location)).resolve()
    return None


def pack(name: str, entries: Any = None) -> Pack:
    """The installed pack with this name, or a refusal naming what is installed.

    The lookup a `lockstep.py` uses. It resolves a *name* to files, which is all it does: putting
    the pack to work is the next line in that file, written by a person, in a diff.
    """
    available = installed(entries)
    for candidate in available:
        if candidate.name == name:
            return candidate
    raise PackNotFound(
        f"no installed pack named {name!r}; have "
        f"{', '.join(p.name for p in available) or '(none installed)'}. "
        f"Packs arrive by being installed — `uv add <distribution>` — and take effect by being "
        f"named in .lockstep/lockstep.py."
    )


def pinning(root: Path, distribution: str) -> str:
    """`pinned`, `unpinned`, or `unknown` — whether this project fixes the pack's version.

    A receipt describes the code that is installed right now; a pin is what makes that the code
    that will be installed next time. Without one, a repository that accepted a pack's capabilities
    has accepted them for a version range, which is not the same thing at all.

    `unknown` when there is no manifest to read, because a project this cannot inspect has not been
    checked. Reporting it as pinned would be the reassuring answer, and reporting it as unpinned
    would be an accusation nothing supports.
    """
    if not distribution:
        return "unknown"
    lock = root / "uv.lock"
    if lock.is_file():
        # uv.lock pins a version and a hash for everything it resolves, so naming the distribution
        # there IS the pin. Read as text: the file is TOML, and parsing it to answer a membership
        # question would make this care about a lockfile format that is not ours.
        needle = f'name = "{distribution}"'
        if needle in lock.read_text():
            return "pinned"
        return "unpinned"
    manifest = root / "pyproject.toml"
    if not manifest.is_file():
        return "unknown"
    try:
        project = tomllib.loads(manifest.read_text()).get("project") or {}
    except tomllib.TOMLDecodeError:
        return "unknown"
    for dependency in project.get("dependencies") or ():
        text = str(dependency)
        if text.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip() == distribution:
            return "pinned" if "==" in text else "unpinned"
    return "unpinned"
