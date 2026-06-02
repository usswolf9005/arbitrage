import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  getStraightPath
} from "@xyflow/react";
import {
  TEST_FLOW_ACTION_DELAY_MS,
  flowCanvasNodes,
  initialLogs,
  opportunities,
  routeEdges,
  testFlowDelay
} from "./data.js";

const nodeTypes = {
  flow: FlowNode
};

const edgeTypes = {
  timed: TimedEdge
};

const CANDIDATE_PAGE_SIZE = 5;
const TEST_CANDIDATE_FILLER_COUNT = 5;

const straightRouteEdges = new Set([
  "signal-precheck",
  "precheck-buy",
  "bridge-cross-chain-sell",
  "cex-deposit-sell"
]);

const legacyFlowNodeAliases = {
  sameDexSell: ["sameChainSell"],
  bridgeDexSell: ["bridge", "crossChainSell"],
  bridgeCexDeposit: ["bridge", "cexDeposit"],
  bridgeCexSell: ["cexSell"],
  directCexDeposit: ["walletHold", "cexDeposit"],
  directCexSell: ["cexSell"]
};

const legacyFlowEdgeAliases = {
  "buy-bridge-dex": ["buy-bridge", "bridge-cross-chain-sell"],
  "buy-bridge-cex": ["buy-bridge", "bridge-cex-deposit"],
  "bridge-cex-sell": ["cex-deposit-sell"],
  "buy-direct-cex": ["buy-wallet-hold"],
  "direct-cex-sell": ["cex-deposit-sell"]
};

const stateLabels = {
  done: "완료",
  active: "진행중",
  wait: "대기",
  blocked: "차단",
  warn: "확인",
  failed: "실패",
  skipped: "건너뜀"
};

const stateColors = {
  done: "#35d48b",
  active: "#55d8ff",
  wait: "#ffc15c",
  blocked: "#ff6b7a",
  warn: "#ffc15c",
  failed: "#ff6b7a",
  skipped: "#687484"
};

function stateBadgeClass(state) {
  return ["blocked", "failed"].includes(state) ? "off" : state;
}

function arrayToNodeState(node) {
  return { state: node[0], badge: node[1], detail: node[2] };
}

function initialNodeState(candidate) {
  return Object.fromEntries(
    Object.entries(candidate.initialNodes).map(([id, node]) => [id, arrayToNodeState(node)])
  );
}

