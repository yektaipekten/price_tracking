# app.py
import os
import time
from io import BytesIO
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Price Tracking", layout="wide")

# ---- CONFIG ----
API_KEY = st.secrets.get("RAINFOREST_API_KEY") or os.environ.get("RAINFOREST_API_KEY")

MARKETPLACE = "amazon.co.uk"

# Optional simple password protection (APP_PASSWORD in Streamlit secrets)
APP_PASSWORD = st.secrets.get("APP_PASSWORD")
if APP_PASSWORD:
    pw = st.text_input("Password", type="password")
    if pw != APP_PASSWORD:
        st.stop()

st.title("Price Tracking")
st.write("Click **Fetch Prices** to pull current price from Amazon (Rainforest API).")

# ASIN input
asins_text = st.text_area(
    "ASINs (one per line)",
    height=150,
    value="B0D7J69H1L"
)
ASINS = [a.strip().upper() for a in asins_text.splitlines() if a.strip()]

# ---- Mapping (keep reading ASINs.csv, but we only USE Size from it) ----
mapping_dict = {}
mapping_path = "ASINs.csv"
if os.path.exists(mapping_path):
    try:
        map_df = pd.read_csv(mapping_path, dtype=str)
        map_df.columns = [c.strip() for c in map_df.columns]
        if "ASIN" not in map_df.columns:
            map_df = pd.read_csv(mapping_path, header=None, dtype=str)
            if map_df.shape[1] == 1:
                map_df.columns = ["ASIN"]
                map_df["Design"] = None
                map_df["Size"] = None
            elif map_df.shape[1] == 2:
                map_df.columns = ["ASIN", "Design"]
                map_df["Size"] = None
            else:
                map_df.columns = ["ASIN", "Design", "Size"] + list(map_df.columns[3:])

        if "Size" not in map_df.columns:
            map_df["Size"] = None
        if "Design" not in map_df.columns:
            map_df["Design"] = None

        map_df["ASIN"] = map_df["ASIN"].astype(str).str.strip().str.upper()
        mapping_dict = map_df.set_index("ASIN")[["Design", "Size"]].T.to_dict()
        st.info(f"Loaded mapping from {mapping_path} ({len(mapping_dict)} rows).")
    except Exception as e:
        st.warning(f"Could not read mapping file {mapping_path}: {e}")

# ---- Helpers ----
def extract_price(product: dict):
    """
    Rainforest product payloads vary.
    We try buybox.price first, then product.price as fallback.
    Returns price value only (you asked not to display currency/etc).
    """
    if not isinstance(product, dict):
        return None

    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        p = buybox.get("price")
        if isinstance(p, dict) and p.get("value") is not None:
            return p.get("value")

    p2 = product.get("price")
    if isinstance(p2, dict) and p2.get("value") is not None:
        return p2.get("value")

    return None


def extract_brand(product: dict):
    """
    Rainforest often returns brand in product['brand'] or in specifications.
    """
    if not isinstance(product, dict):
        return None

    b = product.get("brand")
    if isinstance(b, str) and b.strip():
        return b.strip()

    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for s in specs:
            if not isinstance(s, dict):
                continue
            name = (s.get("name") or "").strip().lower()
            val = s.get("value")
            if name == "brand" and val is not None and str(val).strip():
                return str(val).strip()

    return None


# ---- Main ----
if st.button("Fetch Prices"):
    if not API_KEY:
        st.error("API key not found. Please configure RAINFOREST_API_KEY in Streamlit Secrets.")
    elif not ASINS:
        st.warning("Please enter at least one ASIN.")
    else:
        progress = st.progress(0)
        results = []
        total = len(ASINS)

        credits_used = 0
        credits_remaining = None

        for i, asin in enumerate(ASINS, start=1):
            with st.spinner(f"Fetching {asin} ({i}/{total})"):
                url = "https://api.rainforestapi.com/request"
                params = {
                    "api_key": API_KEY,
                    "type": "product",
                    "amazon_domain": MARKETETPLACE if False else MARKETPLACE,  # keep literal MARKETPLACE, avoids typo edits
                    "asin": asin,
                }

                # Only columns you asked, in the order you asked:
                row = {
                    "Brand": None,
                    "ASIN": asin,
                    "Size": None,
                    "Price": None,
                    "Date (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                }

                try:
                    r = requests.get(url, params=params, timeout=30)
                    data = r.json() if r is not None else {}

                    info = data.get("request_info", {}) or {}
                    credits_used = info.get("credits_used", credits_used)
                    credits_remaining = info.get("credits_remaining", credits_remaining)

                    product = data.get("product")
                    if not isinstance(product, dict) or not product:
                        # keep row as-is, but it's useful to know why in logs; user asked to keep page clean.
                        product = {}

                    # Size from mapping (only Size)
                    map_entry = mapping_dict.get(asin)
                    if map_entry:
                        m_size = map_entry.get("Size")
                        if pd.notna(m_size) and str(m_size).strip() != "":
                            row["Size"] = m_size

                    # Brand + Price from API
                    row["Brand"] = extract_brand(product)
                    row["Price"] = extract_price(product)

                except Exception:
                    # User requested not to show extra columns/errors in table
                    pass

                results.append(row)

            progress.progress(i / total)
            time.sleep(0.5)

        df = pd.DataFrame(results)

        # Force exact column order
        df = df[["Brand", "ASIN", "Size", "Price", "Date (UTC)"]]

        st.subheader("Results")
        st.dataframe(df, use_container_width=True)

        # Excel export
        out = BytesIO()
        try:
            with pd.ExcelWriter(out, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Prices")
            out.seek(0)
            st.download_button(
                "⬇️ Download Price Report",
                data=out.getvalue(),
                file_name="price_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception:
            out2 = BytesIO()
            df.to_excel(out2, index=False)
            out2.seek(0)
            st.download_button(
                "⬇️ Download Price Report",
                data=out2.getvalue(),
                file_name="price_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        # Credits display (keep as you wanted)
        if credits_remaining is not None:
            st.info(f"Credits used in last response: {credits_used}; Credits remaining (approx): {credits_remaining}")
