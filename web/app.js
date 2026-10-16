"use strict";
// Shaky Ground UI. Everything on screen comes from the JSON API; text from papers is always set as
// text (never parsed as HTML), because titles and citation sentences are outside data.

const $ = (s) => document.querySelector(s);
const state = { mode: "blast", config: null, view: null, check: null, cy: null, focus: null, req: 0 };

// ---------- small helpers ----------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}
const num = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
const fix = (x, d = 2) => (x == null ? "–" : Number(x).toFixed(d));
const clean = (t) => (t || "(untitled)").replace(/\s+/g, " ").trim();
const short = (t, n = 60) => { t = clean(t); return t.length > n ? t.slice(0, n - 1) + "…" : t; };
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const model = (by) => { const p = (by || "").split(":"); return p.length > 1 ? p[1] : by || "loader"; };
const oa = (id) => `https://openalex.org/${id}`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  let data;
  try { data = await r.json(); } catch { data = { error: `HTTP ${r.status}` }; }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function busy(text) {
  $("#busy").hidden = !text;
  if (text) $("#busy-text").textContent = text;
}

function panel(...kids) {
  const p = $("#panel");
  p.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
  p.scrollTop = 0;
}

function setURL(params) {
  const u = new URL(location.href);
  u.search = new URLSearchParams(params).toString();
  history.replaceState(null, "", u);
}

// ---------- graph drawing ----------
function cyStyle() {
  const c = css;
  const cond = '"Barlow Condensed", "Arial Narrow", sans-serif', serif = '"Source Serif 4", Georgia, serif';
  // labels that must stay readable with the whole graph in view: never drawn smaller than px on screen
  const steady = (px) => () => Math.max(px, px / (state.cy ? state.cy.zoom() : 1));
  return [
    { selector: "node", style: {
        width: "data(size)", height: "data(size)", "background-color": c("--passing"), "border-width": 1.5,
        "border-color": c("--ink"), label: "", "font-family": cond, "font-size": 14, "font-weight": 700, color: c("--ink"),
        "text-valign": "bottom", "text-margin-y": 6, "text-wrap": "wrap", "text-max-width": 170,
        "text-background-color": c("--surface"), "text-background-opacity": 1, "text-background-padding": 3,
        "text-background-shape": "rectangle", "text-border-width": 1.5, "text-border-color": c("--ink"), "text-border-opacity": 1,
        "z-index-compare": "manual", "z-index": 2, "transition-property": "opacity", "transition-duration": 0.15 } },
    { selector: "node.builds_on", style: { "background-color": c("--builds") } },
    { selector: "node.passing", style: { "background-color": c("--passing") } },
    { selector: "node.not_shown", style: { "background-color": c("--surface"), "border-width": 1.5,
        "border-style": "dashed", "border-color": c("--unknown") } },
    { selector: "node.disputes", style: { "background-color": c("--disputes") } },
    { selector: "node.retracted", style: { "background-color": c("--retracted"), "border-width": 3,
        "border-style": "solid", "border-color": c("--retracted-ink"), color: c("--retracted-ink") } },
    { selector: "node.subject", style: { "background-color": c("--ink"), width: 46, height: 46, "font-size": 15,
        "text-max-width": 230 } },
    { selector: "node.root", style: { width: 70, height: 70, label: "data(short)", "font-family": serif, "font-size": steady(15),
        "font-weight": 600, "text-max-width": steady(240), color: c("--retracted-ink"), "border-width": 7, "border-style": "double",
        "border-color": c("--ink"), "text-border-color": c("--retracted"), "text-border-width": 2.5, "text-margin-y": steady(9) } },
    // isoseismal rings: chain length 1, 2, 3 around the epicentre
    { selector: "node.ring", style: { width: "data(d)", height: "data(d)", "background-opacity": 0, "border-width": 1.5,
        "border-style": "dashed", "border-color": c("--ink"), "border-opacity": 0.3, events: "no", "z-index": 0 } },
    { selector: "node.ringlabel", style: { width: 1, height: 1, "border-width": 0, label: "data(label)", "text-halign": (e) => e.data("ha"), "text-valign": (e) => e.data("va"), "text-wrap": "none",
        "text-margin-y": 0, "font-size": steady(13), color: c("--muted"), "text-background-color": c("--bg"), "text-border-width": 0,
        "text-background-padding": 3 } },
    { selector: "edge", style: {
        width: "mapData(reliance, 0, 1, 1, 6)", "line-color": c("--passing"), "target-arrow-color": c("--passing"),
        "target-arrow-shape": "triangle", "arrow-scale": 0.8, "curve-style": "bezier", opacity: 0.85,
        "z-index-compare": "manual", "z-index": 1, "transition-property": "opacity", "transition-duration": 0.15 } },
    { selector: "edge.FOUNDATIONAL", style: { "line-color": c("--retracted"), "target-arrow-color": c("--retracted") } },
    { selector: "edge.SUPPORTING", style: { "line-color": c("--builds"), "target-arrow-color": c("--builds") } },
    { selector: "edge.CRITICAL", style: { width: 2.5, "line-color": c("--disputes"), "target-arrow-color": c("--disputes") } },
    { selector: "edge.UNKNOWN", style: { "line-style": "dashed", "line-dash-pattern": [6, 4],
        "line-color": c("--unknown"), "target-arrow-color": c("--unknown") } },
    { selector: "edge.nocarry", style: { "line-style": "dotted", opacity: 0.55 } },
    // reference-list view: the list's own links, and each reference's verdict as one summary link
    { selector: "node.listnode", style: { events: "no" } },
    { selector: "edge.listlink", style: { width: 1.5, "line-color": c("--passing"), "target-arrow-color": c("--passing"), events: "no" } },
    { selector: "edge.sum", style: { width: 5, "line-color": c("--retracted"), "target-arrow-color": c("--retracted") } },
    { selector: "edge.sum.notproven", style: { width: 3, "line-style": "dashed", "line-dash-pattern": [7, 4],
        "line-color": c("--builds"), "target-arrow-color": c("--builds") } },
    { selector: "edge.sum.cites", style: { width: 1.5, "line-color": c("--passing"), "target-arrow-color": c("--passing") } },
    { selector: "edge.offchain", style: { opacity: 0.16, width: 1 } },
    { selector: ".hidden", style: { display: "none" } },
    { selector: ".faded", style: { opacity: 0.1 } },
    { selector: "node.hl", style: { label: "data(short)", "z-index": 10, opacity: 1 } },
    { selector: "edge.hl", style: { opacity: 1, "z-index": 9 } },
    { selector: "node:selected", style: { "overlay-color": c("--focus"), "overlay-opacity": 0.2, "overlay-padding": 7 } },
    { selector: "edge:selected", style: { "overlay-color": c("--focus"), "overlay-opacity": 0.2, "overlay-padding": 4 } },
  ];
}

