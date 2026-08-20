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
        r"ebitda",
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
        v = float(raw)
        return [-v if neg else v]

    # OCR possibilities. The values are intentionally small and scoped to
    # financial-number tokens; normal prose is rejected later by row context.
    choices = {
        "O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "t": "1", "T": "1", "L": "1",
        "S": "5", "s": "5", "B": "8", "b": "8", "G": "6", "g": "9", "Z": "2", "z": "2",
        "Q": "0", "q": "9", "R": "8", "r": "1", "A": "8", "a": "4", "E": "3", "e": "3",
        "M": "44", "m": "44", "!": "1", "?": "2", "'": "7",
    }

    # A small beam rather than an uncontrolled combinatorial expansion.
    variants = {raw}
    for ch, rep in choices.items():
        variants |= {v.replace(ch, rep) for v in list(variants) if ch in v}

    # Common OCR hyphen inside a number is usually a decimal point.
    expanded = set(variants)
    for v in list(variants):
        if "-" in v[1:]:
            expanded.add(v.replace("-", ".", 1))
    variants = expanded

    results = []
    for v in variants:
        # Remove OCR punctuation that is not useful.
        v = re.sub(r"[^0-9.\-]", "", v)
        if not v:
            continue
        if v.count(".") > 1:
            first = v.find(".")
            v = v[:first + 1] + v[first + 1:].replace(".", "")
        if v.startswith("-") and v.count("-") > 1:
            continue

        # If no decimal was printed, financial statements generally use
        # two decimal places. Do not do this for very short integer tokens.
        if "." not in v and re.fullmatch(r"\d{3,}", v):
            v = v[:-2] + "." + v[-2:]

        if re.fullmatch(r"\d+(?:\.\d+)?", v):
            x = float(v)
            if x <= 1e10:
                results.append(-x if neg else x)

    # Unique, deterministic order.
    return sorted(set(results))


def clean_numeric_token(token: str):
    vals = numeric_candidates(token)
    return vals[0] if len(vals) == 1 else (vals[0] if vals else None)


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
                and re.search(r"(LIMITED|LTD\.?|INDIA|INDUSTRIES|INVESTMENTS|PRODUCTS)", s, re.I)
                and not re.search(r"REGISTERED|REGD|WEBSITE|EMAIL|CIN|PHONE|TELEPHONE", s, re.I)
            ):
                return re.sub(r"\s+", " ", s).upper()
    return "COMPANY"


def statement_pages(doc, desired_basis: str, force_ocr=False):
    """
    Find the actual statement page(s).
    Explicit basis ALWAYS wins.
    AUTO prefers consolidated when both are present.
    """
    candidates = []

    for i, page in enumerate(doc):
        t = page_text(page, force_ocr)
        low = t.lower()

        cons = bool(re.search(
            r"(unaudited|audited)\s+consolidated\s+financial\s+results|"
            r"consolidated\s+financial\s+results", low
        ))
        stand = bool(re.search(
            r"(unaudited|audited)\s+standalone\s+financial\s+results|"
            r"standalone\s+financial\s+results", low
        ))

        # Some PDFs have "Consolidated" and "Standalone" on separate lines.
        if not cons and re.search(r"consolidated", low) and re.search(r"financial results", low):
            cons = True
        if not stand and re.search(r"standalone", low) and re.search(r"financial results", low):
            stand = True

        # A real results table must contain the financial-results heading AND
        # the core revenue row. This prevents board notices/auditor reports
        # from being mistaken for the actual statement.
        has_results_table = bool(
            re.search(r"revenue\s+from\s+operations|revenue\s+from\s+contracts", low)
            and re.search(r"profit|expense|tax", low)
        )

        if cons and has_results_table:
            candidates.append(("CONSOLIDATED", i, t))
        if stand and has_results_table:
            candidates.append(("STANDALONE", i, t))

    def candidate_score(x):
        basis0, page0, text0 = x
        low0 = text0.lower()
        score = 0
        score += 100 if "quarter ended" in low0 else 0
        score += 80 if "particulars" in low0 else 0
        score += 80 if "revenue from operations" in low0 else 0
        score += 50 if "profit before tax" in low0 else 0
        score += 40 if "profit for the period" in low0 else 0
        score += 30 if "finance costs" in low0 else 0
        score += 20 if "depreciation" in low0 else 0
        return score

    if desired_basis == "CONSOLIDATED":
        c = [x for x in candidates if x[0] == "CONSOLIDATED"]
        if not c:
            raise ValueError("No consolidated financial-results statement was found in this PDF.")
        basis, start, t = max(c, key=candidate_score)
    elif desired_basis == "STANDALONE":
        c = [x for x in candidates if x[0] == "STANDALONE"]
        if not c:
            raise ValueError("No standalone financial-results statement was found in this PDF.")
        basis, start, t = max(c, key=candidate_score)
    else:
        c = [x for x in candidates if x[0] == "CONSOLIDATED"]
        s = [x for x in candidates if x[0] == "STANDALONE"]
        if c:
            basis, start, t = max(c, key=candidate_score)
        elif s:
            basis, start, t = max(s, key=candidate_score)
        else:
            raise ValueError("Could not identify a standalone or consolidated financial-results statement.")

    # Include only the statement page and immediately following continuation
    # pages until the next statement/balance-sheet section.
    pages = [start]
    for j in range(start + 1, min(start + 5, len(doc))):
        tt = page_text(doc[j], force_ocr)
        ll = tt.lower()
        if re.search(r"balance sheet|cash flow statement|segment revenue|segment results", ll):
            break
        if re.search(r"standalone\s+financial\s+results|consolidated\s+financial\s+results", ll):
            break
        # A continuation page generally has financial-result line items.
        if re.search(
            r"profit|revenue|expense|tax|other comprehensive|earnings per share|particulars",
            ll,
        ):
            pages.append(j)
        else:
            break

    return basis, pages


