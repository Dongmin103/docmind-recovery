"""Build the public AVDC report from aggregate measurements (no source documents)."""
import argparse
import csv
import json
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs/measurements/avdc-fresh154-2026-11-10.json"
W, H, M = 595.276, 841.89, 42
RW = W - M * 2
NAVY, TEAL, GRAY, LIGHT = "#173650", "#087F8C", "#526477", "#EEF4F8"


def content(d):
    old, new = d['previous'], d['current']
    stages = d['stage_means_seconds']
    win = d['host_stage_current']
    return [
        ("현재 결과", "기존 색인을 비우고 154개를 다시 처리했습니다", [
            ('p', '2026-11-10 확인. 기존 AVDC 문서 등록 689건과 검색 항목 45,636개를 백업 후 삭제하고 0건에서 시작했습니다. Windows의 암호화 원본은 보존했습니다. 입력 100.74 MiB는 십진수 단위로 약 105.64 MB입니다.'),
            ('table', ['항목', '이전 11월 6일', '이번 11월 10일'], [
                ['같은 입력', '154개 / 100.74 MiB', '154개 / 100.74 MiB'],
                ['등록 → 마지막 색인 활성화', '205.08초', '184.64초'],
                ['등록 → 임시 파일 정리 ACK', '204.98초', '185.18초 (3분 5초)'],
                ['성공 / OCR 필요 제외', '152개 / 2개', '152개 / 2개'],
                ['완료 페이지 / 활성 청크', '456쪽 / 2,135개', '456쪽 / 1,596개'],
                ['서버 CPU 평균 / 관측 최대', '23.59% / 63%', '25.00% / 83%'],
            ]),
            ('h', '전체 색인 시간은 약 10% 짧아졌습니다'),
            ('p', '같은 154개 원본의 크기·수정 시각·SHA256을 확인했습니다. 파일별 성공·제외 결과도 이전과 같습니다. 새 청킹 규칙과 빈 색인 조건이 함께 적용됐으므로, 20.44초 단축을 청킹 하나의 효과로 단정하지 않습니다.'),
            ('h', '원본과 정리 상태까지 확인했습니다'),
            ('p', '현재 문서 등록은 154개, 활성 문서는 152개, 검색 청크는 1,596개입니다. 원본 해시는 모두 유지됐고 Windows·API·파서의 임시 평문 파일은 0개였습니다. OCR이 필요한 2개는 설정대로 제외됐으며 새 오류는 없었습니다.'),
            ('p', '등록부터의 시간에는 복호화·전송·대기·파싱·청킹·임베딩·색인 활성화가 포함됩니다. 설치·백업·색인 삭제·평상시 부하 측정과 사후 검증 시간은 제외합니다. 각 조건은 단회 실측입니다.'),
        ]),
        ("구축 방식과 자원", "GPU 없이, Windows와 Ubuntu가 역할을 나눕니다", [
            ('table', ['단계', '처리 위치와 역할'], [
                ['1. 원본 확인·복호화', 'Windows 워커가 등록한 원본의 안정성과 해시를 확인하고 uEncryptor2로 임시 복호화'],
                ['2. 읽기·청킹', 'Hyper-V Ubuntu VM의 Docker 파서가 본문을 추출하고 DocMind가 청크 생성'],
                ['3. 의미 계산·저장', 'OpenVINO CPU 임베딩 → Elasticsearch 저장 → 최신 청크 세트 활성화'],
                ['4. 정리·완료', 'VM과 Windows 임시 평문을 삭제하고 정리 ACK 확인'],
            ]),
            ('h', '허용량과 실제 사용량을 구분합니다'),
            ('table', ['구성', '최대 허용량 / 현재 설정'], [
                ['AVDC 물리 서버', 'CPU 칩 2개 / 물리 코어 48개 / 논리 프로세서 96개 / RAM 127.6 GiB'],
                ['DocMind VM', '96 vCPU · 상한 100% · RAM 64 GiB'],
                ['임베딩', '22작업자 × 4스레드 / CPU 시간 88개 논리 프로세서분 한도 / RAM 52 GiB 한도'],
                ['파서', '2작업자 / 합산 CPU 시간 4개분 한도 / RAM 1 GiB / OCR 꺼짐'],
                ['동시 처리', '문서 12슬롯 / 임베딩 요청 최대 24개 / 입력 문서 순서 유지'],
                ['모델·내부 배치', 'GTE multilingual base · 768차원 · OpenVINO FP32 / 128토큰 이하 최대 16개, 긴 입력 1개'],
            ]),
            ('p', '88개분은 CPU 칩 88개라는 뜻이 아닙니다. 이번 임베딩의 실제 평균 CPU 사용 시간은 약 18.28개 논리 프로세서분이었습니다. 전체 서버 평균 25%와는 측정 범위가 다릅니다.'),
            ('p', 'VM 내부에서 임베딩은 논리 번호 0-47·56-95, 파서·DB·API 등은 48-55를 사용합니다. 이 분리는 그룹웨어용 물리 코어를 독점 예약한 것이 아닙니다. 로컬 PC GPU 구성과 NAT는 변경하지 않았습니다.'),
        ]),
        ("실제 CPU·그룹웨어 부하", "평균 CPU 25%, 관측 최대 83%", [
            ('chart',),
            ('table', ['동일 실행의 관측 지표', '색인 전', '색인 중'], [
                ['호스트 표본 수 / HTTP 200', '13 / 13', '31 / 31'],
                ['서버 전체 CPU 평균 / 최대', '4.00% / 13%', '25.00% / 83%'],
                ['그룹웨어 VM CPU 평균 / 최대', '0.08% / 1%', '0% / 0%'],
                ['그룹웨어 HTTP 평균', '20.67 ms', '22.18 ms'],
                ['그룹웨어 HTTP p95 / 최대', '31.90 / 31.90 ms', '68.75 / 130.46 ms'],
                ['디스크 대기열 평균 / 최대', '0.31 / 1', '0.16 / 1'],
                ['디스크 읽기 / 쓰기 평균', '0.106 / 0.071 ms', '0.109 / 0.051 ms'],
            ]),
            ('p', 'HTTP 실패는 없었지만 평균 응답은 1.51ms 늘었고 p95·최대 지연도 증가했습니다. 그룹웨어 CPU 0%는 정수 단위 표본 값이므로 실제 사용량이 완전히 0이라는 뜻은 아닙니다.'),
            ('p', '색인 중 호스트 가용 RAM 최저 18.63 GiB. DocMind 컨테이너 합계 메모리 관측 최대 37.17 GiB, 임베딩 33.74 GiB, 파서 0.86 GiB, API 1.07 GiB였습니다. 개별 최대의 시점은 서로 다를 수 있습니다.'),
            ('p', '호스트 약 6초·컨테이너 약 1초 간격 측정입니다. 최대값은 표본에서 관측한 값입니다. HTTP는 그룹웨어 시작 페이지이며 로그인 후 결재·첨부·다중 사용자 부하를 재현한 시험은 아닙니다.'),
        ]),
        ("처음부터 완료까지", "단계별 평균을 전체 시간에 더하지 않습니다", [
            ('p', '같은 154개 입력. 아래 서버 단계는 성공한 152개 문서의 평균이며, 실패·제외 2개는 포함하지 않습니다. 문서와 단계가 병렬로 실행되므로 합계는 실제 경과 시간과 다릅니다.'),
            ('table', ['서버 단계 (초/문서)', '이전 평균', '이번 평균'], [
                [label, f"{stages['previous'][key]:.3f}", f"{stages['current'][key]:.3f}"]
                for key, label in [('parse_http','파서 API 왕복'),('normalize_artifacts','추출 결과 정규화'),('chunk','청크 생성'),('embedding','임베딩 / 대기 포함'),('index_staging','검색 색인 저장'),('activation','최신 세트 활성화'),('container_cleanup','VM 임시 파일 정리')]
            ]),
            ('h', '이번 실행의 Windows 단계'),
            ('table', ['단계', '표본', '평균 (초)', '최대 (초)'], [
                [label, str(win[key]['samples']), f"{win[key]['mean_seconds']:.3f}", f"{win[key]['max_seconds']:.3f}"]
                for key, label in [('source_verify','원본 검증'),('lock_wait','복호화 차례 대기'),('decrypt','복호화 호출·결과 확인'),('post_decrypt_verify','복호화 후 검증'),('host_cleanup','Windows 평문 삭제'),('status_ack','정리 ACK')]
            ]),
            ('p', '파서 API 왕복은 파서 내부 실행 시간과 다릅니다. 임베딩 단계에도 서비스 대기가 들어갈 수 있습니다. Windows 복호화 호출에는 실행 준비와 결과 확인이 포함되므로 실행 파일 자체의 순수 CPU 시간과 같지 않습니다.'),
            ('p', '단계별 일부 실패 기록은 종료 시각이 없어 해당 평균에서 제외하고 실제 표본 수를 표시했습니다. 전체 성공·실패 154개와 정리 상태 검증은 별도로 완료했습니다. 활성화 시각은 초 단위, 정리 ACK는 밀리초 단위입니다.'),
        ]),
        ("기존 GPU와의 비교", "같은 24개 입력에서 CPU는 GPU보다 1.57배 시간", [
            ('p', '이 페이지는 11월 6일까지의 기존 기록입니다. 24개·41.37 MiB 중 22개 성공, OCR 필요 2개 제외. 완료 242쪽·1,359청크로 같은 입력과 결과를 맞춰 비교했습니다.'),
            ('bars',),
            ('table', ['관측 항목', 'PyTorch CPU', 'OpenVINO CPU'], [
                ['전체 색인 시간', '70.159초', '61.697초'],
                ['서버 CPU 평균 / 관측 최대', '28.64% / 78%', '34.20% / 87%'],
                ['전체 컨테이너 최고 메모리', '41.92 GiB', '31.78 GiB'],
                ['그룹웨어 HTTP 평균 / p95', '19.93 / 35.99 ms', '14.63 / 25.81 ms'],
            ]),
            ('h', '무엇을 뜻하는 비교인가'),
            ('p', 'GPU PC 39.191초 대비 OpenVINO CPU는 22.506초 더 걸렸습니다. GPU와 AVDC는 장비·파서·동시성이 다른 전체 시스템 비교입니다. 순수 CPU/GPU 칩 성능 배수로 해석하면 안 됩니다.'),
            ('p', 'OpenVINO의 주 비교는 같은 상세 계측의 PyTorch 70.159초이며 12.06% 단축됐습니다. 66.684초는 앞선 비계측 기록이어서 같은 기준으로 섞지 않습니다. GPU 시험의 조정기 종료 표시 문제는 DB·ES·원본 해시·정리 ACK로 별도 검증한 이력이 있습니다.'),
            ('h', '이번 154개와 GPU 기록은 직접 나누지 않습니다'),
            ('p', '154개를 GPU로 같은 조건에 재측정한 값은 없습니다. 오늘의 184.64초와 과거 24개 GPU의 39.191초를 나눠 배수로 보고하지 않습니다. 오늘 시간당 31,118청크는 청킹 변경으로 청크 수가 줄어든 환산값이며, 이전 37,478청크/시간과 단독으로 속도를 판정하지 않습니다.'),
        ]),
        ("최적화의 의미", "계산 엔진과 병렬 처리, 청킹을 함께 정리했습니다", [
            ('table', ['변경', '확인한 결과와 한계'], [
                ['문서 병렬 처리', '등록 순서를 유지하고 빈 슬롯에 다음 문서를 넣습니다. 앞 문서의 임베딩과 다음 문서 파싱을 겹쳐 전체 처리량을 높이는 방식입니다.'],
                ['CPU 배분·작업자', '스레드 수만 늘리지 않고 임베딩 요청을 여러 작업자로 분산했습니다. 입력 공급·긴 청크·복호화와 저장 대기 때문에 최대 한도까지 항상 쓰지는 않습니다.'],
                ['OpenVINO FP32', '같은 모델·입력·자원 한도로 엔진을 바꿨습니다. 24개 전체 시간 70.159 → 61.697초, 컨테이너 최고 메모리 41.92 → 31.78 GiB. INT8 양자화 효과가 아닙니다.'],
                ['청킹 v3', 'Kordoc 추출 뒤에서 짧은 제목과 다음 본문을 묶습니다. Kordoc 자체를 수정한 청킹 변경은 아닙니다. 오늘 154개 청크 수는 2,135 → 1,596개였습니다.'],
            ]),
            ('h', 'OpenVINO가 빨라질 수 있는 이유'),
            ('p', 'OpenVINO는 학습된 모델을 실행하는 엔진입니다. CPU 플러그인은 oneDNN 등의 계산 구현과 CPU의 병렬 실행 기능을 사용합니다. 모델과 하드웨어에 맞춘 실행·메모리 관리가 추론 비용을 줄일 수 있습니다. 기존 PyTorch에도 최적화가 있으므로 특정 기능 하나만으로 이번 차이를 설명하지 않습니다.'),
            ('p', '연산별 개선량·메모리 이동량·CPU 명령별 기여도는 분리 측정하지 않았습니다. 공식 문서의 일반 원리와 이 보고서의 실측 결과는 구분해야 합니다. [S4] OpenVINO CPU Device 문서.'),
            ('h', '파서 수와 검색 정확도는 제한된 표본의 검증입니다'),
            ('p', '과거 6문서·임베딩 4작업자에서 파서 1개 81.75초, 2개 83.96초였고 양쪽 파서 합산 한도는 4개분이었습니다. 전체 임베딩 작업자 동시 대기는 10.33 → 9.08초였습니다. 단회 결과로 파서 2개가 항상 느리다거나 현재 구성이 절대 최적이라고 결론 내리지 않습니다.'),
            ('p', 'OpenVINO 전환 후 기존 12질문에서 문서·근거 순서가 일치했습니다. 청킹 v3의 별도 사례집 8질문에서도 첫 근거 적중 8/8을 확인했습니다. 오늘 154개 시험은 최종 검색 재평가를 병행하지 않았으므로 모든 문서의 검색 정확도 보장이 아닙니다.'),
        ]),
        ("현재 상태와 다음 판단", "운영 데이터와 과거 실험 결과를 구분합니다", [
            ('table', ['2026-11-10 확인', '현재 상태'], [
                ['문서·색인', '154개 등록 / 152개 활성 / 1,596청크. 과거 누적 689개·45,636항목은 백업 후 삭제됨.'],
                ['디스크', 'VM 루트 파일시스템 할당 100 GiB. 재색인 후 가용 약 49.6 GiB. 가상 디스크 120 GiB 자체를 더 늘린 것은 아님.'],
                ['구성·복구', 'OpenVINO·CPU·메모리 설정과 컨테이너 ID는 측정 전후 동일. 임시 워커 접근은 복원. 원본·설정·키·모델·백업은 Git 밖에 보존.'],
                ['시스템 구분', 'AVDC는 CPU/OpenVINO, 여기 PC는 기존 GPU 경로 유지. NAT 변경 없음. Git 반영은 운영 재배포나 새 설치 ZIP 발행이 아님.'],
            ]),
            ('h', '1TB는 같은 속도 가정으로 약 20.3일'),
            ('p', '105,638,237바이트를 정리 ACK까지 185.176초에 처리한 값을 그대로 환산하면 시간당 약 2.05GB, 1TB(10^12바이트)에 약 487시간입니다. 24시간 연속·같은 문서 구성·같은 속도를 가정한 계산이며, TB 규모 실측이나 완료 기한 보장은 아닙니다.'),
            ('h', '추가로 확인할 범위'),
            ('p', '반복 편차, 장시간 처리, 업무 피크 시간, 로그인 후 그룹웨어 업무, 검색·색인 동시 부하, 다른 문서 길이 분포와 OCR 대상은 별도 검증이 필요합니다. 100% 허용량이 100% 실제 사용량을 뜻하지 않으며 평균 CPU만으로 추가 증설 효과를 판단하지 않습니다.'),
            ('h', '근거와 업데이트 위치'),
            ('p', '[S1] docs/AVDC-INDEXING-154-2026-11-10.md 및 docs/measurements/avdc-fresh154-2026-11-10.json. 개별 문서 정보 없이 같은 실행의 집계·시계열·단계별 평균을 포함합니다.'),
            ('p', '[S2] docs/AVDC-OPENVINO-DEPLOYMENT-2026-11-06.md. [S3] docs/AVDC-CHUNKING-DEPLOYMENT-2026-11-10.md 및 docs/AVDC-DISK-RECOVERY-2026-11-10.md. 예전 문서의 “현재” 표현은 각 기록 당시 상태입니다.'),
            ('p', '[S4] https://docs.openvino.ai/nightly/openvino-workflow/running-inference/inference-devices-and-modes/cpu-device.html (2026-11-10 확인). 일반 엔진 기능 설명의 근거이며 원인별 기여도 실측은 아닙니다.'),
        ]),
    ]


