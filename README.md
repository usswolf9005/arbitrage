# Distressed Arbitrage Monitor

해킹·디페그·브릿지 이슈·대량 덤핑처럼 **DEX 가격이 먼저 무너지는 이벤트**를 감지하고, 매수→매도 실행 플로우를 모니터링하는 event-driven arbitrage 시스템입니다.

> ⚠️ **이 저장소는 모니터링 + no-funds 시뮬레이션 범위입니다.** 실제 매수/매도(지갑 서명, DEX swap 제출, 브릿지 전송, CEX 주문/출금)는 **비활성화**되어 있습니다. 모든 실행 어댑터는 deterministic dry-run이며 실제 자금을 움직이지 않습니다.

## 무엇을 하나

```
가격/페어 수집 (read-only)
  → 토큰 정규화 / USD·KRW 환산
  → DEX 급락 및 venue별 가격 괴리 감지
  → Safety Gate (출구 가능성 최소 검증)
  → 실행 플로우: 감지 → 프리체크 → DEX 매수 → 매도 루트 분기
  → 실시간 모니터 UI
```

매도 루트(평가 단계까지 구현):
- 같은 체인 DEX 매도
- 브릿지 후 다른 체인 DEX 매도
- 브릿지 후 CEX 입금 → CEX 매도
- 직접 CEX 입금 → CEX 매도

## 구조

```
arbitrage/
  api_server.py          # REST + SSE API (read-only snapshot/stream)
  store.py               # SQLite 도메인 스토어 (arb_* 스키마)
  engine.py              # 실행 게이트 / 스냅샷
  detector.py            # 이상/괴리 감지 (drawdown, spread, depeg, divergence 등)
  normalizer.py          # 토큰/가격 정규화
  route_evaluator.py     # 매도 루트 worst-case edge 평가 + 프리체크
  simulation.py          # collect→detect→evaluate→precheck→paper 검증 파이프라인
  live_collectors.py     # read-only provider job runner
  provider_scheduler.py  # 폴링 스케줄러 (interval/jitter/backoff/fallback)
  providers/             # read-only HTTP provider 어댑터 (capability 기반)
  collectors/            # DEX/CEX/FX 관측 수집
  paper_execution.py     # paper(시뮬) 실행 saga
  *_execution.py         # auto_small / live_full 실행 플로우 (dry-run/simulated)
  src/                   # React 모니터 UI (Vite)
tests/                   # pytest (백엔드 계약 테스트)
```

## 실행

백엔드 (Python 3.10+, 외부 런타임 의존성 없음):
```bash
python -m arbitrage.api_server
# 기본 포트는 api_server.py 참조. DB 경로는 ARBITRAGE_DB_PATH 환경변수로 override 가능.
```

UI (Vite):
```bash
cd arbitrage
npm install
npm run build      # 또는 npm run dev
```

테스트:
```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

## 안전 경계

- 실제 DEX swap / bridge / CEX 주문 / 지갑 서명 / CEX 출금: **모두 비활성**
- provider 어댑터는 read-only(공개 시세) + deterministic 시뮬만
- 원시 시크릿은 DB/로그/SSE/스냅샷에 저장하지 않음 (env/시크릿 매니저에서만 해석)
- 실거래 활성화는 별도의 명시적 provider 어댑터 + 권한 감사 + reconcile 검증을 요구하며, 이 저장소 범위 밖입니다.
