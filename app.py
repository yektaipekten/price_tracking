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


# ----------------- Helpers -----------------
def _to_float(x):
    """Best-effort parse numeric price from int/float/str/dict."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip()
        m = re.search(r"(\d+[.,]\d+|\d+)", s)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", "."))
        except Exception:
            return None
    if isinstance(x, dict):
        # IMPORTANT: Do NOT include voucher/coupon keys here.
        for k in ["value", "amount", "raw", "price", "final_price"]:
            if k in x:
                f = _to_float(x.get(k))
                if f is not None:
                    return f
    return None


def _fmt_num(x: float):
    if x is None:
        return None
    if abs(x - int(x)) < 1e-9:
        return str(int(x))
    return str(x).rstrip("0").rstrip(".")


def _normalize_to_cm(num: float, unit: str) -> float:
    unit = (unit or "").lower()
    if unit in ["m", "metre", "metres"]:
        return num * 100.0
    if unit == "mm":
        return num / 10.0
    return num


def _clean_x(s: str) -> str:
    return (s or "").replace("×", "x").replace("*", "x")


def _is_packaging_label(name: str) -> bool:
    n = (name or "").strip().lower()
    return any(k in n for k in ["package", "packaging", "parcel", "box"])


def _parse_size_text_to_cm_pair(text: str):
    """
    Parses size from free text:
      - "122 x 170 cm (Rectangular)"
      - "160*230CM"
      - "2.3 x 1.6 m"
    Returns (a_cm, b_cm) or None.
    """
    if not text or not isinstance(text, str):
        return None

    s = _clean_x(text).lower()
    s = s.split(";")[0]
    s = re.sub(r"\(.*?\)", "", s)  # remove parentheses
    s = re.sub(r"\s+", " ", s).strip()

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*(cm|mm|m|metre|metres)\b", s)
    if m:
        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
        unit = m.group(3)
        return (_normalize_to_cm(a, unit), _normalize_to_cm(b, unit))

    s2 = s.replace(" ", "")
    m2 = re.search(r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)\s*(cm|mm|m)\b", s2)
    if m2:
        a = float(m2.group(1).replace(",", "."))
        b = float(m2.group(2).replace(",", "."))
        unit = m2.group(3)
        return (_normalize_to_cm(a, unit), _normalize_to_cm(b, unit))

    return None


def _format_size(a_cm: float, b_cm: float) -> str:
    return f"{_fmt_num(a_cm)}x{_fmt_num(b_cm)}cm"


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


def extract_size(product: dict):
    """
    Size from:
      - Product details -> Measurements -> Size
      - OR from title
    Returns "122x170cm" or None
    """
    if not isinstance(product, dict):
        return None

    specs = product.get("specifications", [])
    if isinstance(specs, list):
        # A) grouped as Measurements
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            group = (sp.get("group_name") or sp.get("group") or sp.get("section") or sp.get("category") or "")
            group_l = str(group).strip().lower()
            name = (sp.get("name") or "").strip()
            val = sp.get("value")
            if val is None:
                continue
            if _is_packaging_label(name) or _is_packaging_label(group_l):
                continue
            if group_l == "measurements" and name.strip().lower() == "size":
                parsed = _parse_size_text_to_cm_pair(str(val))
                if parsed:
                    return _format_size(parsed[0], parsed[1])

        # B) ungrouped: name == Size
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            name = (sp.get("name") or "").strip()
            val = sp.get("value")
            if val is None:
                continue
            nlow = name.lower()
            if _is_packaging_label(nlow):
                continue
            if nlow == "size":
                parsed = _parse_size_text_to_cm_pair(str(val))
                if parsed:
                    return _format_size(parsed[0], parsed[1])

    title = product.get("title")
    if isinstance(title, str) and title.strip():
        parsed = _parse_size_text_to_cm_pair(title)
        if parsed:
            return _format_size(parsed[0], parsed[1])

    variants = product.get("variants")
    if isinstance(variants, dict):
        selected = variants.get("selected")
        if isinstance(selected, dict):
            v = selected.get("size_name") or selected.get("size")
            if v:
                parsed = _parse_size_text_to_cm_pair(str(v))
                if parsed:
                    return _format_size(parsed[0], parsed[1])

    return None


def _contains_voucher_coupon_key(key: str) -> bool:
    k = (key or "").lower()
    return ("voucher" in k) or ("coupon" in k)


def extract_base_price(product: dict):
    """
    Base price (displayed price) excluding voucher/coupon.
    """
    if not isinstance(product, dict):
        return None

    def price_from_price_obj(pobj):
        if pobj is None:
            return None
        if isinstance(pobj, (int, float, str)):
            return _to_float(pobj)
        if isinstance(pobj, dict):
            for k in ["value", "raw", "amount"]:
                if k in pobj and not _contains_voucher_coupon_key(k):
                    f = _to_float(pobj.get(k))
                    if f is not None and f > 0:
                        return f
        return None

    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        f = price_from_price_obj(buybox.get("price"))
        if f:
            return f

    bbw = product.get("buybox_winner")
    if isinstance(bbw, dict):
        f = price_from_price_obj(bbw.get("price"))
        if f:
            return f

    p2 = product.get("price")
    f = price_from_price_obj(p2)
    if f:
        return f

    for k in ["price_string", "current_price", "displayed_price", "main_price", "price_current"]:
        if k in product and not _contains_voucher_coupon_key(k):
            f = _to_float(product.get(k))
            if f is not None and f > 0:
                return f

    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            f = price_from_price_obj(first.get("price"))
            if f:
                return f

    return None


def extract_voucher_price(product: dict, base_price: float | None):
    """
    Voucher price (e.g. 'Voucher price £59.99') deep scan.
    Returns a float or None.
    """
    if not isinstance(product, dict):
        return None

    candidates = []

    def add(score, value):
        f = _to_float(value)
        if f is not None and f > 0:
            candidates.append((score, f))

    # try to parse voucher price from any string
    def parse_from_string(s: str):
        if not s:
            return
        t = s.lower()

        # strongest: explicit phrase
        if "voucher price" in t:
            add(250, s)
            return

        # other common patterns
        if "voucher" in t or "coupon" in t:
            # pick the first currency number after voucher/coupon texts
            m = re.search(r"(£\s*\d+[.,]?\d*|\d+[.,]\d+|\d+)", s)
            if m:
                add(120, m.group(1))

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k).lower()

                # 1) direct key matches that often hold the voucher-applied price
                if key in [
                    "voucher_price", "coupon_price",
                    "price_after_coupon", "price_after_voucher",
                    "checkout_price", "price_with_coupon", "price_with_voucher",
                    "discounted_price", "final_price_after_coupon"
                ]:
                    add(220, v)

                # 2) any key that contains voucher/coupon AND price
                if (("voucher" in key or "coupon" in key) and "price" in key):
                    add(210, v)

                # 3) strings
                if isinstance(v, str):
                    parse_from_string(v)

                walk(v)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

        elif isinstance(obj, str):
            parse_from_string(obj)

    walk(product)

    if not candidates:
        return None

    # prefer a voucher price that is <= base price (if base known)
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

    if base_price is not None and base_price > 0:
        valid = [c for c in candidates if c[1] <= base_price]
        if valid:
            valid.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return valid[0][1]

    return candidates[0][1]



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
                    voucher_price = extract_voucher_price(product, base_price)

                    # Final price rule: if voucher exists and is <= base price, final = voucher else base
                    if base_price is not None and voucher_price is not None and voucher_price > 0 and voucher_price <= base_price:
                        row["Final price"] = voucher_price
                    else:
                        row["Final price"] = base_price

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
