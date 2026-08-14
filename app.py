import re, json, urllib.request
import streamlit as st
import pandas as pd
import fitz

st.set_page_config(page_title="Quarterly Results Analyser V4.2", page_icon="📊", layout="wide")
st.title("📊 Quarterly Results Analyser V4.2")
st.caption("Indian quarterly-results PDF analyser — consolidated/standalone aware.")

ALIASES = {
    "revenue": [r"revenue\s+from\s+operations", r"net\s+sales", r"total\s+sales", r"turnover", r"total\s+revenue"],
    "other_income": [r"other\s+income"],
    "pbt": [r"profit\s*/?\s*\(?loss\)?\s*before\s+exceptional", r"profit\s+before\s+tax", r"profit\s*/?\s*\(?loss\)?\s+before\s+tax", r"\bpbt\b"],
    "finance_cost": [r"finance\s+costs?", r"interest\s+and\s+finance\s+costs?", r"interest\s+costs?"],
    "depreciation": [r"depreciation\s*(?:and|&|/)\s*amortisation", r"depreciation\s+and\s+amortization", r"depreciation"],
    "pat": [r"profit\s+for\s+the\s+period", r"net\s+profit\s*/?\s*\(?loss\)?\s+for\s+the\s+period", r"profit\s+after\s+tax", r"net\s+profit\s+after\s+tax", r"net\s+profit", r"profit\s+attributable\s+to.*owners"],
    "ebitda": [r"\bebitda\b", r"earnings\s+before\s+interest.*tax.*depreciation.*amortisation"],
}
NUMBER = re.compile(r"(?<![\w.])\(?-?\d[\d,]*(?:\.\d+)?\)?(?![\w.])")

def to_num(x):
    x=str(x).strip().replace(",","").replace(" ","")
    if not x or x in {"-","—","–","na","n/a"}: return 0.0
    neg=x.startswith("(") and x.endswith(")")
    try:
        v=float(x.strip("()")); return -v if neg else v
    except: return None

def nums(s):
    out=[]
    for m in NUMBER.finditer(s):
        v=to_num(m.group(0))
        if v is not None: out.append(v)
    return out

def growth(cur, old):
    if cur is None or old in (None,0): return None
    return (cur/old-1)*100

def fmt_pct(x):
    return "NA" if x is None else f"{x:.1f}%"

def fmt_cr(x):
    return "NA" if x is None else f"{x:,.1f}".rstrip("0").rstrip(".")

