const TIME_FORMAT_KEY = "zk-time-format";

function selectedTimeFormat() {
  const saved = window.localStorage.getItem(TIME_FORMAT_KEY);
  return saved === "12" ? "12" : "24";
}

function datePartsFor(value, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(value);
  const lookup = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${lookup.year}-${lookup.month}-${lookup.day}`;
}

function timeFor(value, timeZone, format) {
  const options = {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: format === "12",
  };
  return new Intl.DateTimeFormat(format === "12" ? "en-US" : "en-GB", options).format(value);
}

function renderTimestampElement(element, format = selectedTimeFormat()) {
  const value = element.dataset.timestamp;
  if (!value) return;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return;
  const timeZone = element.dataset.timezone || document.body.dataset.displayTimezone || "UTC";
  const datePart = element.querySelector("[data-date-part]");
  const timePart = element.querySelector("[data-time-part]");
  if (datePart) datePart.textContent = datePartsFor(date, timeZone);
  if (timePart) timePart.textContent = timeFor(date, timeZone, format);
}

function renderTimestampElements(root = document) {
  const format = selectedTimeFormat();
  root.querySelectorAll("[data-timestamp]").forEach((element) => renderTimestampElement(element, format));
  document.querySelectorAll("[data-time-format-option]").forEach((button) => {
    const active = button.dataset.timeFormatOption === format;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function createTimestampElement(value, timeZone) {
  if (!value) return document.createTextNode("-");
  const element = document.createElement("time");
  element.dataset.timestamp = value;
  element.dataset.timezone = timeZone || document.body.dataset.displayTimezone || "UTC";
  const datePart = document.createElement("span");
  datePart.dataset.datePart = "";
  const timePart = document.createElement("span");
  timePart.dataset.timePart = "";
  element.append(datePart, timePart);
  renderTimestampElement(element);
  return element;
}

function initTimeFormatToggle() {
  document.querySelectorAll("[data-time-format-option]").forEach((button) => {
    button.addEventListener("click", () => {
      window.localStorage.setItem(TIME_FORMAT_KEY, button.dataset.timeFormatOption || "24");
      renderTimestampElements();
    });
  });
  renderTimestampElements();
}

document.addEventListener("DOMContentLoaded", initTimeFormatToggle);

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

function appendTimestampCell(row, value, timeZone) {
  const cell = document.createElement("td");
  cell.appendChild(createTimestampElement(value, timeZone));
  row.appendChild(cell);
}

function renderAttendanceRows(rows, timeZone) {
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
    appendTimestampCell(row, item.device_event_time, timeZone);
    appendTimestampCell(row, item.zone_trusted_time, timeZone);
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
    const params = new URLSearchParams(window.location.search);
    params.set("limit", "200");
    const res = await fetch(`/api/attendance/recent?${params.toString()}`);
    if (!res.ok) throw new Error("Attendance refresh failed");
    const data = await res.json();
    renderAttendanceRows(data.rows || [], data.display_timezone);
    if (status) {
      status.replaceChildren(
        document.createTextNode("Last refreshed "),
        createTimestampElement(data.server_time, data.display_timezone),
      );
    }
  } catch {
    if (status) status.textContent = "Realtime refresh unavailable";
  }
}

setInterval(refreshAttendance, 3000);
refreshAttendance();

function renderBulkJob(jobRoot, job) {
  const summary = jobRoot.querySelector("[data-bulk-job-summary]");
  if (summary) {
    summary.textContent = `Status ${job.status} · Pending ${job.pending_count} · Updating ${job.updating_count} · Verified ${job.verified_count} · Skipped ${job.skipped_count} · Failed ${job.failed_count}`;
  }
  let error = jobRoot.querySelector("[data-bulk-job-error]");
  if (job.last_error) {
    if (!error) {
      error = document.createElement("div");
      error.className = "alert warn compact";
      error.dataset.bulkJobError = "";
      summary?.after(error);
    }
    error.textContent = job.last_error;
  } else if (error) {
    error.remove();
  }
  const body = jobRoot.querySelector("[data-bulk-job-items]");
  if (!body) return;
  body.replaceChildren();
  for (const item of job.items || []) {
    const row = document.createElement("tr");
    appendCell(row, item.user_id);
    appendCell(row, item.status);
    appendCell(row, item.cnic || "");
    appendCell(row, item.expected_name || "");
    appendCell(row, item.message || "");
    body.appendChild(row);
  }
  if (!body.children.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "No job rows.";
    row.appendChild(cell);
    body.appendChild(row);
  }
}

async function refreshBulkJobs() {
  const roots = document.querySelectorAll("[data-bulk-job-id]");
  for (const root of roots) {
    const jobId = root.dataset.bulkJobId;
    if (!jobId) continue;
    try {
      const res = await fetch(`/api/users/bulk-jobs/${jobId}`, { headers: csrfHeaders() });
      if (!res.ok) throw new Error("Bulk job status unavailable");
      renderBulkJob(root, await res.json());
    } catch {
      const summary = root.querySelector("[data-bulk-job-summary]");
      if (summary) summary.textContent = "Bulk job status unavailable";
    }
  }
}

function initBulkJobActions() {
  document.querySelectorAll("[data-bulk-job-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const root = button.closest("[data-bulk-job-id]");
      const jobId = root?.dataset.bulkJobId;
      const action = button.dataset.bulkJobAction;
      if (!jobId || !action) return;
      button.disabled = true;
      try {
        await postJson(`/api/users/bulk-jobs/${jobId}/${action}`, {});
        await refreshBulkJobs();
      } catch (error) {
        const summary = root.querySelector("[data-bulk-job-summary]");
        if (summary) summary.textContent = error.message || "Bulk job action failed";
      } finally {
        button.disabled = false;
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initBulkJobActions();
  refreshBulkJobs();
  if (document.querySelector("[data-bulk-job-id]")) {
    setInterval(refreshBulkJobs, 2000);
  }
});

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
