import re, json, urllib.request
import streamlit as st
import pandas as pd
import pymupdf

st.set_page_config(page_title="Quarterly Results Analyser V5.4", page_icon="📊", layout="wide")
st.title("📊 Quarterly Results Analyser V5.4")
st.caption("Financial-table geometry • strict numeric recognition • safer column reconstruction")

PAT = {
    "revenue":[r"total\s+revenue\s+from\s+operations",r"revenue\s+from\s+operations",r"revenue\s+from\s+contracts?\s+with\s+customers",r"total\s+revenue",r"net\s+sales",r"total\s+sales",r"turnover"],
    "other_income":[r"other\s+income"],
    "finance_cost":[r"finance\s+costs?",r"finance\s+cost",r"interest\s+and\s+finance\s+costs?",r"interest\s+costs?"],
    "depreciation":[r"depreciation\s*(?:and|&|/)\s*amortisation",r"depreciation\s+and\s+amortization",r"depreciation"],
    "pbt":[r"profit\s*/?\s*\(?loss\)?\s+before\s+tax",r"profit\s+before\s+tax",r"\bpbt\b"],
    "pat":[r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+period",r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+year",r"profit\s*/?\s*\(?loss\)?\s+after\s+tax",r"net\s+profit\s+after\s+tax",r"net\s+profit",r"profit\s+attributable\s+to.*owners"],
    "ebitda":[r"\bebitda\b",r"earnings\s+before\s+interest.*depreciation.*amort"]
}
DIGIT = re.compile(r"\d")
STRICT_NUM = re.compile(r"^[\(\[\{]?\s*(?:\d[\d,]*(?:\.\d+)?|\.\d+)\s*[\)\]\}]?$")
CORRUPT_NUM = re.compile(r"^[\(\[\{]?(?=[A-Za-z0-9|OoIlSsBbGgZzQq.,_-]*\d)[A-Za-z0-9|OoIlSsBbGgZzQq.,_-]+[\)\]\}]?$")
OCR_MAP = str.maketrans({"O":"0","o":"0","I":"1","l":"1","|":"1","S":"5","s":"5","B":"8","G":"6","g":"6","Z":"2","z":"2","Q":"0","q":"9"})

def norm(s): return re.sub(r"\s+"," ",str(s).replace("\u00a0"," ").replace("–","-").replace("—","-")).strip()

def strict_clean_num(token):
    raw=str(token).strip().replace(",","").replace("_","")
    if not DIGIT.search(raw): return None,"NONE",None
    if STRICT_NUM.fullmatch(raw):
        neg=raw.startswith(("(","[","{")) and raw.endswith((")","]","}"))
        try:
            v=float(raw.strip("()[]{}"))
            return (-v if neg else v),"HIGH",raw
        except: return None,"NONE",None
    if len(raw)>24 or not CORRUPT_NUM.fullmatch(raw): return None,"NONE",None
    neg=raw.startswith(("(","[","{")) and raw.endswith((")","]","}"))
    y=raw.strip("()[]{}").translate(OCR_MAP)
    y=re.sub(r"[^0-9.\-]","",y)
    if not y or y.count(".")>1: return None,"LOW",None
    try:
        v=float(y)
        if abs(v)>1e9:return None,"LOW",None
        return (-v if neg else v),"REPAIRED",y
    except:return None,"NONE",None

