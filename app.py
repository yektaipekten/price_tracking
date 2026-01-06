# app.py
import os
import streamlit as st
import pandas as pd
import requests
import time
from io import BytesIO
from datetime import datetime, timezone

st.set_page_config(page_title="Price Tracking", layout="wide")

# ---- CONFIG ----
API_KEY = st.secrets.get("RAINFOREST_API_KEY") or os.environ.get("RAINFOREST_API_KEY")
MARKETPLACE = "amazon.co.uk"  # UK

# Optional simple password protection (same as your reference app)
APP_PASSWORD = st.secrets.get("APP_PASSWORD")  # optional
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
ASINS = [a.strip() for a in asins_text.splitlines() if a.strip()]

# ---- Mapping (COPY from your app) ----
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
        if "Design" not in map_df.columns:
            map_df["Design"] = None
        if "Size" not in map_df.columns:
            map_df["Size"] = None

        map_df["ASIN"] = map_df["ASIN"].astype(str).str.strip().str.upper()
        mapping_dict = map_df.set_index("ASIN")[["Design", "Size"]].T.to_dict()
        st.info(f"Loaded mapping from {mapping_path} ({len(mapping_dict)} rows).")
    except Exception as e:
        st.warning(f"Could not read mapping file {mapping_path}: {e}")

# ---- Helpers ----
def extract_price_fields(product: dict):
    """
    Rainforest product payloads vary. We try buybox.price first, then product.price as fallback.
    """
    price = None
    currency = None
    availability = None
    buybox_seller = None

    if not isinstance(product, dict):
        return price, currency, availability, buybox_seller

    availability = product.get("availability")

    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        p = buybox.get("price")
        if isinstance(p, dict):
            price = p.get("value")
            currency = p.get("currency")
        seller = buybox.get("seller")
        if isinstance(seller, dict):
            buybox_seller = seller.get("name")

    p2 = product.get("price")
    if isinstance(p2, dict):
        if price is None:
            price = p2.get("value")
        if currency is None:
            currency = p2.get("currency")

    return price, currency, availability, buybox_seller

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
            val = (s.get("value") or "")
            if name == "brand" and str(val).strip():
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

        # credits (COPY logic from your app)
        credits_used = 0
        credits_remaining = None

        for i, asin in enumerate(ASINS, start=1):
            asin_norm = asin.strip().upper()

            with st.spinner(f"Fetching {asin_norm} ({i}/{total})"):
                url = "https://api.rainforestapi.com/request"
                params = {
                    "api_key": API_KEY,
                    "type": "product",
                    "amazon_domain": MARKETPLACE,
                    "asin": asin_norm
                }

                row = {
                    "ASIN": asin_norm,
                    "Brand": None,
                    "Design": None,
                    "Size": None,
                    "Price": None,
                    "Currency": None,
                    "Availability": None,
                    "Buybox Seller": None,
                    "Fetched At (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "Error": None,
                }

                try:
                    r = requests.get(url, params=params, timeout=30)
                    data = r.json()

                    # credits (COPY)
                    info = data.get("request_info", {})
                    credits_used = info.get("credits_used", credits_used)
                    credits_remaining = info.get("credits_remaining", credits_remaining)

                    product = data.get("product", {})

                    # mapping override (COPY)
                    map_entry = mapping_dict.get(asin_norm)
                    if map_entry:
                        m_design = map_entry.get("Design")
                        m_size = map_entry.get("Size")
                        if pd.notna(m_design) and str(m_design).strip() != "":
                            row["Design"] = m_design
                        if pd.notna(m_size) and str(m_size).strip() != "":
                            row["Size"] = m_size

                    # brand (new)
                    row["Brand"] = extract_brand(product)

                    # price fields (new)
                    price, currency, availability, seller = extract_price_fields(product)
                    row["Price"] = price
                    row["Currency"] = currency
                    row["Availability"] = availability
                    row["Buybox Seller"] = seller

                except Exception as e:
                    row["Error"] = str(e)

                results.append(row)

            progress.progress(i / total)
            time.sleep(0.5)

        df = pd.DataFrame(results)

        st.subheader("Results")
        st.dataframe(df, use_container_width=True)

        # Excel export (COPY pattern)
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

        # credits display (COPY)
        if credits_remaining is not None:
            st.info(f"Credits used in last response: {credits_used}; Credits remaining (approx): {credits_remaining}")
