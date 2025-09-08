# server.py — SafetyLawGPT (Sheets 우선 + 자유형식 + 면책 고지문 맨 끝)
# - 답변은 자유 형식, 마지막 줄에 면책 고지문만 고정
# - 본문에 자연스럽게 블로그/카톡방 유도
# - 데이터 소스: Google Sheets 1차 → law.go.kr DRF 폴백
# - 시트 스키마 자동 감지(열명 유사 매핑), 여러 탭 동시 검색(법/령/규칙 우선), 조문단위별 재조립

import os, re, json, unicodedata, xml.etree.ElementTree as ET
from typing import List, Optional, Dict
from flask import Flask, request, jsonify, Response
import requests
from requests.adapters import HTTPAdapter, Retry

# (선택) CORS
try:
    from flask_cors import CORS
except Exception:
    CORS = None

# (Google Sheets)
try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread, Credentials = None, None

app = Flask(__name__)
if CORS:
    CORS(app)

# ===== 설정 =====
STRICT_MODE = False
OC_KEY = os.getenv("NLIC_API_KEY", "").strip() or "dangerous99"
LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
LAW_SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"

BASE_DIR = os.path.dirname(__file__)
GUIDELINE_FILE = "09.08 구성지침.txt"  # ✅ 이 파일만 사용
GUIDELINE_PATH = os.path.join(BASE_DIR, GUIDELINE_FILE)

DISCLAIMER = (
    "본 응답은 [안전법 도우미 GPT]가 제공하는 참고용 법령 정보입니다.\n"
    "정확한 법률 해석은 변호사 등 전문가와 상담하시기 바랍니다.\n"
    "본 정보는 국가법령정보센터 및 고용노동부 고시 등을 기반으로 제공합니다."
)

FALLBACK_LINKS = [
    "- 블로그: https://safety-korea.tistory.com/",
    "- 실전소통방: https://open.kakao.com/o/g49w3IEh",
]

# Google Sheets ENV
GS_ENABLED = os.getenv("GOOGLE_SHEETS_ENABLED", "1").strip().lower() in ("1", "true", "yes")
GS_KEY = os.getenv("GOOGLE_SHEETS_KEY", "1uHQLgnYoyaHRE2ecjDojUpHkJekUUlRnJbAn513FH94").strip()
GS_TABS = [t.strip() for t in os.getenv("GOOGLE_SHEETS_TAB", "").split(",") if t.strip()]  # 비우면 전체 탭 자동
GS_CRED_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

# ===== 유틸 =====
def headers_xml():
    return {"User-Agent": "Mozilla/5.0", "Accept": "application/xml",
            "Referer": "https://www.law.go.kr", "Content-Type": "application/xml; charset=UTF-8"}

def is_html(text: str) -> bool:
    t = (text or "").lower()
    return ("<html" in t) or ("<!doctype html" in t)

def parse_xml(text: str):
    try:
        root = ET.fromstring(text)
        return root, None
    except ET.ParseError as e:
        return None, str(e)

