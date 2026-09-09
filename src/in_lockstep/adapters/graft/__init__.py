"""Graft, provisioned by the framework into its own cache, and an index it builds before a model
starts (#375).

[Graft](https://github.com/trailhq/Graft) builds a per-symbol code graph with tree-sitter and
answers `ask`, `grep`, `callers`, `skeleton` and `map` from it. Its own onboarding -- a global
install, `graft init`, hooks into an agent's directory, a `graft/` directory it gitignores for you
-- is an agent-integration story. The framework's story is that the tool is there when a verb
runs, or refuses by name, and the adopter installs and configures nothing (O2). This module is
that story's deterministic half: where Graft lives, how it gets there, when the index is rebuilt,
and what every Graft process is and is not handed (O6). The tool a model calls is built on it.

Six facts from running Graft 0.16.0, each of which decides something below:

1. Its plain-text output carries an instruction addressed to the model ("tell the user the total
   graft tokens saved this turn"). Every query here is `--json`; the framework renders text.
2. Its CLI loads `.env` from the current directory (`import "dotenv/config"`). Every Graft
   process runs with `cwd` in the cache, never in the repository, and the tree is a positional
   argument; the environment is the sandbox's pass-through set plus the four names below, so
   nothing Graft could read is present to begin with.
3. A build is byte-deterministic, so the fingerprint is a promise the record can keep.
4. Its content-hash cache survives a change of root path: building a materialised worktree into
   an index last built from the repository re-parses the files that differ and replays the rest.
   The index directory is therefore per repository, with a fingerprint sidecar, rather than per
   fingerprint: a fresh directory per fingerprint would throw that replay away on every edit.
5. A query reads source text from the positional root, not from the index. A build and every
   query over it name the same tree.
6. The install compiles one grammar (Kotlin ships no prebuilt binary), so `npm ci` runs its
   lifecycle scripts and needs a C/C++ toolchain and `python3` beside Node >= 20. A failed
   compile is reported with that sentence rather than npm's stack.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from ...ai.builtins import Hit, SearchAnswer
from ...core.outcome import Finding, Severity
from ...core.types import ChangeSet, FileChange, Resolution
from ...privileged import sink
from ..sandbox import Sandbox, SandboxResult
from ..worktree import WorktreeError, _git, materialize

GRAFT_PACKAGE = "@nanonets/graft"
#: Pinned here and in `package-lock.json` beside this module, by version and by integrity. A
#: docs test holds the two together; bumping one without the other fails the build.
GRAFT_VERSION = "0.16.0"
#: What `@nanonets/graft`'s `engines` field asks for.
NODE_MAJOR = 20
#: On every Graft process, without exception. Telemetry off by construction rather than by a
#: setting somebody could forget; no rebuild behind the framework's back, so a query answers from
#: the index the fingerprint describes; no `.gitignore` edit even though `--dir` already keeps
#: Graft out of the tree.
GRAFT_ENV = {"DO_NOT_TRACK": "1", "GRAFT_NO_REFRESH": "1", "GRAFT_NO_GITIGNORE": "1"}
#: Never on a Graft argv. `--deep` and `blast --name` are Graft's model layer, under Graft's own
#: key, which this framework does not hold and would not route around its own recorder; the rest
#: are Graft's agent-integration surface. Checked at the one place an argv is composed, as a
#: framework invariant rather than a model-facing refusal: no caller here can ask for them.
FORBIDDEN = frozenset(
    {"--deep", "--lsp", "--name", "init", "mcp", "viz", "upgrade", "uninstall", "telemetry"}
)
QUERY_TIMEOUT = 60.0
BUILD_TIMEOUT = 300.0
INSTALL_TIMEOUT = 900.0
TAIL_LINES = 20


def cache_root() -> Path:
    """Where the framework keeps what it provisions for itself: outside every repository.

    `$RUNNER_TEMP` under CI, the way the cassette default in `cli.py` chooses it, so a job's
    install and index die with the runner; `$XDG_CACHE_HOME` or `~/.cache` elsewhere, so a laptop
    installs once. Never the repository, and never a global npm prefix: nothing the framework
    provisions is something the adopter has to notice, commit, or clean up.
    """
    if os.environ.get("CI") and os.environ.get("RUNNER_TEMP"):
        return Path(os.environ["RUNNER_TEMP"]) / "in-lockstep"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "in-lockstep"


class Runner(Protocol):
    """What Graft is run through: `Sandbox.run`'s shape, built with the environment below."""

    async def run(
        self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0
    ) -> SandboxResult: ...