function formatElapsed(milliseconds) {
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

function actionModeLabel(modeValue) {
  const normalized = String(modeValue || "monitor").toLowerCase();
  if (normalized === "paper") return "가상";
  if (normalized === "auto_small") return "소액";
  if (normalized === "live_full") return "전체";
  if (normalized === "one_click") return "승인";
  if (normalized === "precheck") return "프리체크";
  return "모니터링";
}

function compactLabel(value, fallback = "-") {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return fallback;
  return text.length > 30 ? `${text.slice(0, 27)}...` : text;
}

function compactFlowRuntimeText(value, maxLength = 26) {
  const text = String(value || "")
    .replace(/\s+/g, " ")
    .replace(/liquidity/gi, "liq")
    .replace(/transfer/gi, "xfer")
    .replace(/reserve/gi, "resv")
    .replace(/recheck/gi, "rechk")
    .replace(/재확인/g, "재확")
    .replace(/진행중/g, "진행")
    .trim();
  if (!text) return "-";
  return text.length > maxLength ? `${text.slice(0, Math.max(1, maxLength - 3))}...` : text;
}

const compactVenueAliases = {
  QUICKSWAP: "QSWAP",
  UNISWAP: "UNI",
  BASESWAP: "BSWAP",
  SUSHI: "SUSHI",
  PANCAKE: "CAKE",
  CURVE: "CURVE",
  UPBIT: "UPBIT",
  BINANCE: "BINANCE",
  OKX: "OKX",
  BYBIT: "BYBIT"
};

function shortVenueName(value, fallback = "-") {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return fallback;
  const upper = text.toUpperCase();
  const matchedVenue = Object.keys(compactVenueAliases).find((venue) => upper.includes(venue));
  if (matchedVenue) return compactVenueAliases[matchedVenue];
  if (upper.includes(["BITH", "UMB"].join(""))) return "Bithumb";
  if (upper.includes("BRIDGE") || text.includes("브릿지")) return "BRIDGE";
  if (upper.includes("CEX") || text.includes("입금")) return "CEX";
  if (upper.includes("DEX")) return "DEX";
  if (/0x[0-9a-f]/i.test(text) || upper.includes("POOL")) return "POOL";
  return compactLabel(text.split(/[/:|·]/)[0] || text, fallback);
}

function shortChainName(value, fallback = "-") {
  const upper = String(value || "").toUpperCase();
  if (!upper) return fallback;
  if (upper.includes("POLYGON") || upper === "POLY") return "POLY";
  if (upper.includes("ETHEREUM") || upper === "ETH") return "ETH";
  if (upper.includes("BASE")) return "BASE";
  if (upper.includes("BSC") || upper.includes("BNB")) return "BSC";
  if (upper.includes("ARBITRUM")) return "ARB";
  if (upper.includes("KRW")) return "KRW";
  return compactLabel(upper, fallback);
}

function chainPairFromDetail(detail) {
  const match = String(detail || "").match(/([A-Za-z]+)\s*->\s*([A-Za-z]+)/);
  if (!match) return null;
  return [shortChainName(match[1]), shortChainName(match[2])];
}

function venueTone(value, role = "") {
  const text = String(value || "").toUpperCase();
  if (role === "buy" || /QUICKSWAP|BASESWAP|SUSHI|PANCAKE|CURVE/.test(text)) return "dex-buy";
  if (role === "sell" || /UNISWAP|DEX/.test(text)) return "dex-sell";
  if (/upbit|bithumb|binance|okx|bybit|cex|krw/.test(text.toLowerCase())) return "cex";
  if (/차단|BLOCK|FAILED|ERROR|만료/.test(text)) return "risk";
  if (/BRIDGE|브릿지/.test(text)) return "bridge";
  return "neutral";
}

function detailParts(detail) {
  return String(detail || "")
    .split(/\s*\/\s*/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function extractDetailValue(parts, prefix) {
  const row = parts.find((part) => part.toLowerCase().startsWith(prefix.toLowerCase()));
  return row ? row.slice(prefix.length).replace(/^[:\s]+/, "").trim() : "";
}

function extractMetric(detail, badge) {
  const spread = String(detail || "").match(/[+-]\d+(?:\.\d+)?%/);
  if (spread) return spread[0];
  const elapsed = String(detail || "").match(/\d+(?:\.\d+)?s/);
  if (elapsed) return elapsed[0];
  return badge || "대기";
}

function compactFlowDetailPart(part) {
  const text = String(part || "")
    .replace(/^WATCH TARGET:/i, "감시")
    .replace(/^CHECK RESULT:/i, "체크")
    .replace(/^TARGET POOL:/i, "타겟")
    .replace(/^프리체크:/, "체크:")
    .replace(/^동일체인 매도:/, "동일체인:")
    .replace(/^타체인 매도:/, "타체인:")
    .replace(/^CEX 입금:/, "CEX 입금:")
    .replace(/^브릿지:/, "브릿지:")
    .replace(/^지갑보유:/, "지갑:")
    .replace(/^CEX 매도:/, "CEX 매도:")
    .replace(/^매수:/, "매수:")
    .replace(/^매도:/, "매도:")
    .replace(/0x[0-9a-fA-F.]+/g, (match) => shortAddress(match));
  return compactFlowRuntimeText(text);
}

function shortFlowDetail(parts, fallback = "상태 대기") {
  const filtered = parts
    .filter((part) => !part.startsWith("감지 출처:"))
    .filter((part) => !part.startsWith("비교 대상:"))
    .filter((part) => !part.startsWith("spread "))
    .filter((part) => !part.startsWith("route "))
    .filter((part) => !part.startsWith("run "))
    .slice(0, 2)
    .map(compactFlowDetailPart);
  return filtered.join(" · ") || fallback;
}

function flowNodeVisual(data) {
  const parts = detailParts(data.detail);
  if (data.state === "skipped") {
    const isBridge = data.id.toLowerCase().includes("bridge");
    const isCex = data.id.toLowerCase().includes("cex");
    const isWallet = data.id === "walletHold";
    return {
      source: isBridge ? "BRIDGE" : (isCex ? "CEX" : (isWallet ? "WALLET" : "미사용")),
      sourceTone: isBridge ? "bridge" : (isCex ? "cex" : "neutral"),
      target: isWallet ? "HOLD" : (isCex ? "CEX" : (data.id === "dexBuy" ? "포지션" : "DEX")),
      targetTone: isCex ? "cex" : "dex-sell",
      metric: "건너뜀",
      metricLabel: "선택 경로 아님",
      detail: "미선택 분기"
    };
  }
  const source = extractDetailValue(parts, "감지 출처") || extractDetailValue(parts, "매수") || data.title;
  const target = extractDetailValue(parts, "비교 대상") || extractDetailValue(parts, "매도") || extractDetailValue(parts, "프리체크") || data.badge;
  const metric = extractMetric(data.detail, data.badge);

  if (data.id === "signal") {
    return {
      source: shortVenueName(source),
      sourceTone: venueTone(source, "buy"),
      target: shortVenueName(target),
      targetTone: venueTone(target, "sell"),
      metric,
      metricLabel: "감지 spread",
      detail: shortFlowDetail(parts, "감지 근거 대기")
    };
  }

  if (data.id === "precheck") {
    const blocker = parts.find((part) => part.includes("차단 사유")) || shortFlowDetail(parts, "검사 결과 대기");
    const checkTarget = blocker.includes("차단") ? "차단" : (data.state === "done" ? "PASS" : compactFlowRuntimeText(blocker, 18));
    return {
      source: "프리체크",
      sourceTone: "neutral",
      target: checkTarget,
      targetTone: venueTone(blocker),
      metric,
      metricLabel: "프리체크",
      detail: shortFlowDetail(parts, blocker)
    };
  }

  if (data.id === "dexBuy") {
    return {
      source: shortVenueName(source),
      sourceTone: venueTone(source, "buy"),
      target: source.toUpperCase().includes("CEX") ? "CEX 매수" : "DEX 매수",
      targetTone: "neutral",
      metric,
      metricLabel: "매수 상태",
      detail: shortFlowDetail(parts, "매수 경로 대기")
    };
  }

  if (data.id === "bridge") {
    const pair = chainPairFromDetail(data.detail) || ["SRC", "DST"];
    return {
      source: pair[0],
      sourceTone: "bridge",
      target: pair[1],
      targetTone: "bridge",
      metric,
      metricLabel: "브릿지",
      detail: shortFlowDetail(parts, "브릿지 경로 대기")
    };
  }

  if (data.id === "sameChainSell") {
    const pair = chainPairFromDetail(data.detail) || ["SRC", "SRC"];
    return {
      source: pair[0],
      sourceTone: "dex-sell",
      target: pair[1],
      targetTone: "dex-sell",
      metric,
      metricLabel: "동일체인",
      detail: shortFlowDetail(parts, "동일체인 매도 대기")
    };
  }

  if (data.id === "walletHold") {
    const address = String(data.detail || "").match(/0x[0-9a-fA-F]{8,}/)?.[0] || "지갑";
    return {
      source: "WALLET",
      sourceTone: "neutral",
      target: shortAddress(address),
      targetTone: "neutral",
      metric,
      metricLabel: "매수 후 정지",
      detail: shortFlowDetail(parts, "지갑보유 대기")
    };
  }

  if (data.id === "crossChainSell") {
    const sell = target || source;
    return {
      source: "타체인",
      sourceTone: "bridge",
      target: shortVenueName(sell),
      targetTone: venueTone(sell, "sell"),
      metric,
      metricLabel: "매도 상태",
      detail: shortFlowDetail(parts, "타체인 매도 대기")
    };
  }

  if (data.id === "cexDeposit") {
    const deposit = parts[0] || "CEX 입금";
    return {
      source: "BRIDGE",
      sourceTone: "bridge",
      target: shortVenueName(deposit),
      targetTone: venueTone(deposit),
      metric,
      metricLabel: "입금 상태",
      detail: shortFlowDetail(parts, "입금망 대기")
    };
  }

  if (data.id === "cexSell") {
    return {
      source: "CEX",
      sourceTone: "cex",
      target: shortVenueName(target || source),
      targetTone: "cex",
      metric,
      metricLabel: "매도 상태",
      detail: shortFlowDetail(parts, "CEX 매도 대기")
    };
  }

  return {
    source: shortVenueName(source),
    sourceTone: venueTone(source),
    target: shortVenueName(target),
    targetTone: venueTone(target),
    metric,
    metricLabel: "상태",
    detail: shortFlowDetail(parts)
  };
}

function horizontalRoutePath(sourceX, sourceY, targetX) {
  return `M ${sourceX},${sourceY} L ${targetX},${sourceY}`;
}

function elbowRoutePath(sourceX, sourceY, targetX, targetY) {
  const midX = Math.round((sourceX + targetX) / 2);
  return `M ${sourceX},${sourceY} L ${midX},${sourceY} L ${midX},${targetY} L ${targetX},${targetY}`;
}

function routeEdgeTimingPosition(sourceX, sourceY, targetX, targetY, straight) {
  if (straight) {
    return { x: Math.round((sourceX + targetX) / 2), y: sourceY + 16 };
  }
  return { x: Math.round((sourceX + targetX) / 2), y: Math.round((sourceY + targetY) / 2) + 18 };
}

function TimedEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  style,
  data
}) {
  const [straightPath] = getStraightPath({
    sourceX,
    sourceY,
    targetX,
    targetY
  });
  const isStraight = straightRouteEdges.has(id);
  const edgePath = isStraight
    ? horizontalRoutePath(sourceX, sourceY, targetX)
    : elbowRoutePath(sourceX, sourceY, targetX, targetY);
  const state = data?.state || "wait";
  const color = data?.color || stateColors[state] || "#526071";
  const timing = routeEdgeTimingPosition(sourceX, sourceY, targetX, targetY, isStraight);
  const strokeWidth = style?.strokeWidth || (state === "active" ? 4 : 3);
  const progress = Math.max(0, Math.min(1, Number(data?.progress ?? 0)));
  const fillMarkerId = `route-arrow-head-fill-${String(id).replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  const fillArrowHeadVisible = state === "active" && progress >= 0.94;
  void straightPath;
  return (
    <>
      <BaseEdge
        id={`${id}-track`}
        path={edgePath}
        className={`route-arrow-track route-arrow-${state}`}
        markerEnd={markerEnd}
        style={{ stroke: color, strokeWidth }}
      />
      {state !== "active" && (
        <BaseEdge
          id={id}
          path={edgePath}
          markerEnd={markerEnd}
          className={`route-arrow-main route-arrow-${state}`}
          style={{ ...style, stroke: color, strokeWidth }}
        />
      )}
      {state === "active" && (
        <>
          <defs>
            <marker
              id={fillMarkerId}
              markerWidth="10"
              markerHeight="10"
              refX="9"
              refY="5"
              orient="auto"
              markerUnits="strokeWidth"
            >
              <path className="route-arrow-head-fill" d="M 0 0 L 10 5 L 0 10 z" style={{ fill: color }} />
            </marker>
          </defs>
          <path
            id={`${id}-progress`}
            className="react-flow__edge-path route-arrow-fill"
            d={edgePath}
            pathLength="1"
            markerEnd={fillArrowHeadVisible ? `url(#${fillMarkerId})` : undefined}
            style={{ stroke: color, strokeWidth, "--route-progress": progress }}
          />
        </>
      )}
      <EdgeLabelRenderer>
        <div
          className={`route-edge-timing ${state}`}
          data-route-timer={data?.label || "대기 0.0s"}
          style={{
            position: "absolute",
            transform: `translate(-50%, -50%) translate(${timing.x}px, ${timing.y}px)`,
            "--route-color": color
          }}
        >
          <strong className="route-timer">{data?.label || "대기 0.0s"}</strong>
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

function FlowNode({ data }) {
  const visual = flowNodeVisual(data);
  return (
    <article
      className={`flow-node flow-node-compact-v2 ${data.state} ${data.state === "active" ? "pulse" : ""}`}
      data-step={data.id}
      data-visual-contract="중요 거래소/DEX 색상"
    >
      <Handle type="target" position={Position.Left} className="flow-handle" />
      <div className="flow-node-top">
        <h3 data-node-title>{data.title}</h3>
        <span className={`state-badge ${stateBadgeClass(data.state)}`} data-node-badge>{data.badge}</span>
      </div>
      <div className="flow-target-stack">
        <span className={`flow-target-chip ${visual.sourceTone}`}>{visual.source}</span>
        <span className="flow-target-arrow">→</span>
        <span className={`flow-target-chip ${visual.targetTone}`}>{visual.target}</span>
      </div>
      <div className="flow-metric-row">
        <strong>{visual.metric}</strong>
        <span>{visual.metricLabel}</span>
      </div>
      <p className="flow-detail-line" data-node-detail>{visual.detail}</p>
      <Handle type="source" position={Position.Right} className="flow-handle" />
    </article>
  );
}

function makeFlowNodes(nodeStates) {
  return flowCanvasNodes.map((node) => ({
    id: node.id,
    type: "flow",
    position: node.position,
    data: {
      id: node.id,
      title: node.title,
      ...(nodeStates[node.id] || { state: "wait", badge: "대기", detail: "" })
    }
  }));
}

const flowMapPositions = {
  signal: { x: 0, y: 408 },
  precheck: { x: 250, y: 408 },
  dexBuy: { x: 500, y: 408 },
  bridge: { x: 750, y: 100 },
  sameChainSell: { x: 750, y: 300 },
  walletHold: { x: 750, y: 528 },
  crossChainSell: { x: 1000, y: 100 },
  cexDeposit: { x: 1000, y: 300 },
  cexSell: { x: 1250, y: 300 }
};

function flowMapNodePosition(nodeId, index) {
  return flowMapPositions[nodeId] || { x: index * 250, y: 408 };
}

function shortAddress(value) {
  const text = String(value || "");
  if (!text) return "-";
  if (!text.startsWith("0x") || text.length <= 18) return text;
  return `${text.slice(0, 8)}...${text.slice(-6)}`;
}

function selectedActionPairLabel(candidate) {
  if (candidate?.routeSummary) return candidate.routeSummary;
  const venues = [candidate?.buy?.label, candidate?.sell?.label].filter(Boolean).join(" -> ");
  return [candidate?.currentAsset, venues].filter(Boolean).join(" · ") || "-";
}

function walletAddressForAction(candidate, snapshot) {
  const direct = candidate?.wallet_address || snapshot?.selected_execution_run?.wallet_address;
  if (direct) return direct;
  const walletDetail = candidate?.initialNodes?.walletHold?.[2] || "";
  const match = walletDetail.match(/0x[a-fA-F0-9]{10,}/);
  return match?.[0] || "0x7777777777777777777777777777777777777777";
}

function shortWalletForAction(value) {
  const text = String(value || "");
  if (!text) return "지갑 미설정";
  if (text.length <= 12) return text;
  return `${text.slice(0, 4)}...${text.slice(-5)}`;
}

async function copyAddress(value) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Fall back for non-HTTPS or permission-limited browser contexts.
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

function CopyAddress({ label, value }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy(event) {
    event.stopPropagation();
    await copyAddress(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 900);
  }

  return (
    <p className="contract-row">
      <span className="contract-label">{label}</span>
      <button
        className="copy-address"
        type="button"
        aria-label={`${label} 주소 복사`}
        title={`${label} 주소 복사`}
        onClick={handleCopy}
      >
        <span className="copy-icon" aria-hidden="true">📋</span>
        <span className="copy-value" title={value}>{shortAddress(value)}</span>
        <span className="copy-state">{copied ? "복사됨" : "복사"}</span>
        <span className={`copy-toast ${copied ? "show" : ""}`} aria-live="polite">주소 복사됨</span>
      </button>
    </p>
  );
}

function flashActionButton(event) {
  const button = event?.currentTarget;
  if (!button || !button.classList) return;
  button.classList.remove("action-clicked");
  button.dataset.actionFeedback = "";
  // Force a reflow so repeated clicks replay the acknowledgement animation.
  void button.offsetWidth;
  button.dataset.actionFeedback = "pressed";
  button.classList.add("action-clicked");
  window.setTimeout(() => {
    button.classList.remove("action-clicked");
    button.dataset.actionFeedback = "";
  }, 900);
}

function CandidateCard({ candidate, status, selected, onAction, onSelect }) {
  const approvalId = candidate.approval_id || candidate.latest_approval?.approval_id || candidate.latest_approval?.id || "";
  const approvalStatus = String(candidate.approval_status || "MISSING").toUpperCase();
  const approvalRequired = Boolean(candidate.approval_required);
  const hasBackendRoute = Boolean(candidate.backendOnly && candidate.backendId && candidate.routeId);
  const canDecideApproval = hasBackendRoute && Boolean(approvalId);
  const autoSmallState = autoSmallDryRunState(candidate, status);
  const route = candidate.selected_route || {};
  const liveFullState = liveFullRouteState(candidate, status);
  const liveFullApprovalId = route.live_full_approval_id || candidate.live_full_approval_id || "";
  const canDecideLiveFullApproval = hasBackendRoute && Boolean(liveFullApprovalId);
  const payload = routePayload(route);
  const depositNetwork = candidate.sell.deposit_network || candidate.sell.pool || candidate.sell.chain || "-";
  const bridgeStatus = providerStatusFromPayload(payload, ["bridge_status", "bridge_availability", "bridge_route", "bridge"]);
  const cexDepositStatus = providerStatusFromPayload(payload, ["cex_deposit_status", "deposit_status", "cex_deposit"]);
  const cexMarket = candidate.sell.token || route.cex_market || candidate.sell.pool || "-";
  const boundary = candidate.live_full_boundary || {};

  return (
    <article
      className={`opportunity-card compact-card ${selected ? "selected" : ""} active`}
      tabIndex="0"
      data-candidate-id={candidate.id}
      data-route={candidate.route}
      data-route-id={candidate.routeId || candidate.selected_route_id || ""}
      data-chain={candidate.chain}
      data-status={status}
      data-source={candidate.testOnly ? "test" : (candidate.backendOnly ? "backend" : "fallback")}
      data-approval-status={candidate.backendOnly ? approvalStatus : ""}
      data-approval-required={candidate.backendOnly ? String(approvalRequired) : ""}
      data-approval-id={candidate.backendOnly ? approvalId : ""}
      data-live-full-route-ready={candidate.backendOnly ? String(liveFullState.enabled) : ""}
      data-live-full-disabled-reason={candidate.backendOnly ? liveFullState.reason : ""}
      data-live-full-boundary={candidate.backendOnly ? LIVE_FULL_PROVIDER_BOUNDARY : ""}
      data-cex-withdrawal-enabled="false"
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="candidate-head">
        <div className="candidate-title">
          <span className="symbol">{candidate.symbol}</span>
          <span className="route-label">{candidate.route} | {candidate.chainLabel} | route {candidate.routeId || candidate.selected_route_id || "demo"} | {status} | {candidate.spreadFloor}</span>
          <span className={`state-badge ${status === "진행중" ? "active" : "active"}`}>
            {status === "진행중" ? "진행중" : "매수 준비"}
          </span>
        </div>
        <strong className={candidate.spread.startsWith("+") ? "result-plus" : "result-warn"}>{candidate.spread}</strong>
      </div>

      <section className="venue-card buy">
        <h3><span className="tag buy">BUY</span>{candidate.buy.label}</h3>
        <strong>CHAIN: {candidate.buy.chain}</strong>
        <CopyAddress label="token CA" value={candidate.buy.token} />
        <CopyAddress label="pool CA" value={candidate.buy.pool} />
        <small>ask</small>
        <b>{candidate.buy.price}</b>
      </section>

      <section className="venue-card sell">
        <h3><span className="tag sell">SELL</span>{candidate.sell.label}</h3>
        <strong>CHAIN: {candidate.sell.chain}</strong>
        <CopyAddress label="token CA" value={candidate.sell.token} />
        <CopyAddress label="pool CA" value={candidate.sell.pool} />
        <small>bid</small>
        <b>{candidate.sell.price}</b>
      </section>

      {candidate.backendOnly && (
        <section
          className="live-route-panel"
          data-live-full-route-type={candidate.routeType || ""}
          data-live-full-approval-status={route.live_full_approval_status || "MISSING"}
          data-live-full-disabled-reason={liveFullState.reason}
          data-provider-boundary={LIVE_FULL_PROVIDER_BOUNDARY}
          data-simulated-boundary={String(Boolean(boundary.simulated ?? true))}
          data-cex-withdrawal-enabled="false"
        >
          <h3>
            <span>전체</span>
            <span className={`state-badge ${liveFullState.enabled ? "done" : "warn"}`}>
              {liveFullState.enabled ? "준비 완료" : "차단"}
            </span>
          </h3>
          <div className="live-route-grid">
            <span>route_type <strong>{candidate.routeType || "-"}</strong></span>
            <span>deposit_network <strong>{depositNetwork}</strong></span>
            <span>bridge_status <strong>{bridgeStatus}</strong></span>
            <span>cex_deposit <strong>{cexDepositStatus}</strong></span>
            <span>cex_market <strong>{cexMarket}</strong></span>
            <span>approval <strong>{route.live_full_approval_status || "MISSING"}</strong></span>
          </div>
          <p className="live-route-boundary">
            {LIVE_FULL_ROUTE_BOUNDARY_LABEL} · simulated {String(Boolean(boundary.simulated ?? true))} · CEX withdrawal disabled
          </p>
          <p className="live-route-blocker" data-live-full-blocker-list={liveFullState.blockers.join(",")}>
            {liveFullState.blockers.length ? liveFullState.blockers.join(" · ") : "no live_full blockers"}
          </p>
          <div className="live-route-actions">
            <button
              className="action-button primary"
              type="button"
              data-action="live-full-approval-request"
              data-mode="live_full"
              data-feedback="click"
              data-critical-action="true"
              data-opportunity-id={candidate.backendId || ""}
              data-route-id={candidate.routeId || ""}
              data-trade-amount-krw={LIVE_FULL_TRADE_AMOUNT_KRW}
              disabled={!hasBackendRoute || !LIVE_FULL_ROUTE_TYPES.has(candidate.routeType)}
              onClick={(event) => onAction(event, "live-full-approval-request")}
            >
              전체 경로 승인 요청
            </button>
            <button
              className="action-button primary"
              type="button"
              data-action="live-full-approval-approve"
              data-feedback="click"
              data-critical-action="true"
              data-approval-id={liveFullApprovalId}
              data-opportunity-id={candidate.backendId || ""}
              data-route-id={candidate.routeId || ""}
              disabled={!canDecideLiveFullApproval}
              onClick={(event) => onAction(event, "live-full-approval-approve")}
            >
              전체 경로 승인
            </button>
          </div>
        </section>
      )}

      <div className="candidate-actions">
        <button className="action-button primary" type="button" data-action="select" data-feedback="click" data-critical-action="true" data-candidate-id={candidate.id} onClick={(event) => onAction(event, "select")}>선택</button>
        <button className="action-button primary" type="button" data-action="watch" data-feedback="click" data-critical-action="true" data-candidate-id={candidate.id} onClick={(event) => onAction(event, "watch")}>감시</button>
        <button className="action-button primary" type="button" data-action="precheck" data-feedback="click" data-critical-action="true" data-candidate-id={candidate.id} onClick={(event) => onAction(event, "precheck")}>프리체크</button>
        <button
          className="action-button simulation-action"
          type="button"
          data-action="simulation-run"
          data-mode="paper"
          data-feedback="click"
          data-critical-action="true"
          data-candidate-id={candidate.id}
          title="실거래 전 성공/실패 검증"
          onClick={(event) => onAction(event, "simulation-run")}
        >
          검증
        </button>
        <button className="action-button" type="button" data-action="execute" data-mode="paper" data-feedback="click" data-critical-action="true" data-candidate-id={candidate.id} title="전체 경로 가상거래 실행" onClick={(event) => onAction(event, "execute")}>가상</button>
        <button
          className="action-button dry-run-action"
          type="button"
          data-action="auto-small-dry-run"
          data-mode="auto_small"
          data-feedback="click"
          data-critical-action="true"
          data-dry-run="true"
          data-submit-boundary="same-chain-dex-dry-run-no-real-submit"
          data-opportunity-id={candidate.backendId || ""}
          data-route-id={candidate.routeId || ""}
          data-route-type={candidate.routeType || candidate.selected_route?.route_type || ""}
          data-disabled-reason={autoSmallState.reason}
          disabled={!autoSmallState.enabled}
          title={autoSmallState.title}
          onClick={(event) => onAction(event, "auto-small-dry-run")}
        >
          소액
        </button>
        <button
          className="action-button live-full-action"
          type="button"
          data-action="live-full-route"
          data-mode="live_full"
          data-feedback="click"
          data-critical-action="true"
          data-simulated-boundary="true"
          data-provider-boundary={LIVE_FULL_PROVIDER_BOUNDARY}
          data-cex-withdrawal-enabled="false"
          data-opportunity-id={candidate.backendId || ""}
          data-route-id={candidate.routeId || ""}
          data-route-type={candidate.routeType || candidate.selected_route?.route_type || ""}
          data-disabled-reason={liveFullState.reason}
          disabled={!liveFullState.enabled}
          title={liveFullState.title}
          onClick={(event) => onAction(event, "live-full-route")}
        >
          전체
        </button>
        <button className="action-button danger" type="button" data-action="stop" data-feedback="click" data-critical-action="true" data-candidate-id={candidate.id} onClick={(event) => onAction(event, "stop")}>중단</button>
      </div>

      {candidate.backendOnly && (
        <section className="approval-panel">
          <h3>
            <span>운영 승인</span>
            <span className={`state-badge ${approvalBadgeClass(approvalStatus, approvalRequired)}`}>{approvalStatusText(approvalStatus, approvalRequired)}</span>
          </h3>
          <p>
            approval_id {approvalId || "-"} · route {candidate.routeId || "-"} · {candidate.latest_approval_decision?.operator || candidate.latest_approval?.requested_by || "operator 대기"}
          </p>
          <div className="approval-actions" data-backend-route-ready={String(hasBackendRoute)}>
            <button
              className="action-button primary"
              type="button"
              data-action="approval-request"
              data-mode="one_click"
              data-feedback="click"
              data-critical-action="true"
              data-opportunity-id={candidate.backendId || ""}
              data-route-id={candidate.routeId || ""}
              disabled={!hasBackendRoute}
              onClick={(event) => onAction(event, "approval-request")}
            >
              승인 요청
            </button>
            <button
              className="action-button primary"
              type="button"
              data-action="approval-approve"
              data-feedback="click"
              data-critical-action="true"
              data-approval-id={approvalId}
              data-opportunity-id={candidate.backendId || ""}
              data-route-id={candidate.routeId || ""}
              disabled={!canDecideApproval}
              onClick={(event) => onAction(event, "approval-approve")}
            >
              승인
            </button>
            <button
              className="action-button danger"
              type="button"
              data-action="approval-reject"
              data-feedback="click"
              data-critical-action="true"
              data-approval-id={approvalId}
              data-opportunity-id={candidate.backendId || ""}
              data-route-id={candidate.routeId || ""}
              disabled={!canDecideApproval}
              onClick={(event) => onAction(event, "approval-reject")}
            >
              거절
            </button>
            <button
              className="action-button"
              type="button"
              data-action="one-click-held"
              data-mode="one_click"
              data-feedback="click"
              data-critical-action="true"
              data-submit-boundary="held-no-submit"
              data-opportunity-id={candidate.backendId || ""}
              data-route-id={candidate.routeId || ""}
              data-approval-id={approvalId}
              disabled={!hasBackendRoute}
              onClick={(event) => onAction(event, "one-click-held")}
            >
              One-click 보류
            </button>
          </div>
        </section>
      )}
    </article>
  );
}

function MarketWatchCard({ candidate, status, selected, variant = "watch", onSelect, onAction }) {
  const spreadPercent = candidateSpreadPercent(candidate);
  const buyVenue = cleanVenueLabel(candidate.buy?.label);
  const sellVenue = cleanVenueLabel(candidate.sell?.label);
  const routeType = routeTypeForCandidate(candidate);
  const thresholdText = variant === "strike" ? "30% 이상" : "5% 이상";
  return (
    <article
      className={`watch-card compact-card ${variant} ${selected ? "selected" : ""}`}
      tabIndex="0"
      data-candidate-id={candidate.id}
      data-route={candidate.route}
      data-chain={candidate.chain}
      data-status={status}
      data-source={candidate.testOnly ? "test" : (candidate.backendOnly ? "backend" : "fallback")}
      data-spread-threshold={variant === "strike" ? "30" : "5"}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="watch-card-head">
        <div>
          <span className="watch-symbol">{candidate.symbol}</span>
          <strong>{candidate.spread}</strong>
        </div>
        <span className={`state-badge ${status === "차단" ? "warn" : "active"}`}>{status}</span>
      </div>
      <div className="watch-route-row">
        <span className="venue-chip dex-buy">{buyVenue}</span>
        <span className="route-arrow">→</span>
        <span className={`venue-chip ${venueTone(sellVenue, "sell")}`}>{sellVenue}</span>
      </div>
      <div className="watch-meta-grid">
        <span>조건 <strong>{thresholdText}</strong></span>
        <span>spread <strong>{spreadPercent.toFixed(2)}%</strong></span>
        <span>route <strong>{routeLabel(routeType)}</strong></span>
        <span>edge <strong>{candidateEdgeLabel(candidate)}</strong></span>
      </div>
      <div className="watch-contracts" aria-label="토큰 CA / POOL 복사">
        <CopyAddress label="token CA" value={candidate.buy?.token || "-"} />
        <CopyAddress label="pool CA" value={candidate.buy?.pool || "-"} />
      </div>
      <div className="watch-actions">
        <button
          className="action-button primary"
          type="button"
          data-action="select"
          data-feedback="click"
          data-critical-action="true"
          data-candidate-id={candidate.id}
          onClick={(event) => onAction(event, "select")}
        >
          선택
        </button>
      </div>
    </article>
  );
}

function ApiKeyCard({ api }) {
  const tone = apiStatusTone(api.status);
  return (
    <article className={`api-key-card ${tone}`}>
      <span className={`api-key-dot ${tone}`} aria-hidden="true"></span>
      <div>
        <strong>{api.label}</strong>
        <small>{api.capability} · {api.source}</small>
      </div>
      <em>{apiStatusLabel(api.status)}</em>
    </article>
  );
}

const FRONTEND_DEMO_FALLBACK_ONLY = true;
const API_SNAPSHOT_URL = "/api/arbitrage/snapshot";
const API_STREAM_URL = "/api/arbitrage/stream";
const ONE_CLICK_HELD_TRADE_AMOUNT_KRW = 100000;
const AUTO_SMALL_DRY_RUN_TRADE_AMOUNT_KRW = 100000;
const LIVE_FULL_TRADE_AMOUNT_KRW = 100000;
const DRY_RUN_ONLY_LABEL = "모의실행 · 실제 제출 없음";
const LIVE_FULL_PROVIDER_BOUNDARY = "deterministic_default_or_configured_provider_adapter";
const LIVE_FULL_ROUTE_BOUNDARY_LABEL = "Part 8 live_full deterministic adapters by default / configured provider adapters only";
const LIVE_FULL_ROUTE_TYPES = new Set(["direct_cex_sell", "bridge_dex_sell", "bridge_cex_sell"]);
const SSE_EVENT_TYPES = [
  "execution.step.started",
  "execution.step.completed",
  "execution.step.reconcile",
  "execution.log.append",
  "flow.node.update",
  "flow.edge.update",
  "position.update",
  "transfer.update",
  "order.update",
  "provider.health",
  "provider.job.started",
  "provider.job.completed",
  "provider.job.failed",
  "opportunity.upsert",
  "simulation.run.started",
  "simulation.run.stage",
  "simulation.run.completed",
  "simulation.run.failed",
  "replay_truncated",
  "operator_approval.requested",
  "operator_approval.approved",
  "operator_approval.rejected",
  "alert.operator_approval_requested",
  "error"
];

const SNAPSHOT_RELOAD_EVENT_TYPES = new Set([
  "replay_truncated",
  "opportunity.upsert",
  "provider.job.started",
  "provider.job.completed",
  "provider.job.failed",
  "simulation.run.started",
  "simulation.run.stage",
  "simulation.run.completed",
  "simulation.run.failed"
]);

const BACKEND_PENDING_STATUSES = new Set(["요청중", "진행중", "보류 준비"]);
const BACKEND_TERMINAL_RUN_STATUSES = new Set(["SETTLED", "FAILED", "ABORTED", "MANUAL_REVIEW", "BLOCKED"]);

const routeTypeLabels = {
  same_dex_sell: "DEX-DEX",
  direct_cex_sell: "DEX-CEX",
  bridge_dex_sell: "Bridge",
  bridge_cex_sell: "Bridge"
};

const stepLabels = {
  precheck: "프리체크",
  dex_buy: "매수",
  wallet_hold: "지갑보유",
  exit_route_select: "경로 선택",
  same_dex_sell: "동일체인 매도",
  bridge: "브릿지",
  bridge_dex_sell: "타체인 매도",
  cex_deposit: "CEX 입금",
  cex_sell: "매도",
  settle: "정산"
};

const statusLabels = {
  PRECHECK_PASS: "NEW",
  PASS: "PASS",
  OPEN: "OPEN",
  RUNNING: "진행중",
  POSITION_OPEN: "진행중",
  EXITING: "진행중",
  SETTLED: "SETTLED",
  MANUAL_REVIEW: "확인",
  RECONCILE: "확인",
  BLOCKED: "차단",
  FAILED: "실패",
  COMPLETED: "완료",
  PENDING: "대기",
  EXEC_READY: "보류 준비"
};

const RUN_STATUS_VALUES = new Set([
  "ENTERING",
  "POSITION_OPEN",
  "EXITING",
  "SETTLED",
  "MANUAL_REVIEW",
  "BLOCKED",
  "FAILED",
  "ABORTED",
  "EXEC_READY"
]);

const APPROVAL_EVENT_TYPES = new Set([
  "operator_approval.requested",
  "operator_approval.approved",
  "operator_approval.rejected",
  "alert.operator_approval_requested"
]);

function fallbackOpportunityList() {
  return Object.entries(opportunities).map(([id, candidate]) => ({ id, ...candidate, backendOnly: false }));
}

const testCandidateFillerProfiles = [
  {
    symbol: "WETH",
    chain: "base",
    chainLabel: "Base",
    route: "DEX-DEX",
    spread: "+12.40%",
    spreadFloor: "edge +4.80%",
    routeSummary: "WETH · BASESWAP -> UNISWAP",
    buy: { label: "BUY · BASESWAP", chain: "BASE", token: "0x4200000000000000000000000000000000000006", pool: "0xbase000000000000000000000000000000000001", price: "$3,180" },
    sell: { label: "SELL · UNISWAP", chain: "BASE", token: "0x4200000000000000000000000000000000000006", pool: "0xbase000000000000000000000000000000000002", price: "$3,574" }
  },
  {
    symbol: "ARB",
    chain: "arbitrum",
    chainLabel: "Arbitrum",
    route: "DEX-CEX",
    spread: "+18.25%",
    spreadFloor: "edge +6.12%",
    routeSummary: "ARB · SUSHI -> BINANCE",
    buy: { label: "BUY · SUSHI", chain: "ARBITRUM", token: "0x912ce59144191c1204e64559fe8253a0e49e6548", pool: "0xarb000000000000000000000000000000000001", price: "$1.02" },
    sell: { label: "SELL · BINANCE", chain: "CEX", token: "ARB/USDT", pool: "deposit ARBITRUM", price: "$1.21" }
  },
  {
    symbol: "LINK",
    chain: "polygon",
    chainLabel: "Polygon",
    route: "DEX-DEX",
    spread: "+9.70%",
    spreadFloor: "edge +3.20%",
    routeSummary: "LINK · QUICKSWAP -> CURVE",
    buy: { label: "BUY · QUICKSWAP", chain: "POLYGON", token: "0x53e0bca35ec356bd5dddfebbd1fc0fd03fabad39", pool: "0xlink00000000000000000000000000000000001", price: "$13.20" },
    sell: { label: "SELL · CURVE", chain: "POLYGON", token: "0x53e0bca35ec356bd5dddfebbd1fc0fd03fabad39", pool: "0xlink00000000000000000000000000000000002", price: "$14.48" }
  },
  {
    symbol: "PEPE",
    chain: "eth",
    chainLabel: "Ethereum",
    route: "DEX-CEX",
    spread: "+31.60%",
    spreadFloor: "edge +11.40%",
    routeSummary: "PEPE · UNISWAP -> OKX",
    buy: { label: "BUY · UNISWAP", chain: "ETHEREUM", token: "0x6982508145454ce325ddbe47a25d4ec3d2311933", pool: "0xpepe00000000000000000000000000000000001", price: "$0.000010" },
    sell: { label: "SELL · OKX", chain: "CEX", token: "PEPE/USDT", pool: "deposit ETH", price: "$0.000013" }
  }
];

function buildTestCandidateFillers(candidates, fallbackCandidates) {
  const baseCandidates = Array.isArray(candidates) ? candidates : [];
  if (baseCandidates.length >= TEST_CANDIDATE_FILLER_COUNT) return baseCandidates;
  const templates = (Array.isArray(fallbackCandidates) && fallbackCandidates.length) ? fallbackCandidates : fallbackOpportunityList();
  const missingCount = TEST_CANDIDATE_FILLER_COUNT - baseCandidates.length;
  const fillers = Array.from({ length: missingCount }, (_, index) => {
    const profile = testCandidateFillerProfiles[index % testCandidateFillerProfiles.length];
    const template = templates[index % templates.length] || {};
    return {
      ...template,
      ...profile,
      buy: { ...(template.buy || {}), ...profile.buy },
      sell: { ...(template.sell || {}), ...profile.sell },
      id: `test-candidate-${index + 1}`,
      testOnly: true,
      backendOnly: false,
      defaultStatus: "EXEC TEST",
      precheckPass: true,
      title: `테스트 후보: ${profile.symbol} / ${profile.chainLabel}`,
      currentAsset: `${profile.symbol} / ${profile.chainLabel}`,
      initialStep: "테스트 대기"
    };
  });
  return [...baseCandidates, ...fillers];
}

const EMPTY_MONITOR_CANDIDATE = {
  id: "backend-empty",
  backendOnly: true,
  backendId: 0,
  routeId: 0,
  selected_route_id: 0,
  symbol: "-",
  chain: "",
  chainLabel: "-",
  route: "-",
  routeType: "",
  selected_route: {},
  defaultStatus: "대기",
  precheckPass: false,
  spread: "+0.00%",
  spreadFloor: "edge -",
  currentAsset: "백엔드 후보 없음",
  routeSummary: "선택된 아비트라지 없음",
  initialStep: "대기",
  buy: { label: "BUY 대기", chain: "-", token: "-", pool: "-", price: "-" },
  sell: { label: "SELL 대기", chain: "-", token: "-", pool: "-", price: "-" },
  blockers: ["no_backend_opportunity"],
  live_full_boundary: {}
};

function normalizeState(state) {
  const normalized = String(state || "wait").toLowerCase();
  if (["done", "active", "wait", "blocked", "warn", "failed", "skipped"].includes(normalized)) {
    return normalized;
  }
  return "wait";
}

function labelForStatus(status, state = "") {
  return statusLabels[String(status || "").toUpperCase()] || stateLabels[normalizeState(state)] || String(status || "대기");
}

function approvalStatusText(status, required = false) {
  const normalized = String(status || "").toUpperCase();
  if (normalized === "APPROVED") return "승인 완료";
  if (normalized === "REJECTED") return "승인 거절";
  if (normalized === "PENDING") return "승인 대기";
  if (normalized === "MISSING" || required) return "승인 필요";
  return "승인 불필요";
}

function approvalBadgeClass(status, required = false) {
  const normalized = String(status || "").toUpperCase();
  if (normalized === "APPROVED") return "done";
  if (normalized === "REJECTED") return "off";
  if (normalized === "PENDING") return "wait";
  if (normalized === "MISSING" || required) return "warn";
  return "skipped";
}

function formatBps(value) {
  const numeric = Number(value || 0);
  const sign = numeric >= 0 ? "+" : "";
  return `${sign}${(numeric / 100).toFixed(2)}%`;
}

function formatKrw(value) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric) || numeric <= 0) return "-";
  return `₩${Math.round(numeric).toLocaleString("ko-KR")}`;
}