function nodeSize(n) {
  return n.hops === 0 ? 66 : 12 + 5 * Math.log10((n.cited_by || 0) + 1);
}

// Rings around the root: a paper sits on ring k when its chain to the root has k links, next to the
// paper its chain goes through, so every chain reads straight in towards the centre.
function radial(nodes) {
  const pos = new Map();
  const rings = {};
  for (const n of nodes) (rings[n.hops ?? 3] ||= []).push(n);
  const rank = (n) => n.retracted ? 0 : n.tier === "builds_on" ? 1 : n.disputes ? 2 : n.tier === "passing" ? 3 : 4;
  pos.set(rings[0][0].id, { x: 0, y: 0, a: 0 });
  pos.radii = [];
  const r1 = (rings[1] || []).sort((a, b) => rank(a) - rank(b) || (b.cited_by || 0) - (a.cited_by || 0));
  let R = Math.max(240, (r1.length * 34) / (2 * Math.PI));
  if (r1.length) pos.radii.push({ k: 1, R });
  r1.forEach((n, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / r1.length;
    pos.set(n.id, { x: R * Math.cos(a), y: R * Math.sin(a), a });
  });
  for (const k of Object.keys(rings).map(Number).filter((k) => k > 1).sort()) {
    R += 190;
    pos.radii.push({ k, R });
    const gap = Math.min((2 * Math.PI) / rings[k].length, 30 / R);
    const groups = new Map();
    for (const n of rings[k]) (groups.get(n.next) || groups.set(n.next, []).get(n.next)).push(n);
    const want = [];
    for (const [next, group] of groups) {
      const a0 = (pos.get(next) || { a: 0 }).a;
      group.forEach((n, j) => want.push({ n, a: a0 + (j - (group.length - 1) / 2) * gap }));
    }
    want.sort((p, q) => p.a - q.a);
    for (let i = 1; i < want.length; i++) want[i].a = Math.max(want[i].a, want[i - 1].a + gap);
    for (const { n, a } of want) pos.set(n.id, { x: R * Math.cos(a), y: R * Math.sin(a), a });
  }
  // each ring's label goes just outside it, at the angle farthest from the papers drawn near it
  const angles = (k) => (rings[k] || []).map((n) => pos.get(n.id).a);
  for (const ring of pos.radii) {
    const near = angles(ring.k + 1).length ? angles(ring.k + 1) : angles(ring.k);
    let best = Math.PI / 2, bestGap = -1;
    for (let i = 0; i < 72; i++) {
      const a = (2 * Math.PI * i) / 72;
      const gap = Math.min(...near.map((b) => Math.abs(Math.atan2(Math.sin(a - b), Math.cos(a - b)))), Math.PI);
      if (gap > bestGap + 1e-9) { best = a; bestGap = gap; }
    }
    const cx = Math.cos(best), cy = Math.sin(best);    // the label grows away from the ring
    ring.label = { x: (ring.R + 24) * cx, y: (ring.R + 24) * cy,
                   ha: cx > 0.35 ? "right" : cx < -0.35 ? "left" : "center", va: cy > 0.35 ? "bottom" : cy < -0.35 ? "top" : "center" };
  }
  return pos;
}

function ensureCy() {
  if (state.cy) return state.cy;
  if (typeof cytoscape !== "function") throw new Error("The graph library (Cytoscape, from cdn.jsdelivr.net) didn't load.");
  const cy = cytoscape({ container: $("#cy"), style: cyStyle(), minZoom: 0.15, maxZoom: 3,
                         boxSelectionEnabled: false });
  cy.on("tap", "node", (e) => onNode(e.target));
  cy.on("tap", "edge", (e) => onEdge(e.target));
  cy.on("tap", (e) => { if (e.target === cy) clearFocus(); });
  cy.on("mouseover", "node", (e) => tip(e.target, clean(e.target.data("title")) + (e.target.data("year") ? ` (${e.target.data("year")})` : "")));
  cy.on("mouseover", "edge", (e) => tip(e.target, e.target.data("tier") ? `${e.target.data("tier")}: click to open this reference's check`
    : `${e.target.data("cls")} · weight ${e.target.data("reliance")}${e.target.data("carries") === false ? " · does not carry it on" : ""}`));
  cy.on("mouseout", () => { $("#tip").hidden = true; });
  cy.on("viewport", () => { $("#tip").hidden = true; });
  cy.on("zoom", () => cy.$(".root, .ring").updateStyle());
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => cy.style(cyStyle()));
  state.cy = cy;
  return cy;
}

function tip(target, text) {
  const t = $("#tip");
  const p = target.isNode() ? target.renderedPosition() : target.renderedMidpoint();
  t.textContent = text;
  t.hidden = false;
  const box = $("#cy").getBoundingClientRect();
  t.style.left = Math.min(p.x + 14, box.width - 330) + "px";
  t.style.top = p.y + 14 + "px";
}

