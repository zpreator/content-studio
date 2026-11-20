"use strict";
// Content Studio UI. Plain DOM, no framework. All text goes in via textContent (never innerHTML).

const DEPT_COLORS = {
  "Science & Nature": "#3fa34d", "Geography": "#3b8be0", "History": "#d4a62a",
  "Arts & Literature": "#9a7bd1", "Entertainment": "#e0609e", "Sports & Leisure": "#e8872b",
};
const INPUT_TYPES = ["multiple_choice", "true_false", "order", "matching", "slider", "hotspot"];
const $view = document.getElementById("view");
const ui = { ideaDraft: { name: "", dept: "", note: "" }, state: null, hooks: { refresh: null }, collapsed: new Set(), filters: { kind: "", status: "", q: "" }, runStatus: {}, lastActive: 0 };

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "style") el.setAttribute("style", v);
    else if (k === "value") el.value = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(method, path, body) {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.error || res.statusText); e.data = data; throw e; }
  return data;
}

function toast(msg, isErr) {
  const t = h("div", { class: "toast" + (isErr ? " err" : "") }, msg);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), isErr ? 7000 : 4000);
}
async function guarded(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

function modal(title, bodyEls, buttons) {
  const root = document.getElementById("modal-root");
  const close = () => { root.textContent = ""; };
  const box = h("div", { class: "modal" }, h("h2", {}, title), bodyEls,
    h("div", { class: "row", style: "margin-top:14px;justify-content:flex-end" }, buttons.map(b => h("button", { class: b.cls || "", onclick: async () => { if (b.keep !== true) close(); await b.fn?.(); } }, b.label))));
  const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } }, box);
  root.textContent = ""; root.append(back);
  return close;
}

const deptColor = (d) => DEPT_COLORS[d] || "#888";
const tierNames = () => ui.state.tiers.map(t => t.name);
const TIER_HUES = { beginner: "#6fbf73", easy: "#a3c964", intermediate: "#e0c24a", advanced: "#e08a3c", expert: "#d0584f" };
const topicOf = (id) => [...ui.state.topics, ...(ui.state.ideas || []), ...(ui.state.archived || [])].find(t => t.id === id);
const fmtTime = (s) => (s || "").replace("T", " ").slice(0, 16);
const badge = (n) => h("span", { class: "badge " + n.kind }, n.variant_type ? n.variant_type.replace("_", "/") : n.kind);
const mockFlag = (n) => (n.mock || n.provenance?.mock) ? h("span", { class: "mockflag" }, "MOCK") : null;

// ---------------------------------------------------------------- routing
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, qs] = raw.split("?");
  return { parts: path.split("/").filter(Boolean), query: Object.fromEntries(new URLSearchParams(qs || "")) };
}
const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };

async function route() {
  ui.hooks.refresh = null;
  await refreshState();
  const { parts, query } = parseHash();
  renderNav(parts[0] || "board");
  const [a, b, c] = parts;
  if (a !== "topic" && a !== "node") ui.mountedTopic = null;
  if (!a || a === "board") return viewBoard();
  if (a === "topic") return viewTopic(b, c, query);
  if (a === "node") { const d = await guarded(() => api("GET", `/api/nodes/${b}`)); return d && go(`#/topic/${d.node.topic}/${b}`); }
  if (a === "ideas") return viewIdeas();
  if (a === "runs") return b ? viewRun(b) : viewRuns();
  if (a === "settings") return viewSettings();
  if (a === "export") return viewExport();
  viewBoard();
}

async function refreshState() { ui.state = await api("GET", "/api/state"); return ui.state; }

function renderNav(active) {
  const tab = (id, label) => h("a", { class: "tab" + (active === id ? " active" : ""), onclick: () => go("#/" + id) }, label);
  const n = ui.state.active_runs;
  document.getElementById("nav").replaceChildren(
    h("span", { class: "brand" }, "Content Studio"), tab("board", "Board"), tab("ideas", `Ideas${ui.state.ideas.length ? ` (${ui.state.ideas.length})` : ""}`), tab("runs", "Runs"), tab("export", "Export"), tab("settings", "Settings"),
    h("span", { class: "spacer" }),
    n ? h("a", { onclick: () => go("#/runs") }, h("span", { class: "spinner" }), `${n} run${n > 1 ? "s" : ""} active`) : h("span", { class: "muted small" }, `backend: ${ui.state.config.backend}`),
  );
}

async function poll() {
  try {
    const before = ui.lastActive;
    const st = await api("GET", "/api/state");
    ui.state = st;
    renderNav(parseHash().parts[0] || "board");
    if (st.active_runs > 0 || before > 0) {
      const { runs } = await api("GET", "/api/runs");
      let changed = false;
      for (const r of runs) {
        const prev = ui.runStatus[r.id];
        if (prev && prev !== r.status && ["done", "failed", "cancelled"].includes(r.status)) {
          changed = true;
          const label = ui.state.stages[r.stage] || r.stage;
          toast(r.status === "done" ? `${label}: done (${r.created_count} new${r.warning_count ? `, ${r.warning_count} warnings` : ""})` : `${label}: ${r.status}${r.error ? " - " + r.error : ""}`, r.status === "failed");
        }
        ui.runStatus[r.id] = r.status;
      }
      if (changed) ui.hooks.refresh?.();
    }
    ui.lastActive = st.active_runs;
  } catch (_) { /* server restarting */ }
}
setInterval(poll, 2500);

function spreadPanel(t) {
  const cols = tierNames().map(n => {
    const sp = t.spread[n], pct = Math.min(100, sp.target ? sp.cards / sp.target * 100 : 0);
    return h("div", { style: "flex:1;min-width:84px" },
      h("div", { class: "small", style: `color:${TIER_HUES[n] || "var(--text)"};font-weight:600` }, n),
      h("div", { class: "bar", style: "min-width:0;height:8px;margin:4px 0" }, h("i", { style: `width:${pct}%;background:${TIER_HUES[n] || "var(--green)"}` })),
      h("div", { class: "small muted" }, `${sp.cards} / ${sp.target} cards`), h("div", { class: "small muted" }, `${sp.facts} facts`));
  });
  return h("div", {}, h("h3", {}, `Difficulty spread (target ${t.target} cards)`), h("div", { class: "row", style: "align-items:flex-start;gap:14px" }, cols),
    h("div", { class: "muted small", style: "margin-top:6px" }, "Cards = questions + variants that aren't rejected. The same targets are sent to the AI so repeated runs fill the gaps."));
}

function miniSpread(t) {
  return h("div", { style: "display:flex;gap:3px;align-items:flex-end;height:22px", title: tierNames().map(n => `${n}: ${t.spread[n].cards}/${t.spread[n].target}`).join("  ") },
    tierNames().map(n => { const sp = t.spread[n], pct = sp.target ? Math.min(1, sp.cards / sp.target) : 0;
      return h("div", { style: `width:9px;height:22px;background:var(--panel2);border-radius:2px;display:flex;align-items:flex-end` }, h("div", { style: `width:100%;height:${Math.max(pct * 100, sp.cards ? 12 : 0)}%;background:${TIER_HUES[n]};border-radius:2px` })); }));
}

// ---------------------------------------------------------------- board
function stackBar(c) {
  const total = c.draft + c.approved + c.rejected || 1;
  return h("div", { class: "stack" }, h("i", { class: "a", style: `width:${c.approved / total * 100}%` }), h("i", { class: "d", style: `width:${c.draft / total * 100}%` }), h("i", { class: "r", style: `width:${c.rejected / total * 100}%` }));
}