function formatSignedKrw(value) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric)) return "₩0";
  const rounded = Math.round(numeric);
  const sign = rounded > 0 ? "+" : (rounded < 0 ? "-" : "");
  return `${sign}₩${Math.abs(rounded).toLocaleString("ko-KR")}`;
}

function formatUtcTime(milliseconds) {
  const numeric = Number(milliseconds || 0);
  if (!Number.isFinite(numeric) || numeric <= 0) return "--:--:--";
  return new Date(numeric).toISOString().slice(11, 19);
}

function routeLabel(routeType) {
  return routeTypeLabels[String(routeType || "")] || "DEX-DEX";
}

function routeTypeForCandidate(candidate) {
  return String(candidate?.routeType || candidate?.selected_route?.route_type || "");
}

function routeBlockerList(route) {
  return Array.isArray(route?.blocker_reasons) ? route.blocker_reasons : [];
}

function uniqueStrings(values) {
  return [...new Set((values || []).filter(Boolean).map((value) => String(value)))];
}

function routePayload(route) {
  return route?.payload && typeof route.payload === "object" ? route.payload : {};
}

function providerStatusFromPayload(payload, keys) {
  for (const key of keys) {
    const value = payload?.[key];
    if (value == null) continue;
    if (typeof value === "object") return String(value.status || value.state || value.result || "unknown");
    return String(value);
  }
  return "-";
}

function routeBlockersForCandidate(candidate) {
  const route = candidate?.selected_route || {};
  return uniqueStrings([
    ...routeBlockerList(route),
    ...(Array.isArray(candidate?.blockers) ? candidate.blockers : []),
    ...(Array.isArray(routePayload(route).blockers) ? routePayload(route).blockers : [])
  ]);
}

function liveFullApprovalMatches(candidate) {
  const route = candidate?.selected_route || {};
  if (String(route.live_full_approval_status || "").toUpperCase() !== "APPROVED") return false;
  const amount = Number(route.live_full_approval_amount_krw || route.live_full_approval_payload?.trade_amount_krw || 0);
  if (!amount || Math.abs(amount - LIVE_FULL_TRADE_AMOUNT_KRW) > 0.000001) return false;
  const expiresAt = Number(route.live_full_approval_expires_at_ms || route.live_full_approval_payload?.expires_at_ms || 0);
  if (!expiresAt || expiresAt <= Date.now()) return false;
  return true;
}

function liveFullRouteState(candidate, status = "") {
  const hasBackendRoute = Boolean(candidate?.backendOnly && candidate?.backendId && candidate?.routeId);
  if (!hasBackendRoute) {
    return { enabled: false, reason: "missing_backend_route", title: "Backend route required", blockers: ["missing_backend_route"] };
  }
  const routeType = routeTypeForCandidate(candidate);
  if (!LIVE_FULL_ROUTE_TYPES.has(routeType)) {
    return { enabled: false, reason: "route_type_not_supported", title: "Part 8 live_full supports bridge/CEX routes only", blockers: ["route_type_not_supported"] };
  }
  const route = candidate?.selected_route || {};
  const blockers = routeBlockersForCandidate(candidate);
  const routeStatus = String(route.route_status || status || "").toUpperCase();
  const safetyStatus = String(route.safety_status || candidate?.safetyStatus || "").toUpperCase();
  if (!["OPEN", "DONE"].includes(routeStatus)) {
    const reason = `route_status_${routeStatus || "UNKNOWN"}`;
    return { enabled: false, reason, title: reason, blockers: uniqueStrings([reason, ...blockers]) };
  }
  if (safetyStatus !== "PASS") {
    const reason = `route_safety_${safetyStatus || "UNKNOWN"}`;
    return { enabled: false, reason, title: reason, blockers: uniqueStrings([reason, ...blockers]) };
  }
  if (blockers.length > 0) {
    return { enabled: false, reason: blockers[0], title: blockers.join(" · "), blockers };
  }
  if (!liveFullApprovalMatches(candidate)) {
    return { enabled: false, reason: "operator_approval_required", title: "Matching live_full approval required", blockers: ["operator_approval_required"] };
  }
  return {
    enabled: true,
    reason: "",
    title: `${LIVE_FULL_ROUTE_BOUNDARY_LABEL}; CEX withdrawal disabled`,
    blockers: []
  };
}

function autoSmallDryRunState(candidate, status = "") {
  const hasBackendRoute = Boolean(candidate?.backendOnly && candidate?.backendId && candidate?.routeId);
  if (!hasBackendRoute) {
    return { enabled: false, reason: "missing_backend_route", title: "Backend route required" };
  }
  const routeType = routeTypeForCandidate(candidate);
  if (routeType !== "same_dex_sell") {
    return { enabled: false, reason: "route_type_not_supported", title: "same-chain DEX route required" };
  }
  const route = candidate?.selected_route || {};
  const routeStatus = String(route.route_status || status || "").toUpperCase();
  const safetyStatus = String(route.safety_status || candidate?.safetyStatus || "").toUpperCase();
  const blocked = routeStatus === "BLOCKED"
    || ["BLOCK", "ERROR"].includes(safetyStatus)
    || status === "차단"
    || routeBlockerList(route).length > 0;
  if (blocked) {
    return { enabled: false, reason: "blocked_route", title: "Route blocked" };
  }
  return { enabled: true, reason: "", title: `${DRY_RUN_ONLY_LABEL}: same-chain DEX only` };
}

function chainValue(market) {
  return String(market?.chain || market?.deposit_network || "unknown").toLowerCase();
}

function marketLabel(side, market) {
  const venue = market?.venue || market?.market || "UNKNOWN";
  return `${side} · ${venue}`;
}

function selectedRoute(card) {
  return card?.selected_route || {};
}

function backendStatus(card, snapshot) {
  const selectedRun = Number(snapshot?.selected_opportunity_id) === Number(card.id) ? snapshot?.selected_execution_run : null;
  return labelForStatus(selectedRun?.status || card.status || card.safety_status || "NEW");
}

function normalizeBackendOpportunity(card, snapshot) {
  const snapshotSelected = Number(snapshot?.selected_opportunity_id || 0) === Number(card.id || 0);
  const cardRoute = selectedRoute(card);
  const snapshotRoute = snapshotSelected ? (snapshot?.selected_route || {}) : {};
  const route = Object.keys(cardRoute).length ? cardRoute : snapshotRoute;
  const snapshotRouteId = snapshotSelected ? snapshot?.selected_route_id : 0;
  const routeId = Number(card.selected_route_id || route.id || snapshotRouteId || 0);
  const buy = card.buy || {};
  const sell = card.sell || {};
  const chain = chainValue(buy);
  const sellChain = sell.chain || sell.deposit_network || sell.quote_asset || "";
  const spread = formatBps(card.spread_bps);
  const edgeWorst = Number(route.edge_worst_bps ?? card.edge_worst_bps ?? 0);
  const boundary = snapshotSelected ? (snapshot?.live_full_boundary || {}) : {};
  return {
    id: String(card.id),
    backendId: Number(card.id),
    backendOnly: true,
    routeId,
    selected_route_id: routeId,
    symbol: card.symbol || "ASSET",
    chain,
    chainLabel: String(buy.chain || buy.deposit_network || "unknown"),
    route: routeLabel(route.route_type),
    routeType: route.route_type || "same_dex_sell",
    routeStatus: route.route_status || "",
    safetyStatus: route.safety_status || card.safety_status || "",
    approval_required: Boolean(route.approval_required),
    approval_status: route.approval_status || "MISSING",
    approval_id: route.approval_id || "",
    latest_approval: route.latest_approval || null,
    latest_approval_decision: route.latest_approval_decision || null,
    live_full_approval_required: Boolean(route.live_full_approval_required),
    live_full_approval_status: route.live_full_approval_status || "MISSING",
    live_full_approval_id: route.live_full_approval_id || "",
    live_full_latest_approval: route.live_full_latest_approval || null,
    live_full_latest_approval_decision: route.live_full_latest_approval_decision || null,
    live_full_boundary: boundary,
    blockers: snapshotSelected ? (snapshot?.blockers || []) : routeBlockerList(route),
    defaultStatus: backendStatus(card, snapshot),
    spread,
    spreadFloor: `edge ${formatBps(edgeWorst)}`,
    precheckPass: card.safety_status === "PASS" || route.safety_status === "PASS",
    title: `선택 후보: ${card.symbol || "ASSET"} / ${buy.chain || ""}`,
    routeSummary: `${card.symbol || "ASSET"} · ${buy.venue || buy.market || "BUY"} -> ${sell.venue || sell.market || "SELL"}`,
    initialStep: labelForStatus(snapshot?.current_step?.status || snapshot?.current_step_key || "PENDING"),
    currentAsset: `${card.symbol || "ASSET"} / ${buy.chain || sellChain || "unknown"}`,
    buy: {
      label: marketLabel("BUY", buy),
      chain: buy.chain || "-",
      token: buy.token_ca || "-",
      pool: buy.pool_ca || buy.market || "-",
      price: buy.quote_asset ? `${buy.quote_asset} quote` : "quote"
    },
    sell: {
      label: marketLabel("SELL", sell),
      chain: sellChain || "-",
      token: sell.token_ca || sell.market || "-",
      pool: sell.pool_ca || sell.deposit_network || sell.market || "-",
      price: sell.quote_asset ? `${sell.quote_asset} exit` : "exit"
    },
    selected_route: route
  };
}

function normalizeBackendOpportunities(snapshot) {
  return (snapshot?.opportunities || []).map((card) => normalizeBackendOpportunity(card, snapshot));
}

function eventMatchesSelected(row, snapshot) {
  if (!snapshot?.selected_opportunity_id || row.opportunity_id == null) return true;
  return Number(row.opportunity_id) === Number(snapshot.selected_opportunity_id);
}

function shouldReloadSnapshotForEvent(row) {
  return SNAPSHOT_RELOAD_EVENT_TYPES.has(row?.event_type);
}

function appendUniqueEventLog(logs, row) {
  const seq = Number(row?.seq || 0);
  if (!seq) return logs;
  if ((logs || []).some((item) => Number(item.seq || 0) === seq)) return logs;
  return [...(logs || []), row].slice(-140);
}

function approvalRecordFromEvent(row) {
  const payload = row?.payload || {};
  const eventType = String(row?.event_type || "");
  const status = eventType === "operator_approval.approved"
    ? "APPROVED"
    : (eventType === "operator_approval.rejected" ? "REJECTED" : (payload.status || "PENDING"));
  const approvalId = payload.approval_id || row?.approval_id || "";
  return {
    id: approvalId,
    approval_id: approvalId,
    approval_key: payload.approval_key || "",
    opportunity_id: row?.opportunity_id,
    route_id: row?.route_id,
    run_id: row?.run_id ?? null,
    mode: payload.mode || "one_click",
    requested_by: payload.requested_by || "",
    reason: payload.reason || "",
    status,
    approval_status: status,
    requested_at_ms: payload.requested_at_ms || row?.occurred_at_ms || null,
    decided_at_ms: payload.decided_at_ms || null,
    operator: payload.operator || "",
    payload: payload.evidence || {},
    decision_payload: payload.decision_payload || {}
  };
}

function approvalMetadata(approval) {
  if (!approval?.approval_id && !approval?.id) return null;
  return {
    approval_id: approval.approval_id || approval.id,
    approval_key: approval.approval_key || "",
    approval_status: approval.approval_status || approval.status,
    mode: approval.mode || "one_click",
    requested_by: approval.requested_by || "",
    reason: approval.reason || "",
    operator: approval.operator || "",
    requested_at_ms: approval.requested_at_ms || null,
    decided_at_ms: approval.decided_at_ms || null,
    decision_payload: approval.decision_payload || {}
  };
}

function approvalMatchesRoute(approval, opportunityId, routeId) {
  return Number(approval?.opportunity_id || 0) === Number(opportunityId || 0)
    && Number(approval?.route_id || 0) === Number(routeId || 0);
}

function upsertApproval(approvals, approval) {
  if (!approval?.id) return approvals || [];
  const exists = (approvals || []).some((row) => Number(row.id || row.approval_id || 0) === Number(approval.id));
  if (exists) {
    return (approvals || []).map((row) => (
      Number(row.id || row.approval_id || 0) === Number(approval.id) ? { ...row, ...approval } : row
    ));
  }
  return [...(approvals || []), approval];
}

function mergeApprovalIntoRoute(route, approval) {
  if (!route || !approval?.id || Number(route.id || 0) !== Number(approval.route_id || 0)) return route;
  const latestApproval = approvalMetadata(approval);
  if (String(approval.mode || "").toLowerCase() === "live_full") {
    const approvalPayload = approval.payload || {};
    return {
      ...route,
      live_full_approval_required: true,
      live_full_approval_status: approval.status,
      live_full_approval_id: approval.id,
      live_full_latest_approval: latestApproval,
      live_full_latest_approval_decision: approval.decided_at_ms ? latestApproval : route.live_full_latest_approval_decision || null,
      live_full_approval_amount_krw: approvalPayload.trade_amount_krw || route.live_full_approval_amount_krw,
      live_full_approval_expires_at_ms: approvalPayload.expires_at_ms || route.live_full_approval_expires_at_ms,
      live_full_approval_payload: approvalPayload
    };
  }
  return {
    ...route,
    approval_required: true,
    approval_status: approval.status,
    approval_id: approval.id,
    latest_approval: latestApproval,
    latest_approval_decision: approval.decided_at_ms ? latestApproval : route.latest_approval_decision || null
  };
}

function upsertApprovalAlert(alerts, row) {
  const payload = row?.payload || {};
  const alertId = payload.alert_id || row?.seq;
  if (!alertId) return alerts || [];
  const alert = {
    id: alertId,
    opportunity_id: row.opportunity_id,
    channel: "db_sse",
    status: "ACTIVE",
    payload,
    created_at_ms: row.occurred_at_ms
  };
  const exists = (alerts || []).some((item) => Number(item.id || 0) === Number(alertId));
  return exists
    ? (alerts || []).map((item) => (Number(item.id || 0) === Number(alertId) ? { ...item, ...alert } : item))
    : [...(alerts || []), alert].slice(-100);
}

function mergeApprovalFromEvent(snapshot, row) {
  const approval = approvalRecordFromEvent(row);
  let pendingApprovals = snapshot.pending_approvals || [];
  if (row.event_type === "operator_approval.requested") {
    pendingApprovals = upsertApproval(pendingApprovals, approval);
  }
  if (row.event_type === "operator_approval.approved" || row.event_type === "operator_approval.rejected") {
    pendingApprovals = pendingApprovals.filter((item) => Number(item.id || item.approval_id || 0) !== Number(approval.id));
  }
  const mergeCard = (card) => (
    Number(card.id || 0) === Number(row.opportunity_id || 0)
      ? { ...card, selected_route: mergeApprovalIntoRoute(card.selected_route, approval) }
      : card
  );
  return {
    ...snapshot,
    pending_approvals: pendingApprovals,
    alerts: row.event_type === "alert.operator_approval_requested" ? upsertApprovalAlert(snapshot.alerts, row) : snapshot.alerts,
    selected_route: mergeApprovalIntoRoute(snapshot.selected_route, approval),
    opportunities: (snapshot.opportunities || []).map(mergeCard)
  };
}

function pendingApprovalForRoute(snapshot, candidate) {
  const opportunityId = candidate?.backendId || snapshot?.selected_opportunity_id;
  const routeId = candidate?.routeId || snapshot?.selected_route_id;
  return (snapshot?.pending_approvals || []).find((approval) => approvalMatchesRoute(approval, opportunityId, routeId)) || null;
}

function approvalIdForCandidate(snapshot, candidate) {
  return candidate?.approval_id
    || candidate?.latest_approval?.approval_id
    || pendingApprovalForRoute(snapshot, candidate)?.id
    || "";
}

function liveFullApprovalIdForCandidate(candidate) {
  const route = candidate?.selected_route || {};
  return route.live_full_approval_id
    || route.live_full_latest_approval?.approval_id
    || route.live_full_latest_approval?.id
    || "";
}

