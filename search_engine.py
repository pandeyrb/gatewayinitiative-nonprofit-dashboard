"""
Organization search for the GWI dashboard.

Two layers:
  keyword_search  instant word matching; best for org names and exact services
  ai_match        Gemini reads the whole directory and picks the orgs that fit;
                  best for plain-language needs ("I can't pay my rent"),
                  Spanish and typos

smart_search() decides which to use. app.py and scripts/score_search.py both
call into this file, so the score always measures what the site runs.
"""

import difflib
import hashlib
import json
import math
import os
import re
import time
import unicodedata

import pandas as pd

from search_utils import matches, stem
from service_taxonomy import TAGS


def fold(text: str) -> str:
    """Strip accents: 'Inglés' -> 'Ingles', so either spelling finds the other."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", str(text))
        if not unicodedata.combining(ch)
    )


# ── keyword layer ─────────────────────────────────────────────────────────────
# A small ranked search engine, so search works even with the AI off or down:
#   - words people use for a need ("hungry", "desalojo") map to the services
#     that meet it (NEEDS, below)
#   - filler words are dropped, so a whole sentence still matches on the words
#     that carry it
#   - a word not in the directory is corrected to the closest one that is
#     ("imigration" -> "immigration")
#   - every org gets a score (name > services > description) and results come
#     back best first
# The old all-words match (search_utils.matches) still counts, as a strong bonus,
# so exact names and service phrases keep ranking first.

STOPWORDS = {
    # English
    "a", "an", "the", "i", "im", "ive", "id", "me", "my", "mine", "we", "our", "you",
    "your", "he", "she", "him", "her", "his", "they", "them", "their", "it", "its",
    "is", "am", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "dont", "cant", "cannot", "wont", "not", "no", "to", "of", "in", "on", "at",
    "for", "with", "and", "or", "but", "so", "if", "from", "by", "about", "into",
    "this", "that", "these", "those", "there", "here", "where", "what", "who",
    "how", "when", "which", "any", "some", "can", "could", "would", "should",
    "will", "need", "needs", "needed", "want", "wants", "looking", "look", "find",
    "get", "getting", "got", "go", "going", "have", "has", "had", "help", "helps",
    "please", "someone", "feel", "feeling", "having", "thought", "thoughts",
    "apply", "applying", "application", "applications", "sign", "signup", "somewhere", "something", "place", "near", "nearby",
    "lawrence", "free", "some", "just", "really", "very", "all", "more", "like",
    "kind", "anyone", "anything", "today", "tonight", "now", "month", "week",
    "program", "programs", "service", "services", "organization", "organizations",
    # Spanish
    "yo", "mi", "mis", "me", "el", "la", "los", "las", "un", "una", "unos",
    "unas", "de", "del", "al", "a", "en", "con", "para", "por", "que", "y", "o",
    "es", "soy", "estoy", "esta", "tengo", "tiene", "necesito", "necesita",
    "quiero", "busco", "buscar", "ayuda", "ayudar", "donde", "como", "hay", "su",
    "sus", "lo", "le", "se", "no", "gratis", "algo", "alguien", "aqui", "favor",
}

# Everyday words for a need, in English and Spanish, and the services (as
# listed in data/specific_services.csv) that meet it, the most direct first. Words are matched after
# stemming and accent folding, so "evicted"/"eviction" and "médico"/"medico"
# all land. Kept general on purpose: a family of words per kind of help, not a
# phrase per test query.
NEEDS = [
    ({"hungry", "hunger", "food", "groceries", "grocery", "eat", "meal", "meals",
      "pantry", "dinner", "lunch", "breakfast", "comida", "hambre", "comer",
      "alimentos", "despensa", "mercado"},
     ["Food Pantry", "Free Meals", "Mobile Food Distribution", "SNAP & WIC",
      "Holiday Meals & Food Baskets"]),
    ({"snap", "wic", "ebt", "stamps", "cupones", "estampillas"},
     ["SNAP & WIC", "Public Benefits Help"]),
    ({"rent", "evict", "eviction", "evicted", "landlord", "lease", "tenant",
      "renta", "alquiler", "desalojo", "desalojar", "casero", "dueno", "inquilino"},
     ["Rent Help & Tenant Counseling", "Rent Help to Prevent Eviction", "Tenant Rights"]),
    ({"apartment", "apartments", "housing", "vivienda", "apartamento", "casa"},
     ["Affordable Housing", "Rent Help & Tenant Counseling", "Transitional Housing"]),
    ({"homeless", "homelessness", "shelter", "sleep", "street", "streets",
      "desamparado", "refugio", "calle", "dormir", "albergue"},
     ["Emergency Shelter", "Transitional Housing", "Outreach, Showers & Laundry"]),
    ({"shower", "showers", "laundry", "wash", "bano", "ducha", "lavanderia"},
     ["Showers & Laundry", "Outreach, Showers & Laundry"]),
    ({"homebuyer", "mortgage", "foreclosure", "hipoteca", "comprar"},
     ["First-Time Homebuyer Help", "Foreclosure Prevention"]),
    ({"job", "jobs", "work", "unemployed", "unemployment", "fired", "laid",
      "hiring", "career", "resume", "trabajo", "empleo", "desempleado", "chamba"},
     ["Job Search Help", "Job Training & Certificates", "Trades & Apprenticeships",
      "Youth Jobs & Job Training"]),
    ({"homework", "tutor", "tutoring", "math", "reading", "grades", "struggling",
      "tarea", "tutoria", "matematicas", "escuela"},
     ["Tutoring & Homework Help", "After-School Programs"]),
    ({"afterschool", "after-school", "teen", "teens", "despues"},
     ["After-School Programs", "After-School & Teen Programs"]),
    ({"pregnant", "pregnancy", "prenatal", "expecting", "embarazada", "embarazo"},
     ["Women's Health & Pregnancy", "Parent & Baby Programs"]),
    ({"baby", "babies", "infant", "newborn", "diaper", "diapers", "formula",
      "bebe", "panales", "panal"},
     ["Diapers & Baby Supplies", "Parent & Baby Programs"]),
    ({"daycare", "childcare", "toddler", "preschool", "guarderia", "cuidado",
      "ninos", "kinder"},
     ["Child Care", "Preschool & Head Start", "Help Finding Child Care"]),
    ({"depressed", "depression", "anxiety", "anxious", "sad", "stress", "stressed",
      "lonely", "grief", "talk", "therapist", "therapy", "counselor", "counseling",
      "mental", "deprimido", "deprimida", "depresion", "ansiedad", "triste",
      "terapia", "consejeria", "hablar"},
     ["Counseling & Therapy", "Crisis Lines & Emotional Support",
      "Psychiatry & Medication", "Family Counseling"]),
    ({"suicide", "suicidal", "crisis", "hotline", "suicidio"},
     ["Crisis Lines & Emotional Support", "24-Hour Hotline"]),
    ({"abuse", "abused", "abusive", "hits", "hit", "beats", "violence", "violent",
      "assault", "rape", "partner", "husband", "boyfriend", "abuso", "violencia",
      "golpea", "maltrato", "domestica", "pareja", "esposo", "novio"},
     ["Survivor Support", "24-Hour Hotline", "Victim & Court Advocacy"]),
    ({"doctor", "sick", "clinic", "checkup", "medical", "physician", "nurse",
      "fever", "medico", "doctora", "enfermo", "enferma", "clinica", "salud"},
     ["Primary & Walk-in Care", "Hospital & Emergency Care", "Children's Health"]),
    ({"pharmacy", "prescription", "medicine", "medication", "farmacia", "medicina"},
     ["Pharmacy & Lab Tests", "Psychiatry & Medication"]),
    ({"insurance", "masshealth", "medicaid", "medicare", "seguro"},
     ["Health Insurance Help", "Public Benefits Help"]),
    ({"elderly", "elder", "senior", "seniors", "aging", "grandmother",
      "grandfather", "mother", "father", "parent", "anciano", "ancianos",
      "mayores", "abuela", "abuelo", "tercera"},
     ["Programs for Older Adults", "Adult Day Programs", "Senior & Home Medical Care",
      "Respite & In-Home Support"]),
    ({"autism", "autistic", "disability", "disabled", "wheelchair", "special",
      "autismo", "discapacidad", "discapacitado"},
     ["Disability & Autism Services", "Special Education", "Accessibility Services",
      "Early Intervention"]),
    ({"business", "entrepreneur", "startup", "loan", "negocio", "empresa",
      "emprender", "prestamo"},
     ["Starting a Business", "Small Business Loans & Grants", "Business Networks"]),
    ({"citizen", "citizenship", "naturalization", "naturalize", "ciudadano",
      "ciudadania", "ciudadana"},
     ["Citizenship Classes & Applications", "Immigration Legal Help"]),
    ({"immigration", "immigrant", "visa", "green", "asylum", "deportation",
      "undocumented", "daca", "inmigracion", "inmigrante", "asilo", "deportacion",
      "papeles", "indocumentado"},
     ["Immigration Legal Help", "Citizenship Classes & Applications"]),
    ({"lawyer", "attorney", "legal", "court", "sued", "abogado", "abogada",
      "corte", "tribunal"},
     ["Civil Legal Aid", "Immigration Legal Help", "Legal & Court Advocacy"]),
    ({"english", "esl", "esol", "ingles"},
     ["English Classes (ESL)", "English Learner Support"]),
    ({"ged", "hiset", "diploma", "equivalency", "graduate"},
     ["GED & High School Equivalency", "Help Returning to School"]),
    ({"college", "university", "fafsa", "scholarship", "universidad", "beca"},
     ["College Advising & Applications", "Scholarships & Financial Aid",
      "Community College Programs"]),
    ({"clothes", "clothing", "coat", "coats", "shoes", "jacket", "ropa", "abrigo",
      "zapatos"},
     ["Free Clothing", "Thrift & Resale Stores", "Interview Clothing & Confidence"]),
    ({"heat", "heating", "electric", "electricity", "utility", "utilities", "gas",
      "bill", "bills", "shutoff", "calefaccion", "luz", "electricidad", "factura",
      "cuentas"},
     ["Heating & Utility Bill Help", "Financial Assistance & Fee Discounts"]),
    ({"addiction", "addicted", "drug", "drugs", "alcohol", "alcoholic", "drinking",
      "opioid", "opioids", "sober", "sobriety", "detox", "rehab", "adiccion",
      "drogas", "alcoholismo"},
     ["Addiction & Recovery", "Residential Treatment"]),
    ({"veteran", "veterans", "military", "army", "navy", "veterano", "militar"},
     ["Military & Veteran Support"]),
    ({"tax", "taxes", "irs", "impuestos", "taxes"},
     ["Tax Preparation"]),
    ({"translator", "translation", "interpreter", "traductor", "interprete",
      "traduccion"},
     ["Interpreters & Translation"]),
    ({"bus", "train", "transportation", "ride", "rides", "fare", "autobus",
      "transporte", "tren"},
     ["Reduced Bus & Train Fares", "Transportation"]),
    ({"money", "debt", "credit", "budget", "dinero", "deuda", "credito"},
     ["Credit & Money Management", "Financial Assistance & Fee Discounts"]),
    ({"camp", "summer", "campamento", "verano"},
     ["Summer Camps", "Sports Camps", "Summer Learning"]),
    ({"sports", "soccer", "basketball", "swim", "swimming", "gym", "exercise",
      "deportes", "futbol", "baloncesto", "natacion"},
     ["Youth Sports", "Gyms & Exercise Classes", "Boating & Swimming"]),
    ({"benefits", "welfare", "cash", "ssi", "disability", "beneficios"},
     ["Public Benefits Help"]),
]

# Where a word appears in an org's listing, and how much that counts.
_W_NAME, _W_SERVICE, _W_TEXT = 3.0, 1.5, 0.5
_IDF_CAP = 3.0      # so one rare word can't outweigh a matched need
_W_NEED = 8.0       # an org offering the need's main service; later ones count half
_W_PHRASE = 6.0     # the whole search appears as written (search_utils.matches)
_W_IN_NAME = 12.0   # the whole query is in the org's name ("ymca")
_KEEP = 0.34        # drop results scoring below this share of the best one

_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(fold(str(text)).lower().replace("'", ""))


def _toks(text: str) -> set[str]:
    return {stem(w) for w in _words(text)}


def _search_text(row, tax) -> str:
    """Everything one org can be found by, in English and Spanish."""
    tags = row["SvcCanonical"]
    return " ".join(
        [str(row.get(c, "")) for c in ("Name", "ServiceArea", "Services", "City",
                                       "Description", "DescriptionEs")]
        + [s for _c, s in row["Specifics"]]
        + [tax.specific_es.get((c, s), s) for c, s in row["Specifics"]]
        + list(tags)
        + [TAGS[t][1] for t in tags if t in TAGS]
        + [tax.category_es.get(c, c) for c in row["Categories"]]
    )


_NEED_STEMS = [({stem(fold(w)) for w in words}, services) for words, services in NEEDS]


class _Index:
    """Per-org token sets, built once per (directory, taxonomy)."""

    def __init__(self, df: pd.DataFrame, tax):
        self.rows = []
        vocab: set[str] = set()
        for _i, r in df.iterrows():
            specifics = [s for _c, s in r["Specifics"]]
            service_text = " ".join(
                specifics
                + [tax.specific_es.get((c, s), s) for c, s in r["Specifics"]]
                + list(r["SvcCanonical"])
                + [TAGS[t][1] for t in r["SvcCanonical"] if t in TAGS]
                + list(r["Categories"])
                + [tax.category_es.get(c, c) for c in r["Categories"]]
            )
            other = " ".join(str(r.get(c, "")) for c in
                            ("ServiceArea", "Services", "Description", "DescriptionEs"))
            row = {
                "name": _toks(r["Name"]),
                "service": _toks(service_text),
                "text": _toks(other),
                "specifics": set(specifics),
                "folded_name": fold(str(r["Name"])).lower(),
                "all": fold(_search_text(r, tax)),
            }
            self.rows.append(row)
            vocab.update(_words(r["Name"] + " " + service_text + " " + other))
        for words, _s in NEEDS:
            vocab.update(fold(w) for w in words)
        self.vocab = sorted(w for w in vocab if len(w) >= 3 and not w.isdigit())
        self.vocab_set = set(self.vocab)
        self.vocab_stems = {stem(w) for w in self.vocab}
        # Rarer words say more: "eviction" beats "family".
        n = max(len(self.rows), 1)
        self.idf: dict[str, float] = {}
        for row in self.rows:
            for t in row["name"] | row["service"] | row["text"]:
                self.idf[t] = self.idf.get(t, 0) + 1
        self.idf = {t: 1.0 + math.log(n / c) for t, c in self.idf.items()}


_INDEX_CACHE: dict[str, _Index] = {}


def _index(df: pd.DataFrame, tax) -> _Index:
    # Keyed on everything the index reads, so an edited description or service
    # list is picked up on the next search, not at the next restart.
    cols = [c for c in ("Name", "ServiceArea", "Services", "Description", "DescriptionEs",
                        "Specifics", "Categories") if c in df.columns]
    key = hashlib.sha256(
        (df[cols].astype(str).to_csv(index=False)
         + str(sorted(tax.specific_es.items()))).encode("utf-8")
    ).hexdigest()
    if key not in _INDEX_CACHE:
        _INDEX_CACHE.clear()
        _INDEX_CACHE[key] = _Index(df, tax)
    return _INDEX_CACHE[key]


def correct_query(query: str, idx: "_Index") -> tuple[str, dict[str, str]]:
    """Fix words that aren't in the directory: 'imigration' -> 'immigration'.
    Returns the corrected query and {typed: corrected}."""
    fixes: dict[str, str] = {}
    out = []
    for w in _words(query):
        known = w in idx.vocab_set or stem(w) in idx.vocab_stems
        if len(w) >= 4 and w not in STOPWORDS and not known:
            # 0.85 keeps "scared" from becoming "care" while still fixing
            # "tutering", "shleter", "imigration"; five-letter swaps ("chlid") need 0.8.
            cutoff = 0.85 if len(w) >= 6 else 0.8 if len(w) == 5 else 0.85
            close = [c for c in difflib.get_close_matches(w, idx.vocab, n=3, cutoff=cutoff)
                     if c not in STOPWORDS]
            if close:
                fixes[w] = close[0]
                w = close[0]
        out.append(w)
    return " ".join(out), fixes


def rank(df: pd.DataFrame, query: str, tax) -> tuple[pd.DataFrame, dict[str, str]]:
    """Orgs matching the search, best first, plus any spelling corrections."""
    if not query.strip() or df.empty:
        return df, {}
    idx = _index(df, tax)
    fixed, fixes = correct_query(query, idx)
    words = [w for w in _words(fixed) if w not in STOPWORDS]
    terms = {stem(w) for w in words}
    if not terms:  # only filler words: fall back to the whole query
        terms = _toks(fixed)
    needs = [services for stems, services in _NEED_STEMS if stems & terms]
    q_whole = fold(query.strip()).lower()

    scores = []
    for row in idx.rows:
        sc = 0.0
        for t in terms:
            w = min(idx.idf.get(t, 0.0), _IDF_CAP)
            if t in row["name"]:
                sc += _W_NAME * w
            elif t in row["service"]:
                sc += _W_SERVICE * w
            elif t in row["text"]:
                sc += _W_TEXT * w
        for services in needs:
            hits = [i for i, svc in enumerate(services) if svc in row["specifics"]]
            if hits:
                sc += _W_NEED * (1.0 if hits[0] == 0 else 0.5) + 0.5 * (len(hits) - 1)
        if len(q_whole) >= 3 and q_whole in row["folded_name"]:
            sc += _W_IN_NAME
        if matches(fold(fixed), row["all"]):
            sc += _W_PHRASE
        scores.append(sc)

    s = pd.Series(scores, index=df.index)
    top = s.max()
    if top <= 0:
        return df.iloc[0:0], fixes
    keep = s[s >= top * _KEEP].sort_values(ascending=False, kind="stable")
    return df.loc[keep.index], fixes


def spelling_fixes(df: pd.DataFrame, query: str, tax) -> dict[str, str]:
    """{typed word: corrected word} for the search, for a "Showing results for" line."""
    if not query.strip() or df.empty:
        return {}
    return correct_query(query, _index(df, tax))[1]


def keyword_search(df: pd.DataFrame, query: str, tax) -> pd.DataFrame:
    return rank(df, query, tax)[0]


# Kept so app.py's existing call keeps working.
search_orgs = keyword_search


# ── AI layer ──────────────────────────────────────────────────────────────────
PROMPT_VERSION = "v3"  # bump when the prompt or model settings change, so cached answers are redone

PROMPT = """You help residents of Lawrence, Massachusetts find local organizations.

