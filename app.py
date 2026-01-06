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


# -----------------------
# Helpers (generic)
# -----------------------
def _to_float(x):
    try:
        return float(x)
    except Exception:
        return None


def _parse_number_from_string(s: str):
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d+[.,]\d+|\d+)", s)
    if not m:
        return None
    num = m.group(1).replace(",", ".")
    return _to_float(num)


def _parse_percent_from_string(s: str):
    """Returns percent as float (e.g. 15 for '15%') or None."""
    if not s:
        return None
    s = str(s)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", s)
    if not m:
        return None
    return _to_float(m.group(1).replace(",", "."))


def _normalize_mult_sign(s: str):
    return s.replace("×", "x").replace("X", "x")


def _format_percent(p):
    if p is None:
        return 0
    try:
        p = float(p)
    except Exception:
        return 0
    # 0-1 gelirse yüzdeye çevir
    if 0 < p < 1:
        p *= 100
    return round(p, 2)


# -----------------------
# Extraction functions
# -----------------------
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


def extract_price_value(product: dict):
    """
    Tries multiple common Rainforest paths.
    Returns float if possible, otherwise None.
    """
    if not isinstance(product, dict):
        return None

    candidates = []

    # buybox.price.value / buybox_winner.price.value
    for key in ["buybox", "buybox_winner"]:
        bb = product.get(key)
        if isinstance(bb, dict):
            p = bb.get("price")
            candidates.append(p)
            if isinstance(p, dict):
                candidates.append(p.get("value"))
                candidates.append(p.get("raw"))

    # product.price.*
    p2 = product.get("price")
    candidates.append(p2)
    if isinstance(p2, dict):
        candidates.append(p2.get("value"))
        candidates.append(p2.get("raw"))

    # offers[0].price.*
    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            op = first.get("price")
            candidates.append(op)
            if isinstance(op, dict):
                candidates.append(op.get("value"))
                candidates.append(op.get("raw"))

    # string-y fallbacks
    for k in ["price_string", "current_price", "displayed_price", "main_price"]:
        if k in product:
            candidates.append(product.get(k))

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


def extract_discount_percent(product: dict, current_price: float | None):
    """
    Discount%:
    1) If original price is present, compute (orig - current)/orig
    2) Else try known percent fields
    3) Else 0
    """
    if not isinstance(product, dict):
        return 0

    # Try to find original/was price in common places
    original_candidates = []

    p = product.get("price")
    if isinstance(p, dict):
        for k in [
            "original_value",
            "original",
            "original_price",
            "was_price",
            "before_price",
            "rrp",
            "list_price",
        ]:
            if k in p:
                original_candidates.append(p.get(k))
        # sometimes nested
        for k in ["original_price", "list_price", "rrp"]:
            v = p.get(k)
            if isinstance(v, dict):
                original_candidates.append(v.get("value"))
                original_candidates.append(v.get("raw"))

        # percent fields
        for k in ["savings_percent", "savings_percentage", "discount_percent", "discount_percentage"]:
            if k in p:
                perc = p.get(k)
                if isinstance(perc, (int, float)):
                    return _format_percent(perc)
                if isinstance(perc, str):
                    pp = _parse_percent_from_string(perc)
                    if pp is not None:
                        return _format_percent(pp)

    # some payloads have top-level percent
    for k in ["discount_percent", "discount_percentage", "savings_percent", "savings_percentage"]:
        if k in product:
            perc = product.get(k)
            if isinstance(perc, (int, float)):
                return _format_percent(perc)
            if isinstance(perc, str):
                pp = _parse_percent_from_string(perc)
                if pp is not None:
                    return _format_percent(pp)

    # compute from original/current if possible
    orig_val = None
    for c in original_candidates:
        if c is None:
            continue
        if isinstance(c, (int, float)):
            orig_val = float(c)
            break
        if isinstance(c, dict):
            vv = c.get("value")
            if isinstance(vv, (int, float)):
                orig_val = float(vv)
                break
            raw = c.get("raw")
            if isinstance(raw, str):
                n = _parse_number_from_string(raw)
                if n is not None:
                    orig_val = n
                    break
        if isinstance(c, str):
            n = _parse_number_from_string(c)
            if n is not None:
                orig_val = n
                break

    if orig_val and current_price and orig_val > 0 and current_price <= orig_val:
        return _format_percent((orig_val - current_price) / orig_val * 100)

    return 0