def detect_unit(text: str):
    """Detect the monetary unit declared by the selected statement and return 
    a conversion multiplier to standardise all figures into ₹ crore.
    """
    low = re.sub(r"\s+", " ", text.lower())

    million_patterns = [
        r"(?:₹|rs\.?|inr)?\s*(?:in\s+)?millions?",
        r"(?:amount|figures|numbers|financials?)\s*(?:are|is)?\s*(?:stated|reported|given)?\s*(?:in\s+)?millions?",
        r"in\s+millions?",
    ]
    lakh_patterns = [
        r"(?:₹|rs\.?|inr)?\s*(?:in\s+)?lakhs?",
        r"in\s+lakhs?",
    ]
    thousand_patterns = [
        r"(?:₹|rs\.?|inr)?\s*(?:in\s+)?thousands?",
        r"in\s+thousands?",
    ]
    crore_patterns = [
        r"(?:₹|rs\.?|inr)?\s*(?:in\s+)?crores?",
        r"in\s+crores?",
    ]

    # Convert source units to Crores:
    # 1 Million = 0.1 Crore
    # 1 Lakh = 0.01 Crore
    # 1 Thousand = 0.0001 Crore
    if any(re.search(p, low) for p in million_patterns):
        return 0.1, "₹ crore"
    if any(re.search(p, low) for p in lakh_patterns):
        return 0.01, "₹ crore"
    if any(re.search(p, low) for p in thousand_patterns):
        return 0.0001, "₹ crore"
    if any(re.search(p, low) for p in crore_patterns):
        return 1.0, "₹ crore"

    # Default factor to 1.0 if not detected, but label output as INR crore
    return 1.0, "₹ crore"


def find_header(lines):
    """
    Identify the declared four financial periods.
    This is informational; the numerical order is the order printed in
    the statement, not inferred from magnitudes.
    """
    for i, line in enumerate(lines):
        if YEAR_RE.search(line) and (
            "quarter" in line.lower()
            or "year ended" in line.lower()
            or "particulars" in line.lower()
        ):
            return i
    return None


def line_has_metric(line, metric):
    low = re.sub(r"\s+", " ", line.strip().lower())
    for p in METRIC_PATTERNS[metric]:
        if re.search(p, low):
            return True
    return False


def is_numeric_only(line):
    toks = numeric_tokens(line)
    stripped = line.strip()
    if not toks:
        return False
    # A numeric-only row should contain no alphabetic words.
    letters = re.sub(r"[\d\s,().\-\[\]{}.|]", "", stripped)
    return len(letters) == 0


