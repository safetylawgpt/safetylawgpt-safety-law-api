import os
from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__)

# ====== 설정 ======
STRICT_MODE = True  # 결과 없거나 필수 필드 부족 시 실패 처리
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

def is_html(text: str) -> bool:
    t = (text or "").lower()
    return ("<html" in t) or ("<!doctype html" in t)

def parse_xml(text: str):
    try:
        root = ET.fromstring(text)
        return root, None
    except ET.ParseError as e:
        return None, str(e)

def http_get(url, params, headers, timeout=12, retries=1):
    last_exc = None
    for _ in range(max(1, retries)):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.encoding = "utf-8"
            return resp
        except Exception as e:
            last_exc = e
    raise last_exc if last_exc else Exception("HTTP GET 실패")

def trim(s):
    return (s or "").strip()

def extract_text(elem):
    return "".join(elem.itertext()) if elem is not None else ""

def num_from_korean(no_text, default=0):
    # "제4조", "제3호", "3항" 등에서 숫자만 추출
    if not no_text:
        return default
    digits = "".join(ch for ch in no_text if ch.isdigit())
    return int(digits) if digits.isdigit() else default

def build_article_link(law_id):
    # 조문별 앵커까지는 공개 API에서 일관 제공되지 않아 본문 링크만 제공
    return f"https://www.law.go.kr/법령/{law_id}"

# ====== DRF XML 파서 ======
def parse_search_list(root):
    """ lawSearch.do 결과 파싱 """
    laws = []
    for law in root.findall("law"):
        law_name = trim(law.findtext("법령명한글"))
        law_id   = trim(law.findtext("법령ID"))
        pub      = trim(law.findtext("공포일자"))
        enf      = trim(law.findtext("시행일자"))
        dept     = trim(law.findtext("소관부처명"))

        if STRICT_MODE and (not law_name or not law_id or not enf):
            continue

        laws.append({
            "법령명": law_name,
            "법령ID": law_id,
            "공포일자": pub,
            "시행일자": enf,
            "소관부처": dept,
            "링크": f"https://www.law.go.kr/법령/{law_id}",
            "source": "law.go.kr"
        })
    return laws

def parse_law_full_xml(root):
    """
    lawService.do 결과에서 메타 + 조문(항/호/목 구조) 반환
    대표 필드:
      <법령명한글>, <법령ID>, <시행일자>
      <조문> (여러개), 내부에 <조문번호>, <조문제목>, <조문내용>, <항>..(<호>/<목>..)
    """
    meta = {
        "법령명": trim(root.findtext("법령명한글")),
        "법령ID": trim(root.findtext("법령ID")),
        "시행일자": trim(root.findtext("시행일자")),
        "링크": build_article_link(trim(root.findtext("법령ID")))
    }
    articles = root.findall(".//조문")
    return meta, articles

def scan_keyword_in_articles(law_meta, articles, keyword):
    """ 조/항/호/목 전수 스캔 (키워드 포함 전부 수집 후 번호순 정렬) """
    matches = []

    for art in articles:
        art_no  = trim(art.findtext("조문번호"))
        art_ttl = trim(art.findtext("조문제목"))
        art_txt = trim(art.findtext("조문내용")) or trim(extract_text(art))

        # 조문 본문
        if keyword and (keyword in art_txt):
            matches.append({
                "법령명": law_meta.get("법령명"),
                "법령ID": law_meta.get("법령ID"),
                "시행일자": law_meta.get("시행일자"),
                "조문번호": art_no,
                "조문제목": art_ttl,
                "항번호": "",
                "호번호": "",
                "목번호": "",
                "원문": art_txt,
                "링크": build_article_link(law_meta.get("법령ID", "")),
                "source": "law.go.kr"
            })

        # 항
        for hang in art.findall(".//항"):
            hang_no  = trim(hang.findtext("항번호"))
            hang_txt = trim(hang.findtext("항내용")) or trim(extract_text(hang))
            if keyword and (keyword in hang_txt):
                matches.append({
                    "법령명": law_meta.get("법령명"),
                    "법령ID": law_meta.get("법령ID"),
                    "시행일자": law_meta.get("시행일자"),
                    "조문번호": art_no,
                    "조문제목": art_ttl,
                    "항번호": hang_no,
                    "호번호": "",
                    "목번호": "",
                    "원문": hang_txt,
                    "링크": build_article_link(law_meta.get("법령ID", "")),
                    "source": "law.go.kr"
                })

            # 호
            for ho in hang.findall(".//호"):
                ho_no  = trim(ho.findtext("호번호"))
                ho_txt = trim(ho.findtext("호내용")) or trim(extract_text(ho))
                if keyword and (keyword in ho_txt):
                    matches.append({
                        "법령명": law_meta.get("법령명"),
                        "법령ID": law_meta.get("법령ID"),
                        "시행일자": law_meta.get("시행일자"),
                        "조문번호": art_no,
                        "조문제목": art_ttl,
                        "항번호": hang_no,
                        "호번호": ho_no,
                        "목번호": "",
                        "원문": ho_txt,
                        "링크": build_article_link(law_meta.get("법령ID", "")),
                        "source": "law.go.kr"
                    })

                # 목
                for mok in ho.findall(".//목"):
                    mok_no  = trim(mok.findtext("목번호"))
                    mok_txt = trim(mok.findtext("목내용")) or trim(extract_text(mok))
                    if keyword and (keyword in mok_txt):
                        matches.append({
                            "법령명": law_meta.get("법령명"),
                            "법령ID": law_meta.get("법령ID"),
                            "시행일자": law_meta.get("시행일자"),
                            "조문번호": art_no,
                            "조문제목": art_ttl,
                            "항번호": hang_no,
                            "호번호": ho_no,
                            "목번호": mok_no,
                            "원문": mok_txt,
                            "링크": build_article_link(law_meta.get("법령ID", "")),
                            "source": "law.go.kr"
                        })

    matches.sort(
        key=lambda x: (
            num_from_korean(x.get("조문번호")),
            num_from_korean(x.get("항번호")),
            num_from_korean(x.get("호번호")),
            num_from_korean(x.get("목번호")),
        )
    )
    return matches