function viewBoard() {
  const st = ui.state;
  const totalExport = st.topics.reduce((a, t) => a + t.exportable, 0), totalTarget = st.topics.reduce((a, t) => a + t.target, 0);
  const depts = [...new Set(st.topics.map(t => t.department))];
  const rows = [];
  for (const d of depts) {
    rows.push(h("tr", { class: "dept" }, h("td", { colspan: 8 }, h("span", { class: "dot", style: `background:${deptColor(d)}` }), d)));
    for (const t of st.topics.filter(x => x.department === d)) {
      const kindCell = (kind) => {
        const c = t.counts[kind], total = c.draft + c.approved + c.rejected;
        return h("td", { class: "cell-link", onclick: () => go(`#/topic/${t.id}?kind=${kind}`) }, total ? [`${total}`, h("span", { class: "muted small" }, `  ${c.approved} approved`), stackBar(c)] : h("span", { class: "muted" }, "-"));
      };
      const next = [];
      if (t.pending.extract_facts) next.push(h("span", { class: "chip action", onclick: () => go(`#/topic/${t.id}?do=extract_facts`) }, `${t.pending.extract_facts} sources need facts`));
      if (t.pending.draft_questions) next.push(h("span", { class: "chip action", onclick: () => go(`#/topic/${t.id}?do=draft_questions`) }, `${t.pending.draft_questions} facts need questions`));
      if (t.pending.make_variants) next.push(h("span", { class: "chip action", onclick: () => go(`#/topic/${t.id}?do=make_variants`) }, `${t.pending.make_variants} approved q's have no variants`));
      const ck = t.check;
      if (ck.issues) next.push(h("span", { class: "chip action", onclick: () => go(`#/topic/${t.id}`) }, `${ck.issues} with accuracy issues`));
      if (ck.stale) next.push(h("span", { class: "chip action", onclick: () => go(`#/topic/${t.id}`) }, `${ck.stale} changed since checked`));
      if (ck.unchecked) next.push(h("span", { class: "chip action", onclick: () => go(`#/topic/${t.id}`) }, `${ck.unchecked} to check`));
      rows.push(h("tr", { "data-q": `${t.name} ${t.department}`.toLowerCase() },
        h("td", {}, h("a", { onclick: () => go(`#/topic/${t.id}`) }, t.name), t.approved ? h("span", { class: "chip ok", title: "approved for the game" }, "approved") : null),
        kindCell("source"), kindCell("fact"), kindCell("question"), kindCell("variant"),
        h("td", {}, h("div", { class: "bar" }, h("i", { style: `width:${Math.min(100, t.exportable / t.target * 100)}%` })), h("span", { class: "small muted" }, `${t.exportable} / ${t.target} export-ready`)),
        h("td", {}, miniSpread(t)),
        h("td", {}, next.length ? next : h("span", { class: "muted small" }, t.exportable >= t.target ? "target reached" : "")),
      ));
    }
  }
  const tbody = h("tbody", {}, rows);
  const hits = h("div", {});
  let timer = null;
  const search = h("input", { type: "text", placeholder: "Search topics and cards...", class: "boardsearch", oninput: () => {
    const q = search.value.trim().toLowerCase();
    let dept = null, shown = 0;
    const flush = () => { if (dept) dept.style.display = shown || !q ? "" : "none"; };
    for (const tr of tbody.children) {
      if (tr.classList.contains("dept")) { flush(); dept = tr; shown = 0; continue; }
      const on = !q || tr.getAttribute("data-q").includes(q);
      tr.style.display = on ? "" : "none"; if (on) shown++;
    }
    flush();
    clearTimeout(timer);
    if (q.length < 2) return hits.replaceChildren();
    timer = setTimeout(async () => {
      const r = await guarded(() => api("GET", `/api/search?q=${encodeURIComponent(q)}`));
      if (!r || search.value.trim().toLowerCase() !== q) return;
      hits.replaceChildren(r.nodes.length ? h("div", { class: "panel", style: "margin-bottom:14px" }, h("h3", {}, `Cards and facts matching "${q}" (${r.total_nodes})`),
        r.nodes.map(n => h("div", { class: "item", onclick: () => go(`#/topic/${n.topic}/${n.id}`) }, h("span", { class: "st " + n.status }), h("span", { class: "muted small", style: "min-width:130px" }, n.topic_name),
          h("span", { class: "badge " + n.kind }, n.kind), h("span", { class: "txt" }, n.snippet || "(empty)")))) : null);
    }, 250);
  } });
  $view.replaceChildren(
    h("div", { class: "row", style: "justify-content:space-between;margin-bottom:14px" },
      h("div", {}, h("h1", {}, "Question bank progress"), h("div", { class: "muted" }, `${totalExport} of ${totalTarget} target cards approved and export-ready across ${st.topics.length} topics`,
        st.ideas.length ? [" - ", h("a", { onclick: () => go("#/ideas") }, `${st.ideas.length} idea${st.ideas.length > 1 ? "s" : ""} in the inbox`)] : null)),
      h("div", { class: "muted small" }, "bars: ", h("span", { class: "tag-ok" }, "approved"), " / ", h("span", { style: "color:var(--amber)" }, "draft"), " / rejected")),
    h("div", { style: "margin-bottom:14px" }, search), hits,
    h("div", { class: "panel" }, h("table", { class: "board boardview" },
      h("thead", {}, h("tr", {}, ["Topic", "Sources", "Facts", "Questions", "Variants", "Export-ready", "Difficulty", "Next up"].map(x => h("th", {}, x)))), tbody)),
  );
  ui.hooks.refresh = viewBoard;
}

