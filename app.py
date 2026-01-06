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
        for k in ["value", "amount", "raw", "price", "final_price", "voucher_price", "coupon_price"]:
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

    # Pattern: 122 x 170 cm
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*(cm|mm|m|metre|metres)\b", s)
    if m:
        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
        unit = m.group(3)
        a_cm = _normalize_to_cm(a, unit)
        b_cm = _normalize_to_cm(b, unit)
        return (a_cm, b_cm)

    # Some titles: "160x230CM" without spaces
    s2 = s.replace(" ", "")
    m2 = re.search(r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)\s*(cm|mm|m)\b", s2)
    if m2:
        a = float(m2.group(1).replace(",", "."))
        b = float(m2.group(2).replace(",", "."))
        unit = m2.group(3)
        a_cm = _normalize_to_cm(a, unit)
        b_cm = _normalize_to_cm(b, unit)
        return (a_cm, b_cm)

    return None


def _format_size(a_cm: float, b_cm: float) -> str:
    # Keep as "122x170cm"
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


def extract_price(product: dict):
    """Current displayed price (Amazon discount already included if shown)."""
    if not isinstance(product, dict):
        return None

    candidates = []

    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        candidates.append(buybox.get("price"))
        p = buybox.get("price")
        if isinstance(p, dict):
            candidates.extend([p.get("value"), p.get("raw")])

    bbw = product.get("buybox_winner")
    if isinstance(bbw, dict):
        candidates.append(bbw.get("price"))
        p = bbw.get("price")
        if isinstance(p, dict):
            candidates.extend([p.get("value"), p.get("raw")])

    p2 = product.get("price")
    candidates.append(p2)
    if isinstance(p2, dict):
        candidates.extend([
            p2.get("value"),
            p2.get("raw"),
            p2.get("current"),
            p2.get("now"),
            p2.get("displayed"),
        ])

    for k in ["price_string", "current_price", "displayed_price", "main_price", "price_current"]:
        if k in product:
            candidates.append(product.get(k))

    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            candidates.append(first.get("price"))
            op = first.get("price")
            if isinstance(op, dict):
                candidates.extend([op.get("value"), op.get("raw")])

    for c in candidates:
        f = _to_float(c)
        if f is not None and f > 0:
            return f

    return None


def extract_list_price(product: dict):
    """Finds 'before/rrp/list/was' price for computing % Discount."""
    if not isinstance(product, dict):
        return None

    candidates = []

    # top-level
    for k in [
        "list_price", "rrp", "was_price", "price_was", "price_before_discount",
        "before_price", "original_price", "strikethrough_price", "price_original"
    ]:
        if k in product:
            candidates.append(product.get(k))

    # product.price dict may contain before/was/list
    p = product.get("price")
    if isinstance(p, dict):
        for k in ["before_price", "was_price", "list_price", "rrp", "original", "old", "strike", "strikethrough", "raw_before"]:
            if k in p:
                candidates.append(p.get(k))

    # buybox
    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        for k in ["list_price", "rrp", "was_price", "before_price", "original_price", "strikethrough_price"]:
            if k in buybox:
                candidates.append(buybox.get(k))
        bp = buybox.get("price")
        if isinstance(bp, dict):
            for k in ["before_price", "was_price", "list_price", "rrp", "original", "old", "strikethrough"]:
                if k in bp:
                    candidates.append(bp.get(k))

    # prices dict
    prices = product.get("prices")
    if isinstance(prices, dict):
        for k in ["list_price", "rrp", "was_price", "price_was", "before_price", "original_price", "strikethrough_price"]:
            if k in prices:
                candidates.append(prices.get(k))

    # savings block sometimes includes "price_before_discount"
    savings = product.get("savings")
    if isinstance(savings, dict):
        for k in ["price_before_discount", "before_price", "original_price", "was_price"]:
            if k in savings:
                candidates.append(savings.get(k))

    for c in candidates:
        f = _to_float(c)
        if f is not None and f > 0:
            return f

    return None


def extract_discount_percent(product: dict, list_price: float | None, base_price: float | None):
    """
    Prefer explicit percent if Rainforest provides it; else compute from list->base.
    """
    if not isinstance(product, dict):
        return 0

    # explicit percent fields
    for k in ["discount_percentage", "savings_percentage", "deal_percentage", "percent_off"]:
        v = product.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            return int(round(v))
        if isinstance(v, str):
            m = re.search(r"(\d+)\s*%?", v)
            if m:
                return int(m.group(1))

    # nested common blocks
    for block_key in ["deal", "deals", "savings", "promotion", "promotions", "buybox", "buybox_winner"]:
        blk = product.get(block_key)
        if isinstance(blk, dict):
            for k in ["discount_percentage", "savings_percentage", "percent_off", "deal_percentage"]:
                v = blk.get(k)
                if isinstance(v, (int, float)) and v >= 0:
                    return int(round(v))
                if isinstance(v, str):
                    m = re.search(r"(\d+)\s*%?", v)
                    if m:
                        return int(m.group(1))

    # compute
    if list_price and base_price:
        return _pct(list_price, base_price)
    return 0


