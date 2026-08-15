import re, json, urllib.request
from pathlib import Path
import streamlit as st
import pandas as pd
import pymupdf

st.set_page_config(page_title="Quarterly Results Analyser V5.2", page_icon="📊", layout="wide")
st.title("📊 Quarterly Results Analyser V5.2")
st.caption("Financial-table-first extraction • robust Consolidated/Standalone selection • OCR fallback")

# ---------- patterns ----------
PAT = {
 "revenue":[r"total\s+revenue\s+from\s+operations",r"revenue\s+from\s+operations",
            r"revenue\s+from\s+contracts?\s+with\s+customers",r"total\s+revenue",
            r"net\s+sales",r"total\s+sales",r"turnover"],
 "other_income":[r"other\s+income"],
 "finance_cost":[r"finance\s+costs?",r"finance\s+cost",r"interest\s+and\s+finance\s+costs?",r"interest\s+costs?"],
 "depreciation":[r"depreciation\s*(?:and|&|/)\s*amortisation",r"depreciation\s+and\s+amortization",r"depreciation"],
 "pbt":[r"profit\s*/?\s*\(?loss\)?\s+before\s+tax",r"profit\s+before\s+tax",r"\bpbt\b"],
 "pat":[r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+period",r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+year",
        r"total\s+profit\s*/?\s*\(?loss\)?",r"net\s+profit\s+after\s+tax",r"net\s+profit",
        r"profit\s+attributable\s+to.*owners"],
 "ebitda":[r"\bebitda\b",r"earnings\s+before\s+interest.*depreciation.*amort"]
}
NUM = re.compile(r"(?<![\w.])\(?-?\d[\d,]*(?:\.\d+)?\)?(?![\w.])")
OCR_FIX = str.maketrans({"O":"0","o":"0","I":"1","l":"1","|":"1","S":"5","s":"5","B":"8","G":"6","g":"6","Z":"2","z":"2","q":"9"})

def norm(s): return re.sub(r"\s+"," ",str(s).replace("\u00a0"," ").replace("–","-").replace("—","-")).strip()

def num(x, repair=False):
    x=str(x).strip().replace(",","").replace(" ","")
    if x.lower() in {"","-","—","–","na","n/a","nil"}: return None
    neg=x.startswith("(") and x.endswith(")")
    y=x.strip("()")
    try: return -float(y) if neg else float(y)
    except:
        if not repair:return None
        y=y.translate(OCR_FIX); y=re.sub(r"[^0-9.\-]","",y)
        if y.count(".")>1:
            p=y.split("."); y=p[0]+"."+''.join(p[1:])
        try:return -float(y) if neg else float(y)
        except:return None

def nums(s,repair=False):
    return [v for m in NUM.finditer(s) if (v:=num(m.group(0),repair)) is not None]

def growth(a,b): return None if a is None or b in (None,0) else (a/b-1)*100