def _walk_find_voucher_percent(obj):
    """
    Walk dict/list and look for coupon/voucher keys, return first percent found.
    """
    if obj is None:
        return None

    if isinstance(obj, dict):
        # direct percent fields
        key_str = " ".join([str(k).lower() for k in obj.keys()])

        # If this dict looks related to coupon/voucher, search inside values first
        looks_like_coupon = any(k in key_str for k in ["coupon", "voucher", "promo", "promotion", "deal"])

        # common keys
        for k in ["percent", "percentage", "discount_percent", "discount_percentage", "savings_percent"]:
            if k in obj:
                v = obj.get(k)
                if isinstance(v, (int, float)):
                    return _format_percent(v)
                if isinstance(v, str):
                    p = _parse_percent_from_string(v)
                    if p is not None:
                        return _format_percent(p)

        # sometimes a text field
        for k in ["text", "label", "title", "message", "description", "raw"]:
            if k in obj and isinstance(obj.get(k), str):
                s = obj.get(k)
                if looks_like_coupon or any(w in s.lower() for w in ["voucher", "coupon", "off"]):
                    p = _parse_percent_from_string(s)
                    if p is not None:
                        return _format_percent(p)

        # recurse all values
        for v in obj.values():
            found = _walk_find_voucher_percent(v)
            if found is not None:
                return found

    if isinstance(obj, list):
        for it in obj:
            found = _walk_find_voucher_percent(it)
            if found is not None:
                return found

    if isinstance(obj, str):
        # if plain text contains coupon/voucher
        s = obj.lower()
        if any(w in s for w in ["voucher", "coupon", "off"]):
            p = _parse_percent_from_string(obj)
            if p is not None:
                return _format_percent(p)

    return None


def extract_voucher_percent(product: dict):
    """
    Voucher% from typical Rainforest fields (coupons/promotions/etc).
    Returns 0 if none.
    """
    if not isinstance(product, dict):
        return 0

    # prioritize common fields
    for k in ["coupon", "coupons", "promotions", "promotion", "deals", "deal"]:
        if k in product:
            p = _walk_find_voucher_percent(product.get(k))
            if p is not None:
                return p

    # fallback: try entire product (lightly) – still safe because it short-circuits early
    p = _walk_find_voucher_percent(product)
    return p if p is not None else 0


