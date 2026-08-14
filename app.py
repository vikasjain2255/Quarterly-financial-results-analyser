import re
import json
import urllib.request
import streamlit as st
import pandas as pd
import fitz

st.set_page_config(page_title="Quarterly Results Analyser V4.1", page_icon="📊", layout="wide")
st.title("📊 Quarterly Results Analyser V4.1")
st.caption("Paste a direct PDF link or upload a quarterly-results PDF.")

# -----------------------------
# Patterns / helpers
# -----------------------------
NUM_RE = re.compile(
    r"(?<![\w.])(?P<n>\(?-?\d[\d,]*(?:\.\d+)?\)?)(?![\w.])"
)

ALIASES = {
    "revenue": [
        r"revenue from operations",
        r"net sales",
        r"total sales",
        r"sales",
        r"turnover",
        r"total revenue",
    ],
    "pat": [
        r"net profit\s*/?\s*\(loss\)\s*for the period",
        r"profit\s*/?\s*\(loss\)\s*for the period",
        r"profit attributable to.*owners",
        r"profit after tax",
        r"net profit after tax",
        r"profit for the period",
        r"net profit",
    ],
    "pbt": [
        r"profit\s*/?\s*\(loss\)\s*before exceptional",
        r"profit before tax",
        r"profit before taxation",
        r"\bpbt\b",
    ],
    "finance_cost": [
        r"finance costs?",
        r"interest and finance costs?",
        r"interest costs?",
    ],
    "depreciation": [
        r"depreciation and amortisation",
        r"depreciation & amortisation",
        r"depreciation",
    ],
    "other_income": [
        r"other income",
    ],
    "ebitda": [
        r"\bebitda\b",
        r"earnings before interest.*tax.*depreciation.*amortisation",
    ],
}

