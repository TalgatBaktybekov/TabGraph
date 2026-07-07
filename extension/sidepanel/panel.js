// TabGraph QA side panel.

const BACKEND_URL = "http://localhost:8000/ask";

const questionEl = document.getElementById("question");
const askBtn = document.getElementById("ask");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");

async function ask() {
  const question = questionEl.value.trim();
  if (!question) return;
  askBtn.disabled = true;
  resultEl.innerHTML = "";
  statusEl.textContent = "Thinking…";
  try {
    const res = await fetch(BACKEND_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!res.ok) throw new Error(`backend returned ${res.status}`);
    render(await res.json());
    statusEl.textContent = "";
  } catch (err) {
    statusEl.textContent = `Failed: ${err.message} — is the backend running?`;
  } finally {
    askBtn.disabled = false;
  }
}

function render(data) {
  // Render text only
  const answer = document.createElement("div");
  answer.id = "answer";
  answer.textContent = data.answer;
  resultEl.appendChild(answer);

  const badge = document.createElement("span");
  badge.className = `badge ${data.path}`;
  badge.textContent =
    data.path === "cypher" ? "path: text-to-Cypher" : "path: vector + subgraph";
  resultEl.appendChild(badge);

  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "how this was answered";
  details.appendChild(summary);
  const pre = document.createElement("pre");
  if (data.path === "cypher") {
    pre.textContent = data.cypher + "\n\n" + JSON.stringify(data.rows, null, 2);
  } else {
    pre.textContent =
      `retrieved entities:\n` +
      JSON.stringify(data.retrieved_entities, null, 2) +
      `\n\nfacts used: ${data.facts_used}`;
  }
  details.appendChild(pre);
  resultEl.appendChild(details);
}

askBtn.addEventListener("click", ask);
questionEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    ask();
  }
});