function approvalStateForCandidate(snapshot, candidate) {
  const route = candidate?.selected_route || snapshot?.selected_route || {};
  const pendingApproval = pendingApprovalForRoute(snapshot, candidate);
  const latestApproval = route.latest_approval || pendingApproval || null;
  const latestDecision = route.latest_approval_decision || (latestApproval?.decided_at_ms ? latestApproval : null);
  const approvalId = route.approval_id || latestApproval?.approval_id || latestApproval?.id || "";
  const approvalRequired = Boolean(route.approval_required || candidate?.approval_required);
  const approvalStatus = String(
    route.approval_status
      || latestApproval?.approval_status
      || latestApproval?.status
      || (approvalRequired ? "MISSING" : "NOT_REQUIRED")
  ).toUpperCase();
  const selectedRun = snapshot?.selected_execution_run || null;
  const heldOneClick = selectedRun?.mode === "one_click" && selectedRun?.status === "EXEC_READY";
  return {
    approval_id: approvalId,
    approval_required: approvalRequired,
    approval_status: approvalStatus,
    label: approvalStatusText(approvalStatus, approvalRequired),
    latest_approval: latestApproval,
    latest_approval_decision: latestDecision,
    pending_count: (snapshot?.pending_approvals || []).length,
    held_one_click: heldOneClick,
    run_status: selectedRun?.status || "",
    run_approval_id: selectedRun?.payload?.approval?.approval_id || ""
  };
}

function upsertExecutionStepFromEvent(steps, row) {
  const payload = row?.payload || {};
  const stepKey = payload.step_key;
  if (!stepKey) return steps || [];
  const stepUpdate = {
    step_key: stepKey,
    status: payload.status || (row.event_type === "execution.step.reconcile" ? "RECONCILE" : "RUNNING"),
    run_id: row.run_id,
    route_id: row.route_id,
    external_ref: payload.tx_hash || payload.submit_ref || payload.bridge_ref || payload.deposit_ref || payload.order_ref || payload.external_ref || "",
    started_at_ms: payload.started_at_ms ?? null,
    completed_at_ms: payload.completed_at_ms ?? null,
    duration_ms: payload.duration_ms ?? null,
    error_code: payload.error_code || "",
    payload: payload
  };
  const current = steps || [];
  const exists = current.some((step) => step.step_key === stepKey);
  return exists
    ? current.map((step) => (step.step_key === stepKey ? { ...step, ...stepUpdate } : step))
    : [...current, stepUpdate];
}

function currentStepFromExecutionSteps(steps) {
  for (const status of ["RUNNING", "RECONCILE"]) {
    const step = (steps || []).find((row) => String(row.status) === status);
    if (step) return step;
  }
  const pending = (steps || []).find((row) => String(row.status) === "PENDING");
  return pending || (steps || [])[Math.max(0, (steps || []).length - 1)] || null;
}

function cleanVenueLabel(label) {
  return String(label || "-").replace(/^(BUY|SELL)\s*·\s*/i, "").trim();
}

function humanizeBackendDetail(text) {
  const normalized = String(text || "")
    .replace(/edge[_ ]component[_ ]stale:gas/gi, "가스/수수료 최신성 만료")
    .replace(/edge[_ ]component[_ ]stale:quote/gi, "견적 최신성 만료")
    .replace(/edge[_ ]component[_ ]stale:orderbook/gi, "오더북 최신성 만료")
    .replace(/execution[_ ]gate[_ ]pending/gi, "실행 게이트 대기")
    .replace(/demo[_ ]same[_ ]dex[_ ]spread/gi, "동일 체인 DEX 스프레드 감지")
    .replace(/route[_ ]type[_ ]not[_ ]supported/gi, "지원하지 않는 route")
    .replace(/operator[_ ]approval[_ ]required/gi, "운영 승인 필요")
    .replace(/safety[_ ]state[_ ]not[_ ]pass/gi, "안전 상태 미통과")
    .replace(/edge[_ ]worst[_ ]unverified/gi, "최악 수익률 검증 전")
    .replace(/\bBLOCK\b/g, "차단")
    .replace(/\bRUNNING\b/g, "진행중")
    .replace(/\bCOMPLETED\b/g, "완료")
    .replace(/\bSETTLED\b/g, "정산 완료")
    .replace(/\bPENDING\b/g, "대기")
    .replace(/\bRECONCILE\b/g, "재확인")
    .replace(/_/g, " ");
  return normalized;
}

function flowNodeDisplayState(node) {
  const status = String(node.status || "").toUpperCase();
  if (node.id === "signal") return "done";
  if (["BLOCK", "BLOCKED", "ERROR", "FAILED"].includes(status)) return "blocked";
  if (["WARN", "WARNING"].includes(status)) return "warn";
  return normalizeState(node.state);
}

function flowNodeBadge(node) {
  const status = String(node.status || "").toUpperCase();
  if (node.id === "signal") return "완료";
  if (["BLOCK", "BLOCKED"].includes(status)) return "차단";
  if (["ERROR", "FAILED"].includes(status)) return "실패";
  if (["WARN", "WARNING"].includes(status)) return "확인";
  return labelForStatus(node.status, node.state);
}

function flowNodeDetail(node, snapshot, candidate) {
  const buyVenue = cleanVenueLabel(candidate?.buy?.label);
  const sellVenue = cleanVenueLabel(candidate?.sell?.label);
  const buyChain = candidate?.buy?.chain || candidate?.chainLabel || candidate?.chain || "-";
  const sellChain = candidate?.sell?.chain || "-";
  const buyPool = candidate?.buy?.pool ? shortAddress(candidate.buy.pool) : "-";
  const sellPool = candidate?.sell?.pool ? shortAddress(candidate.sell.pool) : "-";
  const rawDetail = humanizeBackendDetail(node.detail || node.status || node.state || "");
  const route = node.route_id ? `route ${node.route_id}` : "";
  const run = node.run_id ? `run ${node.run_id}` : "";
  const duration = node.duration_ms != null ? formatElapsed(node.duration_ms) : "";
  const refs = (node.external_refs || []).map((ref) => `ref ${ref}`);
  const suffix = [rawDetail, route, run, duration, ...refs]
    .filter(Boolean)
    .join(" / ");

  if (node.id === "signal") {
    return [
      `감지 출처: ${buyVenue} ${buyChain} pool ${buyPool}`,
      `비교 대상: ${sellVenue} ${sellChain}`,
      candidate?.spread ? `spread ${candidate.spread}` : "",
      suffix
    ].filter(Boolean).join(" / ");
  }

  if (node.id === "precheck") {
    const blockers = routeBlockerList(snapshot?.selected_route || candidate?.selected_route || {})
      .map(humanizeBackendDetail)
      .slice(0, 2);
    const result = blockers.length ? `차단 사유: ${blockers.join(" · ")}` : "sell quote / liquidity / freshness 확인";
    return [`프리체크: ${result}`, suffix].filter(Boolean).join(" / ");
  }

  if (node.id === "dexBuy") {
    return [`매수: ${buyVenue} ${buyChain} pool ${buyPool}`, suffix].filter(Boolean).join(" / ");
  }

  if (node.id === "bridge") {
    return [`브릿지: ${buyChain} -> ${sellChain}`, suffix].filter(Boolean).join(" / ");
  }

  if (node.id === "sameChainSell") {
    return [`동일체인 매도: ${buyChain} -> ${buyChain} / ${sellVenue} pool ${sellPool}`, suffix].filter(Boolean).join(" / ");
  }

  if (node.id === "walletHold") {
    const wallet = candidate?.wallet_address || snapshot?.selected_execution_run?.wallet_address || "0x7777777777777777777777777777777777777777";
    return [`지갑보유: ${wallet} / 매수 후 정지 / 지갑 주소`, suffix].filter(Boolean).join(" / ");
  }

  if (node.id === "crossChainSell") {
    return [`타체인 매도: ${sellChain} DEX pool ${sellPool}`, suffix].filter(Boolean).join(" / ");
  }

  if (node.id === "cexDeposit") {
    return [`CEX 입금: ${sellVenue} / ${sellChain} network / 입금 거래소`, suffix].filter(Boolean).join(" / ");
  }

  if (node.id === "cexSell") {
    return [`매도: ${sellVenue} ${sellChain}`, suffix].filter(Boolean).join(" / ");
  }

  return suffix || "상태 대기";
}

function upsertDryRunTransactionFromStepEvent(transactions, row) {
  const payload = row?.payload || {};
  if (!payload.dry_run || (!payload.tx_hash && !payload.submit_ref)) return transactions || [];
  const stepKey = payload.step_key || "";
  const txHash = payload.tx_hash || payload.submit_ref;
  const adapterStatus = String(payload.adapter_status || "").toUpperCase();
  const dryRunStatus = adapterStatus
    ? `DRY_RUN_${adapterStatus}`
    : (payload.status === "COMPLETED" ? "DRY_RUN_SUCCESS" : `DRY_RUN_${String(payload.status || "UNKNOWN").toUpperCase()}`);
  const txUpdate = {
    id: `event-${row.seq}`,
    run_id: row.run_id,
    route_id: row.route_id,
    tx_hash: txHash,
    status: dryRunStatus,
    payload: {
      mode: payload.mode || "auto_small",
      dry_run: true,
      dry_run_only: true,
      step_key: stepKey,
      submit_ref: payload.submit_ref || "",
      tx_hash: payload.tx_hash || "",
      adapter_name: payload.adapter_name || ""
    }
  };
  const current = transactions || [];
  const exists = current.some((transaction) => String(transaction.tx_hash || transaction.payload?.tx_hash || transaction.payload?.submit_ref) === String(txHash));
  return exists
    ? current.map((transaction) => (
      String(transaction.tx_hash || transaction.payload?.tx_hash || transaction.payload?.submit_ref) === String(txHash)
        ? { ...transaction, ...txUpdate, payload: { ...(transaction.payload || {}), ...txUpdate.payload } }
        : transaction
    ))
    : [...current, txUpdate];
}

function upsertTransferFromEvent(transfers, row) {
  const payload = row?.payload || {};
  if (!payload.transfer_id) return transfers || [];
  const update = {
    id: payload.transfer_id,
    transfer_key: payload.transfer_key || "",
    run_id: row.run_id,
    route_id: row.route_id,
    status: payload.status || "UNKNOWN",
    from_location: payload.from_location || "",
    to_location: payload.to_location || "",
    payload: {
      ...(payload || {}),
      mode: payload.mode || "live_full",
      simulated: Boolean(payload.simulated)
    }
  };
  const current = transfers || [];
  const exists = current.some((transfer) => Number(transfer.id) === Number(payload.transfer_id));
  return exists
    ? current.map((transfer) => (
      Number(transfer.id) === Number(payload.transfer_id)
        ? { ...transfer, ...update, payload: { ...(transfer.payload || {}), ...update.payload } }
        : transfer
    ))
    : [...current, update];
}

function upsertOrderFromEvent(orders, row) {
  const payload = row?.payload || {};
  if (!payload.order_id) return orders || [];
  const update = {
    id: payload.order_id,
    order_key: payload.order_key || "",
    run_id: row.run_id,
    route_id: row.route_id,
    status: payload.status || "UNKNOWN",
    venue_code: payload.venue_code || "",
    market_key: payload.market_key || "",
    side: payload.side || "SELL",
    external_order_id: payload.external_order_id || payload.order_ref || "",
    payload: {
      ...(payload || {}),
      mode: payload.mode || "live_full",
      simulated: Boolean(payload.simulated),
      cex_withdrawal_enabled: false
    }
  };
  const current = orders || [];
  const exists = current.some((order) => Number(order.id) === Number(payload.order_id));
  return exists
    ? current.map((order) => (
      Number(order.id) === Number(payload.order_id)
        ? { ...order, ...update, payload: { ...(order.payload || {}), ...update.payload } }
        : order
    ))
    : [...current, update];
}

function runStatusFromEventPayload(payload) {
  const status = String(payload?.run_status || payload?.status || "").toUpperCase();
  return RUN_STATUS_VALUES.has(status) ? status : "";
}

function updateSelectedRunFromEvent(run, row) {
  if (!run || Number(run.id || 0) !== Number(row?.run_id || 0)) return run;
  const payload = row.payload || {};
  if (row.event_type === "execution.step.reconcile") {
    return { ...run, status: "MANUAL_REVIEW", error_code: payload.error_code || run.error_code || "" };
  }
  const runStatus = runStatusFromEventPayload(payload);
  if ((row.event_type === "execution.log.append" || row.event_type === "error") && runStatus) {
    return { ...run, status: runStatus, error_code: payload.error_code || run.error_code || "" };
  }
  if (row.event_type === "error" && payload.error_code) {
    return { ...run, error_code: payload.error_code };
  }
  return run;
}

function applySseEventToSnapshot(current, row) {
  if (!current || !row || !eventMatchesSelected(row, current)) return current;
  const payload = row.payload || {};
  const nextSeq = Math.max(Number(current.snapshot_seq || 0), Number(row.seq || 0));
  const occurredAtMs = Number(row.occurred_at_ms || row.occurred_at || 0);
  const next = {
    ...current,
    snapshot_seq: nextSeq,
    server_time: Number.isFinite(occurredAtMs) && occurredAtMs > 0
      ? Math.max(Number(current.server_time || 0), occurredAtMs)
      : current.server_time,
    logs: appendUniqueEventLog(current.logs, row),
    selected_execution_run: updateSelectedRunFromEvent(current.selected_execution_run, row)
  };

  if (String(row.event_type || "").startsWith("execution.step.")) {
    next.execution_steps = upsertExecutionStepFromEvent(current.execution_steps, row);
    next.current_step = currentStepFromExecutionSteps(next.execution_steps);
    next.current_step_key = next.current_step?.step_key || null;
    next.transactions = upsertDryRunTransactionFromStepEvent(current.transactions, row);
  }

  if (row.event_type === "flow.node.update" && payload.node_id) {
    const nodeExists = (current.flow_nodes || []).some((node) => node.id === payload.node_id);
    const nodeUpdate = {
      id: payload.node_id,
      state: normalizeState(payload.state),
      status: payload.status,
      detail: payload.step_key || payload.status,
      route_id: row.route_id,
      run_id: row.run_id,
      duration_ms: payload.duration_ms ?? null,
      started_at_ms: payload.started_at_ms ?? null,
      completed_at_ms: payload.completed_at_ms ?? null,
      step_keys: payload.step_key ? [payload.step_key] : [],
      external_refs: uniqueStrings([
        payload.external_ref,
        payload.tx_hash,
        payload.submit_ref,
        payload.bridge_ref,
        payload.deposit_ref,
        payload.order_ref
      ])
    };
    next.flow_nodes = nodeExists
      ? current.flow_nodes.map((node) => (node.id === payload.node_id ? { ...node, ...nodeUpdate } : node))
      : [...(current.flow_nodes || []), nodeUpdate];
  }

  if (row.event_type === "flow.edge.update" && payload.edge_id) {
    const endpoint = routeEdges.find((edge) => edge.id === payload.edge_id) || {};
    const edgeExists = (current.flow_edges || []).some((edge) => edge.id === payload.edge_id);
    const edgeUpdate = {
      id: payload.edge_id,
      source: endpoint.source,
      target: endpoint.target,
      state: normalizeState(payload.state),
      status: payload.status,
      route_id: row.route_id,
      run_id: row.run_id,
      duration_ms: payload.duration_ms ?? null,
      started_at_ms: payload.started_at_ms ?? null,
      completed_at_ms: payload.completed_at_ms ?? null,
      step_keys: payload.step_key ? [payload.step_key] : [],
      external_refs: uniqueStrings([
        payload.external_ref,
        payload.tx_hash,
        payload.submit_ref,
        payload.bridge_ref,
        payload.deposit_ref,
        payload.order_ref
      ])
    };
    next.flow_edges = edgeExists
      ? current.flow_edges.map((edge) => (edge.id === payload.edge_id ? { ...edge, ...edgeUpdate } : edge))
      : [...(current.flow_edges || []), edgeUpdate];
  }

  if (row.event_type === "position.update" && payload.position_id) {
    const positionUpdate = {
      id: payload.position_id,
      run_id: row.run_id,
      opportunity_id: row.opportunity_id,
      status: payload.status || "OPEN",
      payload: {
        mode: payload.mode || "auto_small",
        dry_run: Boolean(payload.dry_run),
        not_live_trading: Boolean(payload.not_live_trading),
        current_status: payload.status || "OPEN"
      }
    };
    const exists = (current.positions || []).some((position) => Number(position.id) === Number(payload.position_id));
    next.positions = exists
      ? (current.positions || []).map((position) => (
        Number(position.id) === Number(payload.position_id)
          ? { ...position, status: payload.status || position.status, payload: { ...(position.payload || {}), ...positionUpdate.payload } }
          : position
      ))
      : [...(current.positions || []), positionUpdate];
  }

  if (row.event_type === "transfer.update") {
    next.transfers = upsertTransferFromEvent(current.transfers, row);
  }

  if (row.event_type === "order.update") {
    next.orders = upsertOrderFromEvent(current.orders, row);
  }

  if (row.event_type === "provider.health" && payload.provider_key) {
    const existing = (current.provider_health || []).some((provider) => provider.provider_key === payload.provider_key);
    const providerUpdate = {
      provider_key: payload.provider_key,
      status: payload.status || "UNKNOWN",
      payload
    };
    next.provider_health = existing
      ? current.provider_health.map((provider) => (
        provider.provider_key === payload.provider_key ? { ...provider, ...providerUpdate } : provider
      ))
      : [...(current.provider_health || []), providerUpdate];
  }

  if (APPROVAL_EVENT_TYPES.has(row.event_type)) {
    return mergeApprovalFromEvent(next, row);
  }

  return next;
}

function backendNodeStateMap(snapshot, candidate) {
  const base = Object.fromEntries(flowCanvasNodes.map((node) => [
    node.id,
    {
      state: "skipped",
      badge: "미사용",
      detail: "미사용 분기"
    }
  ]));
  for (const node of (snapshot?.flow_nodes || [])) {
    const mappedNodeIds = legacyFlowNodeAliases[node.id] || [node.id];
    for (const mappedNodeId of mappedNodeIds) {
      if (!base[mappedNodeId]) continue;
      const normalizedNode = { ...node, id: mappedNodeId };
      const precheckBlocked = mappedNodeId === "precheck" && routeBlockerList(snapshot?.selected_route || candidate?.selected_route || {}).length > 0;
      base[mappedNodeId] = {
        state: precheckBlocked ? "blocked" : flowNodeDisplayState(normalizedNode),
        badge: precheckBlocked ? "차단" : flowNodeBadge(normalizedNode),
        detail: flowNodeDetail(normalizedNode, snapshot, candidate)
      };
    }
  }
  return base;
}

function backendEdgeStateMap(snapshot) {
  const base = Object.fromEntries(routeEdges.map((edge) => [
    edge.id,
    {
      ...edge,
      state: "skipped",
      status: "미사용"
    }
  ]));
  for (const edge of (snapshot?.flow_edges || [])) {
    const mappedEdgeIds = legacyFlowEdgeAliases[edge.id] || [edge.id];
    for (const mappedEdgeId of mappedEdgeIds) {
      if (!base[mappedEdgeId]) continue;
      base[mappedEdgeId] = {
        ...edge,
        id: mappedEdgeId,
        state: normalizeState(edge.state)
      };
    }
  }
  return base;
}

function emptyNodeStateMap() {
  return Object.fromEntries(flowCanvasNodes.map((node) => [
    node.id,
    {
      state: "wait",
      badge: "대기",
      detail: "backend snapshot 대기"
    }
  ]));
}

function normalizeLogRows(eventLogs, selectedCandidate) {
  return [...(eventLogs || [])]
    .sort((a, b) => Number(b.seq || 0) - Number(a.seq || 0))
    .slice(0, 80)
    .map((row) => {
      if (Array.isArray(row)) return row;
      const payload = row.payload || {};
      const step = stepLabels[payload.step_key] || payload.step_key || row.event_type || "event";
      const status = payload.message || payload.status || row.severity || "";
      return [
        formatUtcTime(row.occurred_at_ms),
        selectedCandidate.symbol,
        step,
        selectedCandidate.buy.label,
        selectedCandidate.sell.label,
        selectedCandidate.spread,
        status
      ];
    });
}

