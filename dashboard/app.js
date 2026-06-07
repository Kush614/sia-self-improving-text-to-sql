/* SIA demo dashboard — renders entirely from window.DEMO_DATA (offline, cached). */
(function () {
  "use strict";
  const D = window.DEMO_DATA;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
  const pct = (x) => (x == null ? "—" : (x * 100).toFixed(1) + "%");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  if (!D) { document.body.innerHTML = '<div class="wrap"><div class="card">No cached data. Run <span class="kbd">python build_cache.py</span> first.</div></div>'; return; }
  const scored = D.gens.filter((g) => g.accuracy != null);
  const state = { gen: null, ba: 0, animated: false, playing: false };

  /* ── theme ─────────────────────────────────────────────────────────── */
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("sia-theme", t); } catch (e) {}
    const b = $("#themeBtn"); if (b) b.textContent = t === "dark" ? "☀ Light" : "☾ Dark";
    if (state.gen != null) drawChart(false);
  }
  function initTheme() {
    let t; try { t = localStorage.getItem("sia-theme"); } catch (e) {}
    if (!t) t = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    applyTheme(t);
  }

  /* ── 3D tilt ───────────────────────────────────────────────────────── */
  function attachTilt() {
    $$(".tilt").forEach((card) => {
      card.addEventListener("mousemove", (e) => {
        if (state.playing) return;
        const r = card.getBoundingClientRect();
        const px = (e.clientX - r.left) / r.width - 0.5;
        const py = (e.clientY - r.top) / r.height - 0.5;
        card.style.transform = `perspective(1000px) rotateX(${(-py * 5).toFixed(2)}deg) rotateY(${(px * 5).toFixed(2)}deg) translateZ(8px)`;
      });
      card.addEventListener("mouseleave", () => { card.style.transform = ""; });
    });
  }

  /* ── count-up ──────────────────────────────────────────────────────── */
  function countUp(node, from, to, ms) {
    const t0 = performance.now();
    function step(t) {
      const k = Math.min(1, (t - t0) / ms);
      const e = 1 - Math.pow(1 - k, 3);
      node.textContent = (from + (to - from) * e).toFixed(1) + "%";
      if (k < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  /* ── hero ──────────────────────────────────────────────────────────── */
  function renderHero() {
    const s = D.summary;
    $("#heroBig").innerHTML =
      `<span class="from">${pct(s.first_acc)}</span> <span class="arrow">→</span> <span id="bigTo">${pct(s.first_acc)}</span>`;
    $("#heroBadge").innerHTML = `▲ +${s.delta_pts} pts &nbsp;·&nbsp; gen ${s.first_gen}→${s.best_gen}`;
    $("#chips").innerHTML = [
      `<span class="chip"><b>${D.meta.scored_set}</b> scored questions</span>`,
      `<span class="chip"><b>${D.meta.databases}</b> databases</span>`,
      `<span class="chip">task model <b>fixed</b> every gen</span>`,
      `<span class="chip"><b>harness-only</b> self-edits</span>`,
    ].join("");
    $("#heroStats").innerHTML = [
      ["Generations", D.meta.generations],
      ["Cold start (gen " + s.first_gen + ")", pct(s.first_acc)],
      ["Best (gen " + s.best_gen + ")", pct(s.best_acc)],
      ["Total climb", "+" + s.delta_pts + " pts"],
    ].map(([k, v]) => `<div class="stat"><span class="k">${k}</span><span class="v">${v}</span></div>`).join("");
    countUp($("#bigTo"), s.first_acc * 100, s.best_acc * 100, 1400);
  }

  /* ── chart ─────────────────────────────────────────────────────────── */
  function drawChart(animate) {
    const W = 1000, H = 320, padL = 64, padR = 24, padT = 26, padB = 44;
    const accs = scored.map((g) => g.accuracy);
    const lo = Math.max(0, Math.floor((Math.min(...accs) - 0.07) * 20) / 20);
    const hi = Math.min(1, Math.ceil((Math.max(...accs) + 0.04) * 20) / 20);
    const x = (i) => padL + (scored.length === 1 ? 0.5 : i / (scored.length - 1)) * (W - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
    const pts = scored.map((g, i) => [x(i), y(g.accuracy)]);
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    const area = line + ` L ${x(scored.length - 1).toFixed(1)} ${y(lo).toFixed(1)} L ${x(0).toFixed(1)} ${y(lo).toFixed(1)} Z`;

    let grid = "", ylab = "";
    for (let s = 0; s <= 4; s++) {
      const v = lo + (hi - lo) * (s / 4), yy = y(v).toFixed(1);
      grid += `<line class="grid" x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}"/>`;
      ylab += `<text class="lbl" x="${padL - 10}" y="${(+yy + 4).toFixed(1)}" text-anchor="end">${(v * 100).toFixed(0)}%</text>`;
    }
    let xlab = "", dots = "", vlbl = "";
    scored.forEach((g, i) => {
      const cl = (animate ? " pop" : "") + (g.gen === state.gen ? " active" : "");
      xlab += `<text class="lbl" x="${x(i)}" y="${H - padB + 22}" text-anchor="middle">gen ${g.gen}</text>`;
      vlbl += `<text class="vlbl${animate ? " fade" : ""}" x="${x(i)}" y="${y(g.accuracy) - 16}" text-anchor="middle" style="animation-delay:${i * 0.12}s">${(g.accuracy * 100).toFixed(1)}%</text>`;
      dots += `<circle class="pt${cl}" data-gen="${g.gen}" cx="${x(i)}" cy="${y(g.accuracy)}" r="7" style="animation-delay:${i * 0.12}s"/>`;
    });

    $("#chart").innerHTML =
      `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
        ${grid}
        <line class="axis" x1="${padL}" y1="${padT}" x2="${padL}" y2="${H - padB}"/>
        <line class="axis" x1="${padL}" y1="${H - padB}" x2="${W - padR}" y2="${H - padB}"/>
        ${ylab}${xlab}
        <path class="area" d="${area}"/>
        <path class="line${animate ? " draw" : ""}" d="${line}"/>
        ${vlbl}${dots}
      </svg>`;

    if (animate) {
      const ln = $("#chart .line");
      try { ln.style.setProperty("--len", ln.getTotalLength()); } catch (e) {}
    }
    const tip = $("#tip");
    $$("#chart .pt").forEach((c) => {
      c.addEventListener("click", () => selectGen(+c.dataset.gen));
      c.addEventListener("mousemove", (e) => {
        const g = scored.find((z) => z.gen === +c.dataset.gen);
        const r = $("#chart").getBoundingClientRect();
        tip.style.left = (e.clientX - r.left) + "px"; tip.style.top = (e.clientY - r.top) + "px";
        tip.style.opacity = 1; tip.textContent = `gen ${g.gen}: ${pct(g.accuracy)} (${g.n_correct}/${g.n_total})`;
      });
      c.addEventListener("mouseleave", () => { tip.style.opacity = 0; });
    });
  }

  /* ── generation explorer ───────────────────────────────────────────── */
  function renderGenTabs() {
    $("#genTabs").innerHTML =
      `<button id="playBtn" class="btn accent">▶ Play evolution</button>` +
      D.gens.map((g) => `<button class="btn gen-tab" data-gen="${g.gen}">gen ${g.gen}<br><small>${pct(g.accuracy)}</small></button>`).join("");
    $$("#genTabs .gen-tab").forEach((b) => b.addEventListener("click", () => selectGen(+b.dataset.gen)));
    $("#playBtn").addEventListener("click", playEvolution);
  }

  async function playEvolution() {
    if (state.playing) return;
    state.playing = true;
    const btn = $("#playBtn"); btn.textContent = "▶ Playing…"; btn.classList.add("sel");
    drawChart(true);
    for (const g of scored) { selectGen(g.gen); await sleep(1300); }
    btn.textContent = "▶ Play evolution"; btn.classList.remove("sel");
    state.playing = false;
  }

  function errorBar(es) {
    const pal = ["var(--bad)", "var(--accent-2)", "var(--accent-4)", "var(--accent-3)", "var(--muted)"];
    const en = Object.entries(es || {}); const total = en.reduce((a, [, v]) => a + v, 0);
    if (!total) return "";
    const segs = en.map(([k, v], i) => `<i title="${esc(k)}: ${v}" style="width:${(v / total * 100).toFixed(1)}%;background:${pal[i % pal.length]}"></i>`).join("");
    const leg = en.map(([k, v], i) => `<span class="chip"><b style="color:${pal[i % pal.length]}">■</b> ${esc(k)} · ${v}</span>`).join(" ");
    return `<div class="errbar">${segs}</div><div class="chips" style="margin-top:10px">${leg}</div>`;
  }

  function selectGen(n) {
    state.gen = n;
    const g = D.gens.find((z) => z.gen === n);
    const idx = scored.findIndex((z) => z.gen === n);
    const prev = idx > 0 ? scored[idx - 1] : null;
    const delta = prev ? (g.accuracy - prev.accuracy) * 100 : null;
    const dh = delta == null ? "" : `<span class="${delta >= 0 ? "delta-up" : "delta-down"}">(${delta >= 0 ? "+" : ""}${delta.toFixed(1)} pts)</span>`;
    $("#genMeta").innerHTML =
      `<div class="row"><span class="k">Accuracy</span><span class="v">${pct(g.accuracy)} ${dh}</span></div>
       <div class="row"><span class="k">Correct</span><span class="v">${g.n_correct}/${g.n_total}</span></div>
       <div class="row"><span class="k">Agent size</span><span class="v">${g.agent_lines} lines</span></div>
       <div class="row"><span class="k">Errors left</span><span class="v">${Object.values(g.error_summary || {}).reduce((a, b) => a + b, 0)}</span></div>
       ${errorBar(g.error_summary)}`;
    $("#genDetail").innerHTML = g.improvement_html
      ? `<div class="md reveal">${g.improvement_html}</div>`
      : `<div class="md reveal"><h3>Cold-start agent</h3><p>Generation 1 was written by the <b>meta-agent</b> from the task description + a minimal reference seed. From here, every change is the <b>feedback-agent's own self-edit</b>, driven by the failure samples in <code>results.json</code>.</p></div>`;
    $$("#genTabs .gen-tab").forEach((b) => b.classList.toggle("sel", +b.dataset.gen === n));
    drawChart(false);
  }

  /* ── before / after — "run query → it learned" ─────────────────────── */
  function resultTable(o) {
    if (o.error) return `<div class="err">✗ execution error: ${esc(o.error)}</div>`;
    if (!o.columns || !o.columns.length) return `<div class="rowcount">no columns</div>`;
    const head = "<tr>" + o.columns.map((c) => `<th>${esc(c)}</th>`).join("") + "</tr>";
    const body = o.rows.map((r) => "<tr>" + r.map((c) => `<td>${esc(c)}</td>`).join("") + "</tr>").join("");
    const more = o.rowcount > o.rows.length ? ` (showing ${o.rows.length})` : "";
    return `<div class="tbl-wrap"><table class="res">${head}${body}</table></div><div class="rowcount">${o.rowcount} row(s)${more}</div>`;
  }

  function renderBA() {
    if (!D.before_after.length) { $("#baSection").style.display = "none"; return; }
    $("#baPicker").innerHTML = D.before_after.map((b, i) => `<option value="${i}">[${esc(b.db_id)}] ${esc(b.question.slice(0, 90))}</option>`).join("");
    $("#baPicker").addEventListener("change", (e) => selectBA(+e.target.value));
    selectBA(0);
  }

  function selectBA(i) {
    state.ba = i;
    const b = D.before_after[i];
    $("#baHead").innerHTML =
      `<div class="q">${esc(b.question)}</div><div class="q-db">database: <b>${esc(b.db_id)}.sqlite</b> · id ${esc(b.id)}</div>
       <div class="run-row"><button id="runBtn" class="btn runbtn">▶ Run both queries on the database</button>
       <span id="runState" class="runstate"></span></div>`;
    const shell = (cls, title, key) =>
      `<div class="ba-card ${cls}"><div class="head"><span>${title}</span><span id="verdict-${key}"></span></div>
        <div class="body"><div class="sql">${esc(D.before_after[i][key].sql) || "(none)"}</div>
        <div id="out-${key}" class="out"><div class="rowcount">press <b>Run</b> to execute</div></div></div></div>`;
    $("#baGrid").innerHTML = shell("bad", `❌ gen ${b.before_gen} — wrong`, "before") + shell("good", `✅ gen ${b.after_gen} — right`, "after");
    $("#baGold").innerHTML = "";
    $("#runBtn").addEventListener("click", () => runBoth(i));
  }

  async function runOne(key, o, ok, db) {
    const out = $("#out-" + key);
    out.innerHTML = `<div class="runstate"><span class="spinner"></span> <span class="typing">executing on ${esc(db)}.sqlite</span></div>`;
    await sleep(900);
    const verdict = ok
      ? `<span class="verdict ok">✓ matches gold</span>`
      : `<span class="verdict no">✗ wrong rows</span>`;
    $("#verdict-" + key).innerHTML = verdict;
    out.innerHTML = `<div class="reveal">${resultTable(o)}${o.verifier_error ? `<div class="err">verifier verdict: ${esc(o.verifier_error)}</div>` : ""}</div>`;
  }

  async function runBoth(i) {
    const b = D.before_after[i];
    const btn = $("#runBtn"); if (btn.disabled) return; btn.disabled = true;
    $("#runState").innerHTML = `<span class="spinner"></span> running gen ${b.before_gen}…`;
    await runOne("before", b.before, false, b.db_id);
    $("#runState").innerHTML = `<span class="spinner"></span> running gen ${b.after_gen}…`;
    await runOne("after", b.after, true, b.db_id);
    $("#runState").innerHTML = `done — same model, smarter harness`;
    const best = D.gens.find((g) => g.gen === b.after_gen);
    const learned = (best && best.headlines && best.headlines.length)
      ? best.headlines.slice(0, 4).map((h) => "• " + esc(h)).join("<br>")
      : "few-shot from the training pool + execute-and-repair";
    $("#baGold").innerHTML =
      `<div class="learned reveal">🧠 Between gen ${b.before_gen} and gen ${b.after_gen}, SIA taught itself:
        <small>${learned}</small></div>
       <div class="gold-wrap"><div class="ba-card gold"><div class="head"><span>🪙 gold query (held-out, never shown to the agent)</span></div>
        <div class="body"><div class="sql">${esc(b.gold.sql)}</div>${resultTable(b.gold)}</div></div></div>`;
    btn.disabled = false;
  }

  function renderFoot() {
    $("#foot").innerHTML =
      `run_${D.meta.run_id} · ${D.meta.generations} generations · meta: ${esc(D.meta.meta_model)} · target: ${esc(D.meta.task_model)}<br>
       verifier: ${esc(D.meta.verifier)} · gold held out in <span class="kbd">data/private</span> · snapshot ${esc(D.meta.generated_at)}<br>
       every gain is a harness self-edit — the task model never changed.`;
  }

  /* ── boot ──────────────────────────────────────────────────────────── */
  initTheme();
  $("#themeBtn").addEventListener("click", () =>
    applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark"));
  renderHero();
  renderGenTabs();
  renderBA();
  renderFoot();
  attachTilt();
  selectGen(D.summary.best_gen);
  drawChart(true);   // animated draw-in on load
  window.addEventListener("resize", () => drawChart(false));
})();
