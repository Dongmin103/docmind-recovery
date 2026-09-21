# 코드·설정 복원 절차

이 절차는 새 디렉터리에 복원한 뒤 비교하는 방식입니다. 운영 폴더에 `git reset --hard` 또는 `git clean`을 실행하지 마세요. Linux 원본에 Windows에서 유효하지 않은 파일명이 있으므로 실제 작업 트리 복원은 Linux에서 수행합니다.

## 1. 암호화 백업 검증과 복호화

해당 태그의 릴리스에서 `.tar.enc` 첨부파일을 내려받고 `checkpoint.json`의 `asset_sha256`과 SHA-256을 대조합니다. GitHub의 자동 생성 Source code ZIP은 실제 소스 백업이 아닙니다.

Python과 `cryptography` 패키지를 준비한 뒤 실행합니다. 키 파일은 별도 보안 경로에 보관하고 출력하거나 저장소에 커밋하지 않습니다.

```bash
python restore_backup.py pre-openviking-refactor-20260921T061958Z.tar.enc \
  --key /secure/pre-openviking-refactor-20260921T061958Z.key \
  --output /secure/restore/recovery-private.tar
```

인증 태그 검증이 성공해야 출력 파일이 확정됩니다. 출력 SHA-256을 `plaintext_archive_sha256`과 대조합니다. 복호화된 묶음은 실제 환경설정을 포함하므로 비공개 경로에만 둡니다.

## 2. 독립된 경로에서 Git 이력과 작업 트리 복원

```bash
umask 077
mkdir -p /secure/restore/components
tar -xf /secure/restore/recovery-private.tar -C /secure/restore/components
git clone /secure/restore/components/history.bundle /secure/restore/docmind
git -C /secure/restore/docmind switch --detach 432dd7e6f98796b875beb41dd1abf2ced3ad8d5b
tar -xzf /secure/restore/components/working-tree.tar.gz -C /secure/restore/docmind
```

내부 `SHA256SUMS.json`의 구성 파일 해시와 `manifest.json`의 작업 파일 해시를 대조합니다. 이 백업에는 삭제된 추적 파일이 있는지도 manifest로 기록되므로, 다른 백업에 이 절차를 재사용할 때는 deleted 항목을 별도로 적용해야 합니다.

staged 상태까지 재현해야 할 경우 별도 복원 checkout의 index만 저장된 `git-index`로 교체하고 `git-status.porcelain`, `staged.patch`, `unstaged.patch`와 비교합니다. 기본 Git bundle clone의 origin은 bundle 경로를 가리키므로 향후 개발용 원격 저장소는 명시적으로 설정합니다.

## 3. 실제 실행 소스와 설정 비교

- `running-ragflow-source.tar.gz`: 실제 실행 중인 DocMind 애플리케이션 소스, 프런트엔드 빌드, 설정.
- `running-openviking-package.tar.gz`: 실제 설치된 OpenViking Python 패키지.
- `running-openviking-app.tar.gz`: 데이터·가상환경을 제외한 앱 디렉터리. 앱 소스가 가상환경에 설치되어 있어 이 파일 자체는 작습니다.
- `runtime-inspect-private.json`: 컨테이너 설정과 환경변수. 공개하거나 로그에 출력하지 마세요.
- `manifest.json`: 원본 상태, 파일 해시, 런타임 이미지 식별자와 마운트.

운영 checkout과 실행 컨테이너는 서로 다를 수 있습니다. 실행 당시 동작을 복원할 때는 위 실행 소스와 컨테이너 설정도 확인해야 합니다. 실행 아카이브는 온라인으로 읽은 소스 스냅샷이며, 프로세스 메모리나 정지된 전체 파일시스템 스냅샷은 아닙니다.

현재 서버의 이미지와 DB·색인·문서 저장소를 보존한 상태에서 코드 변경을 되돌리는 용도입니다. 전체 서버 소실 또는 데이터 마이그레이션의 복원에는 추가 데이터·이미지 백업이 필요합니다. 운영 경로 덮어쓰기, 배포, 재시작은 복원 파일을 검토한 뒤 별도로 수행합니다.