def session_with_retries():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset(["GET", "POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    return s

def norm(s: str) -> str:
    if s is None: return ""
    n = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", n).strip()

def yyyymmdd(s: str) -> str:
    s = re.sub(r"\D", "", s or "")
    return s if len(s) == 8 else ""

def hard_fail(msg=None):
    return jsonify({"ok": False, "message": msg or "국가법령정보센터에서 직접 확인하십시오.", "source": "law.go.kr", "data": []}), 200

# ===== DRF =====
def fetch_law_by_id(law_id: str):
    s = session_with_retries()
    params = {"OC": OC_KEY, "target": "law", "type": "XML", "ID": law_id}
    try:
        resp = s.get(LAW_SERVICE_URL, params=params, headers=headers_xml(), timeout=12)
        resp.encoding = "utf-8"
        if not resp.ok or not resp.text: return None, "전문 응답 없음"
        if is_html(resp.text): return None, "전문 응답이 XML이 아님(HTML)"
        root, err = parse_xml(resp.text)
        if err or root is None: return None, f"전문 XML 파싱 실패: {err}"
        return root, None
    except Exception as e:
        return None, f"전문 조회 실패: {e}"

def pick_latest_exact_law(law_name_query: str):
    s = session_with_retries()
    params = {"OC": OC_KEY, "target": "law", "type": "XML", "query": law_name_query}
    try:
        resp = s.get(LAW_SEARCH_URL, params=params, headers=headers_xml(), timeout=12)
        resp.encoding = "utf-8"
        if not resp.ok or not resp.text: return None, "검색 응답 없음"
        if is_html(resp.text): return None, "검색 응답이 XML이 아님(HTML)"
        root, err = parse_xml(resp.text)
        if err or root is None: return None, f"검색 XML 파싱 실패: {err}"
        wanted = norm(law_name_query)
        rows = []
        for law in root.findall("law"):
            nm  = (law.findtext("법령명한글") or "").strip()
            lid = (law.findtext("법령ID") or "").strip()
            enf = (law.findtext("시행일자") or "").strip()
            if norm(nm) == wanted and lid:
                rows.append({"법령명": nm, "법령ID": lid, "시행일자": yyyymmdd(enf)})
        if not rows: return None, "정확 일치 법령 없음"
        rows.sort(key=lambda x: x["시행일자"] or "00000000", reverse=True)
        return rows[0], None
    except Exception as e:
        return None, f"검색 실패: {e}"

# ===== Google Sheets =====
_gs_client = None
def get_gs_client():
    global _gs_client
    if not (GS_ENABLED and gspread and Credentials and GS_KEY and GS_CRED_JSON):
        return None
    if _gs_client: return _gs_client
    try:
        info = json.loads(GS_CRED_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        _gs_client = gspread.authorize(creds)
        return _gs_client
    except Exception:
        return None

# 열명 매핑(느슨한 스키마 자동 감지)
COL_PATTERNS = {
    "정렬순서": re.compile(r"정렬|순서|order|sort", re.I),
    "법령ID":   re.compile(r"법령\s*id|법령ID|law\s*id|lawid", re.I),
    "법령명":   re.compile(r"법령명|법\s*명|law\s*name", re.I),
    "법령유형": re.compile(r"유형|타입|type|분류", re.I),
    "조문번호": re.compile(r"조문번호|조\s*문\s*번|조문\s*no|article\s*no", re.I),
    "조문단위": re.compile(r"조문단위|단위|unit", re.I),
    "최신개정일": re.compile(r"최신개정|개정일|revision|amend", re.I),
    "시행일":   re.compile(r"시행일|effective|발효", re.I),
    "조문제목": re.compile(r"조문제목|제목|title", re.I),
    "조문내용(Plain)": re.compile(r"(조문)?내용(\(plain\))?|본문|text|plain", re.I),
    "조문내용(HTML)":  re.compile(r"html", re.I),
    "조문경로": re.compile(r"경로|path", re.I),
    "출처URL":  re.compile(r"출처|url|링크|link", re.I),
}

def normalize_keys(row: Dict[str, str]) -> Dict[str, str]:
    def match_key(k):
        kk = re.sub(r"\s+", "", str(k))
        for std, pat in COL_PATTERNS.items():
            if pat.search(kk):
                return std
        return k
    out = {}
    for k, v in row.items():
        out[match_key(k)] = v
    return out

def rank_tab(title: str) -> int:
    t = title or ""
    if re.search(r"(본법|^법$|산업안전보건법)", t): return 0
    if re.search(r"(시행령|령\b)", t): return 1
    if re.search(r"(시행규칙|기준규칙|규칙\b)", t): return 2
    return 9

def gs_all_rows() -> List[Dict[str, str]]:
    client = get_gs_client()
    if not client: return []
    rows: List[Dict[str,str]] = []
    try:
        sh = client.open_by_key(GS_KEY)
        worksheets = []
        if GS_TABS:
            for tab in GS_TABS:
                try:
                    ws = sh.worksheet(tab)
                    worksheets.append(ws)
                except Exception:
                    continue
        else:
            worksheets = sh.worksheets()
            worksheets.sort(key=lambda w: rank_tab(w.title))
        for ws in worksheets:
            data = ws.get_all_records(empty2zero=False, head=1)
            for r in data:
                nr = normalize_keys(r)
                nr["_TAB"] = ws.title
                rows.append(nr)
        return rows
    except Exception:
        return []

def sheet_find_by_citation(law_name_hint: str, citation_hint: str):
    if not (law_name_hint and citation_hint): return None
    rows = gs_all_rows()
    if not rows: return None
    law_norm = norm(law_name_hint)
    cit_norm = norm(citation_hint)
    bucket: List[Dict[str,str]] = []
    for r in rows:
        nm  = norm(str(r.get("법령명","")))
        art = norm(str(r.get("조문번호","")))
        if (law_norm in nm) and (art and (cit_norm.startswith(art) or art.startswith(cit_norm))):
            bucket.append(r)
    if not bucket: return None

    unit_rank = {"조": 0, "항": 1, "호": 2, "목": 3}
    def sort_key(r):
        ordv = r.get("정렬순서", "")
        try: ordv = int(str(ordv).strip() or "0")
        except: ordv = 0
        return (ordv if ordv>0 else 999999, unit_rank.get(str(r.get("조문단위","")).strip(), 9))

    bucket.sort(key=sort_key)

    title = None
    lines = []
    for r in bucket:
        if not title and r.get("조문제목"):
            title = str(r.get("조문제목"))
        t = str(r.get("조문내용(Plain)") or r.get("조문내용") or "").strip()
        if t:
            lines.append(t)
    body = "\n".join(lines).strip()
    return {
        "법령명": bucket[0].get("법령명",""),
        "법령ID": str(bucket[0].get("법령ID","")),
        "시행일자": str(bucket[0].get("시행일","") or ""),
        "조문번호": citation_hint,
        "조문제목": title or "",
        "본문": body,
        "출처URL": bucket[0].get("출처URL",""),
    }

def sheet_search_by_keyword(keyword: str, limit: int = 30):
    if not keyword: return []
    rows = gs_all_rows()
    if not rows: return []
    K = keyword.strip()
    hits = []
    for r in rows:
        law = str(r.get("법령명",""))
        title = str(r.get("조문제목",""))
        body = str(r.get("조문내용(Plain)") or r.get("조문내용") or "")
        if (K in law) or (K in title) or (K in body):
            hits.append(r)
            if len(hits) >= limit:
                break
    hits.sort(key=lambda r: rank_tab(r.get("_TAB","")))
    return hits

# ===== DRF 조문 평문 추출 =====
def extract_article_plaintext(article_el: ET.Element) -> str:
    lines = []
    for t in article_el.findall("./조문내용"):
        if t.text: lines.append(t.text.strip())
    for h_el in article_el.findall("./항"):
        h_no = (h_el.findtext("항번호") or "").strip()
        h_head = h_el.findtext("항내용")
        if h_head:
            prefix = f"{h_no} " if h_no else ""
            lines.append(f"{prefix}{h_head.strip()}")
        for ho_el in h_el.findall("./호"):
            ho_no = (ho_el.findtext("호번호") or "").strip()
            ho_text = ho_el.findtext("호내용")
            if ho_text:
                prefix = f"{ho_no} " if ho_no else ""
                lines.append(f"{prefix}{ho_text.strip()}")
            for mok_el in ho_el.findall("./목"):
                mok_no = (mok_el.findtext("목번호") or "").strip()
                mok_text = mok_el.findtext("목내용")
                if mok_text:
                    prefix = f"{mok_no} " if mok_no else ""
                    lines.append(f"{prefix}{mok_text.strip()}")
    for tag in ("비고","참고","각주"):
        for etc in article_el.findall(f".//{tag}"):
            if etc.text: lines.append(etc.text.strip())
    cleaned = [ln for ln in (ln.strip() for ln in lines) if ln]
    out, seen = [], set()
    for ln in cleaned:
        if ln not in seen:
            seen.add(ln); out.append(ln)
    return "\n".join(out)

def find_article_by_citation(root: ET.Element, citation_hint: str):
    if not root: return None
    hint = (citation_hint or "").replace(" ", "")
    want_hang = None
    m = re.search(r"제(\d+)항", hint)
    if m: want_hang = m.group(1)
    for article in root.findall(".//조문"):
        art_no = (article.findtext("조문번호") or "").replace(" ", "")
        if not art_no or not hint.startswith(art_no): continue
        full_text = extract_article_plaintext(article)
        if want_hang:
            for h in article.findall("./항"):
                h_no = (h.findtext("항번호") or "").strip()
                section = []
                if (h.findtext("항내용") or "").strip():
                    section.append(h.findtext("항내용").strip())
                for ho_el in h.findall("./호"):
                    ho_no = (ho_el.findtext("호번호") or "").strip()
                    ho_text = (ho_el.findtext("호내용") or "").strip()
                    if ho_text:
                        prefix = f"{ho_no} " if ho_no else ""
                        section.append(f"{prefix}{ho_text}")
                    for mok_el in ho_el.findall("./목"):
                        mok_no = (mok_el.findtext("목번호") or "").strip()
                        mok_text = (mok_el.findtext("목내용") or "").strip()
                        if mok_text:
                            prefix = f"{mok_no} " if mok_no else ""
                            section.append(f"{prefix}{mok_text}")
                if h_no == f"제{want_hang}항":
                    return {"조문번호": f"{art_no} {h_no}", "조문제목": (article.findtext("조문제목") or "").strip(), "본문": "\n".join([x for x in section if x])}
            return {"조문번호": art_no, "조문제목": (article.findtext("조문제목") or "").strip(), "본문": full_text}
        return {"조문번호": art_no, "조문제목": (article.findtext("조문제목") or "").strip(), "본문": full_text}
    return None

# ===== 주제 힌트(간단) =====
TOPIC_RULES = [
    {"keywords": ["해임", "안전관리자"], "law": "산업안전보건법 시행규칙", "cite": "제11조 제2항"},
    {"keywords": ["해임", "보건관리자"], "law": "산업안전보건법 시행규칙", "cite": "제11조 제2항"},
    {"keywords": ["위험성평가"], "law": "산업안전보건법", "cite": "제36조"},
    {"keywords": ["정기", "교육"], "law": "산업안전보건법", "cite": "제31조"},
    {"keywords": ["채용", "교육"], "law": "산업안전보건법", "cite": "제31조"},
    {"keywords": ["특별교육"], "law": "산업안전보건법", "cite": "제31조"},
    {"keywords": ["작업중지"], "law": "산업안전보건법", "cite": "제52조"},
    {"keywords": ["급박", "위험"], "law": "산업안전보건법", "cite": "제52조"},
    {"keywords": ["폭염"], "law": "산업안전보건기준에 관한 규칙", "cite": ""},
    {"keywords": ["고열작업"], "law": "산업안전보건기준에 관한 규칙", "cite": ""},
]

def keywords_match(query: str, kw_list: list) -> bool:
    q = query.lower()
    return all(k.lower() in q for k in kw_list)

def guess_target(query: str):
    q = (query or "").strip()
    for rule in TOPIC_RULES:
        if keywords_match(q, rule["keywords"]):
            return (rule["law"], rule["cite"])
    m = re.search(r"제\s*\d+\s*조(\s*제\s*\d+\s*항)?", q)
    if m: return ("산업안전보건법 시행규칙", m.group().replace(" ", ""))
    return ("산업안전보건법 시행규칙", "제11조")

# ===== 자유형식 응답 생성 =====
def build_free_markdown(q: str):
    law_hint, cite_hint = guess_target(q)

    # 1) Sheets 우선
    art = sheet_find_by_citation(law_hint, cite_hint) if GS_ENABLED else None
    if not art and GS_ENABLED:
        hits = sheet_search_by_keyword(q)
        if hits:
            h = hits[0]
            art = {
                "법령명": h.get("법령명",""), "법령ID": h.get("법령ID",""),
                "시행일자": str(h.get("시행일","") or ""),
                "조문번호": h.get("조문번호",""), "조문제목": h.get("조문제목",""),
                "본문": str(h.get("조문내용(Plain)") or h.get("조문내용") or ""),
                "출처URL": h.get("출처URL",""),
            }

    # 2) DRF 폴백
    if not art:
        picked, err = pick_latest_exact_law(law_hint)
        if picked:
            root, err2 = fetch_law_by_id(picked["법령ID"])
            if root:
                found = find_article_by_citation(root, cite_hint) if cite_hint else None
                if not found:
                    first = root.find(".//조문")
                    txt = extract_article_plaintext(first) if first is not None else ""
                    found = {
                        "조문번호": cite_hint or (first.findtext("조문번호") if first is not None else ""),
                        "조문제목": (first.findtext("조문제목") if first is not None else "") or "",
                        "본문": txt or ""
                    }
                art = {
                    "법령명": picked["법령명"], "법령ID": picked["법령ID"],
                    "시행일자": picked.get("시행일자",""),
                    "조문번호": found["조문번호"], "조문제목": found["조문제목"],
                    "본문": found["본문"], "출처URL": f"https://www.law.go.kr/법령/{picked['법령ID']}",
                }

    # 3) 자유 형식 MD 구성 + 유도 + 면책
    lines = []
    if art:
        hbits = []
        if art.get("법령명"): hbits.append(art["법령명"])
        if art.get("조문번호"): hbits.append(art["조문번호"])
        if art.get("시행일자"): hbits.append(f"(시행 {art['시행일자']})")
        if art.get("조문제목"): hbits.append(f"— {art['조문제목']}")
        header = " ".join([x for x in hbits if x]).strip()
        if header: lines.append(f"**{header}**")

        body = (art.get("본문") or "").strip()
        if body:
            preview = body if len(body) <= 1800 else (body[:1800] + " …")
            lines.append(preview)

        if art.get("출처URL"):
            lines.append(f"\n**출처**: {art['출처URL']}")
    else:
        lines.append("관련 조문을 자동 식별하지 못했습니다. 키워드를 바꿔 다시 시도해 주세요.")

    # 자연스러운 안내
    lines.append("\n더 구체적인 서식·사례는 아래에서 빠르게 찾아보세요:")
    lines.extend(FALLBACK_LINKS)

    # 면책 고지문(맨 끝)
    lines.append("\n—\n" + DISCLAIMER)
    return "\n".join(lines)

# ===== 라우트 =====
@app.get("/healthz")
def healthz():
    guideline_loaded = os.path.exists(GUIDELINE_PATH)  # 실제 파일 존재로 판정
    return {"ok": True, "guideline_loaded": guideline_loaded}

@app.get("/guideline")
def get_guideline():
    try:
        with open(GUIDELINE_PATH, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        txt = "(지침 파일을 찾지 못했습니다)"
    return jsonify({"ok": True, "guideline": txt})

@app.get("/answer.md")
def answer_md():
    q = request.args.get("q", "").strip()
    if not q:
        return Response("q(질문)을 입력하세요.", mimetype="text/plain; charset=utf-8")
    md = build_free_markdown(q)   # ★ 자유형식 빌더 사용
    return Response(md, mimetype="text/markdown; charset=utf-8")

# (선택) 필요하면 유지: 검색/스캔
@app.get("/search")
def search_law_api():
    keyword = request.args.get("keyword", "").strip()
    exact = request.args.get("exact", "0").strip().lower() in ("1", "true", "yes")
    if not keyword: return jsonify({"error": "keyword를 입력하세요."}), 400
    s = session_with_retries()
    params = {"OC": OC_KEY, "target": "law", "type": "XML", "query": keyword}
    try:
        resp = s.get(LAW_SEARCH_URL, params=params, headers=headers_xml(), timeout=12); resp.encoding = "utf-8"
        if not resp.ok or not resp.text: return hard_fail()
        if is_html(resp.text): return hard_fail("API 응답이 XML이 아닙니다.")
        root, err = parse_xml(resp.text)
        if err or root is None: return hard_fail(f"XML 파싱 실패: {err}")
        rows = []
        for law in root.findall("law"):
            law_name = (law.findtext("법령명한글") or "").strip()
            law_id   = (law.findtext("법령ID") or "").strip()
            pub      = (law.findtext("공포일자") or "").strip()
            enf      = (law.findtext("시행일자") or "").strip()
            dept     = (law.findtext("소관부처명") or "").strip()
            if STRICT_MODE and (not law_name or not law_id or not enf): continue
            if exact and norm(law_name) != norm(keyword): continue
            rows.append({"법령명": law_name, "법령ID": law_id, "공포일자": pub, "시행일자": enf,
                        "소관부처": dept, "링크": f"https://www.law.go.kr/법령/{law_id}", "source": "law.go.kr"})
        rows.sort(key=lambda x: x.get("시행일자","") or "00000000", reverse=True)
        if STRICT_MODE and not rows: return hard_fail()
        return jsonify({"ok": True, "data": rows})
    except Exception as e:
        return hard_fail(f"요청 실패: {str(e)}")

# ===== 메인 =====
if __name__ == "__main__":
    PORT = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=PORT)
