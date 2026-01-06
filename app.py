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

# Optional: debug (default off)
DEBUG = st.checkbox("Debug voucher extraction (show candidates)", value=False)


# ----------------- Helpers -----------------
def _to_float(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip()
        m = re.search(r"(\d+[.,]\d+|\d+)", s)
        if not m:
            return None
        return float(m.group(1).replace(",", "."))
    if isinstance(x, dict):
        for k in ["value", "amount", "raw", "price", "final_price"]:
            if k in x:
                f = _to_float(x.get(k))
                if f is not None:
                    return f
    return None


def _pct(a, b):
    """percent reduction from a -> b, e.g. a=74.99 b=59.99 => 20"""
    if a is None or b is None or a <= 0:
        return 0
    p = (a - b) / a * 100
    if p < 0:
        return 0
    return int(round(p))


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


def extract_list_price(product: dict):
    if not isinstance(product, dict):
        return None

    candidates = []

    for k in ["list_price", "rrp", "was_price", "price_was", "price_before_discount"]:
        if k in product:
            candidates.append(product.get(k))

    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        for k in ["list_price", "rrp", "was_price"]:
            if k in buybox:
                candidates.append(buybox.get(k))

    prices = product.get("prices")
    if isinstance(prices, dict):
        for k in ["list_price", "rrp", "was_price", "price_was"]:
            if k in prices:
                candidates.append(prices.get(k))

    for c in candidates:
        f = _to_float(c)
        if f is not None and f > 0:
            return f

    return None


def extract_price(product: dict):
    if not isinstance(product, dict):
        return None

    candidates = []

    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        candidates.append(buybox.get("price"))
        p = buybox.get("price")
        if isinstance(p, dict):
            candidates.append(p.get("value"))
            candidates.append(p.get("raw"))

    bbw = product.get("buybox_winner")
    if isinstance(bbw, dict):
        candidates.append(bbw.get("price"))
        p = bbw.get("price")
        if isinstance(p, dict):
            candidates.append(p.get("value"))
            candidates.append(p.get("raw"))

    p2 = product.get("price")
    candidates.append(p2)
    if isinstance(p2, dict):
        candidates.append(p2.get("value"))
        candidates.append(p2.get("raw"))

    for k in ["price_string", "current_price", "displayed_price", "main_price"]:
        if k in product:
            candidates.append(product.get(k))

    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            candidates.append(first.get("price"))
            op = first.get("price")
            if isinstance(op, dict):
                candidates.append(op.get("value"))
                candidates.append(op.get("raw"))

    for c in candidates:
        f = _to_float(c)
        if f is not None and f > 0:
            return f

    return None


def extract_thickness(product: dict):
    if not isinstance(product, dict):
        return None

    specs = product.get("specifications", [])
    if not isinstance(specs, list):
        return None

    for sp in specs:
        if not isinstance(sp, dict):
            continue
        name = (sp.get("name") or "").strip().lower()
        val = sp.get("value")
        if val is None:
            continue

        if any(k in name for k in ["thickness", "pile height", "pileheight", "height", "depth"]):
            s = str(val).lower().strip().replace(" ", "")
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(cm|mm|m)", s)
            if not m:
                continue
            num = float(m.group(1).replace(",", "."))
            unit = m.group(2)
            if unit == "m":
                num *= 100
            elif unit == "mm":
                num /= 10
            if abs(num - int(num)) < 1e-9:
                return f"{int(num)}cm"
            return f"{str(num).rstrip('0').rstrip('.')}cm"

    return None


def extract_size(product: dict):
    if not isinstance(product, dict):
        return None

    raw_candidates = []

    variants = product.get("variants")
    if isinstance(variants, dict):
        selected = variants.get("selected")
        if isinstance(selected, dict):
            for key in ["size", "size_name", "dimensions", "value", "name"]:
                v = selected.get(key)
                if v:
                    raw_candidates.append(str(v))

    attrs = product.get("attributes")
    if isinstance(attrs, dict):
        for key in ["size", "size_name", "dimensions"]:
            v = attrs.get(key)
            if v:
                raw_candidates.append(str(v))

    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            name = (sp.get("name") or "").strip().lower()
            val = sp.get("value")
            if val is None:
                continue
            if any(k in name for k in ["size name", "size", "rug size", "carpet size"]):
                raw_candidates.append(str(val))
                continue
            if any(k in name for k in ["item dimensions", "dimensions"]):
                raw_candidates.append(str(val))

    def normalize_size(s: str):
        s0 = s.lower().strip()
        s0 = s0.split(";")[0]
        s0 = re.sub(r"\(.*?\)", "", s0).strip()
        s0 = s0.replace("×", "x")
        s0 = re.sub(r"\s*x\s*", "x", s0)

        m = re.search(r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)(?:x(\d{1,4}(?:[.,]\d+)?))?\s*(cm|mm|m)\b", s0)
        if not m:
            m2 = re.search(r"(\d+(?:[.,]\d+)?)\s*[lw]\s*x\s*(\d+(?:[.,]\d+)?)\s*[lw]\s*(metre|metres|m)\b", s0)
            if m2:
                a = float(m2.group(1).replace(",", ".")) * 100
                b = float(m2.group(2).replace(",", ".")) * 100
                return f"{int(round(a))}x{int(round(b))}cm"
            return None

        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
        unit = m.group(4)

        if unit == "m":
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

    prioritized = []
    for c in raw_candidates:
        lc = c.lower()
        score = 0
        if "size" in lc:
            score += 3
        if "cm" in lc or "metre" in lc or "m" in lc:
            score += 2
        if "kg" in lc:
            score -= 2
        prioritized.append((score, c))

    prioritized.sort(key=lambda x: x[0], reverse=True)

    for _, cand in prioritized:
        norm = normalize_size(cand)
        if norm:
            return norm

    return None


def _deep_voucher_candidates(obj, path=""):
    """
    Recursively find voucher-related numeric prices inside any nested structure.
    Returns list of tuples: (score, price_float, evidence_path, evidence_text)
    """
    out = []

    def add_candidate(score, val, pth, text):
        f = _to_float(val)
        if f is not None and f > 0:
            out.append((score, f, pth, text))

    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()
            new_path = f"{path}.{k}" if path else str(k)

            # Strong key signals
            if "voucher" in key and "price" in key:
                add_candidate(100, v, new_path, f"key={k}")

            if "coupon" in key and "price" in key:
                add_candidate(90, v, new_path, f"key={k}")

            # Some providers store it in "text"/"label"
            if isinstance(v, str):
                s = v.lower()
                if "voucher price" in s:
                    add_candidate(120, v, new_path, v)
                elif ("voucher" in s or "coupon" in s) and re.search(r"\d", s) and "price" in s:
                    add_candidate(80, v, new_path, v)

            # Recurse
            out.extend(_deep_voucher_candidates(v, new_path))

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            new_path = f"{path}[{i}]"
            out.extend(_deep_voucher_candidates(item, new_path))

    elif isinstance(obj, str):
        s = obj.lower()
        if "voucher price" in s:
            add_candidate(120, obj, path or "<string>", obj)
        elif ("voucher" in s or "coupon" in s) and "price" in s and re.search(r"\d", s):
            add_candidate(70, obj, path or "<string>", obj)

    return out


def extract_voucher_price(product: dict, base_price: float | None):
    """
    Finds explicit voucher price (e.g. 'Voucher price £59.99') inside the payload.
    Chooses best candidate <= base_price (if base_price exists).
    """
    candidates = _deep_voucher_candidates(product)

    if DEBUG and candidates:
        show = sorted(candidates, key=lambda x: x[0], reverse=True)[:20]
        st.write("Voucher candidates (top 20):")
        st.dataframe(pd.DataFrame(show, columns=["score", "price", "path", "evidence"]))

    if not candidates:
        return None

    # Prefer candidates <= base_price (voucher price should be lower or equal)
    if base_price is not None and base_price > 0:
        valid = [c for c in candidates if c[1] <= base_price]
        if valid:
            # best score, and among same score choose closest to base (largest but <= base)
            valid.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return valid[0][1]

    # Fallback: just best score
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
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
                "Thickness": None,
                "% Discount": 0,
                "% Voucher": 0,
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
                    row["Thickness"] = extract_thickness(product)

                    base_price = extract_price(product)           # visible price (discount included)
                    list_price = extract_list_price(product)      # was/rrp if exists

                    # % Discount (only if list exists)
                    if list_price and base_price:
                        row["% Discount"] = _pct(list_price, base_price)

                    # Voucher price: must be explicit voucher price in payload
                    voucher_price = extract_voucher_price(product, base_price)

                    # Apply voucher: Final price must become voucher price if it exists
                    if base_price is not None and voucher_price is not None and voucher_price > 0 and voucher_price <= base_price:
                        row["% Voucher"] = _pct(base_price, voucher_price)
                        row["Final price"] = voucher_price
                    else:
                        row["% Voucher"] = 0
                        row["Final price"] = base_price

            except Exception:
                pass

            results.append(row)

        progress.progress(i / total)
        time.sleep(0.25)

    df = pd.DataFrame(results, columns=[
        "Brand", "ASIN", "Size", "Thickness", "% Discount", "% Voucher", "Final price", "Date (UTC)"
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
