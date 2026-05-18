async function refreshStatus() {
  const badge = document.querySelector("[data-status-json]");
  if (!badge) return;
  try {
    const res = await fetch("/api/status");
    badge.textContent = JSON.stringify(await res.json(), null, 2);
  } catch {
    badge.textContent = "Status unavailable";
  }
}

setInterval(refreshStatus, 5000);

function appendCell(row, value) {
  const cell = document.createElement("td");
  cell.textContent = value ?? "";
  row.appendChild(cell);
}

function renderAttendanceRows(rows) {
  const body = document.querySelector("[data-attendance-body]");
  if (!body) return;
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    row.dataset.emptyRow = "true";
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.textContent = "No attendance captured yet.";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const item of rows) {
    const row = document.createElement("tr");
    appendCell(row, item.device_event_time);
    appendCell(row, item.zone_trusted_time);
    appendCell(row, item.user);
    appendCell(row, item.device_id);
    appendCell(row, item.source_type);
    appendCell(row, item.trust_status);
    appendCell(row, item.fraud_score);
    appendCell(row, item.fraud_reason);
    body.appendChild(row);
  }
}

async function refreshAttendance() {
  const body = document.querySelector("[data-attendance-body]");
  if (!body) return;
  const status = document.querySelector("[data-attendance-refresh]");
  try {
    const res = await fetch("/api/attendance/recent?limit=200");
    if (!res.ok) throw new Error("Attendance refresh failed");
    const data = await res.json();
    renderAttendanceRows(data.rows || []);
    if (status) status.textContent = `Last refreshed ${data.server_time}`;
  } catch {
    if (status) status.textContent = "Realtime refresh unavailable";
  }
}

setInterval(refreshAttendance, 3000);
refreshAttendance();

function base64urlToBuffer(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function csrfHeaders() {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  return csrf ? { "X-CSRF-Token": csrf } : {};
}

async function postJson(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...csrfHeaders() },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || "Action failed");
  }
  return data;
}

function decodeCreationOptions(publicKey) {
  publicKey.challenge = base64urlToBuffer(publicKey.challenge);
  publicKey.user.id = base64urlToBuffer(publicKey.user.id);
  publicKey.excludeCredentials = (publicKey.excludeCredentials || []).map((item) => ({
    ...item,
    id: base64urlToBuffer(item.id),
  }));
  return publicKey;
}

function decodeRequestOptions(publicKey) {
  publicKey.challenge = base64urlToBuffer(publicKey.challenge);
  publicKey.allowCredentials = (publicKey.allowCredentials || []).map((item) => ({
    ...item,
    id: base64urlToBuffer(item.id),
  }));
  return publicKey;
}

function registrationCredentialToJson(credential) {
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    response: {
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      attestationObject: bufferToBase64url(credential.response.attestationObject),
      transports: credential.response.getTransports ? credential.response.getTransports() : [],
    },
  };
}

function authenticationCredentialToJson(credential) {
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    response: {
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      authenticatorData: bufferToBase64url(credential.response.authenticatorData),
      signature: bufferToBase64url(credential.response.signature),
      userHandle: credential.response.userHandle ? bufferToBase64url(credential.response.userHandle) : null,
    },
  };
}

async function enrollWindowsHello(form, button) {
  if (!window.PublicKeyCredential) {
    throw new Error("Windows Hello unlock is not available in this browser.");
  }
  const label = form.querySelector("[data-webauthn-label]")?.value || "Windows Hello";
  const recoveryPassword = form.querySelector("[data-webauthn-recovery-password]")?.value || "";
  const recoveryPasswordConfirm = form.querySelector("[data-webauthn-recovery-password-confirm]")?.value || "";
  const options = await postJson("/api/admin/webauthn/register/options", { label });
  const credential = await navigator.credentials.create({
    publicKey: decodeCreationOptions(options.publicKey),
  });
  const result = await postJson("/api/admin/webauthn/register/verify", {
    challenge_id: options.challenge_id,
    credential: registrationCredentialToJson(credential),
    label,
    recovery_password: recoveryPassword,
    recovery_password_confirm: recoveryPasswordConfirm,
    next: button.dataset.next || "/setup",
  });
  window.location.href = result.redirect || "/setup";
}

async function unlockWithWindowsHello(form) {
  if (!window.PublicKeyCredential) {
    throw new Error("Windows Hello unlock is not available in this browser.");
  }
  const next = form.querySelector('input[name="next"]')?.value || "/dashboard";
  const options = await postJson("/api/admin/webauthn/login/options", {});
  const credential = await navigator.credentials.get({
    publicKey: decodeRequestOptions(options.publicKey),
  });
  const result = await postJson("/api/admin/webauthn/login/verify", {
    challenge_id: options.challenge_id,
    credential: authenticationCredentialToJson(credential),
    next,
  });
  window.location.href = result.redirect || "/dashboard";
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-api-post]");
  if (!button) return;
  event.preventDefault();
  button.disabled = true;
  try {
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
    const headers = csrf ? { "X-CSRF-Token": csrf } : {};
    const res = await fetch(button.dataset.apiPost, { method: "POST", headers });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      alert(body.detail || "Action failed");
    }
    window.location.reload();
  } finally {
    button.disabled = false;
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-webauthn-register]");
  if (!button) return;
  event.preventDefault();
  const form = button.closest("[data-webauthn-register-form]");
  const status = form?.querySelector("[data-webauthn-status]");
  if (!form) return;
  button.disabled = true;
  if (status) status.textContent = "Waiting for Windows Hello...";
  try {
    await enrollWindowsHello(form, button);
  } catch (error) {
    if (status) status.textContent = error.message || "Windows Hello enrollment failed";
    button.disabled = false;
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-webauthn-login]");
  if (!button) return;
  event.preventDefault();
  const form = button.closest("[data-webauthn-login-form]");
  const status = form?.querySelector("[data-webauthn-status]");
  if (!form) return;
  button.disabled = true;
  if (status) status.textContent = "Waiting for Windows Hello...";
  try {
    await unlockWithWindowsHello(form);
  } catch (error) {
    if (status) status.textContent = error.message || "Windows Hello unlock failed";
    button.disabled = false;
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-health-check]");
  if (!button) return;
  event.preventDefault();
  const input = document.querySelector("[data-head-office-url]");
  const result = document.querySelector("[data-health-result]");
  if (!input || !result) return;
  button.disabled = true;
  result.textContent = "Checking...";
  try {
    const url = `/api/head-office/health?base_url=${encodeURIComponent(input.value)}`;
    const res = await fetch(url);
    const data = await res.json();
    if (res.ok && data.ok) {
      result.textContent = `Live: ${data.health.server_utc}`;
    } else {
      result.textContent = data.error || data.detail || "Unavailable";
    }
  } catch {
    result.textContent = "Unavailable";
  } finally {
    button.disabled = false;
  }
});
