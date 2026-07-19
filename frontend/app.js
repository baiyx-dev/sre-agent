const messagesEl = document.getElementById("messages");
const stepsContainerEl = document.getElementById("stepsContainer");
const timelineContainerEl = document.getElementById("timelineContainer");
const postmortemContainerEl = document.getElementById("postmortemContainer");
const messageInputEl = document.getElementById("messageInput");
const sendBtnEl = document.getElementById("sendBtn");
const dataApiBaseInputEl = document.getElementById("dataApiBaseInput");
const dataApiTokenInputEl = document.getElementById("dataApiTokenInput");
const prometheusBaseInputEl = document.getElementById("prometheusBaseInput");
const prometheusTokenInputEl = document.getElementById("prometheusTokenInput");
const prometheusServiceLabelInputEl = document.getElementById("prometheusServiceLabelInput");
const lokiBaseInputEl = document.getElementById("lokiBaseInput");
const lokiTokenInputEl = document.getElementById("lokiTokenInput");
const lokiServiceLabelInputEl = document.getElementById("lokiServiceLabelInput");
const promQueryUpInputEl = document.getElementById("promQueryUpInput");
const promQueryReplicasInputEl = document.getElementById("promQueryReplicasInput");
const promQueryErrorRateInputEl = document.getElementById("promQueryErrorRateInput");
const promQueryCpuInputEl = document.getElementById("promQueryCpuInput");
const promQueryMemoryInputEl = document.getElementById("promQueryMemoryInput");
const promQueryLatencyInputEl = document.getElementById("promQueryLatencyInput");
const promAlertQueryInputEl = document.getElementById("promAlertQueryInput");
const lokiQueryTemplateInputEl = document.getElementById("lokiQueryTemplateInput");
const saveDataApiBaseBtnEl = document.getElementById("saveDataApiBaseBtn");
const testDataApiBtnEl = document.getElementById("testDataApiBtn");
const dataApiBaseStatusEl = document.getElementById("dataApiBaseStatus");
const knownServicesEl = document.getElementById("knownServices");
const serviceNameOptionsEl = document.getElementById("serviceNameOptions");
const targetNameInputEl = document.getElementById("targetNameInput");
const targetUrlInputEl = document.getElementById("targetUrlInput");
const addTargetBtnEl = document.getElementById("addTargetBtn");
const targetsListEl = document.getElementById("targetsList");
const openOnboardingBtnEl = document.getElementById("openOnboardingBtn");
const onboardingModalEl = document.getElementById("onboardingModal");
const onboardingCloseBtnEl = document.getElementById("onboardingCloseBtn");
const apiKeyInputEl = document.getElementById("apiKeyInput");
const guardTokenInputEl = document.getElementById("guardTokenInput");
const connectApiBtnEl = document.getElementById("connectApiBtn");
const authStatusEl = document.getElementById("authStatus");
const workspaceNameEl = document.getElementById("workspaceName");
const workspacePlanEl = document.getElementById("workspacePlan");
const workspaceUsageTextEl = document.getElementById("workspaceUsageText");
const workspaceUsageBarEl = document.getElementById("workspaceUsageBar");
const workspaceUsageHintEl = document.getElementById("workspaceUsageHint");
const workspaceEntitlementEl = document.getElementById("workspaceEntitlement");

const confirmModalEl = document.getElementById("confirmModal");
const modalActionTypeEl = document.getElementById("modalActionType");
const modalServiceNameEl = document.getElementById("modalServiceName");
const modalPolicySummaryEl = document.getElementById("modalPolicySummary");
const modalConfirmBtnEl = document.getElementById("modalConfirmBtn");
const modalCancelBtnEl = document.getElementById("modalCancelBtn");

let currentPendingAction = null;
let sreApiKey = "";
let executionGuardToken = "";
const chatSessionId = getOrCreateChatSessionId();

try {
  sreApiKey = window.sessionStorage.getItem("sre-agent-api-key") || "";
} catch (error) {
}
if (apiKeyInputEl) apiKeyInputEl.value = sreApiKey;

function apiFetch(resource, options = {}) {
  const headers = new Headers(options.headers || {});
  if (sreApiKey) headers.set("X-SRE-API-Key", sreApiKey);
  return window.fetch(resource, { ...options, headers });
}

async function verifyAccess() {
  try {
    const response = await apiFetch("/auth/me");
    const data = await response.json();
    if (!response.ok) {
      if (authStatusEl) authStatusEl.textContent = data.detail || "认证失败";
      return false;
    }
    if (authStatusEl) authStatusEl.textContent = `已连接：${data.role} · ${data.workspace_id || "default"}`;
    return true;
  } catch (error) {
    if (authStatusEl) authStatusEl.textContent = "无法连接后端";
    return false;
  }
}

