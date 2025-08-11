import os
from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__)

# ====== 설정 ======
STRICT_MODE = True  # API 원문이 없거나 불완전하면 실패 처리
OC_KEY = os.getenv("NLIC_API_KEY", "").strip() or "dangerous99"  # Render 등 환경변수 우선, 없으면 임시값
LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"

GUIDELINE_PATH = os.path.join(os.path.dirname(__file__), "08.07 구성지침.txt")
GUIDELINE_FALLBACK = (
    "정확한 조문을 찾을 수 없습니다. 국가법령정보센터에서 직접 확인하십시오."
)

def load_guideline():
    """지침 텍스트 로드(없으면 기본 메시지)"""
    try:
        with open(GUIDELINE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return GUIDELINE_FALLBACK

GUIDELINE_TEXT = load_guideline()

def hard_fail(msg=None):
    """일관된 실패 응답 포맷(지침 포함)"""
    return jsonify({
        "ok": False,
        "message": msg or GUIDELINE_FALLBACK,
        "source": "law.go.kr",
        "data": []
    }), 200

def is_html(text: str) -> bool:
    """XML 대신 HTML 응답이 오면 진단"""
    t = (text or "").lower()
    return "<html" in t or "<!doctype html" in t

def parse_xml(text: str):
    """XML 파싱 시 안전 처리"""
    try:
        root = ET.fromstring(text)
        return root, None
    except ET.ParseError as e:
        return None, str(e)

# ====== 헬스체크 ======
@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "guideline_loaded": GUIDELINE_TEXT != GUIDELINE_FALLBACK,
        "oc_key_set": bool(os.getenv("NLIC_API_KEY")),
        "strict_mode": STRICT_MODE
    }

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

        # 네트워크/빈응답
        if not resp.ok or not resp.text:
            return hard_fail("API 요청 실패 또는 빈 응답")

        # API가 HTML로 리다이렉트되거나 차단된 경우
        if is_html(resp.text):
            return hard_fail("API 응답이 XML이 아닙니다(HTML 감지). 키/쿼터/도메인 확인 필요")

        root, err = parse_xml(resp.text)
        if err or root is None:
            return hard_fail(f"XML 파싱 실패: {err}")

        laws = []
        for law in root.findall("law"):
            law_name = (law.findtext("법령명한글") or "").strip()
            law_id = (law.findtext("법령ID") or "").strip()
            pub = (law.findtext("공포일자") or "").strip()
            enf = (law.findtext

