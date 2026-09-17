"""
GWI Nonprofit Partner Explorer  —  Streamlit app
Run:  streamlit run app.py
"""

import os
import re

from urllib.parse import quote_plus

import folium
import pandas as pd
import requests
import streamlit as st

from streamlit_folium import st_folium

from search_utils import matches as _search_matches
from service_taxonomy import (
    CATEGORY_ORDER,
    QUICK_FILTERS,
    TAGS,
    canonical_tags,
    category_label,
    display_label,
    org_categories,
    sort_key,
    tag_label,
)

# CartoDB's free basemap tiles (Positron, Voyager, etc.) now require an API
# key, and folium's "CartoDB positron" alias points at that gated endpoint.
# Esri's Light Gray Canvas gives a comparable clean/minimal look with no key.
_POSITRON_TILES = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
)
_POSITRON_ATTR = "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ"

# A few more no-API-key Esri basemaps, offered as switchable layers on the
# main map so community members can compare options live during a
# presentation (click the layer icon, top-right of the map).
_STREETS_TILES = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Street_Map/MapServer/tile/{z}/{y}/{x}"
)
_STREETS_ATTR = "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ, TomTom, Intermap"

_IMAGERY_TILES = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
_IMAGERY_ATTR = "Tiles &copy; Esri &mdash; Esri, Maxar, Earthstar Geographics"

_TOPO_TILES = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Topo_Map/MapServer/tile/{z}/{y}/{x}"
)
_TOPO_ATTR = "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, FAO, NOAA, USGS"


def _add_basemap(m: folium.Map) -> None:
    """Add the Esri Light Gray basemap, capped at its native tile zoom.

    This layer has no cached tiles past zoom 16 in our service area — the
    server responds 200 with a small "Map data not yet available" image
    instead of 404, so Leaflet has no way to know to stop. max_native_zoom
    keeps tile requests at 16 and upscales those tiles for deeper zooms
    instead of requesting the placeholder.
    """
    folium.TileLayer(
        tiles=_POSITRON_TILES,
        attr=_POSITRON_ATTR,
        max_native_zoom=16,
        max_zoom=19,
    ).add_to(m)


def _add_basemap_layers(m: folium.Map) -> None:
    """Add several switchable basemaps plus a layer-picker control.

    Used on the main map only, so community members can toggle between
    options during a presentation and give feedback on which they prefer.

    Streets is the default (`show=True`); the rest are opt-in. Exactly one
    layer may carry `show=True` — with two, Leaflet renders both and the
    stacked tiles show through each other.
    """
    folium.TileLayer(
        tiles="OpenStreetMap",
        name=_("basemap_osm"),
        show=False,
    ).add_to(m)
    folium.TileLayer(
        tiles=_POSITRON_TILES,
        attr=_POSITRON_ATTR,
        name=_("basemap_light_gray"),
        max_native_zoom=16,
        max_zoom=19,
        show=False,
    ).add_to(m)
    folium.TileLayer(
        tiles=_STREETS_TILES,
        attr=_STREETS_ATTR,
        name=_("basemap_streets"),
        max_zoom=19,
        show=True,
    ).add_to(m)
    folium.TileLayer(
        tiles=_IMAGERY_TILES,
        attr=_IMAGERY_ATTR,
        name=_("basemap_satellite"),
        max_zoom=19,
        show=False,
    ).add_to(m)
    folium.TileLayer(
        tiles=_TOPO_TILES,
        attr=_TOPO_ATTR,
        name=_("basemap_topo"),
        max_zoom=19,
        show=False,
    ).add_to(m)


# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Lawrence Resource Finder",
    page_icon="🗺️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── design tokens ─────────────────────────────────────────────────────────────
BRAND_DARK = "#1e3a5f"
BRAND_MED = "#2d6cb4"
TEXT_DARK = "#1e293b"
TEXT_MID = "#475569"
BG_WHITE = "#ffffff"
BG_LIGHT = "#f8fafc"
BG_SIDEBAR = "#f0f4f8"
BORDER = "#e2e8f0"

# Muted text. Replaces an earlier lighter grey that scored only 2.6:1 on white
# and failed WCAG AA; this clears 4.5:1 wherever grey text still carries meaning.
TEXT_MUTED = "#64748b"

# System stack, no webfont. The previous stack named Inter but nothing ever
# loaded it, so every user fell through to Segoe UI or Arial. Naming the native
# faces gets SF on Apple and Roboto on Android with no request and no reflow.
FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)

# Type scale, base 16. Six steps, each a perceptible jump — earlier code used
# eleven sizes from 10 to 28, where neighbours like 13/14/15 read as drift
# rather than hierarchy. 12 is the floor; nothing renders smaller.
#   12 micro      badges, popup row labels
#   14 supporting chips, captions, filter labels, buttons
#   16 body       default text, tab labels, popup org name
#   20 panel      sidebar heading
#   24 section    org detail name
#   30 page       h1

GEMINI_GEM_URL = "https://gemini.google.com/gem/ca6a37604b8a?usp=sharing"

# ↓↓↓ PASTE THE GOOGLE FORM LINK HERE ↓↓↓
# Until this is filled in, the Give Feedback button still renders but is inert
# and says so on hover, rather than silently linking nowhere.
FEEDBACK_FORM_URL = ""

