# app.py
import os
import re
import time
from io import BytesIO
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
import base64


# ----------------- Page config -----------------
st.set_page_config(page_title="Competitors Pricing strategy", layout="wide")

# ----------------- THEME (edit here) -----------------
TITLE_COLOR = "#111111"

# Put your logo here (optional)
LOGO_PATH = "assets/logo.png"  # create assets/ and place logo.png inside

# ----------------- CSS -----------------
# ----------------- CSS -----------------
def file_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

bg_b64 = file_to_base64("image.png")

st.markdown(
    f"""
    <style>
      .stApp {{
        background-image: url("data:image/png;base64,{bg_b64}");
        background-size: cover;
        background-position: center;
        background-repeat: no-repeat;
        background-attachment: fixed;
      }}

      /* Make content boxes readable on image */
      .block-container {{
        background: rgba(255, 255, 255, 0.92);
        border-radius: 18px;
        padding: 24px;
        margin-top: 20px;
      }}

      .page-title {{
        font-size: 44px;
        font-weight: 800;
        color: #111111;
        margin: 0 0 6px 0;
      }}
      .page-subtitle {{
        font-size: 14px;
        color: #333;
        margin: 0 0 18px 0;
      }}

      .credits-box {{
        background: #ffffff;
        color: #000000;
        border: 1px solid #e6e6e6;
        border-radius: 10px;
        padding: 12px 14px;
        font-size: 14px;
        line-height: 1.4;
      }}

      /* Title row layout */
      .title-row {{
        display: flex;
        align-items: center;
        gap: 14px;
        margin-bottom: 6px;
      }}
      .title-logo {{
        height: 46px;
      }}
    </style>
    """,
    unsafe_allow_html=True
)



# ----------------- Header -----------------
st.markdown('<div class="page-title">Competitors Pricing strategy</div>', unsafe_allow_html=True)
st.markdown('<div class="page-subtitle">Check competitor prices from Amazon (Rainforest API).</div>', unsafe_allow_html=True)

# ----------------- Config -----------------
API_KEY = st.secrets.get("RAINFOREST_API_KEY") or os.environ.get("RAINFOREST_API_KEY")
MARKETPLACE = "amazon.co.uk"

# ----------------- Logo bottom-right (optional) -----------------
if os.path.exists(LOGO_PATH):
    with open(LOGO_PATH, "rb") as f:
        import base64
        b64 = base64.b64encode(f.read()).decode("utf-8")
    st.markdown(
        f"""<img class="logo-fixed" src="data:image/png;base64,{b64}" />""",
        unsafe_allow_html=True
    )

# ----------------- Inputs -----------------
asins_text = st.text_area("ASINs (one per line)", height=160, value="B0D7J69H1L")
ASINS = [a.strip().upper() for a in asins_text.splitlines() if a.strip()]

# ----------------- Helpers -----------------
def to_float(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        m = re.search(r"(\d+[.,]\d+|\d+)", x)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", "."))
        except Exception:
            return None
    if isinstance(x, dict):
        for k in ["value", "amount", "raw", "price", "final_price"]:
            if k in x:
                f = to_float(x.get(k))
                if f is not None:
                    return f
    return None


def parse_size(text: str):
    if not isinstance(text, str):
        return None
    s = text.lower().replace("×", "x").replace("*", "x")
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*(cm|mm|m|metre|metres)\b", s)
    if not m:
        s2 = s.replace(" ", "")
        m = re.search(r"(\d{2,4}(?:[.,]\d+)?)x(\d{2,4}(?:[.,]\d+)?)\s*(cm|mm|m)\b", s2)
    if not m:
        return None

    a = float(m.group(1).replace(",", "."))
    b = float(m.group(2).replace(",", "."))
    unit = m.group(3)

    if unit in ["m", "metre", "metres"]:
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


def extract_brand(product: dict):
    b = product.get("brand")
    if isinstance(b, str) and b.strip():
        return b.strip()

    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for sp in specs:
            if isinstance(sp, dict) and (sp.get("name") or "").strip().lower() == "brand":
                v = sp.get("value")
                if v:
                    return str(v).strip()
    return None


def extract_size(product: dict):
    specs = product.get("specifications", [])
    if isinstance(specs, list):
        for sp in specs:
            if not isinstance(sp, dict):
                continue
            group = (sp.get("group_name") or sp.get("group") or sp.get("section") or sp.get("category") or "")
            if str(group).strip().lower() == "measurements" and str(sp.get("name") or "").strip().lower() == "size":
                v = sp.get("value")
                out = parse_size(str(v))
                if out:
                    return out

        for sp in specs:
            if not isinstance(sp, dict):
                continue
            if str(sp.get("name") or "").strip().lower() == "size":
                v = sp.get("value")
                out = parse_size(str(v))
                if out:
                    return out

    title = product.get("title")
    out = parse_size(title) if isinstance(title, str) else None
    return out


def extract_base_price(product: dict):
    buybox = product.get("buybox")
    if isinstance(buybox, dict):
        f = to_float(buybox.get("price"))
        if f and f > 0:
            return f

    bbw = product.get("buybox_winner")
    if isinstance(bbw, dict):
        f = to_float(bbw.get("price"))
        if f and f > 0:
            return f

    f = to_float(product.get("price"))
    if f and f > 0:
        return f

    offers = product.get("offers")
    if isinstance(offers, list) and offers:
        first = offers[0]
        if isinstance(first, dict):
            f = to_float(first.get("price"))
            if f and f > 0:
                return f

    return None


# ----------------- Main -----------------
if st.button("Check Prices"):
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
        with st.spinner(f"Checking {asin} ({i}/{total})"):
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
                    row["Final price"] = extract_base_price(product)

            except Exception:
                pass

            results.append(row)

        progress.progress(i / total)
        time.sleep(0.2)

    df = pd.DataFrame(results, columns=["Brand", "ASIN", "Size", "Final price", "Date (UTC)"])

    st.subheader("Results")
    st.dataframe(df, use_container_width=True)

    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Prices")
    out.seek(0)

    st.download_button(
        "⬇️ Download Competitors Pricing Report",
        data=out.getvalue(),
        file_name="competitors_pricing_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if credits_used is not None or credits_remaining is not None:
        used_txt = "N/A" if credits_used is None else str(credits_used)
        rem_txt = "N/A" if credits_remaining is None else str(credits_remaining)
        st.markdown(
            f"""
            <div class="credits-box">
              <b>Used Credit:</b> {used_txt} &nbsp;&nbsp;|&nbsp;&nbsp; <b>Credits Remaining:</b> {rem_txt}
            </div>
            """,
            unsafe_allow_html=True
        )
