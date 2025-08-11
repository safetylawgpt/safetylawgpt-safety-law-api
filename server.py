import os
from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__)

# ====== 설정 ======
STRICT_MODE = True  # API 원문 없으면 무조건 실패
OC_KEY = os.getenv("NLIC_API_KEY", "").strip() or "dangerous99"  # Render 환경변수 가능
LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"

GUIDELINE_PATH = os.path.join(os.path.dirname(__file__), "08.07 구성지침.txt")
GUIDELINE_FALLBACK = (
    "정확한 조문을 찾을 수 없습니다. 국가법령정보센터에서 직접 확인하십시오."
)

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
        resp = requests.get(LAW_SEARCH_URL, params=params, headers=headers, timeout=12)
        resp.encoding = "utf-8"

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

# ====== 실패 시 가이드라인만 돌려주는 안전 엔드포인트 ======
@app.get("/guideline")
def get_guideline():
    return jsonify({"ok": True, "guideline": GUIDELINE_TEXT})

if __name__ == "__main__":
    # 로컬 실행용
    app.run(host="0.0.0.0", port=5000)