function normalizePositionRows(positions, selectedCandidate, fallbackSummary, allowFallback = true) {
  if (!positions?.length) {
    if (!allowFallback) return [];
    return [{
      id: "fallback-position",
      symbol: selectedCandidate.symbol,
      qty: "-",
      avgBuy: selectedCandidate.buy.price,
      exit: selectedCandidate.sell.price,
      status: fallbackSummary
    }];
  }
  return positions.map((position) => ({
    id: position.id,
    symbol: selectedCandidate.symbol,
    qty: position.qty_raw || "-",
    avgBuy: formatKrw(position.avg_buy_price_krw),
    exit: formatKrw(position.payload?.live_exit_estimate_krw || position.latest_mark?.mark_price_krw),
    status: position.status || position.payload?.current_status || "-"
  }));
}

function normalizeTransactionRows(transactions) {
  return (transactions || []).map((transaction) => {
    const payload = transaction.payload || {};
    const evidence = payload.payload_evidence || {};
    const stepKey = payload.step_key || evidence.step_key || "-";
    const txRef = payload.tx_hash || transaction.tx_hash || evidence.tx_hash || payload.submit_ref || evidence.submit_ref || "-";
    return {
      id: transaction.id || txRef,
      step: stepLabels[stepKey] || stepKey,
      status: transaction.status || payload.status || "-",
      adapter: payload.adapter_name || evidence.adapter_name || "-",
      ref: txRef,
      submitRef: payload.submit_ref || evidence.submit_ref || "-",
      gas: formatKrw(payload.gas_krw || payload.quote_evidence?.gas_krw || 0),
      fee: formatKrw(payload.fee_krw || payload.quote_evidence?.fee_krw || 0),
      dryRun: Boolean(payload.dry_run || evidence.dry_run)
    };
  });
}

function normalizeTransferRows(transfers) {
  return (transfers || []).map((transfer) => {
    const payload = transfer.payload || {};
    const stepKey = payload.step_key || "-";
    const ref = payload.bridge_ref || payload.deposit_ref || payload.submit_ref || payload.external_ref || transfer.transfer_key || "-";
    return {
      id: transfer.id || ref,
      step: stepLabels[stepKey] || stepKey,
      status: transfer.status || payload.status || "-",
      from: transfer.from_location || payload.source_chain || "-",
      to: transfer.to_location || payload.destination_chain || payload.destination_venue || "-",
      ref,
      adapter: payload.adapter_name || "-",
      simulated: Boolean(payload.simulated),
      cexWithdrawal: Boolean(payload.cex_withdrawal_enabled)
    };
  });
}

function normalizeOrderRows(orders) {
  return (orders || []).map((order) => {
    const payload = order.payload || {};
    const stepKey = payload.step_key || "-";
    const ref = payload.order_ref || order.external_order_id || payload.external_ref || order.order_key || "-";
    return {
      id: order.id || ref,
      step: stepLabels[stepKey] || stepKey,
      status: order.status || payload.status || "-",
      venue: order.venue_code || payload.destination_venue || "-",
      market: order.market_key || payload.cex_market || "-",
      side: order.side || "SELL",
      ref,
      adapter: payload.adapter_name || "-",
      simulated: Boolean(payload.simulated),
      cexWithdrawal: Boolean(payload.cex_withdrawal_enabled)
    };
  });
}

function normalizeProviderJobRows(providerJobs, providerHealth) {
  const healthByProvider = Object.fromEntries((providerHealth || []).map((row) => [row.provider_key, row]));
  return (providerJobs || []).map((job) => {
    const health = healthByProvider[job.provider_key] || {};
    return {
      id: `${job.provider_key || "provider"}:${job.capability || job.scope_key || "job"}`,
      provider: job.display_name || job.provider_key || "-",
      capability: job.capability || "-",
      scope: job.scope_key || "-",
      status: job.status || health.status || (job.enabled === false ? "DISABLED" : "ENABLED"),
      error: job.error_code || health.error_code || job.reason || "",
      latency: job.latency_ms ?? health.latency_ms ?? null
    };
  });
}

const API_STATUS_SOURCES = [
  { key: "dexscreener", label: "DEX Screener", capability: "DEX pool", aliases: ["dexscreener", "dex-screener"] },
  { key: "geckoterminal", label: "GeckoTerminal", capability: "DEX pool", aliases: ["geckoterminal", "gecko-terminal"] },
  { key: "binance", label: "Binance", capability: "CEX orderbook", aliases: ["binance"] },
  { key: "okx", label: "OKX", capability: "CEX orderbook", aliases: ["okx"] },
  { key: "bybit", label: "Bybit", capability: "CEX orderbook", aliases: ["bybit"] },
  { key: "upbit", label: "Upbit", capability: "KRW orderbook", aliases: ["upbit"] },
  { key: "bithumb", label: "Bithumb", capability: "KRW orderbook", aliases: ["bithumb"] },
  { key: "alchemy", label: "Alchemy RPC", capability: "RPC freshness", aliases: ["alchemy"] }
];

function apiStatusTone(status) {
  const normalized = String(status || "").toUpperCase();
  if (["ACTIVE", "OK", "ENABLED", "SUCCESS", "COMPLETED", "PASS"].includes(normalized)) return "done";
  if (["DEGRADED", "WARN", "WARNING", "RETRYING", "STALE"].includes(normalized)) return "warn";
  if (["FAILED", "ERROR", "BLOCKED", "TIMEOUT", "DISABLED"].includes(normalized)) return "off";
  return "wait";
}

function apiStatusLabel(status) {
  const normalized = String(status || "").toUpperCase();
  if (["ACTIVE", "OK", "ENABLED", "SUCCESS", "COMPLETED", "PASS"].includes(normalized)) return "정상";
  if (["DEGRADED", "WARN", "WARNING", "RETRYING", "STALE"].includes(normalized)) return "지연";
  if (normalized === "DISABLED") return "비활성";
  if (["FAILED", "ERROR", "BLOCKED", "TIMEOUT"].includes(normalized)) return "비정상";
  return "대기";
}

function apiRowMatchesSource(row, source) {
  const haystack = `${row.provider || ""} ${row.id || ""} ${row.scope || ""}`.toLowerCase();
  return source.aliases.some((alias) => haystack.includes(alias));
}

function defaultApiStatusRows(providerHealth, providerJobRows) {
  const healthRows = (providerHealth || []).map((row) => ({
    id: row.provider_key || row.id || "",
    provider: row.provider_key || row.id || "",
    capability: row.capability || "health",
    scope: row.provider_key || "-",
    status: row.status || "WAIT",
    error: row.error_code || "",
    latency: row.latency_ms ?? null
  }));
  const allRows = [...(providerJobRows || []), ...healthRows];
  const matchedIds = new Set();
  const sourceRows = API_STATUS_SOURCES.map((source) => {
    const match = allRows.find((row) => apiRowMatchesSource(row, source));
    if (match) matchedIds.add(match.id);
    const status = match?.status || "WAIT";
    const latencyText = Number.isFinite(Number(match?.latency)) ? `${Math.round(Number(match.latency))}ms` : "";
    const detail = match?.error || latencyText || "관측 대기";
    return {
      id: source.key,
      label: source.label,
      capability: match?.capability || source.capability,
      source: match?.scope || "read-only",
      status,
      detail
    };
  });
  const customRows = (providerJobRows || [])
    .filter((row) => !matchedIds.has(row.id) && !API_STATUS_SOURCES.some((source) => apiRowMatchesSource(row, source)))
    .map((row) => ({
      id: row.id,
      label: row.provider,
      capability: row.capability,
      source: row.scope,
      status: row.status,
      detail: row.error || (Number.isFinite(Number(row.latency)) ? `${Math.round(Number(row.latency))}ms` : "관측 대기")
    }));
  return [...sourceRows, ...customRows];
}

function finiteNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function calculateTotalPnlSummary(snapshot, simulationRuns, usingFallbackData) {
  let total = 0;
  let sourceCount = 0;
  for (const position of (snapshot?.positions || [])) {
    const realized = finiteNumber(position.realized_pnl_krw);
    const latestUnrealized = finiteNumber(position.latest_mark?.unrealized_pnl_krw);
    const status = String(position.status || "").toUpperCase();
    if (["SETTLED", "CLOSED"].includes(status) && realized !== null) {
      total += realized;
      sourceCount += 1;
      continue;
    }
    let positionPnl = 0;
    let hasPositionPnl = false;
    if (realized !== null) {
      positionPnl += realized;
      hasPositionPnl = true;
    }
    if (latestUnrealized !== null) {
      positionPnl += latestUnrealized;
      hasPositionPnl = true;
    }
    if (hasPositionPnl) {
      total += positionPnl;
      sourceCount += 1;
    }
  }

  if (!sourceCount) {
    for (const run of (simulationRuns || [])) {
      const pnl = run.payload?.simulated_pnl || {};
      const value = finiteNumber(pnl.realized_pnl_krw ?? pnl.net_krw ?? pnl.gross_krw);
      if (value !== null) {
        total += value;
        sourceCount += 1;
      }
    }
  }

  const tone = sourceCount === 0 ? "wait" : (total > 0 ? "positive" : (total < 0 ? "negative" : "flat"));
  const detail = sourceCount
    ? `${sourceCount}개 포지션/시뮬레이션 합산`
    : (usingFallbackData ? "오프라인 데모 P&L 미계산" : "실현/미실현 P&L 대기");
  return {
    value: sourceCount ? formatSignedKrw(total) : "₩0",
    detail,
    tone
  };
}

function candidateSpreadPercent(candidate) {
  const raw = String(candidate?.spread || "0").replace("%", "").replace("+", "").trim();
  const numeric = Number(raw);
  return Number.isFinite(numeric) ? numeric : 0;
}

function candidateEdgeLabel(candidate) {
  return candidate?.spreadFloor || candidate?.selected_route?.edge_worst_label || "edge 대기";
}

function buildTradePnlCards(snapshot, simulationRuns, usingFallbackData) {
  const positionCards = (snapshot?.positions || []).map((position, index) => {
    const symbol = position.symbol || position.asset_symbol || `POS-${index + 1}`;
    const realized = finiteNumber(position.realized_pnl_krw);
    const unrealized = finiteNumber(position.latest_mark?.unrealized_pnl_krw);
    const value = (realized || 0) + (unrealized || 0);
    return {
      id: `position-${position.id || index}`,
      label: symbol,
      value: formatSignedKrw(value),
      detail: position.status || "포지션 추적",
      tone: value > 0 ? "positive" : (value < 0 ? "negative" : "flat")
    };
  });

  const simulationCards = (simulationRuns || []).map((run, index) => {
    const pnl = run.payload?.simulated_pnl || {};
    const value = finiteNumber(pnl.realized_pnl_krw ?? pnl.net_krw ?? pnl.gross_krw) || 0;
    return {
      id: `simulation-${run.id || index}`,
      label: `거래 ${run.id || index + 1}`,
      value: formatSignedKrw(value),
      detail: run.status || "시뮬레이션",
      tone: value > 0 ? "positive" : (value < 0 ? "negative" : "flat")
    };
  });

  const cards = [...positionCards, ...simulationCards].slice(0, 4);
  if (cards.length) return cards;
  return [{
    id: "pnl-waiting",
    label: usingFallbackData ? "OFFLINE DEMO" : "거래 대기",
    value: "₩0",
    detail: usingFallbackData ? "실제 P&L 제외" : "실행 후 표시",
    tone: "wait"
  }];
}

function executionApiRowsFor(apiStatusRows, selectedCandidate) {
  const route = routeTypeForCandidate(selectedCandidate);
  const prioritized = apiStatusRows.filter((api) => {
    const text = `${api.label} ${api.capability} ${api.source}`.toLowerCase();
    if (route.includes("cex") && /(binance|okx|bybit|upbit|bithumb|orderbook|krw|cex)/.test(text)) return true;
    if (route.includes("bridge") && /(bridge|rpc|alchemy|dex|pool)/.test(text)) return true;
    return /(dex|pool|rpc|alchemy|fx|krw|orderbook)/.test(text);
  });
  const rows = prioritized.length ? prioritized : apiStatusRows;
  return rows.slice(0, 6);
}

function normalizeSimulationRunRows(simulationRuns) {
  return (simulationRuns || []).map((run) => {
    const payload = run.payload || {};
    const blockers = Array.isArray(payload.blockers) ? payload.blockers : [];
    return {
      id: run.id,
      status: run.status || "-",
      opportunityId: run.opportunity_id || "-",
      routeId: run.route_id || "-",
      executionRunId: run.execution_run_id || "-",
      errorCode: run.error_code || payload.error_code || "",
      blockers: blockers.join(" · ") || "-",
      pnl: formatKrw(payload.simulated_pnl?.net_krw || payload.simulated_pnl?.gross_krw || 0)
    };
  });
}

function resultBlockers(result) {
  return uniqueStrings([
    ...(Array.isArray(result?.blockers) ? result.blockers : []),
    ...(Array.isArray(result?.run?.blockers) ? result.run.blockers : []),
    ...(Array.isArray(result?.run?.payload?.blockers) ? result.run.payload.blockers : []),
    ...(Array.isArray(result?.simulation_run?.payload?.blockers) ? result.simulation_run.payload.blockers : [])
  ]);
}

function actionFailureMessage(prefix, result) {
  const errorCode = result?.error_code || result?.run?.error_code || result?.simulation_run?.error_code || result?.error || "missing_error_code";
  const blockers = resultBlockers(result);
  return `${prefix}: ${errorCode} · blockers ${blockers.length ? blockers.join(",") : "none"}`;
}

function flowIntelSummary(selectedCandidate, selectedApproval, selectedRouteBlockers) {
  const route = selectedCandidate?.selected_route || {};
  const payload = routePayload(route);
  const buyVenue = cleanVenueLabel(selectedCandidate?.buy?.label);
  const sellVenue = cleanVenueLabel(selectedCandidate?.sell?.label);
  const blocker = selectedRouteBlockers?.[0] ? humanizeBackendDetail(selectedRouteBlockers[0]) : "차단 없음";
  return [
    {
      label: "감지 DEX",
      value: buyVenue,
      detail: selectedCandidate?.buy?.chain || selectedCandidate?.chainLabel || "-",
      tone: "dex-buy"
    },
    {
      label: "목표 매도",
      value: sellVenue,
      detail: selectedCandidate?.sell?.chain || payload.cex_market || "-",
      tone: venueTone(sellVenue, "sell")
    },
    {
      label: "Spread",
      value: selectedCandidate?.spread || "+0.00%",
      detail: selectedCandidate?.spreadFloor || "edge 대기",
      tone: "spread"
    },
    {
      label: "Gate",
      value: selectedApproval?.label || "승인 상태 대기",
      detail: blocker,
      tone: selectedRouteBlockers?.length ? "risk" : "neutral"
    }
  ];
}

function candidateDisplayStatus(candidate, candidateStatuses) {
  const localStatus = candidateStatuses[candidate.id];
  if (candidate.backendOnly) {
    return BACKEND_PENDING_STATUSES.has(localStatus) ? localStatus : candidate.defaultStatus;
  }
  return localStatus || candidate.defaultStatus;
}

function uniqueOptionValues(candidates, key, defaults) {
  const values = new Set(defaults);
  candidates.forEach((candidate) => {
    if (candidate[key]) values.add(candidate[key]);
  });
  return [...values];
}

