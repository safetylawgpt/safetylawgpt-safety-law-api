from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__)

@app.route('/search', methods=['GET'])
def search_law():
    keyword = request.args.get('keyword')
    if not keyword:
        return jsonify({"error": "Missing keyword parameter"}), 400

    url = "https://www.law.go.kr/DRF/lawSearch.do"
    params = {
        "OC": "dangerous99",
        "target": "law",
        "type": "XML",
        "query": keyword
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/xml,text/xml,application/xhtml+xml",
        "Referer": "https://www.law.go.kr/",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.encoding = 'utf-8'

        if 'html' in response.text.lower():
            return jsonify({
                "error": "XML 아님 - 응답이 HTML일 수 있음",
                "raw_response": response.text[:500]
            }), 500

        root = ET.fromstring(response.text)
        results = []

        for law in root.findall('law'):  # 'law' 요소는 실제 XML 응답 구조에 따라 바꿔야 함
            result = {
                "법령명": law.findtext('법령명'),
                "법령ID": law.findtext('법령ID'),
                "공포일자": law.findtext('공포일자'),
                "시행일자": law.findtext('시행일자'),
                "소관부처": law.findtext('소관부처'),
                "링크": law.findtext('연결주소')
            }
            results.append(result)

        if not results:
            return jsonify({"message": "검색 결과가 없습니다."}), 200

        return jsonify(results)

    except ET.ParseError as e:
        return jsonify({"error": "XML 파싱 오류", "detail": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "서버 오류", "detail": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)