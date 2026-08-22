
import io, json, re, urllib.request
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Tuple

import streamlit as st
import pandas as pd
import pymupdf


# ============================================================
# QUARTERLY FINANCIAL RESULTS ANALYSER — FINAL
# ============================================================
# Design principles:
# 1. Select the actual financial-results statement first.
# 2. Never mix standalone and consolidated pages.
# 3. Use the table's declared four-period order.
# 4. Extract only rows belonging to the selected statement.
# 5. Repair OCR conservatively and validate values before use.
# 6. Never crash because a metric is missing.
# ============================================================

st.set_page_config(
    page_title="Quarterly Financial Results Analyser",
    page_icon="📊",
    layout="wide",
)

NUM_TOKEN = re.compile(r"^[\(\[\{]?[0-9OoIlLSsBbGgZzQq,\.\-]+[\)\]\}]?$")
YEAR_RE = re.compile(r"(?:19|20)\d{2}")

SECTOR_PATTERNS = {
    "BANK": [r"\bbank\b", r"\bbanking\b", r"\bcommercial\s+bank\b", r"\bprivate\s+sector\s+bank\b", r"\bpublic\s+sector\s+bank\b"],
    "NBFC": [r"\bnon[\s-]?banking\s+financial\s+company\b", r"\bnon[\s-]?banking\s+finance\s+company\b", r"\bNBFC\b", r"\bhousing\s+finance\b"],
}

BANK_METRIC_PATTERNS = {
    "nii": [r"\bnet\s+interest\s+income\b", r"\bnet\s+interest\s+revenue\b"],
    "interest_earned": [r"\binterest\s+earned\b", r"\binterest\s+income\b"],
    "interest_paid": [r"\binterest\s+(?:paid|expended)\b", r"\binterest\s+expense\b"],
    "operating_profit": [r"\boperating\s+profit\b", r"\bpre[\s-]?provision\s+operating\s+profit\b", r"\bPPOP\b"],
    "provisions": [r"\bprovisions?\b", r"\bprovisions?\s+and\s+contingencies\b"],
    "net_profit_before_exceptional": [r"\bprofit\s+(?:for|from)\s+the\s+period\s+before\s+exceptional\b", r"\bnet\s+profit\b.*\bbefore\s+exceptional\b", r"\bprofit\s+before\s+exceptional\b"],
    "gross_npa": [r"\bgross\s+NPA\b", r"\bgross\s+non[\s-]?performing\s+assets?\b"],
    "gross_npa_pct": [r"\bgross\s+NPA\s+(?:ratio|%)", r"\bgross\s+NPA\s*%"],
    "net_npa": [r"\bnet\s+NPA\b", r"\bnet\s+non[\s-]?performing\s+assets?\b"],
    "net_npa_pct": [r"\bnet\s+NPA\s+(?:ratio|%)", r"\bnet\s+NPA\s*%"],
}

METRIC_PATTERNS = {
    "revenue": [
        r"revenue\s+from\s+operations",
        r"total\s+revenue\s+from\s+operations",
        r"revenue\s+from\s+contracts?\s+with\s+customers",
        r"net\s+sales",
        r"total\s+sales",
        r"turnover",
    ],
    "other_income": [r"other\s+[iIl]ncome"],
    "finance_cost": [
        r"finance\s+costs?",
        r"interest\s+and\s+finance\s+costs?",
    ],
    "depreciation": [
        r"depreciation\s*(?:and|&)\s*amortisation",
        r"depreciation\s*/\s*amortisation",
        r"depreciation\s+i\s+amortisation",
        r"depreciation\s+and\s+amortization",
    ],
    "pbt": [
        r"profit\s*/?\s*\(?loss\)?\s+before\s+tax",
        r"profit\s+before\s+tax",
    ],
    "pat_total": [
        r"profit\s*/?\s*\(?loss\)?\s+after\s+tax",
        r"profit\s+for\s+t\s*he\s+period",
        r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+period",
    ],
    "pat_owner": [
        r"profit\s+attributable\s+to\s*:?$",
        r"owners\s+of\s+the\s+company",
        r"owners\s+ofthe\s+company",
        r"profit\s+attributable\s+to\s+owners",
    ],
    "ebitda": [
        r"\bebitda\b",
        r"earnings\s+before\s+interest.*depreciation.*amort",
    ],
}


