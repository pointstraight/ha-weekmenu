"use strict";
// Weekmenu UI. All URLs are relative so the page works behind Home Assistant ingress.
let S = null;            // last state from the server
let tab = "menu";
let timer = null;
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (iso, opts) => new Date(iso + "T12:00:00").toLocaleDateString("nl-BE", opts || { day: "numeric", month: "short" });
const fmtTs = (iso) => iso ? new Date(iso).toLocaleString("nl-BE", { dateStyle: "short", timeStyle: "short" }) : "–";

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("Fout " + res.status));
  return data;
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, 5000);
}
async function act(fn, done) {
  try { await fn(); if (done) toast(done); } catch (e) { toast(e.message); }
  await refresh(true);
}
async function refresh(force) {
  try { S = await api("api/state"); } catch (e) { toast("Geen verbinding: " + e.message); return; }
  renderJob();
  const editing = document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
  if (force || !(editing || tab === "settings")) render();
  clearTimeout(timer);
  timer = setTimeout(refresh, S.job.running ? 2500 : 30000);
}
function renderJob() {
  const el = $("#job"), j = S.job;
  if (j.running) {
    const p = j.progress || {};
    el.className = "pill busy"; el.hidden = false;
    el.textContent = "Bezig: " + (p.step || j.kind) + (p.total ? ` ${p.done}/${p.total}` : "");
  } else if (j.error) { el.className = "pill error"; el.hidden = false; el.textContent = "Laatste taak mislukt"; }
  else el.hidden = true;
}
function render() {
  ({ menu: renderMenu, settings: renderSettings, catalog: renderCatalog, status: renderStatus })[tab]();
}

