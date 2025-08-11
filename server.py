# server.py
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter, Retry
from flask import Flask, request, jsonify

app = Flask(__name__)

# ====== 설정 ======
STRICT_MODE = True  # API 원문 없으면 무조건 실패
OC_KEY = os.getenv("NLIC_API_KEY", "").strip() or "dangerous99"  # Render 환경변수 가능

LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
LAW_SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"

GUIDELINE_PATH = os.path.join(os.path.dirname(__file__), "08.07 구성지침.txt")
GUIDELINE_FALLBACK = (
    "정확한 조문을 찾을 수 없습니다. 국가법령정보센터에서 직접 확인하십시오."
)

# ====== 공통 유틸 ======
def load_guideline():
    try:
        with open(GUIDELINE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return GUIDELINE_FALLBACK

GUIDELINE_TEXT = load_guideline()

def hard_fail(msg=None):
    return jsonify({
        "ok": False,
        "message": msg or GUIDELINE_FALLBACK,
        "source": "law.go.kr",
        "data": []
    }), 200

def headers_xml():
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/xml",
        "Referer": "https://www.law.go.kr",
        "Content-Type": "application/xml; charset=UTF-8"
    }

def is_html(text: str) -> bool:
    t = text.lower()
    return ("<html" in t) or ("<!doctype html" in t)

def parse_xml(text: str):
    try:
        root = ET.fromstring(text)
        return root, None
    except ET.ParseError as e:
        return None, str(e)

def session_with_retries():
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"])
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    return s

def norm(s: str) -> str:
    if s is None:
        return ""
    # NFKC 정규화 + 한글/영문 대소문자/공백 차이 제거
    n = unicodedata.normalize("NFKC", s)
    n = re.sub(r"\s+", "", n).strip()
    return n

def yyyymmdd(s: str) -> str:
    # 'YYYYMMDD' 또는 변형을 'YYYYMMDD'로 보정
    s = re.sub(r"\D", "", s or "")
    if len(s) == 8:
        return s
    return "00000000"

def extract_article_plaintext(article_el: ET.Element) -> str:
    """
    법제처 DRF XML의 '조문' 엘리먼트에서 조문 전체 원문(조/항/호/목)을
    줄바꿈과 번호를 최대한 살려 평문으로 구성.
    """
    lines = []

    # 조문 머리(조문내용) 수집
    for t in article_el.findall("./조문내용"):
        if t.text:
            lines.append(t.text.strip())

    # 항(제1항 등)
    for h_el in article_el.findall("./항"):
        h_no = h_el.findtext("항번호") or ""
        h_head = h_el.findtext("항내용")
        if h_head:
            prefix = f"{h_no} " if h_no else ""
            lines.append(f"{prefix}{h_head.strip()}")

        # 호 (1., 2. 등)
        for ho_el in h_el.findall("./호"):
            ho_no = ho_el.findtext("호번호") or ""
            ho_text = ho_el.findtext("호내용")
            if ho_text:
                prefix = f"{ho_no} " if ho_no else ""
                lines.append(f"{prefix}{ho_text.strip()}")

            # 목 (가., 나. 등) — 일부 법령에 존재
            for mok_el in ho_el.findall("./목"):
                mok_no = mok_el.findtext("목번호") or ""
                mok_text = mok_el.findtext("목내용")
                if mok_text:
                    prefix = f"{mok_no} " if mok_no else ""
                    lines.append(f"{prefix}{mok_text.strip()}")

    # 부칙/비고 등 기타 텍스트가 있으면 덧붙임
    for etc_tag in ("비고", "참고", "각주"):
        for etc in article_el.findall(f".//{etc_tag}"):
            if etc.text:
                lines.append(etc.text.strip())

    # 공백 줄 제거, 중복 줄 제거
    cleaned = [ln for ln in (ln.strip() for ln in lines) if ln]
    # 동일 라인 중복 방지
    out = []
    seen = set()
    for ln in cleaned:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)

    return "\n".join(out)

def fetch_law_by_id(law_id: str):
    """
    lawService.do 로 법령 전문 XML을 조회하여 루트 반환
    """
    s = session_with_retries()
    params = {"OC": OC_KEY, "target": "law", "type": "XML", "ID": law_id}
    try:
        resp = s.get(LAW_SERVICE_URL, params=params, headers=headers_xml(), timeout=12)
        resp.encoding = "utf-8"
        if not resp.ok or not resp.text:
            return None, "전문 응답 없음"
        if is_html(resp.text):
            return None, "전문 응답이 XML이 아님(HTML)"
        root, err = parse_xml(resp.text)
        if err or root is None:
            return None, f"전문 XML 파싱 실패: {err}"
        return root, None
    except Exception as e:
        return None, f"전문 조회 실패: {e}"