def _sandbox(network: bool, env: dict[str, str]) -> Sandbox:
    return Sandbox(allow_network=network, extra_env=env)


def _tail(result: SandboxResult) -> str:
    lines = (result.stderr or result.stdout).strip().splitlines()
    return "\n".join(lines[-TAIL_LINES:])


@dataclass
class Graft:
    """Graft in the framework's cache: provisioned, fingerprinted, and run with nothing in hand.

    `sandbox_factory` is how a test watches what ran: it is handed `(network, env)` and returns
    the runner every Graft process goes through. The default builds a `Sandbox` with no image --
    a subprocess on this host with the ambient credentials dropped -- because Graft reads the
    repository and writes only its cache, and the model never chooses its argv.
    """

    cache: Path = field(default_factory=cache_root)
    version: str = GRAFT_VERSION
    sandbox_factory: Callable[[bool, dict[str, str]], Runner] = _sandbox
    #: How many installs this instance attempted. A test reads it to prove a host without Node
    #: attempted none.
    installs: int = 0
    #: How many index builds this instance ran. A test reads it to prove a second query over the
    #: same tree built nothing.
    builds: int = 0

    # -- where things are ----------------------------------------------------------------------

    @property
    def prefix(self) -> Path:
        return self.cache / "graft" / self.version

    @property
    def binary(self) -> Path:
        return self.prefix / "node_modules" / ".bin" / "graft"

    @property
    def home(self) -> Path:
        """`HOME` for every Graft process. Graft writes version-check state under `homedir()`;
        the adopter's home is not where a framework-provisioned tool keeps its bookkeeping."""
        return self.cache / "graft" / "home"

    def index_dir(self, repo_root: str) -> Path:
        digest = hashlib.sha256(str(Path(repo_root).resolve()).encode()).hexdigest()[:12]
        return self.cache / "graft" / "index" / digest

    def _env(self) -> dict[str, str]:
        return {**GRAFT_ENV, "HOME": str(self.home)}

    def _runner(self, *, network: bool = False) -> Runner:
        self.home.mkdir(parents=True, exist_ok=True)
        return self.sandbox_factory(network, self._env())

    async def _run(self, argv: list[str], *, network: bool = False, timeout: float) -> SandboxResult:
        forbidden = FORBIDDEN.intersection(argv)
        if forbidden:
            raise ValueError(f"never on a Graft argv: {', '.join(sorted(forbidden))}")
        # Fact 2: the cache, never the repository, so Graft's dotenv finds nothing to load.
        return await self._runner(network=network).run(argv, cwd=str(self.cache), timeout=timeout)

    # -- the host ------------------------------------------------------------------------------

    async def node_refusal(self) -> str | None:
        """`None` when a Node the pin can run is on PATH, else the refusal, naming what was found."""
        found = await self._run(["node", "--version"], timeout=QUERY_TIMEOUT)
        if found.exit_code != 0:
            return (
                f"refused: search_code.no_node: code search needs Node >= {NODE_MAJOR} on PATH and "
                f"found none ({_tail(found) or 'node is not installed'})"
            )
        version = found.stdout.strip()
        try:
            major = int(version.lstrip("v").split(".")[0])
        except ValueError:
            major = 0
        if major < NODE_MAJOR:
            return (
                f"refused: search_code.no_node: code search needs Node >= {NODE_MAJOR} and found "
                f"{version or 'an unversioned node'}"
            )
        return None

    def locations(self, root: str) -> tuple[Resolution, ...]:
        """Where Node and Graft are, for `ls` and `doctor`. Graft's probe is its version, so a
        cache holding the wrong pin is `DOC181` rather than a surprise at the first query."""
        node = shutil.which("node")
        graft = str(self.binary) if self.binary.exists() else None
        return (
            Resolution(
                tool="node",
                path=node,
                how="on PATH",
                tried=("node on PATH",),
                probe=(node, "--version") if node else (),
            ),
            Resolution(
                tool="graft",
                path=graft,
                how=f"provisioned into {self.prefix}",
                tried=(f"{self.binary} (`in-lockstep provision` installs it)",),
                probe=(graft, "--version") if graft else (),
            ),
        )

    # -- provisioning --------------------------------------------------------------------------

    async def installed_version(self) -> str | None:
        if not self.binary.exists():
            return None
        probe = await self._run([str(self.binary), "--version"], timeout=QUERY_TIMEOUT)
        return probe.stdout.strip() if probe.exit_code == 0 else None

    async def ensure_installed(self) -> str | None:
        """Graft at the pinned version, in the cache. `None`, or the refusal every query returns.

        Idempotent, and the second call is a probe: a prefix whose `graft --version` answers the
        pin is left alone, which is what lets `in-lockstep provision` run it in every work job and
        a session run it again at start without a second install. The network is reached only on
        the install, through a runner that allows it, and never during a model turn: a query with
        no `graft` on disk refuses, it does not fetch.
        """
        if await self.installed_version() == self.version:
            return None
        if (refusal := await self.node_refusal()) is not None:
            return refusal
        self.prefix.mkdir(parents=True, exist_ok=True)
        package = resources.files("in_lockstep.adapters.graft")
        for name in ("package.json", "package-lock.json"):
            sink.write_text_atomic(self.prefix / name, package.joinpath(name).read_text())
        self.installs += 1
        # `npm ci`, not `npm install`: the lockfile is the pin, by version and by integrity, and
        # a resolver that could pick a newer version would make "which Graft indexed this" a
        # question the record could not answer. `--prefix` keeps the install in the cache and off
        # the adopter's global prefix. Lifecycle scripts run, for fact 6.
        install = await self._run(
            ["npm", "ci", "--prefix", str(self.prefix), "--no-audit", "--no-fund"],
            network=True,
            timeout=INSTALL_TIMEOUT,
        )
        if install.exit_code != 0:
            tail = _tail(install)
            toolchain = (
                " One grammar is compiled at install: a C/C++ toolchain and python3 are required."
                if "gyp" in tail
                else ""
            )
            return (
                f"refused: search_code.unavailable: `npm ci` for {GRAFT_PACKAGE}@{self.version} into "
                f"{self.prefix} exited {install.exit_code}.{toolchain}\n{tail}"
            )
        if (got := await self.installed_version()) != self.version:
            return (
                f"refused: search_code.unavailable: {self.binary} answers {got or 'nothing'} after the "
                f"install, not {self.version}"
            )
        return None

    # -- the index -----------------------------------------------------------------------------

    async def fingerprint(self, repo_root: str, staged: Iterable[tuple[str, str | None]] = ()) -> str:
        """What the index describes: HEAD, every tracked file as it is on disk, and the session's
        staged set. Over-approximate on purpose -- a spurious rebuild costs a stat pass through
        Graft's own cache, an under-approximation is a query answered from a tree that is not the
        one the session sees -- so a dirty checkout is fingerprinted by its bytes, not by HEAD."""
        digest = hashlib.sha256()
        try:
            digest.update((await _git(repo_root, "rev-parse", "HEAD")).encode())
            digest.update((await _git(repo_root, "ls-files", "-s")).encode())
            dirty = await _git(repo_root, "status", "--porcelain", "--untracked-files=all")
        except WorktreeError as e:
            # Not a repository, or one with no commit: the tree's bytes are all there is to say.
            digest.update(f"no-git:{e}".encode())
            dirty = ""
        for line in sorted(dirty.splitlines()):
            path = Path(repo_root) / line[3:].split(" -> ")[-1]
            digest.update(line.encode())
            if path.is_file():
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        for name, contents in sorted(staged):
            digest.update(name.encode())
            digest.update(b"deleted" if contents is None else hashlib.sha256(contents.encode()).digest())
        return digest.hexdigest()

    async def ensure_index(self, repo_root: str, tree: str, fingerprint: str) -> str | None:
        """The index over `tree`, built unless the sidecar already says this fingerprint.

        `tree` is the repository, or the worktree the staged set was materialised into (fact 4:
        that costs the changed files, not a re-index). The sidecar is written after a successful
        build and only then, so a build that failed is retried by the next query rather than
        remembered as done. `None`, or the refusal the query returns.
        """
        index = self.index_dir(repo_root)
        sidecar = index / "fingerprint"
        if sidecar.is_file() and sidecar.read_text() == fingerprint:
            return None
        index.mkdir(parents=True, exist_ok=True)
        self.builds += 1
        built = await self._run([str(self.binary), "--dir", str(index), "build", tree], timeout=BUILD_TIMEOUT)
        if built.exit_code != 0:
            return (
                f"refused: search_code.no_index: `graft build` over {tree} exited {built.exit_code}\n"
                f"{_tail(built)}"
            )
        sink.write_text_atomic(sidecar, fingerprint)
        return None

    async def query(
        self, repo_root: str, argv: list[str], *, timeout: float = QUERY_TIMEOUT
    ) -> SandboxResult:
        """One Graft query over the index for `repo_root`. `argv` is the subcommand, its options,
        `--` and its positionals, as `argv_for` composes them; `--json` goes in right after the
        subcommand because fact 1 makes it a property of every query rather than a choice, and
        Graft takes it only there: not before the subcommand, and not after `--`."""
        return await self._run(
            [str(self.binary), "--dir", str(self.index_dir(repo_root)), argv[0], "--json", *argv[1:]],
            timeout=timeout,
        )

    async def blast(self, repo_root: str, base: str, tree: str, fingerprint: str) -> tuple[str, str]:
        """The blast radius of a diff against `base`, rendered — or an empty answer and a reason.

        `(rendered, note)`, never a raise: a review whose radius could not be computed is a review
        that runs with the diff alone, which is what every review did before this. The reason is
        kept so the outcome can say the lens ran without it rather than leaving a reader to wonder
        which kind of quiet this was.
        """
        if (missing := await self.ensure_index(repo_root, tree, fingerprint)) is not None:
            return "", missing
        result = await self._run(
            [str(self.binary), *blast_argv(tree, base, self.index_dir(repo_root))],
            timeout=BLAST_TIMEOUT,
        )
        if result.exit_code != 0:
            return "", f"review.blast.failed: graft blast exited {result.exit_code}\n{_tail(result)}"
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError:
            return "", "review.blast.unreadable: graft blast did not answer with JSON"
        if not isinstance(payload, dict):
            return "", "review.blast.unreadable: graft blast did not answer with an object"
        return blast_render(payload), ""