// ---------------------------------------------------------------- topic view
async function viewTopic(topicId, nodeId, query) {
  const topic = topicOf(topicId);
  if (!topic) return go("#/board");
  if (query.kind) ui.filters.kind = query.kind;
  // Moving between nodes rebuilds this page; keep the page and the tree where they were so you can see where you are.
  const keep = ui.mountedTopic === topicId ? { y: window.scrollY, list: ui.listScroll || 0 } : null;
  ui.mountedTopic = topicId;
  const listBox = h("div", { class: "list", onscroll: () => { ui.listScroll = listBox.scrollTop; } });
  const detailBox = h("div", {});
  detailBox.addEventListener("input", () => { ui.dirty = true; });
  const spreadBox = h("div", {});
  const facetBox = h("div", {});
  let nodes = [];
  const note = h("input", { type: "text", placeholder: "Extra instruction for this run (optional)", style: "flex:1;min-width:260px" });

  const stageBtn = (stage, label, paramsFn, extra) => h("div", { class: "row" }, h("button", { onclick: () => runStage(topic, stage, { ...paramsFn(), note: note.value }) }, label), extra);
  const num = (v, w = 52) => h("input", { type: "number", value: v, min: 1, style: `width:${w}px` });
  const nSources = num(5), nLimitFacts = num(5), nPerSource = num(6), nLimitQ = num(10), nLimitV = num(5);
  const draftDiff = difficultySelect(topic);
  const vtype = h("select", {}, Object.keys(ui.state.variant_types).map(v => h("option", { value: v }, v)));

  const topicIns = h("textarea", { rows: 3, placeholder: "Standing instructions for this topic (e.g. 'lean on US viewers', 'avoid questions about living people')" });
  const cfg = await api("GET", "/api/config");
  topicIns.value = cfg.instructions.topics?.[topic.id] || "";
  const saveIns = () => guarded(async () => { cfg.instructions.topics[topic.id] = topicIns.value; await api("PUT", "/api/config", { instructions: cfg.instructions }); toast("Topic instructions saved"); });

  // The top bar (approve / check / fill) and the empty-topic panel are rebuilt from fresh state after every run or edit.
  const topBarBox = h("div", {}), emptyBox = h("div", {});
  function renderTop() {
    const t0 = topicOf(topic.id), ck0 = t0.check;
    const approveBtn = t0.approved
      ? h("button", { class: "ghost", title: "This topic's approved cards can be exported to the game. Click to take it out.", onclick: () => setApproved(topic, false) }, "\u2713 Approved for the game (click to undo)")
      : h("button", { class: "good", disabled: !t0.can_approve, title: t0.can_approve ? "" : "Approve at least one question first", onclick: () => setApproved(topic, true) }, "Approve topic for the game");
    const topicBar = h("div", { class: "row", style: "margin-top:12px" }, approveBtn,
      aiBar(topic, null, { pendingCheck: ck0.unchecked + ck0.stale, canCheck: ck0.unchecked + ck0.stale + ck0.ok + ck0.issues > 0, canFill: true,
        extra: [writeAllBtn(topic, null, t0.pending.draft_questions)].filter(Boolean),
        hint: [ck0.issues ? `${ck0.issues} with issues` : "", ck0.stale ? `${ck0.stale} changed since checked` : "", ck0.unchecked ? `${ck0.unchecked} unchecked` : ""].filter(Boolean).join(" - ") }),
      h("button", { class: "ghost", onclick: () => createNode(topic, "source", [], BLANK_SOURCE) }, "+ Add a source by hand"));
    const seedN = h("input", { type: "number", value: 10, min: 1, max: 40, style: "width:64px" });
    const emptyPanel = totalNodes(t0) === 0 ? h("div", { class: "panel emptypanel", style: "margin-bottom:14px" },
      h("h2", {}, "This topic is empty"),
      h("p", { class: "muted" }, "Let the AI find one source and pull facts out of it, or build everything by hand."),
      h("div", { class: "row" }, "Facts to pull", seedN,
        h("button", { class: "primary", onclick: () => runDispatch("fill", topic, null, { count: Math.max(1, +seedN.value || 10) }) }, "Find a source and facts"),
        h("button", { onclick: () => createNode(topic, "source", [], BLANK_SOURCE) }, "+ Add a source by hand")),
      h("div", { class: "muted small", style: "margin-top:8px" }, "You will see the exact prompt first and can edit it, e.g. 'only well-known world capitals' or 'keep facts to events after 2000'.")) : null;
    topBarBox.replaceChildren(topicBar);
    emptyBox.replaceChildren(...(emptyPanel ? [emptyPanel] : []));
  }
  renderTop();
  const pipeline = h("div", { class: "panel", style: "margin-bottom:14px" },
    h("div", { class: "row", style: "justify-content:space-between" }, h("div", {}, h("h1", { style: "display:inline" }, h("span", { class: "dot", style: `background:${deptColor(topic.department)}` }), topic.name), h("span", { class: "muted", style: "margin-left:10px" }, `${topic.department} - ${topic.exportable}/${topic.target} export-ready`), topic.stage !== "active" ? h("span", { class: "chip", style: "margin-left:10px" }, topic.stage) : null),
      h("a", { onclick: () => go(topic.stage === "active" ? "#/board" : "#/ideas") }, topic.stage === "active" ? "back to board" : "back to ideas")),
    topBarBox,
    h("details", { class: "pipe-details", open: false, style: "margin-top:10px" },
    h("summary", { class: "muted" }, "Batch AI steps and difficulty spread"),
    h("div", { class: "actions", style: "margin-top:12px" },
      stageBtn("opentdb", "1. Import OpenTDB", () => ({}), h("span", { class: "muted small" }, "no AI")),
      stageBtn("find_sources", "2. Find sources", () => ({ count: +nSources.value }), nSources),
      stageBtn("extract_facts", "3. Extract facts", () => ({ limit: +nLimitFacts.value, per_item: +nPerSource.value }), [nLimitFacts, h("span", { class: "muted small" }, "sources x"), nPerSource]),
      stageBtn("draft_questions", "4. Draft questions", () => ({ limit: +nLimitQ.value, difficulty: draftDiff.value }), [nLimitQ, draftDiff]),
      stageBtn("make_variants", "5. Make variants", () => ({ limit: +nLimitV.value, variant_type: vtype.value }), [vtype, nLimitV]),
    ),
    h("div", { class: "row", style: "margin-top:10px" }, note),
    h("div", { style: "margin-top:14px" }, spreadBox),
    h("div", { style: "margin-top:14px" }, facetBox)),
    h("details", { style: "margin-top:10px" }, h("summary", { class: "muted" }, "Standing instructions for this topic"), topicIns, h("div", { style: "margin-top:6px" }, h("button", { onclick: saveIns }, "Save instructions"))),
  );

  // Phones collapse the run steps; keep that in step with the live screen width (rotating the phone, resizing a window).
  const pipeDetails = pipeline.querySelector(".pipe-details");
  ui.pipeMq = ui.pipeMq || window.matchMedia("(max-width: 760px)");
  if (ui.pipeSync) ui.pipeMq.removeEventListener("change", ui.pipeSync);
  ui.pipeSync = () => {};
  ui.pipeMq.addEventListener("change", ui.pipeSync);

  const kindSel = h("select", { onchange: () => { ui.filters.kind = kindSel.value; renderList(); } }, ["", "source", "fact", "question", "variant"].map(k => h("option", { value: k }, k || "all kinds")));
  kindSel.value = ui.filters.kind;
  const statusSel = h("select", { onchange: () => { ui.filters.status = statusSel.value; renderList(); } }, ["", "draft", "approved", "rejected"].map(k => h("option", { value: k }, k || "any status")));
  statusSel.value = ui.filters.status;
  const search = h("input", { type: "text", placeholder: "search", value: ui.filters.q, style: "flex:1", oninput: () => { ui.filters.q = search.value; renderList(); } });

  $view.replaceChildren(pipeline, emptyBox, h("div", { class: "topic-layout" + (nodeId ? " has-sel" : "") },
    h("div", { class: "panel tree-panel" }, h("div", { class: "row", style: "margin-bottom:8px" }, kindSel, statusSel, search),
      h("div", { class: "row", style: "margin-bottom:6px" }, h("button", { class: "good", onclick: () => bulkApprove(topic, null, () => reloadAll()) }, "Approve all drafts in topic")), listBox), detailBox));

  let selected = nodeId || null;
  function itemEl(n, depth) {
    const kids = n.child_count;
    return h("div", { class: "item" + (n.id === selected ? " sel" : ""), style: `margin-left:${depth * 18}px`, onclick: () => go(`#/topic/${topic.id}/${n.id}`) },
      h("span", { class: "twisty", onclick: (e) => { e.stopPropagation(); if (!kids) return; ui.collapsed.has(n.id) ? ui.collapsed.delete(n.id) : ui.collapsed.add(n.id); renderList(); } }, kids ? (ui.collapsed.has(n.id) ? "+" : "-") : ""),
      h("span", { class: "st " + n.status, title: n.status }), checkDot(n.check, n.has_suggestion), badge(n), h("span", { class: "txt" }, n.snippet || "(empty)"), mockFlag(n),
      kids ? h("span", { class: "kids" }, `${kids} derived`) : null);
  }
  function renderList() {
    const f = ui.filters, q = f.q.toLowerCase();
    const filtering = f.kind || f.status || q;
    const out = [];
    if (filtering) {
      for (const n of nodes) if ((!f.kind || n.kind === f.kind) && (!f.status || n.status === f.status) && (!q || n.snippet.toLowerCase().includes(q))) out.push(itemEl(n, 0));
    } else {
      const byParent = {}, ids = new Set(nodes.map(n => n.id));
      for (const n of nodes) for (const p of n.parent_ids) (byParent[p] ||= []).push(n);
      const walk = (n, depth, seen) => {
        out.push(itemEl(n, depth));
        if (ui.collapsed.has(n.id) || seen.has(n.id)) return;
        for (const c of byParent[n.id] || []) walk(c, depth + 1, new Set([...seen, n.id]));
      };
      for (const n of nodes) if (!n.parent_ids.some(p => ids.has(p))) walk(n, 0, new Set());
    }
    listBox.replaceChildren(...(out.length ? out : [h("div", { class: "muted", style: "padding:12px" }, nodes.length ? "Nothing matches the filters." : "No nodes yet. Use \"Find a source and facts\" or add a source by hand above.")]));
  }
  async function loadList() { nodes = (await api("GET", `/api/topics/${topic.id}/nodes`)).nodes; renderTop(); renderList(); spreadBox.replaceChildren(spreadPanel(topicOf(topic.id))); renderFacets(); }
  function explanationsRow(t) {
    const ex = t.explanations || { written: 0, verified: 0, missing: 0 };
    return h("div", { style: "margin-top:12px" }, h("h3", {}, "Explanations (the 'did you know' line)"),
      h("span", { class: "chip" }, `${ex.written} written`), h("span", { class: "chip" }, `${ex.verified} verified`),
      ex.missing ? h("span", { class: "chip action", onclick: () => runStage(t, "add_explanations", { limit: 10, note: note.value }) }, `${ex.missing} card(s) have none - add with AI (10 at a time)`) : h("span", { class: "muted small" }, "every card has one"));
  }
  function renderFacets() {
    const t = topicOf(topic.id), facets = t.facets || [];
    const editor = h("textarea", { rows: 6, placeholder: "One facet per line, e.g.\nEurope\nAsia\nAfrica" });
    editor.value = facets.join("\n");
    const save = () => guarded(async () => { await api("PUT", `/api/topics/${t.id}`, { facets: editor.value.split("\n") }); toast("Facets saved"); await refreshState(); await loadList(); await loadDetail(); });
    facetBox.replaceChildren(
      h("h3", {}, "Facets (the label on each card)"),
      facets.length
        ? h("div", {}, facets.map(f => h("span", { class: "chip" }, `${f}  ${t.facet_counts[f] ?? 0}`)),
          t.facet_unassigned ? h("span", { class: "chip action", onclick: () => runStage(t, "assign_facets", { limit: 40, note: note.value }) }, `${t.facet_unassigned} card(s) have no facet - assign with AI`) : h("span", { class: "muted small" }, "every card has a facet"),
          t.facet_orphans ? h("div", { class: "warn small", style: "margin-top:4px" }, `${t.facet_orphans} card(s) use a facet that is no longer in the list - reassign them`) : null)
        : h("div", { class: "muted small" }, "No facets yet, so cards keep their own titles. Type a list below, or let the AI propose one.",
          h("div", { style: "margin-top:6px" }, h("button", { onclick: () => runStage(t, "suggest_facets", { note: note.value }) }, "Suggest facets (AI)"))),
      h("details", { style: "margin-top:8px" }, h("summary", { class: "muted" }, "Edit facets"), editor,
        h("div", { style: "margin-top:6px" }, h("button", { onclick: save }, "Save facets"))),
      explanationsRow(t));
  }
  async function loadDetail() {
    ui.dirty = false;
    if (!selected) return detailBox.replaceChildren(h("div", { class: "panel muted" }, "Select a node to see its content, lineage and provenance."));
    const d = await guarded(() => api("GET", `/api/nodes/${selected}`));
    if (d) detailBox.replaceChildren(h("a", { class: "only-mobile", style: "display:block;margin-bottom:10px", onclick: () => go(`#/topic/${topic.id}`) }, "< back to list"),
      nodeDetail(d, topicOf(topic.id), async () => { await refreshState(); await loadList(); await loadDetail(); }));
  }
  async function reloadAll() { const at = listBox.scrollTop; await refreshState(); await loadList(); listBox.scrollTop = at; await loadDetail(); }
  ui.hooks.refresh = async () => {
    const at = listBox.scrollTop; await refreshState(); await loadList(); listBox.scrollTop = at;
    if (ui.detailRefresh) {   // a check/fill started from the open card has finished
      ui.detailRefresh = false;
      if (ui.dirty) toast("The run finished, but you have unsaved edits here - save, then reopen the card to see the result.");
      else await loadDetail();
    }
  };
  await loadList(); await loadDetail();
  if (keep) { listBox.scrollTop = keep.list; window.scrollTo(0, keep.y); }
  else { const sel = listBox.querySelector(".item.sel"); if (sel) listBox.scrollTop = Math.max(0, sel.offsetTop - listBox.offsetTop - listBox.clientHeight / 3); }
  if (query.do) { history.replaceState(null, "", `#/topic/${topic.id}${nodeId ? "/" + nodeId : ""}`); runStage(topic, query.do, { limit: 5, per_item: 6, variant_type: "paraphrase", note: "" }); }
}

