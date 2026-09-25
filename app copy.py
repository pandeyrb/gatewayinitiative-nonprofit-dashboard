"""
GWI Nonprofit Partner Explorer  —  Streamlit app
Run:  streamlit run app.py
"""

import json
import os
import re

from datetime import datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import folium
import pandas as pd
import requests
import streamlit as st

from streamlit_folium import st_folium

from hours import (
    Schedule,
    format_range,
    open_status,
    parse_hours,
    weekly_grid,
)
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


# Hours rows and the open/closed badge are rendered in two different documents:
# inside the Leaflet popup (its own iframe) and in the organisation-detail tab
# (the Streamlit page). The rules therefore live on their own and are injected
# into both, rather than only into the map.
_HOURS_CSS = """
.gwi-row{display:flex;gap:9px;margin:6px 0;font-size:12px}
.gwi-lbl{flex:0 0 54px;color:__TEXT_MUTED__;font-size:11px;font-weight:700;
  text-transform:uppercase;letter-spacing:.3px;padding-top:1px}
.gwi-val{flex:1 1 auto;min-width:0;overflow-wrap:anywhere}
.gwi-muted{color:__TEXT_MUTED__}
.gwi-mid{color:__TEXT_MID__}

/* Day/time pairs. Fixed-width day column so the times line up in a column. */
.gwi-h{display:flex;gap:8px;margin:1px 0}
.gwi-hd-day{flex:0 0 62px;color:__TEXT_MUTED__;white-space:nowrap}
.gwi-hd-val{flex:1 1 auto}
.gwi-svc{font-weight:600;color:__BRAND_DARK__;margin-top:4px}

.gwi-badge{display:inline-flex;align-items:center;gap:5px;border-radius:999px;
  padding:3px 9px;font-size:11px;font-weight:700;margin:1px 0 5px}
.gwi-dot{font-size:8px;line-height:1}
.gwi-badge span.gwi-tail{font-weight:500;opacity:.88}

.gwi-fold{margin:2px 0}
.gwi-fold>summary,.gwi-pop summary{cursor:pointer;color:__BRAND_MED__;
  font-weight:600;font-size:12px;list-style:none;outline:none}
.gwi-fold>summary::-webkit-details-marker,
.gwi-pop summary::-webkit-details-marker{display:none}
.gwi-fold>summary::marker,.gwi-pop summary::marker{content:""}
.gwi-fold>summary:before,.gwi-pop summary:before{content:"\\25B8  ";font-size:10px}
.gwi-fold[open]>summary:before,
.gwi-pop details[open]>summary:before{content:"\\25BE  "}
.gwi-fold>div,.gwi-pop details>div{padding:3px 0 2px 11px}
"""


def _hours_css() -> str:
    return (
        _HOURS_CSS.replace("__BRAND_MED__", BRAND_MED)
        .replace("__BRAND_DARK__", BRAND_DARK)
        .replace("__TEXT_MID__", TEXT_MID)
        .replace("__TEXT_MUTED__", TEXT_MUTED)
    )


# Popup chrome lives in a stylesheet rather than inline styles on every row.
# Two reasons: a Leaflet popup has no height of its own, so a long entry grew
# until it overflowed the map and the wheel fell through to the page instead of
# scrolling the popup; and 65 popups' worth of repeated inline style strings was
# a large slice of the rendered HTML. Tokens are substituted below.
_POPUP_CSS = """
<style>
/* Let our own container own the padding, the corners and the width. */
.leaflet-popup-content-wrapper{padding:0;border-radius:10px;overflow:hidden;
  box-shadow:0 3px 16px rgba(0,0,0,.20)}
.leaflet-popup-content{margin:0;width:auto!important;line-height:1.45}

.gwi-pop{font-family:__FONT__;width:322px;max-width:84vw;display:flex;
  flex-direction:column;max-height:min(392px,72vh);color:__TEXT_DARK__}
.gwi-pop-hd{flex:0 0 auto;background:__BRAND_MED__;color:#fff;font-size:15px;
  font-weight:700;line-height:1.3;padding:11px 14px}
/* The only scrolling region. overscroll-behavior stops the page from taking
   over the wheel once this reaches its end. */
/* min-height:0 is load-bearing: a column flex item defaults to min-height:auto,
   which refuses to shrink below its content, so overflow-y would never engage
   and the popup would grow past max-height exactly as before. */
.gwi-pop-bd{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;
  -webkit-overflow-scrolling:touch;scrollbar-width:thin;
  padding:9px 14px 6px;background:#fff}
.gwi-pop-bd::-webkit-scrollbar{width:9px}
.gwi-pop-bd::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:5px;
  border:2px solid #fff}
/* Directions/website stay put, so they never need scrolling to reach. */
.gwi-pop-ft{flex:0 0 auto;display:flex;gap:6px;flex-wrap:wrap;align-items:center;
  padding:8px 14px 10px;background:#fff;border-top:1px solid __BORDER__}


.gwi-btn{display:inline-block;padding:6px 12px;border-radius:6px;font-size:12px;
  font-weight:600;text-decoration:none;color:#fff!important}

/* Locations that appear when an organisation is opened. Only opacity is
   animated: Leaflet positions every marker with an inline `transform`, so
   animating transform here would fight the positioning. */
.gwi-pin-in{animation:gwiFade .22s ease-out}
@keyframes gwiFade{from{opacity:0}to{opacity:1}}

/* A site card is the same component as an org popup, just narrower. */
.gwi-pop--site{width:256px;max-height:min(330px,68vh)}
.gwi-pop-sub{font-size:11px;font-weight:500;opacity:.8;margin-top:1px}

/* Focus mode: opening an org that runs several sites fades every other org out,
   so its own cluster is the only thing competing for attention. Closing the
   popup brings them back.
   Only opacity is animated, on purpose — Leaflet positions each marker with an
   inline `transform`, so a transform here would be ignored or fight it. */
/* Caption that names what is being focused. Pins appearing while others fade
   is easy to miss, so the map says what it just did. */
.gwi-focusnote{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);
  z-index:650;pointer-events:none;max-width:82%;
  background:rgba(255,255,255,.96);color:__TEXT_DARK__;
  border:1px solid __BORDER__;border-radius:999px;
  padding:6px 14px;font:600 12px/1.35 __FONT__;text-align:center;
  box-shadow:0 2px 10px rgba(0,0,0,.14);
  opacity:0;transition:opacity .2s ease}
.gwi-focusnote.is-on{opacity:1}

.leaflet-marker-icon{transition:opacity .2s ease}
.gwi-dim{opacity:0!important;pointer-events:none!important}
.gwi-focus{filter:drop-shadow(0 0 7px rgba(45,108,180,.55))}

</style>
"""