# ── i18n ──────────────────────────────────────────────────────────────────────
T = {
    "en": {
        "filters_heading": "🔍 Filters",
        "orgs_total": "{n} organizations total",
        "search_label": "Search",
        "search_placeholder": "Name, city, or service…",
        "category_label": "Category",
        "category_placeholder": "Broad area of need…",
        "services_label": "Specific service",
        "services_placeholder": "Type to search services…",
        "orgtype_label": "Organization Type",
        "orgtype_all": "All",
        "reset_button": "↺  Reset all filters",
        "page_heading": "Lawrence Resource Finder",
        "showing_all": "Showing all {n} organizations",
        "showing_filtered": "Showing {n} of {total} organizations",
        "nav_cta": "Ask AI assistant",
        "feedback_cta": "💬 Give Feedback",
        "feedback_unset": "Feedback form link not yet configured",
        "tab_map": "🗺️  Map",
        "tab_directory": "📋  Directory",
        "tab_org_detail": "🔎  Organization Detail",
        "no_results": "No organizations match the current filters.  \nTry adjusting the filters or click **↺ Reset** in the sidebar.",
        "no_coords": "Matching organizations have no coordinates to plot.",
        "map_caption": "{n} organizations plotted · Click a marker for details · use the layers icon (top-right of the map) to try different basemaps",
        "basemap_light_gray": "Light Gray",
        "basemap_streets": "Streets",
        "basemap_satellite": "Satellite",
        "basemap_topo": "Topographic",
        "basemap_osm": "OpenStreetMap",
        "boundary_layer_name": "Lawrence, MA boundary",
        "popup_address": "Address",
        "popup_phone": "Phone",
        "popup_hours": "Hours",
        "popup_type": "Type",
        "popup_services": "Services",
        "popup_impact": "Impact Report",
        "popup_strategic": "Strategic Plan",
        "view_link": "View",
        "not_available_short": "N/A",
        "visit_website": "🔗 Visit Website",
        "get_directions": "📍 Get Directions",
        "no_website_listed": "No website listed",
        "download_button": "⬇️  Download {n} results as CSV",
        "col_name": "Name",
        "col_city": "City",
        "col_orgtype": "Org Type",
        "col_servicearea": "Service Area",
        "col_services": "Services",
        "link_open": "🔗 Open",
        "impact_open": "📊 Open",
        "strategic_open": "🧭 Open",
        "select_org": "Select an organization",
        "org_not_found": "Organization not found — please try another selection.",
        "sec_location": "Location",
        "sec_orgtype": "Organization Type",
        "sec_phone": "Phone",
        "sec_hours": "Hours",
        "sec_website": "Website",
        "sec_impact": "Impact Report",
        "sec_strategic": "Strategic Plan",
        "sec_services": "Services",
        "sec_map": "Location on Map",
        "not_available": "Not available",
        "not_specified": "Not specified",
        "not_listed": "Not listed",
        "no_map_coords": "No map coordinates available for this organization.",
        "popup_locations": "Locations",
        "sec_locations": "Locations & Hours",
        "loc_admin_badge": "Office only — no walk-in services",
        "loc_admin_note": "The pin above is this organization's office. Services are provided at the locations listed here.",
        "loc_directions": "Directions",
        "loc_call": "Call",
        "loc_no_address": "By phone only — no public address listed",
        "quick_heading": "Common needs — tap to filter the map",
        "quick_food_pantry": "🥫 Food Pantry",
        "quick_meals": "🍽️ Free Meals",
        "quick_shelter": "🛏️ Shelter",
        "quick_housing": "🏠 Rent & Housing Help",
        "quick_health": "🩺 Health Care",
        "quick_immigration": "🛂 Immigration Legal",
        "quick_childcare": "🧸 Child Care",
        "quick_jobs": "💼 Jobs & Training",
    },
    "es": {
        "filters_heading": "🔍 Filtros",
        "orgs_total": "{n} organizaciones en total",
        "search_label": "Buscar",
        "search_placeholder": "Nombre, ciudad o servicio…",
        "category_label": "Categoría",
        "category_placeholder": "Área general de necesidad…",
        "services_label": "Servicio específico",
        "services_placeholder": "Escriba para buscar servicios…",
        "orgtype_label": "Tipo de Organización",
        "orgtype_all": "Todos",
        "reset_button": "↺  Restablecer filtros",
        "page_heading": "Buscador de Recursos de Lawrence",
        "showing_all": "Mostrando las {n} organizaciones",
        "showing_filtered": "Mostrando {n} de {total} organizaciones",
        "nav_cta": "Preguntar al asistente de IA",
        "feedback_cta": "💬 Enviar Comentarios",
        "feedback_unset": "El enlace del formulario aún no está configurado",
        "tab_map": "🗺️  Mapa",
        "tab_directory": "📋  Directorio",
        "tab_org_detail": "🔎  Detalle de la Organización",
        "no_results": "Ninguna organización coincide con los filtros actuales.  \nAjuste los filtros o presione **↺ Restablecer** en la barra lateral.",
        "no_coords": "Las organizaciones encontradas no tienen coordenadas para mostrar en el mapa.",
        "map_caption": "{n} organizaciones en el mapa · Haga clic en un marcador para ver detalles · use el ícono de capas (arriba a la derecha del mapa) para probar diferentes mapas base",
        "basemap_light_gray": "Gris Claro",
        "basemap_streets": "Calles",
        "basemap_satellite": "Satélite",
        "basemap_topo": "Topográfico",
        "basemap_osm": "OpenStreetMap",
        "boundary_layer_name": "Límite de Lawrence, MA",
        "popup_address": "Dirección",
        "popup_phone": "Teléfono",
        "popup_hours": "Horario",
        "popup_type": "Tipo",
        "popup_services": "Servicios",
        "popup_impact": "Informe de Impacto",
        "popup_strategic": "Plan Estratégico",
        "view_link": "Ver",
        "not_available_short": "N/D",
        "visit_website": "🔗 Visitar Sitio Web",
        "get_directions": "📍 Cómo Llegar",
        "no_website_listed": "Sin sitio web registrado",
        "download_button": "⬇️  Descargar {n} resultados en CSV",
        "col_name": "Nombre",
        "col_city": "Ciudad",
        "col_orgtype": "Tipo de Org.",
        "col_servicearea": "Área de Servicio",
        "col_services": "Servicios",
        "link_open": "🔗 Abrir",
        "impact_open": "📊 Abrir",
        "strategic_open": "🧭 Abrir",
        "select_org": "Seleccione una organización",
        "org_not_found": "Organización no encontrada — intente otra selección.",
        "sec_location": "Ubicación",
        "sec_orgtype": "Tipo de Organización",
        "sec_phone": "Teléfono",
        "sec_hours": "Horario",
        "sec_website": "Sitio Web",
        "sec_impact": "Informe de Impacto",
        "sec_strategic": "Plan Estratégico",
        "sec_services": "Servicios",
        "sec_map": "Ubicación en el Mapa",
        "not_available": "No disponible",
        "not_specified": "No especificado",
        "not_listed": "No disponible",
        "no_map_coords": "No hay coordenadas de mapa disponibles para esta organización.",
        "popup_locations": "Ubicaciones",
        "sec_locations": "Ubicaciones y Horarios",
        "loc_admin_badge": "Solo oficina — no atiende sin cita",
        "loc_admin_note": "El marcador de arriba es la oficina de esta organización. Los servicios se ofrecen en las ubicaciones que aparecen aquí.",
        "loc_directions": "Cómo llegar",
        "loc_call": "Llamar",
        "loc_no_address": "Solo por teléfono — sin dirección pública",
        "quick_heading": "Necesidades comunes — toque para filtrar el mapa",
        "quick_food_pantry": "🥫 Despensa de Alimentos",
        "quick_meals": "🍽️ Comidas Gratis",
        "quick_shelter": "🛏️ Refugio",
        "quick_housing": "🏠 Ayuda con Alquiler y Vivienda",
        "quick_health": "🩺 Atención Médica",
        "quick_immigration": "🛂 Legal de Inmigración",
        "quick_childcare": "🧸 Cuidado Infantil",
        "quick_jobs": "💼 Empleo y Capacitación",
    },
}

ORG_TYPE_ES = {
    "Non-profit organization": "Organización sin fines de lucro",
    "Faith-based Organization": "Organización religiosa",
    "Healthcare System": "Sistema de salud",
    "Higher Education": "Educación superior",
    "K-12 School": "Escuela K-12",
    "Municipal Agency": "Agencia municipal",
}

