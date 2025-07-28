from flask import Flask, request, jsonify
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote

app = Flask(__name__)

@app.route('/search', methods=['GET'])
def search_law():
    keyword = request.args.get('keyword')
    if not keyword:
        return jsonify({'error': '검색어가 제공되지 않았습니다.'}), 400

    # 키워드 인코딩
    encoded_keyword = quote(keyword)

    # 국가법령정보센터 API URL 구성
    api_url = f"https://www.law.go.kr/DRF/lawSearch.do?OC=dangerous99&target=law&type=XML&query={encoded_keyword}"

    try:
        response = requests.get(api_url, headers={"User-Agent": "Mozilla/5.0"})
        response.encoding = 'utf-8'

        if "text/html" in response.headers.get("Content-Type", ""):
            return jsonify({
                "error": "HTML 오류 응답 수신 - 도메인/IP 승인 여부를 다시 확인하세요.",
                "detail": "응답이 XML이 아닌 HTML 형식입니다. 실제 국가법령정보센터 오류 페이지일 수 있습니다.",
                "raw_response": response.text[:500]  # 일부만 보여줌
            }), 502

        # XML 파싱
        root = ET.fromstring(response.text)

        items = []
        for law in root.findall("law"):
            item = {
                "법령명": law.findtext("법령명"),
                "법령ID": law.findtext("법령ID"),
                "공포일자": law.findtext("공포일자"),
                "시행일자": law.findtext("시행일자"),
                "소관부처": law.findtext("소관부처"),
                "링크": f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={law.findtext('법령ID')}"
            }
            items.append(item)

        if not items:
            return jsonify({'message': '검색 결과가 없습니다.', 'keyword': keyword}), 200

        return jsonify(items), 200

    except ET.ParseError as e:
        return jsonify({
            "error": "XML 파싱 오류 발생",
            "detail": str(e),
            "raw_response": response.text[:500]
        }), 500

    except Exception as e:
        return jsonify({'error': '알 수 없는 오류 발생', 'detail': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)
