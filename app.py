import re, json, urllib.request
import streamlit as st
import pandas as pd
import fitz

st.set_page_config(page_title="Quarterly Results Analyser V5", page_icon="📊", layout="wide")
st.title("📊 Quarterly Results Analyser V5")
st.caption("Structured quarterly-results parser with table reconstruction, validation and diagnostics.")

ALIASES = {
"revenue":[r"total\s+revenue\s+from\s+operations",r"revenue\s+from\s+operations",r"revenue\s+from\s+contract[s]?\s+with\s+customers",r"total\s+revenue",r"net\s+sales",r"total\s+sales",r"turnover"],
"other_income":[r"other\s+income"],
"finance_cost":[r"finance\s+costs?",r"finance\s+cost",r"interest\s+and\s+finance\s+costs?",r"interest\s+costs?"],
"depreciation":[r"depreciation\s*(?:and|&|/|i)\s*amortisation",r"depreciation\s+and\s+amortization",r"depreciation"],
"pbt":[r"profit\s*/?\s*\(?loss\)?\s+before\s+tax",r"profit\s+before\s+exceptional\s+items?\s+and\s+tax",r"profit\s+before\s+tax",r"\bpbt\b"],
"pat":[r"total\s+profit\s*/?\s*\(?loss\)?",r"profit\s*/?\s*\(?loss\)?\s+after\s+tax",r"profit\s+for\s+the\s+period",r"profit\s+for\s+the\s+year",r"net\s+profit\s+after\s+tax",r"net\s+profit",r"profit\s+attributable\s+to.*owners"],
"ebitda":[r"\bebitda\b",r"earnings\s+before\s+interest.*depreciation.*amort"]
}
NUM=re.compile(r"(?<![\w.])\(?-?\d[\d,]*(?:\.\d+)?\)?(?![\w.])")

def norm(s):
    return re.sub(r"[ \t]+"," ",str(s).replace("\u00a0"," ").replace("\u2013","-").replace("\u2014","-")).strip()

def to_num(x):
    x=str(x).strip().replace(",","").replace(" ","")
    if x.lower() in {"","-","—","–","na","n/a","nil"}: return None
    neg=x.startswith("(") and x.endswith(")")
    try:
        v=float(x.strip("()")); return -v if neg else v
    except: return None

def nums(s):
    out=[]
    for m in NUM.finditer(s):
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

def extract_doc(data,use_ocr=False):
    doc=fitz.open(stream=data,filetype="pdf"); pages=[]
    for pno,p in enumerate(doc):
        text=p.get_text("text",sort=True); words=p.get_text("words",sort=True)
        if use_ocr and len(text.strip())<50:
            try:
                text=p.get_text("text",textpage=p.get_textpage_ocr(),sort=True)
                words=p.get_text("words",sort=True)
            except: pass
        pages.append({"page":pno+1,"text":text,"words":words})
    doc.close(); return pages

def detect_unit(text):
    l=text.lower()
    if re.search(r"\b(?:in|₹)\s*lakhs?\b|\bfigures\s+in\s+lakhs?\b",l): return .01,"₹ crore (converted from ₹ lakh)"
    if re.search(r"\b(?:in|₹)\s*millions?\b|\bfigures\s+in\s+millions?\b",l): return .1,"₹ crore (converted from ₹ million)"
    return 1.0,"₹ crore (assumed)"

def heading_kind(text):
    t=norm(text).lower()
    if re.search(r"(?:unaudited\s+consolidated|consolidated\s+unaudited).*financial\s+results",t): return "CONSOLIDATED"
    if re.search(r"(?:unaudited\s+standalone|standalone\s+unaudited).*financial\s+results",t): return "STANDALONE"
    if re.search(r"consolidated.*financial\s+results|financial\s+results.*consolidated",t): return "CONSOLIDATED"
    if re.search(r"standalone.*financial\s+results|financial\s+results.*standalone",t): return "STANDALONE"
    return None

def find_sections(pages):
    hits=[]
    for i,p in enumerate(pages):
        k=heading_kind(p["text"])
        if k: hits.append((i,k))
    sections=[]
    for n,(idx,k) in enumerate(hits):
        stop=hits[n+1][0] if n+1<len(hits) else min(len(pages),idx+6)
        sections.append({"basis":k,"start":idx,"end":stop})
    return sections

def score_section(sec,pages):
    txt="\n".join(pages[i]["text"] for i in range(sec["start"],sec["end"]))
    score=sum(30 for metric in ("revenue","pbt","pat") if any(re.search(p,txt,re.I) for p in ALIASES[metric]))
    if len(nums(txt))>=10: score+=20
    if re.search(r"(current|corresponding|previous|quarter|year)",txt,re.I): score+=10
    return score,txt

