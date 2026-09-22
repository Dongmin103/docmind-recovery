# Windows 격리 개발환경 백업·복구

이 절차는 `docmind-windows-dev` 개발 프로젝트의 MySQL, Elasticsearch, MinIO만 대상으로 한다. 세 운영 원본 루트, 운영 컨테이너·볼륨, uEncryptor2, 기존 암호화 복구 릴리스에는 접근하지 않는다. 복구 도구는 저장소 안에서는 Git에서 제외되는 `.local/` 경로만 허용하며, 기존 Docker 볼륨을 덮어쓰거나 삭제하지 않는다.

## 복구 범위

| 대상 | 방식 | 이유 |
|---|---|---|
| MySQL | 서비스 정지 후 `mysql_data` 콜드 스냅샷 | 테이블, 트랜잭션 로그, 서버 메타데이터를 같은 시점으로 보존한다. |
| Elasticsearch | 서비스 정지 후 `es_data` 콜드 스냅샷 | 별도 snapshot repository 설정 없이 색인과 cluster metadata를 함께 보존한다. 같은 이미지 계열로 복구해야 한다. |
| MinIO | 서비스 정지 후 `minio_data` 콜드 스냅샷 | 개발 업로드·파서 의존 객체와 메타데이터를 함께 보존한다. 합성·비민감 문서만 있어야 한다. |
| Valkey | 제외 | 캐시, lease, lock, 작업 큐는 재구축 대상이다. 오래된 큐를 복원하면 완료·만료 작업을 다시 실행할 수 있다. |

백업 중 세 영속 서비스는 일관된 시점을 만들기 위해 잠시 중지되고, 이전에 실행 중이던 서비스만 `finally`에서 다시 시작된다. 출력은 AES-256-CBC로 암호화되고 독립 HMAC-SHA256 키로 전체 header와 ciphertext를 인증한다. 64바이트 난수 키는 package와 별도로 보관한다. 콘솔에는 비밀번호, 키, SQL, 문서명, 문서 본문을 출력하지 않는다.

볼륨 archive와 materialization에는 digest로 고정한 BusyBox helper를 network가 없는 container로 사용한다. export는 서비스 중지 전에 helper를 pull하여 중지 시간을 불필요하게 늘리지 않는다.

## 1. 키 생성

PowerShell 7에서 저장소 루트 기준으로 실행한다. 키와 package는 명시적으로 경로를 지정해야 하며, 이미 있는 파일은 덮어쓰지 않는다.

```powershell
powershell -NoProfile -File tools/windows/New-WindowsDevelopmentRecoveryKey.ps1 `
  -OutputPath .local/recovery/keys/windows-dev-recovery.key
```

키는 package와 다른 암호화된 저장장치 또는 승인된 비밀 저장소에 복사한다. 키 분실 시 package를 복구할 수 없다. Git, 채팅, 로그, package 옆에 키 값을 넣지 않는다.

## 2. 사전 점검과 export

먼저 Docker를 호출하지 않는 계획 검사를 한다.

```powershell
powershell -NoProfile -File tools/windows/Export-WindowsDevelopmentRecovery.ps1 `
  -OutputPath .local/recovery/packages/dev-20260921.dmrecovery `
  -RecoveryKeyPath .local/recovery/keys/windows-dev-recovery.key `
  -DryRun
```

실제 export는 같은 개발 엔진에 연결된 Docker CLI로 실행한다. compose 검증이 고정 project, loopback-only port, 전용 volume, 원본 root mount 부재, uEncryptor/폐기 catalog runtime 부재를 먼저 확인한다.

```powershell
powershell -NoProfile -File tools/windows/Export-WindowsDevelopmentRecovery.ps1 `
  -OutputPath .local/recovery/packages/dev-20260921.dmrecovery `
  -RecoveryKeyPath .local/recovery/keys/windows-dev-recovery.key
```

이 호스트처럼 Ubuntu WSL의 Docker를 `docker.cmd`로 호출하면 bind path 변환을 명시한다. Docker Desktop의 native Windows CLI는 이 두 인수를 생략한다.

```powershell
powershell -NoProfile -File tools/windows/Export-WindowsDevelopmentRecovery.ps1 `
  -OutputPath .local/recovery/packages/dev-20260921.dmrecovery `
  -RecoveryKeyPath .local/recovery/keys/windows-dev-recovery.key `
  -DockerCommand C:\Users\uplex\bin\docker.cmd `
  -DockerHostPathStyle Wsl
```

