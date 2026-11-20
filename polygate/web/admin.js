(() => {
  "use strict";

  const PAGE_TITLES = {
    overview: "总览",
    routing: "路由",
    usage: "用量与成本",
    requests: "请求",
    diagnostics: "诊断",
    settings: "设置",
  };

  const PAGE_LOADERS = {
    overview: loadOverview,
    routing: loadRouting,
    usage: loadUsage,
    requests: loadRequests,
    diagnostics: loadDiagnostics,
    settings: loadSettings,
  };

  const state = {
    page: null,
    key: localStorage.getItem("polygate_key") || "change-me",
    loading: new Set(),
    refreshTimer: null,
    historyDays: 7,
    requestOffset: 0,
    requestLimit: 50,
    requestFilters: { model: "", client: "", status: "" },
    activeRequestId: "",
    activeLogicalId: "",
    editingClientKeyId: "",
    clientKeys: [],
    editingModelName: "",
    validatedModelSignature: "",
    status: null,
    config: null,
    uiState: {},
    toastTimer: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const integer = new Intl.NumberFormat("zh-CN");
  const dateTime = new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  function formatInteger(value) {
    return integer.format(Number(value || 0));
  }

  function formatCost(value) {
    if (value == null || Number.isNaN(Number(value))) return "—";
    const number = Number(value);
    if (number === 0) return "0";
    if (Math.abs(number) < 0.01) return number.toFixed(6);
    if (Math.abs(number) < 1) return number.toFixed(4);
    return number.toFixed(2);
  }

  function formatLatency(value) {
    return value == null ? "—" : `${Number(value).toFixed(value < 10 ? 1 : 0)} ms`;
  }

  function formatTimestamp(value) {
    if (!value) return "—";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.getTime()) ? "—" : dateTime.format(date);
  }

  function formatDateInput(value) {
    if (!value) return "";
    const date = new Date(Number(value) * 1000);
    if (Number.isNaN(date.getTime())) return "";
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${date.getFullYear()}-${month}-${day}`;
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds || 0)));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (days) return `${days} 天 ${hours} 小时`;
    if (hours) return `${hours} 小时 ${minutes} 分`;
    return `${minutes} 分`;
  }

  function statusClass(value) {
    if (value === "closed" || value === "ok" || value === true) return "ok";
    if (value === "half_open" || value === "warn") return "warn";
    return "error";
  }

  function providerState(value) {
    return ({ closed: "正常", half_open: "半开", open: "熔断" })[value] || value || "未知";
  }

  function stateMarkup(value, label) {
    return `<span class="state ${statusClass(value)}"><span class="status-dot" aria-hidden="true"></span>${escapeHtml(label)}</span>`;
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.add("visible");
    window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 2200);
  }

  function setConnection(kind, text) {
    const node = $("#connection");
    node.classList.remove("connected", "error", "pending");
    if (kind) node.classList.add(kind);
    $(".connection-text", node).textContent = text;
  }

  function setNavOpen(open, { restoreFocus = false } = {}) {
    document.body.classList.toggle("nav-open", open);
    $('[data-action="open-nav"]')?.setAttribute("aria-expanded", String(open));
    if (open) {
      window.requestAnimationFrame(() => $("#sidebar [data-page]")?.focus());
    } else if (restoreFocus) {
      $('[data-action="open-nav"]')?.focus();
    }
  }

  function errorMessage(payload, fallback) {
    if (payload?.error?.message) return payload.error.message;
    if (typeof payload?.detail === "string") return payload.detail;
    if (typeof payload?.message === "string") return payload.message;
    return fallback;
  }

  async function api(path, options = {}) {
    const headers = { Authorization: `Bearer ${state.key}` };
    const init = { method: options.method || "GET", headers };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, init);
    if (!response.ok) {
      let payload = null;
      const text = await response.text();
      try { payload = text ? JSON.parse(text) : null; } catch { payload = null; }
      const message = errorMessage(payload, `HTTP ${response.status}`);
      const isAuth = response.status === 401 || response.status === 403;
      setConnection("error", isAuth ? "密钥无效" : "连接异常");
      if (isAuth && !$("#key-dialog").open) {
        window.setTimeout(() => openKeyDialog("密钥无效"), 0);
      }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    setConnection("connected", "已同步");
    if (response.status === 204) return null;
    const text = await response.text();
    return text ? JSON.parse(text) : null;
  }

  async function safe(promise) {
    try {
      return { ok: true, data: await promise, error: null };
    } catch (error) {
      return { ok: false, data: null, error };
    }
  }

  function problemText(error) {
    return error?.message || "请求失败";
  }

  function publishState() {
    const connection = $("#connection");
    const payload = {
      route: `#${state.page}`,
      generated_at: new Date().toISOString(),
      connection: connection.classList.contains("connected")
        ? "connected"
        : connection.classList.contains("error") ? "error" : "pending",
      summary: state.uiState[state.page] || {},
    };
    $("#polygate-ui-state").textContent = JSON.stringify(payload);
  }

  function routeFromHash() {
    const page = window.location.hash.replace(/^#/, "");
    return Object.hasOwn(PAGE_TITLES, page) ? page : "overview";
  }

  function applyPage(page) {
    if (!Object.hasOwn(PAGE_TITLES, page)) page = "overview";
    if (window.location.hash !== `#${page}`) {
      history.replaceState(null, "", `#${page}`);
    }
    const initial = state.page == null;
    const changed = !initial && state.page !== page;
    state.page = page;
    document.title = `${PAGE_TITLES[page]} · polygate`;
    $("#page-title").textContent = PAGE_TITLES[page];
    $$(".view").forEach((view) => { view.hidden = view.dataset.view !== page; });
    $$("[data-page]").forEach((link) => {
      if (link.dataset.page === page) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    setNavOpen(false);
    if (changed) $("#main").focus({ preventScroll: true });
    publishState();
    loadPage(page, page === "overview" || !state.uiState[page]?.loaded);
    if (changed) window.scrollTo(0, 0);
  }

  async function loadPage(page = state.page, force = false) {
    if (!page || state.loading.has(page)) return;
    if (!force && state.uiState[page]?.loaded && page !== "overview") return;
    state.loading.add(page);
    const refresh = $('[data-action="refresh"]');
    refresh.disabled = true;
    $(`.view[data-view="${page}"]`)?.setAttribute("aria-busy", "true");
    try {
      const loaded = await PAGE_LOADERS[page]();
      state.uiState[page] = { ...(state.uiState[page] || {}), loaded: loaded !== false };
    } finally {
      state.loading.delete(page);
      refresh.disabled = false;
      $(`.view[data-view="${page}"]`)?.setAttribute("aria-busy", "false");
      publishState();
    }
  }

  function renderChart(container, rows, days) {
    const byDay = new Map((rows || []).map((row) => [row.day, row]));
    const values = [];
    const now = new Date();
    for (let offset = days - 1; offset >= 0; offset -= 1) {
      const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() - offset);
      const key = [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0")].join("-");
      const row = byDay.get(key) || {};
      values.push({
        key,
        label: key.slice(5),
        calls: Number(row.calls || 0),
        errors: Number(row.errors || 0),
        tokens: Number(row.prompt_tokens || 0) + Number(row.completion_tokens || 0),
      });
    }
    if (!values.some((row) => row.calls)) {
      container.innerHTML = '<div class="chart-empty">暂无数据</div>';
      return;
    }

    const width = 720;
    const height = 174;
    const top = 12;
    const bottom = 28;
    const left = 2;
    const right = 2;
    const chartHeight = height - top - bottom;
    const chartWidth = width - left - right;
    const gap = days > 30 ? 1 : 5;
    const barWidth = Math.max(1.5, (chartWidth - gap * (values.length - 1)) / values.length);
    const maximum = Math.max(1, ...values.map((row) => row.calls));
    const labelEvery = Math.max(1, Math.ceil(values.length / 7));
    const grid = [0.25, 0.5, 0.75, 1].map((part) => {
      const y = top + chartHeight - chartHeight * part;
      return `<line class="grid-line" x1="${left}" x2="${width - right}" y1="${y}" y2="${y}"/>`;
    }).join("");
    const bars = values.map((row, index) => {
      const barHeight = row.calls ? Math.max(2, (row.calls / maximum) * chartHeight) : 0;
      const x = left + index * (barWidth + gap);
      const y = top + chartHeight - barHeight;
      const current = index === values.length - 1 ? " current" : "";
      const label = index % labelEvery === 0 || index === values.length - 1
        ? `<text x="${x + barWidth / 2}" y="${height - 7}" text-anchor="middle">${escapeHtml(row.label)}</text>`
        : "";
      return `<rect class="bar${current}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="2"><title>${escapeHtml(row.key)} · ${formatInteger(row.calls)} 请求 · ${formatInteger(row.errors)} 错误</title></rect>${label}`;
    }).join("");
    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">${grid}${bars}</svg>`;
  }

  function modelRoutesMarkup(model) {
    const routes = model.routes || [];
    if (!routes.length) return '<span class="muted">—</span>';
    return `<div class="route-list">${routes.map((route) => (
      `<span class="route-item">${escapeHtml(route.provider)}<span>:</span>${escapeHtml(route.model)}${route.weight != null ? ` <span>×${escapeHtml(route.weight)}</span>` : ""}</span>`
    )).join("")}</div>`;
  }

  function capabilitiesMarkup(capabilities) {
    const items = capabilities || [];
    if (!items.length) return '<span class="muted">—</span>';
    return `<div class="capability-list">${items.map((item) => `<span class="capability">${escapeHtml(item)}</span>`).join("")}</div>`;
  }

  function renderModelRows(target, models, limit = null) {
    const rows = (models || []).slice(0, limit ?? undefined);
    if (!rows.length) {
      target.innerHTML = '<tr><td colspan="4" class="empty">暂无模型</td></tr>';
      return;
    }
    target.innerHTML = rows.map((model) => `
      <tr>
        <td><strong class="mono">${escapeHtml(model.id)}</strong></td>
        <td>${escapeHtml(model.kind || "chat")}</td>
        <td>${modelRoutesMarkup(model)}</td>
        <td>${capabilitiesMarkup([...new Set((model.routes || []).flatMap((route) => route.capabilities || []))])}</td>
      </tr>`).join("");
  }

  function totalsFromUsage(usage) {
    const models = Object.values(usage?.models || {});
    return {
      requests: models.reduce((total, item) => total + Number(item.requests ?? item.calls ?? 0), 0),
      errors: models.reduce((total, item) => total + Number(item.request_errors ?? item.errors ?? 0), 0),
      tokens: models.reduce((total, item) => total + Number(item.total_tokens || 0), 0),
    };
  }

  function weightedP95(usage) {
    const entries = Object.values(usage?.models || {});
    let weight = 0;
    let total = 0;
    entries.forEach((item) => {
      const calls = Number(item.requests ?? item.calls ?? 0);
      const p95 = Number(item.p95_latency_ms || item.avg_latency_ms || 0);
      if (p95 > 0) {
        total += p95 * Math.max(1, calls);
        weight += Math.max(1, calls);
      }
    });
    return weight ? total / weight : 0;
  }

  function attentionItems(status, usage) {
    const items = [];
    Object.entries(status?.providers || {}).forEach(([name, provider]) => {
      if (provider.state === "open") items.push({ kind: "error", title: name, detail: "熔断", href: "#routing" });
      else if (provider.state === "half_open") items.push({ kind: "warn", title: name, detail: "半开", href: "#routing" });
    });
    const missingKeys = Object.values(status?.providers || {}).filter((provider) => !provider.keys).length;
    if (missingKeys) {
      items.push({ kind: "warn", title: "Provider", detail: `${missingKeys} 个缺少密钥`, href: "#settings" });
    }
    const unrouted = (status?.models || []).filter((model) => !(model.routes || []).length).length;
    if (unrouted) items.push({ kind: "warn", title: "模型路由", detail: `${unrouted} 个无路由`, href: "#routing" });
    Object.entries(status?.clients || {}).forEach(([name, client]) => {
      const budget = client.usage?.monthly_budget;
      if (budget?.limit && budget.used / budget.limit >= 0.8) {
        items.push({
          kind: budget.used >= budget.limit ? "error" : "warn",
          title: name,
          detail: "预算接近上限",
          href: "#usage",
        });
      }
    });
    if (Object.keys(usage?.models || {}).length === 0 && status) {
      items.push({ kind: "warn", title: "请求", detail: "本次运行尚无请求", href: "#requests" });
    }
    return items;
  }

  async function loadOverview() {
    setConnection("pending", "同步中");
    const [statusResult, usageResult, historyResult, costsResult] = await Promise.all([
      safe(api("/v1/status")),
      safe(api("/v1/usage")),
      safe(api("/v1/usage/history?days=7")),
      safe(api("/v1/costs")),
    ]);

    const status = statusResult.data;
    const usage = usageResult.data;
    const history = historyResult.data || [];
    const costs = costsResult.data;
    if (status) state.status = status;
    const totals = totalsFromUsage(usage);
    const errorRate = totals.requests ? (totals.errors / totals.requests) * 100 : 0;
    const providerCount = Object.keys(status?.providers || {}).length;
    const modelCount = (status?.models || []).length;
    const unavailableProviders = Object.values(status?.providers || {}).filter((provider) => provider.state === "open").length;
    const degradedProviders = Object.values(status?.providers || {}).filter((provider) => provider.state === "half_open").length;
    const missingKeys = Object.values(status?.providers || {}).filter((provider) => provider.keys === 0).length;
    const items = attentionItems(status, usage);

    if (!status && !usage) {
      $("#overview-health").textContent = "无法连接";
      $("#overview-detail").textContent = problemText(statusResult.error || usageResult.error);
      $("#overview-dot").className = "status-dot large error";
    } else if (unavailableProviders) {
      $("#overview-health").textContent = "需要关注";
      $("#overview-detail").textContent = `${providerCount} 个 Provider · ${modelCount} 个模型 · ${unavailableProviders} 个熔断`;
      $("#overview-dot").className = "status-dot large error";
    } else if (degradedProviders) {
      $("#overview-health").textContent = "需要关注";
      $("#overview-detail").textContent = `${providerCount} 个 Provider · ${modelCount} 个模型 · ${degradedProviders} 个半开`;
      $("#overview-dot").className = "status-dot large warn";
    } else if (missingKeys) {
      $("#overview-health").textContent = "需要配置";
      $("#overview-detail").textContent = `${providerCount} 个 Provider · ${modelCount} 个模型 · ${missingKeys} 个缺少密钥`;
      $("#overview-dot").className = "status-dot large warn";
    } else {
      $("#overview-health").textContent = "运行正常";
      $("#overview-detail").textContent = `${providerCount} 个 Provider · ${modelCount} 个模型`;
      $("#overview-dot").className = "status-dot large ok";
    }

    $("#overview-uptime").textContent = usage ? formatDuration(usage.uptime_seconds) : "—";
    $("#metric-requests").textContent = usage ? formatInteger(totals.requests) : "—";
    $("#metric-tokens").textContent = usage ? formatInteger(totals.tokens) : "—";
    $("#metric-cost").textContent = costs ? formatCost(costs.total) : "—";
    $("#metric-cost-label").textContent = `${Number(costs?.window_days || 30)} 天成本`;
    $("#metric-errors").textContent = usage ? `${errorRate.toFixed(errorRate >= 10 ? 1 : 2)}%` : "—";
    $("#overview-chart-total").textContent = `${formatInteger(history.reduce((total, row) => total + Number(row.calls || 0), 0))} 请求`;
    renderChart($("#overview-chart"), history, 7);

    $("#attention-count").textContent = String(items.length);
    $("#attention-list").innerHTML = items.length
      ? items.slice(0, 6).map((item) => `
          <a class="plain-row" href="${escapeHtml(item.href)}">
            <span class="status-dot ${statusClass(item.kind === "error" ? "open" : "warn")}" aria-hidden="true"></span>
            <strong>${escapeHtml(item.title)}</strong>
            <span>${escapeHtml(item.detail)}</span>
          </a>`).join("")
      : '<div class="plain-row"><strong>无待处理项</strong></div>';

    renderModelRows($("#overview-models"), status?.models || [], 6);
    if (status?.version) {
      $("#sidebar-version").textContent = `v${status.version}`;
    }
    state.uiState.overview = {
      health: $("#overview-health").textContent,
      providers: providerCount,
      models: modelCount,
      requests: totals.requests,
      errors: totals.errors,
      tokens: totals.tokens,
      attention: items.length,
      scope: "process",
      cost_window_days: Number(costs?.window_days || 30),
    };
    return Boolean(status || usage);
  }

  async function loadRouting() {
    setConnection("pending", "同步中");
    const [result, configResult] = await Promise.all([
      safe(api("/v1/status")),
      safe(api("/v1/config")),
    ]);
    if (!result.ok) {
      $("#providers-body").innerHTML = `<tr><td colspan="5" class="empty">${escapeHtml(problemText(result.error))}</td></tr>`;
      $("#models-body").innerHTML = `<tr><td colspan="5" class="empty">${escapeHtml(problemText(result.error))}</td></tr>`;
      return false;
    }
    const status = result.data;
    if (configResult.ok) state.config = configResult.data;
    state.status = status;
    $("#new-model").disabled = !state.config;
    const providers = Object.entries(status.providers || {});
    const models = status.models || [];
    const healthy = providers.filter(([, provider]) => provider.state === "closed" && provider.keys > 0).length;
    $("#routing-summary").textContent = `${providers.length} 个 Provider · ${models.length} 个模型 · ${healthy}/${providers.length} 可用`;
    $("#provider-count").textContent = String(providers.length);
    $("#model-count").textContent = String(models.length);
    $("#providers-body").innerHTML = providers.length ? providers.map(([name, provider]) => {
      const rate = provider.rate_limit?.[0];
      const hasKeys = Number(provider.keys || 0) > 0;
      return `
        <tr>
          <td><strong>${escapeHtml(name)}</strong></td>
          <td>${hasKeys ? stateMarkup(provider.state, providerState(provider.state)) : stateMarkup("warn", "未配置")}</td>
          <td>${provider.keys ? formatInteger(provider.keys) : '<span class="muted">0</span>'}</td>
          <td>${formatInteger(provider.consecutive_failures)}</td>
          <td>${rate == null ? '<span class="muted">—</span>' : `${escapeHtml(rate)} / 秒`}</td>
        </tr>`;
    }).join("") : '<tr><td colspan="5" class="empty">暂无 Provider</td></tr>';
    renderRoutingModelRows(models);
    $("#sidebar-version").textContent = `v${status.version}`;
    state.uiState.routing = { providers: providers.length, models: models.length, healthy };
    return true;
  }

  function renderRoutingModelRows(models) {
    if (!models.length) {
      $("#models-body").innerHTML = '<tr><td colspan="5" class="empty">暂无模型</td></tr>';
      return;
    }
    $("#models-body").innerHTML = models.map((model) => `
      <tr>
        <td><strong class="mono">${escapeHtml(model.id)}</strong></td>
        <td>${escapeHtml(model.kind || "chat")}</td>
        <td>${modelRoutesMarkup(model)}</td>
        <td>${capabilitiesMarkup([...new Set((model.routes || []).flatMap((route) => route.capabilities || []))])}</td>
        <td><button class="row-action" type="button" data-model-action="edit" data-model-name="${escapeHtml(model.id)}">编辑</button></td>
      </tr>`).join("");
  }

  function providerOptions(selected) {
    const names = Object.keys(state.config?.providers || {});
    if (selected && !names.includes(selected)) names.push(selected);
    return names.map((name) => `<option value="${escapeHtml(name)}"${name === selected ? " selected" : ""}>${escapeHtml(name)}</option>`).join("");
  }

  function routeEditorRow(route = {}) {
    return `
      <div class="route-editor-row">
        <select class="route-provider" aria-label="Provider">${providerOptions(route.provider)}</select>
        <input class="route-model" aria-label="上游模型" value="${escapeHtml(route.model || "")}" placeholder="模型" spellcheck="false">
        <input class="route-weight" aria-label="权重" type="number" min="0.01" step="0.01" value="${escapeHtml(route.weight ?? 1)}">
        <input class="route-capabilities" aria-label="能力" value="${escapeHtml((route.capabilities || []).join(","))}" placeholder="stream,tools,json" spellcheck="false">
        <button class="row-action route-remove" type="button" data-route-remove aria-label="删除路由">×</button>
      </div>`;
  }

  function resetModelValidation() {
    state.validatedModelSignature = "";
    const button = $("#model-form button[type=submit]");
    button.textContent = "验证";
    $("#model-diff").hidden = true;
    $("#model-diff").innerHTML = "";
    setFormMessage("#model-message", "");
  }

  function openModelDialog(name = "") {
    if (!state.config) {
      showToast("配置尚未载入");
      return;
    }
    const model = (state.status?.models || []).find((item) => item.id === name);
    state.editingModelName = model?.id || "";
    $("#model-dialog-title").textContent = model ? "编辑模型" : "新建模型";
    $("#model-name").value = model?.id || "";
    $("#model-name").disabled = Boolean(model);
    $("#model-kind").value = model?.kind || "chat";
    $("#model-routes").innerHTML = (model?.routes?.length ? model.routes : [{}]).map(routeEditorRow).join("");
    resetModelValidation();
    const dialog = $("#model-dialog");
    if (!dialog.open) dialog.showModal();
    if (!model) $("#model-name").focus();
  }

  function modelConfigPayload() {
    const providers = $$("#model-routes .route-editor-row").map((row) => ({
      provider: row.querySelector(".route-provider").value,
      model: row.querySelector(".route-model").value.trim(),
      weight: Number(row.querySelector(".route-weight").value || 1),
      capabilities: splitModels(row.querySelector(".route-capabilities").value),
    })).filter((route) => route.provider && route.model);
    return {
      revision: state.config?.revision || null,
      models: [{
        name: $("#model-name").value.trim(),
        kind: $("#model-kind").value,
        providers,
      }],
    };
  }

  function renderConfigDiff(target, diff) {
    target.hidden = false;
    target.innerHTML = diff.length
      ? diff.map((item) => `<div><strong>${escapeHtml(item.title)}</strong> <span>${escapeHtml(item.detail)}</span></div>`).join("")
      : "<div>无配置变化</div>";
  }

  async function submitModel(event) {
    event.preventDefault();
    const payload = modelConfigPayload();
    if (!payload.models[0].name || !payload.models[0].providers.length) {
      setFormMessage("#model-message", "填写模型和路由", "error");
      return;
    }
    const signature = JSON.stringify(payload);
    const button = $("#model-form button[type=submit]");
    button.disabled = true;
    try {
      if (state.validatedModelSignature !== signature) {
        setFormMessage("#model-message", "验证中");
        const validation = await api("/v1/config/validate", { method: "POST", body: payload });
        state.validatedModelSignature = signature;
        state.config.revision = validation.revision;
        renderConfigDiff($("#model-diff"), validation.diff || []);
        button.textContent = "应用";
        setFormMessage("#model-message", "验证通过", "success");
        return;
      }
      await api("/v1/config", { method: "PUT", body: payload });
      $("#model-dialog").close();
      showToast("路由已更新");
      delete state.uiState.settings;
      await loadRouting();
    } catch (error) {
      resetModelValidation();
      setFormMessage("#model-message", problemText(error), "error");
      if (error.status === 409) {
        const fresh = await safe(api("/v1/config"));
        if (fresh.ok) state.config = fresh.data;
      }
    } finally {
      button.disabled = false;
    }
  }

  function quotaValue(definition, usage, format = formatInteger) {
    if (!definition) return '<span class="muted">—</span>';
    if (!usage) return escapeHtml(definition);
    return `${format(usage.used)} <span class="muted">/ ${format(usage.limit)}</span>`;
  }

  async function loadUsage() {
    setConnection("pending", "同步中");
    const [statusResult, usageResult, historyResult, costsResult] = await Promise.all([
      safe(api("/v1/status")),
      safe(api("/v1/usage")),
      safe(api(`/v1/usage/history?days=${state.historyDays}`)),
      safe(api("/v1/costs")),
    ]);
    const status = statusResult.data;
    const usage = usageResult.data;
    const history = historyResult.data || [];
    const costs = costsResult.data;
    if (status) state.status = status;
    const totals = totalsFromUsage(usage);
    $("#usage-requests").textContent = usage ? formatInteger(totals.requests) : "—";
    $("#usage-tokens").textContent = usage ? formatInteger(totals.tokens) : "—";
    $("#usage-cost").textContent = costs ? formatCost(costs.total) : "—";
    $("#usage-cost-label").textContent = `${Number(costs?.window_days || 30)} 天成本`;
    $("#usage-p95").textContent = usage ? formatLatency(weightedP95(usage)) : "—";
    renderChart($("#history-chart"), history, state.historyDays);

    const models = Object.entries(usage?.models || {});
    $("#usage-models-body").innerHTML = models.length ? models.map(([name, item]) => `
      <tr>
        <td><strong class="mono">${escapeHtml(name)}</strong></td>
        <td>${formatInteger(item.requests ?? item.calls)}</td>
        <td>${formatInteger(item.request_errors ?? item.errors)}</td>
        <td>${formatInteger(item.total_tokens)}</td>
        <td>${formatLatency(item.avg_latency_ms)}</td>
        <td>${formatLatency(item.p95_latency_ms)}</td>
      </tr>`).join("") : '<tr><td colspan="6" class="empty">暂无数据</td></tr>';

    $("#cost-total").textContent = costs ? formatCost(costs.total) : "成本未启用";
    const byKey = costs?.by_key || [];
    $("#cost-keys-body").innerHTML = byKey.length ? byKey.map((row) => `
      <tr>
        <td class="mono">${escapeHtml(row.key || "—")}</td>
        <td>${formatCost(row.cost)}</td>
        <td>${formatInteger(row.calls)}</td>
        <td>${formatInteger(Number(row.prompt_tokens || 0) + Number(row.completion_tokens || 0))}</td>
      </tr>`).join("") : `<tr><td colspan="4" class="empty">${costs ? "暂无数据" : "未启用成本记录"}</td></tr>`;
    const byClient = costs?.by_client || [];
    $("#cost-clients-body").innerHTML = byClient.length ? byClient.map((row) => `
      <tr>
        <td>${escapeHtml(row.client || "匿名")}</td>
        <td>${formatCost(row.cost)}</td>
        <td>${formatInteger(row.calls)}</td>
        <td>${formatInteger(Number(row.prompt_tokens || 0) + Number(row.completion_tokens || 0))}</td>
      </tr>`).join("") : `<tr><td colspan="4" class="empty">${costs ? "暂无数据" : "未启用成本记录"}</td></tr>`;

    const clients = Object.entries(status?.clients || {});
    $("#clients-body").innerHTML = clients.length ? clients.map(([name, client]) => {
      const used = client.usage || {};
      return `
        <tr>
          <td><strong>${escapeHtml(name)}</strong></td>
          <td>${escapeHtml((client.models || []).join(", ") || "全部")}</td>
          <td>${client.rpm ? `${escapeHtml(client.rpm)} / 分` : '<span class="muted">—</span>'}</td>
          <td>${quotaValue(client.daily_calls, used.daily_calls)}</td>
          <td>${quotaValue(client.monthly_calls, used.monthly_calls)}</td>
          <td>${quotaValue(client.monthly_budget, used.monthly_budget, formatCost)}</td>
        </tr>`;
    }).join("") : '<tr><td colspan="6" class="empty">暂无客户端</td></tr>';
    const quotaState = status?.quota_persistent ? (status.alerting?.enabled ? "SQLite · 告警已启用" : "SQLite") : (status?.quota_note || "未持久化");
    $("#quota-state").textContent = quotaState;
    state.uiState.usage = {
      requests: totals.requests,
      errors: totals.errors,
      tokens: totals.tokens,
      cost: costs?.total ?? null,
      models: models.length,
      days: state.historyDays,
      scope: "process",
      cost_window_days: Number(costs?.window_days || 30),
    };
    return Boolean(status || usage || costs);
  }

  function requestQuery() {
    const params = new URLSearchParams({ limit: String(state.requestLimit), offset: String(state.requestOffset) });
    Object.entries(state.requestFilters).forEach(([key, value]) => { if (value) params.set(key, value); });
    return params.toString();
  }

  async function loadRequests() {
    setConnection("pending", "同步中");
    const result = await safe(api(`/v1/requests/groups?${requestQuery()}`));
    if (!result.ok) {
      $("#requests-body").innerHTML = `<tr><td colspan="8" class="empty">${escapeHtml(problemText(result.error))}</td></tr>`;
      return false;
    }
    const rows = result.data || [];
    const page = Math.floor(state.requestOffset / state.requestLimit) + 1;
    $("#request-page").textContent = `${formatInteger(rows.length)} 请求 · 第 ${formatInteger(page)} 页`;
    $("#requests-body").innerHTML = rows.length ? rows.map((row) => {
      const ok = row.status === "ok";
      const groupId = row.group_id || row.logical_id || row.req_id;
      const shortId = row.logical_id ? `${groupId.slice(0, 10)}…` : groupId;
      const providers = (row.providers || []).join(" → ") || row.provider || "—";
      return `
        <tr>
          <td class="nowrap">${escapeHtml(formatTimestamp(row.ts))}</td>
          <td><button class="request-id-button" type="button" data-request-group="${escapeHtml(groupId)}" title="${escapeHtml(groupId)}">${escapeHtml(shortId)}</button></td>
          <td class="mono">${escapeHtml(row.model)}</td>
          <td><span class="route-chain">${escapeHtml(providers)}</span> ${row.attempt_count > 1 ? `<span class="muted">${row.attempt_count} 次</span>` : ""}</td>
          <td class="hide-small">${escapeHtml(row.client || "—")}</td>
          <td>${stateMarkup(ok ? "ok" : "error", row.status_code || row.status)}</td>
          <td>${formatLatency(row.latency_ms)}</td>
          <td><button class="row-action" type="button" data-request-group="${escapeHtml(groupId)}">查看</button></td>
        </tr>`;
    }).join("") : '<tr><td colspan="8" class="empty">暂无请求</td></tr>';
    $("#requests-prev").disabled = state.requestOffset === 0;
    $("#requests-next").disabled = rows.length < state.requestLimit;
    state.uiState.requests = { requests: rows.length, offset: state.requestOffset, filters: { ...state.requestFilters } };
    return true;
  }

  async function openRequest(requestId) {
    state.activeRequestId = requestId;
    state.activeLogicalId = "";
    const dialog = $("#request-dialog");
    $("#request-dialog-title").textContent = "详情";
    $("#request-detail").innerHTML = '<p class="empty">载入中</p>';
    if (!dialog.open) dialog.showModal();
    const result = await safe(api(`/v1/requests/${encodeURIComponent(requestId)}`));
    if (state.activeRequestId !== requestId) return;
    if (!result.ok) {
      $("#request-detail").innerHTML = `<p class="empty">${escapeHtml(problemText(result.error))}</p>`;
      return;
    }
    const entry = result.data;
    $("#request-dialog-title").textContent = entry.req_id;
    const requestBody = entry.request == null ? "—" : JSON.stringify(entry.request, null, 2);
    const responseBody = entry.response == null ? "—" : JSON.stringify(entry.response, null, 2);
    const errorBlock = entry.error ? `<section class="detail-section"><h3>错误</h3><pre>${escapeHtml(entry.error)}</pre></section>` : "";
    $("#request-detail").innerHTML = `
      <div class="detail-grid">
        <div class="detail-item"><span>时间</span><strong>${escapeHtml(formatTimestamp(entry.ts))}</strong></div>
        <div class="detail-item"><span>端点</span><strong>${escapeHtml(entry.endpoint || "—")}</strong></div>
        <div class="detail-item"><span>状态</span><strong>${escapeHtml(entry.status_code || entry.status)}</strong></div>
        <div class="detail-item"><span>模型</span><strong>${escapeHtml(entry.model)}</strong></div>
        <div class="detail-item"><span>Provider</span><strong>${escapeHtml(entry.provider || "—")}</strong></div>
        <div class="detail-item"><span>上游模型</span><strong>${escapeHtml(entry.upstream_model || "—")}</strong></div>
        <div class="detail-item"><span>延迟</span><strong>${escapeHtml(formatLatency(entry.latency_ms))}</strong></div>
        <div class="detail-item"><span>客户端</span><strong>${escapeHtml(entry.client || "—")}</strong></div>
        <div class="detail-item"><span>Key</span><strong>${escapeHtml(entry.key || "—")}</strong></div>
        <div class="detail-item"><span>逻辑请求</span><strong class="mono">${escapeHtml(entry.logical_id || "—")}</strong></div>
      </div>
      ${entry.truncated ? '<p class="form-message">日志已裁剪</p>' : ""}
      <section class="detail-section"><h3>请求</h3><pre>${escapeHtml(requestBody)}</pre></section>
      <section class="detail-section"><h3>响应</h3><pre>${escapeHtml(responseBody)}</pre></section>
      ${errorBlock}`;
  }

  function attemptChainMarkup(attempts) {
    if (!attempts?.length) return "";
    return `<section class="detail-section">
      <h3>尝试</h3>
      <div class="attempt-chain">
        ${attempts.map((attempt, index) => `
          <div class="attempt-row">
            <span class="attempt-index">${index + 1}</span>
            <strong>${escapeHtml(attempt.provider || "—")}</strong>
            <span class="mono">${escapeHtml(attempt.upstream_model || attempt.model || "—")}</span>
            <span>${escapeHtml(attempt.status_code || attempt.status)}</span>
            <span>${escapeHtml(formatLatency(attempt.latency_ms))}</span>
            ${attempt.error ? `<span class="attempt-error">${escapeHtml(attempt.error.slice(0, 160))}</span>` : ""}
          </div>`).join("")}
      </div>
    </section>`;
  }

  async function openRequestGroup(groupId) {
    state.activeLogicalId = groupId;
    const dialog = $("#request-dialog");
    $("#request-dialog-title").textContent = "请求";
    $("#request-detail").innerHTML = '<p class="empty">载入中</p>';
    if (!dialog.open) dialog.showModal();
    const result = await safe(api(`/v1/requests/groups/${encodeURIComponent(groupId)}`));
    if (state.activeLogicalId !== groupId) return;
    if (!result.ok) {
      $("#request-detail").innerHTML = `<p class="empty">${escapeHtml(problemText(result.error))}</p>`;
      return;
    }
    const entry = result.data;
    state.activeRequestId = entry.req_id;
    $("#request-dialog-title").textContent = entry.group_id;
    const requestBody = entry.request == null ? "—" : JSON.stringify(entry.request, null, 2);
    const responseBody = entry.response == null ? "—" : JSON.stringify(entry.response, null, 2);
    const errors = (entry.attempts || []).map((attempt) => attempt.error).filter(Boolean);
    $("#request-detail").innerHTML = `
      <div class="detail-grid">
        <div class="detail-item"><span>时间</span><strong>${escapeHtml(formatTimestamp(entry.ts))}</strong></div>
        <div class="detail-item"><span>端点</span><strong>${escapeHtml(entry.endpoint || "—")}</strong></div>
        <div class="detail-item"><span>状态</span><strong>${escapeHtml(entry.status_code || entry.status)}</strong></div>
        <div class="detail-item"><span>模型</span><strong>${escapeHtml(entry.model)}</strong></div>
        <div class="detail-item"><span>Provider</span><strong>${escapeHtml(entry.provider || "—")}</strong></div>
        <div class="detail-item"><span>客户端</span><strong>${escapeHtml(entry.client || "—")}</strong></div>
        <div class="detail-item"><span>尝试</span><strong>${formatInteger(entry.attempt_count)}</strong></div>
        <div class="detail-item"><span>延迟</span><strong>${escapeHtml(formatLatency(entry.latency_ms))}</strong></div>
        <div class="detail-item"><span>逻辑请求</span><strong class="mono">${escapeHtml(entry.group_id)}</strong></div>
        <div class="detail-item"><span>最终请求</span><strong class="mono">${escapeHtml(entry.req_id)}</strong></div>
      </div>
      ${entry.attempt_count > 1 ? attemptChainMarkup(entry.attempts || []) : ""}
      ${entry.truncated ? '<p class="form-message">日志已裁剪</p>' : ""}
      <section class="detail-section"><h3>请求</h3><pre>${escapeHtml(requestBody)}</pre></section>
      <section class="detail-section"><h3>响应</h3><pre>${escapeHtml(responseBody)}</pre></section>
      ${errors.length ? `<section class="detail-section"><h3>错误</h3><pre>${escapeHtml(errors.join("\n\n"))}</pre></section>` : ""}`;
  }

  function compatibilityProbeMarkup(probes) {
    const labels = { chat: "chat", stream_usage: "stream", tools: "tools", json: "json", models: "models" };
    return `<div class="probe-list">${Object.entries(probes || {}).map(([name, result]) => `
      <span class="probe ${result.ok ? "ok" : ""}">${escapeHtml(labels[name] || name)} ${escapeHtml(result.status_code ?? "ERR")}</span>`).join(" ")}</div>`;
  }

  function renderCompatibility(data) {
    const routes = data?.routes || [];
    $("#compat-time").textContent = data?.tested_at ? formatTimestamp(data.tested_at) : "未运行";
    $("#compat-body").innerHTML = routes.length ? routes.map((route) => `
      <tr>
        <td><strong>${escapeHtml(route.provider)}</strong></td>
        <td class="mono">${escapeHtml(route.model)}</td>
        <td class="mono">${escapeHtml(route.upstream_model)}</td>
        <td>${compatibilityProbeMarkup(route.probes)}</td>
        <td>${capabilitiesMarkup(route.capabilities)}</td>
        <td><button class="row-action" type="button" data-compat-apply="${escapeHtml(route.model)}" data-compat-provider="${escapeHtml(route.provider)}" data-compat-upstream="${escapeHtml(route.upstream_model)}">应用</button></td>
      </tr>`).join("") : '<tr><td colspan="6" class="empty">暂无结果</td></tr>';
    state.uiState.diagnostics = {
      ...(state.uiState.diagnostics || {}),
      tested_at: data?.tested_at || null,
      routes: routes.length,
    };
  }

  async function loadDiagnostics() {
    setConnection("pending", "同步中");
    const [result, configResult] = await Promise.all([
      safe(api("/v1/compatibility")),
      safe(api("/v1/config")),
    ]);
    if (configResult.ok) state.config = configResult.data;
    if (result.ok) renderCompatibility(result.data);
    else {
      $("#compat-body").innerHTML = `<tr><td colspan="6" class="empty">${escapeHtml(problemText(result.error))}</td></tr>`;
    }
    return result.ok;
  }

  async function applyCompatibility(button) {
    const modelName = button.dataset.compatApply;
    const provider = button.dataset.compatProvider;
    const upstream = button.dataset.compatUpstream;
    const routeRow = Array.from($$("#compat-body tr")).find((row) => (
      row.querySelector("[data-compat-apply]")?.dataset.compatApply === modelName
      && row.querySelector("[data-compat-provider]")?.dataset.compatProvider === provider
      && row.querySelector("[data-compat-upstream]")?.dataset.compatUpstream === upstream
    ));
    const capabilityNodes = routeRow ? Array.from(routeRow.querySelectorAll(".capability")) : [];
    const discovered = capabilityNodes.map((node) => node.textContent.trim()).filter(Boolean);
    const model = (state.config?.models || []).find((item) => item.name === modelName);
    const route = model?.routes?.find((item) => item.provider === provider && item.model === upstream);
    if (!model || !route) {
      setFormMessage("#compat-message", "找不到对应路由", "error");
      return;
    }
    const capabilities = [...new Set([...(route.capabilities || []), ...discovered])];
    const providers = model.routes.map((item) => ({
      provider: item.provider,
      model: item.model,
      weight: item.weight,
      capabilities: item.provider === provider && item.model === upstream ? capabilities : item.capabilities || [],
    }));
    const payload = {
      revision: state.config.revision,
      models: [{ name: model.name, kind: model.kind, providers }],
    };
    button.disabled = true;
    try {
      const validation = await api("/v1/config/validate", { method: "POST", body: payload });
      const changes = (validation.diff || []).map((item) => `${item.title}：${item.detail}`).join("\n");
      if (changes && !window.confirm(`应用到 ${modelName}？\n\n${changes}`)) return;
      await api("/v1/config", { method: "PUT", body: payload });
      setFormMessage("#compat-message", "已应用到路由", "success");
      delete state.uiState.routing;
      await loadDiagnostics();
    } catch (error) {
      setFormMessage("#compat-message", problemText(error), "error");
    } finally {
      button.disabled = false;
    }
  }

  function splitModels(value) {
    return String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
  }

  async function submitCompatibility(event) {
    event.preventDefault();
    const button = $("#compat-run");
    const message = $("#compat-message");
    const probes = [];
    if ($("#probe-chat").checked) probes.push("chat");
    if ($("#probe-stream").checked) probes.push("stream_usage");
    if ($("#probe-tools").checked) probes.push("tools");
    if ($("#probe-json").checked) probes.push("json");
    if ($("#probe-models").checked) probes.push("models");
    if (!probes.length) {
      message.textContent = "选择检测项";
      message.className = "form-message error";
      return;
    }
    button.disabled = true;
    message.textContent = "运行中";
    message.className = "form-message";
    try {
      const data = await api("/v1/compatibility/run", {
        method: "POST",
        body: { models: splitModels($("#compat-models").value), probes },
      });
      renderCompatibility(data);
      message.textContent = "完成";
      message.className = "form-message success";
    } catch (error) {
      message.textContent = problemText(error);
      message.className = "form-message error";
    } finally {
      button.disabled = false;
    }
  }

  async function submitReplay(event) {
    event.preventDefault();
    const button = $("#replay-run");
    const message = $("#replay-message");
    const requestId = $("#replay-id").value.trim();
    if (!requestId) {
      message.textContent = "填写请求 ID";
      message.className = "form-message error";
      return;
    }
    button.disabled = true;
    message.textContent = "运行中";
    message.className = "form-message";
    $("#replay-json").textContent = "";
    try {
      const data = await api("/v1/replay-lab", {
        method: "POST",
        body: {
          request_id: requestId,
          models: splitModels($("#replay-models").value),
          include_response: $("#replay-response").checked,
        },
      });
      const rows = data?.results || [];
      $("#replay-body").innerHTML = rows.length ? rows.map((row) => {
        let preview = row.error || "";
        if (row.response) {
          preview = row.response?.choices?.[0]?.message?.content || JSON.stringify(row.response);
        }
        return `
          <tr>
            <td class="mono">${escapeHtml(row.model)}</td>
            <td>${escapeHtml(row.provider || "—")}</td>
            <td>${stateMarkup(row.ok ? "ok" : "error", row.status_code || (row.ok ? "ok" : "error"))}</td>
            <td>${formatLatency(row.latency_ms)}</td>
            <td>${formatCost(row.cost)}</td>
            <td class="muted">${escapeHtml(String(preview).slice(0, 180))}</td>
          </tr>`;
      }).join("") : '<tr><td colspan="6" class="empty">暂无结果</td></tr>';
      $("#replay-json").textContent = JSON.stringify(data, null, 2);
      const summary = data?.summary || {};
      message.textContent = `${formatInteger(summary.successful)} 成功 · ${formatInteger(summary.failed)} 失败 · 最快 ${summary.fastest_model || "—"}`;
      message.className = summary.failed ? "form-message error" : "form-message success";
    } catch (error) {
      message.textContent = problemText(error);
      message.className = "form-message error";
    } finally {
      button.disabled = false;
    }
  }

  async function loadSettings() {
    setConnection("pending", "同步中");
    const result = await safe(api("/v1/config"));
    if (!result.ok) {
      $("#config-providers-body").innerHTML = `<tr><td colspan="5" class="empty">${escapeHtml(problemText(result.error))}</td></tr>`;
      return false;
    }
    const config = result.data;
    state.config = config;
    const providers = Object.entries(config.providers || {});
    $("#config-providers-body").innerHTML = providers.length ? providers.map(([name, provider]) => {
      const placeholder = provider.has_key ? `已设置 ${provider.key_hint || ""}`.trim() : "未设置";
      return `
        <tr>
          <td data-label="Provider"><strong>${escapeHtml(name)}</strong></td>
          <td data-label="Base URL"><label><span class="sr-only">${escapeHtml(name)} Base URL</span><input class="field-input config-url" data-provider="${escapeHtml(name)}" type="url" value="${escapeHtml(provider.base_url || "")}" autocomplete="off" spellcheck="false"></label></td>
          <td data-label="API Key"><label><span class="sr-only">${escapeHtml(name)} API Key</span><input class="field-input config-key" data-provider="${escapeHtml(name)}" type="password" placeholder="${escapeHtml(placeholder)}" autocomplete="new-password"></label></td>
          <td data-label="状态"><span data-provider-status>${provider.has_key ? stateMarkup("ok", "已设置") : stateMarkup("warn", "缺少密钥")}</span></td>
          <td data-label="操作"><button class="row-action" type="button" data-provider-test="${escapeHtml(name)}">测试</button></td>
        </tr>`;
    }).join("") : '<tr><td colspan="5" class="empty">暂无 Provider</td></tr>';
    const budget = config.budget || {};
    $("#budget-max-output").value = budget.default_max_output_tokens ?? 4096;
    $("#budget-bytes-token").value = budget.bytes_per_token ?? 2;
    $("#budget-ttl").value = budget.reservation_ttl ?? 900;
    const keysResult = await safe(api("/v1/client-keys"));
    if (keysResult.ok) {
      state.clientKeys = keysResult.data || [];
      renderClientKeys();
    } else {
      $("#client-keys-body").innerHTML = `<tr><td colspan="6" class="empty">${escapeHtml(problemText(keysResult.error))}</td></tr>`;
    }
    state.uiState.settings = {
      providers: providers.length,
      client_keys: state.clientKeys.length,
      budget,
    };
    return true;
  }

  async function testProvider(name, button) {
    const row = button.closest("tr");
    const baseUrl = row.querySelector(".config-url")?.value.trim() || "";
    const apiKey = row.querySelector(".config-key")?.value || null;
    const status = row.querySelector("[data-provider-status]");
    button.disabled = true;
    status.innerHTML = '<span class="muted">测试中</span>';
    try {
      const result = await api(`/v1/providers/${encodeURIComponent(name)}/test`, {
        method: "POST",
        body: { base_url: baseUrl, api_key: apiKey, timeout: 8 },
      });
      const detail = result.ok
        ? `${Math.round(result.latency_ms)} ms · ${formatInteger((result.models || []).length)} 模型`
        : `${result.reachable ? "可连接" : "不可达"} · ${result.status_code || "错误"}`;
      status.innerHTML = stateMarkup(result.ok ? "ok" : (result.reachable ? "warn" : "error"), detail);
      if (result.ok && result.models?.length) {
        $("#discovered-models").innerHTML = result.models
          .map((model) => `<option value="${escapeHtml(model)}"></option>`)
          .join("");
      }
    } catch (error) {
      status.innerHTML = stateMarkup("error", problemText(error));
    } finally {
      button.disabled = false;
    }
  }

  async function testNewProvider() {
    const name = $("#new-name").value.trim() || "provider";
    const baseUrl = $("#new-url").value.trim();
    const apiKey = $("#new-key").value || null;
    const button = $("#test-new-provider");
    if (!baseUrl) {
      setFormMessage("#provider-test-message", "填写 Base URL", "error");
      $("#new-url").focus();
      return;
    }
    button.disabled = true;
    setFormMessage("#provider-test-message", "测试中");
    try {
      const result = await api(`/v1/providers/${encodeURIComponent(name)}/test`, {
        method: "POST",
        body: { base_url: baseUrl, api_key: apiKey, timeout: 8 },
      });
      const detail = result.ok
        ? `${Math.round(result.latency_ms)} ms · ${formatInteger((result.models || []).length)} 模型`
        : `${result.reachable ? "可连接" : "不可达"} · ${result.status_code || "错误"}`;
      setFormMessage("#provider-test-message", detail, result.ok ? "success" : "error");
      if (result.ok && result.models?.length) {
        $("#discovered-models").innerHTML = result.models
          .map((model) => `<option value="${escapeHtml(model)}"></option>`)
          .join("");
      }
    } catch (error) {
      setFormMessage("#provider-test-message", problemText(error), "error");
    } finally {
      button.disabled = false;
    }
  }

  function clientKeyLimits(key) {
    const items = [];
    if (Number(key.rpm || 0) > 0) items.push(`${key.rpm} RPM`);
    if (Number(key.daily_calls || 0) > 0) items.push(`日 ${formatInteger(key.daily_calls)}`);
    if (Number(key.monthly_calls || 0) > 0) items.push(`月 ${formatInteger(key.monthly_calls)}`);
    if (Number(key.monthly_budget || 0) > 0) items.push(`预算 ${formatCost(key.monthly_budget)}`);
    return items.length ? items.join(" · ") : "不限";
  }

  function renderClientKeys() {
    const keys = state.clientKeys;
    $("#client-keys-body").innerHTML = keys.length ? keys.map((key) => {
      const status = key.revoked_at
        ? stateMarkup("error", "已吊销")
        : key.expired ? stateMarkup("error", "已过期") : stateMarkup("ok", "有效");
      const actions = key.revoked_at ? "" : `
        <div class="key-actions">
          <button class="row-action" type="button" data-key-action="edit" data-key-id="${escapeHtml(key.id)}">编辑</button>
          <button class="row-action" type="button" data-key-action="rotate" data-key-id="${escapeHtml(key.id)}">轮换</button>
          <button class="row-action danger" type="button" data-key-action="revoke" data-key-id="${escapeHtml(key.id)}">吊销</button>
        </div>`;
      return `
        <tr>
          <td data-label="名称"><strong>${escapeHtml(key.name)}</strong></td>
          <td data-label="Key"><span class="mono key-prefix">${escapeHtml(key.key_prefix || "—")}</span></td>
          <td data-label="限制"><span class="key-limit">${escapeHtml(clientKeyLimits(key))}</span></td>
          <td data-label="最后使用">${escapeHtml(key.last_used_at ? formatTimestamp(key.last_used_at) : "—")}</td>
          <td data-label="状态">${status}</td>
          <td data-label="操作">${actions}</td>
        </tr>`;
    }).join("") : '<tr><td colspan="6" class="empty">暂无虚拟 Key</td></tr>';
  }

  function openClientKeyDialog(keyId = "") {
    const key = state.clientKeys.find((item) => item.id === keyId);
    state.editingClientKeyId = key?.id || "";
    $("#client-key-dialog-title").textContent = key ? "编辑 Key" : "新建 Key";
    $("#client-key-name").value = key?.name || "";
    $("#client-key-models").value = (key?.models || []).join(", ");
    $("#client-key-rpm").value = key?.rpm || 0;
    $("#client-key-daily").value = key?.daily_calls || 0;
    $("#client-key-monthly").value = key?.monthly_calls || 0;
    $("#client-key-budget").value = key?.monthly_budget || 0;
    $("#client-key-expires").value = formatDateInput(key?.expires_at);
    $("#client-key-note").value = key?.note || "";
    setFormMessage("#client-key-message", "");
    const dialog = $("#client-key-dialog");
    if (!dialog.open) dialog.showModal();
    $("#client-key-name").focus();
  }

  async function saveClientKey(event) {
    event.preventDefault();
    const expiry = $("#client-key-expires").value;
    const payload = {
      name: $("#client-key-name").value.trim(),
      models: splitModels($("#client-key-models").value),
      rpm: Number($("#client-key-rpm").value || 0),
      daily_calls: Number($("#client-key-daily").value || 0),
      monthly_calls: Number($("#client-key-monthly").value || 0),
      monthly_budget: Number($("#client-key-budget").value || 0),
      expires_at: expiry ? new Date(`${expiry}T23:59:59`).getTime() / 1000 : null,
      note: $("#client-key-note").value.trim(),
    };
    if (!payload.name) return;
    const editing = state.editingClientKeyId;
    const button = $("#client-key-form button[type=submit]");
    button.disabled = true;
    setFormMessage("#client-key-message", "保存中");
    try {
      const data = await api(editing ? `/v1/client-keys/${encodeURIComponent(editing)}` : "/v1/client-keys", {
        method: editing ? "PATCH" : "POST",
        body: payload,
      });
      $("#client-key-dialog").close();
      showToast("已保存");
      if (data?.key) showClientSecret(payload.name, data.key);
      await loadSettings();
    } catch (error) {
      setFormMessage("#client-key-message", problemText(error), "error");
    } finally {
      button.disabled = false;
    }
  }

  function showClientSecret(name, secret) {
    $("#client-secret-title").textContent = `${name} · 新 Key`;
    $("#client-secret-value").textContent = secret;
    const dialog = $("#client-secret-dialog");
    if (!dialog.open) dialog.showModal();
  }

  async function rotateClientKey(key) {
    if (!window.confirm(`轮换 ${key.name}？旧 Key 会立即失效。`)) return;
    try {
      const data = await api(`/v1/client-keys/${encodeURIComponent(key.id)}/rotate`, { method: "POST" });
      showClientSecret(key.name, data.key);
      await loadSettings();
    } catch (error) {
      showToast(problemText(error));
    }
  }

  async function revokeClientKey(key) {
    if (!window.confirm(`吊销 ${key.name}？`)) return;
    try {
      await api(`/v1/client-keys/${encodeURIComponent(key.id)}`, { method: "DELETE" });
      showToast("已吊销");
      await loadSettings();
    } catch (error) {
      showToast(problemText(error));
    }
  }

  async function copyClientSecret() {
    const value = $("#client-secret-value").textContent;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      showToast("已复制");
    } catch {
      showToast("复制失败");
    }
  }

  function setFormMessage(id, text, kind = "") {
    const node = $(id);
    node.textContent = text;
    node.className = `form-message${kind ? ` ${kind}` : ""}`;
  }

  async function saveProviders() {
    const providers = {};
    $$(".config-url").forEach((input) => {
      providers[input.dataset.provider] = { base_url: input.value.trim() };
    });
    $$(".config-key").forEach((input) => {
      if (input.value) providers[input.dataset.provider].api_key = input.value;
    });
    const button = $("#save-providers");
    button.disabled = true;
    setFormMessage("#config-message", "验证中");
    try {
      const payload = { revision: state.config?.revision || null, providers };
      const validation = await api("/v1/config/validate", { method: "POST", body: payload });
      const changes = (validation.diff || []).map((item) => `${item.title}：${item.detail}`).join("\n");
      if (changes && !window.confirm(`应用以下修改？\n\n${changes}`)) {
        setFormMessage("#config-message", "未更改");
        return;
      }
      setFormMessage("#config-message", "保存中");
      await api("/v1/config", { method: "PUT", body: payload });
      setFormMessage("#config-message", "已保存", "success");
      delete state.uiState.routing;
      await loadSettings();
    } catch (error) {
      setFormMessage("#config-message", problemText(error), "error");
    } finally {
      button.disabled = false;
    }
  }

  async function resetConfiguration() {
    if (!window.confirm("重置为 config.yaml 配置？")) return;
    try {
      await api("/v1/config", { method: "DELETE" });
      setFormMessage("#config-message", "已重置", "success");
      await loadSettings();
    } catch (error) {
      setFormMessage("#config-message", problemText(error), "error");
    }
  }

  async function addProvider(event) {
    event.preventDefault();
    const name = $("#new-name").value.trim();
    const url = $("#new-url").value.trim();
    const key = $("#new-key").value;
    const model = $("#new-model").value.trim();
    if (!name || !url) return;
    const payload = { revision: state.config?.revision || null, providers: { [name]: { base_url: url } } };
    if (key) payload.providers[name].api_key = key;
    if (model) payload.models = [{ name: model, kind: "chat", providers: [{ provider: name, model }] }];
    const button = $("#provider-form button[type=submit]");
    button.disabled = true;
    try {
      await api("/v1/config", { method: "PUT", body: payload });
      $("#new-name").value = "";
      $("#new-url").value = "";
      $("#new-key").value = "";
      $("#new-model").value = "";
      setFormMessage("#config-message", "已添加", "success");
      delete state.uiState.routing;
      await loadSettings();
    } catch (error) {
      setFormMessage("#config-message", problemText(error), "error");
    } finally {
      button.disabled = false;
    }
  }

  async function saveBudget(event) {
    event.preventDefault();
    const budget = {
      default_max_output_tokens: Number($("#budget-max-output").value),
      bytes_per_token: Number($("#budget-bytes-token").value),
      reservation_ttl: Number($("#budget-ttl").value),
    };
    if (!(budget.default_max_output_tokens > 0 && budget.bytes_per_token > 0 && budget.reservation_ttl > 0)) {
      setFormMessage("#budget-message", "填写正数", "error");
      return;
    }
    const button = $("#budget-form button[type=submit]");
    button.disabled = true;
    try {
      await api("/v1/config", {
        method: "PUT",
        body: { revision: state.config?.revision || null, budget },
      });
      state.config = await api("/v1/config");
      setFormMessage("#budget-message", "已保存", "success");
    } catch (error) {
      setFormMessage("#budget-message", problemText(error), "error");
    } finally {
      button.disabled = false;
    }
  }

  function openKeyDialog(message = "") {
    const dialog = $("#key-dialog");
    const input = $("#admin-key");
    input.value = "";
    input.placeholder = localStorage.getItem("polygate_key") ? "已保存" : "change-me";
    setFormMessage("#key-message", message, message ? "error" : "");
    if (!dialog.open) dialog.showModal();
    input.focus();
  }

  function saveKey(event) {
    event.preventDefault();
    const value = $("#admin-key").value.trim();
    if (!value) return;
    localStorage.setItem("polygate_key", value);
    state.key = value;
    $("#key-dialog").close();
    showToast("密钥已保存");
    state.uiState = {};
    state.status = null;
    state.config = null;
    loadPage(state.page, true);
  }

  function clearKey() {
    localStorage.removeItem("polygate_key");
    state.key = "change-me";
    $("#admin-key").value = "";
    $("#admin-key").placeholder = "change-me";
    $("#key-dialog").close();
    state.uiState = {};
    state.status = null;
    state.config = null;
    showToast("已恢复默认密钥");
    loadPage(state.page, true);
  }

  function closeNearestDialog(button) {
    const dialog = button.closest("dialog");
    if (dialog) dialog.close();
  }

  async function copyRequestId() {
    const value = state.activeLogicalId || state.activeRequestId;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      showToast("已复制");
    } catch {
      showToast("复制失败");
    }
  }

  function useInDiagnostics() {
    if (!state.activeRequestId) return;
    $("#replay-id").value = state.activeRequestId;
    $("#request-dialog").close();
    if (state.page === "diagnostics") {
      $("#replay-id").scrollIntoView({ block: "center" });
      $("#replay-id").focus();
    } else {
      window.location.hash = "diagnostics";
      window.setTimeout(() => $("#replay-id").focus(), 0);
    }
  }

  function bindEvents() {
    window.addEventListener("hashchange", () => applyPage(routeFromHash()));
    $('[data-action="refresh"]').addEventListener("click", () => loadPage(state.page, true));
    $("#history-days").addEventListener("change", (event) => {
      state.historyDays = Number(event.target.value);
      loadUsage();
    });
    $("#request-filters").addEventListener("submit", (event) => {
      event.preventDefault();
      state.requestFilters = {
        model: $("#filter-model").value.trim(),
        client: $("#filter-client").value.trim(),
        status: $("#filter-status").value,
      };
      state.requestOffset = 0;
      loadPage("requests", true);
    });
    $("#requests-prev").addEventListener("click", () => {
      state.requestOffset = Math.max(0, state.requestOffset - state.requestLimit);
      loadPage("requests", true);
    });
    $("#requests-next").addEventListener("click", () => {
      state.requestOffset += state.requestLimit;
      loadPage("requests", true);
    });
    $("#compat-form").addEventListener("submit", submitCompatibility);
    $("#replay-form").addEventListener("submit", submitReplay);
    $("#provider-form").addEventListener("submit", addProvider);
    $("#test-new-provider").addEventListener("click", testNewProvider);
    $("#client-key-form").addEventListener("submit", saveClientKey);
    $("#model-form").addEventListener("submit", submitModel);
    $("#model-form").addEventListener("input", () => {
      if (state.validatedModelSignature) resetModelValidation();
    });
    $("#budget-form").addEventListener("submit", saveBudget);
    $("#key-form").addEventListener("submit", saveKey);
    $("#save-providers").addEventListener("click", saveProviders);
    $("#reset-config").addEventListener("click", resetConfiguration);
    $("#new-client-key").addEventListener("click", () => openClientKeyDialog());
    $("#new-model").addEventListener("click", () => openModelDialog());
    $("#add-model-route").addEventListener("click", () => {
      $("#model-routes").insertAdjacentHTML("beforeend", routeEditorRow());
      resetModelValidation();
    });
    $("#new-preset").addEventListener("change", (event) => {
      const preset = event.target.value;
      if (preset && state.config?.presets?.[preset]) $("#new-url").value = state.config.presets[preset];
    });

    document.addEventListener("click", (event) => {
      const navLink = event.target.closest("[data-page]");
      if (navLink) setNavOpen(false);
      const requestButton = event.target.closest("[data-request-id]");
      if (requestButton) openRequest(requestButton.dataset.requestId);
      const requestGroup = event.target.closest("[data-request-group]");
      if (requestGroup) openRequestGroup(requestGroup.dataset.requestGroup);
      const keyAction = event.target.closest("[data-key-action]");
      if (keyAction) {
        const key = state.clientKeys.find((item) => item.id === keyAction.dataset.keyId);
        if (!key) return;
        if (keyAction.dataset.keyAction === "edit") openClientKeyDialog(key.id);
        if (keyAction.dataset.keyAction === "rotate") rotateClientKey(key);
        if (keyAction.dataset.keyAction === "revoke") revokeClientKey(key);
      }
      const modelAction = event.target.closest("[data-model-action]");
      if (modelAction?.dataset.modelAction === "edit") openModelDialog(modelAction.dataset.modelName);
      const routeRemove = event.target.closest("[data-route-remove]");
      if (routeRemove) {
        routeRemove.closest(".route-editor-row")?.remove();
        resetModelValidation();
      }
      const providerTest = event.target.closest("[data-provider-test]");
      if (providerTest) testProvider(providerTest.dataset.providerTest, providerTest);
      const compatApply = event.target.closest("[data-compat-apply]");
      if (compatApply) applyCompatibility(compatApply);
      const action = event.target.closest("[data-action]");
      if (!action) return;
      if (action.dataset.action === "skip-main") {
        event.preventDefault();
        $("#main").focus();
      }
      if (action.dataset.action === "open-nav") setNavOpen(true);
      if (action.dataset.action === "close-nav") setNavOpen(false, { restoreFocus: true });
      if (action.dataset.action === "open-key") openKeyDialog();
      if (action.dataset.action === "clear-key") clearKey();
      if (action.dataset.action === "copy-request-id") copyRequestId();
      if (action.dataset.action === "copy-client-secret") copyClientSecret();
      if (action.dataset.action === "use-in-diagnostics") useInDiagnostics();
      if (action.dataset.action === "close-dialog") closeNearestDialog(action);
    });

    $$("dialog").forEach((dialog) => {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
    });

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        window.clearInterval(state.refreshTimer);
        state.refreshTimer = null;
      } else {
        startRefreshTimer();
        if (state.page === "overview") loadPage("overview", true);
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && document.body.classList.contains("nav-open")) {
        setNavOpen(false, { restoreFocus: true });
      }
    });
  }

  function startRefreshTimer() {
    window.clearInterval(state.refreshTimer);
    state.refreshTimer = window.setInterval(() => {
      if (!document.hidden && state.page === "overview" && !state.loading.has("overview")) {
        loadPage("overview", true);
      }
    }, 20000);
  }

  function init() {
    bindEvents();
    applyPage(routeFromHash());
    startRefreshTimer();
  }

  init();
})();
