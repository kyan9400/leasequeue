const state = { activeLease: null, selectedJob: null };
const elements = {
  jobsBody: document.querySelector("#jobsBody"),
  emptyState: document.querySelector("#emptyState"),
  queueFilter: document.querySelector("#queueFilter"),
  statusFilter: document.querySelector("#statusFilter"),
  toast: document.querySelector("#toast"),
  dialog: document.querySelector("#jobDialog"),
  leaseCard: document.querySelector("#leaseCard"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || "Request failed");
  return data;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function relativeTime(value) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 5) return "now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  elements.toast.classList.add("visible");
  window.setTimeout(() => elements.toast.classList.remove("visible"), 2800);
}

async function refreshStats() {
  const stats = await api("/api/stats");
  document.querySelector("#statQueued").textContent = stats.queued;
  document.querySelector("#statRunning").textContent = stats.running;
  document.querySelector("#statRetry").textContent = stats.retry;
  document.querySelector("#statDead").textContent = stats.dead;
  document.querySelector("#statAge").textContent = duration(stats.oldestPendingSeconds);
}

async function refreshJobs() {
  const params = new URLSearchParams({ limit: "100" });
  const queue = elements.queueFilter.value.trim();
  const status = elements.statusFilter.value;
  if (queue) params.set("queue", queue);
  if (status) params.set("status", status);
  const { jobs } = await api(`/api/jobs?${params}`);
  elements.jobsBody.innerHTML = jobs
    .map(
      (job) => `
        <tr>
          <td><span class="status status-${escapeHtml(job.status)}">${escapeHtml(job.status)}</span></td>
          <td>
            <span class="task-name">${escapeHtml(job.task)}</span>
            <code class="job-id">${escapeHtml(job.id.slice(0, 13))}</code>
          </td>
          <td>${job.attempt} / ${job.maxAttempts}</td>
          <td>${job.priority > 0 ? "+" : ""}${job.priority}</td>
          <td>${relativeTime(job.updatedAt)}</td>
          <td><button class="row-button" data-job-id="${escapeHtml(job.id)}" type="button">Inspect →</button></td>
        </tr>`,
    )
    .join("");
  elements.emptyState.hidden = jobs.length !== 0;
}

async function refreshAll() {
  try {
    await Promise.all([refreshStats(), refreshJobs()]);
  } catch (error) {
    showToast(error.message, true);
  }
}

document.querySelector("#submitForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  let payload;
  try {
    payload = JSON.parse(form.get("payload"));
  } catch {
    showToast("Payload must be valid JSON", true);
    return;
  }
  try {
    const result = await api("/api/jobs", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        queue: form.get("queue"),
        task: form.get("task"),
        payload,
        priority: Number(form.get("priority")),
        maxAttempts: Number(form.get("maxAttempts")),
        retryBaseSeconds: Number(form.get("retryBaseSeconds")),
      }),
    });
    elements.queueFilter.value = result.job.queue;
    showToast(`Enqueued ${result.job.task}`);
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
  }
});

document.querySelector("#claimForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    const lease = await api("/api/jobs/claim", {
      method: "POST",
      body: JSON.stringify({
        queue: form.get("queue"),
        workerId: form.get("workerId"),
        leaseSeconds: 120,
      }),
    });
    if (!lease) {
      showToast("No eligible job is ready on this queue", true);
      return;
    }
    state.activeLease = lease;
    document.querySelector("#leaseTask").textContent = lease.job.task;
    document.querySelector("#leaseId").textContent = lease.job.id;
    elements.leaseCard.hidden = false;
    showToast(`Lease acquired by ${form.get("workerId")}`);
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
  }
});

async function resolveLease(action) {
  if (!state.activeLease) return;
  const request = { leaseToken: state.activeLease.leaseToken };
  if (action === "fail") request.error = document.querySelector("#failureReason").value;
  try {
    const result = await api(`/api/jobs/${state.activeLease.job.id}/${action}`, {
      method: "POST",
      body: JSON.stringify(request),
    });
    showToast(action === "complete" ? "Job completed" : `Job moved to ${result.job.status}`);
    state.activeLease = null;
    elements.leaseCard.hidden = true;
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
  }
}

document.querySelector("#completeButton").addEventListener("click", () => resolveLease("complete"));
document.querySelector("#failButton").addEventListener("click", () => resolveLease("fail"));

elements.jobsBody.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-job-id]");
  if (!button) return;
  try {
    const [jobResult, eventResult] = await Promise.all([
      api(`/api/jobs/${button.dataset.jobId}`),
      api(`/api/jobs/${button.dataset.jobId}/events`),
    ]);
    renderDetail(jobResult.job, eventResult.events);
    elements.dialog.showModal();
  } catch (error) {
    showToast(error.message, true);
  }
});

function renderDetail(job, events) {
  state.selectedJob = job;
  document.querySelector("#dialogTitle").textContent = job.task;
  document.querySelector("#jobDetail").innerHTML = `
    <div class="detail-grid">
      <div class="detail-cell"><span>Status</span><strong>${escapeHtml(job.status)}</strong></div>
      <div class="detail-cell"><span>Queue</span><strong>${escapeHtml(job.queue)}</strong></div>
      <div class="detail-cell"><span>Priority</span><strong>${job.priority}</strong></div>
      <div class="detail-cell"><span>Attempt</span><strong>${job.attempt} / ${job.maxAttempts}</strong></div>
      <div class="detail-cell"><span>Worker</span><strong>${escapeHtml(job.leaseOwner || "—")}</strong></div>
      <div class="detail-cell"><span>Created</span><strong>${new Date(job.createdAt).toLocaleString()}</strong></div>
    </div>
    <pre class="payload">${escapeHtml(JSON.stringify(job.payload, null, 2))}</pre>
  `;
  document.querySelector("#eventTimeline").innerHTML = events
    .map(
      (item) => `
      <li>
        <strong>${escapeHtml(item.eventType.replaceAll("_", " "))}</strong>
        <time>${new Date(item.createdAt).toLocaleString()}</time>
        <small>${escapeHtml(JSON.stringify(item.detail))}</small>
      </li>`,
    )
    .join("");
  const actions = document.querySelector("#dialogActions");
  actions.innerHTML = "";
  if (["queued", "retry"].includes(job.status)) {
    actions.innerHTML = '<button class="button button-danger" data-mutation="cancel" type="button">Cancel job</button>';
  }
  if (job.status === "dead") {
    actions.innerHTML = '<button class="button button-primary" data-mutation="redrive" type="button">Redrive job <span>→</span></button>';
  }
}

document.querySelector("#dialogActions").addEventListener("click", async (event) => {
  const action = event.target.closest("[data-mutation]")?.dataset.mutation;
  if (!action || !state.selectedJob) return;
  try {
    await api(`/api/jobs/${state.selectedJob.id}/${action}`, { method: "POST" });
    elements.dialog.close();
    showToast(action === "redrive" ? "Job returned to the queue" : "Job cancelled");
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
  }
});

document.querySelector("#closeDialog").addEventListener("click", () => elements.dialog.close());
document.querySelector("#refreshButton").addEventListener("click", refreshAll);
elements.statusFilter.addEventListener("change", refreshJobs);
elements.queueFilter.addEventListener("change", refreshJobs);

refreshAll();
window.setInterval(refreshAll, 15000);
