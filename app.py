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
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip()
        # keep digits, dot, comma
        m = re.search(r"(\d+[.,]\d+|\d+)", s)
        if not m:
            return None
        return float(m.group(1).replace(",", "."))
    if isinstance(x, dict):
        # common: {"value": 74.99, "currency": "GBP", "raw": "£74.99"}
        for k in ["value", "amount", "raw"]:
            v = x.get(k)
            f = _to_float(v)
            if f is not None:
                return f
    return None


def _pct(a, b):
    """percent reduction from a -> b, e.g. a=74.99 b=59.99 => 20"""
    if a is None or b is None:
        return 0
    if a <= 0:
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
    """
    Attempts to find list/rrp/was price (used for % Discount).
    """
    if not isinstance(product, dict):
        return None

    candidates = []

    # common keys
    for k in ["list_price", "rrp", "was_price", "price_was", "price_before_discount"]:
        if k in product:
            candidates.append(product.get(k))

    # buybox/list price variants
    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        for k in ["list_price", "rrp", "was_price"]:
            if k in buybox:
                candidates.append(buybox.get(k))

    # sometimes in "prices" dict
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
    """
    Current displayed price (discount already included if Amazon shows it).
    """
    if not isinstance(product, dict):
        return None

    candidates = []

    # buybox.price
    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        candidates.append(buybox.get("price"))
        p = buybox.get("price")
        if isinstance(p, dict):
            candidates.append(p.get("value"))
            candidates.append(p.get("raw"))

    # buybox_winner.price
    bbw = product.get("buybox_winner")
    if isinstance(bbw, dict):
        candidates.append(bbw.get("price"))
        p = bbw.get("price")
        if isinstance(p, dict):
            candidates.append(p.get("value"))
            candidates.append(p.get("raw"))

    # product.price
    p2 = product.get("price")
    candidates.append(p2)
    if isinstance(p2, dict):
        candidates.append(p2.get("value"))
        candidates.append(p2.get("raw"))

    # fallbacks
    for k in ["price_string", "current_price", "displayed_price", "main_price"]:
        if k in product:
            candidates.append(product.get(k))

    # offers[0].price
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


def extract_voucher_price(product: dict):
    """
    Tries to find "voucher price" (explicit voucher price like £59.99).
    If found, return that numeric price. Otherwise None.
    """
    if not isinstance(product, dict):
        return None

    candidates = []

    # direct keys
    for k in ["voucher_price", "voucherPrice", "coupon_price", "price_with_coupon", "price_with_voucher"]:
        if k in product:
            candidates.append(product.get(k))

    # nested in "prices"
    prices = product.get("prices")
    if isinstance(prices, dict):
        for k in ["voucher_price", "coupon_price", "price_with_coupon", "price_with_voucher"]:
            if k in prices:
                candidates.append(prices.get(k))

    # promotions / coupon blocks sometimes include explicit voucher price
    for k in ["promotions", "promotion", "coupons", "coupon", "deal", "deals"]:
        obj = product.get(k)
        if isinstance(obj, dict):
            # try obvious fields
            for kk in ["voucher_price", "coupon_price", "price", "final_price", "discounted_price", "raw"]:
                if kk in obj:
                    candidates.append(obj.get(kk))
            # sometimes text like "Voucher price £59.99"
            for kk in ["text", "description", "message", "label", "raw"]:
                if kk in obj and isinstance(obj.get(kk), str):
                    candidates.append(obj.get(kk))
        elif isinstance(obj, list):
            for item in obj:
                if not isinstance(item, (dict, str)):
                    continue
                if isinstance(item, str):
                    candidates.append(item)
                    continue
                for kk in ["voucher_price", "coupon_price", "price", "final_price", "discounted_price", "raw"]:
                    if kk in item:
                        candidates.append(item.get(kk))
                for kk in ["text", "description", "message", "label", "raw"]:
                    if kk in item and isinstance(item.get(kk), str):
                        candidates.append(item.get(kk))

    # parse “Voucher price £59.99” from any candidate strings
    parsed = []
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, str):
            # must mention voucher/coupon AND contain a number
            s = c.lower()
            if ("voucher" in s or "coupon" in s) and re.search(r"\d", s):
                f = _to_float(c)
                if f is not None:
                    parsed.append(f)
        else:
            f = _to_float(c)
            if f is not None:
                parsed.append(f)

    # choose the lowest sensible price (voucher price should be <= normal price)
    if parsed:
        parsed = [p for p in parsed if p > 0]
        if parsed:
            return min(parsed)

    return None


