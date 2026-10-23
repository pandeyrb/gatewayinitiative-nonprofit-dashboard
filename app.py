"""
GWI Nonprofit Partner Explorer  —  Streamlit app
Run:  streamlit run app.py
"""

import hashlib
import html
import json
import os
import re
import unicodedata

from datetime import datetime, timedelta
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
    format_minutes,
    format_range,
    open_status,
    parse_hours,
    weekly_grid,
)
import search_utils
from data_source import load_from_supabase, taxonomy_from_code
from search_engine import GeminiMatcher, smart_search, spelling_fixes
from service_taxonomy import (
    canonical_tags,
    org_categories,
    specifics_for,
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
        class_name="gwi-tiles",
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
/* Keep the map credit line small, as Leaflet intends. */
.leaflet-control-attribution{font-size:10px!important;line-height:1.4!important}

.gwi-pop{font-family:__FONT__;width:322px;max-width:84vw;display:flex;
  flex-direction:column;max-height:min(360px,62vh);color:__TEXT_DARK__}
.gwi-pop-hd{flex:0 0 auto;background:#fff;color:__TEXT_DARK__;font-size:17px;
  font-family:__DISPLAY__;font-weight:700;line-height:1.25;padding:13px 14px 10px;border-bottom:1px solid __BORDER__}
/* The only scrolling region. overscroll-behavior stops the page from taking
   over the wheel once this reaches its end. */
/* min-height:0 is load-bearing: a column flex item defaults to min-height:auto,
   which refuses to shrink below its content, so overflow-y would never engage
   and the popup would grow past max-height exactly as before. */
.gwi-pop-bd{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;
  -webkit-overflow-scrolling:touch;scrollbar-width:thin;
  padding:9px 14px 6px;background:#fff}
.gwi-pop-bd::-webkit-scrollbar{width:9px}
.gwi-pop-bd::-webkit-scrollbar-thumb{background:__BORDER_STRONG__;border-radius:5px;
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
.gwi-chip{display:inline-block;background:__TINT__;color:__BRAND_DARK__;
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
.gwi-loc:hover,.gwi-loc:focus{background:__ACCENT_TINT__;outline:none}
.gwi-loc--static{cursor:default}
.gwi-loc--static:hover{background:none}
.gwi-loc-tx{flex:1 1 auto;min-width:0}
.gwi-loc-t{font-size:12px;font-weight:700;color:__TEXT_DARK__;line-height:1.3}
.gwi-loc-m{font-size:11px;color:__TEXT_MID__;margin-top:1px}
.gwi-loc-s{font-size:11px;font-weight:600;margin-top:1px}
.gwi-s-open{color:#1b6b45}
.gwi-s-closed{color:__TEXT_MUTED__}
.gwi-go{flex:0 0 auto;color:__BRAND_MED__;font-size:18px;line-height:1;padding-top:2px}
.gwi-tag{display:inline-block;margin-left:5px;background:__TINT__;color:__TEXT_MUTED__;
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
.gwi-nav button:hover{background:__ACCENT_TINT__}

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
.gwi-focus{filter:drop-shadow(0 0 7px rgba(24,32,44,.35))}
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
        .replace("__BORDER_STRONG__", BORDER_STRONG)
        .replace("__BORDER__", BORDER)
        .replace("__ACCENT_TINT__", ACCENT_TINT)
        .replace("__TINT__", BG_TINT)
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
        class_name="gwi-tiles",
    ).add_to(m)
    folium.TileLayer(
        tiles=_POSITRON_TILES,
        attr=_POSITRON_ATTR,
        name=_("basemap_light_gray"),
        max_native_zoom=16,
        max_zoom=19,
        show=False,
        class_name="gwi-tiles",
    ).add_to(m)
    folium.TileLayer(
        tiles=_STREETS_TILES,
        attr=_STREETS_ATTR,
        name=_("basemap_streets"),
        max_zoom=19,
        show=True,
        class_name="gwi-tiles",
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
        class_name="gwi-tiles",
    ).add_to(m)


# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Lawrence Resource Finder",
    page_icon="🗺️",
    layout="wide",
    # "auto" collapses the sidebar on phones. "expanded" opened it over the whole
    # screen on load, so the first thing a phone user saw was a filter panel.
    initial_sidebar_state="collapsed",
)

# ── design tokens ─────────────────────────────────────────────────────────────
# Three palettes, each one accent on a cool neutral base. All drawn from
# Lawrence: the brick of the mills, the Merrimack, and the city's civic blue.
# River is the default: its teal pins stand out on the Streets basemap, where
# brick pins blend into the orange highways.
# Every text pairing clears WCAG AA (4.5:1) on its page background and on white.
# Pick one with GWI_THEME (and the matching primaryColor in
# .streamlit/config.toml, which colours Streamlit's own toggles and focus rings).
THEMES = {
    "brick": dict(accent="#a8402a", accent_tint="#f8ebe7", accent_tint_hover="#f2dcd4",
                  accent_deep="#7c2f1f", bg="#f5f6f8",
                  ink="#18202c", mid="#4a5566", muted="#647082",
                  border="#e2e6eb", border_strong="#cdd3db", tint="#eceff3"),
    "river": dict(accent="#1c6660", accent_tint="#e4f0ee", accent_tint_hover="#d3e7e3",
                  accent_deep="#124a46", bg="#f4f7f7",
                  ink="#13232a", mid="#44565c", muted="#5c6f75",
                  border="#dde6e7", border_strong="#c7d3d5", tint="#eaf0f0"),
    "civic": dict(accent="#2b58a6", accent_tint="#e8eef8", accent_tint_hover="#d7e2f4",
                  accent_deep="#1d3f7a", bg="#f5f7fa",
                  ink="#16213a", mid="#475569", muted="#607086",
                  border="#e1e6ee", border_strong="#cbd3df", tint="#edf1f6"),
}
_T = THEMES.get(os.environ.get("GWI_THEME", "river"), THEMES["river"])

INK = _T["ink"]
ACCENT = _T["accent"]
ACCENT_TINT = _T["accent_tint"]
ACCENT_TINT_HOVER = _T["accent_tint_hover"]
ACCENT_DEEP = _T["accent_deep"]      # hero background, under white text
PAGE_BG = _T["bg"]

BRAND_DARK = INK
BRAND_MED = ACCENT
TEXT_DARK = INK
TEXT_MID = _T["mid"]
TEXT_MUTED = _T["muted"]
BG_WHITE = "#ffffff"
BG_LIGHT = PAGE_BG
BG_TINT = _T["tint"]
BORDER = _T["border"]
BORDER_STRONG = _T["border_strong"]

# Dark mode. The page follows the phone or computer's own setting, as Apple's
# apps do. Every colour the page's CSS uses is listed here with its dark
# counterpart; _themed() turns each into a CSS variable, so one media query
# flips the whole page. Dark surfaces are teal-tinted greys rather than pure
# black, status colours are Apple's dark-mode system colours, and every text
# pairing still clears 4.5:1. Colours not listed (white text on photos and
# filled buttons) stay as they are; the few that change role in the dark,
# like the filled buttons, are handled in _DARK_EXTRA_CSS.
DARK = {
    PAGE_BG: "#0d1214", BG_WHITE: "#182023", BG_TINT: "#232d30",
    "#f1f5f5": "#1f292c",        # row hover
    "#edf2f2": "#253033",        # hairlines inside the list
    "#e3eaeb": "#2b3639",        # today strip track
    "#cdd6d8": "#52646a",        # today strip spans
    BORDER: "#2a3538", BORDER_STRONG: "#3b4a4f",
    INK: "#ecf1f2", TEXT_MID: "#b8c5c9", TEXT_MUTED: "#98a9ae",
    ACCENT: "#5ec5b9", ACCENT_DEEP: "#86d6cc",
    ACCENT_TINT: "#193532", ACCENT_TINT_HOVER: "#1f423e",
    "#e3f1e8": "#163222", "#1b6b45": "#4fd187",                    # old open badge
    "#fdecea": "#3a1916", "#f1b7ae": "#6e2c25", "#5c1a10": "#ffd8d2",   # crisis box
    "#8f2416": "#ff9a8c", "#7a2d22": "#f3b8ae",
    "#1c7531": "#30d158", "#d70015": "#ff6961", "#b33a00": "#ffa53d",  # status text
    "#5f6066": "#a5adb1", "#34c759": "#30d158", "#ff9500": "#ff9f0a",
    "#e3f6e7": "#15331f", "#fde7e6": "#3d1b1a", "#c4141f": "#ff7b72",  # status tiles
    "#fff0e0": "#3b2911", "#a83700": "#ffb046", "#eef0f2": "#283134",
}
_GLASS = {"rgba(255,255,255,.86)": "glass", "rgba(255,255,255,.88)": "glass"}
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}(?![0-9a-fA-F])")


def _themed(css: str) -> str:
    """Swap each palette colour in `css` (or an inline style) for its variable."""
    css = _HEX_RE.sub(lambda m: f"var(--c-{m[0][1:].lower()})" if m[0].lower() in DARK else m[0], css)
    for k, v in _GLASS.items():
        css = css.replace(k, f"var(--c-{v})")
    return css


def _dark(rules: str) -> str:
    """Dark-mode rules: applied when the device is in dark mode, unless the
    visitor chose light with the sun/moon button, and always when they chose
    dark. The choice is the data-gwi-theme attribute on <html>, set by
    _THEME_JS. Each selector is scoped twice, so `rules` is plain CSS."""
    auto, forced = [], []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", rules, flags=re.S)):
        sels = [x.strip() for x in sel.split(",") if x.strip()]
        def scope(attr):
            return ",".join(f":root{attr}{x[5:]}" if x.startswith(":root") else f":root{attr} {x}" for x in sels)
        auto.append(scope(':not([data-gwi-theme="light"])') + "{" + body + "}")
        forced.append(scope('[data-gwi-theme="dark"]') + "{" + body + "}")
    return "@media (prefers-color-scheme: dark){" + "".join(auto) + "}" + "".join(forced)


DARK = {k.lower(): v for k, v in DARK.items()}
PALETTE_CSS = (
    ":root{" + "".join(f"--c-{k[1:]}:{k};" for k in DARK) + "--c-glass:rgba(255,255,255,.86);color-scheme:light;}"
    + _dark(":root{" + "".join(f"--c-{k[1:]}:{v};" for k, v in DARK.items())
            + "--c-glass:rgba(27,36,39,.82);color-scheme:dark;}")
)

# One sans family throughout; headings get weight and tighter tracking rather
# than a second typeface. Choose with GWI_FONT:
#   atkinson  Atkinson Hyperlegible Next - drawn by the Braille Institute so
#             similar letters (Il1, O0) never get confused. The default: many
#             visitors are older, low-vision, or reading in a second language.
#   public    Public Sans - the US government's civic typeface (USWDS). Plain,
#             sturdy, full Spanish coverage.
#   geist     Geist - a modern geometric sans, crisper and more product-like.
FONTS = {
    "public": ("Public Sans", "Public+Sans:wght@400;500;600;700"),
    "atkinson": ("Atkinson Hyperlegible Next",
                 "Atkinson+Hyperlegible+Next:wght@400;500;600;700"),
    "geist": ("Geist", "Geist:wght@400;500;600;700"),
}
_F = FONTS.get(os.environ.get("GWI_FONT", "atkinson"), FONTS["atkinson"])
FONT_STACK = (
    f"'{_F[0]}', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)
DISPLAY_STACK = FONT_STACK
FONTS_URL = f"https://fonts.googleapis.com/css2?family={_F[1]}&display=swap"

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
        "search_label": "Or search",
        "search_placeholder": "Name, city, or service…",
        "search_placeholder_main": "Search for food, rent help, ESL… or describe what you need",
        "search_ai_note": "Longer searches are matched by Google's AI. Please don't include names or personal details.",
        "search_hint": "Search by an organization's name, a service such as <i>food pantry</i> or <i>ESL classes</i>, or describe your situation in your own words, in English or Spanish. You can also pick a category below.",
        "search_fixed": "Showing results for {q}",
        "ai_searching": "Finding the best matches…",
        "ai_matches_heading": "Best matches for your search",
        "ai_matches_note": "Suggested by AI from each organization's listed services. Confirm details with the organization.",
        "category_label": "Category",
        "category_placeholder": "All categories",
        "orgtype_label": "Organization Type",
        "orgtype_all": "All",
        "page_heading": "Lawrence Resource Finder",
        "showing_all": "Showing all {n} organizations",
        "results_heading": "Top results",
        "results_more": "{n} more on the map and in the directory below.",
        "ai_match": "Why it fits",
        "call": "Call",
        "empty_title": "No matches yet",
        "showing_filtered": "Showing {n} of {total} organizations",
        "nav_cta": "Ask AI assistant",
        "nav_cta_help": "Opens Google Gemini in a new tab. It needs a Google sign-in, and some work and city computers block it.",
        "feedback_cta": "Give feedback",
        "missing_org": "Is an organization missing, or is something here wrong? Tell us →",
        "feedback_unset": "Feedback form link not yet configured",
        "tab_map": "Map & list",
        "tab_directory": "Directory",
        "tab_list": "List",
        "back_to_results": "All results",
        "hours_not_listed": "Hours not listed · call ahead",
        "open_now_count": "{n} open now",
        "legend_open": "Open now ({n})",
        "legend_closed": "Closed now ({n})",
        "legend_unknown": "Hours not listed ({n})",
        "share_one": "Copy these details to share",
        "sort_label": "Sort results",
        "sort_best": "Best match",
        "sort_az": "A–Z",
        "sort_near": "Nearest",
        "sort_open": "Open now",
        "rail_count": "{n} results",
        "rail_count_all": "{n} organizations",
        "sec_who": "Who it's for",
        "sec_cost": "Cost",
        "sec_lang": "Languages",
        "sec_how": "How to get help",
        "miles": "{d} mi away",
        "hours_checked": "Hours checked {d}",
        "show_more": "Show {n} more",
        "near_label": "Sort by distance from",
        "near_placeholder": "Your ZIP code or street address",
        "near_help": "Looked up with OpenStreetMap to measure distance. Nothing you type here is saved.",
        "near_not_found": "Couldn't find that place. Try a ZIP code, like 01841.",
        "share_title": "Share a list",
        "share_pick": "Pick organizations to share",
        "share_placeholder": "Choose one or more",
        "share_help": "Copy the text with the button at its top right, then paste it into a text message or email.",
        "staff_title": "Spreadsheet and download (for staff)",
        "crisis_title": "If you or someone else is in danger, help is available now",
        "crisis_911": "Emergency: call",
        "crisis_988": "988 Suicide &amp; Crisis Lifeline, 24/7, Spanish available: call or text",
        "crisis_ywca": "YWCA domestic violence hotline, 24/7:",
        "crisis_jgcc": "Jeanne Geiger Crisis Center hotline, 24/7:",
        "crisis_note": "These calls are free and confidential. The organizations below can also help.",
        "help_211": "Not finding what you need? Call <b>2-1-1</b> (Mass 211). It's free, open 24/7, and has help in Spanish and other languages.",
        "tab_org_detail": "Organization details",
        "no_results": "No organizations match these filters. Try a different search or clear the filters.",
        "no_coords": "Matching organizations have no coordinates to plot.",
        "map_caption": "{n} organizations on the map. Pick a pin or a name in the list to see its details.",
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
        "status_unknown": "Hours not published. Call ahead",
        "hours_source": "Hours checked {d}",
        "hours_source_link": "source",
        "filter_open_now": "Open now",
        "filter_open_now_help": "Show only organizations open at this moment. Organizations that publish no hours are hidden.",
        "open_now_col": "Open now",
        "status_asof": "Open/closed shown for {t} (Lawrence time)",
        "days_short": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
        "months_short": "Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec",
        "hours_closed_word": "closed",
        "hours_24": "24 hours",
        "st_open": "Open",
        "st_closed": "Closed",
        "st_soon": "Closes soon",
        "st_until": "until {t}",
        "st_opens": "Opens {t}",
        "day_tomorrow": "Tomorrow",
        "view_list": "List",
        "view_map": "Map",
        "view_switch": "Show results as",
        "jump_search": "Search",
        "jump_search_q": "Search: {q}",
        "theme_to_dark": "Switch to dark mode",
        "theme_to_light": "Switch to light mode",
        "hours_today": "Today",
        "hours_full_week": "Full week",
        "hours_other_days": "Other days",
        "focus_banner": "{org} · {n} locations",
        "focus_show_all": "Show all",
        "pin_multi_short": "{n} locations",
        "months_short": "Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec",
        "pin_multi_hint": "{n} locations. Click to see them all",
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
        "org_not_found": "Organization not found. Please try another selection.",
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
        "loc_admin_badge": "Office only. No walk-in services",
        "loc_admin_note": "The pin above is this organization's office. Services are provided at the locations listed here.",
        "loc_admin_note_map": "This pin is the organization's office. Services are at the numbered locations.",
        "loc_directions": "Directions",
        "loc_call": "Call",
        "loc_no_address": "By phone only. No public address listed",
        "quick_heading": "What do you need help with?",
        "quick_food": "Food",
        "quick_housing": "Housing & shelter",
        "quick_health": "Health care",
        "quick_immigration": "Immigration help",
        "quick_jobs": "Jobs & training",
        "clear_filters": "Clear filters",
        "stat_orgs": "local organizations",
        "hero_alt": "The green Duck Bridge over the Merrimack River, with the Ayer Mill clock tower behind it",
        "need_food": "Pantries and free meals",
        "need_housing": "Rent help and shelters",
        "need_health": "Clinics, hospitals and insurance",
        "need_immigration": "Legal help and citizenship",
        "need_jobs": "Job search and training",
        "need_count": "{n} organizations",
        "footer_checked": "Addresses, phone numbers and posted hours were last checked in September 2026.",
        "footer_missing": "Something missing or wrong?",
        "footer_photo": "Photo: The Duck Bridge and Ayer Mill, by PerriAndMe, CC BY-SA 4.0, via Wikimedia Commons.",
        "services_in": "Specific services in {cat}",
        "services_hint": "Choose a category to see its specific services.",
        "back_to": "Back to {org}",
    },
    "es": {
        "search_label": "O busque",
        "search_placeholder": "Nombre, ciudad o servicio…",
        "search_placeholder_main": "Buscar comida, vivienda, inglés… o describa lo que necesita",
        "search_ai_note": "Las búsquedas más largas se procesan con la IA de Google. Por favor, no incluya nombres ni datos personales.",
        "search_hint": "Busque por el nombre de una organización, un servicio como <i>despensa de comida</i> o <i>clases de inglés</i>, o describa su situación con sus propias palabras, en español o inglés. También puede elegir una categoría abajo.",
        "search_fixed": "Mostrando resultados para {q}",
        "ai_searching": "Buscando los mejores resultados…",
        "ai_matches_heading": "Mejores resultados para su búsqueda",
        "ai_matches_note": "Sugerido por IA según los servicios que publica cada organización. Confirme los detalles con la organización.",
        "category_label": "Categoría",
        "category_placeholder": "Todas las categorías",
        "orgtype_label": "Tipo de Organización",
        "orgtype_all": "Todos",
        "page_heading": "Buscador de Recursos de Lawrence",
        "showing_all": "Mostrando las {n} organizaciones",
        "results_heading": "Mejores resultados",
        "results_more": "{n} más en el mapa y el directorio abajo.",
        "ai_match": "Por qué encaja",
        "call": "Llamar",
        "empty_title": "Sin resultados",
        "showing_filtered": "Mostrando {n} de {total} organizaciones",
        "nav_cta": "Preguntar al asistente de IA",
        "nav_cta_help": "Abre Google Gemini en una pestaña nueva. Requiere iniciar sesión con Google, y algunas computadoras del trabajo o de la ciudad lo bloquean.",
        "feedback_cta": "Enviar comentarios",
        "missing_org": "¿Falta una organización o hay algo incorrecto? Avísenos →",
        "feedback_unset": "El enlace del formulario aún no está configurado",
        "tab_map": "Mapa y lista",
        "tab_directory": "Directorio",
        "tab_list": "Lista",
        "back_to_results": "Todos los resultados",
        "hours_not_listed": "Horario no publicado · llame antes",
        "open_now_count": "{n} abiertas ahora",
        "legend_open": "Abiertas ahora ({n})",
        "legend_closed": "Cerradas ahora ({n})",
        "legend_unknown": "Sin horario publicado ({n})",
        "share_one": "Copiar estos datos para compartir",
        "sort_label": "Ordenar resultados",
        "sort_best": "Más relevantes",
        "sort_az": "A–Z",
        "sort_near": "Más cerca",
        "sort_open": "Abiertos ahora",
        "rail_count": "{n} resultados",
        "rail_count_all": "{n} organizaciones",
        "sec_who": "Para quién es",
        "sec_cost": "Costo",
        "sec_lang": "Idiomas",
        "sec_how": "Cómo recibir ayuda",
        "miles": "a {d} mi",
        "hours_checked": "Horario verificado {d}",
        "show_more": "Mostrar {n} más",
        "near_label": "Ordenar por distancia desde",
        "near_placeholder": "Su código postal o dirección",
        "near_help": "Se busca en OpenStreetMap para medir la distancia. No se guarda nada de lo que escriba aquí.",
        "near_not_found": "No encontramos ese lugar. Pruebe con un código postal, como 01841.",
        "share_title": "Compartir una lista",
        "share_pick": "Elija organizaciones para compartir",
        "share_placeholder": "Elija una o más",
        "share_help": "Copie el texto con el botón de arriba a la derecha y péguelo en un mensaje de texto o correo.",
        "staff_title": "Tabla y descarga (para el personal)",
        "crisis_title": "Si usted u otra persona está en peligro, hay ayuda ahora mismo",
        "crisis_911": "Emergencia: llame al",
        "crisis_988": "Línea 988 de Prevención del Suicidio y Crisis, 24/7, en español: llame o envíe un texto al",
        "crisis_ywca": "Línea de violencia doméstica de YWCA, 24/7:",
        "crisis_jgcc": "Línea de Jeanne Geiger Crisis Center, 24/7:",
        "crisis_note": "Estas llamadas son gratuitas y confidenciales. Las organizaciones de abajo también pueden ayudar.",
        "help_211": "¿No encuentra lo que necesita? Llame al <b>2-1-1</b> (Mass 211). Es gratis, atiende 24/7 y ofrece ayuda en español y otros idiomas.",
        "tab_org_detail": "Detalles de la organización",
        "no_results": "Ninguna organización coincide con estos filtros. Pruebe otra búsqueda o borre los filtros.",
        "no_coords": "Las organizaciones encontradas no tienen coordenadas para mostrar en el mapa.",
        "map_caption": "{n} organizaciones en el mapa. Elija un marcador o un nombre de la lista para ver sus detalles.",
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
        "status_unknown": "Horario no publicado. Llame antes de ir",
        "hours_source": "Horario verificado el {d}",
        "hours_source_link": "fuente",
        "filter_open_now": "Abiertos ahora",
        "filter_open_now_help": "Muestra solo las organizaciones abiertas en este momento. Las que no publican horario quedan ocultas.",
        "open_now_col": "Abierto ahora",
        "status_asof": "Abierto/cerrado según las {t} (hora de Lawrence)",
        "days_short": "Lun,Mar,Mié,Jue,Vie,Sáb,Dom",
        "months_short": "ene,feb,mar,abr,may,jun,jul,ago,sep,oct,nov,dic",
        "hours_closed_word": "cerrado",
        "hours_24": "24 horas",
        "st_open": "Abierto",
        "st_closed": "Cerrado",
        "st_soon": "Cierra pronto",
        "st_until": "hasta las {t}",
        "st_opens": "Abre {t}",
        "day_tomorrow": "Mañana",
        "view_list": "Lista",
        "view_map": "Mapa",
        "view_switch": "Ver resultados como",
        "jump_search": "Buscar",
        "jump_search_q": "Buscar: {q}",
        "theme_to_dark": "Cambiar a modo oscuro",
        "theme_to_light": "Cambiar a modo claro",
        "hours_today": "Hoy",
        "hours_full_week": "Semana completa",
        "hours_other_days": "Otros días",
        "focus_banner": "{org} · {n} ubicaciones",
        "focus_show_all": "Mostrar todo",
        "pin_multi_short": "{n} ubicaciones",
        "months_short": "ene,feb,mar,abr,may,jun,jul,ago,sep,oct,nov,dic",
        "pin_multi_hint": "{n} ubicaciones. Haga clic para verlas todas",
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
        "org_not_found": "Organización no encontrada. Intente otra selección.",
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
        "loc_admin_badge": "Solo oficina. No atiende sin cita",
        "loc_admin_note": "El marcador de arriba es la oficina de esta organización. Los servicios se ofrecen en las ubicaciones que aparecen aquí.",
        "loc_admin_note_map": "Este marcador es la oficina de la organización. Los servicios se ofrecen en las ubicaciones numeradas.",
        "loc_directions": "Cómo llegar",
        "loc_call": "Llamar",
        "loc_no_address": "Solo por teléfono. Sin dirección pública",
        "quick_heading": "¿Con qué necesita ayuda?",
        "quick_food": "Comida",
        "quick_housing": "Vivienda y refugio",
        "quick_health": "Atención médica",
        "quick_immigration": "Ayuda de inmigración",
        "quick_jobs": "Empleo y capacitación",
        "clear_filters": "Borrar filtros",
        "stat_orgs": "organizaciones locales",
        "hero_alt": "El puente verde Duck Bridge sobre el río Merrimack, con la torre del reloj de Ayer Mill detrás",
        "need_food": "Despensas y comidas gratis",
        "need_housing": "Ayuda con alquiler y refugios",
        "need_health": "Clínicas, hospitales y seguro médico",
        "need_immigration": "Ayuda legal y ciudadanía",
        "need_jobs": "Empleo y capacitación",
        "need_count": "{n} organizaciones",
        "footer_checked": "Las direcciones, teléfonos y horarios publicados se verificaron por última vez en septiembre de 2026.",
        "footer_missing": "¿Falta algo o hay un error?",
        "footer_photo": "Foto: The Duck Bridge y Ayer Mill, por PerriAndMe, CC BY-SA 4.0, vía Wikimedia Commons.",
        "services_in": "Servicios específicos en {cat}",
        "services_hint": "Elija una categoría para ver sus servicios específicos.",
        "back_to": "Volver a {org}",
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

def _browser_prefers_spanish() -> bool:
    """True when the visitor's browser lists Spanish first. Most of Lawrence
    speaks Spanish at home, and someone in a hurry may never find the switch."""
    try:
        locale = (st.context.locale or "").lower()
        accept = (st.context.headers.get("Accept-Language") or "").lower()
        # Either signal is enough: the page language setting (locale) and the
        # language the browser asks sites for (Accept-Language) can differ.
        return locale.startswith("es") or accept.split(",")[0].strip().startswith("es")
    except Exception:
        return False


if "lang" not in st.session_state:
    # Only the starting language: the English / Español switch still decides after.
    st.session_state["lang"] = "es" if _browser_prefers_spanish() else "en"


def _(key: str) -> str:
    return T[st.session_state["lang"]][key]


def _description(row) -> str:
    """The org's plain-language summary, in Spanish when there is one."""
    if st.session_state["lang"] == "es" and str(row.get("DescriptionEs", "")).strip():
        return str(row["DescriptionEs"]).strip()
    return str(row.get("Description", "")).strip()


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
st.markdown(f"<style>{PALETTE_CSS}</style>", unsafe_allow_html=True)
st.markdown(
    _themed(f"""
<style>
  @import url('{FONTS_URL}');
  html, body, [class*="css"] {{ color:{TEXT_DARK} !important;
    font-family:{FONT_STACK} !important;
    font-size:16px; line-height:1.5; }}
  .stApp {{ background-color:{PAGE_BG}; }}
  .stApp p, .stApp label, .stApp input, .stApp button, .stApp li,
  .stApp textarea {{ font-family:{FONT_STACK}; }}
  header[data-testid="stHeader"] {{ background:transparent; }}
  .stApp a {{ color:{ACCENT}; text-underline-offset:3px; }}

  .block-container {{ padding-top:2.25rem; padding-bottom:3rem; max-width:1240px; }}
  h1,h2,h3,h4,h5,h6 {{ color:{INK} !important; line-height:1.2; }}

  /* ── top bar ── */
  /* Apple-style capsule buttons: one filled (the main action), one tinted. */
  .gwi-links {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
  .gwi-btn {{ display:inline-flex; align-items:center; gap:8px;
    min-height:48px; padding:0 22px; border-radius:999px;
    font-size:16px; font-weight:600; letter-spacing:-.01em;
    text-decoration:none !important; white-space:nowrap;
    transition:transform .15s ease, box-shadow .2s ease, background .2s ease,
      filter .2s ease; }}
  .gwi-btn svg {{ flex:none; }}
  .gwi-btn--fill {{ color:#fff !important;
    background:linear-gradient(180deg, {ACCENT} 0%, {ACCENT_DEEP} 100%);
    box-shadow:0 1px 0 rgba(255,255,255,.18) inset,
      0 8px 20px -10px {ACCENT}; }}
  .gwi-btn--fill:hover {{ filter:brightness(1.08); transform:translateY(-1px);
    box-shadow:0 1px 0 rgba(255,255,255,.18) inset,
      0 12px 24px -10px {ACCENT}; }}
  .gwi-btn--tint {{ color:{ACCENT} !important; background:{ACCENT_TINT}; }}
  .gwi-btn--tint:hover {{ background:{ACCENT_TINT_HOVER}; transform:translateY(-1px); }}
  .gwi-btn:active {{ transform:scale(.97); }}
  .gwi-btn:focus-visible {{ outline:3px solid {ACCENT}; outline-offset:3px; }}
  .st-key-lang_radio {{ display:flex; justify-content:flex-end; }}
  .st-key-gwi_prefs {{ flex-wrap:nowrap !important; }}
  .st-key-gwi_prefs [data-testid="stElementContainer"] {{ width:auto !important; }}
  .st-key-gwi_theme_js {{ position:absolute !important; width:0; height:0; overflow:hidden; opacity:0; pointer-events:none; }}
  /* Light / dark: a round button beside the language switch. */
  .gwi-theme-btn {{ display:inline-flex; align-items:center; justify-content:center; width:44px; height:44px;
    border-radius:50%; border:1px solid {BORDER}; background:{BG_WHITE}; color:{INK}; cursor:pointer;
    transition:background .2s ease, transform .15s ease; padding:0; }}
  .gwi-theme-btn:hover {{ background:{BG_TINT}; }}
  .gwi-theme-btn:active {{ transform:scale(.94); }}
  .gwi-theme-btn:focus-visible {{ outline:3px solid {ACCENT}; outline-offset:2px; }}
  .gwi-theme-btn span {{ display:flex; }}
  .gwi-theme-btn .gwi-ic-sun {{ display:none; }}

  /* ── hero ── */
  .gwi-hero {{ display:grid; grid-template-columns:1.2fr 1fr; min-height:330px;
    border-radius:20px; overflow:hidden; margin:10px 0 0;
    background:{ACCENT_DEEP};
    box-shadow:0 18px 40px -24px rgba(19,35,42,.55); }}
  .gwi-hero-tx {{ display:flex; flex-direction:column; justify-content:space-between;
    gap:28px; padding:44px 44px 96px;
    background:radial-gradient(120% 90% at 0% 0%, {ACCENT} 0%, {ACCENT_DEEP} 70%); }}
  .gwi-h1 {{ font-family:{DISPLAY_STACK} !important; font-weight:700;
    font-size:46px !important; letter-spacing:-.03em; color:#fff !important;
    margin:0 !important; padding:0 !important; line-height:1.05;
    text-wrap:balance; }}
  .gwi-stats {{ display:flex; gap:36px; flex-wrap:wrap; }}
  .gwi-stat b {{ display:block; color:#fff; font-size:30px; font-weight:700;
    line-height:1; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }}
  .gwi-stat span {{ display:block; color:rgba(255,255,255,.85); font-size:14px;
    margin-top:6px; }}
  .gwi-hero-img {{ background-size:cover; background-position:center 32%;
    min-height:260px; }}

  /* ── the finder card ── */
  .st-key-gwi_filters {{
    background:{BG_WHITE}; border:1px solid {BORDER}; border-radius:18px;
    padding:24px 26px 14px; margin:-64px 28px 0; position:relative; z-index:2;
    width:calc(100% - 56px) !important;
    box-shadow:0 20px 40px -24px rgba(19,35,42,.35); }}
  .gwi-q {{ font-size:19px; font-weight:650; letter-spacing:-.01em;
    color:{INK}; margin:0 0 12px; }}
  .gwi-hint-line {{ color:{TEXT_MUTED}; font-size:14px; margin:2px 0 14px; }}

  /* Specific-service pills: quiet until chosen, then the accent. */
  .st-key-gwi_svcs {{ border-top:1px solid {BORDER}; padding-top:12px; margin-top:4px; }}
  .stPills button {{ border-radius:999px !important; border:1px solid {BORDER_STRONG} !important;
    background:{BG_WHITE} !important; min-height:36px;
    transition:background .15s, border-color .15s, transform .1s; }}
  .stPills button p {{ font-size:14px !important; color:{INK} !important; }}
  .stPills button:hover {{ border-color:{ACCENT} !important; }}
  .stPills button[kind="pillsActive"] {{ background:{ACCENT_TINT} !important;
    border-color:{ACCENT} !important; }}
  .stPills button[kind="pillsActive"] p {{ color:{ACCENT} !important; font-weight:600 !important; }}

  /* Press feedback on every button. */
  .stApp button:active {{ transform:translateY(1px); }}
  .stApp button:focus-visible {{ outline:2px solid {ACCENT} !important; outline-offset:2px; }}

  .st-key-gwi_chips {{ padding-bottom:18px; margin-bottom:6px;
    border-bottom:1px solid {BORDER}; }}
  /* Equal-height cards whatever the label length (Spanish runs longer). */
  .st-key-gwi_chips [data-testid="stHorizontalBlock"] {{ align-items:stretch; }}
  .st-key-gwi_chips [data-testid="stColumn"] div:has(button),
  .st-key-gwi_chips button {{ height:100%; }}
  .st-key-gwi_chips button {{
    position:relative; min-height:116px; border-radius:14px !important;
    border:1px solid transparent !important; background:{ACCENT_TINT} !important;
    display:flex !important; flex-direction:column; align-items:flex-start !important;
    justify-content:flex-start !important; text-align:left; padding:36px 16px 14px !important;
    transition:background .2s, transform .15s, box-shadow .2s; }}
  .st-key-gwi_chips button > div, .st-key-gwi_chips button > div > span,
  .st-key-gwi_chips button > div > span > div {{ width:100%; justify-content:flex-start !important; }}
  .st-key-gwi_chips button p {{ font-size:17px !important; font-weight:700 !important;
    color:{INK} !important; text-align:left; }}
  .st-key-gwi_chips button::after {{ display:block; font-size:13px; color:{TEXT_MID};
    margin-top:2px; line-height:1.3; }}
  .st-key-gwi_chips button::before {{ position:absolute; top:12px; left:16px;
    font-size:12px; font-weight:600; color:{ACCENT}; }}
  .st-key-gwi_chips button[kind="secondary"]:hover {{
    background:{ACCENT_TINT_HOVER} !important; transform:translateY(-2px);
    box-shadow:0 10px 20px -14px rgba(19,35,42,.45); }}
  .st-key-gwi_chips button[kind="primary"] {{ background:{ACCENT} !important; }}
  .st-key-gwi_chips button[kind="primary"] p,
  .st-key-gwi_chips button[kind="primary"]::after,
  .st-key-gwi_chips button[kind="primary"]::before {{ color:#fff !important; }}

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
    border-color:{ACCENT} !important; box-shadow:0 0 0 3px {ACCENT_TINT}; }}
  [data-baseweb="select"] * {{ color:{INK}; }}
  .stTextInput input {{ font-size:16px !important; }}
  .stTextInput input::placeholder {{ color:{TEXT_MUTED}; }}

  /* Main search: an Apple-style search field - filled, borderless, rounded,
     with a magnifying glass, lifting to white with a soft ring on focus. */
  .st-key-search .react-aria-TextField > div {{
    background-color:{BG_TINT} !important; border:1px solid transparent !important;
    border-radius:14px !important; min-height:52px;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='%238a8f98' stroke-width='2.2' stroke-linecap='round'%3E%3Ccircle cx='10.5' cy='10.5' r='6.5'/%3E%3Cpath d='M15.5 15.5L20 20'/%3E%3C/svg%3E") !important;
    background-repeat:no-repeat !important; background-position:16px center !important;
    transition:background-color .2s ease, box-shadow .2s ease, border-color .2s ease; }}
  .st-key-search .react-aria-TextField > div:hover {{
    border-color:{BORDER_STRONG} !important; }}
  .st-key-search .react-aria-TextField > div:focus-within {{
    background-color:{BG_WHITE} !important; border-color:{ACCENT} !important;
    box-shadow:0 0 0 4px {ACCENT_TINT}, 0 6px 18px -10px rgba(19,35,42,.35); }}
  .gwi-fixed {{ font-size:15px !important; color:{TEXT_MID}; margin:-4px 0 6px !important; }}
  .gwi-fixed b {{ color:{ACCENT}; font-weight:650; font-style:italic; }}
  .gwi-hint {{ font-size:14px !important; line-height:1.5; color:{TEXT_MID}; margin:-4px 0 4px !important; max-width:72ch; }}
  .gwi-hint i {{ color:{INK}; font-style:normal; font-weight:600; }}
  .st-key-search input {{ background:transparent !important; padding-left:46px !important;
    font-size:17px !important; letter-spacing:-.01em; }}

  /* Selected categories/services. */
  [data-baseweb="tag"], [data-baseweb="tag"] * {{
    background:{ACCENT_TINT} !important; color:{ACCENT} !important; }}
  [data-baseweb="tag"] {{ border-radius:6px !important; font-weight:600 !important; }}
  [data-baseweb="tag"] svg {{ fill:{ACCENT} !important; }}

  /* ── results line + tabs ── */
  .gwi-count {{ color:{TEXT_MID}; font-size:17px; margin:0; letter-spacing:-.01em; }}
  .gwi-count b {{ color:{INK}; font-weight:700; }}
  .st-key-clear_main {{ display:flex; justify-content:flex-end; }}
  .st-key-clear_main button {{ background:{ACCENT_TINT} !important; border-radius:999px !important;
    min-height:36px; padding:0 16px !important; width:auto !important;
    transition:background .2s ease; }}
  .st-key-clear_main button:hover {{ background:{ACCENT_TINT_HOVER} !important; }}
  .st-key-clear_main button p {{ color:{ACCENT} !important; font-weight:600 !important;
    font-size:14px !important; }}

  /* ── search results: Apple-style cards ── */
  .gwi-res-h {{ display:flex; align-items:baseline; justify-content:space-between;
    gap:12px; margin:22px 0 12px; }}
  .gwi-res-t {{ font-family:{DISPLAY_STACK}; font-size:24px; font-weight:700;
    letter-spacing:-.02em; color:{INK}; line-height:1.2; }}
  .gwi-res-h span {{ color:{TEXT_MUTED}; font-size:14px; }}
  .gwi-res {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(330px, 1fr));
    gap:14px; margin-bottom:10px; }}
  .gwi-card {{ background:{BG_WHITE}; border-radius:18px; padding:18px 18px 16px;
    border:1px solid {BORDER}; display:flex; flex-direction:column; gap:10px;
    box-shadow:0 1px 2px rgba(19,35,42,.05), 0 8px 24px -16px rgba(19,35,42,.28);
    transition:transform .2s ease, box-shadow .2s ease; }}
  .gwi-card:hover {{ transform:translateY(-2px);
    box-shadow:0 1px 2px rgba(19,35,42,.06), 0 16px 32px -18px rgba(19,35,42,.38); }}
  .gwi-card-top {{ display:flex; gap:12px; align-items:flex-start; }}
  .gwi-card-n {{ flex:none; width:28px; height:28px; border-radius:50%;
    background:{ACCENT_TINT}; color:{ACCENT}; font-size:13px; font-weight:700;
    display:flex; align-items:center; justify-content:center; margin-top:1px; }}
  .gwi-card-t {{ flex:1; min-width:0; }}
  .gwi-card-name {{ font-size:17px !important; font-weight:650; color:{INK}; line-height:1.3;
    letter-spacing:-.01em; margin:0 !important; }}
  .gwi-card-about {{ font-size:14px !important; line-height:1.45; color:{TEXT_MID};
    margin:0 !important; display:-webkit-box; -webkit-line-clamp:3;
    -webkit-box-orient:vertical; overflow:hidden; }}
  .gwi-detail-about {{ font-size:16px !important; line-height:1.55; color:{INK};
    margin:10px 0 0 !important; max-width:760px; }}
  .gwi-card-meta {{ font-size:13px !important; color:{TEXT_MUTED}; margin:2px 0 0 !important; }}
  .gwi-pill {{ display:inline-flex; align-items:center; gap:6px; font-size:12px;
    font-weight:600; border-radius:999px; padding:3px 10px; width:fit-content; }}
  .gwi-pill::before {{ content:""; width:6px; height:6px; border-radius:50%;
    background:currentColor; }}
  .gwi-pill--open {{ background:#e3f1e8; color:#1b6b45; }}  /* OPEN_GREEN(_BG) */
  .gwi-pill--closed {{ background:{BG_TINT}; color:{TEXT_MID}; }}
  .gwi-pill--unknown {{ background:transparent; color:{TEXT_MUTED}; padding-left:0; }}
  .gwi-pill--unknown::before {{ display:none; }}
  .gwi-why {{ background:{ACCENT_TINT}; border-radius:12px; padding:10px 12px;
    font-size:14px; line-height:1.45; color:{INK}; }}
  .gwi-why b {{ display:flex; align-items:center; gap:5px; color:{ACCENT};
    font-size:12px; font-weight:650; margin-bottom:2px; }}
  .gwi-tags {{ display:flex; flex-wrap:wrap; gap:6px; }}
  .gwi-tags span {{ background:{BG_TINT}; color:{TEXT_MID}; border-radius:8px;
    padding:3px 9px; font-size:12.5px; }}
  .gwi-acts {{ display:flex; gap:8px; margin-top:auto; padding-top:4px; flex-wrap:wrap; }}
  .gwi-act {{ flex:1 1 0; display:inline-flex; align-items:center; justify-content:center;
    gap:6px; min-height:38px; padding:0 12px; border-radius:999px;
    background:{BG_TINT}; color:{ACCENT} !important; font-size:14px; font-weight:600;
    text-decoration:none !important; white-space:nowrap;
    transition:background .2s ease, transform .1s ease; }}
  .gwi-act:hover {{ background:{ACCENT_TINT_HOVER}; }}
  .gwi-act:active {{ transform:scale(.97); }}
  .gwi-act--primary {{ background:{ACCENT}; color:#fff !important; }}
  .gwi-act--primary:hover {{ background:{ACCENT_DEEP}; }}
  .gwi-res-note {{ color:{TEXT_MUTED}; font-size:13px !important; margin:4px 0 18px !important; }}

  /* Empty state: calm and centred, not an alarm. */
  .gwi-empty {{ text-align:center; padding:44px 20px; border-radius:18px;
    background:{BG_WHITE}; border:1px solid {BORDER}; margin:6px 0; }}
  .gwi-empty-ic {{ width:56px; height:56px; border-radius:50%; margin:0 auto 14px;
    background:{BG_TINT}; color:{TEXT_MUTED}; display:flex; align-items:center;
    justify-content:center; }}
  .gwi-empty h4 {{ font-size:19px !important; font-weight:650 !important; margin:0 0 6px !important;
    padding:0 !important; letter-spacing:-.01em; }}
  .gwi-empty p {{ color:{TEXT_MID}; font-size:15px; margin:0 auto; max-width:420px; }}
  .gwi-empty p.gwi-211 {{ margin-top:14px !important; padding-top:14px; border-top:1px solid {BORDER};
    color:{INK}; max-width:460px; }}

  /* Crisis numbers: shown above results when a search sounds like an emergency. */
  .gwi-crisis {{ background:#fdecea; border:1px solid #f1b7ae; border-radius:16px;
    padding:16px 20px 14px; margin:14px 0 6px; color:#5c1a10; }}
  .gwi-crisis-h {{ display:flex; align-items:center; gap:8px; font-weight:700; font-size:17px;
    color:#8f2416; margin-bottom:6px; }}
  .gwi-crisis ul {{ margin:0 0 6px; padding-left:20px; display:grid; gap:4px; }}
  .gwi-crisis li {{ font-size:15.5px; line-height:1.45; }}
  .gwi-crisis a {{ color:#8f2416 !important; font-weight:700; white-space:nowrap;
    font-variant-numeric:tabular-nums; }}
  .gwi-crisis p {{ font-size:13.5px !important; margin:0 !important; color:#7a2d22; }}

  /* Card status line: open/closed pill, then when the hours were last checked. */
  .gwi-status-row {{ display:flex; flex-wrap:wrap; align-items:center; gap:4px 10px; }}
  .gwi-checked {{ font-size:12px; color:{TEXT_MUTED}; }}
  .st-key-near {{ max-width:420px; margin-left:auto; }}
  .st-key-list_more button {{ border-radius:999px !important; }}

  /* Tabs as a segmented bar: the current view is obvious at a glance. */
  [data-testid="stTabs"] [role="tablist"] {{ gap:4px; background:{BG_TINT};
    padding:4px; border-radius:12px; width:fit-content; border:none; }}
  [data-testid="stTab"] {{ border-radius:9px; padding:8px 18px !important;
    height:auto; transition:background .15s; border:none !important; }}
  [data-testid="stTab"] p {{
    font-size:15px !important; font-weight:600 !important; color:{TEXT_MID} !important; }}
  [data-testid="stTab"]:hover p {{ color:{INK} !important; }}
  [data-testid="stTab"] {{ box-shadow:none !important; }}
  [data-testid="stTab"][aria-selected="true"] {{ background:{BG_WHITE};
    box-shadow:0 1px 3px rgba(19,35,42,.14) !important; }}
  [data-testid="stTab"]::before, [data-testid="stTab"]::after,
  [data-testid="stTabs"] [role="tablist"]::before,
  [data-testid="stTabs"] [role="tablist"]::after {{ display:none !important; }}
  [data-testid="stTabs"] .react-aria-SelectionIndicator {{ display:none !important; }}
  [data-testid="stTab"][aria-selected="true"] p {{ color:{INK} !important; }}

  /* ── footer ── */
  .gwi-foot {{ margin-top:48px; padding-top:20px; border-top:1px solid {BORDER};
    color:{TEXT_MUTED}; font-size:14px; }}
  .gwi-foot p {{ margin:0 0 6px; }}
  .gwi-foot-credit, .gwi-foot-credit a {{ font-size:12px; color:{TEXT_MUTED} !important; }}

  /* ── detail tab ── */
  .gwi-detail-h {{ font-family:{DISPLAY_STACK} !important; font-weight:700 !important;
    font-size:28px !important; letter-spacing:-.02em; margin:0 !important; padding:0 !important; }}
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

  /* Phones: the photo goes on top as a banner, the finder stops overlapping
     sideways, and the five needs become a row of cards that scrolls. */
  @media (max-width: 640px) {{
    .block-container {{ padding-top:1rem; }}
    .st-key-lang_radio {{ justify-content:flex-start; }}
    .gwi-links {{ gap:10px; }}
    .gwi-res {{ grid-template-columns:1fr; }}
    .gwi-btn {{ flex:1 1 auto; justify-content:center; min-height:46px;
      padding:0 18px; font-size:15px; }}
    .gwi-hero {{ grid-template-columns:1fr; min-height:0; border-radius:16px; }}
    .gwi-hero-img {{ order:-1; min-height:72px; background-position:center 34%; }}
    .gwi-hero-tx {{ padding:16px 20px 64px; gap:6px; }}
    .gwi-h1 {{ font-size:25px !important; }}
    .gwi-stats {{ gap:22px; }}
    .gwi-stat {{ display:flex; align-items:baseline; gap:6px; }}
    .gwi-stat b {{ font-size:17px; }}
    .gwi-stat span {{ font-size:13px; margin-top:0; }}
    .st-key-gwi_filters {{ margin:-52px 10px 0; padding:18px 16px 8px;
      width:calc(100% - 20px) !important; }}
    .gwi-q {{ font-size:17px; }}
    .st-key-gwi_chips [data-testid="stHorizontalBlock"] {{
      flex-direction:row !important; flex-wrap:nowrap !important;
      overflow-x:auto; gap:10px !important; padding-bottom:4px;
      scrollbar-width:none; scroll-snap-type:x mandatory; }}
    .st-key-gwi_chips [data-testid="stColumn"],
    .st-key-gwi_chips [data-testid="column"] {{
      flex:0 0 72% !important; width:72% !important; min-width:0 !important;
      scroll-snap-align:start; }}
    [data-testid="stTabs"] [data-baseweb="tab-list"] {{ width:100%; }}
  }}

  @media (prefers-reduced-motion: reduce) {{
    .stApp * {{ transition:none !important; }}
  }}
</style>
"""),
    unsafe_allow_html=True,
)

# Same rules the map popups use. The organisation-detail tab renders the same
# badge and hours markup, but it lives in the Streamlit document rather than in
# the map's iframe, so it needs its own copy of the stylesheet.
st.markdown(f"<style>{_themed(_hours_css())}</style>", unsafe_allow_html=True)


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

    # Categories and specific services both come from the categorization draft,
    # via data/specific_services.csv, not from the canonical tags. The draft is
    # the authority on which category a service belongs to, and it assigns
    # several services to more than one.
    df["Categories"] = df.apply(
        lambda r: org_categories(r["SvcTagList"] or r["SvcList"]), axis=1
    )
    df["Specifics"] = df.apply(
        lambda r: specifics_for(r["SvcTagList"] or r["SvcList"]), axis=1
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


# ── data source ───────────────────────────────────────────────────────────────
# DATA_SOURCE in .streamlit/secrets.toml picks where the data comes from:
# "supabase" (the database) or "csv" (the files in data/, the old way).
# With no secrets file at all, the CSVs are used.
try:
    DATA_SOURCE = str(st.secrets.get("DATA_SOURCE", "csv")).lower()
except Exception:
    DATA_SOURCE = "csv"

if DATA_SOURCE == "supabase":
    try:
        df, locations, tax = load_from_supabase()
    except Exception as exc:
        st.error(
            "**Couldn't load the directory from the database.** "
            "Try again in a minute; if it keeps happening, check the Supabase project."
        )
        st.caption(f"{type(exc).__name__}: {exc}")
        st.stop()
    df = df.copy()  # the cached frame is shared between reruns; don't edit it in place
    df["Phone"] = df["Phone"].apply(_format_phone)
else:
    df = load_data(CSV_PATH)
    locations = load_locations(LOCATIONS_PATH)
    tax = taxonomy_from_code()
    if df.empty:
        st.error(
            f"**Data file not found:** `{CSV_PATH}`\n\nMake sure `{CSV_PATH}` is next to `app.py`."
        )
        st.stop()

# The rest of the app uses these names; they now come from whichever source ran.
CATEGORY_ORDER = tax.category_order
SPECIFIC_ORDER = tax.specific_order
QUICK_FILTERS = tax.quick_filters
# search_utils reads its alias list at search time, so swapping it here makes
# synonyms edited in the database take effect.
search_utils.ALIAS_GROUPS = tax.alias_groups


def category_label(category: str, lang: str = "en") -> str:
    return tax.category_es.get(category, category) if lang == "es" else category


def specific_label(category: str, specific: str, lang: str = "en") -> str:
    if lang == "es":
        return tax.specific_es.get((category, specific), specific)
    return specific


@st.cache_resource
def _ai_matcher():
    """The AI search layer, or None to use keyword search only.

    Live site: credentials come from a [gcp_service_account] table in
    Streamlit secrets. Locally: from .env, like the crawler. Set
    AI_SEARCH = false in secrets to turn it off.
    """
    try:
        secrets = st.secrets
        if str(secrets.get("AI_SEARCH", "true")).lower() == "false":
            return None
        sa = secrets.get("gcp_service_account")
        model = secrets.get("GEMINI_MODEL")
        location = secrets.get("GEMINI_LOCATION")
    except Exception:
        sa = model = location = None
    try:
        if sa:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(
                dict(sa), scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            return GeminiMatcher(credentials=creds, project=sa["project_id"],
                                 model=model, location=location)
        return GeminiMatcher(model=model, location=location)
    except Exception:
        return None


ai_matcher = _ai_matcher()


def _quick_text(key: str, part: str) -> str:
    """A quick-filter button's title ("label") or one-line description
    ("desc"). From the database when it has them; otherwise from T."""
    labels = tax.quick_labels.get(key)
    if labels:
        en, es, d_en, d_es = labels
        if part == "label":
            return es if st.session_state["lang"] == "es" else en
        return (d_es if st.session_state["lang"] == "es" else d_en) or ""
    return _(key) if part == "label" else _("need_" + key[6:])


# ── helpers ───────────────────────────────────────────────────────────────────
def _link_cell(val: str) -> str:  # New Change for v5
    if val and str(val).strip():
        v = str(val).strip()
        href = v if v.startswith("http") else f"https://{v}"
        return (
            f'<a href="{href}" target="_blank" '
            f'style="color:{_themed(BRAND_MED)};text-decoration:underline;">{_("view_link")}</a>'
        )
    return f'<span style="color:{_themed(TEXT_MUTED)};">{_("not_available_short")}</span>'


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
    state, detail, _label = open_status(scheds, _now_local(), _day_names(), _month_names())
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
                        f': {loc["ServicesHere"].strip()}'
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
                    f'<span class="gwi-muted">: {svc}</span>'
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
        return f'<span style="color:{_themed(TEXT_MID)};">{text}</span>'
    return (
        f'<a href="tel:{digits}" '
        f'style="color:{_themed(BRAND_MED)};text-decoration:none;font-weight:600;">{text}</a>'
    )


def _hours_lines(hours: str) -> list[str]:
    """Split an Hours string into its per-line segments.

    Hours are authored semicolon-separated ("Mon 9 AM–5 PM; Sun closed") so
    each segment can render on its own line instead of one long run-on.
    """
    return [seg.strip() for seg in str(hours).split(";") if seg.strip()]


# ── opening hours ─────────────────────────────────────────────────────────────
OPEN_GREEN = "#1b6b45"
OPEN_GREEN_BG = "#e3f1e8"
CLOSED_RED = TEXT_MID
CLOSED_RED_BG = BG_TINT


def _now_local() -> datetime:
    """Right now in Lawrence.

    Explicitly zoned: this app is normally served from a container running UTC,
    and a naive now() would put every open/closed badge four or five hours out.
    """
    return datetime.now(LAWRENCE_TZ)


def _day_names() -> list[str]:
    return _("days_short").split(",")


def _month_names() -> list[str]:
    return _("months_short").split(",")


def _schedules(hours_text: str) -> list[Schedule]:
    return parse_hours(str(hours_text or ""))


def _status_badge_html(scheds: list[Schedule], compact: bool = False) -> str:
    """The OPEN NOW / CLOSED pill.

    Orgs that publish nothing get a "call ahead" line rather than a grey
    "unknown" badge — the honest reading of a missing value here is that we do
    not know, and telling someone to phone first is more useful than a shrug.
    """
    state, detail, label = open_status(scheds, _now_local(), _day_names(), _month_names())
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
        f'<span class="gwi-badge" style="{_themed(f"background:{bg};color:{fg};")}">'
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
# Specific services are chosen per category: each open category draws its own
# set of pills under the key "svc::<category>", and the selected services are
# the union of those. See the finder below.
SVC_KEY = "svc::"


def _svc_keys() -> list[str]:
    return [k for k in st.session_state if str(k).startswith(SVC_KEY)]


def _reset_filters():
    st.session_state["search"] = ""
    st.session_state["sel_cats"] = []
    for k in _svc_keys():
        st.session_state[k] = []
    st.session_state["sel_org_type_label"] = _("orgtype_all")
    st.session_state["open_now"] = False


def _selected_specifics() -> list[tuple[str, str]]:
    """Every (category, specific service) currently picked, in a stable order."""
    return sorted(
        (k[len(SVC_KEY):], s)
        for k in _svc_keys()
        for s in (st.session_state.get(k) or [])
    )


def _apply_quick_filter(pairs: list[tuple[str, str]]) -> None:
    """Jump to one common need, and show what it means.

    Runs as a button `on_click` callback, so it can set widget state before
    the widgets are drawn. It opens the categories the chip's services live in
    and pre-selects those services as pills, so the person sees exactly which
    services "Food" covers and can narrow it further.

    Toggles: pressing the active chip again clears the filter, which matches
    what its pressed state implies.
    """
    active = _selected_specifics()
    for k in _svc_keys():
        st.session_state[k] = []
    if active == sorted(pairs):
        st.session_state["sel_cats"] = []
        return
    cats = sorted({c for c, _s in pairs}, key=CATEGORY_ORDER.index)
    st.session_state["sel_cats"] = cats
    for c in cats:
        st.session_state[SVC_KEY + c] = [s for cc, s in pairs if cc == c]


# ── page header ───────────────────────────────────────────────────────────────
# Top bar: the secondary tools on the left, the language switch on the right.
# With FEEDBACK_FORM_URL unset, feedback still renders at full strength but
# points nowhere and says so on hover.
if FEEDBACK_FORM_URL:
    feedback_attrs = (
        f'href="{FEEDBACK_FORM_URL}" target="_blank" rel="noopener noreferrer"'
    )
    feedback_extra = ""
else:
    feedback_attrs = f'href="#" title="{_("feedback_unset")}"'
    feedback_extra = "cursor:not-allowed;"

# SF Symbols-style line icons; stroke follows the button's text colour.
_SVG = ('<svg aria-hidden="true" width="{w}" height="{w}" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round">{d}</svg>')
_ICON_SPARKLE = _SVG.format(w=18, d=(
    '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>'
    '<path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>'))
_ICON_ARROW = _SVG.format(w=14, d='<path d="M7 17L17 7M8 7h9v9"/>')
_ICON_BUBBLE = _SVG.format(w=18, d=(
    '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>'))

# Light / dark. The page follows the device until the visitor taps the
# sun/moon button; then their choice is kept in this browser (localStorage).
# Runs in the page itself (st.iframe content is same-origin) so it can flip
# everything at once without a rerun:
#  - our CSS, via data-gwi-theme on <html> (see _dark), here and in the maps'
#    iframes, which it keeps in step;
#  - Streamlit's own widgets: its automatic theme asks matchMedia whether the
#    device prefers dark and asks again on an "afterprint" event, so the
#    answer is made to follow the visitor's choice and that event is fired.
_THEME_JS = """<script>
(function () {
  var P = window.parent;
  if (!P || P.__gwiTheme) return;
  P.__gwiTheme = true;
  function install() {
    var KEY = "gwi-theme", root = document.documentElement;
    var realMM = window.matchMedia.bind(window);
    var system = realMM("(prefers-color-scheme: dark)");
    function chosen() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
    function isDark() { var c = chosen(); return c ? c === "dark" : system.matches; }
    window.matchMedia = function (q) {
      var c = chosen();
      if (c && /prefers-color-scheme:\\s*dark/.test(q)) {
        return { matches: c === "dark", media: q, onchange: null,
                 addEventListener: function () {}, removeEventListener: function () {},
                 addListener: function () {}, removeListener: function () {},
                 dispatchEvent: function () { return false; } };
      }
      return realMM(q);
    };
    function sync() {
      var c = chosen();
      var docs = [document];
      document.querySelectorAll("iframe").forEach(function (f) {
        try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {}
      });
      docs.forEach(function (d) {
        if (c) d.documentElement.setAttribute("data-gwi-theme", c);
        else d.documentElement.removeAttribute("data-gwi-theme");
      });
      var dark = isDark();
      document.querySelectorAll(".gwi-theme-btn").forEach(function (b) {
        var label = b.getAttribute(dark ? "data-to-light" : "data-to-dark");
        if (b.getAttribute("aria-label") !== label) { b.setAttribute("aria-label", label); b.title = label; }
      });
    }
    function apply() {
      sync();
      window.dispatchEvent(new Event("afterprint"));
    }
    document.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest(".gwi-theme-btn");
      if (!b) return;
      var next = isDark() ? "light" : "dark";
      try {
        // Back to following the device when the choice matches it anyway.
        if ((next === "dark") === system.matches) localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, next);
      } catch (err) {}
      apply();
    });
    if (chosen()) apply();
    setInterval(sync, 400);
  }
  var s = P.document.createElement("script");
  s.textContent = "(" + install.toString() + ")();";
  P.document.head.appendChild(s);
})();
</script>"""

_ICON_MOON = _SVG.format(w=19, d='<path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z"/>')
_ICON_SUN = _SVG.format(w=19, d=(
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
    'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'))

links_col, lang_col = st.columns([8, 3], vertical_alignment="center")
with links_col:
    st.markdown(
        f'<div class="gwi-links">'
        f'<a href="{GEMINI_GEM_URL}" target="_blank" rel="noopener noreferrer" '
        f'title="{_("nav_cta_help")}" class="gwi-btn gwi-btn--fill">'
        f'{_ICON_SPARKLE}<span>{_("nav_cta")}</span>{_ICON_ARROW}</a>'
        f'<a {feedback_attrs} class="gwi-btn gwi-btn--tint" style="{feedback_extra}">'
        f"{_ICON_BUBBLE}<span>{_('feedback_cta')}</span></a></div>",
        unsafe_allow_html=True,
    )
with lang_col, st.container(horizontal=True, horizontal_alignment="right",
                             vertical_alignment="center", gap="small", key="gwi_prefs"):
    lang_choice = st.segmented_control(
        "Language / Idioma",
        options=["en", "es"],
        format_func=lambda k: "English" if k == "en" else "Español",
        default=st.session_state["lang"],
        label_visibility="collapsed",
        key="lang_radio",
    )
    st.markdown(
        f'<button type="button" class="gwi-theme-btn" aria-label="{_("theme_to_dark")}" '
        f'title="{_("theme_to_dark")}" data-to-dark="{_("theme_to_dark")}" data-to-light="{_("theme_to_light")}">'
        f'<span class="gwi-ic-moon">{_ICON_MOON}</span><span class="gwi-ic-sun">{_ICON_SUN}</span></button>',
        unsafe_allow_html=True,
    )
with st.container(key="gwi_theme_js"):
    st.iframe(_THEME_JS, height=1)
    # Clicking the selected option deselects it; treat that as "no change".
    if lang_choice and lang_choice != st.session_state["lang"]:
        # Streamlit resets a widget whose label changes, and every label
        # changes with the language, so switching would silently drop the
        # search and filters. Carry them across the rerun.
        st.session_state["_carry"] = {
            k: st.session_state[k]
            for k in st.session_state
            if k in ("search", "sel_cats", "open_now") or str(k).startswith(SVC_KEY)
        }
        st.session_state["lang"] = lang_choice
        st.rerun()

# Hero: the name, the number of organizations listed, and a photograph of the
# city itself. The Duck Bridge's green is where the site's teal comes from.
# Hotlinked from Wikimedia's CDN, which permits it; the licence (CC BY-SA 4.0)
# requires the credit that sits in the footer.
HERO_PHOTO = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/3/33/"
    "The_Duck_Bridge_%26_Ayer_Mill_Lawrence%2C_Massachusetts.jpg/"
    "1280px-The_Duck_Bridge_%26_Ayer_Mill_Lawrence%2C_Massachusetts.jpg"
)
HERO_PHOTO_PAGE = (
    "https://commons.wikimedia.org/wiki/"
    "File:The_Duck_Bridge_%26_Ayer_Mill_Lawrence,_Massachusetts.jpg"
)
_stats = f'<div class="gwi-stat"><b>{len(df)}</b><span>{_("stat_orgs")}</span></div>'
st.markdown(
    f'<section class="gwi-hero">'
    f'<div class="gwi-hero-tx"><h1 class="gwi-h1">{_("page_heading")}</h1>'
    f'<div class="gwi-stats">{_stats}</div></div>'
    f'<div class="gwi-hero-img" role="img" aria-label="{_("hero_alt")}" '
    f'style="background-image:url(\'{HERO_PHOTO}\')"></div>'
    f"</section>",
    unsafe_allow_html=True,
)


# ── the finder ────────────────────────────────────────────────────────────────
# Every filter lives in one card on the page. There is no sidebar: it starts
# collapsed on phones, so its filters were invisible to exactly the people most
# likely to need this, and on desktop it split attention between two places.
#
# The card reads top to bottom as a question and its answers: the five most
# common needs first, then search and the broader filters. Specific services
# (167 of them, from data/specific_services.csv) are never shown as one long
# list. Choosing a category reveals only that category's services, as tappable
# pills with how many organizations offer each, so the full detail is one step
# away without the clutter.
for _k, _v in st.session_state.pop("_carry", {}).items():
    st.session_state[_k] = _v
st.session_state.setdefault("search", "")
_lang = st.session_state["lang"]

# Options and counts come from the data, not the taxonomy, so no option is a
# dead end and each count says what choosing it will leave on the map.
cat_counts = {}
for lst in df["Categories"]:
    for c in lst:
        cat_counts[c] = cat_counts.get(c, 0) + 1
all_categories = sorted(cat_counts, key=CATEGORY_ORDER.index)

# Counts are keyed by (category, specific service): the same name can sit in
# two categories ("Special Education" is in both K-12 and Disability).
svc_counts: dict[tuple[str, str], int] = {}
for pairs in df["Specifics"]:
    for pair in pairs:
        svc_counts[pair] = svc_counts.get(pair, 0) + 1
# In the order the CSV lists them, which puts the most-needed first.
services_in = {
    c: [s for s in SPECIFIC_ORDER.get(c, []) if (c, s) in svc_counts]
    for c in all_categories
}

# Only offer a common-need chip that leads somewhere.
live_chips = [
    (key, [p for p in pairs if p in svc_counts])
    for key, pairs in QUICK_FILTERS
    if any(p in svc_counts for p in pairs)
]
current_svcs = _selected_specifics()

# Each common-need button is a small card: the need, what it covers, and how
# many organizations that is. Streamlit buttons hold one line of text, so the
# second and third lines are drawn with CSS generated here, per button.
def _need_count(pairs: list[tuple[str, str]]) -> int:
    return int(df["Specifics"].apply(lambda have: any(p in have for p in pairs)).sum())


def _css_str(text: str) -> str:
    """A CSS string literal. Not json.dumps: CSS has no \\uXXXX escape, so an
    escaped "í" rendered as "u00ed". Raw UTF-8 is valid inside CSS quotes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


_need_css = "".join(
    f".st-key-quick_{key} button::after{{content:{_css_str(_quick_text(key, 'desc'))};}}"
    f".st-key-quick_{key} button::before{{content:"
    f"{_css_str(_('need_count').format(n=_need_count(tags)))};}}"
    for key, tags in live_chips
)
st.markdown(f"<style>{_need_css}</style>", unsafe_allow_html=True)

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
        for col, (key, pairs) in zip(st.columns(len(live_chips)), live_chips):
            col.button(
                _quick_text(key, "label"),
                key=f"quick_{key}",
                width="stretch",
                on_click=_apply_quick_filter,
                args=(pairs,),
                type="primary" if current_svcs == sorted(pairs) else "secondary",
            )

    # Search gets a full-width row of its own, so the long placeholder is
    # readable and it reads as the main way in after the five needs.
    search = st.text_input(
        _("search_label"), placeholder=_("search_placeholder_main"), key="search"
    )
    if search:
        # Typos are corrected silently by the search; say so, like Google does,
        # so a result for "immigration" doesn't look like a mismatch for "imigration".
        _fixes = spelling_fixes(df, search, tax)
        if _fixes:
            _fixed = re.sub(
                r"[\w'’]+",
                lambda m: (f"<b>{html.escape(_fixes[_fold(m[0]).lower()])}</b>"
                           if _fold(m[0]).lower() in _fixes else html.escape(m[0])),
                search,
            )
            st.markdown(
                f'<p class="gwi-fixed">{_("search_fixed").format(q=_fixed)}</p>',
                unsafe_allow_html=True,
            )
    else:
        # A blank box is the hardest place to start: say what kinds of search
        # work, since most people don't expect a plain sentence to.
        st.markdown(f'<p class="gwi-hint">{_("search_hint")}</p>', unsafe_allow_html=True)
    if ai_matcher is not None:
        st.caption(_("search_ai_note"))

    c_cat, c_type, c_open = st.columns([4, 2.6, 2], vertical_alignment="bottom")
    with c_cat:
        sel_cats = st.multiselect(
            _("category_label"),
            all_categories,
            key="sel_cats",
            placeholder=_("category_placeholder"),
            format_func=lambda c: category_label(c, _lang),
        )
    with c_type:
        all_org_types = sorted({t for t in df["OrgType"] if t})
        org_type_labels = [_("orgtype_all")] + [_org_type_label(t) for t in all_org_types]
        org_type_label_to_raw = {_("orgtype_all"): "All"}
        org_type_label_to_raw.update({_org_type_label(t): t for t in all_org_types})
        # A label from the other language survives a language switch in state;
        # map it back to "All" rather than letting the selectbox reject it.
        if st.session_state.get("sel_org_type_label") not in org_type_labels:
            st.session_state["sel_org_type_label"] = _("orgtype_all")
        sel_org_type_label = st.selectbox(
            _("orgtype_label"), org_type_labels, key="sel_org_type_label"
        )
        sel_org_type = org_type_label_to_raw[sel_org_type_label]
    with c_open:
        open_now_only = st.toggle(
            _("filter_open_now"), key="open_now", help=_("filter_open_now_help")
        )

    # Specific services, one row of pills per chosen category.
    svc_by_cat: dict[str, list[str]] = {}
    if sel_cats:
        try:
            svc_box = st.container(key="gwi_svcs")
        except TypeError:
            svc_box = st.container()
        with svc_box:
            for c in sel_cats:
                opts = services_in.get(c, [])
                if not opts:
                    continue
                picked = st.pills(
                    _("services_in").format(cat=category_label(c, _lang)),
                    opts,
                    selection_mode="multi",
                    key=SVC_KEY + c,
                    format_func=lambda s, c=c: (
                        f"{specific_label(c, s, _lang)} ({svc_counts[(c, s)]})"
                    ),
                )
                if picked:
                    svc_by_cat[c] = list(picked)
    else:
        st.markdown(
            f"<p class='gwi-hint-line'>{_('services_hint')}</p>",
            unsafe_allow_html=True,
        )

sel_svcs = [s for lst in svc_by_cat.values() for s in lst]


def _skeleton_html() -> str:
    """Placeholder rows and map, shaped like the results they stand in for."""
    row = ('<div class="gwi-skel-row"><i class="gwi-skel-tile"></i><div>'
           '<i style="width:62%"></i><i style="width:44%"></i><i style="width:30%"></i></div></div>')
    return (f'<div class="gwi-skel" role="status" aria-live="polite">'
            f'<p class="gwi-skel-msg">{_ICON_SPARKLE}{_("ai_searching")}</p>'
            f'<div class="gwi-skel-win"><div class="gwi-skel-list">{row * 5}</div>'
            f'<div class="gwi-skel-map"></div></div></div>')


# ── apply filters ─────────────────────────────────────────────────────────────
filtered = df.copy()

search_reasons: dict[str, str] = {}
# Always drawn (usually empty), so the elements after it keep their places.
_loading = st.empty()
if search:
    # A new search can take a few seconds when the AI is asked. Meanwhile the
    # results area shows placeholder rows in its own shape, and the previous
    # results are hidden (Streamlit marks them stale) rather than left
    # looking current. A search already run, like after a language switch,
    # comes back at once and skips this.
    _q_key = (search, st.session_state["lang"])
    if st.session_state.get("_last_q") != _q_key:
        _loading.markdown(_skeleton_html(), unsafe_allow_html=True)
    # All search logic lives in search_engine.py, shared with
    # scripts/score_search.py so the score measures exactly what runs here.
    # Results come back best-first; the filters below keep that order.
    filtered, search_reasons, _search_mode = smart_search(
        filtered, search, tax, ai_matcher
    )
    st.session_state["_last_q"] = _q_key
    _loading.empty()

if sel_cats:
    # Per category: if specific services are picked there, an org must offer
    # one of them; otherwise anything in the category counts. Categories are
    # OR'd, so picking a service in one category never hides another category.
    def _cat_match(row) -> bool:
        for c in sel_cats:
            picked = svc_by_cat.get(c)
            if picked:
                if any((c, s) in row["Specifics"] for s in picked):
                    return True
            elif c in row["Categories"]:
                return True
        return False

    filtered = filtered[filtered.apply(_cat_match, axis=1)]

if sel_org_type != "All":
    filtered = filtered[filtered["OrgType"] == sel_org_type]

if open_now_only:
    # Orgs that publish no hours are excluded rather than assumed open; this
    # filter is used to decide whether to travel somewhere now.
    _now = _now_local()
    filtered = filtered[
        filtered["Hours"].apply(
            lambda h: open_status(parse_hours(str(h)), _now)[0] == "open"
        )
    ]

n_filtered = len(filtered)
n_total = len(df)
has_filters = bool(search or sel_cats or sel_org_type != "All" or open_now_only)

# ── search results helpers ────────────────────────────────────────────────────
_ICON_PHONE = _SVG.format(w=15, d=(
    '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 '
    '1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 '
    '2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 '
    '2.7.7a2 2 0 0 1 1.7 2z"/>'))
_ICON_GLOBE = _SVG.format(w=15, d=(
    '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18'
    'M12 3a14 14 0 0 0 0 18"/>'))
_ICON_PIN = _SVG.format(w=15, d=(
    '<path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/>'
    '<circle cx="12" cy="9.5" r="2.5"/>'))
_ICON_SEARCH = _SVG.format(w=26, d='<circle cx="11" cy="11" r="7"/><path d="M16 16l5 5"/>')
_ICON_ALERT = _SVG.format(w=20, d=(
    '<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4.5M12 17.5v.01"/>'))



def _tel(number: str) -> str:
    """A tappable phone number that still shows the number as text."""
    digits = re.sub(r"\D", "", number)
    return f'<a href="tel:{digits}">{html.escape(number)}</a>'


def _checked_label(raw) -> str:
    """'2026-10-12' -> 'Oct 2026' ('oct 2026' in Spanish); '' if unreadable."""
    m = re.match(r"(\d{4})-(\d{2})", str(raw or ""))
    if not m:
        return ""
    return f"{_month_names()[int(m.group(2)) - 1]} {m.group(1)}"


def _empty_state() -> None:
    st.markdown(
        f'<div class="gwi-empty"><div class="gwi-empty-ic">{_ICON_SEARCH}</div>'
        f'<h4>{_("empty_title")}</h4><p>{_("no_results")}</p>'
        f'<p class="gwi-211">{_("help_211")}</p></div>',
        unsafe_allow_html=True,
    )


# Words that mean someone may be in danger now. When a search contains one,
# the emergency numbers go above everything else: a list of agencies with
# office hours is the wrong first answer to "I want to kill myself".
_CRISIS_SELF = re.compile(
    r"\b(suicid\w*|kill (my|him|her)self|want(ing)? to die|end (my|it all)|self[- ]?harm|"
    r"hurt(ing)? myself|overdos\w*|matarme|quiero morir(me)?|hacerme da[nñ]o|suicidarme)\b", re.I)
_CRISIS_VIOLENCE = re.compile(
    r"\b(hits? me|beats? me|hurts? me|abus\w*|domestic violence|violen\w*|rape\w*|raped|"
    r"assault\w*|stalk\w*|threaten\w*|unsafe at home|scared of my|afraid of my|"
    r"me pega|me golpea|me maltrata|maltrato|abuso|violaci[oó]n|amenaz\w*|tengo miedo)\b", re.I)


def _crisis_box(query: str) -> None:
    q = _fold(query or "")
    selfharm, violence = bool(_CRISIS_SELF.search(q)), bool(_CRISIS_VIOLENCE.search(q))
    if not (selfharm or violence):
        return
    lines = [f'<li><b>{_("crisis_911")}</b> {_tel("911")}</li>']
    line988 = f'<li>{_("crisis_988")} {_tel("988")}</li>'
    lines_dv = [
        f'<li>{_("crisis_ywca")} {_tel("877-509-9922")}</li>',
        f'<li>{_("crisis_jgcc")} {_tel("978-388-1888")}</li>',
    ]
    lines += ([line988] + (lines_dv if violence else [])) if selfharm else (lines_dv + [line988])
    st.markdown(
        f'<section class="gwi-crisis" role="alert"><div class="gwi-crisis-h">{_ICON_ALERT}'
        f'<span>{_("crisis_title")}</span></div><ul>{"".join(lines)}</ul>'
        f'<p>{_("crisis_note")}</p></section>',
        unsafe_allow_html=True,
    )


def _log_search(query: str, n_results: int) -> None:
    """Record the search (text, language, how many results) in search_log, once
    per search per visit, so the team can see what people look for and what
    finds nothing. Never blocks the page: a failed write is ignored."""
    q = (query or "").strip()[:200]
    if DATA_SOURCE != "supabase" or len(q) < 2:
        return
    key = (q.lower(), st.session_state["lang"])
    logged = st.session_state.setdefault("_logged_searches", set())
    if key in logged:
        return
    logged.add(key)
    try:
        from postgrest import ReturnMethod

        from data_source import _client
        # returning=minimal: the public key may add to the log but not read it,
        # and asking for the new row back makes Supabase refuse the insert.
        _client().table("search_log").insert(
            {"query": q, "lang": st.session_state["lang"], "result_count": int(n_results)},
            returning=ReturnMethod.minimal,
        ).execute()
    except Exception:
        pass


@st.cache_data(ttl=86400, show_spinner=False)
def _geocode(place: str) -> tuple[float, float] | None:
    """A ZIP code or street address -> (lat, lon), via OpenStreetMap. Biased to
    Massachusetts, since that is where everyone using this is."""
    q = place.strip()
    if re.fullmatch(r"\d{5}", q):
        q += ", Massachusetts"
    elif "ma" not in q.lower().split() and "massachusetts" not in q.lower():
        q += ", Lawrence, Massachusetts"
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "LawrenceResourceFinder/1.0 (Gateway Initiative)"},
            timeout=8,
        )
        hits = r.json() if r.ok else []
    except Exception:
        return None
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def _miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    from math import asin, cos, radians, sin, sqrt
    la1, lo1, la2, lo2 = map(radians, (*a, *b))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * asin(sqrt(h))


def _nearest_miles(row, origin) -> float | None:
    """Distance to the org's closest site: its main address or any other location."""
    pts = []
    if pd.notna(row.get("Latitude")) and pd.notna(row.get("Longitude")):
        pts.append((float(row["Latitude"]), float(row["Longitude"])))
    for loc in locations.get(row["Name"], []):
        try:
            pts.append((float(loc["Latitude"]), float(loc["Longitude"])))
        except (TypeError, ValueError, KeyError):
            pass
    return min((_miles(origin, p) for p in pts), default=None)


def _share_text(rows) -> str:
    """Plain text a caseworker can paste into a text message or email."""
    blocks = []
    for _ix, r in rows.iterrows():
        lines = [str(r["Name"])]
        if str(r.get("Phone", "")).strip():
            lines.append(f'{_("sec_phone")}: {r["Phone"]}')
        addr = ", ".join(p for p in (str(r.get("Address", "")).strip(), str(r.get("City", "")).strip()) if p)
        if addr:
            lines.append(f'{_("popup_address")}: {addr}')
        hours = str(r.get("Hours", "")).strip()
        lines.append(f'{_("sec_hours")}: {hours}' if hours else _("status_unknown"))
        if str(r.get("URL", "")).strip():
            lines.append(str(r["URL"]).strip())
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# A search that sounds like an emergency gets the numbers first, above the
# result count and everything else.
if search:
    _crisis_box(search)
    _log_search(search, n_filtered)

count_col, clear_col = st.columns([6, 1.2], vertical_alignment="center")
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
            width="stretch",
        )

# ── explore: results list beside the map ──────────────────────────────────────
# One working surface, the way map apps do it: a scannable list of places on
# the left and the map on the right, framed as one window so a place and its
# pin are compared at a glance. Picking a row (or a pin) turns the list into
# that place's card. The list is for comparing; the card is for reading.
#
# Everything below the filters is one st.fragment: picking a place, sorting
# or clicking a pin reruns only this part, not the hero, filters and search.
# Pins carry no popups (the card holds the detail), which is most of what a
# rerun of the map used to cost.
PANE_H = 660          # px; the list/card pane and the map share one height
LIST_CAP = 40         # rows drawn before "Show more"
DAY_START, DAY_END = 6 * 60, 22 * 60   # the today strip runs 6 AM to 10 PM

# Colour means one thing here, status, and nothing else uses these hues:
# Apple's system green for open, orange for closing within the hour, red for
# closed, grey for hours not listed. As in Apple Maps only the status word is
# coloured ("Closed · Opens Fri 10 AM"), so an evening list isn't a wall of red.
# Text uses Apple's accessible variants (4.5:1 on white); the org's tile
# carries the same colour as a pale tint, so a list reads at a glance.
# (Apple's accessible green, #248a3d, is 4.4:1 on white, so text uses a
# slightly deeper green; every pair below is 4.5:1 or more, hovered rows too.)
STATUS_TEXT = {"open": "#1c7531", "soon": "#b33a00", "closed": "#d70015", "unknown": "#5f6066"}
STATUS_TILE = {"open": ("#e3f6e7", "#1c7531"), "soon": ("#fff0e0", "#a83700"),
               "closed": ("#fde7e6", "#c4141f"), "unknown": ("#eef0f2", "#5f6066")}
STATUS_FILL = {"open": "#34c759", "soon": "#ff9500"}   # the today strip's current span
SOON_MIN = 60          # "Closes soon" in the last hour, as Apple Maps does
OPEN_STATES = ("open", "soon")
MONO_BG, MONO_INK = STATUS_TILE["unknown"]

_ICON_WHO = _SVG.format(w=16, d='<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8"/>')
_ICON_COST = _SVG.format(w=16, d='<path d="M20.6 13.4l-7.2 7.2a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.5"/>')
_ICON_LANG = _SVG.format(w=16, d='<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>')
_ICON_HOW = _SVG.format(w=16, d='<path d="M9 18l6-6-6-6"/>')
_ICON_CLOCK = _SVG.format(w=16, d='<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>')
_ICON_LIST = _SVG.format(w=17, d='<path d="M9 6h12M9 12h12M9 18h12"/><circle cx="4" cy="6" r=".6"/><circle cx="4" cy="12" r=".6"/><circle cx="4" cy="18" r=".6"/>')
_ICON_MAP = _SVG.format(w=17, d='<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>')


def _pick_org(name: str) -> None:
    st.session_state["sel_org"] = name


def _unpick_org() -> None:
    st.session_state["sel_org"] = None


def _initials(name: str) -> str:
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ0-9]+", name)
             if w.lower() not in {"the", "of", "and", "for", "a", "de", "la", "inc"}]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper() if not words[0].isupper() else words[0][:2]
    return (words[0][0] + words[1][0]).upper()


def _monogram(row, large: bool = False) -> str:
    bg, ink = STATUS_TILE[_status(row.get("Hours", ""))[0]]
    cls = "gwi-mono gwi-mono--lg" if large else "gwi-mono"
    return f'<span class="{cls}" style="{_themed(f"background:{bg};color:{ink}")}" aria-hidden="true">{html.escape(_initials(str(row["Name"])))}</span>'


def _main_cat(row) -> str | None:
    """The kind of help an org offers most: the category holding most of its
    services (ties go to the more urgent one). Its first category in urgency
    order would call a soccer program "Health care" for one health class."""
    cats = list(row.get("Categories") or [])
    if not cats:
        return None
    counts: dict[str, int] = {}
    for c, _s in row.get("Specifics") or []:
        counts[c] = counts.get(c, 0) + 1
    return max(cats, key=lambda c: (counts.get(c, 0), -cats.index(c)))


def _main_kind(row) -> str:
    c = _main_cat(row)
    return category_label(c, st.session_state["lang"]) if c else _org_type_label(str(row.get("OrgType", "")))


def _status_parts(hours: str) -> tuple[str, str, str]:
    """(state, word, detail): state is open, soon, closed or unknown, e.g.
    ("closed", "Closed", "Opens Fri 10 AM")."""
    state, detail, _label = open_status(_schedules(hours), _now_local(), _day_names(), _month_names())
    if state == "open":
        now = _now_local()
        m_now = now.hour * 60 + now.minute
        ends = [b for a, b in (_day_spans(hours) or []) if a <= m_now < b and b < 1440]
        soon = bool(ends) and ends[0] - m_now <= SOON_MIN
        return ("soon" if soon else "open", _("st_soon") if soon else _("st_open"),
                _("st_until").format(t=detail) if detail else "")
    if state == "closed":
        return state, _("st_closed"), _("st_opens").format(t=detail) if detail else ""
    return state, _("hours_not_listed"), ""


def _status(hours: str) -> tuple[str, str]:
    """(state, words), e.g. ("open", "Open · until 5 PM")."""
    state, word, detail = _status_parts(hours)
    return state, word + (f" · {detail}" if detail else "")


def _status_html(hours: str) -> str:
    """The status line: the word in its status colour, the rest quiet."""
    state, word, detail = _status_parts(hours)
    tail = f'<span class="gwi-st-tail"> · {html.escape(detail)}</span>' if detail else ""
    return f'<p class="gwi-row-status is-{state}"><b>{html.escape(word)}</b>{tail}</p>'


def _day_spans(hours: str, ahead: int = 0) -> list[tuple[int, int]] | None:
    """Opening spans in minutes on the day `ahead` days from today, merged
    across services; None if the org publishes no usable hours, [] if it is
    closed that day."""
    scheds = [s for s in _schedules(hours) if s.has_structure]
    if not scheds:
        return None
    day = _now_local() + timedelta(days=ahead)
    if any(s.always_open for s in scheds):
        return [(0, 1440)]
    from hours import in_season
    spans = sorted((i.start, min(i.end, 1440)) for s in scheds if in_season(s, day)
                   for i in s.intervals if i.day == day.weekday())
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _today_strip(hours: str, labels: bool = False) -> str:
    """The day at a glance: when the place is open today, and where now falls.
    Built from the org's own hours, so a list of places can be compared
    without opening any of them. Once it has closed for the day, the strip
    shows the next day it opens instead, labelled ("Tomorrow", "Mon")."""
    spans = _day_spans(hours)
    if spans is None:
        return ""
    now = _now_local()
    m_now = now.hour * 60 + now.minute
    ahead = 0
    if not spans or m_now >= max(b for _a, b in spans):
        for k in range(1, 8):
            nxt = _day_spans(hours, k)
            if nxt:
                spans, ahead = nxt, k
                break
        else:
            return ""
    lo, hi = DAY_START, DAY_END
    for a, b in spans:
        lo, hi = min(lo, a), max(hi, b)
    pct = lambda m: (min(max(m, lo), hi) - lo) / (hi - lo) * 100
    state = _status_parts(hours)[0]
    words = ", ".join(format_range(a, b) if (a, b) != (0, 1440) else _("hours_24") for a, b in spans)
    day_word = (_("hours_today") if ahead == 0 else _("day_tomorrow") if ahead == 1
                else _day_names()[(now.weekday() + ahead) % 7])
    bars = "".join(f'<span class="gwi-day-open{" is-now is-" + state if ahead == 0 and a <= m_now < b else ""}" '
                   f'style="left:{pct(a):.1f}%;width:{max(pct(b) - pct(a), 1.2):.1f}%"></span>' for a, b in spans)
    tick = f'<i class="gwi-day-now" style="left:{pct(m_now):.1f}%"></i>' if ahead == 0 and lo <= m_now <= hi else ""
    tag = f'<span class="gwi-day-tag">{html.escape(day_word)}</span>' if ahead else ""
    scale = ""
    if labels:
        marks = [(lo, format_minutes(lo)), ((lo + hi) // 2, format_minutes((lo + hi) // 2)), (hi, format_minutes(hi % 1440))]
        scale = '<div class="gwi-day-scale">' + "".join(f"<span>{t}</span>" for _m, t in marks) + "</div>"
    label = f"{day_word}: {words}"
    return (f'<div class="gwi-day-wrap{" is-ahead" if ahead else ""}"><div class="gwi-day-line">'
            f'<div class="gwi-day" role="img" aria-label="{html.escape(label)}" title="{html.escape(label)}">'
            f'{bars}{tick}</div>{tag}</div>{scale}</div>')


def _row_html(row, miles: float | None, selected: bool) -> str:
    esc = html.escape
    name = str(row["Name"])
    meta = " · ".join(p for p in (_main_kind(row), str(row.get("City", "")).strip()) if p)
    dist = f'<span class="gwi-row-dist">{esc(_("miles").format(d=f"{miles:.1f}"))}</span>' if miles is not None else ""
    why = (f'<p class="gwi-row-why">{_ICON_SPARKLE.replace("18", "12")}{esc(search_reasons[name])}</p>'
           if name in search_reasons else "")
    return (
        f'<div class="gwi-row{" is-sel" if selected else ""}" data-org="{_org_id(name)}">{_monogram(row)}'
        f'<div class="gwi-row-main"><div class="gwi-row-top"><p class="gwi-row-name">{esc(name)}</p>{dist}</div>'
        f'<p class="gwi-row-meta">{esc(meta)}</p>'
        f'{_status_html(row.get("Hours", ""))}'
        f'{_today_strip(row.get("Hours", ""))}{why}</div></div>'
    )


def _week_html(hours: str) -> str:
    """The week, one block per service, with today's line marked."""
    today = _now_local().weekday()
    names = _day_names()
    blocks = []
    for s in _schedules(hours):
        if not s.has_structure:
            continue
        if s.always_open:
            rows = [(f"{names[0]}–{names[6]}", _("hours_24"), True)]
        else:
            per = [", ".join(format_range(i.start, i.end) for i in s.intervals if i.day == d) or _("hours_closed_word")
                   for d in range(7)]
            rows, d = [], 0
            while d < 7:
                e = d
                while e + 1 < 7 and per[e + 1] == per[d] and e + 1 != today and d != today:
                    e += 1
                label = names[d] if d == e else f"{names[d]}–{names[e]}"
                rows.append((label, per[d], d <= today <= e))
                d = e + 1
        head = f'<p class="gwi-week-svc">{html.escape(s.label)}</p>' if s.label else ""
        notes = "".join(f'<p class="gwi-week-note">{html.escape(n)}</p>' for n in s.notes + s.unparsed)
        blocks.append(head + '<div class="gwi-week">' + "".join(
            f'<div class="gwi-week-row{" is-today" if t else ""}"><span>{html.escape(l)}</span>'
            f'<span>{html.escape(v)}</span></div>' for l, v, t in rows) + "</div>" + notes)
    return "".join(blocks)


def _place_card_html(row) -> str:
    """One place in full, read top to bottom: what it is, whether it's open,
    how to reach it, what you can get there and where."""
    esc = html.escape
    name = str(row["Name"])
    hours = str(row.get("Hours", "")).strip()
    checked = _checked_label(row.get("HoursVerifiedOn")) if hours else ""
    bg, ink = MONO_BG, MONO_INK
    meta = " · ".join(p for p in (_main_kind(row), str(row.get("City", "")).strip()) if p)

    acts = []
    phone = str(row.get("Phone", "")).strip()
    digits = re.sub(r"\D", "", phone)
    if len(digits) >= 10:
        acts.append(f'<a class="gwi-act gwi-act--primary" href="tel:+1{digits[-10:]}">{_ICON_PHONE}{_("call")}</a>')
    if pd.notna(row.get("Latitude")) and pd.notna(row.get("Longitude")):
        acts.append(f'<a class="gwi-act" href="{_directions_url(row["Latitude"], row["Longitude"])}" '
                    f'target="_blank" rel="noopener noreferrer">{_ICON_PIN}{_("get_directions")}</a>')
    url = str(row.get("URL", "")).strip()
    if url:
        href = url if url.startswith("http") else f"https://{url}"
        acts.append(f'<a class="gwi-act" href="{esc(href)}" target="_blank" rel="noopener noreferrer">{_ICON_GLOBE}{_("sec_website")}</a>')

    out = [
        f'<article class="gwi-place"><header class="gwi-place-hd" style="{_themed(f"--tint:{bg};--tint-ink:{ink}")}">'
        f'{_monogram(row, large=True)}<div class="gwi-place-id"><h2 class="gwi-place-name">{esc(name)}</h2>'
        f'<p class="gwi-place-meta">{esc(meta)}</p></div></header>'
        f'<div class="gwi-place-now">{_status_html(hours)}'
        + (f'<span class="gwi-checked">{esc(_("hours_checked").format(d=checked))}</span>' if checked else "")
        + f'{_today_strip(hours, labels=True)}</div>'
        + (f'<div class="gwi-acts">{"".join(acts)}</div>' if acts else "")
    ]
    if acts:
        # On phones the same actions also sit in a bar at the bottom of the
        # screen, so Call stays one thumb away however far the card scrolls.
        out.append(f'<nav class="gwi-dock" aria-label="{esc(name)}">{"".join(acts)}</nav>')
    about = _description(row)
    if about:
        out.append(f'<p class="gwi-place-about">{esc(about)}</p>')

    facts = [(ic, _(k), str(row.get(c, "")).strip()) for ic, k, c in
             ((_ICON_WHO, "sec_who", "WhoItsFor"), (_ICON_COST, "sec_cost", "Cost"),
              (_ICON_LANG, "sec_lang", "Languages"), (_ICON_HOW, "sec_how", "HowToGetHelp"))]
    facts = [f for f in facts if f[2]]
    if facts:
        out.append('<div class="gwi-group">' + "".join(
            f'<div class="gwi-group-row">{ic}<div><p class="gwi-group-k">{esc(k)}</p>'
            f'<p class="gwi-group-v">{esc(v)}</p></div></div>' for ic, k, v in facts) + "</div>")

    if hours:
        src = str(row.get("HoursSourceURL", "")).strip()
        out.append(f'<section class="gwi-place-sec"><h3>{_("sec_hours")}</h3>{_week_html(hours) or esc(hours)}'
                   + (f'<p class="gwi-place-src"><a href="{esc(src)}" target="_blank" rel="noopener noreferrer">'
                      f'{_("hours_source_link")}</a></p>' if src.startswith("http") else "") + "</section>")

    by_cat: dict[str, list[str]] = {}
    for c, s in sorted(row.get("Specifics") or [],
                       key=lambda p: (CATEGORY_ORDER.index(p[0]) if p[0] in CATEGORY_ORDER else 99, p[1])):
        by_cat.setdefault(c, []).append(specific_label(c, s, st.session_state["lang"]))
    if by_cat:
        out.append(f'<section class="gwi-place-sec"><h3>{_("sec_services")}</h3>' + "".join(
            f'<div class="gwi-svc-group"><p>{esc(category_label(c, st.session_state["lang"]))}</p>'
            f'<div class="gwi-tags">{"".join(f"<span>{esc(s)}</span>" for s in svcs)}</div></div>'
            for c, svcs in by_cat.items()) + "</section>")
    elif row.get("SvcTagList"):
        out.append(f'<section class="gwi-place-sec"><h3>{_("sec_services")}</h3><div class="gwi-tags">'
                   + "".join(f"<span>{esc(s)}</span>" for s in row["SvcTagList"]) + "</div></section>")

    locs = [l for l in _org_locations(name) if l.get("Kind") != "admin"]
    if len(locs) > 1:
        items = []
        for n, loc in enumerate(locs, 1):
            addr = ", ".join(p for p in (loc.get(k, "").strip() for k in ("Address", "City")) if p)
            title = loc.get("LocationName", "").strip() or addr
            items.append(
                f'<li><span class="gwi-loc-n">{n}</span><div><b>{esc(title)}</b>'
                + (f'<span>{esc(loc["ServicesHere"].strip())}</span>' if loc.get("ServicesHere", "").strip() else "")
                + (f'<span>{esc(addr)} · <a href="{_directions_url_address(loc)}" target="_blank" '
                   f'rel="noopener noreferrer">{_("loc_directions")}</a></span>' if addr else "")
                + "</div></li>")
        out.append(f'<section class="gwi-place-sec"><h3>{_("sec_locations")} ({len(locs)})</h3>'
                   f'<ul class="gwi-locs">{"".join(items)}</ul></section>')

    reports = []
    for col, label in (("ImpactReport", _("sec_impact")), ("StrategicPlan", _("sec_strategic"))):
        v = str(row.get(col, "")).strip()
        if v:
            reports.append(f'<a href="{esc(v if v.startswith("http") else "https://" + v)}" '
                           f'target="_blank" rel="noopener noreferrer">{esc(label)}</a>')
    if reports:
        out.append(f'<p class="gwi-place-reports">{" · ".join(reports)}</p>')
    out.append("</article>")
    return "".join(out)


def _fit_view(lats, lons, width_px: int = 640, height_px: int = PANE_H) -> tuple[list, int]:
    """Center and zoom that fit every result (st_folium takes a center and a
    zoom, not bounds, so the zoom comes from the span the results cover)."""
    from math import cos, floor, log2, radians
    lat_lo, lat_hi, lon_lo, lon_hi = float(lats.min()), float(lats.max()), float(lons.min()), float(lons.max())
    center = [(lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2]
    lon_span = max(lon_hi - lon_lo, 0.004) * 1.3
    lat_span = max(lat_hi - lat_lo, 0.004) * 1.3 / cos(radians(center[0]))
    zoom = floor(min(log2(width_px * 360 / (256 * lon_span)), log2(height_px * 360 / (256 * lat_span))))
    return center, max(9, min(15, zoom))


def _org_id(name: str) -> str:
    """A short stable id tying a list row to its pin on the map."""
    return hashlib.md5(str(name).encode("utf-8")).hexdigest()[:10]


# Pins lift and show their name in a callout when hovered on the map, or when
# their row is hovered in the list (the bridge script below adds .gwi-hot).
_MAP_CSS = """<style>
.leaflet-control-attribution{font-size:10px!important}
.leaflet-tooltip{display:none!important}
.gwi-mk{position:relative;width:25px;height:41px}
.gwi-mk .gwi-pin{transform-origin:50% 100%;transition:transform .22s cubic-bezier(.34,1.56,.64,1),filter .2s ease}
.gwi-callout{position:absolute;left:50%;bottom:48px;transform:translate(-50%,6px) scale(.96);opacity:0;
  pointer-events:none;white-space:nowrap;max-width:240px;overflow:hidden;text-overflow:ellipsis;
  background:#ffffff;color:__INK__;font:600 13px/1.25 __FONT__;padding:7px 11px;border-radius:12px;
  box-shadow:0 6px 20px rgba(19,35,42,.22),0 1px 3px rgba(19,35,42,.12);
  transition:opacity .18s ease,transform .22s cubic-bezier(.34,1.56,.64,1)}
.gwi-callout::after{content:"";position:absolute;left:50%;bottom:-5px;width:10px;height:10px;background:#ffffff;
  transform:translateX(-50%) rotate(45deg);border-radius:2px}
.leaflet-marker-icon:hover,.leaflet-marker-icon.gwi-hot{z-index:100000!important}
.leaflet-marker-icon:hover .gwi-pin,.gwi-hot .gwi-pin{transform:translateY(-3px) scale(1.28);
  filter:drop-shadow(0 6px 8px rgba(19,35,42,.35))}
.leaflet-marker-icon:hover .gwi-callout,.gwi-hot .gwi-callout{opacity:1;transform:translate(-50%,0) scale(1)}
</style>"""

# Maps in the dark: the street tiles are inverted into a night map (the
# satellite layer is left alone), and controls, the boundary line, the pins
# and the selection ring are lifted so they still stand out on it.
_MAP_DARK_CSS = "<style>" + _dark("""
.leaflet-container{background:#0d1214}
.gwi-tiles{filter:invert(1) hue-rotate(180deg) brightness(.9) contrast(.86) saturate(.55)}
.leaflet-bar a,.leaflet-bar a:hover,.leaflet-control-layers{background:#232d30!important;color:#ecf1f2!important;border-color:#3b4a4f!important}
.leaflet-control-layers-toggle{filter:invert(1)}
.leaflet-control-layers label{color:#ecf1f2}
.leaflet-control-attribution{background:rgba(24,32,35,.82)!important;color:#98a9ae!important}
.leaflet-control-attribution a{color:#5ec5b9!important}
.leaflet-overlay-pane path[stroke="#13232a"]{stroke:#d7e6e3}
.leaflet-overlay-pane path[stroke="#ffffff"]{stroke:#0d1214}
.leaflet-overlay-pane path[stroke="__ACCENT__"]{stroke:#5ec5b9;fill:#5ec5b9}
.gwi-pin path[fill="__ACCENT__"]{fill:#2a9086}
.gwi-pin path[stroke="__ACCENT__"]{stroke:#5ec5b9}
.gwi-pin text[fill="__ACCENT__"]{fill:#2a9086}
.gwi-callout,.gwi-callout::after{box-shadow:0 6px 20px rgba(0,0,0,.6)}
""".replace("__ACCENT__", ACCENT)) + "</style>"


# Links a list row to its pin across the map's iframe: hovering a row lifts
# its pin and shows its name; hovering a pin lights its row. Installed once
# into the page from a hidden iframe (st.iframe content is same-origin), and
# it looks the map up again on every event, since Streamlit redraws it.
_HOVER_BRIDGE = """<script>
(function () {
  var P = window.parent;
  if (!P || P.__gwiHover) return;
  function bridge() {
    if (window.__gwiHover) return;
    window.__gwiHover = true;
    function mapDoc() {
      var frames = document.querySelectorAll("iframe");
      for (var i = 0; i < frames.length; i++) {
        try {
          var d = frames[i].contentDocument;
          if (d && d.querySelector('[class*="gwi-m-"]')) return d;
        } catch (e) {}
      }
      return null;
    }
    var hotId = null;
    function lift(id) {
      if (id === hotId) return;
      var d = mapDoc();
      if (d) {
        d.querySelectorAll(".gwi-hot").forEach(function (el) { el.classList.remove("gwi-hot"); });
        if (id) {
          var el = d.querySelector(".gwi-m-" + id);
          if (el) el.classList.add("gwi-hot");
        }
      }
      hotId = id;
    }
    var phone = window.matchMedia("(max-width: 640px)");
    function explore() { return document.querySelector(".st-key-gwi_explore"); }
    function setView(v) {
      document.body.classList.toggle("gwi-view-map", v === "map");
      document.querySelectorAll(".gwi-views button").forEach(function (b) {
        b.setAttribute("aria-pressed", b.getAttribute("data-view") === v ? "true" : "false");
      });
    }
    // List | Map on phones: one view at a time, each with the whole screen.
    document.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest(".gwi-views button");
      if (!b) return;
      setView(b.getAttribute("data-view"));
      var sw = document.querySelector(".gwi-views");
      if (sw && sw.getBoundingClientRect().top < 0) sw.scrollIntoView({ block: "start" });
    });
    // A pill that brings the search box back once it has scrolled away.
    var jump = document.createElement("button");
    jump.type = "button";
    jump.className = "gwi-jump";
    document.body.appendChild(jump);
    function searchBox() { return document.querySelector(".st-key-search input"); }
    jump.addEventListener("click", function () {
      var box = searchBox();
      if (!box) return;
      box.scrollIntoView({ block: "center", behavior: "smooth" });
      setTimeout(function () { box.focus({ preventScroll: true }); }, 450);
    });
    var queued = false;
    function checkJump() {
      queued = false;
      var box = searchBox(), sw = document.querySelector(".gwi-views");
      var show = !!box && box.getBoundingClientRect().bottom < 0;
      if (show && sw) {
        var q = (box.value || "").trim();
        var label = q ? sw.getAttribute("data-jump-q").replace("{q}", q) : sw.getAttribute("data-jump");
        if (jump.textContent !== label) jump.textContent = label;
      }
      jump.classList.toggle("is-on", show && !!sw);
      // Hidden tabs stay in the page, so "a card is open" means a visible one.
      var open = Array.prototype.some.call(document.querySelectorAll(".gwi-dock"),
                                           function (d) { return d.getClientRects().length > 0; });
      document.body.classList.toggle("gwi-card-open", open);
    }
    new MutationObserver(function () {
      if (!queued) { queued = true; requestAnimationFrame(checkJump); }
    }).observe(document.body, { childList: true, subtree: true });
    document.addEventListener("scroll", function () {
      if (!queued) { queued = true; requestAnimationFrame(checkJump); }
    }, true);
    setInterval(checkJump, 1500);
    document.addEventListener("mouseover", function (e) {
      var row = e.target.closest && e.target.closest('[class*="st-key-row_"]');
      var tag = row && row.querySelector("[data-org]");
      lift(tag ? tag.getAttribute("data-org") : null);
    });
    setInterval(function () {
      var d = mapDoc();
      if (!d || d.__gwiBound) return;
      d.__gwiBound = true;
      // On a phone, tapping a pin in the map view opens its card in the list.
      d.addEventListener("click", function (e) {
        var icon = e.target.closest && e.target.closest('[class*="gwi-m-"]');
        if (icon && phone.matches && document.body.classList.contains("gwi-view-map")) {
          setView("list");
          var ex = explore();
          if (ex) ex.scrollIntoView({ block: "start" });
        }
      });
      d.addEventListener("mouseover", function (e) {
        var icon = e.target.closest && e.target.closest('[class*="gwi-m-"]');
        var m = icon && icon.className.match(/gwi-m-([0-9a-f]+)/);
        document.querySelectorAll(".gwi-row.is-hot").forEach(function (r) { r.classList.remove("is-hot"); });
        if (m) {
          var r = document.querySelector('.gwi-row[data-org="' + m[1] + '"]');
          if (r) r.classList.add("is-hot");
        }
      });
    }, 700);
  }
  var s = P.document.createElement("script");
  s.textContent = "(" + bridge.toString() + ")();";
  P.document.head.appendChild(s);
  P.__gwiHover = true;
})();
</script>"""


def _explore_map(map_data: pd.DataFrame) -> folium.Map:
    """The base map: boundary and one pin per org, all one colour. No popups:
    a pin click opens the place card beside the map, and hovering a pin (or
    its row in the list) lifts it and shows its name."""
    m = folium.Map(location=[42.7070, -71.1631], zoom_start=13, tiles=None)
    _add_basemap_layers(m)
    m.get_root().header.add_child(folium.Element(
        f"<style>{PALETTE_CSS}</style>"
        + _themed(_MAP_CSS.replace("__INK__", INK).replace("__FONT__", FONT_STACK.replace('"', "'")))
        + _MAP_DARK_CSS))
    geo = load_lawrence_boundary(BOUNDARY_PATH)
    if geo:
        fg = folium.FeatureGroup(name=_("boundary_layer_name"))
        folium.GeoJson(geo, style_function=lambda _f: {"color": "#ffffff", "weight": 6, "opacity": 0.9, "fill": False}).add_to(fg)
        folium.GeoJson(geo, style_function=lambda _f: {"color": BRAND_DARK, "weight": 2.5, "opacity": 0.85,
                                                       "fillColor": BRAND_DARK, "fillOpacity": 0.03,
                                                       "dashArray": "8 6"}).add_to(fg)
        fg.add_to(m)
    offset_of: dict = {}
    for members in _shared_buildings(map_data):
        ordered = sorted(members, key=lambda i: str(map_data.at[i, "Name"]).lower())
        for idx, dx in zip(ordered, _fan_offsets(len(ordered))):
            offset_of[idx] = dx
    for idx, row in map_data.iterrows():
        n_sites = len([l for l in _org_locations(row["Name"]) if l.get("Kind") != "admin"])
        dx = offset_of.get(idx, 0)
        folium.Marker(
            location=[row["Latitude"], row["Longitude"]],
            # The tooltip is hidden (the callout shows the name) but kept:
            # st_folium reports it on a click, which is how a pin opens its card.
            tooltip=html.escape(str(row["Name"])),
            icon=folium.DivIcon(
                html=(f'<div class="gwi-mk">{_pin_svg(BRAND_MED, count=n_sites if n_sites > 1 else 0)}'
                      f'<span class="gwi-callout">{html.escape(str(row["Name"]))}</span></div>'),
                icon_size=(25, 41), icon_anchor=(12 - dx, 41), class_name=f"empty gwi-m-{_org_id(row['Name'])}"),
        ).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    return m


def _selection_layer(row) -> folium.FeatureGroup:
    """Drawn over the base map without redrawing it: a ring on the chosen
    pin, and its other sites as numbered pins that match the card's list."""
    fg = folium.FeatureGroup(name="selected")
    lat, lon = float(row["Latitude"]), float(row["Longitude"])
    folium.CircleMarker([lat, lon], radius=22, color=ACCENT, weight=3, fill=True,
                        fill_color=ACCENT, fill_opacity=0.12).add_to(fg)
    locs = [l for l in _org_locations(row["Name"]) if l.get("Kind") != "admin"]
    if len(locs) > 1:
        for n, loc in enumerate(locs, 1):
            try:
                la, lo = float(loc["Latitude"]), float(loc["Longitude"])
            except (TypeError, ValueError, KeyError):
                continue
            folium.Marker([la, lo], tooltip=html.escape(loc.get("LocationName") or "") or None,
                          icon=folium.DivIcon(html=_pin_svg(ACCENT, number=str(n)), icon_size=(25, 41),
                                              icon_anchor=(12, 41), class_name="empty")).add_to(fg)
    return fg


@st.cache_data(show_spinner=False, max_entries=300)
def _mini_map_html(name: str, lat: float, lon: float, sites: tuple, color: str) -> str:
    """A small static map for the details tab, built once per org."""
    mini = folium.Map(location=[lat, lon], zoom_start=15, tiles=None, zoom_control=True)
    _add_basemap(mini)
    mini.get_root().header.add_child(folium.Element(
        "<style>.leaflet-control-attribution{font-size:10px!important}</style>" + _MAP_DARK_CSS))
    # Names and site labels come from the database; escape them, since this
    # HTML runs in an iframe with access to the page.
    folium.Marker([lat, lon], tooltip=html.escape(name), icon=folium.DivIcon(
        html=_pin_svg(color), icon_size=(25, 41), icon_anchor=(12, 41), class_name="empty")).add_to(mini)
    for n, (la, lo, label) in enumerate(sites, 1):
        folium.Marker([la, lo], tooltip=html.escape(label) if label else None, icon=folium.DivIcon(
            html=_pin_svg(color, number=str(n)), icon_size=(25, 41), icon_anchor=(12, 41),
            class_name="empty")).add_to(mini)
    return mini.get_root().render()


_EXPLORE_CSS = """
<style>
/* The working surface: list and map framed as one window. */
.st-key-gwi_explore { background:__WHITE__; border:1px solid __BORDER__; border-radius:20px;
  overflow:hidden; padding:0 !important; gap:0 !important; }
.st-key-gwi_explore > div > [data-testid="stHorizontalBlock"],
.st-key-gwi_explore [data-testid="stHorizontalBlock"]:has(.st-key-gwi_left) { gap:0 !important; }
.st-key-gwi_explore [data-testid="stColumn"]:has(.st-key-gwi_left) { border-right:1px solid __BORDER__;
  background:__WHITE__; }
.st-key-gwi_explore iframe { border-radius:0 !important; display:block; }
.st-key-gwi_left { padding:0 !important; gap:0 !important; }

/* List header: count, how many are open now, and the sort. */
.st-key-gwi_head { position:sticky; top:0; z-index:5; background:__WHITE__; padding:16px 18px 12px !important;
  gap:10px !important; border-bottom:1px solid __RULE__; }
.gwi-head-line { display:flex; align-items:baseline; justify-content:space-between; gap:12px; padding:2px 0 4px; line-height:1.35; }
.st-key-gwi_head [data-testid="stElementContainer"]:has(.gwi-head-line) { min-height:32px; overflow:visible; }
.gwi-head-t { font-size:18px !important; font-weight:700; color:__INK__; margin:0 !important; letter-spacing:-.01em; }
.gwi-head-s { font-size:13px !important; color:__MUTED__; margin:0 !important; display:flex; align-items:center; gap:6px; }
.gwi-head-s::before { content:""; width:7px; height:7px; border-radius:50%; background:__OPEN_FILL__; }
.gwi-head-s b { color:__OPEN__; font-weight:650; }
/* An iOS-style segmented control: a grey track, the choice a white pill. */
.st-key-rail_sort { width:100% !important; }
.st-key-rail_sort [data-testid="stButtonGroup"] { background:__TINT__; border-radius:10px; padding:2px; width:100%; }
.st-key-rail_sort [role="radiogroup"] { display:flex !important; width:100% !important; max-width:none !important;
  gap:2px !important; flex-wrap:nowrap !important; }
.st-key-rail_sort button { flex:1 1 0 !important; min-width:0 !important; border:0 !important; background:transparent !important;
  border-radius:8px !important; min-height:34px !important; padding:2px 8px !important; box-shadow:none !important; }
.st-key-rail_sort button p { white-space:nowrap; }
.st-key-gwi_head .stMarkdown, .st-key-gwi_head [data-testid="stMarkdown"] { margin-bottom:0 !important; }
.st-key-rail_sort button p { font-size:13px !important; font-weight:600 !important; color:__MUTED__ !important; }
.st-key-rail_sort button[kind$="Active"], .st-key-rail_sort button[aria-checked="true"],
.st-key-rail_sort button[aria-pressed="true"] { background:__WHITE__ !important;
  box-shadow:0 1px 3px rgba(19,35,42,.14) !important; }
.st-key-rail_sort button[kind$="Active"] p, .st-key-rail_sort button[aria-checked="true"] p,
.st-key-rail_sort button[aria-pressed="true"] p { color:__INK__ !important; }
.st-key-near input { font-size:15px !important; }

/* Rows: a monogram slot, name and distance, kind and town, status, today. */
[class*="st-key-row_"] { position:relative; padding:0 !important; gap:0 !important; }
/* Streamlit sizes a button's wrapper to fit its content, so the wrapper is
   stretched explicitly: the whole row, edges included, is one tap target. */
[class*="st-key-pick_"] { position:absolute !important; inset:0; z-index:3; margin:0 !important;
  width:100% !important; height:100% !important; max-width:none !important; }
[class*="st-key-pick_"] > div, [class*="st-key-pick_"] [data-testid="stButton"] { width:100% !important; height:100% !important; }
[class*="st-key-pick_"] button { position:absolute; inset:0; width:100% !important; height:100% !important; opacity:0;
  cursor:pointer; border:0 !important; min-height:0 !important; margin:0 !important; }
[class*="st-key-row_"]:has(button:focus-visible) .gwi-row { outline:2px solid __ACCENT__; outline-offset:-2px; }
.gwi-row { display:flex; gap:12px; align-items:flex-start; padding:13px 18px 14px; border-bottom:1px solid __RULE__;
  transition:background .15s ease; }
[class*="st-key-row_"]:hover .gwi-row, .gwi-row.is-hot { background:__HOVER__; }
.st-key-gwi_bridge { position:absolute !important; width:0; height:0; overflow:hidden; opacity:0; pointer-events:none; }
.gwi-row.is-sel { background:__ACCENT_TINT__; }
.gwi-row p { margin:0 !important; }
.gwi-mono { flex:none; width:40px; height:40px; border-radius:12px; display:flex; align-items:center;
  justify-content:center; font-size:14px; font-weight:700; letter-spacing:.01em; }
.gwi-mono--lg { width:52px; height:52px; border-radius:15px; font-size:18px; }
.gwi-row-main { flex:1; min-width:0; display:grid; gap:2px; }
.gwi-row-top { display:flex; align-items:baseline; justify-content:space-between; gap:10px; }
.gwi-row-name { font-size:15.5px !important; font-weight:650; color:__INK__; line-height:1.3;
  overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
.gwi-row-dist { flex:none; font-size:13px; font-weight:600; color:__MUTED__; font-variant-numeric:tabular-nums; }
.gwi-row-meta { font-size:13px !important; color:__MUTED__; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.gwi-row-status { font-size:13px !important; display:flex; align-items:center; gap:6px; margin-top:3px !important;
  color:__MID__; }
.gwi-row-status::before { content:""; width:7px; height:7px; border-radius:50%; background:var(--st); flex:none; }
.gwi-row-status b { font-weight:650; color:var(--st); white-space:nowrap; }
.gwi-st-tail { margin-left:-3px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.gwi-row-status.is-open { --st:__OPEN__; }
.gwi-row-status.is-soon { --st:__SOON__; }
.gwi-row-status.is-closed { --st:__CLOSED__; }
.gwi-row-status.is-unknown { --st:__UNLISTED__; }
.gwi-row-status.is-unknown b { font-weight:500; }
.gwi-row-status.is-unknown::before { background:transparent; border:1.5px solid var(--st); width:6px; height:6px; }
.gwi-row-why { font-size:13px !important; color:__MID__; line-height:1.4; margin-top:4px !important;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.gwi-row-why svg { color:__ACCENT__; vertical-align:-2px; margin-right:4px; }

/* The today strip: when a place is open today, and where now falls. */
.gwi-day-wrap { margin-top:6px; }
.gwi-day-line { display:flex; align-items:center; gap:8px; }
.gwi-day { position:relative; flex:1; height:6px; border-radius:3px; background:__TRACK__; max-width:260px; }
.gwi-day-open { position:absolute; top:0; bottom:0; border-radius:3px; background:__SOFT__; }
.gwi-day-open.is-now.is-open { background:__OPEN_FILL__; }
.gwi-day-open.is-now.is-soon { background:__SOON_FILL__; }
/* After closing time the strip shows the next day it opens, named. */
.gwi-day-tag { flex:none; font-size:11.5px; font-weight:600; color:__MUTED__; }
.gwi-day-wrap.is-ahead .gwi-day { opacity:.85; }
.gwi-day-now { position:absolute; top:-3px; width:2px; height:12px; border-radius:1px; background:__INK__;
  transform:translateX(-1px); }
.gwi-day-scale { display:flex; justify-content:space-between; max-width:none; margin-top:5px;
  font-size:11.5px; color:__MUTED__; font-variant-numeric:tabular-nums; }
.gwi-place .gwi-day { max-width:none; height:8px; border-radius:4px; }
.gwi-place .gwi-day-now { height:14px; }

.st-key-list_more button, .st-key-rail_back button { border-radius:999px !important; }
.st-key-list_more { padding:12px 18px 16px !important; }
/* The back bar spans the pane and is frosted, like an iOS navigation bar:
   the card scrolls softly underneath instead of behind a white patch. */
.st-key-rail_back { position:sticky; top:0; z-index:5; width:100% !important; max-width:none !important;
  padding:8px 12px !important; margin:0 !important; background:rgba(255,255,255,.86);
  -webkit-backdrop-filter:saturate(180%) blur(20px); backdrop-filter:saturate(180%) blur(20px);
  border-bottom:1px solid __RULE__; }
.st-key-rail_back, .st-key-rail_back [data-testid="stButton"] { display:flex !important; justify-content:flex-start !important; }
.st-key-rail_back button { width:auto !important; }
.st-key-rail_back button { background:transparent !important; border:0 !important; padding:6px 10px !important;
  min-height:36px !important; border-radius:10px !important; transition:background .15s ease; }
.st-key-rail_back button:hover { background:__ACCENT_TINT__ !important; }
.st-key-rail_back button p { color:__ACCENT__ !important; font-weight:650 !important; font-size:15px !important; }

.st-key-gwi_card_body { padding:14px 22px 22px !important; }

/* Place card. */
.gwi-place { display:grid; gap:16px; }
.gwi-place p { margin:0 !important; }
.gwi-place-hd { display:flex; gap:14px; align-items:center; background:var(--tint); color:var(--tint-ink);
  border-radius:16px; padding:16px; }

.gwi-place-id { min-width:0; }
.gwi-place-name { font-family:__DISPLAY__ !important; font-size:22px !important; font-weight:700 !important;
  letter-spacing:-.02em; line-height:1.2 !important; margin:0 !important; padding:0 !important; color:__INK__ !important; }
.gwi-place-meta { font-size:14px !important; color:var(--tint-ink); margin-top:3px !important; font-weight:600; }
.gwi-place-now { display:grid; gap:6px; }
.gwi-place-now .gwi-row-status { font-size:15px !important; margin:0 !important; }
.gwi-place .gwi-checked { font-size:12.5px; color:__MUTED__; }
.gwi-place .gwi-acts { display:flex; gap:8px; flex-wrap:wrap; margin:0; padding:0; }
.gwi-place-about { font-size:15.5px !important; line-height:1.6; color:__INK__; }
.gwi-group { background:__TINT__; border-radius:14px; padding:4px 14px; }
.gwi-group-row { display:flex; gap:12px; padding:11px 0; border-bottom:1px solid __TRACK__; color:__MUTED__; }
.gwi-group-row:last-child { border-bottom:0; }
.gwi-group-row svg { flex:none; margin-top:2px; }
.gwi-group-k { font-size:12.5px !important; color:__MUTED__; font-weight:600; }
.gwi-group-v { font-size:14.5px !important; color:__INK__; line-height:1.45; margin-top:1px !important; }
.gwi-place-sec h3 { font-size:15px !important; font-weight:700 !important; color:__INK__ !important;
  margin:0 0 8px !important; padding:0 !important; letter-spacing:-.005em; }
.gwi-week-svc { font-size:13.5px !important; font-weight:650; color:__INK__; margin:8px 0 2px !important; }
.gwi-week { display:grid; }
.gwi-week-row { display:flex; justify-content:space-between; gap:12px; padding:5px 8px; border-radius:8px;
  font-size:14px; color:__MID__; font-variant-numeric:tabular-nums; }
.gwi-week-row span:last-child { text-align:right; }
.gwi-week-row.is-today { background:__ACCENT_TINT__; color:__INK__; font-weight:650; }
.gwi-week-note { font-size:12.5px !important; color:__MUTED__; margin:4px 8px 0 !important; }
.gwi-place-src { font-size:12.5px !important; margin:6px 8px 0 !important; }
.gwi-svc-group + .gwi-svc-group { margin-top:10px; }
.gwi-svc-group > p { font-size:13px !important; font-weight:600; color:__MUTED__; margin:0 0 6px !important; }
.gwi-tags { display:flex; flex-wrap:wrap; gap:6px; }
.gwi-tags span { background:__TINT__; color:__INK__; border-radius:999px; padding:4px 11px; font-size:13px; }
.gwi-locs { list-style:none; margin:0; padding:0; display:grid; gap:10px; }
.gwi-locs li { display:flex; gap:10px; font-size:14px; }
.gwi-locs li > div { display:grid; gap:1px; }
.gwi-locs li span:not(.gwi-loc-n) { color:__MID__; font-size:13px; }
.gwi-loc-n { flex:none; width:22px; height:22px; border-radius:50%; border:2px solid __ACCENT__; color:__ACCENT__;
  font-size:12px; font-weight:700; display:flex; align-items:center; justify-content:center; }
.gwi-place-reports { font-size:13.5px !important; color:__MUTED__; }

/* Under the map: what the pin colours mean, with today's counts. */
.gwi-legend { display:flex; flex-wrap:wrap; gap:6px 16px; padding:10px 16px 12px; font-size:12.5px !important;
  color:__MID__; background:__WHITE__; border-top:1px solid __RULE__; margin:0 !important; }
.gwi-legend span { display:inline-flex; align-items:center; gap:6px; }
.gwi-legend i { width:9px; height:9px; border-radius:50%; display:inline-block; }
.gwi-legend em { font-style:normal; color:__MUTED__; font-size:12px; flex-basis:100%; }

/* Details tab: the same card, with room for the map beside it. */
.st-key-gwi_detail_card { background:__WHITE__; border:1px solid __BORDER__; border-radius:20px;
  padding:18px 20px 20px !important; }
.st-key-gwi_detail_side iframe { border-radius:16px !important; }
.gwi-detail-contact { display:grid; gap:10px; margin-top:12px; }
.gwi-detail-contact p { margin:0 !important; font-size:14.5px !important; color:__INK__; display:grid; gap:1px; }
.gwi-detail-contact span { font-size:12.5px; font-weight:600; color:__MUTED__; }

/* List | Map, shown on phones only. */
[data-testid="stElementContainer"]:has(.gwi-views) { display:none; }
.gwi-views button { flex:1; display:inline-flex; align-items:center; justify-content:center; gap:7px;
  min-height:44px; border:0; border-radius:10px; background:transparent; color:__MUTED__;
  font:600 15px/1 __FONT__; cursor:pointer; }
.gwi-views button[data-view="list"], body.gwi-view-map .gwi-views button[data-view="map"] {
  background:__WHITE__; color:__INK__; box-shadow:0 1px 3px rgba(19,35,42,.14); }
body.gwi-view-map .gwi-views button[data-view="list"] { background:transparent; color:__MUTED__; box-shadow:none; }

/* Brings the search box back once it has scrolled off the top. */
.gwi-jump { position:fixed; top:14px; left:50%; z-index:999990; transform:translate(-50%,-14px); opacity:0;
  pointer-events:none; max-width:min(78vw,420px); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  display:inline-flex; align-items:center; gap:8px; padding:10px 18px 10px 40px; min-height:44px;
  border:1px solid rgba(19,35,42,.08); border-radius:999px; cursor:pointer;
  background:rgba(255,255,255,.86) __SEARCH_ICON__ no-repeat 15px 50%/17px;
  -webkit-backdrop-filter:saturate(180%) blur(20px); backdrop-filter:saturate(180%) blur(20px);
  box-shadow:0 8px 28px rgba(19,35,42,.16); color:__INK__; font:600 15px/1.2 __FONT__;
  transition:opacity .2s ease, transform .3s cubic-bezier(.34,1.56,.64,1); }
.gwi-jump.is-on { opacity:1; transform:translate(-50%,0); pointer-events:auto; }

/* While a new search runs: placeholders in the results' shape; the previous
   results (marked stale by Streamlit) are hidden instead of looking current. */
body:has(.gwi-skel) [data-stale="true"], body:has(.gwi-skel) [data-testid="stTabs"] { display:none !important; }
.gwi-skel-msg { display:flex; align-items:center; gap:8px; margin:14px 0 12px !important; font-size:15px !important;
  font-weight:600; color:__ACCENT__; }
.gwi-skel-win { display:grid; grid-template-columns:5fr 7fr; height:__PANE__px; background:__WHITE__;
  border:1px solid __BORDER__; border-radius:20px; overflow:hidden; }
.gwi-skel-list { border-right:1px solid __BORDER__; padding-top:96px; }
.gwi-skel-row { display:flex; gap:12px; padding:14px 18px 16px; border-bottom:1px solid __RULE__; }
.gwi-skel-row > div { flex:1; display:grid; gap:8px; padding-top:2px; }
.gwi-skel i, .gwi-skel-map { display:block; height:11px; border-radius:6px; background:__TRACK__; }
.gwi-skel i.gwi-skel-tile { flex:none; width:40px; height:40px; border-radius:12px; }
.gwi-skel-map { height:100%; border-radius:0; background:__TINT__; }
.gwi-skel i, .gwi-skel-map { background-image:linear-gradient(90deg, transparent 0, rgba(255,255,255,.55) 50%, transparent 100%);
  background-size:220px 100%; background-repeat:no-repeat; animation:gwi-shimmer 1.3s ease-in-out infinite; }
@keyframes gwi-shimmer { from { background-position:-220px 0; } to { background-position:calc(100% + 220px) 0; } }
@media (prefers-reduced-motion: reduce) { .gwi-skel i, .gwi-skel-map { animation:none; background-image:none; } }

/* Phones get the card's actions in a bar at the bottom of the screen. */
.gwi-dock { display:none; }

/* Keyboard focus is always visible. */
.gwi-act:focus-visible, .gwi-btn:focus-visible, .gwi-views button:focus-visible, .gwi-jump:focus-visible,
.gwi-place a:focus-visible { outline:3px solid __ACCENT__ !important; outline-offset:2px; }

@media (max-width: 640px) {
  [data-testid="stElementContainer"]:has(.gwi-views) { display:block; }
  .gwi-views { display:flex; gap:2px; padding:2px; margin:0 0 12px; background:__TINT__; border-radius:12px; }
  /* clip, not hidden: still rounds the corners, but lets the back bar stick
     to the top of the screen as the page scrolls. */
  .st-key-gwi_explore { border-radius:16px; overflow:clip !important; }
  .gwi-skel-win { grid-template-columns:1fr; height:auto; border-radius:16px; }
  .gwi-skel-list { border-right:0; padding-top:12px; }
  .gwi-skel-map { display:none; }
  /* One scroll: the list grows with the page instead of scrolling inside it. */
  .st-key-gwi_left, div:has(> .st-key-gwi_left) { height:auto !important; max-height:none !important;
    overflow:visible !important; }
  .st-key-gwi_explore [data-testid="stColumn"]:has(.st-key-gwi_left) { border-right:0; }
  body.gwi-view-map .st-key-gwi_explore [data-testid="stColumn"]:has(.st-key-gwi_left),
  body:not(.gwi-view-map) .st-key-gwi_explore [data-testid="stColumn"]:not(:has(.st-key-gwi_left)) { display:none !important; }
  .st-key-gwi_head { position:static; }
  .gwi-row { padding:12px 14px; }
  .st-key-gwi_card_body { padding:4px 14px 16px !important; }
  /* Touch targets of at least 44pt, as Apple asks. */
  .st-key-rail_sort button, .st-key-lang_radio button { min-height:44px !important; }
  .gwi-place .gwi-act { min-height:44px; }
  .gwi-dock { display:flex; gap:8px; position:fixed; left:0; right:0; bottom:0; z-index:999980;
    padding:10px 14px calc(10px + env(safe-area-inset-bottom));
    background:rgba(255,255,255,.88); -webkit-backdrop-filter:saturate(180%) blur(20px);
    backdrop-filter:saturate(180%) blur(20px); border-top:1px solid rgba(19,35,42,.08); }
  .gwi-dock .gwi-act { flex:1; justify-content:center; min-height:46px; margin:0; }
  body.gwi-card-open [data-testid="stMain"] { padding-bottom:84px; }
  .gwi-place .gwi-acts { display:none; }
  /* The search pill sits at the bottom, in thumb reach, and steps aside
     while a place card (and its action bar) is open. */
  .gwi-jump { top:auto; bottom:calc(18px + env(safe-area-inset-bottom)); transform:translate(-50%,14px); }
  body.gwi-card-open .gwi-jump { display:none; }
}
</style>
"""


_SEARCH_BG = ("url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
              "stroke='%235c6f75' stroke-width='2.4' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E"
              "%3Cpath d='M16 16l5 5'/%3E%3C/svg%3E\")")


# In the dark, a few colours change role rather than just shade. White text on
# the bright dark-mode teal is unreadable, so filled buttons take dark text
# (as Apple's tinted buttons do); the hero keeps its deep teal under white.
_DARK_EXTRA_CSS = "<style>" + _dark("""
  .gwi-skel i, .gwi-skel-map { background-image:linear-gradient(90deg, transparent 0, rgba(255,255,255,.06) 50%, transparent 100%); }
  .gwi-theme-btn .gwi-ic-moon { display:none; }
  .gwi-theme-btn .gwi-ic-sun { display:flex; }
  .gwi-btn--fill { background:linear-gradient(180deg,#6fd0c4 0%,#4fb8ab 100%) !important; color:#06201d !important;
    box-shadow:0 8px 20px -12px rgba(0,0,0,.6) !important; }
  .gwi-act--primary, .gwi-act--primary:hover { color:#06201d !important; }
  .st-key-gwi_chips button[kind="primary"] p, .st-key-gwi_chips button[kind="primary"]::after,
  .st-key-gwi_chips button[kind="primary"]::before { color:#06201d !important; }
  .gwi-hero { background:#0f3b38; box-shadow:none; }
  .gwi-hero-tx { background:radial-gradient(120% 90% at 0% 0%, #1d6d66 0%, #0f3b38 70%); }
  .gwi-hero-img { filter:brightness(.82) saturate(.9); }
  .st-key-gwi_filters { box-shadow:0 20px 40px -24px rgba(0,0,0,.7); }
  .gwi-jump { border-color:rgba(255,255,255,.08); box-shadow:0 8px 28px rgba(0,0,0,.5); }
  .gwi-dock { border-top-color:rgba(255,255,255,.08); }
  /* Segmented controls: in the dark the chosen segment is the lighter one. */
  [data-testid="stTab"][aria-selected="true"], .gwi-views button[data-view="list"],
  body.gwi-view-map .gwi-views button[data-view="map"],
  .st-key-rail_sort button[kind$="Active"], .st-key-rail_sort button[aria-checked="true"],
  .st-key-rail_sort button[aria-pressed="true"] {
    background:#3a474b !important; box-shadow:0 1px 3px rgba(0,0,0,.5) !important; }
  body.gwi-view-map .gwi-views button[data-view="list"] { background:transparent !important; box-shadow:none !important; }
""") + "</style>"


def _explore_css() -> str:
    tokens = {"__WHITE__": BG_WHITE, "__BORDER__": BORDER, "__RULE__": "#edf2f2", "__INK__": INK,
              "__MID__": TEXT_MID, "__MUTED__": TEXT_MUTED, "__ACCENT__": ACCENT, "__ACCENT_TINT__": ACCENT_TINT,
              "__TINT__": BG_TINT, "__HOVER__": "#f1f5f5", "__OPEN__": STATUS_TEXT["open"], "__CLOSED__": STATUS_TEXT["closed"],
              "__UNLISTED__": STATUS_TEXT["unknown"], "__OPEN_FILL__": STATUS_FILL["open"], "__SOON__": STATUS_TEXT["soon"], "__SOON_FILL__": STATUS_FILL["soon"], "__SOFT__": "#cdd6d8",
              "__TRACK__": "#e3eaeb", "__DISPLAY__": DISPLAY_STACK,
              "__FONT__": FONT_STACK, "__SEARCH_ICON__": _SEARCH_BG,
              "__PANE__": str(PANE_H)}
    css = _EXPLORE_CSS
    for k, v in tokens.items():
        css = css.replace(k, v)
    return css


st.markdown(_themed(_explore_css()), unsafe_allow_html=True)
st.markdown(_DARK_EXTRA_CSS, unsafe_allow_html=True)


@st.fragment
def _results(filtered: pd.DataFrame, search_reasons: dict, search: str, has_filters: bool, n_filtered: int):
    """Everything below the filters. A fragment: a click in here reruns only this."""
    tab_map, tab_dir, tab_detail = st.tabs([_("tab_map"), _("tab_directory"), _("tab_org_detail")])

    # ══════════════════════════════════════════════════════════
    # TAB 1 — MAP: the list (or a place card) beside the map
    # ══════════════════════════════════════════════════════════
    with tab_map:
        map_data = filtered.dropna(subset=["Latitude", "Longitude"])
        if filtered.empty:
            _empty_state()
        else:
            names_now = set(filtered["Name"])
            if st.session_state.get("sel_org") not in names_now:
                st.session_state["sel_org"] = None
            st.markdown(
                f'<div class="gwi-views" role="group" aria-label="{_("view_switch")}" '
                f'data-jump="{_("jump_search")}" data-jump-q="{html.escape(_("jump_search_q"))}">'
                f'<button type="button" data-view="list" aria-pressed="true">{_ICON_LIST}{_("view_list")}</button>'
                f'<button type="button" data-view="map" aria-pressed="false">{_ICON_MAP}{_("view_map")}</button></div>',
                unsafe_allow_html=True)
            with st.container(key="gwi_bridge"):
                st.iframe(_HOVER_BRIDGE, height=1)
            with st.container(key="gwi_explore"):
                col_list, col_map = st.columns([5, 7], gap="small")

                # The map is drawn first so a pin click can choose the card
                # the left pane shows in this same run.
                with col_map:
                    if map_data.empty:
                        st.info(_("no_coords"))
                    else:
                        sel = st.session_state.get("sel_org")
                        sel_row = map_data[map_data["Name"] == sel]
                        if not sel_row.empty:
                            center = [float(sel_row.iloc[0]["Latitude"]), float(sel_row.iloc[0]["Longitude"])]
                            zoom, layer = 16, _selection_layer(sel_row.iloc[0])
                        else:
                            center, zoom = (_fit_view(map_data["Latitude"], map_data["Longitude"])
                                            if has_filters else ([42.7070, -71.1631], 13))
                            layer = None
                        out = st_folium(
                            _explore_map(map_data), key="explore_map", width="stretch", height=PANE_H - 66,
                            center=center, zoom=zoom, feature_group_to_add=layer,
                            returned_objects=["last_object_clicked_tooltip"],
                        )
                        tip = ((out or {}).get("last_object_clicked_tooltip") or "").strip()
                        if tip and tip != st.session_state.get("_last_pin"):
                            st.session_state["_last_pin"] = tip
                            if tip in names_now and tip != st.session_state.get("sel_org"):
                                st.session_state["sel_org"] = tip
                                st.rerun(scope="fragment")
                        now = _now_local()
                        t_now = f'{_day_names()[now.weekday()]} {now.strftime("%-I:%M %p")}'
                        st.markdown(f'<p class="gwi-legend"><em>{_("status_asof").format(t=t_now)}</em></p>',
                                    unsafe_allow_html=True)

                with col_list:
                    with st.container(height=PANE_H, key="gwi_left", border=False):
                        sel = st.session_state.get("sel_org")
                        if sel:
                            st.button("← " + _("back_to_results"), key="rail_back", on_click=_unpick_org)
                            with st.container(key="gwi_card_body"):
                                prow = filtered[filtered["Name"] == sel].iloc[0]
                                st.markdown(_place_card_html(prow), unsafe_allow_html=True)
                                with st.expander(_("share_one")):
                                    st.code(_share_text(filtered[filtered["Name"] == sel]), language=None, wrap_lines=True)
                        else:
                            ranked = bool(search)
                            sort_opts = ["best" if ranked else "az", "near", "open"]
                            sort_names = {"best": _("sort_best"), "az": _("sort_az"),
                                          "near": _("sort_near"), "open": _("sort_open")}
                            if st.session_state.get("rail_sort") not in sort_opts:
                                st.session_state["rail_sort"] = sort_opts[0]
                            with st.container(key="gwi_head"):
                                n_open = sum(_status_parts(h)[0] in OPEN_STATES for h in filtered["Hours"])
                                n_txt = _("rail_count") if has_filters else _("rail_count_all")
                                st.markdown(
                                    f'<div class="gwi-head-line"><p class="gwi-head-t">{n_txt.format(n=n_filtered)}</p>'
                                    f'<p class="gwi-head-s">{_("open_now_count").format(n=f"<b>{n_open}</b>")}</p></div>',
                                    unsafe_allow_html=True)
                                sort = st.segmented_control(
                                    _("sort_label"), sort_opts, format_func=sort_names.get,
                                    key="rail_sort", label_visibility="collapsed") or sort_opts[0]
                                near = ""
                                if sort == "near":
                                    near = st.text_input(_("near_label"), placeholder=_("near_placeholder"),
                                                         key="near", label_visibility="collapsed")

                            listed, dist = filtered, {}
                            if sort == "near" and near.strip():
                                origin = _geocode(near)
                                if origin is None:
                                    st.caption(_("near_not_found"))
                                else:
                                    for _ix, r in filtered.iterrows():
                                        d = _nearest_miles(r, origin)
                                        if d is not None:
                                            dist[r["Name"]] = d
                                    listed = filtered.loc[sorted(filtered.index,
                                                                 key=lambda i: dist.get(filtered.at[i, "Name"], 1e9))]
                            elif sort == "open":
                                rank_open = {"open": 0, "soon": 0, "closed": 1, "unknown": 2}
                                listed = filtered.loc[sorted(filtered.index,
                                                             key=lambda i: rank_open[_status(filtered.at[i, "Hours"])[0]])]
                            elif sort == "az":
                                listed = filtered.sort_values("Name", key=lambda c: c.str.lower())

                            # Draw a page of rows at a time: every row is a real
                            # button, and fewer of them keeps clicks quick.
                            sig = (tuple(listed["Name"]), st.session_state["lang"])
                            if st.session_state.get("_list_sig") != sig:
                                st.session_state["_list_sig"] = sig
                                st.session_state["list_n"] = LIST_CAP
                            shown = listed.head(st.session_state["list_n"])
                            for i, (_ix, r) in enumerate(shown.iterrows(), 1):
                                with st.container(key=f"row_{i}"):
                                    st.markdown(_row_html(r, dist.get(r["Name"]), False), unsafe_allow_html=True)
                                    st.button(str(r["Name"]), key=f"pick_{i}", on_click=_pick_org, args=(r["Name"],))
                            rest = len(listed) - len(shown)
                            if rest > 0:
                                with st.container(key="list_more"):
                                    if st.button(_("show_more").format(n=min(rest, LIST_CAP)), key="list_more_btn"):
                                        st.session_state["list_n"] += LIST_CAP
                                        st.rerun(scope="fragment")
            if any(n in search_reasons for n in filtered["Name"]):
                st.caption(_("ai_matches_note"))
            with st.expander(_("share_title")):
                picks = st.multiselect(_("share_pick"), list(filtered["Name"]), key="share_picks",
                                       placeholder=_("share_placeholder"))
                if picks:
                    st.caption(_("share_help"))
                    st.code(_share_text(filtered[filtered["Name"].isin(picks)]), language=None, wrap_lines=True)

    # ══════════════════════════════════════════════════════════
    # TAB 2 — DIRECTORY
    # ══════════════════════════════════════════════════════════
    with tab_dir:
        if filtered.empty:
            _empty_state()
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
                width="stretch",
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
    # TAB 3 — ORGANIZATION DETAILS
    # ══════════════════════════════════════════════════════════
    # Any one organization in full, picked by name: the same card as the Map
    # tab, with its map beside it. Follows the org picked on the Map tab.
    with tab_detail:
        if filtered.empty:
            _empty_state()
        else:
            names = sorted(filtered["Name"], key=str.lower)
            picked_on_map = st.session_state.get("sel_org")
            if picked_on_map in names and picked_on_map != st.session_state.get("_detail_follow"):
                st.session_state["_detail_follow"] = picked_on_map
                st.session_state["detail_select"] = picked_on_map
            if st.session_state.get("detail_select") not in names:
                st.session_state["detail_select"] = names[0]
            choice = st.selectbox(_("select_org"), names, key="detail_select")
            drow = filtered[filtered["Name"] == choice].iloc[0]
            d_main, d_side = st.columns([7, 5], gap="medium")
            with d_main:
                with st.container(key="gwi_detail_card", border=False):
                    st.markdown(_place_card_html(drow), unsafe_allow_html=True)
                    with st.expander(_("share_one")):
                        st.code(_share_text(filtered[filtered["Name"] == choice]), language=None, wrap_lines=True)
            with d_side:
                with st.container(key="gwi_detail_side", border=False):
                    if pd.notna(drow["Latitude"]) and pd.notna(drow["Longitude"]):
                        sites = []
                        locs = [l for l in _org_locations(drow["Name"]) if l.get("Kind") != "admin"]
                        if len(locs) > 1:
                            for loc in locs:
                                try:
                                    sites.append((float(loc["Latitude"]), float(loc["Longitude"]),
                                                  loc.get("LocationName", "")))
                                except (TypeError, ValueError, KeyError):
                                    pass
                        st.iframe(
                            _mini_map_html(str(drow["Name"]), float(drow["Latitude"]), float(drow["Longitude"]),
                                           tuple(sites), BRAND_MED),
                            height=380)
                    else:
                        st.info(_("no_map_coords"))
                    addr = ", ".join(p for p in (str(drow.get(k, "")).strip() for k in ("Address", "City", "State", "Zip")) if p)
                    phone = str(drow.get("Phone", "")).strip()
                    st.markdown(
                        '<div class="gwi-detail-contact">'
                        + (f'<p><span>{_("popup_address")}</span>{html.escape(addr)}</p>' if addr else "")
                        + (f'<p><span>{_("sec_phone")}</span>{_tel(phone)}</p>' if phone else "")
                        + "</div>",
                        unsafe_allow_html=True,
                    )


_results(filtered, search_reasons, search, has_filters, n_filtered)


# ── footer ────────────────────────────────────────────────────────────────────
st.markdown(
    f'<footer class="gwi-foot"><p>{_("footer_checked")} '
    f'<a {feedback_attrs} style="{feedback_extra}">{_("footer_missing")}</a></p>'
    f'<p class="gwi-foot-credit"><a href="{HERO_PHOTO_PAGE}" target="_blank" '
    f'rel="noopener noreferrer">{_("footer_photo")}</a></p></footer>',
    unsafe_allow_html=True,
)