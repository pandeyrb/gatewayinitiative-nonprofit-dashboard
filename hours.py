"""Parse the hand-authored Hours strings into something the app can reason about.

Hours are written by whoever researched the org, copied from the org's own
site, and they are the only field where the wording carries real structure:

    Mon–Fri 8:30 AM–4:30 PM (admin office)
    Soup kitchen: daily 8:30–9:30 AM & 11:30 AM–1 PM; Food pantry: Wed 9 AM–2 PM
    Food pantry: Tue 9–10:30 AM & 5:30–6:30 PM; Wed 12:30–2 PM; Mon closed

Printing that verbatim is what the dashboard used to do, and it means someone
looking for a meal has to read a paragraph to work out whether they can walk in
right now. This module turns those strings into day/time intervals so the app
can answer "is it open?" and draw a week grid — while keeping the original text
for anything it cannot parse, so a sentence we did not anticipate still reaches
the reader instead of vanishing.

The grammar, informally:

    [Label: ] <days> <time>–<time> [& <time>–<time>] [(note)] ; <next segment>

A label carries forward to following segments until a new one appears, because
that is how the multi-service orgs are written ("Food pantry: Tue …; Wed …"
means the Wednesday hours are also the food pantry's).
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field

DAY_ABBR_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_DAY_TOKENS = {
    "mon": 0, "monday": 0, "mo": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "tu": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "we": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "th": 3,
    "fri": 4, "friday": 4, "fr": 4,
    "sat": 5, "saturday": 5, "sa": 5,
    "sun": 6, "sunday": 6, "su": 6,
}

# Anything that names a set of days without listing them.
_DAY_GROUPS = {
    "daily": list(range(7)),
    "every day": list(range(7)),
    "everyday": list(range(7)),
    "all week": list(range(7)),
    "7 days": list(range(7)),
    "weekdays": [0, 1, 2, 3, 4],
    "weekday": [0, 1, 2, 3, 4],
    "weekends": [5, 6],
    "weekend": [5, 6],
}

# en-dash, em-dash, hyphen and "to" all show up as range separators in the data.
_DASH = r"[–—\-]|\bto\b"

_TIME = r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?"
_TIME_RANGE_RE = re.compile(
    rf"(?P<lo>{_TIME})\s*(?:{_DASH})\s*(?P<hi>{_TIME})", re.I
)
_TIME_PARTS_RE = re.compile(
    r"^\s*(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<mer>a\.?m\.?|p\.?m\.?)?\s*$", re.I
)

# "1st & 3rd Thu" — real (GLCAC's Andover WIC office) but not representable on
# a weekly grid, so it is kept as text rather than silently flattened to
# "every Thursday", which would send someone on a wasted trip three weeks in four.
_ORDINAL_RE = re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.I)

_ALWAYS_RE = re.compile(r"\b24\s*(?:hours?|hrs?|/\s*7)\b", re.I)
_CLOSED_RE = re.compile(r"\bclosed\b", re.I)
_NOTE_RE = re.compile(r"\(([^)]*)\)")


@dataclass(frozen=True)
class Interval:
    """One opening on one day, in minutes from local midnight.

    `end` may exceed 1440 for an opening that runs past midnight.
    """

    day: int
    start: int
    end: int


@dataclass
class Schedule:
    """Everything the string said about one named service (or the org itself)."""

    label: str = ""
    intervals: list[Interval] = field(default_factory=list)
    closed_days: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    always_open: bool = False
    raw: str = ""

    @property
    def has_structure(self) -> bool:
        return bool(self.intervals) or self.always_open


# ── low-level parsing ─────────────────────────────────────────────────────────
def _parse_time(text: str, fallback_meridiem: str | None = None) -> int | None:
    m = _TIME_PARTS_RE.match(text)
    if not m:
        return None
    hour = int(m.group("h"))
    minute = int(m.group("m") or 0)
    mer = (m.group("mer") or fallback_meridiem or "").replace(".", "").lower()
    if hour > 23 or minute > 59:
        return None
    if mer.startswith("p") and hour != 12:
        hour += 12
    elif mer.startswith("a") and hour == 12:
        hour = 0
    elif not mer and hour <= 12:
        # No meridiem anywhere to inherit; the caller resolves it.
        return hour * 60 + minute
    return hour * 60 + minute


def _meridiem_of(text: str) -> str | None:
    m = re.search(r"(a\.?m\.?|p\.?m\.?)", text, re.I)
    return m.group(1).replace(".", "").lower() if m else None


def _parse_range(lo_text: str, hi_text: str) -> tuple[int, int] | None:
    """Resolve a time range, inferring the meridiem where it was left implicit.

    "9–10:30 AM" states PM/AM once and means it for both ends; "11:30 AM–1 PM"
    states both. Where only one end says, the other inherits — and if that
    produces a backwards range we flip, which is what "12–8 PM" needs.
    """
    lo_mer, hi_mer = _meridiem_of(lo_text), _meridiem_of(hi_text)
    lo = _parse_time(lo_text, lo_mer or hi_mer)
    hi = _parse_time(hi_text, hi_mer or lo_mer)
    if lo is None or hi is None:
        return None

    if hi <= lo:
        # Try the other reading before giving up on the pair.
        if not hi_mer and hi + 720 > lo:
            hi += 720
        elif not lo_mer and lo >= 720:
            lo -= 720
        else:
            hi += 1440  # genuinely runs past midnight
    if hi <= lo or hi - lo > 1440:
        return None
    return lo, hi


def _days_in(text: str) -> list[int]:
    """Pull a day set out of a fragment like "Mon–Fri", "Mon, Wed & Fri", "daily"."""
    low = text.lower()
    for phrase, days in _DAY_GROUPS.items():
        if phrase in low:
            return list(days)

    # Ranges first, so "Mon–Fri" does not read as the two days Mon and Fri.
    days: list[int] = []
    consumed = low
    for m in re.finditer(
        rf"\b([a-z]{{2,9}})\s*(?:{_DASH})\s*([a-z]{{2,9}})\b", low
    ):
        a, b = _DAY_TOKENS.get(m.group(1)), _DAY_TOKENS.get(m.group(2))
        if a is None or b is None:
            continue
        span = [a] if a == b else [(a + i) % 7 for i in range((b - a) % 7 + 1)]
        days.extend(span)
        consumed = consumed.replace(m.group(0), " ")

    for m in re.finditer(r"\b([a-z]{2,9})\b", consumed):
        d = _DAY_TOKENS.get(m.group(1))
        if d is not None:
            days.append(d)

    return sorted(set(days))


def _is_only_days(text: str) -> bool:
    """True when a fragment names days and nothing else."""
    low = text.lower()
    for phrase in _DAY_GROUPS:
        low = low.replace(phrase, " ")
    low = re.sub(r"\b[a-z]{2,9}\b", lambda m: " " if m.group(0) in _DAY_TOKENS else m.group(0), low)
    return not re.search(r"[a-z0-9]", low)


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split on `sep`, ignoring occurrences inside parentheses.

    Notes carry their own punctuation — "(in person; appointments recommended)"
    and "(families of 1–2)" — and splitting inside them tore them in half.
    """
    parts, buf, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _split_label(segment: str) -> tuple[str | None, str]:
    """Separate a leading "Soup kitchen:" from the hours that follow it.

    Only a colon that is not part of a clock time counts, so "8:30" is safe.
    """
    for m in re.finditer(r":", segment):
        i = m.start()
        if i > 0 and segment[i - 1].isdigit():
            continue
        return segment[:i].strip(), segment[i + 1 :].strip()
    return None, segment.strip()