async function fetchWorkspaceUsage() {
  if (!workspaceNameEl || !workspaceUsageTextEl) return;
  try {
    const [workspaceResponse, usageResponse] = await Promise.all([
      apiFetch("/workspace"),
      apiFetch("/billing/usage"),
    ]);
    const workspaceData = await workspaceResponse.json();
    const usageData = await usageResponse.json();
    if (!workspaceResponse.ok || !usageResponse.ok) {
      workspaceNameEl.textContent = usageData.detail || workspaceData.detail || "读取失败";
      return;
    }
    const workspace = workspaceData.workspace || {};
    const used = Number(usageData.requests_used || 0);
    const limit = Number(usageData.monthly_request_limit || 0);
    const percent = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
    workspaceNameEl.textContent = workspace.name || workspace.id || "default";
    if (workspacePlanEl) workspacePlanEl.textContent = workspace.plan || usageData.plan || "-";
    workspaceUsageTextEl.textContent = limit > 0
      ? `${used.toLocaleString()} / ${limit.toLocaleString()}`
      : `${used.toLocaleString()} / 不限`;
    if (workspaceUsageBarEl) {
      workspaceUsageBarEl.style.width = limit > 0 ? `${percent}%` : "0%";
      workspaceUsageBarEl.classList.toggle("near-limit", limit > 0 && percent >= 80);
    }
    if (workspaceUsageHintEl) {
      workspaceUsageHintEl.textContent = usageData.limit_reached
        ? "额度已用尽；普通请求会返回 429"
        : `UTC 月度额度${limit > 0 ? `，剩余 ${Math.max(0, limit - used).toLocaleString()}` : "，当前不限量"}`;
    }
    if (workspaceEntitlementEl) {
      const entitlements = workspace.entitlements || usageData.entitlements || {};
      const writeAccess = entitlements.production_writes
        ? "含生产变更"
        : "仅诊断与 dry-run";
      const maxKeys = Number(entitlements.max_workspace_api_keys || 0);
      workspaceEntitlementEl.textContent = `${writeAccess} · API Key ${maxKeys > 0 ? `最多 ${maxKeys} 把` : "不限"}`;
    }
  } catch (error) {
    workspaceNameEl.textContent = "读取失败";
  }
}

const intentLabels = {
  status_query: "状态检查",
  troubleshoot: "故障排查",
  deploy: "部署变更",
  rollback: "回滚操作",
};

const stepActionLabels = {
  get_recent_alerts: "检查告警",
  get_service_status: "确认服务状态",
  get_service_metrics: "查看核心指标",
  get_recent_logs: "查看近期日志",
  get_recent_deploy_context: "核对最近变更",
  get_k8s_observability: "查看 K8s 运行态",
  deploy_service: "执行部署",
  rollback_service: "执行回滚",
};

function generateSessionId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function getOrCreateChatSessionId() {
  try {
    const existing = window.localStorage.getItem("sre-agent-chat-session-id");
    if (existing) return existing;
    const nextId = generateSessionId();
    window.localStorage.setItem("sre-agent-chat-session-id", nextId);
    return nextId;
  } catch (error) {
    return generateSessionId();
  }
}

function normalizeUrlInput(value) {
  const raw = (value || "").trim();
  if (!raw) return "";
  if (raw.startsWith("http://") || raw.startsWith("https://")) return raw;
  return `http://${raw}`;
}

function setConfigStatus(message) {
  if (dataApiBaseStatusEl) {
    dataApiBaseStatusEl.textContent = message;
  }
}

function appendMessage(role, text) {
  const div = document.createElement("div");
  div.className = `message ${role}`;
  div.textContent = `${role === "user" ? "你" : "Agent"}：${text}`;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendAnalysisNote(data) {
  if (!data || !data.used_fallback) return;
  appendMessage("assistant", "本次结论基于当前可获取的规则和观测数据生成，建议结合真实监控面板继续确认。");
}

function formatValue(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "number") return Number.isInteger(value) ? `${value}` : value.toFixed(2);
  if (typeof value === "boolean") return value ? "是" : "否";
  return `${value}`;
}