function showPreview(topic, preview, onRun) {
  const promptBox = h("textarea", { class: "prompt-edit", rows: 16, spellcheck: false });
  promptBox.value = preview.prompt;
  const body = h("div", {},
    preview.followup ? h("div", { class: "panel warn", style: "margin-bottom:12px" }, preview.followup) : null,
    h("div", { class: "kv" }, h("div", {}, "Stage"), h("div", {}, preview.label), h("div", {}, "Backend"), h("div", {}, `${preview.backend}  (model: ${preview.model})`),
      h("div", {}, "Tools"), h("div", {}, preview.tools.length ? preview.tools.join(", ") : "none"), h("div", {}, "Inputs"), h("div", {}, preview.inputs.length ? preview.inputs.map(i => h("div", { class: "small" }, `${i.id}: ${i.snippet.slice(0, 90)}`)) : "n/a")),
    h("label", {}, "The prompt that will be sent - you can edit it (for example: 'only well-known world capitals'). Keep the Output: line at the end."), promptBox);
  modal(`Review: ${preview.label} - ${topic.name}`, body, [{ label: "Cancel" }, { label: "Run", cls: "primary", fn: () => guarded(() => onRun(promptBox.value !== preview.prompt ? promptBox.value : undefined)) }]);
}

async function runStage(topic, stage, params) {
  const preview = await guarded(() => api("POST", "/api/run", { stage, topic: topic.id, params, preview: true }));
  if (!preview) return;
  showPreview(topic, preview, async (edited) => {
    await api("POST", "/api/run", { stage, topic: topic.id, params: { ...params, input_ids: preview.input_ids.length ? preview.input_ids : undefined }, preview: false, prompt: edited });
    toast(`${preview.label} started`); await poll(); ui.hooks.refresh?.();
  });
}

// "Check for accuracy" and "Fill in automatically" work at every level: topic (no node), source, fact, question.
async function runDispatch(kind, topic, nodeId, extra) {
  const base = { topic: topic.id, node_id: nodeId || undefined, ...(extra || {}) };
  const preview = await guarded(() => api("POST", `/api/${kind}`, { ...base, preview: true }));
  if (!preview) return;
  showPreview(topic, preview, async (edited) => {
    await api("POST", `/api/${kind}`, { ...base, prompt: edited });
    toast(`${preview.label} started`); await poll();
    await ui.hooks.refresh?.();
    if (nodeId) ui.detailRefresh = true;   // set after the refresh above, so it fires when the run finishes, not now
  });
}

const CHECK_LABEL = { unchecked: "not checked", stale: "changed since checked", ok: "checked ok", issues: "has issues" };
const checkDot = (state, hasSuggestion) => state ? h("span", { class: "ck " + state, title: CHECK_LABEL[state] + (hasSuggestion ? " - a fix is suggested" : "") }, { ok: "\u2713", issues: "!", stale: "\u21BB", unchecked: "\u00B7" }[state]) : null;

function aiBar(topic, node, o) {
  return h("div", { class: "aibar" },
    h("button", { disabled: !o.canCheck, title: o.checkHint || "Check the cards here against their facts (only what is new or changed since the last check)", onclick: () => runDispatch("check", topic, node?.id) },
      "Check for accuracy" + (o.pendingCheck ? ` (${o.pendingCheck})` : "")),
    h("button", { disabled: !o.canFill, title: o.fillHint || "Fill in whatever is blank here. Never overwrites anything you have written.", onclick: () => runDispatch("fill", topic, node?.id) }, "Fill in automatically"),
    ...(o.extra || []),
    o.hint ? h("span", { class: "muted small" }, o.hint) : null);
}

// "Write questions for every fact" beneath a source or topic (30 per run; the preview says how many are left).
const writeAllBtn = (topic, nodeId, n) => n > 0 ? h("button", { class: "primary", title: n > 30 ? `${n} facts have no question yet - this does 30 at a time` : "Draft a question for every fact here that has none",
  onclick: () => runDispatch("fill", topic, nodeId, { what: "questions" }) }, `Write questions for ${n === 1 ? "the fact" : `all ${n} facts`}${n > 30 ? " (30 at a time)" : ""}`) : null;

function confirmModal(title, text, yesLabel, yesClass, onYes) {
  modal(title, h("div", {}, h("p", {}, text)), [{ label: "Cancel" }, { label: yesLabel, cls: yesClass, fn: () => guarded(onYes) }]);
}

async function deleteNode(topic, node) {
  const pre = await guarded(() => api("DELETE", `/api/nodes/${node.id}`, { preview: true }));
  if (!pre) return;
  const parts = Object.entries(pre.by_kind).map(([k, v]) => `${v} ${k}${v === 1 ? "" : "s"}`).join(", ");
  confirmModal(pre.children ? `Delete this and ${pre.children} derived item${pre.children === 1 ? "" : "s"}?` : "Delete this permanently?",
    (pre.children ? `This also deletes: ${parts}. ` : "") + "There is no archive and no undo.", "Delete", "bad",
    async () => { await api("DELETE", `/api/nodes/${node.id}`, { cascade: true }); toast("Deleted"); go(`#/topic/${topic.id}`); });
}

async function setApproved(topic, want) {
  const t = topicOf(topic.id), open = t.check.unchecked + t.check.stale + t.check.issues;
  const doIt = () => guarded(async () => { await api("PUT", `/api/topics/${topic.id}`, { approved: want }); toast(want ? "Topic approved for the game" : "Topic no longer approved"); route(); });
  if (want && open) return confirmModal("Approve anyway?", `${open} card${open === 1 ? " is" : "s are"} unchecked, changed since checked, or have unresolved issues. Approve the topic anyway?`, "Approve anyway", "good", doIt);
  doIt();
}

async function createNode(topic, kind, parents, payload, variantType) {
  const r = await guarded(() => api("POST", "/api/nodes", { topic: topic.id, kind, parent_ids: parents, payload, variant_type: variantType }));
  if (r) go(`#/topic/${topic.id}/${r.node.id}`);
}
const BLANK_SOURCE = { source_type: "manual", title: "", url: "", summary: "", excerpt: "", license: "" };
const BLANK_FACT = { text: "", context: "", difficulty_hint: "intermediate" };
const BLANK_CARD = { title: "", facet: "", question: "", answer: "", choices: ["", "", "", ""], difficulty: "intermediate", input_type: "multiple_choice" };

// ---------------------------------------------------------------- ideas inbox
const VERDICTS = {
  unvetted: { label: "not vetted", color: "var(--muted)", help: "Run the vet to see how much source material exists." },
  vetting: { label: "vetting", color: "var(--amber)", help: "Research is running..." },
  strong: { label: "strong", color: "var(--green)", help: "Meets the bar: enough facts from independent sites." },
  maybe: { label: "maybe", color: "var(--amber)", help: "Some material, but below the bar. Review the sources, reject junk, or vet again." },
  thin: { label: "thin", color: "var(--red)", help: "Very little usable material found. Probably not worth a topic." },
};
const totalNodes = (t) => Object.values(t.counts).reduce((a, c) => a + c.draft + c.approved + c.rejected, 0);

function meter(label, value, goal) {
  return h("div", { style: "margin-top:6px" }, h("div", { class: "small muted" }, `${label}: ${value} / ${goal}`),
    h("div", { class: "bar", style: "min-width:0;height:7px" }, h("i", { style: `width:${Math.min(100, value / goal * 100)}%` })));
}

async function vetIdea(t) {
  const preview = await guarded(() => api("POST", `/api/topics/${t.id}/vet`, { preview: true }));
  if (!preview) return;
  showPreview(t, preview, async () => {
    await api("POST", `/api/topics/${t.id}/vet`, { preview: false });
    toast(`Vetting "${t.name}" started`); await poll(); ui.hooks.refresh?.();
  });
}

function promoteIdea(t) {
  const v = t.vet;
  modal(`Promote "${t.name}"?`, h("div", {},
    h("div", { class: "kv" }, h("div", {}, "Verdict"), h("div", {}, VERDICTS[v.verdict].label), h("div", {}, "Sources"), h("div", {}, `${v.sources} from ${v.domains} site(s)`), h("div", {}, "Facts"), h("div", {}, `${v.facts} (bar: ${v.min_facts})`)),
    v.verdict !== "strong" ? h("p", { class: "warn" }, `This idea hasn't met the bar (${v.min_facts} facts from ${v.min_domains}+ sites). You can still promote it.`) : null,
    h("p", { class: "muted small" }, `It moves onto the Board with its sources and facts intact. Before the game can load its cards, add res://data/${t.folder}/curriculum.json to TOPIC_PATHS in autoload/srs_engine.gd (Export reminds you).`)),
    [{ label: "Cancel" }, { label: "Promote to Board", cls: "primary", fn: () => guarded(async () => { await api("PUT", `/api/topics/${t.id}`, { stage: "active" }); toast(`${t.name} is on the Board`); go(`#/topic/${t.id}`); }) }]);
}

function editIdea(t) {
  const name = h("input", { type: "text", value: t.name, style: "width:100%" });
  const dept = h("select", {}, ui.state.departments.map(d => h("option", { value: d, selected: d === t.department }, d)));
  const note = h("textarea", { rows: 3, value: t.note || "" });
  modal(`Edit idea`, h("div", {}, h("label", {}, "Name"), name, h("label", {}, "Department"), dept, h("label", {}, "Scope notes (sent with every AI run for this topic)"), note),
    [{ label: "Cancel" }, { label: "Save", cls: "primary", fn: () => guarded(async () => { await api("PUT", `/api/topics/${t.id}`, { name: name.value, department: dept.value, note: note.value }); await refreshState(); ui.hooks.refresh?.(); }) }]);
}