#: How far `blast` walks the incoming edges. Two hops is Graft's own default and the right one
#: here: one hop is the direct callers, which a reviewer often has in the diff already, and "all"
#: is the transitive closure of a hub symbol, which for a framework like this one is most of the
#: repository. The depth is the framework's, never a lens's: a number a prompt could raise is a
#: prompt that can spend the review's budget on itself.
BLAST_DEPTH = 2
BLAST_TIMEOUT = 120.0


def blast_argv(tree: str, base: str, index: Path) -> list[str]:
    """The argv for one blast radius. Every token is the framework's; nothing here is model-chosen.

    `--format json` rather than the `--json` every other query takes, because `blast` does not
    have that flag -- it answers `error: unknown option '--json'`, which is the kind of thing that
    is cheaper to find by running it than by reading for it.

    `--no-owners` is not a preference. By default `blast` reads git history and names the people
    behind each affected area, and that is contributor identities flowing into a model prompt to
    answer a question -- who to tag -- that this framework does not ask and would not delegate.

    `--name` would name the areas with an LLM call under Graft's own key. It is in `FORBIDDEN`, so
    this cannot compose it even by mistake.
    """
    return [
        "blast",
        "--dir",
        str(index),
        "--base",
        base,
        "--format",
        "json",
        "--no-owners",
        "-d",
        str(BLAST_DEPTH),
        tree,
    ]


