import os
from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET

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

def is_html(text: str) -> bool:
    t = text.lower()
    return ("<html" in t) or ("<!doctype html" in t)

def parse_xml(text: str):
    try:
        root = ET.fromstring(text)
        return root, None
    except ET.ParseError as e:
        return None, str(e)

def http_get(url, params, headers, timeout=12, retries=1):
    """
    간단 재시도 포함 GET
    """
    last_exc = None
    for _ in range(max(1, retries)):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.encoding = "utf-8"
            return resp
        except Exception as e:
            last_exc = e
    raise last_exc if last_exc else Exception("HTTP GET 실패")

# ====== XML 트리 파서(법령 전체 스캔용) ======
def extract_text(elem):
    # 해당 노드 및 하위 전체 텍스트
    return "".join(elem.itertext()) if elem is not None else ""

def trim(s):
    return (s or "").strip()

def build_match_item(law_meta, path_dict, text, article_link=None):
    """
    결과 표준화
    """
    item = {
        "법령명": law_meta.get("법령명"),
        "법령ID": law_meta.get("법령ID"),
        "시행일자": law_meta.get("시행일자"),
        "조문번호": path_dict.get("조문번호", ""),
        "조문제목": path_dict.get("조문제목", ""),
        "항번호": path_dict.get("항번호", ""),
        "호번호": path_dict.get("호번호", ""),
        "목번호": path_dict.get("목번호", ""),
        "원문": trim(text),
        "링크": article_link or law_meta.get("링크"),
        "source": "law.go.kr",
    }
    return item

def get_article_link(law_id, article_no):
    # 국가법령정보센터 조문 링크 패턴(간이)
    # 법령 본문 링크: https://www.law.go.kr/법령/{법령ID}
    # 세부 조문 앵커는 사이트 구조상 파라미터/앵커가 변동될 수 있어, 일단 본문 링크 제공
    return f"https://www.law.go.kr/법령/{law_id}"

def parse_law_full_xml(root):
    """
    lawService.do XML에서 상단 메타 + 조문 구조 추출
    기대 구조(대표적인 필드):
      <법령명한글>, <법령ID>, <시행일자>
      <조문> 여러 개, 각 조문 안에 <조문번호>, <조문제목>, <조문내용> 및 <항>/<호>/<목> 중첩
    """
    meta = {
        "법령명": trim(root.findtext("법령명한글")),
        "법령ID": trim(root.findtext("법령ID")),
        "시행일자": trim(root.findtext("시행일자")),
        "링크": f"https://www.law.go.kr/법령/{trim(root.findtext('법령ID'))}",
    }

    articles = root.findall(".//조문")
    return meta, articles

def scan_keyword_in_articles(law_meta, articles, keyword):
    """
    전체 조문에서 키워드가 포함된 모든 조/항/호/목을 전수 추출
    번호 순서 보장을 위해 수집 후 간단 정렬 시도(조문번호 내 숫자 추출 기준)
    """
    matches = []

    for art in articles:
        art_no   = trim(art.findtext("조문번호"))
        art_ttl  = trim(art.findtext("조문제목"))
        art_txt  = trim(art.findtext("조문내용"))

        # 1) 조문 본문에서 직접 매칭
        if keyword in art_txt:
            matches.append(build_match_item(
                law_meta,
                {"조문번호": art_no, "조문제목": art_ttl},
                art_txt,
                get_article_link(law_meta.get("법령ID", ""), art_no)
            ))

        # 2) 항 단위 검사
        for hang in art.findall(".//항"):
            hang_no  = trim(hang.findtext("항번호"))
            hang_txt = trim(hang.findtext("항내용")) or trim(extract_text(hang))
            if keyword in hang_txt:
                matches.append(build_match_item(
                    law_meta,
                    {"조문번호": art_no, "조문제목": art_ttl, "항번호": hang_no},
                    hang_txt,
                    get_article_link(law_meta.get("법령ID", ""), art_no)
                ))

            # 3) 호 단위 검사
            for ho in hang.findall(".//호"):
                ho_no  = trim(ho.findtext("호번호"))
                ho_txt = trim(ho.findtext("호내용")) or trim(extract_text(ho))
                if keyword in ho_txt:
                    matches.append(build_match_item(
                        law_meta,
                        {
                            "조문번호": art_no,
                            "조문제목": art_ttl,
                            "항번호": hang_no,
                            "호번호": ho_no
                        },
                        ho_txt,
                        get_article_link(law_meta.get("법령ID", ""), art_no)
                    ))

                # 4) 목 단위 검사 (있을 수도/없을 수도)
                for mok in ho.findall(".//목"):
                    mok_no  = trim(mok.findtext("목번호"))
                    mok_txt = trim(mok.findtext("목내용")) or trim(extract_text(mok))
                    if keyword in mok_txt:
                        matches.append(build_match_item(
                            law_meta,
                            {
                                "조문번호": art_no,
                                "조문제목": art_ttl,
                                "항번호": hang_no,
                                "호번호": ho_no,
                                "목번호": mok_no
                            },
                            mok_txt,
                            get_article_link(law_meta.get("법령ID", ""), art_no)
                        ))

    # 간이 정렬: "제4조" → 4, "제3호" → 3 식으로 숫자만 뽑아 정렬 시도
    def num_from_korean(no_text, default=0):
        # "제4조" "제3호" "3항" 등에서 숫자만 추출
        if not no_text:
            return default
        nums = "".join(ch for ch in no_text if ch.isdigit())
        return int(nums) if nums.isdigit() else default

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

