"""Build one reader-facing performance and engineering handoff report.

No service access or indexing. Source documents remain independently maintained.
"""
import hashlib
import argparse
import json
import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle, Flowable,
)
from reportlab.platypus.tableofcontents import TableOfContents

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/reports/DOCMIND-INTEGRATED-2026-11-10'
NAVY, TEAL = colors.HexColor('#173650'), colors.HexColor('#087F8C')
LIGHT, GRAY = colors.HexColor('#EEF4F8'), colors.HexColor('#526477')
WIDTH = 499.27
BASE = 'http://128.134.187.123:9010/rlaxortj00/docmind/-/blob/main/'


def read(path):
    return (ROOT / path).read_text(encoding='utf-8-sig')


def section(text, heading):
    start = text.index(heading)
    end = text.find('\n## ', start + len(heading))
    return text[start:end if end != -1 else len(text)]


def relocate(text, source):
    """Make supporting links work in a standalone PDF and relocated Markdown."""
    def link(m):
        label, target = m.groups()
        if target.startswith(('http:', 'https:', '#')):
            return m.group(0)
        resolved = (ROOT / source).parent.joinpath(target).resolve()
        try:
            target = BASE + resolved.relative_to(ROOT).as_posix()
        except ValueError:
            return label
        return f'[{label}]({target})'
    return re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link, text)