def pick_latest_exact_law(law_name_query: str):
    """
    lawSearch.do 결과에서 질문한 법령명과 '정확 일치'하는 항목만 추려
    시행일자 최신 순으로 정렬해 최상위 1건을 반환
    """
    s = session_with_retries()
    params = {"OC": OC_KEY, "target": "law", "type": "XML", "query": law_name_query}
    try:
        resp = s.get(LAW_SEARCH_URL, params=params, headers=headers_xml(), timeout=12)
        resp.encoding = "utf-8"

        if not resp.ok or not resp.text:
            return None, "검색 응답 없음"
        if is_html(resp.text):
            return None, "검색 응답이 XML이 아님(HTML)"

        root, err = parse_xml(resp.text)
        if err or root is None:
            return None, f"검색 XML 파싱 실패: {err}"

        wanted = norm(law_name_query)
        rows = []
        for law in root.findall("law"):
            nm = (law.findtext("법령명한글") or "").strip()
            lid = (law.findtext("법령ID") or "").strip()
            enf = (law.findtext("시행일자") or "").strip()
            if norm(nm) == wanted and lid and enf:
                rows.append({
                    "법령명": nm,
                    "법령ID": lid,
                    "시행일자": yyyymmdd(enf)
                })

        if not rows:
            return None, "정확 일치 법령 없음"

        rows.sort(key=lambda x: x["시행일자"], reverse=True)
        return rows[0], None

    except Exception as e:
        return None, f"검색 실패: {e}"

# ====== 헬스체크 ======
@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "guideline_loaded": GUIDELINE_TEXT != GUIDELINE_FALLBACK
    }

# ====== 법령 검색(목록) ======
@app.get("/search")
def search_law():
    keyword = request.args.get("keyword", "").strip()
    exact = request.args.get("exact", "0").strip() in ("1", "true", "True")
    if not keyword:
        return jsonify({"error": "검색어를 입력하세요."}), 400

    s = session_with_retries()
    params = {"OC": OC_KEY, "target": "law", "type": "XML", "query": keyword}

    try:
        resp = s.get(LAW_SEARCH_URL, params=params, headers=headers_xml(), timeout=12)
        resp.encoding = "utf-8"

        if not resp.ok or not resp.text:
            return hard_fail()

        if is_html(resp.text):
            return hard_fail("API 응답이 XML이 아닙니다.")

        root, err = parse_xml(resp.text)
        if err or root is None:
            return hard_fail(f"XML 파싱 실패: {err}")

        rows = []
        for law in root.findall("law"):
            law_name = (law.findtext("법령명한글") or "").strip()
            law_id   = (law.findtext("법령ID") or "").strip()
            pub      = (law.findtext("공포일자") or "").strip()
            enf      = (law.findtext("시행일자") or "").strip()
            dept     = (law.findtext("소관부처명") or "").strip()

            if STRICT_MODE and (not law_name or not law_id or not enf):
                continue

            if exact and norm(law_name) != norm(keyword):
                continue

            rows.append({
                "법령명": law_name,
                "법령ID": law_id,
                "공포일자": pub,
                "시행일자": enf,
                "소관부처": dept,
                "링크": f"https://www.law.go.kr/법령/{law_id}",
                "source": "law.go.kr"
            })

        # 최신 시행일 우선 정렬
        rows.sort(key=lambda x: yyyymmdd(x["시행일자"]), reverse=True)

        if STRICT_MODE and not rows:
            return hard_fail()

        return jsonify({"ok": True, "guideline": GUIDELINE_TEXT[:2000], "data": rows})

    except Exception as e:
        return hard_fail(f"요청 실패: {str(e)}")

# ====== 법령 전문 전수 스캔 (임의 키워드) ======
@app.get("/scan")
def scan_keyword():
    law_name = request.args.get("law_name", "").strip()
    keyword = request.args.get("keyword", "").strip()
    if not law_name or not keyword:
        return jsonify({"ok": False, "message": "law_name, keyword가 필요합니다.", "data": []}), 200

    # 1) 최신 시행일 + 법령명 정확일치 검색
    picked, err = pick_latest_exact_law(law_name)
    if err or not picked:
        return hard_fail(err or "정확 일치 법령 없음")

    # 2) 법령 전문 조회
    root, err = fetch_law_by_id(picked["법령ID"])
    if err or root is None:
        return hard_fail(err or "전문 조회 실패")

    # 3) 조·항·호 전체 전수 스캔
    matches = []
    for article in root.findall(".//조문"):
        article_no = (article.findtext("조문번호") or "").strip()
        article_title = (article.findtext("조문제목") or "").strip()
        full_text = extract_article_plaintext(article)
        if keyword in full_text:
            matches.append({
                "조문번호": article_no,
                "조문제목": article_title,
                "조문원문": full_text
            })

    if not matches:
        return hard_fail(f"'{keyword}'가 포함된 조문을 찾지 못했습니다.")

    # 조문번호 자연 정렬(숫자 우선)
    def art_key(x):
        m = re.search(r"\d+", x.get("조문번호",""))
        return int(m.group()) if m else 999999
    matches.sort(key=art_key)

    return jsonify({
        "ok": True,
        "law_name": picked["법령명"],
        "law_id": picked["법령ID"],
        "시행일자": picked["시행일자"],
        "keyword": keyword,
        "match_count": len(matches),
        "matches": matches
    }), 200

# ====== 주기성 전수 스캔 (기본: '반기') ======
@app.get("/scan_periodic")
def scan_periodic():
    law_name = request.args.get("law_name", "").strip()
    keyword = request.args.get("keyword", "반기").strip()  # 기본 '반기'
    if not law_name:
        return jsonify({"ok": False, "message": "law_name이 필요합니다.", "data": []}), 200

    return scan_keyword()

# ====== 실패 시 가이드라인만 돌려주는 안전 엔드포인트 ======
@app.get("/guideline")
def get_guideline():
    return jsonify({"ok": True, "guideline": GUIDELINE_TEXT})

# ====== 메인 ======
if __name__ == "__main__":
    # 로컬 실행용
    app.run(host="0.0.0.0", port=5000)