def collect_row(lines, idx, max_forward=8):
    """
    Financial PDFs frequently extract a row label on one line and the four
    numbers on the following lines. Collect numeric values without allowing
    later unrelated rows to leak in.
    """
    label = lines[idx].strip()
    vals = numeric_tokens(label)

    # If the label line already contains all four numbers, use them.
    if len(vals) >= 4:
        return vals[:4], label

    collected = list(vals)

    for j in range(idx + 1, min(idx + 1 + max_forward, len(lines))):
        nxt = lines[j].strip()

        # Stop at a new textual row/section.
        if not nxt:
            continue

        nums = numeric_tokens(nxt)

        if nums and is_numeric_only(nxt):
            collected.extend(nums)
            if len(collected) >= 4:
                return collected[:4], label
            continue

        # Some PDF extractors put label fragments before the numbers.
        # Permit short continuation fragments only if they contain no
        # financial-row keywords and no more than a few characters.
        if not nums and len(nxt) < 35 and not re.search(
            r"revenue|income|expense|profit|loss|tax|finance|depreciation|"
            r"materials|employee|purchase|inventory|other|total|particulars|"
            r"share|earnings|comprehensive|exceptional",
            nxt,
            re.I,
        ):
            continue

        break

    return (collected[:4] if len(collected) >= 4 else collected), label


def merge_numeric_items(items):
    """
    Merge adjacent OCR fragments such as:
      71 + S7 -> 71S7
      1?R + 15 -> 1?R15
    Only merge fragments that are very close horizontally.
    """
    out = []
    i = 0
    while i < len(items):
        cur = items[i]
        if i + 1 < len(items):
            nxt = items[i + 1]
            gap = nxt["x"] - cur["x1"]
            if gap <= 3.5 and (
                NUM_TOKEN.fullmatch(cur["text"].strip(",:;"))
                or re.search(r"\d", cur["text"])
            ) and (
                NUM_TOKEN.fullmatch(nxt["text"].strip(",:;"))
                or re.search(r"\d", nxt["text"])
            ):
                cur = {"x": cur["x"], "x1": nxt["x1"],
                       "text": cur["text"] + nxt["text"]}
                i += 1
        out.append(cur)
        i += 1
    return out


def choose_row_values(tokens):
    """
    Pick four values from OCR candidates using financial-table constraints.
    The first three are current quarter / previous quarter / YoY quarter;
    the fourth is FY and may be several times larger.
    """
    candidate_lists = [numeric_candidates(t) for t in tokens]
    if len(candidate_lists) < 4:
        return None

    # Keep the first four visual financial columns.
    candidate_lists = candidate_lists[:4]
    best = None

    import itertools
    for combo in itertools.product(*candidate_lists):
        if len(combo) != 4:
            continue
        a, b, c, d = combo
        if any(abs(x) > 1e10 for x in combo):
            continue
        # The first three periods should normally be in the same order of
        # magnitude. Allow extreme business moves, but strongly penalise
        # impossible 10x/0.1x OCR interpretations.
        score = 0.0
        for x, y in [(a, b), (a, c), (b, c)]:
            if x == 0 or y == 0:
                continue
            ratio = max(abs(x / y), abs(y / x))
            if ratio > 12:
                score -= 50
            else:
                score -= abs(__import__("math").log10(ratio)) * 5

        # FY is cumulative and can legitimately be larger than Q1.
        if a != 0 and d != 0:
            ratio = abs(d / a)
            if ratio < 0.25 or ratio > 20:
                score -= 30

        # Prefer values with two decimals.
        for x in combo:
            if abs(x - round(x, 2)) < 1e-9:
                score += 1

        if best is None or score > best[0]:
            best = (score, list(combo))
    return best[1] if best else None


