"""
GWI Nonprofit Partner Explorer  —  Streamlit app
Run:  streamlit run app.py
"""

import json
import os
import re
import unicodedata

from datetime import datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import folium
import pandas as pd
import requests
import streamlit as st

from branca.element import MacroElement, Template
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
.gwi-lbl{flex:0 0 54px;color:__TEXT_MUTED__;font-size:12px;font-weight:600;padding-top:1px}
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
  flex-direction:column;max-height:min(360px,62vh);color:__TEXT_DARK__}
.gwi-pop-hd{flex:0 0 auto;background:#fff;color:__TEXT_DARK__;font-size:17px;
  font-family:__DISPLAY__;font-weight:600;line-height:1.25;padding:13px 14px 10px;border-bottom:1px solid __BORDER__}
/* The only scrolling region. overscroll-behavior stops the page from taking
   over the wheel once this reaches its end. */
/* min-height:0 is load-bearing: a column flex item defaults to min-height:auto,
   which refuses to shrink below its content, so overflow-y would never engage
   and the popup would grow past max-height exactly as before. */
.gwi-pop-bd{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;
  -webkit-overflow-scrolling:touch;scrollbar-width:thin;
  padding:9px 14px 6px;background:#fff}
.gwi-pop-bd::-webkit-scrollbar{width:9px}
.gwi-pop-bd::-webkit-scrollbar-thumb{background:#d3c9b9;border-radius:5px;
  border:2px solid #fff}
/* Directions/website stay put, so they never need scrolling to reach. */
.gwi-pop-ft{flex:0 0 auto;display:flex;gap:6px;flex-wrap:wrap;align-items:center;
  padding:8px 14px 10px;background:#fff;border-top:1px solid __BORDER__}


.gwi-btn{display:inline-block;padding:6px 12px;border-radius:6px;font-size:12px;
  font-weight:600;text-decoration:none;color:#fff!important;background:__BRAND_MED__}
.gwi-btn:hover{filter:brightness(.92)}
.gwi-btn--ghost{background:#fff;color:__BRAND_DARK__!important;border:1px solid __BORDER__}

.gwi-pop-sub{display:block;font-family:__FONT__;font-size:12px;font-weight:500;color:__TEXT_MID__;margin-top:2px}
.gwi-small{font-size:11px}
.gwi-today{font-size:12px;margin-bottom:4px}
.gwi-checked{font-size:11px;color:__TEXT_MUTED__;margin:-2px 0 6px}
.gwi-prog details{font-size:12px}

/* Address and phone, as plain lines at the full width of the popup. */
.gwi-info{font-size:12px;margin:5px 0;color:__TEXT_DARK__}

/* Small section heading inside a popup. */
.gwi-sec{font-size:12px;font-weight:600;color:__TEXT_MID__;margin:11px 0 5px;padding-top:9px;border-top:1px solid __BORDER__}

.gwi-chips{display:flex;flex-wrap:wrap;gap:4px}
.gwi-chip{display:inline-block;background:#f6e9e3;color:__BRAND_DARK__;
  border-radius:999px;padding:2px 9px;font-size:11px;font-weight:600;line-height:1.5}
.gwi-more{flex-basis:100%}
.gwi-pop .gwi-more>summary{font-size:11px}
.gwi-more>div{display:flex;flex-wrap:wrap;gap:4px;padding:4px 0 0!important}

/* Location list. Each numbered row matches a numbered pin. */
.gwi-hint{font-size:11px;color:__TEXT_MUTED__;margin:-2px 0 6px}
.gwi-note{background:#fff6e5;border-left:3px solid #d9922e;border-radius:4px;
  padding:6px 8px;margin:0 0 6px;font-size:11px;line-height:1.4}
.gwi-locs{display:flex;flex-direction:column;gap:2px}
.gwi-loc{display:flex;gap:9px;align-items:flex-start;padding:6px 6px;
  border-radius:8px;cursor:pointer;transition:background .12s}
.gwi-loc:hover,.gwi-loc:focus{background:#f6e9e3;outline:none}
.gwi-loc--static{cursor:default}
.gwi-loc--static:hover{background:none}
.gwi-loc-tx{flex:1 1 auto;min-width:0}
.gwi-loc-t{font-size:12px;font-weight:700;color:__TEXT_DARK__;line-height:1.3}
.gwi-loc-m{font-size:11px;color:__TEXT_MID__;margin-top:1px}
.gwi-loc-s{font-size:11px;font-weight:600;margin-top:1px}
.gwi-s-open{color:#1d5c57}
.gwi-s-closed{color:__TEXT_MUTED__}
.gwi-go{flex:0 0 auto;color:__BRAND_MED__;font-size:18px;line-height:1;padding-top:2px}
.gwi-tag{display:inline-block;margin-left:5px;background:#f3ede4;color:__TEXT_MUTED__;
  border-radius:4px;padding:0 5px;font-size:10px;font-weight:700;
  text-transform:uppercase;vertical-align:1px}
.gwi-progs{margin-top:3px}
.gwi-prog-li{font-size:11px;margin:3px 0;color:__TEXT_MID__}
.gwi-prog-li b{color:__TEXT_DARK__}

/* The number chip, drawn the same way as the numbered pin it points at. */
.gwi-num{flex:0 0 22px;height:22px;border-radius:50%;display:inline-flex;
  align-items:center;justify-content:center;font-size:12px;font-weight:700;
  background:#fff;color:__BRAND_MED__;border:2px solid __BRAND_MED__;box-sizing:border-box}
.gwi-num--own{background:__BRAND_MED__;color:#fff}
.gwi-num--off{border-color:__BORDER__;color:__TEXT_MUTED__}
.gwi-num--hd{flex:0 0 22px}
.gwi-hd-row{display:flex;gap:8px;align-items:center}

/* Programmes inside one building (a site's clinic and pharmacy). */
.gwi-prog{padding:6px 0;border-bottom:1px solid __BORDER__}
.gwi-prog:last-of-type{border-bottom:none}
.gwi-prog-t{font-size:12px;font-weight:700;margin-bottom:3px}

.gwi-back{cursor:pointer;color:__BRAND_MED__!important;text-decoration:none}
.gwi-back:hover{text-decoration:underline}

/* ‹ 2 / 6 › in a location's footer. */
.gwi-nav{margin-left:auto;display:inline-flex;align-items:center;gap:6px;
  font-size:12px;font-weight:600;color:__TEXT_MID__}
.gwi-nav button{width:28px;height:28px;border-radius:6px;border:1px solid __BORDER__;
  background:#fff;color:__BRAND_MED__;font-size:17px;line-height:1;cursor:pointer;padding:0}
.gwi-nav button:hover{background:#f6e9e3}

.gwi-reports{font-size:11px;color:__TEXT_MID__;margin-top:10px}

/* Hover card on a pin. */
.gwi-tt{font-family:__FONT__;max-width:210px;white-space:normal}
.gwi-tt b{display:block;font-size:13px;color:__BRAND_DARK__;line-height:1.3}
.gwi-tt span{display:block;font-size:11px;color:__TEXT_MUTED__;margin-top:1px}

/* Pins. Only opacity/filter are animated on the marker element itself —
   Leaflet positions every marker with an inline `transform`, so a transform
   there would fight it. Scaling is done on our inner div instead. */
.gwi-pin{position:relative;transform-origin:50% 100%;transition:transform .15s ease}
.gwi-pin-in{animation:gwiFade .22s ease-out}
@keyframes gwiFade{from{opacity:0}to{opacity:1}}
.gwi-count{position:absolute;top:-5px;right:-9px;min-width:17px;height:17px;
  padding:0 4px;box-sizing:border-box;border-radius:999px;background:__BRAND_DARK__;
  color:#fff;border:2px solid #fff;font:700 10px/13px __FONT__;text-align:center;
  box-shadow:0 1px 3px rgba(0,0,0,.3)}
.gwi-hi>.gwi-pin{transform:scale(1.3)}
.gwi-hi{z-index:10000!important}

.gwi-pop--site{width:268px;max-height:min(360px,70vh)}

/* Focus mode: opening an org that runs several sites hides every other org,
   so its own locations are the only thing competing for attention. */
.gwi-focusnote{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);
  z-index:650;max-width:88%;display:flex;align-items:center;gap:10px;
  background:rgba(255,255,255,.97);color:__TEXT_DARK__;
  border:1px solid __BORDER__;border-radius:999px;
  padding:5px 6px 5px 14px;font:600 12px/1.35 __FONT__;
  box-shadow:0 2px 10px rgba(0,0,0,.14);
  opacity:0;pointer-events:none;transition:opacity .2s ease}
.gwi-focusnote.is-on{opacity:1;pointer-events:auto}
.gwi-fn-t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gwi-fn-x{flex:0 0 auto;border:none;border-radius:999px;background:__BRAND_MED__;
  color:#fff;font:600 12px/1 __FONT__;padding:6px 11px;cursor:pointer}
.gwi-fn-x:hover{background:__BRAND_DARK__}

.leaflet-marker-icon{transition:opacity .2s ease}
.gwi-dim{opacity:0!important;pointer-events:none!important}
.gwi-focus{filter:drop-shadow(0 0 7px rgba(163,63,36,.5))}
.gwi-focus .gwi-count{display:none}

</style>
"""


def _popup_css() -> str:
    return (
        _POPUP_CSS.replace("</style>", _hours_css() + "</style>")
        .replace("__FONT__", FONT_STACK)
        .replace("__DISPLAY__", DISPLAY_STACK)
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

    Light Gray is the default (`show=True`): a quiet base lets the brick pins
    carry the map. The rest are opt-in. Exactly one
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
        show=True,
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
    page_title="Lawrence Resource Finder",
    page_icon="🗺️",
    layout="wide",
    # "auto" collapses the sidebar on phones. "expanded" opened it over the whole
    # screen on load, so the first thing a phone user saw was a filter panel.
    initial_sidebar_state="auto",
)

# ── design tokens ─────────────────────────────────────────────────────────────
# "Mill City": the palette is drawn from Lawrence itself — the brick of the
# Merrimack mills, the river, and the paper-and-ink of a printed community
# guide — rather than a generic dashboard blue. Every text pairing below
# clears WCAG AA (4.5:1) on both the paper background and white.
INK = "#1c2230"           # text, headings                    14.9:1 on paper
BRICK = "#a33f24"         # accent: pins, links, primary action  6.0:1 on paper
RIVER = "#1d5c57"         # "open now" only
PAPER = "#faf7f2"         # page background

BRAND_DARK = INK
BRAND_MED = BRICK
TEXT_DARK = INK
TEXT_MID = "#4b5160"
BG_WHITE = "#ffffff"
BG_LIGHT = PAPER
BG_TINT = "#f3ede4"       # quiet fills: chips, sidebar, hover
BRICK_TINT = "#f6e9e3"    # selected / hovered accent fill
BORDER = "#e6dfd3"
BORDER_STRONG = "#d3c9b9" # input and button outlines

# Muted text: warm grey, 5.4:1 on paper, so it still passes where it carries meaning.
TEXT_MUTED = "#6b6558"

# Public Sans for reading (a plain, sturdy civic face with full Spanish
# coverage) and Fraunces for the few display headings, which gives the page a
# printed-guide warmth instead of the default app look. Both fall back to the
# native system faces if Google Fonts is blocked.
FONT_STACK = (
    "'Public Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)
DISPLAY_STACK = "'Fraunces', Georgia, 'Times New Roman', serif"
FONTS_URL = (
    "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;"
    "9..144,600&family=Public+Sans:wght@400;500;600;700&display=swap"
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
        "filters_heading": "More filters",
        "orgs_total": "{n} organizations total",
        "search_label": "Or search",
        "search_placeholder": "Name, city, or service…",
        "search_placeholder_main": "Search for food, rent help, ESL, or an organization…",
        "category_label": "Category",
        "category_placeholder": "All categories",
        "services_label": "Specific service",
        "services_placeholder": "Type to search services…",
        "orgtype_label": "Organization Type",
        "orgtype_all": "All",
        "reset_button": "Clear all filters",
        "page_heading": "Lawrence Resource Finder",
        "page_sub": "Free and low-cost help from {n} local organizations: food, housing, health care, legal help and more.",
        "showing_all": "Showing all {n} organizations",
        "showing_filtered": "Showing {n} of {total} organizations",
        "nav_cta": "Ask AI assistant",
        "nav_cta_help": "Opens Google Gemini in a new tab. It needs a Google sign-in, and some work and city computers block it.",
        "feedback_cta": "Give feedback",
        "missing_org": "Is an organization missing, or is something here wrong? Tell us →",
        "feedback_unset": "Feedback form link not yet configured",
        "tab_map": "Map",
        "tab_directory": "Directory",
        "tab_org_detail": "Organization details",
        "no_results": "No organizations match these filters. Try a different search or clear the filters.",
        "no_coords": "Matching organizations have no coordinates to plot.",
        "map_caption": "{n} organizations on the map. Click a pin for details.",
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
        "filter_open_now": "Open now",
        "filter_open_now_help": "Show only organizations open at this moment. Organizations that publish no hours are hidden.",
        "open_now_col": "Open now",
        "status_asof": "Open/closed shown for {t} (Lawrence time)",
        "days_short": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
        "hours_closed_word": "closed",
        "hours_24": "24 hours",
        "hours_today": "Today",
        "hours_full_week": "Full week",
        "hours_other_days": "Other days",
        "focus_banner": "{org} · {n} locations",
        "focus_show_all": "Show all",
        "pin_multi_short": "{n} locations",
        "months_short": "Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec",
        "pin_multi_hint": "{n} locations — click to see them all",
        "loc_this_pin": "This pin",
        "loc_programs_here": "{n} programs here",
        "loc_tap_hint": "Tap a location to see it on the map.",
        "loc_office_tag": "Office",
        "more_n": "+{n} more",
        "popup_type": "Type",
        "popup_services": "Services",
        "popup_impact": "Impact Report",
        "popup_strategic": "Strategic Plan",
        "view_link": "View",
        "not_available_short": "N/A",
        "visit_website": "Website",
        "get_directions": "Directions",
        "no_website_listed": "No website listed",
        "download_button": "Download {n} results (CSV)",
        "col_name": "Name",
        "col_city": "City",
        "col_orgtype": "Org Type",
        "col_servicearea": "Service Area",
        "col_services": "Services",
        "link_open": "Open",
        "impact_open": "Open",
        "strategic_open": "Open",
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
        "loc_admin_note_map": "This pin is the organization's office. Services are at the numbered locations.",
        "loc_directions": "Directions",
        "loc_call": "Call",
        "loc_no_address": "By phone only — no public address listed",
        "quick_heading": "What do you need help with?",
        "quick_food": "Food",
        "quick_housing": "Housing & shelter",
        "quick_health": "Health care",
        "quick_immigration": "Immigration help",
        "quick_jobs": "Jobs & training",
        "clear_filters": "Clear filters",
        "back_to": "Back to {org}",
        "more_filters_hint": "Narrow by a specific service or organization type.",
    },
    "es": {
        "filters_heading": "Más filtros",
        "orgs_total": "{n} organizaciones en total",
        "search_label": "O busque",
        "search_placeholder": "Nombre, ciudad o servicio…",
        "search_placeholder_main": "Buscar comida, vivienda, inglés o una organización…",
        "category_label": "Categoría",
        "category_placeholder": "Todas las categorías",
        "services_label": "Servicio específico",
        "services_placeholder": "Escriba para buscar servicios…",
        "orgtype_label": "Tipo de Organización",
        "orgtype_all": "Todos",
        "reset_button": "Borrar todos los filtros",
        "page_heading": "Buscador de Recursos de Lawrence",
        "page_sub": "Ayuda gratuita o de bajo costo de {n} organizaciones locales: comida, vivienda, salud, ayuda legal y más.",
        "showing_all": "Mostrando las {n} organizaciones",
        "showing_filtered": "Mostrando {n} de {total} organizaciones",
        "nav_cta": "Preguntar al asistente de IA",
        "nav_cta_help": "Abre Google Gemini en una pestaña nueva. Requiere iniciar sesión con Google, y algunas computadoras del trabajo o de la ciudad lo bloquean.",
        "feedback_cta": "Enviar comentarios",
        "missing_org": "¿Falta una organización o hay algo incorrecto? Avísenos →",
        "feedback_unset": "El enlace del formulario aún no está configurado",
        "tab_map": "Mapa",
        "tab_directory": "Directorio",
        "tab_org_detail": "Detalles de la organización",
        "no_results": "Ninguna organización coincide con estos filtros. Pruebe otra búsqueda o borre los filtros.",
        "no_coords": "Las organizaciones encontradas no tienen coordenadas para mostrar en el mapa.",
        "map_caption": "{n} organizaciones en el mapa. Haga clic en un marcador para ver detalles.",
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
        "filter_open_now": "Abiertos ahora",
        "filter_open_now_help": "Muestra solo las organizaciones abiertas en este momento. Las que no publican horario quedan ocultas.",
        "open_now_col": "Abierto ahora",
        "status_asof": "Abierto/cerrado según las {t} (hora de Lawrence)",
        "days_short": "Lun,Mar,Mié,Jue,Vie,Sáb,Dom",
        "hours_closed_word": "cerrado",
        "hours_24": "24 horas",
        "hours_today": "Hoy",
        "hours_full_week": "Semana completa",
        "hours_other_days": "Otros días",
        "focus_banner": "{org} · {n} ubicaciones",
        "focus_show_all": "Mostrar todo",
        "pin_multi_short": "{n} ubicaciones",
        "months_short": "ene,feb,mar,abr,may,jun,jul,ago,sep,oct,nov,dic",
        "pin_multi_hint": "{n} ubicaciones — haga clic para verlas todas",
        "loc_this_pin": "Este marcador",
        "loc_programs_here": "{n} programas aquí",
        "loc_tap_hint": "Toque una ubicación para verla en el mapa.",
        "loc_office_tag": "Oficina",
        "more_n": "+{n} más",
        "popup_type": "Tipo",
        "popup_services": "Servicios",
        "popup_impact": "Informe de Impacto",
        "popup_strategic": "Plan Estratégico",
        "view_link": "Ver",
        "not_available_short": "N/D",
        "visit_website": "Sitio web",
        "get_directions": "Cómo llegar",
        "no_website_listed": "Sin sitio web registrado",
        "download_button": "Descargar {n} resultados (CSV)",
        "col_name": "Nombre",
        "col_city": "Ciudad",
        "col_orgtype": "Tipo de Org.",
        "col_servicearea": "Área de Servicio",
        "col_services": "Servicios",
        "link_open": "Abrir",
        "impact_open": "Abrir",
        "strategic_open": "Abrir",
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
        "loc_admin_note_map": "Este marcador es la oficina de la organización. Los servicios se ofrecen en las ubicaciones numeradas.",
        "loc_directions": "Cómo llegar",
        "loc_call": "Llamar",
        "loc_no_address": "Solo por teléfono — sin dirección pública",
        "quick_heading": "¿Con qué necesita ayuda?",
        "quick_food": "Comida",
        "quick_housing": "Vivienda y refugio",
        "quick_health": "Atención médica",
        "quick_immigration": "Ayuda de inmigración",
        "quick_jobs": "Empleo y capacitación",
        "clear_filters": "Borrar filtros",
        "back_to": "Volver a {org}",
        "more_filters_hint": "Filtre por un servicio específico o tipo de organización.",
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


def _fold(text: str) -> str:
    """Strip accents: 'Inglés' -> 'Ingles', so either spelling finds the other."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", str(text))
        if not unicodedata.combining(ch)
    )


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
  @import url('{FONTS_URL}');
  html, body, [class*="css"] {{ color:{TEXT_DARK} !important;
    font-family:{FONT_STACK} !important;
    font-size:16px; line-height:1.5; }}
  .stApp {{ background-color:{PAPER}; }}
  .stApp p, .stApp label, .stApp input, .stApp button, .stApp li,
  .stApp textarea {{ font-family:{FONT_STACK}; }}
  header[data-testid="stHeader"] {{ background:transparent; }}
  .stApp a {{ color:{BRICK}; text-underline-offset:3px; }}

  .block-container {{ padding-top:2.25rem; padding-bottom:3rem; max-width:1240px; }}
  h1,h2,h3,h4,h5,h6 {{ color:{INK} !important; line-height:1.2; }}

  /* ── header ── */
  .gwi-h1 {{ font-family:{DISPLAY_STACK} !important; font-weight:600;
    font-size:42px !important; letter-spacing:-.015em; color:{INK} !important;
    margin:0 !important; padding:0 !important; line-height:1.1; }}
  .gwi-sub {{ color:{TEXT_MID}; font-size:17px; margin:8px 0 0; max-width:40em; }}

  .gwi-links {{ display:flex; align-items:center; gap:20px; flex-wrap:wrap;
    margin-top:12px; }}
  .gwi-link {{ color:{TEXT_MID} !important; font-size:14px; font-weight:500;
    text-decoration:underline; text-underline-offset:3px;
    text-decoration-color:{BORDER_STRONG}; }}
  .gwi-link--strong {{ color:{BRICK} !important; font-weight:600;
    text-decoration-color:{BRICK}; }}
  .gwi-link:hover {{ text-decoration-color:currentColor; }}
  /* Language switch pinned top-right, beside the title. */
  .st-key-lang_radio {{ display:flex; justify-content:flex-end; }}

  /* ── the finder card ── */
  .st-key-gwi_filters {{
    background:{BG_WHITE}; border:1px solid {BORDER}; border-radius:14px;
    padding:22px 24px 12px; margin-top:22px;
    box-shadow:0 1px 2px rgba(28,34,48,.04); }}
  .gwi-q {{ font-family:{DISPLAY_STACK} !important; font-size:22px; font-weight:500;
    color:{INK}; margin:0 0 12px; }}

  .st-key-gwi_chips {{ padding-bottom:16px; margin-bottom:6px;
    border-bottom:1px solid {BORDER}; }}
  .st-key-gwi_chips button {{
    min-height:48px; border-radius:10px !important;
    border:1px solid {BORDER_STRONG} !important; background:{BG_WHITE} !important;
    transition:border-color .12s, background .12s; }}
  .st-key-gwi_chips button p {{ font-size:16px !important; font-weight:600 !important;
    color:{INK} !important; }}
  .st-key-gwi_chips button[kind="secondary"]:hover {{
    border-color:{BRICK} !important; background:{BRICK_TINT} !important; }}
  .st-key-gwi_chips button[kind="primary"] {{
    background:{BRICK} !important; border-color:{BRICK} !important; }}
  .st-key-gwi_chips button[kind="primary"] p {{ color:#fff !important; }}

  /* Form labels: readable, not shouting. */
  [data-testid="stWidgetLabel"] p {{
    color:{INK} !important; font-size:14px !important; font-weight:600 !important; }}

  /* Inputs: a visible outline so they read as controls at a glance. */
  .stTextInput .react-aria-TextField > div,
  .stSelectbox .react-aria-ComboBox > div,
  [data-baseweb="select"] > div {{
    background:{BG_WHITE} !important; border:1px solid {BORDER_STRONG} !important;
    border-radius:10px !important; min-height:46px; }}
  .stTextInput .react-aria-TextField > div:focus-within,
  .stSelectbox .react-aria-ComboBox > div:focus-within,
  [data-baseweb="select"] > div:focus-within {{
    border-color:{BRICK} !important; box-shadow:0 0 0 3px {BRICK_TINT}; }}
  [data-baseweb="select"] * {{ color:{INK}; }}
  .stTextInput input {{ font-size:16px !important; }}
  .stTextInput input::placeholder {{ color:{TEXT_MUTED}; }}

  /* Selected categories/services. */
  [data-baseweb="tag"], [data-baseweb="tag"] * {{
    background:{BRICK_TINT} !important; color:{BRICK} !important; }}
  [data-baseweb="tag"] {{ border-radius:6px !important; font-weight:600 !important; }}
  [data-baseweb="tag"] svg {{ fill:{BRICK} !important; }}

  /* ── results line + tabs ── */
  .gwi-count {{ color:{TEXT_MID}; font-size:15px; margin:0; }}
  .gwi-count b {{ color:{INK}; font-weight:600; }}
  [data-testid="stTabs"] button[role="tab"] p {{
    font-size:16px !important; font-weight:600 !important; }}
  [data-testid="stTabs"] [data-baseweb="tab-list"] {{ gap:22px; }}

  /* ── sidebar ── */
  section[data-testid="stSidebar"] {{ background-color:{BG_TINT} !important;
    border-right:1px solid {BORDER}; }}
  section[data-testid="stSidebar"] * {{ color:{INK}; }}
  .gwi-side-h {{ font-family:{DISPLAY_STACK} !important; font-size:22px;
    font-weight:600; color:{INK}; margin:0 0 4px; }}
  section[data-testid="stSidebar"] [data-testid="stButton"] button {{
    background:{BG_WHITE} !important; border:1px solid {BORDER_STRONG} !important;
    border-radius:10px !important; }}

  /* ── detail tab ── */
  .gwi-detail-h {{ font-family:{DISPLAY_STACK} !important; font-weight:600 !important;
    font-size:30px !important; margin:0 !important; padding:0 !important; }}
  .svc-chip {{
    display:inline-block; background:{BG_TINT}; color:{INK};
    border-radius:6px; padding:3px 10px; margin:3px; font-size:14px; line-height:1.5; }}

  [data-testid="stDownloadButton"] button {{
    background:{BG_WHITE} !important; color:{INK} !important;
    border:1px solid {BORDER_STRONG} !important; border-radius:10px !important; }}
  [data-testid="stDataFrame"] {{ border:1px solid {BORDER}; border-radius:12px; overflow:hidden; }}
  [data-testid="stAlert"] {{ border-radius:10px !important; }}
  [data-testid="stCaptionContainer"] p {{ color:{TEXT_MUTED} !important; }}
  iframe {{ border-radius:12px; }}
  hr {{ border:none; border-top:1px solid {BORDER}; margin:10px 0; }}

  /* Phones. The five needs stay in one row that scrolls sideways. */
  @media (max-width: 640px) {{
    .block-container {{ padding-top:1rem; }}
    .gwi-h1 {{ font-size:30px !important; }}
    .st-key-lang_radio {{ justify-content:flex-start; }}
    .gwi-sub {{ font-size:15px; }}
    .st-key-gwi_filters {{ padding:16px 14px 6px; }}
    .gwi-q {{ font-size:19px; }}
    .st-key-gwi_chips [data-testid="stHorizontalBlock"] {{
      flex-direction:row !important; flex-wrap:nowrap !important;
      overflow-x:auto; gap:8px !important; padding-bottom:4px;
      scrollbar-width:none; }}
    .st-key-gwi_chips [data-testid="stColumn"],
    .st-key-gwi_chips [data-testid="column"] {{
      flex:0 0 auto !important; width:auto !important; min-width:0 !important; }}
    .st-key-gwi_chips button {{ white-space:nowrap; padding:4px 16px; }}
  }}
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


def _pin_svg(
    color: str,
    fade: bool = False,
    number: str = "",
    count: int = 0,
) -> str:
    """The teardrop map pin, in two roles.

    Solid: an organisation. When it runs several places, a small count bubble
    sits on its shoulder, so the map says "there's more here" before anyone
    clicks.

    Hollow and numbered: one of that organisation's other locations. Same
    silhouette so it reads as the same organisation, inverted so it is never
    mistaken for a different one; the number matches the popup list.
    """
    if number:
        shape = (
            f'<path d="M16 1.5C8 1.5 1.5 8 1.5 16c0 9.3 14.5 33.5 14.5 33.5S30.5 25.3 '
            f'30.5 16C30.5 8 24 1.5 16 1.5z" fill="#fff" stroke="{color}" '
            f'stroke-width="3"/>'
            f'<text x="16" y="21.5" text-anchor="middle" font-size="15" '
            f'font-weight="700" font-family="Arial,sans-serif" fill="{color}">'
            f"{number}</text>"
        )
    else:
        shape = (
            f'<path d="M16 0C7.163 0 0 7.163 0 16c0 10 16 36 16 36S32 26 32 16'
            f'C32 7.163 24.837 0 16 0z" fill="{color}" stroke="#fff" '
            f'stroke-width="2"/>'
            f'<circle cx="16" cy="16" r="7" fill="white" opacity="0.85"/>'
        )
    bubble = f'<span class="gwi-count">{count}</span>' if count > 1 else ""
    cls = "gwi-pin gwi-pin-in" if fade else "gwi-pin"
    return (
        f'<div class="{cls}" style="width:25px;height:41px;">'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 52" '
        f'width="25" height="41">{shape}</svg>{bubble}</div>'
    )


def _site_coords(loc: dict) -> tuple[float, float] | None:
    try:
        return (float(loc.get("Latitude", "")), float(loc.get("Longitude", "")))
    except (TypeError, ValueError):
        return None


def _addr_line(loc: dict) -> str:
    return ", ".join(
        p for p in (loc.get(k, "").strip() for k in ("Address", "City", "State", "Zip")) if p
    )


def _group_title(locs: list[dict]) -> str:
    """One name for a building.

    The shared site name when the programmes differ only by suffix ("Main Site
    — Clinic"/"— Pharmacy"). Where they share nothing, as with a dozen unrelated
    agency programmes at one address, the street is the honest label — the
    first programme's name would imply the entry only covers that one.
    """
    head = locs[0]
    title = head.get("LocationName", "").strip()
    if len(locs) > 1:
        stems = {loc.get("LocationName", "").split("—")[0].strip() for loc in locs}
        title = (
            stems.pop()
            if len(stems) == 1
            else (_street_of(head.get("Address", "")) or title)
        )
    return title


def _program_label(loc: dict) -> str:
    name = loc.get("LocationName", "").strip()
    return name.split("—")[-1].strip() if "—" in name else name


def _site_plan(org_row, groups: list[tuple[str, list[dict]]]) -> list[dict]:
    """Decide, once, how each of an org's buildings appears on the map.

    Three kinds: the building the org's own pin already stands on ("own"), a
    building with coordinates that gets its own numbered pin, and a site with
    no address (a hotline) that can only be listed. The popup list and the
    pins both read this, so the numbers can never disagree.

    Numbered by distance from the org's own pin, so 1 is the nearest and
    stepping through them with ‹ › moves outward instead of zig-zagging.
    """
    own = (
        _street_of(org_row["Address"]).lower(),
        str(org_row["City"]).strip().lower(),
    )
    try:
        origin = (float(org_row["Latitude"]), float(org_row["Longitude"]))
    except (TypeError, ValueError):
        origin = None

    entries = []
    for _key, locs in groups:
        head = locs[0]
        # Compared by street address rather than by distance: Lazarus House's
        # soup kitchen is a genuinely separate place 40 m from its office.
        is_own = (
            _street_of(head.get("Address", "")).lower(),
            head.get("City", "").strip().lower(),
        ) == own
        coords = None if is_own else _site_coords(head)
        entries.append({"locs": locs, "own": is_own, "coords": coords, "num": 0})

    def _dist(e):
        if not (origin and e["coords"]):
            return 0.0
        return (e["coords"][0] - origin[0]) ** 2 + (e["coords"][1] - origin[1]) ** 2

    entries.sort(key=lambda e: (not e["own"], e["coords"] is None, _dist(e)))
    n = 0
    for e in entries:
        if e["coords"]:
            n += 1
            e["num"] = n
    return entries


def _group_status(locs: list[dict]) -> tuple[str, str]:
    """(state, short text) for a building — open if any programme there is."""
    scheds = [s for loc in locs for s in _schedules(loc.get("Hours", ""))]
    state, detail, _label = open_status(scheds, _now_local(), _day_names())
    if state == "open":
        return state, _("status_open").capitalize() + (
            f" · {_('status_until').format(t=detail)}" if detail else ""
        )
    if state == "closed":
        return state, _("status_closed").capitalize() + (
            f" · {_('status_opens').format(t=detail)}" if detail else ""
        )
    return state, ""


def _checked_date(raw) -> str:
    """'2026-09-24' -> 'Sep 24, 2026' (or '24 sep 2026'). Unparseable passes through."""
    text = str(raw or "").strip()
    try:
        d = datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return text
    mon = _("months_short").split(",")[d.month - 1]
    if st.session_state["lang"] == "es":
        return f"{d.day} {mon} {d.year}"
    return f"{mon} {d.day}, {d.year}"


def _info_line(html: str) -> str:
    return f'<div class="gwi-info">{html}</div>'


def _chips_html(items: list[str], keep: int = 4) -> str:
    """Services as chips; past `keep`, the rest fold behind "+N more"."""
    if not items:
        return ""
    chip = lambda s: f'<span class="gwi-chip">{s}</span>'
    out = "".join(chip(s) for s in items[:keep])
    rest = items[keep:]
    if rest:
        out += (
            f'<details class="gwi-more"><summary>{_("more_n").format(n=len(rest))}'
            f'</summary><div>{"".join(chip(s) for s in rest)}</div></details>'
        )
    return f'<div class="gwi-chips">{out}</div>'


def _locations_html(plan: list[dict], key: str | None, n_pins: int) -> str:
    """An org's locations as a numbered list that drives the map.

    Each numbered row matches a numbered pin. Tapping a row opens that pin;
    hovering it (desktop) lifts the pin, so the eye can connect the two without
    reading addresses. Detail — every programme's hours, phone, directions —
    lives in each location's own popup rather than being stacked here, which is
    what used to make this popup taller than the map.
    """
    if not plan:
        return ""

    rows = []
    admin_pin = False
    for e in plan:
        locs, head = e["locs"], e["locs"][0]
        is_admin = all(loc.get("Kind", "") == "admin" for loc in locs)
        title = _group_title(locs)
        office = f'<span class="gwi-tag">{_("loc_office_tag")}</span>' if is_admin else ""

        svc = head.get("ServicesHere", "").strip() if len(locs) == 1 else ""
        city = head.get("City", "").strip()
        meta_bits = [x for x in (svc if not is_admin else "", city) if x]
        state, status_txt = _group_status(locs)
        status = (
            f'<div class="gwi-loc-s gwi-s-{state}">{status_txt}</div>'
            if status_txt
            else ""
        )

        if e["own"]:
            admin_pin = admin_pin or is_admin
            chip = '<span class="gwi-num gwi-num--own"></span>'
            meta_bits = [_("loc_this_pin")] + (
                [_("loc_programs_here").format(n=len(locs))] if len(locs) > 1 else []
            )
            progs = ""
            if len(locs) > 1:
                items = "".join(
                    f'<div class="gwi-prog-li"><b>{_program_label(loc)}</b>'
                    + (
                        f' — {loc["ServicesHere"].strip()}'
                        if loc.get("ServicesHere", "").strip()
                        and loc["ServicesHere"].strip().lower()
                        != _program_label(loc).lower()
                        else ""
                    )
                    + (
                        f'<div class="gwi-muted">{"; ".join(_hours_lines(loc["Hours"]))}</div>'
                        if loc.get("Hours", "").strip()
                        else ""
                    )
                    + "</div>"
                    for loc in locs
                )
                progs = (
                    f'<details class="gwi-more gwi-progs"><summary>'
                    f'{_("loc_programs_here").format(n=len(locs))}</summary>'
                    f"<div>{items}</div></details>"
                )
            rows.append(
                f'<div class="gwi-loc gwi-loc--static">{chip}<div class="gwi-loc-tx">'
                f'<div class="gwi-loc-t">{title}{office}</div>'
                f'<div class="gwi-loc-m">{" · ".join(meta_bits)}</div>{status}{progs}'
                f"</div></div>"
            )
        elif e["num"] and key:
            i = e["num"] - 1
            act = (
                f'role="button" tabindex="0" onclick="gwiGo(\'{key}\',{i})" '
                f"onmouseenter=\"gwiHi('{key}',{i},1)\" "
                f"onmouseleave=\"gwiHi('{key}',{i},0)\" "
                f"onkeydown=\"if(event.key==='Enter')gwiGo('{key}',{i})\""
            )
            rows.append(
                f'<div class="gwi-loc" {act}><span class="gwi-num">{e["num"]}</span>'
                f'<div class="gwi-loc-tx"><div class="gwi-loc-t">{title}{office}</div>'
                f'<div class="gwi-loc-m">{" · ".join(meta_bits)}</div>{status}</div>'
                f'<span class="gwi-go">›</span></div>'
            )
        else:
            # No coordinates: a hotline or an unlisted site. Listed, never pinned.
            phone = head.get("Phone", "").strip()
            addr = _addr_line(head)
            where = (
                _tel_link(phone)
                if phone
                else (addr or f'<i>{_("loc_no_address")}</i>')
            )
            rows.append(
                f'<div class="gwi-loc gwi-loc--static">'
                '<span class="gwi-num gwi-num--off">–</span>'
                f'<div class="gwi-loc-tx"><div class="gwi-loc-t">{title}{office}</div>'
                f'<div class="gwi-loc-m">{" · ".join(x for x in (svc, where) if x)}</div>'
                f"{status}</div></div>"
            )

    note = f'<div class="gwi-note">{_("loc_admin_note_map")}</div>' if admin_pin else ""
    hint = f'<div class="gwi-hint">{_("loc_tap_hint")}</div>' if n_pins else ""
    return (
        f'<div class="gwi-sec">{_("popup_locations")} · {len(plan)}</div>'
        f'{hint}{note}<div class="gwi-locs">{"".join(rows)}</div>'
    )


def _street_key(address: str) -> str:
    """'60 Island St Suite 200' and '60 Island Street' -> '60 island street'."""
    street = _street_of(address).split(",")[0].lower()
    street = re.sub(r"\bst\b\.?", "street", street)
    street = re.sub(r"\bave\b\.?", "avenue", street)
    return re.sub(r"\s+", " ", street).strip()


def _shared_buildings(rows: pd.DataFrame, meters: float = 25.0) -> list[list]:
    """Groups of orgs whose pins sit on top of each other.

    Everett Mills (15 Union Street) holds seven of these organisations and the
    Island Street complex seven more. Drawn exactly on top of each other, only
    the topmost pin could ever be clicked, so each group is fanned out side by
    side (see _fan_offsets) while every pin keeps its own location.

    Two orgs join a group when their pins are within `meters`, or when they
    give the same street address and are within 100 m (one address geocoded
    twice, e.g. Neighbors in Need 50 m from its 60 Island Street neighbours).
    Distance alone is not stretched further: at 60 m it starts merging separate
    buildings on the same corner. Only groups of two or more are returned, as
    lists of row indices.
    """
    pts = [
        (idx, float(r["Latitude"]), float(r["Longitude"]),
         _street_key(r["Address"]), str(r["City"]).strip().lower())
        for idx, r in rows.iterrows()
        if pd.notna(r["Latitude"]) and pd.notna(r["Longitude"])
    ]
    parent = {p[0]: p[0] for p in pts}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, (ia, la, oa, sa, ca) in enumerate(pts):
        for ib, lb, ob, sb, cb in pts[i + 1:]:
            dy = (la - lb) * 111_000
            dx = (oa - ob) * 111_000 * 0.735  # cos(42.7°)
            d2 = dx * dx + dy * dy
            same_street = bool(sa) and sa == sb and ca == cb
            if d2 <= meters * meters or (same_street and d2 <= 100 * 100):
                parent[find(ia)] = find(ib)

    groups: dict = {}
    for idx, *_rest in pts:
        groups.setdefault(find(idx), []).append(idx)
    return [g for g in groups.values() if len(g) > 1]


def _fan_offsets(n: int, step: int = 20) -> list[int]:
    """Horizontal pixel offsets that set n pins side by side, centred.

    Applied through the icon anchor, so each pin still sits at its org's real
    coordinates and stays put relative to the street at every zoom level.
    """
    return [round((i - (n - 1) / 2) * step) for i in range(n)]


class _MapScript(MacroElement):
    """Raw JavaScript emitted as a child of the map.

    Not figure.script: streamlit-folium rebuilds the page from the map's own
    children and drops the figure-level script block, so code parked there
    never reaches the browser. As a map child it is carried along, and it
    renders after every marker and layer added before it.
    """

    _template = Template("{% macro script(this, kwargs) %}{{ this.js }}{% endmacro %}")

    def __init__(self, js: str):
        super().__init__()
        self._name = "GwiFocus"
        self.js = js


def _site_toggle_js(m: folium.Map, registry: list, all_markers: list) -> str:
    """Focus one organisation on click; restore the whole map on close.

    Opening an org that runs several locations does three things: its other
    locations appear as numbered pins joined to it by dashed lines, every other
    organisation fades out, and a caption at the foot of the map names what is
    being shown, with a "Show all" button. Focus stays on after the popup is
    closed, so the locations can be seen without the popup covering them. The
    popups' list rows and ‹ › buttons call gwiGo/gwiHi below.

    Returns raw JavaScript, not a <script> element: _MapScript places it inside
    the map's own <script> block, and a nested tag there is a syntax error that
    kills the map silently.

    Startup is a poll rather than a load listener. streamlit-folium injects the
    map with innerHTML and re-runs the scripts by hand, so the host page's load
    event has already fired by the time this exists — listening for it meant
    this code never ran. Polling also removes the ordering problem: folium
    renders this element *before* the layers it references, because the map only
    appends its own JS during render.
    """
    entries, hooks = [], []
    for key, marker, layer, bounds, org_name, site_markers in registry:
        pts = ",".join(f"[{lat},{lon}]" for lat, lon in bounds)
        pins = ",".join(sm.get_name() for sm in site_markers)
        entries.append(
            f'"{key}":{{g:{layer.get_name()},b:[{pts}],m:{marker.get_name()},'
            f"p:[{pins}],n:{len(site_markers) + 1},t:{json.dumps(org_name)}}}"
        )
        hooks.append(f'{marker.get_name()}._gwiOrg="{key}";')
        hooks.append(
            f"{layer.get_name()}.eachLayer(function(l){{l._gwiSat=\"{key}\";}});"
        )

    pins = ",".join(mk.get_name() for mk in all_markers)
    labels = json.dumps({"banner": _("focus_banner"), "all": _("focus_show_all")})

    return (
        "(function(){\n"
        "var tries=0;\n"
        f"var LABEL={labels};\n"
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

// Caption naming what is focused, with a way out. Pins appearing while others
// vanish is easy to miss, especially for an org whose sites span three towns.
var note = document.createElement('div');
note.className = 'gwi-focusnote';
note.innerHTML = '<span class="gwi-fn-t"></span>'
  + '<button type="button" class="gwi-fn-x">' + LABEL.all + '</button>';
map.getContainer().appendChild(note);
L.DomEvent.disableClickPropagation(note);
note.querySelector('button').addEventListener('click', function () {
  map.closePopup();
  restore();
});

function paint(focusMarker) {
  PINS.forEach(function (pin) {
    if (!pin._icon) return;
    if (focusMarker && pin !== focusMarker && pin.closeTooltip) pin.closeTooltip();
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

function focus(key, frame) {
  var s = SITES[key];
  if (!s) { restore(); return; }
  if (current !== s) {
    if (current && map.hasLayer(current.g)) map.removeLayer(current.g);
    s.g.addTo(map);
    current = s;
  }
  paint(s.m);
  note.querySelector('.gwi-fn-t').textContent =
    LABEL.banner.replace('{org}', s.t).replace('{n}', s.n);
  note.classList.add('is-on');
  if (frame && s.b.length) setTimeout(function () { frameAll(s, frame); }, 60);
}

// Bring every location into the part of the map the popup is not covering.
// The popup opens above the pin and is most of the map's height, so a plain
// fitBounds put half the locations underneath it. The popup's measured height
// becomes the top padding; the map only moves if something is actually hidden.
function frameAll(s, popup) {
  var el = popup && popup._container;
  var top = (el ? el.offsetHeight : 300) + 56;
  var size = map.getSize();
  var hidden = s.b.some(function (ll) {
    var pt = map.latLngToContainerPoint(ll);
    return pt.x < 30 || pt.x > size.x - 30 || pt.y < top || pt.y > size.y - 50;
  });
  if (!hidden) return;
  map.fitBounds(L.latLngBounds(s.b), {
    paddingTopLeft: [40, Math.min(top, Math.round(size.y * 0.55))],
    paddingBottomRight: [40, 50],
    maxZoom: 15,
    animate: true,
  });
}

// Called from popup HTML. i is a 0-based index into the numbered pins; -1 is
// the organisation's own pin. Wraps, so ‹ › cycle through every location.
window.gwiGo = function (key, i) {
  var s = SITES[key];
  if (!s) return;
  focus(key, false);
  var n = s.p.length;
  var target = i < 0 ? s.m : s.p[((i % n) + n) % n];
  if (!target) return;
  if (target._icon) target._icon.classList.remove('gwi-hi');
  target.openPopup();
};

// Lift the pin a list row points at, so the eye can find it.
window.gwiHi = function (key, i, on) {
  var s = SITES[key];
  var t = s && s.p[i];
  if (t && t._icon) t._icon.classList.toggle('gwi-hi', !!on);
};

var popupOpen = false;

map.on('popupopen', function (e) {
  popupOpen = true;
  var src = e.popup._source;
  if (!src) return;
  if (src._icon) src._icon.classList.add('gwi-open');
  // The hover card would sit over the list. On touch screens Leaflet opens it
  // on tap, after this handler, so close it again on the next tick too.
  if (src.closeTooltip) {
    src.closeTooltip();
    setTimeout(function () { src.closeTooltip(); }, 0);
  }
  if (src._gwiOrg) focus(src._gwiOrg, e.popup);
  else if (src._gwiSat) focus(src._gwiSat, null); // one of the focused org's sites
  else restore();                                  // an org with no other locations
});

// Focus outlives the popup on purpose. The popup covers part of the map, so
// closing it (its x, or Esc) is how you look at all of the locations at once.
// Leaving focus is explicit: "Show all", a click on empty map, or Esc again.
map.on('click', restore);

// With the popup gone the whole map is free, so spread the locations over it.
// Deferred a tick: stepping from the org to one of its locations closes one
// popup and opens the next, and that handover must not trigger a reframe.
map.on('popupclose', function (e) {
  popupOpen = false;
  var src = e.popup && e.popup._source;
  if (src && src._icon) src._icon.classList.remove('gwi-open');
  setTimeout(function () {
    if (popupOpen || !current || !current.b.length) return;
    current.m.closeTooltip();   // a tap can leave it stuck open over the pins
    map.fitBounds(L.latLngBounds(current.b), {
      paddingTopLeft: [50, 50],
      paddingBottomRight: [50, 70],
      maxZoom: 15,
      animate: true,
    });
  }, 0);
});
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape' && current && !document.querySelector('.leaflet-popup')) restore();
});
}
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


def _site_popup_html(org_name: str, key: str, e: dict, n_pins: int) -> str:
    """Popup for one numbered location.

    Every programme in the building is shown — a health-centre site's clinic and
    pharmacy keep separate hours, and showing only the first hid the second.
    The header links back to the organisation; ‹ › step to the next location
    without returning to the list.
    """
    locs, head = e["locs"], e["locs"][0]
    i = e["num"] - 1
    title = _group_title(locs)
    addr = _addr_line(head)

    if len(locs) == 1:
        hours = head.get("Hours", "").strip()
        body = (
            _status_badge_html(_schedules(hours))
            + f'<div class="gwi-today">{_hours_today_html(hours)}</div>'
            if hours
            else f'<div class="gwi-muted gwi-small"><i>{_("status_unknown")}</i></div>'
        )
        svc = head.get("ServicesHere", "").strip()
        if svc:
            body += _chips_html([svc])
    else:
        body = ""
        for loc in locs:
            h = loc.get("Hours", "").strip()
            svc = loc.get("ServicesHere", "").strip()
            label = _program_label(loc)
            body += (
                f'<div class="gwi-prog"><div class="gwi-prog-t">{label}'
                + (
                    f'<span class="gwi-muted"> — {svc}</span>'
                    if svc and svc.lower() != label.lower()
                    else ""
                )
                + "</div>"
                + (
                    _status_badge_html(_schedules(h))
                    + f"<details><summary>{_('popup_hours')}</summary>"
                    f"<div>{_hours_html(h)}</div></details>"
                    if h
                    else f'<div class="gwi-muted gwi-small"><i>{_("status_unknown")}</i></div>'
                )
                + "</div>"
            )

    body += _info_line(addr) if addr else ""
    phones = list(dict.fromkeys(l.get("Phone", "").strip() for l in locs if l.get("Phone", "").strip()))
    for p in phones:
        body += _info_line(_tel_link(p))

    nav = ""
    if n_pins > 1:
        nav = (
            f'<span class="gwi-nav">'
            f'<button type="button" onclick="gwiGo(\'{key}\',{i - 1})" aria-label="Previous">‹</button>'
            f"<span>{e['num']} / {n_pins}</span>"
            f'<button type="button" onclick="gwiGo(\'{key}\',{i + 1})" aria-label="Next">›</button>'
            f"</span>"
        )
    foot = (
        f'<a class="gwi-btn" '
        f'href="{_directions_url_address(head)}" target="_blank">{_("get_directions")}</a>'
        if addr
        else f'<span class="gwi-muted gwi-small">{_("loc_no_address")}</span>'
    ) + nav

    return (
        f'<div class="gwi-pop gwi-pop--site">'
        f'<div class="gwi-pop-hd"><div class="gwi-hd-row">'
        f'<span class="gwi-num gwi-num--hd">{e["num"]}</span><span>{title}</span></div>'
        f'<a class="gwi-pop-sub gwi-back" onclick="gwiGo(\'{key}\',-1)">'
        f"{_('back_to').format(org=org_name)}</a></div>"
        f'<div class="gwi-pop-bd">{body}</div>'
        f'<div class="gwi-pop-ft">{foot}</div>'
        f"</div>"
    )


def _site_layer(
    m: folium.Map,
    org_row,
    plan: list[dict],
    key: str,
) -> tuple[folium.FeatureGroup | None, list[list[float]], list[folium.Marker]]:
    """A hidden layer of an org's other locations, shown when its pin is opened.

    Drawing every org's locations all the time would bury the other 57 orgs, so
    each org's sit in their own layer that stays off until asked for. Each
    location is tied back to the org's own pin by a faint dashed line — the
    quickest way to show "these belong together" without a legend.

    Returns the layer, the points it covers (the org's own pin included) so the
    caller can frame them, and the numbered markers in order.
    """
    pinned = [e for e in plan if e["num"]]
    if not pinned:
        return None, [], []

    layer = folium.FeatureGroup(name=f"sites::{org_row['Name']}", control=False)
    try:
        origin = [float(org_row["Latitude"]), float(org_row["Longitude"])]
    except (TypeError, ValueError):
        origin = None
    bounds: list[list[float]] = [origin] if origin else []

    # Lines first, so the pins sit on top of them.
    if origin:
        for e in pinned:
            folium.PolyLine(
                [origin, list(e["coords"])],
                color=BRAND_MED,
                weight=2,
                opacity=0.55,
                dash_array="5 6",
                interactive=False,
            ).add_to(layer)

    markers = []
    for e in pinned:
        lat, lon = e["coords"]
        bounds.append([lat, lon])
        head = e["locs"][0]
        title = _group_title(e["locs"])
        svc = head.get("ServicesHere", "").strip() if len(e["locs"]) == 1 else ""
        mk = folium.Marker(
            location=[lat, lon],
            tooltip=folium.Tooltip(
                f'<div class="gwi-tt"><b>{e["num"]} · {title}</b>'
                + (f"<span>{svc}</span>" if svc else "")
                + "</div>"
            ),
            popup=folium.Popup(
                _site_popup_html(org_row["Name"], key, e, len(pinned)), max_width=300
            ),
            icon=folium.DivIcon(
                html=_pin_svg(BRAND_MED, fade=True, number=str(e["num"])),
                icon_size=(25, 41),
                icon_anchor=(12, 41),
                popup_anchor=(0, -38),
            ),
        )
        mk.add_to(layer)
        markers.append(mk)

    layer.add_to(m)
    return layer, bounds, markers


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
OPEN_GREEN = RIVER
OPEN_GREEN_BG = "#e2efec"
CLOSED_RED = "#6b4a1f"
CLOSED_RED_BG = "#f4ece0"


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
        f"<span>{word}</span>"
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


# ── filter callbacks ──────────────────────────────────────────────────────────
def _reset_filters():
    st.session_state["search"] = ""
    st.session_state["sel_cats"] = []
    st.session_state["sel_svcs"] = []
    st.session_state["sel_org_type_label"] = _("orgtype_all")
    st.session_state["open_now"] = False


def _apply_quick_filter(tags: list[str]) -> None:
    """Jump the map to one common need.

    Runs as a button `on_click` callback: callbacks run before the rerun, so
    `sel_svcs` can be set before the sidebar multiselect that owns it is drawn.

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


# ── page header ───────────────────────────────────────────────────────────────
title_col, lang_col = st.columns([8, 3])
with title_col:
    # Feedback and the AI assistant are secondary tools, so they sit under the
    # subtitle as quiet links instead of competing with the title. With
    # FEEDBACK_FORM_URL unset, feedback still renders but points nowhere and
    # says so on hover.
    if FEEDBACK_FORM_URL:
        feedback_attrs = (
            f'href="{FEEDBACK_FORM_URL}" target="_blank" rel="noopener noreferrer"'
        )
        feedback_extra = ""
    else:
        feedback_attrs = f'href="#" title="{_("feedback_unset")}"'
        feedback_extra = "opacity:.6;cursor:not-allowed;"

    st.markdown(
        f"<h1 class='gwi-h1'>{_('page_heading')}</h1>"
        f"<p class='gwi-sub'>{_('page_sub').format(n=len(df))}</p>"
        f'<div class="gwi-links">'
        f'<a href="{GEMINI_GEM_URL}" target="_blank" rel="noopener noreferrer" '
        f'title="{_("nav_cta_help")}" class="gwi-link gwi-link--strong">'
        f'{_("nav_cta")} &#8599;</a>'
        f'<a {feedback_attrs} class="gwi-link" style="{feedback_extra}">'
        f"{_('feedback_cta')}</a></div>",
        unsafe_allow_html=True,
    )
with lang_col:
    lang_choice = st.segmented_control(
        "Language / Idioma",
        options=["en", "es"],
        format_func=lambda k: "English" if k == "en" else "Español",
        default=st.session_state["lang"],
        label_visibility="collapsed",
        key="lang_radio",
    )
    # Clicking the selected option deselects it; treat that as "no change".
    if lang_choice and lang_choice != st.session_state["lang"]:
        st.session_state["lang"] = lang_choice
        st.rerun()


# ── main filters ──────────────────────────────────────────────────────────────
# Search, category and "open now" live on the page rather than in the sidebar:
# the sidebar starts collapsed on phones and is easy to miss on desktop, and
# these three are what nearly everyone reaches for first. The finer controls
# (specific service, organization type) stay in the sidebar.
st.session_state.setdefault("search", "")

# Options come from the data, not from the full taxonomy, so a category no org
# offers is never a dead end.
all_categories = sorted(
    {c for lst in df["Categories"] for c in lst}, key=CATEGORY_ORDER.index
)

# One-tap shortcuts for the five needs people search for most. Only offer a
# chip that leads somewhere: a tag no org carries would empty the map.
available = {t for lst in df["SvcCanonical"] for t in lst}
live_chips = [
    (key, tags) for key, tags in QUICK_FILTERS if available.intersection(tags)
]
current_svcs = st.session_state.get("sel_svcs", [])

# The finder: one card that reads top to bottom as a question and its answers.
# The common needs lead, because most visitors arrive with one of them in mind;
# search and category follow for everything else.
try:
    filters_box = st.container(key="gwi_filters")
except TypeError:
    filters_box = st.container()
with filters_box:
    st.markdown(
        f"<p class='gwi-q'>{_('quick_heading')}</p>", unsafe_allow_html=True
    )
    try:
        chips_box = st.container(key="gwi_chips")
    except TypeError:
        chips_box = st.container()
    with chips_box:
        for col, (key, tags) in zip(st.columns(len(live_chips)), live_chips):
            col.button(
                _(key),
                key=f"quick_{key}",
                use_container_width=True,
                on_click=_apply_quick_filter,
                args=(tags,),
                type="primary" if current_svcs == tags else "secondary",
            )

    c_search, c_cat, c_open = st.columns([5, 4, 2], vertical_alignment="bottom")
    with c_search:
        search = st.text_input(
            _("search_label"), placeholder=_("search_placeholder_main"), key="search"
        )
    with c_cat:
        sel_cats = st.multiselect(
            _("category_label"),
            all_categories,
            key="sel_cats",
            placeholder=_("category_placeholder"),
            format_func=lambda c: category_label(c, st.session_state["lang"]),
        )
    with c_open:
        open_now_only = st.toggle(
            _("filter_open_now"), key="open_now", help=_("filter_open_now_help")
        )


# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        f"<p class='gwi-side-h'>{_('filters_heading')}</p>"
        f"<p style='font-size:14px;color:{TEXT_MID};margin:0 0 16px;'>"
        f"{_('more_filters_hint')}</p>",
        unsafe_allow_html=True,
    )

    in_data = {s for lst in df["SvcCanonical"] for s in lst}

    # Picking a category narrows the service list to that category's services,
    # so this dropdown is a short menu of what is actually inside the area you
    # chose rather than all 76 every time.
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

    st.divider()
    st.button(_("reset_button"), use_container_width=True, on_click=_reset_filters)
    st.caption(_("orgs_total").format(n=len(df)))


# ── apply filters ─────────────────────────────────────────────────────────────
filtered = df.copy()

if search:
    # Stem-aware matching with a small alias list — see search_utils.py.
    # Plain substring matching missed "diapers" against "Diaper Distribution"
    # and treated ESL/ESOL as unrelated.
    # Each org's canonical service tags and categories are searched too, in
    # English and Spanish, so a Spanish speaker typing "comida" or "vivienda"
    # finds what an English speaker finds with "food" or "housing". Accents are
    # folded both ways: phone keyboards often drop them ("ingles", "credito").
    def _row_matches(row) -> bool:
        tags = row["SvcCanonical"]
        blob = " ".join(
            [str(row[c]) for c in ("Name", "ServiceArea", "Services", "City")]
            + list(tags)
            + [TAGS[t][1] for t in tags if t in TAGS]
            + [category_label(c, "es") for c in row["Categories"]]
        )
        return _search_matches(_fold(search), _fold(blob))

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
has_filters = bool(
    search or sel_cats or sel_svcs or sel_org_type != "All" or open_now_only
)

count_col, clear_col = st.columns([6, 1], vertical_alignment="center")
with count_col:
    txt = (
        _("showing_all").format(n=f"<b>{n_total}</b>")
        if n_filtered == n_total
        else _("showing_filtered").format(n=f"<b>{n_filtered}</b>", total=n_total)
    )
    st.markdown(f"<p class='gwi-count'>{txt}</p>", unsafe_allow_html=True)
with clear_col:
    if has_filters:
        st.button(
            _("clear_filters"),
            key="clear_main",
            on_click=_reset_filters,
            type="tertiary",
            use_container_width=True,
        )

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
        m.get_root().header.add_child(
            folium.Element(f'<link rel="stylesheet" href="{FONTS_URL}">' + _popup_css())
        )

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

        # (key, org marker, its hidden site layer, points to frame, name, numbered
        # site markers) — consumed after the loop to emit the JS that toggles them.
        site_registry: list[
            tuple[str, folium.Marker, folium.FeatureGroup, list, str, list]
        ] = []
        # Every org pin, so focus mode knows what to fade.
        all_org_markers: list[folium.Marker] = []

        # Orgs sharing one building each keep their own pin, nudged sideways
        # so none hides another.
        offset_of: dict = {}
        for members in _shared_buildings(map_data):
            ordered = sorted(members, key=lambda i: str(map_data.at[i, "Name"]).lower())
            for idx, dx in zip(ordered, _fan_offsets(len(ordered))):
                offset_of[idx] = dx

        for _idx, row in map_data.iterrows():
            # single pin colour — the category legend is gone from the UI, so
            # per-category colours would have nothing to decode them
            pin_color = BRAND_MED
            org_type = _org_type_label(row["OrgType"])
            url = row["URL"]
            impact = row.get("ImpactReport", "")
            strategic = row.get("StrategicPlan", "")
            phone = str(row.get("Phone", "")).strip()
            hours = str(row.get("Hours", "")).strip()

            # Status leads — it is what the pin is usually clicked for — then
            # where and how to reach them. Icons stand in for the old label
            # column, which spent a third of the popup's width on "Address".
            today_html = _hours_today_html(hours)
            checked = _checked_date(row.get("HoursVerifiedOn", "")) if hours else ""
            body_html = _status_badge_html(_schedules(hours)) + (
                f'<div class="gwi-today">{today_html}</div>' if today_html else ""
            ) + (
                # A wrong "open" is worse than no badge, so say how fresh it is.
                f'<div class="gwi-checked">{_("hours_source").format(d=checked)}</div>'
                if checked
                else ""
            )
            body_html += _info_line(f"{row['Address']}, {row['City']}, {row['State']}")
            if phone:
                body_html += _info_line(_tel_link(phone))

            key = f"s{_idx}"
            plan = _site_plan(row, _group_by_address(_org_locations(row["Name"])))
            site_layer, site_bounds, site_markers = _site_layer(m, row, plan, key)

            # An org with several places leads with them — that list is what
            # the numbered pins are keyed to. Services follow.
            body_html += _locations_html(
                plan, key if site_layer is not None else None, len(site_markers)
            )
            svc_items = _smart_split(row.get("Services", ""))
            if svc_items:
                body_html += (
                    f'<div class="gwi-sec">{_("popup_services")}</div>'
                    + _chips_html(svc_items, keep=3 if plan else 4)
                )

            reports = []
            if str(impact).strip():
                reports.append(f'{_("popup_impact")}: {_link_cell(impact)}')
            if str(strategic).strip():
                reports.append(f'{_("popup_strategic")}: {_link_cell(strategic)}')
            if reports:
                body_html += f'<div class="gwi-reports">{" · ".join(reports)}</div>'

            directions_url = _directions_url(row["Latitude"], row["Longitude"])
            action_html = (
                f'<a class="gwi-btn" href="{directions_url}" target="_blank" '
                f'>{_("get_directions")}</a>'
            )
            if url:
                action_html += (
                    f'<a class="gwi-btn gwi-btn--ghost" href="{url}" target="_blank">'
                    f'{_("visit_website")}</a>'
                )
            else:
                action_html += (
                    f'<span class="gwi-muted gwi-small">{_("no_website_listed")}</span>'
                )

            sub = f'<div class="gwi-pop-sub">{org_type}</div>' if org_type else ""
            dx = offset_of.get(_idx, 0)
            popup_html = (
                f'<div class="gwi-pop">'
                f'<div class="gwi-pop-hd">{row["Name"]}{sub}</div>'
                f'<div class="gwi-pop-bd">{body_html}</div>'
                f'<div class="gwi-pop-ft">{action_html}</div>'
                f"</div>"
            )

            n_places = len(site_markers) + 1
            tooltip_html = f'<div class="gwi-tt"><b>{row["Name"]}</b>' + (
                f'<span>{_("pin_multi_hint").format(n=n_places)}</span>'
                if site_markers
                else ""
            ) + "</div>"

            org_marker = folium.Marker(
                location=[row["Latitude"], row["Longitude"]],
                popup=folium.Popup(popup_html, max_width=360),
                tooltip=folium.Tooltip(tooltip_html),
                icon=folium.DivIcon(
                    html=_pin_svg(pin_color, count=n_places if site_markers else 0),
                    icon_size=(25, 41),
                    icon_anchor=(12 - dx, 41),
                    popup_anchor=(dx, -38),
                    class_name="empty",
                ),
            )
            org_marker.add_to(m)
            all_org_markers.append(org_marker)
            if site_layer is not None:
                site_registry.append(
                    (key, org_marker, site_layer, site_bounds, row["Name"], site_markers)
                )

        folium.LayerControl(collapsed=True).add_to(m)

        if site_registry:
            _MapScript(_site_toggle_js(m, site_registry, all_org_markers)).add_to(m)

        st_folium(m, use_container_width=True, height=620, returned_objects=[])
        st.caption(_("map_caption").format(n=len(map_data)))
        if FEEDBACK_FORM_URL:
            st.caption(f"[{_('missing_org')}]({FEEDBACK_FORM_URL})")
        # The badges are computed once per rerun, so a tab left open overnight
        # would otherwise show yesterday's answer with nothing to say so.
        _as_of = _now_local()
        st.caption(
            _("status_asof").format(
                t=f'{_day_names()[_as_of.weekday()]} {_as_of.strftime("%-I:%M %p")}'
            )
        )
        if not lawrence_geojson:
            st.caption(_("boundary_unavailable"))


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

            st.markdown(
                f'<div style="border-bottom:1px solid {BORDER};padding:12px 0 16px;'
                f'margin-bottom:8px;">'
                f'<h2 class="gwi-detail-h">{row["Name"]}</h2>'
                f'<p style="color:{TEXT_MID};font-size:15px;margin:4px 0 0;">'
                f"{_org_type_label(row['OrgType'])}</p></div>",
                unsafe_allow_html=True,
            )

            def section_label(text):
                st.markdown(
                    f"<p style='font-size:14px;font-weight:600;color:{TEXT_MID};"
                    f"margin:18px 0 4px;'>{text}</p>",
                    unsafe_allow_html=True,
                )

            c1, c2 = st.columns(2)

            with c1:
                section_label(_("sec_location"))
                addr_parts = [row["Address"], row["City"], row["State"], row["Zip"]]
                st.write(", ".join(p for p in addr_parts if p) or _("not_available"))

                # Only collected for a handful of orgs so far — the section is
                # hidden rather than shown empty for the rest.
                detail_phone = str(row.get("Phone", "")).strip()
                if detail_phone:
                    section_label(_("sec_phone"))
                    st.markdown(_tel_link(detail_phone), unsafe_allow_html=True)

                detail_hours_raw = str(row.get("Hours", "")).strip()
                section_label(_("sec_hours"))
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
                    line = _("hours_source").format(d=_checked_date(checked))
                    if src.startswith("http"):
                        line += (
                            f' · <a href="{src}" target="_blank" '
                            f'style="color:{BRAND_MED};">{_("hours_source_link")}</a>'
                        )
                    st.markdown(
                        f"<div style='color:{TEXT_MUTED};font-size:12px;'>{line}</div>",
                        unsafe_allow_html=True,
                    )

                section_label(_("sec_website"))
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

                section_label(_("sec_impact"))
                st.markdown(
                    _link_cell(row.get("ImpactReport", "")), unsafe_allow_html=True
                )

                section_label(_("sec_strategic"))
                st.markdown(
                    _link_cell(row.get("StrategicPlan", "")), unsafe_allow_html=True
                )

            with c2:
                section_label(_("sec_services"))
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
                    section_label(_("sec_locations"))
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
                section_label(_("sec_map"))
                mini = folium.Map(
                    location=[row["Latitude"], row["Longitude"]],
                    zoom_start=15,
                    tiles=None,
                )
                _add_basemap(mini)
                folium.Marker(
                    location=[row["Latitude"], row["Longitude"]],
                    tooltip=row["Name"],
                    icon=folium.DivIcon(
                        html=_pin_svg(BRICK),
                        icon_size=(25, 41),
                        icon_anchor=(12, 41),
                        class_name="empty",
                    ),
                ).add_to(mini)
                st_folium(
                    mini, use_container_width=True, height=300, returned_objects=[]
                )
            else:
                st.info(_("no_map_coords"))