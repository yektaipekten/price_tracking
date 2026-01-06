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
def _parse_number(s):
    """Return float from a messy string like '£74.99', '0.5 cm', '2,07 kg' etc."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    m = re.search(r"(\d+[.,]\d+|\d+)", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except:
        return None

def _fmt_pct(x):
    if x is None:
        return 0
    try:
        return int(round(float(x)))
    except:
        return 0

def _fmt_cm(x):
    if x is None:
        return None
    try:
        xf = float(x)
        if abs(xf - int(xf)) < 1e-9:
            return str(int(xf))
        return str(xf).rstrip("0").rstrip(".")
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

def _extract_price_candidates(product: dict):
    """Collect likely price candidates in Rainforest payload."""
    cands = []

    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        cands.append(buybox.get("price"))
        p = buybox.get("price")
        if isinstance(p, dict):
            cands += [p.get("value"), p.get("raw")]

    bbw = product.get("buybox_winner")
    if isinstance(bbw, dict):
        cands.append(bbw.get("price"))
        p = bbw.get("price")
        if isinstance(p, dict):
            cands += [p.get("value"), p.get("raw")]

    p2 = product.get("price")
    cands.append(p2)
    if isinstance(p2, dict):
        cands += [p2.get("value"), p2.get("raw")]

    for k in ["price_string", "current_price", "displayed_price", "main_price"]:
        if k in product:
            cands.append(product.get(k))

    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            op = first.get("price")
            cands.append(op)
            if isinstance(op, dict):
                cands += [op.get("value"), op.get("raw")]

    return cands

def extract_price(product: dict):
    """Return numeric current price (already discounted if discount exists)."""
    if not isinstance(product, dict):
        return None

    for c in _extract_price_candidates(product):
        if c is None:
            continue
        if isinstance(c, (int, float)):
            return float(c)
        if isinstance(c, dict):
            v = c.get("value")
            if isinstance(v, (int, float)):
                return float(v)
            raw = c.get("raw")
            num = _parse_number(raw)
            if num is not None:
                return num
        if isinstance(c, str):
            num = _parse_number(c)
            if num is not None:
                return num
    return None

def extract_discount_pct(product: dict, current_price: float):
    """
    Compute discount % if we can find a 'before' price / list price.
    If not found -> 0
    """
    if not isinstance(product, dict) or current_price is None:
        return 0

    before_candidates = []

    # common fields (vary by payload)
    for k in ["price_before_discount", "price_strikethrough", "list_price", "was_price", "rrp"]:
        if k in product:
            before_candidates.append(product.get(k))

    # sometimes nested
    lp = product.get("list_price")
    if isinstance(lp, dict):
        before_candidates += [lp.get("value"), lp.get("raw")]

    # buybox/price objects sometimes contain "before_price"/"savings"
    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        for k in ["before_price", "list_price", "rrp"]:
            if k in buybox:
                before_candidates.append(buybox.get(k))
        p = buybox.get("price")
        if isinstance(p, dict) and "before_price" in p:
            before_candidates.append(p.get("before_price"))

    # parse numeric
    before = None
    for c in before_candidates:
        val = None
        if isinstance(c, (int, float)):
            val = float(c)
        elif isinstance(c, dict):
            val = _parse_number(c.get("value")) or _parse_number(c.get("raw"))
        else:
            val = _parse_number(c)
        if val and val > 0:
            before = val
            break

    if not before or before <= current_price:
        return 0

    pct = (before - current_price) / before * 100.0
    return _fmt_pct(pct)

def _find_dimension_strings(product: dict):
    """Return candidate strings that may include dimensions and thickness."""
    cands = []
    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            name = (sp.get("name") or "").strip().lower()
            val = sp.get("value")
            if val is None:
                continue
            if any(k in name for k in ["size", "dimension", "dimensions", "item dimensions", "rug size", "measurements"]):
                cands.append(str(val))

    variants = product.get("variants")
    if isinstance(variants, dict):
        selected = variants.get("selected")
        if isinstance(selected, dict):
            for key in ["size", "dimensions", "value", "name"]:
                if selected.get(key):
                    cands.append(str(selected.get(key)))

    attrs = product.get("attributes")
    if isinstance(attrs, dict):
        for key in ["size", "dimensions"]:
            if attrs.get(key):
                cands.append(str(attrs.get(key)))

    return [x for x in cands if x and x.strip()]

def extract_size_and_thickness(product: dict):
    """
    Size => always min x max in cm, like 122x170cm
    Thickness => from 3rd dimension (if present) or from thickness/pile height specs
    """
    if not isinstance(product, dict):
        return None, None

    # thickness from explicit specs first
    thickness = None
    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            name = (sp.get("name") or "").strip().lower()
            val = sp.get("value")
            if val is None:
                continue
            if any(k in name for k in ["thickness", "pile height", "pileheight", "height", "depth"]):
                t = _parse_number(val)
                if t is not None:
                    # try to detect unit
                    v = str(val).lower()
                    unit = "cm"
                    if "mm" in v:
                        t = t / 10.0
                    elif "m" in v and "cm" not in v and "mm" not in v:
                        t = t * 100.0
                    thickness = f"{_fmt_cm(t)}cm"
                    break

    dim_strings = _find_dimension_strings(product)
    if not dim_strings:
        return None, thickness

    raw = " | ".join(dim_strings)
    raw = raw.strip().lower().replace("×", "x")
    # cut off after ';' (often weight)
    raw = raw.split(";")[0].strip()
    raw = raw.replace(" ", "")

    # match: AxB (xC optional) + unit optional
    m = re.search(r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)(?:x(\d{1,4}(?:[.,]\d+)?))?(cm|mm|m)?", raw)
    if not m:
        return raw, thickness

    a = _parse_number(m.group(1))
    b = _parse_number(m.group(2))
    c = _parse_number(m.group(3)) if m.group(3) else None
    unit = m.group(4) or "cm"

    if a is None or b is None:
        return raw, thickness

    # normalize to cm
    if unit == "m":
        a, b = a * 100.0, b * 100.0
        if c is not None:
            c = c * 100.0
        unit = "cm"
    elif unit == "mm":
        a, b = a / 10.0, b / 10.0
        if c is not None:
            c = c / 10.0
        unit = "cm"

    # enforce rug convention: smaller x larger
    w = min(a, b)
    l = max(a, b)
    size = f"{_fmt_cm(w)}x{_fmt_cm(l)}cm"

    # thickness from 3rd dim if we still don't have it
    if thickness is None and c is not None and c > 0:
        thickness = f"{_fmt_cm(c)}cm"

    return size, thickness

def extract_voucher_price_and_pct(product: dict, current_price: float):
    """
    Rainforest payloads vary. We try to detect:
    - voucher_price (final after voucher) OR
    - voucher percent
    Returns: (voucher_pct_int, voucher_price_float)
    """
    if not isinstance(product, dict):
        return 0, None

    candidates = []

    # common keys
    for k in ["voucher", "coupon", "coupons", "promotions", "promotion", "deal", "deals"]:
        if k in product:
            candidates.append(product.get(k))

    # sometimes buybox contains coupon/voucher info
    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        for k in ["voucher", "coupon", "promotion", "promotions"]:
            if k in buybox:
                candidates.append(buybox.get(k))

    voucher_price = None
    voucher_pct = 0

    def scan_obj(obj):
        nonlocal voucher_price, voucher_pct
        if obj is None:
            return
        if isinstance(obj, dict):
            # explicit voucher price fields
            for key in ["voucher_price", "coupon_price", "price_with_coupon", "price_with_voucher", "final_price"]:
                if key in obj and voucher_price is None:
                    vp = _parse_number(obj.get(key))
                    if vp is not None:
                        voucher_price = vp

            # percent fields
            for key in ["voucher_percent", "coupon_percent", "percentage", "percent", "discount_percent"]:
                if key in obj and voucher_pct == 0:
                    p = _parse_number(obj.get(key))
                    if p is not None:
                        voucher_pct = _fmt_pct(p)

            # text fields (e.g. "Save 75%" or "75% voucher")
            for key in ["label", "text", "message", "description", "raw"]:
                if key in obj and voucher_pct == 0:
                    txt = str(obj.get(key) or "")
                    m = re.search(r"(\d{1,2})\s*%", txt)
                    if m:
                        voucher_pct = _fmt_pct(m.group(1))

            # recurse
            for v in obj.values():
                scan_obj(v)

        elif isinstance(obj, list):
            for v in obj:
                scan_obj(v)
        else:
            # string fallback
            txt = str(obj)
            if voucher_pct == 0:
                m = re.search(r"(\d{1,2})\s*%", txt)
                if m:
                    voucher_pct = _fmt_pct(m.group(1))
            if voucher_price is None:
                # if it contains a currency-ish number, it might be voucher price
                vp = _parse_number(txt)
                # only accept as voucher_price if it's plausible and <= current_price
                if vp is not None and current_price is not None and vp > 0 and vp <= current_price:
                    voucher_price = vp

    for c in candidates:
        scan_obj(c)

    # If voucher_price exists but pct not, compute pct from current price
    if voucher_price is not None and current_price is not None and current_price > 0 and voucher_pct == 0:
        voucher_pct = _fmt_pct((1.0 - (voucher_price / current_price)) * 100.0)

    return voucher_pct, voucher_price

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
                "% Discount": 0,
                "% Voucher": 0,
                "Final price": None,
                "Date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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

                    size, thickness = extract_size_and_thickness(product)
                    row["Size"] = size
                    row["Thickness"] = thickness

                    price = extract_price(product)  # discounted price if discount exists
                    row["Final price"] = price

                    # discount %
                    row["% Discount"] = extract_discount_pct(product, price)

                    # voucher
                    voucher_pct, voucher_price = extract_voucher_price_and_pct(product, price)

                    row["% Voucher"] = voucher_pct

                    # IMPORTANT: If voucher_price exists, final price should be voucher_price (as you requested)
                    if voucher_price is not None:
                        row["Final price"] = voucher_price
                    else:
                        # If only voucher_pct exists, compute final price from current price
                        if price is not None and voucher_pct:
                            row["Final price"] = round(price * (1.0 - voucher_pct / 100.0), 2)

            except Exception:
                # keep app running; row stays partially empty
                pass

            results.append(row)

        progress.progress(i / total)
        time.sleep(0.25)

    df = pd.DataFrame(results)

    # Ensure column order exactly as requested
    col_order = ["Brand", "ASIN", "Size", "Thickness", "% Discount", "% Voucher", "Final price", "Date"]
    df = df.reindex(columns=col_order)

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