function drawBlast(v) {
  const cy = ensureCy();
  const pos = radial(v.nodes);
  const els = [];
  for (const n of v.nodes) {
    const classes = [n.tier, n.retracted ? "retracted" : "", n.hops === 0 ? "root" : "", n.disputes && n.tier !== "builds_on" ? "disputes" : ""];
    els.push({ group: "nodes", data: { ...n, size: nodeSize(n), short: short(n.title, n.hops === 0 ? 70 : 44) },
               position: pos.get(n.id), classes: classes.filter(Boolean).join(" ") });
  }
  const ringLabel = { 1: "cites it directly", 2: "2 links away", 3: "3 links away" };
  for (const { k, R, label } of pos.radii) {
    els.push({ group: "nodes", data: { id: `ring-${k}`, d: 2 * R }, position: { x: 0, y: 0 }, classes: "ring",
               selectable: false, grabbable: false });
    els.push({ group: "nodes", data: { id: `ring-${k}-label`, label: (ringLabel[k] || `${k} links away`).toUpperCase(), ha: label.ha, va: label.va },
               position: { x: label.x, y: label.y }, classes: "ring ringlabel", selectable: false, grabbable: false });
  }
  for (const e of v.edges) {
    const classes = [e.cls, e.on_chain ? "onchain" : "offchain", e.carries === false ? "nocarry" : ""];
    els.push({ group: "edges", data: { ...e, id: `${e.source}>${e.target}` }, classes: classes.filter(Boolean).join(" ") });
  }
  cy.elements().remove();
  cy.add(els);
  cy.layout({ name: "preset", fit: true, padding: 40 }).run();
  applyFilters();
}

function applyFilters() {
  const cy = state.cy;
  if (!cy || state.mode !== "blast") return;
  const passing = $("#show-passing").checked, unknown = $("#show-unknown").checked, all = $("#show-all-links").checked;
  cy.batch(() => {
    cy.nodes().not(".ring").forEach((n) => {
      const d = n.data();
      const keep = d.hops === 0 || d.retracted || d.disputes || d.tier === "builds_on" || d.shown_for === "chain"
        || (d.tier === "passing" && passing) || (d.tier === "not_shown" && unknown);
      n.toggleClass("hidden", !keep);
    });
    cy.edges(".offchain").toggleClass("hidden", !all);
  });
}

function pathToRoot(id) {
  // follow each paper's next hop to the root (the chain the drawing is built from)
  const cy = state.cy;
  let eles = cy.collection(), cur = cy.getElementById(id), guard = 0;
  while (cur.nonempty() && guard++ < 6) {
    eles = eles.union(cur);
    const next = cur.data("next");
    if (!next) break;
    eles = eles.union(cy.getElementById(`${cur.id()}>${next}`));
    cur = cy.getElementById(next);
  }
  return eles;
}

function highlight(eles, zoom = true) {
  const cy = state.cy;
  cy.batch(() => {
    cy.elements().not(".ring").removeClass("hl").addClass("faded");
    eles.removeClass("faded").addClass("hl");
  });
  if (zoom) fitClear(eles);
}

// Zoom to eles inside the part of the canvas that the legend and the controls don't cover.
function fitClear(eles) {
  const cy = state.cy, bb = eles.boundingBox();
  const rect = (el) => (el && el.offsetParent && !el.hidden ? el.getBoundingClientRect() : null);
  const legend = rect($("#legend")), controls = rect($("#controls"));
  const pad = { l: 40 + (legend ? legend.width + 12 : 0), r: 40, t: 30 + (controls ? controls.height + 12 : 0), b: 40 };
  const W = cy.width() - pad.l - pad.r, H = cy.height() - pad.t - pad.b;
  const zoom = Math.max(cy.minZoom(), Math.min(1.6, W / Math.max(bb.w, 1), H / Math.max(bb.h, 1)));
  const pan = { x: pad.l + W / 2 - zoom * (bb.x1 + bb.w / 2), y: pad.t + H / 2 - zoom * (bb.y1 + bb.h / 2) };
  cy.stop();
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) cy.viewport({ zoom, pan });
  else cy.animate({ zoom, pan }, { duration: 350, easing: "ease-in-out-cubic" });
}

function clearFocus() {
  const cy = state.cy;
  if (cy) cy.batch(() => cy.elements().removeClass("faded hl"));
  state.focus = null;
  document.querySelectorAll(".tier[aria-pressed]").forEach((b) => b.setAttribute("aria-pressed", "false"));
  const d = $("#detail");
  if (d) d.replaceChildren(emptyDetail());
}

// ---------- blast radius ----------
async function loadBlast(text) {
  const req = ++state.req;
  busy("Building the view…");
  try {
    const v = await api(`/api/blast?paper=${encodeURIComponent(text)}`);
    if (req !== state.req) return;
    state.view = v;
    $("#q").value = v.root.doi || v.root.id;
    setURL({ mode: "blast", paper: v.root.id });
    drawBlast(v);
    renderBlastPanel(v);
  } catch (e) {
    if (req === state.req) panel(errorBox(e.message), blastHelp());
  } finally {
    if (req === state.req) busy(false);
  }
}

function emptyDetail() {
  return h("div", { class: "empty" }, "Click a paper or a link in the graph to see its chain and the citation sentences behind it.");
}

function errorBox(msg) { return h("div", { class: "error", role: "alert" }, msg); }

function rootHeader(r) {
  const ret = r.retractions && r.retractions.length ? r.retractions[r.retractions.length - 1] : null;
  return [
    r.retracted ? h("div", { class: "stamp" }, "Retracted", ret ? h("small", {}, ret.date) : null) : h("div", { class: "stamp plain" }, "Not marked retracted"),
    h("h2", {}, clean(r.title)),
    h("div", { class: "meta" }, `${r.year || ""}${r.venue ? " · " + r.venue : ""} · cited by ${num(r.cited_by)} (OpenAlex) · `,
      r.doi ? h("a", { href: `https://doi.org/${r.doi}`, target: "_blank", rel: "noopener" }, "DOI") : null,
      r.doi ? " · " : "", h("a", { href: oa(r.id), target: "_blank", rel: "noopener" }, r.id)),
    ret && ret.reason ? h("p", { class: "reason" }, "Retraction Watch reasons: " + ret.reason.split(";").map((s) => s.trim()).filter(Boolean).join(", ")) : null,
  ];
}

function tierButton(key, cls, count, label, small) {
  return h("button", { class: `tier ${cls}`, type: "button", "aria-pressed": "false", onclick: (e) => focusTier(key, e.currentTarget) },
    h("b", {}, num(count)), h("span", {}, label), h("small", {}, small));
}