@dataclass
class Metric:
    name: str
    current: Optional[float] = None
    previous_q: Optional[float] = None
    yoy: Optional[float] = None
    qoq_pct: Optional[float] = None
    yoy_pct: Optional[float] = None
    confidence: str = "LOW"
    source: str = ""
    raw: Optional[List[str]] = None


def pct_change(cur, old):
    if cur is None or old in (None, 0):
        return None
    return (cur / old - 1.0) * 100.0


def numeric_candidates(token: str):
    """
    Return plausible numeric interpretations of one OCR token.
    Multiple candidates are retained so the row-level validator can choose
    the interpretation that best fits the neighbouring financial values.
    """
    if not token:
        return []

    raw = token.strip().replace(",", "").replace(" ", "")
    if raw in {"-", "—", "–", "NA", "N/A", "na", "n/a"}:
        return []

    neg = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()[]{}")

    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        v=float(raw)
        return [-v if neg else v]

    # OCR possibilities. The values are intentionally small and scoped to
    # financial-number tokens; normal prose is rejected later by row context.
    choices = {
        "O":"0","o":"0","I":"1","l":"1","|":"1","t":"1","T":"1","L":"1",
        "S":"5","s":"5","B":"8","b":"8","G":"6","g":"9","Z":"2","z":"2",
        "Q":"0","q":"9","R":"8","r":"1","A":"8","a":"4","E":"3","e":"3",
        "M":"44","m":"44","!":"1","?":"2","'":"7",
    }

    # A small beam rather than an uncontrolled combinatorial expansion.
    variants = {raw}
    for ch, rep in choices.items():
        variants |= {v.replace(ch, rep) for v in list(variants) if ch in v}

    # Common OCR hyphen inside a number is usually a decimal point.
    expanded=set(variants)
    for v in list(variants):
        if "-" in v[1:]:
            expanded.add(v.replace("-", ".", 1))
    variants=expanded

    results=[]
    for v in variants:
        # Remove OCR punctuation that is not useful.
        v=re.sub(r"[^0-9.\-]", "", v)
        if not v:
            continue
        if v.count(".")>1:
            first=v.find(".")
            v=v[:first+1]+v[first+1:].replace(".","")
        if v.startswith("-") and v.count("-")>1:
            continue

        # If no decimal was printed, financial statements generally use
        # two decimal places. Do not do this for very short integer tokens.
        if "." not in v and re.fullmatch(r"\d{3,}",v):
            v=v[:-2]+"."+v[-2:]

        if re.fullmatch(r"\d+(?:\.\d+)?",v):
            x=float(v)
            if x<=1e10:
                results.append(-x if neg else x)

    # Unique, deterministic order.
    return sorted(set(results))


def clean_numeric_token(token: str):
    vals=numeric_candidates(token)
    return vals[0] if len(vals)==1 else (vals[0] if vals else None)


def numeric_tokens(line: str) -> List[str]:
    """
    Extract complete numeric-looking tokens from one text line.
    Words such as 'Profit' must never become numbers.
    """
    out = []
    for tok in re.findall(r"\S+", line):
        tok2 = tok.strip(",;:")
        if NUM_TOKEN.fullmatch(tok2) and clean_numeric_token(tok2) is not None:
            out.append(tok2)
    return out


