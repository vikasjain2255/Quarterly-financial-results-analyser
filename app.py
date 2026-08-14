import re
import json
import urllib.request
import streamlit as st
import pandas as pd
import fitz

st.set_page_config(page_title="Quarterly Results Analyser V4", page_icon="📊", layout="wide")
st.title("📊 Quarterly Results Analyser V4")
st.caption("Paste a direct PDF link or upload a quarterly-results PDF.")

NUM_RE = re.compile(r"(?<![\w.])(?P<n>\(?-?\d[\d,]*(?:\.\d+)?\)?)(?![\w.])")
ALIASES = {
 "revenue":[r"revenue from operations",r"revenue",r"net sales",r"total sales",r"sales",r"turnover"],
 "pat":[r"profit attributable to.*owners",r"profit after tax",r"profit for the period",r"net profit",r"net profit after tax"],
 "pbt":[r"profit before tax",r"profit before taxation",r"\bpbt\b"],
 "finance_cost":[r"finance costs?",r"finance cost",r"interest and finance costs?"],
 "depreciation":[r"depreciation and amortisation",r"depreciation & amortisation",r"depreciation"],
 "other_income":[r"other income"],
 "ebitda":[r"\bebitda\b",r"earnings before interest.*tax.*depreciation.*amortisation",r"operating profit"]
}

def num(s):
    s=str(s).strip().replace(",","").replace(" ","")
    if s in ("","-","—","–","na","n/a"): return None
    neg=s.startswith("(") and s.endswith(")")
    try:
        x=float(s.strip("()"))
        return -x if neg else x
    except: return None

def nums(t):
    return [x for x in (num(m.group("n")) for m in NUM_RE.finditer(t)) if x is not None]

def pct(a,b):
    return None if a is None or b in (None,0) else (a/b-1)*100

def fp(x,d=0): return "NA" if x is None else f"{x:.{d}f}%"
def fc(x): return "NA" if x is None else (f"{x:,.0f}" if abs(x-round(x))<.05 else f"{x:,.1f}")