# ====== 법령 검색(목록) ======
@app.get("/search")
def search_law():
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "검색어를 입력하세요."}), 400

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
            # API가 차단되거나 HTML로 리다이렉트된 경우
            return hard_fail("API 응답이 XML이 아닙니다.")

        root, err = parse_xml(resp.text)
        if err or root is None:
            return hard_fail(f"XML 파싱 실패: {err}")

        laws = []
        for law in root.findall("law"):
            law_name = (law.findtext("법령명한글") or "").strip()
            law_id   = (law.findtext("법령ID") or "").strip()
            pub      = (law.findtext("공포일자") or "").strip()
            enf      = (law.findtext("시행일자") or "").strip()
            dept     = (law.findtext("소관부처명") or "").strip()

            # 최소 필드 검증(STRICT)
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

        if STRICT_MODE and not laws:
            return hard_fail()

        return jsonify({"ok": True, "guideline": GUIDELINE_TEXT[:2000], "data": laws})

    except Exception as e:
        return hard_fail(f"요청 실패: {str(e)}")

# ====== 법령 메타 + 전체 조문 원문(XML) 조회 ======
@app.get("/law")
def get_law():
    """
    사용법: /law?id=법령ID
    - 국가법령정보센터 lawService.do를 호출해 해당 법령 메타와 조문 트리를 XML 파싱
    """
    law_id = request.args.get("id", "").strip()
    if not law_id:
        return jsonify({"error": "id 파라미터(법령ID)가 필요합니다."}), 400

    params = {
        "OC": OC_KEY,
        "target": "law",
        "type": "XML",
        "ID": law_id
    }
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

# ====== 전체 조문에서 키워드 전수 스캔 ======
@app.get("/scan")
def scan_keyword():
    """
    사용법:
      /scan?id=법령ID&keyword=반기
    동작:
      - lawService.do로 전체 조문 XML 조회
      - 조/항/호/목 단위 전수 스캔
      - keyword가 포함된 모든 항목을 번호순으로 반환
    """
    law_id = request.args.get("id", "").strip()
    keyword = request.args.get("keyword", "").strip()

    if not law_id or not keyword:
        return jsonify({"error": "id(법령ID), keyword 파라미터가 필요합니다."}), 400

    params = {
        "OC": OC_KEY,
        "target": "law",
        "type": "XML",
        "ID": law_id
    }
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
            # 빈 결과라도 지침은 같이 제공
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

# ====== 실패 시 가이드라인만 돌려주는 안전 엔드포인트 ======
@app.get("/guideline")
def get_guideline():
    return jsonify({"ok": True, "guideline": GUIDELINE_TEXT})

if __name__ == "__main__":
    # 로컬 실행용
    app.run(host="0.0.0.0", port=5000)