HISTORY = r'''
## 6. 최적화 이력 — 현재 설정과 과거 조건을 분리

같은 행 안의 입력·측정 경계가 맞는 조건끼리 비교한다. 서로 다른 문서 수와 계측 방법을 섞어 누적 개선 배수를 만들지 않는다. 아래 기록은 단회 시험이며, 실패·중단 기록은 완료 성능에서 제외한다.

### 초기 98쪽 시험은 현재 속도를 뜻하지 않는다

암호문 9,855,798바이트(9.40MiB), 98쪽·794청크. 초기 VM 8 vCPU·상한 50%, 임베딩 2개분 한도·2스레드에서는 약 843.53초가 걸렸다. 당시 GPU 14.70초와 약 57배 차이는 제한된 초기 배포 구성의 전체 시간 차이다. 현재 24개 OpenVINO/GPU 비교의 1.57배와 구분한다.

초기 임베딩 763초와 이후 129초는 VM 허용량을 두 배로 바꾼 것보다 큰 차이다. 한도·스케줄링·대기와 실제 실행이 함께 작용할 수 있지만, 이전 로그만으로 각 원인의 기여도를 확정하지 못했다. 763초 전체를 CPU 계산 시간 또는 Hyper-V 한 가지 원인의 결과라고 단정하지 않는다. 중간에 1→2개분으로 한도를 바꾼 첫 실패는 일정 조건의 실측으로 집계하지 않는다.

### 파서 1개를 유지했던 근거와 한계

| 같은 6문서·임베딩 4작업자 | 파서 1개 | 파서 2개 |
|---|---|---|
| 전체 색인 시간 | 81.75초 | 83.96초 |
| 파서 합산 CPU 시간 한도 | 논리 프로세서 4개분 | 논리 프로세서 4개분 |
| 임베딩 전체 작업자 동시 대기 | 10.33초 | 9.08초 |

당시에는 파서를 늘려도 대기가 1.25초만 줄어 파서 1개를 유지했다. 단회 측정이므로 파서 2개가 항상 느리다는 결론이 아니다. 문서 12슬롯·임베딩 12작업자로 확대했을 때 파서 1개가 최적인지도 이 시험으로 검증되지 않았다. 현재는 이후 별도 24개 시험을 거친 파서 2개를 사용한다. 원기록: `parser2-sdpa/comparison.json`, 같은 폴더 `write-record.py`.

### 같은 24개에서 작업자·CPU 영역·파서를 비교

24개 중 22개 성공·2개 OCR 제외, 242쪽·1,359청크. CPU 평균/최대는 호스트 전체 표본이며 VM 최대 허용량과 다르다.

| 과거 시험 구성 | 전체 초 | 호스트 CPU 평균/최대 | 그룹웨어 HTTP 평균/p95 ms |
|---|---|---|---|
| 임베딩 4작업자·16개분 한도 | 92.13 | 16.33% / 24% | 16.22 / 29.44 |
| 12작업자·48개분 한도 | 71.22 | 28.55% / 48% | 16.68 / 30.29 |
| 24작업자·96개분 공유 | 77.05 | 28.42% / 74% | 19.80 / 65.64 |
| 22작업자·88개분, 지원 영역 분리 | 76.45 | 28.08% / 80% | 17.03 / 73.39 |
| 위 조건 + 파서 2개/합산 4개분 | 66.684 | 27.00% / 81% | 18.09 / 31.71 |
| 파서 4개/합산 4개분 | 71.86 | 34.55% / 78% | 14.78 / 19.87 |
| 파서 4개/합산 8개분 | 68.48 | 27.91% / 72% | 20.17 / 88.33 |

48개분은 논리 프로세서 96개 중 48개에 해당하는 CPU 시간 한도다. 물리 CPU 칩 48개가 아니며 실제 사용률이 50%였다는 뜻도 아니다. 12작업자 시험의 실제 호스트 평균은 28.55%, 관측 최대는 48%였다.

허용량을 96개분으로 늘려도 입력 공급·문서별 길이·대기·긴 꼬리 때문에 빨라지지 않았다. 지원 영역 분리 시험은 작업자 수도 24→22로 바뀌므로 0.60초 차이를 CPU 영역 분리만의 효과로 주장하지 않는다. 66.684초는 당시 채택한 PyTorch 구성의 기록이며 현재 엔진은 OpenVINO다.

### 파싱이 빨라져도 전체가 같은 만큼 줄지 않는 이유

파서 4개에서 CPU 한도를 4→8개분으로 바꾸면 98쪽 문서의 파싱은 15.77→11.58초로 줄었지만 전체는 71.86→68.48초였다. 전체 파싱 종료가 63.12→60.18초, 이후 남은 구간이 8.75→8.30초였다. 그 문서의 파싱 후 대기는 9.18→10.67초, 임베딩은 15.63→15.56초였다. 단계가 겹치고 다른 문서의 종료를 기다리므로 파싱 절감량이 전체 시간에 그대로 더해지지 않는다.

마지막 임베딩 구간에는 22작업자 중 평균 약 4.24개 요청만 남았다. CPU 한도를 더 늘려도 마지막 일부 긴 요청을 자동으로 병렬 분할하는 것은 아니다. 이 수치는 해당 과거 시험의 꼬리 구간이며 최신 154개 전체의 평균과 구분한다.

### 배치를 바꾼 실험을 모두 운영에 적용하지 않았다

| 같은 24개 상세 계측 | 기본 | 요청을 최대 4개씩 분할 | 긴 청크만 별도 요청 |
|---|---|---|---|
| 전체 시간 | 70.159초 | 71.51초 | 65.11초 |
| 작업자 가동률 | 39.6% | 43.9% | 35.0% |
| 호스트 CPU 평균/최대 | 28.64%/78% | 29.73%/75% | 26.20%/57% |
| HTTP 평균/p95 ms | 19.93/35.99 | 28.27/119.16 | 15.39/20.85 |

긴 청크 분리 시험에서는 첫 임베딩 시작이 18.66→13.94초로 앞당겨졌지만 임베딩 전체 구간은 51.05→50.85초로 거의 같았다. 따라서 5초 단축을 분할 효과만으로 단정하지 않았다. 준비된 입력만 재생한 별도 실험에서는 30.03→29.27초, 가동률 75.5→91.3%였으나 CPU 사용 시간은 16.7% 늘었다. 이 29초는 복호화·파싱을 포함한 전체 색인 시간이 아니다.

채택 기준은 사용률을 최대화하는 것 자체가 아니라 전체 시간·검색 근거·메모리·그룹웨어 응답이다. 현재는 짧은 입력 최대 16개, 긴 입력 개별 계산, OpenVINO FP32를 사용한다. 긴 문서를 앞에 재정렬하지 않는다.

### 과거 GPU 대량 시험은 별도 기준

2026-10-15 GPU PC 시험은 244개·약 280.8MB, 223성공·OCR 제외 18개·기존 잘못된 파일 3개, 12,460청크였다. 워커 시작부터 최종 정리 ACK까지 283.436초였다. 호스트는 Ryzen 5 7600(6코어/12논리), GTX 1080 8GiB, RAM 약 15.16GiB. 입력과 시간 시작점이 현재 AVDC 시험과 달라 직접 배수를 계산하지 않는다.

이 기록의 문서·단계별 실행 시간을 합한 531.7초는 병렬로 겹친 시간을 중복 포함한다. 전체 283.4초와 모순이 아니다. GPU 평균 사용률은 56.0%였으며, 원본 해시 244개 유지·새 오류 없음·임시 평문 없음이 확인됐다. 이후 보고서의 24개 비교가 동일 입력 CPU/GPU 판단의 기준이다.
'''


