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

API_KEY = st.secrets.get("RAINFOREST_API_KEY") or os.environ.get("RAINFOREST_API_KEY")
MARKETPLACE = "amazon.co.uk"

st.title("Price Tracking")
st.write("Click **Fetch Prices** to pull current product data from Amazon (Rainforest API).")

asins_text = st.text_area("ASINs (one per line)", height=150, value="B0D7J69H1L")
ASINS = [a.strip().upper() for a in asins_text.splitlines() if a.strip()]


# ----------------- Helpers -----------------
def to_float(x):
    """Parse first number from int/float/str/dict."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        m = re.search(r"(\d+[.,]\d+|\d+)", x)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", "."))
        except Exception:
            return None
    if isinstance(x, dict):
        for k in ["value", "amount", "raw", "price", "final_price"]:
            if k in x:
                f = to_float(x.get(k))
                if f is not None:
                    return f
    return None


def parse_gbp_from_text(s: str):
    """Only parses GBP values like '£59.99' from text. Returns float or None."""
    if not isinstance(s, str):
        return None
    m = re.search(r"£\s*(\d+(?:[.,]\d+)?)", s)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def extract_brand(product: dict):
    b = product.get("brand")
    if isinstance(b, str) and b.strip():
        return b.strip()

    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for sp in specs:
            if isinstance(sp, dict) and (sp.get("name") or "").strip().lower() == "brand":
                v = sp.get("value")
                if v:
                    return str(v).strip()
    return None


def extract_size(product: dict):
    """Size from Measurements->Size or title. Returns like 122x170cm."""
    def parse_size(text: str):
        if not isinstance(text, str):
            return None
        s = text.lower().replace("×", "x").replace("*", "x")
        s = re.sub(r"\(.*?\)", "", s)
        s = re.sub(r"\s+", " ", s).strip()

        m = re.search(r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*(cm|mm|m|metre|metres)\b", s)
        if not m:
            s2 = s.replace(" ", "")
            m = re.search(r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)\s*(cm|mm|m)\b", s2)
        if not m:
            return None

        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
        unit = m.group(3)

        if unit in ["m", "metre", "metres"]:
            a *= 100
            b *= 100
        elif unit == "mm":
            a /= 10
            b /= 10

        def fmt(x):
            if abs(x - int(x)) < 1e-9:
                return str(int(x))
            return str(x).rstrip("0").rstrip(".")

        return f"{fmt(a)}x{fmt(b)}cm"

    specs = product.get("specifications", [])
    if isinstance(specs, list):
        # Prefer Measurements -> Size
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            group = (sp.get("group_name") or sp.get("group") or sp.get("section") or sp.get("category") or "")
            if str(group).strip().lower() == "measurements" and str(sp.get("name") or "").strip().lower() == "size":
                val = sp.get("value")
                out = parse_size(str(val))
                if out:
                    return out

        # Fallback: direct "Size"
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            if str(sp.get("name") or "").strip().lower() == "size":
                val = sp.get("value")
                out = parse_size(str(val))
                if out:
                    return out

    title = product.get("title")
    out = parse_size(title) if isinstance(title, str) else None
    return out


def extract_base_price(product: dict):
    """Normal displayed price (not voucher)."""
    # 1) buybox.price
    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        p = buybox.get("price")
        f = to_float(p)
        if f and f > 0:
            return f

    # 2) buybox_winner.price
    bbw = product.get("buybox_winner")
    if isinstance(bbw, dict):
        p = bbw.get("price")
        f = to_float(p)
        if f and f > 0:
            return f

    # 3) product.price
    f = to_float(product.get("price"))
    if f and f > 0:
        return f

    # 4) offers[0].price
    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            f = to_float(first.get("price"))
            if f and f > 0:
                return f

    return None


def extract_voucher_price(product: dict):
    """
    Finds 'Voucher price £xx.xx' anywhere in payload.
    ONLY accepts strings containing BOTH 'voucher price' and '£'.
    This prevents '20%' being parsed as 20.
    """
    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                out = walk(v)
                if out is not None:
                    return out
        elif isinstance(obj, list):
            for it in obj:
                out = walk(it)
                if out is not None:
                    return out
        elif isinstance(obj, str):
            t = obj.lower()
            if "voucher price" in t and "£" in obj:
                return parse_gbp_from_text(obj)
        return None

    return walk(product)


# ----------------- Main -----------------
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
                "Final price": None,
                "Date (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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
                    row["Size"] = extract_size(product)

                    base_price = extract_base_price(product)
                    voucher_price = extract_voucher_price(product)

                    row["Final price"] = voucher_price if voucher_price is not None else base_price

            except Exception:
                pass

            results.append(row)

        progress.progress(i / total)
        time.sleep(0.25)

    df = pd.DataFrame(results, columns=[
        "Brand", "ASIN", "Size", "Final price", "Date (UTC)"
    ])

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
