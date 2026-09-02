const defaultApiBase = "http://127.0.0.1:8000";

function apiBase() {
  return (import.meta.env.VITE_API_BASE || defaultApiBase).replace(/\/$/, "");
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || payload.message || `Request failed (${response.status}).`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export async function createWorkspace(name = "AI Workspace") {
  const response = await fetch(`${apiBase()}/workspaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return readJson(response);
}

export async function getWorkspaceState(workspaceId) {
  const response = await fetch(`${apiBase()}/workspaces/${encodeURIComponent(workspaceId)}/state`);
  return readJson(response);
}

export function getWorkspaceCodeFromUrl() {
  const url = new URL(window.location.href);
  const workspaceId = url.searchParams.get("w") || "";
  return /^\d{6}$/.test(workspaceId) ? workspaceId : "";
}

export function setWorkspaceCode(workspaceId) {
  const cleanCode = workspaceId.replace(/\D/g, "").slice(0, 6);
  if (!/^\d{6}$/.test(cleanCode)) return "";

  const url = new URL(window.location.href);
  url.searchParams.set("w", cleanCode);
  window.history.replaceState({}, "", url.toString());
  return cleanCode;
}

export function setUsername(username) {
  const cleanName = username.trim() || "Member A";
  const url = new URL(window.location.href);
  url.searchParams.set("name", cleanName);
  window.history.replaceState({}, "", url.toString());
  window.localStorage.setItem("ai-workspace-name", cleanName);
  return cleanName;
}

function routeMeta(session) {
  return { route: session.task_type, ...(session.route || {}) };
}

function snapshotFromState(state, members) {
  const sessions = state.sessions || [];
  return {
    files: state.files || {},
    lock_map: Object.fromEntries(
      (state.locks || []).map((lock) => [lock.target?.scope_key || "unknown", lock.owner_member_id])
    ),
    action_log: sessions
      .filter((session) => session.status === "completed")
      .map((session) => ({
        user: session.member_id,
        prompt: session.prompt,
        changed_functions: [],
        explanation: session.result_summary || "Session completed.",
        files: Object.keys(session.files || {}),
        provider: session.route?.provider || "unknown",
        route: routeMeta(session),
        timestamp: session.completed_at || session.created_at,
      })),
    members,
  };
}

function detectTaskType(prompt) {
  const normalized = prompt.toLowerCase();
  const webTerms = ["http://", "https://", "latest", "current", "news", "pricing", "scrape", "search", "research"];
  return webTerms.some((term) => normalized.includes(term)) ? "web" : "coding";
}

function inferTargets(prompt) {
  const targets = [];
  const seen = new Set();
  const filePattern = /(?:^|\s|[`'"])([\w.-]+(?:\/[\w.-]+)*\.(?:py|js|jsx|ts|tsx|css|html|json|md|yml|yaml))\b/gi;
  const symbolPattern = /\b(?:function|method|class|def)\s+[`'"]?([A-Za-z_$][\w$]*)/gi;

  for (const match of prompt.matchAll(filePattern)) {
    const scopeKey = match[1];
    if (!seen.has(`file:${scopeKey}`)) {
      targets.push({ scope_type: "file", scope_key: scopeKey });
      seen.add(`file:${scopeKey}`);
    }
  }

  for (const match of prompt.matchAll(symbolPattern)) {
    const scopeKey = match[1];
    if (!seen.has(`symbol:${scopeKey}`)) {
      targets.push({ scope_type: "symbol", scope_key: scopeKey });
      seen.add(`symbol:${scopeKey}`);
    }
  }

  if (targets.length === 0) {
    targets.push({ scope_type: "file", scope_key: "workspace" });
  }

  return targets.slice(0, 12);
}

export function createWorkspaceSocket({ workspaceId, username, onMessage, onStatus }) {
  let socket;
  let closedByClient = false;
  let reconnectTimer;
  let members = [username];
  const deliveredSessions = new Set();

  async function emitSync() {
    const state = await getWorkspaceState(workspaceId);
    onMessage({ type: "sync", ...snapshotFromState(state, members) });
    return state;
  }

  async function emitCompletedSession(session) {
    if (!session?.id || deliveredSessions.has(session.id)) return;
    deliveredSessions.add(session.id);
    const state = await getWorkspaceState(workspaceId);
    onMessage({
      type: "file_update",
      user: session.member_id,
      prompt: session.prompt,
      files: session.files || {},
      explanation: session.result_summary || "Session completed.",
      assistant_message: session.result_summary || "Session completed.",
      provider: session.route?.provider || "unknown",
      route: routeMeta(session),
      snapshot: snapshotFromState(state, members),
    });
  }

  async function handleBackendMessage(message) {
    if (message.type === "connected") {
      members = message.members || members;
      await emitSync();
      return;
    }

    if (message.type === "presence.updated") {
      members = message.members || [];
      onMessage({ type: "user_joined", members });
      return;
    }

    if (message.type === "session.completed") {
      await emitCompletedSession(message.payload);
    }
  }

  async function submitPrompt(payload) {
    const cleanPrompt = String(payload.prompt || "").trim();
    if (!cleanPrompt) return;

    try {
      const response = await fetch(`${apiBase()}/workspaces/${encodeURIComponent(workspaceId)}/prompts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          member_id: username,
          prompt: cleanPrompt,
          task_type: detectTaskType(cleanPrompt),
          targets: inferTargets(cleanPrompt),
          override_conflicts: Boolean(payload.override),
        }),
      });
      const result = await response.json().catch(() => ({}));

      if (response.status === 409) {
        onMessage({
          type: "conflict",
          prompt: cleanPrompt,
          message: result.message || "One or more targets are currently locked.",
          conflicts: result.conflicts || [],
        });
        return;
      }
      if (!response.ok) {
        throw new Error(result.detail || result.message || `Prompt failed (${response.status}).`);
      }

      onMessage({ type: "agent_thinking", prompt: cleanPrompt, provider: result.route?.provider });
      const runResponse = await fetch(
        `${apiBase()}/workspaces/${encodeURIComponent(workspaceId)}/sessions/${encodeURIComponent(result.session_id)}/run`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      );
      const completed = await readJson(runResponse);
      await emitCompletedSession(completed);
    } catch (error) {
      onMessage({ type: "error", message: error.message || "Unable to run the agent." });
    }
  }

  const connect = () => {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const wsBase = apiBase().replace(/^http/, protocol);
    const member = encodeURIComponent(username);
    socket = new WebSocket(
      `${wsBase}/workspaces/${encodeURIComponent(workspaceId)}/ws?member_id=${member}`
    );

    socket.onopen = () => onStatus("connected");
    socket.onmessage = (event) => {
      void handleBackendMessage(JSON.parse(event.data)).catch((error) => {
        onMessage({ type: "error", message: error.message || "Unable to synchronize the workspace." });
      });
    };
    socket.onclose = (event) => {
      if (closedByClient) return;
      onStatus(event.code === 1008 ? "offline" : "reconnecting");
      if (event.code !== 1008) reconnectTimer = window.setTimeout(connect, 900);
    };
    socket.onerror = () => onStatus("offline");
  };

  connect();

  return {
    send(payload) {
      if (payload.type === "prompt") void submitPrompt(payload);
    },
    close() {
      closedByClient = true;
      window.clearTimeout(reconnectTimer);
      socket?.close();
    },
  };
}