NEXT_STEPS = '''## 1. 앞으로 해야 할 일 — 검색 연동과 인수 검증

**우선순위는 KURE 리랭커 연결 → 답변 모델 연결 → 실제 사용자 인증·검색 범위 검증이다.** 색인 엔진과 리랭커 호출 코드는 이미 있지만, AVDC 단독 환경의 검색·답변 전체 연결을 완료했다고 보고하지 않는다. 아래는 후속 작업이며 이번 보고서 수정으로 실행한 내용이 아니다.

### 가장 먼저 확인할 3가지

| 우선 과제 | 이미 있는 것 | 이어서 할 일 / 완료 판단 |
|---|---|---|
| 1. KURE 리랭커 연결 | KURE 호출 코드와 기존 연구 서버의 검색 검증 기록 | AVDC의 모델 선택·키 파일·승인된 통신 경로를 확인한다. 실제 질의에서 재평가 호출·점수·문서/근거 순위까지 검증한다. 키 누락·시간 초과 등의 실패도 확인한다. |
| 2. 답변 생성 모델 연결 | 답변 모델 어댑터·설정 경로 | 모델 URL·인증을 연결하고 실제 질문으로 답변·스트리밍·인용 근거를 확인한다. 검색 성공과 답변 생성 성공을 구분한다. |
| 3. 사용자 인증·검색 범위 검증 | 사용자·tenant/project·scope 처리 코드. 현재 AVDC는 공용 작업공간 기준 | 실제 계정으로 전체·폴더·문서 범위와 접근 권한을 확인한다. 권한 밖 문서가 후보·근거·답변에 섞이지 않는지 검증한다. |

### 리랭커는 신규 개발이 아니라 AVDC 연결 완료가 남았다

현재 코드에는 `KURERerank`와 검색 API의 모델 선택·자격증명 검사가 있다. 인수 시 `DOCMIND_RERANK_ID`와 `KURE_API_KEY_FILE`을 실제 실행 설정에서 대조한다. 기존 연구 서버에서 성공한 기록을 AVDC 독립 실행의 연결 완료로 대신하지 않는다. NAT는 보류 상태이며, 승인된 경로가 정해지기 전에는 임의로 구성하지 않는다.

154개 성능 시험은 색인 생성·청크 조회·정리·부하 검증이다. 이 실행에서 KURE 재평가를 병행하지 않았다. 기존 12질문 검색 회귀와 청킹 사례집 8문항 검증은 별도 기록으로 유지한다.

### 이어서 확인할 인수·운영 항목

| 항목 | 인수 때 확인할 내용 |
|---|---|
| 새 서버 재현과 합성 문서 시험 | 이미지·모델·상용 복호화 모듈·키를 수령하고 한 문서의 복호화 → 활성 청크 → 정리 ACK를 확인한다. 실행 절차는 13장. |
| 상시 동기화·무인 실행 | 문서 추가·수정·삭제, 감시기 재시작, 로그아웃·재부팅 후 실행 계정 조건을 검증한다. |
| 실제 운영 부하 | 장시간·업무 피크·검색과 색인 동시 실행을 측정한다. 현재 단회 시작 페이지 HTTP 검사로 대체하지 않는다. |
| 운영 담당자와의 인계 | 자동 기동·모니터링·백업·복구·네트워크·키 관리 담당자를 정한다. 엔진 전달 범위와 서버 운영 범위를 구분한다. |

인수 완료는 코드 전달만으로 판단하지 않는다. 실행 설정, 실제 연결 결과, 성공·실패 확인과 남은 항목별 담당자를 함께 기록한다. 상세 검증 한계와 체크리스트는 14장에 있다.

'''


