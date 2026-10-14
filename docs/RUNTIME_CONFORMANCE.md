# Intent–Runtime 정합성 검사

## 목적

장애 실험의 결과가 유효하려면 장애를 주입하기 전에 FRR이 의도한 정상 상태에 있어야 한다. 설정 파일이 올바르다는 사실만으로 실제 neighbor, BFD session, RIB/FIB 상태까지 보장할 수는 없다.

`runtime_conformance.py`는 `access1`의 pre-fault snapshot을 versioned intent 및 실험 profile과 비교한다. 검사를 모두 통과한 경우에만 live convergence collector를 시작한다. 하나라도 실패하면 JSON evidence를 남기고 E2E 스크립트를 종료하므로 잘못된 초기 상태에서 얻은 수렴시간을 정상 결과로 저장하지 않는다.

## 검증 흐름

```text
lab/intent.yml ───────────────┐
                             ├─ expected path cost 계산
runtime-expectations.yml ─────┤
                             ├─ 13개 정합성 검사 ── PASS ── 장애 주입
FRR runtime snapshot ─────────┘                  └─ FAIL ── 실험 중단
```

## 검사 항목

| 영역 | ID | 기대 상태 |
|---|---|---|
| OSPF | `ospf.neighbor.10.255.0.21` | agg1 neighbor가 `eth1`에서 Full |
| OSPF | `ospf.neighbor.10.255.0.22` | agg2 neighbor가 `eth2`에서 Full |
| OSPF | `ospf.adjacencies` | router ID·interface·state entry가 추가·누락 없이 정확히 일치 |
| Route | `route.installed` | service route가 RIB/FIB에 설치됨 |
| Route | `route.selected_entry_count` | selected·installed route entry가 정확히 하나 |
| Route | `route.protocol` | 선택된 route가 OSPF에서 학습됨 |
| Route | `route.metric` | intent에서 계산한 최단비용 `30` |
| Route | `route.next_hops` | active FIB next-hop 집합이 `10.0.1.1` via `eth1` 하나와 정확히 일치 |
| BFD | `bfd.status` | peer `10.0.1.1`이 Up |
| BFD | `bfd.receive_interval_ms` | 100ms |
| BFD | `bfd.transmit_interval_ms` | 100ms |
| BFD | `bfd.detect_multiplier` | 3 |
| BFD | `bfd.detection_timeout_ms` | 협상된 탐지 timeout 300ms |

route metric은 expectation 파일에 중복 기록하지 않는다. `lab/intent.yml`에서 `access1`과 `service-host` 사이 최단비용을 다시 계산해 실제 FRR metric과 비교한다.

## 입력과 출력

### Runtime snapshot

E2E 스크립트가 다음 FRR JSON 명령의 결과를 하나의 snapshot으로 묶는다.

```text
show ip ospf neighbor json
show ip route 10.20.0.0/24 json
show bfd peer 10.0.1.1 json
```

### Conformance evidence

`runtime-conformance.json`에는 다음 정보가 포함된다.

- snapshot 수집 시각과 대상 router
- 검사별 expected, actual, passed
- 전체 PASS/FAIL 요약
- 현재 lab configuration fingerprint
- intent, expectation, snapshot SHA-256

각 FRR JSON은 strict snapshot composer에서 최대 1MiB로 제한하고 중복 JSON key 및 `NaN`/`Infinity`를 거부한다. 완성된 snapshot은 최대 5MiB로 다시 제한한다. evidence는 임시 파일을 같은 디렉터리에 기록한 뒤 atomic replace하며 입력·provenance 파일이나 symbolic link를 덮어쓰지 않는다.

## 실행

live E2E에서는 자동으로 실행된다.

```bash
bash scenarios/live_convergence_e2e.sh
```

저장된 snapshot을 별도로 검사할 수도 있다.

```bash
telconet-runtime-audit \
  --intent lab/intent.yml \
  --expectation lab/runtime-expectations.yml \
  --snapshot artifacts/live-convergence/runtime-snapshot.json \
  --project-root . \
  --output artifacts/live-convergence/runtime-conformance.json
```

종료 코드는 모든 검사를 통과하면 `0`, 정합성 drift가 있으면 `1`이다. validation error는 잘못된 입력으로 취급해 evidence를 만들지 않는다.

## 범위

현재 preflight는 실제 장애 실험의 source router인 `access1`과 서비스 경로를 검사한다. 전체 6대 라우터의 LSDB 일관성, passive interface, 모든 transit cost를 독립적으로 감사하는 기능은 다음 확장 범위다.
