from html.parser import HTMLParser
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "arbitrage" / "index.html").read_text(encoding="utf-8")
PACKAGE = (ROOT / "arbitrage" / "package.json").read_text(encoding="utf-8") if (ROOT / "arbitrage" / "package.json").exists() else ""
VITE_CONFIG = (ROOT / "arbitrage" / "vite.config.js").read_text(encoding="utf-8") if (ROOT / "arbitrage" / "vite.config.js").exists() else ""
DATA_JS = (ROOT / "arbitrage" / "src" / "data.js").read_text(encoding="utf-8")
SRC = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "arbitrage" / "src").glob("**/*"))
    if path.is_file() and path.suffix in {".js", ".jsx", ".css"}
)
UI = "\n".join([HTML, PACKAGE, VITE_CONFIG, SRC])


class ClassCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.class_counts: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        for class_name in (attr_map.get("class") or "").split():
            self.class_counts[class_name] = self.class_counts.get(class_name, 0) + 1


def class_count(class_name: str) -> int:
    parser = ClassCounter()
    parser.feed(HTML)
    return parser.class_counts.get(class_name, 0) + len(re.findall(rf"\b{re.escape(class_name)}\b", SRC))


def test_xyflow_react_dependency_and_vite_entrypoint_are_present() -> None:
    required_text = [
        "\"@xyflow/react\"",
        "\"react\"",
        "\"react-dom\"",
        "\"vite\"",
        "<div id=\"root\"></div>",
        "/src/main.jsx",
        "ReactFlow",
        "@xyflow/react/dist/style.css",
    ]

    for text in required_text:
        assert text in UI


def test_web3map_candidate_cards_show_route_contracts_and_spread() -> None:
    assert class_count("opportunity-card") >= 2

    required_text = [
        "SOL",
        "\"sol-polygon-dex\"",
        "route: \"DEX-DEX\"",
        "chain: \"polygon\"",
        "defaultStatus: \"NEW\"",
        "spreadFloor: \">=10%\"",
        "+20.98%",
        "BUY · QUICKSWAP V2",
        "SELL · UNISWAP V3",
        "token CA",
        "pool CA",
        "route: \"DEX-CEX\"",
        "\"test-flow-demo\"",
        "EXEC TEST",
        "+55.40%",
    ]

    for text in required_text:
        assert text in UI

    forbidden_text = [
        "MAPO",
        "mapo",
        "BITHUMB",
        "mapo-bsc-bithumb",
        "NEED DEPOSIT",
        "+840%",
    ]

    for text in forbidden_text:
        assert text not in UI


def test_candidate_contract_addresses_copy_on_click_with_icon() -> None:
    required_text = [
        "CopyAddress",
        "copyAddress",
        "shortAddress",
        "navigator.clipboard.writeText",
        "Fall back for non-HTTPS or permission-limited browser contexts.",
        "document.execCommand(\"copy\")",
        "className=\"copy-address\"",
        "className={`copy-toast ${copied ? \"show\" : \"\"}`}",
        "event.stopPropagation()",
        "📋",
        "복사됨",
        "주소 복사됨",
        "aria-label={`${label} 주소 복사`}",
        "<CopyAddress label=\"token CA\" value={candidate.buy.token} />",
        "<CopyAddress label=\"pool CA\" value={candidate.buy.pool} />",
        "<CopyAddress label=\"token CA\" value={candidate.sell.token} />",
        "<CopyAddress label=\"pool CA\" value={candidate.sell.pool} />",
    ]

    for text in required_text:
        assert text in UI


def test_flowmap_branches_directly_from_dex_buy_without_branch_hub() -> None:
    required_text = [
        "감지",
        "프리체크",
        "매수",
        "브릿지",
        "동일체인 매도",
        "지갑보유",
        "타체인 매도",
        "CEX 입금",
    ]

    for text in required_text:
        assert text in UI

    assert class_count("flow-hub") == 0
    assert ">분기<" not in UI
    assert "routeEdges" in UI
    assert UI.count("source: \"dexBuy\"") >= 3
    assert "pulse" in UI


def test_monitoring_ui_has_select_filter_and_execution_simulation_hooks() -> None:
    required_text = [
        "실행 플로우 모니터링",
        "data-filter-group=\"route\"",
        "data-filter-group=\"chain\"",
        "data-filter-group=\"status\"",
        "data-filter-value=\"all\"",
        "전체보기",
        "\"sol-polygon-dex\"",
        "\"test-flow-demo\"",
        "data-action=\"execute\"",
        "data-action=\"simulation-run\"",
        "data-action=\"select\"",
        "visibleCandidates",
        "selectCandidate",
        "toggleFilter",
        "clearFilters",
        "startExecution",
        "executionTimers",
        "setTimeout",
        "selectedCandidateId",
        "selected-arbitrage-step",
        "선택 후보 시뮬레이션",
        "startSelectedSimulation",
        "fetch(\"/api/arbitrage/simulation-runs\"",
        "mode: \"paper\"",
    ]

    for text in required_text:
        assert text in UI


def test_candidate_filters_are_above_cards_and_cards_are_compact() -> None:
    assert "<dt>route</dt>" not in UI
    assert "candidate-filterbar" in UI
    assert "compact-card" in UI
    assert UI.index("candidate-filterbar") < UI.index("opportunity-list")


def test_candidate_filter_chips_use_small_font() -> None:
    assert ".candidate-filterbar .chip" in UI
    assert "font-size: 10px;" in UI
    assert "min-height: 21px;" in UI


def test_candidate_filters_start_as_all_and_single_click_selects_value() -> None:
    required_text = [
        "filters.route === value",
        "filters.chain === value",
        "filters.status === value",
        "current[groupName] === value ? \"\" : value",
    ]

    for text in required_text:
        assert text in UI

    assert "class=\"chip active\" type=\"button\" data-filter-group" not in UI