def paragraph(c, y, text, size=10, bold=False, width=RW, x=M, color=NAVY):
    style = ParagraphStyle('p', fontName='KRB' if bold else 'KR', fontSize=size,
                           leading=size*1.5, textColor=colors.HexColor(color), wordWrap='CJK')
    p = Paragraph(escape(text), style)
    _, height = p.wrap(width, H)
    assert y + height < 788, (y, height, text[:60])
    p.drawOn(c, x, H-y-height)
    return y+height


def table(c, y, headers, rows):
    cols = len(headers)
    widths = ([155, RW-155] if cols == 2 else [220, (RW-220)/2, (RW-220)/2]
              if cols == 3 else [200, 65, (RW-265)/2, (RW-265)/2])
    def cell(value, bold=False):
        return Paragraph(escape(str(value)), ParagraphStyle('cell', fontName='KRB' if bold else 'KR',
                         fontSize=9, leading=13.4, wordWrap='CJK', textColor=colors.HexColor(NAVY)))
    t = Table([[cell(x, True) for x in headers]] + [[cell(x) for x in row] for row in rows], colWidths=widths)
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor(LIGHT)),('VALIGN',(0,0),(-1,-1),'TOP'),
                          ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
                          ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
                          ('LINEBELOW',(0,0),(-1,-1),.4,colors.HexColor('#D9E3EC'))]))
    _, height = t.wrap(RW, H)
    assert y+height < 782, (y, height, headers)
    t.drawOn(c, M, H-y-height)
    return y+height+13


