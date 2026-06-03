const DATA_URL = "data/latest.json";
const STORAGE_KEY = "a-share-maintrend-trades-v1";

const state = {
  data: null,
  trades: [],
  liveQuotes: new Map(),
  isRefreshing: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  loadTrades();
  await loadData();
  await refreshPositionQuotes();
  render();
  refreshIcons();
});

function bindEvents() {
  $("#refreshButton").addEventListener("click", refreshLatestData);
  $("#notifyButton").addEventListener("click", requestNotifications);
  $("#openTradeButton").addEventListener("click", () => openTradeModal());
  $("#closeTradeModal").addEventListener("click", closeTradeModal);
  $("#cancelTrade").addEventListener("click", closeTradeModal);
  $("#tradeModal").addEventListener("click", (event) => {
    if (event.target.id === "tradeModal") closeTradeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#tradeModal").hidden) closeTradeModal();
  });
  $("#tradeForm").addEventListener("submit", saveTradeFromForm);
  $("#clearClosedButton").addEventListener("click", clearClosedTrades);
  $("#rulesToggle").addEventListener("click", toggleRules);
  $$(".segment").forEach((button) => {
    button.addEventListener("click", () => switchRecommendationView(button.dataset.view));
  });
}

async function refreshLatestData() {
  if (state.isRefreshing) return;
  state.isRefreshing = true;
  const button = $("#refreshButton");
  const hint = $("#refreshHint");
  const previousGeneratedAt = state.data?.meta?.generatedAt || null;
  button.disabled = true;
  hint.textContent = "正在读取 GitHub Pages 上最新的 data/latest.json，不会影响本地交易记录。";

  await loadData();
  state.liveQuotes.clear();
  await refreshPositionQuotes();
  render();

  const nextGeneratedAt = state.data?.meta?.generatedAt || null;
  const checkedAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  hint.textContent =
    nextGeneratedAt && nextGeneratedAt !== previousGeneratedAt
      ? `已读取到新数据，检查时间 ${checkedAt}。`
      : `已检查最新文件，检查时间 ${checkedAt}。如果“数据生成时间”没变，说明 GitHub Actions 还没有生成新数据。`;
  button.disabled = false;
  state.isRefreshing = false;
  refreshIcons();
}

