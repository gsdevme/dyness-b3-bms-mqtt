"""Requirement traceability: every MUST in ``docs/specs/`` is cited by a test (TEST-05..10).

The plugin parses the spec tree for requirement definitions (a bold ``**PREFIX-NN**`` whose
prefix is in the table in ``00-overview.md`` §4.1), reads every collected test's ``req``
markers (module and class marks included) and enforces the rules of TEST-06:

1. every ``must`` requirement is cited by a test that is not marked ``hardware`` or
   ``mosquitto``. Enabled by ``--req-strict``, which ``pyproject.toml`` puts in ``addopts``
   so it is on by default, and only a warning when the run is narrowed (test paths, ``-k``,
   ``-m``, ``--deselect``, ``--lf``, stepwise, a collection error, or any deselection of a
   test that could cover a requirement; deselecting ``mosquitto`` tests when ``--mosquitto``
   is not given therefore leaves the default run whole);
2. no ``req`` marker names an undefined or withdrawn identifier;
3. no identifier is defined more than once;
4. every ``req`` argument matches ``ID_PATTERN``.

Rules 2-4 always apply, to every collected test including deselected ones. A failure turns a
passing session's exit status into ``TESTS_FAILED`` and lists every offending identifier
grouped by spec file. Every session ends with the line
``reqtrace: N must / N covered / N manual / N should``; ``--req-report`` adds the full
matrix and the ``manual`` identifiers to tick against the acceptance checklist.

Requirement text is classified with code spans blanked out, so a ``MUST`` or ``**MANUAL**``
quoted in backticks does not count.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

type Status = Literal["withdrawn", "manual", "must", "should"]
type Groups = dict[str, dict[str, list[str]]]

ID_PATTERN = re.compile(r"^[A-Z]+-[0-9]{2}$")
OVERVIEW = "00-overview.md"
NO_SPEC = "(no spec file)"

_DEFINITION = re.compile(r"(?<!\*)\*\*([A-Z]+-[0-9]{2})\*\*(?!\*)")
_CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}(\s|$)")
_PREFIX_SECTION = re.compile(r"^ {0,3}#{1,6}\s+4\.1\s")
_PREFIX_ROW = re.compile(r"^\|\s*`([^`]+\.md)`\s*\|\s*`([A-Z]+)`\s*\|\s*$")
_MUST = re.compile(r"\bMUST\b")


@dataclass
class Requirement:
    """One requirement definition and the node IDs of the tests that cover it."""

    ident: str
    spec: str
    status: Status
    covered_by: list[str] = field(default_factory=list)


def classify(text: str) -> Status:
    """Return the status of a requirement from its text (TEST-05), first rule winning."""
    if "(withdrawn)" in text:
        return "withdrawn"
    if "**MANUAL**" in text:
        return "manual"
    if _MUST.search(text):
        return "must"
    return "should"


def _blank_code_spans(line: str) -> str:
    """Return ``line`` with every inline code span replaced by spaces of the same length."""
    return _CODE_SPAN.sub(lambda match: " " * len(match.group(0)), line)


def _unfenced(text: str) -> Iterator[str | None]:
    """Yield each line of ``text``, or ``None`` for fence lines and the lines inside a fence."""
    fence: str | None = None
    for line in text.splitlines():
        if fence is not None:
            stripped = line.strip()
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = None
            yield None
        elif opening := _FENCE.match(line):
            fence = opening.group(1)
            yield None
        else:
            yield line


def known_prefixes(spec_dir: Path) -> dict[str, str]:
    """Return ``{prefix: spec file name}`` from the table in ``00-overview.md`` §4.1.

    Raises ``ValueError`` when the section holds no prefix rows.
    """
    path = spec_dir / OVERVIEW
    prefixes: dict[str, str] = {}
    in_section = False
    for line in _unfenced(path.read_text(encoding="utf-8")):
        if line is None:
            continue
        if _HEADING.match(line):
            in_section = bool(_PREFIX_SECTION.match(line))
        elif in_section and (row := _PREFIX_ROW.match(line)):
            prefixes[row.group(2)] = row.group(1)
    if not prefixes:
        raise ValueError(f"no prefix table in section 4.1 of {path}")
    return prefixes


def _definitions(text: str, prefixes: Iterable[str]) -> list[tuple[str, str]]:
    """Return ``(identifier, requirement text)`` for each definition in one spec file.

    A requirement's text runs from its identifier to the next definition, heading, table
    row, blank line or fence. Code spans are blanked before matching and in the text.
    """
    known = set(prefixes)
    found: list[tuple[str, str]] = []
    ident: str | None = None
    body: list[str] = []

    def close() -> None:
        nonlocal ident
        if ident is not None:
            found.append((ident, " ".join(body)))
        ident = None
        body.clear()

    for raw in _unfenced(text):
        if raw is None or not raw.strip():
            close()
            continue
        boundary = bool(_HEADING.match(raw)) or raw.lstrip().startswith("|")
        if boundary:
            close()
        line = _blank_code_spans(raw)
        cursor = 0
        for match in _DEFINITION.finditer(line):
            if match.group(1).split("-")[0] not in known:
                continue
            if ident is not None:
                body.append(line[cursor : match.start()])
            close()
            ident = match.group(1)
            cursor = match.end()
        if ident is not None:
            body.append(line[cursor:])
        if boundary:
            close()
    close()
    return found


def parse_specs(spec_dir: Path) -> tuple[dict[str, Requirement], dict[str, list[str]]]:
    """Parse every ``*.md`` in ``spec_dir`` for requirement definitions (TEST-05).

    Returns the requirements by identifier (first definition wins) and, for each identifier
    defined more than once, the spec file of every definition.
    """
    prefixes = known_prefixes(spec_dir)
    requirements: dict[str, Requirement] = {}
    seen: dict[str, list[str]] = defaultdict(list)
    for path in sorted(spec_dir.glob("*.md")):
        for ident, text in _definitions(path.read_text(encoding="utf-8"), prefixes):
            seen[ident].append(path.name)
            requirements.setdefault(ident, Requirement(ident, path.name, classify(text)))
    duplicates = {ident: specs for ident, specs in seen.items() if len(specs) > 1}
    return requirements, duplicates


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--req-strict``, ``--req-report`` and ``--req-specs``."""
    group = parser.getgroup("reqtrace", "requirement traceability")
    group.addoption(
        "--req-strict",
        action="store_true",
        default=False,
        help="fail when a MUST requirement is not cited by any test (whole-suite runs only)",
    )
    group.addoption(
        "--req-report",
        action="store_true",
        default=False,
        help="print the requirement matrix and the list of MANUAL requirements",
    )
    group.addoption(
        "--req-specs",
        metavar="PATH",
        default=None,
        help="spec directory to parse (default: <rootdir>/docs/specs)",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the session object as ``reqtrace-session``."""
    config.pluginmanager.register(ReqTrace(config), "reqtrace-session")


def _cited(item: pytest.Item) -> list[object]:
    """Return every ``req`` argument on ``item``, its class and its module, once each."""
    args = [arg for mark in item.iter_markers("req") for arg in mark.args]
    return list(dict.fromkeys(args))


def _counts_as_coverage(item: pytest.Item) -> bool:
    """Return whether ``item`` may cover a requirement: not ``hardware`` or ``mosquitto``."""
    return not any(next(item.iter_markers(name), None) for name in ("hardware", "mosquitto"))


def _add(groups: Groups, group: str, label: str, nodeid: str) -> None:
    """Record that ``nodeid`` produced ``label`` under spec file ``group``."""
    nodes = groups.setdefault(group, {}).setdefault(label, [])
    if nodeid not in nodes:
        nodes.append(nodeid)


def _ordered(groups: Iterable[str]) -> list[str]:
    """Return spec file group names sorted, with ``NO_SPEC`` last."""
    return sorted(groups, key=lambda group: (group == NO_SPEC, group))


def _by_spec(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """Return ``"  spec: A, B"`` lines for ``(spec, identifier)`` pairs, one per spec file."""
    grouped: dict[str, list[str]] = {}
    for spec, ident in pairs:
        if ident not in grouped.setdefault(spec, []):
            grouped[spec].append(ident)
    return [f"  {spec}: {', '.join(grouped[spec])}" for spec in _ordered(grouped)]


def _plural(count: int, noun: str) -> str:
    """Return ``"1 noun"`` or ``"N nouns"``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class ReqTrace:
    """Per-session traceability state and the hooks that check and report it."""

    def __init__(self, config: pytest.Config) -> None:
        """Hold ``config``; the specs are parsed when the session starts."""
        self.config = config
        self.requirements: dict[str, Requirement] = {}
        self.duplicates: dict[str, list[str]] = {}
        self.prefixes: dict[str, str] = {}
        self.collected: list[pytest.Item] = []
        self.deselected = False
        self.collection_failed = False
        self.undefined: Groups = {}
        self.withdrawn: Groups = {}
        self.malformed: Groups = {}
        self.finished = False

    def spec_dir(self) -> Path:
        """Return the spec directory from ``--req-specs`` or ``<rootdir>/docs/specs``."""
        option: str | None = self.config.getoption("--req-specs")
        if option is None:
            return self.config.rootpath / "docs" / "specs"
        return self.config.invocation_params.dir / option

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        """Parse the spec tree; an unreadable tree is a usage error."""
        spec_dir = self.spec_dir()
        try:
            self.prefixes = known_prefixes(spec_dir)
            self.requirements, self.duplicates = parse_specs(spec_dir)
        except (OSError, ValueError) as error:
            raise pytest.UsageError(
                f"reqtrace: cannot parse specs in {spec_dir}: {error}"
            ) from error

    def pytest_itemcollected(self, item: pytest.Item) -> None:
        """Remember every collected test, selected or not, for rules 2-4."""
        self.collected.append(item)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """Note a collection error: the suite was not wholly collected."""
        if report.failed:
            self.collection_failed = True

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        """Note that the run was narrowed by deselecting a test that could cover (TEST-07)."""
        if any(_counts_as_coverage(item) for item in items):
            self.deselected = True

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Check every reference (rules 2 and 4) and record coverage by the selected tests."""
        for item in self.collected:
            for arg in _cited(item):
                ident = arg if isinstance(arg, str) else ""
                group = self.prefixes.get(ident.split("-")[0], NO_SPEC)
                if not ID_PATTERN.fullmatch(ident):
                    _add(self.malformed, group, repr(arg), item.nodeid)
                elif ident not in self.requirements:
                    _add(self.undefined, group, ident, item.nodeid)
                elif self.requirements[ident].status == "withdrawn":
                    _add(self.withdrawn, group, ident, item.nodeid)
        for item in session.items:
            if not _counts_as_coverage(item):
                continue
            for arg in _cited(item):
                requirement = self.requirements.get(arg) if isinstance(arg, str) else None
                if requirement is not None and requirement.status != "withdrawn":
                    requirement.covered_by.append(item.nodeid)
        self.finished = True

    def narrowing(self) -> str | None:
        """Return why rule 1 cannot judge this run, or ``None`` for a whole-suite run."""
        config = self.config
        options = (
            ("keyword", "-k"),
            ("markexpr", "-m"),
            ("deselect", "--deselect"),
            ("lf", "--lf"),
            ("stepwise", "--stepwise"),
            ("stepwise_skip", "--stepwise-skip"),
        )
        if config.args_source == pytest.Config.ArgsSource.ARGS:
            return "test paths"
        for dest, flag in options:
            if config.getoption(dest, default=None):
                return flag
        if self.deselected:
            return "deselection"
        if self.collection_failed:
            return "collection errors"
        return None

    def uncovered(self) -> list[Requirement]:
        """Return the ``must`` requirements no counting test cites."""
        return [
            req for req in self.requirements.values() if req.status == "must" and not req.covered_by
        ]

    def rule_1_reason(self) -> str | None:
        """Return why rule 1 is only a warning, or ``None`` when it fails the session."""
        if not self.config.getoption("--req-strict"):
            return "--req-strict not given"
        narrowed = self.narrowing()
        return None if narrowed is None else f"narrowed run: {narrowed}"

    def failed(self) -> bool:
        """Return whether any enforced rule is broken."""
        rule_1 = bool(self.uncovered()) and self.rule_1_reason() is None
        return rule_1 or any((self.undefined, self.withdrawn, self.malformed, self.duplicates))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        """Turn a passing (or empty) session into a failed one when a rule is broken."""
        passing = (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED)
        if self.finished and self.failed() and session.exitstatus in passing:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def summary(self) -> str:
        """Return the one-line count summary of TEST-09."""
        statuses = [req.status for req in self.requirements.values()]
        covered = statuses.count("must") - len(self.uncovered())
        return (
            f"reqtrace: {statuses.count('must')} must / {covered} covered / "
            f"{statuses.count('manual')} manual / {statuses.count('should')} should"
        )

    def problem_lines(self) -> Iterator[tuple[str, bool]]:
        """Yield ``(line, is_error)`` for every broken rule, grouped by spec file."""
        uncovered = self.uncovered()
        reason = self.rule_1_reason()
        if uncovered and reason is None:
            yield "reqtrace: error: MUST requirements not covered by any test:", True
            for line in _by_spec((req.spec, req.ident) for req in uncovered):
                yield line, True
        for title, groups in (
            ("req markers naming undefined identifiers", self.undefined),
            ("req markers naming withdrawn identifiers", self.withdrawn),
            (f"malformed req arguments (expected {ID_PATTERN.pattern})", self.malformed),
        ):
            if groups:
                yield f"reqtrace: error: {title}:", True
                for group in _ordered(groups):
                    for label, nodes in groups[group].items():
                        yield f"  {group}: {label} ({', '.join(nodes)})", True
        if self.duplicates:
            yield "reqtrace: error: requirements defined more than once:", True
            pairs = [(spec, ident) for ident, specs in self.duplicates.items() for spec in specs]
            for line in _by_spec(pairs):
                yield line, True
        if uncovered and reason is not None:
            count = _plural(len(uncovered), "MUST requirement")
            yield f"reqtrace: warning: {count} not covered ({reason})", False

    def report_lines(self) -> Iterator[str]:
        """Yield the ``--req-report`` matrix, grouped by spec file."""
        width = max((len(ident) for ident in self.requirements), default=0)
        spec: str | None = None
        for req in sorted(self.requirements.values(), key=lambda req: req.spec):
            if req.spec != spec:
                spec = req.spec
                yield f"{spec}:"
            nodes = ", ".join(req.covered_by) or "-"
            yield f"  {req.ident:<{width}}  {req.status:<9}  {nodes}"

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        """Print the problems, the optional report and the summary line."""
        if not self.finished:
            return
        if self.config.getoption("--req-report"):
            terminalreporter.write_sep("-", "requirement matrix")
            for line in self.report_lines():
                terminalreporter.write_line(line)
            terminalreporter.write_sep(
                "-", "manual requirements (tick against the hardware acceptance checklist)"
            )
            manual = [req for req in self.requirements.values() if req.status == "manual"]
            for line in _by_spec((req.spec, req.ident) for req in manual):
                terminalreporter.write_line(line)
        for line, is_error in self.problem_lines():
            terminalreporter.write_line(line, red=is_error, yellow=not is_error)
        terminalreporter.write_line(self.summary())