Below is a numbered directory. A resident typed a search, in any language, possibly
with typos, possibly describing their situation rather than naming a service.

Return the organizations that can directly help with what they need, best first,
at most {limit}. Rules:
- Only use ids from the directory. Never invent an organization.
- Judge only from each organization's listed services. Do not assume services
  that are not listed.
- Only include organizations that directly offer help with this need. Do not
  pad the list with loosely related ones (a school's student meals are not food
  help for the public; general counseling is not domestic violence help).
  Fewer, better matches are better. If nothing fits, return an empty list.
- If the search is an organization's name or acronym, return that organization first.
- For each match, write the reason as one short, plain sentence a resident would
  understand (at most 15 words), saying what they can get there. Not a list of
  service names. Same language as the search. Based only on the listing.

DIRECTORY
{directory}

SEARCH: {query}"""


def directory_text(df: pd.DataFrame, tax) -> str:
    """One compact line per org, numbered by its row position in df."""
    lines = []
    for i, (_idx, r) in enumerate(df.iterrows()):
        specifics = sorted({s for _c, s in r["Specifics"]})
        parts = [f"[{i}] {r['Name']}"]
        if str(r.get("Description", "")).strip():
            parts.append(f"about: {r['Description']}")
        if specifics:
            parts.append("services: " + ", ".join(specifics))
        if r["SvcTagList"]:
            parts.append("in their words: " + ", ".join(r["SvcTagList"]))
        elif r["ServiceArea"]:
            parts.append("area: " + r["ServiceArea"])
        lines.append(" | ".join(parts))
    return "\n".join(lines)


class GeminiMatcher:
    """Calls Gemini on Vertex AI.

    Locally it uses the same .env setup as the crawler:
    GOOGLE_APPLICATION_CREDENTIALS and GEMINI_PROJECT_ID. On the live site,
    app.py passes credentials from Streamlit secrets instead. Optional:
    GEMINI_MODEL (default gemini-2.5-flash), GEMINI_LOCATION (default us-central1).

    Answers are cached in memory, and on disk if cache_path is given, keyed on
    the query, the directory text, the model and the prompt version — so a
    data or prompt change never serves a stale answer.
    """

    MAX_CACHED = 2000  # in-memory answers kept before starting over

    def __init__(self, cache_path: str | None = None, limit: int = 8,
                 credentials=None, project: str | None = None,
                 model: str | None = None, location: str | None = None):
        from dotenv import load_dotenv
        from google import genai
        from google.genai import types

        load_dotenv()
        project = project or os.getenv("GEMINI_PROJECT_ID")
        if not project or not (credentials or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")):
            raise RuntimeError("Set GOOGLE_APPLICATION_CREDENTIALS and GEMINI_PROJECT_ID in .env")
        self.client = genai.Client(
            vertexai=True, project=project, credentials=credentials,
            location=location or os.getenv("GEMINI_LOCATION", "us-central1"),
            # A stuck call shouldn't freeze the page; smart_search falls back.
            http_options=types.HttpOptions(timeout=15_000),
        )
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.thinking = self._thinking_config(types)
        self.limit = limit
        self.cache_path = cache_path
        self.cache: dict[str, list[list]] = {}
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as fh:
                self.cache = json.load(fh)

    def _thinking_config(self, types):
        """Turn the model's "thinking" down. Picking orgs from a list doesn't
        need it, and it's most of the wait: Gemini 2.5 Flash thinks by default.
        GEMINI_THINKING=on in .env/secrets restores the model's default."""
        if os.getenv("GEMINI_THINKING", "").lower() == "on":
            return None
        m = self.model.lower()
        if "2.5-flash" in m:
            return types.ThinkingConfig(thinking_budget=0)
        if m.startswith("gemini-3"):
            return types.ThinkingConfig(thinking_level="LOW")
        return None

    def _key(self, query: str, directory: str) -> str:
        raw = "\n".join([PROMPT_VERSION, self.model, fold(query.strip().lower()), directory])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def __call__(self, query: str, directory: str) -> list[tuple[int, str]]:
        key = self._key(query, directory)
        if key not in self.cache:
            if len(self.cache) >= self.MAX_CACHED:
                self.cache.clear()
            self.cache[key] = self._ask(query, directory)
            if self.cache_path:
                with open(self.cache_path, "w", encoding="utf-8") as fh:
                    json.dump(self.cache, fh)
        return [(int(i), r) for i, r in self.cache[key]]

    def _ask(self, query: str, directory: str) -> list[list]:
        from google.genai import types
        from pydantic import BaseModel

        class Match(BaseModel):
            id: int
            reason: str

        class Matches(BaseModel):
            matches: list[Match]

        prompt = PROMPT.format(limit=self.limit, directory=directory, query=query)
        for attempt in range(4):  # back off on rate limits
            config = types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=Matches,
                thinking_config=self.thinking,
            )
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=prompt, config=config
                )
                parsed = resp.parsed or Matches.model_validate_json(resp.text)
                return [[m.id, m.reason] for m in parsed.matches][: self.limit]
            except Exception as exc:
                msg = str(exc).lower()
                if self.thinking is not None and "think" in msg:
                    self.thinking = None  # this model rejects the setting; use its default
                    continue
                if "429" in msg and attempt < 3:
                    time.sleep(2 ** attempt * 5)
                    continue
                raise
        return []


