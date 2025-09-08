# server.py — SafetyLawGPT (Sheets 우선 + 자유형식 + 면책 고지문 맨 끝)
# 요구사항:
# - 자유 형식 답변(섹션 강제 X), 끝에 면책 고지문만 고정
# - 본문 중 자연스럽게 블로그/카톡방 유도
# - 데이터 소스: Google Sheets 1차 → law.go.kr DRF 폴백
# - 시트 스키마 자동 감지(열명 유사 매핑), 여러 탭 동시 검색(법/령/규칙 우선), 조문단위별 재조립

import os, re, json, unicodedata, xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Tuple
from flask import Flask, request, jsonify, Response
import requests
from requests.adapters import HTTPAdapter, Retry

# (선택) CORS 허용
try:
    from flask_cors import CORS  # pip install flask-cors
except Exception:
    CORS = None

# (Sheets)
try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread, Credentials = None, None

app = Flask(__name__)
if CORS:
    CORS(app)

# ===== 설정 =====
ANSWER_STYLE = os.getenv("ANSWER_STYLE", "free").strip().lower()  # "free" | "structured"
STRICT_MODE = False
OC_KEY = os.getenv("NLIC_API_KEY", "").strip() or "dangerous99"
LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
LAW_SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"

# 지침: 09.08 우선, 없으면 08.17
BASE_DIR = os.path.dirname(__file__)
CANDIDATE_GUIDELINES = [
    os.path.join(BASE_DIR, "09.08 구성지침.txt"),
    os.path.join(BASE_DIR, "08.17 구성지침.txt"),
]
GUIDELINE_FALLBACK = "정확한 조문을 찾을 수 없습니다. 국가법령정보센터에서 직접 확인하십시오."

DISCLAIMER = (
    "본 응답은 [안전법 도우미 GPT]가 제공하는 참고용 법령 정보입니다.\n"
    "정확한 법률 해석은 변호사 등 전문가와 상담하시기 바랍니다.\n"
    "본 정보는 국가법령정보센터 및 고용노동부 고시 등을 기반으로 제공합니다."
)

FALLBACK_RESOURCES = [
    {"title": "세이프티 코리아 – 서식/작성 예시", "desc": "공식 별지 서식이 명시되지 않은 경우 참고 자료",
     "url": "https://safety-korea.tistory.com/", "publisher": "세이프티 코리아"},
    {"title": "카카오톡 오픈채팅 ‘안전보건 실전소통방’", "desc": "현장 사례 공유 및 서식 작성 Q&A",
     "url": "https://open.kakao.com/o/g49w3IEh", "publisher": "세이프티 코리아"},
]

# Google Sheets ENV
GS_ENABLED = os.getenv("GOOGLE_SHEETS_ENABLED", "0").strip() in ("1", "true", "True")
GS_KEY = os.getenv("GOOGLE_SHEETS_KEY", "").strip()          # spreadsheetId
GS_TABS = [t.strip() for t in os.getenv("GOOGLE_SHEETS_TAB", "").split(",") if t.strip()]
GS_CRED_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

# ===== 지침 로드 =====
def load_guideline() -> str:
    for p in CANDIDATE_GUIDELINES:
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            continue
    return GUIDELINE_FALLBACK

GUIDELINE_TEXT = load_guideline()

# ===== 공통 유틸 =====
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
    return re.sub