function escapeHtml(value) {
  return String(value ?? "-").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function safeCssToken(value) {
  return String(value || "unknown").replace(/[^a-zA-Z0-9_-]/g, "");
}

function getStepTitle(action) {
  return stepActionLabels[action] || action || "处理步骤";
}

function renderAssessmentCard(assessment) {
  if (!assessment) return "";

  const hypotheses = Array.isArray(assessment.hypotheses) ? assessment.hypotheses : [];
  const evidence = Array.isArray(assessment.evidence) ? assessment.evidence : [];
  const missingSignals = Array.isArray(assessment.missing_signals) ? assessment.missing_signals : [];
  const nextActions = Array.isArray(assessment.next_actions) ? assessment.next_actions : [];

  return `
    <div class="assessment-card">
      <div class="assessment-header">
        <div>
          <div class="assessment-eyebrow">诊断视图</div>
          <div class="assessment-summary">${escapeHtml(assessment.summary || "-")}</div>
        </div>
        <div class="assessment-badges">
          <span class="assessment-badge severity-${safeCssToken(assessment.severity_assessment)}">风险 ${escapeHtml(assessment.severity_assessment || "-")}</span>
          <span class="assessment-badge confidence-${safeCssToken(assessment.confidence)}">置信度 ${escapeHtml(assessment.confidence || "-")}</span>
        </div>
      </div>
      <div class="assessment-grid">
        <div class="assessment-section">
          <div class="assessment-title">关键证据</div>
          <ul>${evidence.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>-</li>"}</ul>
        </div>
        <div class="assessment-section">
          <div class="assessment-title">根因候选</div>
          <ul>${hypotheses.map((item) => `<li>${escapeHtml(item.hypothesis)}（${escapeHtml(item.confidence)}）: ${escapeHtml(item.rationale)}</li>`).join("") || "<li>-</li>"}</ul>
        </div>
        <div class="assessment-section">
          <div class="assessment-title">缺失信号</div>
          <ul>${missingSignals.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>-</li>"}</ul>
        </div>
        <div class="assessment-section">
          <div class="assessment-title">建议下一步</div>
          <ul>${nextActions.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>-</li>"}</ul>
        </div>
      </div>
    </div>
  `;
}

function summarizeStepResult(action, result) {
  if (!result) {
    return {
      summary: "本步骤没有返回可展示的信息。",
      bullets: [],
      raw: null,
    };
  }

  if (action === "get_service_status" && typeof result === "object" && !Array.isArray(result)) {
    return {
      summary: `服务 ${result.name || "-"} 当前为 ${result.status || "-"}。`,
      bullets: [
        `版本：${formatValue(result.version)}`,
        `错误率：${formatValue(result.error_rate)}%`,
        `副本数：${formatValue(result.replicas)}`,
        `延迟：${result.latency_ms ? `${formatValue(result.latency_ms)} ms` : "-"}`,
      ],
      raw: result,
    };
  }

  if (action === "get_service_metrics" && typeof result === "object" && !Array.isArray(result)) {
    return {
      summary: `已读取 ${result.service || "-"} 的核心运行指标。`,
      bullets: [
        `CPU：${formatValue(result.cpu)}%`,
        `内存：${formatValue(result.memory)}%`,
        `错误率：${formatValue(result.error_rate)}%`,
        `状态：${formatValue(result.status)}`,
      ],
      raw: result,
    };
  }

  if (action === "get_recent_alerts" && Array.isArray(result)) {
    if (result.length === 0) {
      return {
        summary: "当前没有发现未恢复告警。",
        bullets: [],
        raw: result,
      };
    }
    return {
      summary: `发现 ${result.length} 条近期告警。`,
      bullets: result.slice(0, 3).map((item) => `${item.severity || "info"} · ${item.service || "-"} · ${item.title || item.message || "-"}`),
      raw: result,
    };
  }

  if (action === "get_recent_logs" && Array.isArray(result)) {
    if (result.length === 0) {
      return {
        summary: "没有读取到近期日志。",
        bullets: [],
        raw: result,
      };
    }
    return {
      summary: `已查看 ${result.length} 条近期日志。`,
      bullets: result.slice(0, 3).map((item) => `${item.level || "INFO"} · ${item.message || "-"}`),
      raw: result,
    };
  }

  if (action === "get_recent_deploy_context" && Array.isArray(result)) {
    if (result.length === 0) {
      return {
        summary: "最近没有读取到相关变更记录。",
        bullets: [],
        raw: result,
      };
    }
    return {
      summary: `已核对 ${result.length} 条最近变更。`,
      bullets: result.slice(0, 3).map((item) => `${item.created_at || "-"} · ${item.old_version || "-"} -> ${item.new_version || "-"} · ${item.status || "-"}`),
      raw: result,
    };
  }

  if ((action === "deploy_service" || action === "rollback_service") && typeof result === "object" && !Array.isArray(result)) {
    return {
      summary: `操作结果：${result.status || result.message || "已完成"}`,
      bullets: Object.entries(result)
        .slice(0, 4)
        .map(([key, value]) => `${key}: ${formatValue(value)}`),
      raw: result,
    };
  }

  return {
    summary: "已完成该步骤，更多细节可展开查看原始结果。",
    bullets: [],
    raw: result,
  };
}

function renderSteps(steps, assessmentDetails = null) {
  stepsContainerEl.innerHTML = "";

  if (!steps || steps.length === 0) {
    stepsContainerEl.innerHTML = `<p class="empty">没有可展示的执行步骤。</p>`;
    return;
  }

  if (assessmentDetails) {
    stepsContainerEl.innerHTML = renderAssessmentCard(assessmentDetails);
  }

  steps.forEach((step) => {
    const presentation = summarizeStepResult(step.action, step.result);
    const wrapper = document.createElement("div");
    wrapper.className = "step-item";

    const title = document.createElement("div");
    title.className = "step-title";
    title.textContent = `${step.step}. ${getStepTitle(step.action)}`;

    const summary = document.createElement("div");
    summary.className = "step-summary";
    summary.textContent = presentation.summary;

    wrapper.appendChild(title);
    wrapper.appendChild(summary);

    if (presentation.bullets.length > 0) {
      const highlights = document.createElement("ul");
      highlights.className = "step-highlights";
      presentation.bullets.forEach((item) => {
        const li = document.createElement("li");
        li.textContent = item;
        highlights.appendChild(li);
      });
      wrapper.appendChild(highlights);
    }

    if (presentation.raw !== null) {
      const details = document.createElement("details");
      details.className = "step-details";

      const detailsSummary = document.createElement("summary");
      detailsSummary.textContent = "查看原始结果";

      const result = document.createElement("pre");
      result.className = "step-result";
      result.textContent = JSON.stringify(presentation.raw, null, 2);

      details.appendChild(detailsSummary);
      details.appendChild(result);
      wrapper.appendChild(details);
    }

    stepsContainerEl.appendChild(wrapper);
  });
}

function closeConfirmationModal() {
  confirmModalEl.classList.add("hidden");
  currentPendingAction = null;
}

function openOnboardingModal() {
  if (!onboardingModalEl) return;
  onboardingModalEl.classList.remove("hidden");
}

function closeOnboardingModal() {
  if (!onboardingModalEl) return;
  onboardingModalEl.classList.add("hidden");
}

function openConfirmationModal(pendingAction) {
  currentPendingAction = pendingAction;
  modalActionTypeEl.textContent = pendingAction.action_type || pendingAction.action || "未知操作";
  modalServiceNameEl.textContent = pendingAction.service_name || "未知服务";
  if (modalPolicySummaryEl) {
    modalPolicySummaryEl.textContent = (pendingAction.policy_decision && pendingAction.policy_decision.summary) || "本次操作将进入执行前确认。";
  }
  confirmModalEl.classList.remove("hidden");
}

function renderTimeline(timeline) {
  timelineContainerEl.innerHTML = "";

  if (!timeline || timeline.length === 0) {
    timelineContainerEl.innerHTML = `<p class="empty">暂无任务记录。</p>`;
    return;
  }

  timeline.forEach((item) => {
    const wrapper = document.createElement("div");
    wrapper.className = "timeline-item";

    const meta = document.createElement("div");
    meta.className = "timeline-meta";
    meta.textContent = `#${item.id} · ${item.created_at} · ${intentLabels[item.intent] || item.intent}`;

    const message = document.createElement("div");
    message.className = "timeline-message";
    message.textContent = `任务：${item.user_message}`;

    const answer = document.createElement("div");
    answer.className = "timeline-answer";
    answer.textContent = `结果：${item.final_answer}`;

    const steps = document.createElement("div");
    steps.className = "timeline-steps";
    steps.textContent = `步骤数：${(item.steps || []).length}`;

    const actions = document.createElement("div");
    actions.className = "timeline-actions";
    actions.innerHTML = `<button class="btn-timeline" data-task-run-id="${item.id}">查看复盘</button>`;

    wrapper.appendChild(meta);
    wrapper.appendChild(message);
    wrapper.appendChild(answer);
    wrapper.appendChild(steps);
    wrapper.appendChild(actions);
    timelineContainerEl.appendChild(wrapper);
  });
}

function formatList(values) {
  if (!values || values.length === 0) return "-";
  return values.map((value) => escapeHtml(value)).join("；");
}

function renderPostmortem(postmortem) {
  const impact = postmortem.impact || {};
  const impactSummary = [
    `告警 ${formatValue(impact.alert_count)} 条`,
    `未恢复 ${formatValue(impact.unresolved_alert_count)} 条`,
    `错误日志 ${formatValue(impact.error_log_count)} 条`,
    `相关变更 ${formatValue(impact.deployment_count)} 次`,
  ].join("，");

  postmortemContainerEl.innerHTML = `
    <div class="postmortem-grid">
      <div class="postmortem-section">
        <div class="postmortem-heading">事件概述</div>
        <div class="postmortem-item">${escapeHtml(postmortem.narrative_summary || postmortem.summary || "-")}</div>
      </div>
      <div class="postmortem-section">
        <div class="postmortem-heading">基础信息</div>
        <div class="postmortem-item"><span class="postmortem-label">服务</span>${escapeHtml(postmortem.service_name || "-")}</div>
        <div class="postmortem-item"><span class="postmortem-label">事件类型</span>${escapeHtml(postmortem.incident_type || "-")}</div>
        <div class="postmortem-item"><span class="postmortem-label">当前状态</span>${escapeHtml(postmortem.current_status || "-")}</div>
      </div>
      <div class="postmortem-section">
        <div class="postmortem-heading">影响范围</div>
        <div class="postmortem-item">${escapeHtml(impactSummary)}</div>
      </div>
      <div class="postmortem-section">
        <div class="postmortem-heading">现象</div>
        <div class="postmortem-item">${formatList(postmortem.symptoms || [])}</div>
      </div>
      <div class="postmortem-section">
        <div class="postmortem-heading">疑似根因</div>
        <div class="postmortem-item">${escapeHtml(postmortem.likely_root_cause || "-")}</div>
      </div>
      <div class="postmortem-section">
        <div class="postmortem-heading">已采取动作</div>
        <div class="postmortem-item">${formatList(postmortem.actions_taken || [])}</div>
      </div>
      <div class="postmortem-section">
        <div class="postmortem-heading">后续行动</div>
        <div class="postmortem-item">${formatList(postmortem.follow_ups || [])}</div>
      </div>
    </div>
  `;
}

async function fetchPostmortem(taskRunId) {
  try {
    const response = await apiFetch(`/postmortem?task_run_id=${taskRunId}`);
    const data = await response.json();
    if (!response.ok) {
      postmortemContainerEl.innerHTML = `<p class="empty">复盘生成失败。</p>`;
      return;
    }
    renderPostmortem(data.postmortem || {});
  } catch (error) {
    postmortemContainerEl.innerHTML = `<p class="empty">复盘生成失败。</p>`;
  }
}

async function fetchTimeline() {
  try {
    const response = await apiFetch("/timeline?limit=20");
    const data = await response.json();
    if (!response.ok) {
      return;
    }
    renderTimeline(data.timeline || []);
  } catch (error) {
  }
}

async function loadDataSourceConfig() {
  if (!dataApiBaseInputEl) return;
  try {
    const response = await apiFetch("/settings/data-source");
    const data = await response.json();
    if (!response.ok) return;
    dataApiBaseInputEl.value = data.sre_data_api_base || "";
    const databaseSecretsAllowed = data.secret_storage_mode === "insecure_database_opt_in";
    if (dataApiTokenInputEl) {
      dataApiTokenInputEl.value = "";
      dataApiTokenInputEl.placeholder = databaseSecretsAllowed
        ? (data.sre_data_api_token_configured ? "已配置；留空不修改" : "可选 Token")
        : (data.sre_data_api_token_configured ? "已通过环境 Secret 配置" : "请通过环境 Secret 配置");
    }
    if (prometheusBaseInputEl) {
      prometheusBaseInputEl.value = data.prometheus_base_url || "";
    }
    if (prometheusTokenInputEl) {
      prometheusTokenInputEl.value = "";
      prometheusTokenInputEl.placeholder = databaseSecretsAllowed
        ? (data.prometheus_token_configured ? "已配置；留空不修改" : "可选 Token")
        : (data.prometheus_token_configured ? "已通过环境 Secret 配置" : "请通过环境 Secret 配置");
    }
    if (prometheusServiceLabelInputEl) {
      prometheusServiceLabelInputEl.value = data.prometheus_service_label || "";
    }
    if (lokiBaseInputEl) {
      lokiBaseInputEl.value = data.loki_base_url || "";
    }
    if (lokiTokenInputEl) {
      lokiTokenInputEl.value = "";
      lokiTokenInputEl.placeholder = databaseSecretsAllowed
        ? (data.loki_token_configured ? "已配置；留空不修改" : "可选 Token")
        : (data.loki_token_configured ? "已通过环境 Secret 配置" : "请通过环境 Secret 配置");
    }
    if (lokiServiceLabelInputEl) {
      lokiServiceLabelInputEl.value = data.loki_service_label || "";
    }
    if (promQueryUpInputEl) {
      promQueryUpInputEl.value = data.prom_query_up || "";
    }
    if (promQueryReplicasInputEl) {
      promQueryReplicasInputEl.value = data.prom_query_replicas || "";
    }
    if (promQueryErrorRateInputEl) {
      promQueryErrorRateInputEl.value = data.prom_query_error_rate || "";
    }
    if (promQueryCpuInputEl) {
      promQueryCpuInputEl.value = data.prom_query_cpu || "";
    }
    if (promQueryMemoryInputEl) {
      promQueryMemoryInputEl.value = data.prom_query_memory || "";
    }
    if (promQueryLatencyInputEl) {
      promQueryLatencyInputEl.value = data.prom_query_latency_p95_ms || "";
    }
    if (promAlertQueryInputEl) {
      promAlertQueryInputEl.value = data.prom_alert_query || "";
    }
    if (lokiQueryTemplateInputEl) {
      lokiQueryTemplateInputEl.value = data.loki_query_template || "";
    }
    const configured = [
      data.sre_data_api_base ? "SRE API" : null,
      data.prometheus_base_url ? "Prometheus" : null,
      data.loki_base_url ? "Loki" : null,
    ].filter(Boolean);
    setConfigStatus(configured.length > 0
      ? `已加载当前配置：${configured.join(" / ")}`
      : "当前未配置，将使用后端环境变量");
  } catch (error) {
    setConfigStatus("配置加载失败");
  }
}

async function saveDataSourceConfig() {
  if (!dataApiBaseInputEl) return;
  const baseValue = normalizeUrlInput(dataApiBaseInputEl.value);
  const tokenValue = dataApiTokenInputEl ? dataApiTokenInputEl.value.trim() : "";
  const prometheusBaseValue = prometheusBaseInputEl ? normalizeUrlInput(prometheusBaseInputEl.value) : "";
  const prometheusTokenValue = prometheusTokenInputEl ? prometheusTokenInputEl.value.trim() : "";
  const prometheusServiceLabelValue = prometheusServiceLabelInputEl ? prometheusServiceLabelInputEl.value.trim() : "";
  const lokiBaseValue = lokiBaseInputEl ? normalizeUrlInput(lokiBaseInputEl.value) : "";
  const lokiTokenValue = lokiTokenInputEl ? lokiTokenInputEl.value.trim() : "";
  const lokiServiceLabelValue = lokiServiceLabelInputEl ? lokiServiceLabelInputEl.value.trim() : "";
  const promQueryUpValue = promQueryUpInputEl ? promQueryUpInputEl.value.trim() : "";
  const promQueryReplicasValue = promQueryReplicasInputEl ? promQueryReplicasInputEl.value.trim() : "";
  const promQueryErrorRateValue = promQueryErrorRateInputEl ? promQueryErrorRateInputEl.value.trim() : "";
  const promQueryCpuValue = promQueryCpuInputEl ? promQueryCpuInputEl.value.trim() : "";
  const promQueryMemoryValue = promQueryMemoryInputEl ? promQueryMemoryInputEl.value.trim() : "";
  const promQueryLatencyValue = promQueryLatencyInputEl ? promQueryLatencyInputEl.value.trim() : "";
  const promAlertQueryValue = promAlertQueryInputEl ? promAlertQueryInputEl.value.trim() : "";
  const lokiQueryTemplateValue = lokiQueryTemplateInputEl ? lokiQueryTemplateInputEl.value.trim() : "";
  dataApiBaseInputEl.value = baseValue;
  if (prometheusBaseInputEl) {
    prometheusBaseInputEl.value = prometheusBaseValue;
  }
  if (lokiBaseInputEl) {
    lokiBaseInputEl.value = lokiBaseValue;
  }
  const payload = {
    sre_data_api_base: baseValue || null,
    prometheus_base_url: prometheusBaseValue || null,
    prometheus_service_label: prometheusServiceLabelValue || null,
    loki_base_url: lokiBaseValue || null,
    loki_service_label: lokiServiceLabelValue || null,
    prom_query_up: promQueryUpValue || null,
    prom_query_replicas: promQueryReplicasValue || null,
    prom_query_error_rate: promQueryErrorRateValue || null,
    prom_query_cpu: promQueryCpuValue || null,
    prom_query_memory: promQueryMemoryValue || null,
    prom_query_latency_p95_ms: promQueryLatencyValue || null,
    prom_alert_query: promAlertQueryValue || null,
    loki_query_template: lokiQueryTemplateValue || null,
  };
  if (tokenValue) payload.sre_data_api_token = tokenValue;
  if (prometheusTokenValue) payload.prometheus_token = prometheusTokenValue;
  if (lokiTokenValue) payload.loki_token = lokiTokenValue;
  setConfigStatus("保存中...");
  try {
    const response = await apiFetch("/settings/data-source", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      setConfigStatus(data.detail || "保存失败");
      return;
    }
    const configured = [
      data.sre_data_api_base ? "SRE API" : null,
      data.prometheus_base_url ? "Prometheus" : null,
      data.loki_base_url ? "Loki" : null,
    ].filter(Boolean);
    setConfigStatus(configured.length > 0
      ? `保存成功，已启用：${configured.join(" / ")}`
      : "已清空，后续请求将使用后端环境变量");
    await loadDataSourceConfig();
    await fetchKnownServices();
  } catch (error) {
    setConfigStatus("保存失败");
  }
}

async function testDataSourceConfig() {
  if (!dataApiBaseInputEl) return;
  const baseValue = normalizeUrlInput(dataApiBaseInputEl.value);
  const tokenValue = dataApiTokenInputEl ? dataApiTokenInputEl.value.trim() : "";
  const prometheusBaseValue = prometheusBaseInputEl ? normalizeUrlInput(prometheusBaseInputEl.value) : "";
  const prometheusTokenValue = prometheusTokenInputEl ? prometheusTokenInputEl.value.trim() : "";
  const prometheusServiceLabelValue = prometheusServiceLabelInputEl ? prometheusServiceLabelInputEl.value.trim() : "";
  const lokiBaseValue = lokiBaseInputEl ? normalizeUrlInput(lokiBaseInputEl.value) : "";
  const lokiTokenValue = lokiTokenInputEl ? lokiTokenInputEl.value.trim() : "";
  const lokiServiceLabelValue = lokiServiceLabelInputEl ? lokiServiceLabelInputEl.value.trim() : "";
  const promQueryUpValue = promQueryUpInputEl ? promQueryUpInputEl.value.trim() : "";
  const promQueryReplicasValue = promQueryReplicasInputEl ? promQueryReplicasInputEl.value.trim() : "";
  const promQueryErrorRateValue = promQueryErrorRateInputEl ? promQueryErrorRateInputEl.value.trim() : "";
  const promQueryCpuValue = promQueryCpuInputEl ? promQueryCpuInputEl.value.trim() : "";
  const promQueryMemoryValue = promQueryMemoryInputEl ? promQueryMemoryInputEl.value.trim() : "";
  const promQueryLatencyValue = promQueryLatencyInputEl ? promQueryLatencyInputEl.value.trim() : "";
  const promAlertQueryValue = promAlertQueryInputEl ? promAlertQueryInputEl.value.trim() : "";
  const lokiQueryTemplateValue = lokiQueryTemplateInputEl ? lokiQueryTemplateInputEl.value.trim() : "";
  dataApiBaseInputEl.value = baseValue;
  if (prometheusBaseInputEl) {
    prometheusBaseInputEl.value = prometheusBaseValue;
  }
  if (lokiBaseInputEl) {
    lokiBaseInputEl.value = lokiBaseValue;
  }
  setConfigStatus("测试中...");
  try {
    const response = await apiFetch("/settings/data-source/test", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        sre_data_api_base: baseValue || null,
        sre_data_api_token: tokenValue || null,
        prometheus_base_url: prometheusBaseValue || null,
        prometheus_token: prometheusTokenValue || null,
        prometheus_service_label: prometheusServiceLabelValue || null,
        loki_base_url: lokiBaseValue || null,
        loki_token: lokiTokenValue || null,
        loki_service_label: lokiServiceLabelValue || null,
        prom_query_up: promQueryUpValue || null,
        prom_query_replicas: promQueryReplicasValue || null,
        prom_query_error_rate: promQueryErrorRateValue || null,
        prom_query_cpu: promQueryCpuValue || null,
        prom_query_memory: promQueryMemoryValue || null,
        prom_query_latency_p95_ms: promQueryLatencyValue || null,
        prom_alert_query: promAlertQueryValue || null,
        loki_query_template: lokiQueryTemplateValue || null,
      }),
    });
    const data = await response.json();
    if (!response.ok || !data) {
      setConfigStatus("连接测试失败");
      return;
    }
    if (data.ok) {
      const sources = Array.isArray(data.connected_sources) ? data.connected_sources.join(" / ") : "unknown";
      setConfigStatus(`连接成功：已连通 ${sources}`);
    } else {
      setConfigStatus(`连接失败：${data.message || "unknown_error"}`);
    }
  } catch (error) {
    setConfigStatus("连接测试失败");
  }
}

