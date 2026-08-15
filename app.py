import re, json, urllib.request, math
from pathlib import Path
import streamlit as st
import pandas as pd
import pymupdf

st.set_page_config(page_title="Quarterly Results Analyser V5.1", page_icon="📊", layout="wide")
st.title("📊 Quarterly Results Analyser V5.1")
st.caption("Table-aware extraction • Consolidated/Standalone control • OCR fallback • validation diagnostics")

# ----------------------------
# Helpers
# ----------------------------
ALIASES = {
    "revenue": [
        r"total\s+revenue\s+from\s+operations", r"revenue\s+from\s+operations",
        r"revenue\s+from\s+contract[s]?\s+with\s+customers", r"total\s+revenue",
        r"net\s+sales", r"total\s+sales", r"turnover"
    ],
    "other_income": [r"other\s+income"],
    "finance_cost": [r"finance\s+costs?", r"finance\s+cost", r"interest\s+and\s+finance\s+costs?", r"interest\s+costs?"],
    "depreciation": [
        r"depreciation\s*(?:and|&|/)\s*amortisation", r"depreciation\s+and\s+amortization",
        r"depreciation"
    ],
    "pbt": [
        r"profit\s*/?\s*\(?loss\)?\s+before\s+tax",
        r"profit\s+before\s+exceptional\s+items?.*tax", r"profit\s+before\s+tax", r"\bpbt\b"
    ],
    "pat": [
        r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+period",
        r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+year",
        r"total\s+profit\s*/?\s*\(?loss\)?",
        r"net\s+profit\s+after\s+tax", r"net\s+profit",
        r"profit\s+attributable\s+to.*owners"
    ],
    "ebitda": [r"\bebitda\b", r"earnings\s+before\s+interest.*depreciation.*amort"]
}
NUM = re.compile(r"(?<![\w.])\(?-?\d[\d,]*(?:\.\d+)?\)?(?![\w.])")

OCR_FIX = str.maketrans({
    "O":"0","o":"0","I":"1","l":"1","|":"1","S":"5","s":"5",
    "B":"8","G":"6","g":"6","Z":"2","z":"2","q":"9"
})

def norm(s):
    return re.sub(r"\s+"," ",str(s).replace("\u00a0"," ").replace("–","-").replace("—","-")).strip()

def to_num(x, repair=False):
    x=str(x).strip().replace(",","").replace(" ","")
    if x.lower() in {"","-","—","–","na","n/a","nil"}: return None
    neg=x.startswith("(") and x.endswith(")")
    y=x.strip("()")
    try: return -float(y) if neg else float(y)
    except:
        if repair:
            y=y.translate(OCR_FIX)
            y=re.sub(r"[^0-9.\-]","",y)
            if y.count(".")>1:
                parts=y.split("."); y=parts[0]+"."+''.join(parts[1:])
            try: return -float(y) if neg else float(y)
            except: return None
    return None

def nums(s, repair=False):
    out=[]
    for m in NUM.finditer(s):
        v=to_num(m.group(0),repair)
        if v is not None: out.append(v)
    return out

def growth(a,b):
    return None if a is None or b in (None,0) else (a/b-1)*100

def get_pdf(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"application/pdf,*/*"})
    with urllib.request.urlopen(req,timeout=90) as r: data=r.read()
    if not data.startswith(b"%PDF"): raise ValueError("URL did not return a PDF.")
    return data

def page_text(page, ocr=False):
    txt=page.get_text("text", sort=True)
    if ocr and len(txt.strip()) < 80:
        try: txt=page.get_text("text", textpage=page.get_textpage_ocr(), sort=True)
        except: pass
    return txt

def detect_unit(text):
    l=text.lower()
    if re.search(r"\b(?:in|₹)\s*lakhs?\b|\bfigures\s+in\s+lakhs?\b",l): return .01,"₹ crore (converted from ₹ lakh)"
    if re.search(r"\b(?:in|₹)\s*millions?\b|\bfigures\s+in\s+millions?\b",l): return .1,"₹ crore (converted from ₹ million)"
    if re.search(r"\b(?:in|₹)\s*crores?\b|\bfigures\s+in\s+crores?\b",l): return 1,"₹ crore"
    return 1,"₹ crore (assumed)"

def basis_score(text, basis):
    l=text.lower()
    if basis=="CONSOLIDATED":
        pats=[r"unaudited\s+consolidated",r"consolidated\s+financial\s+results",r"consolidated\s+statement"]
    else:
        pats=[r"unaudited\s+standalone",r"standalone\s+financial\s+results",r"standalone\s+statement"]
    return sum(len(re.findall(p,l)) for p in pats)*100