/* ---------- Menu ---------- */
function renderMenu() {
  const root = $("#tab-menu"), c = S.catalog, busy = S.job.running;
  let html = "";
  if (!c.main) {
    html += `<div class="card"><h2>Eerste keer instellen</h2>
      <ol class="steps">
        <li><b>Zelftest.</b> Haalt de index en 5 recepten op en toont wat er gelezen kon worden.
          <div class="row" style="margin-top:6px"><button class="btn" data-job="selftest" ${busy ? "disabled" : ""}>Zelftest starten</button>
          ${S.selftest ? `<span class="muted">Laatste zelftest: ${esc(selftestLine())}</span>` : ""}</div></li>
        <li><b>Catalogus opbouwen.</b> Eenmalig alle recepten ophalen (ongeveer een uur, op de achtergrond, zonder AI).
          <div class="row" style="margin-top:6px"><button class="btn" data-job="update" ${busy ? "disabled" : ""}>Catalogus opbouwen</button>
          <span class="muted">${c.total ? `${c.ok} van ${c.total} recepten opgehaald` : "nog niets opgehaald"}</span></div></li>
        <li><b>Eerste voorstel.</b> Zodra er hoofdgerechten zijn, verschijnt hier de knop voor een weekmenu.</li>
      </ol></div>`;
  }
  if (c.main) {
    html += `<div class="row spread" style="margin-bottom:12px">
      <span class="muted">${S.next_run ? "Volgende automatische run: " + esc(S.next_run) : "Automatische run staat uit"}</span>
      <button class="btn primary" data-job="generate" ${busy ? "disabled" : ""}>Voorstel voor volgende periode</button></div>`;
  }
  if (c.main && !S.menus.length) html += `<div class="card"><p class="muted">Nog geen weekmenu. Maak een voorstel met de knop hierboven.</p></div>`;
  const order = [...S.menus].sort((a, b) => (a.end < S.today) - (b.end < S.today) || a.start.localeCompare(b.start));
  for (const m of order) html += menuCard(m, busy);
  root.innerHTML = html;
}
function selftestLine() {
  const t = S.selftest; if (!t) return "";
  if (t.index_error) return "index mislukt (" + t.index_error + ")";
  const s = t.summary || {};
  return `${t.index.found} recepten in de index; ${s.recipes_ok}/5 gelezen, ${s.with_minutes} met tijd, ${s.with_category} met type, ${s.with_ingredients} met ingrediënten`;
}
function menuCard(m, busy) {
  const past = m.end < S.today, approved = m.status === "approved";
  const counts = S.tags.filter((t) => m.counts[t]).map((t) => `<span class="chip ${t}">${m.counts[t]} ${esc(S.tag_nl[t])}</span>`).join(" ");
  const days = m.days.map((d) => {
    const today = d.date === S.today ? " · vandaag" : "";
    if (!d.name) return `<li class="${d.long_day ? "long" : ""}"><div class="when"><span class="wd">${esc(d.weekday)}</span><small>${fmtDate(d.date)}${today}</small></div><div class="dish muted">Geen gerecht gevonden</div>
      <div class="acts">${past ? "" : `<button class="btn small" data-replace="${d.date}" data-start="${m.start}" ${busy ? "disabled" : ""}>Zoek gerecht</button>`}</div></li>`;
    const time = d.minutes ? `${d.minutes} min` : "tijd onbekend";
    return `<li class="${d.long_day ? "long" : ""}"><div class="when"><span class="wd">${esc(d.weekday)}</span><small>${fmtDate(d.date)}${today}</small></div>
      <div class="dish"><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name)}</a>
        <div class="meta"><span>${time}</span><span class="chip ${d.tag}">${esc(d.tag_nl)}</span>${d.reason && d.reason !== "vervangen" ? `<span>${esc(d.reason)}</span>` : ""}</div></div>
      <div class="acts">${past ? "" : `<button class="btn small" data-replace="${d.date}" data-start="${m.start}" ${busy ? "disabled" : ""}>Vervang</button>
        <button class="btn small danger" data-never="${esc(d.url)}" data-day="${d.date}" data-start="${m.start}" title="Dit gerecht nooit meer voorstellen" ${busy ? "disabled" : ""}>Nooit meer</button>`}</div></li>`;
  }).join("");
  return `<div class="card">
    <div class="row spread"><div><h2>${fmtDate(m.start, { weekday: "short", day: "numeric", month: "short" })} – ${fmtDate(m.end, { weekday: "short", day: "numeric", month: "short" })}</h2>
      <div class="row" style="margin-top:6px"><span class="pill ${approved ? "" : "busy"}">${past ? "voorbij" : approved ? "goedgekeurd" : "voorstel – te beoordelen"}</span>${counts}<span class="muted">${esc(m.method)}</span></div></div>
      ${past ? "" : `<div class="row">
        ${approved ? `<button class="btn" data-approve="${m.start}" data-undo="1">Heropen</button>` : `<button class="btn primary" data-approve="${m.start}">Keur goed</button>`}
        <button class="btn" data-regen="${m.start}" ${busy ? "disabled" : ""}>Volledig nieuw voorstel</button>
        <button class="btn danger" data-delete="${m.start}">Verwijder</button></div>`}
    </div>
    ${m.problems.length ? `<div class="warn"><b>Niet alle regels konden gehaald worden:</b><ul>${m.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>` : ""}
    <ul class="days">${days}</ul></div>`;
}

/* ---------- Settings ---------- */
function renderSettings() {
  const s = S.settings, root = $("#tab-settings");
  const mixRows = S.tags.map((t) => `<tr><td><span class="chip ${t}">${esc(S.tag_nl[t])}</span></td>
    <td><input type="number" min="0" max="7" name="mix.${t}.min" value="${s.mix[t].min}" aria-label="minimum ${esc(S.tag_nl[t])}"></td>
    <td><input type="number" min="0" max="7" name="mix.${t}.max" value="${s.mix[t].max}" aria-label="maximum ${esc(S.tag_nl[t])}"></td>
    <td class="num muted">${S.catalog.quick_by_tag[t] || 0} / ${S.catalog.by_tag[t] || 0}</td></tr>`).join("");
  const dayBoxes = (name, sel) => S.day_names.map((d, i) => `<label style="font-weight:400;margin-right:10px"><input type="checkbox" name="${name}" value="${i}" ${sel.includes(i) ? "checked" : ""}> ${d.slice(0, 2)}</label>`).join("");
  const dayOpts = S.day_names.map((d, i) => `<option value="${i}" ${i === s.run_weekday ? "selected" : ""}>${d}</option>`).join("");
  root.innerHTML = `<form id="settings" class="card">
    <h2>Weekmix</h2><p class="muted">Aantal gerechten per type in een week van 7 dagen. De laatste kolom toont hoeveel hoofdgerechten er zijn: binnen de weekdaglimiet / totaal.</p>
    <div class="scroll"><table class="mix"><thead><tr><th>Type</th><th>Minimum</th><th>Maximum</th><th class="num">Beschikbaar</th></tr></thead><tbody>${mixRows}</tbody></table></div>
    <div class="field"><label for="sv">Strikt vegetarisch</label><div><input id="sv" type="checkbox" name="strict_vegetarian" ${s.strict_vegetarian ? "checked" : ""}></div>
      <div class="hint">Aan: gerechten met kippenbouillon, vissaus, gelatine of ansjovis tellen niet als vegetarisch.</div></div>
    <h3>Tijd</h3>
    <div class="field"><label for="wd">Maximum op weekdagen</label><div><input id="wd" type="number" name="weekday_max_minutes" min="5" max="600" value="${s.weekday_max_minutes}"> minuten</div></div>
    <div class="field"><label for="we">Maximum op ruime dagen</label><div><input id="we" type="number" name="weekend_max_minutes" min="0" max="600" value="${s.weekend_max_minutes}"> minuten</div><div class="hint">0 = geen limiet.</div></div>
    <div class="field"><span class="lbl">Ruime dagen</span><div>${dayBoxes("long_days", s.long_days)}</div></div>
    <div class="field"><label for="ut">Recepten zonder tijd</label><div><select id="ut" name="unknown_time">
      <option value="never" ${s.unknown_time === "never" ? "selected" : ""}>nooit voorstellen</option>
      <option value="long_days" ${s.unknown_time === "long_days" ? "selected" : ""}>alleen op ruime dagen</option>
      <option value="any" ${s.unknown_time === "any" ? "selected" : ""}>op elke dag</option></select></div></div>
    <h3>Keuze</h3>
    <div class="field"><label for="nr">Niet herhalen binnen</label><div><input id="nr" type="number" name="no_repeat_weeks" min="0" max="104" value="${s.no_repeat_weeks}"> weken</div></div>
    <div class="field"><label for="ex">Nooit voorstellen</label><div><textarea id="ex" name="exclusions" placeholder="één woord per regel, bv. spruitjes">${esc(s.exclusions.join("\n"))}</textarea></div>
      <div class="hint">Een recept valt af als het woord in de naam of de ingrediënten staat.</div></div>
    <div class="field"><label for="ais">Slimme keuze met AI</label><div><input id="ais" type="checkbox" name="ai_selection" ${s.ai_selection ? "checked" : ""}></div>
      <div class="hint">${S.ai.available ? "Eén kleine AI-oproep per voorstel, voor variatie en seizoen. Uit: willekeurig binnen de regels, zonder AI." : "Geen API-sleutel ingesteld: de keuze gebeurt willekeurig binnen de regels."}</div></div>
    <div class="field"><label for="ait">AI voor onduidelijke recepten</label><div><input id="ait" type="checkbox" name="ai_tagging" ${s.ai_tagging ? "checked" : ""}></div>
      <div class="hint">Alleen recepten waar de regels het type niet konden bepalen, één keer per recept.</div></div>
    <h3>Planning</h3>
    <div class="field"><label for="ar">Automatisch elke week</label><div><input id="ar" type="checkbox" name="auto_run" ${s.auto_run ? "checked" : ""}></div></div>
    <div class="field"><label for="rw">Dag en uur van de run</label><div class="row"><select id="rw" name="run_weekday">${dayOpts}</select><input type="time" name="run_time" value="${esc(s.run_time)}" aria-label="uur"></div>
      <div class="hint">De run haalt eerst nieuwe recepten op en maakt dan het voorstel.</div></div>
    <div class="field"><label for="so">Menu start</label><div><input id="so" type="number" name="start_offset_days" min="0" max="6" value="${s.start_offset_days}"> dagen na de run</div><div class="hint">1 = run op maandag, menu van dinsdag tot en met maandag.</div></div>
    <div class="field"><label for="cp">Pauze tussen recepten</label><div><input id="cp" type="number" name="crawl_pause_seconds" min="0.5" max="30" step="0.5" value="${s.crawl_pause_seconds}"> seconden</div></div>
    <div class="row" style="margin-top:16px"><button class="btn primary" type="submit">Bewaar instellingen</button><span class="muted">Geldt vanaf het volgende voorstel.</span></div>
  </form>`;
}
function readSettings(form) {
  const f = new FormData(form), out = { mix: {} };
  for (const t of S.tags) out.mix[t] = { min: +f.get(`mix.${t}.min`), max: +f.get(`mix.${t}.max`) };
  for (const k of ["weekday_max_minutes", "weekend_max_minutes", "no_repeat_weeks", "run_weekday", "start_offset_days", "crawl_pause_seconds"]) out[k] = +f.get(k);
  for (const k of ["strict_vegetarian", "ai_selection", "ai_tagging", "auto_run"]) out[k] = f.get(k) === "on";
  out.long_days = f.getAll("long_days").map(Number);
  out.unknown_time = f.get("unknown_time");
  out.run_time = f.get("run_time");
  out.exclusions = f.get("exclusions");
  return out;
}

/* ---------- Catalogue ---------- */
function renderCatalog() {
  const c = S.catalog, root = $("#tab-catalog"), busy = S.job.running, p = S.job.progress || {};
  const stat = (n, label) => `<div class="stat"><b>${n}</b><span class="muted">${label}</span></div>`;
  if (!$("#cat-top")) root.innerHTML = `<div id="cat-top"></div>
    <div class="card"><h2>Recepten</h2>
      <div class="row" style="margin:10px 0"><input id="rq" type="search" placeholder="Zoek op naam of ingrediënt" aria-label="Zoek recept">
        <select id="rtag" aria-label="Type"><option value="">alle types</option>${S.tags.map((t) => `<option value="${t}">${esc(S.tag_nl[t])}</option>`).join("")}</select>
        <select id="rscope" aria-label="Selectie"><option value="main">hoofdgerechten</option><option value="all">alles</option><option value="unsure">type onzeker</option><option value="blocked">nooit meer</option></select>
        <button class="btn" id="rsearch">Zoek</button></div>
      <div class="scroll" id="rlist"><p class="muted">Zoek een recept om het type te corrigeren of het te blokkeren.</p></div></div>`;
  $("#cat-top").innerHTML = `<div class="card">
    <div class="row spread"><h2>Catalogus</h2><div class="row">
      <button class="btn" data-job="selftest" ${busy ? "disabled" : ""}>Zelftest</button>
      <button class="btn primary" data-job="update" ${busy ? "disabled" : ""}>Nieuwe recepten ophalen</button>
      ${busy ? `<button class="btn danger" id="stop">Stop</button>` : ""}</div></div>
    ${busy && p.total ? `<div style="margin-top:12px"><progress max="${p.total}" value="${p.done}"></progress><div class="muted">${p.done} van ${p.total} · ${esc(p.last || "")}</div></div>` : ""}
    <div class="stats" style="margin-top:14px">${stat(c.ok, "recepten gelezen")}${stat(c.main, "hoofdgerechten")}${stat(c.main_quick, `binnen ${S.settings.weekday_max_minutes} min`)}
      ${stat(c.todo, "nog op te halen")}${stat(c.nodata + c.failed, "mislukt")}${stat(c.blocked, "nooit meer")}</div>
    <p class="muted" style="margin:12px 0 0">Bron: ${esc(S.source)} · laatst bijgewerkt ${fmtTs(c.last_update)}
      ${c.no_category ? ` · ${c.no_category} zonder type gerecht` : ""}${c.main_no_time ? ` · ${c.main_no_time} hoofdgerechten zonder tijd` : ""}</p>
    ${c.ok > 20 && c.no_category > c.ok / 2 ? `<div class="warn">Bij de meeste recepten kon het type gerecht niet gelezen worden. Bezorg de zelftest hieronder aan de bouwer van deze app.</div>` : ""}
    ${S.selftest ? `<h3>Laatste zelftest</h3><p>${esc(selftestLine())}</p><pre class="report" id="report">${esc(JSON.stringify(S.selftest, null, 1))}</pre>
      <button class="btn small" id="copy">Kopieer rapport</button>` : ""}</div>`;
}
async function searchRecipes() {
  const q = encodeURIComponent($("#rq").value), tag = $("#rtag").value, scope = $("#rscope").value;
  let rows;
  try { rows = await api(`api/recipes?q=${q}&tag=${tag}&scope=${scope}`); } catch (e) { return toast(e.message); }
  $("#rlist").innerHTML = rows.length ? `<table><thead><tr><th>Recept</th><th class="num">Min</th><th>Type</th><th>Nooit meer</th></tr></thead><tbody>${rows.map((r) => `<tr>
      <td><a href="${esc(r.url)}" target="_blank" rel="noopener" style="color:inherit">${esc(r.name)}</a><div class="muted">${esc(r.category || "geen type gerecht")} · ${esc(r.tag_why || "")}</div></td>
      <td class="num">${r.minutes ?? "–"}</td>
      <td><select data-tag="${esc(r.url)}" aria-label="Type voor ${esc(r.name)}">${S.tags.map((t) => `<option value="${t}" ${t === (r.tag || "other") ? "selected" : ""}>${esc(S.tag_nl[t])}</option>`).join("")}</select></td>
      <td><input type="checkbox" data-block="${esc(r.url)}" ${r.blocked ? "checked" : ""} aria-label="Nooit meer voorstellen: ${esc(r.name)}"></td></tr>`).join("")}</tbody></table>
      ${rows.length === 100 ? `<p class="muted">Eerste 100 resultaten; verfijn de zoekopdracht voor meer.</p>` : ""}` : `<p class="muted">Niets gevonden.</p>`;
}

/* ---------- Status ---------- */
function renderStatus() {
  const u = S.ai.usage_30d, j = S.job;
  $("#tab-status").innerHTML = `<div class="card"><h2>Status</h2>
    <table><tbody>
      <tr><th>Volgende automatische run</th><td>${S.next_run ? esc(S.next_run) : "uitgeschakeld"}</td></tr>
      <tr><th>Laatste taak</th><td>${j.kind ? `${esc(j.kind)} · ${j.running ? "bezig" : "klaar " + fmtTs(j.finished)}${j.error ? ` · <span style="color:var(--danger)">${esc(j.error)}</span>` : ""}` : "nog geen"}</td></tr>
      <tr><th>AI</th><td>${S.ai.available ? `ingesteld (${esc(S.ai.model)})` : "geen API-sleutel; alles werkt op regels"}</td></tr>
      <tr><th>AI-verbruik, laatste 30 dagen</th><td>${u.calls} oproepen · ${u.input.toLocaleString("nl-BE")} tokens in · ${u.output.toLocaleString("nl-BE")} tokens uit</td></tr>
      <tr><th>Versie</th><td>${esc(S.version)} · parser ${S.parser_version}</td></tr>
    </tbody></table></div>
    <div class="card"><h2>Logboek</h2><div class="log">${S.log.map((l) => `<div class="${esc(l.level)}">${esc(fmtTs(l.ts))}  ${esc(l.msg)}</div>`).join("") || "leeg"}</div></div>`;
}

/* ---------- Events ---------- */
document.addEventListener("click", (e) => {
  const t = e.target.closest("button, [role=tab]");
  if (!t) return;
  const d = t.dataset;
  if (d.tab) {
    tab = d.tab;
    document.querySelectorAll("[role=tab]").forEach((b) => b.setAttribute("aria-selected", b === t));
    document.querySelectorAll("main > section").forEach((s) => { s.hidden = s.id !== "tab-" + tab; });
    return render();
  }
  if (d.job) return act(() => api("api/job", { kind: d.job }), { selftest: "Zelftest gestart", update: "Ophalen gestart", generate: "Voorstel wordt gemaakt" }[d.job]);
  if (d.regen) return act(() => api("api/job", { kind: "generate", start: d.regen }), "Nieuw voorstel wordt gemaakt");
  if (d.replace) return act(() => api("api/replace", { start: d.start, day: d.replace }));
  if (d.never) return act(async () => { await api("api/recipe", { url: d.never, blocked: true }); await api("api/replace", { start: d.start, day: d.day }); }, "Gerecht wordt niet meer voorgesteld");
  if (d.approve) return act(() => api("api/approve", { start: d.approve, undo: !!d.undo }), d.undo ? "Menu heropend" : "Menu goedgekeurd");
  if (d.delete) return act(() => api("api/delete", { start: d.delete }), "Menu verwijderd");
  if (t.id === "stop") return act(() => api("api/stop", {}), "Stoppen…");
  if (t.id === "rsearch") return searchRecipes();
  if (t.id === "copy") return navigator.clipboard.writeText($("#report").textContent).then(() => toast("Rapport gekopieerd"), () => toast("Kopiëren lukt hier niet; selecteer de tekst"));
});
document.addEventListener("submit", (e) => {
  if (e.target.id !== "settings") return;
  e.preventDefault();
  act(() => api("api/settings", readSettings(e.target)), "Instellingen bewaard");
});
document.addEventListener("change", (e) => {
  const d = e.target.dataset;
  if (d.tag) api("api/recipe", { url: d.tag, tag: e.target.value }).then(() => toast("Type aangepast"), (err) => toast(err.message));
  if (d.block) api("api/recipe", { url: d.block, blocked: e.target.checked }).then(() => toast(e.target.checked ? "Geblokkeerd" : "Weer toegelaten"), (err) => toast(err.message));
});
document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.id === "rq") searchRecipes(); });
refresh(true);
