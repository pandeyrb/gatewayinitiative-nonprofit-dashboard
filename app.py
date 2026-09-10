"""
GWI Nonprofit Partner Explorer  —  Streamlit app
Run:  streamlit run app.py
"""

import os
import re

import folium
import pandas as pd
import requests
import streamlit as st

from streamlit_folium import st_folium

from search_utils import matches as _search_matches

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
    OpenStreetMap stays the default (`show=True`); the rest are opt-in.
    """
    folium.TileLayer(
        tiles="OpenStreetMap",
        name=_("basemap_osm"),
        show=True,
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
        show=False,
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
    page_title="Lawrence Community Resource Finder",
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

# ── i18n ──────────────────────────────────────────────────────────────────────
T = {
    "en": {
        "filters_heading": "🔍 Filters",
        "orgs_total": "{n} organizations total",
        "search_label": "Search",
        "search_placeholder": "Name, city, or service…",
        "services_label": "Services",
        "services_placeholder": "Type to search services…",
        "orgtype_label": "Organization Type",
        "orgtype_all": "All",
        "reset_button": "↺  Reset all filters",
        "page_heading": "Lawrence Community Resource Finder",
        "showing_all": "Showing all {n} organizations",
        "showing_filtered": "Showing {n} of {total} organizations",
        "nav_cta": "Ask AI assistant",
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
    },
    "es": {
        "filters_heading": "🔍 Filtros",
        "orgs_total": "{n} organizaciones en total",
        "search_label": "Buscar",
        "search_placeholder": "Nombre, ciudad o servicio…",
        "services_label": "Servicios",
        "services_placeholder": "Escriba para buscar servicios…",
        "orgtype_label": "Tipo de Organización",
        "orgtype_all": "Todos",
        "reset_button": "↺  Restablecer filtros",
        "page_heading": "Buscador de Recursos Comunitarios de Lawrence",
        "showing_all": "Mostrando las {n} organizaciones",
        "showing_filtered": "Mostrando {n} de {total} organizaciones",
        "nav_cta": "Preguntar al asistente de IA",
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

    df["SvcList"] = df["ServiceArea"].apply(_smart_split)
    if "Services" not in df.columns:
        df["Services"] = ""
    df["SvcTagList"] = df["Services"].apply(_smart_split)

    df = df.rename(
        columns={"Impact Report": "ImpactReport", "Strategic Plan": "StrategicPlan"}
    )

    for col in ("ImpactReport", "StrategicPlan", "Phone", "Hours"):
        if col not in df.columns:
            df[col] = ""

    return df


df = load_data(CSV_PATH)

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


def _tel_link(phone: str) -> str:
    """Render a phone number as a tap-to-call link (works on mobile)."""
    digits = re.sub(r"[^\d+]", "", str(phone))
    return (
        f'<a href="tel:{digits}" '
        f'style="color:{BRAND_MED};text-decoration:none;font-weight:600;">{phone}</a>'
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

    all_services = sorted({s for lst in df["SvcTagList"] for s in lst})
    sel_svcs = st.multiselect(
        _("services_label"),
        all_services,
        key="sel_svcs",
        placeholder=_("services_placeholder"),
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
        st.session_state["sel_svcs"] = []
        st.session_state["sel_org_type_label"] = _("orgtype_all")

    st.divider()
    st.button(_("reset_button"), use_container_width=True, on_click=_reset_filters)


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

if sel_svcs:
    filtered = filtered[
        filtered["SvcTagList"].apply(lambda lst: any(s in lst for s in sel_svcs))
    ]

if sel_org_type != "All":
    filtered = filtered[filtered["OrgType"] == sel_org_type]

n_filtered = len(filtered)
n_total = len(df)

# active filter pills
active_filters: list[str] = []
if search:
    active_filters.append(f'"{search}"')
if sel_svcs:
    active_filters.extend(sel_svcs)
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
    st.markdown(
        f'<div style="display:flex;justify-content:flex-end;">'
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

            popup_html = (
                f'<div style="font-family:{FONT_STACK};width:310px;'
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
                popup=folium.Popup(popup_html, max_width=340),
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