def page_features(page_text_value):
    l=page_text_value.lower()
    return {
        "consolidated": basis_score(page_text_value,"CONSOLIDATED"),
        "standalone": basis_score(page_text_value,"STANDALONE"),
        "financial_results": 80 if re.search(r"financial\s+results|statement\s+of\s+profit|profit\s+and\s+loss",l) else 0,
        "revenue": 60 if any(re.search(p,l) for p in ALIASES["revenue"]) else 0,
        "pat": 50 if any(re.search(p,l) for p in ALIASES["pat"]) else 0,
        "pbt": 40 if any(re.search(p,l) for p in ALIASES["pbt"]) else 0
    }

def select_pages(doc,basis,quarter):
    pages=[]
    for i,p in enumerate(doc):
        txt=page_text(p)
        f=page_features(txt)
        pages.append((i,txt,f))
    # Find explicit basis anchor pages first.
    explicit=[]
    for i,txt,f in pages:
        if basis=="AUTO":
            bs=max(f["consolidated"],f["standalone"])
        else:
            bs=f[basis.lower()]
        if bs>0:
            explicit.append((i,bs,f))
    if basis=="AUTO":
        # Prefer consolidated when a document contains a consolidated statement, otherwise standalone.
        chosen_basis="CONSOLIDATED" if any(x[2]["consolidated"]>0 for x in explicit) else "STANDALONE"
    else:
        chosen_basis=basis
    anchors=[x for x in explicit if x[2][chosen_basis.lower()]>0]
    if not anchors:
        raise ValueError(f"{chosen_basis.title()} section could not be located.")
    anchor=max(anchors,key=lambda x:x[1])[0]
    # Financial table is normally anchor page or within next 3 pages. Score each candidate.
    candidates=[]
    for i in range(anchor,min(len(doc),anchor+5)):
        txt=pages[i][1]; f=pages[i][2]
        score=f[chosen_basis.lower()]+f["financial_results"]+f["revenue"]+f["pat"]+f["pbt"]
        # penalise auditor/notes pages
        if re.search(r"independent\s+auditor|limited\s+review|notes\s+to\s+financial",txt,re.I): score-=100
        candidates.append((score,i,txt))
    best=max(candidates,key=lambda x:x[0])[1]
    # Include adjacent table pages until a strong non-table/auditor page appears.
    end=best+1
    for i in range(best+1,min(len(doc),best+4)):
        txt=pages[i][1]
        if re.search(r"independent\s+auditor|limited\s+review",txt,re.I) and not re.search(r"revenue|profit|finance\s+cost|depreciation",txt,re.I):
            break
        if len(nums(txt,repair=True))>=6 or re.search(r"profit|revenue|depreciation|finance\s+cost",txt,re.I):
            end=i+1
        else: break
    return chosen_basis,best,end,pages

def extract_rows(page):
    # First try native word coordinates. Each y-band becomes a visual row.
    words=page.get_text("words",sort=True)
    bands={}
    for w in words:
        x0,y0,x1,y1,text,*_=w
        key=round((y0+y1)/2,1)
        bands.setdefault(key,[]).append((x0,text))
    rows=[]
    for y,items in sorted(bands.items()):
        text=norm(" ".join(t for _,t in sorted(items)))
        rows.append({"y":y,"text":text,"nums":nums(text),"repair_nums":nums(text,True),"source":"native"})
    # If native extraction is sparse/corrupt, OCR page rows.
    if len(rows)<8 or sum(len(r["repair_nums"])>=3 for r in rows)<2:
        try:
            tp=page.get_textpage_ocr()
            owords=page.get_text("words",textpage=tp,sort=True)
            ob={}
            for w in owords:
                x0,y0,x1,y1,text,*_=w
                ob.setdefault(round((y0+y1)/2,1),[]).append((x0,text))
            orows=[]
            for y,items in sorted(ob.items()):
                text=norm(" ".join(t for _,t in sorted(items)))
                orows.append({"y":y,"text":text,"nums":nums(text),"repair_nums":nums(text,True),"source":"ocr"})
            if sum(len(r["repair_nums"])>=3 for r in orows) >= sum(len(r["repair_nums"])>=3 for r in rows):
                rows=orows
        except Exception:
            pass
    return rows

def metric_match(text,metric):
    scores=[]
    for rank,p in enumerate(ALIASES[metric]):
        if re.search(p,text,re.I):
            scores.append(250-rank*25)
    return max(scores) if scores else -1

