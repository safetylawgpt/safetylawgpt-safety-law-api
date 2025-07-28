from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__)

@app.route('/search', methods=['GET'])
def search_law():
    keyword = request.args.get('keyword')
    if not keyword:
        return jsonify({"error": "Missing 'keyword' parameter"}), 400

    base_url = 'https://www.law.go.kr/DRF/lawSearch.do'
    api_key = 'dangerous99'  # ← 발급받은 사용자 키
    params = {
        'OC': api_key,
        'target': 'law',
        'type': 'XML',
        'query': keyword
    }

    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/xml'
    }

    try:
        response = requests.get(base_url, params=params, headers=headers)
        if response.status_code != 200:
            return jsonify({
                "error": f"HTTP error {response.status_code}",
                "detail": response.text
            }), response.status_code

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            return jsonify({
                "error": "HTML 오류 응답 수신 - 도메인/IP 승인 여부를 다시 확인하세요.",
                "detail": "응답이 XML이 아닌 HTML 형식입니다. 실제 국가법령정보센터 오류 페이지일 수 있습니다.",
                "raw_response": response.text[:500]  # 응답의 앞부분 일부만 반환
            }), 500

        results = []
        for law in root.findall('.//law'):
            law_info = {
                "법령명": law.findtext('법령명'),
                "법령ID": law.findtext('법령ID'),
                "공포일자": law.findtext('공포일자'),
                "시행일자": law.findtext('시행일자'),
                "소관부처": law.findtext('소관부처'),
                "링크": f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={law.findtext('법령ID')}"
            }
            results.append(law_info)

        return jsonify(results), 200

    except Exception as e:
        return jsonify({"error": "예상치 못한 서버 오류", "detail": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)