def compose_source():
    status = read('docs/reports/AVDC-STATUS-2026-11-10.md')
    hand = read('docs/ENGINE-HANDOFF-2026-11-10.md')
    parts = ['''# DocMind 통합보고서

구축 과정 · 성능과 리소스 검증 · 엔진 인수인계

기준일 2026-11-10 | AVDC CPU/OpenVINO | OCR 미사용

## 전체 내용 요약 — 구축·성능·인수인계

GPU 없는 AVDC에 문서 색인 엔진을 구축했다. Windows에서 원본을 임시 복호화하고, Ubuntu VM에서 파싱·청킹·OpenVINO 임베딩·색인을 수행한 뒤 임시 평문을 정리한다.

**우선 해야 할 일: KURE 리랭커 연결 → 답변 모델 연결 → 실제 사용자 인증·검색 범위 검증.**

[VISUAL_OVERVIEW]

핵심 결론: 154개·100.74MiB 색인은 정리 ACK까지 185.18초, 서버 CPU 평균 25%·관측 최대 83%였다. KURE 호출 코드는 있으나 AVDC의 독립 연결·검색 검증이 남았다. 답변 모델과 인증·권한 검증을 함께 완료하고 새 서버 재현·상시 동기화·장시간 부하를 이어서 확인한다.
''']
    mappings = [
        ('기존 색인을 비우고 154개를 다시 처리했습니다', '1. 최신 결과 — 실제 154개를 약 3분 5초에 처리'),
        ('같은 24개 입력에서 CPU는 GPU보다 1.57배 시간', '2. 핵심 비교 — 같은 입력에서 GPU와 CPU'),
        ('GPU 없이, Windows와 Ubuntu가 역할을 나눕니다', '3. 구축 구조와 현재 리소스 배분'),
        ('평균 CPU 25%, 관측 최대 83%', '4. 실제 CPU·메모리·그룹웨어 부하'),
        ('단계별 평균을 전체 시간에 더하지 않습니다', '5. 복호화부터 완료까지 단계별 시간'),
    ]
    for old, new in mappings:
        part = section(status, '## ' + old).replace('## '+old, '## '+new, 1)
        parts.append(relocate(part, 'docs/reports/AVDC-STATUS-2026-11-10.md'))
        if new.startswith('2.'):
            parts.append('\n시간당 청크 수 환산: GPU 124,835개 / PyTorch CPU 69,733개 / OpenVINO CPU 79,297개. 같은 1,359청크를 각 단회 전체 시간으로 나눈 값이며 한 시간 연속 실측이 아니다.\n\n[VISUAL_GPU]\n')
        if new.startswith('3.'):
            parts.append('\n[VISUAL_FLOW]\n')
        if new.startswith('4.'):
            parts.append('\n[VISUAL_CPU]\n')
    parts.append(HISTORY)
    part = section(status, '## 계산 엔진과 병렬 처리, 청킹을 함께 정리했습니다')
    parts.append(relocate(part.replace('## 계산 엔진과 병렬 처리, 청킹을 함께 정리했습니다', '## 7. OpenVINO·청킹 품질과 확인하지 못한 원인'), 'docs/reports/AVDC-STATUS-2026-11-10.md'))
    parts.append('''
### 같은 24개 비교의 단계별 상세값

| 단계 / 문서별 초 | PyTorch 평균 | OpenVINO 평균 | OpenVINO 최대 |
|---|---|---|---|
| 원본 검증 / 24건 | 0.393 | 0.320 | 0.504 |
| 복호화 차례 대기 / 24건 | 2.480 | 2.693 | 9.912 |
| 복호화 호출·결과 검증 / 24건 | 0.835 | 0.634 | 1.190 |
| 파서 대기 / 24건 | 2.234 | 1.694 | 7.043 |
| 파서 내부 실행 / 24건 | 1.777 | 1.650 | 9.397 |
| 청크 생성 / 22건 | 0.241 | 0.271 | 1.219 |
| 임베딩·대기 / 22건 | 10.111 | 8.639 | 13.023 |
| 색인 저장 / 22건 | 0.643 | 0.390 | 2.824 |
| 활성화 / 22건 | 0.413 | 0.384 | 2.236 |
| VM 임시 정리 / 24건 | 0.176 | 0.145 | 0.739 |
| Windows 임시 정리 / 24건 | 0.055 | 0.057 | 0.212 |

복호화 실행 파일만의 OpenVINO 평균은 0.241초다. 호출·검증 0.634초와 구분한다. 단계 평균은 병렬 처리로 겹치며 합산하지 않는다. 154개 시험의 파서 API 왕복과 위 파서 내부 실행도 측정 경계가 다르다.

### 검색 근거가 달랐던 한 건

OpenVINO/PyTorch 1,359벡터의 최대 절대 차이는 1.63624e-6이었다. 별도 색인에서 달랐던 한 근거는 BM25의 샤드별 통계와 동점 정렬 차이로 추적했다. 전역 통계로 진단할 때 동점과 후보 일치가 확인됐으며, 이를 운영 검색 알고리즘 변경으로 적용한 것은 아니다. 이후 동일 source의 12질문에서 문서·근거·관련성 상태가 12/12 일치했다.

청킹 v3는 Kordoc 출력 뒤에서 제목과 본문을 묶는다. 사례집은 794→440청크, 48토큰 미만 44개·표 청크 108개, 8질문 첫 근거 적중 8/8 유지가 확인됐다. 이 표본이 모든 문서의 정확도를 보장하지는 않는다.
''')
    parts.append('## 8. 엔진 전달 범위·코드·배포 위치\n')
    for heading in ['## 1. 무엇을 만들었는가', '## 2. 전달 범위와 책임 경계', '## 3. 코드와 파일 위치']:
        p = section(hand, heading)
        p = p.replace(heading, '### '+heading.split('. ', 1)[1], 1)
        p = re.sub(r'```mermaid.*?```', '처리 흐름은 3장의 구조도를 따른다.', p, flags=re.S)
        parts.append(relocate(p, 'docs/ENGINE-HANDOFF-2026-11-10.md'))
    for filename, title in [
        ('ASSETS.md', '9. 수령할 실행 자산과 전달·복원 방법'),
        ('CONFIGURATION.md', '10. 실행 설정 작성과 확인'),
        ('API.md', '11. Windows 워커와 엔진 API 연동'),
        ('RUNBOOK.md', '12. 개발자 실행 순서와 합격 확인'),
    ]:
        path = 'docs/engine-handoff/'+filename
        p = read(path).split('\n', 1)[1]
        p = re.sub(r'^## ', '### ', p, flags=re.M)
        p = re.sub(r'^\[인수인계서\].*$', '', p, flags=re.M)
        p = re.sub(r'```mermaid.*?```', '상태: DISCOVERED / RETRY_WAIT → DECRYPTING → PARSING → INDEXING → CLEANUP → COMPLETE. 실패는 FAILED / ACTION_REQUIRED / CLEANUP_FAILED로 구분한다.', p, flags=re.S)
        parts.append('## '+title+'\n'+relocate(p,path))
        if filename == 'API.md':
            embed = section(hand, '## 5. 연동 방법')
            embed = embed[embed.index('### 임베딩 연동'):]
            parts.append(relocate(embed, 'docs/ENGINE-HANDOFF-2026-11-10.md'))
    parts.append('## 13. 진단·검증 범위·남은 업무\n')
    parts.append('''### 정지·복구 원칙

워커는 실행 때 지정한 stop 파일로 정상 종료를 요청하고 진행 중 작업·정리 ACK가 끝날 때까지 기다린다. 준비된 AVDC 서비스 정지는 `sudo /opt/docmind-avdc/manage.sh stop`, 상태 확인은 `sudo /opt/docmind-avdc/manage.sh ps`다. `down -v` 또는 볼륨 삭제는 정지 방법이 아니다.

변경 전 실제 Compose·이미지 ID·runtime 소스·모델 식별자를 별도 보관한다. 복구할 때는 해당 서비스의 코드와 설정을 함께 되돌리고 health와 기존 작업·청크를 확인한다. 청킹 버전 복구만으로 이미 만든 청크가 자동으로 이전 결과가 되지는 않는다. DB·색인 전체 삭제를 일반 복구 절차로 사용하지 않는다.
''')
    for heading in ['## 6. 완료·실패 판단과 기본 진단', '## 7. 검증된 부분', '## 8. 남은 부분 — 미검증·개선 가능성과 운영 인계 구분', '## 9. 인수 시 확인 목록']:
        p = section(hand, heading).replace(heading, '### '+heading.split('. ',1)[1], 1)
        parts.append(relocate(p, 'docs/ENGINE-HANDOFF-2026-11-10.md'))
    p = section(status, '## 운영 데이터와 과거 실험 결과를 구분합니다')
    parts.append(relocate(p.replace('## 운영 데이터와 과거 실험 결과를 구분합니다', '## 14. 보관·용량·대량 처리 추정과 근거'), 'docs/reports/AVDC-STATUS-2026-11-10.md'))
    parts.append('''
### 기존 보고서의 내용은 어디에 통합했는가

| 기존 PDF | 통합 위치 |
|---|---|
| 10월 15일 GPU 성능·리소스 | 6장 GPU 과거 기준 및 병렬 시간 해석 |
| 11월 3일 98쪽 성능 | 6장 초기 제약과 실패 기록의 경계 |
| 11월 4일·5일·6일 성능 보고서 | 2·6·7장 같은 입력 비교, 확대·파서·배치·OpenVINO |
| 11월 5일·6일 도입 설명 보고서 | 3·6·7장 구축 구조, 허용량과 실제 사용량, 원인/한계 |
| 11월 6일 종합보고서 | 2·5·7·14장 GPU, 단계 시간, 이전 154개 결과와 한계 |
| 11월 10일 종합보고서 | 1~5·7·14장 최신 154개, 청킹, 부하·용량 |
| 엔진 인수인계서와 보완 5종 | 8~13장 + 부록: 자산·설정·연동·실행·합격·미완료 |

중복 설명은 합치고 현재 수치가 바뀐 부분은 최신 기록을 우선했다. 과거 PDF의 페이지 순서나 모든 중간 시도를 그대로 복제한 합본은 아니다. 원본 기록은 삭제하지 않으며 실패·준비·단계 단독 시험은 전체 색인 성능에서 구분한다.

실험 기록 정리 후 기준 루트는 `D:\\DocMind\\archive\\avdc-experiments`다. 주요 하위 기록: `openvino-full-24-r1/comparison.json`, `stage-comparison.json`, `scale48-gpu24-r2/result.json`, `openvino-154-20261106/result.json`, `cleanup-proof.json`, `host-load.csv`, `audit-summary.json`, `openvino-search-diagnosis-20261106/diagnosis.json`, `parser2-sdpa/comparison.json`. 이전 보고서의 `D:\\DocMind-AVDC` 경로는 정리 이전 위치다.

GitLab과 GitHub main에 통합본·소스·기존 상세 문서를 함께 보관한다. 실제 키, 원본 문서, 모델 가중치, 상용 복호화 모듈은 Git 외부 자산이다. GitLab 기존 main은 legacy 브랜치에 보존되어 있으며 이 통합 작업은 force push나 실행 설정 변경을 하지 않는다.
''')
    assets = json.loads(read('docs/engine-handoff/assets.json'))
    parts.append('## 부록 A. 실제 확인한 자산 식별자\n\n2026-11-10 읽기 전용 확인 기록. 이미지 ID와 전송 archive SHA256은 서로 다른 식별자다. 아래 JSON에는 실제 비밀 키 값이 없다. Windows 상용 모듈의 재해시는 인수 때 수행한다.\n\n```json\n'+json.dumps(assets,ensure_ascii=False,indent=2)+'\n```\n')
    parts.append('## 부록 B. 통합에 사용한 PDF 원본 식별자\n\n원본 폴더: `D:\\DocMind\\reports`. 이력 파일은 유지하며 대표 문서는 이 통합본이다. SHA256은 원본 파일 대조용이다.\n')
    for p in sorted(Path('D:/DocMind/reports').glob('*.pdf')):
        if '통합보고서' in p.name:
            continue
        parts.append('### '+p.name+'\n\nSHA256: `'+hashlib.sha256(p.read_bytes()).hexdigest()+'`\n')
    text = '\n\n'.join(parts)
    # Insert the new first chapter and shift existing chapters/references once.
    text = re.sub(r'^## (\d+)\.',lambda m:'## '+str(int(m[1])+1)+'.',text,flags=re.M)
    text = re.sub(r'\d+(?:[~·]\d+)*장',lambda m:re.sub(r'\d+',lambda n:str(int(n[0])+1),m[0]),text)
    text = text.replace('## 2. 최신 결과',NEXT_STEPS+'## 2. 최신 결과',1)
    OUT.with_suffix('.md').write_text(text,encoding='utf-8',newline='\n')
    return text