def get_pdf(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/pdf,*/*"})
    with urllib.request.urlopen(req,timeout=90) as r:data=r.read()
    if not data.startswith(b"%PDF"):raise ValueError("URL did not return a PDF.")
    return data

def text(page,ocr=False):
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

def has_any(s,patterns):return any(re.search(p,s,re.I) for p in patterns)

def visual_rows(page):
    words=page.get_text("words",sort=True)
    # cluster by close y rather than rounding exact floating-point positions
    rows=[]
    for w in words:
        x0,y0,x1,y1,t,*_=w
        cy=(y0+y1)/2
        placed=False
        for row in rows:
            if abs(row["y"]-cy)<=2.5:
                row["items"].append((x0,t)); row["y"]=(row["y"]+cy)/2; placed=True; break
        if not placed: rows.append({"y":cy,"items":[(x0,t)]})
    out=[]
    for r in sorted(rows,key=lambda z:z["y"]):
        s=norm(" ".join(t for _,t in sorted(r["items"])))
        out.append({"y":round(r["y"],1),"text":s,"nums":nums(s),"repair_nums":nums(s,True),"source":"native"})
    return out

def ocr_rows(page):
    tp=page.get_textpage_ocr()
    words=page.get_text("words",textpage=tp,sort=True)
    rows=[]
    for w in words:
        x0,y0,x1,y1,t,*_=w; cy=(y0+y1)/2; placed=False
        for r in rows:
            if abs(r["y"]-cy)<=3:
                r["items"].append((x0,t)); r["y"]=(r["y"]+cy)/2; placed=True; break
        if not placed:rows.append({"y":cy,"items":[(x0,t)]})
    out=[]
    for r in sorted(rows,key=lambda z:z["y"]):
        s=norm(" ".join(t for _,t in sorted(r["items"])))
        out.append({"y":round(r["y"],1),"text":s,"nums":nums(s),"repair_nums":nums(s,True),"source":"ocr"})
    return out

def page_profile(page,idx):
    s=text(page)
    l=s.lower()
    rows=visual_rows(page)
    # Financial-table evidence: multiple distinct P&L labels and multiple numeric-heavy rows.
    labels=sum(has_any(l,PAT[k]) for k in ["revenue","finance_cost","depreciation","pbt","pat"])
    numeric_rows=sum(len(r["repair_nums"])>=3 for r in rows)
    table=labels*30 + min(numeric_rows,8)*20
    audit=-120 if re.search(r"independent\s+auditor|limited\s+review",l) else 0
    notes=-100 if re.search(r"\bnotes\s+to\s+(?:the\s+)?financial|accounting\s+policies|basis\s+of\s+preparation",l) else 0
    # A page that merely says "consolidated results include..." should not win.
    narrative=-80 if re.search(r"consolidated\s+results.*include|results.*include.*subsidiar",l) and labels<2 else 0
    standalone=sum(bool(re.search(p,l)) for p in [r"unaudited\s+standalone",r"standalone\s+financial\s+results",r"standalone\s+statement"])
    consolidated=sum(bool(re.search(p,l)) for p in [r"unaudited\s+consolidated",r"consolidated\s+financial\s+results",r"consolidated\s+statement"])
    return {"idx":idx,"text":s,"rows":rows,"labels":labels,"numeric_rows":numeric_rows,
            "table":table,"audit":audit,"notes":notes,"narrative":narrative,
            "standalone":standalone,"consolidated":consolidated}

def choose_pages(doc,basis):
    prof=[page_profile(p,i) for i,p in enumerate(doc)]
    # Candidate must contain at least 2 P&L labels OR 2 numeric-heavy rows.
    cand=[]
    for p in prof:
        base=p["table"]+p["audit"]+p["notes"]+p["narrative"]
        if p["labels"]>=2 or p["numeric_rows"]>=2:
            cand.append((base,p))
    if not cand: raise ValueError("No financial-results table candidate could be identified.")
    def basis_score(p,b):
        explicit=120 if p[b.lower()]>0 else 0
        opposite= -80 if p["standalone" if b=="CONSOLIDATED" else "consolidated"]>0 else 0
        return explicit+opposite
    if basis=="AUTO":
        # Find best consolidated and standalone table candidates independently.
        cons=max(cand,key=lambda x:x[0]+basis_score(x[1],"CONSOLIDATED"))
        stan=max(cand,key=lambda x:x[0]+basis_score(x[1],"STANDALONE"))
        # If both are plausible, prefer consolidated only when explicit consolidated heading is on/near table page.
        if cons[0]+basis_score(cons[1],"CONSOLIDATED") >= stan[0]+basis_score(stan[1],"STANDALONE"):
            actual="CONSOLIDATED"; chosen=cons[1]
        else: actual="STANDALONE"; chosen=stan[1]
    else:
        actual=basis
        chosen=max(cand,key=lambda x:x[0]+basis_score(x[1],basis))[1]
        # Require explicit heading where possible; otherwise inspect immediate preceding pages.
        if chosen[basis.lower()]==0:
            nearby=[]
            for p in prof:
                if abs(p["idx"]-chosen["idx"])<=2 and p[basis.lower()]>0:
                    nearby.append(p)
            if nearby:
                # keep table page, but use nearby heading as evidence
                chosen=min([chosen]+nearby,key=lambda p: abs(p["idx"]-chosen["idx"]) if p["idx"]==chosen["idx"] else abs(p["idx"]-chosen["idx"]))
    start=chosen["idx"]; end=start+1
    # Include immediately following page if it continues the same P&L table.
    for i in range(start+1,min(len(doc),start+3)):
        q=prof[i]
        if q["audit"]<0 and q["labels"]<2:break
        if q["labels"]>=1 or q["numeric_rows"]>=2:end=i+1
        else:break
    # Include preceding page if heading is there and table begins on chosen page.
    for i in range(max(0,start-2),start):
        q=prof[i]
        if q[basis.lower()]>0 and not (q["audit"]<0 or q["notes"]<0):
            start=i
    return actual,start,end,prof

def metric_row(rows,metric,factor):
    candidates=[]
    for i,r in enumerate(rows):
        score=-1
        for rank,p in enumerate(PAT[metric]):
            if re.search(p,r["text"],re.I):score=max(score,300-rank*25)
        if score<0:continue
        vals=r["repair_nums"] if len(r["repair_nums"])>=3 else r["nums"]
        if len(vals)>=3:
            vals=vals[:4]
            # Reject dates/EPS-like rows.
            if max(abs(v) for v in vals)>1000000:continue
            candidates.append((score+len(vals)*10,i,r,vals))
    if not candidates:return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[]}
    candidates.sort(key=lambda x:x[0],reverse=True)
    _,_,r,vals=candidates[0]
    vals=[v*factor for v in vals]
    return {"current":vals[0],"previous":vals[1] if len(vals)>1 else None,"yoy":vals[2] if len(vals)>2 else None,
            "qoq":growth(vals[0],vals[1] if len(vals)>1 else None),"yoypct":growth(vals[0],vals[2] if len(vals)>2 else None),
            "confidence":"HIGH" if len(vals)>=4 and r["source"]=="native" else "MEDIUM","source":r["text"],"raw":vals}

def derive_ebitda(metrics):
    req=["pbt","finance_cost","depreciation","other_income"]
    if any(metrics[k]["current"] is None for k in req):return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[]}
    n=min(len(metrics[k]["raw"]) for k in req)
    vals=[metrics["pbt"]["raw"][i]+metrics["finance_cost"]["raw"][i]+metrics["depreciation"]["raw"][i]-metrics["other_income"]["raw"][i] for i in range(min(n,4))]
    if len(vals)<3:return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[]}
    return {"current":vals[0],"previous":vals[1],"yoy":vals[2],"qoq":growth(vals[0],vals[1]),"yoypct":growth(vals[0],vals[2]),
            "confidence":"DERIVED","source":"PBT + Finance Costs + Depreciation - Other Income","raw":vals}

def company_from_doc(doc,table_text):
    first="\n".join(text(doc[i]) for i in range(min(2,len(doc))))
    # Prefer prominent all-caps/company-like line on first pages.
    for line in first.splitlines():
        s=norm(line)
        if 5<=len(s)<=140 and re.search(r"\b(LIMITED|LTD\.?|INDIA|INDUSTRIES|BANK|FINANCE|STEEL|CEMENT|PHARMA|FOODS|INVESTMENTS)\b",s,re.I):
            if not re.search(r"financial results|auditor|registered office|notes to|profit before|finance costs",s,re.I):
                return s.upper()
    return "COMPANY"

def analyse(data,basis,ocr):
    doc=pymupdf.open(stream=data,filetype="pdf")
    actual,start,end,prof=choose_pages(doc,basis)
    # OCR only selected pages when requested or native table evidence is weak.
    rows=[]
    for i in range(start,end):
        rs=prof[i]["rows"]
        if ocr or sum(len(r["repair_nums"])>=3 for r in rs)<2:
            try:
                ors=ocr_rows(doc[i])
                if sum(len(r["repair_nums"])>=3 for r in ors)>sum(len(r["repair_nums"])>=3 for r in rs):rs=ors
            except:pass
        for r in rs:
            r=dict(r);r["page"]=i+1;rows.append(r)
    section="\n".join(prof[i]["text"] for i in range(start,end))
    factor,unit=unit_factor(section)
    metrics={k:metric_row(rows,k,factor) for k in ["revenue","other_income","finance_cost","depreciation","pbt","pat","ebitda"]}
    if metrics["ebitda"]["current"] is None:metrics["ebitda"]=derive_ebitda(metrics)
    rev,eb,pat=metrics["revenue"],metrics["ebitda"],metrics["pat"]
    mc=eb["current"]/rev["current"]*100 if eb["current"] is not None and rev["current"] not in (None,0) else None
    mp=eb["previous"]/rev["previous"]*100 if eb["previous"] is not None and rev["previous"] not in (None,0) else None
    my=eb["yoy"]/rev["yoy"]*100 if eb["yoy"] is not None and rev["yoy"] not in (None,0) else None
    warnings=[]
    for lab,m in [("REVENUE",rev),("EBITDA",eb),("PAT",pat)]:
        if m["current"] is None:warnings.append(f"{lab} could not be extracted.")
        for k in ["qoq","yoypct"]:
            if m.get(k) is not None and abs(m[k])>500:warnings.append(f"{lab} has an unusually large {k.upper()} change ({m[k]:.0f}%). Review the source row.")
    name=company_from_doc(doc,section)
    diagnostics=[{"page":p["idx"]+1,"score":p["table"]+p["audit"]+p["notes"]+p["narrative"],
                  "labels":p["labels"],"numeric_rows":p["numeric_rows"],
                  "standalone":p["standalone"],"consolidated":p["consolidated"]} for p in prof]
    doc.close()
    return {"company":name,"quarter":"Q1","basis":actual,"start":start+1,"end":end,"unit":unit,
            **metrics,"margin_current":mc,"margin_previous":mp,"margin_yoy":my,
            "warnings":warnings,"diagnostic_rows":rows,"page_diagnostics":diagnostics}

def change(v):
    if v is None:return "NA"
    return f"{'UP' if v>=0 else 'DOWN'} {abs(v):.0f}%"

def fmt(v):return "NA" if v is None else f"{v:.1f}%"

def result_text(r):
    return "\n\n".join([
      f"{r['company']} {r['quarter']} :",
      "REVENUE NA" if r["revenue"]["current"] is None else f"REVENUE {change(r['revenue']['yoypct'])} AT ₹{r['revenue']['current']:,.1f} CR (YOY), {change(r['revenue']['qoq'])} (QOQ)",
      "EBITDA NA" if r["ebitda"]["current"] is None else f"EBITDA {change(r['ebitda']['yoypct'])} AT ₹{r['ebitda']['current']:,.1f} CR (YOY), {change(r['ebitda']['qoq'])} (QOQ)",
      f"MARGINS {fmt(r['margin_current'])} V {fmt(r['margin_yoy'])} (YOY), {fmt(r['margin_previous'])} (QOQ)",
      "CONS NET PROFIT NA" if r["pat"]["current"] is None else f"CONS NET PROFIT {change(r['pat']['yoypct'])} AT ₹{r['pat']['current']:,.1f} CR (YOY), {change(r['pat']['qoq'])} (QOQ)"
    ])

# ---------- UI ----------
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
        with st.spinner("Finding the actual financial-results table..."):
            r=analyse(data,basis,force_ocr)
        st.success(f"Selected {r['basis']} financial-results table — PDF pages {r['start']}–{r['end']}")
        st.subheader("Results");st.code(result_text(r))
        calc=[]
        for label,m in [("Revenue",r["revenue"]),("EBITDA",r["ebitda"]),("Net Profit",r["pat"])]:
            calc.append({"Metric":label,"Current ₹cr":m["current"],"Previous Q ₹cr":m["previous"],"YoY ₹cr":m["yoy"],"QoQ %":m["qoq"],"YoY %":m["yoypct"],"Confidence":m["confidence"]})
        st.subheader("Calculation details");st.dataframe(pd.DataFrame(calc),width="stretch",hide_index=True)
        if r["warnings"]:
            with st.expander("⚠ Review warnings",True):
                for w in r["warnings"]:st.warning(w)
        with st.expander("🔎 Page-selection diagnostics",True):
            st.write("Company:",r["company"]);st.write("Basis:",r["basis"]);st.write("Pages:",f"{r['start']}–{r['end']}");st.write("Unit:",r["unit"])
            st.dataframe(pd.DataFrame(r["page_diagnostics"]),width="stretch",hide_index=True)
            for label,m in [("REVENUE",r["revenue"]),("EBITDA",r["ebitda"]),("PAT",r["pat"])]:
                st.markdown(f"**{label} — {m['confidence']}**")
                st.code(m["source"] or "Not found")
        if show:
            st.subheader("🧩 Reconstructed table")
            st.dataframe(pd.DataFrame(r["diagnostic_rows"])[["page","y","text","repair_nums","source"]],width="stretch",hide_index=True)
        st.download_button("Download JSON",json.dumps(r,indent=2,default=str),file_name="results_analysis_v5_2.json",mime="application/json")
    except Exception as e:
        st.error(f"Analysis failed: {e}")
        st.exception(e)