def _parse_segment(body: str, schedule: Schedule) -> None:
    """Fold one semicolon-delimited segment into `schedule`."""
    notes = [n.strip() for n in _NOTE_RE.findall(body) if n.strip()]
    # Notes are stripped before time parsing — several of them contain their own
    # time ranges ("intake staff at lunch 1–2 PM") that are not opening hours.
    text = _NOTE_RE.sub(" ", body).strip(" ,;")
    for n in notes:
        if n not in schedule.notes:
            schedule.notes.append(n)

    if not text:
        return

    if _ORDINAL_RE.search(text):
        schedule.unparsed.append(body.strip())
        return

    if _ALWAYS_RE.search(text):
        schedule.always_open = True
        return

    matches = list(_TIME_RANGE_RE.finditer(text))

    if not matches:
        if _CLOSED_RE.search(text):
            days = _days_in(_CLOSED_RE.sub(" ", text))
            schedule.closed_days.update(days or range(7))
        elif schedule.always_open and _is_only_days(text):
            # The "7 days" half of "24 hours; 7 days" — already covered by
            # always_open, so it is redundant rather than unparseable.
            pass
        else:
            schedule.unparsed.append(body.strip())
        return

    current: list[int] = []
    cursor = 0
    for m in matches:
        gap = text[cursor : m.start()]
        found = _days_in(gap)
        if found:
            current = found
        parsed = _parse_range(m.group("lo"), m.group("hi"))
        if parsed and current:
            lo, hi = parsed
            for d in current:
                schedule.intervals.append(Interval(d, lo, hi))
        elif parsed:
            # A time with no day anywhere ("Dinner: 4–6 PM"). Real, but we will
            # not invent a day for it, so it stays as text.
            schedule.unparsed.append(body.strip())
            return
        cursor = m.end()

    tail = text[cursor:]
    if _CLOSED_RE.search(tail):
        schedule.closed_days.update(_days_in(_CLOSED_RE.sub(" ", tail)) or [])


