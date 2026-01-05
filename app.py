import os
import time
import csv
from io import BytesIO
from datetime import datetime, timezone

import requests
import streamlit as st
import pandas as pd
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

st.set_page_config(page_title="Price Tracking", layout="wide")

try:
    API_KEY = st.secrets["RAINFOREST_API_KEY"]
except Exception:
    API_KEY = os.environ.get("RAINFOREST_API_KEY")



st.title("Price Tracking")
st.write("Click **Fetch Prices** to pull current price from Amazon (Rainforest API).")

# ---- Optional mapping from ASINs.csv (ASIN, Design, Size) ----
mapping = {}
mapping_path = "ASINs.csv"

def load_mapping(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            asin = (row.get("ASIN") or "").strip().upper()
            if not asin:
                continue
            out[asin] = {
                "Design": (row.get("Design") or "").strip() or None,
                "Size": (row.get("Size") or "").strip() or None,
            }
    return out

try:
    mapping = load_mapping(mapping_path)
    if mapping:
        st.info(f"Loaded mapping from {mapping_path} ({len(mapping)} rows).")
except Exception as e:
    st.warning(f"Could not read {mapping_path}: {e}")
    mapping = {}

# ---- ASIN input ----
asins_text = st.text_area(
    "ASINs (one per line)",
    height=150,
    value="B0D7J69H1L\nB0D7J7MV6G\nB0D7J71G7J",
)
ASINS = [a.strip().upper() for a in asins_text.splitlines() if a.strip()]

# ---- Helpers ----
RAINFOREST_URL = "https://api.rainforestapi.com/request"

def rf_get_product(asin: str, amazon_domain: str, api_key: str) -> dict:
    params = {
        "api_key": api_key,
        "type": "product",
        "amazon_domain": amazon_domain,
        "asin": asin,
    }
    r = requests.get(RAINFOREST_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def pick_price(product: dict):
    availability = product.get("availability")

    buybox_seller = None
    if isinstance(product.get("buybox"), dict):
        seller = product["buybox"].get("seller")
        if isinstance(seller, dict):
            buybox_seller = seller.get("name")

    price = None
    currency = None
    p = product.get("price")

    if isinstance(p, dict):
        price = p.get("value")
        currency = p.get("currency")
        if price is None and isinstance(p.get("raw"), str):
            raw = p["raw"]
            cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in [".", ","])
            cleaned = cleaned.replace(",", ".")
            try:
                price = float(cleaned) if cleaned else None
            except:
                price = None

    if price is None and isinstance(product.get("buybox"), dict):
        bp = product["buybox"].get("price")
        if isinstance(bp, dict):
            price = bp.get("value")
            currency = currency or bp.get("currency")

    try:
        price = float(price) if price is not None else None
    except:
        price = None

    return price, currency, availability, buybox_seller

def autosize_columns(ws):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val = "" if cell.value is None else str(cell.value)
            max_len = max(max_len, len(val))
        ws.column_dimensions[col_letter].width = min(max_len + 2, 50)

def build_excel(details_rows: list, summary_rows: list) -> bytes:
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Details"

    headers = list(details_rows[0].keys()) if details_rows else ["ASIN"]
    ws1.append(headers)
    for row in details_rows:
        ws1.append([row.get(h) for h in headers])
    autosize_columns(ws1)

    ws2 = wb.create_sheet("Summary")
    ws2.append(["Metric", "Value"])
    for m, v in summary_rows:
        ws2.append([m, v])
    autosize_columns(ws2)

    out = BytesIO()
    wb.save(out)
    return out.getvalue()

# ---- Main button ----
if st.button("Fetch Prices"):
    if not API_KEY:
        st.error("API key not found. Please set RAINFOREST_API_KEY in Streamlit Secrets.")
        st.stop()

    if not ASINS:
        st.warning("Please enter at least one ASIN.")
        st.stop()

    progress = st.progress(0)
    results = []
    credits_used = None
    credits_remaining = None

    for i, asin in enumerate(ASINS, start=1):
        with st.spinner(f"Fetching {asin} ({i}/{len(ASINS)})"):
            row = {
                "ASIN": asin,
                "Design": mapping.get(asin, {}).get("Design") if mapping else None,
                "Size": mapping.get(asin, {}).get("Size") if mapping else None,
                "Price": None,
                "Currency": None,
                "Availability": None,
                "Buybox Seller": None,
                "Fetched At (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "Error": None,
            }

            try:
                data = rf_get_product(asin, MARKETPLACE, API_KEY)
                info = data.get("request_info", {}) or {}
                credits_used = info.get("credits_used", credits_used)
                credits_remaining = info.get("credits_remaining", credits_remaining)

                product = data.get("product", {}) or {}
                price, currency, availability, buybox_seller = pick_price(product)

                row["Price"] = price
                row["Currency"] = currency
                row["Availability"] = availability
                row["Buybox Seller"] = buybox_seller

            except Exception as e:
                row["Error"] = str(e)

            results.append(row)

        progress.progress(i / len(ASINS))
        time.sleep(0.2)

    st.subheader("Results")
    df = pd.DataFrame(results)
    st.dataframe(df, use_container_width=True)

    prices = [r["Price"] for r in results if isinstance(r.get("Price"), (int, float))]
    summary_rows = [
        ("ASIN count", len(results)),
        ("Avg price", round(sum(prices) / len(prices), 2) if prices else None),
        ("Min price", min(prices) if prices else None),
        ("Max price", max(prices) if prices else None),
        ("Amazon domain", MARKETPLACE),
        ("Fetched at (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
    ]

    xlsx_bytes = build_excel(results, summary_rows)
    st.download_button(
        "⬇️ Download Price Report (XLSX)",
        data=xlsx_bytes,
        file_name="price_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if credits_remaining is not None:
        st.info(f"Credits used (last call): {credits_used}; Credits remaining (approx): {credits_remaining}")
