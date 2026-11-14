"use strict";

/* ================= Hilfsfunktionen ================= */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;
const WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];

function bytes(n) {
  if (n == null || Number.isNaN(n)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(i < 2 ? 0 : 2).replace(".", ",")} ${u[i]}`;
}
function when(isoStr) {
  if (!isoStr) return "–";
  return new Date(isoStr).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
function ago(isoStr) {
  if (!isoStr) return "noch nie";
  const h = (Date.now() - new Date(isoStr)) / 36e5;
  if (h < 1) return "vor weniger als einer Stunde";
  if (h < 48) return `vor ${Math.round(h)} Std.`;
  return `vor ${Math.round(h / 24)} Tagen`;
}
function scheduleText(s) {
  const hh = `${String(s.hour).padStart(2, "0")}:00 Uhr`;
  return { off: "Keine automatische Sicherung", hourly: "Stündlich", daily: `Täglich um ${hh}`,
    weekly: `Jeden ${WEEKDAYS[s.weekday]} um ${hh}` }[s.mode] || "–";
}
function sanitizeName(v) { return v.trim().replace(/\s+/g, "_"); }
function randomPassword() {
  const a = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
  const buf = new Uint32Array(20);
  crypto.getRandomValues(buf);
  return [...buf].map((n) => a[n % a.length]).join("");
}

async function api(path, { method = "GET", body, form } = {}) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (form) opt.body = form;
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/api" + path, opt);
  if (r.status === 401 && path !== "/login") { showLogin(); throw new Error("Bitte erneut anmelden."); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join(", ") : d || data || r.statusText);
  }
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isError ? 7000 : 3500);
}
const fail = (e) => toast(e.message || String(e), true);

// Aufklappmenüs schließen, wenn außerhalb geklickt wird
document.addEventListener("click", (e) => {
  $$("details.menu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
});

/* ================= Dialog ================= */
function dialog({ title, html = "", confirm = "Bestätigen", cancel = "Abbrechen", danger = false, typeToConfirm = null, onOpen, validate }) {
  return new Promise((resolve) => {
    const dlg = $("#dialog");
    const form = $("#dialog-form");
    form.innerHTML = `
      <h3>${esc(title)}</h3>
      ${html}
      ${typeToConfirm ? `<label>Zur Bestätigung „${esc(typeToConfirm)}“ eintippen<input name="__confirm" autocomplete="off"></label>` : ""}
      <p class="form-error" id="dlg-error"></p>
      <div class="actions">
        ${cancel ? `<button class="btn ghost" value="cancel" formnovalidate>${esc(cancel)}</button>` : ""}
        ${confirm ? `<button class="btn ${danger ? "danger" : "primary"}" value="ok" id="dlg-ok">${esc(confirm)}</button>` : ""}
      </div>`;
    const ok = $("#dlg-ok", form);
    if (typeToConfirm && ok) {
      ok.disabled = true;
      $("[name=__confirm]", form).addEventListener("input", (e) => (ok.disabled = e.target.value !== typeToConfirm));
    }
    const collect = () => {
      const data = Object.fromEntries(new FormData(form));
      $$("input[type=checkbox]", form).forEach((c) => (data[c.name] = c.checked));
      return data;
    };
    if (ok && validate) {
      ok.addEventListener("click", (e) => {
        const msg = validate(collect(), form);
        if (msg) { e.preventDefault(); $("#dlg-error", form).textContent = msg; }
      });
    }
    if (onOpen) onOpen(form);
    dlg.onclose = () => resolve(dlg.returnValue === "ok" ? collect() : null);
    dlg.returnValue = "";
    dlg.showModal();
  });
}

/* ================= Aufgaben ================= */
let jobTimer = null;
async function runJob(promise, after) {
  try { const { job } = await promise; watchJob(job, after); }
  catch (e) { fail(e); }
}
function watchJob(id, after) {
  $("#job").hidden = false;
  clearTimeout(jobTimer);
  const tick = async () => {
    try {
      const j = await api(`/jobs/${id}`);
      $("#job-title").textContent = j.title;
      const st = $("#job-state");
      const running = j.status === "läuft";
      st.textContent = running ? "läuft …" : j.status === "fertig" ? "Fertig" : "Fehlgeschlagen";
      st.className = "job-state " + (running ? "running" : j.status === "fertig" ? "done" : "failed");
      const log = $("#job-log");
      const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
      log.textContent = j.log.join("\n");
      if (atEnd) log.scrollTop = log.scrollHeight;
      if (running) { jobTimer = setTimeout(tick, 1200); return; }
      toast(j.status === "fertig" ? `${j.title}: fertig` : `${j.title}: ${j.error}`, j.status !== "fertig");
      if (after) after(j); else render();
    } catch (_) { jobTimer = setTimeout(tick, 3000); }
  };
  tick();
}
$("#job-close").addEventListener("click", () => ($("#job").hidden = true));

/* ================= Marke & Anmeldung ================= */
async function loadBranding() {
  try {
    const b = await api("/branding");
    document.title = b.name;
    $$(".brand-name").forEach((el) => (el.textContent = b.name));
    $$(".brand-logo").forEach((img) => {
      img.hidden = !b.logo;
      if (b.logo) img.src = `/api/branding/logo?v=${b.logo_v}`;
    });
  } catch (_) { /* Anmeldeseite funktioniert auch ohne */ }
}
async function loadAccountName() {
  try { const a = await api("/account"); $("#user-name").textContent = a.name || a.username; } catch (_) { /* egal */ }
}
function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  loadBranding();
  $("[name=username]").focus();
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    await api("/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    e.target.reset();
    start();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", async () => { await api("/logout", { method: "POST" }).catch(() => {}); showLogin(); });

/* Datei-Symbol mit „KEY“ für den Zugangsschlüssel (.resolvedbkey) */
const KEY_ICON = `
  <svg viewBox="0 0 40 48" width="44" height="53" aria-hidden="true" focusable="false">
    <g fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M27 44 H31 a3 3 0 0 0 3 -3 V14 L24 4 H9 a3 3 0 0 0 -3 3 V29"/>
      <path d="M24 4 V11 a3 3 0 0 0 3 3 H34"/>
    </g>
    <text x="2.5" y="42" class="key-file-label">KEY</text>
  </svg>`;

/* ================= Verbinden-Dialog ================= */
async function showConnect(name, prefetched) {
  const c = prefetched || await api(`/libraries/${enc(name)}/connection`);
  await dialog({
    title: `Mit „${name}“ verbinden`, confirm: null, cancel: "Schließen",
    html: `
      <p class="hint">In Resolve: Projektmanager öffnen → Projektbibliotheken → Bibliothek hinzufügen → Netzwerk. Diese Werte eintragen oder den Zugangsschlüssel importieren.</p>
      <dl class="kv">
        <dt>Name</dt><dd>${esc(name)}</dd>
        <dt>Adresse</dt><dd>${esc(c.host)}${c.port !== 5432 ? `:${c.port}` : ""}</dd>
        <dt>Datenbank</dt><dd>${esc(c.database)}</dd>
        <dt>Benutzer</dt><dd>${esc(c.user)}</dd>
        <dt>Passwort</dt><dd><span id="c-pw">••••••••••</span> <button type="button" class="btn small ghost" id="c-show">Anzeigen</button></dd>
      </dl>
      ${c.own_access ? "" : `<p class="banner">Diese Bibliothek wurde in Resolve angelegt und nutzt den Admin-Zugang des Servers. Unter „Erweitert → Eigenen Zugang einrichten“ bekommt sie ein eigenes Passwort.</p>`}
      <div class="actions">
        <a class="key-file" href="/api/libraries/${enc(name)}/connect-key" download
           title="Zugangsschlüssel herunterladen" aria-label="Zugangsschlüssel herunterladen">${KEY_ICON}</a>
        <button type="button" class="btn" id="c-copy">Alles kopieren</button>
      </div>`,
    onOpen: (f) => {
      $("#c-show", f).onclick = (e) => {
        const pw = $("#c-pw", f);
        const shown = pw.textContent !== "••••••••••";
        pw.textContent = shown ? "••••••••••" : c.password;
        e.target.textContent = shown ? "Anzeigen" : "Verbergen";
      };
      $("#c-copy", f).onclick = async () => {
        const text = `Adresse: ${c.host}\nPort: ${c.port}\nDatenbank: ${c.database}\nBenutzer: ${c.user}\nPasswort: ${c.password}`;
        try { await navigator.clipboard.writeText(text); toast("Zugangsdaten kopiert"); }
        catch (_) { toast("Kopieren nicht möglich.", true); }
      };
    },
  });
}

/* ================= Ansichten ================= */
const views = {};

/* ---------- Übersicht ---------- */
views.start = async (el) => {
  const [s, libs] = await Promise.all([api("/status"), api("/libraries").catch((e) => ({ error: e.message }))]);
  const running = s.container === "running";
  const noHost = !s.public_host;
  const dbUsed = s.disk.db_total - s.disk.db_free;
  const bkUsed = s.disk.bk_total - s.disk.bk_free;

  el.innerHTML = `
    <div class="layout-main-aside">
      <section>
        <div class="view-head">
          <h2>Projektbibliotheken</h2>
          <div class="actions">
            <button class="btn icon" id="refresh" title="Neu laden" aria-label="Neu laden">↻</button>
            <button class="btn primary" id="create">Neu anlegen</button>
          </div>
        </div>
        ${s.boot.error ? `<p class="banner danger">Datenbankserver konnte nicht starten: ${esc(s.boot.error)}</p>` : ""}
        ${libs.error ? `<p class="banner danger">${esc(libs.error)}</p>`
          : libs.length ? `<div class="libs">${libs.map((d) => libRow(d, noHost)).join("")}</div>`
          : `<div class="empty-state">Noch keine Projektbibliotheken. <a href="#" id="create-inline">Jetzt eine anlegen</a>.</div>`}
      </section>

      <aside class="stack">
        <div class="card">
          <h3>Datenbankserver</h3>
          <p class="sub">Erreichbarkeit für Resolve</p>
          <p class="tally ${s.job ? "busy" : running ? "on" : "off"}"><span class="dot"></span>
            ${s.job ? esc(s.job.title) : running ? "Online" : "Offline"}</p>
          <dl class="kv section">
            <dt>Adresse</dt><dd>${noHost ? `<a href="#/konfiguration?markiere=adresse">nicht konfiguriert</a>` : esc(s.public_host)}</dd>
            <dt>Port</dt><dd>${s.port}</dd>
            <dt>Verbunden</dt><dd>${s.connections ?? "–"}</dd>
          </dl>
        </div>
        <div class="card">
          <h3>Speicher</h3>
          <p class="sub">Belegung der Laufwerke</p>
          <dl class="kv"><dt>Datenbank</dt><dd>${bytes(dbUsed)} / ${bytes(s.disk.db_total)}</dd></dl>
          <div class="meter"><i data-pct="${(dbUsed / s.disk.db_total) * 100}"></i></div>
          <dl class="kv"><dt>Sicherungen</dt><dd>${bytes(bkUsed)} / ${bytes(s.disk.bk_total)}</dd></dl>
          <div class="meter"><i data-pct="${(bkUsed / s.disk.bk_total) * 100}"></i></div>
          <p class="sub">Bibliotheken belegen ${bytes(s.db_size)}, Sicherungen ${bytes(s.backups.size)}.</p>
        </div>
        <div class="card">
          <h3>Version</h3>
          <p class="sub">Softwarestand</p>
          <dl class="kv">
            <dt>Web-UI</dt><dd>${esc(s.app_version)}</dd>
            <dt>PostgreSQL</dt><dd>${esc(s.server_version || s.pg_version)}</dd>
          </dl>
          <div class="actions section"><a class="btn small" href="#/server">Version ändern</a></div>
        </div>
      </aside>
    </div>`;

  $$(".meter i", el).forEach((i) => (i.style.width = `${Math.min(100, +i.dataset.pct || 0)}%`));
  $("#refresh").onclick = render;
  $("#create").onclick = createLibrary;
  if ($("#create-inline")) $("#create-inline").onclick = (e) => { e.preventDefault(); createLibrary(); };
  if (!libs.error) bindLibRows(el);
};

function libRow(d, noHost) {
  return `
    <article class="lib" data-name="${esc(d.name)}">
      <div class="lib-main">
        <div class="lib-name">${esc(d.name)}</div>
        <div class="lib-meta">
          <span>${bytes(d.size)}</span>
          ${d.projects != null ? `<span>${d.projects} Projekt${d.projects === 1 ? "" : "e"}</span>` : ""}
          ${d.connections ? `<span class="pill live">${d.connections} verbunden</span>` : ""}
          ${d.last_backup ? `<span>Gesichert ${ago(d.last_backup)}</span>` : `<span class="pill cue">Nie gesichert</span>`}
          ${d.schedule.mode === "off" ? "" : `<span>${esc(scheduleText(d.schedule))}</span>`}
          ${d.own_access ? "" : `<span class="pill">Admin-Zugang</span>`}
        </div>
      </div>
      <div class="actions">
        ${noHost
          ? `<a class="blocked" href="#/konfiguration?markiere=adresse">Zuerst die Zugangsadresse konfigurieren</a>`
          : `<button class="btn" data-act="connect">Verbinden</button>`}
        <a class="btn" href="#/bibliothek/${enc(d.name)}">Sicherungen</a>
        <details class="menu btn-menu">
          <summary>Erweitert</summary>
          <div class="menu-list">
            <button type="button" data-act="backup">Jetzt sichern</button>
            <button type="button" data-act="optimize">Optimieren</button>
            <button type="button" data-act="password">${d.own_access ? "Passwort ändern" : "Eigenen Zugang einrichten"}</button>
            <button type="button" data-act="delete" class="danger">Löschen</button>
          </div>
        </details>
      </div>
    </article>`;
}

function bindLibRows(el) {
  $$(".lib[data-name]", el).forEach((row) => {
    const name = row.dataset.name;
    $$("[data-act]", row).forEach((b) => (b.onclick = async () => {
      const menu = b.closest("details");
      if (menu) menu.open = false;
      const act = b.dataset.act;
      try {
        if (act === "connect") return showConnect(name);
        if (act === "backup") return runJob(api(`/libraries/${enc(name)}/backup`, { method: "POST" }));
        if (act === "optimize") {
          const r = await dialog({
            title: `„${name}“ optimieren`, confirm: "Optimieren",
            html: `<p class="hint">Räumt gelöschte Datensätze auf und aktualisiert die Statistiken – wie „Optimieren“ im Resolve-Projektmanager.</p>
                   <label class="check"><input type="checkbox" name="full"> Gründlich (gibt Speicher frei, trennt aber alle Verbindungen zu dieser Bibliothek)</label>`,
          });
          if (r) runJob(api(`/libraries/${enc(name)}/optimize`, { method: "POST", body: { full: !!r.full } }));
        }
        if (act === "password") {
          const r = await dialog({
            title: `Passwort für „${name}“`, confirm: "Passwort speichern",
            html: passwordFields() + `<p class="hint">Alle Arbeitsplätze brauchen danach das neue Passwort bzw. einen neuen Zugangsschlüssel.</p>`,
            onOpen: bindPasswordFields, validate: validatePasswords,
          });
          if (r) { await api(`/libraries/${enc(name)}/password`, { method: "POST", body: { password: r.password } }); toast("Passwort gespeichert"); render(); }
        }
        if (act === "delete") {
          const r = await dialog({
            title: `„${name}“ löschen`, confirm: "Endgültig löschen", danger: true, typeToConfirm: name,
            html: `<p class="banner danger">Die Bibliothek, alle Projekte darin und ihr Zugang werden vom Server entfernt. Verbundene Arbeitsplätze werden getrennt.</p>
                   <label class="check"><input type="checkbox" name="backup" checked> Vorher sichern (empfohlen)</label>`,
          });
          if (r) runJob(api(`/libraries/${enc(name)}?backup=${!!r.backup}`, { method: "DELETE" }));
        }
      } catch (e) { fail(e); }
    }));
  });
}

function passwordFields() {
  return `
    <div class="actions"><button type="button" class="btn small ghost" id="pw-gen">Zufallspasswort erzeugen</button></div>
    <div class="pw-line"><input name="password" type="password" placeholder="Passwort" autocomplete="new-password" required>
      <button type="button" class="btn small ghost" id="pw-eye">Anzeigen</button></div>
    <input name="password2" type="password" placeholder="Passwort bestätigen" autocomplete="new-password" required>`;
}
function bindPasswordFields(f) {
  const a = $("[name=password]", f), b = $("[name=password2]", f);
  $("#pw-gen", f).onclick = () => { const p = randomPassword(); a.value = b.value = p; a.type = b.type = "text"; $("#pw-eye", f).textContent = "Verbergen"; };
  $("#pw-eye", f).onclick = (e) => { const show = a.type === "password"; a.type = b.type = show ? "text" : "password"; e.target.textContent = show ? "Verbergen" : "Anzeigen"; };
}
function validatePasswords(d) {
  if ((d.password || "").length < 8) return "Das Passwort muss mindestens 8 Zeichen haben.";
  if (d.password.includes("'")) return "Bitte keinen Apostroph im Passwort verwenden.";
  if (d.password !== d.password2) return "Die Passwörter stimmen nicht überein.";
  return null;
}

async function createLibrary() {
  const defaultNote = "Keine Sonderzeichen oder Umlaute. Leerzeichen werden automatisch zu Unterstrichen (_).";
  const r = await dialog({
    title: "Neue Projektbibliothek anlegen", confirm: "Anlegen",
    html: `
      <label>Name<input name="name" autocomplete="off" required></label>
      <p class="form-note" id="name-note">${defaultNote}</p>
      <p class="hint">Damit sich Resolve verbinden kann, bekommt jede Bibliothek ein eigenes Passwort. Benutzername ist der Name der Bibliothek.</p>
      ${passwordFields()}`,
    onOpen: (f) => {
      bindPasswordFields(f);
      const input = $("[name=name]", f);
      input.addEventListener("input", () => {
        const s = sanitizeName(input.value);
        $("#name-note", f).textContent = s && s !== input.value.trim() ? `Wird angelegt als: ${s}` : defaultNote;
      });
      input.focus();
    },
    validate: (d) => {
      const n = sanitizeName(d.name || "");
      if (!/^[A-Za-z0-9_]{1,63}$/.test(n)) return "Nur Buchstaben (ohne Umlaute), Ziffern und _ erlaubt, höchstens 63 Zeichen.";
      return validatePasswords(d);
    },
  });
  if (!r) return;
  try {
    toast("Wird angelegt …");
    const res = await api("/libraries", { method: "POST", body: { name: sanitizeName(r.name), password: r.password } });
    toast(`„${res.name}“ angelegt`);
    render();  // Liste im Hintergrund neu laden, Dialog sofort zeigen
    if (res.connection.host) showConnect(res.name, res.connection);
  } catch (e) { fail(e); }
}

/* ---------- Sicherungen einer Bibliothek ---------- */
views.bibliothek = async (el, name) => {
  const [libs, list, hooks] = await Promise.all([api("/libraries"), api(`/backups?library=${enc(name)}`), api("/hooks")]);
  const lib = libs.find((l) => l.name === name);
  if (!lib) throw new Error(`Die Bibliothek „${name}“ gibt es nicht (mehr).`);
  const s = lib.schedule;
  const activeHooks = hooks.hooks.filter((h) => h.active).map((h) => h.name);

  el.innerHTML = `
    <div class="layout-aside-main">
      <aside class="stack">
        <div class="card">
          <h3>Sicherungen von</h3>
          <p class="lib-name">${esc(name)}</p>
        </div>
        <div class="card">
          <h3>Manuelle Sicherung</h3>
          <p class="sub">Sofort eine Sicherung dieser Bibliothek anlegen.</p>
          <button class="btn primary" id="backup-now">Jetzt sichern</button>
        </div>
        <form class="card" id="schedule">
          <h3>Automatische Sicherung</h3>
          <p class="sub">${esc(scheduleText(s))}</p>
          <div class="stack">
            <label>Häufigkeit
              <select name="mode">
                ${[["off", "Aus"], ["hourly", "Stündlich"], ["daily", "Täglich"], ["weekly", "Wöchentlich"]]
                  .map(([v, t]) => `<option value="${v}" ${s.mode === v ? "selected" : ""}>${t}</option>`).join("")}
              </select></label>
            <label data-for="weekly">Wochentag
              <select name="weekday">${WEEKDAYS.map((d, i) => `<option value="${i}" ${s.weekday === i ? "selected" : ""}>${d}</option>`).join("")}</select></label>
            <label data-for="daily weekly">Uhrzeit
              <select name="hour">${[...Array(24).keys()].map((h) => `<option value="${h}" ${s.hour === h ? "selected" : ""}>${String(h).padStart(2, "0")}:00</option>`).join("")}</select></label>
            <button class="btn">Zeitplan speichern</button>
          </div>
        </form>
        <div class="card">
          <h3>Backup-Hooks</h3>
          <p class="sub">Eigene Skripte vor und nach jeder Sicherung ausführen, z. B. um Sicherungen auf ein NAS oder in die Cloud zu kopieren.</p>
          <label class="check"><input type="checkbox" id="hooks" ${lib.hooks ? "checked" : ""}> Für diese Bibliothek ausführen</label>
          <p class="sub section">${activeHooks.length ? `Aktiv: ${activeHooks.map((h) => `<code>${h}</code>`).join(" ")}.` : "Noch keine Hook-Skripte vorhanden."}
            Sie liegen auf dem Server unter <code>${esc(hooks.host_dir)}</code>, Beispiele sind dabei.</p>
        </div>
      </aside>

      <section>
        <div class="view-head">
          <h2>Vorhandene Sicherungen</h2>
          <div class="actions"><button class="btn icon" id="refresh" title="Neu laden" aria-label="Neu laden">↻</button></div>
        </div>
        ${s.mode === "off" ? `<p class="banner">Diese Bibliothek wird nicht automatisch gesichert. Wir empfehlen, jetzt einen <a href="#" id="to-schedule">Zeitplan für automatische Sicherungen</a> einzurichten.</p>` : ""}
        <p class="lead">Aus jeder dieser Sicherungen lässt sich die Bibliothek wiederherstellen – unter gleichem oder neuem Namen.</p>
        ${list.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Erstellt</th><th>Anlass</th><th>PostgreSQL</th><th class="num">Größe</th><th></th></tr></thead>
          <tbody>${list.map((b) => `
            <tr data-path="${esc(b.path)}">
              <td>${when(b.created)} ${b.pinned ? `<span class="pill cue">angeheftet</span>` : ""}</td>
              <td class="sub">${esc(b.reason.replace(/-/g, " "))}</td>
              <td>${esc(b.pg_version || "–")}</td>
              <td class="num">${bytes(b.size)}</td>
              <td class="row-actions">
                <button class="btn small" data-act="restore">Wiederherstellen</button>
                <a class="btn small" href="/api/backups/download?path=${enc(b.path)}">Herunterladen</a>
                <details class="menu btn-menu"><summary>Mehr</summary><div class="menu-list">
                  <button type="button" data-act="pin">${b.pinned ? "Lösen (darf automatisch gelöscht werden)" : "Anheften (nie automatisch löschen)"}</button>
                  <button type="button" data-act="delete" class="danger">Löschen</button>
                </div></details>
              </td>
            </tr>`).join("")}</tbody></table></div>`
          : `<div class="empty-state">Keine Sicherungen gefunden.</div>`}
      </section>
    </div>`;

  const form = $("#schedule");
  const syncFields = () => {
    const mode = form.mode.value;
    $$("[data-for]", form).forEach((l) => (l.hidden = !l.dataset.for.split(" ").includes(mode)));
  };
  form.mode.onchange = syncFields;
  syncFields();
  const saveSchedule = () => api(`/libraries/${enc(name)}/schedule`, { method: "PUT", body: {
    mode: form.mode.value, hour: +form.hour.value, weekday: +form.weekday.value, hooks: $("#hooks").checked } });
  form.onsubmit = async (e) => { e.preventDefault(); try { await saveSchedule(); toast("Zeitplan gespeichert"); render(); } catch (err) { fail(err); } };
  $("#hooks").onchange = async () => { try { await saveSchedule(); toast("Gespeichert"); } catch (err) { fail(err); } };
  $("#backup-now").onclick = () => runJob(api(`/libraries/${enc(name)}/backup`, { method: "POST" }));
  $("#refresh").onclick = render;
  if ($("#to-schedule")) $("#to-schedule").onclick = (e) => { e.preventDefault(); form.classList.add("highlight"); form.mode.focus(); };

  $$("tr[data-path]", el).forEach((row) => {
    const path = row.dataset.path;
    $$("[data-act]", row).forEach((b) => (b.onclick = async () => {
      const menu = b.closest("details");
      if (menu) menu.open = false;
      try {
        if (b.dataset.act === "pin") {
          await api("/backups/pin", { method: "POST", body: { path, pinned: b.textContent.startsWith("Anheften") } });
          render();
        }
        if (b.dataset.act === "delete") {
          const r = await dialog({ title: "Sicherung löschen", confirm: "Löschen", danger: true, html: `<p class="hint">${esc(path)}</p>` });
          if (r) { await api(`/backups?path=${enc(path)}`, { method: "DELETE" }); toast("Sicherung gelöscht"); render(); }
        }
        if (b.dataset.act === "restore") {
          const r = await dialog({
            title: "Bibliothek wiederherstellen", confirm: "Wiederherstellen",
            html: `
              <label class="check"><input type="radio" name="mode" value="new" checked> Als neue Bibliothek</label>
              <label data-new>Name der neuen Bibliothek<input name="target" value="${esc(name)}_wiederhergestellt"></label>
              <label class="check"><input type="radio" name="mode" value="overwrite"> „${esc(name)}“ überschreiben (wird vorher gesichert)</label>
              <p class="hint">Tipp: Erst als neue Bibliothek wiederherstellen und in Resolve prüfen. Eine neue Bibliothek bekommt ein Zufallspasswort – zu sehen unter „Verbinden“.</p>`,
            onOpen: (f) => $$("[name=mode]", f).forEach((rb) => (rb.onchange = () => ($("[data-new]", f).hidden = f.mode.value !== "new"))),
          });
          if (!r) return;
          const overwrite = r.mode === "overwrite";
          runJob(api("/backups/restore", { method: "POST", body: { path, target: overwrite ? name : sanitizeName(r.target), overwrite } }));
        }
      } catch (e) { fail(e); }
    }));
  });
};

/* ---------- Konfiguration ---------- */
views.konfiguration = async (el, _arg, params) => {
  const [s, tpl, branding, libs] = await Promise.all([
    api("/settings"), api("/settings/key-template"), api("/branding"), api("/libraries").catch(() => []),
  ]);
  el.innerHTML = `
    <h2>Konfiguration</h2>
    <form id="settings" class="stack section">
      <div class="two-col">
        <div class="card">
          <h3>Server</h3>
          <p class="sub">Name und Bild erscheinen in der Kopfzeile und auf der Anmeldeseite.</p>
          <div class="stack">
            <label>Name des Servers<input name="server_name" value="${esc(s.server_name)}" maxlength="60" required></label>
            <div class="logo-row">
              <img class="logo-preview" alt="Server-Bild" ${branding.logo ? `src="/api/branding/logo?v=${branding.logo_v}"` : "hidden"}>
              <div class="actions">
                <label class="btn">Bild hochladen<input type="file" id="logo-file" accept="image/png,image/jpeg,image/webp" hidden></label>
                ${branding.logo ? `<button type="button" class="btn ghost danger" id="logo-rm">Bild entfernen</button>` : ""}
              </div>
            </div>
            <p class="sub">PNG, JPG oder WebP, höchstens 2 MB.</p>
          </div>
        </div>
        <div class="card" id="adresse-card">
          <h3>Zugang für Resolve</h3>
          <p class="sub">Unter dieser Adresse erreichen die Arbeitsplätze den Server. Ohne sie gibt es kein „Verbinden“ und keine Zugangsschlüssel.</p>
          <label>Zugangsadresse (Domain oder öffentliche IP)
            <input name="public_host" value="${esc(s.public_host)}" placeholder="resolve.example.com"></label>
        </div>
      </div>
      <div class="card">
        <h3>Sicherungen</h3>
        <p class="sub">Zeitpläne für einzelne Bibliotheken stellst du auf deren Sicherungsseite ein.</p>
        <div class="form-grid">
          <label>Sicherungen behalten (Tage)<input type="number" name="retention_days" min="1" max="3650" value="${s.retention_days}"></label>
          <label>Mindestens behalten pro Bibliothek<input type="number" name="keep_min" min="1" max="100" value="${s.keep_min}"></label>
          <label class="check"><input type="checkbox" name="full_backup" ${s.full_backup ? "checked" : ""}> Nächtliche Vollsicherung des ganzen Servers (inkl. Zugänge)</label>
          <label>Uhrzeit Vollsicherung
            <select name="full_backup_hour">${[...Array(24).keys()].map((h) => `<option value="${h}" ${s.full_backup_hour === h ? "selected" : ""}>${String(h).padStart(2, "0")}:00</option>`).join("")}</select></label>
        </div>
      </div>
      <p class="form-error" id="settings-error"></p>
      <div class="actions"><button class="btn primary">Konfiguration speichern</button></div>
    </form>

    <section class="card section">
      <h3>Vorlage für Zugangsschlüssel (.resolvedbkey)</h3>
      <p class="hint">Blackmagic dokumentiert dieses Dateiformat nicht. ${tpl.custom ? "Du nutzt eine eigene Vorlage."
        : `<strong>Die Standardvorlage ist ungeprüft.</strong> Zuverlässig wird es so: In Resolve eine Bibliothek dieses Servers verbinden, dort ihren Zugangsschlüssel exportieren und die Datei hier hochladen – die echten Werte werden durch Platzhalter ersetzt.`}
        Platzhalter: <code>{{NAME}}</code> <code>{{HOST}}</code> <code>{{PORT}}</code> <code>{{DATABASE}}</code> <code>{{USER}}</code> <code>{{PASSWORD}}</code> <code>{{SERVER}}</code></p>
      <form id="learn" class="form-grid section">
        <label>Aus Resolve exportierte Datei<input type="file" name="file" required></label>
        <label>Gehört zur Bibliothek
          <select name="database" required>${libs.map((d) => `<option>${esc(d.name)}</option>`).join("")}</select></label>
        <div class="actions"><button class="btn" ${libs.length ? "" : "disabled"}>Vorlage aus Datei lernen</button></div>
      </form>
      <label class="section">Vorlage<textarea id="tpl" spellcheck="false">${esc(tpl.template)}</textarea></label>
      <p class="form-error" id="tpl-msg"></p>
      <div class="actions">
        <button class="btn primary" id="tpl-save">Vorlage speichern</button>
        ${tpl.custom ? `<button class="btn ghost" id="tpl-reset">Standard wiederherstellen</button>` : ""}
      </div>
    </section>`;

  if (params.get("markiere") === "adresse") {
    const card = $("#adresse-card");
    card.classList.add("highlight");
    card.scrollIntoView({ block: "center" });
    $("[name=public_host]").focus();
  }

  $("#settings").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    $("#settings-error").textContent = "";
    try {
      await api("/settings", { method: "PUT", body: {
        server_name: f.get("server_name"), public_host: f.get("public_host"),
        retention_days: +f.get("retention_days"), keep_min: +f.get("keep_min"),
        full_backup: f.get("full_backup") === "on", full_backup_hour: +f.get("full_backup_hour"),
      } });
      toast("Konfiguration gespeichert");
      loadBranding();
      $("#adresse-card").classList.remove("highlight");
    } catch (err) { $("#settings-error").textContent = err.message; }
  };
  $("#logo-file").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try { await api("/settings/logo", { method: "POST", form }); toast("Bild gespeichert"); loadBranding(); render(); }
    catch (err) { fail(err); }
  };
  if ($("#logo-rm")) $("#logo-rm").onclick = async () => { await api("/settings/logo", { method: "DELETE" }); loadBranding(); render(); };
  $("#learn").onsubmit = async (e) => {
    e.preventDefault();
    const msg = $("#tpl-msg");
    try {
      const r = await api("/settings/key-template/learn", { method: "POST", form: new FormData(e.target) });
      $("#tpl").value = r.template;
      msg.textContent = r.missing.length
        ? `Nicht gefunden: ${r.missing.join(" ")} – bitte prüfen und ggf. von Hand einsetzen, dann speichern.`
        : "Alle Werte gefunden. Prüfen und speichern.";
    } catch (err) { msg.textContent = err.message; }
  };
  $("#tpl-save").onclick = async () => {
    try { await api("/settings/key-template", { method: "PUT", body: { template: $("#tpl").value } }); toast("Vorlage gespeichert"); render(); }
    catch (err) { fail(err); }
  };
  if ($("#tpl-reset")) $("#tpl-reset").onclick = async () => { await api("/settings/key-template", { method: "DELETE" }); render(); };
};