async function viewIdeas() {
  const st = ui.state, d = ui.ideaDraft;
  if (!d.dept) d.dept = st.departments[0];
  const name = h("input", { type: "text", value: d.name, placeholder: "Topic idea, e.g. Roman Emperors", style: "flex:1;min-width:220px", oninput: () => { d.name = name.value; } });
  const dept = h("select", { onchange: () => { d.dept = dept.value; } }, st.departments.map(x => h("option", { value: x, selected: x === d.dept }, x)));
  const note = h("textarea", { rows: 2, value: d.note, oninput: () => { d.note = note.value; }, placeholder: "What's it about? Scope hints help the research, e.g. 'only the original trilogy' or 'US parks only, not worldwide'." });
  const add = (thenVet) => guarded(async () => {
    const t = await api("POST", "/api/topics", { name: name.value, department: dept.value, note: note.value });
    toast(`Added idea: ${t.name}`); d.name = ""; d.note = "";
    await refreshState(); renderNav("ideas"); viewIdeas();
    if (thenVet) vetIdea(t);
  });
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") add(false); });

  const card = (t) => {
    const v = t.vet, verdict = VERDICTS[v.verdict], busy = v.verdict === "vetting";
    return h("div", { class: "panel" },
      h("div", { class: "row", style: "justify-content:space-between;align-items:flex-start" },
        h("div", {}, h("span", { class: "dot", style: `background:${deptColor(t.department)}` }), h("a", { onclick: () => go(`#/topic/${t.id}`), style: "font-weight:600;font-size:15px" }, t.name),
          h("div", { class: "muted small", style: "margin-left:17px" }, `${t.department} - added ${fmtTime(t.created_at)}`)),
        h("span", { class: "chip", style: `border-color:${verdict.color};color:${verdict.color}` }, busy ? h("span", { class: "spinner" }) : null, verdict.label)),
      t.note ? h("div", { style: "margin:8px 0 0 17px" }, t.note) : null,
      h("div", { class: "muted small", style: "margin:8px 0 0 17px" }, verdict.help),
      v.sources || v.facts ? h("div", { style: "margin:4px 0 0 17px" }, meter(`facts (from ${v.sources} sources, ${v.domains} site${v.domains === 1 ? "" : "s"})`, v.facts, v.min_facts), meter("independent sites", v.domains, v.min_domains)) : null,
      v.mock ? h("div", { class: "small", style: "margin:6px 0 0 17px;color:var(--pink)" }, "These numbers come from the mock backend and mean nothing.") : null,
      h("div", { class: "actions", style: "margin-top:12px" },
        h("button", { class: "primary", disabled: busy, onclick: () => vetIdea(t) }, v.verdict === "unvetted" ? "Vet this idea" : "Vet again"),
        (v.sources || v.facts) ? h("button", { onclick: () => go(`#/topic/${t.id}`) }, "Review sources & facts") : null,
        h("button", { class: "good", disabled: busy, onclick: () => promoteIdea(t) }, "Promote"),
        h("button", { class: "ghost", onclick: () => editIdea(t) }, "Edit"),
        h("button", { class: "ghost", onclick: () => guarded(async () => { await api("PUT", `/api/topics/${t.id}`, { stage: "archived" }); toast("Archived"); await refreshState(); viewIdeas(); renderNav("ideas"); }) }, "Archive"),
        totalNodes(t) === 0 ? h("button", { class: "ghost bad", onclick: () => confirm(`Delete "${t.name}"?`) && guarded(async () => { await api("DELETE", `/api/topics/${t.id}`); await refreshState(); viewIdeas(); renderNav("ideas"); }) }, "Delete") : null));
  };

  const ideas = [...st.ideas].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const archived = st.archived.length ? h("details", { style: "margin-top:18px" }, h("summary", { class: "muted" }, `Archived (${st.archived.length})`),
    st.archived.map(t => h("div", { class: "row", style: "margin-top:6px" }, h("span", {}, t.name), h("span", { class: "muted small" }, t.department),
      h("button", { class: "ghost", onclick: () => guarded(async () => { await api("PUT", `/api/topics/${t.id}`, { stage: "idea" }); await refreshState(); viewIdeas(); renderNav("ideas"); }) }, "Restore")))) : null;

  $view.replaceChildren(h("h1", {}, "Ideas inbox"),
    h("div", { class: "muted", style: "margin-bottom:12px" }, `Jot a topic down, then vet it: the studio looks for sources and counts distinct facts. Promote it once it clears the bar (${st.ideas[0]?.vet.min_facts ?? 30} facts from 2+ sites) and it joins the Board.`),
    h("div", { class: "panel", style: "margin-bottom:16px" }, h("div", { class: "row" }, name, dept), h("label", {}, "Notes (optional)"), note,
      h("div", { class: "row", style: "margin-top:10px" }, h("button", { onclick: () => add(false) }, "Add idea"), h("button", { class: "primary", onclick: () => add(true) }, "Add and vet"))),
    ideas.length ? h("div", { class: "grid2" }, ideas.map(card)) : h("div", { class: "muted" }, "No ideas waiting. Add one above."), archived || "");
  ui.hooks.refresh = async () => { await refreshState(); renderNav("ideas"); viewIdeas(); };
}

// ---------------------------------------------------------------- difficulty picker
// "auto" lets the AI rate each question and steer toward the tiers the topic is short of; a tier asks for a question written for that tier.
function difficultySelect(topic) {
  const sp = topicOf(topic.id)?.spread || {};
  const sel = h("select", { onchange: () => { ui.draftDifficulty = sel.value; } },
    h("option", { value: "auto" }, "auto (AI rates it, favouring tiers still short)"),
    tierNames().map(n => { const t = ui.state.tiers.find(x => x.name === n), c = sp[n]; return h("option", { value: n }, `${n} - ${t?.score ?? "?"} pts${c ? ` (${c.cards}/${c.target} so far)` : ""}`); }));
  sel.value = ui.draftDifficulty && [...sel.options].some(o => o.value === ui.draftDifficulty) ? ui.draftDifficulty : "auto";
  return sel;
}

// ---------------------------------------------------------------- bulk approve
async function bulkApprove(topic, nodeId, after) {
  const scope = nodeId ? { node_id: nodeId } : {};
  const pre = await guarded(() => api("POST", `/api/topics/${topic.id}/approve`, { ...scope, preview: true }));
  if (!pre) return;
  const where = nodeId ? "under this item" : `in ${topic.name}`;
  if (!pre.approved) return toast(pre.blocked_count ? `Nothing to approve ${where}: ${pre.blocked_count} draft card(s) need fixing first` : `No drafts to approve ${where}`, !!pre.blocked_count);
  const parts = Object.entries(pre.by_kind).map(([k, v]) => `${v} ${k}${v === 1 ? "" : "s"}`).join(", ");
  modal(`Approve ${pre.approved} draft${pre.approved === 1 ? "" : "s"} ${where}?`, h("div", {},
    h("p", {}, parts + ". Rejected items stay rejected, and anything already approved is untouched."),
    pre.blocked_count ? h("div", { class: "warn small" }, `${pre.blocked_count} draft card(s) will be skipped because they need fixing first:`,
      pre.blocked.map(b => h("div", { class: "small" }, h("a", { onclick: () => { document.getElementById("modal-root").textContent = ""; go(`#/topic/${topic.id}/${b.id}`); } }, b.snippet.slice(0, 70)), ` - ${b.why}`)),
      pre.blocked_count > pre.blocked.length ? h("div", { class: "small" }, `...and ${pre.blocked_count - pre.blocked.length} more`) : null) : null,
    h("p", { class: "muted small" }, "Each change is recorded in the item's edit history as a bulk approval.")),
    [{ label: "Cancel" }, { label: `Approve ${pre.approved}`, cls: "primary", fn: () => guarded(async () => {
      const r = await api("POST", `/api/topics/${topic.id}/approve`, { ...scope, preview: false });
      toast(`Approved ${r.approved}${r.blocked_count ? `, skipped ${r.blocked_count}` : ""}`); await after?.();
    }) }]);
}

// ---------------------------------------------------------------- node detail
function nodeLine(n, topicId) {
  return h("div", { class: "item", onclick: () => go(`#/topic/${topicId}/${n.id}`) }, h("span", { class: "st " + n.status }), badge(n), h("span", { class: "txt" }, n.snippet || "(empty)"), mockFlag(n));
}

