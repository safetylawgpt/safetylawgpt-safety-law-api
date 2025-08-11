import os
from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__)

# ===== 설정 =====
STRICT_MODE = True                         # 신뢰성 부족하면 실패 문구
OC_KEY = os.getenv("NLIC_API_KEY", "").strip() or "dangerous99"
LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"    # 목록
LAW_SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"  # 본문(조문)
TIMEOUT = 20

GUIDELINE_PATH = os.path.join(os.path.dirname(__file__), "08.07 구성지침.txt")
GUIDELINE_FALLBACK = "정확한 조문을 찾을 수 없습니다. 국가법령정보센터에서 직접 확인하십시오."

# ===== 공통 유틸 =====
def load_guideline():
    try:
        with open(GUIDELINE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return GUIDELINE_FALLBACK

GUIDELINE_TEXT = load_guideline()

def ok(data=None, **kw):
    return jsonify({"ok": True, "guideline": GUIDELINE_TEXT[:2000], "data": data or [], **kw}), 200

def hard_fail(msg=None):
    # 커넥터가 끊기지 않도록 항상 JSON으로 정상 200 반환
    return jsonify({
        "ok": False,
        "message": msg or GUIDELINE_FALLBACK,
        "source": "law.go.kr",
        "data": []
    }), 200

def is_html(text: str) -> bool:
    t = (text or "").lower()
    return ("<html" in t) or ("<!doctype html" in t)

def parse_xml(text: str):
    try:
        root = ET.fromstring(text)
        return root, None
    except ET.ParseError as e:
        return None, str(e)

def fetch_xml(url: str, params: dict):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/xml",
        "Referer": "https://www.law.go.kr",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    resp.encoding = "utf-8"
    if not resp.ok or not resp.text or is_html(resp.text):
        return None, "API 응답이 XML이 아닙니다."
    return parse_xml(resp.text)

# ===== 내부: 목록/상세 =====
def search_law_list(keyword: str):
    params = {"OC": OC_KEY, "target": "law", "type": "XML", "query": keyword}
    root, err = fetch_xml(LAW_SEARCH_URL, params)
    if err or root is None:
        return None, err or "XML 파싱 실패"
    items = []
    for law in root.findall("law"):
        name = (law.findtext("법령명한글") or "").strip()
        law_id = (law.findtext("법령ID") or "").strip()
        pub = (law.findtext("공포일자") or "").strip()
        enf = (law.findtext("시행일자") or "").strip()
        dept = (law.findtext("소관부처명") or "").strip()
        if STRICT_MODE and (not name or not law_id or not enf):
            continue
        items.append({
            "법령명": name,
            "법령ID": law_id,
            "공포일자": pub,
            "시행일자": enf,
            "소관부처": dept,
            "링크": f"https://www.law.go.kr/법령/{law_id}",
            "source": "law.go.kr",
        })
    if STRICT_MODE and not items:
        return None, "검색 결과 없음"
    return items, None

def get_law_xml_by_id(law_id: str):
    # 본문(조문) 전체 XML
    params = {"OC": OC_KEY, "target": "law", "type": "XML", "ID": law_id}
    root, err = fetch_xml(LAW_SERVICE_URL, params)
    return root, err

def pick_exact_law_by_name(keyword: str, exact_name: str):
    items, err = search_law_list(keyword)
    if err or not items:
        return None, err or "검색 결과 없음"
    # 정확 일치만 채택
    exacts = [x for x in items if (x.get("법령명") or "") == exact_name]
    if not exacts:
        return None, "법령명 불일치"
    # 최신 시행일 우선
    exacts.sort(key=lambda x: x.get("시행일자", ""), reverse=True)
    return exacts[0], None

# ===== 내부: 조문/텍스트 추출 =====
def text_of(elem):
    # 해당 요소의 모든 하위 텍스트를 공백 정리해서 합치기
    parts = []
    for t in elem.itertext():
        s = (t or "").strip()
        if s:
            parts.append(s)
    return " ".join(parts).strip()

def find_articles(root):
    # 조문 요소 후보를 광의로 수집(태그명이 환경마다 조금씩 달라 XML 불일치 대비)
    # 가장 일반적인 태그: "조문"
    candidates = root.findall(".//조문")
    if candidates:
        return candidates
    # 혹시 다른 구조면 '조문번호'가 있는 상위 요소를 조문으로 간주
    alt = []
    for e in root.iter():
        if e.find("조문번호") is not None or e.find("조문제목") is not None:
            alt.append(e)
    return alt

def article_number_of(elem):
    n = elem.findtext("조문번호")
    if n:
        return n.strip()
    # 번호 태그가 없으면 텍스트에서 '제123조' 패턴 추출
    txt = text_of(elem)
    # 매우 보수적으로 숫자만 비교할 수 있게 처리
    import re
    m = re.search(r"제\s*(\d+)\s*조", txt)
    return m.group(1) if m else ""

def article_title_of(elem):
    t = elem.findtext("조문제목")
    return (t or "").strip()

def article_text_block(elem):
    # 조문 전체 원문(항/호/목 포함) 텍스트
    return text_of(elem)

def pick_article_by_number(root, article_no: str):
    for art in find_articles(root):
        no = article_number_of(art)
        if no == str(article_no).strip():
            return art
    return None

def scan_keyword_from_law(root, keyword: str):
    """조문 단위를 기준으로 키워드 포함 문장을 전수 추출"""
    out = []
    k = (keyword or "").strip()
    if not k:
        return out
    for art in find_articles(root):
        full_txt = article_text_block(art)
        if k in full_txt:
            out.append({
                "조문번호": article_number_of(art),
                "조문제목": article_title_of(art),
                "원문": full_txt
            })
    return out

# ===== 엔드포인트 =====
@app.get("/healthz")
def healthz():
    return ok({"service": "alive"})

@app.get("/guideline")
def get_guideline():
    return jsonify({"ok": True, "guideline": GUIDELINE_TEXT})

# 1) 목록 검색
@app.get("/search")
def search_endpoint():
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return jsonify({"ok": False, "message": "검색어를 입력하세요.", "data": []}), 200
    try:
        items, err = search_law_list(keyword)
        if err or not items:
            return hard_fail()
        return ok(items)
    except Exception as e:
        return hard_fail(f"요청 실패: {e}")

# 2) 전수 스캔(법령명 정확 지정 + 키워드)
@app.get("/scan_by_name")
def scan_by_name():
    name = request.args.get("name", "").strip()
    keyword = request.args.get("keyword", "").strip()
    if not name or not keyword:
        return jsonify({"ok": False, "message": "name과 keyword를 입력하세요.", "data": []}), 200
    try:
        # 목록에서 정확 일치 법령 하나 선택
        picked, err = pick_exact_law_by_name(name, name)
        if err or not picked:
            return hard_fail("법령명 불일치 또는 검색 실패")
        law_id = picked["법령ID"]
        root, err = get_law_xml_by_id(law_id)
        if err or root is None:
            return hard_fail("본문 조회 실패")
        hits = scan_keyword_from_law(root, keyword)
        if STRICT_MODE and not hits:
            return hard_fail()
        # 번호 오름차순으로 정렬(숫자로 비교 시 실패하면 문자열 기준)
        def sort_key(x):
            no = x.get("조문번호") or ""
            try:
                return int(no)
            except:
                return 999999
        hits.sort(key=sort_key)
        return ok({
            "법령명": picked["법령명"],
            "법령ID": law_id,
            "시행일자": picked["시행일자"],
            "keyword": keyword,
            "결과": hits
        })
    except Exception as e:
        return hard_fail(f"요청 실패: {e}")

# 3) 특정 조문 정확 조회(법령명 + 조문번호)
@app.get("/find_by_name_and_article")
def find_by_name_and_article():
    name = request.args.get("name", "").strip()
    art_no = request.args.get("article_no", "").strip()
    if not name or not art_no:
        return jsonify({"ok": False, "message": "name과 article_no를 입력하세요.", "data": []}), 200
    try:
        picked, err = pick_exact_law_by_name(name, name)
        if err or not picked:
            return hard_fail("법령명 불일치 또는 검색 실패")
        law_id = picked["법령ID"]
        root, err = get_law_xml_by_id(law_id)
        if err or root is None:
            return hard_fail("본문 조회 실패")
        art = pick_article_by_number(root, art_no)
        if art is None:
            return hard_fail("해당 조문을 찾지 못했습니다.")
        data = {
            "법령명": picked["법령명"],
            "법령ID": law_id,
            "시행일자": picked["시행일자"],
            "조문번호": article_number_of(art),
            "조문제목": article_title_of(art),
            "원문": article_text_block(art)
        }
        return ok(data)
    except Exception as e:
        return hard_fail(f"요청 실패: {e}")

if __name__ == "__main__":
    # 로컬 실행용
    app.run(host="0.0.0.0", port=5000)