async function fetchKnownServices() {
  if (!knownServicesEl || !serviceNameOptionsEl) return;
  try {
    const response = await apiFetch("/services/");
    const data = await response.json();
    if (!response.ok) {
      knownServicesEl.textContent = "读取失败";
      return;
    }

    const services = Array.isArray(data.services) ? data.services : [];
    const names = services
      .map((svc) => (svc && svc.name ? svc.name : ""))
      .filter((name) => !!name);

    knownServicesEl.textContent = names.length > 0 ? names.join("，") : "暂无";

    serviceNameOptionsEl.innerHTML = "";
    names.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      serviceNameOptionsEl.appendChild(option);
    });
  } catch (error) {
    knownServicesEl.textContent = "读取失败";
  }
}

async function fetchTargets() {
  if (!targetsListEl) return;
  try {
    const response = await apiFetch("/settings/targets");
    const data = await response.json();
    if (!response.ok) {
      targetsListEl.textContent = "读取失败";
      return;
    }
    const targets = Array.isArray(data.targets) ? data.targets : [];
    if (targets.length === 0) {
      targetsListEl.textContent = "暂无已接入目标";
      try {
        if (!window.localStorage.getItem("sre-agent-onboarding-seen")) {
          openOnboardingModal();
        }
      } catch (error) {
      }
      return;
    }

    try {
      window.localStorage.setItem("sre-agent-onboarding-seen", "1");
    } catch (error) {
    }

    targetsListEl.replaceChildren();
    targets.forEach((target) => {
      const row = document.createElement("div");
      row.className = "target-item";
      const label = document.createElement("span");
      label.textContent = `${target.name} -> ${target.base_url}`;
      const button = document.createElement("button");
      button.textContent = "删除";
      button.dataset.targetName = target.name;
      row.appendChild(label);
      row.appendChild(button);
      targetsListEl.appendChild(row);
    });
  } catch (error) {
    targetsListEl.textContent = "读取失败";
  }
}

