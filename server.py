from flask import Flask, request, jsonify, send_from_directory
import os
import requests
import xml.etree.ElementTree as ET

app = Flask(__name__)

@app.route('/search', methods=['GET'])
def search_law():
    keyword = request.args.get('keyword')
    api_url = 'https://www.law.go.kr/DRF/lawSearch.do'
    params = {
        'OC': 'dangerous99',
        'target': 'law',
        'query': keyword,
        'type': 'XML'
    }

    response = requests.get(api_url, params=params)

    if response.status_code == 200:
        try:
            # 👉 여기 추가
            print("=== 응답 XML 원본 ===")
            print(response.text)

            root = ET.fromstring(response.content)
            laws = []
            for law in root.findall('law'):
                law_info = {
                    '법령명': law.findtext('법령명한글') or '',
                    '법령ID': law.findtext('법령ID') or '',
                    '공포일자': law.findtext('공포일자') or '',
                    '시행일자': law.findtext('시행일자') or '',
                    '소관부처': law.findtext('소관부처명') or '',
                    '링크': 'https://www.law.go.kr' + (law.findtext('법령상세링크') or ''),
                }
                laws.append(law_info)
            return jsonify(laws)
        except Exception as e:
            return jsonify({'error': 'XML 파싱 중 오류 발생', 'detail': str(e)})
    else:
        return jsonify({'error': 'API 요청 실패', 'status': response.status_code})

@app.route("/openapi.yaml")
def openapi_yaml():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'openapi.yaml', mimetype='text/yaml')

if __name__ == '__main__':
    app.run(port=5001)