def _popup_css() -> str:
    return (
        _POPUP_CSS.replace("</style>", _hours_css() + "</style>")
        .replace("__FONT__", FONT_STACK)
        .replace("__BRAND_MED__", BRAND_MED)
        .replace("__BRAND_DARK__", BRAND_DARK)
        .replace("__TEXT_DARK__", TEXT_DARK)
        .replace("__TEXT_MID__", TEXT_MID)
        .replace("__TEXT_MUTED__", TEXT_MUTED)
        .replace("__BORDER__", BORDER)
    )


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

# Every opening time in this dataset is local to Lawrence, MA.
LAWRENCE_TZ = ZoneInfo("America/New_York")

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
        "boundary_unavailable": "City boundary could not be loaded, so the dashed Lawrence outline is not shown.",
        "popup_address": "Address",
        "popup_phone": "Phone",
        "popup_hours": "Hours",
        "status_open": "OPEN NOW",
        "status_closed": "CLOSED",
        "status_until": "until {t}",
        "status_opens": "opens {t}",
        "status_unknown": "Hours not published — call ahead",
        "hours_source": "Hours checked {d}",
        "hours_source_link": "source",
        "filter_open_now": "Open now only",
        "filter_open_now_help": "Show only organizations open at this moment. Organizations that publish no hours are hidden.",
        "open_now_col": "Open now",
        "status_asof": "Open/closed shown for {t} (Lawrence time)",
        "days_short": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
        "hours_closed_word": "closed",
        "hours_24": "24 hours",
        "hours_today": "Today",
        "hours_full_week": "Full week",
        "hours_other_days": "Other days",
        "loc_on_map": "also shown on the map",
        "focus_banner": "{n} locations shown — other organizations hidden",
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
        "boundary_unavailable": "No se pudo cargar el límite municipal, por lo que no se muestra el contorno discontinuo de Lawrence.",
        "popup_address": "Dirección",
        "popup_phone": "Teléfono",
        "popup_hours": "Horario",
        "status_open": "ABIERTO AHORA",
        "status_closed": "CERRADO",
        "status_until": "hasta las {t}",
        "status_opens": "abre {t}",
        "status_unknown": "Horario no publicado — llame antes de ir",
        "hours_source": "Horario verificado el {d}",
        "hours_source_link": "fuente",
        "filter_open_now": "Solo abiertos ahora",
        "filter_open_now_help": "Muestra solo las organizaciones abiertas en este momento. Las que no publican horario quedan ocultas.",
        "open_now_col": "Abierto ahora",
        "status_asof": "Abierto/cerrado según las {t} (hora de Lawrence)",
        "days_short": "Lun,Mar,Mié,Jue,Vie,Sáb,Dom",
        "hours_closed_word": "cerrado",
        "hours_24": "24 horas",
        "hours_today": "Hoy",
        "hours_full_week": "Semana completa",
        "hours_other_days": "Otros días",
        "loc_on_map": "también en el mapa",
        "focus_banner": "{n} ubicaciones mostradas — otras organizaciones ocultas",
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

# Same rules the map popups use. The organisation-detail tab renders the same
# badge and hours markup, but it lives in the Streamlit document rather than in
# the map's iframe, so it needs its own copy of the stylesheet.
st.markdown(f"<style>{_hours_css()}</style>", unsafe_allow_html=True)


# ── Lawrence, MA boundary ─────────────────────────────────────────────────────
BOUNDARY_PATH = "data/lawrence_boundary.geojson"


