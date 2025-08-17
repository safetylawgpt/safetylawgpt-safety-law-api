# server.py — 안전법 도우미 최종본
# 어떤 질문이 와도 "법적 근거 → 절차 요약 → 서식 안내 → 면책 고지문" 형식으로 응답
# 공식 서식이 없을 땐: 세이프티 코리아(티스토리) & 안전보건 실전 소통방(카카오) 자동 안내

import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict
from flask import Flask, request, jsonify, Response
import requests
from requests.adapters import HTTPAdapter, Retry

app = Flask(__name__)

# ===== 설정 =====
STRICT_MODE = False  # 실패해도 형식 유지 폴백
OC_KEY = os.getenv("NLIC_API_KEY", "").strip() or "dangerous99"
LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
LAW_SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"

GUIDELINE_PATH = os.path.join(os.path.dirname(__file__), "08.07 구성지침.txt")
GUIDELINE_FALLBACK = "정확한 조문을 찾을 수 없습니다. 국가법령정보센터에서 직접 확인하십시오."

DISCLAIMER = (
    "본 응답은 [안전법 도우미 GPT]가 제공하는 참고용 법령 정보입니다.\n"
    "정확한 법률 해석은 변호사 등 전문가와 상담하시기 바랍니다.\n"
    "본 정보는 국가법령정보센터 및 고용노동부 고시 등을 기반으로 제공합니다."
)

FALLBACK_RESOURCES = [
    {
        "title": "세이프티 코리아 – 서식/작성 예시",
        "desc": "공식 별지 서식이 명시되지 않은 경우 참고 자료",
        "url": "https://safety-korea.tistory.com/",
        "publisher": "세이프티 코리아"
    },
    {
        "title": "카카오톡 오픈채팅 ‘안전보건 실전소통방’",
        "desc": "현장 사례 공유 및 서식 작성 Q&A",
        "url": "https://open.kakao.com/o/g49w3IEh",
        "publisher": "세이프티 코리아"
    }
]