def blast_render(payload: dict[str, Any], *, max_files: int = 40, max_symbols: int = 8) -> str:
    """What a reviewer is told, out of what `blast` answers.

    The raw JSON is not it: 186 KB for a sixteen-file change here, most of it the diff's own hunks,
    which the review already has in front of it. What a diff cannot show is what DEPENDS on the
    lines it touched -- 109 of the 142 impacted symbols in that same change were outside it -- and
    which test modules cover that. Rendered, those are 5 KB.

    Bounded by files and by symbols per file, because a change that touches a hub reaches most of
    a repository and a list that long stops being read. What was left out is said rather than
    silently dropped.
    """
    changed = {str(entry.get("path", "")) for entry in payload.get("changed", []) or ()}
    outside: dict[str, list[str]] = {}
    for entry in payload.get("impacted", []) or ():
        path = str(entry.get("path", ""))
        name = str(entry.get("name", ""))
        if not path or path in changed or not name:
            continue
        outside.setdefault(path, [])
        if name not in outside[path]:
            outside[path].append(name)
    if not outside:
        return ""

    lines = [
        "What this change reaches and the diff does not show — from the repository's own call "
        f"graph, {BLAST_DEPTH} hops out. Symbols that depend on the changed lines:",
        "",
    ]
    for path in sorted(outside)[:max_files]:
        names = sorted(outside[path])
        shown = ", ".join(names[:max_symbols])
        more = f" (+{len(names) - max_symbols} more)" if len(names) > max_symbols else ""
        lines.append(f"- {path}: {shown}{more}")
    if len(outside) > max_files:
        lines.append(f"- …and {len(outside) - max_files} more files")

    covering = sorted(
        {
            file
            for module in payload.get("testModules", []) or ()
            for file in (module.get("files") or ())
            if str(file) not in changed
        }
    )
    if covering:
        lines += [
            "",
            "Test modules covering that reach, which this change does not touch:",
            *(f"- {file}" for file in covering[:max_files]),
        ]
        if len(covering) > max_files:
            lines.append(f"- …and {len(covering) - max_files} more")
    return "\n".join(lines)


