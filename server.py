# server.py  — Sheets ALL + 한국어 헤더 대응 + 시트>YAML 우선
import os, glob, yaml, re, datetime
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

LAWS_DIR      = os.getenv("LAWS_DIR", "./laws")
SHEETS_ID     = os.getenv("SHEETS_SPREADSHEET_ID")
SHEETS_RANGE  = os.getenv("SHEETS_RANGE", "ALL")  # ALL 또는 '탭명!A:Z, 다른탭!A:Z'
GOOGLE_CREDS  = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

DISCLAIMER = ("본 응답은 [안전법 도우미 GPT]가 제공하는 참고용 법령 정보입니다. "
              "법적 해석이나 자문은 제공하지 않으며, 반드시 최신 법령 원문과 전문가 상담을 통해 확인하시기 바랍니다.")

app = FastAPI(title="SafetyLawGPT API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

LAWS = []  # 메모리 DB (시트 우선, YAML 보조)

def _strip_html(s): return re.sub("<[^>]+>", "", s or "")
def _now_iso():     return datetime.datetime.now().astimezone().isoformat()

# ---------------- YAML 보조 로더 ----------------
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
        if not r or not r.get("law_id") or not r.get("article_no"):
            continue
        r["_text"]   = (r.get("text_plain") or _strip_html(r.get("text_html"))).strip()
        r["_source"] = "yaml"
        out.append(r)
    return out

# ---------------- Sheets 로더 (ALL 탭 + 한국어 헤더) ----------------
def _sheets_service():
    creds = Credentials.from_service_account_file(
        GOOGLE_CREDS, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds)

def _ranges_from_env(svc):
    rng = (SHEETS_RANGE or "").strip()
    if rng.upper() == "ALL":
        meta = svc.spreadsheets().get(spreadsheetId=SHEETS_ID).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        return [f"{t}!A:Z" for t in titles]
    # 콤마 구분
    parts = [r.strip() for r in rng.split(",") if r.strip()]
    return parts if parts else ["Sheet1!A:Z"]

def _process_values(values):
    """단일 탭 values -> 레코드 리스트"""
    if not values: 
        return []

    header = [h.strip() for h in values[0]]
    idx = {k:i for i,k in enumerate(header)}

    def ci(*names):
        for n in names:
            if n in idx: return idx[n]
        return None

    # 네가 제공한 한국어 헤더 대응
    c_ord       = ci("정렬순서","ord")
    c_enforce   = ci("시행일")
    c_newly     = ci("신설일")
    c_rev       = ci("최신개정일","개정일","revision_date")
    c_law_id    = ci("법령ID","law_id","ID")
    c_law_name  = ci("법령명","law_name")
    c_law_type  = ci("법령유형")
    c_article   = ci("조문번호","article_no")
    c_para      = ci("항번호")
    c_ho        = ci("호번호")
    c_mok       = ci("목번호")
    c_unit      = ci("조","unit","조문구분","구분")
    c_title     = ci("조문제목","article_title")
    c_path      = ci("조문경로")
    c_text      = ci("조문내용(Plain)","text","본문")
    c_html      = ci("조문내용(HTML)","text_html")
    c_deleted   = ci("삭제여부(Y/N)","삭제여부","삭제")
    c_src       = ci("출처URL","source_url","URL","url")
    c_note      = ci("비고","note")

    def g(row, col_index):
        return (row[col_index].strip() if col_index is not None and col_index < len(row) and row[col_index] is not None else "")

    by_key = {}  # (law_id, article_no) -> rec

    for row in values[1:]:
        # 삭제 Y는 스킵
        if g(row, c_deleted).upper() == "Y":
            continue

        law_id     = g(row, c_law_id)
        law_name   = g(row, c_law_name)
        article_no = g(row, c_article)
        if not law_id or not article_no:
            continue

        unit_v   = g(row, c_unit)       # '조' / '항' / '호' / '목' 등
        title    = g(row, c_title)
        revdate  = g(row, c_rev)
        src_url  = g(row, c_src)
        t_plain  = g(row, c_text)
        t_html   = g(row, c_html)

        key = (law_id, article_no)
        is_head = ("조" in unit_v) or (key not in by_key)

        if is_head:
            rec = {
                "law_id":        law_id,
                "law_name":      law_name,
                "article_no":    article_no,
                "article_title": title,
                "revision_date": revdate,
                "db_synced_at":  _now_iso().split("T")[0],  # 시트에 별도 칼럼 없으니 오늘 날짜
                "status":        "유효",
                "source_url":    src_url,
                "text_plain":    (t_plain + "\n") if t_plain else "",
                "text_html":     (t_html  + "\n") if t_html  else "",
                "_source":       "sheets",
            }
            by_key[key] = rec
        else:
            if t_plain: by_key[key]["text_plain"] += t_plain + "\n"
            if t_html:  by_key[key]["text_html"]  += t_html  + "\n"

    out = []
    for rec in by_key.values():
        rec["_text"] = (rec.get("text_plain") or _strip_html(rec.get("text_html"))).strip()
        out.append(rec)
    return out

def load_from_sheets():
    if not (SHEETS_ID and GOOGLE_CREDS):
        return []
    try:
        svc = _sheets_service()
        ranges = _ranges_from_env(svc)
        resp = svc.spreadsheets().values().batchGet(spreadsheetId=SHEETS_ID, ranges=ranges).execute()
        valueRanges = resp.get("valueRanges", [])
        merged = []
        for vr in valueRanges:
            merged.extend(_process_values(vr.get("values", [])))
        return merged
    except Exception:
        return []

# ---------------- 전체 리로드 ----------------
def reload_all():
    global LAWS
    s = load_from_sheets()
    y = load_from_yaml()
    seen = set(); merged = []
    for rec in s + y:   # 시트가 먼저, 중복키는 시트 우선
        key = (rec.get("law_id"), rec.get("article_no"))
        if key in seen: 
            continue
        seen.add(key)
        merged.append(rec)
    LAWS = merged

reload_all()

# ---------------- 검색 & 엔드포인트 ----------------
def _search_local(keyword: str, limit: int = 5):
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
    return {"ok": True, "ts": _now_iso(), "laws_loaded": len(LAWS), "sheets": sheets, "yaml": yaml_n}

@app.get("/reload", operation_id="reload")
def reload():
    reload_all()
    sheets = sum(1 for r in LAWS if r.get("_source")=="sheets")
    yaml_n = sum(1 for r in LAWS if r.get("_source")=="yaml")
    return {"ok": True, "reloaded": len(LAWS), "sheets": sheets, "yaml": yaml_n}

@app.get("/search", operation_id="search")
def search(keyword: str = Query(..., min_length=1), limit: int = 5):
    hits = _search_local(keyword, limit)
    return {"count": len(hits), "items": [
        {"law_id": h.get("law_id"), "law_name": h.get("law_name"),
         "article_no": h.get("article_no"), "title": h.get("article_title"),
         "revision_date": h.get("revision_date"), "db_synced_at": h.get("db_synced_at"),
         "source": h.get("_source")}
        for h in hits]}

@app.get("/answer", operation_id="answer")
def answer(keyword: str = Query(..., min_length=1)):
    hits = _search_local(keyword, 1)
    if hits:
        r = hits[0]
        head  = f"{r.get('law_name','')} | {r.get('article_no','')}({r.get('article_title','')}) | 최신 개정일: {r.get('revision_date','')}"
        body  = r.get("_text","")
        basis = f"{head}\n— 내부 DB 기준일: {r.get('db_synced_at','')}\n— 원문:\n{body}\n— 출처: {r.get('source_url','')}"
        middle = "- 일반 요약: 원문 조문을 근거로 현장 절차·별표·서식 유무를 확인하십시오. 불명확한 부분은 상위법·별표·고시를 추가 확인하십시오."
        return {
            "status":"ok","generated_at":_now_iso(),
            "legal_basis":basis,"middle":middle,"disclaimer":DISCLAIMER,
            "law_name":r.get("law_name"),"article_no":r.get("article_no"),
            "revision_date":r.get("revision_date"),"db_synced_at":r.get("db_synced_at"),
            "source_url":r.get("source_url"), "source": r.get("_source")
        }
    srch = _law_search_url(keyword)
    basis = f"[DB 미수록] 내부 DB(시트/로컬)에 해당 조문이 없습니다. law.go.kr에서 '{keyword}'로 검색하여 최신 원문을 확인하십시오.\n— 검색 경로: {srch}"
    middle = "- 참고: 법률 > 시행령 > 시행규칙 > 고시·지침 순서로 원문을 확인하십시오. (확실하지 않음)"
    return {"status":"fallback","generated_at":_now_iso(),"legal_basis":basis,"middle":middle,"disclaimer":DISCLAIMER,"source_url":srch}
from fastapi.responses import JSONResponse

@app.get("/diag", operation_id="diag")
def diag():
    info = {"sheets_id_set": bool(SHEETS_ID), "creds_path": GOOGLE_CREDS, "range": SHEETS_RANGE}
    try:
        creds = Credentials.from_service_account_file(
            GOOGLE_CREDS, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        svc = build("sheets", "v4", credentials=creds)
        meta = svc.spreadsheets().get(spreadsheetId=SHEETS_ID).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        info.update({"ok": True, "sheet_titles": titles})
        return info
    except Exception as e:
        info.update({"ok": False, "error_type": e.__class__.__name__, "error": str(e)})
        return JSONResponse(info, status_code=500)