def parse_hours(text: str) -> list[Schedule]:
    """Turn one Hours cell into one Schedule per named service.

    An org with no labels at all gets a single Schedule with `label == ""`.
    """
    text = (text or "").strip()
    if not text:
        return []

    schedules: list[Schedule] = []
    by_label: dict[str, Schedule] = {}
    current_label = ""

    for raw_segment in _split_top_level(text, ";"):
        segment = raw_segment.strip()
        if not segment:
            continue

        label, body = _split_label(segment)
        if label is not None and not _days_in(label) and not label[:1].isdigit():
            current_label = label
        elif label is not None:
            # The colon belonged to the hours, not to a service name.
            body = segment

        sched = by_label.get(current_label)
        if sched is None:
            sched = Schedule(label=current_label)
            by_label[current_label] = sched
            schedules.append(sched)
        sched.raw = f"{sched.raw}; {segment}".lstrip("; ")

        # A segment can hold several day-clauses separated by commas
        # ("Wed 9 AM–1 PM, Thu 9–11 AM"). Commas inside a day list
        # ("Mon, Tue, Thu 10 AM–3 PM") must not split, so only break where the
        # piece that follows carries its own time range.
        pieces = _split_clauses(body)
        for piece in pieces:
            _parse_segment(piece, sched)

    for s in schedules:
        s.intervals = sorted(set(s.intervals), key=lambda i: (i.day, i.start))
        s.closed_days -= {i.day for i in s.intervals}

    return schedules


def _split_clauses(body: str) -> list[str]:
    parts = [p.strip() for p in _split_top_level(body, ",")]
    if len(parts) == 1:
        return parts
    clauses: list[str] = []
    pending: list[str] = []
    for p in parts:
        pending.append(p)
        if _TIME_RANGE_RE.search(p) or _CLOSED_RE.search(p):
            clauses.append(", ".join(pending))
            pending = []
    if pending:
        if clauses:
            clauses[-1] = clauses[-1] + ", " + ", ".join(pending)
        else:
            clauses.append(", ".join(pending))
    return clauses


