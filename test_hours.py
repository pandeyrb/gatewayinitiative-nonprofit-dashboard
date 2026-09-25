"""Checks for hours.py, run with `python3 test_hours.py`.

The open/closed badge is the one piece of this dashboard that makes a claim
about right now, so it is tested against a frozen clock rather than by eye.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from hours import format_minutes, open_status, parse_hours, weekly_grid

TZ = ZoneInfo("America/New_York")
FAILURES: list[str] = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


# 2026-09-21 is a Monday, so 21=Mon 22=Tue 23=Wed 24=Thu 25=Fri 26=Sat 27=Sun.

# ── time formatting ───────────────────────────────────────────────────────────
check("noon", format_minutes(720), "12 PM")
check("midnight", format_minutes(0), "12 AM")
check("8:30am", format_minutes(510), "8:30 AM")
check("4:30pm", format_minutes(16 * 60 + 30), "4:30 PM")

# ── meridiem inference ────────────────────────────────────────────────────────
lib = parse_hours("Mon 9 AM–5 PM; Tue–Wed 9 AM–9 PM; Thu–Sat 9 AM–5 PM; Sun closed")
check("library tue 8pm", open_status(lib, at(2026, 9, 22, 20))[0], "open")
check("library sun noon", open_status(lib, at(2026, 9, 27, 12))[0], "closed")
check("library mon 8pm", open_status(lib, at(2026, 9, 21, 20))[0], "closed")
check("library mon 4pm", open_status(lib, at(2026, 9, 21, 16))[0], "open")

# "12–8 PM" must read as noon-to-8pm, not 12am-to-8pm.
noon8 = parse_hours("Tue 12–8 PM")
check("12-8pm grid", weekly_grid(noon8[0]), [("Tue", "12 PM–8 PM")])
check("12-8pm at 1pm", open_status(noon8, at(2026, 9, 22, 13))[0], "open")
check("12-8pm at 9am", open_status(noon8, at(2026, 9, 22, 9))[0], "closed")

# "9–10:30 AM" — the AM on the right end governs both.
am = parse_hours("Thu 9–10:30 AM")
check("9-10:30am grid", weekly_grid(am[0]), [("Thu", "9 AM–10:30 AM")])
check("9-10:30am at 10", open_status(am, at(2026, 9, 24, 10))[0], "open")
check("9-10:30am at 11", open_status(am, at(2026, 9, 24, 11))[0], "closed")

# ── multi-service, label carry-forward ────────────────────────────────────────
laz = parse_hours(
    "Soup kitchen: daily 8:30–9:30 AM & 11:30 AM–1 PM; Food pantry: Wed 9 AM–2 PM"
)
check("lazarus n schedules", len(laz), 2)
check("lazarus labels", [s.label for s in laz], ["Soup kitchen", "Food pantry"])
check("lazarus soup grid", weekly_grid(laz[0]), [("Mon–Sun", "8:30 AM–9:30 AM, 11:30 AM–1 PM")])
# Open for lunch on a Sunday -> soup kitchen, not the pantry.
check("lazarus sun 12pm", open_status(laz, at(2026, 9, 27, 12))[:1], ("open",))
check("lazarus sun 12pm who", open_status(laz, at(2026, 9, 27, 12))[2], "Soup kitchen")
check("lazarus sun 3pm", open_status(laz, at(2026, 9, 27, 15))[0], "closed")
# Wed 1:30pm: soup kitchen shut at 1, pantry open until 2.
check("lazarus wed 1:30", open_status(laz, at(2026, 9, 23, 13, 30))[2], "Food pantry")

nin = parse_hours(
    "Food pantry: Tue 9–10:30 AM & 5:30–6:30 PM; Wed 12:30–2 PM; "
    "Thu 9–10:30 AM & 4–5 PM; Fri 9–10:30 AM; Mon closed"
)
check("nin one schedule", len(nin), 1)
check("nin label carries", nin[0].label, "Food pantry")
check("nin mon closed", 0 in nin[0].closed_days, True)
check("nin tue 6pm", open_status(nin, at(2026, 9, 22, 18))[0], "open")
check("nin tue 3pm", open_status(nin, at(2026, 9, 22, 15))[0], "closed")
check("nin tue 3pm next", open_status(nin, at(2026, 9, 22, 15))[1], "5:30 PM")
check("nin mon 10am next", open_status(nin, at(2026, 9, 21, 10))[1], "Tue 9 AM")

# ── notes must not be read as opening times ───────────────────────────────────
njc = parse_hours("In-person intake: Mon–Thu 9 AM–4 PM (intake staff at lunch 1–2 PM)")
check("njc grid", weekly_grid(njc[0]), [("Mon–Thu", "9 AM–4 PM")])
check("njc note kept", njc[0].notes, ["intake staff at lunch 1–2 PM"])
check("njc mon 3pm", open_status(njc, at(2026, 9, 21, 15))[0], "open")

sal = parse_hours(
    "Office & social services: Mon–Thu 9 AM–2 PM; Food pantry: "
    "Wed 9 AM–1 PM (families of 3+), Thu 9–11 AM (families of 1–2)"
)
check("salvation n", len(sal), 2)
check("salvation labels", [s.label for s in sal], ["Office & social services", "Food pantry"])
check("salvation pantry grid", weekly_grid(sal[1]), [("Wed", "9 AM–1 PM"), ("Thu", "9 AM–11 AM")])
check("salvation notes", sal[1].notes, ["families of 3+", "families of 1–2"])

# A semicolon inside a parenthetical must not split the segment.
hab = parse_hours("Mon–Fri 8:30 AM–1:30 PM (in person; appointments recommended)")
check("habitat grid", weekly_grid(hab[0]), [("Mon–Fri", "8:30 AM–1:30 PM")])
check("habitat note", hab[0].notes, ["in person; appointments recommended"])
check("habitat no leftovers", hab[0].unparsed, [])

# ── always open ───────────────────────────────────────────────────────────────
hot = parse_hours("Crisis hotline: 24 hours, 7 days")
check("hotline always", hot[0].always_open, True)
check("hotline 3am sun", open_status(hot, at(2026, 9, 27, 3))[0], "open")
shelter = parse_hours("24 hours; 7 days")
check("shelter no leftovers", shelter[0].unparsed, [])
check("shelter always", shelter[0].always_open, True)

# ── day-list collapsing ───────────────────────────────────────────────────────
thrift = parse_hours("Mon, Tue, Thu, Fri, Sat 10 AM–3 PM; Wed 9 AM–5 PM")
check(
    "thrift grid",
    weekly_grid(thrift[0]),
    [("Mon–Tue", "10 AM–3 PM"), ("Wed", "9 AM–5 PM"), ("Thu–Sat", "10 AM–3 PM")],
)
amp = parse_hours("Mon & Wed 9 AM–5 PM; Tue & Thu 12–8 PM; Sat 9 AM–1 PM")
check(
    "ampersand days",
    weekly_grid(amp[0]),
    [("Mon", "9 AM–5 PM"), ("Tue", "12 PM–8 PM"), ("Wed", "9 AM–5 PM"),
     ("Thu", "12 PM–8 PM"), ("Sat", "9 AM–1 PM")],
)

# ── things we deliberately refuse to guess ────────────────────────────────────
ordinal = parse_hours("1st & 3rd Thu 10 AM–1 PM and 2–6 PM")
check("ordinal not flattened", ordinal[0].intervals, [])
check("ordinal kept as text", ordinal[0].unparsed, ["1st & 3rd Thu 10 AM–1 PM and 2–6 PM"])
check("ordinal status", open_status(ordinal, at(2026, 9, 24, 11))[0], "unknown")

check("empty", parse_hours(""), [])
check("empty status", open_status([], at(2026, 9, 24, 11)), ("unknown", "", ""))

# ── timezone: the badge must follow Lawrence, not the server ──────────────────
utc_evening = datetime(2026, 9, 22, 23, 0, tzinfo=ZoneInfo("UTC"))  # 7 PM in Lawrence
check("tz aware", open_status(lib, utc_evening.astimezone(TZ))[0], "open")

if FAILURES:
    print(f"{len(FAILURES)} FAILED\n")
    for f in FAILURES:
        print(" -", f)
    raise SystemExit(1)
print("all checks passed")
