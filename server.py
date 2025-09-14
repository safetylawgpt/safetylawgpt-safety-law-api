import os, glob, yaml, re, datetime
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

# ---- Google Sheets ----
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

LAWS_DIR = os.getenv("LAWS_DIR", "./laws")
SHEETS_ID = os.getenv("SHEETS_SPREADSHEET_ID")
SHEETS_RANGE = os.getenv("SHEETS_RANGE", "법령DB!A:Z")
GOOGLE_CREDS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

DISCLAIMER = ("본 응답은 [안전법 도우미 GPT]가 제공하는 참고용 법령 정보입니다. "
              "법적 해석이나 자문은 제공하지 않으며, 반드시 최신 법령 원문과 전문가 상담을 통해 확인하시기 바랍니다.")

app = FastAPI(title="SafetyLawGPT API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

LAWS = []  # 메모리 DB (시트 우선, YAML 보조)

def _strip_html(s): return re.sub("<[^>]+>", "", s or "")
def _now(): return datetime.datetime.now().astimezone().isoformat()

# ---------- YAML 로더 ----------
def _load_yaml(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None

def load_from_yaml():
    out = []
    for y in glob.glob(os.path.join(LAWS_DIR, "**", "*.yml"), recursive=True):
        r = _load_yaml(y)
        if not r or not r.get("law_id") or not r.get("article_no"): continue
        r["_text"] = (r.get("text_plain") or _strip_html(r.get("text_html"))).strip()
        r["_source"] = "yaml"
        out.append(r)
    return out

# ---------- Google Sheets 로더 ----------
def load_from_sheets():
    if not (SHEETS_ID and GOOGLE_CREDS):
        return []
    try:
        creds = Credentials.from_service_account_file(
            GOOGLE_CREDS, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        svc = build("sheets", "v4", credentials=creds)
        resp = svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID, range=SHEETS_RANGE
        ).execute()
        rows = resp.get("values", [])
        if not rows: return []
        header = rows[0]; idx = {k:i for i,k in enumerate(header)}
        def g(r, k): 
            i = idx.get(k)
            return (r[i] if i is not None and i < len(r) else "").strip()

        by_key = {}
        for r in rows[1:]:
            key = (g(r,"law_id"), g(r,"article_no"))
            unit = g(r,"unit")
            if unit == "조" or key not in by_key:
                rec = {
                    "law_id": g(r,"law_id"),
                    "law_name": g(r,"law_name"),
                    "article_no": g(r,"article_no"),
                    "article_title": g(r,"article_title"),
                    "revision_date": g(r,"revision_date"),
                    "db_synced_at": g(r,"db_synced_at"),
                    "status": g(r,"status") or "유효",
                    "source_url": g(r,"source_url"),
                    "text_plain": (g(r,"text") + "\n") if g(r,"text") else "",
                    "text_html": "",
                    "_source": "sheets"
                }
                by_key[key] = rec
            else:
                if g(r,"text"):
                    by_key[key]["text_plain"] += g(r,"text") + "\n"

        out = []
        for rec in by_key.values():
            rec["_text"] = (rec.get("text_plain") or _strip_html(rec.get("text_html"))).strip()
            out.append(rec)
        return out
    except Exception:
        return []

# ---------- 전체 리로드 (시트 > YAML) ----------
def reload_all():
    global LAWS
    s = load_from_sheets()
    y = load_from_yaml()
    seen = set(); merged = []
    for rec in s + y:
        key = (rec.get("law_id"), rec.get("article_no"))
        if key in seen: continue
        seen.add(key); merged.append(rec)
    LAWS = merged

reload_all()

# ---------- 검색/응답 ----------
def _search(keyword: str, limit: int = 5):
    kw = keyword.strip()
    res = []
    for r in LAWS:
        hay = f"{r.get('law_name','')} {r.get('article_no','')} {r.get('_text','')}"
        score = 0
        for t in kw.split():
            if t in hay: score += hay.count(t)
        if kw in hay: score += 2
        if score > 0: res.append((score, r))
    res.sort(key=lambda x:x[0], reverse=True)
    return [x[1] for x in res[:limit]]

def _law_search_url(q): return f"https://law.go.kr/검색?query={q}"

@app.get("/healthz", operation_id="healthz")
def healthz():
    sheets = sum(1 for r in LAWS if r.get("_source")=="sheets")
    yaml_n = sum(1 for r in LAWS if r.get("_source")=="yaml")
    return {"ok": True, "ts": _now(), "laws_loaded": len(LAWS), "sheets": sheets, "yaml": yaml_n}

@app.get("/reload", operation_id="reload")
def reload():
    reload_all()
    sheets = sum(1 for r in LAWS if r.get("_source")=="sheets")
    yaml_n = sum(1 for r in LAWS if r.get("_source")=="yaml")
    return {"ok": True, "reloaded": len(LAWS), "sheets": sheets, "yaml": yaml_n}

@app.get("/search", operation_id="search")
def search(keyword: str = Query(..., min_length=1), limit: int = 5):
    hits = _search(keyword, limit)
    return {"count": len(hits), "items": [
        {"law_id": h.get("law_id"), "law_name": h.get("law_name"),
         "article_no": h.get("article_no"), "title": h.get("article_title"),
         "revision_date": h.get("revision_date"), "db_synced_at": h.get("db_synced_at"),
         "source": h.get("_source")}
        for h in hits]}

@app.get("/answer", operation_id="answer")
def answer(keyword: str = Query(..., min_length=1)):
    hits = _search(keyword, 1)
    if hits:
        r = hits[0]
        head = f"{r.get('law_name','')} | {r.get('article_no','')}({r.get('article_title','')}) | 최신 개정일: {r.get('revision_date','')}"
        body = r.get("_text","")
        basis = f"{head}\n— 내부 DB 기준일: {r.get('db_synced_at','')}\n— 원문:\n{body}\n— 출처: {r.get('source_url','')}"
        middle = "- 일반 요약: 원문 조문을 근거로 현장 절차·별표·서식 유무를 확인하십시오. 불명확한 부분은 상위법·별표·고시를 추가 확인하십시오."
        return {
            "status":"ok","generated_at":_now(),
            "legal_basis":basis,"middle":middle,"disclaimer":DISCLAIMER,
            "law_name":r.get("law_name"),"article_no":r.get("article_no"),
            "revision_date":r.get("revision_date"),"db_synced_at":r.get("db_synced_at"),
            "source_url":r.get("source_url"), "source": r.get("_source")
        }
    srch = _law_search_url(keyword)
    basis = f"[DB 미수록] 내부 DB(시트/로컬)에 해당 조문이 없습니다. law.go.kr에서 '{keyword}'로 검색하여 최신 원문을 확인하십시오.\n— 검색 경로: {srch}"
    middle = "- 참고: 법률 > 시행령 > 시행규칙 > 고시·지침 순서로 원문을 확인하십시오. (확실하지 않음)"
    return {"status":"fallback","generated_at":_now(),"legal_basis":basis,"middle":middle,"disclaimer":DISCLAIMER,"source_url":srch}