if "lang" not in st.session_state:
    st.session_state["lang"] = "en"


def _(key: str) -> str:
    return T[st.session_state["lang"]][key]


def _org_type_label(org_type: str) -> str:
    if st.session_state["lang"] == "es":
        return ORG_TYPE_ES.get(org_type, org_type)
    return org_type


def _format_phone(raw: str) -> str:
    """Normalise a US phone number to (NNN) NNN-NNNN.

    Returns the input untouched when it is not a 10-digit number — the Phone
    column is hand-maintained and one cell holds "multiple locations" rather
    than a number, which must survive to the popup as written.
    """
    text = str(raw).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return text
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def _smart_split(s: str) -> list[str]:
    """Split on commas that are NOT inside parentheses.

    Source strings are written as prose with an Oxford comma ("X, Y, and Z"),
    so splitting on every comma strands the "and" from the last item on its
    own fragment ("and Z"). Strip it back off.
    """
    if not s:
        return []
    parts = [p.strip() for p in re.split(r",(?![^(]*\))", s) if p.strip()]
    parts = [re.sub(r"^and\s+", "", p, flags=re.IGNORECASE) for p in parts]
    return [p for p in parts if p]


# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown(
    f"""
<style>
  html, body, [class*="css"] {{ color:{TEXT_DARK} !important;
    font-family:{FONT_STACK} !important;
    font-size:16px; line-height:1.5; }}
  .stApp {{ background-color:{BG_WHITE}; }}

  section[data-testid="stSidebar"] {{ background-color:{BG_SIDEBAR} !important; }}
  section[data-testid="stSidebar"] * {{ color:{TEXT_DARK} !important; }}
  section[data-testid="stSidebar"] label {{
    color:{TEXT_MID} !important; font-size:14px !important;
    font-weight:600 !important; }}
  section[data-testid="stSidebar"] input,
  section[data-testid="stSidebar"] [data-baseweb="select"] {{
    background:{BG_WHITE} !important; color:{TEXT_DARK} !important;
    border:1px solid {BORDER} !important; border-radius:8px !important; }}
  section[data-testid="stSidebar"] [data-baseweb="select"] * {{
    color:{TEXT_DARK} !important; background:{BG_WHITE} !important; }}

  /* Selected multiselect chips: the wildcard above painted dark text on the
     dark primary-color chip, leaving it unreadable. Give chips a light fill
     with dark text so the selection stands out against the white control. */
  section[data-testid="stSidebar"] [data-baseweb="tag"],
  section[data-testid="stSidebar"] [data-baseweb="tag"] * {{
    background:#dbeafe !important; color:{BRAND_DARK} !important; }}
  section[data-testid="stSidebar"] [data-baseweb="tag"] {{
    border:1px solid #93c5fd !important; border-radius:6px !important;
    font-weight:600 !important; }}
  section[data-testid="stSidebar"] [data-baseweb="tag"] svg {{
    fill:{BRAND_DARK} !important; }}
  section[data-testid="stSidebar"] [data-baseweb="tag"] [role="presentation"]:hover,
  section[data-testid="stSidebar"] [data-baseweb="tag"] span[role="button"]:hover {{
    background:#bfdbfe !important; }}

  .block-container {{ padding-top:1.5rem; padding-bottom:2rem; max-width:1400px; }}
  h1,h2,h3,h4,h5,h6 {{ color:{BRAND_DARK} !important; line-height:1.25; }}

  .filter-pill {{
    display:inline-block; background:#dbeafe; color:#1e40af;
    border-radius:20px; padding:3px 12px; font-size:12px;
    margin:2px 3px; font-weight:600; border:1px solid #bfdbfe; }}

  .cat-badge {{
    display:inline-block; border-radius:20px; padding:2px 10px;
    font-size:12px; font-weight:700; margin:2px; color:white; }}

  .svc-chip {{
    display:inline-block; background:#f1f5f9; color:{TEXT_DARK};
    border:1px solid {BORDER}; border-radius:6px;
    padding:3px 10px; margin:3px; font-size:14px; line-height:1.5; }}

  [data-testid="stDownloadButton"] button {{
    background-color:{BRAND_DARK} !important; color:white !important;
    border-radius:8px !important; border:none !important; font-weight:600 !important; }}

  section[data-testid="stSidebar"] [data-testid="stButton"] button {{
    background:{BG_WHITE} !important; color:{BRAND_DARK} !important;
    border:1.5px solid {BORDER} !important; border-radius:8px !important; font-weight:600 !important; }}

  [data-testid="stDataFrame"] {{ border:1px solid {BORDER}; border-radius:10px; overflow:hidden; }}
  [data-testid="stAlert"] {{ border-radius:8px !important; }}
  hr {{ border:none; border-top:1px solid {BORDER}; margin:10px 0; }}

  [data-testid="stTabs"] button[role="tab"] {{
    font-size:16px !important; font-weight:600 !important; }}

  div[data-testid="stRadio"] {{ margin-top:18px; }}
</style>
""",
    unsafe_allow_html=True,
)


# ── Lawrence, MA boundary ─────────────────────────────────────────────────────
@st.cache_data(ttl=86400)
def fetch_lawrence_boundary() -> dict | None:
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": "Lawrence, MA, USA",
                "format": "json",
                "polygon_geojson": "1",
                "limit": "1",
            },
            headers={"User-Agent": "GWI-Nonprofit-Explorer/1.0"},
            timeout=10,
        )
        results = resp.json()
        if results and "geojson" in results[0]:
            return results[0]["geojson"]
    except Exception:
        pass
    return None


# ── load & prep data ──────────────────────────────────────────────────────────
CSV_PATH = "data/GWIorgs_v6.csv"


