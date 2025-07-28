from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__)

@app.route("/search")
def search_law():
    keyword = request.args.get("keyword")
    if not keyword:
        return jsonify({"error": "검색어를 입력하세요."}), 400

    # 국가법령정보센터 API 주소
    url = "https://www.law.go.kr/DRF/lawSearch.do"
    params = {
        "OC": "dangerous99",  # 승인받은 OC 키
        "target": "law",
        "type": "XML",
        "query": keyword
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/xml",
        "Referer": "https://www.law.go.kr",
        "Content-Type": "application/xml; charset=UTF-8"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.encoding = "utf-8"

        # HTML 응답이면 실패로 처리
        if "<html" in response.text.lower():
            return jsonify({
                "error": "XML 아님 - 응답이 HTML일 수 있음",
                "raw_response": response.text[:500]
            }), 500

        # XML 파싱
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as e:
            return jsonify({
                "error": "XML 파싱 실패",
                "detail": str(e),
                "raw_response": response.text[:500]
            }), 500

        laws = []
        for law in root.findall("law"):
            laws.append({
                "법령명": law.findtext("법령명한글"),
                "법령ID": law.findtext("법령ID"),
                "공포일자": law.findtext("공포일자"),
                "시행일자": law.findtext("시행일자"),
                "소관부처": law.findtext("소관부처명"),
                "링크": f"https://www.law.go.kr/법령/{law.findtext('법령ID')}"
            })

        return jsonify(laws)

    except Exception as e:
        return jsonify({"error": "요청 실패", "detail": str(e)}), 500

if __name__ == "__main__":
    app.run()
