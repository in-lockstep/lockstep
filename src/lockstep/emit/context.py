"""Shared emission state: resolved pins, provenance, and the generated-file registry."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .. import __version__
from ..errors import EmitError
from ..spec.model import Command, Profile, SourceFile, Spec
from ..spec.parse import EVENT_SOURCES

PINS_PATH = ".pipeline/pins.lock"

# A pin of the right shape and no value. `lockstep pin` writes these when it has nothing to resolve
# against — an unpublished capability repository, an image that has never been built — so that the
# lock file records the intent. They compile, and they cannot run: a workflow referencing one is
# pointing at a commit and a digest that do not exist. Doctor treats them as unpinned, because that
# is what they are, and the compiler says so on every run rather than leaving it to be noticed.
# Where a PEP 508 requirement stops being a name: `in-lockstep>=0.1,<1.0` -> `in-lockstep`.
REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")


def requirement_name(requirement: str) -> str:
    match = REQUIREMENT_NAME.match(requirement.strip())
    return match.group(0) if match else requirement.strip()


def is_local_requirement(requirement: str) -> bool:
    """A path rather than a distribution — this repository compiling itself from the checkout."""
    text = requirement.strip().strip("\"'")
    return text in (".", "..") or text.startswith(("./", "../", "/", "file://"))


PLACEHOLDER_SHA = "0" * 40
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


def _agree(key: str, field: str, locked: object, declared: str) -> None:
    if locked and declared and str(locked) != declared:
        raise EmitError(
            f"{PINS_PATH} pins {field} {str(locked)!r}, but {key} now says {declared!r}",
            hint="run `lockstep pin` — the digest on record was resolved against the old value",
        )


@dataclass
class Pins:
    """Resolved capability pins: tags/versions from the manifest, SHAs/digests from pins.lock."""

    actions_repo: str = ""
    actions_tag: str = ""
    actions_sha: str = ""
    exec_package: str = "in-lockstep-exec"
    exec_version: str = ""
    exec_image: str = ""
    exec_digest: str = ""
    gh_aw_version: str = ""
    # The compiler requirement from the manifest, and the exact version `lockstep pin` resolved it
    # to. A range is not a pin: the gate would install whatever the index offers that day.
    compiler_requirement: str = ""
    compiler_version: str = ""
    external: dict[str, str] = field(default_factory=dict)
    external_tags: dict[str, str] = field(default_factory=dict)
    resolved: bool = False

    @classmethod
    def load(cls, spec: Spec) -> Pins:
        caps = spec.manifest.capabilities
        actions_repo, _, actions_tag = caps.actions.partition("@")
        actions_repo = actions_repo.removeprefix("github.com/")
        exec_package, _, exec_version = caps.exec.partition("==")

        # Where things live is the manifest's answer. There is no built-in default: a compiler that
        # silently points at a repository it happens to know the name of produces a workflow that
        # references somebody else's code, which is a worse outcome than refusing to compile.
        pins = cls(
            actions_repo=actions_repo,
            actions_tag=actions_tag,
            exec_package=exec_package or "in-lockstep-exec",
            exec_version=exec_version,
            exec_image=caps.exec_image,
            gh_aw_version=caps.gh_aw,
            compiler_requirement=caps.compiler,
        )

        path = spec.home / PINS_PATH
        if not path.is_file():
            return pins

        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        cap_pins = data.get("capabilities", {}) or {}
        actions = cap_pins.get("actions", {}) or {}
        exec_pin = cap_pins.get("exec", {}) or {}
        # The lock supplies what was *resolved*. Where the manifest says something different from
        # what the lock was resolved against, the pin describes a different artifact than the one
        # this pipeline now asks for — so it is refused rather than quietly preferred either way.
        pins.actions_sha = str(actions.get("sha") or "")
        pins.exec_digest = str(exec_pin.get("digest") or "")
        _agree("capabilities.actions", "repo", actions.get("repo"), pins.actions_repo)
        _agree("capabilities.actions", "tag", actions.get("tag"), pins.actions_tag)
        _agree("capabilities.exec-image", "image", exec_pin.get("image"), pins.exec_image)
        pins.exec_version = str(exec_pin.get("version") or pins.exec_version)
        compiler = cap_pins.get("compiler", {}) or {}
        _agree(
            "capabilities.compiler",
            "requirement",
            compiler.get("requirement"),
            pins.compiler_requirement,
        )
        pins.compiler_version = str(compiler.get("version") or "")
        pins.external = {str(k): str(v.get("sha", "")) for k, v in (data.get("external") or {}).items()}
        pins.external_tags = {
            str(v.get("sha", "")): str(v.get("tag", "")) for v in (data.get("external") or {}).values()
        }
        pins.resolved = bool(pins.actions_sha)
        return pins

    def placeholders(self) -> list[str]:
        """Pins that record an intention rather than an artifact that exists."""
        found = []
        if self.actions_sha == PLACEHOLDER_SHA:
            found.append(f"capability actions ({self.actions_repo or 'unset'}@{self.actions_tag})")
        if self.exec_digest == PLACEHOLDER_DIGEST:
            found.append(f"executor image ({self.exec_image or 'unset'})")
        return found

    def action(self, name: str) -> str:
        """A pinned reference to one of the capability repo's composite actions."""
        if not self.actions_repo:
            raise EmitError(
                "no capability actions repository",
                hint="set capabilities.actions in pipeline.yaml to where the composite actions are "
                "published, e.g. `github.com/<owner>/<repo>@v1.0.0`",
            )
        if not self.actions_sha:
            raise EmitError(
                f"capability action {name!r} is not pinned",
                hint=f"run `lockstep pin` to resolve {self.actions_repo}@{self.actions_tag} into {PINS_PATH}",
            )
        return f"{self.actions_repo}/{name}@{self.actions_sha}"

    def external_action(self, name: str) -> str:
        sha = self.external.get(name)
        if not sha:
            raise EmitError(
                f"external action {name!r} is not pinned",
                hint=f"add it to the `external` block of {PINS_PATH}",
            )
        return f"{name}@{sha}"

    def compiler_install(self) -> str:
        """What a generated check installs, exact when it can be.

        Everything else in a compiled pipeline is pinned to something immutable, and then the gate
        that enforces that installed its own compiler from a version range. A newer release could
        therefore change what a consumer's security check ran without a line changing in their
        repository — the exact event pinning exists to catch, in the one place nobody was looking.

        `lockstep pin` records the compiler that produced the committed output, which is the only
        version known to reproduce it. A local path (this repository compiling itself) is passed
        through: there is nothing to pin, the checkout *is* the version.
        """
        requirement = self.compiler_requirement or "in-lockstep"
        if self.compiler_version and not is_local_requirement(requirement):
            return f"{requirement_name(requirement)}=={self.compiler_version}"
        return requirement

    def exec_requirement(self) -> str:
        """The executor, pinned exactly, for the rare job that installs it rather than running in it.

        A job that has to materialize inherited definitions needs the compiler, which the executor
        image deliberately lacks — so it runs on the bare runner and installs both.
        """
        return f"{self.exec_package}=={self.exec_version}" if self.exec_version else self.exec_package

    def exec_container(self) -> str:
        if not self.exec_image:
            raise EmitError(
                "no executor image",
                hint="set capabilities.exec-image in pipeline.yaml to where the image is published, "
                "e.g. `quay.io/<owner>/pipeline-exec` or `ghcr.io/<owner>/pipeline-exec`",
            )
        if not self.exec_digest:
            raise EmitError(
                "executor image is not pinned by digest",
                hint=f"add capabilities.exec.digest to {PINS_PATH}",
            )
        return f"{self.exec_image}@{self.exec_digest}"

    def sha_tags(self) -> dict[str, str]:
        """sha -> human tag, used to annotate `uses:` lines with the version they pin."""
        tags = dict(self.external_tags)
        if self.actions_sha and self.actions_tag:
            tags[self.actions_sha] = self.actions_tag
        return {k: v for k, v in tags.items() if k and v}