function nodeDetail(d, topic, reload) {
  const node = d.node, p = node.payload;
  const isQ = node.kind === "question" || node.kind === "variant";
  const fields = {};
  const field = (key, label, el) => { fields[key] = el; return [h("label", {}, label), el]; };
  let choiceInputs = [], answerIdx = -1, choicesBox = null, form, verifiedBox = null;

  if (node.kind === "source") {
    form = [field("title", "Title", h("input", { type: "text", value: p.title || "", style: "width:100%" })), field("url", "URL", h("input", { type: "text", value: p.url || "", style: "width:100%" })),
      p.url ? h("a", { href: p.url, target: "_blank", rel: "noopener" }, "open source page") : null,
      field("summary", "Summary", h("textarea", { rows: 3, value: p.summary || "" })), field("excerpt", "Excerpt", h("textarea", { rows: 2, value: p.excerpt || "" })),
      field("license", "License / usage note", h("input", { type: "text", value: p.license || "", style: "width:100%" }))];
  } else if (node.kind === "fact") {
    form = [field("text", "Fact", h("textarea", { rows: 4, value: p.text || "" })),
      field("difficulty_hint", "Difficulty hint", h("select", {}, tierNames().map(x => h("option", { value: x, selected: x === p.difficulty_hint }, x)))),
      field("context", "Context (extra detail the source states)", h("textarea", { rows: 3, value: p.context || "" }))];
  } else {
    const fixed = p.input_type === "true_false";
    const choices = fixed ? ["True", "False"] : [...(p.choices || []), "", "", "", ""].slice(0, 4);
    answerIdx = choices.indexOf(p.answer);
    choiceInputs = choices.map((c, i) => h("input", { type: "text", value: c, disabled: fixed }));
    choicesBox = h("div", {}, choices.map((c, i) => h("div", { class: "choice-row" },
      h("input", { type: "radio", name: "ans", checked: i === answerIdx, onchange: () => { answerIdx = i; } }), choiceInputs[i])));
    const facets = topic.facets || [];
    const faceField = () => facets.length
      ? field("facet", "Facet (this card's label in the game)", h("select", {}, [
        h("option", { value: "" }, p.facet ? "(clear)" : (p.title ? `(none yet - old title: ${p.title})` : "(pick one)")),
        ...facets.map(f => h("option", { value: f, selected: f === p.facet }, f))]))
      : field("title", "Card title", h("input", { type: "text", value: p.title || "", style: "width:100%" }));
    verifiedBox = h("input", { type: "checkbox", checked: !!p.explanation_verified });
    const explTa = h("textarea", { rows: 3, value: p.explanation || "", oninput: () => { verifiedBox.checked = false; } });  // editing the text un-verifies it
    form = [faceField(),
      field("question", "Question", h("textarea", { rows: 3, value: p.question || "" })),
      h("label", {}, `Choices - select the correct one (${p.input_type})`), choicesBox,
      h("div", { class: "grid2" }, h("div", {}, field("difficulty", "Difficulty", h("select", {}, tierNames().map(x => h("option", { value: x, selected: x === p.difficulty }, `${x} (${ui.state.tiers.find(t => t.name === x)?.score ?? "?"} pts)`))))),
        h("div", {}, field("card_id", "Card id (used in the game files)", h("input", { type: "text", value: p.card_id || "", style: "width:100%" })))),
      field("explanation", "Did you know (shown after the answer, max 240 characters)", explTa),
      field("explanation_source", "Explanation source (url)", h("input", { type: "text", value: p.explanation_source || "", style: "width:100%" })),
      h("label", { style: "display:flex;gap:8px;align-items:center;color:var(--text)" }, verifiedBox, "I checked this explanation against its source")];
  }

  const collect = () => {
    const out = {};
    for (const [k, el] of Object.entries(fields)) out[k] = el.value;
    if (isQ) { out.choices = choiceInputs.map(i => i.value); out.answer = answerIdx >= 0 ? out.choices[answerIdx] : ""; out.input_type = p.input_type; out.explanation_verified = !!verifiedBox?.checked; }
    return out;
  };
  const save = (status) => guarded(async () => {
    const body = { payload: collect() }; if (status) body.status = status;
    await api("PUT", `/api/nodes/${node.id}`, body); toast(status ? `Marked ${status}` : "Saved"); await reload();
  });

  const reviseNote = h("textarea", { rows: 2, placeholder: "What should change? e.g. 'make the distractors closer in era' or 'too easy, ask about the year instead'" });
  const revise = isQ ? h("div", { class: "panel", style: "margin-top:12px" }, h("h3", {}, "Revise with AI"), reviseNote,
    h("div", { style: "margin-top:6px" }, h("button", { onclick: () => runStage(topic, "revise", { input_ids: [node.id], note: reviseNote.value }) }, "Revise (creates a child variant)"))) : null;

  const lineage = d.ancestors.length ? h("div", { class: "panel", style: "margin-bottom:12px" }, h("h3", {}, "Came from"),
    d.ancestors.map(a => h("div", { class: "lineage", style: `margin-left:${(d.ancestors[0].depth - a.depth) * 14}px` }, nodeLine(a, topic.id)))) : null;
  const kids = d.children.length ? h("div", { class: "panel", style: "margin-top:12px" },
    h("div", { class: "row", style: "justify-content:space-between;margin-bottom:6px" }, h("h3", { style: "margin:0" }, `Derived from this (${d.children.length})`),
      h("button", { class: "good", onclick: () => bulkApprove(topic, node.id, reload) }, "Approve everything derived from this")),
    d.children.map(c => nodeLine(c, topic.id))) : null;

  const haveFacts = d.children.filter(c => c.kind === "fact").length;
  const moreN = h("input", { type: "number", value: 25, min: 1, max: 40, style: "width:64px" });
  const moreNote = h("input", { type: "text", placeholder: "Optional steer, e.g. 'only African countries' or 'harder ones'", style: "flex:1;min-width:200px" });
  const diff = (node.kind === "fact" || d.facts_waiting) ? difficultySelect(topic) : null;
  const qNote = h("input", { type: "text", placeholder: "Optional steer, e.g. 'ask about the founding date' or 'avoid the obvious distractors'", style: "flex:1;min-width:200px" });
  const haveQs = d.children.filter(c => c.kind === "question").length;
  const writeQs = node.kind === "fact"
    ? h("div", { class: "panel", style: "margin-top:12px" }, h("h3", {}, haveQs ? `Write another question from this fact (${haveQs} so far)` : "Write a question from this fact"),
      h("div", { class: "row" }, h("span", { class: "muted small" }, "Difficulty"), diff, qNote,
        h("button", { class: "primary", onclick: () => runStage(topic, "draft_questions", { input_ids: [node.id], difficulty: diff.value, note: qNote.value }) }, "Write question")))
    : d.facts_waiting
      ? h("div", { class: "panel", style: "margin-top:12px" }, h("h3", {}, `Write questions for the ${d.facts_waiting} fact${d.facts_waiting === 1 ? "" : "s"} under this that have none`),
        h("div", { class: "muted small", style: "margin-bottom:8px" }, "Rejected facts and facts that already have a question are skipped. 30 per run; run it again for the rest."),
        h("div", { class: "row" }, h("span", { class: "muted small" }, "Difficulty"), diff, qNote,
          h("button", { class: "primary", onclick: () => runStage(topic, "draft_questions", { under: node.id, limit: 30, difficulty: diff.value, note: qNote.value }) }, `Write questions (${Math.min(30, d.facts_waiting)})`)))
      : null;
  const moreFacts = node.kind === "source" ? h("div", { class: "panel", style: "margin-top:12px" }, h("h3", {}, haveFacts ? `Get more facts from this source (${haveFacts} so far)` : "Extract facts from this source"),
    h("div", { class: "muted small", style: "margin-bottom:8px" }, "The AI reads the page again and returns only facts it hasn't already taken from here. Up to 40 per run - run it again for more."),
    h("div", { class: "row" }, h("span", { class: "muted small" }, "How many"), moreN, moreNote,
      h("button", { class: "primary", onclick: () => runStage(topic, "extract_facts", { input_ids: [node.id], limit: 1, per_item: Math.min(40, Math.max(1, +moreN.value || 25)), note: moreNote.value }) }, haveFacts ? "Get more facts" : "Extract facts"))) : null;

  const prov = node.provenance || {};
  const runLink = prov.run_id ? h("a", { onclick: () => go(`#/runs/${prov.run_id}`) }, prov.run_id) : null;
  const instrBox = h("div", {});
  const provPanel = h("div", { class: "panel", style: "margin-top:12px" }, h("h3", {}, "Provenance"),
    h("div", { class: "kv" }, h("div", {}, "Origin"), h("div", {}, prov.origin || "unknown", mockFlag(node)), h("div", {}, "Created"), h("div", {}, fmtTime(node.created_at)),
      prov.stage ? [h("div", {}, "Stage"), h("div", {}, prov.stage)] : null, prov.backend ? [h("div", {}, "Backend"), h("div", {}, `${prov.backend}${prov.model && prov.model !== prov.backend ? " / " + prov.model : ""}`)] : null,
      runLink ? [h("div", {}, "Run"), h("div", {}, runLink)] : null, prov.note ? [h("div", {}, "Note"), h("div", {}, prov.note)] : null,
      node.payload.opentdb ? [h("div", {}, "OpenTDB"), h("div", { class: "small" }, `${node.payload.opentdb.category} / ${node.payload.opentdb.difficulty}: ${node.payload.opentdb.question} -> ${node.payload.opentdb.correct}`)] : null),
    prov.run_id ? h("details", { style: "margin-top:8px", ontoggle: async (e) => { if (e.target.open && !instrBox.childNodes.length) { const r = await guarded(() => api("GET", `/api/runs/${prov.run_id}`)); if (r) instrBox.append(h("div", { class: "prompt" }, r.instructions || "(no standing instructions were set)")); } } }, h("summary", { class: "muted" }, "Instructions that were in force"), instrBox) : null,
    node.history.length ? h("details", { style: "margin-top:8px" }, h("summary", { class: "muted" }, `Edit history (${node.history.length})`),
      node.history.slice().reverse().map(x => h("div", { class: "small", style: "margin-top:4px" }, `${fmtTime(x.at)} - ${x.action}${x.action === "status" ? `: ${x.from} -> ${x.to}` : ""}`))) : null);

  const problems = d.problems.length ? h("div", { class: "warn small", style: "margin-top:8px" }, "Needs fixing before approval: " + d.problems.join("; ")) : null;
  const ck = d.check || {}, roll = ck.rollup;
  const approveGuard = () => {
    const why = { unchecked: "hasn't been checked for accuracy", stale: "changed since its last accuracy check", issues: "was checked and has unresolved issues" }[ck.state];
    if (isQ && ck.has_fact && why) return confirmModal("Approve anyway?", `This card ${why}.`, "Approve anyway", "good", () => save("approved"));
    save("approved");
  };
  const pendingCheck = isQ ? (ck.state === "unchecked" || ck.state === "stale" ? 1 : 0) : (roll ? roll.unchecked + roll.stale : 0);
  const aiRow = h("div", { class: "panel", style: "margin-top:12px" }, h("h3", {}, "AI help for this " + node.kind),
    aiBar(topic, node, isQ
      ? { pendingCheck, canCheck: !!ck.has_fact, checkHint: ck.has_fact ? "" : "No fact to check against - this card has no parent fact", canFill: (ck.missing || []).length > 0,
          fillHint: (ck.missing || []).length ? "" : "Nothing is blank on this card", hint: (ck.missing || []).length ? `Blank: ${ck.missing.join(", ")}` : "" }
      : { pendingCheck, canCheck: !!roll && roll.unchecked + roll.stale + roll.ok + roll.issues > 0, checkHint: "No cards beneath this to check yet",
          canFill: true, fillHint: node.kind === "source" ? "Read this source and add new facts from it" : "", extra: [writeAllBtn(topic, node.id, d.facts_waiting)].filter(Boolean), hint: roll ? [roll.issues ? `${roll.issues} with issues` : "", roll.stale ? `${roll.stale} changed since checked` : "", roll.unchecked ? `${roll.unchecked} unchecked` : ""].filter(Boolean).join(" - ") : "" }));
  const act = (action) => guarded(async () => { await api("POST", `/api/nodes/${node.id}/suggestion`, { action }); await reload(); });
  const lastHist = node.history[node.history.length - 1];
  const view = (v) => Array.isArray(v) ? v.join("  |  ") : String(v ?? "");
  const STATE_TXT = { unchecked: ["Not checked yet", "muted"], stale: ["Changed since the last check", "warn"], ok: ["Checked: consistent with its fact", "tag-ok"], issues: ["Checked: issues found", "err"] };
  const ckPanel = isQ ? h("div", { class: "panel", style: "margin-top:12px" }, h("h3", {}, "Accuracy check"),
    h("div", { class: STATE_TXT[ck.state][1] }, STATE_TXT[ck.state][0], ck.record?.at ? h("span", { class: "muted small" }, `  (${fmtTime(ck.record.at)})`) : null),
    ck.record?.notes ? h("div", { style: "margin-top:6px" }, ck.record.notes) : null,
    ck.has_fact ? null : h("div", { class: "muted small" }, "This card has no parent fact, so there is nothing to check it against."),
    ck.suggestion ? h("div", { class: "sug" }, h("div", { class: "small muted" }, "Suggested change - nothing has been applied yet:"),
      Object.entries(ck.suggestion.fields).map(([k, v]) => h("div", { class: "diff" }, h("div", { class: "small muted" }, k), h("div", { class: "old" }, view(p[k])), h("div", { class: "new" }, view(v)))),
      h("div", { class: "row", style: "margin-top:8px" }, h("button", { class: "good", onclick: () => act("accept") }, "Accept suggestion"), h("button", { onclick: () => act("dismiss") }, "Dismiss"),
        h("button", { class: "ghost", onclick: () => act("keep") }, "Keep as written"), h("span", { class: "muted small" }, "Or edit the card yourself and save - that clears the suggestion."))) : null,
    lastHist?.action === "accept_suggestion" ? h("div", { style: "margin-top:8px" }, h("button", { class: "ghost", onclick: () => act("undo") }, "Undo the accepted suggestion")) : null) : null;
  let addRow = null;
  if (node.kind === "source") addRow = h("div", { class: "row", style: "margin-top:12px" }, h("button", { onclick: () => createNode(topic, "fact", [node.id], BLANK_FACT) }, "+ Add a fact by hand"));
  else if (node.kind === "fact") addRow = h("div", { class: "row", style: "margin-top:12px" }, h("button", { onclick: () => createNode(topic, "question", [node.id], BLANK_CARD) }, "+ Add a question by hand"));
  else if (node.kind === "question") {
    const vsel = h("select", {}, Object.keys(ui.state.variant_types).map(v => h("option", { value: v }, v.replace("_", "/"))));
    const copy = () => vsel.value === "true_false"
      ? { ...BLANK_CARD, title: p.title, facet: p.facet, question: p.question, answer: "True", choices: ["True", "False"], difficulty: p.difficulty, input_type: "true_false" }
      : { ...BLANK_CARD, title: p.title, facet: p.facet, question: p.question, answer: p.answer, choices: [...(p.choices || [])], difficulty: p.difficulty, explanation: p.explanation || "" };
    addRow = h("div", { class: "panel", style: "margin-top:12px" }, h("h3", {}, "Make a variant of this question"),
      h("div", { class: "row" }, vsel, h("button", { onclick: () => createNode(topic, "variant", [node.id], copy(), vsel.value) }, "Copy to edit by hand"),
        h("button", { class: "primary", onclick: () => runStage(topic, "make_variants", { input_ids: [node.id], variant_type: vsel.value, note: "" }) }, "Write it with AI")),
      h("div", { class: "muted small", style: "margin-top:6px" }, "A variant uses the same fact: easier, harder, reworded or true/false. 'Copy to edit by hand' starts from this question's text."));
  }
  return h("div", {}, lineage,
    h("div", { class: "panel" },
      h("div", { class: "row", style: "justify-content:space-between" }, h("div", {}, badge(node), h("span", { class: "st " + node.status }), node.status, mockFlag(node), h("span", { class: "mono muted", style: "margin-left:10px" }, node.id)),
        h("div", { class: "actions" },
          h("button", { class: "good", onclick: approveGuard }, "Approve"), h("button", { class: "bad", onclick: () => save("rejected") }, "Reject"),
          node.status !== "draft" ? h("button", { class: "ghost", onclick: () => save("draft") }, "Back to draft") : null,
          h("button", { class: "primary", onclick: () => save() }, "Save edits"))),
      form, problems,
      h("div", { style: "margin-top:12px" }, h("button", { class: "ghost bad", onclick: () => deleteNode(topic, node) }, "Delete permanently"))),
    ckPanel, aiRow, addRow,
    (moreFacts || writeQs || revise) ? h("details", { class: "panel", style: "margin-top:12px" }, h("summary", { class: "muted" }, "More AI tools (batch and revise)"), moreFacts, writeQs, revise) : null,
    kids, provPanel);
}

