# app.py
import os
import re
import time
from io import BytesIO
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Price Tracking", layout="wide")

# ---- CONFIG ----
API_KEY = st.secrets.get("RAINFOREST_API_KEY") or os.environ.get("RAINFOREST_API_KEY")
MARKETPLACE = "amazon.co.uk"  # UK domain

st.title("Price Tracking")
st.write("Click **Fetch Prices** to pull current product data from Amazon (Rainforest API).")

asins_text = st.text_area("ASINs (one per line)", height=150, value="B0D7J69H1L")
ASINS = [a.strip().upper() for a in asins_text.splitlines() if a.strip()]

# ---- Helpers ----
def _parse_number_from_string(s: str):
    if not s:
        return None
    s = s.strip()
    m = re.search(r"(\d+[.,]\d+|\d+)", s)
    if not m:
        return None
    num = m.group(1).replace(",", ".")
    try:
        return float(num)
    except:
        return None

def extract_brand(product: dict):
    if not isinstance(product, dict):
        return None
    b = product.get("brand")
    if isinstance(b, str) and b.strip():
        return b.strip()

    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            name = (sp.get("name") or "").strip().lower()
            val = sp.get("value")
            if name == "brand" and val:
                v = str(val).strip()
                if v:
                    return v
    return None

def extract_price(product: dict):
    """
    Tries multiple common Rainforest paths for UK product pages.
    Returns float if possible, otherwise None.
    """
    if not isinstance(product, dict):
        return None

    candidates = []

    # buybox.price.value / buybox.price (dict/string)
    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        candidates.append(buybox.get("price"))
        p = buybox.get("price")
        if isinstance(p, dict):
            candidates.append(p.get("value"))
            candidates.append(p.get("raw"))

    # buybox_winner.price.*
    bbw = product.get("buybox_winner")
    if isinstance(bbw, dict):
        candidates.append(bbw.get("price"))
        p = bbw.get("price")
        if isinstance(p, dict):
            candidates.append(p.get("value"))
            candidates.append(p.get("raw"))

    # product.price.*
    p2 = product.get("price")
    if isinstance(p2, dict):
        candidates.append(p2.get("value"))
        candidates.append(p2.get("raw"))
    candidates.append(p2)

    # string-y fallbacks
    for k in ["price_string", "current_price", "displayed_price", "main_price"]:
        if k in product:
            candidates.append(product.get(k))

    # offers[0].price.*
    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            op = first.get("price")
            if isinstance(op, dict):
                candidates.append(op.get("value"))
                candidates.append(op.get("raw"))
            candidates.append(op)

    for c in candidates:
        if c is None:
            continue

        if isinstance(c, (int, float)):
            return float(c)

        if isinstance(c, dict):
            v = c.get("value")
            if isinstance(v, (int, float)):
                return float(v)
            raw = c.get("raw")
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                num = _parse_number_from_string(raw)
                if num is not None:
                    return num
            continue

        if isinstance(c, str):
            num = _parse_number_from_string(c)
            if num is not None:
                return num

    return None

def extract_size(product: dict):
    """
    Size might appear in:
    - product.variants (selected variant)
    - product.specifications (name contains size/dimensions)
    - product.title (last resort)
    Returns string or None.
    """
    if not isinstance(product, dict):
        return None

    # 1) specifications
    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            name = (sp.get("name") or "").strip().lower()
            val = sp.get("value")
            if val is None:
                continue
            if any(k in name for k in ["size", "dimensions", "dimension", "rug size", "item dimensions"]):
                v = str(val).strip()
                if v:
                    return v

    # 2) variants (some payloads include selected variant)
    variants = product.get("variants")
    if isinstance(variants, dict):
        # common pattern: variants["selected"] contains size-like fields
        selected = variants.get("selected")
        if isinstance(selected, dict):
            for key in ["size", "dimensions", "value", "name"]:
                if key in selected and selected.get(key):
                    v = str(selected.get(key)).strip()
                    if v:
                        return v

    # 3) occasionally attributes
    attrs = product.get("attributes")
    if isinstance(attrs, dict):
        for key in ["size", "dimensions"]:
            if key in attrs and attrs.get(key):
                v = str(attrs.get(key)).strip()
                if v:
                    return v

    return None

# ---- Main ----
if st.button("Fetch Prices"):
    if not API_KEY:
        st.error("API key not found. Please configure RAINFOREST_API_KEY in Streamlit Secrets.")
        st.stop()
    if not ASINS:
        st.warning("Please enter at least one ASIN.")
        st.stop()

    progress = st.progress(0)
    results = []
    total = len(ASINS)

    credits_used = None
    credits_remaining = None

    for i, asin in enumerate(ASINS, start=1):
        with st.spinner(f"Fetching {asin} ({i}/{total})"):
            row = {
                "Brand": None,
                "ASIN": asin,
                "Size": None,
                "Price": None,
                "Date (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }

            try:
                r = requests.get(
                    "https://api.rainforestapi.com/request",
                    params={
                        "api_key": API_KEY,
                        "type": "product",
                        "amazon_domain": MARKETPLACE,
                        "asin": asin,
                    },
                    timeout=30,
                )
                data = r.json()

                info = data.get("request_info") or {}
                credits_used = info.get("credits_used", credits_used)
                credits_remaining = info.get("credits_remaining", credits_remaining)

                product = data.get("product")
                if not isinstance(product, dict) or not product:
                    # başarısızsa boş bırak
                    pass
                else:
                    row["Brand"] = extract_brand(product)
                    row["Size"] = extract_size(product)
                    row["Price"] = extract_price(product)

            except Exception:
                # hata olursa bile uygulama çökmeyecek; satır boş kalır
                pass

            results.append(row)

        progress.progress(i / total)
        time.sleep(0.25)

    df = pd.DataFrame(results)

    st.subheader("Results")
    st.dataframe(df, use_container_width=True)

    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Prices")
    out.seek(0)

    st.download_button(
        "⬇️ Download Price Report",
        data=out.getvalue(),
        file_name="price_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if credits_remaining is not None:
        st.info(f"Credits used in last response: {credits_used}; Credits remaining (approx): {credits_remaining}")