def ai_match(df: pd.DataFrame, query: str, tax, matcher) -> tuple[pd.DataFrame, dict[str, str]]:
    """Orgs Gemini picked, best first, plus {org name: reason}.
    Ids that aren't real rows are dropped, so it can't return an invented org."""
    picks = matcher(query, directory_text(df, tax))
    seen, rows, reasons = set(), [], {}
    for i, reason in picks:
        if 0 <= i < len(df) and i not in seen:
            seen.add(i)
            rows.append(i)
            reasons[df.iloc[i]["Name"]] = reason.strip()
    return df.iloc[rows], reasons


# ── deciding between them ─────────────────────────────────────────────────────
_SENTENCE_WORDS = {
    "i", "im", "my", "me", "need", "want", "help", "can", "cant", "how", "where",
    "for", "with", "to", "a", "is", "am", "and", "lost", "getting",
    "yo", "mi", "necesito", "quiero", "ayuda", "para", "con", "estoy", "donde", "como",
}


def looks_like_a_sentence(query: str) -> bool:
    """'I can't pay my rent' -> True; 'food pantry', 'GLCAC' -> False."""
    words = re.findall(r"[a-z]+", fold(query.lower()).replace("'", ""))
    return len(words) >= 4 or any(w in _SENTENCE_WORDS for w in words)


