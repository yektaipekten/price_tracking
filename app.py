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
        # NOTE: Do NOT include voucher/coupon keys here (base price extraction must avoid them).
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


# ---------- PRICE EXTRACTION (FIXED) ----------
def _contains_voucher_coupon_key(key: str) -> bool:
    k = (key or "").lower()
    return ("voucher" in k) or ("coupon" in k)


def extract_base_price(product: dict):
    """
    Base price MUST be the displayed price BEFORE voucher/coupon application.
    Therefore we:
      - prefer buybox / buybox_winner price.value/raw
      - ignore any dict keys containing voucher/coupon
      - ignore any fields that are explicitly voucher/coupon price
    """
    if not isinstance(product, dict):
        return None

    def price_from_price_obj(pobj):
        if pobj is None:
            return None
        if isinstance(pobj, (int, float, str)):
            return _to_float(pobj)
        if isinstance(pobj, dict):
            # only safe keys
            for k in ["value", "raw", "amount"]:
                if k in pobj and not _contains_voucher_coupon_key(k):
                    f = _to_float(pobj.get(k))
                    if f is not None and f > 0:
                        return f
        return None

    # 1) buybox.price
    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        p = buybox.get("price")
        f = price_from_price_obj(p)
        if f:
            return f

    # 2) buybox_winner.price
    bbw = product.get("buybox_winner")
    if isinstance(bbw, dict):
        p = bbw.get("price")
        f = price_from_price_obj(p)
        if f:
            return f

    # 3) product.price
    p2 = product.get("price")
    f = price_from_price_obj(p2)
    if f:
        return f

    # 4) fallback string fields (avoid voucher/coupon labelled)
    for k in ["price_string", "current_price", "displayed_price", "main_price", "price_current"]:
        if k in product and not _contains_voucher_coupon_key(k):
            f = _to_float(product.get(k))
            if f is not None and f > 0:
                return f

    # 5) offers[0].price
    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            p = first.get("price")
            f = price_from_price_obj(p)
            if f:
                return f

    return None


def _deep_find_price_candidates(obj, want="list_price"):
    """
    Deep scan for list/rrp/was/before/strike price candidates.
    want="list_price" only (exclude voucher/coupon).
    Returns list of (score, price_float).
    """
    out = []

    def add(score, v):
        f = _to_float(v) if not isinstance(v, dict) else _to_float(v)
        if f is not None and f > 0:
            out.append((score, f))

    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()

            # ignore voucher/coupon branches entirely
            if _contains_voucher_coupon_key(key):
                continue

            if want == "list_price":
                # strong signals for "before price"
                if any(t in key for t in ["list_price", "rrp", "was_price", "before_price", "original_price",
                                          "strikethrough", "strike", "price_before_discount", "price_was", "old_price"]):
                    add(120, v)

            out.extend(_deep_find_price_candidates(v, want=want))

    elif isinstance(obj, list):
        for item in obj:
            out.extend(_deep_find_price_candidates(item, want=want))

    return out


def extract_list_price(product: dict):
    """
    More robust list/was/rrp/before price extraction:
      - shallow candidates
      - then deep scan
    """
    if not isinstance(product, dict):
        return None

    candidates = []

    # shallow (same as before)
    for k in [
        "list_price", "rrp", "was_price", "price_was", "price_before_discount",
        "before_price", "original_price", "strikethrough_price", "price_original"
    ]:
        if k in product and not _contains_voucher_coupon_key(k):
            candidates.append(product.get(k))

    p = product.get("price")
    if isinstance(p, dict):
        for k in ["before_price", "was_price", "list_price", "rrp", "original", "old", "strike", "strikethrough", "raw_before"]:
            if k in p and not _contains_voucher_coupon_key(k):
                candidates.append(p.get(k))

    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        for k in ["list_price", "rrp", "was_price", "before_price", "original_price", "strikethrough_price"]:
            if k in buybox and not _contains_voucher_coupon_key(k):
                candidates.append(buybox.get(k))
        bp = buybox.get("price")
        if isinstance(bp, dict):
            for k in ["before_price", "was_price", "list_price", "rrp", "original", "old", "strikethrough", "strike"]:
                if k in bp and not _contains_voucher_coupon_key(k):
                    candidates.append(bp.get(k))

    savings = product.get("savings")
    if isinstance(savings, dict):
        for k in ["price_before_discount", "before_price", "original_price", "was_price"]:
            if k in savings and not _contains_voucher_coupon_key(k):
                candidates.append(savings.get(k))

    # pick first valid
    for c in candidates:
        f = _to_float(c)
        if f is not None and f > 0:
            return f

    # deep scan
    deep = _deep_find_price_candidates(product, want="list_price")
    if deep:
        deep.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return deep[0][1]

    return None


