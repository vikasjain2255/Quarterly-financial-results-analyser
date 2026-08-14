import re, json, urllib.request
import streamlit as st
import pandas as pd
import fitz

st.set_page_config(page_title="Quarterly Results Analyser V4.3", page_icon="📊", layout="wide")
st.title("📊 Quarterly Results Analyser V4.3")
st.caption("Quarterly-results PDF analyser — robust standalone/consolidated table selection.")

ALIASES={
"revenue":[r"revenue\s+from\s+operations",r"net\s+sales",r"total\s+sales",r"turnover",r"total\s+revenue"],
"other_income":[r"other\s+income"],
"pbt":[r"profit\s+before\s+exceptional\s+item\s*(?:&|and)?\s*tax",r"profit\s+before\s+tax",r"profit\s*/?\s*\(?loss\)?\s+before\s+tax",r"\bpbt\b"],
"finance_cost":[r"finance\s+costs?",r"interest\s+and\s+finance\s+costs?",r"interest\s+costs?"],
"depreciation":[r"depreciation\s*(?:and|&|/|i)\s*amortisation",r"depreciation\s+and\s+amortization",r"depreciation"],
"pat":[r"profit\s+for\s+the\s+period",r"net\s+profit\s*/?\s*\(?loss\)?\s+for\s+the\s+period",r"profit\s+after\s+tax",r"net\s+profit\s+after\s+tax",r"net\s+profit",r"profit\s+attributable\s+to.*owners"],
"ebitda":[r"\bebitda\b",r"earnings\s+before\s+interest.*tax.*depreciation.*amortisation"]
}
NUMBER=re.compile(r"(?<![\w.])\(?-?\d[\d,]*(?:\.\d+)?\)?(?![\w.])")

def norm(s):
    s=s.replace("\u00a0"," ").replace("\u2013","-").replace("\u2014","-")
    return re.sub(r"[ \t]+"," ",s).strip()

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

def growth(a,b):
    return None if a is None or b in (None,0) else (a/b-1)*100