def get_pdf(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/pdf,*/*"})
    with urllib.request.urlopen(req,timeout=90) as r:data=r.read()
    if not data.startswith(b"%PDF"):raise ValueError("URL did not return a PDF.")
    return data

def unit_factor(s):
    l=s.lower()
    if re.search(r"\b(?:in|₹)\s*lakhs?\b|\bfigures\s+in\s+lakhs?\b",l):return .01,"₹ crore (converted from ₹ lakh)"
    if re.search(r"\b(?:in|₹)\s*millions?\b|\bfigures\s+in\s+millions?\b",l):return .1,"₹ crore (converted from ₹ million)"
    return 1,"₹ crore"

def make_rows(words):
    rows=[]
    for w in words:
        x0,y0,x1,y1,t,*_=w; cy=(y0+y1)/2
        row=next((r for r in rows if abs(r["y"]-cy)<=2.8),None)
        item={"x":float(x0),"x1":float(x1),"text":str(t)}
        if row:row["items"].append(item);row["y"]=(row["y"]+cy)/2
        else:rows.append({"y":cy,"items":[item]})
    for r in rows:
        r["items"].sort(key=lambda z:z["x"]);r["text"]=norm(" ".join(i["text"] for i in r["items"]))
    return sorted(rows,key=lambda r:r["y"])

def row_words(page,ocr=False):
    if ocr:
        try:return make_rows(page.get_text("words",textpage=page.get_textpage_ocr(),sort=True))
        except:pass
    return make_rows(page.get_text("words",sort=True))

def enrich(rows):
    out=[]
    for r in rows:
        q=dict(r); q["numbers"]=[]
        for it in r["items"]:
            v,c,rep=strict_clean_num(it["text"])
            if v is not None:q["numbers"].append({"value":v,"x":it["x"],"x1":it["x1"],"token":it["text"],"confidence":c,"repaired":rep})
        out.append(q)
    return out

def page_text(page,ocr=False):
    if ocr:
        try:return page.get_text("text",textpage=page.get_textpage_ocr(),sort=True)
        except:pass
    return page.get_text("text",sort=True)

def profile(page,idx):
    s=page_text(page);l=s.lower();rows=enrich(row_words(page))
    labels=sum(any(re.search(p,l,re.I) for p in PAT[k]) for k in ["revenue","finance_cost","depreciation","pbt","pat"])
    heavy=sum(len(r["numbers"])>=3 for r in rows)
    audit=-140 if re.search(r"independent\s+auditor|limited\s+review",l) else 0
    notes=-120 if re.search(r"\bnotes\s+to\s+(?:the\s+)?financial|accounting\s+policies|basis\s+of\s+preparation",l) else 0
    standalone=sum(bool(re.search(p,l)) for p in [r"unaudited\s+standalone",r"standalone\s+financial\s+results",r"standalone\s+statement"])
    consolidated=sum(bool(re.search(p,l)) for p in [r"unaudited\s+consolidated",r"consolidated\s+financial\s+results",r"consolidated\s+statement"])
    return {"idx":idx,"text":s,"rows":rows,"labels":labels,"heavy":heavy,"audit":audit,"notes":notes,"standalone":standalone,"consolidated":consolidated,"score":labels*30+min(heavy,8)*20+audit+notes}

def choose_pages(doc,basis):
    prof=[profile(p,i) for i,p in enumerate(doc)]
    cand=[p for p in prof if p["labels"]>=2 or p["heavy"]>=2]
    if not cand:raise ValueError("No financial-results table candidate could be identified.")
    def bs(p,b):
        return p["score"]+(120 if p[b.lower()]>0 else 0)+(-80 if p["standalone" if b=="CONSOLIDATED" else "consolidated"]>0 else 0)
    if basis=="AUTO":
        c=max(cand,key=lambda p:bs(p,"CONSOLIDATED"));s=max(cand,key=lambda p:bs(p,"STANDALONE"))
        actual,chosen=("CONSOLIDATED",c) if bs(c,"CONSOLIDATED")>=bs(s,"STANDALONE") else ("STANDALONE",s)
    else:actual,chosen=basis,max(cand,key=lambda p:bs(p,basis))
    start=chosen["idx"];end=start+1
    for i in range(start+1,min(len(doc),start+3)):
        q=prof[i]
        if q["audit"]<0 or q["notes"]<0:break
        if q["labels"]>=1 or q["heavy"]>=2:end=i+1
        else:break
    return actual,start,end,prof

def infer_columns(rows):
    pts=[n["x"] for r in rows for n in r["numbers"] if n["x"]>=240]
    if not pts:return []
    pts.sort();clusters=[]
    for x in pts:
        if not clusters or x-clusters[-1][-1]>12:clusters.append([x])
        else:clusters[-1].append(x)
    cs=[{"center":sum(c)/len(c),"support":len(c)} for c in clusters if len(c)>=3]
    if len(cs)>4:cs=sorted(cs,key=lambda z:(z["support"],z["center"]),reverse=True)[:4];cs=sorted(cs,key=lambda z:z["center"])
    return [c["center"] for c in cs]

def map_row(row,centers,tol=28):
    vals=[None]*len(centers)
    for n in row["numbers"]:
        if not centers:continue
        j=min(range(len(centers)),key=lambda k:abs(n["x"]-centers[k]));d=abs(n["x"]-centers[j])
        if d<=tol and (vals[j] is None or d<abs(vals[j]["x"]-centers[j])):vals[j]=n
    return vals

def empty():return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[],"mapped":[],"mapped_x":[]}

def growth(a,b):return None if a is None or b in (None,0) else (a/b-1)*100

def extract(rows,metric,centers,factor):
    found=[]
    for i,r in enumerate(rows):
        for k,p in enumerate(PAT[metric]):
            if re.search(p,r["text"],re.I):found.append((300-k*20,i,r));break
    found.sort(key=lambda x:(x[0],len(x[2]["numbers"])),reverse=True)
    for _,_,r in found:
        m=map_row(r,centers);v=[x["value"]*factor if x else None for x in m]
        if sum(x is not None for x in v)>=3:
            return {"current":v[0],"previous":v[1],"yoy":v[2],"qoq":growth(v[0],v[1]),"yoypct":growth(v[0],v[2]),"confidence":"HIGH","source":r["text"],"raw":v,"mapped":[x["token"] if x else None for x in m],"mapped_x":[x["x"] if x else None for x in m]}
    return empty()

def derive_ebitda(m):
    keys=["pbt","finance_cost","depreciation","other_income"]
    if any(m[k]["current"] is None for k in keys):return empty()
    n=min(len(m[k]["raw"]) for k in keys)
    if n<3:return empty()
    v=[m["pbt"]["raw"][i]+m["finance_cost"]["raw"][i]+m["depreciation"]["raw"][i]-m["other_income"]["raw"][i] for i in range(min(n,4))]
    return {"current":v[0],"previous":v[1],"yoy":v[2],"qoq":growth(v[0],v[1]),"yoypct":growth(v[0],v[2]),"confidence":"DERIVED","source":"PBT + Finance Costs + Depreciation - Other Income","raw":v,"mapped":[],"mapped_x":[]}

def company_name(doc):
    txt="\n".join(page_text(doc[i]) for i in range(min(2,len(doc))))
    for line in txt.splitlines():
        s=norm(line)
        if 5<=len(s)<=140 and re.search(r"\b(LIMITED|LTD\.?|INDIA|INDUSTRIES|BANK|FINANCE|STEEL|CEMENT|PHARMA|FOODS|INVESTMENTS|TUBE)\b",s,re.I) and not re.search(r"financial results|auditor|registered office|notes to|profit before|finance costs",s,re.I):
            return s.upper()
    return "COMPANY"

def analyse(data,basis,ocr,quarter):
    doc=pymupdf.open(stream=data,filetype="pdf")
    actual,start,end,prof=choose_pages(doc,basis)
    rows=[]
    for i in range(start,end):
        for r in enrich(row_words(doc[i],ocr)):
            q=dict(r);q["page"]=i+1;rows.append(q)
    section="\n".join(prof[i]["text"] for i in range(start,end));factor,unit=unit_factor(section)
    centers=infer_columns(rows)
    metrics={k:extract(rows,k,centers,factor) for k in ["revenue","other_income","finance_cost","depreciation","pbt","pat","ebitda"]}
    if metrics["ebitda"]["current"] is None:metrics["ebitda"]=derive_ebitda(metrics)
    rev,eb,pat=metrics["revenue"],metrics["ebitda"],metrics["pat"]
    mc=eb["current"]/rev["current"]*100 if eb["current"] is not None and rev["current"] not in (None,0) else None
    mp=eb["previous"]/rev["previous"]*100 if eb["previous"] is not None and rev["previous"] not in (None,0) else None
    my=eb["yoy"]/rev["yoy"]*100 if eb["yoy"] is not None and rev["yoy"] not in (None,0) else None
    warnings=[]
    for lab,m in [("REVENUE",rev),("EBITDA",eb),("PAT",pat)]:
        if m["current"] is None:warnings.append(f"{lab} could not be extracted.")
        if m.get("qoq") is not None and abs(m["qoq"])>500:warnings.append(f"{lab} has an unusually large QOQ change ({m['qoq']:.0f}%). Period mapping may be unreliable.")
        if m.get("yoypct") is not None and abs(m["yoypct"])>10000:m["yoypct"]=None;warnings.append(f"{lab} YoY growth suppressed because it exceeds 10,000%.")
    if len(centers)!=4:warnings.append(f"Only {len(centers)} recurring financial numeric columns were detected; four-period mapping may be incomplete.")
    diagnostics=[{"page":p["idx"]+1,"score":p["score"],"labels":p["labels"],"numeric_heavy_rows":p["heavy"],"standalone":p["standalone"],"consolidated":p["consolidated"]} for p in prof]
    name=company_name(doc);doc.close()
    return {"company":name,"quarter":quarter,"basis":actual,"start":start+1,"end":end,"unit":unit,"period_column_centers":centers,**metrics,"margin_current":mc,"margin_previous":mp,"margin_yoy":my,"warnings":warnings,"diagnostic_rows":rows,"page_diagnostics":diagnostics}

def change(v):return "NA" if v is None else f"{'UP' if v>=0 else 'DOWN'} {abs(v):.0f}%"
def pct(v):return "NA" if v is None else f"{v:.1f}%"
def result_text(r):
    return "\n\n".join([f"{r['company']} {r['quarter']} :",
        "REVENUE NA" if r["revenue"]["current"] is None else f"REVENUE {change(r['revenue']['yoypct'])} AT ₹{r['revenue']['current']:,.1f} CR (YOY), {change(r['revenue']['qoq'])} (QOQ)",
        "EBITDA NA" if r["ebitda"]["current"] is None else f"EBITDA {change(r['ebitda']['yoypct'])} AT ₹{r['ebitda']['current']:,.1f} CR (YOY), {change(r['ebitda']['qoq'])} (QOQ)",
        f"MARGINS {pct(r['margin_current'])} V {pct(r['margin_yoy'])} (YOY), {pct(r['margin_previous'])} (QOQ)",
        "CONS NET PROFIT NA" if r["pat"]["current"] is None else f"CONS NET PROFIT {change(r['pat']['yoypct'])} AT ₹{r['pat']['current']:,.1f} CR (YOY), {change(r['pat']['qoq'])} (QOQ)"])

with st.sidebar:
    quarter=st.selectbox("Quarter",["Q1","Q2","Q3","Q4"])
    basis=st.radio("Results basis",["AUTO","CONSOLIDATED","STANDALONE"])
    force_ocr=st.checkbox("Force OCR",False)
    show=st.checkbox("Show reconstructed table",True)

url=st.text_input("Direct PDF link",placeholder="https://nsearchives.nseindia.com/...pdf")
upload=st.file_uploader("Or upload PDF",type=["pdf"])
if st.button("ANALYSE",type="primary",width="stretch"):
    try:
        data=upload.getvalue() if upload else get_pdf(url.strip()) if url.strip() else None
        if data is None:st.error("Paste a direct PDF link or upload a PDF.");st.stop()
        r=analyse(data,basis,force_ocr,quarter)
        st.success(f"Selected {r['basis']} financial-results table — PDF pages {r['start']}–{r['end']}")
        st.subheader("Results");st.code(result_text(r))
        calc=[{"Metric":lab,"Current ₹cr":m["current"],"Previous Q ₹cr":m["previous"],"YoY ₹cr":m["yoy"],"QoQ %":m["qoq"],"YoY %":m["yoypct"],"Confidence":m["confidence"]} for lab,m in [("Revenue",r["revenue"]),("EBITDA",r["ebitda"]),("Net Profit",r["pat"])]]
        st.subheader("Calculation details");st.dataframe(pd.DataFrame(calc),width="stretch",hide_index=True)
        if r["warnings"]:
            with st.expander("⚠ Review warnings",True):
                for w in r["warnings"]:st.warning(w)
        with st.expander("🔎 Page & column diagnostics",True):
            st.write("Company:",r["company"]);st.write("Basis:",r["basis"]);st.write("Pages:",f"{r['start']}–{r['end']}");st.write("Unit:",r["unit"])
            st.write("Detected financial numeric column X-centers:",r["period_column_centers"])
            st.dataframe(pd.DataFrame(r["page_diagnostics"]),width="stretch",hide_index=True)
            for label,m in [("REVENUE",r["revenue"]),("FINANCE COST",r["finance_cost"]),("DEPRECIATION",r["depreciation"]),("PBT",r["pbt"]),("PAT",r["pat"]),("EBITDA",r["ebitda"])]:
                st.markdown(f"**{label} — {m['confidence']}**");st.code(m["source"] or "Not found")
                st.caption("Mapped tokens: "+" | ".join(str(x) for x in m.get("mapped",[])))
                st.caption("Mapped X: "+" | ".join("NA" if x is None else f"{x:.1f}" for x in m.get("mapped_x",[])))
        if show:
            st.subheader("🧩 Reconstructed table")
            view=[{"page":q["page"],"y":round(q["y"],1),"text":q["text"],"numbers":" | ".join(f"{n['token']}@x{n['x']:.1f} [{n['confidence']}]" for n in q["numbers"])} for q in r["diagnostic_rows"]]
            st.dataframe(pd.DataFrame(view),width="stretch",hide_index=True)
        st.download_button("Download JSON",json.dumps(r,indent=2,default=str),file_name="results_analysis_v5_4.json",mime="application/json")
    except Exception as e:
        st.error(f"Analysis failed: {e}");st.exception(e)