function renderBlastPanel(v) {
  const t = v.tiers, c = v.coverage;
  const builds = v.nodes.filter((n) => n.tier === "builds_on").sort((a, b) => a.hops - b.hops || (b.cited_by || 0) - (a.cited_by || 0));
  const retractedBuilders = builds.filter((n) => n.retracted).length;
  const through = builds.filter((n) => n.hops > 1).length;
  panel(
    rootHeader(v.root),
    h("h3", {}, "Answer"),
    h("div", { class: "tiers" },
      tierButton("builds_on", "builds", t.builds_on, "build on it",
        `every link of a chain judged FOUNDATIONAL or SUPPORTING${retractedBuilders ? ` · ${retractedBuilders} retracted themselves` : ""}`),
      tierButton("passing", "passing", t.passing, "cite it in passing", "every chain with evidence has a BACKGROUND link"),
      tierButton("disputes", "disputes", t.disputes, "dispute it", "the citing sentence disputes or fails to replicate it"),
      tierButton("not_shown", "unknown", t.not_shown, "not shown either way", "no chain has a sentence on every link: neither cleared nor counted")),
    h("p", { class: "note" },
      `${through} of the ${t.builds_on} build on it through another paper: two models agreed they rely on the part of that paper that came from the retracted one. `,
      `OpenAlex lists ${num(c.citers_listed)} citing works; ${num(c.loaded)} are loaded, ${num(c.with_sentences)} have citation sentences (Semantic Scholar) and ${num(c.judged)} of those are judged. `,
      `Chains up to ${v.max_hops} citations long. The graph shows every paper that builds on it, every retracted paper downstream and every dispute, plus the most-cited of the rest.`),
    h("h3", {}, "Selected"),
    h("div", { id: "detail" }, emptyDetail()),
    h("h3", {}, `Papers that build on it (${builds.length})`),
    h("ul", { class: "list" }, builds.map((n) => h("li", {},
      h("button", { type: "button", onclick: () => onNode(state.cy.getElementById(n.id)) },
        h("span", { class: "t" }, n.retracted ? h("span", { class: "badge retracted" }, "retracted") : null, n.retracted ? " " : "", short(n.title, 90)),
        h("span", { class: "n" }, `${n.hops} link${n.hops > 1 ? "s" : ""} · ${n.year || ""}`))))),
    agentLog(v.investigations),
    howToRead(),
  );
}

function agentLog(invs) {
  if (!invs || !invs.length) return null;
  return [
    h("h3", {}, "What the agent explored"),
    h("p", { class: "note" }, "Each round the agent picked which papers to expand (load their citers, read and judge how they cite them), within a credit budget. Its reasons, verbatim:"),
    invs.map((inv) => h("div", { class: "card" },
      h("div", { class: "by" }, `investigation ${inv.id} · ${String(inv.created_at || "").slice(0, 16).replace("T", " ")} UTC`),
      inv.rounds.map((r) => h("div", { style: null },
        h("p", { class: "reason" }, h("b", {}, `Round ${r.round}: `),
          h("button", { class: "linkbtn", type: "button", onclick: () => onNode(state.cy.getElementById(r.paper)) }, short(r.title, 70)),
          ` · ${num(r.citers)} citers, ${num(r.judged)} judged, ${r.credits} credits`),
        h("blockquote", {}, r.reason || "(no reason given)"),
        h("div", { class: "by" }, `decided by ${String(r.decided_by || "").split(":").pop()}`))))),
  ];
}

function howToRead() {
  const cfg = state.config;
  return h("details", {},
    h("summary", {}, "How to read this"),
    h("p", { class: "note" }, "Each citation is judged from the sentences in which the citing paper cites the cited one. A model answers yes/no questions about those sentences; fixed rules turn the answers into a class, and this table turns the class into a weight. The risk of a chain is the product of its weights, computed in Cypher."),
    h("table", { class: "weights" }, Object.entries(cfg.weights).map(([k, w]) =>
      h("tr", {}, h("td", {}, h("span", { class: `badge cls ${k}` }, k)), h("td", {}, cfg.meaning[k]), h("td", {}, w)))),
    h("p", { class: "note" }, `A paper builds on the retracted one if its proven risk is at least ${cfg.builds_on}: with these weights, that means every link of some chain is FOUNDATIONAL or SUPPORTING (0.6³ = 0.216; any BACKGROUND link caps a chain at 0.15). Beyond the first link, a link counts only if two models agreed it relies on the part of the cited paper that came from the retracted work.`),
    h("p", { class: "note" }, "Shaky Ground scores papers, never people. Data: Retraction Watch (via Crossref), OpenAlex, Semantic Scholar."));
}

function blastHelp() {
  return [h("h3", {}, "Try"), h("p", { class: "note" }, state.config.allow_runs
    ? `${state.config.demo_root} (Lesné et al. 2006, Nature, retracted 2024), or any paper that is loaded in the graph.`
    : `${state.config.demo_root} (Lesné et al. 2006, Nature, retracted 2024). This server is read only, so it traces that one paper; click any paper in its graph to see its chain and the sentences behind it.`)];
}

function focusTier(key, btn) {
  const cy = state.cy;
  const on = btn.getAttribute("aria-pressed") !== "true";
  document.querySelectorAll(".tier").forEach((b) => b.setAttribute("aria-pressed", "false"));
  if (!on) return clearFocus();
  btn.setAttribute("aria-pressed", "true");
  if (key === "passing") $("#show-passing").checked = true;
  if (key === "not_shown") $("#show-unknown").checked = true;
  applyFilters();
  let eles = cy.collection();
  cy.nodes().forEach((n) => {
    const d = n.data();
    const match = key === "disputes" ? d.disputes : d.tier === key && !(key === "not_shown" && d.disputes);
    if (match) eles = eles.union(key === "disputes" ? n.union(cy.getElementById(`${n.id()}>${state.view.root.id}`)).union(cy.getElementById(state.view.root.id)) : pathToRoot(n.id()));
  });
  highlight(eles, false);
}

