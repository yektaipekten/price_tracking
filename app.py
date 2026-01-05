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

# ---------------- CONFIG ----------------
st.set_page_config(page_title="Price Tracking", layout="wide")

try:
    API_KEY = st.secrets["RAINFOREST_API_KEY"]
except Exception:
    API_KEY = os.environ.get("RAINFOREST_API_KEY")

MARKETPLACE = "amazon.co.uk"
RAINFOREST_URL = "https://api.rainforestapi.com/request"
# ----------------------------------------

st.title("Price Tracking")
st.write("Click **Fetch Prices** to pull current price from Amazon (Rainforest API).")

# ---------- ASIN MAPPING ----------
def load_mapping(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            asin = (r.get("ASIN") or "").strip().upper()
            if asin:
                out[asin] = {
                    "Design": r.get("Design"),
                    "Size": r.get("Size"),
                }
    return out

mapping = load_mapping("ASINs.csv")

# ---------- INPUT ----------
asins_text = st.text_area(
    "ASINs (one per line)",
    height=120,
    value="B0D7J69H1L",
)
ASINS = [a.strip().upper() for a in asins_text.splitlines() if a.strip()]

# ---------- HELPERS ----------
def rf_get_product(asin: str):
    params = {
        "api_key": API_KEY,
        "type": "product",
        "amazon_domain": MARKETPLACE,
        "asin": asin,
    }
    r = requests.get(RAINFOREST_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def pick_price(product: dict):
    price = None
    currency = None
    availability = product.get("availability")

    if isinstance(product.get("price"), dict):
        price = product["price"].get("value")
        currency = product["price"].get("currency")

    seller = None
    if isinstance(product.get("buybox"), dict):
        seller = product["buybox"].get("seller", {}).get("name")

    return price, currency, availability, seller

# ---------- MAIN ----------
if st.button("Fetch Prices"):
    if not API_KEY:
        st.error("API key not found.")
        st.stop()

    rows = []
    for asin in ASINS:
        row = {
            "ASIN": asin,
            "Design": mapping.get(asin, {}).get("Design"),
            "Size": mapping.get(asin, {}).get("Size"),
            "Price": None,
            "Currency": None,
            "Availability": None,
            "Buybox Seller": None,
            "Fetched At (UTC)": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "Error": None,
        }

        try:
            data = rf_get_product(asin)
            product = data.get("product", {})
            price, currency, availability, seller = pick_price(product)

            row["Price"] = price
            row["Currency"] = currency
            row["Availability"] = availability
            row["Buybox Seller"] = seller

        except Exception as e:
            row["Error"] = str(e)

        rows.append(row)
        time.sleep(0.2)

    df = pd.DataFrame(rows)
    st.subheader("Results")
    st.dataframe(df, use_container_width=True)