def get_pdf(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/pdf,*/*"})
    with urllib.request.urlopen(req,timeout=60) as r: data=r.read()
    if not data.startswith(b"%PDF"): raise ValueError("URL did not return a PDF.")
    return data

def extract_pages(data,use_ocr=False):
    doc=fitz.open(stream=data,filetype="pdf"); pages=[]; ocr=0
    for p in doc:
        t=p.get_text("text",sort=True)
        if use_ocr and len(t.strip())<50:
            try:
                t=p.get_text("text",textpage=p.get_textpage_ocr(),sort=True); ocr+=1
            except: pass
        pages.append(t)
    doc.close(); return pages,ocr

def detect_unit(text):
    l=text.lower()
    if re.search(r"\b(?:in|₹)\s*lakhs?\b|\bfigures\s+in\s+lakhs?\b",l): return .01,"₹ crore (converted from ₹ lakh)"
    if re.search(r"\b(?:in|₹)\s*millions?\b|\bfigures\s+in\s+millions?\b",l): return .1,"₹ crore (converted from ₹ million)"
    return 1.0,"₹ crore (assumed)"

def heading_match(text,basis):
    t=norm(text).lower()
    # Handle both common word orders:
    # "Unaudited Consolidated Financial Results"
    # "Consolidated Unaudited Financial Results"
    if basis=="CONSOLIDATED":
        return bool(re.search(r"(?:unaudited\s+consolidated|consolidated\s+unaudited)\s+financial\s+results",t))
    if basis=="STANDALONE":
        return bool(re.search(r"(?:unaudited\s+standalone|standalone\s+unaudited)\s+financial\s+results",t))
    return False

def select_section(pages,basis):
    cons=[i for i,p in enumerate(pages) if heading_match(p,"CONSOLIDATED")]
    stand=[i for i,p in enumerate(pages) if heading_match(p,"STANDALONE")]
    if basis=="CONSOLIDATED":
        if not cons: raise ValueError("Consolidated financial-results table was not found.")
        idx=cons[-1]; return pages[idx], "CONSOLIDATED", idx+1
    if basis=="STANDALONE":
        if not stand: raise ValueError("Standalone financial-results table was not found.")
        idx=stand[-1]; return pages[idx], "STANDALONE", idx+1
    if cons:
        idx=cons[-1]; return pages[idx], "CONSOLIDATED", idx+1
    if stand:
        idx=stand[-1]; return pages[idx], "STANDALONE", idx+1
    # Fallback: locate "consolidated" / "standalone" financial result wording loosely.
    for typ in ("CONSOLIDATED","STANDALONE"):
        hits=[i for i,p in enumerate(pages) if re.search(rf"{typ.lower()}.*financial\s+results|financial\s+results.*{typ.lower()}",norm(p).lower())]
        if hits:
            idx=hits[-1]; return pages[idx],typ,idx+1
    raise ValueError("Could not identify a standalone or consolidated financial-results table.")

def find_metric(section,metric):
    lines=[norm(x) for x in section.splitlines() if norm(x)]
    candidates=[]
    for i,line in enumerate(lines):
        if any(re.search(p,line,re.I) for p in ALIASES[metric]):
            for span in (1,2,3):
                w=" ".join(lines[i:i+span])
                vals=nums(w)
                if len(vals)>=3:
                    # Financial statement row should normally contain 4 columns.
                    score=100-(span-1)*10+(25 if len(vals)==4 else 0)
                    candidates.append((score,vals[:4],w)); break
    if not candidates: return None
    return max(candidates,key=lambda x:x[0])

def metric(raw,factor):
    if not raw: return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":""}
    cur,prev,yoy=[v*factor for v in raw[1][:3]]
    return {"current":cur,"previous":prev,"yoy":yoy,"qoq":growth(cur,prev),"yoypct":growth(cur,yoy),"confidence":("HIGH" if raw[0]>=120 else "MEDIUM"),"source":raw[2]}

def derive_ebitda(raw,factor):
    keys=["pbt","finance_cost","depreciation","other_income"]
    if not all(raw.get(k) and len(raw[k][1])>=3 for k in keys): return None
    vals=[raw["pbt"][1][i]+raw["finance_cost"][1][i]+raw["depreciation"][1][i]-raw["other_income"][1][i] for i in range(3)]
    return metric((100,vals,"Derived: PBT + Finance Costs + Depreciation - Other Income"),factor)

def company_name(section):
    # Prefer an explicit legal/company-name line.
    for line in section.splitlines()[:35]:
        s=norm(line)
        if re.search(r"\b(EURO\s+PANEL|LIMITED|LTD\.?|INDUSTRIES|BANK|FINANCE|STEEL|CEMENT|PHARMA|FOODS)\b",s,re.I):
            if len(s)<100 and "financial results" not in s.lower() and "registered" not in s.lower():
                return s.upper()
    return "COMPANY"

def analyse(data,quarter,basis,use_ocr):
    pages,ocr=extract_pages(data,use_ocr)
    section,actual,page=select_section(pages,basis)
    factor,unit=detect_unit(section)
    raw={k:find_metric(section,k) for k in ALIASES}
    rev=metric(raw["revenue"],factor); pat=metric(raw["pat"],factor)
    ebitda=metric(raw["ebitda"],factor) if raw["ebitda"] else derive_ebitda(raw,factor)
    mc=ebitda["current"]/rev["current"]*100 if ebitda["current"] is not None and rev["current"] not in (None,0) else None
    mp=ebitda["previous"]/rev["previous"]*100 if ebitda["previous"] is not None and rev["previous"] not in (None,0) else None
    my=ebitda["yoy"]/rev["yoy"]*100 if ebitda["yoy"] is not None and rev["yoy"] not in (None,0) else None
    warnings=[f"{x} could not be extracted." for x,m in [("REVENUE",rev),("EBITDA",ebitda),("PAT",pat)] if m["current"] is None]
    if ocr: warnings.append(f"OCR used on {ocr} page(s).")
    return {"company":company_name(section),"quarter":quarter,"basis":actual,"page":page,"unit":unit,"revenue":rev,"ebitda":ebitda,"pat":pat,"margin_current":mc,"margin_previous":mp,"margin_yoy":my,"warnings":warnings,"diagnostics":raw}

def out(r):
    name=re.sub(r"\s+(LIMITED|LTD\.?)$","",r["company"],flags=re.I).strip()
    pl="CONS NET PROFIT" if r["basis"]=="CONSOLIDATED" else "NET PROFIT"
    def line(label,m):
        if m["current"] is None: return f"{label} NA"
        return f"{label} {'UP' if m['yoypct']>=0 else 'DOWN'} {abs(m['yoypct']):.0f}% AT ₹{m['current']:,.1f} CR (YOY), {'UP' if m['qoq']>=0 else 'DOWN'} {abs(m['qoq']):.0f}% (QOQ)"
    f=lambda x:"NA" if x is None else f"{x:.1f}%"
    return "\n\n".join([f"{name} {r['quarter']} :",line("REVENUE",r["revenue"]),line("EBITDA",r["ebitda"]),f"MARGINS {f(r['margin_current'])} V {f(r['margin_yoy'])} (YOY), {f(r['margin_previous'])} (QOQ)",line(pl,r["pat"])])

with st.sidebar:
    quarter=st.selectbox("Quarter",["Q1","Q2","Q3","Q4"])
    basis=st.radio("Results basis",["AUTO","CONSOLIDATED","STANDALONE"],help="AUTO prefers consolidated when both are present.")
    use_ocr=st.checkbox("Use OCR if needed")

url=st.text_input("Direct PDF link",placeholder="https://nsearchives.nseindia.com/...pdf")
upload=st.file_uploader("Or upload PDF",type=["pdf"])

if st.button("ANALYSE",type="primary",width="stretch"):
    try:
        if upload: data=upload.getvalue()
        elif url.strip(): data=get_pdf(url.strip())
        else: st.error("Paste a direct PDF link or upload a PDF."); st.stop()
        with st.spinner("Selecting the requested financial-results table..."):
            r=analyse(data,quarter,basis,use_ocr)
        st.success(f"Analysis complete — {r['basis']} results selected (page {r['page']})")
        st.subheader("Results"); st.code(out(r),language="text")
        rows=[]
        for label,m in [("Revenue",r["revenue"]),("EBITDA",r["ebitda"]),("Net Profit",r["pat"])]:
            rows.append({"Metric":label,"Current ₹cr":m["current"],"Previous Q ₹cr":m["previous"],"YoY ₹cr":m["yoy"],"QoQ %":m["qoq"],"YoY %":m["yoypct"],"Confidence":m["confidence"]})
        st.subheader("Calculation details"); st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
        if r["warnings"]:
            with st.expander("⚠ Review warnings",expanded=True):
                for w in r["warnings"]: st.warning(w)
        with st.expander("🔎 Extraction diagnostics"):
            st.write("Company:",r["company"]); st.write("Selected basis:",r["basis"]); st.write("Selected PDF page:",r["page"]); st.write("Unit:",r["unit"])
            for k,raw in r["diagnostics"].items():
                st.markdown(f"**{k.upper()}**"); st.code(str(raw[2]) if raw else "Not found")
        st.download_button("Download JSON",json.dumps(r,indent=2,default=str),file_name="results_analysis_v4_3.json",mime="application/json")
    except Exception as e:
        st.error(f"Analysis failed: {e}")
        st.exception(e)