def extract_size_thickness(product: dict):
    """
    Returns (size_str, thickness_str)
    - Size: prefer "size name"/"size"/"rug size" specs (NOT item dimensions metres)
    - Thickness: from "thickness/pile height" or 3rd dimension (x...xTHICK)
    """
    if not isinstance(product, dict):
        return (None, None)

    specs = product.get("specifications", [])
    size_candidates = []
    thickness_candidates = []

    if isinstance(specs, list):
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            name = (sp.get("name") or "").strip().lower()
            val = sp.get("value")
            if val is None:
                continue
            v = str(val).strip()

            # SIZE: prefer these labels
            if any(k in name for k in ["size name", "size", "rug size", "dimensions"]):
                # EXCLUDE item dimensions / metres formatted ones
                if "item dimensions" in name:
                    continue
                # if it looks like "2.2L x 1.6W metres" skip
                if re.search(r"\bmetres\b|\bmeters\b", v.lower()):
                    continue
                size_candidates.append(v)

            # THICKNESS
            if any(k in name for k in ["thickness", "pile height", "pileheight", "height"]):
                # avoid generic height
                if "package" in name:
                    continue
                thickness_candidates.append(v)

    # try variants/attributes for size if not found
    if not size_candidates:
        variants = product.get("variants")
        if isinstance(variants, dict):
            selected = variants.get("selected")
            if isinstance(selected, dict):
                for key in ["size", "size_name", "dimensions", "name", "value"]:
                    if selected.get(key):
                        size_candidates.append(str(selected.get(key)))

        attrs = product.get("attributes")
        if isinstance(attrs, dict):
            for key in ["size", "size_name", "dimensions"]:
                if attrs.get(key):
                    size_candidates.append(str(attrs.get(key)))

    raw_size = None
    if size_candidates:
        raw_size = size_candidates[0]

    # Parse size into "122x170cm" style (keep first 2 dims)
    size_out = None
    if raw_size:
        s = _normalize_mult_sign(raw_size).lower()
        # take before ';' to remove weight
        s = s.split(";")[0].strip()
        # remove spaces around x
        s = s.replace(" ", "")

        # patterns like "160x220cm" or "160x220cm;..."
        m = re.search(r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)(cm|mm|m)?", s)
        if not m:
            # pattern "160x220" without unit
            m = re.search(r"(\d{2,4})x(\d{2,4})", s)

        if m:
            a = m.group(1).replace(",", ".")
            b = m.group(2).replace(",", ".")
            unit = m.group(3) if len(m.groups()) >= 3 else None
            if not unit:
                unit = "cm"

            af = _to_float(a)
            bf = _to_float(b)

            if af is not None and bf is not None:
                # convert to cm if needed
                if unit == "m":
                    af *= 100
                    bf *= 100
                    unit = "cm"
                elif unit == "mm":
                    af /= 10
                    bf /= 10
                    unit = "cm"

                def fmt(x):
                    if abs(x - int(x)) < 1e-9:
                        return str(int(x))
                    return str(x).rstrip("0").rstrip(".")

                size_out = f"{fmt(af)}x{fmt(bf)}{unit}"
            else:
                size_out = f"{m.group(1)}x{m.group(2)}{unit}"
        else:
            size_out = raw_size  # fallback

    # thickness: try explicit thickness specs first
    thickness_out = None
    if thickness_candidates:
        t = thickness_candidates[0].strip()
        # pick something like "0.5 cm"
        mt = re.search(r"(\d+(?:[.,]\d+)?)\s*(mm|cm|m)\b", t.lower())
        if mt:
            val = mt.group(1).replace(",", ".")
            unit = mt.group(2)
            tv = _to_float(val)
            if tv is not None:
                if unit == "m":
                    tv *= 100
                    unit = "cm"
                elif unit == "mm":
                    tv /= 10
                    unit = "cm"
                thickness_out = f"{str(tv).rstrip('0').rstrip('.')}{unit}"
            else:
                thickness_out = f"{mt.group(1)}{unit}"

    # if thickness still empty, try from raw_size if it includes 3rd dimension: "170x122x0.5cm"
    if not thickness_out and raw_size:
        s = _normalize_mult_sign(raw_size).lower().replace(" ", "")
        m3 = re.search(r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)x(\d+(?:[.,]\d+)?)(cm|mm|m)?", s)
        if m3:
            tval = _to_float(m3.group(3).replace(",", "."))
            unit = m3.group(4) or "cm"
            if tval is not None:
                if unit == "m":
                    tval *= 100
                    unit = "cm"
                elif unit == "mm":
                    tval /= 10
                    unit = "cm"
                thickness_out = f"{str(tval).rstrip('0').rstrip('.')}{unit}"

    return (size_out, thickness_out)


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
                "Price": None,
                "% Discount": 0,
                "% Voucher": 0,
                "Final price": None,
                "Date (UTC)": datetime.now(timezone.utc).date().isoformat(),  # date only
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

                    size_out, thickness_out = extract_size_thickness(product)
                    row["Size"] = size_out
                    row["Thickness"] = thickness_out

                    price = extract_price_value(product)
                    row["Price"] = price

                    disc = extract_discount_percent(product, price)
                    row["% Discount"] = _format_percent(disc)

                    voucher = extract_voucher_percent(product)
                    row["% Voucher"] = _format_percent(voucher)

                    if price is not None:
                        row["Final price"] = round(price * (1 - (row["% Voucher"] / 100.0)), 2)
                    else:
                        row["Final price"] = None

            except Exception:
                # hata olursa bile uygulama çökmeyecek; satır boş kalır
                pass

            results.append(row)

        progress.progress(i / total)
        time.sleep(0.25)

    df = pd.DataFrame(results)

    # Ensure column order exactly as requested
    desired_cols = ["Brand", "ASIN", "Size", "Thickness", "Price", "% Discount", "% Voucher", "Final price", "Date (UTC)"]
    for c in desired_cols:
        if c not in df.columns:
            df[c] = None
    df = df[desired_cols]

    st.subheader("Results")
    st.dataframe(df, use_container_width=True)

    # Excel export
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
