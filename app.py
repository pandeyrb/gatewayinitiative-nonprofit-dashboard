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

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="GWI Nonprofit Partner Explorer",
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

GEMINI_GEM_URL = "https://gemini.google.com/gem/ca6a37604b8a?usp=sharing"

# Category palette — aligned with CATEGORY_MAP below
CAT_COLORS = {
    "Education": "#e63946",
    "Youth Development": "#f4a261",
    "Economic Mobility": "#2a9d8f",
    "Family & Basic Needs": "#e9c46a",
    "Health & Wellness": "#457b9d",
    "Justice, Legal & Immigration": "#6a0572",
    "Community & Civic Life": "#2d6a4f",
    "Other": "#94a3b8",
    "Unknown": "#cbd5e1",
}

# Mapping from v4 ServiceArea values → broad categories
CATEGORY_MAP = {
    "Education": [
        "Education",
        "Adult education",
        "Adult Education",
        "Literacy",
        "Youth Education",
    ],
    "Youth Development": [
        "Youth Development",
        "Youth Education",
    ],
    "Economic Mobility": [
        "Economic Mobility",
        "Non-Profit Support",
    ],
    "Family & Basic Needs": [
        "Family Services",
        "Food Insecurity",
        "Housing Insecurity",
        "Homelessness",
        "Human Services",
        "Other: Basic needs",
        "Other: Clothing/Personal Growth",
    ],
    "Health & Wellness": [
        "Healthcare",
        "Mental and Behavioral Health",
        "Mental Health",
        "Disability Support",
        "Elder Services",
        "Early Intervention Services",
    ],
    "Justice, Legal & Immigration": [
        "Legal services",
        "Legal Services",
    ],
    "Community & Civic Life": [
        "Environmental Justice",
        "Faith Institution",
        "Faith-based Services",
    ],
}


def _smart_split(s: str) -> list[str]:
    """Split on commas that are NOT inside parentheses."""
    return [p.strip() for p in re.split(r",(?![^(]*\))", s) if p.strip()] if s else []


def _get_categories(svc_str: str) -> list[str]:
    """Return all matching broad categories for a service area string."""
    svcs = _smart_split(svc_str)
    matched = set()
    for cat, keywords in CATEGORY_MAP.items():
        for svc in svcs:
            for kw in keywords:
                if kw.lower() in svc.lower():
                    matched.add(cat)
    if not matched:
        return ["Other"] if svcs else ["Unknown"]
    return sorted(matched)


# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown(
    f"""
<style>
  html, body, [class*="css"] {{ color:{TEXT_DARK} !important;
    font-family:"Inter","Segoe UI",Arial,sans-serif; }}
  .stApp {{ background-color:{BG_WHITE}; }}

  section[data-testid="stSidebar"] {{ background-color:{BG_SIDEBAR} !important; }}
  section[data-testid="stSidebar"] * {{ color:{TEXT_DARK} !important; }}
  section[data-testid="stSidebar"] label {{
    color:{TEXT_MID} !important; font-size:12px !important;
    font-weight:600 !important; text-transform:uppercase; letter-spacing:.4px; }}
  section[data-testid="stSidebar"] input,
  section[data-testid="stSidebar"] [data-baseweb="select"] {{
    background:{BG_WHITE} !important; color:{TEXT_DARK} !important;
    border:1px solid {BORDER} !important; border-radius:8px !important; }}
  section[data-testid="stSidebar"] [data-baseweb="select"] * {{
    color:{TEXT_DARK} !important; background:{BG_WHITE} !important; }}

  .block-container {{ padding-top:1.5rem; padding-bottom:2rem; max-width:1400px; }}
  h1,h2,h3,h4,h5,h6 {{ color:{BRAND_DARK} !important; }}

  .filter-pill {{
    display:inline-block; background:#dbeafe; color:#1e40af;
    border-radius:20px; padding:3px 12px; font-size:12px;
    margin:2px 3px; font-weight:600; border:1px solid #bfdbfe; }}

  .cat-badge {{
    display:inline-block; border-radius:20px; padding:2px 10px;
    font-size:11px; font-weight:700; margin:2px; color:white; }}

  .svc-chip {{
    display:inline-block; background:#f1f5f9; color:{TEXT_DARK};
    border:1px solid {BORDER}; border-radius:6px;
    padding:3px 10px; margin:3px; font-size:13px; line-height:1.5; }}

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

    df["SvcList"] = df["ServiceArea"].apply(_smart_split)
    df["CatList"] = df["ServiceArea"].apply(_get_categories)
    if "Services" not in df.columns:
        df["Services"] = ""
    df["SvcTagList"] = df["Services"].apply(_smart_split)

    df = df.rename(columns={"Impact Report": "ImpactReport",
                            "Strategic Plan": "StrategicPlan"})

    for col in ("ImpactReport", "StrategicPlan"):
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
def _link_cell(val: str) -> str: #New Change for v5
    if val and str(val).strip():
        v = str(val).strip()
        href = v if v.startswith("http") else f"https://{v}"
        return (
            f'<a href="{href}" target="_blank" '
            f'style="color:{BRAND_MED};text-decoration:underline;">View</a>'
        )
    return '<span style="color:#94a3b8;">N/A</span>'

_NO_RESULTS = (
    "No organizations match the current filters.  \n"
    "Try adjusting the filters or click **↺ Reset** in the sidebar."
)

# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        f"<p style='font-size:20px;font-weight:800;color:{BRAND_DARK};"
        "margin:0 0 4px;'>🔍 Filters</p>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<p style='font-size:16px;color:{TEXT_MID};margin:0 0 12px;'>"
        f"{len(df)} organizations total</p>",
        unsafe_allow_html=True,
    )
    st.divider()

    search = st.text_input("Search", placeholder="Name, city, or service…", key="search")

    all_services = sorted({s for lst in df["SvcTagList"] for s in lst})
    sel_svcs = st.multiselect(
        "Services",
        all_services,
        key="sel_svcs",
        placeholder="Type to search services…",
    )

    all_org_types = sorted({t for t in df["OrgType"] if t})
    sel_org_type = st.selectbox("Organization Type", ["All"] + all_org_types, key="sel_org_type")

    def _reset_filters():
        st.session_state["search"] = ""
        st.session_state["sel_svcs"] = []
        st.session_state["sel_org_type"] = "All"

    st.divider()
    st.button("↺  Reset all filters", use_container_width=True, on_click=_reset_filters)


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
    active_filters.append(sel_org_type)


# ── page header ───────────────────────────────────────────────────────────────
st.markdown(
    f"<h1 style='color:{BRAND_DARK};font-size:28px;font-weight:800;margin:0 0 6px;'>"
    "GWI Nonprofit Partner Explorer</h1>",
    unsafe_allow_html=True,
)

hdr_l, hdr_r = st.columns([5, 4])
with hdr_l:
    txt = (
        f"Showing all {n_total} organizations"
        if n_filtered == n_total
        else f"Showing {n_filtered} of {n_total} organizations"
    )
    st.markdown(
        f"<p style='color:{TEXT_MID};font-size:18px;margin:0;'>{txt}</p>",
        unsafe_allow_html=True,
    )
with hdr_r:
    st.markdown(
        f'<div style="display:flex;justify-content:flex-end;">'
        f'<a href="{GEMINI_GEM_URL}" target="_blank" rel="noopener noreferrer" '
        f'style="display:inline-flex;align-items:center;gap:8px;'
        f'background:{BRAND_MED};color:white;'
        f'padding:11px 18px;border-radius:8px;font-size:14px;font-weight:700;'
        f'text-decoration:none;">'
        f'Community Resource Navigator ↗</a></div>',
        unsafe_allow_html=True,
    )

st.divider()

# ── tabs ──────────────────────────────────────────────────────────────────────
tab_map, tab_dir, tab_detail = st.tabs(
    ["🗺️  Map", "📋  Directory", "🔎  Organization Detail"]
)


# ══════════════════════════════════════════════════════════
# TAB 1 — MAP
# ══════════════════════════════════════════════════════════
with tab_map:
    map_data = filtered.dropna(subset=["Latitude", "Longitude"])

    if filtered.empty:
        st.warning(_NO_RESULTS)
    elif map_data.empty:
        st.info("Matching organizations have no coordinates to plot.")
    else:
        m = folium.Map(
            location=[42.7070, -71.1631],
            zoom_start=13,
            tiles="OpenStreetMap",
        )

        lawrence_geojson = fetch_lawrence_boundary()
        if lawrence_geojson:
            folium.GeoJson(
                lawrence_geojson,
                style_function=lambda _: {
                    "color": BRAND_DARK,
                    "weight": 3,
                    "fillColor": BRAND_DARK,
                    "fillOpacity": 0.05,
                    "dashArray": "8 5",
                },
            ).add_to(m)

        for _, row in map_data.iterrows():
            # single pin colour — the category legend is gone from the UI, so
            # per-category colours would have nothing to decode them
            pin_color = BRAND_MED
            svc_tags = row["Services"] or "Not specified"
            org_type = row["OrgType"] or "Not specified"
            url = row["URL"]
            
            impact = row.get("ImpactReport", "")
            strategic = row.get("StrategicPlan", "")

            url_html = (
                f'<a href="{url}" target="_blank" '
                f'style="display:inline-block;margin-top:10px;padding:6px 14px;'
                f"background:{BRAND_DARK};color:white;border-radius:6px;"
                f'font-size:12px;font-weight:600;text-decoration:none;">🔗 Visit Website</a>'
                if url
                else '<span style="color:#94a3b8;font-size:12px;">No website listed</span>'
            )

            popup_html = (
                f'<div style="font-family:Inter,sans-serif;width:310px;'
                f'border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.12);">'
                f'<div style="background:{pin_color};padding:14px 16px;">'
                f'<div style="font-size:15px;font-weight:700;color:white;'
                f'line-height:1.3;">{row["Name"]}</div>'
                f"</div>"
                f'<div style="padding:12px 16px;background:white;">'
                f'<table style="width:100%;border-collapse:collapse;font-size:12px;'
                f'color:{TEXT_DARK};">'
                f'<tr><td style="color:#94a3b8;padding:3px 10px 3px 0;font-size:10px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">Address</td>'
                f"<td>{row['Address']}, {row['City']}, {row['State']}</td></tr>"
                f'<tr><td style="color:#94a3b8;padding:3px 10px 3px 0;font-size:10px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">Type</td>'
                f"<td>{org_type}</td></tr>"
                f'<tr><td style="color:#94a3b8;padding:3px 10px 3px 0;font-size:10px;'
                f"font-weight:700;text-transform:uppercase;white-space:nowrap;"
                f'vertical-align:top;">Services</td>'
                f'<td style="color:{TEXT_MID};">{svc_tags}</td></tr>' ##New Change for v5
                f'<tr><td style="color:#94a3b8;padding:3px 10px 3px 0;font-size:10px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">Impact Report</td>'
                f"<td>{_link_cell(impact)}</td></tr>"
                f'<tr><td style="color:#94a3b8;padding:3px 10px 3px 0;font-size:10px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">Strategic Plan</td>'
                f"<td>{_link_cell(strategic)}</td></tr>"
                f"</table>" ##New Change for v5
                f"{url_html}"
                f"</div></div>"
            )

            tooltip_html = (
                f'<div style="font-family:Inter,sans-serif;font-size:13px;'
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

        st_folium(m, use_container_width=True, height=620, returned_objects=[])
        st.caption(
            f"{len(map_data)} organizations plotted · Click a marker for details"
        )


# ══════════════════════════════════════════════════════════
# TAB 2 — DIRECTORY
# ══════════════════════════════════════════════════════════
with tab_dir:
    if filtered.empty:
        st.warning(_NO_RESULTS)
    else:
        dl_col, _ = st.columns([2, 5])
        with dl_col:
            export_df = filtered[
                [
                    "Name",
                    "Address",
                    "City",
                    "State",
                    "Zip",
                    "URL",
                    "OrgType",
                    "ServiceArea",
                    "ImpactReport",
                    "StrategicPlan"
                ]
            ].rename(columns={"OrgType": "Org Type", "ServiceArea": "Service Area"})
            st.download_button(
                f"⬇️  Download {n_filtered} results as CSV",
                data=export_df.to_csv(index=False).encode("utf-8"),
                file_name="gwi_nonprofits_filtered.csv",
                mime="text/csv",
            )

        dir_df = (
            filtered[["Name", "City", "OrgType", "Services", "URL", "ImpactReport", "StrategicPlan"]]
            .rename(columns={"OrgType": "Org Type", "ImpactReport": "Impact Report", "StrategicPlan": "Strategic Plan"})
            .copy()
        )
        for c in ("Impact Report", "Strategic Plan"):
            dir_df[c] = dir_df[c].apply(
                lambda v: "" if not str(v).strip()
                else (str(v) if str(v).startswith("http") else f"https://{v}")
            )
        st.dataframe(
            dir_df,
            use_container_width=True,
            height=520,
            column_config={
                "URL": st.column_config.LinkColumn("Website", display_text="🔗 Open"),
                "Impact Report": st.column_config.LinkColumn("Impact Report", display_text="📊 Open"),
                "Strategic Plan": st.column_config.LinkColumn("Strategic Plan", display_text="🧭 Open"),
            },
            hide_index=True,
        )


# ══════════════════════════════════════════════════════════
# TAB 3 — ORGANIZATION DETAIL
# ══════════════════════════════════════════════════════════
with tab_detail:
    if filtered.empty:
        st.warning(_NO_RESULTS)
    else:
        selected_name = st.selectbox(
            "Select an organization",
            sorted(filtered["Name"].tolist()),
            key="detail_select",
        )
        matches = filtered[filtered["Name"] == selected_name]
        if matches.empty:
            st.warning("Organization not found — please try another selection.")
        else:
            row = matches.iloc[0]
            header_color = BRAND_MED

            st.markdown(
                f'<div style="background:{BG_WHITE};border-radius:12px;'
                f"padding:22px 26px;box-shadow:0 1px 8px rgba(0,0,0,.08);"
                f'border-left:5px solid {header_color};margin-bottom:20px;">'
                f'<h2 style="color:{BRAND_DARK};margin:0;font-size:22px;">'
                f"{row['Name']}</h2>"
                f"</div>",
                unsafe_allow_html=True,
            )

            def section_label(icon, text):
                st.markdown(
                    f"<p style='font-size:11px;font-weight:700;color:{TEXT_MID};"
                    f"text-transform:uppercase;letter-spacing:.5px;margin:16px 0 4px;'>"
                    f"{icon} {text}</p>",
                    unsafe_allow_html=True,
                )

            c1, c2 = st.columns(2)

            with c1:
                section_label("📍", "Location")
                addr_parts = [row["Address"], row["City"], row["State"], row["Zip"]]
                st.write(", ".join(p for p in addr_parts if p) or "Not available")

                section_label("🏢", "Organization Type")
                st.write(row["OrgType"] or "Not specified")

                section_label("🌐", "Website")
                url = row["URL"]
                if url and url.startswith("http"):
                    st.markdown(f"[{url}]({url})")
                elif url:
                    st.markdown(f"[https://{url}](https://{url})")
                else:
                    st.markdown(
                        f"<span style='color:{TEXT_MID};'>Not listed</span>",
                        unsafe_allow_html=True,
                    )
                    
                section_label("📊", "Impact Report") ##New change for v5
                st.markdown(_link_cell(row.get("ImpactReport", "")), unsafe_allow_html=True)

                section_label("🧭", "Strategic Plan") ##New change for v5
                st.markdown(_link_cell(row.get("StrategicPlan", "")), unsafe_allow_html=True) 

            with c2:
                section_label("🛠️", "Services")
                detail_svcs = _smart_split(row.get("Services", ""))
                if detail_svcs:
                    st.markdown(
                        " ".join(
                            f'<span class="svc-chip">{s}</span>' for s in detail_svcs
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    st.write("Not specified")

            if pd.notna(row["Latitude"]) and pd.notna(row["Longitude"]):
                section_label("🗺️", "Location on Map")
                mini = folium.Map(
                    location=[row["Latitude"], row["Longitude"]],
                    zoom_start=15,
                    tiles="OpenStreetMap",
                )
                folium.Marker(
                    location=[row["Latitude"], row["Longitude"]],
                    tooltip=row["Name"],
                    icon=folium.Icon(color="blue", icon="info-sign"),
                ).add_to(mini)
                st_folium(
                    mini, use_container_width=True, height=300, returned_objects=[]
                )
            else:
                st.info("No map coordinates available for this organization.")