def cpu_chart(c, y, samples):
    left, right, top, bottom = M+30, W-M, H-y, H-y-112
    xmax = max(x['elapsed_seconds'] for x in samples)
    for value in [0,25,50,75,100]:
        py = bottom + 112*value/100
        c.setStrokeColor(colors.HexColor('#D9E3EC')); c.line(left,py,right,py)
        c.setFont('KR',7); c.drawRightString(left-5,py-3,str(value))
    for a,b in zip(samples,samples[1:]):
        c.setStrokeColor(colors.HexColor(TEAL if b['phase']=='indexing' else '#AAB7C4'))
        c.setLineWidth(1.6)
        c.line(left+a['elapsed_seconds']/xmax*(right-left),bottom+a['cpu_percent']/100*112,
               left+b['elapsed_seconds']/xmax*(right-left),bottom+b['cpu_percent']/100*112)
    return paragraph(c,y+120,'서버 전체 CPU(%) · 청록색: 색인 구간 / 회색: 전후 구간 · 약 6초 간격 표본',8,color=GRAY)+13


def bars(c,y):
    for label,value,color in [('기존 GPU PC',39.191,'#5279A5'),('PyTorch CPU',70.159,'#8094A7'),('OpenVINO CPU',61.697,TEAL)]:
        paragraph(c,y,label,9.5,width=145)
        c.setFillColor(colors.HexColor(color)); c.roundRect(M+150,H-y-20,250*value/80,18,3,fill=1,stroke=0)
        paragraph(c,y,f'{value:.2f}초',10,True,width=85,x=M+420,color=color);y+=34
    return y+5


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--font',default='C:/Windows/Fonts/malgun.ttf')
    parser.add_argument('--bold-font',default='C:/Windows/Fonts/malgunbd.ttf')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/reports/AVDC-STATUS-2026-11-10.pdf')
    args=parser.parse_args();d=json.loads(DATA.read_text(encoding='utf-8'));pages=content(d)
    pdfmetrics.registerFont(TTFont('KR',args.font));pdfmetrics.registerFont(TTFont('KRB',args.bold_font))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    c=canvas.Canvas(str(args.output),pagesize=(W,H));c.setTitle('DocMind AVDC 구축·색인 성능 종합보고서 - 2026-11-10');c.setAuthor('DocMind')
    md=['# DocMind AVDC 구축·색인 성능 종합보고서','', '2026-11-10 현재 상태 · OCR 미사용 · 기존 PC GPU 경로 유지','', '[PDF 보고서](AVDC-STATUS-2026-11-10.pdf)','']
    for number,(label,title,blocks) in enumerate(pages,1):
        c.setFillColor(colors.HexColor(TEAL));c.rect(0,H-7,W,7,fill=1,stroke=0)
        paragraph(c,25,'DOCMIND / AVDC 운영·성능 보고서',9,color=GRAY)
        paragraph(c,49,f'{number:02d} / {label}',11,True,color=TEAL)
        y=paragraph(c,74,title,19,True)+17
        md.extend(['## '+title,''])
        for block in blocks:
            if block[0]=='p':y=paragraph(c,y,block[1])+12;md.extend([block[1],''])
            elif block[0]=='h':y=paragraph(c,y+3,block[1],11,True,color=TEAL)+7;md.extend(['### '+block[1],''])
            elif block[0]=='table':
                y=table(c,y,block[1],block[2]);md.extend(['| '+' | '.join(block[1])+' |','| '+' | '.join(['---']*len(block[1]))+' |'])
                md.extend('| '+' | '.join(map(str,row))+' |' for row in block[2]);md.append('')
            elif block[0]=='chart':y=cpu_chart(c,y,d['host_samples']);md.extend(['CPU 시계열은 PDF와 [집계 JSON](../measurements/avdc-fresh154-2026-11-10.json)에 수록했다.',''])
            elif block[0]=='bars':y=bars(c,y);md.extend(['GPU PC 39.191초 / PyTorch CPU 70.159초 / OpenVINO CPU 61.697초.',''])
        c.setStrokeColor(colors.HexColor('#D9E3EC'));c.line(M,38,W-M,38)
        c.setFont('KR',8);c.setFillColor(colors.HexColor(GRAY));c.drawString(M,23,'2026.11.10 · 최신 실측과 과거 비교의 입력·측정 범위 구분')
        c.drawRightString(W-M,23,f'{number} / {len(pages)}');c.showPage()
    c.save();args.output.with_suffix('.md').write_text('\n'.join(md).rstrip()+'\n',encoding='utf-8',newline='\n')
    print(f'Generated {len(pages)} pages: {args.output.name}')


if __name__=='__main__':main()
