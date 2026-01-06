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


def extract_size_thickness_weight(product: dict):
    """
    Returns:
      size: "122x170cm"
      thickness: "0.5 cm"
      weight: "2.07 kg"
    Logic:
      - size: first 2 dimensions (cm), ignores thickness
      - thickness: a third dimension in cm (if present)
      - weight: first 'kg' occurrence
    """
    if not isinstance(product, dict):
        return None, None, None

    specs = product.get("specifications", [])
    raw_candidates = []

    if isinstance(specs, list):
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            name = (sp.get("name") or "").strip().lower()
            val = sp.get("value")
            if val is None:
                continue
            # En olası alanlar
            if any(k in name for k in ["size", "dimensions", "dimension", "rug size", "item dimensions"]):
                raw_candidates.append(str(val))

    # variants / attributes fallback
    variants = product.get("variants")
    if isinstance(variants, dict):
        selected = variants.get("selected")
        if isinstance(selected, dict):
            for key in ["size", "dimensions", "value", "name"]:
                if selected.get(key):
                    raw_candidates.append(str(selected.get(key)))

    attrs = product.get("attributes")
    if isinstance(attrs, dict):
        for key in ["size", "dimensions"]:
            if attrs.get(key):
                raw_candidates.append(str(attrs.get(key)))

    if not raw_candidates:
        return None, None, None

    raw = " | ".join([c for c in raw_candidates if c and c.strip()]).strip()
    s = raw.lower().replace("×", "x")

    # weight: "2.07 kg"
    weight = None
    m_weight = re.search(r"(\d+(?:[.,]\d+)?)\s*kg", s)
    if m_weight:
        weight = m_weight.group(1).replace(",", ".") + " kg"

    # normalize for dimension parsing
    dim = s.replace(" ", "")
    # remove everything after ';' (often weight etc.)
    dim = dim.split(";")[0]

    # match 2 or 3 dims with optional unit (cm/mm/m)
    # examples:
    # 170x122x0.5cm
    # 170x122x0.5 cm
    # 170x122 cm
    m = re.search(
        r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)(?:x(\d{1,4}(?:[.,]\d+)?))?(cm|mm|m)?",
        dim,
    )
    if not m:
        # fallback: return raw as size
        return raw, None, weight

    a = m.group(1).replace(",", ".")
    b = m.group(2).replace(",", ".")
    c = m.group(3).replace(",", ".") if m.group(3) else None
    unit = m.group(4) if m.group(4) else None

    if not unit:
        unit = "cm"

    def to_float(x):
        try:
            return float(x)
        except:
            return None

    af = to_float(a)
    bf = to_float(b)
    cf = to_float(c) if c else None

    # unit -> cm
    if unit == "m":
        if af is not None:
            af *= 100
        if bf is not None:
            bf *= 100
        if cf is not None:
            cf *= 100
        unit = "cm"
    elif unit == "mm":
        if af is not None:
            af /= 10
        if bf is not None:
            bf /= 10
        if cf is not None:
            cf /= 10
        unit = "cm"

    def fmt(x):
        if x is None:
            return None
        if abs(x - int(x)) < 1e-9:
            return str(int(x))
        return str(x).rstrip("0").rstrip(".")

    # size: enforce small x big
    if af is not None and bf is not None:
        x1 = min(af, bf)
        x2 = max(af, bf)
        size = f"{fmt(x1)}x{fmt(x2)}cm"
    else:
        size = f"{m.group(1)}x{m.group(2)}{unit}"

    thickness = None
    if cf is not None:
        thickness = f"{fmt(cf)} cm"

    return size, thickness, weight


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
                "Thickness": None,
                "Weight": None,
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
                if isinstance(product, dict) and product:
                    row["Brand"] = extract_brand(product)
                    size, thickness, weight = extract_size_thickness_weight(product)
                    row["Size"] = size
                    row["Thickness"] = thickness
                    row["Weight"] = weight
                    row["Price"] = extract_price(product)

            except Exception:
                pass

            results.append(row)

        progress.progress(i / total)
        time.sleep(0.25)

    df = pd.DataFrame(results)

    # Column order exactly as requested
    df = df[["Brand", "ASIN", "Size", "Thickness", "Weight", "Price", "Date (UTC)"]]

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