# ====== 헬스체크 ======
@app.get("/healthz")
def healthz():
    return {"ok": True, "guideline_loaded": GUIDELINE_TEXT != GUIDELINE_FALLBACK}

# ====== 1) 법령 검색(목록) ======
@app.get("/search")
def search_law():
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "keyword 파라미터를 입력하세요."}), 400

    params = {
        "OC": OC_KEY,
        "target": "law",
        "type": "XML",
        "query": keyword
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/xml",
        "Referer": "https://www.law.go.kr",
        "Content-Type": "application/xml; charset=UTF-8"
    }

    try:
        resp = http_get(LAW_SEARCH_URL, params=params, headers=headers, timeout=12, retries=2)

        if not resp.ok or not resp.text:
            return hard_fail()

        if is_html(resp.text):
            return hard_fail("API 응답이 XML이 아닙니다.")

        root, err = parse_xml(resp.text)
        if err or root is None:
            return hard_fail(f"XML 파싱 실패: {err}")

        laws = parse_search_list(root)
        if STRICT_MODE and not laws:
            return hard_fail()

        return jsonify({"ok": True, "guideline": GUIDELINE_TEXT[:2000], "data": laws})

    except Exception as e:
        return hard_fail(f"요청 실패: {str(e)}")

# ====== 2) 법령 전체 조문 조회 ======
@app.get("/law")
def get_law():
    law_id = request.args.get("id", "").strip()
    if not law_id:
        return jsonify({"error": "id 파라미터(법령ID)가 필요합니다."}), 400

    params = {"OC": OC_KEY, "target": "law", "type": "XML", "ID": law_id}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/xml",
        "Referer": "https://www.law.go.kr",
        "Content-Type": "application/xml; charset=UTF-8"
    }

    try:
        resp = http_get(LAW_SERVICE_URL, params=params, headers=headers, timeout=15, retries=2)
        if not resp.ok or not resp.text:
            return hard_fail("법령 원문 조회 실패")
        if is_html(resp.text):
            return hard_fail("법령 원문 응답이 XML이 아닙니다.")

        root, err = parse_xml(resp.text)
        if err or root is None:
            return hard_fail(f"법령 원문 XML 파싱 실패: {err}")

        meta, articles = parse_law_full_xml(root)
        return jsonify({
            "ok": True,
            "guideline": GUIDELINE_TEXT[:2000],
            "meta": meta,
            "article_count": len(articles)
        })
    except Exception as e:
        return hard_fail(f"요청 실패: {str(e)}")

# ====== 3) 법령ID + 키워드 전수 스캔 ======
@app.get("/scan")
def scan_keyword():
    law_id = request.args.get("id", "").strip()
    keyword = request.args.get("keyword", "").strip()

    if not law_id or not keyword:
        return jsonify({"error": "id(법령ID)와 keyword 파라미터가 필요합니다."}), 400

    params = {"OC": OC_KEY, "target": "law", "type": "XML", "ID": law_id}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/xml",
        "Referer": "https://www.law.go.kr",
        "Content-Type": "application/xml; charset=UTF-8"
    }

    try:
        resp = http_get(LAW_SERVICE_URL, params=params, headers=headers, timeout=20, retries=2)
        if not resp.ok or not resp.text:
            return hard_fail("법령 원문 조회 실패")
        if is_html(resp.text):
            return hard_fail("법령 원문 응답이 XML이 아닙니다.")

        root, err = parse_xml(resp.text)
        if err or root is None:
            return hard_fail(f"법령 원문 XML 파싱 실패: {err}")

        meta, articles = parse_law_full_xml(root)
        matches = scan_keyword_in_articles(meta, articles, keyword)

        if STRICT_MODE and not matches:
            return jsonify({
                "ok": False,
                "message": "키워드가 포함된 조문을 찾지 못했습니다.",
                "guideline": GUIDELINE_TEXT[:2000],
                "source": "law.go.kr",
                "data": []
            }), 200

        return jsonify({
            "ok": True,
            "guideline": GUIDELINE_TEXT[:2000],
            "keyword": keyword,
            "count": len(matches),
            "data": matches
        })
    except Exception as e:
        return hard_fail(f"요청 실패: {str(e)}")

# ====== 4) 법령명 + 키워드 (검색→원문→전수 스캔) ======
@app.get("/scan_by_name")
def scan_by_name():
    """
    사용법:
      /scan_by_name?name=중대재해 처벌 등에 관한 법률 시행령&keyword=반기
    동작:
      - lawSearch.do로 name과 가장 잘 맞는 법령 1건 선택(최신 시행일 우선)
      - lawService.do로 원문 조회
      - keyword 포함된 모든 조/항/호/목 전수 스캔
    """
    name = request.args.get("name", "").strip()
    keyword = request.args.get("keyword", "").strip