KW_TOO_FEW = 3    # fewer keyword hits than this: let the AI look
KW_TOO_MANY = 8   # more than this: let the AI pick the best ones to show first


def smart_search(df: pd.DataFrame, query: str, tax, matcher=None):
    """Returns (results best-first, {org name: reason}, mode).

    Keyword search runs first; it's instant and enough for names and exact
    services. The AI layer runs when the query reads like a sentence, or
    keyword search found too few orgs, or too many to be useful unranked.
    Any AI failure falls back to keyword results, so search never breaks.
    """
    kw = keyword_search(df, query, tax)
    if matcher is None or not query.strip():
        return kw, {}, "keyword"
    sentence = looks_like_a_sentence(query)
    too_many = len(kw) > KW_TOO_MANY
    q = fold(query.strip().lower())
    named = kw.loc[kw["Name"].map(lambda n: q in fold(n.lower())).astype(bool)]
    if not sentence and not too_many and (len(kw) >= KW_TOO_FEW or not named.empty):
        # Enough keyword hits, or the search is an org's name ("ymca").
        return kw, {}, "keyword"

    try:
        ai, reasons = ai_match(df, query, tax, matcher)
    except Exception:
        return kw, {}, "keyword (AI failed)"
    if ai.empty:
        return kw, {}, "keyword (AI found nothing)"

    # An org whose name contains the search goes first, even if the AI missed it.
    parts = [named, ai[~ai["Name"].isin(named["Name"])]]
    if too_many and not sentence:
        # A broad search like "food": the AI's picks go first, but every
        # keyword match stays in the list, so nothing that used to show up
        # disappears.
        shown = set(named["Name"]) | set(ai["Name"])
        parts.append(kw[~kw["Name"].isin(shown)])
    return pd.concat(parts), reasons, "ai"