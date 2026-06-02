export const TEST_FLOW_ACTION_DELAY_MS = 5000;

export function testFlowDelay(step) {
  return step * TEST_FLOW_ACTION_DELAY_MS;
}

export const routeEdges = [
  { id: "signal-precheck", source: "signal", target: "precheck" },
  { id: "precheck-buy", source: "precheck", target: "dexBuy" },
  { id: "buy-bridge", source: "dexBuy", target: "bridge" },
  { id: "buy-same", source: "dexBuy", target: "sameChainSell" },
  { id: "buy-wallet-hold", source: "dexBuy", target: "walletHold" },
  { id: "bridge-cross-chain-sell", source: "bridge", target: "crossChainSell" },
  { id: "bridge-cex-deposit", source: "bridge", target: "cexDeposit" },
  { id: "cex-deposit-sell", source: "cexDeposit", target: "cexSell" }
];

export const flowCanvasNodes = [
  { id: "signal", title: "감지", position: { x: 0, y: 408 } },
  { id: "precheck", title: "프리체크", position: { x: 250, y: 408 } },
  { id: "dexBuy", title: "매수", position: { x: 500, y: 408 } },
  { id: "bridge", title: "브릿지", position: { x: 750, y: 100 } },
  { id: "sameChainSell", title: "동일체인 매도", position: { x: 750, y: 300 } },
  { id: "walletHold", title: "지갑보유", position: { x: 750, y: 528 } },
  { id: "crossChainSell", title: "타체인 매도", position: { x: 1000, y: 100 } },
  { id: "cexDeposit", title: "CEX 입금", position: { x: 1000, y: 300 } },
  { id: "cexSell", title: "매도", position: { x: 1250, y: 300 } }
];

export const clusterNodes = [
  {
    id: "cluster-same",
    type: "cluster",
    position: { x: 0, y: 30 },
    data: {
      title: "동일 체인 DEX",
      state: "wait",
      badge: "대기",
      detail: "매수 체인에서 바로 sell quote를 잡는 가장 짧은 경로"
    }
  },
  {
    id: "cluster-bridge-dex",
    type: "cluster",
    position: { x: 270, y: 30 },
    data: {
      title: "브릿지 + DEX",
      state: "wait",
      badge: "대기",
      detail: "도착 체인 DEX 가격이 유지될 때만 활성화"
    }
  },
  {
    id: "cluster-bridge-cex",
    type: "cluster",
    position: { x: 540, y: 30 },
    data: {
      title: "브릿지 + CEX",
      state: "blocked",
      badge: "차단",
      detail: "브릿지 후 입금망과 CEX 반영 상태를 같이 확인"
    }
  },
  {
    id: "cluster-direct-cex",
    type: "cluster",
    position: { x: 810, y: 30 },
    data: {
      title: "직접 CEX",
      state: "blocked",
      badge: "차단",
      detail: "CEX 입금망이 열려 있을 때만 수동 승인 후보"
    }
  }
];

export const clusterEdges = [
  { id: "cluster-same-bridge", source: "cluster-same", target: "cluster-bridge-dex", type: "straight" },
  { id: "cluster-bridge-cex", source: "cluster-bridge-dex", target: "cluster-bridge-cex", type: "straight" },
  { id: "cluster-cex-direct", source: "cluster-bridge-cex", target: "cluster-direct-cex", type: "straight" }
];