def get_pdf(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/pdf,*/*"})
    with urllib.request.urlopen(req,timeout=60) as r: data=r.read()
    if not data.startswith(b"%PDF"): raise ValueError("URL did not return a PDF. Use a direct PDF URL.")
    return data

def extract_pages(data,use_ocr=False):
    doc=fitz.open(stream=data,filetype="pdf"); pages=[]; ocr=0
    for p in doc:
        t=p.get_text("text",sort=True)
        if use_ocr and len(t.strip())<50:
            try: t=p.get_text("text",textpage=p.get_textpage_ocr(),sort=True); ocr+=1
            except: pass
        pages.append(t)
    doc.close(); return pages,ocr

def norm(s):
    s=s.replace("\u00a0"," ")
    return re.sub(r"[ \t]+"," ",s).strip()

def detect_unit(text):
    l=text.lower()
    if re.search(r"\b(?:in|₹)\s*lakhs?\b|\bfigures\s+in\s+lakhs?\b",l): return .01,"₹ crore (converted from ₹ lakh)"
    if re.search(r"\b(?:in|₹)\s*millions?\b|\bfigures\s+in\s+millions?\b",l): return .1,"₹ crore (converted from ₹ million)"
    return 1.0,"₹ crore (assumed)"

def find_section(pages, requested):
    full="\n\f\n".join(pages); l=full.lower()
    cons=list(re.finditer(r"(?:unaudited|audited)\s+consolidated\s+financial\s+results",l))
    stand=list(re.finditer(r"(?:unaudited|audited)\s+standalone\s+financial\s+results",l))
    if requested=="CONSOLIDATED":
        if not cons: raise ValueError("Consolidated financial-results table was not found in this PDF.")
        start=cons[-1].start()
        return full[start:], "CONSOLIDATED"
    if requested=="STANDALONE":
        if not stand: raise ValueError("Standalone financial-results table was not found in this PDF.")
        start=stand[-1].start()
        end=cons[-1].start() if cons and cons[-1].start()>start else len(full)
        return full[start:end], "STANDALONE"
    # AUTO deliberately prefers consolidated when available.
    if cons: return full[cons[-1].start():], "CONSOLIDATED"
    if stand: return full[stand[-1].start():], "STANDALONE"
    return full,"UNKNOWN"

def find_metric(section,metric):
    lines=[norm(x) for x in section.splitlines() if norm(x)]
    candidates=[]
    for i,line in enumerate(lines):
        if any(re.search(p,line,re.I) for p in ALIASES[metric]):
            for span in (1,2,3):
                w=" ".join(lines[i:i+span]); vals=nums(w)
                if len(vals)>=3:
                    score=100-(span-1)*10+(20 if len(vals)==4 else 0)
                    candidates.append((score,vals[:4],w)); break
    if not candidates: return None
    candidates.sort(reverse=True,key=lambda x:x[0])
    score,vals,src=candidates[0]
    return vals,src,"HIGH" if score>=110 else "MEDIUM"

def make_metric(raw,factor):
    if not raw: return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":""}
    # Never unpack the entire row: Indian tables often contain 4 columns including full year.
    cur,prev,yoy=[v*factor for v in raw[0][:3]]
    return {"current":cur,"previous":prev,"yoy":yoy,"qoq":growth(cur,prev),"yoypct":growth(cur,yoy),"confidence":raw[2],"source":raw[1]}

def company_name(section):
    for x in section.splitlines()[:50]:
        s=norm(x)
        if len(s)<120 and re.search(r"\b(LIMITED|LTD\.?|INDUSTRIES|BANK|FINANCE|STEEL|CEMENT|PHARMA|FOODS)\b",s,re.I) and "financial results" not in s.lower():
            return s.upper()
    return "COMPANY"

def analyse(data,quarter,basis,use_ocr):
    pages,ocr=extract_pages(data,use_ocr)
    section,actual=find_section(pages,basis)
    factor,unit=detect_unit(section)
    raw={k:find_metric(section,k) for k in ALIASES}
    rev=make_metric(raw["revenue"],factor); pat=make_metric(raw["pat"],factor)
    if raw["ebitda"]:
        ebitda=make_metric(raw["ebitda"],factor)
    elif all(raw[k] and len(raw[k][0])>=3 for k in ["pbt","finance_cost","depreciation","other_income"]):
        vals=[raw["pbt"][0][i]+raw["finance_cost"][0][i]+raw["depreciation"][0][i]-raw["other_income"][0][i] for i in range(3)]
        fake=(vals,"Derived: PBT + Finance Costs + Depreciation - Other Income","MEDIUM")
        ebitda=make_metric(fake,factor)
    else: ebitda=make_metric(None,factor)
    mc=ebitda["current"]/rev["current"]*100 if ebitda["current"] is not None and rev["current"] not in (None,0) else None
    mp=ebitda["previous"]/rev["previous"]*100 if ebitda["previous"] is not None and rev["previous"] not in (None,0) else None
    my=ebitda["yoy"]/rev["yoy"]*100 if ebitda["yoy"] is not None and rev["yoy"] not in (None,0) else None
    warnings=[f"{x} could not be extracted." for x,m in [("REVENUE",rev),("EBITDA",ebitda),("PAT",pat)] if m["current"] is None]
    if ocr: warnings.append(f"OCR used on {ocr} page(s).")
    return {"company":company_name(section),"quarter":quarter,"basis":actual,"unit":unit,"revenue":rev,"ebitda":ebitda,"pat":pat,"margin_current":mc,"margin_previous":mp,"margin_yoy":my,"warnings":warnings,"diagnostics":raw}

def output_text(r):
    name=re.sub(r"\s+(LIMITED|LTD\.?)$","",r["company"],flags=re.I).strip()
    pl="CONS NET PROFIT" if r["basis"]=="CONSOLIDATED" else "NET PROFIT"
    def ln(label,m):
        if m["current"] is None: return f"{label} NA"
        return f"{label} {'UP' if m['yoypct']>=0 else 'DOWN'} {abs(m['yoypct']):.0f}% AT ₹{fmt_cr(m['current'])} CR (YOY), {'UP' if m['qoq']>=0 else 'DOWN'} {abs(m['qoq']):.0f}% (QOQ)"
    return "\n\n".join([f"{name} {r['quarter']} :",ln("REVENUE",r["revenue"]),ln("EBITDA",r["ebitda"]),f"MARGINS {fmt_pct(r['margin_current'])} V {fmt_pct(r['margin_yoy'])} (YOY), {fmt_pct(r['margin_previous'])} (QOQ)",ln(pl,r["pat"])])

with st.sidebar:
    quarter=st.selectbox("Quarter",["Q1","Q2","Q3","Q4"])
    basis=st.radio("Results basis",["AUTO","CONSOLIDATED","STANDALONE"],help="AUTO prefers consolidated whenever a consolidated financial-results table exists.")
    use_ocr=st.checkbox("Use OCR if needed")

url=st.text_input("Direct PDF link",placeholder="https://nsearchives.nseindia.com/...pdf")
upload=st.file_uploader("Or upload PDF",type=["pdf"])

if st.button("ANALYSE",type="primary",use_container_width=True):
    try:
        if upload: data=upload.getvalue()
        elif url.strip(): data=get_pdf(url.strip())
        else: st.error("Paste a direct PDF link or upload a PDF."); st.stop()
        with st.spinner("Analysing selected financial-results table..."):
            r=analyse(data,quarter,basis,use_ocr)
        st.success(f"Analysis complete — {r['basis']} results selected")
        st.subheader("Results"); st.code(output_text(r),language="text")
        rows=[]
        for label,m in [("Revenue",r["revenue"]),("EBITDA",r["ebitda"]),("Net Profit",r["pat"])]:
            rows.append({"Metric":label,"Current ₹cr":m["current"],"Previous Q ₹cr":m["previous"],"YoY ₹cr":m["yoy"],"QoQ %":m["qoq"],"YoY %":m["yoypct"],"Confidence":m["confidence"]})
        st.subheader("Calculation details"); st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
        if r["warnings"]:
            with st.expander("⚠ Review warnings",expanded=True):
                for w in r["warnings"]: st.warning(w)
        with st.expander("🔎 Extraction diagnostics"):
            st.write("Company:",r["company"]); st.write("Selected basis:",r["basis"]); st.write("Unit:",r["unit"])
            for k,raw in r["diagnostics"].items():
                st.markdown(f"**{k.upper()}**")
                st.code(str(raw[1]) if raw else "Not found")
        st.download_button("Download JSON",json.dumps(r,indent=2,default=str),file_name="results_analysis_v4_2.json",mime="application/json")
    except Exception as e:
        st.error(f"Analysis failed: {e}"); st.exception(e)