// ---------- details: a paper ----------
async function onNode(node) {
  if (!node || node.empty()) return;
  if (node.data("open_check")) return openCheck(node.data("open_check"));      // a reference in the list view
  const id = node.id();
  const root = state.mode === "blast" ? state.view.root.id : node.data("ancestor") || null;
  state.cy.elements().unselect();
  node.select();
  highlight(state.mode === "blast" ? pathToRoot(id) : node.closedNeighborhood());
  const d = $("#detail");
  if (!d) return;
  if (!root) {   // foundation-check graph: the paper being checked
    d.replaceChildren(h("div", { class: "card" }, h("b", {}, clean(node.data("title"))), h("div", { class: "meta" }, node.data("year") || "", " · ", h("a", { href: oa(id), target: "_blank", rel: "noopener" }, id))));
    return;
  }
  d.replaceChildren(h("div", { class: "empty" }, "Loading…"));
  try {
    const p = await api(`/api/paper?id=${encodeURIComponent(id)}&root=${encodeURIComponent(root)}`);
    d.replaceChildren(paperCard(p, node.data("disputes"), state.mode === "blast"));
  } catch (e) {
    d.replaceChildren(errorBox(e.message));
  }
  reveal(d);
}

function reveal(el) {   // bring the details into view, e.g. after a click in the list further down the panel
  const smooth = !matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.previousElementSibling.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
}

const TIER_TEXT = {
  root: "The retracted paper.",
  builds_on: "Builds on the retracted paper: every link of its strongest chain was judged FOUNDATIONAL or SUPPORTING.",
  passing: "Cites it only in passing: every chain with evidence has a BACKGROUND link.",
  not_shown: "Not shown either way: no chain has a citation sentence on every link. Neither cleared nor counted.",
};

function paperCard(p, disputes, blast) {
  const proven = p.chains.risk_proven, possible = p.chains.risk;
  const showChain = proven || possible;
  return h("div", { class: "card" },
    p.retracted ? h("span", { class: "badge retracted" }, "retracted") : null,
    h("div", {}, h("b", {}, clean(p.title))),
    h("div", { class: "meta" }, `${p.year || ""}${p.venue ? " · " + p.venue : ""}${p.is_review ? " · review" : ""} · cited by ${num(p.cited_by)} · `,
      h("a", { href: oa(p.id), target: "_blank", rel: "noopener" }, p.id),
      p.doi ? [" · ", h("a", { href: `https://doi.org/${p.doi}`, target: "_blank", rel: "noopener" }, "DOI")] : null),
    // tiers and stored risk belong to the blast-radius root, so they are shown only in that mode
    blast ? h("p", { class: "reason" }, disputes ? "Disputes the retracted paper (a CRITICAL citation). " : "", TIER_TEXT[p.tier] || "No chain to this root.") : null,
    blast && p.tier !== "root" ? h("p", { class: "by" }, `proven risk ${fix(p.risk_proven)} · possible risk ${fix(p.risk)}${p.expanded_at ? " · citers loaded by the agent" : ""}`) : null,
    showChain ? [
      h("div", { class: "by", style: null }, proven ? `Strongest proven chain (risk ${fix(proven.risk)})` : `Strongest possible chain (risk ${fix(possible.risk)}; links without a sentence count 0.3)`),
      chainList(proven || possible),
    ] : null);
}

function chainList(ch) {
  const items = [];
  ch.papers.forEach((pp, i) => {
    items.push(h("li", { class: `paper${pp.retracted ? " retracted" : ""}` },
      h("span", { class: "paper-title", role: "button", tabindex: "0", onclick: () => onNode(state.cy.getElementById(pp.id)),
                  onkeydown: (e) => { if (e.key === "Enter") onNode(state.cy.getElementById(pp.id)); } },
        short(pp.title, 110)), pp.year ? ` (${pp.year})` : "", pp.retracted ? " · retracted" : ""));
    if (i < ch.links.length) {
      const l = ch.links[i], next = ch.papers[i + 1];
      items.push(h("li", { class: "link" },
        h("span", { class: `badge cls ${l.cls}` }, l.cls), ` weight ${l.reliance}`,
        quote(l.evidence),
        h("div", { class: "by" }, `judged by ${model(l.judged_by)} · `,
          h("button", { class: "linkbtn", type: "button", onclick: () => onEdgeIds(pp.id, next.id) }, "all sentences")),
        i < ch.links.length - 1 ? carryBox(l) : null));
    }
  });
  return h("ol", { class: "chain" }, items);
}

function quote(text) {
  return text ? h("blockquote", {}, h("mark", {}, `“${clean(text)}”`))
              : h("blockquote", { class: "missing" }, "No citation sentence available (Semantic Scholar has none for this link).");
}

function carryBox(l) {
  const votes = l.carry_votes || [];
  if (l.carries == null && !votes.length) {
    return h("div", { class: "carry split" }, "Carry check: not run for this link, so it counts at most as a passing mention.");
  }
  const kind = l.carries === true ? "yes" : l.carries === false ? "no" : "split";
  const verdict = kind === "yes" ? "relies on the part that came from the retracted paper" : kind === "no" ? "relies on a different part: does not carry it on" : "the models disagree: not proven";
  return h("div", { class: `carry ${kind}` }, h("b", {}, "Carry check: "), verdict, ` (${votes.join(", ")})`, carryWhy(l.carry_why));
}

// carry_why reads "cited for: …; B took: …": what the citing paper uses the middle one for, and what
// the middle one took from the retracted paper (the models' summaries, stored with the votes)
function carryWhy(why) {
  if (!why) return null;
  const m = /^cited for:\s*(.*?);\s*B took:\s*(.*)$/s.exec(why);
  if (!m) return h("p", { class: "reason" }, why);
  return h("dl", { class: "took" }, h("dt", {}, "Used here for"), h("dd", {}, m[1]),
    h("dt", {}, "What that paper took from the retracted one"), h("dd", {}, m[2]));
}

// ---------- details: a link ----------
function onEdge(edge) {
  if (edge.data("tier")) return openCheck(edge.source().data("open_check"));     // a reference-list summary link
  state.cy.elements().unselect();
  edge.select();
  highlight(edge.union(edge.connectedNodes()));
  onEdgeIds(edge.data("source"), edge.data("target"));
}