def page_rows(doc, pages, force_ocr=False):
    """
    Reconstruct visual table rows from PDF word coordinates.
    This is critical for NSE PDFs where text extraction is column-major:
    labels may be emitted first and the four numerical columns afterwards.
    """
    rows = []
    for pno in pages:
        page = doc[pno]
        try:
            if force_ocr:
                tp = page.get_textpage_ocr()
                words = page.get_text("words", textpage=tp, sort=True)
            else:
                words = page.get_text("words", sort=True)
        except Exception:
            words = page.get_text("words", sort=True)

        grouped = []
        for w in words:
            x0, y0, x1, y1, txt = w[:5]
            cy = (y0 + y1) / 2
            target = None
            for g in grouped:
                if abs(g["y"] - cy) <= 3.2:
                    target = g
                    break
            item = {"x": float(x0), "x1": float(x1), "text": str(txt)}
            if target is None:
                grouped.append({"y": cy, "items": [item]})
            else:
                target["items"].append(item)
                target["y"] = (target["y"] + cy) / 2

        for g in grouped:
            g["items"].sort(key=lambda z: z["x"])
            g["items"] = merge_numeric_items(g["items"])
            g["text"] = " ".join(x["text"] for x in g["items"])
            g["numbers"] = []

            # Financial columns start well to the right of the particulars
            # column in NSE result tables.
            financial_items = [x for x in g["items"] if x["x"] > 300]
            token_text = [x["text"] for x in financial_items]
            chosen = choose_row_values(token_text) if len(token_text) >= 4 else None

            if chosen is not None:
                for x, v in zip(financial_items[:4], chosen):
                    if not (1900 <= v <= 2100):
                        g["numbers"].append({
                            "value": v,
                            "x": x["x"],
                            "token": x["text"],
                        })
            else:
                for x in financial_items:
                    vals = numeric_candidates(x["text"])
                    if vals:
                        v = vals[0]
                        if not (1900 <= v <= 2100):
                            g["numbers"].append({
                                "value": v,
                                "x": x["x"],
                                "token": x["text"],
                            })
            g["page"] = pno + 1
            rows.append(g)

    return sorted(rows, key=lambda r: (r["page"], r["y"]))


def extract_pat_owner(rows):
    """Pick the Owners row immediately associated with 'Profit attributable to'."""
    anchor_indices = []
    for i, r in enumerate(rows):
        if re.search(r"profit\s+attributable\s+to", r["text"], re.I):
            anchor_indices.append(i)
    candidates = []
    for ai in anchor_indices:
        for j in range(ai + 1, min(ai + 6, len(rows))):
            r = rows[j]
            if re.search(r"owners\s+of\s*the\s+compan", r["text"], re.I):
                nums = [n for n in r["numbers"] if n["x"] > 300]
                if len(nums) >= 4:
                    candidates.append((j - ai, [n["value"] for n in nums[:4]], r["text"], [n["token"] for n in nums[:4]]))
                break
    if not candidates:
        return None
    _, vals, label, raw = min(candidates, key=lambda x: x[0])
    return vals, label, raw


def extract_metric_rows(rows, metric):
    """
    Primary extraction path. Match the metric label to the SAME VISUAL ROW
    as its four numeric values. This prevents cross-row mixing of:
      Revenue from Operations / Other Operating Revenue / Total Revenue.
    """
    candidates = []

    for r in rows:
        low = re.sub(r"\s+", " ", r["text"].strip().lower())
        if not any(re.search(p, low) for p in METRIC_PATTERNS[metric]):
            continue

        nums = sorted(r["numbers"], key=lambda x: x["x"])

        # Financial statement rows normally have exactly four period values.
        # If there are more, take the four right-most/most plausible values.
        if len(nums) >= 4:
            # The financial columns in these NSE statements are to the right
            # of the Particulars column. Ignore tiny left-side numbering.
            nums = [n for n in nums if n["x"] > 300]
            if len(nums) >= 4:
                nums = nums[:4]
                vals = [n["value"] for n in nums]
                candidates.append((vals, r["text"], [n["token"] for n in nums], r["page"]))

    if not candidates:
        return None

    def score(c):
        label = c[1].lower()
        score = 0
        exact = {
            "revenue": ["revenue from operations"],
            "other_income": ["other income"],
            "finance_cost": ["finance costs"],
            "depreciation": ["depreciation and amortisation", "depreciation / amortisation"],
            "pbt": ["profit before tax"],
            "pat_total": ["profit for the period", "profit/(loss) after tax"],
            "pat_owner": ["owners ofthe company", "owners of the company"],
            "ebitda": ["ebitda"],
        }
        for phrase in exact.get(metric, []):
            if phrase in label:
                score += 50
        if "segment" in label:
            score -= 100
        if "earnings per share" in label or "eps" in label:
            score -= 100
        return score

    return max(candidates, key=score)