def select_section(pages,basis):
    secs=find_sections(pages)
    candidates=([s for s in secs if s["basis"]=="CONSOLIDATED"] or [s for s in secs if s["basis"]=="STANDALONE"]) if basis=="AUTO" else [s for s in secs if s["basis"]==basis]
    if not candidates: raise ValueError(f"{basis.title()} financial-results section was not found.")
    best=max(candidates,key=lambda s:score_section(s,pages)[0])
    score,txt=score_section(best,pages)
    return txt,best["basis"],best["start"]+1,best["end"],score

def word_rows(page):
    rows={}
    for w in page["words"]:
        x0,y0,x1,y1,text,*_=w
        rows.setdefault(round(y0,1),[]).append((x0,text))
    return [(y," ".join(t for _,t in sorted(items,key=lambda z:z[0]))) for y,items in sorted(rows.items())]

def table_rows(pages,start,end):
    rows=[]
    for i in range(start,end):
        for y,text in word_rows(pages[i]):
            rows.append({"page":pages[i]["page"],"y":y,"text":norm(text),"nums":nums(text)})
    return rows

def metric_label_score(line,metric):
    scores=[]
    for rank,p in enumerate(ALIASES[metric]):
        if re.search(p,line,re.I):
            score=200-rank*25
            if re.search(r"\btotal\b",line,re.I): score+=60
            scores.append(score)
    return max(scores) if scores else -1

def candidate_rows(rows,metric):
    c=[]
    for i,r in enumerate(rows):
        s=metric_label_score(r["text"],metric)
        if s<0: continue
        if len(r["nums"])>=3:
            c.append((s+60,i,r["text"],r["nums"])); continue
        vals=[]; combined=r["text"]
        for j in range(i+1,min(i+7,len(rows))):
            if rows[j]["page"]!=r["page"]: break
            combined+=" "+rows[j]["text"]; vals+=rows[j]["nums"]
            if len(vals)>=3:
                c.append((s-(j-i)*12,i,combined,vals[:4])); break
    return sorted(c,reverse=True)

def parse_metric(rows,metric,factor):
    cands=candidate_rows(rows,metric)
    if not cands:
        return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[]}
    c=next((x for x in cands if len(x[3])>=4),cands[0])
    vals=c[3][:4]; cur,prev,yoy=[v*factor for v in vals[:3]]
    q,y=growth(cur,prev),growth(cur,yoy)
    conf="HIGH" if len(vals)>=4 else "MEDIUM"
    if (q is not None and abs(q)>1000) or (y is not None and abs(y)>1000): conf="LOW"
    return {"current":cur,"previous":prev,"yoy":yoy,"qoq":q,"yoypct":y,"confidence":conf,"source":c[2],"raw":vals}

def derive_ebitda(p):
    keys=["pbt","finance_cost","depreciation","other_income"]
    if not all(p[k]["current"] is not None and len(p[k]["raw"])>=3 for k in keys):
        return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","raw":[]}
    vals=[p["pbt"]["raw"][i]+p["finance_cost"]["raw"][i]+p["depreciation"]["raw"][i]-p["other_income"]["raw"][i] for i in range(3)]
    cur,prev,yoy=vals
    return {"current":cur,"previous":prev,"yoy":yoy,"qoq":growth(cur,prev),"yoypct":growth(cur,yoy),"confidence":"DERIVED","source":"PBT + Finance Costs + Depreciation - Other Income","raw":vals}

def company_name(section):
    for line in section.splitlines()[:50]:
        s=norm(line)
        if 3<len(s)<120 and re.search(r"\b(LIMITED|LTD\.?|INDIA|INDUSTRIES|BANK|FINANCE|STEEL|CEMENT|PHARMA|FOODS|INVESTMENTS)\b",s,re.I) and not re.search(r"financial results|finance costs|depreciation|profit before|registered office",s,re.I):
            return s.upper()
    return "COMPANY"