def _fetch_boundary_from_nominatim() -> dict | None:
    """Last-resort live lookup, used only when the vendored file is missing.

    Deliberately NOT cached. st.cache_data would memoise a None from a single
    transient failure for the whole TTL, which is exactly how the boundary used
    to disappear for a day at a time.
    """
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


@st.cache_data(ttl=86400)
def load_lawrence_boundary(path: str) -> dict | None:
    """Lawrence's city limits, read from the repo rather than fetched.

    The polygon used to be pulled from Nominatim on every cold start. That is
    fragile in two ways that both fail silently: Nominatim answers 403 to
    datacenter IPs (so a deployed app never gets it at all), and any failure
    was cached as None for the full 24h TTL. The boundary is a municipal
    outline that changes on the order of never, so it lives in the repo now
    and the network is only touched if that file has gone missing.
    """
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return _fetch_boundary_from_nominatim()


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


# Floor/suite/building qualifiers, which distinguish a programme but not a
# destination. "305 Essex Street, 2nd Floor" and "305 Essex Street, 4th Floor"
# are one place to travel to — and one point on the map.
_ADDR_QUALIFIER_RE = re.compile(
    r",?\s*(?:"
    r"\([^)]*\)"
    r"|#\S+"
    r"|\b(?:suite|ste\.?|unit|apt\.?|rm\.?|room|bldg\.?|building)\b[^,]*"
    r"|\b\d+(?:st|nd|rd|th)\s+floor\b[^,]*"
    r"|\bfloor\b[^,]*"
    r")",
    re.I,
)


def _street_of(address: str) -> str:
    """The travel-to part of an address, with programme qualifiers removed."""
    out = _ADDR_QUALIFIER_RE.sub("", str(address or ""))
    return re.sub(r"\s*,\s*$", "", out).strip(" ,")


