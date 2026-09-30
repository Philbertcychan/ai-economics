"""The stack: the AI build-out as a chain of stages, each selling one unit to the next.

Why a chain
-----------
The company models answer what a dollar of GPU compute earns. The stack answers where that
dollar has to travel first: a megawatt of firm power becomes a connected megawatt, a rack-ready
megawatt, wafers and memory, GPUs in racks, GPU-hours, tokens, and finally an application
someone pays for. Each stage has its own unit, its own lead time and its own suppliers, and the
tightest stage at any moment decides how fast everything downstream can grow. Keeping the chain
as data, one folder of CSVs under ``stack/``, lets a reader trace any figure on the site to its
row and source, and lets the stages be re-ordered or split without touching code.

Files
-----
``stages.csv``             the chain, one row per stage, in ``order``
``consumption_tiers.csv``  the demand side of the last stage, split by tokens per user per day
``metrics.csv``            one row per figure about a stage (capacity, price, lead time)
``players.csv``            one row per company per stage, with its role
``conversions.csv``        factors that turn one stage's unit into another's (MW per wafer...)
``campuses.csv``           the demand side of the data-centre stage: one row per announced AI
                           campus with its planned and operating MW, each sourced (optional
                           file; a missing one means no campuses yet)
``primers/<stage>.md``     a markdown primer per stage, rendered on the stage's page

Columns
-------
stages:            ``order`` (integer, unique), ``stage`` (key: lower-case letters, digits, ``-``
                   and ``_``; also the page and primer file name), ``name``, ``unit``, ``sells``,
                   ``buys_from`` (stage keys separated by ``;``), ``lead_time_years`` (number, may
                   be blank), ``bottleneck_score`` (blank or an integer 1 to 5),
                   ``bottleneck_note``, ``status`` (``skeleton`` or ``deep``), ``summary``
consumption_tiers: ``tier`` (key, unique), ``name``, ``examples``, ``tokens_per_user_day`` (text:
                   a range or a floor reads better than a false point), ``revenue_model``, ``note``
metrics:           ``stage``, ``metric``, ``value`` (number), ``unit``, ``as_of``, ``scope``,
                   ``source_url``, ``source``, ``confidence``, ``note``
players:           ``stage``, ``company``, ``ticker``, ``role``, ``listed`` (``true``/``false``),
                   ``note``, ``source_url``
conversions:       ``from_stage``, ``to_stage``, ``factor`` (number), ``unit``, ``as_of``,
                   ``source_url``, ``source``, ``confidence``, ``note``
campuses:          ``campus`` (key, unique), ``sponsor``, ``developer``, ``state`` (two capital
                   letters), then two figures, ``planned`` and ``operating``, each as
                   ``<figure>_mw`` (number or blank), ``<figure>_as_of`` (``YYYY-MM`` or
                   ``YYYY-MM-DD`` or blank), ``<figure>_source_url``, ``<figure>_source``,
                   ``<figure>_confidence``, plus ``planned_basis`` (what the planned number
                   counts: IT load, total power...), ``power_source``, ``status``, ``note``.
                   A figure that is present needs its ``source_url`` and its ``confidence``; a
                   blank figure needs neither.

``confidence`` is ``high``, ``medium`` or ``low``. Every ``stage`` reference must name a row of
``stages.csv``. A ``source_url`` is blank or starts with ``http``. Blank text stays blank (never
NaN) so the site can test for it.

Whose judgement
---------------
Metrics, players, conversions and campuses are research: each row carries its source and a
confidence.
``bottleneck_score``, ``bottleneck_note`` and ``status`` are not research; they are the owner's
(Philbert's) judgement about where the chain binds and how far each stage has been worked, and
only he edits them. The loaders check their form, never their substance.

A malformed file raises ``ValueError`` naming the file and the row, because a silently skipped
row is worse than a loud failure.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Iterable
from pathlib import Path

import pandas as pd

from data import STACK_DIR, STACK_PRIMERS_DIR

STAGE_COLUMNS = (
    "order",
    "stage",
    "name",
    "unit",
    "sells",
    "buys_from",
    "lead_time_years",
    "bottleneck_score",
    "bottleneck_note",
    "status",
    "summary",
)
CONSUMPTION_TIER_COLUMNS = (
    "tier",
    "name",
    "examples",
    "tokens_per_user_day",
    "revenue_model",
    "note",
)
METRIC_COLUMNS = (
    "stage",
    "metric",
    "value",
    "unit",
    "as_of",
    "scope",
    "source_url",
    "source",
    "confidence",
    "note",
)
PLAYER_COLUMNS = ("stage", "company", "ticker", "role", "listed", "note", "source_url")
CONVERSION_COLUMNS = (
    "from_stage",
    "to_stage",
    "factor",
    "unit",
    "as_of",
    "source_url",
    "source",
    "confidence",
    "note",
)

# The two figures a campus row carries, each with its own mw, as_of, source_url, source and
# confidence columns; ``planned`` also has a ``basis`` saying what the number counts.
CAMPUS_FIGURES = ("planned", "operating")
CAMPUS_COLUMNS = (
    "campus",
    "sponsor",
    "developer",
    "state",
    "planned_mw",
    "planned_basis",
    "planned_as_of",
    "planned_source_url",
    "planned_source",
    "planned_confidence",
    "operating_mw",
    "operating_as_of",
    "operating_source_url",
    "operating_source",
    "operating_confidence",
    "power_source",
    "status",
    "note",
)

CONFIDENCES = ("high", "medium", "low")
# Optional columns of campuses.csv: the owner's likelihood score (1 to 5, from the rubric in
# the data-centre primer) and the sentence behind it. Both may be blank; a proposed score is
# one Claude suggested from the evidence, which the owner confirms or overrides in the note.
CAMPUS_LIKELIHOOD_COLUMNS = ("likelihood_proposed", "likelihood_basis")
LIKELIHOOD_RANGE = (1, 5)
# What ``load_campuses`` returns: the file's required columns plus the optional two, filled
# with blanks when the file lacks them, so the site can rely on every column being there.
CAMPUS_FRAME_COLUMNS = (*CAMPUS_COLUMNS, *CAMPUS_LIKELIHOOD_COLUMNS)
# campus_evidence.csv: one sourced claim per row behind a campus's likelihood score.
CAMPUS_EVIDENCE_COLUMNS = (
    "campus",
    "criterion",
    "claim",
    "as_of",
    "source_url",
    "source",
    "confidence",
)
CAMPUS_CRITERIA = (
    "power_secured",
    "interconnection",
    "permits",
    "water",
    "construction",
    "financing",
)
STATUSES = ("skeleton", "deep")
BOTTLENECK_RANGE = (1, 5)

# A stage key names a page (``stack/<stage>.html``) and a primer (``primers/<stage>.md``), so it
# is restricted to characters that cannot escape those folders or need URL-encoding.
_STAGE_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_STATE_RE = re.compile(r"[A-Z]{2}")
# A month or a day: the precision a source gives, no more (a press release dates a plan to the
# day, a directory page to the month it was last updated).
_AS_OF_RE = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?")


def split_stages(text: str) -> list[str]:
    """``"silicon; memory" -> ["silicon", "memory"]``; blank input gives an empty list."""
    return [part.strip() for part in str(text).split(";") if part.strip()]


# --------------------------------------------------------------------------------------------
# Shared checks. Each returns problems as "row N (key): message" so the error names the line the
# reader sees in Excel (line 1 is the header, so the first data row is row 2).
# --------------------------------------------------------------------------------------------


def _read(path: Path, columns: tuple[str, ...], optional: tuple[str, ...] = ()) -> pd.DataFrame:
    """Read a CSV as text with blanks kept as ``""``; ``FileNotFoundError`` when it is absent.

    ``columns`` must all be present; ``optional`` ones are kept when the file has them and
    added blank when it does not, so callers see one shape either way.

    The frame is indexed by the physical line number of each row, so a later check can name
    the line the reader sees in Excel. The ``csv`` module is used instead of ``pd.read_csv``
    because a row with an unquoted comma (the usual way a hand-edited CSV breaks) must be
    reported by line, and pandas' C parser gives an opaque error for it.
    """
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = [cell.strip() for cell in next(reader, [])]
        if not header:
            raise ValueError(f"{path.name}: empty file, expected a header row")
        missing = [c for c in columns if c not in header]
        if missing:
            raise ValueError(f"{path.name}: missing columns {missing}")
        rows: list[list[str]] = []
        lines: list[int] = []
        problems: list[str] = []
        for fields in reader:
            if not any(cell.strip() for cell in fields):
                continue  # a blank line, usually the trailing one
            if len(fields) != len(header):
                problems.append(
                    f"row {reader.line_num} has {len(fields)} fields, expected {len(header)} "
                    "(an unquoted comma in a text field?)"
                )
                continue
            rows.append([cell.strip() for cell in fields])
            lines.append(reader.line_num)
    _raise(path, problems)
    frame = pd.DataFrame(rows, columns=header, index=lines, dtype=str)
    for column in optional:
        if column not in frame.columns:
            frame[column] = ""
    return frame[[*columns, *optional]].copy()


def _rows(frame: pd.DataFrame, key: Callable[[pd.Series], str]) -> list[str]:
    """``"row 3 (power)"`` labels, one per row, from the line numbers ``_read`` kept as index."""
    return [f"row {line} ({key(row)})" for line, row in frame.iterrows()]


def _check_numeric(
    frame: pd.DataFrame, column: str, labels: list[str], *, required: bool
) -> tuple[pd.Series, list[str]]:
    """Parse ``column`` to float (NaN for blank) and list the rows whose text is not a number."""
    text = frame[column]
    numbers = pd.to_numeric(text.where(text != ""), errors="coerce").astype("float64")
    problems = []
    for label, raw, number in zip(labels, text, numbers, strict=True):
        if raw == "":
            if required:
                problems.append(f"{label}: {column} is blank")
        elif pd.isna(number):
            problems.append(f"{label}: {column} {raw!r} is not a number")
    return numbers, problems


def _check_enum(
    frame: pd.DataFrame, column: str, allowed: Iterable[str], labels: list[str]
) -> list[str]:
    allowed = tuple(allowed)
    return [
        f"{label}: {column} must be one of {allowed}, not {value!r}"
        for label, value in zip(labels, frame[column], strict=True)
        if value not in allowed
    ]


def _check_blank(frame: pd.DataFrame, column: str, labels: list[str]) -> list[str]:
    return [
        f"{label}: {column} is blank"
        for label, value in zip(labels, frame[column], strict=True)
        if value == ""
    ]


def _check_url(frame: pd.DataFrame, labels: list[str]) -> list[str]:
    return [
        f"{label}: source_url {value!r} does not start with http"
        for label, value in zip(labels, frame["source_url"], strict=True)
        if value and not value.startswith("http")
    ]


def _check_stage_refs(
    frame: pd.DataFrame, column: str, known: set[str], labels: list[str], *, multi: bool = False
) -> list[str]:
    """Rows whose ``column`` names a stage that is not in ``stages.csv``."""
    problems = []
    for label, value in zip(labels, frame[column], strict=True):
        refs = split_stages(value) if multi else [value]
        unknown = [ref for ref in refs if ref not in known]
        if unknown:
            problems.append(f"{label}: {column} names unknown stage(s) {unknown}")
    return problems


def _raise(path: Path, problems: list[str]) -> None:
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))


# --------------------------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------------------------


def load_stages(directory: Path = STACK_DIR) -> pd.DataFrame:
    """The chain in ``order``; ``order`` is int, ``lead_time_years`` float (NaN when blank),
    ``bottleneck_score`` nullable ``Int64``; everything else text with blanks kept as ``""``.

    Raises ``ValueError`` naming the row for a duplicate key or order, a non-numeric or missing
    order, a bad key, an unknown ``buys_from`` stage, a score outside 1 to 5, or a bad status.
    """
    path = Path(directory) / "stages.csv"
    frame = _read(path, STAGE_COLUMNS)
    labels = _rows(frame, lambda row: row["stage"] or "?")
    problems: list[str] = []

    problems += _check_blank(frame, "stage", labels)
    problems += [
        f"{label}: stage {value!r} must be lower-case letters, digits, '-' or '_'"
        for label, value in zip(labels, frame["stage"], strict=True)
        if value and not _STAGE_KEY_RE.fullmatch(value)
    ]
    duplicates = frame.loc[frame["stage"].duplicated() & (frame["stage"] != ""), "stage"]
    if not duplicates.empty:
        problems.append(f"duplicate stage keys {sorted(set(duplicates))}")
    problems += _check_blank(frame, "name", labels)

    order, order_problems = _check_numeric(frame, "order", labels, required=True)
    problems += order_problems
    problems += [
        f"{label}: order {raw!r} is not an integer"
        for label, raw, number in zip(labels, frame["order"], order, strict=True)
        if not pd.isna(number) and number != int(number)
    ]
    whole_orders = order.dropna()
    repeated = whole_orders[whole_orders.duplicated()]
    if not repeated.empty:
        problems.append(f"duplicate order values {sorted({int(o) for o in repeated})}")

    lead_time, lead_problems = _check_numeric(frame, "lead_time_years", labels, required=False)
    problems += lead_problems
    problems += [
        f"{label}: lead_time_years {number:g} is negative"
        for label, number in zip(labels, lead_time, strict=True)
        if not pd.isna(number) and number < 0
    ]

    score, score_problems = _check_numeric(frame, "bottleneck_score", labels, required=False)
    problems += score_problems
    low, high = BOTTLENECK_RANGE
    problems += [
        f"{label}: bottleneck_score {raw!r} must be blank or an integer {low} to {high}"
        for label, raw, number in zip(labels, frame["bottleneck_score"], score, strict=True)
        if not pd.isna(number) and (number != int(number) or not low <= number <= high)
    ]

    problems += _check_enum(frame, "status", STATUSES, labels)
    known = set(frame["stage"]) - {""}
    problems += _check_stage_refs(frame, "buys_from", known, labels, multi=True)
    problems += [
        f"{label}: buys_from names the stage itself"
        for label, stage, refs in zip(labels, frame["stage"], frame["buys_from"], strict=True)
        if stage in split_stages(refs)
    ]
    _raise(path, problems)

    frame["order"] = order.astype("int64")
    frame["lead_time_years"] = lead_time
    frame["bottleneck_score"] = score.astype("Int64")
    return frame.sort_values("order", kind="stable").reset_index(drop=True)


def _stage_keys(directory: Path) -> set[str]:
    return set(load_stages(directory)["stage"])


def load_consumption_tiers(directory: Path = STACK_DIR) -> pd.DataFrame:
    """The demand tiers in file order; all columns text. Tier keys must be present and unique."""
    path = Path(directory) / "consumption_tiers.csv"
    frame = _read(path, CONSUMPTION_TIER_COLUMNS)
    labels = _rows(frame, lambda row: row["tier"] or "?")
    problems = _check_blank(frame, "tier", labels) + _check_blank(frame, "name", labels)
    duplicates = frame.loc[frame["tier"].duplicated() & (frame["tier"] != ""), "tier"]
    if not duplicates.empty:
        problems.append(f"duplicate tier keys {sorted(set(duplicates))}")
    _raise(path, problems)
    return frame.reset_index(drop=True)


def load_metrics(directory: Path = STACK_DIR) -> pd.DataFrame:
    """Figures about stages in file order (the site shows the first two per stage as key figures).

    ``value`` is float and required; ``stage`` must exist in ``stages.csv``; ``confidence`` is
    one of ``CONFIDENCES``; ``source_url`` is blank or ``http...``.
    """
    path = Path(directory) / "metrics.csv"
    frame = _read(path, METRIC_COLUMNS)
    labels = _rows(frame, lambda row: f"{row['stage'] or '?'} / {row['metric'] or '?'}")
    known = _stage_keys(directory)
    value, problems = _check_numeric(frame, "value", labels, required=True)
    problems += _check_blank(frame, "metric", labels)
    problems += _check_stage_refs(frame, "stage", known, labels)
    problems += _check_enum(frame, "confidence", CONFIDENCES, labels)
    problems += _check_url(frame, labels)
    _raise(path, problems)
    frame["value"] = value
    return frame.reset_index(drop=True)


def load_players(directory: Path = STACK_DIR) -> pd.DataFrame:
    """Companies per stage in file order; ``listed`` becomes bool.

    ``company`` is required, ``stage`` must exist, ``listed`` is ``true`` or ``false`` (any
    case), ``source_url`` is blank or ``http...``.
    """
    path = Path(directory) / "players.csv"
    frame = _read(path, PLAYER_COLUMNS)
    labels = _rows(frame, lambda row: f"{row['stage'] or '?'} / {row['company'] or '?'}")
    known = _stage_keys(directory)
    listed = frame["listed"].str.lower()
    problems = _check_blank(frame, "company", labels)
    problems += _check_stage_refs(frame, "stage", known, labels)
    problems += [
        f"{label}: listed must be true or false, not {value!r}"
        for label, value in zip(labels, frame["listed"], strict=True)
        if value.lower() not in ("true", "false")
    ]
    problems += _check_url(frame, labels)
    _raise(path, problems)
    frame["listed"] = (listed == "true").astype(bool)
    return frame.reset_index(drop=True)


def load_conversions(directory: Path = STACK_DIR) -> pd.DataFrame:
    """Unit conversions between stages in file order; ``factor`` is float and required.

    Both stages must exist in ``stages.csv``; ``confidence`` and ``source_url`` are checked as
    for metrics.
    """
    path = Path(directory) / "conversions.csv"
    frame = _read(path, CONVERSION_COLUMNS)
    labels = _rows(frame, lambda row: f"{row['from_stage'] or '?'} -> {row['to_stage'] or '?'}")
    known = _stage_keys(directory)
    factor, problems = _check_numeric(frame, "factor", labels, required=True)
    problems += _check_stage_refs(frame, "from_stage", known, labels)
    problems += _check_stage_refs(frame, "to_stage", known, labels)
    problems += _check_enum(frame, "confidence", CONFIDENCES, labels)
    problems += _check_url(frame, labels)
    _raise(path, problems)
    frame["factor"] = factor
    return frame.reset_index(drop=True)


def _empty_campuses() -> pd.DataFrame:
    """The frame ``load_campuses`` returns when there is no file: same columns, same dtypes."""
    mw_columns = {f"{figure}_mw" for figure in CAMPUS_FIGURES}
    return pd.DataFrame(
        {
            column: pd.Series(dtype="float64" if column in mw_columns else "str")
            for column in CAMPUS_FRAME_COLUMNS
        }
    )


def load_campuses(directory: Path = STACK_DIR) -> pd.DataFrame:
    """Announced AI data-centre campuses in file order; the two ``*_mw`` columns are float
    (NaN when blank), everything else text with blanks kept as ``""``.

    The file is optional: a missing ``campuses.csv`` gives an empty frame with the right
    columns, because campuses are the demand side of one stage and the chain stands without
    them. When the file exists every problem is reported at once, naming the row and the
    campus: a blank or duplicate ``campus``, a ``state`` that is not two capital letters, a
    ``*_mw`` that is not a number or is negative, a present figure without its ``source_url``
    (``http...``) or its ``confidence`` (one of ``CONFIDENCES``), a ``source_url`` or
    ``confidence`` that is malformed even beside a blank figure, an ``*_as_of`` that is not
    ``YYYY-MM`` or ``YYYY-MM-DD``.
    """
    path = Path(directory) / "campuses.csv"
    if not path.is_file():
        return _empty_campuses()
    frame = _read(path, CAMPUS_COLUMNS, optional=CAMPUS_LIKELIHOOD_COLUMNS)
    labels = _rows(frame, lambda row: row["campus"] or "?")
    problems: list[str] = _check_blank(frame, "campus", labels)
    duplicates = frame.loc[frame["campus"].duplicated() & (frame["campus"] != ""), "campus"]
    if not duplicates.empty:
        problems.append(f"duplicate campus names {sorted(set(duplicates))}")
    problems += [
        f"{label}: state {value!r} must be two capital letters"
        for label, value in zip(labels, frame["state"], strict=True)
        if not _STATE_RE.fullmatch(value)
    ]

    numbers: dict[str, pd.Series] = {}
    for figure in CAMPUS_FIGURES:
        mw, mw_problems = _check_numeric(frame, f"{figure}_mw", labels, required=False)
        numbers[f"{figure}_mw"] = mw
        problems += mw_problems
        problems += [
            f"{label}: {figure}_mw {number:g} is negative"
            for label, number in zip(labels, mw, strict=True)
            if not pd.isna(number) and number < 0
        ]
        rows = zip(
            labels,
            frame[f"{figure}_mw"],
            frame[f"{figure}_source_url"],
            frame[f"{figure}_confidence"],
            frame[f"{figure}_as_of"],
            strict=True,
        )
        for label, raw_mw, url, confidence, as_of in rows:
            present = raw_mw != ""
            if url and not url.startswith("http"):
                problems.append(f"{label}: {figure}_source_url {url!r} does not start with http")
            elif present and not url:
                problems.append(f"{label}: {figure}_mw {raw_mw!r} has no {figure}_source_url")
            if confidence and confidence not in CONFIDENCES:
                problems.append(
                    f"{label}: {figure}_confidence must be one of {CONFIDENCES}, not {confidence!r}"
                )
            elif present and not confidence:
                problems.append(f"{label}: {figure}_mw {raw_mw!r} has no {figure}_confidence")
            if as_of and not _AS_OF_RE.fullmatch(as_of):
                problems.append(f"{label}: {figure}_as_of {as_of!r} is not YYYY-MM or YYYY-MM-DD")
    low, high = LIKELIHOOD_RANGE
    for label, score in zip(labels, frame["likelihood_proposed"], strict=True):
        if score and not (score.isdigit() and low <= int(score) <= high):
            problems.append(
                f"{label}: likelihood_proposed {score!r} must be blank or {low} to {high}"
            )
    _raise(path, problems)
    for column, mw in numbers.items():
        frame[column] = mw
    return frame.reset_index(drop=True)


def _empty_campus_evidence() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="str") for c in CAMPUS_EVIDENCE_COLUMNS})


def load_campus_evidence(directory: Path = STACK_DIR) -> pd.DataFrame:
    """The sourced claims behind the campus likelihood scores, in file order; all text.

    Optional like ``campuses.csv``: no file, an empty frame. Every problem is reported at once,
    naming the row and the campus: a blank ``campus`` or ``claim``, a ``criterion`` outside
    ``CAMPUS_CRITERIA``, a ``source_url`` that does not start with ``http``, a ``confidence``
    outside ``CONFIDENCES``, an ``as_of`` that is not ``YYYY-MM`` or ``YYYY-MM-DD``. Whether the
    campus exists in ``campuses.csv`` is the site's business: a claim about a campus that is
    not listed is simply not shown.
    """
    path = Path(directory) / "campus_evidence.csv"
    if not path.is_file():
        return _empty_campus_evidence()
    frame = _read(path, CAMPUS_EVIDENCE_COLUMNS)
    labels = _rows(frame, lambda row: row["campus"] or "?")
    problems = _check_blank(frame, "campus", labels) + _check_blank(frame, "claim", labels)
    for label, criterion, url, confidence, as_of in zip(
        labels,
        frame["criterion"],
        frame["source_url"],
        frame["confidence"],
        frame["as_of"],
        strict=True,
    ):
        if criterion not in CAMPUS_CRITERIA:
            problems.append(
                f"{label}: criterion must be one of {CAMPUS_CRITERIA}, not {criterion!r}"
            )
        if not url.startswith("http"):
            problems.append(f"{label}: source_url {url!r} does not start with http")
        if confidence not in CONFIDENCES:
            problems.append(f"{label}: confidence must be one of {CONFIDENCES}, not {confidence!r}")
        if as_of and not _AS_OF_RE.fullmatch(as_of):
            problems.append(f"{label}: as_of {as_of!r} is not YYYY-MM or YYYY-MM-DD")
    _raise(path, problems)
    return frame.reset_index(drop=True)


def load_primer(stage: str, directory: Path = STACK_PRIMERS_DIR) -> str | None:
    """The markdown primer for ``stage`` (``<directory>/<stage>.md``), or ``None`` when absent.

    The key is checked against the same pattern as ``stages.csv`` so a stray ``../`` can never
    read outside the primers folder.
    """
    if not _STAGE_KEY_RE.fullmatch(stage):
        raise ValueError(f"invalid stage key {stage!r}")
    path = Path(directory) / f"{stage}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8-sig")