def _deep_find_percent_candidates(obj):
    """
    Deep scan for discount percent candidates (exclude voucher/coupon).
    Returns list of (score, percent_int)
    """
    out = []

    def add(score, v):
        if isinstance(v, (int, float)) and v >= 0:
            out.append((score, int(round(v))))
            return
        if isinstance(v, str):
            m = re.search(r"(\d{1,3})\s*%?", v)
            if m:
                out.append((score, int(m.group(1))))

    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()

            # ignore voucher/coupon
            if _contains_voucher_coupon_key(key):
                continue

            # strong percent keys
            if any(t in key for t in ["discount_percentage", "savings_percentage", "deal_percentage", "percent_off", "percentage", "percent"]):
                add(140, v)

            # common deal/savings blocks (the percent might be inside)
            if any(t in key for t in ["deal", "deals", "savings", "promotion", "promotions"]):
                # just recurse; score will be applied inside
                pass

            out.extend(_deep_find_percent_candidates(v))

    elif isinstance(obj, list):
        for item in obj:
            out.extend(_deep_find_percent_candidates(item))

    return out


def extract_discount_percent(product: dict, list_price: float | None, base_price: float | None):
    """
    Robust discount %:
      1) deep scan for percent fields (non voucher)
      2) else compute from list_price -> base_price
    """
    if not isinstance(product, dict):
        return 0

    cands = _deep_find_percent_candidates(product)
    if cands:
        cands.sort(key=lambda x: x[0], reverse=True)
        p = cands[0][1]
        if 0 <= p <= 99:
            return p

    if list_price and base_price:
        return _pct(list_price, base_price)

    return 0


def _deep_find_voucher_price_candidates(obj):
    """
    Recursively scan payload for voucher/coupon PRICE signals.
    Returns list of tuples: (score, price_float)
    """
    out = []

    def add(score, value):
        f = _to_float(value)
        if f is not None and f > 0:
            out.append((score, f))

    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()

            # voucher/coupon branches are relevant here
            if "voucher price" in key or ("voucher" in key and "price" in key) or ("coupon" in key and "price" in key):
                add(200, v)

            # sometimes voucher text is in strings under promotions/deals
            if isinstance(v, str):
                s = v.lower()
                if "voucher price" in s:
                    add(220, v)

            out.extend(_deep_find_voucher_price_candidates(v))

    elif isinstance(obj, list):
        for item in obj:
            out.extend(_deep_find_voucher_price_candidates(item))

    elif isinstance(obj, str):
        s = obj.lower()
        if "voucher price" in s:
            add(220, obj)

    return out


def extract_voucher_price(product: dict, base_price: float | None):
    """
    Returns voucher price (e.g., 59.99) if exists.
    Chooses best candidate <= base_price when possible.
    """
    if not isinstance(product, dict):
        return None

    cands = _deep_find_voucher_price_candidates(product)
    if not cands:
        return None

    # prefer <= base_price if base_price known
    if base_price is not None and base_price > 0:
        valid = [c for c in cands if c[1] <= base_price]
        if valid:
            valid.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return valid[0][1]

    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return cands[0][1]


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
                "Base price": None,
                "Voucher price": None,
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

                    base_price = extract_base_price(product)     # FIXED: base price excludes voucher/coupon
                    list_price = extract_list_price(product)     # FIXED: deeper list price search
                    voucher_price = extract_voucher_price(product, base_price)

                    row["Base price"] = base_price
                    row["Voucher price"] = voucher_price

                    row["% Discount"] = extract_discount_percent(product, list_price, base_price)

                    # Final price logic:
                    # if voucher exists and is lower than base, final = voucher
                    if base_price is not None and voucher_price is not None and voucher_price > 0 and voucher_price < base_price:
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
        "Brand", "ASIN", "Size",
        "% Discount",
        "Base price", "Voucher price", "% Voucher",
        "Final price",
        "Date (UTC)"
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