def _group_by_address(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Collapse an org's rows into one entry per physical building.

    Big providers list a separate row per program at the same street address —
    a health center's clinic and its pharmacy, or a dozen agency programs on
    three floors of one building. Rendering each as its own card is what made
    the popup unreadable, and once these sites are drawn it would also stack a
    column of numbered pins on one point. Keying on the street address with the
    floor/suite stripped gives one card, and one pin, per place you would
    actually travel to, with its programs listed inside.

    Insertion-ordered, so the public-first sort from _org_locations survives.
    """
    groups: dict[str, list[dict]] = {}
    for loc in rows:
        key = " ".join(
            x
            for x in (
                _street_of(loc.get("Address", "")).lower(),
                loc.get("City", "").strip().lower(),
                loc.get("Zip", "").strip(),
            )
            if x
        ).strip()
        # Rows with no address (a hotline, a shelter with an unlisted site)
        # each stand alone rather than collapsing into one empty-key group.
        groups.setdefault(key or f"__noaddr_{id(loc)}", []).append(loc)
    return list(groups.items())


def _pin_svg(color: str, fade: bool = False) -> str:
    """The teardrop map pin.

    Shared by an organisation's own marker and by its other locations on
    purpose: one marker design for the whole map. An earlier version drew the
    extra locations as small dots, which read as a different kind of thing
    entirely and made a focused organisation look cluttered rather than like
    one organisation in several places.
    """
    return (
        f'<div style="width:25px;height:41px;"'
        f'{" class=\"gwi-pin-in\"" if fade else ""}>'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 52" '
        f'width="25" height="41">'
        f'<path d="M16 0C7.163 0 0 7.163 0 16c0 10 16 36 16 36S32 26 32 16'
        f'C32 7.163 24.837 0 16 0z" fill="{color}" stroke="#fff" '
        f'stroke-width="2"/>'
        f'<circle cx="16" cy="16" r="7" fill="white" opacity="0.85"/>'
        f"</svg></div>"
    )


def _site_coords(loc: dict) -> tuple[float, float] | None:
    try:
        return (float(loc.get("Latitude", "")), float(loc.get("Longitude", "")))
    except (TypeError, ValueError):
        return None


def _locations_html(groups: list[tuple[str, list[dict]]], n_plotted: int = 0) -> str:
    """Render an org's sites as a folded block for the map popup.

    One card per building (see _group_by_address). The sites are also drawn on
    the map while this popup is open, so the list says so rather than leaving
    the dots to explain themselves.
    """
    if not groups:
        return ""

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

        # Card title: the shared site name when the programmes differ only by
        # suffix ("Main Site — Clinic"/"— Pharmacy"). Where they share nothing,
        # as with a dozen unrelated agency programmes at one address, the
        # street is the honest label — the first programme's name would imply
        # the card only covers that one.
        title = head.get("LocationName", "").strip()
        if len(locs) > 1:
            stems = {
                loc.get("LocationName", "").split("—")[0].strip() for loc in locs
            }
            title = (
                stems.pop()
                if len(stems) == 1
                else (_street_of(head.get("Address", "")) or title)
            )

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
                # A building with several programmes (a clinic and its
                # pharmacy, say) keeps its hours as one compact line — a full
                # grid per programme would make these popups enormous — but
                # still gets the badge, so you can see which counter is open.
                badge = (
                    _status_badge_html(_schedules(loc.get("Hours", "")), compact=True)
                    if loc.get("Hours", "").strip()
                    else ""
                )
                lines.append(
                    f'<div style="margin-top:4px;">'
                    f'<span style="font-weight:600;color:{TEXT_DARK};">{label}</span>'
                    + (
                        f'<span style="color:{TEXT_MUTED};"> — {svc}</span>'
                        if svc and svc.lower() != label.lower()
                        else ""
                    )
                    + badge
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
                prog_html += _status_badge_html(
                    _schedules(head["Hours"]), compact=True
                ) + _hours_html(head["Hours"])

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
        if any(
            loc.get("Kind", "") == "admin" for _k, locs in groups for loc in locs
        )
        else ""
    )

    # Folded away, and with no scroll box of its own: the popup body is now the
    # single scrolling region, and a scroll area nested inside another one is
    # exactly the thing that made these popups awkward to read.
    hint = f" — {_('loc_on_map')}" if n_plotted else ""
    return (
        f'<div class="gwi-row"><div class="gwi-lbl"></div><div class="gwi-val">'
        f"<details><summary>{_('popup_locations')} ({len(groups)}){hint}</summary>"
        f'<div style="font-size:12px;">{note}{"".join(cards)}</div>'
        f"</details></div></div>"
    )


def _site_toggle_js(m: folium.Map, registry: list, all_markers: list) -> str:
    """Focus one organisation on click; restore the whole map on close.

    Opening an org that runs several locations does two things: its other
    locations appear, and every other organisation fades out. Six host churches
    are impossible to read against 64 unrelated pins, and fading the rest is
    cheaper to understand than any legend.

    Returns raw JavaScript, not a <script> element: this goes into folium's
    figure.script, which already wraps every child in one <script> block, and a
    nested tag there is a syntax error that kills the map silently.

    Startup is a poll rather than a load listener. streamlit-folium injects the
    map with innerHTML and re-runs the scripts by hand, so the host page's load
    event has already fired by the time this exists — listening for it meant
    this code never ran. Polling also removes the ordering problem: folium
    renders this element *before* the layers it references, because the map only
    appends its own JS during render.
    """
    entries, hooks = [], []
    for i, (marker, layer, bounds, org_name) in enumerate(registry):
        key = f"s{i}"
        pts = ",".join(f"[{lat},{lon}]" for lat, lon in bounds)
        entries.append(
            f'"{key}":{{g:{layer.get_name()},b:[{pts}],m:{marker.get_name()},'
            f"n:{len(bounds) - 1},t:{json.dumps(org_name)}}}"
        )
        hooks.append(f'{marker.get_name()}._gwiOrg="{key}";')
        hooks.append(
            f"{layer.get_name()}.eachLayer(function(l){{l._gwiSat=\"{key}\";}});"
        )

    pins = ",".join(mk.get_name() for mk in all_markers)
    banner_label = _("focus_banner")

    return (
        "(function(){\n"
        "var tries=0;\n"
        "function init(){\n"
        f"var map={m.get_name()};\n"
        "var SITES={" + ",".join(entries) + "};\n"
        "var PINS=[" + pins + "];\n"
        + "\n".join(hooks)
        + """
// Start with every location layer off. Done here rather than through folium's
// show=False so the layers definitely exist as JS objects we can toggle.
Object.keys(SITES).forEach(function (k) {
  if (map.hasLayer(SITES[k].g)) map.removeLayer(SITES[k].g);
});

var current = null;

// A small caption so the change is legible: pins quietly appearing while others
// vanish is easy to miss, especially for an org whose sites span three towns.
var note = document.createElement('div');
note.className = 'gwi-focusnote';
map.getContainer().appendChild(note);

function paint(focusMarker) {
  PINS.forEach(function (pin) {
    if (!pin._icon) return;
    pin._icon.classList.toggle('gwi-dim', !!focusMarker && pin !== focusMarker);
    pin._icon.classList.toggle('gwi-focus', pin === focusMarker);
  });
}

function restore() {
  if (current && map.hasLayer(current.g)) map.removeLayer(current.g);
  current = null;
  paint(null);
  note.classList.remove('is-on');
}

function focus(key) {
  var s = SITES[key];
  if (!s) { restore(); return; }
  if (current !== s) {
    if (current && map.hasLayer(current.g)) map.removeLayer(current.g);
    s.g.addTo(map);
    current = s;
  }
  paint(s.m);
  note.textContent = s.t
    ? s.t + ' \u00b7 ' + LABEL.replace('{n}', s.n)
    : LABEL.replace('{n}', s.n);
  note.classList.add('is-on');
  if (!s.b.length) return;
  var b = L.latLngBounds(s.b);
  // Only move the map when something would otherwise be off-screen — a click
  // that reframes the view for no reason is disorienting. The generous top
  // padding leaves room for the popup, which opens above the pin.
  if (!map.getBounds().pad(-0.06).contains(b)) {
    setTimeout(function () {
      map.fitBounds(b, {
        paddingTopLeft: [44, 170],
        paddingBottomRight: [44, 64],
        maxZoom: 15,
        animate: true,
      });
    }, 70);
  }
}

var openCount = 0;

map.on('popupopen', function (e) {
  openCount++;
  var src = e.popup._source;
  if (!src) return;
  if (src._gwiOrg) focus(src._gwiOrg);
  else if (src._gwiSat) focus(src._gwiSat);   // one of the focused org's sites
  else restore();                             // an org with no other locations
});

// Clicking a location closes the org popup and opens its own, so the focus has
// to survive that handover — restore only once nothing is left open.
// Counted rather than read off the DOM: Leaflet fades a popup out before
// removing the element, so checking for .leaflet-popup races the animation and
// the map would stay stuck in focus mode. The next-tick defer lets the
// close/open pair from that handover settle before we decide.
map.on('popupclose', function () {
  openCount = Math.max(0, openCount - 1);
  setTimeout(function () {
    if (openCount === 0) restore();
  }, 0);
});
}
"""
        + f'var LABEL={banner_label!r};\n'.replace("'", '"')
        + """
