import re,json,urllib.request
import streamlit as st
import pandas as pd
import pymupdf

st.set_page_config(page_title="Quarterly Results Analyser V5.5",page_icon="📊",layout="wide")
st.title("📊 Quarterly Results Analyser V5.5")
st.caption("Header-anchored financial-table reconstruction • controlled OCR repair")

PAT={
"revenue":[r"total\s+revenue\s+from\s+operations",r"revenue\s+from\s+operations",r"revenue\s+from\s+contracts?\s+with\s+customers",r"total\s+revenue",r"net\s+sales",r"total\s+sales",r"turnover"],
"other_income":[r"other\s+income"],"finance_cost":[r"finance\s+costs?",r"interest\s+and\s+finance\s+costs?"],
"depreciation":[r"depreciation\s*(?:and|&)\s*amortisation",r"depreciation\s+and\s+amortization",r"depreciation"],
"pbt":[r"profit\s*/?\s*\(?loss\)?\s+before\s+tax",r"profit\s+before\s+tax",r"\bpbt\b"],
"pat":[r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+period",r"profit\s*/?\s*\(?loss\)?\s+for\s+the\s+year",r"net\s+profit\s+after\s+tax",r"net\s+profit",r"profit\s+attributable\s+to.*owners"],
"ebitda":[r"\bebitda\b",r"earnings\s+before\s+interest.*depreciation.*amort"]
}
OCR=str.maketrans({"O":"0","o":"0","I":"1","l":"1","|":"1","S":"5","s":"5","B":"8","G":"6","g":"6","Z":"2","z":"2","Q":"0","q":"9"})

def norm(s): return re.sub(r"\s+"," ",str(s).replace("\u00a0"," ")).strip()

def num(t):
    t=str(t).strip().replace(",","")
    neg=t.startswith("(") and t.endswith(")")
    c=t.strip("()[]{}")
    if not re.search(r"\d",c): return None,"NONE"
    if re.fullmatch(r"\d+(?:\.\d+)?",c):
        try:return (-1 if neg else 1)*float(c),"HIGH"
        except:return None,"NONE"
    if len(c)>18:return None,"NONE"
    x=re.sub(r"[^0-9.\-]","",c.translate(OCR))
    if not re.fullmatch(r"\d+(?:\.\d+)?",x):return None,"NONE"
    try:
        v=float(x)
        return ((-v if neg else v),"REPAIRED") if abs(v)<=1e8 else (None,"NONE")
    except:return None,"NONE"

def pdfdata(url):
    q=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(q,timeout=90) as r:b=r.read()
    if not b.startswith(b"%PDF"):raise ValueError("URL did not return a PDF.")
    return b

def rows(page,ocr=False):
    tp=page.get_textpage_ocr() if ocr else None
    ws=page.get_text("words",textpage=tp,sort=True) if tp else page.get_text("words",sort=True)
    rr=[]
    for x0,y0,x1,y1,t,*_ in ws:
        cy=(y0+y1)/2
        r=next((z for z in rr if abs(z["y"]-cy)<=2.8),None)
        it={"x":float(x0),"x1":float(x1),"text":str(t)}
        if r:r["items"].append(it);r["y"]=(r["y"]+cy)/2
        else:rr.append({"y":cy,"items":[it]})
    for r in rr:
        r["items"].sort(key=lambda x:x["x"])
        r["text"]=norm(" ".join(x["text"] for x in r["items"]))
        r["numbers"]=[]
        for x in r["items"]:
            v,c=num(x["text"])
            if v is not None:r["numbers"].append({"value":v,"x":x["x"],"token":x["text"],"confidence":c})
    return sorted(rr,key=lambda x:x["y"])

def text(page,ocr=False):
    tp=page.get_textpage_ocr() if ocr else None
    return page.get_text("text",textpage=tp,sort=True) if tp else page.get_text("text",sort=True)

def choose(doc,basis,ocr):
    ps=[]
    for i,p in enumerate(doc):
        t=text(p,ocr);l=t.lower();rs=rows(p,ocr)
        lab=sum(any(re.search(x,l,re.I) for x in PAT[k]) for k in ["revenue","finance_cost","depreciation","pbt","pat"])
        cons=len(re.findall("consolidated",l));stand=len(re.findall("standalone",l))
        dense=sum(len(r["numbers"])>=2 for r in rs)
        if lab>=2 and dense>=2:ps.append((i,t,rs,lab,cons,stand,dense))
    if not ps:raise ValueError("Could not identify a financial-results table.")
    def sc(p,w):
        good=p[4] if w=="CONSOLIDATED" else p[5]
        bad=p[5] if w=="CONSOLIDATED" else p[4]
        return p[3]*40+p[6]*15+good*100-bad*70
    if basis=="AUTO":
        c=max(ps,key=lambda p:sc(p,"CONSOLIDATED"));s=max(ps,key=lambda p:sc(p,"STANDALONE"))
        b="CONSOLIDATED" if sc(c,"CONSOLIDATED")>=sc(s,"STANDALONE") else "STANDALONE"
    else:b=basis
    p=max(ps,key=lambda p:sc(p,b))
    start=p[0];end=start+1
    return b,start,end,ps

def header(rs):
    best=None
    for r in rs:
        ds=[x for x in r["items"] if re.search(r"(?:19|20)\d{2}",x["text"]) or len(re.sub(r"\D","",x["text"]))>=6]
        if len(ds)>=2 and re.search(r"particular|quarter|year|period|ended|s\.?no|no\.?",r["text"],re.I):
            z=[(x["x"]+x["x1"])/2 for x in ds]
            if len(z)>=2 and (best is None or len(z)>len(best[0])):best=(z,r["text"])
    return best

def anchors(rs):
    h=header(rs)
    if h:return h[0][:4],"HEADER",h[1]
    pts=[]
    for r in rs:
        if any(re.search(p,r["text"],re.I) for k in ["revenue","finance_cost","depreciation","pbt","pat"] for p in PAT[k]):
            pts += [n["x"] for n in r["numbers"] if n["x"]>250]
    pts.sort();cs=[]
    for x in pts:
        if not cs or x-cs[-1][-1]>14:cs.append([x])
        else:cs[-1].append(x)
    return sorted([sum(c)/len(c) for c in cs if len(c)>=2])[:4],"NUMERIC_FALLBACK",None

def maprow(r,a):
    out=[None]*len(a)
    for n in r["numbers"]:
        if 1900<=n["value"]<=2100:continue
        j=min(range(len(a)),key=lambda k:abs(n["x"]-a[k]))
        d=abs(n["x"]-a[j])
        if d<=30 and (out[j] is None or d<abs(out[j]["x"]-a[j])):out[j]=n
    return out

def metric(rs,key,a,f):
    hits=[]
    for r in rs:
        if any(re.search(p,r["text"],re.I) for p in PAT[key]):
            m=maprow(r,a)
            if sum(x is not None for x in m)>=2:hits.append((sum(x is not None for x in m),r,m))
    if not hits:return {"current":None,"previous":None,"yoy":None,"qoq":None,"yoypct":None,"confidence":"LOW","source":"","mapped":[],"mapped_x":[]}
    _,r,m=max(hits,key=lambda x:(x[0],len(x[1]["numbers"])))
    v=[x["value"]*f if x else None for x in m]
    gr=lambda x,y:None if x is None or y in (None,0) else (x/y-1)*100
    return {"current":v[0],"previous":v[1],"yoy":v[2] if len(v)>2 else None,
            "qoq":gr(v[0],v[1]),"yoypct":gr(v[0],v[2] if len(v)>2 else None),
            "confidence":"HIGH","source":r["text"],
            "mapped":[x["token"] if x else None for x in m],
            "mapped_x":[x["x"] if x else None for x in m]}

def analyse(data,basis,ocr,q):
    doc=pymupdf.open(stream=data,filetype="pdf")
    b,s,e,profiles=choose(doc,basis,ocr)
    rs=[]
    for i in range(s,e):
        for r in rows(doc[i],ocr):
            r["page"]=i+1;rs.append(r)
    section="\n".join(profiles[j][1] for j in range(len(profiles)) if s<=profiles[j][0]<e)
    f=.01 if re.search(r"figures?\s+in\s+lakhs?|₹\s*lakhs?",section,re.I) else .1 if re.search(r"figures?\s+in\s+millions?|₹\s*millions?",section,re.I) else 1
    unit="₹ crore (converted)" if f!=1 else "₹ crore"
    a,src,hd=anchors(rs)
    m={k:metric(rs,k,a,f) for k in PAT}
    rev,eb,pat=m["revenue"],m["ebitda"],m["pat"]
    if eb["current"] is None and all(m[k]["current"] is not None for k in ["pbt","finance_cost","depreciation","other_income"]):
        v=[]
        for ix in range(3):
            z=[m[k]["current"] if ix==0 else m[k]["previous"] if ix==1 else m[k]["yoy"] for k in ["pbt","finance_cost","depreciation","other_income"]]
            if any(x is None for x in z):break
            v.append(z[0]+z[1]+z[2]-z[3])
        if len(v)==3:
            gr=lambda x,y:None if x is None or y in (None,0) else (x/y-1)*100
            eb={"current":v[0],"previous":v[1],"yoy":v[2],"qoq":gr(v[0],v[1]),"yoypct":gr(v[0],v[2]),
                "confidence":"DERIVED","source":"PBT + Finance Costs + Depreciation - Other Income","mapped":[],"mapped_x":[]}
            m["ebitda"]=eb
    warnings=[]
    for lab,x in [("REVENUE",m["revenue"]),("EBITDA",m["ebitda"]),("PAT",m["pat"])]:
        if x["current"] is None:warnings.append(f"{lab} could not be extracted.")
        if x["qoq"] is not None and abs(x["qoq"])>500:warnings.append(f"{lab} has an unusually large QOQ change ({x['qoq']:.0f}%). Check period mapping.")
    if len(a)!=4:warnings.append(f"{len(a)} column anchors detected; four-period mapping may be incomplete.")
    if src!="HEADER":warnings.append("Header anchors were not detected; numeric fallback was used.")
    name="COMPANY"
    for line in text(doc[s]).splitlines():
        z=norm(line)
        if len(z)<100 and re.search(r"LIMITED|LTD|INDIA|INDUSTRIES|INVESTMENTS|TUBE",z,re.I) and not re.search("financial results|registered office",z,re.I):
            name=z.upper();break
    gr=lambda x,y:None if x is None or y in (None,0) else (x/y-1)*100
    r={"company":name,"quarter":q,"basis":b,"start_page":s+1,"end_page":e,"unit":unit,
       "anchor_source":src,"header":hd,"column_anchors":a,**m,
       "margin_current":eb["current"]/rev["current"]*100 if eb["current"] is not None and rev["current"] not in (None,0) else None,
       "margin_previous":eb["previous"]/rev["previous"]*100 if eb["previous"] is not None and rev["previous"] not in (None,0) else None,
       "margin_yoy":eb["yoy"]/rev["yoy"]*100 if eb["yoy"] is not None and rev["yoy"] not in (None,0) else None,
       "warnings":warnings,"diagnostic_rows":rs}
    doc.close()
    return r

def ch(v):return "NA" if v is None else f"{'UP' if v>=0 else 'DOWN'} {abs(v):.0f}%"
def pct(v):return "NA" if v is None else f"{v:.1f}%"

def summary(r):
    rev,eb,pat=r["revenue"],r["ebitda"],r["pat"]
    revenue="REVENUE NA" if rev["current"] is None else f"REVENUE {ch(rev['yoypct'])} AT ₹{rev['current']:,.1f} CR (YOY), {ch(rev['qoq'])} (QOQ)"
    ebitda="EBITDA NA" if eb["current"] is None else f"EBITDA {ch(eb['yoypct'])} AT ₹{eb['current']:,.1f} CR (YOY), {ch(eb['qoq'])} (QOQ)"
    profit="CONS NET PROFIT NA" if pat["current"] is None else f"CONS NET PROFIT {ch(pat['yoypct'])} AT ₹{pat['current']:,.1f} CR (YOY), {ch(pat['qoq'])} (QOQ)"
    return "\n\n".join([f"{r['company']} {r['quarter']} :",revenue,ebitda,
        f"MARGINS {pct(r['margin_current'])} V {pct(r['margin_yoy'])} (YOY), {pct(r['margin_previous'])} (QOQ)",profit])

with st.sidebar:
    q=st.selectbox("Quarter",["Q1","Q2","Q3","Q4"])
    basis=st.radio("Results basis",["AUTO","CONSOLIDATED","STANDALONE"])
    ocr=st.checkbox("Force OCR",False)

url=st.text_input("Direct PDF link")
up=st.file_uploader("Or upload PDF",type=["pdf"])

if st.button("ANALYSE",type="primary",width="stretch"):
    try:
        data=up.getvalue() if up else pdfdata(url.strip()) if url.strip() else None
        if not data:st.error("Paste a direct PDF link or upload a PDF.");st.stop()
        r=analyse(data,basis,ocr,q)
        st.success(f"Selected {r['basis']} table — pages {r['start_page']}–{r['end_page']}")
        st.subheader("Results");st.code(summary(r))
        if r["warnings"]:
            with st.expander("⚠ Review warnings",True):
                for w in r["warnings"]:st.warning(w)
        with st.expander("🔎 Extraction diagnostics",True):
            st.write("Header:",r["header"] or "Not detected")
            st.write("Anchor source:",r["anchor_source"])
            st.write("Column X anchors:",r["column_anchors"])
            for lab in ["revenue","finance_cost","depreciation","pbt","pat","ebitda"]:
                m=r[lab]
                st.markdown(f"**{lab.upper()} — {m['confidence']}**")
                st.code(m["source"] or "Not found")
                st.caption("Mapped: "+" | ".join(str(x) for x in m["mapped"]))
        calc=[{"Metric":k.title(),"Current":r[k]["current"],"Previous Q":r[k]["previous"],"YoY":r[k]["yoy"],"QoQ %":r[k]["qoq"],"YoY %":r[k]["yoypct"],"Confidence":r[k]["confidence"]} for k in ["revenue","ebitda","pat"]]
        st.dataframe(pd.DataFrame(calc),width="stretch",hide_index=True)
        st.download_button("Download JSON",json.dumps(r,indent=2,default=str),file_name="results_analysis_v5_5.json",mime="application/json")
    except Exception as e:
        st.error(f"Analysis failed: {e}");st.exception(e)