def test_layout_prioritizes_watch_cards_flow_execution_api_pnl_and_bottom_tabs() -> None:
    required_text = [
        "operations-grid",
        "action-stack",
        "candidate-stack",
        "center-stack",
        "candidate-panel",
        "candidate-trading-list",
        "arbitrage-watch-section",
        "arbitrageWatchCards",
        "아비트라지 감시",
        "5% 이상",
        "className=\"panel flow-panel\"",
        "실행 플로우 모니터링",
        "className=\"selected-actionbar\"",
        "api-health-panel",
        "execution-api-grid",
        "apiStatusTab",
        "setApiStatusTab",
        "className=\"performance-stack bottom-performance-stack\"",
        "className=\"panel performance-panel\"",
        "trade-scoreboard",
        "tradePnlCards",
        "bottom-tabs-panel",
        "bottomTab",
        "setBottomTab",
        "bottomTab === \"pnl\"",
        "bottom-tab-content",
        "grid-template-areas:",
        "\"action action\"",
        "\"candidates flow\"",
        "grid-template-columns: minmax(340px, 380px) minmax(0, 1fr);",
        "grid-template-rows: auto minmax(0, var(--monitor-column-height));",
        "grid-area: action;",
        "grid-area: candidates;",
        "grid-area: flow;",
        "gap: 12px;",
        ".bottom-performance-stack",
        ".bottom-api-health-panel",
    ]

    for text in required_text:
        assert text in UI

    assert UI.index("className=\"action-stack\"") < UI.index("className=\"selected-actionbar\"")
    assert UI.index("className=\"candidate-stack\"") < UI.index("className=\"panel candidate-panel\"")
    assert UI.index("className=\"center-stack\"") < UI.index("className=\"panel flow-panel\"")
    assert UI.index("className=\"bottom-tabs-panel\"") < UI.index("className={`panel api-health-panel bottom-api-health-panel ${apiStateTone}`}")
    assert UI.index("className=\"performance-stack bottom-performance-stack\"") < UI.index("className=\"panel performance-panel\"")
    assert UI.index("className=\"bottom-tabs-panel\"") < UI.index("id=\"execution-log-body\"")
    assert "SIMULATION ONLY" not in UI
    assert "className=\"safety-banner\"" not in UI

    forbidden_text = [
        "className=\"right-stack\"",
        "className=\"panel route-cluster\"",
        "className=\"panel compact-status-panel pnl-only-panel\"",
        "className=\"panel provider-job-panel\"",
        "className=\"panel simulation-run-panel\"",
        "className=\"panel dry-run-transaction-panel\"",
        "className=\"panel live-full-ref-panel\"",
        "className=\"lower-grid\"",
        "실행 클러스터",
        "className=\"cluster-flow\"",
        "\"left pnl pnl\"",
        "grid-area: pnl;",
    ]
    for text in forbidden_text:
        assert text not in UI


def test_important_execution_buttons_show_click_feedback_animation() -> None:
    required_text = [
        "flashActionButton(event)",
        "action-clicked",
        "실행 요청됨",
        "data-feedback=\"click\"",
        "data-critical-action=\"true\"",
        ".action-button.action-clicked",
        ".action-button.action-clicked::after",
        "@keyframes actionPulse",
        "@keyframes actionToast",
        "@keyframes criticalGlow",
        "animation: actionPulse",
        "data-action-feedback=\"pressed\"",
        ".action-button[data-critical-action=\"true\"]::before",
        ".action-button:active",
        "className=\"action-mode-grid\"",
        ".action-card-grid",
        ".manual-action-card",
        ".auto-action-card",
        ".manual-action-buttons",
        "grid-template-columns: repeat(2, minmax(0, 1fr))",
        "min-height: 32px",
        "padding: 3px 10px",
    ]

    for text in required_text:
        assert text in UI


def test_candidate_filters_are_only_candidate_filters_and_actions_are_separate() -> None:
    required_text = [
        "<FilterGroup title=\"전체\">",
        "<FilterGroup title=\"route\">",
        "<FilterGroup title=\"chain\">",
        "<FilterGroup title=\"상태\">",
        "className=\"selected-actionbar\"",
        "aria-label=\"선택 후보 실행 동작\"",
        "className=\"action-mode-grid\"",
        "className=\"action-mode-card real-trade-card\"",
        "className=\"action-mode-card simulation-card\"",
        "saveSpikeRule(event, \"simulation\")",
        "handleCandidateAction(event, selectedCandidate.id, \"execute\")",
        "handleCandidateAction(event, selectedCandidate.id, \"buy-wallet-hold\")",
        "uniqueOptionValues(opportunityList, \"route\", [])",
        "동작 <strong>{actionModeLabel(selectedRun?.mode || \"monitor\")}</strong>",
        "function actionModeLabel(modeValue)",
    ]

    for text in required_text:
        assert text in UI

    forbidden_text = [
        "<FilterGroup title=\"동작\">",
        "<FilterGroup title=\"모드\">",
        "data-mode={value}",
        "setMode(",
        "useState(\"monitor\")",
        "자동실행 차단",
        ">자동 OFF<",
        "실 OFF · 시뮬 ON",
        "uniqueOptionValues(opportunityList, \"route\", [\"DEX-DEX\", \"DEX-CEX\", \"Bridge\", \"KRW\"])",
        "uniqueOptionValues(opportunityList, \"route\", [\"DEX-DEX\", \"DEX-CEX\"])",
        "<span>mode <strong>",
        "className=\"selected-action-buttons\"",
        "handleCandidateAction(event, selectedCandidate.id, \"auto-small-dry-run\")",
        "handleCandidateAction(event, selectedCandidate.id, \"live-full-route\")",
    ]
    for text in forbidden_text:
        assert text not in UI


def test_monitor_ui_removes_top_demo_buttons_and_uses_selected_simulation_action() -> None:
    required_text = [
        "선택 후보 시뮬레이션",
        "startSelectedSimulation",
        "data-action=\"simulation-run\"",
        "requested_by: \"monitor-ui-simulation\"",
        "실거래 전 성공/실패 검증",
    ]

    for text in required_text:
        assert text in UI

    assert "SOL 데모" not in UI
    assert "data-action=\"seed-demo\"" not in UI
    assert "data-action=\"demo-simulation\"" not in UI