class Bars(Flowable):
    def __init__(self):
        super().__init__(); self.width=WIDTH; self.height=133
    def draw(self):
        c=self.canv
        for i,(label,value,col) in enumerate([('기존 GPU PC',39.191,NAVY),('PyTorch CPU',70.159,GRAY),('OpenVINO CPU',61.697,TEAL)]):
            y=100-i*38
            c.setFont('KR',9); c.setFillColor(NAVY); c.drawString(0,y+8,label)
            c.setFillColor(col); c.roundRect(119,y,300*value/75,22,3,fill=1,stroke=0)
            c.setFillColor(NAVY); c.drawString(127+300*value/75,y+6,f'{value:.3f}초')


class Flow(Flowable):
    def __init__(self):
        super().__init__(); self.width=WIDTH; self.height=160
    def draw(self):
        c=self.canv
        rows=[('Windows','원본 확인 → 공유 게이트 → 임시 복호화'),('Ubuntu VM','파싱 → 청킹 → OpenVINO 임베딩'),('저장·활성화','Elasticsearch + MySQL → 최신 청크 세트'),('완료','Windows·VM 평문 정리 → 호스트 ACK')]
        for i,(label,value) in enumerate(rows):
            y=125-i*38
            c.setFillColor(LIGHT); c.roundRect(0,y,WIDTH,30,4,stroke=0,fill=1)
            c.setFont('KRB',9);c.setFillColor(TEAL);c.drawString(12,y+10,label)
            c.setFont('KR',9);c.setFillColor(NAVY);c.drawString(105,y+10,value)