async function loadData() {
  try {
    const response = await fetch(`${DATA_URL}?ts=${Date.now()}`, {
      cache: "no-store",
      headers: { "Cache-Control": "no-cache" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
  } catch (error) {
    state.data = fallbackData(error);
  }
}

function fallbackData(error) {
  return {
    meta: {
      generatedAt: null,
      mode: "本地预览",
      errors: [`无法读取 data/latest.json：${error.message}`],
      sourceHealth: [],
    },
    market: { recommendationCount: 0, qualifiedBoardCount: 0 },
    boards: [],
    recommendations: [],
    news: [],
  };
}

function render() {
  renderStatus();
  renderRecommendations();
  renderRecommendationTable();
  renderSectors();
  renderPositions();
  renderNews();
  renderSources();
  refreshIcons();
}

function renderStatus() {
  const meta = state.data?.meta || {};
  const generatedAt = meta.generatedAt ? new Date(meta.generatedAt) : null;
  $("#updatedAt").textContent = generatedAt
    ? generatedAt.toLocaleString("zh-CN", { hour12: false })
    : "--";
  $("#updateMode").textContent = meta.mode || "等待数据";
  $("#recommendationCount").textContent = state.data?.recommendations?.length || 0;
  $("#sourceCount").textContent = (meta.sourceHealth || []).filter((source) => source.ok).length;
  $("#alertCount").textContent = getPositionAlerts().length;
}

function renderRecommendations() {
  const root = $("#recommendationCards");
  const recommendations = state.data?.recommendations || [];
  if (!recommendations.length) {
    root.innerHTML = `
      <div class="empty-state">
        <strong>今日暂无严格达标推荐。</strong>
        <p>系统没有找到同时满足“主升板块4条以上 + 个股主升5条以上”的标的。按 sk 纪律，今天可空仓等待。</p>
      </div>
    `;
    return;
  }

  root.innerHTML = recommendations.map(renderRecommendationCard).join("");
  root.querySelectorAll("[data-action='buy']").forEach((button) => {
    button.addEventListener("click", () => {
      const recommendation = recommendations.find((item) => item.code === button.dataset.code);
      openTradeModal(recommendation);
    });
  });
  drawSparklines();
}

function renderRecommendationCard(item, index) {
  const rank = item.rank || index + 1;
  const criteria = [...(item.criteria?.board || []), ...(item.criteria?.stock || [])].slice(0, 10);
  const links = item.sourceLinks || [];
  const targetPrice = item.sellPlan?.targetPrice ?? item.sellPlan?.takeProfit;
  const targetTime = item.sellPlan?.targetTime ?? item.sellPlan?.timeWindow ?? "--";
  const stopLoss = item.stopPlan?.stopLoss ?? item.sellPlan?.stopLoss;

  return `
    <article class="recommendation-card">
      <div class="card-top">
        <div class="rank-badge">#${rank}</div>
        <div>
          <h3 class="stock-name">${escapeHtml(item.name)} <span class="stock-code">${item.code}</span></h3>
          <div class="stock-code">${escapeHtml(item.board?.name || "未分组")} · ${formatPct(item.pct)} · 换手 ${formatPct(item.turnover, false)}</div>
        </div>
        <span class="score-pill">胜率 ${Math.round(item.winRate || item.confidence || 0)}%</span>
      </div>
      <div class="card-body">
        <div>
          <div class="plan-grid plan-grid-four">
            <div class="plan-box">
              <span>买点</span>
              <strong>${escapeHtml(item.buyPlan?.type || "--")}</strong>
              <div class="stock-code">${escapeHtml(item.buyPlan?.timeWindow || "--")}</div>
            </div>
            <div class="plan-box">
              <span>买入区间</span>
              <strong>${formatRange(item.buyPlan?.priceRange)}</strong>
              <div class="stock-code">${escapeHtml(item.buyPlan?.trigger || "")}</div>
            </div>
            <div class="plan-box">
              <span>预估峰值</span>
              <strong>${formatPrice(targetPrice)}</strong>
              <div class="stock-code">${escapeHtml(targetTime)}</div>
            </div>
            <div class="plan-box stop-box">
              <span>止损价</span>
              <strong>${formatPrice(stopLoss)}</strong>
              <div class="stock-code">分时/结构触发即走</div>
            </div>
          </div>
          <div class="criteria-list">
            ${criteria.map((criterion) => `<span class="tag pass">${escapeHtml(criterion)}</span>`).join("")}
          </div>
          <div class="source-links">
            ${links.map((link) => `<a class="tag" href="${link.url}" target="_blank" rel="noopener">${escapeHtml(link.name)}</a>`).join("")}
          </div>
        </div>
        <canvas class="sparkline" data-code="${item.code}" width="420" height="152"></canvas>
      </div>
      <div class="card-actions">
        <button class="ghost-button" type="button" data-action="buy" data-code="${item.code}">
          <i data-lucide="square-pen"></i>
          记为买入
        </button>
      </div>
    </article>
  `;
}

function renderRecommendationTable() {
  const rows = state.data?.recommendations || [];
  const body = $("#recommendationTable");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8" class="table-empty">暂无严格达标推荐</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((item, index) => {
      const targetPrice = item.sellPlan?.targetPrice ?? item.sellPlan?.takeProfit;
      const targetTime = item.sellPlan?.targetTime ?? item.sellPlan?.timeWindow ?? "--";
      const stopLoss = item.stopPlan?.stopLoss ?? item.sellPlan?.stopLoss;
      return `
        <tr>
          <td class="col-rank">#${item.rank || index + 1}</td>
          <td class="col-stock"><strong>${escapeHtml(item.name)}</strong><span>${item.code}</span></td>
          <td class="col-score">${Math.round(item.winRate || item.confidence || 0)}%</td>
          <td class="col-plan">${escapeHtml(item.buyPlan?.type || "--")}<span>${escapeHtml(item.buyPlan?.timeWindow || "")}</span></td>
          <td class="col-price">${formatPrice(targetPrice)}</td>
          <td class="col-time">${escapeHtml(targetTime)}</td>
          <td class="col-price">${formatPrice(stopLoss)}</td>
          <td class="col-board">${escapeHtml(item.board?.name || "--")}</td>
        </tr>
      `;
    })
    .join("");
}

function renderSectors() {
  const root = $("#sectorList");
  const boards = state.data?.boards || [];
  if (!boards.length) {
    root.innerHTML = `<div class="empty-state">暂无板块数据。等待 GitHub Action 完成首次更新。</div>`;
    return;
  }
  root.innerHTML = boards
    .slice(0, 8)
    .map((board) => {
      const score = Math.round(board.score || 0);
      return `
        <article class="sector-item">
          <div class="sector-head">
            <div>
              <strong>${escapeHtml(board.name)}</strong>
              <div class="stock-code">${formatPct(board.pct)} · 涨停 ${board.limitUpCount || 0} · 大涨 ${board.bigUpCount || 0}</div>
            </div>
            <span class="tag ${board.qualified ? "pass" : "warn"}">${board.passed || 0}/5</span>
          </div>
          <div class="progress-track"><div class="progress-bar" style="width:${Math.min(100, score)}%"></div></div>
        </article>
      `;
    })
    .join("");
}

function renderPositions() {
  const root = $("#positionsList");
  if (!state.trades.length) {
    root.innerHTML = `<div class="empty-state">还没有交易记录。点击“记录交易”后，页面会用本地浏览器记忆并按卖点监控。</div>`;
    return;
  }

  root.innerHTML = state.trades.map(renderPositionItem).join("");
  root.querySelectorAll("[data-action='edit']").forEach((button) => {
    const trade = state.trades.find((item) => item.id === button.dataset.id);
    button.addEventListener("click", () => openTradeModal(null, trade));
  });
  root.querySelectorAll("[data-action='sell']").forEach((button) => {
    button.addEventListener("click", () => markTradeSold(button.dataset.id));
  });
  root.querySelectorAll("[data-action='delete']").forEach((button) => {
    button.addEventListener("click", () => deleteTrade(button.dataset.id));
  });
}

function renderPositionItem(trade) {
  const quote = getQuoteForTrade(trade);
  const currentPrice = quote?.price || trade.lastPrice || trade.buyPrice;
  const pnlPct = ((currentPrice - trade.buyPrice) / trade.buyPrice) * 100;
  const alert = getAlertForTrade(trade, currentPrice);
  const statusClass = trade.status === "sold" ? "warn" : alert ? "alert-pill" : "pass";

  return `
    <article class="position-item">
      <div class="position-head">
        <div>
          <strong>${escapeHtml(trade.name)} <span class="stock-code">${trade.code}</span></strong>
          <div class="stock-code">${escapeHtml(trade.note || "未填写备注")}</div>
        </div>
        <span class="${alert ? "alert-pill" : `tag ${statusClass}`}">${trade.status === "sold" ? "已卖出" : alert || "持仓中"}</span>
      </div>
      <div class="position-metrics">
        <div><span class="detail-label">现价</span><strong>${formatPrice(currentPrice)}</strong></div>
        <div><span class="detail-label">浮盈亏</span><strong class="${pnlPct >= 0 ? "positive" : "negative"}">${formatPct(pnlPct, false)}</strong></div>
        <div><span class="detail-label">止损/卖点</span><strong>${formatPrice(trade.stopLoss)} / ${formatPrice(trade.takeProfit)}</strong></div>
      </div>
      <div class="position-actions">
        <button class="ghost-button" type="button" data-action="edit" data-id="${trade.id}">
          <i data-lucide="pencil"></i>
          编辑
        </button>
        <button class="ghost-button" type="button" data-action="sell" data-id="${trade.id}">
          <i data-lucide="circle-check"></i>
          记为卖出
        </button>
        <button class="ghost-button" type="button" data-action="delete" data-id="${trade.id}">
          <i data-lucide="trash-2"></i>
          删除
        </button>
      </div>
    </article>
  `;
}

function renderNews() {
  const root = $("#newsList");
  const news = state.data?.news || [];
  if (!news.length) {
    root.innerHTML = `<div class="empty-state">暂无新闻线索。数据脚本会优先抓取东方财富、同花顺、第一财经。</div>`;
    return;
  }
  root.innerHTML = news
    .slice(0, 8)
    .map(
      (item) => `
      <article class="news-item">
        <span>${escapeHtml(item.source || "来源")}</span>
        <strong><a href="${item.url}" target="_blank" rel="noopener">${escapeHtml(item.title)}</a></strong>
        <time>${escapeHtml(item.time || "")}</time>
      </article>
    `,
    )
    .join("");
}

function renderSources() {
  const root = $("#sourceList");
  const sources = state.data?.meta?.sourceHealth || [];
  if (!sources.length) {
    root.innerHTML = `<div class="empty-state">暂无来源状态。</div>`;
    return;
  }
  root.innerHTML = sources
    .map(
      (source) => `
      <article class="source-item ${source.ok ? "ok" : "warn"}">
        <div class="source-head">
          <strong>${escapeHtml(source.name)}</strong>
          <span class="tag ${source.ok ? "pass" : "warn"}">${source.ok ? "正常" : "异常"}</span>
        </div>
        <div class="stock-code">${escapeHtml(source.note || source.url || "")}</div>
      </article>
    `,
    )
    .join("");
}

function switchRecommendationView(view) {
  $$(".segment").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $("#recommendationCards").classList.toggle("hidden", view !== "cards");
  $("#recommendationTableWrap").classList.toggle("hidden", view !== "table");
}

function toggleRules() {
  const content = $("#rulesContent");
  const button = $("#rulesToggle");
  const isOpen = button.getAttribute("aria-expanded") === "true";
  button.setAttribute("aria-expanded", String(!isOpen));
  content.hidden = isOpen;
}

function openTradeModal(recommendation = null, trade = null) {
  const modal = $("#tradeModal");
  const targetPrice = recommendation?.sellPlan?.targetPrice ?? recommendation?.sellPlan?.takeProfit;
  const stopLoss = recommendation?.stopPlan?.stopLoss ?? recommendation?.sellPlan?.stopLoss;
  $("#tradeId").value = trade?.id || "";
  $("#tradeCode").value = trade?.code || recommendation?.code || "";
  $("#tradeName").value = trade?.name || recommendation?.name || "";
  $("#tradeBuyPrice").value = valueForInput(trade?.buyPrice || recommendation?.buyPlan?.priceRange?.[0] || recommendation?.price);
  $("#tradeQuantity").value = valueForInput(trade?.quantity);
  $("#tradeStopLoss").value = valueForInput(trade?.stopLoss || stopLoss);
  $("#tradeTakeProfit").value = valueForInput(trade?.takeProfit || targetPrice);
  $("#tradeNote").value = trade?.note || buildRecommendationNote(recommendation);
  modal.hidden = false;
  $("#tradeCode").focus();
}

function closeTradeModal() {
  $("#tradeModal").hidden = true;
  $("#tradeForm").reset();
}

function saveTradeFromForm(event) {
  event.preventDefault();
  const existingId = $("#tradeId").value;
  const existing = state.trades.find((item) => item.id === existingId);
  const buyPrice = Number($("#tradeBuyPrice").value);
  const stopLoss = Number($("#tradeStopLoss").value) || round2(buyPrice * 0.95);
  const trade = {
    id: existingId || createId(),
    code: $("#tradeCode").value.trim(),
    name: $("#tradeName").value.trim(),
    buyPrice,
    quantity: Number($("#tradeQuantity").value) || null,
    stopLoss,
    takeProfit: Number($("#tradeTakeProfit").value) || null,
    note: $("#tradeNote").value.trim(),
    status: existing?.status || "open",
    createdAt: existing?.createdAt || new Date().toISOString(),
  };
  const index = state.trades.findIndex((item) => item.id === trade.id);
  if (index >= 0) state.trades[index] = { ...state.trades[index], ...trade };
  else state.trades.unshift(trade);
  persistTrades();
  closeTradeModal();
  refreshPositionQuotes().then(render);
}

function buildRecommendationNote(recommendation) {
  if (!recommendation) return "";
  const buyType = recommendation.buyPlan?.type || "计划买点";
  const board = recommendation.board?.name || "主升板块";
  const targetTime = recommendation.sellPlan?.targetTime || recommendation.sellPlan?.timeWindow || "";
  return `${board}；${buyType}；预估时间 ${targetTime}`;
}

function loadTrades() {
  try {
    state.trades = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  } catch {
    state.trades = [];
  }
}

function persistTrades() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.trades));
}