// ---------------------------------------------------------------- runs
async function viewRuns() {
  const { runs } = await api("GET", "/api/runs");
  const rows = runs.map(r => h("tr", { class: "cell-link", onclick: () => go(`#/runs/${r.id}`) },
    h("td", {}, fmtTime(r.created_at)), h("td", {}, (ui.state.stages[r.stage] || r.stage) + (r.chain === "vet" || r.chain_parent ? " (vet)" : "")), h("td", {}, topicOf(r.topic)?.name || r.topic),
    h("td", {}, h("span", { class: "st " + (r.status === "done" ? "approved" : r.status === "failed" ? "rejected" : "draft") }), r.status), h("td", {}, r.backend),
    h("td", {}, `${r.created_count} nodes${r.warning_count ? `, ${r.warning_count} warnings` : ""}`), h("td", {}, r.cost_usd != null ? `$${(+r.cost_usd).toFixed(3)}` : ""), h("td", {}, r.duration_s != null ? `${r.duration_s}s` : "")));
  $view.replaceChildren(h("h1", {}, "Runs"), h("div", { class: "panel" }, runs.length ? h("table", { class: "board" }, h("thead", {}, h("tr", {}, ["When", "Stage", "Topic", "Status", "Backend", "Result", "Cost", "Time"].map(x => h("th", {}, x)))), h("tbody", {}, rows)) : h("div", { class: "muted" }, "No runs yet.")));
  ui.hooks.refresh = viewRuns;
}

async function viewRun(id) {
  const r = await guarded(() => api("GET", `/api/runs/${id}`));
  if (!r) return go("#/runs");
  $view.replaceChildren(
    h("div", { class: "row" }, h("a", { onclick: () => go("#/runs") }, "all runs"), h("h1", { style: "margin:0 0 0 10px" }, `${ui.state.stages[r.stage] || r.stage} - ${topicOf(r.topic)?.name || r.topic}`)),
    h("div", { class: "panel", style: "margin-top:12px" },
      h("div", { class: "kv" }, h("div", {}, "Status"), h("div", {}, r.status, r.status === "running" || r.status === "queued" ? h("button", { style: "margin-left:10px", onclick: () => guarded(async () => { await api("POST", `/api/runs/${id}/cancel`, {}); toast("Cancelling"); }) }, "Cancel") : null),
        h("div", {}, "Backend"), h("div", {}, `${r.backend} / ${r.model || "default"}`), h("div", {}, "Started"), h("div", {}, fmtTime(r.started_at)), h("div", {}, "Duration"), h("div", {}, r.duration_s != null ? r.duration_s + "s" : ""),
        h("div", {}, "Cost"), h("div", {}, r.cost_usd != null ? "$" + (+r.cost_usd).toFixed(4) : "n/a"), h("div", {}, "Tools"), h("div", {}, r.tools?.length ? r.tools.join(", ") : "none"),
        h("div", {}, "Params"), h("div", { class: "mono" }, JSON.stringify(r.params))),
      r.error ? [h("label", {}, "Error"), h("div", { class: "err" }, r.error)] : null,
      r.warnings?.length ? [h("label", {}, "Warnings"), r.warnings.map(w => h("div", { class: "warn small" }, w))] : null,
      h("label", {}, `Created nodes (${r.created_node_ids.length})`), h("div", {}, r.created_node_ids.map(n => h("a", { class: "chip", onclick: () => go(`#/node/${n}`) }, n))),
      r.updated_node_ids?.length ? [h("label", {}, `Updated nodes (${r.updated_node_ids.length})`), h("div", {}, r.updated_node_ids.map(n => h("a", { class: "chip", onclick: () => go(`#/node/${n}`) }, n)))] : null,
      r.applied_facets?.length ? [h("label", {}, "Facets applied to the topic"), h("div", {}, r.applied_facets.map(f => h("span", { class: "chip" }, f)))] : null,
      h("label", {}, "Input nodes"), h("div", {}, r.input_ids.map(n => h("a", { class: "chip", onclick: () => go(`#/node/${n}`) }, n))),
      h("label", {}, "Instructions in force"), h("div", { class: "prompt" }, r.instructions || "(none)"),
      h("label", {}, "Prompt sent"), h("div", { class: "prompt" }, r.prompt),
      h("label", {}, "Raw output"), h("div", { class: "prompt" }, r.raw_output || "(none)")));
  ui.hooks.refresh = () => viewRun(id);
}