class CpuChart(Flowable):
    def __init__(self):
        super().__init__(); self.width=WIDTH; self.height=145
    def draw(self):
        c=self.canv
        samples=json.loads(read('docs/measurements/avdc-fresh154-2026-11-10.json'))['host_samples']
        max_t=max(s['elapsed_seconds'] for s in samples)
        c.setFont('KR',8)
        for v in [0,25,50,75,100]:
            y=25+v
            c.setStrokeColor(LIGHT);c.line(28,y,WIDTH,y)
            c.setFillColor(GRAY);c.drawRightString(23,y-3,str(v))
        for s in samples:
            x=28+(WIDTH-36)*s['elapsed_seconds']/max_t
            c.setStrokeColor(TEAL if s['phase']=='indexing' else GRAY)
            c.setLineWidth(2);c.line(x,25,x,25+s['cpu_percent'])
        c.setFillColor(GRAY);c.drawString(28,8,'호스트 CPU(%) · 청록: 색인 중 · 회색: 전/후')
        c.drawRightString(WIDTH,8,f'{max_t:.0f}초 관측')


class Overview(Flowable):
    """A one-page overview; each metric keeps its own experiment boundary."""
    def __init__(self):
        super().__init__(); self.width=WIDTH; self.height=500

    def draw(self):
        c=self.canv
        def txt(text,x,y,size=9,bold=False,col=NAVY):
            c.setFillColor(col);c.setFont('KRB' if bold else 'KR',size);c.drawString(x,y,text)
        def para(text,x,y,w,size=8.5,leading=13):
            st=ParagraphStyle('overview',fontName='KR',fontSize=size,leading=leading,textColor=NAVY,wordWrap='CJK')
            obj=Paragraph(text,st);_,h=obj.wrap(w,1000);obj.drawOn(c,x,y-h);return h
        def box(x,y,w,h):
            c.setFillColor(LIGHT);c.roundRect(x,y,w,h,5,stroke=0,fill=1)
        card=(WIDTH-20)/3
        for i,(label,value,detail) in enumerate([
            ('최신 실제 문서 154개','3분 5초','100.74MiB · 152성공 / OCR 제외 2개'),
            ('같은 24개 / GPU 대비','1.57배 시간','GPU 39.19초 → CPU 61.70초'),
            ('154개 / 서버 전체 CPU','평균 25%','관측 최대 83% · 약 6초 간격 표본')]):
            x=i*(card+10);box(x,419,card,78)
            txt(label,x+10,477,8,True,TEAL);txt(value,x+10,449,20,True)
            para(detail,x+10,438,card-20,7.7,11)
        txt('처음부터 완료까지',0,397,11,True)
        step=(WIDTH-30)/4
        for i,(a,b) in enumerate([
            ('1  Windows','원본 확인<br/>공유 게이트 · 임시 복호화'),
            ('2  파서 + 청커','Kordoc 추출<br/>제목·본문 청킹 v3'),
            ('3  CPU 색인','OpenVINO 의미 계산<br/>저장 · 최신 세트 활성화'),
            ('4  정리 확인','Windows·VM 평문 삭제<br/>원본 해시 · 정리 ACK')]):
            x=i*(step+10);box(x,321,step,63);txt(a,x+8,365,8.5,True,TEAL)
            para(b,x+8,354,step-16,7.8,11)
            if i<3:txt('›',x+step+2,346,12,True,TEAL)
        txt('현재 배분: 허용량과 실제 사용량은 다름',0,300,11,True)
        para('물리 서버: CPU 칩 2개 · 48코어 / 96논리 · RAM 127.6GiB<br/>VM: 96 vCPU · 상한 100% · RAM 64GiB',0,287,WIDTH,8.5,13)
        y=237;embedw=WIDTH*88/96
        c.setFillColor(TEAL);c.rect(0,y,embedw,22,fill=1,stroke=0)
        c.setFillColor(NAVY);c.rect(embedw,y,WIDTH-embedw,22,fill=1,stroke=0)
        txt('임베딩: 논리 프로세서 88개분 시간 한도',9,y+7,8,True,colors.white)
        txt('8개분',embedw+5,y+7,7.7,True,colors.white)
        para('임베딩 22작업자 × 4스레드 / 메모리 한도 52GiB<br/>나머지 영역은 파서·API·DB 공유. 파서 2개 / 합산 4개분·1GiB. 문서 12슬롯.',0,230,WIDTH,8.2,12)
        txt('최적화 흐름',0,185,10.5,True)
        para('문서 순서를 유지한 병렬 처리 → CPU 영역·작업자 조정 → OpenVINO FP32 → 청킹 v3',0,174,WIDTH,8.3,12)
        half=(WIDTH-14)/2
        box(0,22,half,125);box(half+14,22,half,125)
        txt('검증된 결과',10,129,10,True,TEAL)
        para('• 154개: 456쪽 · 활성 청크 1,596개<br/>• 원본 해시 유지 · 임시 평문 0개<br/>• 컨테이너 합계 메모리 최대 37.17GiB<br/>• 그룹웨어 HTTP 200: 31/31<br/>  평균 20.67 → 22.18ms<br/>  색인 중 p95 68.75ms / 최대 130.46ms',10,118,half-20,8.1,13)
        txt('우선 해야 할 일 → 본문 1장',half+24,129,10,True,TEAL)
        para('1. KURE 리랭커: 키·모델·통신 연결 검증<br/>   호출 코드는 있음 / AVDC 연결 완료 필요<br/>2. 답변 모델: URL·인증·답변·인용 확인<br/>3. 실제 계정: 인증·검색 범위·권한 확인<br/>후속: 새 서버 재현·상시 동기화·장시간 부하<br/>인수: 자산·설정·API·실행·합격 결과 전달',half+24,118,half-20,8.1,13)
        txt('기준일 2026-11-10. 단회 시험이며 그룹웨어 시작 페이지 검사를 실제 업무 부하 시험과 구분한다.',0,6,7.3,col=GRAY)


