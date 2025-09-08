# -*- coding: utf-8 -*-
"""
안전법 도우미 서버 v5.2 (2025-09-08)
- DRF API 1순위, TSV DB 백업
- 구글 서비스 계정 연동 (Sheets API)
- 응답은 자유 형식, 마지막에 면책 고지문 고정
"""

import os, io, re, csv, json, requests, unicodedata
from flask import Flask, request, jsonify, Response
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
STARTED_AT = datetime.utcnow()

# ===== 환경 변수 =====
DRF_BASE = "https://www.law.go.kr/DRF/lawService.do"
API_KEY = os.getenv("NLIC_API_KEY", "")
TSV_URL = os.getenv("LAW_TSV_URL", "")
TSV_PATH = os.getenv("LAW_TSV_PATH", "")
GOOGLE_SERVICE_JSON = os.getenv("GOOGLE_SERVICE_JSON", "")
SHEET_ID = os.getenv("SHEET_ID", "")
SHEET_RANGE = os.getenv("SHEET_RANGE", "A:Z")

DISCLAIMER = (
    "⚠ 본 응답은 [안전법 도우미 GPT]가 제공하는 참고용 법령 정보입니다.\n"
    "정확한 법률 해석이나 적용은 변호사 등 전문가와 반드시 상담하시기 바랍니다.\n"
    "본 정보는 국가법령정보센터 및 고용노동부 고시 등을 기반으로 제공됩니다."
)

INDEX = []

# ===== TSV / 구글시트 로딩 =====
def load_from_tsv():
    text = ""
    if TSV_URL:
        r = requests.get(TSV_URL, timeout=20)
        r.raise_for_status()
        text = r.text
    elif TSV_PATH and os.path.exists(TSV_PATH):
        with open(TSV_PATH, encoding="utf-8") as f:
            text = f.read()
    if not text: return []
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = next(reader)
    idx = {h:i for i,h in enumerate(header)}
    data = []
    for row in reader:
        def get(c): return row[idx[c]].strip() if c in idx and idx[c] < len(row) else ""
        data.append({
            "lawId": get("법령ID"),
            "lawTitle": get("법령명"),
            "unitType": get("조문단위"),
            "unitNo": get("조문번호"),
            "title": get("조문제목"),
            "textPlain": get("조문내용(Plain)"),
            "textHtml": get("조문내용(HTML)"),
            "enactedAt": get("시행일"),
            "amendedAt": get("최신개정일"),
            "sourceUrl": get("출처URL")
        })
    return data

def load_from_sheet():
    if not GOOGLE_SERVICE_JSON or not SHEET_ID: return []
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    service = build("sheets","v4",credentials=creds)
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=SHEET_RANGE
    ).execute()
    values = resp.get("values", [])
    if not values: return []
    header, *rows = values
    idx = {h:i for i,h in enumerate(header)}
    data = []
    for row in rows:
        def get(c): return row[idx[c]].strip() if c in idx and idx[c] < len(row) else ""
        data.append({
            "lawId": get("법령ID"),
            "lawTitle": get("법령명"),
            "unitType": get("조문단위"),
            "unitNo": get("조문번호"),
            "title": get("조문제목"),
            "textPlain": get("조문내용(Plain)"),
            "textHtml": get("조문내용(HTML)"),
            "enactedAt": get("시행일"),
            "amendedAt": get("최신개정일"),
            "sourceUrl": get("출처URL")
        })
    return data

def load_index():
    global INDEX
    try:
        INDEX = load_from_sheet()
        if not INDEX: INDEX = load_from_tsv()
    except Exception as e:
        print("Load fail:", e)
        INDEX = []

load_index()

# ===== API =====
@app.get("/healthz")
def healthz():
    uptime = (datetime.utcnow()-STARTED_AT).total_seconds()
    return {"status":"ok","uptime":round(uptime,1),"version":"v5.2","count":len(INDEX)}

@app.get("/search")
def search():
    kw = request.args.get("keyword","").strip()
    if not kw: return {"total":0,"items":[]}
    exact = request.args.get("exact","0").lower() in ("1","true")
    page = max(int(request.args.get("page",1)),1)
    page_size = min(max(int(request.args.get("page_size",50)),1),100)

    def match(row):
        text = row.get("textPlain","")
        if exact: return kw in text
        return all(tok in text for tok in kw.split())

    items = [r for r in INDEX if match(r)]
    start=(page-1)*page_size
    return {"total":len(items),"page":page,"page_size":page_size,"items":items[start:start+page_size]}

@app.get("/answer")
def answer():
    q = request.args.get("q","").strip()
    if not q: return {"ok":False,"message":"q required"}
    data = [r for r in INDEX if any(tok in r.get("textPlain","") for tok in q.split())]
    if not data:
        return {"ok":True,"content":"관련 조문을 찾지 못했습니다.","disclaimer":DISCLAIMER}
    # 자유 형식 content
    content = f"총 {len(data)}건의 관련 조문이 확인되었습니다.\n\n"
    for i,r in enumerate(data[:20],1):
        content += f"{i}) {r['lawTitle']} {r['unitNo']} — {r['title']}\n{r['textPlain']}\n출처: 국가법령정보센터(https://law.go.kr/)\n\n"
    return {"ok":True,"content":content.strip(),"disclaimer":DISCLAIMER}

@app.get("/openapi.yaml")
def openapi_yaml():
    path=os.path.join(os.path.dirname(__file__),"openapi.yaml")
    if not os.path.exists(path): return Response("openapi.yaml not found",404)
    return Response(open(path,encoding="utf-8").read(),mimetype="text/yaml")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",5000)))
