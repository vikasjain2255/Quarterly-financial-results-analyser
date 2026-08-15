import re, json, urllib.request
from pathlib import Path
import streamlit as st
import pandas as pd
import pymupdf

st.set_page_config(page_title="Quarterly Results Analyser V5.3", page_icon="📊", layout="wide")
st.title("📊 Quarterly Results Analyser V5.3")
st.caption("Coordinate-based financial table reconstruction • safer period mapping • Consolidated/Standalone aware")

PAT = {
 "revenue":[r"total\s+revenue\s+from\s+operations",r"revenue\s+from\s+operations",
            r"revenue\s+from\s+contracts?\s+with\s+customers",r"total\s+revenue",r"net\s+sales",r"total\s+sales",r"turnover"],
 "other_income":[r"other\s+income"],
 "finance_cost":[r"finance\s+costs?",r"finance\s+cost",r"interest\s+and\s+finance\s+costs?",r"interest\s+costs?"],
 "depreciation":[r"depreciation\s*(?:and|&|/)\s*amortisation",r"depreciation\s+and\s+amortization",r"depreciation"],
 "pbt":[r"profit\s*/?\s*\(?loss\)?\s+before\s+tax",r"profit\s+before\s+tax",r"\bpbt\b"],
 "pat":[r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+period",r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+year",
        r"profit\s*/?\s*\(?loss\)?\s+after\s+tax",r"net\s+profit\s+after\s+tax",r"net\s+profit",
        r"profit\s+attributable\s+to.*owners"],
 "ebitda":[r"\bebitda\b",r"earnings\s+before\s+interest.*depreciation.*amort"]
}
NUMTOKEN = re.compile(r"(?<![\w.])[\(\[\{]?[0-9OoIl|SsBbGgZzQq][0-9OoIl|SsBbGgZzQq,._-]*[\)\]\}]?")
OCR_FIX = str.maketrans({"O":"0","o":"0","I":"1","l":"1","|":"1","S":"5","s":"5","B":"8","G":"6","g":"6","Z":"2","z":"2","q":"9"})

def norm(s):
    return re.sub(r"\s+"," ",str(s).replace("\u00a0"," ").replace("–","-").replace("—","-")).strip()

def clean_num(x):
    x=str(x).strip().replace(",","").replace(" ","").replace("_",".")
    if x.lower() in {"","-","—","–","na","n/a","nil"}: return None
    neg=x.startswith(("(","[","{")) and x.endswith((")","]","}"))
    y=x.strip("()[]{}").translate(OCR_FIX)
    y=re.sub(r"[^0-9.\-]","",y)
    if not y:return None
    if y.count(".")>1:
        p=y.split("."); y=p[0]+"."+''.join(p[1:])
    try:
        v=float(y)
        return -v if neg else v
    except:return None

def growth(a,b):
    return None if a is None or b in (None,0) else (a/b-1)*100

