# TelcoNet Sentinel

통신사형 Access–Aggregation–Core IP 전송망을 로컬에서 모사하고, 링크 장애의 원인과 가입자 영향 범위를 설명한 뒤 승인 가능한 복구 계획을 생성하는 네트워크 운영 자동화 프로젝트입니다.

> 이 저장소는 학습 및 포트폴리오 목적의 시뮬레이션입니다. 실제 통신사 망 구성·장비·주소·운영 데이터는 포함하지 않습니다.

## 왜 만들었나

유선/IP Infra 엔지니어는 망을 설계·구축하는 데서 끝나지 않고 장비와 선로의 장애를 모니터링하고, 원인과 서비스 영향을 판단하며, 재발 방지 가능한 운용 절차를 만들어야 합니다. 이 프로젝트는 단순 CPU 임계치 대시보드가 아니라 다음 역량을 한 흐름으로 검증합니다.

- OSPF 기반 이중화 IP망 설계
- 링크 장애와 우회 경로 분석
- OSPF cost 기반 topology-aware impact analysis
- 영향받는 Access 노드와 가입자 prefix 계산
- 임의 명령 실행을 차단한 승인형 복구 절차
- 테스트 및 JSON evidence 기반 재현성

## 동작 흐름

```text
FRR 라우터 6대 + 가입자 단말 2대 + Core 뒤 서비스 호스트
              ↓ link event
FastAPI Incident Service
              ↓
Topology-aware impact analysis
              ↓
원인 링크 · 영향 노드 · 가입자 Prefix · 서비스 영향
              ↓
승인 대기 중인 restore_link 계획
```

## 현재 검증 상태

- 단위·API·랩 계약·evidence 테스트 39개
- branch coverage 89.29%
- 6-router OSPF 구성과 containerlab 선언을 계약 테스트로 검증
- `access1--agg1` 장애 시 Core 뒤 서비스까지 대체 경로와 `DEGRADED` 판정 검증
- containerlab 0.79.0 + FRR 10.7.0에서 실제 9-node 랩 기동 검증
- fresh-deploy 100ms 간격 160패킷 실험: 손실 0.625%, 경로 전환 150ms 이내, 기본 경로 복귀 1.226초 이내

분석 예시는 `scenario_injected_event`, 실험 결과는 `containerlab_observation`으로 분리합니다. [원시 실험 로그](evidence/measured-link-failure.log)를 파서가 [측정 JSON](evidence/measured-link-failure.json)으로 재계산하며, 테스트에서 두 결과가 일치하는지 검증합니다.

## 빠른 시작

### 분석 API와 테스트

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest -q --cov=telconet_sentinel --cov-branch --cov-fail-under=80
uvicorn telconet_sentinel.main:app --reload
```

API 문서는 `http://127.0.0.1:8000/docs`에서 확인할 수 있습니다.

```bash
curl -X POST http://127.0.0.1:8000/api/events \
  -H 'content-type: application/json' \
  -d '{"event_type":"link_down","link_id":"access1--agg1"}'
```

주요 엔드포인트:

| Method | Path | 목적 |
|---|---|---|
| GET | `/health` | API 상태 확인 |
| GET | `/api/topology` | 노드·링크·가입자 prefix 확인 |
| POST | `/api/events` | 장애 이벤트 분석 및 중복 제거 |
| GET | `/api/incidents/{id}` | 장애 분석 결과 확인 |
| POST | `/api/incidents/{id}/approve` | 고정 복구 계획 승인 |

### 시나리오 evidence 생성

```powershell
python -m telconet_sentinel.demo `
  --link access1--agg1 `
  --output evidence/simulated-link-failure.json
```

### 실제 라우팅 랩

containerlab은 Linux 네트워크 기능을 사용하므로 Windows에서는 WSL2 안에서 실행합니다.

```bash
python -m pip install -e .
bash scenarios/link_failure_lab.sh
```

스크립트는 기존 동일 이름 lab을 제거해 현재 설정으로 다시 배포하고, 100ms 간격 timestamped ping 중 링크를 단절·복구한 뒤 route와 traceroute를 함께 기록합니다. 이벤트 epoch와 `iputils ping -D` 응답 timestamp를 비교해 경로 전환 상한을 계산하며 topology·intent·FRR configuration SHA-256도 남기므로 수치를 수동으로 입력하지 않습니다.

상세 검증 순서는 [링크 장애 Runbook](docs/RUNBOOK_LINK_FAILURE.md)에 있습니다.

## 설계 포인트

### 설명 가능한 영향 분석

장애 이벤트는 관측된 실패 링크를 입력으로 받으며, 링크 자체의 원인을 추론한다고 주장하지 않습니다. 대신 Dijkstra 기반으로 OSPF cost를 반영한 장애 전후 최단 서비스 경로를 비교합니다. 경로가 사라지면 `OUTAGE`, cost가 증가하면 `DEGRADED`, 활성 최단 경로는 유지되지만 예비 링크만 줄어들면 `REDUNDANCY_REDUCED`로 구분합니다.

### 안전한 복구 경계

API는 raw shell command를 받지 않습니다. Phase 1에서 가능한 동작은 기존 topology link에 대한 `restore_link`뿐이며, 승인 API는 인증된 운영자 승인 기능이 아닌 로컬 typed state transition입니다. 실제 실행기와 연결하지 않았고, 권한이 필요한 containerlab 명령은 별도 로컬 Runbook에서 실행합니다.

중복 판단은 클라이언트 timestamp가 아니라 서버 monotonic clock의 60초 창을 사용하고, 인메모리 incident는 최대 1,000개로 제한합니다. 외부 `observed_at`은 timezone 정보가 있을 때만 evidence로 보존됩니다.

### 단일 기준과 계약 테스트

- `lab/intent.yml`: 영향 분석용 노드·역할·링크 cost·가입자 prefix의 기준
- `lab/telconet.clab.yml`: 실제 에뮬레이션 링크와 컨테이너의 기준
- 계약 테스트: 두 선언의 링크 일치, 이미지 버전 태그, 최소 컨테이너 권한, OSPF 설정과 Router ID 검증

FRR 이미지는 현재 `10.7.0` 태그로 고정했습니다. 실제 랩 pull 검증 후에는 registry digest까지 고정할 예정입니다.

FRR 10.7 컨테이너의 `zebra`와 `ospfd`가 요구하는 capability 때문에 라우터에 `NET_ADMIN`, `NET_RAW`, `SYS_ADMIN`을 부여합니다. 컨테이너는 privileged 모드가 아니며 FRR 설정 bind는 읽기 전용입니다. 이 권한 모델은 격리된 로컬 실험용이며 운영 환경 배포를 전제로 하지 않습니다.

아키텍처와 신뢰 경계는 [ARCHITECTURE.md](docs/ARCHITECTURE.md)에 정리했습니다.

## 다음 단계

1. FRR syslog에서 링크 이벤트를 수집해 scenario injection과 실제 탐지를 분리
2. BFD 도입 전후 수렴시간과 패킷 손실 비교
3. BGP 및 MPLS L3VPN 추가
4. Prometheus/Grafana 기반 인터페이스·라우팅 상태 시각화
5. 반복 장애 실험으로 MTTR과 오탐률 측정

## 기술 스택

- Network lab: containerlab, FRRouting, OSPF
- Backend: Python, FastAPI, Pydantic
- Validation: pytest, coverage, Ruff, mypy, GitHub Actions
- Packaging: Docker, Docker Compose
