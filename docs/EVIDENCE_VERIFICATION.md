# Evidence verification

이 프로젝트의 수치는 README에 직접 입력한 독립된 숫자가 아니다. 원시 측정, 파생 JSON, 랩 설정과 생성 보고서를 하나의 검증 체계로 연결한다.

## 로컬 검증

```bash
uv sync --frozen --extra dev
uv run telconet-evidence verify --project-root .
```

검증기는 다음 64개 artifact를 확인한다.

| 역할 | 개수 | 검증 내용 |
|---|---:|---|
| 랩 설정 | 17 | baseline·candidate containerlab과 intent, design catalog, failure domain, runtime expectation, 실제로 mount하는 FRR 설정과 configuration fingerprint |
| 원시 측정 | 42 | OSPF/BFD 단회 로그 2개와 프로필별 반복 로그 20개씩 |
| 파생 evidence | 4 | 단회 비교, 반복 분포, SRLG 감사 결과, attested runtime N-1 run의 게시용 provenance index |
| 생성 보고서 | 1 | 원시 로그와 선언형 입력에서 다시 만든 보고서와 LF-normalized content 비교 |

검증이 통과하면 artifact별 SHA-256과 크기를 정렬한 canonical manifest의 `bundle_sha256`을 출력한다. 원시 로그, 설정, 파생 결과 또는 보고서 중 하나라도 바뀌면 검증은 실패한다.

manifest는 다음 명령으로만 다시 만든다.

```bash
uv run telconet-evidence generate --project-root .
```

manifest 갱신은 측정값의 정당성을 증명하지 않는다. 변경된 원시 로그와 설정을 코드 리뷰한 뒤 파생 evidence와 보고서를 재생성하고, CI 검증이 통과한 경우에만 함께 반영해야 한다.

`evidence/runtime-n1-dashboard.json`은 대시보드가 표시할 검증 결과와 source commit·workflow run·archive SHA-256을 묶은 작은 게시용 요약이다. 원본 route·neighbor·ping 로그를 대체하지 않는다. API는 시작 시 이 파일의 topology fingerprint와 링크·노드 시나리오 전체가 현재 candidate intent에서 생성한 matrix와 정확히 같은지 확인한다. candidate containerlab과 실제 mount되는 FRR·daemon 파일의 통합 fingerprint, 시나리오당 8개 검증, scenario/check 집계의 일관성도 함께 검사한다. 요약이나 fingerprint 입력이 없거나 바뀌면 API는 시작하지 않는다.

## CI provenance attestation

`containerlab E2E` workflow는 digest로 고정한 FRR·Alpine 이미지를 먼저 적재하고 live convergence, dual-homing, runtime N-1 matrix 결과를 결정론적인 `telconet-live-evidence.tar.gz`로 묶는다. runtime matrix에는 intent에서 만든 `plan.json`, 15개 장애의 route·neighbor·ping 원본, `observation.json`, 120개 expected/actual 비교가 든 `evidence.json`이 포함된다. main push와 main 기준 정기·수동 실행에서는 별도의 최소 권한 job이 GitHub Actions 단기 OIDC identity로 이 archive에 Sigstore provenance attestation을 발급한다.

다운로드한 archive는 GitHub CLI로 검증할 수 있다.

```bash
gh attestation verify telconet-live-evidence.tar.gz \
  --repo teriyakki-jin/telconet-sentinel \
  --signer-workflow teriyakki-jin/telconet-sentinel/.github/workflows/lab-e2e.yml \
  --source-ref refs/heads/main
```

attestation은 archive가 어떤 repository, commit, event와 workflow에서 생성됐는지를 연결한다. 네트워크 실험 결과 자체가 실서비스 성능이나 안전성을 보장한다는 뜻은 아니다. 결과의 의미는 원시 로그 재계산과 실험 조건 검토를 통해 별도로 판단해야 한다.

## 신뢰 경계

- Git commit과 manifest는 versioned evidence의 변경 이력을 제공한다.
- 독립 검증기는 저장소 내부 artifact 간 정합성과 재계산 가능성을 확인한다.
- provenance attestation은 CI가 생성한 live archive를 workflow와 commit에 암호학적으로 연결한다.
- runtime N-1 evidence의 plan·observation hash는 비교 입력을 묶고, attested commit은 실행에 사용한 versioned intent·containerlab·FRR 설정을 식별한다.
- 이 저장소는 로컬 containerlab 시뮬레이션이며 실제 통신사 망의 SLA를 검증하지 않는다.