// Poll until folium has defined the map and the layers. init() builds its
// references first, so a premature call throws before attaching anything and
// is safe to retry.
(function wait() {
  try {
    init();
    return;
  } catch (e) {}
  if (++tries > 200) return;   // ~10s, then give up quietly
  setTimeout(wait, 50);
})();
})();"""
    )


def _site_layer(
    m: folium.Map,
    org_row,
    groups: list[tuple[str, list[dict]]],
) -> tuple[folium.FeatureGroup | None, list[list[float]]]:
    """A hidden layer of an org's other locations, shown when its pin is opened.

    These were listed in the popup but never drawn, so a food pantry operating
    out of six host churches showed as one pin on an office. Drawing all of them
    all the time is the opposite failure — they would bury the other 57 orgs —
    so each org's locations sit in their own layer that stays off until asked
    for, and every other org is faded out while it is on.

    The locations use the same teardrop pin as the organisation itself: they are
    the same kind of thing, and a second marker style made the map look busy.

    Returns the layer and the points it covers (the org's own pin included) so
    the caller can frame them.
    """
    # The org's own pin already stands for its own address, so that site would
    # otherwise be drawn twice. Compared by street address rather than by
    # distance: Lazarus House's soup kitchen is a genuinely separate place 40 m
    # from its office, and a distance test would wrongly swallow it.
    own = (
        _street_of(org_row["Address"]).lower(),
        str(org_row["City"]).strip().lower(),
    )

    plotted = []
    for g in groups:
        head = g[1][0]
        coords = _site_coords(head)
        if not coords:
            continue
        if (
            _street_of(head.get("Address", "")).lower(),
            head.get("City", "").strip().lower(),
        ) == own:
            continue
        plotted.append((g, coords))
    if not plotted:
        return None, []

    layer = folium.FeatureGroup(name=f"sites::{org_row['Name']}", control=False)
    try:
        origin = [float(org_row["Latitude"]), float(org_row["Longitude"])]
    except (TypeError, ValueError):
        origin = None
    bounds: list[list[float]] = [origin] if origin else []

    for (_key, locs), (lat, lon) in plotted:
        head = locs[0]
        bounds.append([lat, lon])

        addr = ", ".join(
            x
            for x in (head.get(k, "").strip() for k in ("Address", "City", "State", "Zip"))
            if x
        )
        svc = head.get("ServicesHere", "").strip()
        site_hours = head.get("Hours", "").strip()
        site_phone = head.get("Phone", "").strip()

        # Same three-part card as an org popup — fixed header, scrolling body,
        # pinned action — so a site never opens as an unpadded, clipped box.
        body = (
            f"{_status_badge_html(_schedules(site_hours))}{_hours_today_html(site_hours)}"
            if site_hours
            else f'<div class="gwi-muted" style="font-size:11px;font-style:italic;'
            f'margin:1px 0 5px;">{_("status_unknown")}</div>'
        )
        if svc:
            body += (
                f'<div class="gwi-row"><div class="gwi-lbl">{_("popup_services")}</div>'
                f'<div class="gwi-val gwi-mid">{svc}</div></div>'
            )
        if addr:
            body += (
                f'<div class="gwi-row"><div class="gwi-lbl">{_("popup_address")}</div>'
                f'<div class="gwi-val">{addr}</div></div>'
            )
        else:
            body += (
                f'<div class="gwi-row"><div class="gwi-lbl"></div>'
                f'<div class="gwi-val gwi-muted" style="font-style:italic;">'
                f'{_("loc_no_address")}</div></div>'
            )
        if site_phone:
            body += (
                f'<div class="gwi-row"><div class="gwi-lbl">{_("popup_phone")}</div>'
                f'<div class="gwi-val">{_tel_link(site_phone)}</div></div>'
            )

        foot = (
            f'<a class="gwi-btn" style="background:{BRAND_DARK};" '
            f'href="{_directions_url_address(head)}" target="_blank">'
            f'{_("get_directions")}</a>'
            if addr
            else f'<span class="gwi-muted" style="font-size:11px;">'
            f'{_("loc_no_address")}</span>'
        )

        # The parent org is named in the header: a lone dot gives no clue whose
        # site it is once the org's own popup has closed.
        site_html = (
            f'<div class="gwi-pop gwi-pop--site">'
            f'<div class="gwi-pop-hd">{head.get("LocationName", "").strip()}'
            f'<div class="gwi-pop-sub">{org_row["Name"]}</div></div>'
            f'<div class="gwi-pop-bd">{body}</div>'
            f'<div class="gwi-pop-ft">{foot}</div>'
            f"</div>"
        )

        folium.Marker(
            location=[lat, lon],
            tooltip=folium.Tooltip(
                f'<div style="font-family:{FONT_STACK};font-size:12px;'
                f'font-weight:700;color:{BRAND_DARK};max-width:190px;">'
                f'{head.get("LocationName", "").strip()}</div>'
                + (
                    f'<div style="font-family:{FONT_STACK};font-size:11px;'
                    f'color:{TEXT_MUTED};max-width:190px;">{svc}</div>'
                    if svc
                    else ""
                )
            ),
            popup=folium.Popup(site_html, max_width=280),
            icon=folium.DivIcon(
                html=_pin_svg(BRAND_MED, fade=True),
                icon_size=(25, 41),
                icon_anchor=(12, 41),
                popup_anchor=(0, -38),
            ),
        ).add_to(layer)

    layer.add_to(m)
    return layer, bounds


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


# ── opening hours ─────────────────────────────────────────────────────────────
OPEN_GREEN = "#15803d"
OPEN_GREEN_BG = "#dcfce7"
CLOSED_RED = "#b91c1c"
CLOSED_RED_BG = "#fee2e2"


def _now_local() -> datetime:
    """Right now in Lawrence.

    Explicitly zoned: this app is normally served from a container running UTC,
    and a naive now() would put every open/closed badge four or five hours out.
    """
    return datetime.now(LAWRENCE_TZ)


def _day_names() -> list[str]:
    return _("days_short").split(",")


def _schedules(hours_text: str) -> list[Schedule]:
    return parse_hours(str(hours_text or ""))


def _status_badge_html(scheds: list[Schedule], compact: bool = False) -> str:
    """The OPEN NOW / CLOSED pill.

    Orgs that publish nothing get a "call ahead" line rather than a grey
    "unknown" badge — the honest reading of a missing value here is that we do
    not know, and telling someone to phone first is more useful than a shrug.
    """
    state, detail, label = open_status(scheds, _now_local(), _day_names())
    if state == "unknown":
        return (
            f'<div class="gwi-muted" style="font-size:11px;font-style:italic;'
            f'margin:1px 0 5px;">{_("status_unknown")}</div>'
        )

    is_open = state == "open"
    fg, bg = (OPEN_GREEN, OPEN_GREEN_BG) if is_open else (CLOSED_RED, CLOSED_RED_BG)
    word = _("status_open") if is_open else _("status_closed")

    tail = ""
    if detail:
        tail = _("status_until").format(t=detail) if is_open else _("status_opens").format(t=detail)
    # Name the service when several share one pin, so "OPEN NOW" says what is open.
    if label and len([x for x in scheds if x.has_structure]) > 1:
        tail = f"{label} · {tail}" if tail else label

    return (
        f'<span class="gwi-badge" style="background:{bg};color:{fg};">'
        f'<span class="gwi-dot">●</span><span>{word}</span>'
        + (f'<span class="gwi-tail">· {tail}</span>' if tail else "")
        + "</span>"
    )


def _grid_rows_html(sched: Schedule) -> str:
    rows = weekly_grid(sched, _day_names())
    if not rows:
        return ""
    out = []
    for day, value in rows:
        shown = value
        if value == "closed":
            shown = f'<span class="gwi-muted">{_("hours_closed_word")}</span>'
        elif value == "24 hours":
            shown = _("hours_24")
        out.append(
            f'<div class="gwi-h"><div class="gwi-hd-day">{day}</div>'
            f'<div class="gwi-hd-val">{shown}</div></div>'
        )
    return "".join(out)


def _hours_today_html(hours_text: str) -> str:
    """Hours led by today, with the rest of the week folded away.

    The question someone actually has in front of a map is "can I go now?", and
    the old popup answered it by printing all seven days of every programme —
    which is what made these popups taller than the map. Today's line answers it
    directly; the full week is one click behind it for anyone planning ahead.
    """
    scheds = _schedules(hours_text)
    if not scheds:
        return ""

    names = _day_names()
    today = _now_local().weekday()
    structured = [x for x in scheds if x.has_structure]
    multi = len(structured) > 1

    today_lines = []
    for sched in structured:
        if sched.always_open:
            value = _("hours_24")
        else:
            todays = [i for i in sched.intervals if i.day == today]
            if todays:
                value = ", ".join(format_range(i.start, i.end) for i in todays)
            else:
                value = f'<span class="gwi-muted">{_("hours_closed_word")}</span>'
        left = sched.label if multi and sched.label else names[today]
        today_lines.append(
            f'<div class="gwi-h"><div class="gwi-hd-day">{left}</div>'
            f'<div class="gwi-hd-val">{value}</div></div>'
        )

    head = ""
    if today_lines:
        caption = (
            f'<div class="gwi-muted" style="font-size:11px;">'
            f'{_("hours_today")} · {names[today]}</div>'
            if multi
            else ""
        )
        head = caption + "".join(today_lines)

    rest = _hours_html(hours_text)
    if rest:
        summary = _("hours_full_week") if today_lines else _("hours_other_days")
        head += f"<details><summary>{summary}</summary><div>{rest}</div></details>"
    return head


def _hours_html(hours_text: str) -> str:
    """Hours as a compact week grid, one block per named service.

    A single unlabelled schedule renders as a bare grid. Several services get
    <details> blocks — plain HTML, which Leaflet popups render, so a pantry with
    three programmes collapses to three lines instead of filling the popup.
    """
    scheds = _schedules(hours_text)
    if not scheds:
        return ""

    blocks = []
    for sched in scheds:
        body = _grid_rows_html(sched)
        extras = sched.unparsed + [f"({n})" for n in sched.notes]
        if extras:
            body += (
                '<div class="gwi-muted" style="font-size:11px;margin-top:2px;">'
                + "<br>".join(extras)
                + "</div>"
            )
        if not body:
            continue

        head = f'<div class="gwi-svc">{sched.label}</div>' if sched.label else ""
        blocks.append(head + body)

    return "".join(blocks)


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

    open_now_only = st.checkbox(
        _("filter_open_now"), key="open_now", help=_("filter_open_now_help")
    )

    def _reset_filters():
        st.session_state["search"] = ""
        st.session_state["sel_cats"] = []
        st.session_state["sel_svcs"] = []
        st.session_state["sel_org_type_label"] = _("orgtype_all")
        st.session_state["open_now"] = False

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

if open_now_only:
    # Orgs that publish no hours are excluded rather than assumed open — this
    # filter is used to decide whether to travel somewhere now.
    _now = _now_local()
    filtered = filtered[
        filtered["Hours"].apply(
            lambda h: open_status(parse_hours(str(h)), _now)[0] == "open"
        )
    ]

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
if open_now_only:
    active_filters.append(_("filter_open_now"))


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
        m.get_root().header.add_child(folium.Element(_popup_css()))

        lawrence_geojson = load_lawrence_boundary(BOUNDARY_PATH)
        if lawrence_geojson:
            # Drawn as two stacked lines inside one FeatureGroup: a solid white
            # casing underneath and the navy dashes on top. Esri Streets — the
            # default basemap — draws its own faint municipal boundaries, and a
            # bare navy dash disappears into them. The white halo lifts the
            # outline off Streets, Topo and Satellite alike. The FeatureGroup
            # keeps it to one checkbox in the layer picker instead of two.
            boundary_group = folium.FeatureGroup(name=_("boundary_layer_name"))
            folium.GeoJson(
                lawrence_geojson,
                style_function=lambda _f: {
                    "color": "#ffffff",
                    "weight": 7,
                    "opacity": 0.9,
                    "fill": False,
                },
            ).add_to(boundary_group)
            folium.GeoJson(
                lawrence_geojson,
                style_function=lambda _f: {
                    "color": BRAND_DARK,
                    "weight": 3.5,
                    "opacity": 1,
                    "fillColor": BRAND_DARK,
                    "fillOpacity": 0.04,
                    "dashArray": "10 6",
                },
            ).add_to(boundary_group)
            boundary_group.add_to(m)

        # (org marker, its hidden site layer, points to frame) — consumed after
        # the loop to emit the JS that toggles them.
        site_registry: list[
            tuple[folium.Marker, folium.FeatureGroup, list, str]
        ] = []
        # Every org pin, so focus mode knows what to fade.
        all_org_markers: list[folium.Marker] = []

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
            def _row(label, value, cls="gwi-val"):
                return (
                    f'<div class="gwi-row"><div class="gwi-lbl">{label}</div>'
                    f'<div class="{cls}">{value}</div></div>'
                )

            rows_html = _row(
                _("popup_address"),
                f"{row['Address']}, {row['City']}, {row['State']}",
            )
            if phone:
                rows_html += _row(_("popup_phone"), _tel_link(phone))

            hours_block = _hours_today_html(hours)
            status_html = _status_badge_html(_schedules(hours))
            # Status and hours lead the body — they are what the pin is being
            # clicked for — with the badge sitting above the label grid.
            body_html = status_html
            if hours_block:
                body_html += _row(_("popup_hours"), hours_block)
            body_html += rows_html
            body_html += _row(_("popup_type"), org_type)

            # Services is the longest field by far (one org lists nine). Folded
            # away past a few entries so it cannot dominate the popup.
            svc_items = _smart_split(row.get("Services", ""))
            if len(svc_items) > 3:
                svc_val = (
                    f'<details><summary>{_("popup_services")} ({len(svc_items)})'
                    f'</summary><div class="gwi-mid">{svc_tags}</div></details>'
                )
                body_html += f'<div class="gwi-row"><div class="gwi-lbl"></div>{svc_val}</div>'
            else:
                body_html += _row(_("popup_services"), svc_tags, "gwi-val gwi-mid")

            site_groups = _group_by_address(_org_locations(row["Name"]))
            site_layer, site_bounds = _site_layer(m, row, site_groups)
            locs_html = _locations_html(
                site_groups, n_plotted=len(site_bounds) - 1 if site_bounds else 0
            )
            if locs_html:
                body_html += locs_html

            reports = []
            if str(impact).strip():
                reports.append(f'{_("popup_impact")}: {_link_cell(impact)}')
            if str(strategic).strip():
                reports.append(f'{_("popup_strategic")}: {_link_cell(strategic)}')
            if reports:
                body_html += f'<div class="gwi-row"><div class="gwi-lbl"></div>' \
                             f'<div class="gwi-val" style="font-size:11px;">' \
                             f'{" · ".join(reports)}</div></div>'

            directions_url = _directions_url(row["Latitude"], row["Longitude"])
            action_html = (
                f'<a class="gwi-btn" href="{directions_url}" target="_blank" '
                f'style="background:{BRAND_DARK};">{_("get_directions")}</a>'
            )
            if url:
                action_html += (
                    f'<a class="gwi-btn" href="{url}" target="_blank" '
                    f'style="background:{BRAND_MED};">{_("visit_website")}</a>'
                )
            else:
                action_html += (
                    f'<span class="gwi-muted" style="font-size:11px;">'
                    f'{_("no_website_listed")}</span>'
                )

            popup_html = (
                f'<div class="gwi-pop">'
                f'<div class="gwi-pop-hd">{row["Name"]}</div>'
                f'<div class="gwi-pop-bd">{body_html}</div>'
                f'<div class="gwi-pop-ft">{action_html}</div>'
                f"</div>"
            )

            tooltip_html = (
                f'<div style="font-family:{FONT_STACK};font-size:14px;'
                f'font-weight:700;color:{BRAND_DARK};max-width:200px;">{row["Name"]}</div>'
            )

            pin_svg = _pin_svg(pin_color)
            org_marker = folium.Marker(
                location=[row["Latitude"], row["Longitude"]],
                popup=folium.Popup(popup_html, max_width=360),
                tooltip=folium.Tooltip(tooltip_html),
                icon=folium.DivIcon(
                    html=pin_svg,
                    icon_size=(25, 41),
                    icon_anchor=(12, 41),
                    popup_anchor=(0, -38),
                ),
            )
            org_marker.add_to(m)
            all_org_markers.append(org_marker)
            if site_layer is not None:
                site_registry.append(
                    (org_marker, site_layer, site_bounds, row["Name"])
                )

        folium.LayerControl(collapsed=True).add_to(m)

        if site_registry:
            m.get_root().script.add_child(
                folium.Element(_site_toggle_js(m, site_registry, all_org_markers))
            )

        st_folium(m, use_container_width=True, height=620, returned_objects=[])
        st.caption(_("map_caption").format(n=len(map_data)))
        # The badges are computed once per rerun, so a tab left open overnight
        # would otherwise show yesterday's answer with nothing to say so.
        _as_of = _now_local()
        st.caption(
            _("status_asof").format(
                t=f'{_day_names()[_as_of.weekday()]} {_as_of.strftime("%-I:%M %p")}'
            )
        )
        if not lawrence_geojson:
            st.caption(f"⚠️ {_('boundary_unavailable')}")


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
                    "Hours",
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
                    "Hours": _("sec_hours"),
                    "ImpactReport": _("sec_impact"),
                    "StrategicPlan": _("sec_strategic"),
                }
            )
            .copy()
        )
        dir_df[_("col_orgtype")] = dir_df[_("col_orgtype")].apply(_org_type_label)

        # Sortable open/closed column: True, False, or None where the org
        # publishes no hours, so "unknown" never sorts in with "closed".
        _dir_now = _now_local()

        def _open_flag(h):
            state = open_status(parse_hours(str(h)), _dir_now)[0]
            return None if state == "unknown" else state == "open"

        dir_df.insert(
            2, _("open_now_col"), filtered["Hours"].apply(_open_flag).values
        )
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
                _("open_now_col"): st.column_config.CheckboxColumn(
                    _("open_now_col"), help=_("filter_open_now_help")
                ),
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

                detail_hours_raw = str(row.get("Hours", "")).strip()
                section_label("🕒", _("sec_hours"))
                st.markdown(
                    _status_badge_html(_schedules(detail_hours_raw)),
                    unsafe_allow_html=True,
                )
                if detail_hours_raw:
                    # Full width here, so the whole week for every service is
                    # shown outright rather than folded the way the popup needs.
                    st.markdown(
                        _hours_html(detail_hours_raw), unsafe_allow_html=True
                    )
                src = str(row.get("HoursSourceURL", "")).strip()
                checked = str(row.get("HoursVerifiedOn", "")).strip()
                if checked:
                    line = _("hours_source").format(d=checked)
                    if src.startswith("http"):
                        line += (
                            f' · <a href="{src}" target="_blank" '
                            f'style="color:{BRAND_MED};">{_("hours_source_link")}</a>'
                        )
                    st.markdown(
                        f"<div style='color:{TEXT_MUTED};font-size:12px;'>{line}</div>",
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
                                    _status_badge_html(_schedules(loc["Hours"]))
                                    + _hours_html(loc["Hours"]),
                                    unsafe_allow_html=True,
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