// ---------------------------------------------------------------- settings
async function viewSettings() {
  const cfg = await api("GET", "/api/config");
  const ins = cfg.instructions;
  const backend = h("select", {}, ["claude_cli", "mock"].map(b => h("option", { value: b, selected: b === cfg.backend }, b === "mock" ? "mock (offline fake data, never exported)" : "claude_cli (Claude Code login)")));
  const model = h("input", { type: "text", value: cfg.model || "", placeholder: "blank = CLI default, or e.g. sonnet / opus", style: "width:100%" });
  const budget = h("input", { type: "number", step: "0.25", value: cfg.budget_usd, style: "width:90px" }), timeout = h("input", { type: "number", value: cfg.timeout_s, style: "width:90px" }), conc = h("input", { type: "number", value: cfg.concurrency, style: "width:70px" });
  const reqVerified = h("input", { type: "checkbox", checked: cfg.require_verified_explanations !== false });
  const typeBoxes = INPUT_TYPES.map(t => h("label", { style: "display:inline-flex;gap:4px;margin-right:14px;align-items:center;color:var(--text)" }, h("input", { type: "checkbox", checked: cfg.game_input_types.includes(t), value: t }), t));
  const globalIns = h("textarea", { rows: 12, value: ins.global || "" });
  const depts = [...new Set(cfg.topics.map(t => t.department))];
  const deptIns = depts.map(d => [h("label", {}, `Department: ${d}`), h("textarea", { rows: 2, value: ins.departments?.[d] || "", "data-dept": d })]);
  const stageIns = Object.keys(ui.state.stages).filter(s => s !== "opentdb").map(s => [h("label", {}, `Stage: ${ui.state.stages[s]}`), h("textarea", { rows: 2, value: ins.stages?.[s] || "", "data-stage": s })]);
  const topicRows = cfg.topics.filter(t => (t.stage || "active") === "active").map(t => ({ t,
    target: h("input", { type: "number", value: t.target, style: "width:70px" }),
    cats: h("input", { type: "text", value: (t.opentdb?.categories || []).join(","), style: "width:90px" }),
    words: h("input", { type: "text", value: (t.opentdb?.keywords || []).join(", "), style: "width:100%" }) }));
  const result = h("span", { class: "muted small" });

  const save = () => guarded(async () => {
    const instructions = { global: globalIns.value, departments: {}, stages: {}, topics: ins.topics || {} };
    document.querySelectorAll("textarea[data-dept]").forEach(e => instructions.departments[e.dataset.dept] = e.value);
    document.querySelectorAll("textarea[data-stage]").forEach(e => instructions.stages[e.dataset.stage] = e.value);
    await api("PUT", "/api/config", {
      backend: backend.value, model: model.value.trim(), budget_usd: +budget.value, timeout_s: +timeout.value, concurrency: +conc.value,
      game_input_types: typeBoxes.map(l => l.querySelector("input")).filter(i => i.checked).map(i => i.value), require_verified_explanations: reqVerified.checked, instructions,
      topics: topicRows.map(r => ({ id: r.t.id, target: +r.target.value, opentdb: { categories: r.cats.value.split(",").map(x => parseInt(x)).filter(x => x > 0), keywords: r.words.value.split(",").map(x => x.trim()).filter(Boolean) } })),
    });
    toast("Settings saved"); await refreshState(); renderNav("settings");
  });

  $view.replaceChildren(h("h1", {}, "Settings"),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("h2", {}, "AI backend"),
        h("label", {}, "Backend"), backend, h("label", {}, "Model"), model,
        h("div", { class: "row", style: "margin-top:10px" }, h("span", { class: "muted small" }, "Budget per run $"), budget, h("span", { class: "muted small" }, "Timeout s"), timeout, h("span", { class: "muted small" }, "Parallel runs"), conc),
        h("div", { class: "muted small" }, "Parallel runs take effect after restarting the studio."),
        h("div", { class: "row", style: "margin-top:12px" }, h("button", { onclick: () => guarded(async () => { result.textContent = "testing..."; await save(); const r = await api("POST", "/api/test-backend", {}); result.textContent = (r.ok ? "OK: " : "FAILED: ") + r.detail; result.className = r.ok ? "tag-ok small" : "err small"; }) }, "Save + test AI backend"), result),
        h("label", {}, "Input types the game can currently play (only these are exported)"), h("div", {}, typeBoxes),
        h("label", { style: "display:flex;gap:8px;align-items:center;color:var(--text);margin-top:12px" }, reqVerified, "Only export explanations I've marked verified"),
        h("div", { style: "margin-top:14px" }, h("button", { onclick: () => guarded(async () => { const r = await api("POST", "/api/import-game-data", {}); toast(`Imported ${r.imported} cards from data/`); }) }, "Import existing game cards from data/"))),
      h("div", { class: "panel" }, h("h2", {}, "Standing instructions"), h("div", { class: "muted small" }, "Sent with every AI run, in this order: house rules, department, topic (set on each topic page), stage."),
        h("label", {}, "House rules (all stages)"), globalIns, deptIns, stageIns)),
    h("div", { class: "panel", style: "margin-top:12px" }, h("h2", {}, "Topics: targets and OpenTDB import hints"),
      h("table", { class: "board" }, h("thead", {}, h("tr", {}, ["Topic", "Target cards", "OpenTDB category ids", "Keyword filter"].map(x => h("th", {}, x)))),
        h("tbody", {}, topicRows.map(r => h("tr", {}, h("td", {}, r.t.name), h("td", {}, r.target), h("td", {}, r.cats), h("td", {}, r.words)))))),
    h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "primary", onclick: save }, "Save settings")));
}

// ---------------------------------------------------------------- export
async function viewExport() {
  const out = h("div", {});
  const render = (results, wrote) => out.replaceChildren(...results.map(r => {
    const t = topicOf(r.topic);
    return h("div", { class: "panel", style: "margin-bottom:10px" },
      h("div", { class: "row", style: "justify-content:space-between" }, h("div", {}, h("b", {}, t?.name || r.topic), h("span", { class: "muted", style: "margin-left:10px" }, `${r.card_count} cards -> ${r.path}`)),
        wrote ? h("span", { class: r.written ? "tag-ok" : "muted" }, r.written ? "written" : "nothing written") : h("button", { class: "primary", disabled: !r.card_count, onclick: () => guarded(async () => { const w = await api("POST", "/api/export", { topic: r.topic, write: true }); toast(`Wrote ${w.results[0].card_count} cards to ${r.path}`); render(results.map(x => x.topic === r.topic ? w.results[0] : x), false); }) }, "Write file")),
      r.added.length || r.removed.length ? h("div", { class: "small" }, `${r.added.length} new card id(s), ${r.removed.length} id(s) that would be dropped from the file`, r.removed.length ? h("span", { class: "warn" }, ` (dropping: ${r.removed.join(", ")})`) : null) : h("div", { class: "small muted" }, "Same card ids as the file on disk (content/ordering may still differ)."),
      r.note ? h("div", { class: "muted small" }, r.note) : null,
      r.warnings.map(w => h("div", { class: "warn small" }, w)), r.skipped.map(s => h("div", { class: "small muted" }, `skipped ${s.id}: ${s.why}`)));
  }));
  $view.replaceChildren(h("h1", {}, "Export to game files"),
    h("p", { class: "muted" }, "Writes approved questions and variants into data/<topic>/curriculum.json using the game's schema. Choices are shuffled with a stable seed so the right answer isn't always first. Preview first - nothing is written until you click Write."),
    h("div", { class: "row", style: "margin-bottom:12px" }, h("button", { onclick: () => guarded(async () => render((await api("POST", "/api/export", { write: false })).results, false)) }, "Preview all topics"),
      h("button", { class: "primary", onclick: () => confirm("Overwrite the curriculum.json file for every topic that has approved cards?") && guarded(async () => { const w = await api("POST", "/api/export", { write: true }); render(w.results, true); toast("Export complete"); }) }, "Write all")),
    out);
}

window.addEventListener("hashchange", route);
route();