def extract_metric(rows,metric,factor):
    candidates=[]
    for i,r in enumerate(rows):
        ms=metric_match(r["text"],metric)
        if ms<0: continue
        # Prefer same-row 3/4 numeric columns.
        vals=r["repair_nums"] if len(r["repair_nums"])>=3 else r["nums"]
        if len(vals)>=3:
            candidates.append((ms+100,i,r["text"],vals,r["source"]))
        # Combine continuation rows, but only if they are spatially close.
        vals2=list(vals)
        combined=r["text"]
        for j in range(i+1,min(i+5,len(rows))):
            if rows[j]["y"]-r["y"]>45: break
            combined+=" "+rows[j]["text"]
            vals2+=rows[j]["repair_nums"]
            if len(vals2)>=3:
                candidates.append((ms-(j-i)*15,i,combined,vals2[:5],rows[j]["source"]))
                break
    if not candidates:
        return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[]}
    # Prefer 4 values, then 3; reject obviously tiny fragments where better candidates exist.
    candidates.sort(key=lambda x:(len(x[3])>=4,x[0]),reverse=True)
    _,_,source,vals,method=candidates[0]
    vals=vals[:4]
    vals=[v*factor for v in vals]
    cur=vals[0]; prev=vals[1] if len(vals)>1 else None; yoy=vals[2] if len(vals)>2 else None
    return {"current":cur,"previous":prev,"yoy":yoy,"qoq":growth(cur,prev),"yoypct":growth(cur,yoy),
            "confidence":"HIGH" if len(vals)>=4 and method=="native" else ("MEDIUM" if len(vals)>=3 else "LOW"),
            "source":source,"raw":vals}

def derive_ebitda(p):
    # EBITDA = PBT + finance cost + depreciation - other income.
    if any(p[k]["current"] is None for k in ["pbt","finance_cost","depreciation","other_income"]):
        return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[]}
    vals=[]
    n=min(len(p["pbt"]["raw"]),len(p["finance_cost"]["raw"]),len(p["depreciation"]["raw"]),len(p["other_income"]["raw"]),3)
    for i in range(n):
        vals.append(p["pbt"]["raw"][i]+p["finance_cost"]["raw"][i]+p["depreciation"]["raw"][i]-p["other_income"]["raw"][i])
    if len(vals)<3: return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[]}
    return {"current":vals[0],"previous":vals[1],"yoy":vals[2],"qoq":growth(vals[0],vals[1]),"yoypct":growth(vals[0],vals[2]),
            "confidence":"DERIVED","source":"PBT + Finance Costs + Depreciation - Other Income","raw":vals}

def company_name(text):
    for line in text.splitlines()[:40]:
        s=norm(line)
        if 3<len(s)<120 and re.search(r"\b(LIMITED|LTD\.?|INDIA|INDUSTRIES|BANK|FINANCE|STEEL|CEMENT|PHARMA|FOODS|INVESTMENTS)\b",s,re.I):
            if not re.search(r"financial results|finance costs|depreciation|profit before|registered office|independent auditor",s,re.I):
                return s.upper()
    return "COMPANY"

def analyse(data,quarter,basis,use_ocr):
    doc=pymupdf.open(stream=data,filetype="pdf")
    actual,start,end,pages=select_pages(doc,basis,quarter)
    section_text="\n".join(pages[i][1] for i in range(start,end))
    factor,unit=detect_unit(section_text)
    rows=[]
    for i in range(start,end):
        for r in extract_rows(doc[i]):
            r.update(page=i+1)
            rows.append(r)
    p={k:extract_metric(rows,k,factor) for k in ["revenue","other_income","finance_cost","depreciation","pbt","pat","ebitda"]}
    if p["ebitda"]["current"] is None:
        p["ebitda"]=derive_ebitda(p)
    rev,ebitda,pat=p["revenue"],p["ebitda"],p["pat"]
    mc=ebitda["current"]/rev["current"]*100 if ebitda["current"] is not None and rev["current"] not in (None,0) else None
    mp=ebitda["previous"]/rev["previous"]*100 if ebitda["previous"] is not None and rev["previous"] not in (None,0) else None
    my=ebitda["yoy"]/rev["yoy"]*100 if ebitda["yoy"] is not None and rev["yoy"] not in (None,0) else None
    warnings=[]
    for label,m in [("REVENUE",rev),("EBITDA",ebitda),("PAT",pat)]:
        if m["current"] is None: warnings.append(f"{label} could not be extracted.")
    for label,m in [("REVENUE",rev),("EBITDA",ebitda),("PAT",pat)]:
        for key in ["qoq","yoypct"]:
            v=m.get(key)
            if v is not None and abs(v)>500:
                warnings.append(f"{label} has an unusually large {key.upper()} change ({v:.0f}%). Review the source row.")
    name=company_name(section_text)
    doc.close()
    return {"company":name,"quarter":quarter,"basis":actual,"start":start+1,"end":end,"unit":unit,
            "revenue":rev,"ebitda":ebitda,"pat":pat,"margin_current":mc,"margin_previous":mp,"margin_yoy":my,
            "warnings":warnings,"table":rows}

