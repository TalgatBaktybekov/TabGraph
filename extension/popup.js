// Popup: capture toggle + the promote-to-evidence gesture.

const BACKEND = "http://localhost:8000";

const toggle = document.getElementById("capture-toggle");
const projectInput = document.getElementById("project");
const projectList = document.getElementById("project-list");
const promoteBtn = document.getElementById("promote");
const statusEl = document.getElementById("promote-status");

chrome.storage.local.get({ captureEnabled: false }).then(({ captureEnabled }) => {
  toggle.checked = captureEnabled;
});

toggle.addEventListener("change", () => {
  chrome.storage.local.set({ captureEnabled: toggle.checked });
});

// Existing projects feed the datalist; free text creates a new one.
fetch(`${BACKEND}/projects`)
  .then((res) => res.json())
  .then((data) => {
    for (const p of data.projects ?? []) {
      const opt = document.createElement("option");
      opt.value = p.name;
      projectList.appendChild(opt);
    }
  })
  .catch(() => {}); // backend down; picker still works as free text

promoteBtn.addEventListener("click", async () => {
  promoteBtn.disabled = true;
  statusEl.textContent = "Saving…";
  statusEl.className = "";
  try {
    const response = await chrome.runtime.sendMessage({
      type: "promote-current-tab",
      project: projectInput.value.trim() || null,
    });
    if (response?.ok) {
      statusEl.textContent = response.detail;
      statusEl.className = "ok";
    } else {
      statusEl.textContent = response?.error ?? "failed";
      statusEl.className = "err";
    }
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.className = "err";
  } finally {
    promoteBtn.disabled = false;
  }
});