def analyse(data,quarter,basis,use_ocr):
    pages=extract_doc(data,use_ocr)
    section,actual,start,end,score=select_section(pages,basis)
    factor,unit=detect_unit(section)
    rows=table_rows(pages,start-1,end)
    p={k:parse_metric(rows,k,factor) for k in ["revenue","other_income","finance_cost","depreciation","pbt","pat","ebitda"]}
    if p["ebitda"]["current"] is None: p["ebitda"]=derive_ebitda(p)
    rev,ebitda,pat=p["revenue"],p["ebitda"],p["pat"]
    mc=ebitda["current"]/rev["current"]*100 if ebitda["current"] is not None and rev["current"] not in (None,0) else None
    mp=ebitda["previous"]/rev["previous"]*100 if ebitda["previous"] is not None and rev["previous"] not in (None,0) else None
    my=ebitda["yoy"]/rev["yoy"]*100 if ebitda["yoy"] is not None and rev["yoy"] not in (None,0) else None
    warnings=[f"{x} could not be extracted." for x,m in [("REVENUE",rev),("EBITDA",ebitda),("PAT",pat)] if m["current"] is None]
    return {"company":company_name(section),"quarter":quarter,"basis":actual,"start":start,"end":end,"unit":unit,"section_score":score,"revenue":rev,"ebitda":ebitda,"pat":pat,"margin_current":mc,"margin_previous":mp,"margin_yoy":my,"warnings":warnings,"table":rows}

def line(label,m):
    if m["current"] is None: return f"{label} NA"
    return f"{label} {('UP' if m['yoypct']>=0 else 'DOWN')} {abs(m['yoypct']):.0f}% AT ₹{m['current']:,.1f} CR (YOY), {('UP' if m['qoq']>=0 else 'DOWN')} {abs(m['qoq']):.0f}% (QOQ)"

def output_text(r):
    f=lambda x:"NA" if x is None else f"{x:.1f}%"
    name=re.sub(r"\s+(LIMITED|LTD\.?)$","",r["company"],flags=re.I).strip()
    return "\n\n".join([f"{name} {r['quarter']} :",line("REVENUE",r["revenue"]),line("EBITDA",r["ebitda"]),f"MARGINS {f(r['margin_current'])} V {f(r['margin_yoy'])} (YOY), {f(r['margin_previous'])} (QOQ)",line("CONS NET PROFIT" if r["basis"]=="CONSOLIDATED" else "NET PROFIT",r["pat"])])

with st.sidebar:
    quarter=st.selectbox("Quarter",["Q1","Q2","Q3","Q4"])
    basis=st.radio("Results basis",["AUTO","CONSOLIDATED","STANDALONE"])
    use_ocr=st.checkbox("Use OCR if needed")
    show_table=st.checkbox("Show reconstructed table",value=True)

url=st.text_input("Direct PDF link",placeholder="https://nsearchives.nseindia.com/...pdf")
upload=st.file_uploader("Or upload PDF",type=["pdf"])

if st.button("ANALYSE",type="primary",width="stretch"):
    try:
        if upload: data=upload.getvalue()
        elif url.strip(): data=get_pdf(url.strip())
        else: st.error("Paste a direct PDF link or upload a PDF."); st.stop()
        with st.spinner("Reconstructing financial-results table..."): r=analyse(data,quarter,basis,use_ocr)
        st.success(f"Analysis complete — {r['basis']} results selected (PDF pages {r['start']}–{r['end']})")
        st.subheader("Results"); st.code(output_text(r),language="text")
        out=[]
        for label,m in [("Revenue",r["revenue"]),("EBITDA",r["ebitda"]),("Net Profit",r["pat"])]:
            out.append({"Metric":label,"Current ₹cr":m["current"],"Previous Q ₹cr":m["previous"],"YoY ₹cr":m["yoy"],"QoQ %":m["qoq"],"YoY %":m["yoypct"],"Confidence":m["confidence"]})
        st.subheader("Calculation details"); st.dataframe(pd.DataFrame(out),width="stretch",hide_index=True)
        if r["warnings"]:
            with st.expander("⚠ Review warnings",expanded=True):
                for w in r["warnings"]: st.warning(w)
        with st.expander("🔎 Extraction diagnostics",expanded=True):
            st.write("Company:",r["company"]); st.write("Selected basis:",r["basis"]); st.write("Selected PDF pages:",f"{r['start']}–{r['end']}"); st.write("Unit:",r["unit"]); st.write("Section score:",r["section_score"])
            for label,m in [("REVENUE",r["revenue"]),("EBITDA",r["ebitda"]),("PAT",r["pat"])]:
                st.markdown(f"**{label}**"); st.write("Confidence:",m["confidence"]); st.write("Extracted values:",m["raw"]); st.code(m["source"] or "Not found")
        if show_table:
            st.subheader("🧩 Reconstructed table")
            st.dataframe(pd.DataFrame(r["table"])[["page","y","text"]],width="stretch",hide_index=True)
        st.download_button("Download JSON",json.dumps(r,indent=2,default=str),file_name="results_analysis_v5.json",mime="application/json")
    except Exception as e:
        st.error(f"Analysis failed: {e}"); st.exception(e)