def test_monitor_ui_has_api_status_dashboard() -> None:
    required_text = [
        "api-health-panel",
        "API 현황",
        "api-tabbar",
        "apiStatusTab === \"execution\"",
        "apiStatusTab === \"all\"",
        "apiStatusTab === \"system\"",
        "실행 API",
        "전체 API",
        "시스템",
        "실행 사용 API",
        "executionApiRows",
        "api-key-dot",
        "apiStateLabel",
        "apiStateTone",
        "apiStatusRows",
        "apiStatusLabel(",
        "apiStatusTone(",
        "API 상세",
        "className=\"api-status-list\"",
        "className={`api-status-row ${apiStatusTone(api.status)}`}",
        "aria-label=\"API별 정상 상태\"",
        "grid-template-columns: minmax(0, 1fr) auto minmax(42px, 58px);",
        "min-height: 38px;",
        "SSE 연결",
        "Snapshot",
        "Simulation",
        "실거래 차단",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_shows_total_pnl_as_own_animated_card() -> None:
    required_text = [
        "totalPnlSummary",
        "calculateTotalPnlSummary(",
        "performance-panel",
        "trade-scoreboard",
        "tradePnlCards",
        "거래당 P&L",
        "className={`total-pnl-card ${totalPnlSummary.tone}`}",
        "aria-label=\"총 P&L\"",
        "<span>총 P&L</span>",
        "id=\"total-pnl-value\"",
        "pnlPulse",
        "pnlGlow",
        ".total-pnl-card",
        ".total-pnl-card strong",
    ]

    for text in required_text:
        assert text in UI

    assert "pnl-only-panel" not in UI
    assert "className=\"status-board\"" not in UI
    assert "<StatusCard label=\"활성 후보\"" not in UI
    assert "<StatusCard label=\"프리체크\"" not in UI
    assert "<StatusCard label=\"현재 위치\"" not in UI
    assert "<StatusCard label=\"포지션\"" not in UI


def test_api_status_dashboard_lists_each_api_and_hides_provider_jobs_label() -> None:
    required_text = [
        "defaultApiStatusRows(providerHealth, providerJobRows)",
        "DEX Screener",
        "GeckoTerminal",
        "Binance",
        "OKX",
        "Bybit",
        "Upbit",
        "Bithumb",
        "Alchemy RPC",
        "api-status-source",
        "API 상세 상태",
        "id=\"api-status-body\"",
        "snapshot API 상태 대기",
        "<th>API</th>",
        "<th>데이터</th>",
        "<th>대상</th>",
        "<th>상태</th>",
        "<th>오류</th>",
    ]

    for text in required_text:
        assert text in UI


def test_bottom_operational_tables_are_tabbed_not_all_visible_panels() -> None:
    required_text = [
        "const bottomTabs = [",
        "id: \"logs\"",
        "id: \"positions\"",
        "id: \"api\"",
        "id: \"pnl\"",
        "id: \"simulation\"",
        "id: \"dryRun\"",
        "id: \"liveRefs\"",
        "role=\"tablist\"",
        "role=\"tab\"",
        "aria-selected={bottomTab === tab.id}",
        "bottomTab === \"logs\"",
        "bottomTab === \"positions\"",
        "bottomTab === \"api\"",
        "bottomTab === \"pnl\"",
        "bottomTab === \"simulation\"",
        "bottomTab === \"dryRun\"",
        "bottomTab === \"liveRefs\"",
        "id=\"execution-log-body\"",
        "id=\"position-body\"",
        "id=\"provider-job-body\"",
        "id=\"simulation-run-body\"",
        "id=\"dry-run-transaction-body\"",
        "id=\"transfer-ref-body\"",
        "id=\"order-ref-body\"",
    ]

    for text in required_text:
        assert text in UI

    assert ">Provider Jobs<" not in UI
    assert "providerHealthSummary" not in UI
    assert "<th>provider</th>" not in UI
    assert "<th>capability</th>" not in UI
    assert "<th>scope</th>" not in UI
    assert "<th>error_code</th>" not in UI


def test_monitor_ui_has_mobile_flow_list_and_bounded_evidence_tables() -> None:
    required_text = [
        "mobile-flow-list",
        "모바일 실행 단계",
        "mobile-flow-step",
        ".opportunity-card {",
        "width: 100%;",
        ".candidate-head,",
        ".route-label {",
        ".live-route-grid strong",
        ".mobile-flow-list",
        ".bottom-tab-content",
        "max-height: 360px;",
        "overflow: auto;",
        "@media (max-width: 620px)",
        ".flow-map {",
        "display: none;",
    ]

    for text in required_text:
        assert text in UI


def test_flow_map_spacing_and_demo_candidate_are_visible() -> None:
    required_text = [
        "아비트라지 현황",
        "\"test-flow-demo\"",
        "TEST",
        "EXEC TEST",
        "flowCanvasNodes",
        "position: { x: 0, y: 408 }",
        "position: { x: 250, y: 408 }",
        "position: { x: 500, y: 408 }",
        "fitView",
        "nodeTypes",
    ]

    for text in required_text:
        assert text in UI

    assert "아비트라지 후보 카드" not in UI


def test_route_bars_are_auto_aligned_between_cards_not_fixed_coordinates() -> None:
    required_text = [
        "ReactFlow",
        "source: \"signal\", target: \"precheck\"",
        "source: \"precheck\", target: \"dexBuy\"",
        "source: \"dexBuy\", target: \"bridge\"",
        "source: \"dexBuy\", target: \"sameChainSell\"",
        "source: \"dexBuy\", target: \"walletHold\"",
        "source: \"bridge\", target: \"crossChainSell\"",
        "source: \"bridge\", target: \"cexDeposit\"",
        "source: \"cexDeposit\", target: \"cexSell\"",
        "animated: false",
        "type: \"timed\"",
        "edgeTypes",
        "flowMapNodes",
        "flowMapEdges",
        "flowMapNodePosition",
        "flowNodes.map((node, index) => ({",
        "flowEdges.map((edge) => ({",
    ]

    for text in required_text:
        assert text in UI

    assert ".l-signal-pre" not in UI
    assert ".t-signal-pre" not in UI
    assert "left: 17.2%" not in UI
    assert "filter((node) => node.data.state !== \"skipped\")" not in UI
    assert "edge.data?.state !== \"skipped\"" not in UI


def test_dex_buy_branch_routes_are_staggered_to_avoid_overdraw() -> None:
    required_text = [
        "const flowMapPositions = {",
        "id: \"bridge\"",
        "id: \"sameChainSell\"",
        "id: \"walletHold\"",
        "id: \"crossChainSell\"",
        "id: \"cexDeposit\"",
        "id: \"cexSell\"",
        "position: { x: 750, y: 100 }",
        "position: { x: 750, y: 300 }",
        "position: { x: 750, y: 528 }",
        "position: { x: 1000, y: 100 }",
        "position: { x: 1000, y: 300 }",
        "position: { x: 1250, y: 300 }",
        "precheck: { x: 250, y: 408 }",
        "dexBuy: { x: 500, y: 408 }",
        "bridge: { x: 750, y: 100 }",
        "sameChainSell: { x: 750, y: 300 }",
        "walletHold: { x: 750, y: 528 }",
        "crossChainSell: { x: 1000, y: 100 }",
        "cexDeposit: { x: 1000, y: 300 }",
        "cexSell: { x: 1250, y: 300 }",
        "return flowMapPositions[nodeId] || { x: index * 250, y: 408 };",
        "fitViewOptions={{ padding: 0.08 }}",
        "minZoom={0.28}",
        "box-sizing: border-box;",
    ]

    for text in required_text:
        assert text in UI


def test_layout_scales_for_more_candidates_and_keeps_flow_readable() -> None:
    required_text = [
        ".operations-grid {",
        "--monitor-column-height: 812px;",
        "grid-template-areas:",
        "\"action action\"",
        "\"candidates flow\"",
        ".action-stack {",
        "grid-area: action;",
        ".candidate-stack {",
        "grid-area: candidates;",
        ".candidate-panel .content",
        "max-height: var(--monitor-column-height);",
        ".candidate-trading-list",
        ".candidate-trading-list .watch-card",
        ".candidate-trading-list .watch-contracts",
        ".candidate-trading-list .watch-actions",
        "overflow: auto;",
        ".performance-panel .content",
        ".trade-scoreboard",
        ".execution-api-grid",
        ".center-stack {",
        "grid-area: flow;",
        "height: var(--monitor-column-height);",
        ".performance-stack {",
        ".bottom-performance-stack",
        ".bottom-api-health-panel",
        ".flow-map {",
        "height: 520px;",
        ".flow-node {",
        "width: 172px;",
        ".flow-node-compact-v2.skipped",
        "min-height: 116px;",
        "--flow-node-font-size: 11px;",
        ".flow-node h3 {",
        ".flow-target-chip",
        ".flow-metric-row strong",
        "font-size: var(--flow-node-font-size);",
    ]

    for text in required_text:
        assert text in UI


def test_api_health_panel_does_not_stretch_into_blank_column() -> None:
    required_text = [
        ".bottom-tabs-panel",
        ".bottom-api-health-panel",
        ".api-health-panel {",
        ".api-health-panel .content {",
        "flex: 0 0 auto;",
        ".bottom-tab-content .performance-panel .content",
    ]

    for text in required_text:
        assert text in UI

    forbidden_text = [
        "className=\"right-stack\"",
        "grid-template-rows: 1fr;",
        "align-self: stretch;",
        ".center-stack,\n.performance-stack {\n  height: 100%;\n}",
        ".center-stack,\n.right-stack,\n.performance-stack {\n  height: 100%;\n}",
        ".flow-panel,\n.api-health-panel {\n  display: flex;\n  flex-direction: column;\n  height: 100%;\n}",
        ".flow-panel {\n  display: flex;\n  flex-direction: column;\n  height: 100%;\n}",
        ".api-health-panel .content {\n  flex: 1;\n}",
    ]

    for text in forbidden_text:
        assert text not in UI


def test_execution_flow_cards_prevent_runtime_text_overlap() -> None:
    required_text = [
        "function compactFlowRuntimeText(value, maxLength = 26)",
        "compactFlowRuntimeText(",
        ".flow-target-chip {",
        "overflow: hidden;",
        "text-overflow: ellipsis;",
        ".flow-detail-line {",
        "display: -webkit-box;",
        "-webkit-line-clamp: 2;",
        "-webkit-box-orient: vertical;",
        ".flow-metric-row span",
    ]

    for text in required_text:
        assert text in UI

    forbidden_text = [
        ".flow-target-chip {\n  display: inline-flex;\n  flex: 1 1 0;\n  align-items: center;\n  justify-content: center;\n  min-height: 23px;\n  min-width: 0;\n  overflow: visible;",
        ".flow-detail-line {\n  display: block;",
        "return text.length > 42 ? `${text.slice(0, 39)}...` : text;",
    ]

    for text in forbidden_text:
        assert text not in UI


def test_candidate_status_panel_is_paginated_for_many_live_opportunities() -> None:
    required_text = [
        "const CANDIDATE_PAGE_SIZE = 5;",
        "const TEST_CANDIDATE_FILLER_COUNT = 5;",
        "function buildTestCandidateFillers(candidates, fallbackCandidates)",
        "testOnly: true",
        "data-source={candidate.testOnly ? \"test\" : (candidate.backendOnly ? \"backend\" : \"fallback\")}",
        "candidatePage",
        "setCandidatePage",
        "candidatePageCount",
        "safeCandidatePage",
        "pagedCandidates",
        "candidatePageStart",
        "candidatePageEnd",
        "className=\"candidate-pager\"",
        "aria-label=\"아비트라지 현황 페이지\"",
        "aria-label=\"이전 페이지\"",
        "aria-label=\"다음 페이지\"",
        "setCandidatePage((page) => Math.max(1, page - 1))",
        "setCandidatePage((page) => Math.min(candidatePageCount, page + 1))",
        "arbitrageWatchCards = pagedCandidates.filter",
        "priceStrikeCards = opportunityList.filter",
        "const opportunityList = usingBackendSnapshot ? buildTestCandidateFillers(backendCandidates, fallbackCandidates)",
    ]

    for text in required_text:
        assert text in UI


def test_flow_and_candidate_address_fonts_are_consistent() -> None:
    required_text = [
        ".flow-node .state-badge",
        ".flow-metric-row span",
        ".flow-detail-line",
        ".flow-node-compact-v2.skipped .flow-metric-row strong",
        ".flow-node-compact-v2.skipped .flow-metric-row span",
        ".copy-address",
        ".copy-value",
        ".copy-icon",
        "font-size: var(--flow-node-font-size);",
        "font-size: 11px;",
    ]

    for text in required_text:
        assert text in UI

    assert ".flow-metric-row strong {\n  overflow: visible;\n  color: var(--text);\n  font-size: 19px;" not in UI
    assert ".flow-node h3,\n.cluster-card h3" not in UI
    assert ".copy-address {\n" in UI


def test_flow_target_chips_use_compact_venue_aliases_to_avoid_clipping() -> None:
    required_text = [
        "const compactVenueAliases = {",
        "QUICKSWAP: \"QSWAP\"",
        "UNISWAP: \"UNI\"",
        "BASESWAP: \"BSWAP\"",
        "const matchedVenue = Object.keys(compactVenueAliases).find",
        "return compactVenueAliases[matchedVenue];",
        ".flow-target-stack",
        "gap: 3px;",
        ".flow-target-chip",
        "flex: 1 1 0;",
    ]

    for text in required_text:
        assert text in UI


def test_flow_monitor_redesign_uses_compact_colored_stage_cards_and_external_summary() -> None:
    required_text = [
        "flow-intel-strip",
        "flowIntelSummary(selectedCandidate, selectedApproval, selectedRouteBlockers)",
        "flowNodeVisual(data)",
        "flow-node-compact-v2",
        "flow-node-top",
        "flow-target-stack",
        "flow-target-chip",
        "flow-metric-row",
        "flow-detail-line",
        "venueTone(",
        "shortVenueName(",
        "className={`flow-target-chip ${visual.sourceTone}`}",
        "className={`flow-target-chip ${visual.targetTone}`}",
        ".flow-target-chip.dex-buy",
        ".flow-target-chip.dex-sell",
        ".flow-target-chip.cex",
        ".flow-detail-line",
        "white-space: normal;",
        "중요 거래소/DEX 색상",
    ]

    for text in required_text:
        assert text in UI


def test_flow_nodes_explain_venue_spread_checks_and_execution_targets() -> None:
    required_text = [
        "감지 출처: QUICKSWAP V2 Polygon pool 0x898386...f6...",
        "비교 대상: UNISWAP V3 Polygon",
        "프리체크: sell quote PASS / liquidity PASS / transfer PASS / pool sync PASS",
        "매수: QUICKSWAP V2 on Polygon / max $1,000 / slippage 1.8%",
        "동일체인 매도: Polygon -> Polygon / UNISWAP V3 bid $86.29",
        "브릿지: Polygon -> Ethereum / bridge quote 대기",
        "타체인 매도: Ethereum DEX / quote 대기",
        "지갑보유: 0x7777777777777777777777777777777777777777 / 매수 후 정지",
        "감지 출처: BASESWAP V2 Base pool 0x22222222...pool",
        "프리체크: quote PASS / transfer PASS / deposit BASE PASS / depth PASS",
        "CEX 입금: UPBIT TEST / Ethereum network / 가상 반영 중",
        "매도: UPBIT TEST/KRW / 가상 매도 / 정산 완료",
        "flowNodeDetail(node, snapshot, candidate)",
        "humanizeBackendDetail",
        "감지 출처",
        "매수:",
        "매도:",
    ]

    for text in required_text:
        assert text in UI


def test_route_arrows_track_elapsed_time_without_overlay_loading_bars() -> None:
    required_text = [
        "TimedEdge",
        "BaseEdge",
        "EdgeLabelRenderer",
        "getStraightPath",
        "const straightRouteEdges = new Set([",
        "function horizontalRoutePath(sourceX, sourceY, targetX)",
        "function elbowRoutePath(sourceX, sourceY, targetX, targetY)",
        "function routeEdgeTimingPosition(",
        "straightRouteEdges.has(id)",
        "M ${sourceX},${sourceY} L ${targetX},${sourceY}",
        "M ${sourceX},${sourceY} L ${midX},${sourceY} L ${midX},${targetY} L ${targetX},${targetY}",
        "route-arrow-track",
        "route-arrow-main",
        "route-arrow-fill",
        "route-arrow-head-fill",
        "const fillMarkerId = `route-arrow-head-fill-${",
        "const fillArrowHeadVisible = state === \"active\" && progress >= 0.94;",
        "markerEnd={fillArrowHeadVisible ? `url(#${fillMarkerId})` : undefined}",
        "edgeProgress(edge.id, edgeState)",
        "pathLength=\"1\"",
        "\"--route-progress\": progress",
        "Math.min(1, elapsed / TEST_FLOW_ACTION_DELAY_MS)",
        "animated: false",
        "route-edge-timing",
        "route-timer",
        "data-route-timer",
        "data: { state: edgeState, label: edgeLabel(edge.id), color: stateColors[edgeState], progress }",
        "flowMapNodes",
        "flowMapEdges",
        "flow-metric-row",
        "signal-precheck",
        "precheck-buy",
        "buy-bridge",
        "buy-wallet-hold",
        "bridge-cross-chain-sell",
        "bridge-cex-deposit",
        "cex-deposit-sell",
        "lineStartedAt",
        "lineTimerIntervals",
        "startLineTimer",
        "finishLineTimer",
        "formatElapsed",
        "진행 중",
        "완료",
        "WATCH TARGET: QUICKSWAP V2 pool 0x89838605f0ed...6b78ef",
        "CHECK RESULT: UNISWAP V3 sell quote PASS / pool 0x019c294b...cf183a",
        "TARGET POOL: BASESWAP V2 pool 0x22222222...pool",
    ]

    for text in required_text:
        assert text in UI

    assert "route-edge-label" not in UI
    assert "route-loading-bar" not in UI
    assert "data-route-loading-bar" not in UI
    assert "route-arrow-progress" not in UI
    assert "strokeWidth: strokeWidth + 2" not in UI
    assert "Math.min(0.96, elapsed / TEST_FLOW_ACTION_DELAY_MS)" not in UI
    assert "routeArrowFlow" not in UI
    assert "stroke-dasharray: 16 20;" not in UI
    assert ".route-line.route-active path" not in UI
    assert ".route-line.route-wait path" not in UI
    assert "stroke-dasharray: 10 8;" not in UI
    assert "stroke-dasharray: 7 8;" not in UI
    assert "animated: edgeState === \"active\"" not in UI
    assert "getSmoothStepPath" not in UI
    assert "borderRadius: 18" not in UI
    assert 'type: "smoothstep"' not in UI


def test_flowmap_uses_requested_buy_bridge_sell_wallet_branching() -> None:
    required_text = [
        "{ id: \"bridge\", title: \"브릿지\"",
        "{ id: \"sameChainSell\", title: \"동일체인 매도\"",
        "{ id: \"walletHold\", title: \"지갑보유\"",
        "{ id: \"crossChainSell\", title: \"타체인 매도\"",
        "{ id: \"cexDeposit\", title: \"CEX 입금\"",
        "{ id: \"cexSell\", title: \"매도\"",
        "\"buy-bridge\"",
        "\"buy-wallet-hold\"",
        "\"bridge-cross-chain-sell\"",
        "\"bridge-cex-deposit\"",
        "\"cex-deposit-sell\"",
        "legacyFlowNodeAliases",
        "legacyFlowEdgeAliases",
        "지갑보유",
        "지갑 주소",
        "Polygon -> Ethereum",
        "입금 거래소",
        "metric: \"건너뜀\"",
        "metricLabel: \"선택 경로 아님\"",
    ]

    for text in required_text:
        assert text in UI

    for old_visible_title in [
        "브릿지 후 DEX 매도",
        "브릿지 후 CEX 입금",
        "직접 CEX 입금",
    ]:
        assert old_visible_title not in DATA_JS

    assert "metric: \"skip\"" not in UI
    assert "metricLabel: \"선택 route 아님\"" not in UI


def test_action_buttons_use_korean_operational_labels() -> None:
    required_text = [
        "실거래",
        "시뮬",
        "수동",
        "자동",
        "매수/매도",
        "지갑 보유",
        "스파이크 %",
        "저장",
        "자동 감지",
        "프라이스 스파이크 감시",
        "className=\"action-card-grid\"",
        "className=\"manual-action-card\"",
        "className=\"auto-action-card\"",
        "className=\"manual-action-buttons\"",
        "className=\"spike-rule-form\"",
        "selectedActionPairLabel",
        "walletAddressForAction",
        "shortWalletForAction",
        "spikeRules",
        "updateSpikeThreshold",
        "saveSpikeRule",
        "data-action-kind=\"real\"",
        "data-action-kind=\"simulation\"",
        "data-action=\"save-spike-rule\"",
        "data-spike-rule-kind=\"real\"",
        "data-spike-rule-kind=\"simulation\"",
        "data-auto-exec-scope=\"price-spike-only\"",
        "data-auto-exec-button=\"price-spike-buy-sell\"",
        "data-action=\"price-spike-auto-execute\"",
        "spike-auto-execute-action",
        "handleCandidateAction(event, selectedCandidate.id, \"simulation-run\")",
        "data-real-action-locked=\"true\"",
        "data-action=\"real-buy-sell\"",
        "data-action=\"real-buy-hold\"",
        "data-sim-action=\"manual\"",
        "data-sim-action=\"hold\"",
        "소액 요청",
        "매수 후 지갑보유: 매수까지만 실행",
        "매수 후 지갑보유 요청",
        "execution_policy: \"buy_then_hold\"",
        "data-mode=\"buy_then_hold\"",
        "wallet-hold-action",
    ]

    for text in required_text:
        assert text in UI

    for text in [
        ">Paper 실행<",
        ">Auto Small Dry Run<",
        ">Live Full Route<",
        ">Live Full 시뮬레이션<",
        ">무자금 성공/실패 검증<",
        ">가상거래 실행<",
        ">소액 자동 모의실행<",
        ">전체 경로 모의실행<",
        ">잠금<",
        ">무자금<",
        ">보유<",
        "실 OFF · 시뮬 ON",
        "data-action=\"real-manual\"",
        "data-action=\"real-auto\"",
        "data-action=\"real-hold\"",
        "data-sim-action=\"auto\"",
        "<span>매수/매도 자동 실행</span>",
        "paper/dry-run/simulated live only",
        "auto_small dry-run 요청",
        "backend paper 실행 요청",
    ]:
        assert text not in UI


def test_all_manual_auto_test_actions_use_slow_visual_flow() -> None:
    required_text = [
        "export const TEST_FLOW_ACTION_DELAY_MS = 5000;",
        "export function testFlowDelay(step) {",
        "delay: testFlowDelay(1)",
        "delay: testFlowDelay(2)",
        "delay: testFlowDelay(3)",
        "delay: testFlowDelay(4)",
        "delay: testFlowDelay(5)",
        "localFlowOverride",
        "setLocalFlowOverride(true)",
        "const usingLocalFlowState = localFlowOverride || usingFallbackData;",
        "startLocalVisualFlow(candidateId",
        "startLocalWalletHoldFlow(candidateId",
        "수동 매수/매도 테스트",
        "자동 매수/매도 테스트",
        "액션당 5초",
        "handleCandidateAction(event, selectedCandidate.id, \"execute\")",
        "handleCandidateAction(event, selectedCandidate.id, \"buy-wallet-hold\")",
        "handleCandidateAction(event, selectedCandidate.id, \"simulation-run\")",
        "data-action=\"price-spike-auto-execute\"",
    ]

    for text in required_text:
        assert text in UI

    for text in [
        "delay: 700",
        "delay: 800",
        "delay: 900",
        "delay: 1500",
        "delay: 1900",
        "delay: 2400",
        "delay: 2800",
        "delay: 3100",
        "delay: 3700",
        "delay: 4600",
        "}, 800));",
        "setSimulationState(\"가상 요청\")",
        "setSimulationState(result.ok ? \"가상 완료\"",
        "setSimulationState(\"선택 후보 시뮬레이션 요청\")",
        "setSimulationState(result.ok ? \"매수 후 지갑보유 완료\"",
    ]:
        assert text not in UI


def test_monitor_ui_uses_backend_snapshot_contract_as_primary_source() -> None:
    required_text = [
        "const API_SNAPSHOT_URL = \"/api/arbitrage/snapshot\";",
        "fetch(`${API_SNAPSHOT_URL}${query}`",
        "normalizeBackendOpportunities(snapshot)",
        "snapshot?.opportunities",
        "selected_opportunity_id",
        "selected_route_id",
        "selected_execution_run",
        "provider_health",
        "provider_jobs",
        "simulation_runs",
        "flow_nodes",
        "flow_edges",
        "transactions",
        "positions",
        "logs",
        "backend snapshot/SSE",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_uses_top_level_selected_route_as_card_fallback() -> None:
    required_text = [
        "const snapshotRoute = snapshotSelected ? (snapshot?.selected_route || {}) : {};",
        "const route = Object.keys(cardRoute).length ? cardRoute : snapshotRoute;",
        "const snapshotRouteId = snapshotSelected ? snapshot?.selected_route_id : 0;",
        "const routeId = Number(card.selected_route_id || route.id || snapshotRouteId || 0);",
    ]

    for text in required_text:
        assert text in UI


def test_vite_dev_server_proxies_arbitrage_api_to_backend() -> None:
    required_text = [
        "defineConfig",
        "react()",
        "ARBITRAGE_API_PROXY_TARGET",
        "\"http://127.0.0.1:8791\"",
        "\"/api/arbitrage\"",
        "\"/health\"",
        "target: apiTarget",
        "changeOrigin: true",
    ]

    for text in required_text:
        assert text in UI

    assert "\"http://127.0.0.1:8788\"" not in UI


def test_single_port_product_entrypoint_serves_api_and_built_monitor() -> None:
    required_text = [
        "\"dev\": \"npm run build && npm run serve:static\"",
        "\"serve\": \"cd .. && ARBITRAGE_API_HOST=${ARBITRAGE_API_HOST:-0.0.0.0} python -m arbitrage.api_server\"",
        "\"preview\": \"npm run build && npm run serve:static\"",
        "ARBITRAGE_API_STATIC_DIR",
        "arbitrage/dist",
    ]

    for text in required_text:
        assert text in UI

    assert "\"dev\": \"vite --host 0.0.0.0\"" not in UI
    assert "vite preview --host 0.0.0.0 --port 8791 --strictPort" not in PACKAGE


def test_vite_preview_config_is_not_the_product_entrypoint() -> None:
    required_text = [
        "preview:",
        "port: 8791",
        "strictPort: true",
        "proxy: {}",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_stream_applies_sequence_events_without_duplicate_logs() -> None:
    required_text = [
        "const API_STREAM_URL = \"/api/arbitrage/stream\";",
        "new EventSource(`${API_STREAM_URL}?after_seq=${lastAppliedSeq.current}`)",
        "SSE_EVENT_TYPES.forEach((eventType) => source.addEventListener(eventType, applySseEvent))",
        "const applySseEvent = useCallback((event) =>",
        "if (seq && seq <= lastAppliedSeq.current) return;",
        "lastAppliedSeq.current = seq;",
        "applySseEventToSnapshot(current, row)",
        "server_time: Number.isFinite(occurredAtMs) && occurredAtMs > 0",
        "appendUniqueEventLog",
        "some((item) => Number(item.seq || 0) === seq)",
        "eventMatchesSelected(row, current)",
        "\"error\"",
        "\"opportunity.upsert\"",
        "\"provider.job.started\"",
        "\"provider.job.completed\"",
        "\"provider.job.failed\"",
        "\"simulation.run.started\"",
        "\"simulation.run.stage\"",
        "\"simulation.run.completed\"",
        "\"simulation.run.failed\"",
        "\"replay_truncated\"",
        "SNAPSHOT_RELOAD_EVENT_TYPES.has(row?.event_type)",
        "shouldReloadSnapshotForEvent(row)",
        "loadSnapshot(row.opportunity_id || snapshot?.selected_opportunity_id || null)",
        "upsertDryRunTransactionFromStepEvent(current.transactions, row)",
        "currentStepFromExecutionSteps(next.execution_steps)",
        "RUN_STATUS_VALUES",
        "payload?.run_status || payload?.status",
        "payload.adapter_status",
        "\"transfer.update\"",
        "\"order.update\"",
        "upsertTransferFromEvent(current.transfers, row)",
        "upsertOrderFromEvent(current.orders, row)",
        "BACKEND_TERMINAL_RUN_STATUSES.has(terminalStatus)",
        "delete next[id];",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_executes_backend_paper_runs_with_ids_only() -> None:
    required_text = [
        "fetch(\"/api/arbitrage/executions\"",
        "opportunity_id: candidate.backendId",
        "route_id: candidate.routeId",
        "mode: \"paper\"",
        "idempotency_key: `ui-paper:${candidate.backendId}:${candidate.routeId}`",
        "requested_by: \"monitor-ui\"",
        "data-mode=\"paper\"",
    ]

    for text in required_text:
        assert text in UI

    forbidden_text = [
        "submitSwap",
        "createOrder",
        "bridgeTransfer",
    ]
    for text in forbidden_text:
        assert text not in UI


def test_monitor_ui_auto_small_dry_run_action_uses_backend_ids_only() -> None:
    required_text = [
        "AUTO_SMALL_DRY_RUN_TRADE_AMOUNT_KRW",
        "DRY_RUN_ONLY_LABEL",
        "data-action=\"auto-small-dry-run\"",
        "data-mode=\"auto_small\"",
        "data-dry-run=\"true\"",
        "data-submit-boundary=\"same-chain-dex-dry-run-no-real-submit\"",
        "data-opportunity-id={candidate.backendId || \"\"}",
        "data-route-id={candidate.routeId || \"\"}",
        "data-route-type={candidate.routeType || candidate.selected_route?.route_type || \"\"}",
        "fetch(\"/api/arbitrage/executions\"",
        "opportunity_id: candidate.backendId",
        "route_id: candidate.routeId",
        "mode: \"auto_small\"",
        "dry_run: true",
        "idempotency_key: `ui-auto-small-dry-run:${candidate.backendId}:${candidate.routeId}`",
        "requested_by: \"monitor-ui\"",
        "trade_amount_krw: AUTO_SMALL_DRY_RUN_TRADE_AMOUNT_KRW",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_disables_auto_small_for_unsupported_or_non_backend_routes() -> None:
    required_text = [
        "autoSmallDryRunState(candidate, status)",
        "Boolean(candidate?.backendOnly && candidate?.backendId && candidate?.routeId)",
        "routeType !== \"same_dex_sell\"",
        "missing_backend_route",
        "route_type_not_supported",
        "blocked_route",
        "routeBlockerList(route).length > 0",
        "disabled={!autoSmallState.enabled}",
        "data-disabled-reason={autoSmallState.reason}",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_renders_auto_small_snapshot_transactions_and_run_state() -> None:
    required_text = [
        "snapshot?.transactions",
        "normalizeTransactionRows(snapshot?.transactions)",
        "transactionEvidenceRows",
        "selectedAutoSmallRunStatus",
        "selectedRun?.mode === \"auto_small\"",
        "snapshot?.selected_execution_run",
        "snapshot?.flow_nodes",
        "snapshot?.flow_edges",
        "snapshot?.logs",
        "snapshot?.positions",
        "dryRunTransactionRefs",
        "className=\"execution-meta-strip\"",
        "data-bottom-tab=\"dryRun\"",
        "id=\"dry-run-transaction-body\"",
        "className=\"dry-run-tx-ref\"",
        "data-dry-run={String(transaction.dryRun)}",
        "MANUAL_REVIEW",
        "RECONCILE",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_reads_approval_snapshot_state_from_backend() -> None:
    required_text = [
        "snapshot?.pending_approvals",
        "pendingApprovalForRoute",
        "approvalStateForCandidate(snapshot, selectedCandidate)",
        "approval_required",
        "approval_status",
        "approval_id",
        "latest_approval",
        "latest_approval_decision",
        "selected_execution_run",
        "selected_route",
        "id=\"selected-approval-state\"",
        "data-approval-status={selectedApproval.approval_status}",
        "data-approval-required={String(selectedApproval.approval_required)}",
        "data-approval-id={selectedApproval.approval_id || \"\"}",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_applies_operator_approval_sse_events_without_duplicate_logs() -> None:
    required_text = [
        "\"operator_approval.requested\"",
        "\"operator_approval.approved\"",
        "\"operator_approval.rejected\"",
        "\"alert.operator_approval_requested\"",
        "APPROVAL_EVENT_TYPES.has(row.event_type)",
        "mergeApprovalFromEvent(next, row)",
        "approvalRecordFromEvent(row)",
        "upsertApproval(pendingApprovals, approval)",
        "upsertApprovalAlert(snapshot.alerts, row)",
        "pendingApprovals.filter((item) => Number(item.id || item.approval_id || 0) !== Number(approval.id))",
        "appendUniqueEventLog(current.logs, row)",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_cards_send_approval_and_one_click_payload_ids() -> None:
    required_text = [
        "data-action=\"approval-request\"",
        "data-action=\"approval-approve\"",
        "data-action=\"approval-reject\"",
        "data-action=\"one-click-held\"",
        "data-mode=\"one_click\"",
        "data-submit-boundary=\"held-no-submit\"",
        "data-opportunity-id={candidate.backendId || \"\"}",
        "data-route-id={candidate.routeId || \"\"}",
        "data-approval-id={approvalId}",
        "disabled={!hasBackendRoute}",
        "disabled={!canDecideApproval}",
        "fetch(\"/api/arbitrage/approvals\"",
        "fetch(`/api/arbitrage/approvals/${approvalId}/${decision}`",
        "fetch(\"/api/arbitrage/executions\"",
        "opportunity_id: candidate.backendId",
        "route_id: candidate.routeId",
        "mode: \"one_click\"",
        "approval_key: `operator_approval:${candidate.backendId}:${candidate.routeId}:none:one_click`",
        "idempotency_key: `ui-one-click-held:${candidate.backendId}:${candidate.routeId}`",
        "approval_id: approvalId",
        "ONE_CLICK_HELD_TRADE_AMOUNT_KRW",
        "trade_amount_krw: ONE_CLICK_HELD_TRADE_AMOUNT_KRW",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_exposes_guarded_live_full_route_action_with_backend_payload() -> None:
    required_text = [
        "LIVE_FULL_TRADE_AMOUNT_KRW",
        "LIVE_FULL_PROVIDER_BOUNDARY",
        "LIVE_FULL_ROUTE_BOUNDARY_LABEL",
        "const LIVE_FULL_ROUTE_TYPES = new Set([\"direct_cex_sell\", \"bridge_dex_sell\", \"bridge_cex_sell\"]);",
        "liveFullRouteState(candidate, status)",
        "liveFullApprovalMatches(candidate)",
        "route_type_not_supported",
        "operator_approval_required",
        "data-action=\"live-full-route\"",
        "data-mode=\"live_full\"",
        "data-simulated-boundary=\"true\"",
        "data-provider-boundary={LIVE_FULL_PROVIDER_BOUNDARY}",
        "data-cex-withdrawal-enabled=\"false\"",
        "disabled={!liveFullState.enabled}",
        "data-disabled-reason={liveFullState.reason}",
        "fetch(\"/api/arbitrage/executions\"",
        "opportunity_id: candidate.backendId",
        "route_id: candidate.routeId",
        "mode: \"live_full\"",
        "idempotency_key: `ui-live-full:${candidate.backendId}:${candidate.routeId}:${LIVE_FULL_TRADE_AMOUNT_KRW}`",
        "requested_by: \"monitor-ui\"",
        "trade_amount_krw: LIVE_FULL_TRADE_AMOUNT_KRW",
        "simulated: true",
        "provider_boundary: LIVE_FULL_PROVIDER_BOUNDARY",
        "live_full_boundary_ack: true",
        "cex_withdrawal_enabled: false",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_shows_live_full_route_readiness_and_approval_controls() -> None:
    required_text = [
        "className=\"live-route-panel\"",
        "data-live-full-route-ready={candidate.backendOnly ? String(liveFullState.enabled) : \"\"}",
        "data-live-full-disabled-reason={candidate.backendOnly ? liveFullState.reason : \"\"}",
        "data-live-full-route-type={candidate.routeType || \"\"}",
        "data-live-full-approval-status={route.live_full_approval_status || \"MISSING\"}",
        "deposit_network",
        "bridge_status",
        "cex_deposit",
        "cex_market",
        "liveFullState.blockers.join(\" · \")",
        "data-live-full-blocker-list={liveFullState.blockers.join(\",\")}",
        "data-action=\"live-full-approval-request\"",
        "data-action=\"live-full-approval-approve\"",
        "mode: \"live_full\"",
        "approval_key: `operator_approval:${candidate.backendId}:${candidate.routeId}:none:live_full:${LIVE_FULL_TRADE_AMOUNT_KRW}`",
        "expires_at_ms: Date.now() + 600000",
        "liveFullApprovalIdForCandidate(candidate)",
        "fetch(`/api/arbitrage/approvals/${approvalId}/${decision}`",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_renders_live_full_orders_transfers_refs_and_boundaries() -> None:
    required_text = [
        "snapshot?.orders",
        "snapshot?.transfers",
        "normalizeOrderRows(snapshot?.orders)",
        "normalizeTransferRows(snapshot?.transfers)",
        "transferEvidenceRows",
        "orderEvidenceRows",
        "data-order-source=\"snapshot?.orders\"",
        "data-transfer-source=\"snapshot?.transfers\"",
        "data-live-full-boundary-source=\"snapshot?.live_full_boundary\"",
        "data-bottom-tab=\"liveRefs\"",
        "id=\"transfer-ref-body\"",
        "id=\"order-ref-body\"",
        "bridge/deposit ref",
        "order ref",
        "data-cex-withdrawal={String(transfer.cexWithdrawal)}",
        "data-cex-withdrawal={String(order.cexWithdrawal)}",
        "external_refs",
        "ref ${ref}",
        "MANUAL_REVIEW",
        "RECONCILE",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_has_no_primary_dummy_approval_state_or_real_submit_mode_usage() -> None:
    assert "approval_status" not in DATA_JS
    assert "approval_required" not in DATA_JS
    assert "approval_id" not in DATA_JS

    forbidden_text = [
        "submitSwap",
        "createOrder",
        "bridgeTransfer",
        "wallet.sign",
        "eth_sendRawTransaction",
    ]
    for text in forbidden_text:
        assert text not in UI


def test_approval_state_labels_do_not_share_route_bar_canvas_space() -> None:
    required_text = [
        "grid-template-columns: repeat(4, minmax(0, 1fr));",
        "approval-monitor-item",
        ".approval-panel",
        ".approval-actions",
        "flow-map",
    ]

    for text in required_text:
        assert text in UI

    assert ".route-edge-label" not in UI


def test_monitor_ui_backend_flow_nodes_edges_and_duration_labels_are_rendered() -> None:
    required_text = [
        "backendNodeStateMap",
        "backendEdgeStateMap",
        "미사용 분기",
        "status: \"미사용\"",
        "duration_ms",
        "started_at_ms",
        "completed_at_ms",
        "route_id",
        "run_id",
        "edgeLabel(edge.id)",
        "animated: false",
        "route {candidate.routeId || candidate.selected_route_id || \"demo\"}",
        "flowMapEdges",
        "flow-metric-row",
        "backendNodeStateMap(snapshot, selectedCandidate)",
    ]

    for text in required_text:
        assert text in UI


def test_frontend_demo_data_is_only_api_unavailable_fallback() -> None:
    required_text = [
        "const FRONTEND_DEMO_FALLBACK_ONLY = true;",
        "const usingFallbackData = FRONTEND_DEMO_FALLBACK_ONLY && !usingBackendSnapshot && apiState === \"fallback\";",
        "const usingLocalFlowState = localFlowOverride || usingFallbackData;",
        "const opportunityList = usingBackendSnapshot ? buildTestCandidateFillers(backendCandidates, fallbackCandidates) : (usingFallbackData ? fallbackCandidates : []);",
        "const selectedCandidate = candidateMap[selectedCandidateId] || opportunityList[0] || (usingFallbackData ? fallbackInitialCandidate : EMPTY_MONITOR_CANDIDATE);",
        "const displaySelectedStep = usingLocalFlowState",
        "? selectedStep",
        ": (usingBackendSnapshot ? backendSelectedStep : EMPTY_MONITOR_CANDIDATE.initialStep);",
        "const dataSourceLabel = usingBackendSnapshot",
        "backend snapshot empty/no candidates",
        "EMPTY_MONITOR_CANDIDATE",
        "백엔드 후보 없음",
        "선택된 아비트라지 없음",
        "OFFLINE DEMO frontend fallback-only",
        "offline-demo-banner",
        "data-offline-demo=\"true\"",
        "data-source={candidate.testOnly ? \"test\" : (candidate.backendOnly ? \"backend\" : \"fallback\")}",
        "data-empty-opportunities=\"true\"",
    ]

    for text in required_text:
        assert text in UI


def test_monitor_ui_renders_provider_jobs_simulation_runs_and_exact_failures() -> None:
    required_text = [
        "normalizeProviderJobRows(snapshot?.provider_jobs, providerHealth)",
        "normalizeSimulationRunRows(snapshot?.simulation_runs)",
        "data-provider-job-source=\"snapshot?.provider_jobs\"",
        "data-simulation-run-source=\"snapshot?.simulation_runs\"",
        "id=\"provider-job-body\"",
        "id=\"simulation-run-body\"",
        "API 상세 상태",
        "error_code / blockers",
        "actionFailureMessage(",
        "resultBlockers(result)",
        "result?.simulation_run?.error_code",
        "result?.simulation_run?.payload?.blockers",
        "\"missing_error_code\"",
        "blockers ${blockers.length ? blockers.join(\",\") : \"none\"}",
        "backendExecutionState",
        "backend snapshot/SSE seq ${lastSeq || snapshot?.snapshot_seq || 0} · ${backendExecutionState}",
        "result.ok && result.status === \"COMPLETED\"",
    ]

    for text in required_text:
        assert text in UI

    assert "실행 실패: ${result.error_code || result.run?.error_code || \"unknown\"}" not in UI


def test_backend_candidate_status_uses_local_state_only_for_pending() -> None:
    required_text = [
        "const BACKEND_PENDING_STATUSES = new Set([\"요청중\", \"진행중\", \"보류 준비\"]);",
        "function candidateDisplayStatus(candidate, candidateStatuses)",
        "if (candidate.backendOnly)",
        "BACKEND_PENDING_STATUSES.has(localStatus) ? localStatus : candidate.defaultStatus",
        "candidateDisplayStatus(candidate, candidateStatuses)",
        "delete next[id];",
    ]

    for text in required_text:
        assert text in UI
