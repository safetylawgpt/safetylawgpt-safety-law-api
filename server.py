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
        'OC': 'dangerous99',  # 여기에 본인의 승인된 OC 값 사용
        'target': 'law',
        'query': keyword,
        'type': 'XML'
    }

    response = requests.get(api_url, params=params)
    response.encoding = 'utf-8'  # 한글 깨짐 방지

    if response.status_code == 200:
        content_type = response.headers.get('Content-Type', '')
        # HTML 응답일 경우 오류 처리
        if 'text/html' in content_type:
            return jsonify({
                'error': 'HTML 오류 응답 수신 - 도메인/IP 승인 여부를 다시 확인하세요.',
                'detail': '응답이 XML이 아닌 HTML 형식입니다. 실제 국가법령정보센터 오류 페이지일 수 있습니다.',
                'raw_response': response.text[:500]
            })

        try:
            root = ET.fromstring(response.text)
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

        except ET.ParseError as e:
            return jsonify({
                'error': 'XML 파싱 중 오류 발생',
                'detail': str(e),
                'raw_response': response.text[:500]
            })

    else:
        return jsonify({
            'error': 'API 요청 실패',
            'status': response.status_code,
            'response_text': response.text[:500]
        })

@app.route("/openapi.yaml")
def openapi_yaml():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'openapi.yaml', mimetype='text/yaml')


if __name__ == '__main__':
    app.run(port=5001)