function markTradeSold(id) {
  const trade = state.trades.find((item) => item.id === id);
  if (!trade) return;
  trade.status = "sold";
  trade.soldAt = new Date().toISOString();
  persistTrades();
  render();
}

function deleteTrade(id) {
  state.trades = state.trades.filter((item) => item.id !== id);
  persistTrades();
  render();
}

function clearClosedTrades() {
  state.trades = state.trades.filter((item) => item.status !== "sold");
  persistTrades();
  render();
}

async function refreshPositionQuotes() {
  const openCodes = state.trades.filter((trade) => trade.status !== "sold").map((trade) => trade.code);
  await Promise.all(openCodes.map(fetchLiveQuote).slice(0, 20));
  const alerts = getPositionAlerts();
  if (alerts.length) notifyAlerts(alerts);
}

async function fetchLiveQuote(code) {
  if (!code || state.liveQuotes.has(code)) return state.liveQuotes.get(code);
  const secid = getSecid(code);
  const url = `https://push2.eastmoney.com/api/qt/stock/get?secid=${secid}&fields=f43,f44,f45,f46,f47,f48,f49,f57,f58,f60,f170`;
  try {
    const data = await jsonp(url);
    const item = data?.data;
    if (!item) return null;
    const quote = {
      code,
      price: normalizeEastMoneyPrice(item.f43),
      pct: normalizeEastMoneyPrice(item.f170),
      name: item.f58,
    };
    state.liveQuotes.set(code, quote);
    return quote;
  } catch {
    return null;
  }
}

