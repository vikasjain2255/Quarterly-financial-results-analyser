# Quarterly Results Analyser V4.1

Streamlit app that extracts quarterly financial results from Indian company result PDFs or direct PDF URLs.

## Output
- Revenue: YoY and QoQ growth
- EBITDA: explicit EBITDA where available; otherwise derived as:
  PBT + Finance Cost + Depreciation - Other Income
- EBITDA margins: current, YoY comparable and QoQ
- Net profit: last line
- Consolidated/standalone auto-detection
- Extraction confidence and diagnostics

## Streamlit Community Cloud
Entrypoint: `app.py`
Dependencies: `requirements.txt`