def get_pdf(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/pdf,*/*"})
    with urllib.request.urlopen(req,timeout=90) as r:data=r.read()
    if not data.startswith(b"%PDF"): raise ValueError("URL did not return a PDF.")
    return data

def page_text(page,ocr=False):
    s=page.get_text("text",sort=True)
    if ocr and len(s.strip())<80:
        try:s=page.get_text("text",textpage=page.get_textpage_ocr(),sort=True)
        except:pass
    return s

def unit_factor(s):
    l=s.lower()
    if re.search(r"\b(?:in|₹)\s*lakhs?\b|\bfigures\s+in\s+lakhs?\b",l):return .01,"₹ crore (converted from ₹ lakh)"
    if re.search(r"\b(?:in|₹)\s*millions?\b|\bfigures\s+in\s+millions?\b",l):return .1,"₹ crore (converted from ₹ million)"
    return 1,"₹ crore"

def row_words(page):
    words=page.get_text("words",sort=True)
    rows=[]
    for w in words:
        x0,y0,x1,y1,t,*_=w
        cy=(y0+y1)/2
        found=None
        for r in rows:
            if abs(r["y"]-cy)<=2.8:
                found=r;break
        if found:
            found["items"].append({"x":x0,"x1":x1,"text":t})
            found["y"]=(found["y"]+cy)/2
        else:
            rows.append({"y":cy,"items":[{"x":x0,"x1":x1,"text":t}]})
    for r in rows:
        r["items"].sort(key=lambda z:z["x"])
        r["text"]=norm(" ".join(x["text"] for x in r["items"]))
    return sorted(rows,key=lambda z:z["y"])

def parse_token(tok):
    return clean_num(tok)

def row_numbers(row):
    vals=[]
    for it in row["items"]:
        # Strip punctuation only after preserving token boundaries.
        v=parse_token(it["text"])
        if v is not None:
            vals.append({"value":v,"x":it["x"],"x1":it["x1"],"token":it["text"]})
    return vals

def numeric_rows(rows):
    out=[]
    for r in rows:
        r2=dict(r);r2["numbers"]=row_numbers(r)
        out.append(r2)
    return out

def profile(page,idx):
    s=page_text(page); l=s.lower(); rows=numeric_rows(row_words(page))
    labels=sum(any(re.search(p,l,re.I) for p in PAT[k]) for k in ["revenue","finance_cost","depreciation","pbt","pat"])
    heavy=sum(len(r["numbers"])>=3 for r in rows)
    audit=-140 if re.search(r"independent\s+auditor|limited\s+review",l) else 0
    notes=-120 if re.search(r"\bnotes\s+to\s+(?:the\s+)?financial|accounting\s+policies|basis\s+of\s+preparation",l) else 0
    narrative=-100 if re.search(r"consolidated\s+results.*include|results.*include.*subsidiar",l) and labels<2 else 0
    standalone=sum(bool(re.search(p,l)) for p in [r"unaudited\s+standalone",r"standalone\s+financial\s+results",r"standalone\s+statement"])
    consolidated=sum(bool(re.search(p,l)) for p in [r"unaudited\s+consolidated",r"consolidated\s+financial\s+results",r"consolidated\s+statement"])
    score=labels*30+min(heavy,8)*20+audit+notes+narrative
    return {"idx":idx,"text":s,"rows":rows,"labels":labels,"heavy":heavy,"audit":audit,"notes":notes,
            "narrative":narrative,"standalone":standalone,"consolidated":consolidated,"score":score}

def choose_pages(doc,basis):
    prof=[profile(p,i) for i,p in enumerate(doc)]
    cand=[p for p in prof if p["labels"]>=2 or p["heavy"]>=2]
    if not cand:raise ValueError("No financial-results table candidate could be identified.")
    def score(p,b):
        explicit=120 if p[b.lower()]>0 else 0
        opposite=-80 if p["standalone" if b=="CONSOLIDATED" else "consolidated"]>0 else 0
        return p["score"]+explicit+opposite
    if basis=="AUTO":
        c=max(cand,key=lambda p:score(p,"CONSOLIDATED")); s=max(cand,key=lambda p:score(p,"STANDALONE"))
        if score(c,"CONSOLIDATED")>=score(s,"STANDALONE"):actual="CONSOLIDATED";chosen=c
        else:actual="STANDALONE";chosen=s
    else:
        actual=basis;chosen=max(cand,key=lambda p:score(p,basis))
    start=chosen["idx"];end=start+1
    # Keep only adjacent pages that look like a continuation of a financial table.
    for i in range(start+1,min(len(doc),start+3)):
        q=prof[i]
        if q["audit"]<0 or q["notes"]<0:break
        if q["labels"]>=1 or q["heavy"]>=2:end=i+1
        else:break
    return actual,start,end,prof

def detect_column_centers(rows):
    # Candidate numeric x positions. Financial tables generally reuse the same
    # column centers for every line. Cluster x by a tolerance and use medians.
    xs=[]
    for r in rows:
        for n in r["numbers"]:
            xs.append(n["x"])
    if not xs:return []
    xs=sorted(xs)
    clusters=[]
    for x in xs:
        if not clusters or x-clusters[-1][-1]>18:
            clusters.append([x])
        else:clusters[-1].append(x)
    centers=[sum(c)/len(c) for c in clusters if len(c)>=2]
    return centers

def map_to_columns(row,centers,maxdist=35):
    mapped=[None]*len(centers)
    for n in row["numbers"]:
        if not centers:continue
        j=min(range(len(centers)),key=lambda k:abs(n["x"]-centers[k]))
        if abs(n["x"]-centers[j])<=maxdist:
            if mapped[j] is None or abs(n["x"]-centers[j])<abs(mapped[j]["x"]-centers[j]):
                mapped[j]={"value":n["value"],"x":n["x"],"token":n["token"]}
    return mapped

def infer_period_columns(rows):
    """
    Find a 3/4-column numeric pattern by x positions.
    We deliberately require repeated support across multiple rows, rather than
    trusting the first row's number order.
    """
    centers=detect_column_centers(rows)
    if len(centers)>8:centers=centers[-8:]
    support=[]
    for c in centers:
        support.append(sum(1 for r in rows if any(abs(n["x"]-c)<=25 for n in r["numbers"])))
    # Financial period columns tend to be the rightmost 3-4 repeated numeric columns.
    candidates=[(i,c) for i,c in enumerate(centers) if support[i]>=2]
    if len(candidates)>=4:return [c for _,c in candidates[-4:]]
    if len(candidates)>=3:return [c for _,c in candidates[-3:]]
    return centers[-4:]

def find_metric_rows(rows,metric):
    out=[]
    for i,r in enumerate(rows):
        rank=-1
        for k,p in enumerate(PAT[metric]):
            if re.search(p,r["text"],re.I):rank=max(rank,300-k*25)
        if rank>=0:out.append((rank,i,r))
    return sorted(out,key=lambda x:(x[0],-len(x[2]["numbers"])),reverse=True)

def extract_metric(rows,metric,centers,factor):
    candidates=find_metric_rows(rows,metric)
    if not candidates:return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[],"mapped":[]}
    # Try the best textual match, then nearby duplicate candidates.
    for _,i,r in candidates:
        mapped=map_to_columns(r,centers)
        vals=[m["value"]*factor if m else None for m in mapped]
        non=sum(v is not None for v in vals)
        if non>=3:
            return {"current":vals[0],"previous":vals[1],"yoy":vals[2],"qoq":growth(vals[0],vals[1]),
                    "yoypct":growth(vals[0],vals[2]),"confidence":"HIGH","source":r["text"],
                    "raw":vals,"mapped":[m["token"] if m else None for m in mapped]}
    # fallback: if exactly 3/4 numeric tokens, retain their order but mark medium.
    for _,i,r in candidates:
        ns=r["numbers"]
        if len(ns)>=3:
            vals=[n["value"]*factor for n in ns[-4:]]
            if len(vals)==3:vals=[vals[0],vals[1],vals[2]]
            return {"current":vals[0],"previous":vals[1] if len(vals)>1 else None,"yoy":vals[2] if len(vals)>2 else None,
                    "qoq":growth(vals[0],vals[1] if len(vals)>1 else None),
                    "yoypct":growth(vals[0],vals[2] if len(vals)>2 else None),
                    "confidence":"MEDIUM","source":r["text"],"raw":vals,"mapped":[n["token"] for n in ns[-4:]]}
    return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[],"mapped":[]}

def derive_ebitda(metrics):
    req=["pbt","finance_cost","depreciation","other_income"]
    if any(metrics[k]["current"] is None for k in req):
        return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[],"mapped":[]}
    n=min(len(metrics[k]["raw"]) for k in req)
    if n<3:return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[],"mapped":[]}
    vals=[metrics["pbt"]["raw"][i]+metrics["finance_cost"]["raw"][i]+metrics["depreciation"]["raw"][i]-metrics["other_income"]["raw"][i] for i in range(min(n,4))]
    return {"current":vals[0],"previous":vals[1],"yoy":vals[2],
            "qoq":growth(vals[0],vals[1]),"yoypct":growth(vals[0],vals[2]),
            "confidence":"DERIVED","source":"PBT + Finance Costs + Depreciation - Other Income","raw":vals,"mapped":[]}

def company_name(doc):
    first="\n".join(page_text(doc[i]) for i in range(min(2,len(doc))))
    for line in first.splitlines():
        s=norm(line)
        if 5<=len(s)<=140 and re.search(r"\b(LIMITED|LTD\.?|INDIA|INDUSTRIES|BANK|FINANCE|STEEL|CEMENT|PHARMA|FOODS|INVESTMENTS|TUBE)\b",s,re.I):
            if not re.search(r"financial results|auditor|registered office|notes to|profit before|finance costs",s,re.I):
                return s.upper()
    return "COMPANY"

def analyse(data,basis,ocr):
    doc=pymupdf.open(stream=data,filetype="pdf")
    actual,start,end,prof=choose_pages(doc,basis)
    rows=[]
    for i in range(start,end):
        rs=prof[i]["rows"]
        if ocr:
            try:
                tp=doc[i].get_textpage_ocr()
                words=doc[i].get_text("words",textpage=tp,sort=True)
                # rebuild OCR rows using the same coordinate grouping
                tmp=[]
                for w in words:
                    x0,y0,x1,y1,t,*_=w;cy=(y0+y1)/2
                    rr=next((r for r in tmp if abs(r["y"]-cy)<=3),None)
                    if rr:rr["items"].append({"x":x0,"x1":x1,"text":t})
                    else:tmp.append({"y":cy,"items":[{"x":x0,"x1":x1,"text":t}]})
                for r in tmp:
                    r["items"].sort(key=lambda z:z["x"]);r["text"]=norm(" ".join(x["text"] for x in r["items"]))
                rs=numeric_rows(tmp)
            except:pass
        for r in rs:
            q=dict(r);q["page"]=i+1;rows.append(q)
    section="\n".join(prof[i]["text"] for i in range(start,end))
    factor,unit=unit_factor(section)
    # Determine shared financial columns using all selected table rows.
    centers=infer_period_columns(rows)
    metrics={k:extract_metric(rows,k,centers,factor) for k in ["revenue","other_income","finance_cost","depreciation","pbt","pat","ebitda"]}
    if metrics["ebitda"]["current"] is None:metrics["ebitda"]=derive_ebitda(metrics)
    rev,eb,pat=metrics["revenue"],metrics["ebitda"],metrics["pat"]
    mc=eb["current"]/rev["current"]*100 if eb["current"] is not None and rev["current"] not in (None,0) else None
    mp=eb["previous"]/rev["previous"]*100 if eb["previous"] is not None and rev["previous"] not in (None,0) else None
    my=eb["yoy"]/rev["yoy"]*100 if eb["yoy"] is not None and rev["yoy"] not in (None,0) else None
    warnings=[]
    for lab,m in [("REVENUE",rev),("EBITDA",eb),("PAT",pat)]:
        if m["current"] is None:warnings.append(f"{lab} could not be extracted.")
        for k in ["qoq","yoypct"]:
            if m.get(k) is not None and abs(m[k])>500:warnings.append(f"{lab} has an unusually large {k.upper()} change ({m[k]:.0f}%). Period mapping may be unreliable.")
    # Hard safety: do not publish absurd growth if row mapping is suspicious.
    for lab,m in [("REVENUE",rev),("EBITDA",eb),("PAT",pat)]:
        if m.get("yoypct") is not None and abs(m["yoypct"])>10000:
            warnings.append(f"{lab} YoY growth suppressed because it exceeds 10,000%; source period mapping needs review.")
            m["yoypct"]=None
    diagnostics=[]
    for p in prof:
        diagnostics.append({"page":p["idx"]+1,"score":p["score"],"labels":p["labels"],"numeric_heavy_rows":p["heavy"],
                            "standalone":p["standalone"],"consolidated":p["consolidated"]})
    doc.close()
    return {"company":company_name(pymupdf.open(stream=data,filetype="pdf")),"quarter":"Q1","basis":actual,
            "start":start+1,"end":end,"unit":unit,"period_column_centers":centers,
            **metrics,"margin_current":mc,"margin_previous":mp,"margin_yoy":my,
            "warnings":warnings,"diagnostic_rows":rows,"page_diagnostics":diagnostics}

def fmt(v):return "NA" if v is None else f"{v:.1f}%"
def change(v):
    if v is None:return "NA"
    return f"{'UP' if v>=0 else 'DOWN'} {abs(v):.0f}%"

def result_text(r):
    return "\n\n".join([
      f"{r['company']} {r['quarter']} :",
      "REVENUE NA" if r["revenue"]["current"] is None else f"REVENUE {change(r['revenue']['yoypct'])} AT ₹{r['revenue']['current']:,.1f} CR (YOY), {change(r['revenue']['qoq'])} (QOQ)",
      "EBITDA NA" if r["ebitda"]["current"] is None else f"EBITDA {change(r['ebitda']['yoypct'])} AT ₹{r['ebitda']['current']:,.1f} CR (YOY), {change(r['ebitda']['qoq'])} (QOQ)",
      f"MARGINS {fmt(r['margin_current'])} V {fmt(r['margin_yoy'])} (YOY), {fmt(r['margin_previous'])} (QOQ)",
      "CONS NET PROFIT NA" if r["pat"]["current"] is None else f"CONS NET PROFIT {change(r['pat']['yoypct'])} AT ₹{r['pat']['current']:,.1f} CR (YOY), {change(r['pat']['qoq'])} (QOQ)"
    ])

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
        with st.spinner("Selecting the financial table and reconstructing numeric columns..."):
            r=analyse(data,basis,force_ocr)
        st.success(f"Selected {r['basis']} financial-results table — PDF pages {r['start']}–{r['end']}")
        st.subheader("Results");st.code(result_text(r))
        calc=[]
        for label,m in [("Revenue",r["revenue"]),("EBITDA",r["ebitda"]),("Net Profit",r["pat"])]:
            calc.append({"Metric":label,"Current ₹cr":m["current"],"Previous Q ₹cr":m["previous"],"YoY ₹cr":m["yoy"],
                         "QoQ %":m["qoq"],"YoY %":m["yoypct"],"Confidence":m["confidence"]})
        st.subheader("Calculation details");st.dataframe(pd.DataFrame(calc),width="stretch",hide_index=True)
        if r["warnings"]:
            with st.expander("⚠ Review warnings",True):
                for w in r["warnings"]:st.warning(w)
        with st.expander("🔎 Page & column diagnostics",True):
            st.write("Company:",r["company"]);st.write("Basis:",r["basis"]);st.write("Pages:",f"{r['start']}–{r['end']}");st.write("Unit:",r["unit"])
            st.write("Detected numeric column X-centers:",r["period_column_centers"])
            st.dataframe(pd.DataFrame(r["page_diagnostics"]),width="stretch",hide_index=True)
            for label,m in [("REVENUE",r["revenue"]),("FINANCE COST",r["finance_cost"]),("DEPRECIATION",r["depreciation"]),("PBT",r["pbt"]),("PAT",r["pat"]),("EBITDA",r["ebitda"])]:
                st.markdown(f"**{label} — {m['confidence']}**")
                st.code(m["source"] or "Not found")
                if m.get("mapped"):st.caption("Mapped tokens: "+" | ".join(str(x) for x in m["mapped"]))
        if show:
            st.subheader("🧩 Reconstructed table")
            view=[]
            for q in r["diagnostic_rows"]:
                view.append({"page":q["page"],"y":q["y"],"text":q["text"],
                             "numbers":" | ".join(f"{n['token']}@x{n['x']:.1f}" for n in q["numbers"])})
            st.dataframe(pd.DataFrame(view),width="stretch",hide_index=True)
        st.download_button("Download JSON",json.dumps(r,indent=2,default=str),file_name="results_analysis_v5_3.json",mime="application/json")
    except Exception as e:
        st.error(f"Analysis failed: {e}")
        st.exception(e)