# ── formatting & status ───────────────────────────────────────────────────────
def format_minutes(m: int) -> str:
    m %= 1440
    h, mi = divmod(m, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mi:02d} {suffix}" if mi else f"{h12} {suffix}"


def format_range(start: int, end: int) -> str:
    return f"{format_minutes(start)}–{format_minutes(end)}"


def weekly_grid(
    schedule: Schedule, day_names: list[str] | None = None
) -> list[tuple[str, str]]:
    """One row per run of consecutive days that share the same opening times.

    Collapsing "Mon 9–5, Tue 9–5, Wed 9–5, Thu 9–5" back into "Mon–Thu 9–5" is
    what keeps the popup to a few lines instead of seven.
    """
    names = day_names or DAY_ABBR_EN
    if schedule.always_open:
        return [(f"{names[0]}–{names[6]}", "24 hours")]

    per_day: dict[int, str] = {}
    for d in range(7):
        ranges = [i for i in schedule.intervals if i.day == d]
        if ranges:
            per_day[d] = ", ".join(format_range(i.start, i.end) for i in ranges)
        elif d in schedule.closed_days:
            per_day[d] = ""

    rows: list[tuple[str, str]] = []
    run_start: int | None = None
    for d in range(8):
        value = per_day.get(d) if d < 7 else None
        prev = per_day.get(run_start) if run_start is not None else None
        if run_start is not None and (d == 7 or value != prev):
            end = d - 1
            label = names[run_start] if run_start == end else f"{names[run_start]}–{names[end]}"
            rows.append((label, prev or "closed"))
            run_start = None
        if d < 7 and value is not None and run_start is None:
            run_start = d
    return rows


def _status_for(schedule: Schedule, weekday: int, minute: int):
    if schedule.always_open:
        return True, None
    for offset in (0, -1):  # -1 catches an opening that began yesterday
        day = (weekday + offset) % 7
        probe = minute - offset * 1440
        for i in schedule.intervals:
            if i.day == day and i.start <= probe < i.end:
                return True, i.end
    return False, None


def _next_opening(schedule: Schedule, weekday: int, minute: int):
    if not schedule.intervals:
        return None
    for ahead in range(8):
        day = (weekday + ahead) % 7
        for i in sorted(
            (x for x in schedule.intervals if x.day == day), key=lambda x: x.start
        ):
            if ahead > 0 or i.start > minute:
                return ahead, i.start
    return None


def open_status(
    schedules: list[Schedule], now, day_names: list[str] | None = None
) -> tuple[str, str, str]:
    """Is anything open right now?

    Returns (state, detail, label) where state is "open", "closed" or "unknown".
    An org with several services counts as open if any one of them is, and
    `label` names which — "OPEN NOW · Food Pantry until 2 PM" is the useful
    answer, not a bare green dot.

    `now` must be a timezone-aware local datetime; the caller owns the timezone
    so this module stays testable with a frozen clock. `day_names` localises the
    day shown in "opens Wed 9 AM"; it defaults to English abbreviations.
    """
    structured = [s for s in schedules if s.has_structure]
    if not structured:
        return "unknown", "", ""

    weekday, minute = now.weekday(), now.hour * 60 + now.minute

    for s in structured:
        is_open, ends = _status_for(s, weekday, minute)
        if is_open:
            detail = "" if ends is None else format_minutes(ends)
            return "open", detail, s.label

    best = None
    for s in structured:
        nxt = _next_opening(s, weekday, minute)
        if nxt and (best is None or nxt[:2] < best[0][:2]):
            best = (nxt, s.label)
    if best is None:
        return "closed", "", ""

    (ahead, start), label = best
    when = format_minutes(start)
    if ahead == 0:
        return "closed", when, label
    day_name = (day_names or DAY_ABBR_EN)[(weekday + ahead) % 7]
    return "closed", f"{day_name} {when}", label
