# server.py  — v5.8 응답 강제 / Sheets ALL + 한국어 헤더 / 다층 근거 + 역할잠금 + 전수스캔
import os, glob, yaml, re, datetime, urllib.parse
from typing import List, Dict, Tuple, Optional
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ---------- 환경변수 ----------
LAWS_DIR      = os.getenv("LAWS_DIR", "./laws")
SHEETS_ID     = os.getenv("SHEETS_SPREADSHEET_ID")
SHEETS_RANGE  = os.getenv("SHEETS_RANGE", "ALL")  # ALL 또는 '탭명!A:Z, 다른탭!A:Z'
GOOGLE_CREDS  = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
TZ            = os.getenv("TZ", "Asia/Seoul")

DISCLAIMER = (
    "본 답변은 안전법도우미 GPT가 생성한 일반 정보이며, 법률 자문이나 법률사무를 제공하지 않습니다. "
    "특정 사실관계에 대한 해석·적용은 관할기관의 공식 안내와 자격 있는 변호사·노무사의 자문으로 검증하시기 바랍니다. "
    "본 대화는 변호사–의뢰인 관계를 형성하지 않으며, 정보의 최신성·완전성·적합성을 보장하지 않습니다. "
    "이 정보를 바탕으로 한 결정과 실행의 책임은 사용자에게 있습니다."
)