결과는 암호화 package와 공개 sidecar manifest 두 파일이다. sidecar에는 package checksum, 크기, backup ID, 서비스 이름만 있고 자격증명·파일 목록·원문은 없다. 작업 중 평문 archive는 `.local/recovery/work`에만 생성되고 성공·실패 모두 `finally`에서 삭제를 검증한다. 삭제가 완료되지 않으면 작업 자체가 실패한다. 강제 프로세스 종료나 호스트 장애 뒤에는 다음 실행 전에 이 work 폴더의 잔여물을 민감정보로 간주해 확인 없이 열거나 백업하지 말고 삭제한다. recovery 경로에 junction/symbolic link가 있으면 도구는 실행을 거부한다.

## 3. package 검증

복구 전에 외부 checksum, HMAC, 내부 manifest와 세 payload checksum을 모두 확인한다.

```powershell
powershell -NoProfile -File tools/windows/Test-WindowsDevelopmentRecovery.ps1 `
  -PackagePath .local/recovery/packages/dev-20260921.dmrecovery `
  -RecoveryKeyPath .local/recovery/keys/windows-dev-recovery.key
```

합성 fixture만 사용하는 도구 자체 회귀 검사는 Docker나 서비스 데이터를 읽지 않는다.

```powershell
powershell -NoProfile -File tools/windows/Test-WindowsDevelopmentRecovery.ps1 -SelfTest
```

## 4. 격리 restore

restore는 기존 `docmind-windows-dev_*` 볼륨을 절대 교체하지 않는다. backup ID를 포함한 `docmind-windows-dev-recovery-*` 볼륨 세 개를 새로 만들고, network와 container에 연결하지 않으며 port도 게시하지 않는다. 같은 이름의 볼륨이나 receipt가 있으면 중단한다.

```powershell
powershell -NoProfile -File tools/windows/Restore-WindowsDevelopmentRecovery.ps1 `
  -InputPath .local/recovery/packages/dev-20260921.dmrecovery `
  -RecoveryKeyPath .local/recovery/keys/windows-dev-recovery.key `
  -ReceiptPath .local/recovery/receipts/dev-20260921.json
```

WSL Docker wrapper를 사용할 때는 restore에도 동일한 `-DockerCommand`와 `-DockerHostPathStyle Wsl`을 추가한다.

receipt와 복구 볼륨 label을 package에 대조하고, network를 끈 read-only helper로 MySQL data dictionary, Elasticsearch nodes, MinIO metadata/object marker가 실제 복구됐는지 확인한다. 파일명이나 내용은 출력하지 않는다.

```powershell
powershell -NoProfile -File tools/windows/Test-WindowsDevelopmentRecovery.ps1 `
  -PackagePath .local/recovery/packages/dev-20260921.dmrecovery `
  -RecoveryKeyPath .local/recovery/keys/windows-dev-recovery.key `
  -ReceiptPath .local/recovery/receipts/dev-20260921.json
```

이 검사는 cryptographic integrity와 볼륨 materialization을 증명한다. 실제 서비스 기동과 합성 marker 대조는 [Windows recovery boot verification](WINDOWS-RECOVERY-BOOT-VERIFICATION.md)의 별도 recovery Compose 절차로 수행한다. 이 절차는 manifest의 정확한 MySQL/Elasticsearch/MinIO image ID, 별도 내부 network, 다른 loopback port, 인증된 marker를 강제한다. 복구 볼륨을 운영 또는 원래 개발 compose에 바로 연결하지 않는다. 애플리케이션 이미지와 기준 문서 검색은 recovery package에 포함되지 않으므로 별도 E2E 검증으로 남는다.

## 안전 및 한계

- 이 백업은 합성·비민감 개발 데이터 전용이다. 운영 데이터나 운영 이미지를 이 도구로 내려받지 않는다.
- container와 volume은 compose label이 정확히 `docmind-windows-dev`인 경우에만 export한다.
- 복구 package는 암호화하지만 sidecar와 receipt는 데이터 내용 없이 식별자·checksum·볼륨명만 기록한다.
- 서비스 이미지 변경 뒤의 물리 볼륨 호환성은 보장하지 않는다. manifest의 image reference를 보존하고 동일 버전에서 먼저 시험한다.
- 콜드 스냅샷은 실행 중 쓰기를 허용하지 않는다. export 도중 중지 실패, archive 실패 또는 재시작 실패가 하나라도 있으면 성공으로 기록하지 않는다.
- 복구 볼륨 삭제는 자동화하지 않았다. 검증 뒤 삭제할 때는 receipt의 정확한 세 이름과 `com.docmind.recovery=true` label을 다시 확인한다.
- 저장/전송 암호화의 운영 승인, 키 rotation·escrow·폐기, 보존 기간, 외부 저장소 권한은 별도 운영 정책으로 결정한다.
