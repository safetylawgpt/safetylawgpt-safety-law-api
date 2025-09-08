# -*- coding: utf-8 -*-
import os, csv, io, re, unicodedata, requests
from flask import Flask, request, jsonify, Response
from datetime import datetime

app = Flask(__name__)

# ====== 환경변수 ======
# 1) LAW_TSV_URL: 구글시트 "웹에 게시" TSV/CSV 링크 (권장)
# 2) LAW_TSV_PATH: 로컬 TSV 파일 경로 (테스트용; URL이 없을 때만 사용)
LAW_TSV_URL  = os.environ.get("LAW_TSV_URL", "").strip()
LAW_TSV_PATH = os.environ.get("LAW_TSV_PATH", "").strip()

# ====== 전역 인덱스 ======
INDEX = []
STARTED_AT = datetime.utcnow()

# ====== 유틸 ======
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def _phrase_regex(kw: str):
    # 정확 문구 검색 시, 공백/개행/태그로 끊겨도 매칭되도록 공백을 \s*로 완화
    p = re.escape(kw).replace(r"\ ", r"\s*")
    return re.compile(p, re.IGNORECASE)

def _load_tsv_text() -> str:
    if LAW_TSV_URL:
        r = requests.get(LAW_TSV_URL, timeout=30)
        r.raise_for_status()
        return r.text
    if LAW_TSV_PATH and os.path.exists(LAW_TSV_PATH):
        with open(LAW_TSV_PATH, "r", encoding="utf-8") as f:
            return f.read()
    raise RuntimeError("TSV 소스가 없습니다. LAW_TSV_URL 또는 LAW_TSV_PATH를 설정하세요.")

def load_index():
    """TSV를 읽어 메모리 인덱스(INDEX) 구성"""
    global INDEX
    INDEX = []

    raw = _load_tsv_text()
    # \t(탭) 구분. CSV(쉼표)면 delimiter=','로 바꾸세요.
    reader = csv.reader(io.StringIO(raw), delimiter="\t")
    header = next(reader, [])

    name_to_idx = {name: i for i, name in enumerate(header)}
    # 필수 컬럼 존재 확인 (당신의 TSV 규칙에 맞추어 이름 유지)
    required = [
        "법령ID","법령명","조문단위","조문번호","조문제목",
        "조문내용(Plain)","조문내용(HTML)","출처URL","최신개정일","시행일"
    ]
    for col in required:
        if col not in name_to_idx:
            raise ValueError(f"TSV에 '{col}' 컬럼이 없습니다.")

    for row in reader:
        def get(c):
            idx = name_to_idx.get(c, -1)
            return row[idx].strip() if 0 <= idx < len(row) else ""

        INDEX.append({
            "lawId":     get("법령ID"),
            "lawTitle":  get("법령명"),
            "unitType":  get("조문단위"),         # 조/항/호/목
            "unitNo":    get("조문번호"),         # 제29조 제1항 제3호 가목 등
            "title":     get("조문제목"),
            "textPlain": get("조문내용(Plain)"),
            "textHtml":  get("조문내용(HTML)"),
            "enactedAt": get("시행일") or None,   # 신설일 별도 칼럼이 있으면 필요시 추가
            "amendedAt": get("최신개정일") or None,
            "sourceUrl": get("출처URL"),
        })

# 서버 기동 시 1회 로드
try:
    load_index()
except Exception as e:
    print("[WARN] TSV 초기 로드 실패:", e)

# ====== CORS (간단 와일드카드) ======
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp

# ====== 라우트 ======
@app.get("/healthz")
def healthz():
    uptime = (datetime.utcnow() - STARTED_AT).total_seconds()
    return jsonify({
        "status": "ok",
        "count": len(INDEX),
        "uptime": round(uptime, 1),
        "version": "1.1.0"
    }), 200

@app.get("/reload")
def reload_index():
    load_index()
    return jsonify({"reloaded": True, "count": len(INDEX)}), 200

@app.get("/search")
def search():
    """
    정확문구/다건/원문그대로 검색
    - keyword: 필수
    - exact: 정확 문구 매칭 (0/1, 기본 0)
    - page, page_size: 페이지네이션 (기본 1, 50)
    """
    kw = request.args.get("keyword", "").strip()
    if not kw:
        return jsonify({"total": 0, "page": 1, "page_size": 0, "items": []}), 200

    exact = request.args.get("exact", "0").lower() in ("1","true","t","yes","y")
    page = max(int(request.args.get("page", 1)), 1)
    page_size = min(max(int(request.args.get("page_size", 50)), 1), 100)

    def row_match(R):
        hay = [
            _norm(R["textPlain"]),
            _norm(R["title"]),
            _norm(R["unitNo"]),
            _norm(R["lawTitle"]),
        ]
        if exact:
            rx = _phrase_regex(kw)
            return any(rx.search(h) for h in hay)
        # 기본 AND 토큰매칭
        tokens = [t for t in re.split(r"\s+", _norm(kw)) if t]
        return all(any(tok.lower() in h.lower() for h in hay) for tok in tokens)

    cands = [R for R in INDEX if row_match(R)]

    # 간단 스코어(본문>제목>번호>법령명)
    def score(R):
        s = 0
        n_kw = _norm(kw)
        if n_kw in _norm(R["textPlain"]): s += 5
        if n_kw in _norm(R["title"]):     s += 3
        if n_kw in _norm(R["unitNo"]):    s += 2
        if n_kw in _norm(R["lawTitle"]):  s += 1
        return s

    cands.sort(key=score, reverse=True)

    total = len(cands)
    start = (page - 1) * page_size
    items = cands[start:start + page_size]

    # 원문 그대로 반환
    return jsonify({
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items
    }), 200

@app.get("/openapi.yaml")
def openapi_yaml():
    """
    같은 폴더의 openapi.yaml 파일을 그대로 서빙
    (없으면 404)
    """
    path = os.path.join(os.path.dirname(__file__), "openapi.yaml")
    if not os.path.exists(path):
        return Response("openapi.yaml not found", status=404, mimetype="text/plain")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    return Response(text, mimetype="text/yaml")

# ====== 메인 ======
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
