# OSPF는 4초, BFD는 0.5초: 장애 수렴을 40번 측정해봤다

> FRRouting과 containerlab으로 Access–Aggregation–Core IP망을 설계하고, carrier-up 블랙홀에서 OSPF와 BFD의 장애 탐지 성능을 반복 측정한 기록이다.

## 시작하며

네트워크를 공부하면서 OSPF dead timer보다 BFD가 빠르다는 설명은 자주 접했다. 하지만 다음 질문에는 직접 답해본 적이 없었다.

- 실제로 얼마나 빠른가?
- 링크가 물리적으로 Down되지 않고 패킷만 사라지는 상황에서도 차이가 나는가?
- 한 번 잘 나온 결과가 아니라 반복 측정에서도 같은 결론을 얻을 수 있는가?
- 실험 결과를 다른 사람이 원시 로그부터 다시 계산할 수 있는가?

이 질문에 답하기 위해 **TelcoNet Sentinel**이라는 개인 프로젝트를 만들었다.

단순히 라우터 몇 대를 연결하는 데서 끝내지 않고 다음 과정을 하나로 연결하는 것이 목표였다.

```text
OSPF 망 설계
→ 장애 주입
→ 경로 수렴 측정
→ 원시 로그 파싱
→ 통계 evidence 생성
→ Prometheus 수집
→ Grafana 시각화
→ 장애 영향 분석 API
```

프로젝트 저장소는 아래에서 확인할 수 있다.