@st.cache_data(ttl=3600)
def load_data(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()

    df = pd.read_csv(path, dtype=str).fillna("")
    df = df[df["Name"].str.strip() != ""].reset_index(drop=True)

    df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")

    # Every MA zip starts with 0, which spreadsheet round-trips strip by
    # reading the column as a number ("01840" -> 1840). Re-pad on load so a
    # future re-export of the CSV can't silently reintroduce it.
    df["Zip"] = df["Zip"].str.strip().apply(
        lambda z: z.zfill(5) if z.isdigit() and len(z) < 5 else z
    )

    # Phone numbers are hand-entered, so they arrive in whatever style the
    # source site used — "(978) 555-1234", "978-555-1234", "978.555.1234" and
    # bare "9785551234" are all present. Normalise the display so a column of
    # them scans as one list; anything that is not a 10-digit US number (one
    # cell reads "multiple locations") is left exactly as written.
    df["Phone"] = df["Phone"].apply(_format_phone)

    df["SvcList"] = df["ServiceArea"].apply(_smart_split)
    if "Services" not in df.columns:
        df["Services"] = ""
    df["SvcTagList"] = df["Services"].apply(_smart_split)

    # The raw tags stay on the row — popups, the directory table and the detail
    # panel all keep showing an org's own wording. Only the sidebar filter uses
    # the canonical set, because 485 one-off raw tags is not a usable dropdown.
    #
    # One org has an empty Services cell; fall back to its broader ServiceArea
    # so a data gap doesn't make it unreachable from every service filter.
    df["SvcCanonical"] = df.apply(
        lambda r: canonical_tags(r["SvcTagList"] or r["SvcList"]), axis=1
    )

    # Categories come from the categorization draft (data/service_categories.csv),
    # not from the canonical tags — the draft is the authority on which category
    # a service belongs to, and it assigns several services to more than one.
    df["Categories"] = df.apply(
        lambda r: org_categories(r["SvcTagList"] or r["SvcList"]), axis=1
    )

    df = df.rename(
        columns={"Impact Report": "ImpactReport", "Strategic Plan": "StrategicPlan"}
    )

    for col in ("ImpactReport", "StrategicPlan", "Phone", "Hours"):
        if col not in df.columns:
            df[col] = ""

    return df


LOCATIONS_PATH = "data/org_locations.csv"


@st.cache_data(ttl=3600)
def load_locations(path: str) -> dict[str, list[dict]]:
    """Extra sites for orgs that operate more than one.

    Most orgs have a single address and are fully described by the Phone and
    Hours columns on their own row. A handful run their services out of
    several buildings — Lazarus House's soup kitchen, pantry and thrift store
    are three different addresses, and Neighbors in Need distributes food at
    six host churches while its own address is an office where no food is
    handed out. For those, one pin plus one address is actively misleading.

    Keyed by OrgName for an O(1) lookup inside the marker loop. A missing file
    returns {} so the dashboard still runs without this data.
    """
    if not os.path.exists(path):
        return {}

    loc_df = pd.read_csv(path, dtype=str).fillna("")
    loc_df["Zip"] = loc_df["Zip"].str.strip().apply(
        lambda z: z.zfill(5) if z.isdigit() and len(z) < 5 else z
    )

    out: dict[str, list[dict]] = {}
    for _i, r in loc_df.iterrows():
        out.setdefault(r["OrgName"].strip(), []).append(r.to_dict())
    return out


df = load_data(CSV_PATH)
locations = load_locations(LOCATIONS_PATH)

if df.empty:
    st.error(
        f"**Data file not found:** `{CSV_PATH}`\n\nMake sure `{CSV_PATH}` is next to `app.py`."
    )
    st.stop()


# ── helpers ───────────────────────────────────────────────────────────────────
def _link_cell(val: str) -> str:  # New Change for v5
    if val and str(val).strip():
        v = str(val).strip()
        href = v if v.startswith("http") else f"https://{v}"
        return (
            f'<a href="{href}" target="_blank" '
            f'style="color:{BRAND_MED};text-decoration:underline;">{_("view_link")}</a>'
        )
    return f'<span style="color:{TEXT_MUTED};">{_("not_available_short")}</span>'


def _directions_url(lat, lng) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}"


def _directions_url_address(loc: dict) -> str:
    """Directions to a site that has an address but no coordinates.

    Sub-locations are deliberately not geocoded — they are listed inside an
    org's popup rather than plotted — so Google Maps gets the address string
    to resolve instead of a lat/lng pair.
    """
    parts = [loc.get(k, "").strip() for k in ("Address", "City", "State", "Zip")]
    dest = quote_plus(", ".join(p for p in parts if p))
    return f"https://www.google.com/maps/dir/?api=1&destination={dest}"


def _org_locations(name: str) -> list[dict]:
    """Extra sites for an org, public ones first so services lead the list."""
    rows = locations.get(str(name).strip(), [])
    return sorted(rows, key=lambda r: r.get("Kind", "") == "admin")