def get_pdf(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/pdf,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        raise ValueError("The URL did not return a PDF. Please use the direct NSE PDF link.")
    return data


def page_text(page, force_ocr=False):
    if force_ocr:
        try:
            tp = page.get_textpage_ocr()
            return page.get_text("text", textpage=tp, sort=True)
        except Exception:
            pass
    return page.get_text("text", sort=True)


def detect_company(doc):
    for page in doc:
        for line in page_text(page).splitlines()[:20]:
            s = " ".join(line.split()).strip()
            if (
                len(s) <= 120
                and len(s) >= 4
                and re.search(r"\b(LIMITED|LTD\.?|INDIA|INDUSTRIES|INVESTMENTS|PRODUCTS)\b", s, re.I)
                and not re.search(r"REGISTERED|REGD|WEBSITE|EMAIL|CIN|PHONE|TELEPHONE", s, re.I)
            ):
                return re.sub(r"\s+", " ", s).upper()
    return "COMPANY"


def statement_pages(doc, desired_basis: str, force_ocr=False):
    """Identify corporate or financial-sector result pages.

    Bank/NBFC investor-result PDFs may not contain the literal
    "standalone/consolidated financial results" heading. In that case,
    identify the actual result table from interest income/NII/PPOP/provisions/
    net-profit rows and infer standalone unless consolidated is explicit.
    """
    candidates = []
    for i, page in enumerate(doc):
        t = page_text(page, force_ocr)
        low = re.sub(r"\s+", " ", t.lower())
        cons = bool(re.search(r"(?:unaudited|audited)\s+consolidated\s+financial\s+results|consolidated\s+financial\s+results", low))
        stand = bool(re.search(r"(?:unaudited|audited)\s+standalone\s+financial\s+results|standalone\s+financial\s+results", low))
        if not cons and re.search(r"\bconsolidated\b", low) and re.search(r"\bfinancial results\b", low): cons = True
        if not stand and re.search(r"\bstandalone\b", low) and re.search(r"\bfinancial results\b", low): stand = True

        corporate = bool(re.search(r"revenue\s+from\s+operations|revenue\s+from\s+contracts|total\s+income", low) and re.search(r"\bprofit\b|\bexpense\b|\btax\b", low))
        financial_sector = bool(
            re.search(r"\binterest\s+(?:income|earned|expenses?|expended|paid)\b", low) and
            re.search(r"\bnet\s+interest\s+income\b|\bnii\b", low, re.I) and
            re.search(r"\boperating\s+profit\b|\bppop\b|pre[- ]provision", low, re.I) and
            re.search(r"\bprovisions?\b", low) and
            re.search(r"\bnet\s+profit\b|profit\s+after\s+tax", low)
        )

        if cons and corporate: candidates.append(("CONSOLIDATED", i, t, "CORPORATE"))
        if stand and corporate: candidates.append(("STANDALONE", i, t, "CORPORATE"))
        if financial_sector: candidates.append(("CONSOLIDATED" if cons else "STANDALONE", i, t, "FINANCIAL_SECTOR"))

    def score(x):
        basis, page, txt, kind = x; low = txt.lower(); z = 0
        for phrase, pts in [("quarter ended",100),("particulars",80),("revenue from operations",80),("profit before tax",50),("profit for the period",40),("net interest income",100),("operating profit",70),("provisions",50),("gross npa",40),("net npa",40)]:
            if phrase in low: z += pts
        if kind == "FINANCIAL_SECTOR": z += 30
        return z

    if desired_basis == "CONSOLIDATED": pool=[x for x in candidates if x[0]=="CONSOLIDATED"]
    elif desired_basis == "STANDALONE": pool=[x for x in candidates if x[0]=="STANDALONE"]
    else:
        pool=[x for x in candidates if x[0]=="CONSOLIDATED"] or [x for x in candidates if x[0]=="STANDALONE"]
    if not pool:
        raise ValueError("Could not identify a standalone, consolidated, or financial-sector results statement.")
    basis,start,t,kind=max(pool,key=score)

    pages=[start]
    for j in range(start+1,min(len(doc),start+5)):
        ll=page_text(doc[j],force_ocr).lower()
        if re.search(r"\b(?:particulars|revenue|interest income|net interest income|operating profit|provisions|net profit|gross npa|net npa)\b",ll,re.I): pages.append(j)
        else: break
    return basis,pages


def detect_sector(doc, pages, force_ocr=False):
    corpus = "\n".join(page_text(doc[p], force_ocr) for p in pages[:3])
    nbfc = len(re.findall(r"\b(?:NBFC|non[\s-]?banking\s+financial\s+company|non[\s-]?banking\s+finance\s+company|housing\s+finance)\b", corpus, re.I))
    bank = len(re.findall(r"\b(?:bank|banking|CASA|CRR|SLR)\b", corpus, re.I))
    if nbfc >= 1 and nbfc >= bank:
        return "NBFC"
    if bank >= 2:
        return "BANK"
    return "MANUFACTURING"

def extract_financial_sector_metric(rows, metric, factor=1.0, percent=False):
    pats = BANK_METRIC_PATTERNS.get(metric, [])
    candidates=[]
    for r in rows:
        label=r.get("text", "")
        if not any(re.search(p,label,re.I) for p in pats):
            continue
        nums=sorted([n for n in r.get("numbers",[]) if n["x"]>300], key=lambda n:n["x"])
        if len(nums)>=3:
            vals=[n["value"] for n in nums[:4]]
            if not percent: vals=[v*factor for v in vals]
            candidates.append((vals,label,[n["token"] for n in nums[:4]],r["page"]))
    if not candidates: return Metric(name=metric)
    vals,label,raw,page=candidates[0]
    vals=vals[:3]
    return Metric(name=metric,current=vals[0],previous_q=vals[1],yoy=vals[2],qoq_pct=pct_change(vals[0],vals[1]),yoy_pct=pct_change(vals[0],vals[2]),confidence="HIGH",source=label,raw=raw)

def derive_nii_from_interest(metrics):
    a=metrics.get("interest_earned"); b=metrics.get("interest_paid")
    if not a or not b or a.current is None or b.current is None: return Metric(name="nii")
    vals=[]
    for x,y in zip((a.current,a.previous_q,a.yoy),(b.current,b.previous_q,b.yoy)):
        vals.append(x-y if x is not None and y is not None else None)
    if any(v is None for v in vals): return Metric(name="nii")
    return Metric(name="nii",current=vals[0],previous_q=vals[1],yoy=vals[2],qoq_pct=pct_change(vals[0],vals[1]),yoy_pct=pct_change(vals[0],vals[2]),confidence="DERIVED",source="Interest earned - Interest paid",raw=[])

def make_metric(name, extracted, factor):
    if not extracted:
        return Metric(name=name)

    if len(extracted) == 4:
        vals, label, raw, page = extracted
    else:
        vals, label, raw = extracted
    vals = [x * factor for x in vals]

    # Statement order is:
    # current quarter, previous quarter, corresponding previous-year quarter,
    # previous financial year.
    cur, prev_q, yoy = vals[:3]

    return Metric(
        name=name,
        current=cur,
        previous_q=prev_q,
        yoy=yoy,
        qoq_pct=pct_change(cur, prev_q),
        yoy_pct=pct_change(cur, yoy),
        confidence="HIGH",
        source=label,
        raw=raw,
    )


def derive_ebitda(metrics):
    # EBITDA = PBT + Finance Costs + Depreciation - Other Income.
    # This is only a fallback when all required components exist.
    required = ["pbt", "finance_cost", "depreciation", "other_income"]
    if not all(metrics[k] and metrics[k].current is not None for k in required):
        return Metric(name="ebitda")

    def d(i):
        vals = []
        for k in required:
            m = metrics[k]
            vals.append([m.current, m.previous_q, m.yoy][i])
        if any(v is None for v in vals):
            return None
        return vals[0] + vals[1] + vals[2] - vals[3]

    vals = [d(0), d(1), d(2)]
    if any(v is None for v in vals):
        return Metric(name="ebitda")

    return Metric(
        name="ebitda",
        current=vals[0],
        previous_q=vals[1],
        yoy=vals[2],
        qoq_pct=pct_change(vals[0], vals[1]),
        yoy_pct=pct_change(vals[0], vals[2]),
        confidence="DERIVED",
        source="PBT + Finance Costs + Depreciation - Other Income",
        raw=[],
    )


def validate_metric(m: Metric, revenue: Metric, warnings):
    if m.current is None:
        warnings.append(f"{m.name.upper()} could not be confidently extracted.")
        return

    # Catch obvious OCR/column errors without rejecting legitimate volatility.
    if revenue.current not in (None, 0) and m.name in {"ebitda", "pat"}:
        ratio = abs(m.current / revenue.current)
        if ratio > 1.5:
            warnings.append(
                f"{m.name.upper()} is >150% of current revenue; possible column/OCR error."
            )

    if m.qoq_pct is not None and abs(m.qoq_pct) > 5000:
        warnings.append(
            f"{m.name.upper()} shows an extreme QoQ change ({m.qoq_pct:.0f}%). Review extraction."
        )
    if m.yoy_pct is not None and abs(m.yoy_pct) > 5000:
        warnings.append(
            f"{m.name.upper()} shows an extreme YoY change ({m.yoy_pct:.0f}%). Review extraction."
        )


def analyse(data, basis, quarter, force_ocr):
    doc = pymupdf.open(stream=data, filetype="pdf")
    company = detect_company(doc)

    selected_basis, pages = statement_pages(doc, basis, force_ocr)
    sector = detect_sector(doc, pages, force_ocr)
    # The statement page is the authoritative company-name source.
    statement_head = page_text(doc[pages[0]], force_ocr).splitlines()
    for line in statement_head[:12]:
        candidate_name = re.sub(r"\s+", " ", line.strip())
        if (
            4 <= len(candidate_name) <= 120
            and re.search(r"\b(LIMITED|LTD\.?|INDIA|INDUSTRIES|INVESTMENTS|PRODUCTS)\b",
                          candidate_name, re.I)
            and not re.search(r"REGISTERED|REGD|WEBSITE|EMAIL|CIN|PHONE|TELEPHONE",
                              candidate_name, re.I)
        ):
            company = candidate_name.upper()
            break

    all_lines = []
    page_texts = []
    for pno in pages:
        t = page_text(doc[pno], force_ocr)
        page_texts.append(t)
        all_lines.extend([x.strip() for x in t.splitlines() if x.strip()])

    unit_factor, unit_name = detect_unit("\n".join(page_texts))

    # Primary extraction uses visual rows. Text-line extraction is retained
    # as a fallback for unusual PDFs.
    visual_rows = page_rows(doc, pages, force_ocr)

    metrics = {}

    if sector in {"BANK", "NBFC"}:
        for k in ["interest_earned","interest_paid","operating_profit","provisions","net_profit_before_exceptional","gross_npa","net_npa"]:
            metrics[k] = extract_financial_sector_metric(visual_rows, k, unit_factor, percent=False)
        metrics["gross_npa_pct"] = extract_financial_sector_metric(visual_rows, "gross_npa_pct", 1.0, percent=True)
        metrics["net_npa_pct"] = extract_financial_sector_metric(visual_rows, "net_npa_pct", 1.0, percent=True)
        metrics["nii"] = derive_nii_from_interest(metrics)
        explicit = extract_financial_sector_metric(visual_rows, "nii", unit_factor, percent=False)
        if explicit.current is not None:
            metrics["nii"] = explicit
        # aliases used by downstream rendering
        metrics["operating_profit"].name="operating_profit"
        metrics["provisions"].name="provisions"
        metrics["net_profit_before_exceptional"].name="net_profit_before_exceptional"
    else:
        for k in ["revenue", "other_income", "finance_cost", "depreciation", "pbt"]:
            ex = extract_metric_rows(visual_rows, k)
            metrics[k] = make_metric(k, ex, unit_factor) if ex is not None else make_metric(k, extract_metric(all_lines, k), unit_factor)
        if selected_basis == "CONSOLIDATED":
            owner0 = extract_pat_owner(visual_rows)
            if owner0:
                metrics["pat"] = make_metric("pat", owner0, unit_factor)
            else:
                ex=extract_metric_rows(visual_rows,"pat_total")
                metrics["pat"]=make_metric("pat",ex if ex else extract_metric(all_lines,"pat_total"),unit_factor)
        else:
            ex=extract_metric_rows(visual_rows,"pat_total")
            metrics["pat"]=make_metric("pat",ex if ex else extract_metric(all_lines,"pat_total"),unit_factor)
        explicit=extract_metric_rows(visual_rows,"ebitda") or extract_metric(all_lines,"ebitda")
        metrics["ebitda"]=make_metric("ebitda",explicit,unit_factor) if explicit else derive_ebitda(metrics)

    revenue = metrics["revenue"]
    ebitda = metrics["ebitda"]
    pat = metrics["pat"]

    current_margin = (
        ebitda.current / revenue.current * 100
        if ebitda.current is not None and revenue.current not in (None, 0)
        else None
    )
    prev_margin = (
        ebitda.previous_q / revenue.previous_q * 100
        if ebitda.previous_q is not None and revenue.previous_q not in (None, 0)
        else None
    )
    yoy_margin = (
        ebitda.yoy / revenue.yoy * 100
        if ebitda.yoy is not None and revenue.yoy not in (None, 0)
        else None
    )

    warnings = []
    for m in [revenue, ebitda, pat]:
        validate_metric(m, revenue, warnings)

    # Do not let a single malformed metric crash the application.
    if current_margin is not None and not -100 <= current_margin <= 100:
        warnings.append("Current EBITDA margin is outside a normal range; review extraction.")

    # Diagnostic table: show only the selected statement, never mixed pages.
    diagnostics = []
    for k, m in metrics.items():
        diagnostics.append({
            "Metric": k,
            "Current": m.current,
            "Previous Q": m.previous_q,
            "YoY": m.yoy,
            "QoQ %": m.qoq_pct,
            "YoY %": m.yoy_pct,
            "Confidence": m.confidence,
            "Source": m.source,
            "Raw": " | ".join(m.raw or []),
        })

    result = {
        "company": company,
        "quarter": quarter,
        "basis": selected_basis,
        "sector": sector,
        "unit": unit_name,
        "pages": [p + 1 for p in pages],
        "revenue": asdict(revenue),
        "ebitda": asdict(ebitda),
        "pat": asdict(pat),
        "margin_current": current_margin,
        "margin_previous_q": prev_margin,
        "margin_yoy": yoy_margin,
        "margin_qoq_bps": (
            (current_margin - prev_margin) * 100
            if current_margin is not None and prev_margin is not None
            else None
        ),
        "margin_yoy_bps": (
            (current_margin - yoy_margin) * 100
            if current_margin is not None and yoy_margin is not None
            else None
        ),
        "warnings": warnings,
        "diagnostics": diagnostics,
        "statement_page_count": len(pages),
    }
    doc.close()
    return result


def direction(v):
    if v is None:
        return "NA"
    return "UP" if v >= 0 else "DOWN"


def pct_text(v, decimals=0):
    return "NA" if v is None else f"{abs(v):.{decimals}f}%"


def result_line(name, m):
    if m["current"] is None:
        return f"{name} NA"
    return (
        f"{name} {direction(m['yoy_pct'])} {pct_text(m['yoy_pct'])} "
        f"AT ₹{m['current']:,.1f} CR (YOY), "
        f"{direction(m['qoq_pct'])} {pct_text(m['qoq_pct'])} (QOQ)"
    )


def render_summary(r):
    company = re.sub(r"\s+(LIMITED|LTD\.?|PRIVATE LIMITED|PVT\. LTD\.?)$", "", r["company"], flags=re.I).strip().upper()
    if r.get("sector") in {"BANK", "NBFC"}:
        def line(label,key,percent=False):
            m=r.get(key,{})
            v=m.get("current")
            if v is None: return f"{label} NA"
            unit="%" if percent else " CR"
            return f"{label} {v:,.2f}{unit} | QoQ {pct_text(m.get('qoq_pct'))} | YoY {pct_text(m.get('yoy_pct'))}"
        return "\n\n".join([
            f"{company} {r['quarter']} ({r['sector']}) :",
            line("NII","nii"),
            line("OPERATING PROFIT / PPOP","operating_profit"),
            line("PROVISIONS","provisions"),
            line("NET PROFIT BEFORE EXCEPTIONAL","net_profit_before_exceptional"),
            line("GROSS NPA","gross_npa"),
            line("GROSS NPA %","gross_npa_pct",True),
            line("NET NPA","net_npa"),
            line("NET NPA %","net_npa_pct",True),
        ])
    margin="NA" if r["margin_current"] is None else f"{r['margin_current']:.1f}%"
    yoy_margin="NA" if r["margin_yoy"] is None else f"{r['margin_yoy']:.1f}%"
    qoq_margin="NA" if r["margin_previous_q"] is None else f"{r['margin_previous_q']:.1f}%"
    return "\n\n".join([f"{company} {r['quarter']} :",result_line("REVENUE",r["revenue"]),result_line("EBITDA",r["ebitda"]),f"MARGINS {margin} V {yoy_margin} (YOY), {qoq_margin} (QOQ)",result_line("CONS NET PROFIT",r["pat"])])


# ============================================================
# UI
# ============================================================

st.title("📊 Quarterly Financial Results Analyser")
st.caption("Final build — statement-first extraction, strict basis locking, OCR-safe parsing")

with st.sidebar:
    st.header("Analysis settings")
    quarter = st.selectbox("Quarter", ["Q1", "Q2", "Q3", "Q4"], index=0)
    basis = st.radio(
        "Results basis",
        ["AUTO", "CONSOLIDATED", "STANDALONE"],
        index=0,
        help="AUTO prefers consolidated when both statements are present.",
    )
    force_ocr = st.checkbox(
        "Force OCR",
        value=False,
        help="Use only if the PDF has no usable text layer.",
    )

url = st.text_input(
    "Direct PDF link",
    placeholder="https://nsearchives.nseindia.com/corporate/....pdf",
)
upload = st.file_uploader("Or upload PDF", type=["pdf"])

if st.button("ANALYSE", type="primary", width="stretch"):
    try:
        if upload is not None:
            data = upload.getvalue()
        elif url.strip():
            data = get_pdf(url.strip())
        else:
            st.error("Please paste a direct PDF link or upload a PDF.")
            st.stop()

        r = analyse(data, basis, quarter, force_ocr)

        st.success(
            f"Selected {r['sector']} • {r['basis']} statement • PDF pages {r['pages'][0]}–{r['pages'][-1]} "
            f"• {r['unit']}"
        )

        st.subheader("Results")
        st.code(render_summary(r))

        if r["warnings"]:
            with st.expander("⚠ Review warnings", expanded=True):
                for w in r["warnings"]:
                    st.warning(w)
        else:
            st.success("No extraction validation warnings.")

        with st.expander("🔎 Extraction diagnostics", expanded=False):
            st.write("Basis:", r["basis"])
            st.write("Selected PDF pages:", r["pages"])
            st.write("Unit:", r["unit"])
            st.dataframe(
                pd.DataFrame(r["diagnostics"]),
                width="stretch",
                hide_index=True,
            )

        table = pd.DataFrame([
            {
                "Metric": x["Metric"],
                "Current ₹cr": x["Current"],
                "Previous Q ₹cr": x["Previous Q"],
                "YoY ₹cr": x["YoY"],
                "QoQ %": x["QoQ %"],
                "YoY %": x["YoY %"],
                "Confidence": x["Confidence"],
            }
            for x in r["diagnostics"]
            if x["Metric"] in {"revenue", "ebitda", "pat"}
        ])
        st.subheader("Calculation details")
        st.dataframe(table, width="stretch", hide_index=True)

        st.download_button(
            "Download JSON",
            json.dumps(r, indent=2, default=str),
            file_name="results_analysis_final.json",
            mime="application/json",
        )

    except Exception as e:
        # Never leave the user with a blank page or a cryptic NoneType error.
        st.error(f"Analysis failed: {type(e).__name__}: {e}")
        st.info(
            "If this is a new PDF format, download the JSON diagnostics and share it "
            "for a controlled parser update."
        )
        with st.expander("Technical error", expanded=False):
            st.exception(e)