def fmt_change(v):
    if v is None:return "NA"
    return f"{'UP' if v>=0 else 'DOWN'} {abs(v):.0f}%"

def line(label,m):
    if m["current"] is None:return f"{label} NA"
    return f"{label} {fmt_change(m['yoypct'])} AT ₹{m['current']:,.1f} CR (YOY), {fmt_change(m['qoq'])} (QOQ)"

def output_text(r):
    f=lambda x:"NA" if x is None else f"{x:.1f}%"
    return "\n\n".join([
        f"{r['company']} {r['quarter']} :",
        line("REVENUE",r["revenue"]),
        line("EBITDA",r["ebitda"]),
        f"MARGINS {f(r['margin_current'])} V {f(r['margin_yoy'])} (YOY), {f(r['margin_previous'])} (QOQ)",
        line("CONS NET PROFIT" if r["basis"]=="CONSOLIDATED" else "NET PROFIT",r["pat"])
    ])

# ----------------------------
# UI
# ----------------------------
with st.sidebar:
    quarter=st.selectbox("Quarter",["Q1","Q2","Q3","Q4"])
    basis=st.radio("Results basis",["AUTO","CONSOLIDATED","STANDALONE"])
    use_ocr=st.checkbox("Force OCR fallback",value=False)
    show_table=st.checkbox("Show reconstructed table",value=True)

url=st.text_input("Direct PDF link",placeholder="https://nsearchives.nseindia.com/...pdf")
upload=st.file_uploader("Or upload PDF",type=["pdf"])

if st.button("ANALYSE",type="primary",width="stretch"):
    try:
        if upload:data=upload.getvalue()
        elif url.strip():data=get_pdf(url.strip())
        else:st.error("Paste a direct PDF link or upload a PDF.");st.stop()
        with st.spinner("Selecting the requested results table and extracting values..."):
            r=analyse(data,quarter,basis,use_ocr)
        st.success(f"Analysis complete — {r['basis']} results selected (PDF pages {r['start']}–{r['end']})")
        st.subheader("Results");st.code(output_text(r),language="text")
        calc=[]
        for label,m in [("Revenue",r["revenue"]),("EBITDA",r["ebitda"]),("Net Profit",r["pat"])]:
            calc.append({"Metric":label,"Current ₹cr":m["current"],"Previous Q ₹cr":m["previous"],"YoY ₹cr":m["yoy"],"QoQ %":m["qoq"],"YoY %":m["yoypct"],"Confidence":m["confidence"]})
        st.subheader("Calculation details");st.dataframe(pd.DataFrame(calc),width="stretch",hide_index=True)
        if r["warnings"]:
            with st.expander("⚠ Review warnings",expanded=True):
                for w in r["warnings"]:st.warning(w)
        with st.expander("🔎 Extraction diagnostics",expanded=True):
            st.write("Company:",r["company"]);st.write("Basis:",r["basis"]);st.write("Pages:",f"{r['start']}–{r['end']}");st.write("Unit:",r["unit"])
            for label,m in [("REVENUE",r["revenue"]),("EBITDA",r["ebitda"]),("PAT",r["pat"])]:
                st.markdown(f"**{label}**")
                st.write("Confidence:",m["confidence"]);st.write("Extracted values:",m["raw"]);st.code(m["source"] or "Not found")
        if show_table:
            st.subheader("🧩 Reconstructed table")
            st.dataframe(pd.DataFrame(r["table"])[["page","y","text","repair_nums","source"]],width="stretch",hide_index=True)
        # JSON-safe export: omit raw PDF bytes and large document objects.
        export={k:v for k,v in r.items() if k!="table"}
        export["diagnostic_rows"]=r["table"]
        st.download_button("Download JSON",json.dumps(export,indent=2,default=str),file_name="results_analysis_v5_1.json",mime="application/json")
    except Exception as e:
        st.error(f"Analysis failed: {e}")
        st.exception(e)