export const opportunities = {
  "sol-polygon-dex": {
    symbol: "SOL",
    chain: "polygon",
    chainLabel: "Polygon",
    route: "DEX-DEX",
    defaultStatus: "NEW",
    spread: "+20.98%",
    spreadFloor: ">=10%",
    precheckPass: true,
    title: "선택 후보: SOL / Polygon",
    routeSummary: "SOL · QUICKSWAP V2 -> UNISWAP V3",
    initialStep: "매수 진행중",
    currentAsset: "SOL / Polygon",
    buy: {
      label: "BUY · QUICKSWAP V2",
      chain: "POLYGON",
      token: "0x7dff46b4f9a8d2c1e96b03dd11184...",
      pool: "0x89838605f0ed1b53ecad9b38f58f6...",
      price: "$71.33"
    },
    sell: {
      label: "SELL · UNISWAP V3",
      chain: "POLYGON",
      token: "0xd93f7e14f7c9082d4de4f4a66cce750...",
      pool: "0x019c294b45c2b78e9410be78f812cf7...",
      price: "$86.29"
    },
    initialNodes: {
      signal: ["done", "완료", "감지 출처: QUICKSWAP V2 Polygon pool 0x898386...f6... / 비교 대상: UNISWAP V3 Polygon / +20.98% / WATCH TARGET: QUICKSWAP V2 pool 0x89838605f0ed...6b78ef"],
      precheck: ["done", "완료", "프리체크: sell quote PASS / liquidity PASS / transfer PASS / pool sync PASS / CHECK RESULT: UNISWAP V3 sell quote PASS / pool 0x019c294b...cf183a"],
      dexBuy: ["active", "진행중", "매수: QUICKSWAP V2 on Polygon / max $1,000 / slippage 1.8%"],
      bridge: ["wait", "대기", "브릿지: Polygon -> Ethereum / bridge quote 대기 / 수동 승인"],
      sameChainSell: ["wait", "대기", "동일체인 매도: Polygon -> Polygon / UNISWAP V3 bid $86.29"],
      walletHold: ["wait", "대기", "지갑보유: 0x7777777777777777777777777777777777777777 / 매수 후 정지 / 지갑 주소"],
      crossChainSell: ["wait", "대기", "타체인 매도: Ethereum DEX / quote 대기"],
      cexDeposit: ["blocked", "차단", "CEX 입금: BINANCE / Ethereum network BLOCK / 입금 거래소"],
      cexSell: ["blocked", "차단", "매도: BINANCE SOL/USDT / 입금 반영 전 주문 금지"]
    },
    initialEdges: {
      "signal-precheck": "done",
      "precheck-buy": "active",
      "buy-bridge": "wait",
      "buy-same": "wait",
      "buy-wallet-hold": "wait",
      "bridge-cross-chain-sell": "wait",
      "bridge-cex-deposit": "blocked",
      "cex-deposit-sell": "blocked"
    },
    execution: [
      { delay: testFlowDelay(0), step: "프리체크 재확인", simulation: "실행 시뮬레이션: 프리체크", node: ["precheck", "active", "진행중", "프리체크: sell quote 재확인 / liquidity PASS / pool reserve PASS / CHECK RESULT: UNISWAP V3 sell quote PASS / pool 0x019c294b...cf183a"], edge: ["precheck-buy", "active"], log: ["프리체크", "QUICKSWAP V2 / Polygon", "UNISWAP V3 / Polygon", "+20.98%", "quote 재확인"] },
      { delay: testFlowDelay(1), step: "매수 제출", simulation: "실행 시뮬레이션: 매수 tx", node: ["precheck", "done", "완료", "프리체크: sell quote PASS / liquidity PASS / transfer PASS / pool sync PASS / CHECK RESULT: UNISWAP V3 sell quote PASS / pool 0x019c294b...cf183a"], nextNode: ["dexBuy", "active", "진행중", "매수: QUICKSWAP V2 on Polygon / max $1,000 / slippage 1.8%"], edge: ["precheck-buy", "done"], log: ["매수", "QUICKSWAP V2 / Polygon", "UNISWAP V3 / Polygon", "+20.98%", "tx 생성"] },
      { delay: testFlowDelay(2), step: "동일체인 매도 감시", simulation: "실행 시뮬레이션: exit route 선택", node: ["dexBuy", "done", "완료", "매수: QUICKSWAP V2 on Polygon / 가상 fill 완료"], nextNode: ["sameChainSell", "active", "진행중", "동일체인 매도: Polygon -> Polygon / UNISWAP V3 bid $86.29"], edge: ["buy-same", "active"], extraEdges: [["buy-bridge", "wait"], ["buy-wallet-hold", "wait"]], log: ["route 선택", "QUICKSWAP V2 / Polygon", "UNISWAP V3 / Polygon", "+20.98%", "동일체인 선택"] },
      { delay: testFlowDelay(3), step: "정산 완료", simulation: "실행 시뮬레이션: 완료", node: ["sameChainSell", "done", "완료", "동일체인 매도: UNISWAP V3 Polygon / 가상 매도 완료"], edge: ["buy-same", "done"], log: ["정산", "QUICKSWAP V2 / Polygon", "UNISWAP V3 / Polygon", "+20.98%", "가상 PnL +$149.60"], position: "SOL 가상거래 정산 완료" }
    ]
  },
  "test-flow-demo": {
    symbol: "TEST",
    chain: "base",
    chainLabel: "Base",
    route: "DEX-CEX",
    defaultStatus: "EXEC TEST",
    spread: "+55.40%",
    spreadFloor: ">=50%",
    precheckPass: true,
    title: "선택 후보: TEST / Base",
    routeSummary: "TEST · BASESWAP V2 -> UPBIT KRW",
    initialStep: "테스트 실행 대기",
    currentAsset: "TEST / Base",
    buy: {
      label: "BUY · BASESWAP V2",
      chain: "BASE",
      token: "0x111111111111111111111111111111...",
      pool: "0x22222222...pool",
      price: "$0.4200"
    },
    sell: {
      label: "SELL · UPBIT KRW",
      chain: "KRW",
      token: "TEST/KRW",
      pool: "deposit BASE",
      price: "₩900"
    },
    initialNodes: {
      signal: ["wait", "대기", "감지 출처: BASESWAP V2 Base pool 0x22222222...pool / 비교 대상: UPBIT KRW / +55.40% / TARGET POOL: BASESWAP V2 pool 0x22222222...pool"],
      precheck: ["wait", "대기", "프리체크: quote PASS / transfer PASS / deposit BASE PASS / depth PASS"],
      dexBuy: ["wait", "대기", "매수: BASESWAP V2 on Base / 가상 주문 준비"],
      bridge: ["wait", "대기", "브릿지: Base -> Ethereum / quote 대기"],
      sameChainSell: ["wait", "대기", "동일체인 매도: Base -> Base / Base DEX quote 대기"],
      walletHold: ["wait", "대기", "지갑보유: 0x8888888888888888888888888888888888888888 / 매수 후 정지 / 지갑 주소"],
      crossChainSell: ["wait", "대기", "타체인 매도: Ethereum DEX / quote 대기"],
      cexDeposit: ["wait", "대기", "CEX 입금: UPBIT TEST / Base network / 가상 반영 중 / 입금 거래소"],
      cexSell: ["wait", "대기", "매도: UPBIT TEST/KRW / 가상 매도 대기"]
    },
    initialEdges: {
      "signal-precheck": "wait",
      "precheck-buy": "wait",
      "buy-bridge": "wait",
      "buy-same": "wait",
      "buy-wallet-hold": "wait",
      "bridge-cross-chain-sell": "wait",
      "bridge-cex-deposit": "wait",
      "cex-deposit-sell": "wait"
    },
    execution: [
      { delay: testFlowDelay(0), step: "감지", simulation: "실행 시뮬레이션: 후보 감지", node: ["signal", "active", "진행중", "감지: BASESWAP V2 $0.4200 vs UPBIT KRW ₩900 = +55.40% / TARGET POOL: BASESWAP V2 pool 0x22222222...pool"], edge: ["signal-precheck", "active"], log: ["감지", "BASESWAP V2 / Base", "UPBIT KRW", "+55.40%", "target pool 감지"] },
      { delay: testFlowDelay(1), step: "프리체크", simulation: "실행 시뮬레이션: 프리체크 통과", node: ["signal", "done", "완료", "감지: BASESWAP V2 $0.4200 vs UPBIT KRW ₩900 = +55.40% / TARGET POOL: BASESWAP V2 pool 0x22222222...pool"], nextNode: ["precheck", "active", "진행중", "프리체크: quote PASS / transfer PASS / deposit BASE PASS / depth PASS"], edge: ["signal-precheck", "done"], extraEdges: [["precheck-buy", "active"]], log: ["프리체크", "BASESWAP V2 / Base", "UPBIT KRW", "+55.40%", "PASS"] },
      { delay: testFlowDelay(2), step: "매수", simulation: "실행 시뮬레이션: 매수 가상 fill", node: ["precheck", "done", "완료", "프리체크: quote PASS / transfer PASS / deposit BASE PASS / depth PASS"], nextNode: ["dexBuy", "active", "진행중", "매수: BASESWAP V2 on Base / 가상 fill 진행"], edge: ["precheck-buy", "done"], log: ["매수", "BASESWAP V2 / Base", "UPBIT KRW", "+55.40%", "가상 fill"] },
      { delay: testFlowDelay(3), step: "브릿지 선택", simulation: "실행 시뮬레이션: 매수 후 브릿지 경로 선택", node: ["dexBuy", "done", "완료", "매수: BASESWAP V2 on Base / 가상 fill 완료"], nextNode: ["bridge", "active", "진행중", "브릿지: Base -> Ethereum / bridge quote 진행"], edge: ["buy-bridge", "active"], extraEdges: [["buy-same", "wait"], ["buy-wallet-hold", "wait"]], log: ["route 선택", "BASESWAP V2 / Base", "UPBIT KRW", "+55.40%", "브릿지 선택"] },
      { delay: testFlowDelay(4), step: "CEX 입금 반영", simulation: "실행 시뮬레이션: CEX 입금 준비", node: ["bridge", "done", "완료", "브릿지: Base -> Ethereum / 가상 bridge 완료"], nextNode: ["cexDeposit", "active", "진행중", "CEX 입금: UPBIT TEST / Ethereum network / 가상 반영 중 / 입금 거래소"], edge: ["bridge-cex-deposit", "active"], extraEdges: [["buy-bridge", "done"], ["bridge-cross-chain-sell", "wait"]], log: ["CEX 입금", "BASESWAP V2 / Base", "UPBIT KRW", "+55.40%", "입금 반영"] },
      { delay: testFlowDelay(5), step: "테스트 정산 완료", simulation: "실행 시뮬레이션: 완료", node: ["cexDeposit", "done", "완료", "CEX 입금: UPBIT TEST / Ethereum network / 가상 반영 완료"], nextNode: ["cexSell", "done", "완료", "매도: UPBIT TEST/KRW / 가상 매도 / 정산 완료"], edge: ["cex-deposit-sell", "done"], log: ["정산", "BASESWAP V2 / Base", "UPBIT KRW", "+55.40%", "가상 PnL +₩480,000"], position: "TEST 가상거래 정산 완료" }
    ]
  }
};

export const initialLogs = [
  ["12:42:18", "SOL", "매수", "QUICKSWAP V2 / Polygon", "UNISWAP V3 / Polygon", "+20.98%", "tx 생성 대기"]
];