def _deep_find_voucher_candidates(obj, path=""):
    """
    Recursively scan payload for voucher/coupon price signals.
    Returns list of tuples: (score, price_float, path, evidence_str)
    """
    out = []

    def add(score, value, pth, ev):
        f = _to_float(value)
        if f is not None and f > 0:
            out.append((score, f, pth, ev))

    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()
            p = f"{path}.{k}" if path else str(k)

            # strongest: keys that explicitly mean voucher/coupon price
            if ("voucher" in key or "coupon" in key) and "price" in key:
                add(120, v, p, f"key={k}")

            # common blocks holding voucher info
            if key in ["voucher", "vouchers", "coupon", "coupons", "promotions", "promotion", "deal", "deals"]:
                add(40, v, p, f"block={k}")

            # string evidence like "Voucher price £59.99"
            if isinstance(v, str):
                s = v.lower()
                if "voucher price" in s:
                    add(200, v, p, v)
                elif ("voucher" in s or "coupon" in s) and "price" in s:
                    add(80, v, p, v)

            out.extend(_deep_find_voucher_candidates(v, p))

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            p = f"{path}[{i}]"
            out.extend(_deep_find_voucher_candidates(item, p))

    elif isinstance(obj, str):
        s = obj.lower()
        if "voucher price" in s:
            add(200, obj, path or "<string>", obj)
        elif ("voucher" in s or "coupon" in s) and "price" in s:
            add(60, obj, path or "<string>", obj)

    return out


def extract_voucher_price(product: dict, base_price: float | None):
    """
    Finds explicit voucher/coupon price and returns it.
    Strategy:
      - deep recursive scan
      - choose best candidate <= base_price (closest to base but lower), else best scored.
    """
    if not isinstance(product, dict):
        return None

    cands = _deep_find_voucher_candidates(product)
    if not cands:
        return None

    if base_price is not None and base_price > 0:
        valid = [c for c in cands if c[1] <= base_price]
        if valid:
            valid.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return valid[0][1]

    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return cands[0][1]


def extract_size(product: dict):
    """
    EXACT requirement:
      - take size from Product details -> Measurements -> Size
      - OR from title
    No thickness. No package dimensions.

    Returns like: "122x170cm" or None
    """
    if not isinstance(product, dict):
        return None

    # 1) Try specifications: Measurements -> Size
    specs = product.get("specifications", [])
    if isinstance(specs, list):
        # Strategy A: find a "Measurements" group (some payloads use group_name/category)
        for sp in specs:
            if not isinstance(sp, dict):
                continue

            # Some rainforest payloads: {"name":"Size","value":"122 x 170 cm (Rectangular)","group_name":"Measurements"}
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

        # Strategy B: if grouping not available, just search for a "Size" row
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

            # accept only exact "Size" (avoid "Package Dimensions" etc.)
            if nlow == "size":
                parsed = _parse_size_text_to_cm_pair(str(val))
                if parsed:
                    return _format_size(parsed[0], parsed[1])

    # 2) Fallback: parse from title
    title = product.get("title")
    if isinstance(title, str) and title.strip():
        parsed = _parse_size_text_to_cm_pair(title)
        if parsed:
            return _format_size(parsed[0], parsed[1])

    # 3) Variant selected size_name (optional fallback; harmless and often correct)
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

                    base_price = extract_price(product)      # displayed current price
                    list_price = extract_list_price(product) # was/rrp/etc for % discount

                    row["% Discount"] = extract_discount_percent(product, list_price, base_price)

                    voucher_price = extract_voucher_price(product, base_price)

                    # Final price + % voucher
                    if base_price is not None and voucher_price is not None and voucher_price > 0 and voucher_price <= base_price:
                        row["% Voucher"] = _pct(base_price, voucher_price)
                        row["Final price"] = voucher_price
                    else:
                        row["% Voucher"] = 0
                        row["Final price"] = base_price

            except Exception:
                # keep row with None/0 if request fails
                pass

            results.append(row)

        progress.progress(i / total)
        time.sleep(0.25)

    df = pd.DataFrame(results, columns=[
        "Brand", "ASIN", "Size", "% Discount", "% Voucher", "Final price", "Date (UTC)"
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