@dataclass
class EmitContext:
    """Everything an emitter needs, plus the provenance it must record."""

    spec: Spec
    pins: Pins
    profile: Profile
    multi_profile: bool = False
    runs_on_override: str = ""

    @property
    def runs_on(self) -> str:
        return self.runs_on_override or self.spec.manifest.target.default_runs_on

    @property
    def state_db_path(self) -> str:
        return f"{self.output_dir_env}/.state/{self.spec.manifest.name}.db"

    @property
    def out_dir(self) -> str:
        return self.spec.manifest.target.out

    def container(self) -> dict[str, Any]:
        """The `container:` block for a job running a deterministic step.

        The image is pinned by digest; the options carry the sandbox floor. Both are produced here
        so no emitter can build an exec job that quietly skips either — and there are four of them.
        """
        return {
            "image": self.pins.exec_container(),
            "options": self.spec.manifest.target.sandbox.options(),
        }

    @property
    def output_dir_env(self) -> str:
        return "outputs"

    def header(self, sources: list[SourceFile | None], *, extra: list[str] | None = None) -> list[str]:
        """The provenance block every generated file carries."""
        stamps = [s.stamp() for s in sources if s is not None]
        lines = [
            f"GENERATED by lockstep {__version__} — do not edit.",
            "Edit the spec, add an overlay, or run `lockstep eject` on this file.",
            f"sources: {' '.join(stamps)}" if stamps else "sources: (none)",
        ]
        if self.profile.name:
            lines.append(f"profile: {self.profile.name}")
        lines.extend(extra or [])
        return lines

    def resolved_values(self) -> dict[str, str]:
        """Profile values with `${NAME}` references lowered to secret/var expressions."""
        from .profiles import resolve_value

        location = self.profile.src.rel if self.profile.src else self.profile.name
        return {
            key: resolve_value(raw, self.profile, location=location)
            for key, raw in self.profile.values.items()
        }

    def expand(self, text: str, command: Command | None = None) -> str:
        """Resolve `{param}`, `{profile_value}`, `{output_dir}` and `{pipeline_name}` placeholders.

        Command parameters win over profile values: a command that declares `profile` as a
        parameter means the caller chooses it, and the compiled workflow must read its input.
        """
        if command:
            chat = command.github.command
            from_comment = set(chat.arguments) if chat else set()
            for parameter in command.parameters:
                # A chain, first non-empty wins, because the same pipeline is reachable several ways
                # and a step should not care which one fired.
                #
                # `inputs` exists only for `workflow_dispatch` and `workflow_call`. On a comment or
                # a schedule it is empty — and a declared default reaches the dispatch input
                # definition, not the expression. So `source`, declared `default: github`, arrived
                # as the empty string on the comment path and `issue-fetch` refused it:
                # "Invalid value for '--source': '' is not one of 'github', 'jira'". `issue`
                # survived only because it is a command argument and had the gate to fall back to.
                sources = ["inputs." + parameter.input_name]
                if parameter.name in from_comment:
                    # A comment supplies the same values through the gate.
                    sources.append("needs.command-gate.outputs." + parameter.input_name)
                if parameter.from_event:
                    # A fact the event already carries. `/implement 18` on issue #18 should not need
                    # the number repeated — behind anything explicit, ahead of the literal default,
                    # because what the payload knows beats what the spec guessed.
                    sources.append(EVENT_SOURCES[parameter.from_event])
                if parameter.default:
                    # Last, so an explicit value always wins over it. Single quotes doubled, which
                    # is how a GitHub expression escapes one.
                    sources.append("'" + parameter.default.replace("'", "''") + "'")
                text = text.replace(
                    "{" + parameter.name + "}", "${{ " + " || ".join(sources) + " }}"
                )
            if chat:
                # What the human actually wrote after the command — usually the point of the run.
                text = text.replace("{instruction}", "${{ needs.command-gate.outputs.instruction }}")
                text = text.replace("{pull_request}", "${{ needs.command-gate.outputs.pull_request }}")
                # The bare words after the command, as a JSON array — what a pipeline fans out over
                # when one invocation asks for several things at once.
                text = text.replace("{positional}", "${{ needs.command-gate.outputs.positional }}")
        for key, value in self.resolved_values().items():
            text = text.replace("{" + key + "}", value)
        # Where this pipeline's own files live, from the repository root. A step reading one of
        # them — an aspects directory, a template — needs the path the workflow will actually use.
        text = text.replace("{lockstep}", self.spec.repo_path(".").rstrip("/.") or ".")
        text = text.replace("{output_dir}", self.output_dir_env)
        text = text.replace("{state_db}", self.state_db_path)
        return text.replace("{pipeline_name}", self.spec.manifest.name)
