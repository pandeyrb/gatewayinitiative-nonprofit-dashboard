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

STATUS_HEX = {
    "Active": "#16a34a",
    "Potential/Prospective": "#ea580c",
    "Unknown": "#6b7280",
}
STATUS_FOLIUM = {
    "Active": "green",
    "Potential/Prospective": "orange",
    "Unknown": "gray",
}

# Category palette — aligned with CATEGORY_MAP below
CAT_COLORS = {
    "Education":                    "#e63946",  # vivid red
    "Youth Development":            "#f4a261",  # warm orange
    "Economic Mobility":            "#2a9d8f",  # teal
    "Family & Basic Needs":         "#e9c46a",  # golden yellow
    "Health & Wellness":            "#457b9d",  # steel blue
    "Justice, Legal & Immigration": "#6a0572",  # deep purple
    "Community & Civic Life":       "#2d6a4f",  # forest green
    "Other":                        "#94a3b8",  # slate
    "Unknown":                      "#cbd5e1",
}

# Mapping from raw ServiceArea tags → broad categories
CATEGORY_MAP = {
    "Education": [
        "Education",
        "Adult education",
        "Literacy",
        "College prep",
        "Higher Education",
        "After school/Out of school",
        "Early childhood development",
        "Applied Behavior Analysis (ABA) Services for Children with Autism",
    ],
    "Youth Development": [
        "Youth Development",
        "After school/Out of school",
        "Child Welfare/Protection Systems & Services",
        "Mentoring",
    ],
    "Economic Mobility": [
        "Economic Mobility/Workforce Development",
        "Economic Development (Community-level)",
        "Financial Literacy",
        "Capacity Building Services",
    ],
    "Family & Basic Needs": [
        "Family Services",
        "Anti-Poverty Programs",
        "Social Services",
        "Food Insecurity",
        "Food pantry",
        "Housing Insecurity/Homelessness",
        "Homelessness",
        "Intimate Partner/Domestic Violence",
        "Other: Basic Needs",
        "Other: Clothing/Personal Growth",
    ],
    "Health & Wellness": [
        "Health/Medical",
        "Mental Health",
        "Public Health",
        "Substance Use Disorders",
        "Disabillities",
        "Disabilities",
        "Aging",
        "Other: Adult Daycare",
    ],
    "Justice, Legal & Immigration": [
        "Legal Services",
        "Legal services",
        "Criminal Justice",
        "Immigration",
    ],
    "Community & Civic Life": [
        "Athletics",
        "Faith-based Services",
        "Arts and Culture",
        "Climate Change & Environmental Justice",
        "Other: Equine Assisted Programs",
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
                if kw.lower() in svc.lower() or svc.lower() in kw.lower():
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

  /* Tab label font size */
  [data-testid="stTabs"] button[role="tab"] {{
    font-size:16px !important; font-weight:600 !important; }}
</style>
""",
    unsafe_allow_html=True,
)


# ── Lawrence, MA boundary ─────────────────────────────────────────────────────
@st.cache_data(ttl=86400)
def fetch_lawrence_boundary() -> dict | None:
    """Fetch Lawrence, MA city boundary GeoJSON from Nominatim."""
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
CSV_PATH = "GWIorgs_v3.csv"


@st.cache_data(ttl=3600)
def load_data(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()

    df = pd.read_csv(path, dtype=str).fillna("")
    # Drop fully empty rows
    df = df[df["Name"].str.strip() != ""].reset_index(drop=True)

    df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")

    # Derive list columns from ServiceArea and Population
    df["SvcList"] = df["ServiceArea"].apply(_smart_split)
    df["PopList"] = df["Population"].apply(_smart_split)
    df["CatList"] = df["ServiceArea"].apply(_get_categories)

    # Status normalise
    df["Status"] = df["Status"].str.strip().replace("", "Unknown")
    return df


df = load_data(CSV_PATH)

if df.empty:
    st.error(
        f"**Data file not found:** `{CSV_PATH}`\n\nMake sure `{CSV_PATH}` is next to `app.py`."
    )
    st.stop()


# ── helpers ───────────────────────────────────────────────────────────────────
def cat_badge(cat: str) -> str:
    color = CAT_COLORS.get(cat, "#94a3b8")
    return f'<span class="cat-badge" style="background:{color};">{cat}</span>'


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

    search = st.text_input("Search", placeholder="Name, city, or service…")

    sel_status = st.selectbox(
        "Partner Status",
        ["All"] + sorted(df["Status"].unique().tolist()),
    )

    all_cats = sorted(
        {c for lst in df["CatList"] for c in lst if c not in ("Unknown",)}
    )
    sel_cat = st.selectbox("Category", ["All"] + all_cats)

    all_pops = sorted({p for lst in df["PopList"] for p in lst if p})
    sel_pop = st.selectbox("Population Served", ["All"] + all_pops)

    all_svcs = sorted({s for lst in df["SvcList"] for s in lst if s})
    sel_svc = st.selectbox("Specific Service", ["All"] + all_svcs)

    st.divider()
    if st.button("↺  Reset all filters", use_container_width=True):
        st.rerun()

    # legend: categories
    st.divider()
    st.markdown(
        f"<p style='font-size:12px;font-weight:700;color:{TEXT_DARK};"
        "margin:0 0 6px;'>MARKER CATEGORIES</p>",
        unsafe_allow_html=True,
    )
    for label, hex_c in CAT_COLORS.items():
        if label in ("Other", "Unknown"):
            continue
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:5px;"
            f"font-size:13px;color:{TEXT_DARK};'>"
            f"<span style='width:12px;height:12px;border-radius:50%;background:{hex_c};"
            f"flex-shrink:0;display:inline-block;'></span>{label}</div>",
            unsafe_allow_html=True,
        )



# ── apply filters ─────────────────────────────────────────────────────────────
filtered = df.copy()

if search:
    mask = (
        filtered["Name"].str.contains(search, case=False, na=False)
        | filtered["ServiceArea"].str.contains(search, case=False, na=False)
        | filtered["City"].str.contains(search, case=False, na=False)
    )
    filtered = filtered[mask]

if sel_status != "All":
    filtered = filtered[filtered["Status"] == sel_status]

if sel_cat != "All":
    filtered = filtered[filtered["CatList"].apply(lambda lst: sel_cat in lst)]

if sel_pop != "All":
    filtered = filtered[filtered["PopList"].apply(lambda lst: sel_pop in lst)]

if sel_svc != "All":
    filtered = filtered[filtered["SvcList"].apply(lambda lst: sel_svc in lst)]

n_filtered = len(filtered)
n_total = len(df)

# active filter pills
active_filters: list[str] = []
if search:
    active_filters.append(f'"{search}"')
if sel_status != "All":
    active_filters.append(sel_status)
if sel_cat != "All":
    active_filters.append(sel_cat)
if sel_pop != "All":
    active_filters.append(sel_pop)
if sel_svc != "All":
    active_filters.append(sel_svc)


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
    if active_filters:
        st.markdown(
            "".join(f'<span class="filter-pill">{f}</span>' for f in active_filters),
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
            tiles="CartoDB Voyager",
        )

        # City boundary
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
            status = row["Status"]
            color_hex = STATUS_HEX.get(status, "#6b7280")
            cats = row["CatList"]
            pin_color = CAT_COLORS.get(cats[0] if cats else "Unknown", "#94a3b8")
            svc_tags = row["ServiceArea"] or "Not specified"
            pop = row["Population"] or "Not specified"
            org_type = row["OrgType"] or "Not specified"
            url = row["URL"]

            url_html = (
                f'<a href="{url}" target="_blank" '
                f'style="display:inline-block;margin-top:10px;padding:6px 14px;'
                f"background:{BRAND_DARK};color:white;border-radius:6px;"
                f'font-size:12px;font-weight:600;text-decoration:none;">🔗 Visit Website</a>'
                if url
                else f'<span style="color:#94a3b8;font-size:12px;">No website listed</span>'
            )

            cat_badges = " ".join(
                f'<span style="background:{CAT_COLORS.get(c, "#94a3b8")};color:white;'
                f'border-radius:12px;padding:2px 9px;font-size:10px;font-weight:700;">{c}</span>'
                for c in cats
            )

            popup_html = (
                # Colored header bar
                f'<div style="font-family:Inter,sans-serif;width:310px;'
                f'border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.12);">'
                f'<div style="background:{pin_color};padding:14px 16px;">'
                f'<div style="font-size:15px;font-weight:700;color:white;'
                f'line-height:1.3;">{row["Name"]}</div>'
                f'<div style="margin-top:6px;">'
                f'<span style="background:rgba(0,0,0,.25);color:white;border-radius:20px;'
                f'padding:2px 10px;font-size:11px;font-weight:600;">{status}</span>'
                f"</div></div>"
                # Body
                f'<div style="padding:12px 16px;background:white;">'
                f'<div style="margin-bottom:8px;">{cat_badges}</div>'
                f'<table style="width:100%;border-collapse:collapse;font-size:12px;'
                f'color:{TEXT_DARK};">'
                f'<tr><td style="color:#94a3b8;padding:3px 10px 3px 0;font-size:10px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">Address</td>'
                f"<td>{row['Address']}, {row['City']}, {row['State']}</td></tr>"
                f'<tr><td style="color:#94a3b8;padding:3px 10px 3px 0;font-size:10px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">Type</td>'
                f"<td>{org_type}</td></tr>"
                f'<tr><td style="color:#94a3b8;padding:3px 10px 3px 0;font-size:10px;'
                f'font-weight:700;text-transform:uppercase;white-space:nowrap;">Population</td>'
                f"<td>{pop}</td></tr>"
                f'<tr><td style="color:#94a3b8;padding:3px 10px 3px 0;font-size:10px;'
                f"font-weight:700;text-transform:uppercase;white-space:nowrap;"
                f'vertical-align:top;">Services</td>'
                f'<td style="color:{TEXT_MID};">{svc_tags}</td></tr>'
                f"</table>"
                f"{url_html}"
                f"</div></div>"
            )

            tooltip_html = (
                f'<div style="font-family:Inter,sans-serif;font-size:13px;'
                f'font-weight:700;color:{BRAND_DARK};max-width:200px;">{row["Name"]}</div>'
                f'<div style="font-size:11px;color:{TEXT_MID};">{cats[0] if cats else ""}</div>'
            )

            pin_svg = (
                f'<div style="width:25px;height:41px;">'
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 52" width="25" height="41">'
                f'<path d="M16 0C7.163 0 0 7.163 0 16c0 10 16 36 16 36S32 26 32 16C32 7.163 24.837 0 16 0z"'
                f' fill="{pin_color}" stroke="#fff" stroke-width="2"/>'
                f'<circle cx="16" cy="16" r="7" fill="white" opacity="0.85"/>'
                f'</svg></div>'
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
            f"{len(map_data)} organizations plotted · Markers colored by category · Click for details"
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
                    "Status",
                    "OrgType",
                    "Population",
                    "ServiceArea",
                ]
            ].rename(columns={"OrgType": "Org Type"})
            st.download_button(
                f"⬇️  Download {n_filtered} results as CSV",
                data=export_df.to_csv(index=False).encode("utf-8"),
                file_name="gwi_nonprofits_filtered.csv",
                mime="text/csv",
            )

        dir_df = (
            filtered[
                [
                    "Name",
                    "City",
                    "Status",
                    "OrgType",
                    "Population",
                    "ServiceArea",
                    "URL",
                ]
            ]
            .rename(
                columns={
                    "OrgType": "Org Type",
                    "Population": "Population Served",
                    "ServiceArea": "Services",
                }
            )
            .copy()
        )
        st.dataframe(
            dir_df,
            use_container_width=True,
            height=520,
            column_config={
                "URL": st.column_config.LinkColumn("Website", display_text="🔗 Open"),
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
            status = row["Status"]
            badge_color = STATUS_HEX.get(status, "#6b7280")
            cats = row["CatList"]

            # profile header

            st.markdown(
                f'<div style="background:{BG_WHITE};border-radius:12px;'
                f"padding:22px 26px;box-shadow:0 1px 8px rgba(0,0,0,.08);"
                f'border-left:5px solid {badge_color};margin-bottom:20px;">'
                f'<h2 style="color:{BRAND_DARK};margin:0 0 8px;font-size:22px;">'
                f"{row['Name']}</h2>"
                f'<span style="background:{badge_color};color:white;border-radius:20px;'
                f'padding:3px 14px;font-size:12px;font-weight:700;margin-right:8px;">'
                f"{status}</span>"
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

            with c2:
                section_label("👥", "Population Served")
                pops = row["PopList"]
                st.write(" · ".join(pops) if pops else "Not specified")

                section_label("🗂️", "Categories")
                st.markdown(
                    " ".join(cat_badge(c) for c in cats) or "Not specified",
                    unsafe_allow_html=True,
                )

                section_label("🛠️", "Services & Focus Areas")
                svcs = row["SvcList"]
                if svcs:
                    st.markdown(
                        " ".join(f'<span class="svc-chip">{s}</span>' for s in svcs),
                        unsafe_allow_html=True,
                    )
                else:
                    st.write("Not specified")

            # mini map
            if pd.notna(row["Latitude"]) and pd.notna(row["Longitude"]):
                section_label("🗺️", "Location on Map")
                mini = folium.Map(
                    location=[row["Latitude"], row["Longitude"]],
                    zoom_start=15,
                    tiles="CartoDB positron",
                )
                folium.Marker(
                    location=[row["Latitude"], row["Longitude"]],
                    tooltip=row["Name"],
                    icon=folium.Icon(
                        color=STATUS_FOLIUM.get(status, "gray"), icon="info-sign"
                    ),
                ).add_to(mini)
                st_folium(
                    mini, use_container_width=True, height=300, returned_objects=[]
                )
            else:
                st.info("No map coordinates available for this organization.")