function jsonp(url) {
  return new Promise((resolve, reject) => {
    const callbackName = `jsonp_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    const script = document.createElement("script");
    const separator = url.includes("?") ? "&" : "?";
    script.src = `${url}${separator}cb=${callbackName}`;
    script.async = true;
    window[callbackName] = (payload) => {
      delete window[callbackName];
      script.remove();
      resolve(payload);
    };
    script.onerror = () => {
      delete window[callbackName];
      script.remove();
      reject(new Error("JSONP failed"));
    };
    document.body.appendChild(script);
  });
}

function getPositionAlerts() {
  return state.trades
    .filter((trade) => trade.status !== "sold")
    .map((trade) => {
      const quote = getQuoteForTrade(trade);
      const current = quote?.price || trade.lastPrice || trade.buyPrice;
      const alert = getAlertForTrade(trade, current);
      return alert ? { trade, current, alert } : null;
    })
    .filter(Boolean);
}

function getAlertForTrade(trade, currentPrice) {
  if (trade.status === "sold") return "";
  if (trade.stopLoss && currentPrice <= trade.stopLoss) return "到止损";
  if (trade.takeProfit && currentPrice >= trade.takeProfit) return "到卖点";
  if (currentPrice <= trade.buyPrice * 0.95) return "亏损5%";
  return "";
}

function getQuoteForTrade(trade) {
  return state.liveQuotes.get(trade.code) || findRecommendationQuote(trade.code);
}

function findRecommendationQuote(code) {
  const item = (state.data?.recommendations || []).find((recommendation) => recommendation.code === code);
  return item ? { price: item.price, pct: item.pct, name: item.name } : null;
}

function requestNotifications() {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") {
    notifyAlerts(getPositionAlerts());
    return;
  }
  Notification.requestPermission().then((permission) => {
    if (permission === "granted") notifyAlerts(getPositionAlerts());
  });
}

function notifyAlerts(alerts) {
  if (!alerts.length || !("Notification" in window) || Notification.permission !== "granted") return;
  alerts.slice(0, 3).forEach(({ trade, current, alert }) => {
    new Notification(`${trade.name} ${alert}`, {
      body: `现价 ${formatPrice(current)}，买入 ${formatPrice(trade.buyPrice)}`,
      tag: `${trade.id}-${alert}`,
    });
  });
}

function drawSparklines() {
  const recommendations = state.data?.recommendations || [];
  $$(".sparkline").forEach((canvas) => {
    const item = recommendations.find((recommendation) => recommendation.code === canvas.dataset.code);
    const values = item?.sparkline || [];
    drawSparkline(canvas, values);
  });
}

function drawSparkline(canvas, values) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = "#dce4df";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, height * 0.68);
  ctx.lineTo(width, height * 0.68);
  ctx.stroke();
  if (!values.length) {
    ctx.fillStyle = "#63706a";
    ctx.font = "22px sans-serif";
    ctx.fillText("等待K线", 22, 72);
    return;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  ctx.strokeStyle = values[values.length - 1] >= values[0] ? "#c43f3f" : "#197a55";
  ctx.lineWidth = 4;
  ctx.lineJoin = "round";
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = (index / Math.max(1, values.length - 1)) * (width - 28) + 14;
    const y = height - 18 - ((value - min) / span) * (height - 36);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function getSecid(code) {
  if (/^(6|9)/.test(code)) return `1.${code}`;
  return `0.${code}`;
}

function normalizeEastMoneyPrice(value) {
  if (value === undefined || value === null || value === "-") return null;
  return Number(value) / 100;
}

function valueForInput(value) {
  return value === undefined || value === null || Number.isNaN(Number(value)) ? "" : String(value);
}

function formatRange(range) {
  if (!Array.isArray(range) || range.length < 2) return "--";
  return `${formatPrice(range[0])} - ${formatPrice(range[1])}`;
}

function formatPrice(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(2);
}

function formatPct(value, signed = true) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "--";
  const prefix = signed && Number(value) > 0 ? "+" : "";
  return `${prefix}${Number(value).toFixed(2)}%`;
}

function round2(value) {
  return Math.round(Number(value) * 100) / 100;
}

function createId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `trade-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}
