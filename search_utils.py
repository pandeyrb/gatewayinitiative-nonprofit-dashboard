"""
Search normalization for the GWI dashboard.

Two problems this solves, both found while testing real queries:
  1. "diapers" missed "Diaper Distribution" — exact substring matching
  2. "ESL" missed orgs tagged "ESOL" — same service, different acronym

No new dependencies: a small suffix stemmer rather than nltk, since the app
only needs English plurals and adding a corpus download to deployment isn't
worth it for that.
"""

import re

# ── aliases ───────────────────────────────────────────────────────────────────
# Hand-curated, deliberately small: just terms the cohort is likely to type.
# Each set is a group of interchangeable terms. Searching any member matches
# tags containing any other member.
ALIAS_GROUPS = [
    {"esl", "esol", "english class", "english classes", "english language"},
    {"daycare", "day care", "childcare", "child care"},
    {"food pantry", "food bank", "pantry"},
    {"shelter", "homeless shelter", "emergency shelter"},
    {"ged", "high school equivalency", "hiset"},
    {"legal", "legal aid", "legal services", "lawyer", "attorney"},
    {"job", "jobs", "employment", "workforce", "career"},
    {"senior", "seniors", "elder", "elderly", "aging"},
    {"mental health", "behavioral health", "counseling", "therapy"},
    {"immigration", "immigrant", "citizenship", "naturalization"},
    {"substance use", "addiction", "recovery", "substance abuse"},
    {"disability", "disabilities", "special needs"},
    {"tutoring", "tutor", "homework help", "academic support"},
    {"clothing", "clothes", "thrift"},
    {"diaper", "diapers", "baby supplies"},
]

# ── stemmer ───────────────────────────────────────────────────────────────────
# Words that look plural but aren't, or that break under naive rules.
_STEM_EXCEPTIONS = {
    "bus", "class", "glass", "access", "business", "wellness", "illness",
    "homeless", "campus", "status", "gas", "plus", "less", "press", "address",
    "process", "success", "fitness", "awareness", "readiness", "aids",
    "news", "series", "species", "analysis", "crisis", "basis", "hiv", "us",
}


def stem(word: str) -> str:
    """
    Conservative English stemmer — plurals and a few verb endings only.

    Deliberately does NOT strip a trailing "s" blindly: that turns "class"
    into "clas" and "bus" into "bu", breaking more searches than it fixes.
    """
    w = word.lower()

    if len(w) <= 3 or w in _STEM_EXCEPTIONS:
        return w

    # -ies -> -y   (families -> family, pantries -> pantry)
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"

    # -sses / -shes / -ches / -xes / -zes -> drop "es"
    if re.search(r"(ss|sh|ch|x|z|s)es$", w):
        return w[:-2]

    # -ss stays (class, wellness)
    if w.endswith("ss"):
        return w

    # -us stays (campus, status)
    if w.endswith("us"):
        return w

    # plain plural -s
    if w.endswith("s") and not w.endswith("s s"):
        w = w[:-1]

    # -ing / -ed, so "housing" and "house" meet in the middle
    if w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]

    # collapse a doubled final consonant left behind (running -> runn -> run)
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]

    # trailing "e" so house/housing both land on "hous"
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]

    return w


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> set[str]:
    """Stemmed token set for a piece of text."""
    return {stem(t) for t in _TOKEN_RE.findall(str(text).lower())}


def expand_query(query: str) -> list[str]:
    """Query plus any alias-group members it belongs to."""
    q = query.strip().lower()
    out = [q]
    for group in ALIAS_GROUPS:
        if q in group:
            out.extend(g for g in group if g != q)
    return out


def matches(query: str, haystack: str) -> bool:
    """
    True if `query` should match `haystack`.

    Three ways to match, most permissive first — this is strictly a superset
    of the old substring behaviour, so nothing that matched before stops
    matching now:
      1. raw substring (so partial words like "diap" still work)
      2. an alias of the query appears as a substring
      3. every stemmed query token appears in the stemmed haystack
    """
    if not query.strip():
        return True

    hay_low = str(haystack).lower()

    for variant in expand_query(query):
        if variant in hay_low:
            return True

    hay_tokens = tokens(haystack)
    for variant in expand_query(query):
        q_tokens = tokens(variant)
        if q_tokens and q_tokens <= hay_tokens:
            return True

    return False