def extract_metric(lines, metric):
    candidates = []

    for i, line in enumerate(lines):
        if not line_has_metric(line, metric):
            continue

        vals, label = collect_row(lines, i)
        if len(vals) >= 4:
            parsed = [clean_numeric_token(x) for x in vals[:4]]
            if all(x is not None for x in parsed):
                candidates.append((parsed, label, vals))

    if not candidates:
        return None

    # Prefer the most exact label and avoid a segment/notes row.
    def score(c):
        vals, label, raw = c
        s = 0
        if "revenue from operations" in label.lower(): s += 30
        if "profit before tax" in label.lower(): s += 30
        if "profit for the period" in label.lower(): s += 30
        if "finance costs" in label.lower(): s += 30
        if "depreciation" in label.lower(): s += 30
        if "other income" in label.lower(): s += 30
        if "segment" in label.lower(): s -= 100
        if "eps" in label.lower(): s -= 100
        return s

    return max(candidates, key=score)


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
    # The statement page is the authoritative company-name source.
    statement_head = page_text(doc[pages[0]], force_ocr).splitlines()
    for line in statement_head[:12]:
        candidate_name = re.sub(r"\s+", " ", line.strip())
        if (
            4 <= len(candidate_name) <= 120
            and re.search(r"(LIMITED|LTD\.?|INDIA|INDUSTRIES|INVESTMENTS|PRODUCTS)",
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

    unit_factor, unit_name = detect_unit("
".join(page_texts))

    # Primary extraction uses visual rows. Text-line extraction is retained
    # as a fallback for unusual PDFs.
    visual_rows = page_rows(doc, pages, force_ocr)

    metrics = {}
    for k in ["revenue", "other_income", "finance_cost", "depreciation", "pbt"]:
        ex = extract_metric_rows(visual_rows, k)
        if ex is not None:
            metrics[k] = make_metric(k, ex, unit_factor)
        else:
            metrics[k] = make_metric(k, extract_metric(all_lines, k), unit_factor)

    # Prefer consolidated "profit attributable to owners" for CONSOLIDATED.
    if selected_basis == "CONSOLIDATED":
        owner0 = extract_pat_owner(visual_rows)
        owner = owner0
        if owner:
            metrics["pat"] = make_metric("pat", owner, unit_factor)
        else:
            ex = extract_metric_rows(visual_rows, "pat_total")
            metrics["pat"] = make_metric(
                "pat", ex if ex else extract_metric(all_lines, "pat_total"), unit_factor
            )
    else:
        ex = extract_metric_rows(visual_rows, "pat_total")
        metrics["pat"] = make_metric(
            "pat", ex if ex else extract_metric(all_lines, "pat_total"), unit_factor
        )

    explicit = extract_metric_rows(visual_rows, "ebitda")
    if explicit is None:
        explicit = extract_metric(all_lines, "ebitda")
    if explicit:
        metrics["ebitda"] = make_metric("ebitda", explicit, unit_factor)
    else:
        metrics["ebitda"] = derive_ebitda(metrics)

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


def result_line(name, m, unit):
    if m["current"] is None:
        return f"{name} NA"
    return (
        f"{name} {direction(m['yoy_pct'])} {pct_text(m['yoy_pct'])} "
        f"AT {unit}{m['current']:,.2f} (YOY), "
        f"{direction(m['qoq_pct'])} {pct_text(m['qoq_pct'])} (QOQ)"
    )


def render_summary(r):
    company = re.sub(
        r"\s+(LIMITED|LTD\.?|PRIVATE LIMITED|PVT\. LTD\.?)$",
        "",
        r["company"],
        flags=re.I,
    ).strip().upper()

    margin = "NA" if r["margin_current"] is None else f"{r['margin_current']:.1f}%"
    yoy_margin = "NA" if r["margin_yoy"] is None else f"{r['margin_yoy']:.1f}%"
    qoq_margin = "NA" if r["margin_previous_q"] is None else f"{r['margin_previous_q']:.1f}%"

    unit = "₹ crore "
    return "

".join([
        f"{company} {r['quarter']} :",
        result_line("REVENUE", r["revenue"], unit),
        result_line("EBITDA", r["ebitda"], unit),
        f"MARGINS {margin} V {yoy_margin} (YOY), {qoq_margin} (QOQ)",
        result_line("CONS NET PROFIT", r["pat"], unit),
    ])


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
            f"Selected {r['basis']} statement • PDF pages {r['pages'][0]}–{r['pages'][-1]} "
            f"• Output converted to {r['unit']}"
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
                f"Current {r['unit']}": x["Current"],
                f"Previous Q {r['unit']}": x["Previous Q"],
                f"YoY {r['unit']}": x["YoY"],
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