def argv_for(mode: str, args: dict[str, object], tree: str, *, scope: str = "") -> list[str]:
    """The Graft argv for one bounded question, options first and every model-chosen token after
    `--`, so a regex or a symbol beginning with a dash is a pattern and never an option. The
    model chose the words; the framework chose everything else."""
    query, file = str(args.get("query", "")), str(args.get("file", ""))
    where = str(args.get("scope", "") or scope)
    narrow = ["--in", where] if where else []
    if mode == "ask":
        return ["ask", *narrow, "--", query, tree]
    if mode == "grep":
        return ["grep", *narrow, "--", query, tree]
    if mode == "callers":
        direction = "out" if args.get("direction") == "out" else "in"
        return ["callers", "--direction", direction, "-d", str(args.get("depth", 1)), "--", query, tree]
    if mode == "skeleton":
        return ["skeleton", "--", file, tree]
    if mode == "map":
        return ["map", "--", tree]
    raise ValueError(f"no such search mode: {mode}")


def _path_of(pointer: str) -> str:
    return pointer.split(":", 1)[0]


def render(mode: str, payload: dict[str, Any]) -> tuple[Hit, ...]:
    """Graft's JSON as lines a model already knows how to read, one `Hit` per line with the path
    it names. Every note Graft attaches is dropped here: those are Graft's sentences to a model,
    and the framework does not forward a third party's prompt text (fact 1)."""
    hits: list[Hit] = []
    if mode == "ask":
        for hit in payload.get("hits", ()):
            pointer = str(hit.get("pointer", ""))
            line = f"{pointer}  {hit.get('title', '')}  {hit.get('snippet', '')}".rstrip()
            hits.append(Hit(_path_of(pointer), line))
    elif mode == "grep":
        for group in payload.get("groups", ()):
            symbol = group.get("symbol") or {}
            where = f"  (in {symbol.get('kind', '')} {symbol.get('name', '')})" if symbol else ""
            path = str(group.get("path", ""))
            for hit in group.get("hits", ()):
                text = str(hit.get("text", "")).strip()[:200]
                hits.append(Hit(path, f"{path}:{hit.get('line', '')}: {text}{where}"))
    elif mode == "callers":
        for match in payload.get("matches", ()):
            symbol = match.get("symbol") or {}
            edges = match.get("hits") or ()
            path = str(symbol.get("path", ""))
            head = f"{path}:{symbol.get('span', '')}  {symbol.get('kind', '')} {symbol.get('name', '')}"
            hits.append(Hit(path, f"{head}  {len(edges)} edge(s)"))
            for edge in edges:
                line = (
                    f"  {edge.get('path', '')}:{edge.get('span', '')}  {edge.get('kind', '')} "
                    f"{edge.get('name', '')}  ({edge.get('relation', '')}, depth {edge.get('depth', '')})"
                )
                hits.append(Hit(str(edge.get("path", "")), line))
    elif mode == "skeleton":
        file = str(payload.get("file", ""))
        for entry in payload.get("entries", ()):
            line = (
                f"{file}:{entry.get('span', '')}  {entry.get('kind', '')} {entry.get('name', '')}"
                f"  {entry.get('signature', '')}"
            )
            hits.append(Hit(file, line))
    elif mode == "map":
        totals = payload.get("totals") or {}
        summary = (
            f"{totals.get('files', '?')} files · {totals.get('symbols', '?')} symbols · "
            f"{totals.get('edges', '?')} edges"
        )
        hits.append(Hit("", summary))
        for entry in payload.get("dirs", ()):
            hubs = ", ".join(
                f"{h.get('name', '')} ({h.get('inDegree', '?')}←)" for h in entry.get("hubs", ())
            )
            path = str(entry.get("path", ""))
            counts = f"{entry.get('files', '?')} files · {entry.get('symbols', '?')} symbols"
            line = f"{path}/  {counts}  hubs: {hubs}"
            hits.append(Hit(path, line))
        for hot in payload.get("hotspots", ()):
            path = str(hot.get("path", ""))
            line = (
                f"hotspot  {path}:{hot.get('span', '')}  {hot.get('kind', '')} {hot.get('name', '')}"
                f"  {hot.get('inDegree', '?')}←"
            )
            hits.append(Hit(path, line))
    return tuple(hits)


