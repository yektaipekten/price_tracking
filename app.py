import os
import re
import time
import csv
from io import BytesIO
from datetime import datetime, timezone

import requests
import streamlit as st
import pandas as pd

# ---------------- CONFIG ----------------
st.set_page_config(page_title="Price Tracking", layout="wide")

RAINFOREST_URL = "https://api.rainforestapi.com/request"
MARKETPLACE = "amazon.co.uk"  # UK

# Secrets (Streamlit Cloud) > fallback env
API_KEY = None
try:
    API_KEY = st.secrets["RAINFOREST_API_KEY"]
except Exception:
    API_KEY = os.environ.get("RAINFOREST_API_KEY")

# ---------------- UI ----------------
st.title("Price Tracking")
st.write("Click **Fetch Prices** to pull current price from Amazon (Rainforest API).")

# ---------------- OPTIONAL ASIN MAPPING ----------------
def load_mapping(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, "r", encoding="utf-8-sig") as f:
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

mapping = load_mapping("ASINs.csv")

# ---------------- HELPERS ----------------
def rf_get_product(asin: str) -> dict:
    params = {
        "api_key": API_KEY,
        "type": "product",
        "amazon_domain": MARKETPLACE,
        "asin": asin,
    }
    r = requests.get(RAINFOREST_URL, params=params, timeout=45)
    r.raise_for_status()
    return r.json()

def get_credits(payload: dict) -> tuple:
    info = payload.get("request_info") or {}
    return info.get("credits_used"), info.get("credits_remaining")

def extract_brand(product: dict) -> str | None:
    # Rainforest commonly returns brand in product["brand"]
    for k in ("brand", "manufacturer"):
        v = product.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Sometimes inside "specifications"
    specs = product.get("specifications")
    if isinstance(specs, list):
        for s in specs:
            if not isinstance(s, dict):
                continue
            name = (s.get("name") or "").strip().lower()
            val = (s.get("value") or "").strip()
            if name in ("brand", "manufacturer") and val:
                return val
    return None

def extract_size_from_title(title: str | None) -> str | None:
    if not title:
        return None
    t = title.lower()

    # Basic patterns for rugs etc: "120x170cm", "120 x 170 cm", "4x6 ft"
    m = re.search(r"\b(\d{2,3})\s*[x×]\s*(\d{2,3})\s*(cm)\b", t)
    if m:
        return f"{m.group(1)}x{m.group(2)}{m.group(3)}"
    m = re.search(r"\b(\d{1,2})\s*[x×]\s*(\d{1,2})\s*(ft)\b", t)
    if m:
        return f"{m.group(1)}x{m.group(2)} {m.group(3)}"
    return None

def extract_price(product: dict):
    price = None
    currency = None
    availability = product.get("availability")
    buybox_seller = None

    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        p = buybox.get("price")
        if isinstance(p, dict):
            price = p.get("value")
            currency = p.get("currency")
        seller = buybox.get("seller")
        if isinstance(seller, dict):
            buybox_seller = seller.get("name")

    # fallback
    p2 = product.get("price")
    if isinstance(p2, dict):
        price = p2.get("value", price)
        currency = p2.get("currency", currency)

    return price, currency, availability, buybox_seller

def to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Prices")
    return output.getvalue()

# ---------------- INPUT ----------------
asins_text = st.text_area("ASINs (one per line)", height=140, value="B0D7J69H1L")
ASINS = [a.strip().upper() for a in asins_text.splitlines() if a.strip()]

# ---------------- MAIN ----------------
if st.button("Fetch Prices"):
    if not API_KEY:
        st.error("API key not found. Please set RAINFOREST_API_KEY in Streamlit Secrets.")
        st.stop()

    results = []
    credits_used_total = 0
    credits_remaining_last = None

    progress = st.progress(0)

    for i, asin in enumerate(ASINS, start=1):
        row = {
            "ASIN": asin,
            "Brand": None,
            "Design": mapping.get(asin, {}).get("Design"),
            "Size": mapping.get(asin, {}).get("Size"),  # priority: ASINs.csv
            "Price": None,
            "Currency": None,
            "Availability": None,
            "Buybox Seller": None,
            "Fetched At (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "Error": None,
        }

        try:
            payload = rf_get_product(asin)

            cu, cr = get_credits(payload)
            if isinstance(cu, int):
                credits_used_total += cu
            credits_remaining_last = cr

            product = payload.get("product") or {}

            row["Brand"] = extract_brand(product)

            title = product.get("title")
            if not row["Size"]:
                row["Size"] = extract_size_from_title(title)

            price, currency, availability, seller = extract_price(product)
            row["Price"] = price
            row["Currency"] = currency
            row["Availability"] = availability
            row["Buybox Seller"] = seller

        except Exception as e:
            row["Error"] = str(e)

        results.append(row)
        progress.progress(i / max(len(ASINS), 1))
        time.sleep(0.15)

    df = pd.DataFrame(results)

    # Credits box (like your reference app)
    st.info(
        f"Credits used in last run: {credits_used_total}; "
        f"Credits remaining (approx): {credits_remaining_last}"
    )

    st.subheader("Results")
    st.dataframe(df, use_container_width=True)

    xlsx_bytes = to_xlsx_bytes(df)
    st.download_button(
        "⬇️ Download Price Report (XLSX)",
        data=xlsx_bytes,
        file_name="price_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