class Doc(BaseDocTemplate):
    def afterFlowable(self, flow):
        if isinstance(flow,Paragraph) and flow.style.name=='chapter':
            title=flow.getPlainText(); key='chapter-'+str(self.page)
            self.canv.bookmarkPage(key); self.canv.addOutlineEntry(title,key,0,False)
            if title!='목차':self.notify('TOCEntry',(0,title,self.page,key))


def build(text):
    pdfmetrics.registerFont(TTFont('KR','C:/Windows/Fonts/malgun.ttf'))
    pdfmetrics.registerFont(TTFont('KRB','C:/Windows/Fonts/malgunbd.ttf'))
    pdfmetrics.registerFontFamily('KR',normal='KR',bold='KRB',italic='KR',boldItalic='KRB')
    body=ParagraphStyle('body',fontName='KR',fontSize=9.3,leading=15.2,textColor=NAVY,spaceAfter=7,wordWrap='CJK')
    styles={
        'body':body,
        'chapter':ParagraphStyle('chapter',parent=body,fontName='KRB',fontSize=18,leading=26,spaceAfter=17,keepWithNext=True),
        'sub':ParagraphStyle('sub',parent=body,fontName='KRB',fontSize=11,leading=17,spaceBefore=12,spaceAfter=7,keepWithNext=True),
        'table':ParagraphStyle('table',parent=body,fontSize=8.1,leading=12.6,spaceAfter=0),
        'code':ParagraphStyle('code',parent=body,fontSize=7.6,leading=11.5,spaceAfter=0,leftIndent=7,rightIndent=7,backColor=LIGHT),
    }
    def inline(s):
        s=escape(s)
        s=re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)',lambda m:f'<link href="{m[2]}"><u>{m[1]}</u></link>',s)
        s=re.sub(r'\*\*(.*?)\*\*',r'<b>\1</b>',s)
        s=re.sub(r'`([^`]+)`',r'<font color="#087F8C">\1</font>',s)
        return s
    def p(s,kind='body'):return Paragraph(inline(s),styles[kind])
    def footer(c,d):
        c.saveState();c.setStrokeColor(TEAL);c.line(48,798,547,798)
        c.setFont('KR',8);c.setFillColor(GRAY)
        c.drawString(48,809,'DOCMIND / 구축·성능 검증·엔진 인수인계')
        c.drawRightString(547,809,'2026.11.10 통합본')
        c.line(48,41,547,41);c.drawString(48,27,'현재 적용과 과거 시험을 구분 · 비밀 키/원본 미포함')
        c.drawRightString(547,27,str(d.page));c.restoreState()
    doc=Doc(str(OUT.with_suffix('.pdf')),pagesize=(595.27,841.89),leftMargin=48,rightMargin=48,topMargin=60,bottomMargin=55,title='DocMind 통합보고서 — 성능 검증과 엔진 인수인계',author='DocMind')
    doc.addPageTemplates(PageTemplate('normal',[Frame(48,55,WIDTH,725,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)],onPage=footer))
    title=ParagraphStyle('cover',parent=body,fontName='KRB',fontSize=31,leading=45,spaceAfter=24)
    story=[Spacer(1,80),Paragraph('DocMind<br/>통합보고서',title),p('구축 과정 · 성능과 리소스 검증 · 엔진 인수인계','sub'),Spacer(1,25),p('AVDC CPU/OpenVINO · OCR 미사용 · 2026-11-10'),Spacer(1,25)]
    for a,b in [('최신 실자료','154개 / 100.74MiB / 정리 ACK까지 185.18초'),('같은 입력 GPU 비교','24개: GPU 39.19초 / AVDC OpenVINO 61.70초'),('최신 서버 실제 사용률','154개 시험 평균 CPU 25% / 관측 최대 83%'),('인수 개발자용','자산 수령 → 설정 → 연결 → 실행 → 합격 확인')]:
        story.append(p(a,'sub'));story.append(p(b))
    story += [Spacer(1,25),p('이전 성능 PDF와 보완한 인수인계서를 한 문서로 통합했습니다. 현재 적용 기준과 과거 시험·미검증 범위를 분리했습니다.'),PageBreak(),p('목차','chapter')]
    toc=TableOfContents();toc.levelStyles=[ParagraphStyle('toc',parent=body,fontSize=9.2,leading=18,spaceBefore=2)]
    story += [toc,PageBreak()]
    lines=text.splitlines();i=0;first=True
    while i<len(lines):
        line=lines[i];i+=1
        if not line.strip() or line.startswith('# '): continue
        if line.startswith('## '):
            if not first:story.append(PageBreak())
            first=False;story.append(p(line[3:],'chapter'));continue
        if first:continue
        if line.startswith('### '):story.append(p(line[4:],'sub'));continue
        if line=='[VISUAL_GPU]':story.append(Bars());continue
        if line=='[VISUAL_FLOW]':story.append(Flow());continue
        if line=='[VISUAL_CPU]':story.append(CpuChart());continue
        if line=='[VISUAL_OVERVIEW]':story.append(Overview());continue
        if line.startswith('```'):
            lang=line[3:];story.append(p('명령/데이터 · '+(lang or 'text'),'sub'))
            while i<len(lines) and not lines[i].startswith('```'):
                code=lines[i];i+=1
                # Visual wrap only. Copy canonical commands from linked source or unified Markdown.
                spaces=len(code)-len(code.lstrip(' '))
                rendered='&#160;'*spaces+escape(code.lstrip(' '))
                story.append(Paragraph(rendered or '&#160;',styles['code']))
            i+=1;story.append(Spacer(1,9));continue
        if line.startswith('|'):
            rows=[line]
            while i<len(lines) and lines[i].startswith('|'):rows.append(lines[i]);i+=1
            data=[]
            for r in rows:
                cells=[v.strip() for v in r.strip().strip('|').split('|')]
                if all(re.fullmatch(r':?-+:?',x or '-') for x in cells):continue
                data.append([p(c,'table') for c in cells])
            n=len(data[0]);widths=([WIDTH*.40,WIDTH*.60] if n==2 else [WIDTH/n]*n)
            if n==4:widths=[WIDTH*.34]+[WIDTH*.22]*3
            t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
            t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#DCEAF0')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,LIGHT]),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('LINEBELOW',(0,0),(-1,0),.7,TEAL)]))
            story += [t,Spacer(1,9)];continue
        paragraph=[line]
        while i<len(lines) and lines[i].strip() and not lines[i].startswith(('#','|','```','- ','[VISUAL')):
            if re.match(r'^\d+\. ',lines[i]):break
            paragraph.append(lines[i]);i+=1
        story.append(p(' '.join(paragraph)))
    doc.multiBuild(story)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--refresh-sources',action='store_true',help='Reassemble from supporting Markdown and local archival PDFs')
    args=parser.parse_args()
    source=compose_source() if args.refresh_sources or not OUT.with_suffix('.md').exists() else OUT.with_suffix('.md').read_text(encoding='utf-8')
    build(source)
    print(OUT.with_suffix('.pdf'))
