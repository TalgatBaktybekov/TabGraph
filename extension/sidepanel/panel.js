// TabGraph QA side panel.

const BACKEND = "http://localhost:8000";

const questionEl = document.getElementById("question");
const askBtn = document.getElementById("ask");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const discoveriesEl = document.getElementById("discoveries");

async function ask() {
  const question = questionEl.value.trim();
  if (!question) return;
  askBtn.disabled = true;
  resultEl.innerHTML = "";
  statusEl.textContent = "Thinking…";
  try {
    const res = await fetch(`${BACKEND}/ask`, {
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

const PATH_LABELS = {
  cypher: "path: text-to-Cypher",
  synthesis: "path: claim synthesis",
  subgraph: "path: vector + subgraph",
  recall: "path: chunk recall",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function pageLink(item) {
  const a = el("a", "page-link", item.page_title || item.page_url);
  a.href = item.page_url;
  a.target = "_blank";
  return a;
}

function render(data) {
  const answer = el("div", "", data.answer);
  answer.id = "answer";
  resultEl.appendChild(answer);

  resultEl.appendChild(
    el("span", `badge ${data.path}`, PATH_LABELS[data.path] || `path: ${data.path}`)
  );

  if (data.path === "synthesis") {
    if (data.citations?.length) {
      const list = el("ol", "citations");
      for (const c of data.citations) {
        const li = el("li");
        li.value = c.n;
        li.append(pageLink(c), el("span", "muted", ` — ${c.captured_at || ""}`));
        list.appendChild(li);
      }
      resultEl.append(el("h4", "", "Sources"), list);
    }
    if (data.related?.length) {
      const list = el("ul", "related");
      for (const r of data.related) {
        const li = el("li", "", `${r.statement} `);
        li.appendChild(pageLink(r));
        list.appendChild(li);
      }
      resultEl.append(el("h4", "", "Related things you read"), list);
    }
  }

  if (data.path === "recall" && data.chunks?.length) {
    const list = el("ul", "related");
    for (const c of data.chunks) {
      const li = el("li");
      li.append(
        pageLink({ page_title: c.title, page_url: c.url }),
        el("span", "muted", ` — read ${c.captured_at || ""}`)
      );
      list.appendChild(li);
    }
    resultEl.append(el("h4", "", "From pages you read"), list);
  }

  const details = el("details");
  details.appendChild(el("summary", "", "how this was answered"));
  const pre = el("pre");
  if (data.path === "cypher") {
    pre.textContent = data.cypher + "\n\n" + JSON.stringify(data.rows, null, 2);
  } else if (data.path === "recall") {
    pre.textContent = `chunks used: ${data.chunks?.length ?? 0}`;
  } else if (data.path === "synthesis") {
    pre.textContent =
      `claims retrieved: ${(data.citations?.length || 0) + (data.related?.length || 0)}` +
      `\ncited: ${data.citations?.length || 0}` +
      `\ncommunity summaries: ${data.community_summaries?.length || 0}`;
  } else {
    pre.textContent =
      `retrieved entities:\n` +
      JSON.stringify(data.retrieved_entities, null, 2) +
      `\n\nfacts used: ${data.facts_used}`;
  }
  details.appendChild(pre);
  resultEl.appendChild(details);
}

function reviewControls(tension) {
  const row = el("div", "review-row");
  const label = el("span", "muted", "real conflict? ");
  const review = async (decision) => {
    row.textContent = "…";
    try {
      await fetch(`${BACKEND}/tensions/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          iri_a: tension.iri_a,
          iri_b: tension.iri_b,
          decision,
        }),
      });
      row.textContent = decision === "confirmed" ? "✓ confirmed" : "dismissed";
    } catch {
      row.textContent = "failed — backend down?";
    }
  };
  const yes = el("button", "review-btn", "confirm");
  yes.addEventListener("click", () => review("confirmed"));
  const no = el("button", "review-btn", "dismiss");
  no.addEventListener("click", () => review("dismissed"));
  row.append(label, yes, no);
  return row;
}

function claimPair(a, b) {
  const div = el("div", "pair");
  for (const side of [a, b]) {
    const p = el("p", "", `“${side.statement}” `);
    p.appendChild(pageLink({ page_title: side.title, page_url: side.url }));
    div.appendChild(p);
  }
  return div;
}

async function loadDiscoveries() {
  try {
    const res = await fetch(`${BACKEND}/discoveries`);
    if (!res.ok) return;
    const data = await res.json();
    discoveriesEl.innerHTML = "";

    if (data.bridges?.length) {
      discoveriesEl.appendChild(el("h4", "", "Connections you may have missed"));
      for (const br of data.bridges) {
        discoveriesEl.appendChild(
          claimPair(
            { statement: br.statement_a, title: br.title_a, url: br.url_a },
            { statement: br.statement_b, title: br.title_b, url: br.url_b }
          )
        );
      }
    }
    if (data.tensions?.length) {
      discoveriesEl.appendChild(el("h4", "", "Your sources disagree"));
      for (const t of data.tensions) {
        const pair = claimPair(
          { statement: t.statement_a, title: t.title_a, url: t.url_a },
          { statement: t.statement_b, title: t.title_b, url: t.url_b }
        );
        pair.classList.add("tension");
        pair.prepend(el("p", "muted", `“${t.evidence_a}” vs “${t.evidence_b}”`));
        if (t.review_status === "proposed" && t.iri_a && t.iri_b) {
          pair.appendChild(reviewControls(t));
        } else if (t.review_status === "confirmed") {
          pair.appendChild(el("p", "muted confirmed", "✓ confirmed"));
        }
        discoveriesEl.appendChild(pair);
      }
    }
    if (data.communities?.length) {
      discoveriesEl.appendChild(el("h4", "", "What you've been reading about"));
      for (const c of data.communities) {
        const div = el("div", "pair");
        div.appendChild(el("p", "", c.summary));
        div.appendChild(el("p", "muted", (c.members || []).join(", ")));
        discoveriesEl.appendChild(div);
      }
    }
  } catch {
    // Backend down; leave the section empty.
  }
}

askBtn.addEventListener("click", ask);
questionEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    ask();
  }
});
loadDiscoveries();
