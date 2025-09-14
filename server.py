import os, glob, yaml, re, datetime
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

LAWS_DIR = os.getenv("LAWS_DIR", "./laws")
DISCLAIMER = ("본 응답은 [안전법 도우미 GPT]가 제공하는 참고용 법령 정보입니다. "
              "법적 해석이나 자문은 제공하지 않으며, 반드시 최신 법령 원문과 전문가 상담을 통해 확인하시기 바랍니다.")

app = FastAPI(title="SafetyLawGPT API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

LAWS = []

def _load_yaml(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None

def _strip_html(s): return re.sub("<[^>]+>", "", s or "")

def reload_laws():
    global LAWS
    LAWS = []
    for y in glob.glob(os.path.join(LAWS_DIR, "**", "*.yml"), recursive=True):
        r = _load_yaml(y)
        if not r: continue
        if not r.get("law_id") or not r.get("article_no"): continue
        r["_path"] = y
        r["_text"] = (r.get("text_plain") or _strip_html(r.get("text_html"))).strip()
        LAWS.append(r)

reload_laws()

def now_iso(): return datetime.datetime.now().astimezone().isoformat()

def search_local(q, limit=5):
    kw = q.strip()
    res = []
    for r in LAWS:
        hay = f"{r.get('law_name','')} {r.get('article_no','')} {r.get('_text','')}"
        score = 0
        for t in kw.split():
            if t in hay: score += hay.count(t)
        if kw in hay: score += 2
        if score>0: res.append((score, r))
    res.sort(key=lambda x:x[0], reverse=True)
    return [x[1] for x in res[:limit]]

def law_search_url(q): return f"https://law.go.kr/검색?query={q}"

@app.get("/healthz", operation_id="healthz")
def healthz(): return {"ok": True, "ts": now_iso(), "laws_loaded": len(LAWS)}

@app.get("/reload", operation_id="reload")
def reload(): reload_laws(); return {"ok": True, "reloaded": len(LAWS)}

@app.get("/search", operation_id="search")
def search(keyword: str = Query(..., min_length=1), limit: int = 5):
    hits = search_local(keyword, limit)
    return {"count": len(hits), "items": [
        {"law_id": h.get("law_id"), "law_name": h.get("law_name"),
         "article_no": h.get("article_no"), "title": h.get("article_title"),
         "revision_date": h.get("revision_date"), "db_synced_at": h.get("db_synced_at")}
        for h in hits]}

@app.get("/answer", operation_id="answer")
def answer(keyword: str = Query(..., min_length=1)):
    hits = search_local(keyword, 1)
    if hits:
        r = hits[0]
        head = f"{r.get('law_name','')} | {r.get('article_no','')}({r.get('article_title','')}) | 최신 개정일: {r.get('revision_date','')}"
        body = r.get("_text","")
        basis = f"{head}\n— 내부 DB 기준일: {r.get('db_synced_at','')}\n— 원문:\n{body}\n— 출처: {r.get('source_url','')}"
        middle = "- 일반 요약: 원문 조문을 근거로 현장 절차·별표·서식 유무를 확인하십시오. 불명확한 부분은 상위법·별표·고시를 추가 확인하십시오."
        return {
            "status":"ok","generated_at":now_iso(),
            "legal_basis":basis,"middle":middle,"disclaimer":DISCLAIMER,
            "law_name":r.get("law_name"),"article_no":r.get("article_no"),
            "revision_date":r.get("revision_date"),"db_synced_at":r.get("db_synced_at"),
            "source_url":r.get("source_url")
        }
    # Fallback: 무응답 금지
    srch = law_search_url(keyword)
    basis = f"[DB 미수록] 내부 DB에 해당 조문이 없습니다. law.go.kr에서 '{keyword}'로 검색하여 최신 원문을 확인하십시오.\n— 검색 경로: {srch}"
    middle = "- 참고: 법률 > 시행령 > 시행규칙 > 고시·지침 순서로 원문을 확인하십시오. (확실하지 않음)"
    return {"status":"fallback","generated_at":now_iso(),"legal_basis":basis,"middle":middle,"disclaimer":DISCLAIMER,"source_url":srch}