/* ---------- Server & Version ---------- */
views.server = async (el) => {
  const [info, s, all] = await Promise.all([api("/server"), api("/status"), api("/backups")]);
  const fullList = all.filter((b) => b.kind === "full");
  el.innerHTML = `
    <div class="view-head">
      <h2>Server</h2>
      <div class="actions"><button class="btn" id="restart">Datenbankserver neu starten</button></div>
    </div>
    <div class="two-col">
      <div class="card">
        <h3>PostgreSQL-Version</h3>
        <p class="sub">Aktuell PostgreSQL ${esc(s.server_version || info.version)}, Image <code>postgres:${esc(info.version)}</code></p>
        <div class="stack">
          <label>Neue Version
            <select id="ver-pick">
              ${info.hints.map((h) => `<option value="${esc(h.version)}" ${h.version === info.version ? "disabled" : ""}>${esc(h.version)} – ${esc(h.note)}</option>`).join("")}
              <option value="__custom">Andere Version eingeben …</option>
            </select></label>
          <label id="ver-custom-wrap" hidden>Version (z. B. 13 oder 13.18)<input id="ver-custom" inputmode="decimal"></label>
          <div class="actions"><button class="btn primary" id="upgrade">Version wechseln</button></div>
        </div>
      </div>
      <div class="card">
        <h3>So bleibt beim Wechsel alles erhalten</h3>
        <p class="hint">Tabellen und Zeilen aller Bibliotheken werden gezählt, dann wird alles gesichert. Die neue Version startet zunächst intern ohne Port, die Daten werden eingespielt und jede Tabelle Zeile für Zeile verglichen. Erst wenn alles übereinstimmt, wird umgeschaltet – sonst läuft die alte Version einfach weiter. Das alte Datenverzeichnis bleibt erhalten.</p>
      </div>
    </div>

    <section class="section">
      <h3>Datenverzeichnisse</h3>
      <p class="hint">Auf dem Host unter <code>${esc(info.host_data_dir)}/postgres/</code>. Alte Verzeichnisse erlauben ein Zurückwechseln; löschen erst, wenn die neue Version sicher läuft.</p>
      <div class="table-wrap section"><table>
        <thead><tr><th>Verzeichnis</th><th class="num">Größe</th><th></th></tr></thead>
        <tbody>${info.data_dirs.map((d) => `
          <tr data-dir="${esc(d.name)}"><td><strong>${esc(d.name)}</strong> ${d.current ? `<span class="pill live">in Betrieb</span>` : ""}</td>
          <td class="num">${bytes(d.size)}</td>
          <td class="row-actions">${d.current ? "" : `<button class="btn small ghost danger" data-act="rm">Löschen</button>`}</td></tr>`).join("")}
        </tbody></table></div>
    </section>

    <section class="section">
      <div class="view-head">
        <h3>Vollsicherungen des Servers</h3>
        <div class="actions">
          <button class="btn" id="prune">Alte Sicherungen aufräumen</button>
          <button class="btn" id="full">Vollsicherung jetzt</button>
        </div>
      </div>
      ${fullList.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Erstellt</th><th>Anlass</th><th>PostgreSQL</th><th class="num">Größe</th><th></th></tr></thead>
        <tbody>${fullList.map((b) => `<tr data-path="${esc(b.path)}"><td>${when(b.created)} ${b.pinned ? `<span class="pill cue">angeheftet</span>` : ""}</td>
          <td class="sub">${esc(b.reason.replace(/-/g, " "))}</td><td>${esc(b.pg_version || "–")}</td><td class="num">${bytes(b.size)}</td>
          <td class="row-actions"><a class="btn small" href="/api/backups/download?path=${enc(b.path)}">Herunterladen</a>
          <button class="btn small ghost danger" data-act="del">Löschen</button></td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty-state">Noch keine Vollsicherung.</div>`}
    </section>

    <section class="section">
      <h3>Bisherige Versionswechsel</h3>
      ${info.history.length ? `<div class="table-wrap section"><table><thead><tr><th>Datum</th><th>Von</th><th>Nach</th></tr></thead>
        <tbody>${info.history.slice().reverse().map((h) => `<tr><td>${when(h.at)}</td><td>${esc(h.from)}</td><td>${esc(h.to)}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="hint section">Noch keine.</p>`}
    </section>`;

  const pick = $("#ver-pick");
  const firstFree = [...pick.options].find((o) => !o.disabled);
  if (firstFree) pick.value = firstFree.value;
  pick.onchange = () => ($("#ver-custom-wrap").hidden = pick.value !== "__custom");
  $("#restart").onclick = () => runJob(api("/server/restart", { method: "POST" }));
  $("#full").onclick = () => runJob(api("/backups/full", { method: "POST" }));
  $("#prune").onclick = async () => {
    const st = await api("/settings");
    const r = await dialog({ title: "Alte Sicherungen aufräumen", confirm: "Aufräumen",
      html: `<p class="hint">Gelöscht werden Sicherungen, die älter als ${st.retention_days} Tage sind. Pro Bibliothek bleiben immer die ${st.keep_min} neuesten erhalten, angeheftete sowieso.</p>` });
    if (r) runJob(api("/backups/prune", { method: "POST" }));
  };
  $("#upgrade").onclick = async () => {
    const version = pick.value === "__custom" ? $("#ver-custom").value.trim() : pick.value;
    if (!version) return toast("Bitte eine Version angeben.", true);
    const hint = info.hints.find((h) => h.version === version);
    const down = parseFloat(version) < parseFloat(info.version);
    const r = await dialog({
      title: `Auf PostgreSQL ${version} wechseln`, confirm: "Wechsel starten",
      html: `
        ${down ? `<p class="banner danger">Das ist ein Wechsel auf eine ältere Version. Er kann fehlschlagen – dann läuft die alte Version einfach weiter.</p>` : ""}
        ${hint && hint.version !== "13" ? `<p class="banner">${esc(hint.note)}. Vorher mit einer Kopie testen.</p>` : ""}
        <p class="hint">Während des Wechsels ist der Server für Resolve kurz nicht erreichbar. Bitte alle Arbeitsplätze vorher schließen.</p>
        <label class="check"><input type="checkbox" name="force"> Auch wechseln, wenn noch Arbeitsplätze verbunden sind</label>`,
    });
    if (r) runJob(api("/server/upgrade", { method: "POST", body: { version, force: !!r.force } }));
  };
  $$("tr[data-dir] [data-act=rm]", el).forEach((b) => (b.onclick = async () => {
    const name = b.closest("tr").dataset.dir;
    const r = await dialog({ title: `Datenverzeichnis „${name}“ löschen`, confirm: "Endgültig löschen", danger: true, typeToConfirm: name,
      html: `<p class="banner danger">Ein Zurückwechseln auf diese Version ist dann nur noch über eine Sicherung möglich.</p>` });
    if (r) { try { await api(`/server/data-dirs/${enc(name)}`, { method: "DELETE" }); toast("Verzeichnis gelöscht"); render(); } catch (e) { fail(e); } }
  }));
  $$("tr[data-path] [data-act=del]", el).forEach((b) => (b.onclick = async () => {
    const path = b.closest("tr").dataset.path;
    const r = await dialog({ title: "Vollsicherung löschen", confirm: "Löschen", danger: true, html: `<p class="hint">${esc(path)}</p>` });
    if (r) { try { await api(`/backups?path=${enc(path)}`, { method: "DELETE" }); render(); } catch (e) { fail(e); } }
  }));
};

/* ---------- Konto ---------- */
views.konto = async (el) => {
  const a = await api("/account");
  el.innerHTML = `
    <h2>Konto bearbeiten</h2>
    <div class="two-col section">
      <form class="card stack" id="acc">
        <h3>Persönliche Angaben</h3>
        <label>Benutzername<input name="username" value="${esc(a.username)}" required></label>
        <label>Name<input name="name" value="${esc(a.name)}"></label>
        <label>E-Mail-Adresse<input name="email" type="email" value="${esc(a.email)}"></label>
        <p class="form-error" id="acc-err"></p>
        <div class="actions"><button class="btn primary">Änderungen speichern</button></div>
      </form>
      <form class="card stack" id="pw">
        <h3>Passwort ändern</h3>
        <p class="sub">Andere angemeldete Browser werden danach abgemeldet.</p>
        <label>Aktuelles Passwort<input name="current" type="password" autocomplete="current-password" required></label>
        <label>Neues Passwort<input name="new" type="password" autocomplete="new-password" minlength="10" required></label>
        <label>Neues Passwort bestätigen<input name="new2" type="password" autocomplete="new-password" required></label>
        <p class="form-error" id="pw-err"></p>
        <div class="actions"><button class="btn danger">Passwort ändern</button></div>
      </form>
    </div>`;
  $("#acc").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    $("#acc-err").textContent = "";
    try { await api("/account", { method: "PUT", body: f }); toast("Änderungen gespeichert"); loadAccountName(); }
    catch (err) { $("#acc-err").textContent = err.message; }
  };
  $("#pw").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    $("#pw-err").textContent = "";
    if (f.new !== f.new2) { $("#pw-err").textContent = "Die neuen Passwörter stimmen nicht überein."; return; }
    try { await api("/account/password", { method: "POST", body: { current: f.current, new: f.new } }); e.target.reset(); toast("Passwort geändert"); }
    catch (err) { $("#pw-err").textContent = err.message; }
  };
};