👉 [GitHub - TelcoNet Sentinel](https://github.com/teriyakki-jin/telconet-sentinel)

---

## 1. 어떤 네트워크를 만들었나

containerlab 위에 다음 9개 노드를 구성했다.

- FRRouting 라우터 6대: `access1`, `access2`, `agg1`, `agg2`, `core1`, `core2`
- 가입자 단말 2대: `client-a`, `client-b`
- 서비스 호스트 1대: `service-host`

모든 라우터는 OSPF Area 0에 배치했다. 소규모 랩에서 multi-area 변수를 제거하고 경로 선택과 장애 수렴에 집중하기 위한 선택이다.

```text
================================================================================
                         OSPF Area 0 Network Architecture
================================================================================

             [ Service Network · 10.20.0.0/24 · Cost 10 · Passive ]
                                   │
                                   ▼
         ┌──────────────────┐   Cost 20   ┌──────────────────┐
         │  core1 · .0.31   ├─────────────┤  core2 · .0.32   │
         └─────┬────────┬───┘             └───┬────────┬─────┘
    Primary 10 │        ╲ Backup 100  Backup 100 ╱        │ Primary 10
               │         ╲                   ╱         │
               ▼          ╲                 ╱          ▼
         ┌──────────────────┐             ┌──────────────────┐
         │  agg1 · .0.21    │             │  agg2 · .0.22    │
         └─────┬────────┬───┘             └───┬────────┬─────┘
    Primary 10 │        ╲ Backup 100  Backup 100 ╱        │ Primary 10
               │         ╲                   ╱         │
               ▼          ╲                 ╱          ▼
         ┌──────────────────┐             ┌──────────────────┐
         │ access1 · .0.11  │             │ access2 · .0.12  │
         └──────────────────┘             └──────────────────┘

Router-ID prefix: 10.255.0.x/32 · Transit links: /31 point-to-point
```

### 주소와 OSPF 정책

설계에서 중요하게 본 것은 **경로가 왜 선택됐는지 계산할 수 있어야 한다는 점**이었다.

- Transit: `/31`, OSPF point-to-point
- Router ID: loopback `/32`
- 가입자·서비스 interface: passive
- Primary cost: `10`
- Backup cost: `100`
- Inter-core cost: `20`
- OSPF hello/dead timer: `1초 / 4초`

대역폭 기반 자동 cost 대신 명시적인 cost를 사용했다. 이 덕분에 `access1`에서 서비스망으로 가는 정상 경로는 다음처럼 계산된다.

```text
access1 → agg1 → core1 → service
10 + 10 + 10 = 30
```

`access1–agg1` 구간에 장애가 발생하면 backup uplink를 사용하는 경로가 선택된다.

```text
access1 → agg2 → core2 → core1 → service
100 + 10 + 20 + 10 = 140
```

따라서 실험에서는 서비스 경로의 OSPF metric이 `30 → 140`으로 바뀌는 것을 failover 완료 조건으로 사용했다.

---

## 2. 왜 단순 link-down 대신 블랙홀을 만들었나

인터페이스를 `down`시키면 운영체제가 carrier 변화를 즉시 알 수 있다. 이런 실험만으로는 OSPF dead timer와 BFD의 탐지 차이가 충분히 드러나지 않는다.

실제 운영에서는 링크 상태는 Up이지만 중간 장비나 전송 구간에서 패킷만 사라지는 상황도 발생할 수 있다. 이를 재현하기 위해 `agg1:eth1`의 carrier는 유지하고 ingress 패킷을 100% drop했다.

```bash
tc qdisc add dev eth1 clsact
tc filter add dev eth1 ingress protocol all pref 1 flower action drop
```

이 상태에서는 인터페이스가 물리적으로 내려가지 않는다. OSPF 또는 BFD가 control packet 손실을 감지해야만 우회 경로가 활성화된다.

비교한 profile은 다음과 같다.

| Profile | 장애 탐지 방식 |
|---|---|
| OSPF only | hello 1초, dead 4초 |
| BFD | minimum TX/RX 100ms, multiplier 3 |

BFD를 적용해도 OSPF cost나 우회 경로는 바꾸지 않았다. **경로 정책은 동일하게 유지하고 탐지 계층만 변경**해야 공정한 비교가 가능하다고 판단했다.

---

## 3. 한 번의 결과를 믿지 않기로 했다

초기 단발 실험에서는 다음 결과가 나왔다.

| Profile | 관측 경로 전환 상한 | 전환 전 손실 |
|---|---:|---:|
| OSPF only | 3,384ms | 31 packets |
| BFD | 336ms | 2 packets |

BFD가 약 90% 빠르게 보였지만 단 한 번의 측정으로 결론을 내리기에는 부족했다. 로컬 환경에서도 프로세스 scheduling, 명령 실행 시간, protocol timer 위상에 따라 결과가 달라질 수 있기 때문이다.

그래서 OSPF-only와 BFD를 각각 20회, 총 40회 측정하도록 자동화했다.

### 매 trial의 시작 조건

이전 trial의 상태가 다음 측정에 섞이지 않도록 시작 조건을 명시했다.

```text
OSPF-only
  1. BFD 설정 제거
  2. BFD session count = 0 확인
  3. 서비스 경로 metric = 30 확인
  4. 측정 시작

BFD
  1. BFD 설정 적용
  2. peer status = up 확인
  3. 서비스 경로 metric = 30 확인
  4. 측정 시작
```

각 측정에서는 100ms 간격으로 ICMP 80개를 전송했다. `ping -D`의 epoch timestamp, sequence, TTL과 FRR route metric, traceroute를 한 원시 로그에 기록했다.

---

## 4. 실험 자동화를 만들며 발견한 문제

반복 실험은 단순히 같은 명령을 20번 실행하는 작업이 아니었다. 자동화 과정에서 측정값을 왜곡하는 문제를 실제로 발견했다.

### 문제 1: OSPF-only인데 BFD가 남아 있었다

처음에는 `no ip ospf bfd` 명령 직후 바로 OSPF-only 측정을 시작했다. 일부 trial에서 OSPF dead timer를 기다리지 않고 약 0.2초 만에 경로가 바뀌었다.

원시 로그를 확인해 보니 제거 중인 BFD session이 잠시 OSPF에 남아 있었다. 명령이 성공했다고 해서 protocol state가 즉시 정리됐다고 가정하면 안 됐다.

해결 방법은 단순했다.

```text
no ip ospf bfd
→ show bfd peers brief
→ Session count: 0 확인
→ OSPF metric 30 확인
→ 측정 시작
```

### 문제 2: egress drop과 비대칭 반환 경로

초기에는 `agg1:eth1`의 egress를 차단했다. 그런데 일부 trial에서 route metric은 30에서 140으로 바뀌었지만 ping 손실이나 TTL 변화가 나타나지 않았다.

원인은 비대칭 경로였다. 요청은 primary를 지나가도 응답은 이미 다른 경로로 돌아올 수 있었다. 즉, egress 한 방향만 차단하면 ICMP 관측이 실제 경로 변경을 안정적으로 반영하지 못했다.

그래서 원격 `agg1:eth1`의 **ingress**를 차단하도록 fault model을 변경했다. 가입자 요청이 primary 구간에서 확실히 차단되고, failover 후에만 다시 전달되도록 만들었다.

### 문제 3: BusyBox ping의 `+1 errors`

일부 ping summary에는 다음처럼 ICMP error 개수가 포함됐다.

```text
80 packets transmitted, 49 received, +1 errors, 38.75% packet loss
```

기존 parser는 이 형식을 인식하지 못했다. 실제 로그를 fixture로 추가하고 `+N errors`가 선택적으로 존재하는 형식까지 처리하도록 정규식을 보완했다.

이 세 문제를 해결하면서 얻은 가장 큰 교훈은 다음이었다.

> 명령의 성공 여부보다 protocol state와 실제 data-plane 관측을 함께 확인해야 한다.

---

## 5. 40회 반복 측정 결과

최종 결과는 다음과 같다.

| Profile | p50 | p95 | max | 손실 p50 | 손실 p95 |
|---|---:|---:|---:|---:|---:|
| OSPF only | 3,794.5ms | 4,058ms | 4,158ms | 34.5 packets | 37 packets |
| BFD 100ms × 3 | 498ms | 519ms | 523ms | 3 packets | 3 packets |

- p50 탐지 상한: **86.88% 단축**
- p95 탐지 상한: **87.21% 단축**
- OSPF p95: `4.058초`
- BFD p95: `0.519초`

100ms ICMP probe로 관측했기 때문에 이 값은 정확한 protocol 내부 detection timestamp가 아니라 **경로 전환이 data-plane에서 확인된 상한값**이다.

![20회 반복 실험 Grafana 대시보드](https://raw.githubusercontent.com/teriyakki-jin/telconet-sentinel/main/docs/assets/grafana-bfd-repeated-trials.png)

Grafana에는 p50, p95, max뿐 아니라 40개 개별 관측값을 모두 표시했다. 요약 수치만 보여주는 것보다 분포와 outlier를 함께 확인하는 편이 실험 결과를 더 정직하게 전달한다고 생각했다.

---

## 6. 결과를 어떻게 재현 가능하게 만들었나

측정값을 README나 dashboard에 직접 입력하지 않았다.

```text
40개 원시 로그
  → timestamp·sequence·TTL·route metric 파싱
  → p50·nearest-rank p95·max 계산
  → configuration SHA-256 검증
  → JSON evidence 생성
  → FastAPI /metrics
  → Prometheus
  → Grafana
```

configuration fingerprint에는 다음 파일이 포함된다.

- containerlab topology
- network intent
- 모든 FRR configuration
- FRR daemon configuration

통합 테스트는 checked-in JSON을 40개 원시 로그에서 다시 계산하고 현재 configuration fingerprint와 일치하는지 확인한다.

```text
66 tests passed
CI branch coverage 85.73%
Ruff passed
strict mypy passed
GitHub Actions passed
```

원시 데이터와 집계 결과는 저장소에서 확인할 수 있다.

- [40개 원시 로그](https://github.com/teriyakki-jin/telconet-sentinel/tree/main/evidence/repeated)
- [반복 실험 JSON](https://github.com/teriyakki-jin/telconet-sentinel/blob/main/evidence/bfd-repeated-trials.json)
- [반복 실험 자동화](https://github.com/teriyakki-jin/telconet-sentinel/blob/main/scenarios/bfd_repeated_trials_lab.sh)

---

## 7. 장애 영향 분석까지 연결하기

실험 결과만 보여주는 데서 끝내지 않고 topology-aware impact analysis API를 추가했다.

API는 관측된 `link_id`를 입력으로 받아 장애 전후 최단 경로를 비교하고 다음 결과를 반환한다.

```json
{
  "failed_component": "access1--agg1",
  "affected_nodes": ["access1"],
  "affected_prefixes": ["10.10.1.0/24"],
  "service_impact": "degraded",
  "recommended_action": {
    "action": "restore_link",
    "target": "access1--agg1"
  },
  "status": "awaiting_approval"
}
```

서비스 경로가 사라지면 `OUTAGE`, cost가 증가하면 `DEGRADED`, 활성 경로는 유지되지만 예비 링크가 줄면 `REDUNDANCY_REDUCED`로 구분한다.

API는 raw shell command를 받거나 Docker socket을 mount하지 않는다. 복구 제안도 topology에 존재하는 링크를 대상으로 한 `restore_link` typed state transition으로 제한했다.

---

## 8. 이 프로젝트를 통해 보여주고 싶었던 것

이 프로젝트의 핵심은 “BFD가 OSPF보다 빠르다”는 사실 자체가 아니다.

보여주고 싶었던 것은 다음과 같은 네트워크 엔지니어링 과정이다.

1. 요구사항에 맞게 OSPF topology와 cost를 설계한다.
2. 정상 경로와 장애 우회 경로를 계산한다.
3. carrier-up 블랙홀을 재현한다.
4. 측정 오류와 protocol state 경쟁을 원시 로그로 진단한다.
5. 단발 결과를 반복 실험과 백분위수로 검증한다.
6. 결과를 evidence, metrics, dashboard, test로 연결한다.
7. 실험의 범위와 한계를 명확하게 밝힌다.

즉, 라우팅 설정뿐 아니라 **장애를 실험하고 수치로 검증하며 운영 가능한 근거로 만드는 역량**을 보여주는 것이 목표였다.

---

## 9. 한계와 다음 단계

이 결과는 격리된 로컬 containerlab 관측값이며 상용망 성능을 대표하지 않는다.

- Single Area 0만 구성했다.
- 서비스망이 core1에만 연결돼 단일 장애점이 존재한다.
- 실장비와 vendor interoperability는 검증하지 않았다.
- OSPF authentication과 장기 부하 실험은 포함하지 않았다.
- 현재 탐지 시간은 100ms ICMP probe로 확인한 data-plane 상한값이다.

다음 단계는 아래 control-plane 이벤트를 하나의 타임라인으로 수집하는 것이다.

```text
blackhole 주입
→ BFD Up → Down
→ OSPF Neighbor Full → Down
→ RIB metric 30 → 140
→ 첫 ICMP 복구
```

FRR syslog와 neighbor metric exporter를 연결하면 “경로가 언제 복구됐는가”뿐 아니라 “protocol 내부에서 어떤 상태 변화가 어떤 순서로 일어났는가”까지 설명할 수 있을 것이다.

---

## 마치며

처음에는 OSPF와 BFD의 속도를 비교하는 간단한 실험으로 시작했다. 하지만 반복 측정을 자동화하면서 남아 있는 BFD session, 비대칭 경로, ping 출력 형식처럼 한 번의 수동 실험에서는 놓치기 쉬운 문제를 만났다.

오히려 이 문제를 해결하는 과정이 최종 수치보다 더 의미 있었다. 네트워크 장애 실험에서는 설정값만 보는 것이 아니라 control-plane state, data-plane probe, 원시 로그를 함께 확인해야 한다는 점을 배웠다.

프로젝트의 전체 코드와 재현 방법은 GitHub에 공개했다.

👉 [TelcoNet Sentinel GitHub Repository](https://github.com/teriyakki-jin/telconet-sentinel)

---

Velog 태그 추천: `네트워크`, `OSPF`, `BFD`, `FRRouting`, `containerlab`, `Prometheus`, `Grafana`, `포트폴리오`