async function onEdgeIds(a, b) {
  const d = $("#detail");
  if (!d) return;
  d.replaceChildren(h("div", { class: "empty" }, "Loading…"));
  try {
    const e = await api(`/api/edge?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    d.replaceChildren(edgeCard(e));
    reveal(d);
  } catch (err) {
    d.replaceChildren(errorBox(err.message));
  }
}

function edgeCard(e) {
  const cfg = state.config;
  const contexts = e.contexts || [];
  const carryRoot = e.carry_root;
  return h("div", { class: "card" },
    h("div", { class: "meta" }, "How"),
    h("div", {}, h("b", {}, short(e.source.title, 120)), ` (${e.source.year || ""})`),
    h("div", { class: "meta" }, "cites"),
    h("div", {}, e.target.retracted ? h("span", { class: "badge retracted" }, "retracted") : null, e.target.retracted ? " " : "",
      h("b", {}, short(e.target.title, 120)), ` (${e.target.year || ""})`),
    h("p", {}, h("span", { class: `badge cls ${e.cls}` }, e.cls), ` weight ${e.reliance} · ${cfg.meaning[e.cls] || ""}`),
    e.evidence ? [h("div", { class: "by" }, "The deciding sentence:"), quote(e.evidence)] : quote(null),
    e.judged_by ? h("div", { class: "by" }, `judged by ${model(e.judged_by)}${e.judged_at ? " · " + e.judged_at.slice(0, 10) : ""}`) : null,
    e.why ? h("p", { class: "reason" }, `The model's note: ${e.why}`) : null,
    carryRoot ? h("div", { class: `carry ${e.carries === true ? "yes" : e.carries === false ? "no" : "split"}` },
      h("b", {}, "Carry check: "),
      e.carries === true ? "both models say the citing paper relies on the part that came from the retracted paper"
        : e.carries === false ? "both models say it relies on a different part: the dependence stops here"
        : "the models disagree: not proven", ` (${(e.carry_votes || []).join(", ")})`,
      carryWhy(e.carry_why)) : null,
    contexts.length > 1 ? h("details", {}, h("summary", {}, `All ${contexts.length} sentences`), contexts.map((c) => quote(c))) : null,
    h("div", { class: "by" }, `source: ${e.evidence_src || "none"}${(e.intents || []).length ? " · S2 intents: " + e.intents.join(", ") : ""}${e.influential ? " · S2 influential" : ""}`));
}

// ---------- foundation check ----------
const VERDICT_CLASS = (v) => v.startsWith("RESTS") ? "rests" : v === "NOT PROVEN" ? "notproven" : v.startsWith("CITES") ? "cites"
  : v.startsWith("NO RETRACTED") ? "none" : v.startsWith("CLEAN") ? "clean" : v === "NOT CHECKED" ? "notchecked" : "nodata";

function placard(verdict, kicker, text) {
  return h("div", { class: `verdict ${VERDICT_CLASS(verdict)}` },
    h("div", { class: "kicker" }, h("span", {}, kicker[0]), h("span", {}, kicker[1])), h("b", {}, verdict), h("p", {}, text));
}

async function checkHome(selectedId) {
  const cfg = state.config;
  let list = [];
  try { list = await api("/api/checks"); } catch { /* shown as empty */ }
  const intro = [
    h("h2", {}, "Does a paper rest on retracted work?"),
    h("p", { class: "note" }, `Paste a DOI above. The check loads the paper's references and their references (depth ${cfg.depth}), finds retracted work among them, and judges every link on the way from the citing sentences. It says CLEAN only when every reference to depth ${cfg.depth} was checked.`),
    cfg.allow_runs ? null : h("p", { class: "note" }, "This server is read only: it shows checks that were already run. Run new ones locally with python -m shaky.foundation <doi>."),
  ];
  const recent = [
    h("h3", {}, "Recent checks"),
    list.length ? h("ul", { class: "list tagged" }, list.map((c) => h("li", {},
      h("button", { type: "button", onclick: () => openCheck(c.investigation) },
        h("span", { class: `v ${VERDICT_CLASS(c.verdict)}` }, c.verdict),
        h("span", { class: "n" }, String(c.created_at || "").slice(0, 10)),
        h("span", { class: "t" }, short(c.title, 110)))))) : h("div", { class: "empty" }, "No checks yet."),
  ];
  return { intro, recent, list };
}

async function showCheckHome() {
  const { intro, recent } = await checkHome();
  if (state.mode !== "check") return;
  $("#legend").dataset.mode = "check";
  panel(intro, refBox(), h("div", { id: "detail" }), recent);
  if (state.cy) state.cy.elements().remove();
}

// ---------- reference list ----------
function refBox() {
  const ta = h("textarea", { id: "refs", spellcheck: "false", "aria-label": "Reference list or BibTeX",
    placeholder: "Paste a reference list or a BibTeX file. Every DOI in it is checked; an entry without a DOI is counted as not checked." });
  ta.value = state.refsDraft || "";
  ta.addEventListener("input", () => { state.refsDraft = ta.value; });
  const example = async () => {
    try { ta.value = state.refsDraft = await (await fetch("/example.bib")).text(); ta.focus(); } catch { /* the box stays as it was */ }
  };
  return [
    h("h3", {}, "Or check a whole reference list"),
    h("form", { class: "refbox", onsubmit: (e) => { e.preventDefault(); runList(ta.value); } },
      ta,
      h("div", { class: "row" },
        h("span", { class: "by" }, state.config.allow_runs ? "Stored checks are reused; the rest are checked now (minutes each). "
          : "Read only: references checked here before are shown, the rest are NOT CHECKED. ",
          h("button", { class: "linkbtn", type: "button", onclick: example }, "paste an example")),
        h("button", { class: "btn", type: "submit" }, "Check list"))),
  ];
}

async function runList(text) {
  if (!text.trim()) return;
  const req = ++state.req;
  busy(state.config.allow_runs ? "Checking the list: stored checks first, then each new reference… (minutes)" : "Reading the stored checks…");
  try {
    const r = await api("/api/list", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    if (req !== state.req) return;
    showList(r);
  } catch (e) {
    if (req === state.req) { const { intro, recent } = await checkHome(); panel(errorBox(e.message), intro, refBox(), recent); }
  } finally {
    if (req === state.req) busy(false);
  }
}

function showList(r) {
  state.list = r;
  setURL({ mode: "check" });
  drawList(r);
  $("#legend").dataset.mode = "list";
  const now = r.entries.filter((e) => e.investigation && !e.reused).length, ret = r.cited_papers_retracted;
  panel(
    placard(r.verdict, [`Reference list · depth ${state.config.depth}`, `${r.references} references`],
      `The worst verdict among its references. ${state.config.verdicts[r.verdict] || ""}`),
    h("p", { class: "note" }, `${r.with_doi} with a DOI and ${r.without_doi} without (a reference without a DOI can't be looked up, so it counts as not checked). `,
      ret ? `${ret} of the cited papers ${ret > 1 ? "are retracted themselves" : "is retracted itself"}. ` : "",
      `${r.reused} from earlier checks on this server, ${now} checked now · ${r.credits} OpenAlex credits.`),
    h("h3", {}, "Selected"),
    h("div", { id: "detail" }, h("div", { class: "empty" }, "Click a reference to open its check, or a retracted paper to see what it is.")),
    h("h3", {}, `References with a DOI (${r.entries.length})`),
    h("ul", { class: "list tagged entries" }, r.entries.map((e) => h("li", {},
      h("button", { type: "button", disabled: !e.investigation, onclick: () => openCheck(e.investigation) },
        h("span", { class: `v ${VERDICT_CLASS(e.verdict)}` }, e.verdict),
        h("span", { class: "n" }, e.reused ? "earlier check" : e.investigation ? "checked now" : ""),
        h("span", { class: "t" }, e.paper_retracted ? [h("span", { class: "badge retracted" }, "retracted itself"), " "] : null, e.title ? short(e.title, 110) : e.doi)),
      e.why ? h("div", { class: "why" }, e.why) : null))),
    h("button", { class: "btn ghost", type: "button", onclick: () => { setURL({ mode: "check" }); showCheckHome(); } }, "← all checks"),
  );
}

// the list on the left, its references in the middle, the retracted papers they lead to on the right
function drawList(r) {
  const cy = ensureCy();
  const els = [], refs = new Set();
  const col = (list, x, i) => ({ x, y: (i - (list.length - 1) / 2) * 86 });
  els.push({ group: "nodes", data: { id: "list", title: "Your reference list", short: `Your reference list · ${r.references} references`, size: 46, hops: 0 },
             position: { x: 0, y: 0 }, classes: "subject listnode hl" });
  r.entries.forEach((e, i) => {
    const id = e.id || `doi:${e.doi}`;
    refs.add(id);
    const cls = e.paper_retracted ? "retracted" : e.verdict.startsWith("RESTS") ? "builds_on" : e.verdict === "NOT CHECKED" ? "not_shown" : "passing";
    els.push({ group: "nodes", data: { id, title: e.title || e.doi, year: e.year, short: short(e.title || e.doi, 46), size: 26, open_check: e.investigation },
               position: col(r.entries, 360, i), classes: `${cls} hl` });
    els.push({ group: "edges", data: { id: `list>${id}`, source: "list", target: id, reliance: 0.3 }, classes: "listlink hl" });
  });
  const ancestors = new Map();
  r.entries.forEach((e) => (e.ancestors || []).forEach((a) => { if (!refs.has(a.id)) ancestors.set(a.id, a); }));
  [...ancestors.values()].forEach((a, i, all) => els.push({ group: "nodes",
    data: { id: a.id, title: a.title, year: a.year, short: short(a.title, 46), size: 26 }, position: col(all, 760, i), classes: "retracted hl" }));
  r.entries.forEach((e) => (e.ancestors || []).forEach((a) => {
    const id = e.id || `doi:${e.doi}`;
    if (id === a.id) return;
    els.push({ group: "edges", data: { id: `${id}>${a.id}`, source: id, target: a.id, tier: a.tier, reliance: 1 },
               classes: `sum ${VERDICT_CLASS(a.tier)} hl` });
  }));
  cy.elements().remove();
  cy.add(els);
  cy.layout({ name: "preset", fit: true, padding: 60 }).run();
}

async function openCheck(inv) {
  const req = ++state.req;
  busy("Opening the check…");
  try {
    const r = await api(`/api/check?id=${encodeURIComponent(inv)}`);
    if (req !== state.req) return;
    showCheck(r);
  } catch (e) {
    if (req === state.req) panel(errorBox(e.message));
  } finally {
    if (req === state.req) busy(false);
  }
}

async function runCheck(text) {
  const req = ++state.req;
  busy("Checking references, reading citation sentences, judging links… (up to a few minutes)");
  try {
    const r = await api("/api/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paper: text }) });
    if (req !== state.req) return;
    showCheck(r);
  } catch (e) {
    if (req === state.req) { const { intro, recent } = await checkHome(); panel(errorBox(e.message), intro, recent); }
  } finally {
    if (req === state.req) busy(false);
  }
}

function showCheck(r) {
  state.check = r;
  $("#legend").dataset.mode = "check";
  setURL({ mode: "check", id: r.investigation });
  $("#q").value = r.paper.doi || r.paper.id;
  drawCheck(r);
  const cov = r.coverage;
  panel(
    placard(r.verdict, [`Foundation check · depth ${state.config.depth}`, r.investigation], state.config.verdicts[r.verdict] || ""),
    h("h2", {}, clean(r.paper.title)),
    h("div", { class: "meta" }, `${r.paper.year || ""} · `, h("a", { href: oa(r.paper.id), target: "_blank", rel: "noopener" }, r.paper.id),
      r.paper.doi ? [" · ", h("a", { href: `https://doi.org/${r.paper.doi}`, target: "_blank", rel: "noopener" }, "DOI")] : null,
      r.paper_retracted ? [" · ", h("span", { class: "badge retracted" }, "itself retracted")] : null),
    r.details_missing ? h("p", { class: "note" }, r.details_missing) : null,
    r.why ? h("p", { class: "note" }, r.why) : null,
    cov ? h("p", { class: "note" }, `Checked ${num(cov.references_found)} of ${num(cov.references)} references and ${num(cov.second_hop_found)} of ${num(cov.second_hop)} second-hop references`,
      cov.second_hop_skipped_for_budget ? ` (${num(cov.second_hop_skipped_for_budget)} skipped for the credit budget)` : "",
      `; ${num(cov.without_doi)} have no DOI, so their retraction flag is OpenAlex's alone. ${r.credits} OpenAlex credits.`) : null,
    h("h3", {}, "Selected"),
    h("div", { id: "detail" }, emptyDetail()),
    h("h3", {}, `Retracted work in reach (${(r.retracted_ancestors || []).length})`),
    (r.retracted_ancestors || []).map((a) => h("div", { class: "card" },
      h("span", { class: `badge cls` }, a.tier),
      h("div", {}, h("span", { class: "badge retracted" }, "retracted"), " ", h("b", {}, short(a.title, 120)), ` (${a.year || ""})`),
      a.direct_link ? h("div", { class: "by" }, `cited directly as ${a.direct_link}`) : h("div", { class: "by" }, "reached through another paper"),
      a.chain ? chainList(a.chain) : h("p", { class: "note" }, "Only critical links lead there."))),
    h("p", { class: "note" }, `check ${r.investigation}`),
    h("button", { class: "btn ghost", type: "button", onclick: () => { setURL({ mode: "check" }); showCheckHome(); } }, "← all checks"),
  );
}

function drawCheck(r) {
  const cy = ensureCy();
  const nodes = new Map(), edges = new Map();
  const add = (p, extra) => { if (!nodes.has(p.id)) nodes.set(p.id, { ...p, ...extra }); };
  add({ id: r.paper.id, title: r.paper.title, year: r.paper.year, retracted: r.paper_retracted }, { col: 0 });
  (r.retracted_ancestors || []).forEach((a) => {
    const ch = a.chain;
    if (!ch) { add({ id: a.id, title: a.title, year: a.year, retracted: true }, { ancestor: a.id }); return; }
    ch.papers.forEach((p, i) => add(p, { ancestor: a.id }));
    ch.links.forEach((l, i) => {
      const s = ch.papers[i].id, t = ch.papers[i + 1].id;
      edges.set(`${s}>${t}`, { id: `${s}>${t}`, source: s, target: t, cls: l.cls, reliance: l.reliance, carries: l.carries });
    });
  });
  // columns: the checked paper left, retracted work right, papers in between in the middle
  const rows = { 0: [], 1: [], 2: [] };
  for (const n of nodes.values()) {
    const col = n.id === r.paper.id ? 0 : n.retracted ? 2 : 1;
    n.col = col;
    rows[col].push(n);
  }
  const els = [];
  for (const [col, list] of Object.entries(rows)) {
    list.forEach((n, i) => els.push({ group: "nodes", position: { x: col * 320, y: (i - (list.length - 1) / 2) * 90 },
      data: { ...n, size: n.col === 0 ? 46 : 26, short: short(n.title, 50), hops: n.col === 0 ? 0 : null },
      classes: [n.retracted ? "retracted" : "", n.col === 0 ? "subject" : "passing"].filter(Boolean).join(" ") }));
  }
  for (const e of edges.values()) els.push({ group: "edges", data: e, classes: [e.cls, e.carries === false ? "nocarry" : "", "onchain"].join(" ") });
  cy.elements().remove();
  cy.add(els);
  cy.nodes().addClass("hl");
  cy.layout({ name: "preset", fit: true, padding: 60 }).run();
  if (cy.nodes().length === 1) cy.zoom(1.2), cy.center();
}

// ---------- modes ----------
function setMode(mode) {
  state.mode = mode;
  for (const b of document.querySelectorAll(".modes button")) b.setAttribute("aria-selected", String(b.dataset.mode === mode));
  $("#controls").hidden = mode !== "blast";
  $("#stage").dataset.mode = mode;
  $("#legend").dataset.mode = mode;
  $("#go").textContent = mode === "blast" ? "Trace" : "Check";
  $("#q").placeholder = mode === "blast" ? "Retracted paper: DOI, OpenAlex ID or PubMed ID" : "Paper to check: DOI, OpenAlex ID or PubMed ID";
  const go = $("#go");
  go.disabled = mode === "check" && !state.config.allow_runs;
  go.title = go.disabled ? "This server is read only" : "";
}

// the canvas draws labels only in fonts that are already loaded, so load them first (or give up after 1.5 s)
const fontsReady = Promise.all(["700 14px 'Barlow Condensed'", "600 15px 'Source Serif 4'"].map((f) => document.fonts.load(f)))
  .catch(() => null);

async function init() {
  try {
    state.config = await api("/api/config");
  } catch (e) {
    panel(errorBox(`Can't reach the server: ${e.message}`));
    return;
  }
  await Promise.race([fontsReady, new Promise((r) => setTimeout(r, 1500))]);
  fontsReady.then(() => { if (state.cy) state.cy.style(cyStyle()); });
  $("#search").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#q").value.trim();
    if (!text) return;
    if (state.mode === "blast") loadBlast(text);
    else if (state.config.allow_runs) runCheck(text);
  });
  for (const b of document.querySelectorAll(".modes button")) {
    b.addEventListener("click", () => {
      if (b.dataset.mode === state.mode) return;
      setMode(b.dataset.mode);
      if (state.mode === "blast") loadBlast(state.view ? state.view.root.id : state.config.demo_root);
      else { $("#q").value = ""; setURL({ mode: "check" }); showCheckHome(); }
    });
  }
  for (const id of ["#show-passing", "#show-unknown", "#show-all-links"]) $(id).addEventListener("change", applyFilters);
  $("#fit").addEventListener("click", () => { if (state.cy) { state.cy.stop(); state.cy.fit(state.cy.elements(":visible"), 40); } });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") clearFocus(); });

  const u = new URLSearchParams(location.search);
  setMode(u.get("mode") === "check" ? "check" : "blast");
  if (state.mode === "blast") loadBlast(u.get("paper") || state.config.demo_root);
  else if (u.get("id")) openCheck(u.get("id"));
  else showCheckHome();
}

init();
