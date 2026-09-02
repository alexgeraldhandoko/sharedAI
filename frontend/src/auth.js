const googleClientId = import.meta.env.VITE_GOOGLE_CLIENT_ID;
const storageKey = "ai-workspace-user";

export const hasGoogleOAuth = Boolean(googleClientId);

export function getStoredUser() {
  const raw = window.localStorage.getItem(storageKey);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function saveStoredUser(user) {
  window.localStorage.setItem(storageKey, JSON.stringify(user));
}

export function clearStoredUser() {
  window.localStorage.removeItem(storageKey);
  window.google?.accounts?.id?.disableAutoSelect?.();
}

export function sessionName(user) {
  return user?.name || user?.email?.split("@")[0] || "Member A";
}

export function renderGoogleButton(container, onUser, onError) {
  if (!googleClientId) {
    onError("Google OAuth client ID is not configured.");
    return;
  }

  if (!window.google?.accounts?.id) {
    onError("Google Identity Services is still loading. Try again in a moment.");
    return;
  }

  window.google.accounts.id.initialize({
    client_id: googleClientId,
    callback: (response) => {
      if (!response?.credential) {
        onError("Google did not return a credential.");
        return;
      }
      const profile = decodeJwt(response.credential);
      const user = {
        id: profile.sub,
        name: profile.name || profile.email?.split("@")[0] || "Member A",
        email: profile.email || "",
        picture: profile.picture || "",
        provider: "Google",
      };
      saveStoredUser(user);
      onUser(user);
    },
  });

  container.innerHTML = "";
  window.google.accounts.id.renderButton(container, {
    theme: "filled_black",
    size: "large",
    shape: "pill",
    type: "standard",
    text: "continue_with",
    width: Math.min(360, container.offsetWidth || 360),
  });
}

function decodeJwt(token) {
  const [, payload] = token.split(".");
  if (!payload) return {};
  const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
  const decoded = window.atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "="));
  return JSON.parse(decodeURIComponent(escape(decoded)));
}