def num(s):
    s = str(s).strip().replace(",", "").replace(" ", "")
    if s in ("", "-", "—", "–", "na", "n/a"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    try:
        x = float(s.strip("()"))
        return -x if neg else x
    except Exception:
        return None

def nums(t):
    return [num(m.group("n")) for m in NUM_RE.finditer(t) if num(m.group("n")) is not None]

def pct(a, b):
    return None if a is None or b in (None, 0) else (a / b - 1) * 100

def fp(x, d=0):
    return "NA" if x is None else f"{x:.{d}f}%"

def fc(x):
    if x is None:
        return "NA"
    return f"{x:,.0f}" if abs(x - round(x)) < 0.05 else f"{x:,.1f}"

def get_pdf(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36",
            "Accept": "application/pdf,*/*",
            "Referer": "https://www.google.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        raise ValueError("URL did not return a PDF. Please use a direct PDF link.")
    return data

def pdf_text(data, ocr=False):
    doc = fitz.open(stream=data, filetype="pdf")
    out, o = [], 0
    for p in doc:
        t = p.get_text("text", sort=True)
        if ocr and len(t.strip()) < 40:
            try:
                t = p.get_text("text", textpage=p.get_textpage_ocr(), sort=True)
                o += 1
            except Exception:
                pass
        out.append(t)
    doc.close()
    return "\n\f\n".join(out), o

def tables_text(data):
    """Extract table rows when PyMuPDF table detection is available."""
    out = []
    doc = fitz.open(stream=data, filetype="pdf")
    for p in doc:
        try:
            finder = p.find_tables()
            for tab in finder.tables:
                for row in tab.extract():
                    out.append(" | ".join("" if x is None else str(x) for x in row))
        except Exception:
            pass
    doc.close()
    return "\n".join(out)

def company(t):
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    for x in lines[:100]:
        if 5 <= len(x) <= 140 and not any(
            k in x.lower()
            for k in [
                "bse", "nse", "statement", "unaudited", "audited",
                "financial results", "registered office", "quarter ended", "cin"
            ]
        ):
            if re.search(
                r"\b(limited|ltd\.?|industries|bank|finance|foods|motors|pharma|steel|cement)\b",
                x, re.I
            ):
                return x
    return lines[0] if lines else "COMPANY"

def unit(t):
    l = t.lower()
    # Financial-result PDFs often state "Rs. in Lakhs" / "Rs. in Millions".
    if re.search(r"(?:amounts?|figures?).{0,20}\bmillions?\b", l):
        return 0.1, "₹ crore (converted from ₹ million)"
    if re.search(r"(?:amounts?|figures?).{0,20}\blakhs?\b", l):
        return 0.01, "₹ crore (converted from ₹ lakh)"
    return 1.0, "₹ crore (assumed)"

def results_basis(text):
    l = text.lower()
    cons = (
        l.count("consolidated financial results")
        + l.count("consolidated statement")
        + l.count("consolidated financial statements")
    )
    stand = (
        l.count("standalone financial results")
        + l.count("standalone statement")
        + l.count("standalone financial statements")
    )
    if cons > stand and cons > 0:
        return "CONSOLIDATED"
    if stand > 0:
        return "STANDALONE"
    return "STANDALONE"

def order(text):
    """Determine whether the first 3 numeric columns are Current, QoQ, YoY."""
    l = text.lower()
    y = min(
        [l.find(x) for x in [
            "corresponding previous year",
            "corresponding quarter",
            "previous year quarter",
            "30-jun-25",
            "30-jun-2025"
        ] if l.find(x) >= 0],
        default=10**9,
    )
    p = min(
        [l.find(x) for x in [
            "previous quarter",
            "preceding quarter",
            "31-mar-26",
            "31-mar-2026"
        ] if l.find(x) >= 0],
        default=10**9,
    )
    return "current_yoy_prev" if y < p else "current_prev_yoy"

def find_metric(t, metric_name):
    """
    Return up to the first four relevant numbers on the best matching row/window.
    The fourth number is normally the full-year column and is deliberately ignored
    for QoQ/YoY analysis.
    """
    lines = t.splitlines()
    best = None

    for i, line in enumerate(lines):
        score = sum(10 for a in ALIASES[metric_name] if re.search(a, line, re.I))
        if score <= 0:
            continue

        # Prefer the actual row. If a PDF splits a row across lines, allow a
        # short window, but don't mix in too many unrelated rows.
        for span in (1, 2, 3):
            w = " ".join(lines[i:i + span])
            vals = nums(w)
            if len(vals) >= 3:
                candidate = (score - span, vals[:4], w)
                if best is None or candidate[0] > best[0]:
                    best = candidate
                break

    if best is None:
        return None

    vals = best[1]
    return vals, best[2], ("HIGH" if best[0] >= 9 else "MEDIUM" if best[0] >= 6 else "LOW")

def metric(name, raw, meta, factor, ordr):
    vals = raw.get(name)
    if not vals or len(vals) < 3:
        return {
            "name": name, "current": None, "previous": None, "yoy": None,
            "qoq": None, "yoypct": None, "confidence": "LOW", "source": ""
        }

    # IMPORTANT V4.1 FIX:
    # Financial-result tables commonly contain 4 columns:
    # Current quarter | Previous quarter | YoY quarter | Full year.
    # V4 crashed by trying to unpack all 4 into 3 variables.
    a, b, c = [x * factor for x in vals[:3]]

    if ordr == "current_yoy_prev":
        cur, yoy, prev = a, b, c
    else:
        cur, prev, yoy = a, b, c

    return {
        "name": name,
        "current": cur,
        "previous": prev,
        "yoy": yoy,
        "qoq": pct(cur, prev),
        "yoypct": pct(cur, yoy),
        "confidence": meta.get(name, ("", "LOW"))[1],
        "source": meta.get(name, ("", ""))[0],
    }

def derive_ebitda(raw):
    required = ["pbt", "finance_cost", "depreciation", "other_income"]
    if not all(k in raw and len(raw[k]) >= 3 for k in required):
        return None

    n = min(len(raw[k]) for k in required)
    return [
        raw["pbt"][i]
        + raw["finance_cost"][i]
        + raw["depreciation"][i]
        - raw["other_income"][i]
        for i in range(n)
    ]

def analyse(data, q, basis, ocr):
    text, o = pdf_text(data, ocr)
    try:
        table_text = tables_text(data)
    except Exception:
        table_text = ""

    combined = text + "\n" + table_text

    if basis == "AUTO":
        basis = results_basis(combined)

    factor, unitname = unit(combined)
    ordr = order(combined)

    raw, meta = {}, {}

    for m in ALIASES:
        z = find_metric(combined, m)
        if z:
            raw[m] = z[0]
            meta[m] = (z[1], z[2])

    # If EBITDA isn't explicitly reported, calculate:
    # EBITDA = PBT + Finance Cost + Depreciation - Other Income
    if "ebitda" not in raw:
        derived = derive_ebitda(raw)
        if derived is not None:
            raw["ebitda"] = derived
            meta["ebitda"] = (
                "Derived: PBT + Finance Cost + Depreciation - Other Income",
                "MEDIUM",
            )

    r = metric("revenue", raw, meta, factor, ordr)
    e = metric("ebitda", raw, meta, factor, ordr)
    p = metric("pat", raw, meta, factor, ordr)

    cm = e["current"] / r["current"] * 100 if (
        e["current"] is not None and r["current"] not in (None, 0)
    ) else None
    pm = e["previous"] / r["previous"] * 100 if (
        e["previous"] is not None and r["previous"] not in (None, 0)
    ) else None
    ym = e["yoy"] / r["yoy"] * 100 if (
        e["yoy"] is not None and r["yoy"] not in (None, 0)
    ) else None

    warnings = []
    for m in [r, e, p]:
        if m["current"] is None:
            warnings.append(m["name"].upper() + " could not be extracted.")
        elif m["qoq"] is None or m["yoypct"] is None:
            warnings.append(m["name"].upper() + " has insufficient comparison data.")
        elif m["confidence"] == "LOW":
            warnings.append(m["name"].upper() + " extraction confidence is LOW.")

    if o:
        warnings.append(f"OCR used on {o} page(s).")

    return {
        "company": company(text),
        "quarter": q,
        "basis": basis,
        "unit": unitname,
        "revenue": r,
        "ebitda": e,
        "pat": p,
        "cm": cm,
        "pm": pm,
        "ym": ym,
        "ybps": (cm - ym) * 100 if cm is not None and ym is not None else None,
        "qbps": (cm - pm) * 100 if cm is not None and pm is not None else None,
        "warnings": warnings,
        "meta": meta,
    }

def direction(x):
    if x is None:
        return "NA"
    return "UP" if x >= 0 else "DOWN"

def line(m, label):
    if m["current"] is None:
        return f"{label} NA"
    return (
        f"{label} {direction(m['yoypct'])} {fp(abs(m['yoypct']))} "
        f"AT ₹{fc(m['current'])} CR (YOY), "
        f"{direction(m['qoq'])} {fp(abs(m['qoq']))} (QOQ)"
    )

def compact(r):
    co = re.sub(
        r"\s+(PRIVATE LIMITED|PVT\.?\s*LTD\.?|LIMITED|LTD\.?)$",
        "",
        r["company"],
        flags=re.I,
    ).strip().upper()

    profit_label = "CONS NET PROFIT" if r["basis"] == "CONSOLIDATED" else "NET PROFIT"

    return "\n\n".join([
        f"{co} {r['quarter']} :",
        line(r["revenue"], "REVENUE"),
        line(r["ebitda"], "EBITDA"),
        f"MARGINS {fp(r['cm'],1)} V {fp(r['ym'],1)} (YOY), {fp(r['pm'],1)} (QOQ)",
        line(r["pat"], profit_label),
    ])

# -----------------------------
# UI
# -----------------------------
with st.sidebar:
    quarter = st.selectbox("Quarter", ["Q1", "Q2", "Q3", "Q4"])
    basis = st.radio("Results basis", ["AUTO", "CONSOLIDATED", "STANDALONE"])
    ocr = st.checkbox("Use OCR if needed")

url = st.text_input(
    "Direct PDF link",
    placeholder="https://www.bseindia.com/...pdf",
)
upload = st.file_uploader("Or upload PDF", type=["pdf"])

if st.button("ANALYSE", type="primary", use_container_width=True):
    try:
        if upload:
            data = upload.getvalue()
        elif url.strip():
            data = get_pdf(url.strip())
        else:
            st.error("Paste a direct PDF link or upload a PDF.")
            st.stop()

        with st.spinner("Analysing..."):
            result = analyse(data, quarter, basis, ocr)

        st.success("Analysis complete")

        st.subheader("Results")
        st.code(compact(result), language="text")

        rows = []
        for m in [result["revenue"], result["ebitda"], result["pat"]]:
            rows.append({
                "Metric": m["name"].upper(),
                "Current ₹cr": m["current"],
                "Previous Q ₹cr": m["previous"],
                "YoY Comparable ₹cr": m["yoy"],
                "QoQ %": m["qoq"],
                "YoY %": m["yoypct"],
                "Confidence": m["confidence"],
            })

        st.subheader("Calculation details")
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )

        st.write({
            "Current margin": result["cm"],
            "Previous Q margin": result["pm"],
            "YoY margin": result["ym"],
            "YoY bps": result["ybps"],
            "QoQ bps": result["qbps"],
        })

        if result["warnings"]:
            with st.expander("⚠ Review warnings", expanded=True):
                for w in result["warnings"]:
                    st.warning(w)

        with st.expander("🔎 Extraction diagnostics"):
            st.write("Company:", result["company"])
            st.write("Basis:", result["basis"])
            st.write("Unit:", result["unit"])
            for k, value in result["meta"].items():
                src, conf = value
                st.markdown(f"**{k.upper()} — {conf}**")
                st.code(src)

        st.download_button(
            "Download JSON",
            json.dumps(result, indent=2, default=str),
            file_name="results_analysis.json",
            mime="application/json",
        )

    except Exception as e:
        st.error(f"Analysis failed: {e}")
        st.info(
            "If this is a new PDF format, turn on 'Use OCR if needed' and retry. "
            "The app now avoids the V4 four-column unpacking crash."
        )