@dataclass
class GraftSearch:
    """`search_code`'s backend: Graft over the tree this session sees (#375).

    One per run. With nothing staged the tree is the repository; with staged writes the staged
    set is materialised into a throwaway worktree for the query, the way `run_tests` materialises
    it for a suite, and the index is built over that tree -- fact 4 makes that the changed files'
    parse rather than a re-index, and fact 5 is why the query runs while the worktree exists.
    The fingerprint covers the staged set, so a query after a new write rebuilds and a query after
    none does not; the sidecar is what makes two queries over one staged set build once.

    What it saw goes on the record through `notes()`: the Graft version and the index fingerprint
    when a query ran or `prepare` built, or the refusal by name when the tool refused, so two runs
    that searched different trees say so and a run that went on without the tool says that.
    """

    graft: Graft
    repo_root: str
    #: A default `--in` for every query, for a Test bound at a package directory (#372).
    scope: str = ""
    _refusal: str | None = field(default=None, init=False)
    _fingerprint: str = field(default="", init=False)

    async def prepare(self) -> None:
        """Install if the cache is cold and build the index over the repository, before the first
        model call. A refusal is kept for the record and returned by every query; it is not raised,
        because a run goes on without the tool (GATE-SEARCH-1)."""
        if (refusal := await self.graft.ensure_installed()) is not None:
            self._refusal = refusal
            return
        fingerprint = await self.graft.fingerprint(self.repo_root)
        refusal = await self.graft.ensure_index(self.repo_root, self.repo_root, fingerprint)
        if refusal is not None:
            self._refusal = refusal
            return
        self._refusal, self._fingerprint = None, fingerprint

    async def __call__(
        self, mode: str, args: dict[str, object], staged: tuple[tuple[str, str | None], ...]
    ) -> SearchAnswer:
        if (refusal := await self.graft.ensure_installed()) is not None:
            self._refusal = refusal
            return SearchAnswer(refusal=refusal)
        fingerprint = await self.graft.fingerprint(self.repo_root, staged)
        if not staged:
            return await self._answer(mode, args, self.repo_root, fingerprint)
        changes = ChangeSet(changes=tuple(FileChange(path=p, contents=c) for p, c in staged))
        async with materialize(self.repo_root, changes) as tree:
            return await self._answer(mode, args, tree, fingerprint)

    async def _answer(self, mode: str, args: dict[str, object], tree: str, fingerprint: str) -> SearchAnswer:
        if (refusal := await self.graft.ensure_index(self.repo_root, tree, fingerprint)) is not None:
            self._refusal = refusal
            return SearchAnswer(refusal=refusal)
        self._refusal, self._fingerprint = None, fingerprint
        result = await self.graft.query(self.repo_root, argv_for(mode, args, tree, scope=self.scope))
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            if result.exit_code != 0:
                return SearchAnswer(
                    refusal=(
                        f"refused: search_code.no_index: `graft {mode}` exited {result.exit_code}\n"
                        f"{_tail(result)}"
                    )
                )
            # Graft answers an unknown symbol in prose rather than JSON; the framework's own words.
            return SearchAnswer(empty=f"no symbol {str(args.get('query', ''))!r} in the index")
        if not isinstance(payload, dict):  # pragma: no cover - Graft always answers an object
            return SearchAnswer(refusal=f"refused: search_code.no_index: `graft {mode}` answered no object")
        return SearchAnswer(hits=render(mode, payload))

    def notes(self) -> tuple[Finding, ...]:
        """What the record carries: the version and fingerprint the run searched, or why it could
        not. NOTE severity, never blocking: the tool's absence is a fact about the run, not a
        verdict on the change."""
        if self._refusal is not None:
            parts = self._refusal.split(":", 2)
            code = parts[1].strip() if len(parts) > 1 else "search_code.unavailable"
            return (Finding(id=code, message=self._refusal, severity=Severity.NOTE),)
        if self._fingerprint:
            message = f"graft {self.graft.version} · index {self._fingerprint[:12]}"
            return (Finding(id="search_code.index", message=message, severity=Severity.NOTE),)
        return ()