def _group_by_address(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Collapse an org's rows into one entry per physical building.

    Big providers list a separate row per program at the same street address —
    a health center's clinic and its pharmacy, or a dozen agency programs on
    three floors of one building. Rendering each as its own card is what made
    the popup unreadable. Grouping by address gives one card per place you
    would actually travel to, with its programs listed inside.

    Insertion-ordered, so the public-first sort from _org_locations survives.
    """
    groups: dict[str, list[dict]] = {}
    for loc in rows:
        key = " ".join(
            loc.get(k, "").strip().lower()
            for k in ("Address", "City", "State", "Zip")
        ).strip()
        # Rows with no address (a hotline, a shelter with an unlisted site)
        # each stand alone rather than collapsing into one empty-key group.
        groups.setdefault(key or f"__noaddr_{id(loc)}", []).append(loc)
    return list(groups.items())


def _locations_html(rows: list[dict]) -> str:
    """Render an org's sites as a scrollable block for the map popup.

    One card per building (see _group_by_address), capped with max-height so a
    provider with many programs cannot push the popup off the map.
    """
    if not rows:
        return ""

    groups = _group_by_address(rows)
    cards: list[str] = []

    for _key, locs in groups:
        head = locs[0]
        is_admin = all(loc.get("Kind", "") == "admin" for loc in locs)
        addr = ", ".join(
            p
            for p in (
                head.get(k, "").strip() for k in ("Address", "City", "State", "Zip")
            )
            if p
        )

        # Card title: the shared site name when programs differ only by suffix,
        # otherwise the first location's name.
        title = head.get("LocationName", "").strip()
        if len(locs) > 1:
            title = title.split("—")[0].strip() or title

        badge = (
            f'<span style="background:{BG_SIDEBAR};color:{TEXT_MUTED};'
            f"padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;"
            f'text-transform:uppercase;margin-left:6px;">'
            f'{_("loc_admin_badge")}</span>'
            if is_admin
            else ""
        )

        if addr:
            addr_html = (
                f'<div style="color:{TEXT_MID};">{addr}</div>'
                f'<a href="{_directions_url_address(head)}" target="_blank" '
                f'style="color:{BRAND_MED};font-weight:600;text-decoration:none;">'
                f'{_("loc_directions")} →</a>'
            )
        else:
            addr_html = (
                f'<div style="color:{TEXT_MUTED};font-style:italic;">'
                f'{_("loc_no_address")}</div>'
            )

        # One line per program at this address. A single-program site needs no
        # program line — its name is already the card title.
        prog_html = ""
        if len(locs) > 1:
            lines = []
            for loc in locs:
                name = loc.get("LocationName", "").strip()
                label = name.split("—")[-1].strip() if "—" in name else name
                svc = loc.get("ServicesHere", "").strip()
                detail = " · ".join(
                    x
                    for x in (
                        loc.get("Phone", "").strip(),
                        "; ".join(_hours_lines(loc.get("Hours", ""))),
                    )
                    if x
                )
                lines.append(
                    f'<div style="margin-top:4px;">'
                    f'<span style="font-weight:600;color:{TEXT_DARK};">{label}</span>'
                    + (
                        f'<span style="color:{TEXT_MUTED};"> — {svc}</span>'
                        if svc and svc.lower() != label.lower()
                        else ""
                    )
                    + (
                        f'<div style="color:{TEXT_MID};">{detail}</div>'
                        if detail
                        else ""
                    )
                    + "</div>"
                )
            prog_html = "".join(lines)
        else:
            svc = head.get("ServicesHere", "").strip()
            if svc:
                prog_html += (
                    f'<div style="color:{BRAND_DARK};font-size:11px;'
                    f'font-weight:600;">{svc}</div>'
                )
            if head.get("Phone", "").strip():
                prog_html += _tel_link(head["Phone"].strip())
            if head.get("Hours", "").strip():
                prog_html += (
                    f'<div style="color:{TEXT_MID};">'
                    f'{"<br>".join(_hours_lines(head["Hours"]))}</div>'
                )

        cards.append(
            f'<div style="padding:7px 0;border-top:1px solid #e8eef5;">'
            f'<div style="font-weight:700;color:{TEXT_DARK};">{title}{badge}</div>'
            f'<div style="display:flex;flex-direction:column;gap:2px;'
            f'margin-top:3px;">{addr_html}</div>'
            f"{prog_html}</div>"
        )

    # If the org's own pin is an office, say so — sites are listed here rather
    # than plotted, so the pin itself cannot self-explain.
    note = (
        f'<div style="background:#fff6e5;border-left:3px solid #d9922e;'
        f"padding:6px 8px;margin:6px 0;color:{TEXT_DARK};font-size:11px;"
        f'line-height:1.4;">{_("loc_admin_note")}</div>'
        if any(r.get("Kind", "") == "admin" for r in rows)
        else ""
    )

    return (
        f'<div style="margin-top:10px;">'
        f'<div style="color:{TEXT_MUTED};font-size:12px;font-weight:700;'
        f'text-transform:uppercase;">{_("popup_locations")} ({len(groups)})</div>'
        f"{note}"
        f'<div style="max-height:240px;overflow-y:auto;font-size:12px;">'
        f'{"".join(cards)}</div></div>'
    )


def _tel_link(phone: str) -> str:
    """Render a phone number as a tap-to-call link (works on mobile).

    Not every Phone cell holds a number — one org's reads "multiple locations".
    Linking that produces `tel:` with nothing after it, which on a phone opens
    the dialler with a blank number. Anything without a dialable run of digits
    renders as plain text instead.
    """
    text = str(phone).strip()
    digits = re.sub(r"[^\d+]", "", text)
    if len(re.sub(r"\D", "", digits)) < 7:
        return f'<span style="color:{TEXT_MID};">{text}</span>'
    return (
        f'<a href="tel:{digits}" '
        f'style="color:{BRAND_MED};text-decoration:none;font-weight:600;">{text}</a>'
    )


def _hours_lines(hours: str) -> list[str]:
    """Split an Hours string into its per-line segments.

    Hours are authored semicolon-separated ("Mon 9 AM–5 PM; Sun closed") so
    each segment can render on its own line instead of one long run-on.
    """
    return [seg.strip() for seg in str(hours).split(";") if seg.strip()]


# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        f"<p style='font-size:20px;font-weight:700;color:{BRAND_DARK};"
        f"margin:0 0 4px;'>{_('filters_heading')}</p>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<p style='font-size:14px;color:{TEXT_MID};margin:0 0 12px;'>"
        f"{_('orgs_total').format(n=len(df))}</p>",
        unsafe_allow_html=True,
    )
    st.divider()

    search = st.text_input(
        _("search_label"), placeholder=_("search_placeholder"), key="search"
    )

    # Options come from the data, not from the full taxonomy, so a tag no org
    # offers is never a dead end. Ordered by category (see sort_key) so the
    # list reads as grouped sections rather than one alphabetical run.
    # Two levels of browsing. Category is the broad sweep straight from the
    # categorization draft; Service is the specific one. Category first because
    # most people know the area of need before the exact service name.
    all_categories = sorted(
        {c for lst in df["Categories"] for c in lst}, key=CATEGORY_ORDER.index
    )
    sel_cats = st.multiselect(
        _("category_label"),
        all_categories,
        key="sel_cats",
        placeholder=_("category_placeholder"),
        format_func=lambda c: category_label(c, st.session_state["lang"]),
    )

    in_data = {s for lst in df["SvcCanonical"] for s in lst}

    # Picking a category narrows the service list to that category's services,
    # so the second dropdown is a short menu of what is actually inside the
    # area you chose rather than all 76 every time.
    if sel_cats:
        scoped = {s for s in in_data if TAGS[s][0] in sel_cats}
        # A category whose services all sit under a different home category
        # would leave an empty menu; fall back to the full list rather than a
        # dropdown with nothing in it.
        all_services = sorted(scoped or in_data, key=sort_key)
    else:
        all_services = sorted(in_data, key=sort_key)

    # Selections made before the category changed can fall outside the new
    # option list, which Streamlit rejects. Prune them here — legal because the
    # widget below has not been instantiated yet this run.
    kept = [s for s in st.session_state.get("sel_svcs", []) if s in all_services]
    if kept != st.session_state.get("sel_svcs", []):
        st.session_state["sel_svcs"] = kept

    # With a single category chosen every option would repeat that category
    # name, so the prefix is dropped and the label is just the service.
    one_cat = len({TAGS[s][0] for s in all_services}) == 1
    sel_svcs = st.multiselect(
        _("services_label"),
        all_services,
        key="sel_svcs",
        placeholder=_("services_placeholder"),
        format_func=(
            (lambda tag: tag_label(tag, st.session_state["lang"]))
            if one_cat
            else (lambda tag: display_label(tag, st.session_state["lang"]))
        ),
    )

    all_org_types = sorted({t for t in df["OrgType"] if t})
    org_type_labels = [_("orgtype_all")] + [_org_type_label(t) for t in all_org_types]
    org_type_label_to_raw = {_("orgtype_all"): "All"}
    org_type_label_to_raw.update({_org_type_label(t): t for t in all_org_types})
    sel_org_type_label = st.selectbox(
        _("orgtype_label"), org_type_labels, key="sel_org_type_label"
    )
    sel_org_type = org_type_label_to_raw[sel_org_type_label]

    def _reset_filters():
        st.session_state["search"] = ""
        st.session_state["sel_cats"] = []
        st.session_state["sel_svcs"] = []
        st.session_state["sel_org_type_label"] = _("orgtype_all")

    st.divider()
    st.button(_("reset_button"), use_container_width=True, on_click=_reset_filters)


def _apply_quick_filter(tags: list[str]) -> None:
    """Jump the map to one common need.

    Must run as a button `on_click` callback, not inline after the button:
    `sel_svcs` belongs to the sidebar multiselect, which Streamlit has already
    instantiated by this point in the script, and assigning to a live widget's
    key raises. Callbacks run before the rerun, so the widget picks the value
    up cleanly — same pattern as _reset_filters above.

    Toggles: pressing the active chip again clears the filter, which matches
    what its `type="primary"` pressed state implies. Otherwise it replaces the
    selection rather than appending, so a chip is a clean jump instead of
    piling onto whatever was already selected.

    Also clears the category. The service dropdown is scoped to the chosen
    category, so a chip whose service sits outside it would be pruned away the
    moment it was set — the chip would appear to do nothing.
    """
    current = st.session_state.get("sel_svcs", [])
    st.session_state["sel_cats"] = []
    st.session_state["sel_svcs"] = [] if current == tags else tags


# ── apply filters ─────────────────────────────────────────────────────────────
filtered = df.copy()

if search:
    # Stem-aware matching with a small alias list — see search_utils.py.
    # Plain substring matching missed "diapers" against "Diaper Distribution"
    # and treated ESL/ESOL as unrelated.
    def _row_matches(row) -> bool:
        blob = " ".join(
            str(row[c]) for c in ("Name", "ServiceArea", "Services", "City")
        )
        return _search_matches(search, blob)

    filtered = filtered[filtered.apply(_row_matches, axis=1)]

if sel_cats:
    filtered = filtered[
        filtered["Categories"].apply(lambda lst: any(c in lst for c in sel_cats))
    ]

if sel_svcs:
    filtered = filtered[
        filtered["SvcCanonical"].apply(lambda lst: any(s in lst for s in sel_svcs))
    ]

if sel_org_type != "All":
    filtered = filtered[filtered["OrgType"] == sel_org_type]

n_filtered = len(filtered)
n_total = len(df)

# active filter pills
active_filters: list[str] = []
if search:
    active_filters.append(f'"{search}"')
if sel_cats:
    active_filters.extend(
        category_label(c, st.session_state["lang"]) for c in sel_cats
    )
if sel_svcs:
    active_filters.extend(
        tag_label(t, st.session_state["lang"]) for t in sel_svcs
    )
if sel_org_type != "All":
    active_filters.append(_org_type_label(sel_org_type))


# ── page header ───────────────────────────────────────────────────────────────
title_col, lang_col = st.columns([5, 2])
with title_col:
    st.markdown(
        f"<h1 style='color:{BRAND_DARK};font-size:30px;font-weight:700;margin:0 0 6px;'>"
        f"{_('page_heading')}</h1>",
        unsafe_allow_html=True,
    )
with lang_col:
    lang_choice = st.radio(
        "Language / Idioma",
        options=["en", "es"],
        format_func=lambda k: "English" if k == "en" else "Español",
        horizontal=True,
        index=0 if st.session_state["lang"] == "en" else 1,
        label_visibility="collapsed",
        key="lang_radio",
    )
    if lang_choice != st.session_state["lang"]:
        st.session_state["lang"] = lang_choice
        st.rerun()

hdr_l, hdr_r = st.columns([5, 4])
with hdr_l:
    txt = (
        _("showing_all").format(n=n_total)
        if n_filtered == n_total
        else _("showing_filtered").format(n=n_filtered, total=n_total)
    )
    st.markdown(
        f"<p style='color:{TEXT_MID};font-size:16px;margin:0;'>{txt}</p>",
        unsafe_allow_html=True,
    )
with hdr_r:
    # Feedback is an outline button so it reads as secondary next to the filled
    # AI CTA. With FEEDBACK_FORM_URL unset it still renders — the button was
    # wanted before the form exists — but points nowhere and says so on hover.
    if FEEDBACK_FORM_URL:
        feedback_attrs = (
            f'href="{FEEDBACK_FORM_URL}" target="_blank" rel="noopener noreferrer"'
        )
        feedback_extra = ""
    else:
        feedback_attrs = f'href="#" title="{_("feedback_unset")}"'
        feedback_extra = "opacity:.55;cursor:not-allowed;"

    st.markdown(
        f'<div style="display:flex;justify-content:flex-end;gap:10px;'
        f'flex-wrap:wrap;">'
        f"<a {feedback_attrs} "
        f'style="display:inline-flex;align-items:center;gap:8px;'
        f"background:transparent;color:{BRAND_MED};"
        f"border:2px solid {BRAND_MED};"
        f"padding:9px 16px;border-radius:8px;font-size:14px;font-weight:700;"
        f'text-decoration:none;{feedback_extra}">'
        f"{_('feedback_cta')}</a>"
        f'<a href="{GEMINI_GEM_URL}" target="_blank" rel="noopener noreferrer" '
        f'style="display:inline-flex;align-items:center;gap:8px;'
        f"background:{BRAND_MED};color:white;"
        f"padding:11px 18px;border-radius:8px;font-size:14px;font-weight:700;"
        f'text-decoration:none;">'
        f"{_('nav_cta')} ↗</a></div>",
        unsafe_allow_html=True,
    )

st.divider()

# ── tabs ──────────────────────────────────────────────────────────────────────
tab_map, tab_dir, tab_detail = st.tabs(
    [_("tab_map"), _("tab_directory"), _("tab_org_detail")]
)


# ══════════════════════════════════════════════════════════
# TAB 1 — MAP
# ══════════════════════════════════════════════════════════
with tab_map:
    # Quick filters sit above the map, outside the empty-state branches, so a
    # filter that matches nothing can still be changed without hunting for the
    # sidebar reset.
    st.markdown(
        f"<p style='font-size:14px;font-weight:700;color:{TEXT_MID};"
        f"margin:0 0 6px;'>{_('quick_heading')}</p>",
        unsafe_allow_html=True,
    )

    # Only offer a chip that leads somewhere: a tag no org carries would be a
    # button that empties the map.
    available = {t for lst in df["SvcCanonical"] for t in lst}
    live_chips = [
        (key, tags) for key, tags in QUICK_FILTERS if available.intersection(tags)
    ]

    for chip_row in (live_chips[:4], live_chips[4:]):
        if not chip_row:
            continue
        for col, (key, tags) in zip(st.columns(len(chip_row)), chip_row):
            with col:
                col.button(
                    _(key),
                    key=f"quick_{key}",
                    use_container_width=True,
                    on_click=_apply_quick_filter,
                    args=(tags,),
                    type="primary" if sel_svcs == tags else "secondary",
                )

    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)

    map_data = filtered.dropna(subset=["Latitude", "Longitude"])

    if filtered.empty:
        st.warning(_("no_results"))
    elif map_data.empty:
        st.info(_("no_coords"))
    else:
        m = folium.Map(
            location=[42.7070, -71.1631],
            zoom_start=13,
            tiles=None,
        )
        _add_basemap_layers(m)

        lawrence_geojson = fetch_lawrence_boundary()
        if lawrence_geojson:
            folium.GeoJson(
                lawrence_geojson,
                name=_("boundary_layer_name"),
                style_function=lambda _f: {
                    "color": BRAND_DARK,
                    "weight": 3,
                    "fillColor": BRAND_DARK,
                    "fillOpacity": 0.05,
                    "dashArray": "8 5",
                },
            ).add_to(m)

        for _idx, row in map_data.iterrows():
            # single pin colour — the category legend is gone from the UI, so
            # per-category colours would have nothing to decode them
            pin_color = BRAND_MED
            svc_tags = row["Services"] or _("not_specified")
            org_type = _org_type_label(row["OrgType"]) or _("not_specified")
            url = row["URL"]

            impact = row.get("ImpactReport", "")
            strategic = row.get("StrategicPlan", "")

            # Phone/hours are only filled in for a handful of orgs so far —
            # skip the row entirely rather than showing an empty field.
            phone = str(row.get("Phone", "")).strip()
            hours = str(row.get("Hours", "")).strip()
            phone_row = (
                f'<tr><td style="color:{TEXT_MUTED};padding:3px 10px 3px 0;font-size:12px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">{_("popup_phone")}</td>'
                f"<td>{_tel_link(phone)}</td></tr>"
                if phone
                else ""
            )
            hours_row = (
                f'<tr><td style="color:{TEXT_MUTED};padding:3px 10px 3px 0;font-size:12px;'
                f"font-weight:700;text-transform:uppercase;white-space:nowrap;"
                f'vertical-align:top;">{_("popup_hours")}</td>'
                f'<td style="color:{TEXT_MID};">{"<br>".join(_hours_lines(hours))}</td></tr>'
                if hours
                else ""
            )

            directions_url = _directions_url(row["Latitude"], row["Longitude"])
            action_html = (
                f'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;'
                f'margin-top:10px;">'
                f'<a href="{directions_url}" target="_blank" '
                f'style="display:inline-block;padding:6px 14px;'
                f"background:{BRAND_DARK};color:white;border-radius:6px;"
                f'font-size:12px;font-weight:600;text-decoration:none;">{_("get_directions")}</a>'
            )
            if url:
                action_html += (
                    f'<a href="{url}" target="_blank" '
                    f'style="display:inline-block;padding:6px 14px;'
                    f"background:{BRAND_MED};color:white;border-radius:6px;"
                    f'font-size:12px;font-weight:600;text-decoration:none;">{_("visit_website")}</a>'
                )
            else:
                action_html += f'<span style="color:{TEXT_MUTED};font-size:12px;">{_("no_website_listed")}</span>'
            action_html += "</div>"

            locs_html = _locations_html(_org_locations(row["Name"]))

            popup_html = (
                f'<div style="font-family:{FONT_STACK};width:{340 if locs_html else 310}px;'
                f'border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.12);">'
                f'<div style="background:{pin_color};padding:14px 16px;">'
                f'<div style="font-size:16px;font-weight:700;color:white;'
                f'line-height:1.3;">{row["Name"]}</div>'
                f"</div>"
                f'<div style="padding:12px 16px;background:white;">'
                f'<table style="width:100%;border-collapse:collapse;font-size:12px;'
                f'color:{TEXT_DARK};">'
                f'<tr><td style="color:{TEXT_MUTED};padding:3px 10px 3px 0;font-size:12px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">{_("popup_address")}</td>'
                f"<td>{row['Address']}, {row['City']}, {row['State']}</td></tr>"
                f"{phone_row}"
                f"{hours_row}"
                f'<tr><td style="color:{TEXT_MUTED};padding:3px 10px 3px 0;font-size:12px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">{_("popup_type")}</td>'
                f"<td>{org_type}</td></tr>"
                f'<tr><td style="color:{TEXT_MUTED};padding:3px 10px 3px 0;font-size:12px;'
                f"font-weight:700;text-transform:uppercase;white-space:nowrap;"
                f'vertical-align:top;">{_("popup_services")}</td>'
                f'<td style="color:{TEXT_MID};">{svc_tags}</td></tr>'
                f'<tr><td style="color:{TEXT_MUTED};padding:3px 10px 3px 0;font-size:12px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">{_("popup_impact")}</td>'
                f"<td>{_link_cell(impact)}</td></tr>"
                f'<tr><td style="color:{TEXT_MUTED};padding:3px 10px 3px 0;font-size:12px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">{_("popup_strategic")}</td>'
                f"<td>{_link_cell(strategic)}</td></tr>"
                f"</table>"
                f"{locs_html}"
                f"{action_html}"
                f"</div></div>"
            )

            tooltip_html = (
                f'<div style="font-family:{FONT_STACK};font-size:14px;'
                f'font-weight:700;color:{BRAND_DARK};max-width:200px;">{row["Name"]}</div>'
            )

            pin_svg = (
                f'<div style="width:25px;height:41px;">'
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 52" width="25" height="41">'
                f'<path d="M16 0C7.163 0 0 7.163 0 16c0 10 16 36 16 36S32 26 32 16C32 7.163 24.837 0 16 0z"'
                f' fill="{pin_color}" stroke="#fff" stroke-width="2"/>'
                f'<circle cx="16" cy="16" r="7" fill="white" opacity="0.85"/>'
                f"</svg></div>"
            )
            folium.Marker(
                location=[row["Latitude"], row["Longitude"]],
                popup=folium.Popup(popup_html, max_width=380 if locs_html else 340),
                tooltip=folium.Tooltip(tooltip_html),
                icon=folium.DivIcon(
                    html=pin_svg,
                    icon_size=(25, 41),
                    icon_anchor=(12, 41),
                    popup_anchor=(0, -38),
                ),
            ).add_to(m)

        folium.LayerControl(collapsed=True).add_to(m)

        st_folium(m, use_container_width=True, height=620, returned_objects=[])
        st.caption(_("map_caption").format(n=len(map_data)))


# ══════════════════════════════════════════════════════════
# TAB 2 — DIRECTORY
# ══════════════════════════════════════════════════════════
with tab_dir:
    if filtered.empty:
        st.warning(_("no_results"))
    else:
        dl_col, _dl_spacer = st.columns([2, 5])
        with dl_col:
            export_df = filtered[
                [
                    "Name",
                    "Address",
                    "City",
                    "State",
                    "Zip",
                    "Phone",
                    "Hours",
                    "URL",
                    "OrgType",
                    "ServiceArea",
                    "ImpactReport",
                    "StrategicPlan",
                ]
            ].rename(
                columns={
                    "Name": _("col_name"),
                    "City": _("col_city"),
                    "Phone": _("sec_phone"),
                    "Hours": _("sec_hours"),
                    "OrgType": _("col_orgtype"),
                    "ServiceArea": _("col_servicearea"),
                }
            )
            st.download_button(
                _("download_button").format(n=n_filtered),
                data=export_df.to_csv(index=False).encode("utf-8"),
                file_name="gwi_nonprofits_filtered.csv",
                mime="text/csv",
            )

        dir_df = (
            filtered[
                [
                    "Name",
                    "City",
                    "OrgType",
                    "Services",
                    "URL",
                    "ImpactReport",
                    "StrategicPlan",
                ]
            ]
            .rename(
                columns={
                    "Name": _("col_name"),
                    "City": _("col_city"),
                    "OrgType": _("col_orgtype"),
                    "Services": _("col_services"),
                    "ImpactReport": _("sec_impact"),
                    "StrategicPlan": _("sec_strategic"),
                }
            )
            .copy()
        )
        dir_df[_("col_orgtype")] = dir_df[_("col_orgtype")].apply(_org_type_label)
        for c in (_("sec_impact"), _("sec_strategic")):
            dir_df[c] = dir_df[c].apply(
                lambda v: (
                    ""
                    if not str(v).strip()
                    else (str(v) if str(v).startswith("http") else f"https://{v}")
                )
            )
        st.dataframe(
            dir_df,
            use_container_width=True,
            height=520,
            column_config={
                "URL": st.column_config.LinkColumn(
                    _("sec_website"), display_text=_("link_open")
                ),
                _("sec_impact"): st.column_config.LinkColumn(
                    _("sec_impact"), display_text=_("impact_open")
                ),
                _("sec_strategic"): st.column_config.LinkColumn(
                    _("sec_strategic"), display_text=_("strategic_open")
                ),
            },
            hide_index=True,
        )


# ══════════════════════════════════════════════════════════
# TAB 3 — ORGANIZATION DETAIL
# ══════════════════════════════════════════════════════════
with tab_detail:
    if filtered.empty:
        st.warning(_("no_results"))
    else:
        selected_name = st.selectbox(
            _("select_org"),
            sorted(filtered["Name"].tolist()),
            key="detail_select",
        )
        matches = filtered[filtered["Name"] == selected_name]
        if matches.empty:
            st.warning(_("org_not_found"))
        else:
            row = matches.iloc[0]
            header_color = BRAND_MED

            st.markdown(
                f'<div style="background:{BG_WHITE};border-radius:12px;'
                f"padding:22px 26px;box-shadow:0 1px 8px rgba(0,0,0,.08);"
                f'border-left:5px solid {header_color};margin-bottom:20px;">'
                f'<h2 style="color:{BRAND_DARK};margin:0;font-size:24px;">'
                f"{row['Name']}</h2>"
                f"</div>",
                unsafe_allow_html=True,
            )

            def section_label(icon, text):
                st.markdown(
                    f"<p style='font-size:12px;font-weight:700;color:{TEXT_MID};"
                    f"text-transform:uppercase;letter-spacing:.5px;margin:16px 0 4px;'>"
                    f"{icon} {text}</p>",
                    unsafe_allow_html=True,
                )

            c1, c2 = st.columns(2)

            with c1:
                section_label("📍", _("sec_location"))
                addr_parts = [row["Address"], row["City"], row["State"], row["Zip"]]
                st.write(", ".join(p for p in addr_parts if p) or _("not_available"))

                # Only collected for a handful of orgs so far — the section is
                # hidden rather than shown empty for the rest.
                detail_phone = str(row.get("Phone", "")).strip()
                if detail_phone:
                    section_label("📞", _("sec_phone"))
                    st.markdown(_tel_link(detail_phone), unsafe_allow_html=True)

                detail_hours = _hours_lines(row.get("Hours", ""))
                if detail_hours:
                    section_label("🕒", _("sec_hours"))
                    hours_html = "<br>".join(detail_hours)
                    st.markdown(
                        f"<div style='color:{TEXT_DARK};line-height:1.6;'>{hours_html}</div>",
                        unsafe_allow_html=True,
                    )

                section_label("🏢", _("sec_orgtype"))
                st.write(_org_type_label(row["OrgType"]) or _("not_specified"))

                section_label("🌐", _("sec_website"))
                url = row["URL"]
                if url and url.startswith("http"):
                    st.markdown(f"[{url}]({url})")
                elif url:
                    st.markdown(f"[https://{url}](https://{url})")
                else:
                    st.markdown(
                        f"<span style='color:{TEXT_MID};'>{_('not_listed')}</span>",
                        unsafe_allow_html=True,
                    )

                section_label("📊", _("sec_impact"))
                st.markdown(
                    _link_cell(row.get("ImpactReport", "")), unsafe_allow_html=True
                )

                section_label("🧭", _("sec_strategic"))
                st.markdown(
                    _link_cell(row.get("StrategicPlan", "")), unsafe_allow_html=True
                )

            with c2:
                section_label("🛠️", _("sec_services"))
                detail_svcs = _smart_split(row.get("Services", ""))
                if detail_svcs:
                    st.markdown(
                        " ".join(
                            f'<span class="svc-chip">{s}</span>' for s in detail_svcs
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    st.write(_("not_specified"))

                detail_locs = _org_locations(row["Name"])
                if detail_locs:
                    section_label("📍", _("sec_locations"))
                    if any(loc.get("Kind") == "admin" for loc in detail_locs):
                        st.info(_("loc_admin_note"))
                    for loc in detail_locs:
                        addr = ", ".join(
                            p
                            for p in (
                                loc.get(k, "").strip()
                                for k in ("Address", "City", "State", "Zip")
                            )
                            if p
                        )
                        title = loc.get("LocationName", "").strip()
                        if loc.get("Kind") == "admin":
                            title += f" — {_('loc_admin_badge')}"
                        with st.expander(title, expanded=len(detail_locs) <= 6):
                            if loc.get("ServicesHere", "").strip():
                                st.markdown(f"**{loc['ServicesHere'].strip()}**")
                            if addr:
                                st.markdown(
                                    f"{addr}  \n"
                                    f"[{_('loc_directions')} →]"
                                    f"({_directions_url_address(loc)})"
                                )
                            else:
                                st.caption(_("loc_no_address"))
                            if loc.get("Phone", "").strip():
                                st.markdown(
                                    _tel_link(loc["Phone"].strip()),
                                    unsafe_allow_html=True,
                                )
                            if loc.get("Hours", "").strip():
                                st.markdown(
                                    "  \n".join(_hours_lines(loc["Hours"]))
                                )

            if pd.notna(row["Latitude"]) and pd.notna(row["Longitude"]):
                section_label("🗺️", _("sec_map"))
                mini = folium.Map(
                    location=[row["Latitude"], row["Longitude"]],
                    zoom_start=15,
                    tiles=None,
                )
                _add_basemap(mini)
                folium.Marker(
                    location=[row["Latitude"], row["Longitude"]],
                    tooltip=row["Name"],
                    icon=folium.Icon(color="blue", icon="info-sign"),
                ).add_to(mini)
                st_folium(
                    mini, use_container_width=True, height=300, returned_objects=[]
                )
            else:
                st.info(_("no_map_coords"))