def get_pdf(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/pdf,*/*"})
    with urllib.request.urlopen(req,timeout=45) as r: data=r.read()
    if not data.startswith(b"%PDF"): raise ValueError("URL did not return a PDF. Use a direct PDF link.")
    return data

def pdf_text(data,ocr=False):
    doc=fitz.open(stream=data,filetype="pdf"); out=[]; o=0
    for p in doc:
        t=p.get_text("text",sort=True)
        if ocr and len(t.strip())<40:
            try: t=p.get_text("text",textpage=p.get_textpage_ocr(),sort=True); o+=1
            except: pass
        out.append(t)
    doc.close(); return "\n\f\n".join(out),o

def tables_text(data):
    out=[]
    doc=fitz.open(stream=data,filetype="pdf")
    for p in doc:
        try:
            for tab in p.find_tables().tables:
                for row in tab.extract(): out.append(" | ".join("" if x is None else str(x) for x in row))
        except: pass
    doc.close(); return "\n".join(out)

def company(t):
    lines=[x.strip() for x in t.splitlines() if x.strip()]
    for x in lines[:70]:
        if 5<=len(x)<=140 and not any(k in x.lower() for k in ["bse","nse","statement","unaudited","audited","financial results","registered office","quarter ended","cin"]):
            if re.search(r"\b(limited|ltd\.?|industries|bank|finance|foods|motors|pharma)\b",x,re.I): return x
    return lines[0] if lines else "COMPANY"

def unit(t):
    l=t.lower()
    if re.search(r"(?:amounts?|figures?).{0,20}\bmillions?\b",l): return .1,"₹ million"
    if re.search(r"(?:amounts?|figures?).{0,20}\blakhs?\b",l): return .01,"₹ lakh"
    return 1.0,"₹ crore (assumed)"

def find(t,m):
    lines=t.splitlines(); best=None
    for i,line in enumerate(lines):
        score=sum(10 for a in ALIASES[m] if re.search(a,line,re.I))
        if score<=0: continue
        for span in (1,2,3,4):
            w=" ".join(lines[i:i+span]); v=nums(w)
            if len(v)>=3:
                cand=(score-span,v,w)
                if best is None or cand[0]>best[0]: best=cand
                break
    if not best:return None
    return best[1],best[2],("HIGH" if best[0]>=9 else "MEDIUM" if best[0]>=6 else "LOW")

def order(t):
    l=t.lower()
    y=min([l.find(x) for x in ["corresponding previous year","corresponding quarter","previous year quarter"] if l.find(x)>=0],default=10**9)
    p=min([l.find(x) for x in ["previous quarter","preceding quarter"] if l.find(x)>=0],default=10**9)
    return "current_yoy_prev" if y<p else "current_prev_yoy"

def metric(name,raw,meta,factor,ordr):
    if name not in raw or len(raw[name])<3: return {"name":name,"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":""}
    a,b,c=[x*factor for x in raw[name]]
    cur,prev,yoy=(a,c,b) if ordr=="current_yoy_prev" else (a,b,c)
    return {"name":name,"current":cur,"previous":prev,"yoy":yoy,"qoq":pct(cur,prev),"yoypct":pct(cur,yoy),"confidence":meta[name][1],"source":meta[name][0]}

def analyse(data,q,basis,ocr):
    text,o=pdf_text(data,ocr); combined=text+"\n"+tables_text(data); l=combined.lower()
    if basis=="AUTO":
        c=l.count("consolidated financial results")+l.count("consolidated statement")
        s=l.count("standalone financial results")+l.count("standalone statement")
        basis="CONSOLIDATED" if c>=s and "consolidated" in l else "STANDALONE"
    factor,unitname=unit(combined); ordr=order(combined); raw={}; meta={}
    for m in ALIASES:
        z=find(combined,m)
        if z: raw[m],meta[m]=z[0],(z[1],z[2])
    if "ebitda" not in raw and all(x in raw for x in ["pbt","finance_cost","depreciation","other_income"]):
        n=min(len(raw[x]) for x in ["pbt","finance_cost","depreciation","other_income"])
        raw["ebitda"]=[raw["pbt"][i]+raw["finance_cost"][i]+raw["depreciation"][i]-raw["other_income"][i] for i in range(n)]
        meta["ebitda"]=("Derived: PBT + Finance Cost + Depreciation - Other Income","MEDIUM")
    r=metric("revenue",raw,meta,factor,ordr); e=metric("ebitda",raw,meta,factor,ordr); p=metric("pat",raw,meta,factor,ordr)
    cm=e["current"]/r["current"]*100 if e["current"] is not None and r["current"] not in (None,0) else None
    pm=e["previous"]/r["previous"]*100 if e["previous"] is not None and r["previous"] not in (None,0) else None
    ym=e["yoy"]/r["yoy"]*100 if e["yoy"] is not None and r["yoy"] not in (None,0) else None
    warnings=[]
    for m in [r,e,p]:
        if m["current"] is None or m["qoq"] is None or m["yoypct"] is None: warnings.append(m["name"].upper()+" could not be confidently calculated.")
        elif m["confidence"]=="LOW": warnings.append(m["name"].upper()+" extraction confidence is LOW.")
    if o: warnings.append(f"OCR used on {o} page(s).")
    return {"company":company(text),"quarter":q,"basis":basis,"unit":unitname,"revenue":r,"ebitda":e,"pat":p,"cm":cm,"pm":pm,"ym":ym,"ybps":(cm-ym)*100 if cm is not None and ym is not None else None,"qbps":(cm-pm)*100 if cm is not None and pm is not None else None,"warnings":warnings,"meta":meta}

def line(m,label):
    d=lambda x:"UP" if x is not None and x>=0 else "DOWN"
    return f"{label} {d(m['yoypct'])} {fp(abs(m['yoypct']))} AT ₹{fc(m['current'])} CR (YOY), {d(m['qoq'])} {fp(abs(m['qoq']))} (QOQ)"

def compact(r):
    co=re.sub(r"\s+(PRIVATE LIMITED|PVT\.?\s*LTD\.?|LIMITED|LTD\.?)$","",r["company"],flags=re.I).strip().upper()
    b="CONS" if r["basis"]=="CONSOLIDATED" else "SA"
    return "\n\n".join([f"{co} {r['quarter']} :",line(r["revenue"],"REVENUE"),line(r["ebitda"],"EBITDA"),f"MARGINS {fp(r['cm'],1)} V {fp(r['ym'],1)} (YOY), {fp(r['pm'],1)} (QOQ)",line(r["pat"],b+" NET PROFIT")])

with st.sidebar:
    quarter=st.selectbox("Quarter",["Q1","Q2","Q3","Q4"])
    basis=st.radio("Results basis",["AUTO","CONSOLIDATED","STANDALONE"])
    ocr=st.checkbox("Use OCR if needed")
url=st.text_input("Direct PDF link",placeholder="https://www.bseindia.com/...pdf")
upload=st.file_uploader("Or upload PDF",type=["pdf"])

if st.button("ANALYSE",type="primary",use_container_width=True):
    try:
        if upload: data=upload.getvalue()
        elif url.strip(): data=get_pdf(url.strip())
        else: st.error("Paste a direct PDF link or upload a PDF."); st.stop()
        with st.spinner("Analysing..."): r=analyse(data,quarter,basis,ocr)
        st.success("Analysis complete")
        st.subheader("Results")
        st.code(compact(r),language="text")
        rows=[]
        for m in [r["revenue"],r["ebitda"],r["pat"]]:
            rows.append({"Metric":m["name"].upper(),"Current ₹cr":m["current"],"Previous Q ₹cr":m["previous"],"YoY Comparable ₹cr":m["yoy"],"QoQ %":m["qoq"],"YoY %":m["yoypct"],"Confidence":m["confidence"]})
        st.subheader("Calculation details")
        st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
        st.write({"Current margin":r["cm"],"Previous Q margin":r["pm"],"YoY margin":r["ym"],"YoY bps":r["ybps"],"QoQ bps":r["qbps"]})
        if r["warnings"]:
            with st.expander("⚠ Review warnings",expanded=True):
                for w in r["warnings"]: st.warning(w)
        with st.expander("🔎 Extraction diagnostics"):
            st.write("Company:",r["company"]); st.write("Basis:",r["basis"]); st.write("Unit:",r["unit"])
            for k,(src,conf) in r["meta"].items():
                st.markdown(f"**{k.upper()} — {conf}**"); st.code(src)
        st.download_button("Download JSON",json.dumps(r,indent=2,default=str),file_name="results_analysis.json",mime="application/json")
    except Exception as e:
        st.error(f"Analysis failed: {e}")