def extract_thickness(product: dict):
    """
    Returns thickness like "0.5cm" if found.
    """
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
            # grab something like 0.5cm / 5mm
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(cm|mm|m)", s)
            if not m:
                continue
            num = float(m.group(1).replace(",", "."))
            unit = m.group(2)
            # normalize to cm
            if unit == "m":
                num *= 100
            elif unit == "mm":
                num /= 10
            # format
            if abs(num - int(num)) < 1e-9:
                return f"{int(num)}cm"
            return f"{str(num).rstrip('0').rstrip('.')}cm"

    return None


def extract_size(product: dict):
    """
    Returns clean size like 122x170cm (ONLY).
    Priority:
      1) variants.selected.* (size option)
      2) specification names that are clearly size (not item dimensions, not weight)
    """
    if not isinstance(product, dict):
        return None

    raw_candidates = []

    # 1) variants.selected (best for chosen size like "160 x 220 cm")
    variants = product.get("variants")
    if isinstance(variants, dict):
        selected = variants.get("selected")
        if isinstance(selected, dict):
            for key in ["size", "size_name", "dimensions", "value", "name"]:
                v = selected.get(key)
                if v:
                    raw_candidates.append(str(v))

    # 2) attributes fallback
    attrs = product.get("attributes")
    if isinstance(attrs, dict):
        for key in ["size", "size_name", "dimensions"]:
            v = attrs.get(key)
            if v:
                raw_candidates.append(str(v))

    # 3) specifications (filtered)
    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            name = (sp.get("name") or "").strip().lower()
            val = sp.get("value")
            if val is None:
                continue

            # strong signals for "size"
            if any(k in name for k in ["size name", "size", "rug size", "carpet size"]):
                raw_candidates.append(str(val))
                continue

            # "item dimensions" often includes thickness/weight/metres -> lower priority
            if any(k in name for k in ["item dimensions", "dimensions"]):
                raw_candidates.append(str(val))

    # pick first candidate that looks like "NxM cm"
    def normalize_size(s: str):
        s0 = s.lower().strip()
        # remove anything after ';' (often weight)
        s0 = s0.split(";")[0]
        # remove bracketed like "(rectangular)"
        s0 = re.sub(r"\(.*?\)", "", s0).strip()
        # normalize separators
        s0 = s0.replace("×", "x")
        # remove spaces around x
        s0 = re.sub(r"\s*x\s*", "x", s0)
        # keep spaces elsewhere for unit parsing
        # find two dims + unit
        m = re.search(r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)(?:x(\d{1,4}(?:[.,]\d+)?))?\s*(cm|mm|m)\b", s0)
        if not m:
            # sometimes "2.2L x 1.6W metres"
            m2 = re.search(r"(\d+(?:[.,]\d+)?)\s*[lw]\s*x\s*(\d+(?:[.,]\d+)?)\s*[lw]\s*(metre|metres|m)\b", s0)
            if m2:
                a = float(m2.group(1).replace(",", ".")) * 100
                b = float(m2.group(2).replace(",", ".")) * 100
                return f"{int(round(a))}x{int(round(b))}cm"
            return None

        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
        unit = m.group(4)

        # normalize unit -> cm
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

    # prefer candidates that explicitly include "size" wording
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
            base_price = None
            voucher_price = None

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

                    base_price = extract_price(product)
                    list_price = extract_list_price(product)

                    # discount based on list->base
                    if list_price and base_price:
                        row["% Discount"] = _pct(list_price, base_price)

                    # voucher price (explicit voucher price, if exists)
                    voucher_price = extract_voucher_price(product)

                    # voucher percent + final price rules:
                    # If voucher price exists and is lower than base price -> apply voucher
                    if base_price is not None and voucher_price is not None and voucher_price > 0 and voucher_price <= base_price:
                        row["% Voucher"] = _pct(base_price, voucher_price)
                        row["Final price"] = voucher_price
                    else:
                        row["% Voucher"] = 0
                        row["Final price"] = base_price

            except Exception:
                # keep row minimal; app shouldn't crash
                pass

            results.append(row)

        progress.progress(i / total)
        time.sleep(0.25)

    # Ensure column order exactly as requested
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