# ===== 지침 로드 =====
def load_guideline():
    try:
        with open(GUIDELINE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return GUIDELINE_FALLBACK

GUIDELINE_TEXT = load_guideline()

# ===== 공통 유틸 =====
def hard_fail(msg=None):
    return jsonify({"ok": False, "message": msg or GUIDELINE_FALLBACK, "source": "law.go.kr", "data": []}), 200

def headers_xml():
    return {"User-Agent": "Mozilla/5.0", "Accept": "application/xml", "Referer": "https://www.law.go.kr", "Content-Type": "application/xml; charset=UTF-8"}

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
    retries = Retry(total=3, backoff_factor=0.4, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(["GET", "POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    return s

def norm(s: str) -> str:
    if s is None: return ""
    n = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", n).strip()

def yyyymmdd(s: str) -> str:
    s = re.sub(r"\D", "", s or "")
    return s if len(s) == 8 else "00000000"

# ===== 응답 스키마 =====
@dataclass
class SourceRef:
    label: str
    url: str

@dataclass
class LegalBasisItem:
    law_name: str
    article: str
    summary: str
    text: str
    title: Optional[str] = None
    effective_date: Optional[str] = None
    sources: List[SourceRef] = None

@dataclass
class SafetyAnswerV1:
    key: str
    legal_basis: List[LegalBasisItem]
    procedure: List[str]
    forms: List[Dict[str, str]]
    disclaimer: str
    def to_dict(self):
        d = asdict(self)
        for lb in d["legal_basis"]:
            if lb["sources"] is None: lb["sources"] = []
        return d

def make_answer_v1(key, law_name, article, title, effective_date, summary, text, source_url, procedure_steps, forms_list, disclaimer_text):
    lb = LegalBasisItem(law_name=law_name, article=article, title=title, effective_date=effective_date, summary=summary, text=text, sources=[SourceRef(label="국가법령정보센터", url=source_url)])
    ans = SafetyAnswerV1(key=key, legal_basis=[lb], procedure=procedure_steps or [], forms=forms_list or [], disclaimer=disclaimer_text)
    return ans.to_dict()

# ===== 조문 평문 구성 =====
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
    for etc_tag in ("비고", "참고", "각주"):
        for etc in article_el.findall(f".//{etc_tag}"):
            if etc.text: lines.append(etc.text.strip())
    cleaned = [ln for ln in (ln.strip() for ln in lines) if ln]
    out, seen = [], set()
    for ln in cleaned:
        if ln not in seen:
            seen.add(ln); out.append(ln)
    return "\n".join(out)

# ===== DRF: 법령 조회/검색 =====
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
            if norm(nm) == wanted and lid and enf:
                rows.append({"법령명": nm, "법령ID": lid, "시행일자": yyyymmdd(enf)})
        if not rows: return None, "정확 일치 법령 없음"
        rows.sort(key=lambda x: x["시행일자"], reverse=True)
        return rows[0], None
    except Exception as e:
        return None, f"검색 실패: {e}"

# ===== 절차/서식 추출 규칙 =====
RE_DEADLINE = re.compile(r"(\d+)\s*일\s*이내")
RE_FORM = re.compile(r"별지\s*제?\s*(\d+)(?:호의?\d*호)?\s*서식")
RE_AUTH = re.compile(r"(관할\s*[가-힣]+관서|지방고용노동관서|산재예방과|산업안전과)")


def extract_procedure_steps(text: str):
    steps = []
    if RE_FORM.search(text): steps.append("조문에 명시된 별지 서식으로 보고서 작성")
    m_auth = RE_AUTH.search(text)
    if m_auth: steps.append(f"제출처: {m_auth.group(1)}")
    m_dead = RE_DEADLINE.search(text)
    if m_dead: steps.append(f"제출 기한: {m_dead.group(0)}")
    if not steps: steps.append("조문 내 절차 문구를 확인하십시오.")
    def sort_key(x):
        if x.startswith("조문에 명시된 별지 서식"): return 0
        if x.startswith("제출처:"): return 1
        if x.startswith("제출 기한:"): return 2
        return 9
    steps.sort(key=sort_key)
    return steps

def extract_forms(text: str):
    forms, seen = [], set()
    for m in RE_FORM.finditer(text):
        no = m.group(1)
        title = f"별지 제{no}호 서식"
        if title in seen: continue
        seen.add(title)
        forms.append({"title": title, "desc": f"조문 확인된 서식 번호: 제{no}호", "url": "https://www.moel.go.kr/", "publisher": "고용노동부"})
    return forms

def ensure_forms_with_fallback(forms: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return forms if forms else list(FALLBACK_RESOURCES)

# ===== 특정 조문 찾기 =====
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
                h_head = h.findtext("항내용") or ""
                if h_head: section.append(h_head.strip())
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

# ===== 키워드 → 법령/조문 라우팅(간단버전) =====
TOPIC_RULES = [
    # 표준안(해임 신고)
    {"keywords": ["해임", "안전관리자"], "law": "산업안전보건법 시행규칙", "cite": "제11조 제2항"},
    {"keywords": ["해임", "보건관리자"], "law": "산업안전보건법 시행규칙", "cite": "제11조 제2항"},
    # 위험성평가
    {"keywords": ["위험성평가"], "law": "산업안전보건법", "cite": "제36조"},
    # 교육
    {"keywords": ["정기", "교육"], "law": "산업안전보건법", "cite": "제31조"},
    {"keywords": ["채용", "교육"], "law": "산업안전보건법", "cite": "제31조"},
    {"keywords": ["특별교육"], "law": "산업안전보건법", "cite": "제31조"},
    # 작업중지
    {"keywords": ["작업중지"], "law": "산업안전보건법", "cite": "제52조"},
    {"keywords": ["급박", "위험"], "law": "산업안전보건법", "cite": "제52조"},
    # 폭염/고열
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

# ===== 표준 형식 응답 빌더 =====
def build_structured_answer(law_hint: str, citation_hint: str, query: str) -> Dict:
    picked, err = pick_latest_exact_law(law_hint)
    if err or not picked:
        forms_fb = ensure_forms_with_fallback([])
        return make_answer_v1(
            key=f"{law_hint}-{citation_hint}", law_name=law_hint, article=citation_hint,
            title=None, effective_date=None, summary="(요약) 해당 조문을 확인하십시오.",
            text="(조문 원문 로딩 실패) 국가법령정보센터에서 해당 조문을 직접 확인하십시오.",
            source_url="https://www.law.go.kr/",
            procedure_steps=[
                "조문에 명시된 별지 서식으로 보고서 작성(미확인 시 참고 자료 활용)",
                "제출처: 관할 지방고용노동관서(산재예방과) 등",
                "제출 기한: 조문상 정한 기한(예: OO일 이내)"
            ],
            forms_list=forms_fb, disclaimer_text=DISCLAIMER
        )

    root, err = fetch_law_by_id(picked["법령ID"])
    if err or root is None:
        forms_fb = ensure_forms_with_fallback([])
        return make_answer_v1(
            key=f"{picked['법령명']}-{citation_hint}", law_name=picked["법령명"], article=citation_hint,
            title=None, effective_date=picked["시행일자"], summary="(요약) 해당 조문을 확인하십시오.",
            text="(조문 원문 로딩 실패) 국가법령정보센터에서 해당 조문을 직접 확인하십시오.",
            source_url=f"https://www.law.go.kr/법령/{picked['법령ID']}",
            procedure_steps=[
                "조문에 명시된 별지 서식으로 보고서 작성(미확인 시 참고 자료 활용)",
                "제출처: 관할 지방고용노동관서(산재예방과) 등",
                "제출 기한: 조문상 정한 기한(예: OO일 이내)"
            ],
            forms_list=forms_fb, disclaimer_text=DISCLAIMER
        )

    # 조문이 지정되면 해당 조문/항, 아니면 전체에서 스캔한 느낌 그대로 본문 사용
    art = None
    if citation_hint:
        art = find_article_by_citation(root, citation_hint)
    if not art:
        first = root.find(".//조문")
        text = extract_article_plaintext(first) if first is not None else ""
        art = {"조문번호": citation_hint or (first.findtext("조문번호") if first is not None else ""),
               "조문제목": (first.findtext("조문제목") if first is not None else "") or "",
               "본문": text or "(해당 조문을 찾지 못했습니다. 포털에서 확인하십시오.)"}

    procedure = extract_procedure_steps(art["본문"])
    forms = ensure_forms_with_fallback(extract_forms(art["본문"]))

    summary_bits = []
    m_dead = RE_DEADLINE.search(art["본문"])
    if m_dead: summary_bits.append(f"{m_dead.group(0)} 내 보고 의무")
    if RE_FORM.search(art["본문"]): summary_bits.append("별지 서식에 따른 보고서 제출")
    if not summary_bits: summary_bits.append("조문에 따른 보고·제출 의무가 적용됨")
    summary = " · ".join(summary_bits)

    return make_answer_v1(
        key=f"{picked['법령명']}-{citation_hint or '전문'}",
        law_name=picked["법령명"], article=art["조문번호"] or "전문",
        title=art["조문제목"], effective_date=picked["시행일자"],
        summary=summary, text=art["본문"],
        source_url=f"https://www.law.go.kr/법령/{picked['법령ID']}",
        procedure_steps=procedure, forms_list=forms, disclaimer_text=DISCLAIMER
    )

# ===== 헬스체크/보조 API =====
@app.get("/healthz")
def healthz():
    return {"ok": True, "guideline_loaded": GUIDELINE_TEXT != GUIDELINE_FALLBACK}

@app.get("/guideline")
def get_guideline():
    return jsonify({"ok": True, "guideline": GUIDELINE_TEXT})

@app.get("/search")
def search_law_api():
    keyword = request.args.get("keyword", "").strip()
    exact = request.args.get("exact", "0").strip() in ("1", "true", "True")
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
            rows.append({"법령명": law_name, "법령ID": law_id, "공포일자": pub, "시행일자": enf, "소관부처": dept, "링크": f"https://www.law.go.kr/법령/{law_id}", "source": "law.go.kr"})
        rows.sort(key=lambda x: yyyymmdd(x["시행일자"]), reverse=True)
        if STRICT_MODE and not rows: return hard_fail()
        return jsonify({"ok": True, "guideline": GUIDELINE_TEXT[:2000], "data": rows})
    except Exception as e:
        return hard_fail(f"요청 실패: {str(e)}")

@app.get("/scan")
def scan_keyword():
    law_name = request.args.get("law_name", "").strip()
    keyword = request.args.get("keyword", "").strip()
    if not law_name or not keyword:
        return jsonify({"ok": False, "message": "law_name, keyword가 필요합니다.", "data": []}), 200
    picked, err = pick_latest_exact_law(law_name)
    if err or not picked: return hard_fail(err or "정확 일치 법령 없음")
    root, err = fetch_law_by_id(picked["법령ID"])
    if err or root is None: return hard_fail(err or "전문 조회 실패")
    matches = []
    for article in root.findall(".//조문"):
        article_no = (article.findtext("조문번호") or "").strip()
        article_title = (article.findtext("조문제목") or "").strip()
        full_text = extract_article_plaintext(article)
        if keyword in full_text:
            matches.append({"조문번호": article_no, "조문제목": article_title, "조문원문": full_text})
    if not matches: return hard_fail(f"'{keyword}'가 포함된 조문을 찾지 못했습니다.")
    def art_key(x):
        m = re.search(r"\d+", x.get("조문번호","")); return int(m.group()) if m else 999999
    matches.sort(key=art_key)
    return jsonify({"ok": True, "law_name": picked["법령명"], "law_id": picked["법령ID"], "시행일자": picked["시행일자"], "keyword": keyword, "match_count": len(matches), "matches": matches}), 200

@app.get("/scan_periodic")
def scan_periodic():
    law_name = request.args.get("law_name", "").strip()
    keyword = request.args.get("keyword", "반기").strip()
    if not law_name:
        return jsonify({"ok": False, "message": "law_name이 필요합니다.", "data": []}), 200
    with app.test_request_context(f"/scan?law_name={law_name}&keyword={keyword}"):
        return scan_keyword()

# ===== 표준 형식 자동 응답 =====
@app.get("/answer")
def answer():
    q = request.args.get("q", "").strip()
    if not q: return jsonify({"ok": False, "message": "q(질문)을 입력하세요."}), 400
    law_hint, cite_hint = guess_target(q)
    data = build_structured_answer(law_hint, cite_hint, q)
    return jsonify({"ok": True, **data})

@app.get("/answer.md")
def answer_md():
    q = request.args.get("q", "").strip()
    if not q: return Response("q(질문)을 입력하세요.", mimetype="text/plain; charset=utf-8")
    law_hint, cite_hint = guess_target(q)
    data = build_structured_answer(law_hint, cite_hint, q)
    lines = []
    lines.append("# 법적 근거")
    for art in data["legal_basis"]:
        srcs = ", ".join([f"[{s['label']}]({s['url']})" for s in art.get("sources", [])]) or "N/A"
        txt = (art["text"] or "").strip()
        if len(txt) > 1000: txt = txt[:1000] + " …"
        ed = f" (개정 {art['effective_date']})" if art.get("effective_date") else ""
        title = f" — {art['title']}" if art.get("title") else ""
        lines.append(f"- **{art['law_name']} {art['article']}**{ed}{title}\n  - _원문_: {txt}\n  - 출처: {srcs}")
    lines.append("\n# 절차 요약")
    if data["procedure"]:
        for i, s in enumerate(data["procedure"], 1):
            lines.append(f"{i}) {s}")
    else:
        lines.append("- (조문 내 절차 문구를 확인하십시오.)")
    lines.append("\n# 서식 안내")
    if data["forms"]:
        for f in data["forms"]:
            lines.append(f"- **{f['title']}** — {f['desc']} ({f['publisher']})\n  링크: {f['url']}")
    else:
        lines.append("- 조문 내 서식 번호가 확인되지 않았습니다.")
        lines.append("  · 참고: 세이프티 코리아(티스토리) — https://safety-korea.tistory.com/")
        lines.append("  · 카카오톡 오픈채팅 ‘안전보건 실전소통방’ — https://open.kakao.com/o/g49w3IEh")
    lines.append("\n# 면책 고지문")
    lines.append(DISCLAIMER)
    return Response("\n".join(lines), mimetype="text/markdown; charset=utf-8")

# ===== 메인 =====
if __name__ == "__main__":
    # 예:
    #  - JSON: http://127.0.0.1:5000/answer?q=건설업 안전관리자 해임 신고 방법
    #  - MD:   http://127.0.0.1:5000/answer.md?q=폭염 휴식 기준
    app.run(host="0.0.0.0", port=5000)