app = FastAPI(title="SafetyLawGPT API", version="1.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

LAWS: List[Dict] = []  # 메모리 DB (시트 우선, YAML 보조)

# ---------- 유틸 ----------
def _strip_html(s: Optional[str]) -> str:
    return re.sub("<[^>]+>", "", s or "")

def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat()

def _safe(s: str) -> str:
    return (s or "").strip()

def _ellipsis(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"

def _law_level(law_name: str) -> str:
    n = law_name or ""
    if "시행규칙" in n: return "rule"
    if "시행령"  in n: return "decree"
    if "고시" in n or "지침" in n: return "notice"
    # '기준에 관한 규칙' 같은 표현은 rule로 간주
    if "기준에 관한 규칙" in n: return "rule"
    return "act"

def _safe_link(text: str, url: str) -> str:
    return f"[{text}](<{url}>)" if url else text

def _search_url(query: str) -> str:
    base = "https://www.law.go.kr/lsSc.do?section=&menuId=1&subMenuId=15&tabMenuId=81&eventGubun=060101&query="
    return base + urllib.parse.quote(query.strip())

ROLE_TOKENS = ["안전관리자","보건관리자","안전보건총괄책임자","관리감독자","안전보건관리담당자","산업보건의"]

def _detect_role(q: str) -> Optional[str]:
    for r in ROLE_TOKENS:
        if r in q:
            return r
    return None

FREQ_PAT = re.compile(r"(반기\s*1회(?:\s*이상)?|반기|6개월\s*1회(?:\s*이상)?)")
VERB_PAT = re.compile(r"(점검|평가|관리|확인|검토)")

def _scan_frequency(text: str) -> List[str]:
    out = []
    for m in FREQ_PAT.finditer(text or ""):
        # 주변 문맥 60자 추출
        start = max(0, m.start()-40); end = min(len(text), m.end()+40)
        ctx = text[start:end].replace("\n"," ").strip()
        if VERB_PAT.search(ctx):
            out.append(ctx)
    # 중복 제거
    seen = set(); uniq = []
    for t in out:
        k = re.sub(r"\s+", " ", t)
        if k in seen: continue
        seen.add(k); uniq.append(t)
    return uniq[:20]

# ---------- YAML 보조 로더 ----------
def _load_yaml(path: str) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None

def load_from_yaml() -> List[Dict]:
    out = []
    for y in glob.glob(os.path.join(LAWS_DIR, "**", "*.yml"), recursive=True):
        r = _load_yaml(y)
        if not r or not r.get("law_id") or not r.get("article_no"):
            continue
        r["_text"]   = (r.get("text_plain") or _strip_html(r.get("text_html"))).strip()
        r["_source"] = "yaml"
        out.append(r)
    return out

# ---------- Sheets 로더 (ALL 탭 + 한국어 헤더) ----------
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
    parts = [r.strip() for r in rng.split(",") if r.strip()]
    return parts if parts else ["Sheet1!A:Z"]

def _process_values(values: List[List[str]]) -> List[Dict]:
    if not values: 
        return []

    header = [h.strip() for h in values[0]]
    idx = {k:i for i,k in enumerate(header)}
    def ci(*names):
        for n in names:
            if n in idx: return idx[n]
        return None

    # 한국어 헤더 대응
    c_rev       = ci("최신개정일","개정일","revision_date")
    c_law_id    = ci("법령ID","law_id","ID")
    c_law_name  = ci("법령명","law_name")
    c_article   = ci("조문번호","article_no")
    c_unit      = ci("조","unit","조문구분","구분")
    c_title     = ci("조문제목","article_title")
    c_text      = ci("조문내용(Plain)","text","본문")
    c_html      = ci("조문내용(HTML)","text_html")
    c_deleted   = ci("삭제여부(Y/N)","삭제여부","삭제")
    c_src       = ci("출처URL","source_url","URL","url")

    def g(row, col_index):
        return (row[col_index].strip() if col_index is not None and col_index < len(row) and row[col_index] is not None else "")

    by_key: Dict[Tuple[str,str], Dict] = {}

    for row in values[1:]:
        if _safe(g(row, c_deleted)).upper() == "Y":
            continue
        law_id     = _safe(g(row, c_law_id))
        law_name   = _safe(g(row, c_law_name))
        article_no = _safe(g(row, c_article))
        if not law_id or not article_no:
            continue

        unit_v   = _safe(g(row, c_unit))   # '조' / '항' / '호' / '목'
        title    = _safe(g(row, c_title))
        revdate  = _safe(g(row, c_rev))
        src_url  = _safe(g(row, c_src))
        t_plain  = _safe(g(row, c_text))
        t_html   = _safe(g(row, c_html))

        key = (law_id, article_no)
        is_head = ("조" in unit_v) or (key not in by_key)

        if is_head:
            rec = {
                "law_id":        law_id,
                "law_name":      law_name,
                "article_no":    article_no,
                "article_title": title,
                "revision_date": revdate,
                "db_synced_at":  _now_iso().split("T")[0],
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
        rec["_level"] = _law_level(rec.get("law_name",""))
        out.append(rec)
    return out

def load_from_sheets() -> List[Dict]:
    if not (SHEETS_ID and GOOGLE_CREDS):
        return []
    try:
        svc = _sheets_service()
        ranges = _ranges_from_env(svc)
        resp = svc.spreadsheets().values().batchGet(spreadsheetId=SHEETS_ID, ranges=ranges).execute()
        valueRanges = resp.get("valueRanges", [])
        merged: List[Dict] = []
        for vr in valueRanges:
            merged.extend(_process_values(vr.get("values", [])))
        return merged
    except Exception:
        return []

# ---------- 전체 리로드 ----------
def reload_all():
    global LAWS
    s = load_from_sheets()
    y = load_from_yaml()
    seen = set(); merged: List[Dict] = []
    for rec in s + y:   # 시트가 먼저, 중복키는 시트 우선
        key = (rec.get("law_id"), rec.get("article_no"))
        if key in seen: 
            continue
        seen.add(key)
        merged.append(rec)
    LAWS = merged

reload_all()

# ---------- 간단 검색 ----------
def _score(hay: str, kw: str) -> int:
    score = 0
    for t in kw.split():
        if t in hay: score += hay.count(t)
    if kw in hay: score += 3
    return score

def _search_local(keyword: str, limit: int = 12) -> List[Dict]:
    kw = keyword.strip()
    res: List[Tuple[int, Dict]] = []
    for r in LAWS:
        hay = f"{r.get('law_name','')} {r.get('article_no','')} {r.get('article_title','')} {r.get('_text','')}"
        sc = _score(hay, kw)
        if sc > 0:
            res.append((sc, r))
    res.sort(key=lambda x:x[0], reverse=True)
    return [x[1] for x in res[:limit]]

# ---------- 엔드포인트 ----------
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
def search(keyword: str = Query(..., min_length=1), limit: int = 10):
    hits = _search_local(keyword, limit)
    return {"count": len(hits), "items": [
        {"law_id": h.get("law_id"), "law_name": h.get("law_name"),
         "article_no": h.get("article_no"), "title": h.get("article_title"),
         "revision_date": h.get("revision_date"), "db_synced_at": h.get("db_synced_at"),
         "level": h.get("_level"), "source": h.get("_source")}
        for h in hits]}

def _group_by_level(hits: List[Dict]) -> Dict[str, List[Dict]]:
    buckets: Dict[str, List[Dict]] = {"act":[], "decree":[], "rule":[], "notice":[]}
    for r in hits:
        buckets.setdefault(r.get("_level","act"), []).append(r)
    # 각 레벨 상위 3개까지만
    for k in buckets:
        buckets[k] = buckets[k][:3]
    return buckets

def _summarize(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return _ellipsis(t, 220)

def _basis_block_for(rec: Dict, scan_freq: bool=False) -> str:
    # 반말체로 작성
    law_name = rec.get("law_name","")
    article  = rec.get("article_no","")
    title    = rec.get("article_title","")
    rev      = rec.get("revision_date","")
    src      = rec.get("source_url","")
    summary  = _summarize(rec.get("_text",""))
    lines = []
    lines.append(f"- **법령명:** {law_name}")
    lines.append(f"- **조문:** {article}({title})")
    lines.append(f"- **최신개정일:** {rev}")
    lines.append(f"- **원문 요지:** {summary}")
    if scan_freq:
        matches = _scan_frequency(rec.get("_text",""))
        if matches:
            lines.append(f"- **〈매칭 항목(전수)〉**")
            for m in matches:
                mm = _ellipsis(m, 140)
                lines.append(f"  - “**{mm}**”")
    lines.append(f"- **출처:** {_safe_link('국가법령정보센터 바로가기', src)}")
    return "\n".join(lines)

def _compose_blocks(keyword: str, role_lock: Optional[str], include_all_levels: bool, scan_frequency: bool) -> Tuple[str,str]:
    hits = _search_local(keyword, 16)
    if not hits:
        srch = _search_url(keyword)
        basis = f"**[근거]**\n- 원문을 찾지 못했다. 내부 DB(시트/로컬)에 해당 조문이 없다.\n- **검색 경로:** {_safe_link('국가법령정보센터 검색', srch)}"
        body  = ("**질문 해결 요약**\n"
                 "- 법률 → 시행령 → 시행규칙 → 고시·지침 순서로 최신 원문을 확인해 주세요.\n"
                 "- 조문·별표 정확 일치 항목만 인용합니다.\n"
                 "※ 추가 확인: 상·하위법 개정일을 꼭 비교해 주세요.")
        return basis, body

    # 역할 잠금: 질의에 역할 키워드가 있으면, 그 역할 관련 글자 포함 레코드 우선
    role = role_lock or _detect_role(keyword) or ""
    if role:
        role_hits = [r for r in hits if role in (r.get("_text","")+r.get("article_title","")+r.get("law_name",""))]
        if role_hits:
            hits = role_hits + [r for r in hits if r not in role_hits]

    buckets = _group_by_level(hits)
    order = ["act","decree","rule","notice"] if include_all_levels else ["decree"]  # 최소 시행령
    labels = {"act":"(법률)","decree":"(시행령)","rule":"(시행규칙)","notice":"(고시·지침)"}

    basis_parts = ["📌 **[근거]**"]
    for lv in order:
        if not buckets.get(lv): continue
        for rec in buckets[lv]:
            basis_parts.append(f"- **{labels[lv]}**")
            basis_parts.append(_basis_block_for(rec, scan_freq=scan_frequency))
    basis_md = "\n".join(basis_parts)

    # 본문(존댓말)
    body_lines = []
    body_lines.append("**내용 요약**")
    if role:
        body_lines.append(f"- 본 질의는 **{role}** 관련으로 해석했습니다(역할 잠금).")
    if scan_frequency:
        body_lines.append("- 요청하신 **반기 1회 이상** 관련 조항을 전수로 매칭하여 요지를 정리했습니다.")
    body_lines.append("- 상위법 우선 원칙을 적용했으며, 직접 관련된 하위법만 포함했습니다.")
    body_lines.append("※ 추가 확인: 상·하위법의 **최신개정일**이 서로 다른 경우가 있으니 반드시 개정일을 비교해 주세요.")
    body_md = "\n".join(body_lines)

    return basis_md, body_md

def _compose_markdown(basis_md: str, body_md: str, disclaimer: str) -> str:
    # v5.8 3블록 강제
    return f"{basis_md}\n\n---\n{body_md}\n\n---\n> ⚠️ **[면책고지]**\n> {disclaimer}"

@app.get("/answer", operation_id="answer")
def answer(
    keyword: str = Query(..., min_length=1),
    role_lock: Optional[str] = Query(None, description="역할 잠금: 안전관리자/보건관리자/안전보건총괄책임자/관리감독자/안전보건관리담당자/산업보건의"),
    include_all_levels: bool = Query(True, description="법·령·규칙·고시까지 다층 근거 출력"),
    scan_frequency: Optional[bool] = Query(None, description="‘반기 1회 이상’ 등 빈도 전수 스캔"),
):
    # scan_frequency 자동 판별
    if scan_frequency is None:
        scan_frequency = bool(re.search(r"(반기|6개월|1회\s*이상)", keyword))

    basis_md, body_md = _compose_blocks(keyword, role_lock, include_all_levels, scan_frequency)
    markdown = _compose_markdown(basis_md, body_md, DISCLAIMER)

    return {
        "status": "ok",
        "generated_at": _now_iso(),
        # v5.8 3블록 호환 필드
        "legal_basis": basis_md,     # [근거]
        "middle": body_md,           # (제목 없는 본문)
        "disclaimer": DISCLAIMER,    # [면책고지]
        # 통합 마크다운(직접 렌더링용)
        "markdown": markdown,
        # 디버그 힌트
        "params": {
            "role_lock": role_lock or _detect_role(keyword),
            "include_all_levels": include_all_levels,
            "scan_frequency": scan_frequency
        }
    }

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
        info.update({"ok": True, "sheet_titles": titles, "laws_loaded": len(LAWS)})
        return info
    except Exception as e:
        info.update({"ok": False, "error_type": e.__class__.__name__, "error": str(e)})
        return JSONResponse(info, status_code=500)

