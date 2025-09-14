# server.py — v5.8 응답 강제 / 세그먼트(항·호·목) 분리 / 전수스캔(세그먼트 우선) / 링크 빌더
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

DISCLAIMER = (
    "본 답변은 안전법도우미 GPT가 생성한 일반 정보이며, 법률 자문이나 법률사무를 제공하지 않습니다. "
    "특정 사실관계에 대한 해석·적용은 관할기관의 공식 안내와 자격 있는 변호사·노무사의 자문으로 검증하시기 바랍니다. "
    "본 대화는 변호사–의뢰인 관계를 형성하지 않으며, 정보의 최신성·완전성·적합성을 보장하지 않습니다. "
    "이 정보를 바탕으로 한 결정과 실행의 책임은 사용자에게 있습니다."
)

app = FastAPI(title="SafetyLawGPT API", version="1.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

LAWS: List[Dict] = []  # 메모리 DB (시트 우선, YAML 보조)

# ---------- 유틸 ----------
def _strip_html(s: Optional[str]) -> str:
    return re.sub("<[^>]+>", "", s or "")

def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat()

def _safe(s: Optional[str]) -> str:
    return (s or "").replace("\u00A0"," ").strip()

def _ellipsis(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"

def _law_level(law_name: str) -> str:
    n = law_name or ""
    if "시행규칙" in n or "기준에 관한 규칙" in n: return "rule"
    if "시행령"  in n: return "decree"
    if "고시" in n or "지침" in n: return "notice"
    return "act"

def _safe_link(text: str, url: str) -> str:
    return f"[{text}](<{url}>)" if url else text

# 강화형 검색 URL(법령 탭 고정)
def _law_search_url(query: str) -> str:
    base = "https://www.law.go.kr/lsSc.do?section=&menuId=1&subMenuId=15&tabMenuId=81&eventGubun=060101&query="
    return base + urllib.parse.quote(_safe(query))

# 링크 빌더: lsId 직행 > (옵션)시트 URL > 검색(법령명+조문)
def _build_source_url(rec: Dict) -> str:
    lsid = _safe(rec.get("lsId") or rec.get("lsid") or rec.get("LSID"))
    if lsid:
        return f"https://www.law.go.kr/lsInfoP.do?lsId={urllib.parse.quote(lsid)}"
    src = _safe(rec.get("source_url"))
    if os.getenv("PREFER_SHEET_URL") == "1" and src.startswith("http") and "law.go.kr" in src:
        return src
    law_name = _safe(rec.get("law_name","")); article = _safe(rec.get("article_no",""))
    q = f"{law_name} {article}".strip()
    return _law_search_url(q if q else law_name)

ROLE_TOKENS = ["안전관리자","보건관리자","안전보건총괄책임자","관리감독자","안전보건관리담당자","산업보건의"]
def _detect_role(q: str) -> Optional[str]:
    for r in ROLE_TOKENS:
        if r in q: return r
    return None

# 빈도/주기 전수 스캔(키워드)
FREQ_PAT = re.compile(r"(반기\s*1회(?:\s*이상)?|반기|6\s*개월(?:\s*1회(?:\s*이상)?)?|분기|정기)")
VERB_PAT = re.compile(r"(점검|평가|관리|확인|검토)")

# ---------- YAML 보조 로더 ----------
def _load_yaml(path: str) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f: return yaml.safe_load(f)
    except Exception: return None

def load_from_yaml() -> List[Dict]:
    out=[]
    for y in glob.glob(os.path.join(LAWS_DIR, "**", "*.yml"), recursive=True):
        r=_load_yaml(y)
        if not r or not r.get("law_id") or not r.get("article_no"): continue
        r["_text"]=(r.get("text_plain") or _strip_html(r.get("text_html"))).strip()
        r["_source"]="yaml"; r["_level"]=_law_level(r.get("law_name",""))
        # YAML에는 세그먼트가 없을 수 있으므로 _segments 없음
        out.append(r)
    return out

# ---------- Sheets 로더 (ALL 탭 + 한국어 헤더) ----------
def _sheets_service():
    creds = Credentials.from_service_account_file(
        GOOGLE_CREDS, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds)

def _ranges_from_env(svc):
    rng=(SHEETS_RANGE or "").strip()
    if rng.upper()=="ALL":
        meta=svc.spreadsheets().get(spreadsheetId=SHEETS_ID).execute()
        titles=[s["properties"]["title"] for s in meta.get("sheets",[])]
        return [f"{t}!A:Z" for t in titles]
    parts=[r.strip() for r in rng.split(",") if r.strip()]
    return parts if parts else ["Sheet1!A:Z"]

def _process_values(values: List[List[str]]) -> List[Dict]:
    if not values: return []
    header=[h.strip() for h in values[0]]; idx={k:i for i,k in enumerate(header)}
    def ci(*names):
        for n in names:
            if n in idx: return idx[n]
        return None
    # 시트 헤더
    c_rev=ci("최신개정일","개정일","revision_date")
    c_law_id=ci("법령ID","law_id","ID")
    c_law_name=ci("법령명","law_name")
    c_article=ci("조문번호","article_no")
    c_unit=ci("조","unit","조문구분","구분")
    c_title=ci("조문제목","article_title")
    c_text=ci("조문내용(Plain)","text","본문")
    c_html=ci("조문내용(HTML)","text_html")
    c_deleted=ci("삭제여부(Y/N)","삭제여부","삭제")
    c_src=ci("출처URL","source_url","URL","url")
    c_lsid=ci("lsId","LSID","lsid","법제처ID")
    # 세그먼트용
    c_para=ci("항번호"); c_ho=ci("호번호"); c_mok=ci("목번호")

    def g(row, i): 
        return (row[i].strip() if i is not None and i < len(row) and row[i] is not None else "")

    by_key: Dict[Tuple[str,str], Dict]={}

    for row in values[1:]:
        if _safe(g(row,c_deleted)).upper()=="Y": continue
        law_id=_safe(g(row,c_law_id)); law_name=_safe(g(row,c_law_name)); article_no=_safe(g(row,c_article))
        if not law_id or not article_no: continue

        unit=_safe(g(row,c_unit))
        title=_safe(g(row,c_title))
        rev=_safe(g(row,c_rev))
        src=_safe(g(row,c_src))
        lsid=_safe(g(row,c_lsid))
        t_plain=_safe(g(row,c_text))
        t_html=_safe(g(row,c_html))
        para=_safe(g(row,c_para)); ho=_safe(g(row,c_ho)); mok=_safe(g(row,c_mok))

        key=(law_id,article_no)
        is_head=("조" in unit) or (key not in by_key)

        if is_head:
            rec={
                "law_id":law_id,"law_name":law_name,"article_no":article_no,"article_title":title,
                "revision_date":rev,"db_synced_at":_now_iso().split("T")[0],"status":"유효",
                "source_url":src,"lsId":lsid,
                "text_plain":(t_plain+"\n") if t_plain else "", "text_html":(t_html+"\n") if t_html else "",
                "_source":"sheets","_level":_law_level(law_name),
                "_segments":[]  # 항/호/목 세그먼트 누적
            }
            by_key[key]=rec
        else:
            # 본문 누적(백업용)
            if t_plain: by_key[key]["text_plain"]+=t_plain+"\n"
            if t_html:  by_key[key]["text_html"] +=t_html+"\n"
            # 세그먼트 추가
            seg_text = t_plain or _strip_html(t_html)
            if seg_text:
                by_key[key]["_segments"].append({
                    "para": para, "ho": ho, "mok": mok,
                    "text": seg_text
                })

    out=[]
    for rec in by_key.values():
        rec["_text"]=(rec.get("text_plain") or _strip_html(rec.get("text_html"))).strip()
        out.append(rec)
    return out

def load_from_sheets() -> List[Dict]:
    if not (SHEETS_ID and GOOGLE_CREDS): return []
    try:
        svc=_sheets_service(); ranges=_ranges_from_env(svc)
        resp=svc.spreadsheets().values().batchGet(spreadsheetId=SHEETS_ID, ranges=ranges).execute()
        valueRanges=resp.get("valueRanges",[])
        merged=[]
        for vr in valueRanges:
            merged.extend(_process_values(vr.get("values",[])))
        return merged
    except Exception:
        return []

# ---------- 전체 리로드 ----------
def reload_all():
    global LAWS
    s=load_from_sheets(); y=load_from_yaml()
    seen=set(); merged=[]
    for rec in s+y:  # 시트 우선
        key=(rec.get("law_id"), rec.get("article_no"))
        if key in seen: continue
        seen.add(key); merged.append(rec)
    LAWS=merged

reload_all()

# ---------- 간단 검색 ----------
def _score(hay: str, kw: str) -> int:
    score=0
    for t in kw.split():
        if t in hay: score+=hay.count(t)
    if kw in hay: score+=3
    return score

def _search_local(keyword: str, limit: int = 16) -> List[Dict]:
    kw=keyword.strip(); res=[]
    for r in LAWS:
        hay=f"{r.get('law_name','')} {r.get('article_no','')} {r.get('article_title','')} {r.get('_text','')}"
        sc=_score(hay, kw)
        if sc>0: res.append((sc,r))
    res.sort(key=lambda x:x[0], reverse=True)
    return [x[1] for x in res[:limit]]

# ---------- 전수 스캔(세그먼트 우선) ----------
def _mk_path(article: str, para: str, ho: str, mok: str) -> str:
    path = article or ""
    if para: path += f"제{para}항"
    if ho:   path += f"제{ho}호"
    if mok:  path += f"{mok}목"
    return path or article

def _scan_frequency_segments(rec: Dict) -> List[Tuple[str,str]]:
    """세그먼트에 빈도 키워드가 있으면 (경로, 스니펫) 반환; 없으면 빈 리스트"""
    out: List[Tuple[str,str]] = []
    segs: List[Dict] = rec.get("_segments") or []
    for sg in segs:
        txt = (sg.get("text") or "").strip()
        if not txt: continue
        if FREQ_PAT.search(txt) and VERB_PAT.search(txt):
            snippet = re.sub(r"\s+"," ", txt)
            out.append((_mk_path(rec.get("article_no",""), sg.get("para",""), sg.get("ho",""), sg.get("mok","")), snippet))
    # 중복 제거
    seen=set(); uniq=[]
    for p,s in out:
        k=(p, re.sub(r"\s+"," ",s))
        if k in seen: continue
        seen.add(k); uniq.append((p,s))
    return uniq

# ---------- 엔드포인트 ----------
@app.get("/healthz", operation_id="healthz")
def healthz():
    sheets=sum(1 for r in LAWS if r.get("_source")=="sheets")
    yaml_n=sum(1 for r in LAWS if r.get("_source")=="yaml")
    return {"ok": True, "ts": _now_iso(), "laws_loaded": len(LAWS), "sheets": sheets, "yaml": yaml_n}

@app.get("/reload", operation_id="reload")
def reload():
    reload_all()
    sheets=sum(1 for r in LAWS if r.get("_source")=="sheets")
    yaml_n=sum(1 for r in LAWS if r.get("_source")=="yaml")
    return {"ok": True, "reloaded": len(LAWS), "sheets": sheets, "yaml": yaml_n}

@app.get("/search", operation_id="search")
def search(keyword: str = Query(..., min_length=1), limit: int = 10):
    hits=_search_local(keyword, limit)
    return {"count": len(hits), "items":[
        {"law_id":h.get("law_id"),"law_name":h.get("law_name"),"article_no":h.get("article_no"),
         "title":h.get("article_title"),"revision_date":h.get("revision_date"),"db_synced_at":h.get("db_synced_at"),
         "level":h.get("_level"),"source":h.get("_source")}
        for h in hits]}

def _group_by_level(hits: List[Dict]) -> Dict[str, List[Dict]]:
    buckets={"act":[], "decree":[], "rule":[], "notice":[]}
    for r in hits: buckets.setdefault(r.get("_level","act"), []).append(r)
    for k in buckets: buckets[k]=buckets[k][:3]  # 레벨별 최대 3개
    return buckets

def _summarize(text: str) -> str:
    return _ellipsis(re.sub(r"\s+"," ", (text or "").strip()), 220)

def _basis_block_for(rec: Dict, scan_freq: bool=False) -> str:
    # [근거] — 반말체
    law_name=rec.get("law_name",""); article=rec.get("article_no",""); title=rec.get("article_title","")
    rev=rec.get("revision_date",""); summary=_summarize(rec.get("_text",""))
    url=_build_source_url(rec)
    lines=[]
    lines.append(f"- **법령명:** {law_name}")
    lines.append(f"- **조문:** {article}({title})")
    lines.append(f"- **최신개정일:** {rev}")
    lines.append(f"- **원문 요지:** {summary}")
    if scan_freq:
        # 1순위: 세그먼트 사용, 2순위: 없음(세그먼트 없으면 출력 생략)
        matches=_scan_frequency_segments(rec)
        if len(matches)>=2:
            for path, snip in matches:
                mm=_ellipsis(snip, 140)
                lines.append(f"  - `{path}` — “**{mm}**”")
    lines.append(f"- **출처:** {_safe_link('국가법령정보센터 바로가기', url)}")
    return "\n".join(lines)

def _compose_blocks(keyword: str, role_lock: Optional[str], include_all_levels: bool, scan_frequency: bool) -> Tuple[str,str]:
    hits=_search_local(keyword, 16)
    if not hits:
        srch=_law_search_url(keyword)
        basis=f"📌 **[근거]**\n- 원문을 찾지 못했다. 내부 DB(시트/로컬)에 해당 조문이 없다.\n- **검색 경로:** {_safe_link('국가법령정보센터 검색', srch)}"
        body=("**내용 요약**\n"
              "- 법률 → 시행령 → 시행규칙 → 고시·지침 순서로 최신 원문을 확인해 주세요.\n"
              "- 조문·별표 정확 일치 항목만 인용합니다.\n"
              "※ 추가 확인: 상·하위법 개정일을 꼭 비교해 주세요.")
        return basis, body

    role = role_lock or _detect_role(keyword) or ""
    if role:
        role_hits=[r for r in hits if role in (r.get("_text","")+r.get("article_title","")+r.get("law_name",""))]
        if role_hits: hits=role_hits + [r for r in hits if r not in role_hits]

    buckets=_group_by_level(hits)
    order=["act","decree","rule","notice"] if include_all_levels else ["decree"]
    labels={"act":"(법률)","decree":"(시행령)","rule":"(시행규칙)","notice":"(고시·지침)"}

    basis_parts=["📌 **[근거]**"]
    for lv in order:
        if not buckets.get(lv): continue
        for rec in buckets[lv]:
            basis_parts.append(f"- **{labels[lv]}**")
            basis_parts.append(_basis_block_for(rec, scan_freq=scan_frequency))
    basis_md="\n".join(basis_parts)

    body_lines=["**내용 요약**"]
    if role: body_lines.append(f"- 본 질의는 **{role}** 관련으로 해석했습니다(역할 잠금).")
    if scan_frequency: body_lines.append("- 요청하신 **반기 1회 이상** 관련 조항을 전수 매칭해 요지를 정리했습니다.")
    body_lines.append("- 상위법 우선 원칙을 적용했고, 직접 관련된 하위법만 포함했습니다.")
    body_lines.append("※ 추가 확인: 상·하위법의 **최신개정일**이 서로 다를 수 있으니 반드시 비교해 주세요.")
    body_md="\n".join(body_lines)

    return basis_md, body_md

def _compose_markdown(basis_md: str, body_md: str, disclaimer: str) -> str:
    return f"{basis_md}\n\n---\n{body_md}\n\n---\n> ⚠️ **[면책고지]**\n> {disclaimer}"

@app.get("/answer", operation_id="answer")
def answer(
    keyword: str = Query(..., min_length=1),
    role_lock: Optional[str] = Query(None, description="역할 잠금: 안전관리자/보건관리자/안전보건총괄책임자/관리감독자/안전보건관리담당자/산업보건의"),
    include_all_levels: bool = Query(True, description="법·령·규칙·고시까지 다층 근거 출력"),
    scan_frequency: Optional[bool] = Query(None, description="‘반기 1회 이상’ 등 빈도 전수 스캔"),
):
    if scan_frequency is None:
        scan_frequency = bool(re.search(r"(반기|6\s*개월|분기|1회\s*이상|정기)", keyword))

    basis_md, body_md = _compose_blocks(keyword, role_lock, include_all_levels, scan_frequency)
    markdown = _compose_markdown(basis_md, body_md, DISCLAIMER)

    return {
        "status":"ok","generated_at":_now_iso(),
        "legal_basis":basis_md,"middle":body_md,"disclaimer":DISCLAIMER,
        "markdown": markdown,
        "params":{"role_lock": role_lock or _detect_role(keyword),
                  "include_all_levels": include_all_levels,
                  "scan_frequency": scan_frequency}
    }

@app.get("/diag", operation_id="diag")
def diag():
    info={"sheets_id_set": bool(SHEETS_ID), "creds_path": GOOGLE_CREDS, "range": SHEETS_RANGE}
    try:
        creds=Credentials.from_service_account_file(
            GOOGLE_CREDS, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        svc=build("sheets","v4",credentials=creds)
        meta=svc.spreadsheets().get(spreadsheetId=SHEETS_ID).execute()
        titles=[s["properties"]["title"] for s in meta.get("sheets",[])]
        info.update({"ok": True, "sheet_titles": titles, "laws_loaded": len(LAWS)})
        return info
    except Exception as e:
        info.update({"ok": False, "error_type": e.__class__.__name__, "error": str(e)})
        return JSONResponse(info, status_code=500)