async function addTarget() {
  if (!targetNameInputEl || !targetUrlInputEl) return;
  const name = targetNameInputEl.value.trim();
  const baseUrl = normalizeUrlInput(targetUrlInputEl.value);
  if (!name || !baseUrl) {
    setConfigStatus("请填写服务名和服务地址");
    return;
  }
  targetUrlInputEl.value = baseUrl;
  setConfigStatus("添加中...");
  try {
    const response = await apiFetch("/settings/targets", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ name, base_url: baseUrl }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      setConfigStatus(data.detail || "添加失败");
      return;
    }
    targetNameInputEl.value = "";
    targetUrlInputEl.value = "";
    setConfigStatus(`已添加监测目标：${name}`);
    await fetchTargets();
    await fetchKnownServices();
    closeOnboardingModal();
  } catch (error) {
    setConfigStatus("添加失败");
  }
}

async function deleteTarget(name) {
  if (!name) return;
  try {
    const response = await apiFetch(`/settings/targets/${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      setConfigStatus(data.detail || "删除失败");
      return;
    }
    setConfigStatus(`已删除监测目标：${name}`);
    await fetchTargets();
    await fetchKnownServices();
  } catch (error) {
    setConfigStatus("删除失败");
  }
}

async function executeConfirmedAction(pendingAction) {
  const headers = { "Content-Type": "application/json" };
  if (executionGuardToken) headers["X-Guard-Token"] = executionGuardToken;
  const response = await apiFetch("/chat/confirm", {
    method: "POST",
    headers,
    body: JSON.stringify({
      session_id: chatSessionId,
      pending_action: pendingAction,
    }),
  });

  const data = await response.json();

  if (!response.ok) {
    appendMessage("assistant", data.detail || "请求失败");
    renderSteps([]);
    return;
  }

  appendMessage("assistant", data.final_answer || "没有返回结果");
  renderSteps(data.steps || [], data.assessment_details || null);
  appendAnalysisNote(data);
  if (data.execution_mode === "queued" && data.change_request_id) {
    await waitForChangeCompletion(data.change_request_id);
  }
  await fetchTimeline();
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForChangeCompletion(changeRequestId) {
  const terminalStatuses = new Set([
    "executed",
    "dry_run",
    "denied",
    "failed",
    "unknown",
    "cancelled",
    "expired",
  ]);
  for (let attempt = 0; attempt < 120; attempt += 1) {
    await delay(1000);
    try {
      const response = await apiFetch(`/changes/${encodeURIComponent(changeRequestId)}`);
      const data = await response.json();
      if (!response.ok) {
        appendMessage("assistant", data.detail || "变更状态查询失败");
        return;
      }
      const changeRequest = data.change_request || {};
      if (!terminalStatuses.has(changeRequest.status)) continue;

      const result = changeRequest.result || {};
      appendMessage(
        "assistant",
        result.final_answer || `变更请求已结束，状态：${changeRequest.status}`,
      );
      renderSteps(result.steps || [], result.assessment_details || null);
      appendAnalysisNote(result);
      return;
    } catch (error) {
      appendMessage("assistant", "变更状态查询失败，请稍后在变更记录中查看。");
      return;
    }
  }
  appendMessage("assistant", "变更仍在执行中，请稍后查询变更记录。");
}

async function sendMessage() {
  const message = messageInputEl.value.trim();
  if (!message) return;

  appendMessage("user", message);
  messageInputEl.value = "";

  try {
    const response = await apiFetch("/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        message,
        session_id: chatSessionId,
      }),
    });

    const data = await response.json();

    if (!response.ok) {
      appendMessage("assistant", data.detail || "请求失败");
      renderSteps([]);
      return;
    }

    appendMessage("assistant", data.final_answer || "没有返回结果");
    renderSteps(data.steps || [], data.assessment_details || null);
    appendAnalysisNote(data);
    await fetchTimeline();

    if (data.requires_confirmation && data.pending_action) {
      openConfirmationModal(data.pending_action);
    }
  } catch (error) {
    appendMessage("assistant", "请求失败，请检查后端是否正常运行。");
    renderSteps([]);
  }
}

modalConfirmBtnEl.addEventListener("click", async () => {
  if (!currentPendingAction) {
    closeConfirmationModal();
    return;
  }

  const actionToExecute = currentPendingAction;
  closeConfirmationModal();
  await executeConfirmedAction(actionToExecute);
});

modalCancelBtnEl.addEventListener("click", () => {
  appendMessage("assistant", "已取消操作");
  closeConfirmationModal();
});

confirmModalEl.addEventListener("click", (event) => {
  if (event.target === confirmModalEl) {
    appendMessage("assistant", "已取消操作");
    closeConfirmationModal();
  }
});

timelineContainerEl.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const taskRunId = target.dataset.taskRunId;
  if (!taskRunId) return;
  fetchPostmortem(taskRunId);
});

sendBtnEl.addEventListener("click", sendMessage);

messageInputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    sendMessage();
  }
});

if (saveDataApiBaseBtnEl) {
  saveDataApiBaseBtnEl.addEventListener("click", saveDataSourceConfig);
}

if (testDataApiBtnEl) {
  testDataApiBtnEl.addEventListener("click", testDataSourceConfig);
}

if (addTargetBtnEl) {
  addTargetBtnEl.addEventListener("click", addTarget);
}

if (openOnboardingBtnEl) {
  openOnboardingBtnEl.addEventListener("click", openOnboardingModal);
}

if (onboardingCloseBtnEl) {
  onboardingCloseBtnEl.addEventListener("click", closeOnboardingModal);
}

if (onboardingModalEl) {
  onboardingModalEl.addEventListener("click", (event) => {
    if (event.target === onboardingModalEl) {
      closeOnboardingModal();
    }
  });
}

if (targetsListEl) {
  targetsListEl.addEventListener("click", async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const name = target.dataset.targetName;
    if (!name) return;
    await deleteTarget(name);
  });
}

async function connectAndLoad() {
  sreApiKey = apiKeyInputEl ? apiKeyInputEl.value.trim() : "";
  executionGuardToken = guardTokenInputEl ? guardTokenInputEl.value.trim() : "";
  try {
    if (sreApiKey) {
      window.sessionStorage.setItem("sre-agent-api-key", sreApiKey);
    } else {
      window.sessionStorage.removeItem("sre-agent-api-key");
    }
  } catch (error) {
  }

  if (!(await verifyAccess())) return;
  await Promise.all([
    loadDataSourceConfig(),
    fetchTargets(),
    fetchKnownServices(),
    fetchTimeline(),
    fetchWorkspaceUsage(),
  ]);
}

if (connectApiBtnEl) {
  connectApiBtnEl.addEventListener("click", connectAndLoad);
}
if (apiKeyInputEl) {
  apiKeyInputEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") connectAndLoad();
  });
}

connectAndLoad();