/* ---------- Zusammenarbeit ---------- */
const SCOPES = {
  privat: ["Lokales Netz", "Nur im selben Netz erreichbar"],
  vpn: ["VPN", "Über Tailscale o. Ä. erreichbar"],
  oeffentlich: ["Öffentlich", "Port muss am Router des Arbeitsplatzes weitergeleitet sein"],
  lokal: ["Nur lokal", "Loopback-Adresse – andere können nicht verbinden"],
  unbekannt: ["Unbekannt", "Adresse nicht erkannt"],
};

views.zusammenarbeit = async (el) => {
  const data = await api("/collab");
  const e = data.entries;
  const mixed = new Set(e.map((x) => x.scope)).size > 1;
  el.innerHTML = `
    <h2>Zusammenarbeit</h2>
    <p class="lead section">Wenn mehrere Personen gleichzeitig im selben Projekt arbeiten, verbinden sich die Resolve-Arbeitsplätze direkt miteinander. Jeder Arbeitsplatz öffnet dafür selbst einen Port und trägt seine Adresse in die Bibliothek ein.</p>

    <div class="table-wrap">
      <table>
        <thead><tr><th>Port</th><th>Wo</th><th>Wofür</th></tr></thead>
        <tbody>
          <tr><td><strong>${data.db_port}</strong> TCP</td><td>Dieser Server</td><td>Datenbank – alle Arbeitsplätze müssen ihn erreichen. Am Router weiterleiten.</td></tr>
          <tr><td><strong>50059</strong> TCP <span class="sub">bis 50314</span></td><td>Jeder Arbeitsplatz</td><td>Zusammenarbeit (Chat, Sperren, Aktualisierungen). Öffnet Resolve selbst, sobald ein Projekt im Kollaborationsmodus geladen ist. Ist 50059 belegt, nimmt Resolve den nächsten freien Port.</td></tr>
          <tr><td><strong>443</strong> / <strong>8543</strong> TCP</td><td>Dieser Server</td><td>Web-Oberfläche: 443 mit HTTPS über die Domain, 8543 unverschlüsselt nur im lokalen Netz.</td></tr>
        </tbody>
      </table>
    </div>

    <div class="two-col section">
      <div class="card">
        <h3>Alle im selben Büro</h3>
        <p class="hint">Funktioniert meist ohne Zutun. Nur die Firewall der Rechner muss Port 50059 für Resolve zulassen.</p>
      </div>
      <div class="card">
        <h3>Verteilt über das Internet</h3>
        <p class="hint">Resolve trägt die <em>interne</em> IP des Rechners ein – aus einem anderen Netz ist die nicht erreichbar. Zuverlässig klappt das, wenn alle Arbeitsplätze (und am besten auch dieser Server) im selben VPN sind, z. B. Tailscale oder WireGuard. Portweiterleitungen an einzelne Rechner sind fehleranfällig.</p>
      </div>
    </div>

    <section class="section">
      <div class="view-head">
        <h3>Angemeldete Arbeitsplätze</h3>
        <div class="actions"><button class="btn icon" id="refresh" title="Neu laden" aria-label="Neu laden">↻</button></div>
      </div>
      ${mixed ? `<p class="banner">Die Arbeitsplätze stehen in unterschiedlichen Netzen. Wer nicht im selben Netz oder VPN ist, kann die anderen nicht erreichen – Zusammenarbeit wird dann abbrechen.</p>` : ""}
      ${e.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Arbeitsplatz</th><th>Bibliothek</th><th>Adresse</th><th>Netz</th><th></th></tr></thead>
        <tbody>${e.map((x, i) => `
          <tr data-i="${i}">
            <td><strong>${esc(x.machine || "–")}</strong></td>
            <td>${esc(x.library)}</td>
            <td>${x.ip ? `${esc(x.ip)}${x.port ? `:${x.port}` : ""}` : "–"}</td>
            <td><span class="pill ${x.scope === "vpn" ? "live" : x.scope === "privat" ? "" : "cue"}" title="${esc(SCOPES[x.scope][1])}">${esc(SCOPES[x.scope][0])}</span></td>
            <td class="row-actions">${x.ip ? `<button class="btn small" data-act="test">Erreichbarkeit testen</button><span class="sub" data-result></span>` : ""}</td>
          </tr>`).join("")}</tbody></table></div>
        <p class="hint section">Der Test prüft, ob <em>dieser Server</em> den Port des Arbeitsplatzes erreicht. Das klappt nur, wenn Resolve dort gerade ein Projekt im Kollaborationsmodus geöffnet hat und der Server im selben Netz oder VPN ist. Die Einträge stammen direkt aus den Bibliotheken; ihr Aufbau ist von Blackmagic nicht dokumentiert, deshalb können einzelne Angaben fehlen.</p>`
      : `<div class="empty-state">Noch kein Arbeitsplatz hat sich für die Zusammenarbeit angemeldet. Einträge erscheinen, sobald jemand in Resolve ein Projekt einer Bibliothek dieses Servers öffnet.</div>`}
    </section>`;

  $("#refresh").onclick = render;
  $$("tr[data-i]", el).forEach((row) => {
    const x = e[+row.dataset.i];
    const b = $("[data-act=test]", row);
    if (!b) return;
    b.onclick = async () => {
      const out = $("[data-result]", row);
      b.disabled = true;
      out.textContent = " prüfe …";
      try {
        const r = await api("/collab/test", { method: "POST", body: { ip: x.ip, port: x.port || 50059 } });
        out.textContent = r.reachable ? ` erreichbar (${r.ms} ms)` : " nicht erreichbar";
      } catch (err) { out.textContent = ""; fail(err); }
      b.disabled = false;
    };
  });
};

/* ---------- Hilfe ---------- */
views.hilfe = async (el) => {
  const s = await api("/status");
  el.innerHTML = `
    <h2>Hilfe</h2>
    <div class="two-col section">
      <div class="card stack">
        <h3>Einen Arbeitsplatz verbinden</h3>
        <ol class="hint">
          <li>In der <a href="#/konfiguration">Konfiguration</a> die Zugangsadresse eintragen${s.public_host ? ` (aktuell <code>${esc(s.public_host)}</code>)` : ""}.</li>
          <li>Auf der Übersicht eine Bibliothek anlegen oder bei einer vorhandenen auf „Verbinden“ klicken.</li>
          <li>In Resolve den Projektmanager öffnen, „Projektbibliotheken“ aufklappen und eine Netzwerk-Bibliothek hinzufügen.</li>
          <li>Die angezeigten Werte eintragen oder den heruntergeladenen Zugangsschlüssel importieren.</li>
        </ol>
      </div>
      <div class="card stack">
        <h3>Wenn keine Verbindung zustande kommt</h3>
        <ul class="hint">
          <li>Am Router muss TCP-Port <code>${s.port}</code> auf diesen Server weitergeleitet sein.</li>
          <li>Für gleichzeitiges Arbeiten im selben Projekt siehe <a href="#/zusammenarbeit">Zusammenarbeit</a> (Port 50059).</li>
          <li>Im lokalen Netz ist diese Oberfläche auch unter <code>http://SERVER-IP:8543</code> erreichbar.</li>
          <li>Die Zugangsadresse muss von außen auf deine öffentliche IP zeigen.</li>
          <li>Benutzer ist bei eigenen Zugängen der Name der Bibliothek, nicht „postgres“.</li>
          <li>Mehr Sicherheit bietet ein VPN (z. B. WireGuard oder Tailscale) statt eines offenen Ports.</li>
        </ul>
      </div>
    </div>`;
};

/* ================= Router ================= */
async function render() {
  const [pathPart, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const [viewName, ...rest] = pathPart.split("/");
  const view = views[viewName] ? viewName : "start";
  const arg = rest.length ? decodeURIComponent(rest.join("/")) : null;
  $$(".nav > a").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  $("#nav-back").hidden = view === "start";
  $$("details.menu[open]").forEach((d) => (d.open = false));
  const el = $("#view");
  try { await views[view](el, arg, new URLSearchParams(query)); }
  catch (e) {
    el.innerHTML = `<div class="view-head"><h2>Nicht verfügbar</h2><div class="actions"><button class="btn" id="retry">Erneut laden</button></div></div>
      <p class="banner danger">${esc(e.message)}</p>`;
    $("#retry").onclick = render;
  }
}
window.addEventListener("hashchange", () => { render(); window.scrollTo(0, 0); });

async function start() {
  let acc;
  try { acc = await api("/account"); } catch (_) { return; }
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#user-name").textContent = acc.name || acc.username;
  loadBranding();
  // Laufende Aufgabe (z. B. nach Neuladen während einer Sicherung) wieder anzeigen
  api("/status").then((s) => { if (s.job) watchJob(s.job.id, () => render()); }).catch(() => {});
  render();
}
start();
