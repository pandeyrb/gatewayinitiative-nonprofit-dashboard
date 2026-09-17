# Service Categorization — How the Draft Is Wired In

The 2026-09-08 categorization draft now **drives the dashboard's browse
filter**. This memo says exactly how, and lists every place the running app
departs from the document.

## Where things live

| File | Role |
|---|---|
| `qa/boss_categorization_2026-09-08.md` | The draft, **kept exactly as received**. Never edited by code. |
| `qa/categorization_corrections.csv` | Overlay. Four rows, each a deliberate departure with its reason. Delete a row to restore the draft's assignment. |
| `data/service_categories.csv` | Generated. What the app reads. |
| `scripts/parse_boss_categorization.py` | Regenerates the above and reports coverage. |

To change a categorization: edit the markdown (or the corrections overlay), run
`.venv/bin/python scripts/parse_boss_categorization.py --write`, and the app
picks it up. The draft is the source of truth, not the code.

## How it drives browsing

The sidebar now has **two levels**:

1. **Category** — the draft's 25 categories (plus one addition, below).
   Selecting "Food Assistance" matches any organization with a service the
   draft assigned to Food Assistance. Multi-category assignments work as
   intended: community gardens appear under both Food Assistance and
   Environmental Programs.
2. **Specific service** — 76 canonical tags for when someone knows exactly what
   they need. This layer exists because the draft's leaves are raw strings, so
   "Food Assistance (33)" would otherwise offer `Food Pantry`, `Food Pantry
   Services`, `Emergency Food Pantry`, `Mobile Pantry` and `Soup Kitchen` as
   five separate choices. The canonical layer merges duplicate *wording*
   without merging *meaning*.

Both filters combine. Category → Food Assistance gives 20 orgs; adding
Service → Food Pantry narrows to 5.

## Coverage against live data

The draft was written against a 410-tag snapshot. The CSV now holds **485**.

| | Count |
|---|---|
| Raw tags assigned a category by the draft | **383** |
| Draft's "Needs Review" (no category given) | 22 |
| **Not mentioned in the draft at all** | **80** |
| In the draft but no longer in the data | 5 |

The 80 uncovered tags fall back to a category inferred from the pattern rules,
so nothing is invisible. But they are unreviewed. Run the parser script to list
all 80; they include real services like `Immigration Legal Services`,
`Transitional Housing`, `Public Benefits Assistance`, `Outpatient Addiction
Program` and `Women's Health Services`.

The 5 stale entries are case variants — `ESOL classes` vs `ESOL Classes`,
`Summer Program`/`Summer programs` vs `Summer Programs`, `Transportation
services`, `Youth Development programs`. Worth normalising in a future pass.

Note: the draft's 22 "Needs Review" tags are also given a provisional
rule-inferred category rather than being dropped, on the reading that they need
review rather than permanent exclusion. Say the word and they can be excluded
instead.

## The one addition: Domestic & Sexual Violence Services

**The draft has no domestic-violence category, and none of these tags appear in
it anywhere:**

- `Domestic Violence Survivor Services` — Jeanne Geiger Crisis Center
- `Domestic Violence and Sexual Assault Support Services` — YWCA
- `Domestic Violence and Crime Victim Legal Advocacy` — Northeast Justice Center
- `Intimate Partner Abuse Education`, `Youth Empowerment and Bystander
  Education` — Jeanne Geiger Crisis Center
- `24-Hour Crisis Hotline` — Jeanne Geiger Crisis Center and YWCA

Family Support & Child Welfare would be wrong (a survivor need not have
children) and Legal & Immigration covers only the advocacy piece. **A 26th
category was added**, currently matching 3 organizations. This is the change
most worth a second opinion.

## The four corrections

All in `qa/categorization_corrections.csv`, each reversible by deleting one row.

| Tag | Draft says | Now | Why |
|---|---|---|---|
| `Youth Basketball & Life Skills Program for At-Risk Youth (SISU Basketball)` | Legal & Immigration; Youth Development; Sports | Youth Development; Sports | A basketball program is not a legal service. "At-Risk Youth" in the title most likely triggered it. |
| `Valet Parking and Shuttle Service` | Employment & Workforce Training; Community Events | Civic & Government Services; Community Events | It is a patient service at Lawrence General Hospital, not job training. |
| `Foreclosure Prevention` | Homelessness & Emergency Shelter; Homebuyers | Homebuyers only | Someone who needs a bed tonight should not get homeowner counseling. Keeping the urgent category clean matters more than completeness. |
| `Foreclosure Prevention and Homeowner Counseling` | same as above | Homebuyers only | Same reasoning. |

**Correction to an earlier version of this memo:** it claimed `Book clubs` and
`HOLA Podcast` were filed only under Civic & Government Services and needed
moving to Arts. That was a misreading — the draft already lists both under Arts
*and* Civic. No change was made to either.

`Rental Assistance and Counseling` and `Rental Counseling` are also dual-listed
under Homelessness and Homebuyers. That one was left as the draft has it —
rental assistance genuinely does prevent homelessness, unlike foreclosure
counseling.

## Observation, no change made

`Youth Development & Mentoring` matches **29 of 65 organizations** — the
broadest category by some margin, since it absorbs leadership programs,
community-service days, school clubs, summer camps and youth employment. It
still works as a browse heading, but it is the first candidate if the category
list is ever refined.

## Open data questions

Unrelated to categorization; these are in `qa/locations_to_verify.csv`:

- **Bread and Roses Housing has merged** into Centro de Apoyo Familiar. Keep,
  relabel, or remove?
- **Lawrence Prospera — Quintana Center**: the CSV says 404 Haverhill St; their
  site lists 404 as the former JCC Gym and puts Quintana at 580 Haverhill St.
- **Neighbors in Need** publishes (978) 699-3683; the CSV has (978) 685-8321.