function App() {
  const fallbackCandidates = useMemo(() => fallbackOpportunityList(), []);
  const fallbackInitialCandidate = fallbackCandidates[0];
  const [apiState, setApiState] = useState("loading");
  const [snapshot, setSnapshot] = useState(null);
  const [lastSeq, setLastSeq] = useState(0);
  const [selectedCandidateId, setSelectedCandidateId] = useState("sol-polygon-dex");
  const [nodeStates, setNodeStates] = useState(() => initialNodeState(opportunities["sol-polygon-dex"]));
  const [edgeStates, setEdgeStates] = useState(() => ({ ...opportunities["sol-polygon-dex"].initialEdges }));
  const [localFlowOverride, setLocalFlowOverride] = useState(false);
  const [candidateStatuses, setCandidateStatuses] = useState(() => (
    Object.fromEntries(Object.entries(opportunities).map(([id, candidate]) => [id, candidate.defaultStatus]))
  ));
  const [filters, setFilters] = useState({ route: "", chain: "", status: "" });
  const [candidatePage, setCandidatePage] = useState(1);
  const [spikeRules, setSpikeRules] = useState({
    real: { threshold: "30", saved: false, pair: "" },
    simulation: { threshold: "30", saved: false, pair: "" }
  });
  const [apiStatusTab, setApiStatusTab] = useState("execution");
  const [bottomTab, setBottomTab] = useState("logs");
  const [selectedStep, setSelectedStep] = useState(fallbackInitialCandidate.initialStep);
  const [simulationState, setSimulationState] = useState("대기 중");
  const [positionSummary, setPositionSummary] = useState("매수 체결 전");
  const [positionCount, setPositionCount] = useState(0);
  const [logs, setLogs] = useState(initialLogs);
  const [finishedLineDurations, setFinishedLineDurations] = useState({});
  const [tick, setTick] = useState(0);
  const executionTimers = useRef([]);
  const lineStartedAt = useRef({});
  const lineTimerIntervals = useRef({});
  const eventSourceRef = useRef(null);
  const lastAppliedSeq = useRef(0);

  const backendCandidates = useMemo(() => normalizeBackendOpportunities(snapshot), [snapshot]);
  const usingBackendSnapshot = backendCandidates.length > 0;
  const usingFallbackData = FRONTEND_DEMO_FALLBACK_ONLY && !usingBackendSnapshot && apiState === "fallback";
  const usingLocalFlowState = localFlowOverride || usingFallbackData;
  const opportunityList = usingBackendSnapshot ? buildTestCandidateFillers(backendCandidates, fallbackCandidates) : (usingFallbackData ? fallbackCandidates : []);
  const candidateMap = useMemo(
    () => Object.fromEntries(opportunityList.map((candidate) => [candidate.id, candidate])),
    [opportunityList]
  );
  const selectedCandidate = candidateMap[selectedCandidateId] || opportunityList[0] || (usingFallbackData ? fallbackInitialCandidate : EMPTY_MONITOR_CANDIDATE);
  const backendNodeStates = useMemo(() => backendNodeStateMap(snapshot, selectedCandidate), [snapshot, selectedCandidate]);
  const backendEdgeStates = useMemo(() => backendEdgeStateMap(snapshot), [snapshot]);
  const waitingNodeStates = useMemo(() => emptyNodeStateMap(), []);
  const activeNodeStates = usingLocalFlowState ? nodeStates : (usingBackendSnapshot ? backendNodeStates : waitingNodeStates);
  const providerHealth = snapshot?.provider_health || [];
  const providerJobRows = normalizeProviderJobRows(snapshot?.provider_jobs, providerHealth);
  const apiStatusRows = defaultApiStatusRows(providerHealth, providerJobRows);
  const simulationRunRows = normalizeSimulationRunRows(snapshot?.simulation_runs);
  const apiHealthText = "API별 상태";
  const apiStateLabel = {
    connected: "정상",
    reconnecting: "재연결",
    loading: "로딩",
    empty: "후보 없음",
    fallback: "오프라인"
  }[apiState] || apiState;
  const apiStateTone = apiState === "connected" ? "done" : (apiState === "fallback" ? "off" : "warn");
  const latestSimulationStatus = simulationRunRows[0]?.status || "대기";
  const totalPnlSummary = calculateTotalPnlSummary(snapshot, snapshot?.simulation_runs, usingFallbackData);
  const selectedRun = snapshot?.selected_execution_run || null;
  const backendSelectedStep = stepLabels[snapshot?.current_step_key] || snapshot?.current_step?.step_key || snapshot?.selected_execution_run?.status || "대기";
  const displaySelectedStep = usingLocalFlowState
    ? selectedStep
    : (usingBackendSnapshot ? backendSelectedStep : EMPTY_MONITOR_CANDIDATE.initialStep);
  const backendExecutionState = selectedRun?.payload?.execution_policy === "buy_then_hold" && selectedRun?.status === "POSITION_OPEN"
    ? "매수 후 지갑보유 완료"
    : (statusLabels[selectedRun?.status] || selectedRun?.status || simulationState);
  const displaySimulationState = usingLocalFlowState
    ? simulationState
    : (usingBackendSnapshot ? `backend snapshot/SSE seq ${lastSeq || snapshot?.snapshot_seq || 0} · ${backendExecutionState}` : simulationState);
  const dataSourceLabel = usingBackendSnapshot
    ? "backend snapshot/SSE"
    : (usingFallbackData ? "OFFLINE DEMO frontend fallback-only" : "backend snapshot empty/no candidates");
  const displayPositionCount = usingLocalFlowState ? positionCount : (usingBackendSnapshot ? (snapshot?.positions || []).length : positionCount);
  const displayPositionSummary = usingLocalFlowState
    ? positionSummary
    : (usingBackendSnapshot ? ((snapshot?.positions || [])[0]?.status || "매수 체결 전") : positionSummary);
  const transactionEvidenceRows = normalizeTransactionRows(snapshot?.transactions);
  const transferEvidenceRows = normalizeTransferRows(snapshot?.transfers);
  const orderEvidenceRows = normalizeOrderRows(snapshot?.orders);
  const dryRunTransactionRefs = transactionEvidenceRows
    .filter((transaction) => transaction.dryRun)
    .map((transaction) => `${transaction.step}:${transaction.ref}`)
    .slice(0, 2)
    .join(" · ") || "-";
  const selectedAutoSmallRunStatus = selectedRun?.mode === "auto_small"
    ? `${selectedRun.status || "대기"} · ${DRY_RUN_ONLY_LABEL}`
    : (selectedRun?.status || "대기");
  const selectedLiveFullBoundary = snapshot?.live_full_boundary || selectedCandidate.live_full_boundary || {};
  const selectedRouteBlockers = routeBlockersForCandidate(selectedCandidate);
  const selectedLiveFullRunStatus = selectedRun?.mode === "live_full"
    ? `${selectedRun.status || "대기"} · ${LIVE_FULL_ROUTE_BOUNDARY_LABEL}`
    : (selectedRun?.status || "대기");
  const selectedApproval = useMemo(
    () => approvalStateForCandidate(snapshot, selectedCandidate),
    [snapshot, selectedCandidate]
  );
  const selectedCandidateStatus = candidateDisplayStatus(selectedCandidate, candidateStatuses);
  const selectedHasActionTarget = Boolean(selectedCandidate.id && selectedCandidate.id !== EMPTY_MONITOR_CANDIDATE.id);
  const selectedAutoSmallActionState = autoSmallDryRunState(selectedCandidate, selectedCandidateStatus);
  const selectedLiveFullActionState = liveFullRouteState(selectedCandidate, selectedCandidateStatus);
  const selectedActionPair = selectedActionPairLabel(selectedCandidate);
  const selectedActionWalletAddress = walletAddressForAction(selectedCandidate, snapshot);
  const selectedActionWalletLabel = shortWalletForAction(selectedActionWalletAddress);
  const flowIntel = flowIntelSummary(selectedCandidate, selectedApproval, selectedRouteBlockers);
  const routeFilterOptions = useMemo(
    () => uniqueOptionValues(opportunityList, "route", []),
    [opportunityList]
  );
  const chainFilterOptions = useMemo(
    () => uniqueOptionValues(opportunityList, "chain", ["polygon", "base", "eth"]),
    [opportunityList]
  );
  const statusFilterOptions = useMemo(
    () => uniqueOptionValues(
      opportunityList.map((candidate) => ({ ...candidate, status: candidateDisplayStatus(candidate, candidateStatuses) })),
      "status",
      ["NEW", "진행중", "EXEC TEST", "차단"]
    ),
    [candidateStatuses, opportunityList]
  );

  useEffect(() => {
    setCandidatePage(1);
  }, [filters.route, filters.chain, filters.status, opportunityList.length]);

  const loadSnapshot = useCallback(async (selectedOpportunityId = null) => {
    const query = selectedOpportunityId ? `?selected_opportunity_id=${encodeURIComponent(selectedOpportunityId)}` : "";
    const response = await fetch(`${API_SNAPSHOT_URL}${query}`, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      throw new Error(`snapshot_${response.status}`);
    }
    const payload = await response.json();
    const nextSeq = Number(payload.snapshot_seq || 0);
    lastAppliedSeq.current = Math.max(lastAppliedSeq.current, nextSeq);
    setLastSeq(lastAppliedSeq.current);
    setSnapshot(payload);
    setApiState(payload?.opportunities?.length ? "connected" : "empty");
    setCandidateStatuses((current) => {
      const backendIds = new Set((payload?.opportunities || []).map((card) => String(card.id)));
      if (!backendIds.size) return current;
      let changed = false;
      const next = { ...current };
      backendIds.forEach((id) => {
        if (BACKEND_PENDING_STATUSES.has(next[id])) {
          delete next[id];
          changed = true;
        }
      });
      return changed ? next : current;
    });
    if (payload?.selected_opportunity_id && payload?.opportunities?.length) {
      setSelectedCandidateId((current) => (
        payload.opportunities.some((card) => String(card.id) === String(current))
          ? current
          : String(payload.selected_opportunity_id)
      ));
    }
    return payload;
  }, []);

  const applySseEvent = useCallback((event) => {
    if (!event?.data || event.data === "{}") return;
    let row;
    try {
      row = JSON.parse(event.data);
    } catch {
      return;
    }
    const seq = Number(row.seq || 0);
    if (seq && seq <= lastAppliedSeq.current) return;
    if (seq) {
      lastAppliedSeq.current = seq;
      setLastSeq(seq);
    }
    if (shouldReloadSnapshotForEvent(row)) {
      loadSnapshot(row.opportunity_id || snapshot?.selected_opportunity_id || null)
        .catch(() => setApiState((current) => (current === "fallback" ? current : "reconnecting")));
      return;
    }
    setSnapshot((current) => applySseEventToSnapshot(current, row));
    const terminalStatus = runStatusFromEventPayload(row.payload || {}) || (row.event_type === "error" ? "FAILED" : "");
    if (row.opportunity_id && BACKEND_TERMINAL_RUN_STATUSES.has(terminalStatus)) {
      setCandidateStatuses((current) => {
        const id = String(row.opportunity_id);
        if (!BACKEND_PENDING_STATUSES.has(current[id])) return current;
        const next = { ...current };
        delete next[id];
        return next;
      });
    }
    setApiState((current) => (current === "fallback" ? current : "connected"));
  }, [loadSnapshot, snapshot?.selected_opportunity_id]);

  useEffect(() => {
    lineTimerIntervals.current.tick = setInterval(() => setTick((value) => value + 1), 100);
    return () => clearInterval(lineTimerIntervals.current.tick);
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function connectSnapshotAndStream() {
      try {
        await loadSnapshot();
        if (cancelled || typeof EventSource === "undefined") return;
        const source = new EventSource(`${API_STREAM_URL}?after_seq=${lastAppliedSeq.current}`);
        eventSourceRef.current = source;
        SSE_EVENT_TYPES.forEach((eventType) => source.addEventListener(eventType, applySseEvent));
        source.addEventListener("message", applySseEvent);
        source.onerror = () => {
          if (!cancelled) setApiState((current) => (current === "connected" ? "reconnecting" : current));
        };
      } catch {
        if (!cancelled) setApiState("fallback");
      }
    }
    connectSnapshotAndStream();
    return () => {
      cancelled = true;
      if (eventSourceRef.current) {
        SSE_EVENT_TYPES.forEach((eventType) => eventSourceRef.current.removeEventListener(eventType, applySseEvent));
        eventSourceRef.current.close();
      }
    };
  }, [applySseEvent, loadSnapshot]);

  useEffect(() => {
    if (!usingBackendSnapshot) return;
    if (!selectedCandidate?.backendId) return;
    if (Number(snapshot?.selected_opportunity_id) === Number(selectedCandidate.backendId)) return;
    loadSnapshot(selectedCandidate.backendId).catch(() => setApiState("fallback"));
  }, [loadSnapshot, selectedCandidate, snapshot?.selected_opportunity_id, usingBackendSnapshot]);

  const clearExecutionTimers = useCallback(() => {
    executionTimers.current.forEach((timer) => clearTimeout(timer));
    executionTimers.current = [];
    lineStartedAt.current = {};
    setFinishedLineDurations({});
  }, []);

  const resetCandidateState = useCallback((candidateId) => {
    const candidate = opportunities[candidateId] || candidateMap[candidateId];
    clearExecutionTimers();
    setLocalFlowOverride(false);
    setSelectedCandidateId(candidateId);
    if (!candidate || candidate.backendOnly) return;
    setNodeStates(initialNodeState(candidate));
    setEdgeStates({ ...candidate.initialEdges });
    setSelectedStep(candidate.initialStep);
    setSimulationState("대기 중");
    setPositionSummary("매수 체결 전");
    setPositionCount(0);
  }, [candidateMap, clearExecutionTimers]);

  function startLineTimer(lineName) {
    lineStartedAt.current[lineName] = performance.now();
    setFinishedLineDurations((current) => {
      const next = { ...current };
      delete next[lineName];
      return next;
    });
  }

  function finishLineTimer(lineName) {
    const startedAt = lineStartedAt.current[lineName];
    if (!startedAt) {
      setFinishedLineDurations((current) => ({ ...current, [lineName]: 0 }));
      return;
    }
    setFinishedLineDurations((current) => ({ ...current, [lineName]: performance.now() - startedAt }));
    delete lineStartedAt.current[lineName];
  }

  function setLineState(lineName, state, trackTiming = true) {
    setEdgeStates((current) => ({ ...current, [lineName]: state }));
    if (!trackTiming) return;
    if (state === "active") startLineTimer(lineName);
    if (state === "done") finishLineTimer(lineName);
  }

  function setNodeState(step, state, badge, detail) {
    setNodeStates((current) => ({ ...current, [step]: { state, badge, detail } }));
  }

  function addExecutionLog(candidate, log) {
    setLogs((current) => [[new Date().toISOString().slice(11, 19), candidate.symbol, ...log], ...current]);
  }

  function edgeLabel(edgeId) {
    if (!usingLocalFlowState && usingBackendSnapshot) {
      const edge = backendEdgeStates[edgeId] || {};
      const edgeState = edge.state || "wait";
      if (edgeState === "active") {
        const elapsed = edge.started_at_ms ? Date.now() - Number(edge.started_at_ms) : tick * 100;
        return `진행 중 ${formatElapsed(Math.max(0, elapsed))}`;
      }
      if (edge.duration_ms != null && edgeState === "done") return `완료 ${formatElapsed(edge.duration_ms)}`;
      if (edge.duration_ms != null) return `${stateLabels[edgeState] || edge.status || "대기"} ${formatElapsed(edge.duration_ms)}`;
      return `${stateLabels[edgeState] || "대기"} ${formatElapsed(0)}`;
    }
    const edgeState = edgeStates[edgeId] || "wait";
    if (edgeState === "active") {
      const startedAt = lineStartedAt.current[edgeId];
      const elapsed = startedAt ? performance.now() - startedAt : 0;
      return `진행 중 ${formatElapsed(elapsed + tick * 0)}`;
    }
    if (edgeState === "done") return `완료 ${formatElapsed(finishedLineDurations[edgeId] || 0)}`;
    return `${stateLabels[edgeState] || "대기"} ${formatElapsed(0)}`;
  }

  function edgeProgress(edgeId, edgeState) {
    if (edgeState === "done") return 1;
    if (edgeState !== "active") return 0;
    if (!usingLocalFlowState && usingBackendSnapshot) {
      const edge = backendEdgeStates[edgeId] || {};
      const elapsed = edge.started_at_ms ? Date.now() - Number(edge.started_at_ms) : tick * 100;
      return Math.max(0.08, Math.min(1, elapsed / TEST_FLOW_ACTION_DELAY_MS));
    }
    const startedAt = lineStartedAt.current[edgeId];
    const elapsed = startedAt ? performance.now() - startedAt : 0;
    return Math.max(0.08, Math.min(1, elapsed / TEST_FLOW_ACTION_DELAY_MS));
  }

  function updateCandidateStatus(candidateId, status) {
    setCandidateStatuses((current) => ({ ...current, [candidateId]: status }));
  }

  function selectCandidate(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    resetCandidateState(candidateId);
    if (candidate?.backendOnly) {
      loadSnapshot(candidate.backendId).catch(() => setApiState("fallback"));
    }
  }

  function visualFlowTemplateForCandidate(candidate) {
    if (candidate?.initialNodes && candidate?.initialEdges && candidate?.execution) return candidate;
    const routeText = `${candidate?.route || ""} ${candidate?.routeType || ""} ${candidate?.sell?.label || ""}`.toLowerCase();
    if (routeText.includes("cex") || routeText.includes("krw") || routeText.includes("bridge")) {
      return opportunities["test-flow-demo"];
    }
    return opportunities["sol-polygon-dex"];
  }

  function prepareLocalVisualFlow(candidateId, template, status) {
    clearExecutionTimers();
    setLocalFlowOverride(true);
    setSelectedCandidateId(candidateId);
    setNodeStates(initialNodeState(template));
    setEdgeStates({ ...template.initialEdges });
    setPositionCount(0);
    setPositionSummary("매수 체결 전");
    updateCandidateStatus(candidateId, status);
  }

  function applyVisualFlowEvent(candidate, event) {
    setSelectedStep(event.step);
    setSimulationState(event.simulation);
    if (event.node) setNodeState(event.node[0], event.node[1], event.node[2], event.node[3]);
    if (event.nextNode) setNodeState(event.nextNode[0], event.nextNode[1], event.nextNode[2], event.nextNode[3]);
    if (event.edge) setLineState(event.edge[0], event.edge[1]);
    if (event.extraEdges) event.extraEdges.forEach((edge) => setLineState(edge[0], edge[1]));
    if (event.log) addExecutionLog(candidate, event.log);
    if (event.position) {
      setPositionCount(1);
      setPositionSummary(event.position);
    }
  }

  function scheduleVisualFlowEvents(candidate, events) {
    events.forEach((event) => {
      const timer = setTimeout(() => applyVisualFlowEvent(candidate, event), event.delay);
      executionTimers.current.push(timer);
    });
  }

  function startLocalPrecheckFlow(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    const template = visualFlowTemplateForCandidate(candidate);
    prepareLocalVisualFlow(candidateId, template, "프리체크");
    setSelectedStep("프리체크");
    setSimulationState(`프리체크 테스트: 액션당 5초`);
    setLineState("signal-precheck", "active");
    setNodeState("precheck", "active", "진행중", "quote, deposit, transfer, pool reserve 확인");
    executionTimers.current.push(setTimeout(() => {
      setNodeState("precheck", candidate.precheckPass ? "done" : "wait", candidate.precheckPass ? "완료" : "대기", candidate.precheckPass ? "프리체크 통과" : "입금망 수동 확인 필요");
      setLineState("signal-precheck", "done");
      addExecutionLog(candidate || template, ["프리체크", candidate?.buy?.label || template.buy.label, candidate?.sell?.label || template.sell.label, candidate?.spread || template.spread, "5초 테스트 완료"]);
    }, TEST_FLOW_ACTION_DELAY_MS));
  }

  function startLocalVisualFlow(candidateId, label = "수동 매수/매도 테스트") {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    if (!candidate) {
      setSimulationState(`${label}: 후보 없음`);
      return;
    }
    const template = visualFlowTemplateForCandidate(candidate);
    prepareLocalVisualFlow(candidateId, template, "진행중");
    setSelectedStep(label);
    setSimulationState(`${label}: 액션당 5초`);
    scheduleVisualFlowEvents(candidate, template.execution);
  }

  function startLocalWalletHoldFlow(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    if (!candidate) {
      setSimulationState("매수 후 지갑보유 설정 실패: 후보 없음");
      return;
    }

    const template = visualFlowTemplateForCandidate(candidate);
    const buy = candidate.buy || template.buy;
    const sell = candidate.sell || template.sell;
    const walletDetail = candidate.initialNodes?.walletHold?.[2] || template.initialNodes?.walletHold?.[2] || "지갑보유: 지갑 주소 확인 / 매수 후 정지";
    const spread = candidate.spread || template.spread;
    prepareLocalVisualFlow(candidateId, template, "지갑보유");
    setSelectedStep("매수 후 지갑보유 설정");
    setSimulationState("매수 후 지갑보유: 매수까지만 실행 · 액션당 5초");

    const events = [
      {
        delay: testFlowDelay(0),
        step: "프리체크",
        simulation: "매수 후 지갑보유: 프리체크",
        node: ["precheck", "active", "진행중", "프리체크: 매수 전 quote/liquidity 확인"],
        edge: ["signal-precheck", "active"],
        log: ["프리체크", buy.label, sell.label, spread, "지갑보유 경로"]
      },
      {
        delay: testFlowDelay(1),
        step: "매수",
        simulation: "매수 후 지갑보유: 매수 준비",
        node: ["precheck", "done", "완료", "프리체크: PASS / 매수만 진행"],
        nextNode: ["dexBuy", "active", "진행중", `매수: ${buy.label.replace("BUY · ", "")} / ${buy.chain} / 매수 후 정지`],
        edge: ["signal-precheck", "done"],
        extraEdges: [["precheck-buy", "active"]],
        log: ["매수 준비", buy.label, "지갑보유", spread, "매도/브릿지 미실행"]
      },
      {
        delay: testFlowDelay(2),
        step: "지갑보유",
        simulation: "매수 후 지갑보유: 지갑 보유 전환",
        node: ["dexBuy", "done", "완료", `매수: ${buy.label.replace("BUY · ", "")} / 가상 fill 완료`],
        nextNode: ["walletHold", "active", "진행중", walletDetail],
        edge: ["precheck-buy", "done"],
        extraEdges: [["buy-wallet-hold", "active"], ["buy-bridge", "wait"], ["buy-same", "wait"]],
        log: ["지갑보유", buy.label, "wallet hold", spread, "매수 후 정지"]
      },
      {
        delay: testFlowDelay(3),
        step: "지갑보유 완료",
        simulation: "매수 후 지갑보유: 지갑에 보유 중",
        node: ["walletHold", "done", "완료", walletDetail],
        edge: ["buy-wallet-hold", "done"],
        log: ["보유", buy.label, "wallet", spread, "매도/브릿지 중지"],
        position: `${candidate.symbol} 매수 후 지갑 보유 중`
      }
    ];

    scheduleVisualFlowEvents(candidate, events);
  }

  function runPrecheck(candidateId) {
    startLocalPrecheckFlow(candidateId);
  }

  function startExecution(candidateId) {
    startLocalVisualFlow(candidateId, "수동 매수/매도 테스트");
  }

  function startBuyThenWalletHold(candidateId) {
    startLocalWalletHoldFlow(candidateId);
  }

  async function startBackendSimulationRun(candidate) {
    if (!candidate?.backendOnly || !candidate.backendId || !candidate.routeId) {
      return { ok: false, error_code: "missing_backend_route" };
    }
    const result = await fetch("/api/arbitrage/simulation-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        opportunity_id: candidate.backendId,
        route_id: candidate.routeId,
        mode: "paper",
        requested_by: "monitor-ui-simulation",
        trade_amount_krw: 99862
      })
    }).then((response) => response.json());
    const completed = result.ok && result.status === "COMPLETED";
    return { ...result, completed };
  }

  async function startBackendPaperRun(candidate) {
    if (!candidate?.backendOnly || !candidate.backendId || !candidate.routeId) {
      return { ok: false, error_code: "missing_backend_route" };
    }
    return fetch("/api/arbitrage/executions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        opportunity_id: candidate.backendId,
        route_id: candidate.routeId,
        mode: "paper",
        idempotency_key: `ui-paper:${candidate.backendId}:${candidate.routeId}`,
        requested_by: "monitor-ui"
      })
    }).then((response) => response.json());
  }

  async function startBackendBuyThenHoldPaperRun(candidate) {
    const requestLabel = "매수 후 지갑보유 요청";
    if (!candidate?.backendOnly || !candidate.backendId || !candidate.routeId) {
      return { ok: false, error_code: "missing_backend_route", requestLabel };
    }
    return fetch("/api/arbitrage/executions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        opportunity_id: candidate.backendId,
        route_id: candidate.routeId,
        mode: "paper",
        execution_policy: "buy_then_hold",
        idempotency_key: `ui-buy-wallet-hold:${candidate.backendId}:${candidate.routeId}`,
        requested_by: "monitor-ui"
      })
    }).then((response) => response.json());
  }

  async function startAutoSmallDryRun(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    const autoSmallState = autoSmallDryRunState(candidate, candidateStatuses[candidateId] || candidate?.defaultStatus);
    if (!candidate?.backendOnly || !autoSmallState.enabled) return;
    clearExecutionTimers();
    setSelectedCandidateId(candidateId);
    updateCandidateStatus(candidateId, "진행중");
    setSimulationState("소액 요청");
    const result = await fetch("/api/arbitrage/executions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        opportunity_id: candidate.backendId,
        route_id: candidate.routeId,
        mode: "auto_small",
        dry_run: true,
        idempotency_key: `ui-auto-small-dry-run:${candidate.backendId}:${candidate.routeId}`,
        requested_by: "monitor-ui",
        trade_amount_krw: AUTO_SMALL_DRY_RUN_TRADE_AMOUNT_KRW
      })
    })
      .then((response) => response.json())
      .catch(() => {
        setApiState("fallback");
        return { ok: false, error_code: "api_unavailable" };
      });
    setSimulationState(result.ok ? `소액 ${DRY_RUN_ONLY_LABEL}` : actionFailureMessage("소액 실패", result));
    await loadSnapshot(candidate.backendId).catch(() => setApiState("fallback"));
  }

  async function requestApproval(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    if (!candidate?.backendOnly || !candidate.backendId || !candidate.routeId) return;
    setSelectedCandidateId(candidateId);
    setSimulationState("one-click 운영 승인 요청");
    await fetch("/api/arbitrage/approvals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        opportunity_id: candidate.backendId,
        route_id: candidate.routeId,
        mode: "one_click",
        approval_key: `operator_approval:${candidate.backendId}:${candidate.routeId}:none:one_click`,
        requested_by: "monitor-ui",
        reason: "operator confirmation required for one_click held execution",
        payload: {
          route_type: candidate.routeType,
          trade_amount_krw: ONE_CLICK_HELD_TRADE_AMOUNT_KRW,
          approval_required: true,
          source: "monitor-ui"
        }
      })
    }).catch(() => setApiState("fallback"));
    await loadSnapshot(candidate.backendId).catch(() => setApiState("fallback"));
  }

  async function decideApproval(candidateId, decision) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    const approvalId = approvalIdForCandidate(snapshot, candidate);
    if (!candidate?.backendOnly || !approvalId) return;
    setSelectedCandidateId(candidateId);
    setSimulationState(decision === "approve" ? "one-click 운영 승인 처리" : "one-click 운영 거절 처리");
    await fetch(`/api/arbitrage/approvals/${approvalId}/${decision}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        operator: "monitor-ui",
        decision_payload: {
          source: "monitor-ui",
          opportunity_id: candidate.backendId,
          route_id: candidate.routeId,
          approval_id: approvalId
        }
      })
    }).catch(() => setApiState("fallback"));
    await loadSnapshot(candidate.backendId).catch(() => setApiState("fallback"));
  }

  async function requestLiveFullApproval(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    if (!candidate?.backendOnly || !candidate.backendId || !candidate.routeId) return;
    setSelectedCandidateId(candidateId);
    setSimulationState("live_full 운영 승인 요청");
    await fetch("/api/arbitrage/approvals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        opportunity_id: candidate.backendId,
        route_id: candidate.routeId,
        mode: "live_full",
        approval_key: `operator_approval:${candidate.backendId}:${candidate.routeId}:none:live_full:${LIVE_FULL_TRADE_AMOUNT_KRW}`,
        requested_by: "monitor-ui",
        reason: "operator approval required for Part 8 live_full bridge/CEX route",
        payload: {
          route_type: candidate.routeType,
          trade_amount_krw: LIVE_FULL_TRADE_AMOUNT_KRW,
          expires_at_ms: Date.now() + 600000,
          provider_boundary: LIVE_FULL_PROVIDER_BOUNDARY,
          simulated: true,
          cex_withdrawal_enabled: false,
          source: "monitor-ui"
        }
      })
    }).catch(() => setApiState("fallback"));
    await loadSnapshot(candidate.backendId).catch(() => setApiState("fallback"));
  }

  async function decideLiveFullApproval(candidateId, decision) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    const approvalId = liveFullApprovalIdForCandidate(candidate);
    if (!candidate?.backendOnly || !approvalId) return;
    setSelectedCandidateId(candidateId);
    setSimulationState(decision === "approve" ? "live_full 운영 승인 처리" : "live_full 운영 거절 처리");
    await fetch(`/api/arbitrage/approvals/${approvalId}/${decision}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        operator: "monitor-ui",
        decision_payload: {
          source: "monitor-ui",
          opportunity_id: candidate.backendId,
          route_id: candidate.routeId,
          approval_id: approvalId,
          provider_boundary: LIVE_FULL_PROVIDER_BOUNDARY,
          simulated: true,
          cex_withdrawal_enabled: false
        }
      })
    }).catch(() => setApiState("fallback"));
    await loadSnapshot(candidate.backendId).catch(() => setApiState("fallback"));
  }

  async function startOneClickHeldExecution(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    if (!candidate?.backendOnly || !candidate.backendId || !candidate.routeId) return;
    clearExecutionTimers();
    setSelectedCandidateId(candidateId);
    updateCandidateStatus(candidateId, "보류 준비");
    setSimulationState("one-click 보류 실행 요청");
    const result = await fetch("/api/arbitrage/executions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        opportunity_id: candidate.backendId,
        route_id: candidate.routeId,
        mode: "one_click",
        idempotency_key: `ui-one-click-held:${candidate.backendId}:${candidate.routeId}`,
        requested_by: "monitor-ui",
        trade_amount_krw: ONE_CLICK_HELD_TRADE_AMOUNT_KRW
      })
    })
      .then((response) => response.json())
      .catch(() => {
        setApiState("fallback");
        return { ok: false, error_code: "api_unavailable" };
      });
    if (result.approval_required) {
      setSimulationState(`one-click 승인 필요: approval_id ${result.approval?.id || "-"}`);
    } else {
      setSimulationState(result.ok ? "one-click EXEC_READY 보류" : actionFailureMessage("one-click 보류 실패", result));
    }
    await loadSnapshot(candidate.backendId).catch(() => setApiState("fallback"));
  }

  async function startLiveFullRoute(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    const liveFullState = liveFullRouteState(candidate, candidateStatuses[candidateId] || candidate?.defaultStatus);
    if (!candidate?.backendOnly || !candidate.backendId || !candidate.routeId || !liveFullState.enabled) return;
    clearExecutionTimers();
    setSelectedCandidateId(candidateId);
    updateCandidateStatus(candidateId, "진행중");
    setSimulationState("live_full route 실행 요청");
    const result = await fetch("/api/arbitrage/executions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        opportunity_id: candidate.backendId,
        route_id: candidate.routeId,
        mode: "live_full",
        idempotency_key: `ui-live-full:${candidate.backendId}:${candidate.routeId}:${LIVE_FULL_TRADE_AMOUNT_KRW}`,
        requested_by: "monitor-ui",
        trade_amount_krw: LIVE_FULL_TRADE_AMOUNT_KRW,
        simulated: true,
        provider_boundary: LIVE_FULL_PROVIDER_BOUNDARY,
        live_full_boundary_ack: true,
        cex_withdrawal_enabled: false
      })
    })
      .then((response) => response.json())
      .catch(() => {
        setApiState("fallback");
        return { ok: false, error_code: "api_unavailable" };
      });
    setSimulationState(result.ok ? "live_full route 완료" : actionFailureMessage("live_full route 실패", result));
    await loadSnapshot(candidate.backendId).catch(() => setApiState("fallback"));
  }

  async function stopExecution(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    clearExecutionTimers();
    if (candidate?.backendOnly && snapshot?.selected_execution_run?.id) {
      await fetch(`/api/arbitrage/executions/${snapshot.selected_execution_run.id}/abort`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      }).catch(() => setApiState("fallback"));
      await loadSnapshot(candidate.backendId).catch(() => setApiState("fallback"));
      return;
    }
    updateCandidateStatus(candidateId, opportunities[candidateId]?.defaultStatus || candidate?.defaultStatus || "NEW");
    setSelectedStep("중단");
    setSimulationState("수동 중단");
  }

  async function startSelectedSimulation(candidateId) {
    const candidate = candidateMap[candidateId] || opportunities[candidateId];
    if (!candidate) {
      setSimulationState("선택 후보 시뮬레이션 실패: 후보 없음");
      return;
    }
    startLocalVisualFlow(candidateId, "자동 매수/매도 테스트");
  }

  function handleCandidateAction(event, candidateId, action) {
    flashActionButton(event);
    event.stopPropagation();
    if (action === "select" || action === "watch") selectCandidate(candidateId);
    if (action === "precheck") runPrecheck(candidateId);
    if (action === "simulation-run") startSelectedSimulation(candidateId);
    if (action === "execute") startExecution(candidateId);
    if (action === "buy-wallet-hold") startBuyThenWalletHold(candidateId);
    if (action === "auto-small-dry-run") startAutoSmallDryRun(candidateId);
    if (action === "approval-request") requestApproval(candidateId);
    if (action === "approval-approve") decideApproval(candidateId, "approve");
    if (action === "approval-reject") decideApproval(candidateId, "reject");
    if (action === "live-full-approval-request") requestLiveFullApproval(candidateId);
    if (action === "live-full-approval-approve") decideLiveFullApproval(candidateId, "approve");
    if (action === "live-full-route") startLiveFullRoute(candidateId);
    if (action === "one-click-held") startOneClickHeldExecution(candidateId);
    if (action === "stop") stopExecution(candidateId);
  }

  function toggleFilter(groupName, value) {
    setFilters((current) => ({ ...current, [groupName]: current[groupName] === value ? "" : value }));
  }

  function clearFilters() {
    setFilters({ route: "", chain: "", status: "" });
  }

  function updateSpikeThreshold(kind, value) {
    const cleaned = String(value || "").replace(/[^\d.]/g, "").slice(0, 6);
    setSpikeRules((current) => ({
      ...current,
      [kind]: {
        ...(current[kind] || {}),
        threshold: cleaned,
        saved: false
      }
    }));
  }

  function saveSpikeRule(event, kind) {
    flashActionButton(event);
    event.stopPropagation();
    const threshold = spikeRules[kind]?.threshold || "30";
    setSpikeRules((current) => ({
      ...current,
      [kind]: {
        ...(current[kind] || {}),
        threshold,
        saved: true,
        pair: selectedActionPair
      }
    }));
    setSimulationState(`${kind === "real" ? "실거래" : "시뮬"} 스파이크 ${threshold}% 조건 저장`);
  }

  const visibleCandidates = opportunityList.filter((candidate) => {
    const status = candidateDisplayStatus(candidate, candidateStatuses);
    return (
      (!filters.route || candidate.route === filters.route)
      && (!filters.chain || candidate.chain === filters.chain)
      && (!filters.status || status === filters.status)
    );
  });
  const candidatePageCount = Math.max(1, Math.ceil(visibleCandidates.length / CANDIDATE_PAGE_SIZE));
  const safeCandidatePage = Math.min(candidatePage, candidatePageCount);
  const candidatePageStart = visibleCandidates.length ? ((safeCandidatePage - 1) * CANDIDATE_PAGE_SIZE) + 1 : 0;
  const candidatePageEnd = Math.min(visibleCandidates.length, safeCandidatePage * CANDIDATE_PAGE_SIZE);
  const pagedCandidates = visibleCandidates.slice(
    (safeCandidatePage - 1) * CANDIDATE_PAGE_SIZE,
    safeCandidatePage * CANDIDATE_PAGE_SIZE
  );
  const arbitrageWatchCards = pagedCandidates.filter((candidate) => candidateSpreadPercent(candidate) >= 5);
  const priceStrikeCards = opportunityList.filter((candidate) => candidateSpreadPercent(candidate) >= 30);
  const executionApiRows = executionApiRowsFor(apiStatusRows, selectedCandidate);
  const tradePnlCards = buildTradePnlCards(snapshot, snapshot?.simulation_runs, usingFallbackData);
  const bottomTabs = [
    { id: "logs", label: "실행 로그" },
    { id: "positions", label: "포지션" },
    { id: "api", label: "API 현황" },
    { id: "pnl", label: "P&L" },
    { id: "simulation", label: "시뮬레이션" },
    { id: "dryRun", label: "Dry-run" },
    { id: "liveRefs", label: "Live refs" }
  ];

  const precheckPassCount = opportunityList.filter((candidate) => candidate.precheckPass).length;
  const visibleLogRows = normalizeLogRows(usingBackendSnapshot && !usingLocalFlowState ? snapshot?.logs : (usingLocalFlowState ? logs : []), selectedCandidate);
  const positionRows = usingBackendSnapshot || usingFallbackData
    ? normalizePositionRows(usingBackendSnapshot ? snapshot?.positions : [], selectedCandidate, displayPositionSummary, usingFallbackData)
    : [];
  const flowNodes = useMemo(() => makeFlowNodes(activeNodeStates), [activeNodeStates]);
  const flowEdges = useMemo(() => routeEdges.map((edge) => {
    const backendEdge = backendEdgeStates[edge.id];
    const edgeState = usingLocalFlowState ? (edgeStates[edge.id] || "wait") : (usingBackendSnapshot ? (backendEdge?.state || "wait") : "wait");
    const progress = edgeProgress(edge.id, edgeState);
    return {
      ...edge,
      type: "timed",
      animated: false,
      className: `route-line route-${edgeState}`,
      data: { state: edgeState, label: edgeLabel(edge.id), color: stateColors[edgeState], progress },
      style: { stroke: stateColors[edgeState], strokeWidth: edgeState === "active" ? 4 : 3 },
      markerEnd: { type: MarkerType.ArrowClosed, color: stateColors[edgeState], width: 16, height: 16 }
    };
  }), [backendEdgeStates, edgeStates, finishedLineDurations, tick, usingBackendSnapshot, usingLocalFlowState]);
  const flowMapNodes = useMemo(
    () => flowNodes.map((node, index) => ({
      ...node,
      position: flowMapNodePosition(node.id, index)
    })),
    [flowNodes]
  );
  const flowMapEdges = useMemo(
    () => flowEdges.map((edge) => ({ ...edge })),
    [flowEdges]
  );

  return (
    <div className="shell">
      <header className="topbar">
        <div>
          <h1>아비트라지 실행 모니터</h1>
          <p>마지막 업데이트: {formatUtcTime(snapshot?.server_time)} UTC · {dataSourceLabel} · {apiState}</p>
        </div>
        <div className="top-actions">
          <span className="status-pill"><i className="dot"></i> 감시 ON</span>
          <span className="status-pill amber"><i className="dot"></i> 시뮬레이션 대기</span>
          <span className="status-pill red"><i className="dot"></i> 실거래 OFF</span>
          <span className="status-pill green"><i className="dot"></i> {apiHealthText}</span>
        </div>
      </header>
      {usingFallbackData && (
        <div className="offline-demo-banner" data-offline-demo="true">
          <strong>OFFLINE DEMO</strong>
          <span>backend snapshot unavailable · hardcoded demo data separated from live monitoring</span>
        </div>
      )}

      <main className="operations-grid">
        <div className="center-stack">
        <section className="panel flow-panel">
          <div className="panel-head">
            <h2>실행 플로우 모니터링</h2>
            <span>선택 후보: {selectedCandidate.currentAsset}</span>
          </div>
          <div className="content">
            <div className="flow-map" aria-label="아비트라지 실행 플로우맵">
              <ReactFlow
                nodes={flowMapNodes}
                edges={flowMapEdges}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                fitView
                fitViewOptions={{ padding: 0.08 }}
                minZoom={0.28}
                maxZoom={1.2}
                nodesDraggable={false}
                nodesConnectable={false}
                elementsSelectable={false}
                zoomOnScroll={false}
                proOptions={{ hideAttribution: true }}
              >
                <Background color="#24303c" gap={24} size={1} />
                <Controls showInteractive={false} />
              </ReactFlow>
            </div>
            <div className="mobile-flow-list" aria-label="모바일 실행 단계">
              {flowNodes.map((node) => (
                <article key={node.id} className={`mobile-flow-step ${node.data.state}`}>
                  <div>
                    <strong>{node.data.title}</strong>
                    <span>{node.data.detail}</span>
                  </div>
                  <em>{node.data.badge}</em>
                </article>
              ))}
            </div>
            <section className="flow-intel-strip" aria-label="중요 거래소/DEX 색상">
              {flowIntel.map((item) => (
                <article key={item.label} className={`flow-intel-card ${item.tone}`}>
                  <span>{item.label}</span>
                  <strong>{item.value}</strong>
                  <small>{item.detail}</small>
                </article>
              ))}
            </section>
            <div className="monitor-summary" aria-label="선택된 아비트라지 실행 시뮬레이션">
              <div className="monitor-item">
                <span>선택된 아비트라지</span>
                <strong id="selected-arbitrage-route">{selectedCandidate.routeSummary}</strong>
              </div>
              <div className="monitor-item">
                <span>현재 위치</span>
                <strong id="selected-arbitrage-step">{displaySelectedStep}</strong>
              </div>
              <div className="monitor-item">
                <span>실행 시뮬레이션</span>
                <strong id="simulation-state">{displaySimulationState}</strong>
              </div>
              <div
                className={`monitor-item approval-monitor-item ${approvalBadgeClass(selectedApproval.approval_status, selectedApproval.approval_required)}`}
                data-approval-status={selectedApproval.approval_status}
                data-approval-required={String(selectedApproval.approval_required)}
                data-approval-id={selectedApproval.approval_id || ""}
              >
                <span>운영 승인</span>
                <strong id="selected-approval-state">{selectedApproval.held_one_click ? "EXEC_READY · " : ""}{selectedApproval.label}</strong>
                <small>approval_id {selectedApproval.approval_id || "-"} · pending {selectedApproval.pending_count}</small>
              </div>
            </div>
            <div
              className="execution-meta-strip"
              data-auto-small-source="snapshot?.selected_execution_run"
              data-transaction-source="snapshot?.transactions"
              data-flow-node-source="snapshot?.flow_nodes"
              data-flow-edge-source="snapshot?.flow_edges"
              data-log-source="snapshot?.logs"
              data-position-source="snapshot?.positions"
              data-order-source="snapshot?.orders"
              data-transfer-source="snapshot?.transfers"
              data-live-full-boundary-source="snapshot?.live_full_boundary"
              data-dry-run-boundary="same-chain DEX dry-run only"
              data-live-full-provider-boundary={LIVE_FULL_PROVIDER_BOUNDARY}
              data-cex-withdrawal-enabled="false"
            >
              <span>동작 <strong>{actionModeLabel(selectedRun?.mode || "monitor")}</strong></span>
              <span>run <strong>{selectedRun?.id || "-"}</strong></span>
              <span>route <strong>{selectedCandidate.routeId || selectedCandidate.selected_route_id || "demo"}</strong></span>
              <span>status <strong>{selectedRun?.mode === "live_full" ? selectedLiveFullRunStatus : selectedAutoSmallRunStatus}</strong></span>
              <span className="dry-run-tx-ref">dry-run tx <strong>{dryRunTransactionRefs}</strong></span>
              <span>live_full <strong>{String(Boolean(selectedLiveFullBoundary.simulated ?? false))}</strong></span>
              <span>blockers <strong>{selectedRouteBlockers[0] || "none"}</strong></span>
            </div>
          </div>
        </section>

        </div>

        <div className="candidate-stack">
        <div className="action-stack">
        <div className="selected-actionbar" aria-label="선택 후보 실행 동작">
          <div className="actionbar-head">
            <span>동작</span>
            <strong>{selectedCandidate.routeSummary}</strong>
          </div>
          <div className="action-mode-grid">
            <section className="action-mode-card real-trade-card" aria-label="실거래 동작">
              <div className="action-mode-head">
                <strong>실거래</strong>
              </div>
              <div className="action-card-grid" data-action-kind="real">
                <section className="manual-action-card" aria-label="실거래 수동">
                  <h3>수동</h3>
                  <div className="manual-action-buttons">
                    <button className="action-button real-action" type="button" data-action="real-buy-sell" data-real-action-locked="true" disabled title="선택 페어 실거래 매수/매도">
                      <strong>{selectedActionPair}</strong>
                      <span>매수/매도</span>
                    </button>
                    <button className="action-button real-action wallet-hold-action" type="button" data-action="real-buy-hold" data-real-action-locked="true" disabled title="선택 페어 실거래 매수 후 지갑 보유">
                      <strong>{selectedActionPair}</strong>
                      <span>지갑 보유</span>
                      <small>{selectedActionWalletLabel}</small>
                    </button>
                  </div>
                </section>
                <section className="auto-action-card" aria-label="실거래 자동">
                  <h3>자동</h3>
                  <div className="spike-rule-form" data-auto-exec-scope="price-spike-only">
                    <label>
                      <span>스파이크 %</span>
                      <input
                        type="number"
                        min="0"
                        step="0.1"
                        value={spikeRules.real.threshold}
                        data-spike-rule-kind="real"
                        onChange={(event) => updateSpikeThreshold("real", event.target.value)}
                      />
                    </label>
                    <button
                      className="action-button spike-save-action"
                      type="button"
                      data-action="save-spike-rule"
                      data-spike-rule-kind="real"
                      data-feedback="click"
                      data-critical-action="true"
                      onClick={(event) => saveSpikeRule(event, "real")}
                    >
                      저장
                    </button>
                  </div>
                  <small>{spikeRules.real.saved ? `${spikeRules.real.threshold}% 자동 감지 저장` : "프라이스 스파이크 감시"}</small>
                  <button
                    className="action-button spike-auto-execute-action real-action"
                    type="button"
                    data-action="price-spike-auto-execute"
                    data-spike-rule-kind="real"
                    data-auto-exec-button="price-spike-buy-sell"
                    data-auto-exec-scope="price-spike-only"
                    data-real-action-locked="true"
                    disabled
                    title="프라이스 스파이크 감시 기준 실거래 매수/매도 자동 실행"
                  >
                    매수/매도 자동 실행
                  </button>
                </section>
              </div>
            </section>
            <section className="action-mode-card simulation-card" aria-label="시뮬레이션 동작">
              <div className="action-mode-head">
                <strong>시뮬</strong>
              </div>
              <div className="action-card-grid" data-action-kind="simulation">
                <section className="manual-action-card" aria-label="시뮬레이션 수동">
                  <h3>수동</h3>
                  <div className="manual-action-buttons">
                    <button
                      className="action-button simulation-action"
                      type="button"
                      data-action="execute"
                      data-mode="paper"
                      data-sim-action="manual"
                      data-feedback="click"
                      data-critical-action="true"
                      data-action-scope="selected-candidate"
                      disabled={!selectedHasActionTarget}
                      title="선택 페어 시뮬레이션 매수/매도"
                      onClick={(event) => handleCandidateAction(event, selectedCandidate.id, "execute")}
                    >
                      <strong>{selectedActionPair}</strong>
                      <span>매수/매도</span>
                    </button>
                    <button
                      className="action-button wallet-hold-action"
                      type="button"
                      data-action="buy-wallet-hold"
                      data-mode="buy_then_hold"
                      data-sim-action="hold"
                      data-feedback="click"
                      data-critical-action="true"
                      data-action-scope="selected-candidate"
                      disabled={!selectedHasActionTarget}
                      title="선택 페어 시뮬레이션 매수 후 지갑 보유"
                      onClick={(event) => handleCandidateAction(event, selectedCandidate.id, "buy-wallet-hold")}
                    >
                      <strong>{selectedActionPair}</strong>
                      <span>지갑 보유</span>
                      <small>{selectedActionWalletLabel}</small>
                    </button>
                  </div>
                </section>
                <section className="auto-action-card" aria-label="시뮬레이션 자동">
                  <h3>자동</h3>
                  <div className="spike-rule-form" data-auto-exec-scope="price-spike-only">
                    <label>
                      <span>스파이크 %</span>
                      <input
                        type="number"
                        min="0"
                        step="0.1"
                        value={spikeRules.simulation.threshold}
                        data-spike-rule-kind="simulation"
                        onChange={(event) => updateSpikeThreshold("simulation", event.target.value)}
                      />
                    </label>
                    <button
                      className="action-button spike-save-action"
                      type="button"
                      data-action="save-spike-rule"
                      data-spike-rule-kind="simulation"
                      data-feedback="click"
                      data-critical-action="true"
                      onClick={(event) => saveSpikeRule(event, "simulation")}
                    >
                      저장
                    </button>
                  </div>
                  <small>{spikeRules.simulation.saved ? `${spikeRules.simulation.threshold}% 자동 감지 저장` : "프라이스 스파이크 감시"}</small>
                  <button
                    className="action-button spike-auto-execute-action simulation-action"
                    type="button"
                    data-action="price-spike-auto-execute"
                    data-spike-rule-kind="simulation"
                    data-auto-exec-button="price-spike-buy-sell"
                    data-auto-exec-scope="price-spike-only"
                    data-feedback="click"
                    data-critical-action="true"
                    disabled={!selectedHasActionTarget}
                    title="프라이스 스파이크 감시 기준 시뮬레이션 매수/매도 자동 실행"
                    onClick={(event) => handleCandidateAction(event, selectedCandidate.id, "simulation-run")}
                  >
                    매수/매도 자동 실행
                  </button>
                </section>
              </div>
            </section>
          </div>
        </div>
        </div>

        <section className="panel candidate-panel">
          <div className="panel-head">
            <h2>아비트라지 현황</h2>
            <span>{visibleCandidates.length}/{opportunityList.length} candidates</span>
          </div>
          <div className="content">
            <div className="candidate-filterbar" aria-label="아비트라지 후보 필터">
              <FilterGroup title="전체">
                <button
                  className={`chip ${!filters.route && !filters.chain && !filters.status ? "active" : ""}`}
                  type="button"
                  data-filter-group="all"
                  data-filter-value="all"
                  onClick={clearFilters}
                >
                  전체보기
                </button>
              </FilterGroup>
              <FilterGroup title="route">
                {routeFilterOptions.map((value) => (
                  <button key={value} className={`chip ${filters.route === value ? "active" : ""}`} type="button" data-filter-group="route" data-filter-value={value} onClick={() => toggleFilter("route", value)}>{value}</button>
                ))}
              </FilterGroup>
              <FilterGroup title="chain">
                {chainFilterOptions.map((value) => (
                  <button key={value} className={`chip ${filters.chain === value ? "active" : ""}`} type="button" data-filter-group="chain" data-filter-value={value} onClick={() => toggleFilter("chain", value)}>{value}</button>
                ))}
              </FilterGroup>
              <FilterGroup title="상태">
                {statusFilterOptions.map((value) => (
                  <button key={value} className={`chip ${filters.status === value ? "active" : ""}`} type="button" data-filter-group="status" data-filter-value={value} onClick={() => toggleFilter("status", value)}>{value}</button>
                ))}
              </FilterGroup>
            </div>

            <div className="candidate-pager" aria-label="아비트라지 현황 페이지">
              <span>{candidatePageStart}-{candidatePageEnd}/{visibleCandidates.length}</span>
              <div>
                <button
                  type="button"
                  aria-label="이전 페이지"
                  disabled={safeCandidatePage <= 1}
                  onClick={() => setCandidatePage((page) => Math.max(1, page - 1))}
                >
                  ‹
                </button>
                <strong>{safeCandidatePage}/{candidatePageCount}</strong>
                <button
                  type="button"
                  aria-label="다음 페이지"
                  disabled={safeCandidatePage >= candidatePageCount}
                  onClick={() => setCandidatePage((page) => Math.min(candidatePageCount, page + 1))}
                >
                  ›
                </button>
              </div>
            </div>

            <div className="opportunity-list watch-card-list candidate-trading-list">
              <section className="arbitrage-watch-section" aria-label="아비트라지 감시 5% 이상">
                <div className="watch-section-head">
                  <h3>아비트라지 감시</h3>
                  <span>5% 이상</span>
                </div>
                {arbitrageWatchCards.map((candidate) => (
                  <MarketWatchCard
                    key={`watch-${candidate.id}`}
                    candidate={candidate}
                    status={candidateDisplayStatus(candidate, candidateStatuses)}
                    selected={candidate.id === selectedCandidate.id}
                    variant="watch"
                    onSelect={() => selectCandidate(candidate.id)}
                    onAction={(event, action) => handleCandidateAction(event, candidate.id, action)}
                  />
                ))}
                {!arbitrageWatchCards.length && (
                  <div className="watch-card-empty" data-empty-opportunities="true">
                    <strong>5% 이상 후보 없음</strong>
                    <span>필터 조건 또는 backend snapshot을 확인하세요.</span>
                  </div>
                )}
              </section>
            </div>
          </div>
        </section>
        </div>
      </main>

      <section className="bottom-tabs-panel">
        <div className="bottom-tabbar" role="tablist" aria-label="운영 상세 탭">
          {bottomTabs.map((tab) => (
            <button
              key={tab.id}
              role="tab"
              type="button"
              aria-selected={bottomTab === tab.id}
              onClick={() => setBottomTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div className="bottom-tab-content">
          {bottomTab === "logs" && (
            <section className="tab-table execution-log-panel">
              <div className="panel-head">
                <h2>실행 로그</h2>
                <span>신호, 프리체크, 매수, 매도</span>
              </div>
              <div className="content">
                <table>
                  <thead>
                    <tr>
                      <th>시간</th>
                      <th>토큰</th>
                      <th>단계</th>
                      <th>매수</th>
                      <th>매도 route</th>
                      <th>spread</th>
                      <th>상태</th>
                    </tr>
                  </thead>
                  <tbody id="execution-log-body">
                    {visibleLogRows.map((row, index) => (
                      <tr key={`${row[0]}-${index}`}>
                        <td>{row[0]}</td>
                        <td><strong>{row[1]}</strong></td>
                        <td>{row[2]}</td>
                        <td>{row[3]}</td>
                        <td>{row[4]}</td>
                        <td className={String(row[5]).startsWith("+") ? "result-plus" : "result-warn"}>{row[5]}</td>
                        <td>{row[6]}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {bottomTab === "positions" && (
            <section className="tab-table position-panel">
              <div className="panel-head">
                <h2>포지션 상태</h2>
                <span>매수 체결 이후</span>
              </div>
              <div className="content">
                <table>
                  <thead>
                    <tr>
                      <th>토큰</th>
                      <th>수량</th>
                      <th>매수</th>
                      <th>목표 매도</th>
                      <th>상태</th>
                    </tr>
                  </thead>
                  <tbody id="position-body">
                    {positionRows.map((position) => (
                      <tr key={position.id}>
                        <td><strong>{position.symbol}</strong></td>
                        <td>{position.qty}</td>
                        <td>{position.avgBuy}</td>
                        <td>{position.exit}</td>
                        <td>{position.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {bottomTab === "api" && (
            <>
            <section className={`panel api-health-panel bottom-api-health-panel ${apiStateTone}`}>
              <div className="panel-head">
                <h2>API 현황</h2>
                <span>{apiStatusTab === "execution" ? "실행 사용 API" : "read-only monitor"}</span>
              </div>
              <div className="content">
                <div className="api-tabbar" role="tablist" aria-label="API 현황 탭">
                  <button role="tab" type="button" aria-selected={apiStatusTab === "execution"} onClick={() => setApiStatusTab("execution")}>실행 API</button>
                  <button role="tab" type="button" aria-selected={apiStatusTab === "all"} onClick={() => setApiStatusTab("all")}>전체 API</button>
                  <button role="tab" type="button" aria-selected={apiStatusTab === "system"} onClick={() => setApiStatusTab("system")}>시스템</button>
                </div>

                {apiStatusTab === "execution" && (
                  <section className="execution-api-grid" aria-label="실행 사용 API">
                    {executionApiRows.map((api) => (
                      <ApiKeyCard key={api.id} api={api} />
                    ))}
                  </section>
                )}

                {apiStatusTab === "all" && (
                  <section className="api-status-list" id="api-status-body" aria-label="API별 정상 상태">
                    <div className="api-status-list-head">
                      <strong>API 상세</strong>
                      <span>개별 read-only 연결</span>
                    </div>
                    {apiStatusRows.map((api) => (
                      <article key={api.id} className={`api-status-row ${apiStatusTone(api.status)}`}>
                        <div>
                          <strong>{api.label}</strong>
                          <span className="api-status-source">{api.capability} · {api.source}</span>
                        </div>
                        <span className={`state-badge ${apiStatusTone(api.status)}`}>{apiStatusLabel(api.status)}</span>
                        <small>{api.detail}</small>
                      </article>
                    ))}
                  </section>
                )}

                {apiStatusTab === "system" && (
                  <section className="api-status-grid system-status-grid" aria-label="API 및 시스템 상태">
                    <StatusCard label="Snapshot" value={apiStateLabel} detail={dataSourceLabel} />
                    <StatusCard label="SSE 연결" value={lastSeq || snapshot?.snapshot_seq || 0} detail={apiState === "reconnecting" ? "재연결 중" : "seq replay 준비"} />
                    <StatusCard label="Simulation" value={latestSimulationStatus} detail="시뮬 paper 검증" />
                    <StatusCard label="실거래 차단" value="ON" detail="sign/order/withdraw disabled" />
                  </section>
                )}
              </div>
            </section>
            <section className="tab-table provider-job-panel" data-provider-job-source="snapshot?.provider_jobs">
              <div className="panel-head">
                <h2>API 상세 상태</h2>
                <span>selected monitor snapshot</span>
              </div>
              <div className="content">
                <table>
                  <thead>
                    <tr>
                      <th>API</th>
                      <th>데이터</th>
                      <th>대상</th>
                      <th>상태</th>
                      <th>오류</th>
                    </tr>
                  </thead>
                  <tbody id="provider-job-body">
                    {providerJobRows.map((job) => (
                      <tr key={job.id}>
                        <td><strong>{job.provider}</strong></td>
                        <td>{job.capability}</td>
                        <td>{job.scope}</td>
                        <td>{job.status}</td>
                        <td>{job.error || "-"}</td>
                      </tr>
                    ))}
                    {!providerJobRows.length && (
                      <tr>
                        <td colSpan="5">snapshot API 상태 대기</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>
            </>
          )}

          {bottomTab === "pnl" && (
            <div className="performance-stack bottom-performance-stack">
            <section className="panel performance-panel">
              <div className="panel-head">
                <h2>총 P&L</h2>
                <span>거래당/합산</span>
              </div>
              <div className="content">
                <section className={`total-pnl-card ${totalPnlSummary.tone}`} aria-label="총 P&L">
                  <span>총 P&L</span>
                  <strong id="total-pnl-value">{totalPnlSummary.value}</strong>
                  <small>{totalPnlSummary.detail}</small>
                </section>
                <section className="trade-scoreboard" aria-label="거래당 P&L">
                  <div className="scoreboard-head">
                    <strong>거래당 P&L</strong>
                    <span>{tradePnlCards.length} rows</span>
                  </div>
                  {tradePnlCards.map((card) => (
                    <article key={card.id} className={`trade-pnl-card ${card.tone}`}>
                      <span>{card.label}</span>
                      <strong>{card.value}</strong>
                      <small>{card.detail}</small>
                    </article>
                  ))}
                </section>
              </div>
            </section>
            </div>
          )}

          {bottomTab === "simulation" && (
            <section className="tab-table simulation-run-panel" data-simulation-run-source="snapshot?.simulation_runs">
              <div className="panel-head">
                <h2>Simulation Runs</h2>
                <span>selected opportunity scoped</span>
              </div>
              <div className="content">
                <table>
                  <thead>
                    <tr>
                      <th>run</th>
                      <th>status</th>
                      <th>route</th>
                      <th>execution</th>
                      <th>error_code / blockers</th>
                    </tr>
                  </thead>
                  <tbody id="simulation-run-body">
                    {simulationRunRows.map((run) => (
                      <tr key={run.id}>
                        <td><strong>{run.id}</strong></td>
                        <td>{run.status}</td>
                        <td>{run.routeId}</td>
                        <td>{run.executionRunId}</td>
                        <td>{run.errorCode || run.blockers}</td>
                      </tr>
                    ))}
                    {!simulationRunRows.length && (
                      <tr>
                        <td colSpan="5">snapshot simulation run rows 대기</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {bottomTab === "dryRun" && (
            <section className="tab-table dry-run-transaction-panel" data-bottom-tab="dryRun">
              <div className="panel-head">
                <h2>Dry-run 트랜잭션</h2>
                <span>{DRY_RUN_ONLY_LABEL} · auto_small Part 7</span>
              </div>
              <div className="content">
                <table>
                  <thead>
                    <tr>
                      <th>단계</th>
                      <th>상태</th>
                      <th>adapter</th>
                      <th>tx/ref</th>
                      <th>submit_ref</th>
                      <th>gas</th>
                      <th>fee</th>
                    </tr>
                  </thead>
                  <tbody id="dry-run-transaction-body">
                    {transactionEvidenceRows.map((transaction) => (
                      <tr key={transaction.id} data-dry-run={String(transaction.dryRun)}>
                        <td>{transaction.step}</td>
                        <td>{transaction.status}</td>
                        <td>{transaction.adapter}</td>
                        <td className="dry-run-tx-ref">{transaction.ref}</td>
                        <td>{transaction.submitRef}</td>
                        <td>{transaction.gas}</td>
                        <td>{transaction.fee}</td>
                      </tr>
                    ))}
                    {!transactionEvidenceRows.length && (
                      <tr>
                        <td colSpan="7">snapshot dry-run transaction evidence 대기</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {bottomTab === "liveRefs" && (
            <section
              className="tab-table live-full-ref-panel"
              data-bottom-tab="liveRefs"
              data-transfer-source="snapshot?.transfers"
              data-order-source="snapshot?.orders"
              data-provider-boundary={LIVE_FULL_PROVIDER_BOUNDARY}
              data-cex-withdrawal-enabled="false"
            >
              <div className="panel-head">
                <h2>Live Full Route Refs</h2>
                <span>{LIVE_FULL_ROUTE_BOUNDARY_LABEL} · CEX withdrawal disabled</span>
              </div>
              <div className="content live-ref-grid">
                <table>
                  <thead>
                    <tr>
                      <th>단계</th>
                      <th>상태</th>
                      <th>from</th>
                      <th>to</th>
                      <th>bridge/deposit ref</th>
                      <th>adapter</th>
                    </tr>
                  </thead>
                  <tbody id="transfer-ref-body">
                    {transferEvidenceRows.map((transfer) => (
                      <tr key={transfer.id} data-simulated={String(transfer.simulated)} data-cex-withdrawal={String(transfer.cexWithdrawal)}>
                        <td>{transfer.step}</td>
                        <td>{transfer.status}</td>
                        <td>{transfer.from}</td>
                        <td>{transfer.to}</td>
                        <td className="dry-run-tx-ref">{transfer.ref}</td>
                        <td>{transfer.adapter}</td>
                      </tr>
                    ))}
                    {!transferEvidenceRows.length && (
                      <tr>
                        <td colSpan="6">snapshot transfer/bridge/deposit refs 대기</td>
                      </tr>
                    )}
                  </tbody>
                </table>
                <table>
                  <thead>
                    <tr>
                      <th>단계</th>
                      <th>상태</th>
                      <th>venue</th>
                      <th>market</th>
                      <th>order ref</th>
                      <th>adapter</th>
                    </tr>
                  </thead>
                  <tbody id="order-ref-body">
                    {orderEvidenceRows.map((order) => (
                      <tr key={order.id} data-simulated={String(order.simulated)} data-cex-withdrawal={String(order.cexWithdrawal)}>
                        <td>{order.step}</td>
                        <td>{order.status}</td>
                        <td>{order.venue}</td>
                        <td>{order.market}</td>
                        <td className="dry-run-tx-ref">{order.ref}</td>
                        <td>{order.adapter}</td>
                      </tr>
                    ))}
                    {!orderEvidenceRows.length && (
                      <tr>
                        <td colSpan="6">snapshot order refs 대기</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </div>
      </section>
      <footer>Arbitrage monitor · @xyflow/react execution flow · backend snapshot/SSE primary · OFFLINE DEMO frontend fallback-only · {LIVE_FULL_ROUTE_BOUNDARY_LABEL} · CEX withdrawal disabled</footer>
    </div>
  );
}

function FilterGroup({ title, children }) {
  return (
    <section className="filter-group">
      <h2>{title}</h2>
      <div className="chips">{children}</div>
    </section>
  );
}

function StatusCard({ label, value, detail, id }) {
  return (
    <article className="status-card">
      <span>{label}</span>
      <strong id={id}>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

export default App;
