/* =========================================================================
   Moneten-Tracker — kleines Client-JS
   Aufgaben:
   1. Theme-Initialisierung: Wert aus localStorage > User-Pref > Default Dark
   2. Theme-Toggle-Buttons binden
   3. PIN-Pad auf der Login-Seite
   ========================================================================= */

(function () {
  "use strict";

  // -------- Theme ------------------------------------------------------
  // EINACHSIG: ein Theme-Name ("dark" | "light" | "nord" | …).
  // Die Liste steht NICHT hier, sondern kommt vom Server — jeder Umschalt-Knopf
  // trägt data-theme (Name), data-bg (Grundfarbe) und data-dark. So braucht ein
  // neues Theme keinerlei JS-Änderung. Registry: moneten/themes.py.
  function themeMeta(key) {
    const btn = document.querySelector('[data-theme-pick][data-theme="' + key + '"]');
    if (btn) return { bg: btn.dataset.bg, dark: btn.dataset.dark === "1" };
    // Fallback, falls kein Umschalter auf der Seite ist (z.B. Login).
    const root = document.documentElement;
    return { bg: root.style.backgroundColor || "#1A1917", dark: key !== "light" };
  }

  function applyTheme(theme) {
    const root = document.documentElement;
    const meta = themeMeta(theme);
    root.setAttribute("data-theme", theme);
    // Inline-Hintergrund mitziehen → kein Farbrand, wenn der Server-Wert abweicht.
    if (meta.bg) root.style.backgroundColor = meta.bg;
    const mc = document.querySelector('meta[name="theme-color"]');
    if (mc && meta.bg) mc.setAttribute("content", meta.bg);
    const cs = document.querySelector('meta[name="color-scheme"]');
    if (cs) cs.setAttribute("content", meta.dark ? "dark" : "light");
    try { localStorage.setItem("moneten.theme", theme); } catch (_) {}

    document.querySelectorAll("[data-theme-pick]").forEach((btn) => {
      const on = btn.dataset.theme === theme;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    const label = document.getElementById("active-theme-label");
    if (label) {
      const btn = document.querySelector('[data-theme-pick][data-theme="' + theme + '"]');
      label.textContent = (btn && btn.dataset.label) || theme;
    }
  }

  function saveTheme(theme) {
    fetch("/settings/theme", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "theme=" + encodeURIComponent(theme),
      credentials: "same-origin",
    }).catch(() => {});
  }

  function initTheme() {
    const serverTheme = document.documentElement.dataset.theme || "dark";
    // Nur Namen akzeptieren, die es auf dieser Seite wirklich gibt — sonst
    // würde ein entferntes Theme aus dem localStorage die App farblos lassen.
    const known = [...document.querySelectorAll("[data-theme-pick]")].map((b) => b.dataset.theme);
    let theme = null;
    try { theme = localStorage.getItem("moneten.theme"); } catch (_) {}
    if (!theme || (known.length && known.indexOf(theme) === -1)) theme = serverTheme;
    applyTheme(theme);
    // Selbst-Heilung: weicht die Client-Wahl vom gespeicherten Theme ab, einmal
    // persistieren → künftige server-gerenderte Seiten starten sofort korrekt.
    if (theme !== serverTheme) saveTheme(theme);

    document.querySelectorAll("[data-theme-pick]").forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => {
        applyTheme(btn.dataset.theme);
        saveTheme(btn.dataset.theme);
      });
    });
  }

  // -------- PIN-Pad ----------------------------------------------------
  function initPinPad() {
    const form = document.getElementById("pin-form");
    if (!form) return;

    const input = form.querySelector('input[name="pin"]');
    const dots  = form.querySelectorAll(".pin-dot");
    const errorBox = document.getElementById("login-error-box");

    function renderDots() {
      const len = input.value.length;
      dots.forEach((dot, i) => dot.classList.toggle("filled", i < len));
    }

    function submit() {
      if (errorBox) errorBox.innerHTML = "";
      form.requestSubmit();
    }

    form.querySelectorAll(".pin-key[data-digit]").forEach((key) => {
      key.addEventListener("click", () => {
        if (input.value.length >= 6) return;
        input.value += key.dataset.digit;
        renderDots();
        if (input.value.length === 6) submit();
      });
    });

    const backKey = form.querySelector('.pin-key[data-action="back"]');
    if (backKey) {
      backKey.addEventListener("click", () => {
        input.value = input.value.slice(0, -1);
        renderDots();
      });
    }

    // Hardware-Tastatur unterstützen.
    document.addEventListener("keydown", (e) => {
      if (e.key >= "0" && e.key <= "9") {
        if (input.value.length >= 6) return;
        input.value += e.key;
        renderDots();
        if (input.value.length === 6) submit();
      } else if (e.key === "Backspace") {
        input.value = input.value.slice(0, -1);
        renderDots();
      } else if (e.key === "Enter") {
        if (input.value.length === 6) submit();
      }
    });

    // Nach Fehler-Reload: Eingabefeld leeren.
    document.body.addEventListener("htmx:afterSwap", (e) => {
      if (e.target && e.target.id === "login-error-box") {
        input.value = "";
        renderDots();
      }
    });
  }

  // -------- Donut-Diagramme (Hover-Interaktivität, mehrere möglich) ----
  function bindDonut(wrap) {
    const big = wrap.querySelector(".dc-big");
    const label = wrap.querySelector(".dc-label");
    const totalBig = big ? big.textContent : "";
    const totalLabel = label ? label.textContent : "";
    const segs = wrap.querySelectorAll(".donut-seg");

    function activate(idx) {
      wrap.classList.add("dim");
      segs.forEach((s) => s.classList.toggle("is-active", s.dataset.idx === String(idx)));
      const seg = wrap.querySelector('.donut-seg[data-idx="' + idx + '"]');
      if (seg && big && label) {
        big.textContent = seg.dataset.balance;
        label.textContent = seg.dataset.name + " · " + seg.dataset.pct + "%";
      }
    }
    function reset() {
      wrap.classList.remove("dim");
      segs.forEach((s) => s.classList.remove("is-active"));
      if (big) big.textContent = totalBig;
      if (label) label.textContent = totalLabel;
    }

    segs.forEach((s) => {
      s.addEventListener("mouseenter", () => activate(s.dataset.idx));
      s.addEventListener("mouseleave", reset);
    });
    // Legenden-Einträge, die auf genau diesen Donut zeigen.
    document.querySelectorAll('.legend-item[data-target="' + wrap.id + '"]').forEach((it) => {
      it.addEventListener("mouseenter", () => activate(it.dataset.idx));
      it.addEventListener("mouseleave", reset);
    });
  }

  function initDonuts() {
    document.querySelectorAll(".donut-wrap").forEach(bindDonut);
  }

  // -------- Animationen (Eyecandy) -------------------------------------
  function prefersReducedMotion() {
    try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
    catch (_) { return false; }
  }
  // Animationen liefen früher NUR auf dem Desktop — der Grund war Layout-Wackeln
  // durch wachsenden Zahlentext. Das ist inzwischen doppelt gelöst: die Endbreite
  // wird vor dem Zählen reserviert, und alle Beträge laufen auf tabellarischen
  // Ziffern (gleich breite Zahlen). Am Handy war die App dadurch aber statisch,
  // ausgerechnet auf dem Gerät, auf dem sie hauptsächlich benutzt wird.
  //
  // `prefers-reduced-motion` bleibt selbstverständlich respektiert — das ist eine
  // bewusste Einstellung des Nutzers, keine Geräteklasse.
  const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);

  // CHF-Format exakt wie der Server-Filter: Apostroph-Tausender, 2 Dezimalen.
  function formatCHF(n) {
    const neg = n < 0;
    const fixed = Math.abs(n).toFixed(2);
    let [intPart, dec] = fixed.split(".");
    intPart = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, "'");
    return "CHF " + (neg ? "-" : "") + intPart + "." + dec;
  }

  // Beträge mit [data-countup] von 0 auf den Zielwert hochzählen.
  function initCountUp() {
    const els = document.querySelectorAll("[data-countup]");
    if (!els.length) return;
    // Mobile + Reduced-Motion: Zahl sofort final zeigen — kein Per-Frame-Reflow
    // (→ kein Vibrieren des Vermögens-Charts) und weniger CPU-Last.
    if (prefersReducedMotion()) return;
    els.forEach((el) => {
      const target = parseFloat(el.dataset.countup);
      if (isNaN(target)) return;
      const finalText = el.textContent;       // exakter Server-String
      // Endbreite reservieren, damit der wachsende Zahlentext keinen Reflow der
      // Nachbarelemente (Chart) auslöst → kein Wackeln auch auf dem Desktop.
      // Die reservierte Breite gilt NUR waehrend der Animation und wird danach
      // wieder entfernt. Sonst bleibt sie als Inline-Stil stehen und ueberlebt
      // jede Groessenaenderung: gemessen auf einem 360px-Schirm ein
      // `min-width: 697px` an der Summenzahl — die Breite stammte aus dem
      // Zustand, in dem die Seite geladen worden war. Auf einem Faltgeraet ist
      // das der Normalfall, nicht die Ausnahme.
      const w = el.getBoundingClientRect().width;
      if (w) el.style.minWidth = Math.ceil(w) + "px";
      const breiteFreigeben = () => { el.style.minWidth = ""; };
      const dur = 950;
      // Rueckfall per Uhr, nicht per Zeichentakt: `requestAnimationFrame`
      // PAUSIERT in einem Hintergrund-Tab. Wer die App oeffnet und sofort
      // wegwischt, dessen Animation laeuft nie zu Ende — und die reservierte
      // Breite bliebe stehen. Gemessen: 10 von 14 Zahlen behielten sie.
      // `setTimeout` laeuft weiter, nur langsamer; die Zahl steht ohnehin schon
      // richtig im Text, es geht allein um das Aufraeumen.
      setTimeout(breiteFreigeben, dur + 200);
      let started = null;
      function frame(now) {
        if (started === null) started = now;
        const t = Math.min(1, (now - started) / dur);
        el.textContent = formatCHF(target * easeOutCubic(t));
        if (t < 1) { requestAnimationFrame(frame); return; }
        el.textContent = finalText;            // exakt = Server-Format
        breiteFreigeben();
      }
      requestAnimationFrame(frame);
    });
  }

  // Donut-Segmente beim Laden aufzeichnen (stroke-dasharray 0 → Ziel).
  function initDonutDraw() {
    const wraps = document.querySelectorAll(".donut-wrap");
    if (!wraps.length || prefersReducedMotion()) return;
    wraps.forEach((wrap) => {
      const segs = Array.prototype.slice.call(wrap.querySelectorAll(".donut-seg"));
      if (!segs.length) return;
      segs.forEach((s) => {
        const parts = (s.getAttribute("stroke-dasharray") || "").split(/\s+/);
        s._dash = parseFloat(parts[0]) || 0;
        s._gap = parseFloat(parts[1]) || 0;
        s.setAttribute("stroke-dasharray", "0 " + (s._dash + s._gap));
      });
      const dur = 850;
      let started = null;
      function frame(now) {
        if (started === null) started = now;
        const e = easeOutCubic(Math.min(1, (now - started) / dur));
        segs.forEach((s) => {
          const cur = s._dash * e;
          s.setAttribute("stroke-dasharray", cur + " " + (s._dash + s._gap - cur));
        });
        if (e < 1) requestAnimationFrame(frame);
        else segs.forEach((s) => s.setAttribute("stroke-dasharray", s._dash + " " + s._gap));
      }
      requestAnimationFrame(frame);
    });
  }

  // -------- Quick-Add: Schnell-Pills setzen Kategorie + Konto ----------
  function initQuickPills() {
    const hidden = document.getElementById("quick-cat");   // verstecktes category_id
    if (!hidden) return;
    const amount = document.getElementById("quick-amount");
    const acc = document.getElementById("quick-acc");
    // Alle Kategorie-Pillen (häufige + Grid) markieren die aktuelle Auswahl.
    const allCatPills = document.querySelectorAll("#quick-catgrid .cat-pill, .quick-pill[data-cat]");
    function select(catId) {
      hidden.value = catId || "";
      allCatPills.forEach((b) => b.classList.toggle("active", (b.dataset.cat || "") === (catId || "")));
    }
    // Häufige Kombi-Pillen: setzen Kategorie + Konto.
    document.querySelectorAll(".quick-pill[data-cat]").forEach((pill) => {
      pill.addEventListener("click", () => {
        select(pill.dataset.cat);
        if (acc && pill.dataset.acc) acc.value = pill.dataset.acc;
        if (amount) amount.focus();
      });
    });
    // Grid-Pillen: setzen nur die Kategorie.
    document.querySelectorAll("#quick-catgrid .cat-pill").forEach((pill) => {
      pill.addEventListener("click", () => { select(pill.dataset.cat); });
    });
  }

  // -------- WebAuthn / Passkeys ---------------------------------------
  // Base64url <-> ArrayBuffer (WebAuthn arbeitet mit Binärdaten, der Server
  // mit base64url-Strings).
  function b64urlToBuf(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    const pad = s.length % 4 ? "=".repeat(4 - (s.length % 4)) : "";
    const bin = atob(s + pad);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  }
  function bufToB64url(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (const b of bytes) bin += String.fromCharCode(b);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  async function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  }
  function setMsg(el, text, ok) {
    if (!el) return;
    el.textContent = text;
    el.style.color = ok ? "var(--accent-tertiary)" : "var(--danger)";
  }

  // Die PIN aus dem Feld daneben. Das Anlegen eines Passkeys verlangt sie, weil
  // der Passkey die PIN danach ersetzt — siehe auth/webauthn.py.
  function waPin() {
    const el = document.getElementById("wa-pin");
    return el ? el.value.trim() : "";
  }

  async function waAnzahlZeigen() {
    const el = document.getElementById("wa-anzahl");
    if (!el) return;
    try {
      const r = await fetch("/auth/webauthn/registered");
      el.textContent = r.ok ? String((await r.json()).count) : "–";
    } catch (e) { el.textContent = "–"; }
  }

  async function registerPasskey(msgEl) {
    if (!window.PublicKeyCredential) { setMsg(msgEl, "Dieser Browser unterstützt keine Passkeys.", false); return; }
    const pin = waPin();
    if (!/^[0-9]{6}$/.test(pin)) { setMsg(msgEl, "Bitte zuerst die aktuelle PIN eingeben.", false); return; }
    try {
      const begin = await postJSON("/auth/webauthn/register/begin", { pin });
      if (!begin.ok) {
        setMsg(msgEl, begin.status === 429
          ? "Zu viele Fehlversuche. Bitte ein paar Minuten warten."
          : "PIN stimmt nicht.", false);
        return;
      }
      const opts = await begin.json();
      opts.challenge = b64urlToBuf(opts.challenge);
      opts.user.id = b64urlToBuf(opts.user.id);
      (opts.excludeCredentials || []).forEach((c) => { c.id = b64urlToBuf(c.id); });
      const cred = await navigator.credentials.create({ publicKey: opts });
      const body = {
        id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
        response: {
          clientDataJSON: bufToB64url(cred.response.clientDataJSON),
          attestationObject: bufToB64url(cred.response.attestationObject),
        },
      };
      const r = await postJSON("/auth/webauthn/register/complete", body);
      setMsg(msgEl, r.ok ? "✓ Passkey gespeichert. Du kannst dich jetzt damit anmelden." : "Registrierung fehlgeschlagen.", r.ok);
      const feld = document.getElementById("wa-pin");
      if (feld) feld.value = "";
      waAnzahlZeigen();
    } catch (e) {
      setMsg(msgEl, "Abgebrochen oder fehlgeschlagen.", false);
    }
  }

  // Laufende Passkey-Abfrage, damit eine zweite sie abloesen kann. Ohne das
  // scheitert der Knopf mit „operation already in progress", solange die
  // automatische Abfrage vom Seitenaufbau noch offen steht.
  let waLaufend = null;

  function waAbbrechen() {
    if (waLaufend) { try { waLaufend.abort(); } catch (e) { /* egal */ } waLaufend = null; }
  }

  async function loginPasskey(msgEl, automatisch) {
    if (!window.PublicKeyCredential) {
      if (!automatisch) setMsg(msgEl, "Dieser Browser unterstützt keine Passkeys.", false);
      return;
    }
    try {
      const begin = await postJSON("/auth/webauthn/authenticate/begin");
      if (!begin.ok) {
        // Kein Passkey hinterlegt: von Hand ist das eine Auskunft, automatisch
        // waere es eine Fehlermeldung fuer etwas, das niemand angestossen hat.
        if (!automatisch) setMsg(msgEl, "Noch kein Passkey registriert — bitte mit PIN anmelden.", false);
        return;
      }
      const opts = await begin.json();
      opts.challenge = b64urlToBuf(opts.challenge);
      (opts.allowCredentials || []).forEach((c) => { c.id = b64urlToBuf(c.id); });
      waAbbrechen();
      const ctl = new AbortController();
      waLaufend = ctl;
      const cred = await navigator.credentials.get({ publicKey: opts, signal: ctl.signal });
      const body = {
        id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
        response: {
          clientDataJSON: bufToB64url(cred.response.clientDataJSON),
          authenticatorData: bufToB64url(cred.response.authenticatorData),
          signature: bufToB64url(cred.response.signature),
          userHandle: cred.response.userHandle ? bufToB64url(cred.response.userHandle) : null,
        },
      };
      const r = await postJSON("/auth/webauthn/authenticate/complete", body);
      waLaufend = null;
      if (r.ok) { window.location.href = "/"; }
      else { setMsg(msgEl, "Login fehlgeschlagen.", false); }
    } catch (e) {
      waLaufend = null;
      if (e && e.name === "AbortError") return;   // von einer neuen Abfrage abgeloest
      if (automatisch) {
        // Er hat den Dialog weggewischt (oder das Geraet konnte nicht). Kein
        // Rot fuer etwas, das er nicht angestossen hat — und in DIESER Sitzung
        // nicht noch einmal von selbst aufpoppen, sonst kaempft er dagegen an.
        try { sessionStorage.setItem("wa-kein-auto", "1"); } catch (err) { /* egal */ }
        setMsg(msgEl, "Mit PIN anmelden oder unten auf Passkey tippen.", true);
        if (msgEl) msgEl.style.color = "var(--text-tertiary)";
        return;
      }
      setMsg(msgEl, "Abgebrochen oder fehlgeschlagen.", false);
    }
  }

  /* Passkey OHNE Knopfdruck, sobald die Anmeldeseite aufgeht.
     Das ist der Normalfall am Handy: die App startet, die Sitzung ist
     abgelaufen, und der Fingerabdruck soll sofort kommen.

     Vier Bedingungen, jede aus einem eigenen Grund:

     * `data-auto-passkey` am Markup — der Server setzt es NICHT, wenn gerade
       abgemeldet wurde. Wer sich abmeldet, will nicht im selben Atemzug wieder
       angemeldet werden.
     * `isUserVerifyingPlatformAuthenticatorAvailable()` — ohne eingebautes
       Verfahren (Fingerabdruck/Gesicht) waere der Dialog eine Sackgasse. Auf
       einem Desktop ohne Sensor bleibt es damit beim Knopf.
     * kein Merker aus dieser Sitzung — wer den Dialog einmal weggewischt hat,
       bekommt ihn beim naechsten Seitenaufbau nicht wieder vorgesetzt.
     * ein registrierter Passkey; das prueft `loginPasskey` selbst am 400 von
       `/authenticate/begin`.

     Bewusst KEIN `mediation: "conditional"`: das zeigt den Passkey nur als
     Vorschlag in der Tastatur-Zeile, und am Handy erst, wenn man ein Feld
     antippt — also genau der Knopfdruck, der wegfallen sollte. */
  async function autoPasskey() {
    const shell = document.querySelector("[data-auto-passkey]");
    if (!shell || shell.dataset.autoPasskey !== "1") return;
    if (!window.PublicKeyCredential) return;
    try { if (sessionStorage.getItem("wa-kein-auto")) return; } catch (e) { /* egal */ }
    try {
      const da = await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
      if (!da) return;
    } catch (e) { return; }
    loginPasskey(document.getElementById("wa-login-msg"), true);
  }

  function initWebAuthn() {
    const reg = document.getElementById("wa-register");
    if (reg) reg.addEventListener("click", () => registerPasskey(document.getElementById("wa-msg")));

    const weg = document.getElementById("wa-entfernen");
    if (weg) weg.addEventListener("click", async () => {
      const msgEl = document.getElementById("wa-msg");
      const pin = waPin();
      if (!/^[0-9]{6}$/.test(pin)) { setMsg(msgEl, "Bitte zuerst die aktuelle PIN eingeben.", false); return; }
      const r = await postJSON("/auth/webauthn/entfernen", { pin });
      if (!r.ok) {
        setMsg(msgEl, r.status === 429
          ? "Zu viele Fehlversuche. Bitte ein paar Minuten warten."
          : "PIN stimmt nicht.", false);
        return;
      }
      const { entfernt } = await r.json();
      setMsg(msgEl, entfernt ? `✓ ${entfernt} Passkey(s) entfernt.` : "Es war keiner eingerichtet.", true);
      const feld = document.getElementById("wa-pin");
      if (feld) feld.value = "";
      waAnzahlZeigen();
    });

    waAnzahlZeigen();
    const login = document.getElementById("wa-login");
    if (login) {
      login.addEventListener("click", () => {
        try { sessionStorage.removeItem("wa-kein-auto"); } catch (e) { /* egal */ }
        loginPasskey(document.getElementById("wa-login-msg"), false);
      });
    }
    // Wer anfaengt, die PIN zu tippen, hat sich entschieden: die offene
    // Passkey-Abfrage wird beendet, damit sie das Feld nicht blockiert.
    document.querySelectorAll(".pin-key").forEach((k) => {
      k.addEventListener("click", waAbbrechen, { once: true });
    });
    autoPasskey();
  }

  // -------- Quittungs-Zuordnen: Kandidaten-Dropdown filtern -----------
  function initCandFilter(root) {
    root = root || document;
    root.querySelectorAll(".cand-filter").forEach((inp) => {
      if (inp.dataset.bound) return;
      inp.dataset.bound = "1";
      inp.addEventListener("input", () => {
        const form = inp.closest("form");
        const sel = form && form.querySelector(".cand-select");
        if (!sel) return;
        const term = inp.value.trim().toLowerCase();
        let firstVisible = null;
        Array.from(sel.options).forEach((o) => {
          const match = !term || o.text.toLowerCase().includes(term);
          o.hidden = !match;
          if (match && firstVisible === null) firstVisible = o;
        });
        // Wenn die aktuelle Auswahl ausgeblendet ist, auf den ersten Treffer springen.
        if (sel.selectedOptions[0] && sel.selectedOptions[0].hidden && firstVisible) {
          firstVisible.selected = true;
        }
      });
    });
  }

  // -------- Kategorie-Pill-Picker: EIN Panel für die ganze Seite --------
  // Früher trug jeder Auslöser sein eigenes Panel. In der Buchungsliste ist das
  // unbezahlbar (54 Kategorien × 81 Zeilen ≈ 1136 KB Markup), also lieferte der
  // Server das aufgeklappte Panel für GENAU EINE Zeile nach (`?quickcat=<id>`).
  // Damit stand „offen" nur im URL-Parameter: die nächste Antwort für
  // #transactions-root — nachlaufende Suche (400 ms), Filterwechsel, zweiter
  // Klick — rendert ohne quickcat, und der Picker war weg. Bei 2512 Buchungen
  // dauert der Roundtrip lange genug, dass das regelmässig eintritt; mit 81
  // Zeilen und 15 ms war es lokal nicht zu sehen.
  // Jetzt: EIN #cat-panel in der Shell, ausserhalb jedes HTMX-Ziels. Es merkt
  // sich per data-picker-key, FÜR WEN es offen ist, sucht seinen Auslöser nach
  // einem Neurendern wieder und richtet sich neu aus. Nur wenn die Zeile aus der
  // Liste gefallen ist, schliesst es — offen ohne Bezug wäre eine Lüge.
  let _catBound = false;
  let _catOwner = null;      // .cat-picker, zu dem das Panel gerade gehört
  let _catOwnerKey = null;   // dessen data-picker-key (überlebt das Neurendern)
  let _catAllowNone = true;

  function catPanel() { return document.getElementById("cat-panel"); }
  function catInput(pk) { return pk ? pk.querySelector("input[type=hidden]") : null; }
  function catBtn(pk) { return pk ? pk.querySelector("button") : null; }

  function catClose() {
    const panel = catPanel();
    if (panel) panel.hidden = true;
    _catOwner = null;
    _catOwnerKey = null;
  }

  // Panel (position:fixed) so platzieren, dass es komplett im Viewport bleibt und
  // NICHT von einem overflow:hidden-Vorfahren (z.B. .month-card) geclippt wird:
  // horizontal an der Kante des Auslösers, ins Bild geklemmt; vertikal nach unten
  // — oder nach oben, wenn unten zu wenig Platz ist. Höhe begrenzt + scrollbar.
  function catPlace() {
    const panel = catPanel();
    const btn = catBtn(_catOwner);
    if (!panel || !btn) return;
    const r = btn.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    panel.style.right = "auto";
    const pw = panel.offsetWidth || Math.min(680, vw * 0.92);
    let left = r.left;
    if (left + pw > vw - 8) left = vw - 8 - pw;
    if (left < 8) left = 8;
    panel.style.left = Math.round(left) + "px";
    const below = vh - r.bottom - 14;
    const above = r.top - 14;
    const cap = Math.round(vh * 0.82);
    if (below < 280 && above > below) {
      panel.style.top = "auto";
      panel.style.bottom = Math.round(vh - r.top + 6) + "px";
      panel.style.maxHeight = Math.min(above, cap) + "px";
    } else {
      panel.style.bottom = "auto";
      panel.style.top = Math.round(r.bottom + 6) + "px";
      panel.style.maxHeight = Math.min(Math.max(below, 220), cap) + "px";
    }
  }

  // Ist das Element wirklich sichtbar — nicht bloss im DOM?
  // checkVisibility() ist hier der einzige verlässliche Test. Eine zugeklappte
  // Monatskarte ist ein geschlossenes <details>: dessen Inhalt wird über
  // content-visibility übersprungen, BEHÄLT aber seine zuletzt berechnete Box.
  // Am laufenden Server nachgemessen: getClientRects().length === 1,
  // offsetParent gesetzt, getBoundingClientRect() 99×27 — alle drei lügen, nur
  // checkVisibility() sagt false. Verworfen deshalb: offsetParent (meldet
  // zusätzlich bei position:fixed null) und ein Test auf document.contains()
  // (genau das war ja der Fehler). getClientRects bleibt nur Notnagel für
  // Browser ohne checkVisibility.
  function catSichtbar(el) {
    if (!el || !el.isConnected) return false;
    if (typeof el.checkVisibility === "function") {
      return el.checkVisibility({ checkVisibilityCSS: true });
    }
    return el.getClientRects().length > 0;
  }

  // Tastatur-Schnellsuche: tippen filtert die Pillen live; "so" → Software.
  // Rückgabe: { treffer, anzahl } — die ANZAHL braucht der Enter-Handler, um
  // „eindeutig" von „erstbester" zu unterscheiden.
  function catFilter(q) {
    const panel = catPanel();
    if (!panel) return { treffer: null, anzahl: 0 };
    q = (q || "").trim().toLowerCase();
    let firstVisible = null, anzahl = 0;
    panel.querySelectorAll(".cat-pill").forEach((pill) => {
      const isNone = pill.classList.contains("cat-picker-none");
      const lbl = (pill.dataset.label || pill.textContent || "").toLowerCase();
      const match = (!q || lbl.indexOf(q) !== -1) && (!isNone || _catAllowNone);
      pill.style.display = match ? "" : "none";
      if (match && !isNone) { anzahl++; if (!firstVisible) firstVisible = pill; }
    });
    panel.querySelectorAll(".cat-pill-row").forEach((row) => {
      const vis = Array.prototype.some.call(row.querySelectorAll(".cat-pill"), (p) => p.style.display !== "none");
      const header = row.previousElementSibling;
      if (header && header.classList.contains("cat-picker-group")) header.style.display = vis ? "" : "none";
      row.style.display = vis ? "" : "none";
    });
    const emptyMsg = panel.querySelector(".cat-picker-empty");
    if (emptyMsg) emptyMsg.hidden = anzahl > 0 || !q;
    panel.querySelectorAll(".cat-pill.is-hl").forEach((p) => p.classList.remove("is-hl"));
    // Hervorgehoben wird nur, was Enter auch wirklich wählt. Die frühere
    // Hervorhebung des ERSTEN von vielen Treffern war ein Versprechen, das
    // Enter nach dieser Korrektur nicht mehr einlöst.
    if (q && anzahl === 1) firstVisible.classList.add("is-hl");
    return { treffer: firstVisible, anzahl: anzahl };
  }

  // Enter im Suchfeld des Panels darf nur wählen, wenn WIRKLICH gefiltert wurde
  // und genau eine Kategorie übrig ist. Vorher nahm Enter schlicht das erste
  // sichtbare Element — bei leerer Eingabe war das die erste Kategorie der
  // Liste, und ein Enter ohne einen einzigen Tastendruck davor speicherte sie
  // still an der Buchung.
  function catEindeutigerTreffer(q) {
    const roh = (q || "").trim();
    const stand = catFilter(roh);
    return (roh && stand.anzahl === 1) ? stand.treffer : null;
  }

  function catOpen(pk) {
    const panel = catPanel();
    if (!panel || !pk) return;
    _catOwner = pk;
    _catOwnerKey = pk.dataset.pickerKey || null;
    _catAllowNone = pk.dataset.allowNone !== "0";
    // Die „keine/alle"-Pille gehört dem Auslöser, nicht dem Panel: im Filter
    // heisst sie „Alle Kategorien", in der Buchungszeile „— keine —".
    const none = panel.querySelector(".cat-picker-none");
    if (none) {
      const lbl = pk.dataset.noneLabel || "Alle Kategorien";
      none.dataset.label = lbl;
      none.textContent = lbl;
    }
    const input = catInput(pk);
    const val = input ? (input.value || "") : "";
    panel.querySelectorAll(".cat-pill").forEach((p) => {
      p.classList.toggle("active", (p.dataset.cat || "") === val);
    });
    panel.hidden = false;
    catPlace();
    const search = panel.querySelector(".cat-picker-search");
    if (search) { search.value = ""; catFilter(""); search.focus(); }
  }

  function catChoose(pill) {
    const pk = _catOwner;
    catClose();
    if (!pk || !document.contains(pk)) return;
    const input = catInput(pk);
    if (!input) return;
    input.value = pill.dataset.cat || "";
    const label = pk.querySelector(".cat-picker-label");
    if (label) {
      label.textContent = pill.dataset.label || pill.textContent.trim();
    } else {
      // Auslöser ohne eigenes Beschriftungsfeld (Buchungszeile): dort IST der
      // Knopf die Anzeige. Er übernimmt die Pille samt Icon, damit die neue
      // Kategorie sofort dasteht — die Antwort mit der neu gerenderten Liste
      // braucht bei 2512 Buchungen zu lange, um als „sofort" durchzugehen.
      const btn = catBtn(pk);
      if (btn) {
        btn.innerHTML = pill.innerHTML;
        btn.classList.toggle("is-empty", !(pill.dataset.cat || ""));
      }
    }
    // Vertrag zum Server, unverändert: HTMX hängt am "change" des Hidden-Inputs.
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function initCatPicker(root) {
    root = root || document;
    const panel = catPanel();
    if (panel && !_catBound) {
      _catBound = true;
      // Klick ausserhalb von Panel UND Auslöser → schliessen.
      document.addEventListener("click", (e) => {
        if (panel.hidden) return;
        if (panel.contains(e.target)) return;
        if (_catOwner && _catOwner.contains(e.target)) return;
        catClose();
      });
      // Panel ist position:fixed → beim Scrollen der Seite würde es „kleben".
      // Es wird deshalb NEU POSITIONIERT statt geschlossen: vorher verlor man bei
      // der kleinsten Scrollbewegung die Auswahl und musste von vorn anfangen —
      // besonders ärgerlich am Handy, wo Tippen und Scrollen ineinander übergehen.
      window.addEventListener("scroll", (e) => {
        if (panel.hidden) return;
        if (e.target === panel || (e.target instanceof Node && panel.contains(e.target))) return;
        catPlace();
      }, true);
      panel.addEventListener("click", (e) => {
        const pill = e.target.closest ? e.target.closest(".cat-pill") : null;
        if (!pill) return;
        e.preventDefault();
        catChoose(pill);
      });
      const search = panel.querySelector(".cat-picker-search");
      if (search) {
        search.addEventListener("input", () => catFilter(search.value));
        search.addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            const treffer = catEindeutigerTreffer(search.value);
            if (treffer) catChoose(treffer);
          } else if (e.key === "Escape") {
            e.preventDefault();
            const btn = catBtn(_catOwner);
            catClose();
            if (btn) btn.focus();
          }
        });
      }
    }

    // DER eigentliche Fehler von früher: hier wird ein offenes Panel nach dem
    // Neurendern wieder an seinen Auslöser gehängt statt geschlossen. Läuft VOR
    // dem Binden, damit ein frisch eingeswappter autoopen-Picker (Inbox) das
    // letzte Wort behält.
    if (panel && !panel.hidden) {
      const neu = _catOwnerKey
        ? document.querySelector('.cat-picker[data-picker-key="' + _catOwnerKey + '"]')
        : (_catOwner && document.contains(_catOwner) ? _catOwner : null);
      // Geprüft wird SICHTBARKEIT, nicht blosse Existenz. Bei einer zugeklappten
      // Monatskarte steht der Auslöser weiterhin im DOM — das Panel hing dann an
      // etwas Unsichtbarem und schwebte verwaist über der Seite, ohne dass man
      // sah, für welche Zeile es gilt.
      if (neu && catSichtbar(catBtn(neu) || neu)) { _catOwner = neu; catPlace(); } else { catClose(); }
    }

    root.querySelectorAll(".cat-picker").forEach((pk) => {
      if (pk.dataset.bound) return;
      pk.dataset.bound = "1";
      const btn = catBtn(pk);
      if (!btn || !catInput(pk)) return;
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        const p = catPanel();
        if (p && !p.hidden && _catOwner === pk) catClose(); else catOpen(pk);
      });
      // Direkt aufklappen (z.B. Inbox „zuordnen" → Auswahl erscheint sofort statt
      // erst der eingeklappte Knopf). Greift, sobald der Auslöser neu kam.
      if (pk.dataset.autoopen === "1") catOpen(pk);
    });
  }

  // -------- Massen-Zuweisung: kein Knopf, der garantiert scheitert ---------
  // Ohne Zielkategorie antwortet /transactions/assign-filtered mit 400. Ein
  // Knopf, der das anbietet, ist eine Einladung in einen Fehler. Serverseitig
  // steht das `disabled` schon im Markup (auch ohne JS greift der Schutz); hier
  // wird es aufgehoben, sobald eine Kategorie gewählt ist — der Picker feuert
  // dazu ein `change` am Hidden-Input, dasselbe Ereignis, an dem sonst HTMX hängt.
  function initBulkGuard() {
    const bar = document.getElementById("tx-bulk");
    if (!bar || bar.dataset.guard) return;
    bar.dataset.guard = "1";
    const knopf = bar.querySelector(".tx-bulk-go");
    const feld = bar.querySelector('input[name="assign_category_id"]');
    if (!knopf || !feld) return;
    const hinweis = bar.querySelector("#tx-bulk-hint");
    // Die zweite Sperre (keine Treffer) bleibt beim Server: sie hängt an Zahlen,
    // die nur er kennt, und darf von JS nicht überstimmt werden.
    const hatTreffer = (knopf.dataset.ziel || "0") !== "0";
    const setzen = () => {
      const gewaehlt = !!(feld.value || "").trim();
      knopf.disabled = !(hatTreffer && gewaehlt);
      if (hinweis) hinweis.hidden = gewaehlt;
    };
    feld.addEventListener("change", setzen);
    setzen();
  }

  // -------- Schnell-Zuordnen-Inbox: Gruppen-Suche + Mehrfachauswahl --------
  function initInboxGroup(root) {
    root = root || document;
    // Kopfzeile = Aufklapp-Schalter (kein <details> mehr): Klick toggelt die
    // zugehörige .inbox-expand und dreht den Chevron (aria-expanded).
    root.querySelectorAll(".inbox-summary").forEach((btn) => {
      if (btn.dataset.ibxBound) return;
      btn.dataset.ibxBound = "1";
      const exp = btn.nextElementSibling;
      btn.addEventListener("click", () => {
        const open = btn.getAttribute("aria-expanded") === "true";
        btn.setAttribute("aria-expanded", open ? "false" : "true");
        if (exp && exp.classList.contains("inbox-expand")) exp.hidden = open;
      });
    });
    root.querySelectorAll(".inbox-expand").forEach((det) => {
      if (det.dataset.ibxBound) return;
      det.dataset.ibxBound = "1";
      const search = det.querySelector(".inbox-search");
      const selall = det.querySelector(".inbox-selall-cb");
      const bulk = det.querySelector(".inbox-bulk");
      const countEl = bulk ? bulk.querySelector(".inbox-bulk-count strong") : null;
      const rows = () => Array.prototype.slice.call(det.querySelectorAll(".inbox-tx-light"));

      function update() {
        let n = 0;
        rows().forEach((r) => {
          const cb = r.querySelector(".inbox-cb");
          if (cb && cb.checked && r.style.display !== "none") n += 1;
        });
        if (countEl) countEl.textContent = n;
        if (bulk) bulk.hidden = n === 0;
      }
      if (search) {
        search.addEventListener("input", () => {
          const q = search.value.trim().toLowerCase();
          rows().forEach((r) => {
            const m = !q || (r.dataset.desc || "").indexOf(q) !== -1;
            r.style.display = m ? "" : "none";
            if (!m) { const cb = r.querySelector(".inbox-cb"); if (cb) cb.checked = false; }
          });
          if (selall) selall.checked = false;
          update();
        });
      }
      if (selall) {
        selall.addEventListener("change", () => {
          rows().forEach((r) => {
            if (r.style.display === "none") return;
            const cb = r.querySelector(".inbox-cb");
            if (cb) cb.checked = selall.checked;
          });
          update();
        });
      }
      det.addEventListener("change", (e) => {
        if (e.target && e.target.classList && e.target.classList.contains("inbox-cb")) update();
      });
      // KUGELSICHER: vor dem Absenden des Bulk-Requests JEDE ausgeblendete (gefilterte)
      // Buchung abwählen — so kann NIEMALS eine unsichtbare/veraltete Auswahl mitgehen,
      // egal wie sie markiert wurde. Capture-Phase → läuft GARANTIERT vor HTMX.
      const bulkBtn = bulk ? bulk.querySelector("button[hx-post]") : null;
      if (bulkBtn) {
        bulkBtn.addEventListener("click", () => {
          rows().forEach((r) => {
            if (r.style.display === "none") {
              const cb = r.querySelector(".inbox-cb");
              if (cb) cb.checked = false;
            }
          });
        }, true);
      }
    });
  }

  // -------- Sparklines interaktiv: Hover zeigt Monat/Jahr + Betrag -----
  function initSparkTooltip(root) {
    root = root || document;
    let tip = document.getElementById("spark-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.id = "spark-tip";
      tip.className = "spark-tip";
      tip.style.display = "none";
      document.body.appendChild(tip);
    }
    root.querySelectorAll(".spark-interactive").forEach((svg) => {
      if (svg.dataset.bound) return;
      svg.dataset.bound = "1";
      let pts = [];
      try { pts = JSON.parse(svg.dataset.points || "[]"); } catch (_) { pts = []; }
      if (!pts.length) return;
      const dot = svg.querySelector(".spark-hover-dot");
      const vline = svg.querySelector(".spark-hover-line");
      // viewBox-Breite auslesen (Sparkline 116, Vermögens-Chart 620 …) statt fest.
      const vb = svg.viewBox && svg.viewBox.baseVal;
      const VBW = (vb && vb.width) ? vb.width : 116;
      // Beim Prognose-Chart kann der Stresstest die Skala (und damit die Punkt-Y)
      // live ändern → bei jedem Move frisch einlesen, sonst sitzt der Hover daneben.
      // Sparklines ändern sich nie → einmaliges Parsen reicht.
      const dynamic = svg.classList.contains("forecast-chart");
      svg.addEventListener("mousemove", (e) => {
        let cur = pts;
        if (dynamic) { try { const j = JSON.parse(svg.dataset.points || "[]"); if (j.length) cur = j; } catch (_) {} }
        const rect = svg.getBoundingClientRect();
        if (!rect.width) return;
        const vbx = (e.clientX - rect.left) / rect.width * VBW;
        let best = cur[0], bd = Infinity;
        for (const p of cur) { const d = Math.abs(p.x - vbx); if (d < bd) { bd = d; best = p; } }
        if (dot) { dot.setAttribute("cx", best.x); dot.setAttribute("cy", best.y); dot.style.display = ""; }
        if (vline) { vline.setAttribute("x1", best.x); vline.setAttribute("x2", best.x); vline.style.display = ""; }
        tip.textContent = best.label + " · " + best.value;
        tip.style.display = "block";
        tip.style.left = e.clientX + "px";
        tip.style.top = (rect.top - 8) + "px";
      });
      svg.addEventListener("mouseleave", () => {
        tip.style.display = "none";
        if (dot) dot.style.display = "none";
        if (vline) vline.style.display = "none";
      });
    });
  }

  // -------- Vermögens-Verlauf: Führungslinie + Werte-Kasten ------------
  // Sieben Linien in einem Bild sind ohne Ablesehilfe nicht auswertbar. Kein
  // Fremd-Diagramm — die App ist offline. Muster wie initSparkTooltip
  // (dataset.bound als Wächter, Wurzel aus dem HTMX-Swap), nur mit mehreren
  // Reihen und mit Zeigerereignissen statt mousemove: am Handy gibt es keinen
  // Hover, und dieselbe Logik muss dort auf Berührung laufen.
  //
  // Alle Beträge kommen fertig formatiert aus data-monate. Im Browser gäbe es
  // weder Decimal noch das Schweizer Apostroph umsonst — gerechnet wird hier
  // nichts, nur zugeordnet.
  function initVerlaufHover(root) {
    (root || document).querySelectorAll(".nw-canvas[data-monate]").forEach((canvas) => {
      if (canvas.dataset.bound) return;
      canvas.dataset.bound = "1";
      let monate = [];
      try { monate = JSON.parse(canvas.dataset.monate || "[]"); } catch (_) { monate = []; }
      if (!monate.length) return;

      const svg = canvas.querySelector(".nw-chart");
      const vb = svg && svg.viewBox && svg.viewBox.baseVal;
      const VBW = (vb && vb.width) || 620;
      const VBH = (vb && vb.height) || 160;
      const tip = canvas.querySelector(".nw-tip");
      const monatEl = tip ? tip.querySelector(".nw-tip-monat") : null;
      const werte = tip ? tip.querySelectorAll(".nw-tip-wert") : [];
      const fuehrung = canvas.querySelector(".nw-guide");
      const punkte = canvas.querySelectorAll(".nw-hdot:not(.nw-hdot-total)");
      const gesamtPunkt = canvas.querySelector(".nw-hdot-total");
      const gesamtLinie = canvas.querySelector(".nw-line");
      const kontur = canvas.querySelector(".nw-line-kontur");
      const kontoLinien = canvas.querySelectorAll(".nw-acc-line");
      const karte = canvas.closest(".nw-card");
      const feinzeiger = window.matchMedia("(hover: hover)").matches;
      let offen = false;
      let gedrueckt = false;

      // Marken sitzen als HTML über dem SVG: ein <circle> im mit
      // preserveAspectRatio=none gestreckten viewBox wäre eine Ellipse.
      function setze(el, x, y) {
        if (!el) return;
        el.style.left = (x / VBW * 100) + "%";
        if (y !== undefined) el.style.top = (y / VBH * 100) + "%";
        el.hidden = false;
      }

      function zeige(clientX) {
        const rect = canvas.getBoundingClientRect();
        if (!rect.width) return;
        const vx = (clientX - rect.left) / rect.width * VBW;
        let treffer = 0, naechster = Infinity;
        for (let i = 0; i < monate.length; i++) {
          const d = Math.abs(monate[i].x - vx);
          if (d < naechster) { naechster = d; treffer = i; }
        }
        const m = monate[treffer];
        if (monatEl) monatEl.textContent = m.monat;
        if (werte.length) {
          werte[0].textContent = m.gesamt;
          for (let i = 1; i < werte.length; i++) {
            werte[i].textContent = (m.werte && m.werte[i - 1]) || "–";
          }
        }
        if (fuehrung) { fuehrung.style.left = (m.x / VBW * 100) + "%"; fuehrung.hidden = false; }
        setze(gesamtPunkt, m.x, m.gy);
        punkte.forEach((p, i) => setze(p, m.x, m.wy[i]));
        if (tip) {
          tip.hidden = false;
          // Waagrecht klemmen statt nur seitlich kippen: bei 375px ist der
          // Kasten breiter als der Platz neben der Führungslinie, und ohne
          // Klemme hinge er über den Kartenrand hinaus.
          const px = m.x / VBW * rect.width;
          const breite = tip.offsetWidth;
          const rechts = px + 12 + breite <= rect.width;
          const ziel = rechts ? px + 12 : px - 12 - breite;
          tip.style.left = Math.max(0, Math.min(ziel, rect.width - breite)) + "px";
        }
        // Am schmalen Bildschirm liegt der Kasten auf der Legende. Deren Reste
        // lugten darunter hervor; visibility (nicht display) blendet sie aus,
        // ohne dass die Karte in der Höhe springt.
        if (karte) karte.classList.add("nw-liest");
        offen = true;
      }

      function verstecke() {
        offen = false;
        if (karte) karte.classList.remove("nw-liest");
        if (tip) tip.hidden = true;
        if (fuehrung) fuehrung.hidden = true;
        if (gesamtPunkt) gesamtPunkt.hidden = true;
        punkte.forEach((p) => { p.hidden = true; });
      }

      // Hervorheben einer einzelnen Reihe (Legende). Die Kontur wird mit der
      // Gesamtlinie abgeblendet — bliebe sie stehen, zerschnitte sie die Linie,
      // die man gerade sehen will.
      function hervorhebe(serie) {
        const aus = serie === null;
        const gesamt = serie === "gesamt";
        [gesamtLinie, kontur].forEach((p) => {
          if (p) p.classList.toggle("nw-blass", !aus && !gesamt);
        });
        kontoLinien.forEach((p, i) => {
          p.classList.toggle("nw-blass", !aus && String(i) !== serie);
        });
      }

      canvas.addEventListener("pointerdown", (e) => {
        gedrueckt = true;
        // Nur für Finger/Stift einfangen: mit eingefangener Maus feuert
        // pointerleave erst beim Loslassen, der Kasten bliebe stehen.
        if (e.pointerType !== "mouse" && canvas.setPointerCapture) {
          try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
        }
        zeige(e.clientX);
      });
      canvas.addEventListener("pointermove", (e) => {
        if (e.pointerType === "mouse" || gedrueckt) zeige(e.clientX);
      });
      canvas.addEventListener("pointerup", () => { gedrueckt = false; });
      canvas.addEventListener("pointercancel", () => { gedrueckt = false; verstecke(); });
      canvas.addEventListener("pointerleave", (e) => {
        if (e.pointerType === "mouse") verstecke();
      });

      // Am Handy bleibt der Kasten nach dem Loslassen stehen: WÄHREND der
      // Berührung liegt der Finger auf der Stelle, die man lesen will. Der
      // nächste Griff woanders räumt ihn weg. Der Wächter hängt sich selbst
      // aus, sobald HTMX die Karte ersetzt hat.
      const ausserhalb = (e) => {
        if (!document.contains(canvas)) {
          document.removeEventListener("pointerdown", ausserhalb, true);
          return;
        }
        if (offen && !canvas.contains(e.target)) verstecke();
      };
      document.addEventListener("pointerdown", ausserhalb, true);

      if (karte && feinzeiger) {
        karte.querySelectorAll(".nw-legend-item[data-nw-serie]").forEach((el) => {
          el.addEventListener("mouseenter", () => hervorhebe(el.dataset.nwSerie));
          el.addEventListener("mouseleave", () => hervorhebe(null));
        });
      }
    });
  }

  // -------- Verlaufs-Balken: Führungslinie + Positionen der Periode ----
  // Dieselbe Mechanik wie initVerlaufHover — dataset.bound als Wächter, Wurzel
  // aus dem HTMX-Swap, Zeigerereignisse statt mousemove (am Handy gibt es
  // keinen Hover, und dieselbe Logik muss dort auf Berührung laufen).
  //
  // Neu ist nur, WAS zugeordnet wird: statt sieben Konto-Linien die Positionen
  // einer Rechnung, und die sind je Periode verschieden. Die Zeilen stehen
  // fertig im Markup, eine je Positionsname der Reihe; hier wird nur ein- und
  // ausgeblendet und der Betrag geschrieben. Ein hier zusammengebauter Name
  // wäre Text aus einer PDF, der als HTML in die Seite ginge.
  //
  // Alle Beträge kommen fertig formatiert aus data-monate. Im Browser gäbe es
  // weder Decimal noch das Schweizer Apostroph umsonst.
  function initPositionsBalken(root) {
    (root || document).querySelectorAll(".vb-canvas[data-monate]").forEach((canvas) => {
      if (canvas.dataset.bound) return;
      canvas.dataset.bound = "1";
      let monate = [];
      try { monate = JSON.parse(canvas.dataset.monate || "[]"); } catch (_) { monate = []; }
      if (!monate.length) return;

      const tip = canvas.querySelector(".vb-tip");
      const monatEl = tip ? tip.querySelector(".vb-tip-monat") : null;
      const zeilen = tip ? Array.from(tip.querySelectorAll(".vb-tip-zeile")) : [];
      const fuehrung = canvas.querySelector(".vb-guide");
      const punkt = canvas.querySelector(".vb-pkt-hover");
      const karte = canvas.closest(".vl-karte");
      let offen = false;
      let gedrueckt = false;

      function zeige(clientX) {
        const rect = canvas.getBoundingClientRect();
        if (!rect.width) return;
        // Die x-Werte stehen in Prozent der Zeichenfeldbreite — dieselbe
        // Einheit, in der die Spaltenmitten gerechnet wurden.
        const pct = (clientX - rect.left) / rect.width * 100;
        let treffer = 0, naechster = Infinity;
        for (let i = 0; i < monate.length; i++) {
          const d = Math.abs(monate[i].x - pct);
          if (d < naechster) { naechster = d; treffer = i; }
        }
        const m = monate[treffer];
        if (monatEl) monatEl.textContent = m.label;
        zeilen.forEach((z) => {
          const wert = m.zeilen[z.dataset.pos];
          // Eine Zeile ohne Wert wird ausgeblendet und nicht auf „–" gesetzt:
          // die Position gibt es in dieser Periode nicht, und ein Strich
          // behauptete, sie stünde mit null auf der Rechnung.
          z.hidden = wert === undefined;
          const feld = z.querySelector(".vb-tip-wert");
          if (feld) feld.textContent = wert === undefined ? "" : wert;
        });
        if (fuehrung) { fuehrung.style.left = m.x + "%"; fuehrung.hidden = false; }
        if (punkt) {
          punkt.style.left = m.x + "%";
          punkt.style.top = m.y + "%";
          // Geklemmte Marke: der bezahlte Betrag liegt ausserhalb der Achse.
          punkt.classList.toggle("is-aus", !!m.aus);
          punkt.hidden = false;
        }
        if (tip) {
          tip.hidden = false;
          // Waagrecht klemmen statt nur seitlich kippen: bei 375px ist der
          // Kasten breiter als der Platz neben der Führungslinie, und ohne
          // Klemme hinge er über den Kartenrand hinaus.
          const px = m.x / 100 * rect.width;
          const breite = tip.offsetWidth;
          const rechts = px + 12 + breite <= rect.width;
          const ziel = rechts ? px + 12 : px - 12 - breite;
          tip.style.left = Math.max(0, Math.min(ziel, rect.width - breite)) + "px";
        }
        // Der Kasten ist höher als das Zeichenfeld und liegt damit auf Achse und
        // Legende; deren Reste lugten darunter hervor. visibility (nicht
        // display) blendet sie aus, ohne dass die Karte in der Höhe springt.
        if (karte) karte.classList.add("vb-liest");
        offen = true;
      }

      function verstecke() {
        offen = false;
        if (karte) karte.classList.remove("vb-liest");
        if (tip) tip.hidden = true;
        if (fuehrung) fuehrung.hidden = true;
        if (punkt) punkt.hidden = true;
      }

      canvas.addEventListener("pointerdown", (e) => {
        gedrueckt = true;
        // Nur für Finger/Stift einfangen: mit eingefangener Maus feuert
        // pointerleave erst beim Loslassen, der Kasten bliebe stehen.
        if (e.pointerType !== "mouse" && canvas.setPointerCapture) {
          try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
        }
        zeige(e.clientX);
      });
      canvas.addEventListener("pointermove", (e) => {
        if (e.pointerType === "mouse" || gedrueckt) zeige(e.clientX);
      });
      canvas.addEventListener("pointerup", () => { gedrueckt = false; });
      canvas.addEventListener("pointercancel", () => { gedrueckt = false; verstecke(); });
      canvas.addEventListener("pointerleave", (e) => {
        if (e.pointerType === "mouse") verstecke();
      });

      // Am Handy bleibt der Kasten nach dem Loslassen stehen: WÄHREND der
      // Berührung liegt der Finger auf der Stelle, die man lesen will. Der
      // nächste Griff woanders räumt ihn weg. Der Wächter hängt sich selbst
      // aus, sobald HTMX die Karte ersetzt hat.
      const ausserhalb = (e) => {
        if (!document.contains(canvas)) {
          document.removeEventListener("pointerdown", ausserhalb, true);
          return;
        }
        if (offen && !canvas.contains(e.target)) verstecke();
      };
      document.addEventListener("pointerdown", ausserhalb, true);
    });
  }

  // Bank-Import: Fortschrittsbalken einblenden, sobald der Upload startet.
  // (Klassischer Formular-POST -> der Balken laeuft, bis die Antwort die Seite ersetzt.)
  function initImportProgress() {
    var f = document.getElementById("import-form");
    if (!f || f.dataset.bound) return;
    f.dataset.bound = "1";
    f.addEventListener("submit", function () {
      var p = document.getElementById("import-progress");
      if (p) p.hidden = false;
      var b = f.querySelector("button[type=submit]");
      if (b) { b.disabled = true; b.textContent = "Import läuft…"; }
    });
  }

  // -------- Buchungen-Timeline: geschwungene Verbindungslinie + fahrendes Jahr ----
  let _txScrollBound = false;
  // Kürzeste Verbindung: einfach gerade Linien von Karte zu Karte (ruhig,
  // keine Überschwinger). Reihenfolge folgt den Monaten von neu nach alt.
  function smoothPath(p) {
    if (p.length < 2) return "";
    let d = "M" + p[0][0].toFixed(1) + "," + p[0][1].toFixed(1);
    for (let i = 1; i < p.length; i++) {
      d += "L" + p[i][0].toFixed(1) + "," + p[i][1].toFixed(1);
    }
    return d;
  }
  function drawTxConnector() {
    const wrap = document.getElementById("tx-months");
    const svg = document.getElementById("tx-connector");
    if (!wrap || !svg) return;
    const cards = wrap.querySelectorAll(".tl-card");
    if (cards.length < 2) { svg.innerHTML = ""; return; }
    const W = wrap.offsetWidth, H = wrap.offsetHeight;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    const pts = [];
    cards.forEach((c) => pts.push([c.offsetLeft + c.offsetWidth / 2, c.offsetTop + 24]));
    svg.innerHTML = '<path class="tx-conn-path" d="' + smoothPath(pts) + '"></path>';
  }
  function updateTxYear() {
    const yearEl = document.getElementById("tx-year");
    if (!yearEl) return;
    const cards = document.querySelectorAll(".tl-card");
    if (!cards.length) return;
    const mark = window.innerHeight * 0.4;
    let best = null, bestD = Infinity;
    cards.forEach((c) => {
      const r = c.getBoundingClientRect();
      const d = Math.abs((r.top + 18) - mark);
      if (d < bestD) { bestD = d; best = c; }
    });
    if (best && yearEl.textContent.trim() !== best.dataset.year) {
      yearEl.style.opacity = "0";
      window.setTimeout(() => { yearEl.textContent = best.dataset.year; yearEl.style.opacity = ""; }, 170);
    }
  }
  function initTxTimeline() {
    const wrap = document.getElementById("tx-months");
    if (!wrap) return;
    drawTxConnector(); updateTxYear();
    if (!wrap.dataset.tlBound) {
      wrap.dataset.tlBound = "1";
      // Auf-/Zuklappen eines Monats → Linie neu zeichnen.
      wrap.addEventListener("toggle", () => window.setTimeout(drawTxConnector, 0), true);
    }
    if (!_txScrollBound) {
      _txScrollBound = true;
      let raf = null;
      window.addEventListener("scroll", () => {
        if (raf) return;
        raf = requestAnimationFrame(() => { raf = null; updateTxYear(); });
      }, { passive: true });
      window.addEventListener("resize", () => { drawTxConnector(); updateTxYear(); });
    }
  }

  // -------- Aufteilungs-Editor (Auto-Split) --------------------------
  // Live-Restbetrag, Zeilen hinzufuegen/entfernen. Neue Picker werden via
  // initCatPicker() gebunden (gleiche Modul-Scope-Funktion).
  function initSplitEditor() {
    const ed = document.getElementById("split-editor");
    if (!ed || ed.dataset.bound) return;
    ed.dataset.bound = "1";
    const rowsBox = ed.querySelector("#split-rows");
    const tpl = ed.querySelector("#split-row-tpl");
    const addBtn = ed.querySelector("#split-add");
    const rem = ed.querySelector("#split-remainder");
    const target = parseFloat(ed.dataset.splitTarget || "0") || 0;
    function recalc() {
      let sum = 0;
      ed.querySelectorAll(".split-amount").forEach((i) => {
        const v = parseFloat((i.value || "").replace(",", "."));
        if (!isNaN(v)) sum += v;
      });
      const left = Math.round((target - sum) * 100) / 100;
      const nRows = ed.querySelectorAll(".split-row").length;
      if (rem) {
        rem.textContent = "Verteilt: CHF " + sum.toFixed(2) + "  ·  Rest: CHF " + left.toFixed(2);
        const done = Math.abs(left) < 0.005 && nRows > 0;
        rem.classList.toggle("ok", done);
        rem.classList.toggle("warn", !done);
      }
    }
    function bindRow(r) {
      const del = r.querySelector(".split-del");
      if (del && !del.dataset.b) {
        del.dataset.b = "1";
        del.addEventListener("click", () => { r.remove(); recalc(); });
      }
      r.querySelectorAll(".split-amount").forEach((i) => {
        if (!i.dataset.b) { i.dataset.b = "1"; i.addEventListener("input", recalc); }
      });
    }
    if (rowsBox) rowsBox.querySelectorAll(".split-row").forEach(bindRow);
    if (addBtn && tpl) {
      addBtn.addEventListener("click", () => {
        const node = tpl.content.firstElementChild.cloneNode(true);
        rowsBox.appendChild(node);
        initCatPicker();
        bindRow(node);
        recalc();
      });
    }
    recalc();
  }

  // -------- Lohnzusammensetzung: Editor -------------------------------
  // Zeilen hinzufuegen und entfernen — mehr nicht.
  //
  // Die mitlaufende Gegenprobe RECHNET HIER NICHT. Sie kommt vom Server
  // (POST .../lohn/probe) und geht dort durch dieselbe Pruefung und dieselbe
  // Aufstellung wie das Speichern. Vorher stand die Rechnung hier ein zweites
  // Mal, in einer zweiten Sprache: die Vorschau zaehlte Abzuege selbst zusammen,
  // setzte ihr eigenes „≈" und zeigte auch fuer Eingaben ein Ergebnis, die der
  // Server ablehnt. Zwei Darstellungen derselben Zahl laufen auseinander, sobald
  // eine Regel sich aendert — und die eine hier ist die, in der der Nutzer
  // entscheidet.
  //
  // Die Probe ist bewusst KEINE Sperre (anders als der Restbetrag der
  // Kategorie-Aufteilung, der stimmen MUSS): aus Jahreswerten geschaetzte
  // Posten treffen den gebuchten Betrag fast nie exakt. Wer hier auf
  // Uebereinstimmung zwingt, zwingt zum Zurechtbiegen der Zahlen.
  function initLohnEditor() {
    const ed = document.getElementById("lohn-editor");
    if (!ed || ed.dataset.bound) return;
    ed.dataset.bound = "1";
    const probe = ed.querySelector("#lohn-probe");
    // Tippen meldet htmx selbst („input from:#lohn-form"). Eine geloeschte oder
    // eine neu geklonte Zeile loest kein input-Ereignis aus — dafuer dieses.
    function neuRechnen() {
      if (probe) probe.dispatchEvent(new CustomEvent("lohnprobe"));
    }
    function bindRow(r) {
      const del = r.querySelector(".lohn-del");
      if (del && !del.dataset.b) {
        del.dataset.b = "1";
        del.addEventListener("click", () => { r.remove(); neuRechnen(); });
      }
    }
    ed.querySelectorAll(".lohn-row").forEach(bindRow);
    // Je Gruppe ein „+ Position": der Knopf weiss ueber data-Attribute, in
    // welchen Kasten er klont und aus welcher Vorlage.
    ed.querySelectorAll(".lohn-add").forEach((btn) => {
      const ziel = document.getElementById(btn.dataset.lohnZiel || "");
      const tpl = document.getElementById(btn.dataset.lohnTpl || "");
      if (!ziel || !tpl) return;
      btn.addEventListener("click", () => {
        const node = tpl.content.firstElementChild.cloneNode(true);
        ziel.appendChild(node);
        bindRow(node);
        node.querySelector(".lohn-label").focus();
        neuRechnen();
      });
    });
  }

  // -------- Quittungs-Detail-Popup -----------------------------------
  function initReceiptModal(root) {
    root = root || document;
    const modal = document.getElementById("receipt-modal");
    if (!modal) return;
    const merchant = document.getElementById("receipt-merchant");
    const sub = document.getElementById("receipt-sub");
    const body = document.getElementById("receipt-modal-text");
    const totalEl = document.getElementById("receipt-total");
    const totalAmt = document.getElementById("receipt-total-amount");
    const foot = document.getElementById("receipt-modal-foot");
    const splitPanel = document.getElementById("receipt-split");
    const splitRows = document.getElementById("receipt-split-rows");
    const editBtn = document.getElementById("receipt-split-edit");
    // Die aktuell geöffnete Buchungs-ID lebt am MODAL-Element (dataset), nicht in
    // dieser Closure: Buttons aus nachgeladenen Monaten werden von einem SPÄTEREN
    // initReceiptModal-Aufruf gebunden, der Edit-Handler aber nur einmal — eine
    // Closure-Variable wäre dann veraltet und öffnete die falsche Buchung.
    const close = () => { modal.hidden = true; };
    root.querySelectorAll(".tx-receipt").forEach((b) => {
      if (b.dataset.bound) return;
      b.dataset.bound = "1";
      b.addEventListener("click", () => {
        // Kopf: Händler (Buchungstext) zwischen den Strichen, darunter Datum + Quelle.
        merchant.textContent = b.dataset.merchant || b.dataset.name || "Beleg";
        const subParts = [];
        if (b.dataset.date) subParts.push(b.dataset.date);
        // „rechnung" ist die dritte Herkunft: aus der Betragsspalte einer
        // Rechnung gelesen und gegen deren Summe geprüft. Vorher hiess alles
        // ausser „text-layer" hier OCR — das nähme diesem Beleg genau die
        // Eigenschaft, für die es ihn gibt.
        const quelle = { "text-layer": "Text-Layer", ocr: "OCR", rechnung: "Rechnung" }[b.dataset.method];
        if (quelle) subParts.push(quelle);
        sub.textContent = subParts.join("  ·  ");
        // Körper: Belegtext ON DEMAND vom Server holen — statt ~1.5 KB OCR-Text
        // pro Beleg als data-Attribut in jede Listen-Antwort zu packen.
        // Stale-Guard: kommt eine LANGSAME Antwort (Handy→NAS) erst an, nachdem
        // schon ein anderer Beleg geöffnet wurde, darf sie den nicht überschreiben.
        const attId = b.dataset.attid || "";
        body.dataset.attid = attId;
        body.textContent = "…";
        if (attId) {
          fetch("/transactions/attachment/" + attId + "/ocr-text", { credentials: "same-origin" })
            .then((r) => (r.ok ? r.text() : ""))
            .then((t) => {
              if (body.dataset.attid !== attId) return;
              body.textContent = (t || "").trim() || "(keine Einzelpositionen erkannt)";
            })
            .catch(() => {
              if (body.dataset.attid !== attId) return;
              body.textContent = "(keine Einzelpositionen erkannt)";
            });
        } else {
          body.textContent = "(keine Einzelpositionen erkannt)";
        }
        // TOTAL: erkannter Betrag, sonst der Buchungsbetrag.
        const amt = b.dataset.amount ? ("CHF " + b.dataset.amount) : (b.dataset.txamount || "");
        if (amt) { totalAmt.textContent = amt; totalEl.hidden = false; } else { totalEl.hidden = true; }
        foot.textContent = b.dataset.name ? ("Beleg-Datei: " + b.dataset.name) : "";
        // Aufteilung (falls vorhanden) auflisten.
        modal.dataset.txid = b.dataset.txid || "";
        let splits = [];
        try { splits = JSON.parse(b.dataset.splits || "[]"); } catch (_) { splits = []; }
        if (splitPanel && splitRows) {
          splitRows.innerHTML = "";
          if (splits.length) {
            splits.forEach((s) => {
              const row = document.createElement("div");
              row.className = "receipt-split-row";
              const n = document.createElement("span");
              n.textContent = s.name || "Ohne Kategorie";
              const a = document.createElement("span");
              a.className = "mono";
              a.textContent = "CHF " + (s.amount || "");
              row.appendChild(n); row.appendChild(a);
              splitRows.appendChild(row);
            });
            splitPanel.hidden = false;
          } else {
            splitPanel.hidden = true;
          }
        }
        if (editBtn) {
          editBtn.hidden = !modal.dataset.txid;
          editBtn.textContent = splits.length ? "Aufteilung bearbeiten" : "In Kategorien aufteilen";
        }
        modal.hidden = false;
      });
    });
    if (!modal.dataset.bound) {
      modal.dataset.bound = "1";
      const c = document.getElementById("receipt-modal-close");
      if (c) c.addEventListener("click", close);
      modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
      // „Aufteilung bearbeiten" → Beleg schliessen + Bearbeiten-Formular der Buchung laden.
      if (editBtn) editBtn.addEventListener("click", () => {
        const id = modal.dataset.txid;
        if (!id) return;
        close();
        if (window.htmx) {
          window.htmx.ajax("GET", "/transactions?form=edit&id=" + id,
                           { target: "#transactions-root", swap: "innerHTML" });
        }
      });
    }
  }

  // -------- Buchungen: Belegfilter-Umschalter (Quittungs-Icon) --------
  function initReceiptFilter() {
    const btn = document.getElementById("tx-receipt-filter");
    const inp = document.getElementById("tx-only-receipts");
    const form = document.getElementById("tx-filter");
    if (!btn || !inp || !form || btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      const on = inp.value === "1";
      inp.value = on ? "0" : "1";
      // Programmatisches value-Setzen feuert KEIN Event → Filter-Gedächtnis
      // (initFilterMemory) würde den Beleg-Toggle sonst nie mitschreiben.
      inp.dispatchEvent(new Event("change", { bubbles: true }));
      btn.classList.toggle("active", !on);
      btn.setAttribute("aria-pressed", String(!on));
      const vals = {};
      form.querySelectorAll("input[name], select[name]").forEach((el) => { if (el.name) vals[el.name] = el.value; });
      if (window.htmx) {
        window.htmx.ajax("GET", "/transactions", { target: "#transactions-root", swap: "innerHTML", values: vals });
      }
    });
  }

  // -------- Bestätigungs-Dialog für klassische POST-Formulare (CSP-konform) --
  function initConfirmForms(root) {
    root = root || document;
    root.querySelectorAll("form[data-confirm]").forEach((f) => {
      if (f.dataset.confirmBound) return;
      f.dataset.confirmBound = "1";
      f.addEventListener("submit", (e) => {
        if (!window.confirm(f.dataset.confirm)) e.preventDefault();
      });
    });
  }

  // -------- Lern-Regel-Checkbox: Stichwortfeld aktivieren/deaktivieren -------
  function initLearnToggle(root) {
    root = root || document;
    root.querySelectorAll(".js-learn-toggle").forEach((cb) => {
      if (cb.dataset.bound) return;
      cb.dataset.bound = "1";
      const apply = () => {
        const box = cb.closest("[data-learn]");
        const kw = box && box.querySelector("[name=learn_keyword]");
        if (kw) kw.disabled = !cb.checked;
      };
      cb.addEventListener("change", apply);
      apply();
    });
  }

  // -------- Beleg-Auswahl: Suchen + Anklicken (statt langem Dropdown) ----
  function initReceiptPicker(root) {
    root = root || document;
    root.querySelectorAll("[data-rcpt]").forEach((p) => {
      if (p.dataset.bound) return;
      p.dataset.bound = "1";
      const search = p.querySelector(".rcpt-search");
      const hidden = p.querySelector("input[name=filename]");
      const chosen = p.querySelector(".rcpt-chosen");
      const opts = Array.from(p.querySelectorAll(".rcpt-opt"));
      if (search) search.addEventListener("input", () => {
        const q = search.value.trim().toLowerCase();
        opts.forEach((o) => {
          if (o.classList.contains("rcpt-none")) return;
          o.style.display = (!q || (o.dataset.name || "").toLowerCase().includes(q)) ? "" : "none";
        });
      });
      opts.forEach((o) => o.addEventListener("click", () => {
        if (hidden) hidden.value = o.dataset.name || "";
        opts.forEach((x) => x.classList.remove("active"));
        o.classList.add("active");
        if (chosen) chosen.textContent = o.dataset.name ? ("Gewählt: " + o.dataset.name) : "Keine Quittung";
      }));
    });
  }

  // -------- Quick-Add: Kategorie-Suche (filtert das Pillen-Grid) -------
  function initQuickCatSearch() {
    const inp = document.getElementById("quick-catsearch");
    const grid = document.getElementById("quick-catgrid");
    if (!inp || !grid || inp.dataset.bound) return;
    inp.dataset.bound = "1";
    inp.addEventListener("input", () => {
      const q = inp.value.trim().toLowerCase();
      // Das Raster liegt hinter einem <details>. Wer tippt, will Treffer sehen —
      // also aufklappen, sobald etwas im Suchfeld steht.
      const mehr = document.getElementById("quick-catmore");
      if (mehr && q) mehr.open = true;
      grid.querySelectorAll(".cat-pill-row").forEach((row) => {
        let any = false;
        row.querySelectorAll(".cat-pill").forEach((p) => {
          const match = !q || p.textContent.trim().toLowerCase().includes(q);
          p.style.display = match ? "" : "none";
          if (match) any = true;
        });
        row.style.display = any ? "" : "none";
        const header = row.previousElementSibling;
        if (header && header.classList.contains("cat-picker-group")) header.style.display = any ? "" : "none";
      });
    });
  }

  // -------- Kategorie-Verwaltung: Icon-Picker (Suche) + Farbe + Art-Feld ----
  function initCategoryAdmin() {
    const form = document.getElementById("cat-form");
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    // „Art" nur für Top-Kategorien (ohne Oberkategorie) zeigen.
    const parent = form.querySelector("[data-art-toggle]");
    const artField = document.getElementById("cat-art-field");
    const toggleArt = () => { if (artField) artField.style.display = (parent && parent.value) ? "none" : ""; };
    if (parent) { parent.addEventListener("change", toggleArt); toggleArt(); }
    // Icon-Picker: tippen filtert nach Stichwörtern, klicken wählt.
    const ip = form.querySelector("[data-iconpick]");
    if (ip) {
      const hidden = ip.querySelector("input[name=icon]");
      const search = ip.querySelector(".iconpick-search");
      const cur = ip.querySelector(".iconpick-current");
      const curUse = cur && cur.querySelector("svg use");
      const curName = cur && cur.querySelector(".iconpick-name");
      const opts = Array.from(ip.querySelectorAll(".iconpick-opt"));
      if (search) search.addEventListener("input", () => {
        const q = search.value.trim().toLowerCase();
        opts.forEach((o) => { o.style.display = (!q || (o.dataset.keywords || "").includes(q)) ? "" : "none"; });
      });
      opts.forEach((o) => o.addEventListener("click", () => {
        const n = o.dataset.name;
        if (hidden) hidden.value = n;
        opts.forEach((x) => x.classList.remove("active"));
        o.classList.add("active");
        if (curUse) curUse.setAttribute("href", "#i-" + n);
        if (curName) curName.textContent = n;
      }));
    }
    // Farb-Picker (Swatches → Hidden-Feld).
    const cp = form.querySelector("[data-colorpick]");
    if (cp) {
      const hidden = cp.querySelector("input[name=color]");
      const sw = Array.from(cp.querySelectorAll(".cat-swatch"));
      sw.forEach((s) => s.addEventListener("click", () => {
        if (hidden) hidden.value = s.dataset.color || "";
        sw.forEach((x) => x.classList.remove("active"));
        s.classList.add("active");
      }));
    }
  }

  // -------- Prognose: Stresstest live (Regler + Diagramm, ohne Roundtrip) ---
  function fmtCHF(n) {
    const neg = n < 0;
    let [a, b] = Math.abs(n).toFixed(2).split(".");
    a = a.replace(/\B(?=(\d{3})+(?!\d))/g, "'");
    return (neg ? "−" : "") + "CHF " + a + "." + b;
  }
  function initStressLive() {
    const data = document.getElementById("stress-data");
    const form = document.getElementById("stress-form");
    if (!data || !form || form.dataset.bound) return;
    form.dataset.bound = "1";
    let cfg;
    try { cfg = JSON.parse(data.dataset.cfg || "{}"); } catch (_) { return; }
    const inc = document.getElementById("st-income");
    const exp = document.getElementById("st-expense");
    const one = document.getElementById("st-onetime");
    const ch = cfg.chart || {};
    const line = document.getElementById("fc-stress-line");
    const legend = document.getElementById("fc-stress-legend");
    const endWrap = document.getElementById("fc-stress-end");
    const histLine = document.getElementById("fc-hist-line");
    const fcLine = document.getElementById("fc-fc-line");
    const svg = line ? line.closest("svg") : null;
    const vals = Array.isArray(ch.vals) ? ch.vals : [];   // Rohwerte aller Stützpunkte
    let hoverPts = [];
    if (svg) { try { hoverPts = JSON.parse(svg.dataset.points || "[]"); } catch (_) { hoverPts = []; } }

    function grad(slider) {
      const min = +slider.min, max = +slider.max, val = +slider.value;
      const zero = (0 - min) / (max - min) * 100, pos = (val - min) / (max - min) * 100;
      const lo = Math.min(zero, pos), hi = Math.max(zero, pos);
      let col = "var(--border-emphasis)";
      if (val > 0) col = (slider.dataset.good === "right") ? "var(--accent-tertiary)" : "var(--dusty-rose)";
      else if (val < 0) col = (slider.dataset.good === "right") ? "var(--dusty-rose)" : "var(--accent-tertiary)";
      slider.style.background = "linear-gradient(90deg, var(--bg-sunken) 0 " + lo + "%, " + col + " " + lo + "% " + hi + "%, var(--bg-sunken) " + hi + "% 100%)";
    }
    function xAt(idx) {
      return ch.pad + (ch.w - 2 * ch.pad) * (idx / Math.max(ch.nTotal - 1, 1));
    }
    function yAt(val, lo, span) {
      return (ch.h - ch.pad) - (ch.h - 2 * ch.pad) * ((val - lo) / (span || 1));
    }
    function xy(idx, val, lo, span) {
      return xAt(idx).toFixed(1) + "," + yAt(val, lo, span).toFixed(1);
    }
    // Historie + neutrale Prognose in der gegebenen Y-Skala neu zeichnen und die
    // Hover-Stützpunkte deckungsgleich nachführen (sonst sitzt der Tooltip daneben,
    // wenn die Skala beim Stresstest mitwächst).
    function redrawBase(lo, span) {
      if (!vals.length) return;
      if (histLine) {
        const p = [];
        for (let i = 0; i < ch.histLen; i++) p.push(xy(i, vals[i], lo, span));
        histLine.setAttribute("points", p.join(" "));
      }
      if (fcLine) {
        const p = [];
        for (let i = ch.histLen - 1; i < ch.nTotal; i++) p.push(xy(i, vals[i], lo, span));
        fcLine.setAttribute("points", p.join(" "));
      }
      if (svg && hoverPts.length === ch.nTotal) {
        for (let i = 0; i < ch.nTotal; i++) {
          hoverPts[i].x = Math.round(xAt(i) * 10) / 10;
          hoverPts[i].y = Math.round(yAt(vals[i], lo, span) * 10) / 10;
        }
        svg.dataset.points = JSON.stringify(hoverPts);
      }
    }
    function setText(id, txt, color) {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = txt;
      if (color) el.style.color = color;
    }
    function recalc() {
      const ip = +inc.value, ep = +exp.value;
      const ot = Math.max(0, parseFloat((one.value || "0").replace(",", ".")) || 0);
      const newIncome = cfg.baseIncome * (100 + ip) / 100;
      const newExpense = cfg.baseExpense * (100 + ep) / 100;
      const saldo = newIncome - newExpense;
      const available = cfg.liquid - ot;
      const green = "var(--accent-tertiary)", danger = "var(--danger)";
      grad(inc); grad(exp);
      const ivEl = document.getElementById("st-income-val");
      const evEl = document.getElementById("st-expense-val");
      if (ivEl) ivEl.innerHTML = "<strong>" + (ip > 0 ? "+" : "") + ip + " %</strong> · " + fmtCHF(newIncome) + "/Mt";
      if (evEl) evEl.innerHTML = "<strong>" + (ep > 0 ? "+" : "") + ep + " %</strong> · " + fmtCHF(newExpense) + "/Mt";
      setText("st-new-saldo", fmtCHF(saldo), saldo >= 0 ? green : danger);
      setText("st-saldo-note", fmtCHF(newIncome) + " − " + fmtCHF(newExpense));
      // Delta des Monatssaldos gegenüber heute — prominent, spart Kopfrechnen.
      const delta = saldo - cfg.baseSaldo;
      setText("st-delta", "Δ zu heute: " + (delta >= 0 ? "+" : "") + fmtCHF(delta) + "/Mt",
              Math.abs(delta) < 0.005 ? "var(--text-tertiary)" : (delta >= 0 ? green : "var(--dusty-rose)"));
      if (saldo >= 0) {
        setText("st-runway", "unbegrenzt", green);
        setText("st-runway-note", "Saldo bleibt positiv");
      } else {
        const rw = available > 0 ? Math.round(available / Math.abs(saldo) * 10) / 10 : 0;
        setText("st-runway", rw + " Mt", rw < 12 ? danger : "var(--text-primary)");
        setText("st-runway-note", "so lange decken die liquiden Mittel den Verlust");
      }
      const warn = document.getElementById("st-warn");
      if (warn) {
        const rw = saldo >= 0 ? null : (available > 0 ? available / Math.abs(saldo) : 0);
        warn.innerHTML = (rw !== null && rw < 6)
          ? '<div class="feedback err" style="margin-top:12px">⚠ In diesem Szenario wären die liquiden Mittel in unter 6 Monaten aufgebraucht.</div>'
          : "";
      }
      // Stress-Linie + Diagramm: die Y-Skala wächst mit dem Szenario mit, statt die
      // Linie am Rand abzuschneiden (das erzeugte sonst einen flachen „Knick" am
      // oberen/unteren Diagrammrand). Historie + neutrale Prognose werden in der
      // gleichen, neuen Skala mitgezeichnet, damit alles vergleichbar bleibt.
      if (line && ch.horizon && vals.length) {
        const active = ip !== 0 || ep !== 0;
        if (active) {
          // Szenario-Werte ab „heute" — konstante Steigung = neuer Monatssaldo.
          const sv = [];
          for (let j = 0; j <= ch.horizon; j++) sv.push(ch.lastVal + saldo * j);
          // Skala so wählen, dass Historie, neutrale Prognose UND Szenario passen.
          const baseHi = ch.lo + ch.span;
          const lo = Math.min(ch.lo, Math.min.apply(null, sv));
          const hi = Math.max(baseHi, Math.max.apply(null, sv));
          const span = (hi - lo) || 1;
          redrawBase(lo, span);
          const pts = [];
          for (let j = 0; j <= ch.horizon; j++) pts.push(xy(ch.histLen - 1 + j, sv[j], lo, span));
          line.setAttribute("points", pts.join(" "));
          line.setAttribute("stroke", saldo >= cfg.baseSaldo ? green : "var(--dusty-rose)");
          line.style.display = "";
          if (legend) legend.hidden = false;
          if (endWrap) {
            endWrap.hidden = false;
            const s = endWrap.querySelector("strong");
            if (s) { s.textContent = fmtCHF(sv[ch.horizon]); s.style.color = saldo >= cfg.baseSaldo ? green : "var(--dusty-rose)"; }
          }
        } else {
          // Neutral (0/0): Original-Skala wiederherstellen, Szenario-Linie ausblenden.
          redrawBase(ch.lo, ch.span);
          line.style.display = "none";
          if (legend) legend.hidden = true;
          if (endWrap) endWrap.hidden = true;
        }
      }
    }
    // Reglerstand merken (über Reload/Navigation hinweg).
    const SKEY = "moneten.stress";
    function saveStress() {
      try { localStorage.setItem(SKEY, JSON.stringify({ i: inc && inc.value, e: exp && exp.value, o: one && one.value })); } catch (_) {}
    }
    try {
      const s = JSON.parse(localStorage.getItem(SKEY) || "null");
      if (s) {
        if (inc && s.i != null) inc.value = s.i;
        if (exp && s.e != null) exp.value = s.e;
        if (one && s.o != null) one.value = s.o;
      }
    } catch (_) {}
    [inc, exp, one].forEach((el) => { if (el) el.addEventListener("input", () => { recalc(); saveStress(); }); });
    // Schnellszenarien (Presets): setzen die Regler und lösen sofort ein Update aus.
    document.querySelectorAll("#stress-presets [data-preset]").forEach((b) => {
      if (b.dataset.bound) return;
      b.dataset.bound = "1";
      b.addEventListener("click", () => {
        const d = b.dataset;
        if (d.reset === "1") {
          if (inc) inc.value = 0; if (exp) exp.value = 0; if (one) one.value = 0;
        } else {
          if (inc && d.income !== undefined && d.income !== "") inc.value = d.income;
          if (exp && d.expense !== undefined && d.expense !== "") exp.value = d.expense;
          if (one && d.onetime !== undefined && d.onetime !== "") one.value = d.onetime;
        }
        recalc(); saveStress();
      });
    });
    recalc();
  }

  // -------- Mobile „Mehr"-Sheet (untere App-Navigation) ----------------
  function initMoreSheet() {
    const btn = document.getElementById("more-btn");
    const sheet = document.getElementById("more-sheet");
    const back = document.getElementById("more-backdrop");
    if (!btn || !sheet || !back || btn.dataset.bound) return;
    btn.dataset.bound = "1";
    const open = () => { sheet.classList.add("open"); back.classList.add("open"); btn.setAttribute("aria-expanded", "true"); };
    const close = () => { sheet.classList.remove("open"); back.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", () => sheet.classList.contains("open") ? close() : open());
    back.addEventListener("click", close);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    // Nach Auswahl eines Eintrags schliessen (Theme-Buttons NICHT — die bleiben offen).
    sheet.querySelectorAll("a").forEach((a) => a.addEventListener("click", close));
  }

  // -------- Regeln: globale Suche über ALLE offenen Buchungen ----------
  function initInboxGlobalSearch() {
    const g = document.getElementById("inbox-global-search");
    if (!g || g.dataset.bound) return;
    g.dataset.bound = "1";
    const emptyMsg = document.querySelector(".inbox-global-empty");
    g.addEventListener("input", () => {
      const q = g.value.trim().toLowerCase();
      let anyGroupVisible = false;
      document.querySelectorAll(".inbox-row").forEach((row) => {
        const summary = row.querySelector(".inbox-summary");
        const exp = row.querySelector(".inbox-expand");
        if (!exp) return;
        const rows = Array.prototype.slice.call(exp.querySelectorAll(".inbox-tx-light"));
        if (!q) {
          // zurücksetzen: alle Gruppen sichtbar, eingeklappt, alle Zeilen sichtbar
          row.style.display = "";
          rows.forEach((r) => { r.style.display = ""; });
          if (summary) summary.setAttribute("aria-expanded", "false");
          exp.hidden = true;
          return;
        }
        let matches = 0;
        rows.forEach((r) => {
          const m = (r.dataset.desc || "").indexOf(q) !== -1;
          r.style.display = m ? "" : "none";
          if (!m) { const cb = r.querySelector(".inbox-cb"); if (cb) cb.checked = false; }
          if (m) matches += 1;
        });
        // Ohne `?.` geschrieben, damit die Datei von einem ES2017-Parser
        // gelesen werden kann — `tests/test_js_syntax.py` haelt sie darauf
        // fest. Das ist kein Selbstzweck: ein Syntaxfehler in dieser Datei
        // legt Scan, Picker und Suche still lahm, und nichts wird rot.
        const labelEl = row.querySelector(".inbox-label");
        const labelText = (labelEl && labelEl.textContent) || "";
        const labelMatch = labelText.toLowerCase().indexOf(q) !== -1;
        if (matches > 0 || labelMatch) {
          row.style.display = "";
          anyGroupVisible = true;
          if (matches > 0) { if (summary) summary.setAttribute("aria-expanded", "true"); exp.hidden = false; }
        } else {
          row.style.display = "none";
        }
      });
      if (emptyMsg) emptyMsg.hidden = !q || anyGroupVisible;
    });
  }

  // -------- Toast: kurze Rückmeldung, optional mit Aktion („Rückgängig") -----
  function showToast(message, opts) {
    opts = opts || {};
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.className = "toast-host";
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
    }
    const el = document.createElement("div");
    el.className = "toast";
    const txt = document.createElement("span");
    txt.className = "toast-msg";
    txt.textContent = message;
    el.appendChild(txt);
    let timer;
    const close = () => { if (timer) clearTimeout(timer); el.classList.remove("show"); setTimeout(() => el.remove(), 220); };
    if (opts.actionLabel && typeof opts.onAction === "function") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "toast-action";
      btn.textContent = opts.actionLabel;
      btn.addEventListener("click", () => { opts.onAction(); close(); });
      el.appendChild(btn);
    }
    const x = document.createElement("button");
    x.type = "button"; x.className = "toast-x"; x.setAttribute("aria-label", "Schliessen"); x.textContent = "✕";
    x.addEventListener("click", close);
    el.appendChild(x);
    host.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    timer = setTimeout(close, opts.timeout || 9000);
    return el;
  }

  // Server-ausgelöste Toasts (HX-Trigger „moneten:toast"): zeigt Meldung +
  // optional eine „Rückgängig"-Aktion, die per HTMX die Undo-Route aufruft.
  function initUndoTriggers() {
    if (document.body.dataset.undoBound) return;
    document.body.dataset.undoBound = "1";
    document.body.addEventListener("moneten:toast", (e) => {
      const d = (e && e.detail) || {};
      const opts = { timeout: d.timeout || 9000 };
      if (d.undo && d.undo.url) {
        opts.actionLabel = "Rückgängig";
        opts.onAction = () => {
          if (window.htmx) {
            window.htmx.ajax("POST", d.undo.url, {
              target: d.undo.target || "body",
              swap: d.undo.swap || "innerHTML",
              values: d.undo.values || {},
            });
          }
        };
      }
      showToast(d.message || "Erledigt.", opts);
    });
  }

  // -------- Betragsfeld autofokussieren (Erfassung keyboard-first) -----------
  function focusAmount() {
    const el = document.getElementById("tx-amount") || document.getElementById("quick-amount");
    if (!el || el.dataset.autofocused) return;
    el.dataset.autofocused = "1";
    try { el.focus({ preventScroll: false }); el.select(); } catch (_) {}
  }

  // -------- Buchungen-Filter merken (über Reload/Navigation hinweg) ----------
  function initFilterMemory(firstLoad) {
    const KEY = "moneten.txfilter";
    if (firstLoad && location.pathname === "/transactions" && !location.search) {
      let saved = null;
      try { saved = localStorage.getItem(KEY); } catch (_) {}
      if (saved && saved.length > 1) {
        // Altbestand: frueher gemerkte Suchbegriffe hier herausnehmen, sonst
        // traegt der erste Aufruf nach dem Update den alten Begriff doch noch
        // in die Chronik.
        const ohneQ = new URLSearchParams(saved);
        if (ohneQ.has("q")) {
          ohneQ.delete("q");
          saved = ohneQ.toString();
          try { localStorage.setItem(KEY, saved); } catch (_) {}
        }
        if (saved.length > 1) { location.replace("/transactions?" + saved); return; }
      }
    }
    const filter = document.getElementById("tx-filter");
    if (!filter || filter.dataset.memBound) return;
    filter.dataset.memBound = "1";
    const save = () => {
      const params = new URLSearchParams();
      filter.querySelectorAll("input, select").forEach((el) => {
        if (!el.name) return;
        // Der Suchbegriff wird BEWUSST nicht gemerkt. Gemerkte Filter landen
        // beim naechsten Aufruf per location.replace in der Adresszeile — und
        // damit stuende "/transactions?q=<Name eines Arztes>" in der Chronik und
        // in der Autovervollstaendigung. Im normalen Betrieb laeuft die Suche
        // ueber HTMX und taucht dort nie auf. Nebenbei ist ein Suchbegriff von
        // vorgestern als stiller Dauerfilter ohnehin unerwuenscht.
        if (el.name === "q") return;
        if ((el.type === "checkbox" || el.type === "radio") && !el.checked) return;
        if (el.value === "" || el.value == null) return;
        params.set(el.name, el.value);
      });
      try { localStorage.setItem(KEY, params.toString()); } catch (_) {}
    };
    filter.addEventListener("change", save);
    filter.addEventListener("input", save);
    // Expliziter Filter-Reset (✕) muss auch das Gedächtnis leeren — sonst stellt
    // der nächste query-lose Aufruf den bewusst verworfenen Filter wieder her.
    const rst = document.getElementById("tx-filter-reset");
    if (rst && !rst.dataset.memBound) {
      rst.dataset.memBound = "1";
      rst.addEventListener("click", () => {
        try { localStorage.removeItem(KEY); } catch (_) {}
        const s = document.getElementById("tx-search");
        if (s) s.value = "";  // hx-preserve würde den alten Suchtext sonst behalten
      });
    }
  }

  // -------- Globale Befehls-/Sprungsuche (Strg+K / ⌘+K) -------------------
  function initCmdK() {
    const modal = document.getElementById("cmdk");
    if (!modal || modal.dataset.bound) return;
    modal.dataset.bound = "1";
    const input = document.getElementById("cmdk-input");
    const list = document.getElementById("cmdk-list");
    const empty = document.getElementById("cmdk-empty");
    const backdrop = document.getElementById("cmdk-backdrop");
    const items = Array.prototype.slice.call(list.querySelectorAll(".cmdk-item"));

    const closeMoreSheet = () => {
      const s = document.getElementById("more-sheet");
      const b = document.getElementById("more-backdrop");
      const mb = document.getElementById("more-btn");
      if (s) s.classList.remove("open");
      if (b) b.classList.remove("open");
      if (mb) mb.setAttribute("aria-expanded", "false");
    };
    const isOpen = () => !modal.hidden;
    const selected = () => list.querySelector(".cmdk-item.sel");
    const visible = () => items.filter((it) => !it.hidden);
    function select(it) {
      items.forEach((x) => x.classList.toggle("sel", x === it));
      if (it) it.scrollIntoView({ block: "nearest" });
    }
    function filter() {
      const q = input.value.trim().toLowerCase();
      let first = null;
      items.forEach((it) => {
        const lbl = (it.querySelector(".cmdk-lbl").textContent || "").toLowerCase();
        const show = !q || (it.dataset.keywords + " " + lbl).indexOf(q) !== -1;
        it.hidden = !show;
        if (show && !first) first = it;
      });
      select(first);
      if (empty) empty.hidden = !!first;
    }
    function move(dir) {
      const vis = visible();
      if (!vis.length) return;
      let i = vis.indexOf(selected());
      i = (i + dir + vis.length) % vis.length;
      select(vis[i]);
    }
    function open() {
      closeMoreSheet();
      modal.hidden = false;
      input.value = "";
      filter();
      requestAnimationFrame(() => input.focus());
    }
    function close() { modal.hidden = true; }
    function activate(it) {
      if (!it) return;
      const href = it.dataset.href;
      const action = it.dataset.action;
      close();
      // "theme:<name>" deckt ALLE Farbwelten ab — die
      // Einträge kommen aus der Registry, hier muss nichts gepflegt werden.
      if (action && action.startsWith("theme:")) {
        const t = action.slice(6);
        applyTheme(t);
        saveTheme(t);
      } else if (href) {
        window.location.href = href;
      }
    }

    document.querySelectorAll(".cmdk-open").forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", (e) => { e.preventDefault(); open(); });
    });
    input.addEventListener("input", filter);
    list.addEventListener("click", (e) => { const it = e.target.closest(".cmdk-item"); if (it) activate(it); });
    if (backdrop) backdrop.addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        isOpen() ? close() : open();
        return;
      }
      if (!isOpen()) return;
      if (e.key === "Escape") { e.preventDefault(); close(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); activate(selected()); }
    });
  }

  // -------- Beleg-Scan: editierbare digitale Quittung ----------------------
  const KZ_HG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12M6 21h12M8 3c0 4 4 5 4 7 0-2 4-3 4-7M8 21c0-4 4-5 4-7 0 2 4 3 4 7"/></svg>';
  function kzEsc(s) { return (s == null ? "" : String(s)).replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m])); }

  // Der Beleg-Scan ist der einzige Vorgang der App, der nicht wiederholbar ist:
  // das Foto wird nur im Speicher gelesen und danach verworfen, die Analyse
  // rechnet auf dem NAS bis zu zwanzig Sekunden. Was hier verlorengeht, ist weg
  // — der Beleg muss neu fotografiert werden, sofern man ihn noch hat. Darum
  // hält ein Entwurf den Zustand ausserhalb des DOM fest.
  const KZ_DRAFT = "moneten.beleg.entwurf";

  // **Beim Abmelden ist der Entwurf weg.** Er hielt bis zu 24 Stunden das
  // komplette Markup des Beleg-Dialogs: Haendler, Datum, Betrag und jede
  // einzelne Position. Weder /logout noch eine abgelaufene Sitzung raeumten ihn
  // auf — wer danach das Geraet aus der Hand gab, gab die letzte Quittung mit.
  //
  // Die Anmeldeseite ist die verlaesslichste Stelle dafuer: dort landet man
  // nach dem Abmelden UND nach jedem Ablauf der Sitzung.
  (function entwurfBeimAbmeldenLoeschen() {
    if (!document.querySelector(".login-shell")) return;
    try {
      localStorage.removeItem("moneten.beleg.entwurf");
      localStorage.removeItem("moneten.beleg.auftrag");
    } catch (_) { /* privater Modus: dann gibt es ohnehin nichts zu loeschen */ }
  })();

  // Fassung der ausgelieferten Oberflaeche. Sie steckt als `?v=` an jeder
  // statischen Datei und aendert sich bei jedem Deploy.
  //
  // WARUM DAS HIER STEHT: der Entwurf sichert den fertigen Dialog als HTML.
  // Kommt er aus einer aelteren Fassung zurueck, bringt er deren Markup UND
  // deren Erkennungsergebnis mit — er sieht aus wie ein frischer Scan, ist aber
  // ein Schnappschuss. Gemessen an einem echten Fall: nach einem Deploy meldete
  // dreimal „immer noch falsch" gemeldet, waehrend die Korrekturen laengst liefen.
  // Die App zeigte ihm einen Entwurf von vor dem Deploy.
  function kzFassung() {
    try {
      const skript = document.querySelector('script[src*="/static/js/app.js"]');
      return new URL(skript.src, location.href).searchParams.get("v") || "";
    } catch (_) { return ""; }
  }
  // Älter als ein Tag ist kein unterbrochener Vorgang mehr, sondern ein
  // vergessener — ein Dialog, der dann ungefragt aufginge, wäre Belästigung.
  const KZ_DRAFT_MAX_AGE = 24 * 60 * 60 * 1000;

  function kzModal() { return document.getElementById("kz-modal"); }
  function kzIstOffen() { const m = kzModal(); return !!(m && m.innerHTML.trim()); }
  function kzBusy() { return document.body.dataset.kzBusy === "1"; }

  // Der Browser fragt nach, statt kommentarlos neu zu laden. Nur währenddessen
  // registriert: sonst nervt die Rückfrage auf jeder Seite.
  function kzWarnBeforeUnload(e) { e.preventDefault(); e.returnValue = ""; }

  function kzSetBusy(on, label) {
    const ind = document.getElementById("kz-analyzing");
    const txt = document.getElementById("kz-an-text");
    if (on && txt && label) txt.textContent = label;
    if (ind) ind.classList.toggle("htmx-request", !!on);
    if (on) {
      document.body.dataset.kzBusy = "1";
      window.addEventListener("beforeunload", kzWarnBeforeUnload);
    } else {
      delete document.body.dataset.kzBusy;
      window.removeEventListener("beforeunload", kzWarnBeforeUnload);
    }
    // Doppeltes Speichern legte den Beleg zweimal an; „Abbrechen" mitten im
    // Speichern hätte die Antwort ins Leere geswappt.
    document.querySelectorAll("#kz-confirm, #kz-cancel").forEach((b) => { b.disabled = !!on; });
  }

  // ---- Scan-Auftrag: die Analyse laeuft auf dem Server, nicht in dieser Seite --
  //
  // Vorher wartete der Browser in EINER Anfrage, bis die Erkennung fertig war.
  // Wer waehrenddessen kurz in eine andere App wechselte, kam zurueck und fand
  // nichts vor: das Handy haelt eine Seite im Hintergrund nicht am Leben, die
  // Anfrage stirbt mit ihr, und das Ergebnis stand nur in ihrer Antwort.
  //
  // Jetzt merkt sich die Seite nur eine NUMMER. Der Server rechnet weiter, egal
  // was das Handy mit der Seite macht, und beim naechsten Oeffnen wird die
  // Nummer wieder abgefragt.
  const KZ_AUFTRAG = "moneten.beleg.auftrag";
  const KZ_AUFTRAG_MAX_ALTER = 20 * 60 * 1000;  // laenger als jede Erkennung dauert
  const KZ_POLL_MS = 1500;

  function kzMerkeAuftrag(jid) {
    try { localStorage.setItem(KZ_AUFTRAG, JSON.stringify({ jid, ts: Date.now() })); } catch (_) {}
  }

  function kzVergissAuftrag() {
    try { localStorage.removeItem(KZ_AUFTRAG); } catch (_) {}
  }

  function kzOffenerAuftrag() {
    try {
      const a = JSON.parse(localStorage.getItem(KZ_AUFTRAG) || "null");
      if (!a || !a.jid) return null;
      if (Date.now() - (a.ts || 0) > KZ_AUFTRAG_MAX_ALTER) { kzVergissAuftrag(); return null; }
      return a.jid;
    } catch (_) { return null; }
  }

  // Fragt die Nummer, bis ein Ergebnis da ist. `202` heisst „laeuft noch",
  // `410` heisst „Nummer unbekannt" (Server neu gestartet) — dann hilft nur
  // ein neuer Scan, und das wird auch so gesagt.
  async function kzHoleErgebnis(jid) {
    kzSetBusy(true, "Beleg wird analysiert …");
    try {
      for (;;) {
        const r = await fetch("/import/receipts/photo/job/" + encodeURIComponent(jid),
                              { credentials: "same-origin" });
        if (r.status === 202) { await new Promise((f) => setTimeout(f, KZ_POLL_MS)); continue; }
        kzVergissAuftrag();
        if (r.status === 410) {
          document.body.dispatchEvent(new CustomEvent("moneten:toast",
            { detail: { message: "Die Analyse ist verlorengegangen — bitte nochmal aufnehmen." } }));
          return;
        }
        if (!r.ok) throw new Error("job " + r.status);
        const modal = kzModal();
        if (modal) { modal.innerHTML = await r.text(); if (window.htmx) window.htmx.process(modal); }
        // Der erste render() legt sofort den Entwurf an — ab hier ist die
        // Analyse gegen Neuladen und Seitenwechsel abgesichert.
        initReceiptScan();
        return;
      }
    } finally {
      kzSetBusy(false);
    }
  }

  function kzSaveDraft(scan, state) {
    try {
      const c = scan.cloneNode(true);
      c.dataset.receipt = JSON.stringify(state);  // die KORRIGIERTEN Werte, nicht die erkannten
      delete c.dataset.bound;                     // sonst steigt initReceiptScan beim Wiederherstellen aus
      const p = c.querySelector("#kz-paper");
      if (p) p.innerHTML = "";                    // wird aus dem Zustand neu gezeichnet
      localStorage.setItem(KZ_DRAFT, JSON.stringify({ ts: Date.now(), v: kzFassung(), html: c.outerHTML }));
    } catch (_) { /* privater Modus / Quota: der Entwurf ist die Absicherung, nicht der Vorgang */ }
  }

  function kzReadDraft() {
    try {
      const d = JSON.parse(localStorage.getItem(KZ_DRAFT) || "null");
      if (!d || !d.html) return null;
      if (Date.now() - (d.ts || 0) > KZ_DRAFT_MAX_AGE) { kzDropDraft(); return null; }
      // Andere Fassung: das gesicherte Markup und das Erkennungsergebnis stammen
      // aus einer App, die es so nicht mehr gibt. Wiederherstellen hiesse, eine
      // behobene Fehlerkennung als frisches Ergebnis zu zeigen.
      if ((d.v || "") !== kzFassung()) { kzDropDraft(); return null; }
      return d;
    } catch (_) { return null; }
  }

  function kzDropDraft() { try { localStorage.removeItem(KZ_DRAFT); } catch (_) {} }

  // Holt den Dialog samt aller Korrekturen zurück — nach Neuladen, Seitenwechsel
  // oder Tab-Absturz beim nächsten boot(), nach „Abbrechen" über den Toast.
  function kzRestoreDraft(draft) {
    const m = kzModal();
    if (!m || kzIstOffen()) return false;  // ein offener Dialog hat Vorrang
    draft = draft || kzReadDraft();
    if (!draft) return false;
    m.innerHTML = draft.html;
    if (window.htmx) window.htmx.process(m);
    initReceiptScan();
    // Sagen, was man sieht. Ein wiederhergestellter Entwurf sah aus wie ein
    // frisch analysierter Beleg — man kann ihn also fuer das Ergebnis eines
    // Scans halten, den man gerade gar nicht gemacht hat.
    const wann = new Date(draft.ts || Date.now());
    const sheet = m.querySelector(".kz-sheet");
    if (sheet && !sheet.querySelector(".kz-entwurf")) {
      const hinweis = document.createElement("div");
      hinweis.className = "kz-entwurf";
      hinweis.textContent = "Fortgesetzter Beleg von "
        + wann.toLocaleString("de-CH", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
      sheet.insertBefore(hinweis, sheet.firstChild);
    }
    return true;
  }

  // `undoable`: der Nutzer hat selbst abgebrochen. Der Entwurf fällt weg — das
  // hat er gesagt —, bleibt aber für die Dauer des Toasts zurückholbar. Zwanzig
  // Sekunden Analyse dürfen nicht an einem Fehlgriff hängen.
  function kzCloseModal(opts) {
    opts = opts || {};
    const m = kzModal();
    if (m) m.innerHTML = "";
    const draft = opts.undoable ? kzReadDraft() : null;
    kzDropDraft();
    if (draft) {
      showToast("Beleg verworfen.", {
        actionLabel: "Zurückholen",
        onAction: () => kzRestoreDraft(draft),
      });
    }
  }

  function initReceiptScan() {
    // Einmalige, delegierte Handler.
    if (!document.body.dataset.kzCloseBound) {
      document.body.dataset.kzCloseBound = "1";
      // BEWUSST ohne Backdrop: ein Tipp neben den Dialog schloss ihn und warf
      // die fertige Analyse weg. Am Handy passiert das mit dem Daumen laufend,
      // und wiederherstellbar war nichts — das Foto ist zu diesem Zeitpunkt
      // schon verworfen. Hinaus geht es über „Abbrechen" (und mit Escape).
      document.addEventListener("click", (e) => {
        if (e.target.closest("#kz-saved-close")) kzCloseModal();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        // Während gerechnet oder gespeichert wird, gibt es kein Zurück — auch
        // nicht über die Taste. Der Vorgang läuft weiter, nicht der Dialog.
        if (kzBusy()) return;
        const pick = document.querySelector(".kz-pick-ov");
        if (pick) { pick.remove(); return; }  // erst den Kategorie-Picker
        if (!kzIstOffen()) return;
        e.preventDefault();
        // Nur der Editor hält eine Analyse; die Gespeichert-Meldung nicht mehr.
        kzCloseModal({ undoable: !!document.getElementById("kz-scan") });
      });
    }
    const scan = document.getElementById("kz-scan");
    if (!scan || scan.dataset.bound) return;
    scan.dataset.bound = "1";
    let state, cats;
    try { state = JSON.parse(scan.dataset.receipt || "{}"); } catch (_) { state = {}; }
    try { cats = JSON.parse(scan.dataset.categories || "[]"); } catch (_) { cats = []; }
    if (!state.items) state.items = [];
    const paper = document.getElementById("kz-paper");
    const catName = (id) => { const c = cats.find((x) => String(x.id) === String(id)); return c ? c.name : null; };

    // Beträge in RAPPEN rechnen: die Gegenprobe geht auf zwei Rappen genau, dafür
    // ist ein Float-Vergleich zu ungenau.
    const rp = (v) => Math.round((parseFloat(String(v == null ? "" : v).replace(",", ".")) || 0) * 100);
    // Die Toleranz kommt vom Server, wo sie aus der Rappenrundung folgt — eine
    // eigene Zahl hier wäre eine zweite Regel, sobald jemand eine davon anfasst.
    // Fehlt sie, gibt es keine Regel: dann gilt die Liste als ungeprüft, nicht als richtig.
    const TOL_RP = state.positions_toleranz == null ? 999999 : rp(state.positions_toleranz);
    // Spanne einer einzelnen Position, in Rappen. Muss zu _PREIS_MIN/_PREIS_MAX
    // in services/receipt_digital.py passen; zwei Zahlen an zwei Orten sind hier
    // in Kauf genommen, weil der Browser die Regel VOR dem Absenden braucht.
    const PREIS_MIN_RP = 1;
    const PREIS_MAX_RP = 1000000;

    function render() {
      // TOTAL = erkanntes Beleg-Total (das wird gespeichert + mit der Bank abgeglichen),
      // NICHT die Summe der Positionen. Fallback: Summe der Positionen, sonst 0.
      const hatTotal = state.amount != null && String(state.amount).trim() !== "";
      let summeRp = 0;
      state.items.forEach((it) => { summeRp += rp(it.price); });
      let total;
      if (hatTotal) {
        total = String(state.amount).trim();
      } else if (state.items.length) {
        total = (summeRp / 100).toFixed(2);
      } else {
        total = "0.00";
      }
      // Gegenprobe nach JEDER Änderung neu. Beide Seiten sind editierbar: wer
      // sieht, dass die Positionen nicht das Total ergeben, korrigiert die
      // Position ODER das Total — und sieht sofort, ob es aufgeht. Ohne
      // erkanntes Total gibt es nichts zu prüfen (Fallback wäre die Summe selbst
      // und ginge immer auf).
      // Jede Position muss FUER SICH in der Spanne liegen — dieselbe Regel, die
      // der Server beim Speichern anwendet (services/receipt_digital.py,
      // _PREIS_MIN/_PREIS_MAX). Sie stand nur dort: eine Gratis-Zeile (0.00)
      // oder ein negativer Preis sah hier "geprueft" aus, der Server verwarf
      // danach ALLE Positionen, und gemeldet wurde nur "gespeichert".
      const alleImRahmen = state.items.every((it) => {
        const r = rp(it.price);
        return r >= PREIS_MIN_RP && r <= PREIS_MAX_RP;
      });
      const geprueft = TOL_RP !== null && state.items.length > 0 && hatTotal && alleImRahmen
        && Math.abs(summeRp - rp(total)) <= TOL_RP;
      // Kopf (Anbieter + Datum) und Total werden IMMER gezeigt — auch ohne Positionen, damit
      // ein erkanntes Total/Datum nutzbar bleibt (man kann die Buchung speichern).
      let h = '<div class="kz-stat">' + KZ_HG + "</div>";
      h += '<div class="kz-head"><div class="kz-shop" data-edit="merchant">' + kzEsc(state.merchant || "—") + "</div>"
         + '<div class="kz-sub" data-edit="date">' + kzEsc(state.date || "Datum?") + "</div></div><hr class=\"kz-rule\">";
      if (state.items.length) {
        h += '<div class="kz-pos">';
        state.items.forEach((it, i) => {
          const cn = it.category_name || catName(it.category_id);
          h += '<div class="kz-row"><span class="nm" data-edit="name" data-i="' + i + '">' + kzEsc(it.name)
             + '</span><span class="pr" data-edit="price" data-i="' + i + '">' + kzEsc(it.price) + "</span>"
             + '<button type="button" class="kz-del" data-del="' + i + '" aria-label="Position entfernen">×</button></div>'
             + '<div class="kz-catline"><span class="kz-pill' + (cn ? "" : " empty") + '" data-pick="' + i + '">'
             + kzEsc(cn || "Kategorie wählen") + "</span></div>";
        });
        h += "</div>";
        if (!geprueft) {
          // Ohne „CHF": die Währung steht eine Zeile tiefer am Total, und auf
          // 375px kostet ein überflüssiges Wort eine ganze Zeile.
          h += '<div class="kz-warn">' + (hatTotal
            ? "Positionen ergeben " + (summeRp / 100).toFixed(2) + " statt " + kzEsc(total)
            // Nicht „kein Total": darunter steht eines. Es ist nur die Summe der
            // Positionen und taugt deshalb nicht als Gegenprobe zu ihnen selbst.
            : "Total nicht vom Beleg") + " — ungeprüft</div>";
        }
      } else {
        const raw = ((document.getElementById("kz-ocr") || {}).value || "").trim();
        const hadText = raw.length > 0 && !raw.startsWith("[OCR-Diagnose]");
        h += '<div class="kz-empty">' + (hadText
          ? "Keine Positionen erkannt"
          : "Kein Text erkannt") + "</div>";
      }
      // Von Hand nachtragen. Ohne diesen Knopf war ein Beleg, den die Erkennung
      // nicht aufschluesselt, eine Sackgasse: Total speichern oder nichts —
      // gemessen an einem Kinobeleg, der GAR KEINE Position hergab. Jetzt
      // schreibt man sie hin. Zwei Positionen von Hand sind schneller als ein
      // zweiter Scan, der wieder nichts findet.
      h += '<button type="button" class="kz-add" id="kz-add">+ Position</button>';
      h += '<hr class="kz-rule"><div class="kz-total"><span>TOTAL CHF</span><span class="pr" data-edit="amount">' + kzEsc(total) + "</span></div>";
      paper.innerHTML = h;
      paper.classList.toggle("unsicher", !geprueft && state.items.length > 0);
      // Der Knopf sagt, was passiert: ungeprüfte Positionen werden nicht
      // gespeichert (der Server verwirft sie ebenfalls, siehe receipt_digital).
      const btn = document.getElementById("kz-confirm");
      if (btn) btn.textContent = (geprueft || !state.items.length)
        ? "Bestätigen & speichern" : "Ohne Positionen speichern";
      // Nach JEDER Änderung sichern, nicht nur beim Öffnen: sonst überlebte die
      // Analyse zwar, die Korrekturen daran aber nicht.
      kzSaveDraft(scan, state);
    }

    paper.addEventListener("click", (e) => {
      if (e.target.closest("#kz-add")) {
        // Preis 0.00 und nicht leer: ein leeres Feld faellt beim Rechnen durch
        // und die Gegenprobe meldete „nicht lesbar" statt „stimmt noch nicht".
        state.items.push({ name: "Position", price: "0.00", category_id: null });
        render();
        const zeilen = paper.querySelectorAll('[data-edit="name"]');
        const letzte = zeilen[zeilen.length - 1];
        if (letzte) letzte.click();  // gleich zum Tippen oeffnen
        return;
      }
      const del = e.target.closest("[data-del]");
      if (del) {
        // Falsch erkannte Position (z. B. MwSt-/Terminal-Zeile) ganz entfernen.
        state.items.splice(parseInt(del.dataset.del, 10), 1);
        render();
        return;
      }
      const pick = e.target.closest("[data-pick]");
      if (pick) { openPicker(parseInt(pick.dataset.pick, 10)); return; }
      const ed = e.target.closest("[data-edit]");
      if (ed && !ed.querySelector("input")) inlineEdit(ed);
    });

    function inlineEdit(el) {
      const field = el.dataset.edit, i = el.dataset.i;
      const inp = document.createElement("input");
      inp.className = "kz-inp";
      inp.value = (field === "merchant" || field === "date") ? (state[field] || "")
        : (field === "amount") ? (state.amount || "")
        : (state.items[i][field] || "");
      el.textContent = ""; el.appendChild(inp); inp.focus(); inp.select();
      const done = () => {
        const v = inp.value.trim();
        if (field === "merchant") state.merchant = v;
        else if (field === "date") state.date = v;
        else if (field === "amount") state.amount = v;
        else state.items[i][field] = v;
        render();
      };
      inp.addEventListener("blur", done);
      inp.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); inp.blur(); } });
    }

    function openPicker(i) {
      const ov = document.createElement("div");
      ov.className = "kz-pick-ov";
      const groups = {};
      cats.forEach((c) => { (groups[c.group] = groups[c.group] || []).push(c); });
      let h = '<div class="kz-pick-sheet"><div class="kz-pick-h">Kategorie wählen</div>';
      Object.keys(groups).forEach((g) => {
        h += '<div class="kz-pick-g">' + kzEsc(g) + "</div>";
        groups[g].forEach((c) => {
          const sel = String(c.id) === String(state.items[i].category_id) ? " sel" : "";
          h += '<button type="button" class="kz-pick-it' + sel + '" data-cid="' + c.id + '">' + kzEsc(c.name) + "</button>";
        });
      });
      h += "</div>";
      ov.innerHTML = h;
      document.body.appendChild(ov);
      ov.addEventListener("click", (e) => {
        const it = e.target.closest("[data-cid]");
        if (it) {
          state.items[i].category_id = parseInt(it.dataset.cid, 10);
          state.items[i].category_name = catName(it.dataset.cid);
          render();
        }
        ov.remove();
      });
    }

    // Speichern per fetch statt htmx.ajax: nur so lässt sich die Sperrschicht
    // über den ganzen Vorgang legen UND am Status ablesen, ob der Server den
    // Beleg wirklich angenommen hat — erst dann darf der Entwurf weg.
    const confirmBtn = document.getElementById("kz-confirm");
    if (confirmBtn) confirmBtn.addEventListener("click", async () => {
      if (kzBusy()) return;
      // Ohne Positionen UND ohne Total gibt es nichts zu speichern; der Server
      // weist das ab (422). Es hier zu sagen, hält den Editor offen — das Total
      // lässt sich nachtragen, statt den Vorgang zu beenden.
      if (!state.items.length && !String(state.amount == null ? "" : state.amount).trim()) {
        showToast("Kein Betrag erkannt — Total antippen und eintragen.");
        return;
      }
      const fd = new FormData();
      fd.append("data", JSON.stringify(state));
      fd.append("ocr_text", (document.getElementById("kz-ocr") || {}).value || "");
      fd.append("image_path", (document.getElementById("kz-image") || {}).value || "");
      kzSetBusy(true, "Beleg wird gespeichert …");
      try {
        const r = await fetch("/import/receipts/photo/confirm",
                              { method: "POST", body: fd, credentials: "same-origin" });
        if (r.ok) {
          const html = await r.text();
          const m = kzModal();
          if (m) { m.innerHTML = html; if (window.htmx) window.htmx.process(m); }
          kzDropDraft();  // erst jetzt liegt der Beleg beim Server
        } else {
          // Abgelehnt heisst: nicht gespeichert. Editor und Entwurf bleiben,
          // sonst wäre die Analyse für nichts verloren.
          showToast("Nicht gespeichert — bitte Total oder Positionen ergänzen.");
        }
      } catch (_) {
        showToast("Speichern fehlgeschlagen — der Beleg bleibt offen.");
      } finally {
        kzSetBusy(false);
      }
    });
    const cancelBtn = document.getElementById("kz-cancel");
    if (cancelBtn) cancelBtn.addEventListener("click", () => kzCloseModal({ undoable: true }));

    render();
  }

  // Gestylter Datei-Wähler (S3): zeigt nach Auswahl den/die Dateinamen an, statt
  // des nativen, englischen „No files selected." — CSP-konform über app.js.
  function initFileFields(root) {
    root = root || document;
    root.querySelectorAll("[data-file-field]").forEach((field) => {
      if (field.dataset.fileBound) return;
      field.dataset.fileBound = "1";
      const input = field.querySelector('input[type="file"]');
      const name = field.querySelector(".file-field-name");
      if (!input || !name) return;
      const empty = name.dataset.empty || "Keine Datei ausgewählt";
      input.addEventListener("change", () => {
        const files = Array.from(input.files || []);
        if (!files.length) {
          name.textContent = empty;
          field.classList.remove("has-files");
          return;
        }
        name.textContent = files.length === 1 ? files[0].name : (files.length + " Dateien ausgewählt");
        field.classList.add("has-files");
      });
    });
  }

  // Zeilen-Aktionsmenü (⋯): EIN document-Listener (Delegation) deckt auch
  // HTMX-neu-gerenderte Zeilen ab → schliesst offene Menüs bei Klick ausserhalb /
  // Escape. Das exklusive Aufklappen macht das native <details name="rowmenu">.
  function initRowMenus() {
    if (window.__rowmenuBound) return;
    window.__rowmenuBound = true;
    // Es ist immer höchstens EIN Menü offen (natives <details name="rowmenu">),
    // daher reicht querySelector + früher Ausstieg statt Scan über alle Zeilen.
    document.addEventListener("click", (e) => {
      const open = document.querySelector("details.rowmenu[open]");
      if (open && !open.contains(e.target)) open.removeAttribute("open");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const open = document.querySelector("details.rowmenu[open]");
      if (open) open.removeAttribute("open");
    });
  }

  // -------- HTMX: server-gerenderte 4xx-Fehler-Partials trotzdem einswappen --
  // HTMX 2.x swap't non-2xx per Default NICHT → Validierungs-/Fehlermeldungen
  // (status 400/404/409/422 mit HTML-Body) gingen sonst verloren und das Formular
  // wirkte tot. Nur HTML-Antworten MIT Inhalt einswappen; 401 (HX-Redirect → Login)
  // und JSON (z.B. FastAPI-422) bleiben unangetastet.
  document.addEventListener("htmx:beforeSwap", (e) => {
    const xhr = e.detail && e.detail.xhr;
    if (!xhr) return;
    const s = xhr.status;
    if (s !== 400 && s !== 404 && s !== 409 && s !== 422 && s !== 429) return;
    const ct = xhr.getResponseHeader("Content-Type") || "";
    if (ct.indexOf("text/html") !== -1 && (xhr.responseText || "").trim()) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });

  // Harte Fehler, die der beforeSwap NICHT inline zeigt (kein HTML-Body, 5xx,
  // 413 zu grosses Foto, Timeout, Netzwerkfehler), würden sonst stumm verpuffen
  // (z.B. der Beleg-FAB-Upload). → Toast als Rückmeldung. Das hx-indicator-Overlay
  // entfernt HTMX nach Request-Ende selbst. 401-Session-Ablauf (HX-Redirect) ausgenommen.
  function htmxHardError(e) {
    const xhr = e.detail && e.detail.xhr;
    if (xhr && xhr.getResponseHeader && xhr.getResponseHeader("HX-Redirect")) return;
    showToast("Etwas hat nicht geklappt — bitte erneut versuchen.");
  }
  document.addEventListener("htmx:responseError", htmxHardError);
  document.addEventListener("htmx:sendError", htmxHardError);
  document.addEventListener("htmx:timeout", htmxHardError);

  // -------- Beleg-Foto: vor dem Upload herunterskalieren --------------------
  // Quittungs-Fotos brauchen keine 12 MP. Wir verkleinern clientseitig auf max.
  // 2400 px (lange Kante) — genau die Auflösung, mit der das OCR rechnet (target=2400 in
  // receipt_ocr._preprocess): scharf genug für die Beträge, spart aber Upload + NAS-Speicher.
  async function downscalePhoto(file, maxEdge) {
    try {
      // „from-image": EXIF-Drehung in die Pixel übernehmen (Canvas verwirft EXIF sonst →
      // Bild käme verdreht an). Greift nur, wenn das Foto eine EXIF-Lage trägt; bei von
      // oben fotografierten Belegen fehlt sie oft → dafür dreht der Server automatisch.
      const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
      const longest = Math.max(bmp.width, bmp.height);
      if (longest <= maxEdge) { if (bmp.close) bmp.close(); return file; }
      const s = maxEdge / longest;
      const w = Math.round(bmp.width * s), h = Math.round(bmp.height * s);
      const c = document.createElement("canvas"); c.width = w; c.height = h;
      c.getContext("2d").drawImage(bmp, 0, 0, w, h);
      if (bmp.close) bmp.close();
      const blob = await new Promise((res) => c.toBlob((b) => res(b), "image/jpeg", 0.85));
      return blob || file;
    } catch (_) { return file; }  // HEIC o.ä. nicht dekodierbar → unverändert hochladen
  }

  function initReceiptPhotoInputs(root) {
    root = root || document;
    root.querySelectorAll("input.js-receipt-photo").forEach((inp) => {
      if (inp.dataset.bound) return;
      inp.dataset.bound = "1";
      inp.addEventListener("change", async () => {
        const file = inp.files && inp.files[0];
        inp.value = "";  // erlaubt erneute Auswahl derselben Datei
        if (!file) return;
        // Das ist das teure Fenster: das Foto liegt nur im Speicher, die Analyse
        // rechnet auf dem NAS. Bis sie da ist, ist nichts anzutippen.
        kzSetBusy(true, "Beleg wird analysiert …");
        try {
          const blob = await downscalePhoto(file, 2400);
          const fd = new FormData();
          // Nur zu .jpg umbenennen, wenn wirklich JPEG re-encodiert wurde — der
          // Fallback (HEIC o.ä. nicht dekodierbar) lädt die ORIGINALDATEI hoch
          // und darf serverseitig nicht als JPEG behandelt werden.
          const name = (blob === file)
            ? (file.name || "beleg")
            : (file.name || "beleg").replace(/\.[^.]+$/, "") + ".jpg";
          fd.append("photo", blob, name);
          const r = await fetch("/import/receipts/photo/start", { method: "POST", body: fd, credentials: "same-origin" });
          if (!r.ok) {
            // Der Server sagt bei 413 (zu gross) und 429 (zu viele offene
            // Erkennungen), WARUM er ablehnt. Ohne diesen Zweig wurde daraus
            // ein pauschales "bitte nochmal" — und "nochmal" ist bei 429
            // genau der falsche Rat: es macht die Warteschlange laenger.
            let grund = "";
            try { grund = (await r.json()).fehler || ""; } catch (_) {}
            const fehler = new Error("upload " + r.status);
            fehler.meldung = grund;
            throw fehler;  // Fehlerseite nie ins Modal swappen
          }
          const { jid } = await r.json();
          kzMerkeAuftrag(jid);
          await kzHoleErgebnis(jid);
        } catch (fehler) {
          kzVergissAuftrag();
          const meldung = (fehler && fehler.meldung) || "Beleg-Upload fehlgeschlagen — bitte nochmal.";
          document.body.dispatchEvent(new CustomEvent("moneten:toast", { detail: { message: meldung } }));
          kzSetBusy(false);
        }
      });
    });
  }

  // -------- Kopier-Buttons (data-copy-target) --------------------------------
  function initCopyButtons() {
    document.querySelectorAll("[data-copy-target]").forEach((b) => {
      if (b.dataset.copyBound) return;
      b.dataset.copyBound = "1";
      b.addEventListener("click", async () => {
        const el = document.querySelector(b.getAttribute("data-copy-target"));
        if (!el) return;
        const txt = el.value != null ? el.value : el.textContent;
        try {
          await navigator.clipboard.writeText(txt);
          const old = b.textContent;
          b.textContent = "✓ Kopiert";
          setTimeout(() => { b.textContent = old; }, 1500);
        } catch (_) {
          if (el.select) { el.focus(); el.select(); }  // Fallback: markieren, manuell kopieren
        }
      });
    });
  }

  // -------- Bootstrap --------------------------------------------------
  // -------- Sankey: Beschriftung gegen die SVG-Skalierung ---------------
  // In SVG zählt `font-size: 12px` ZWÖLF NUTZEREINHEITEN des viewBox, keine
  // Bildschirmpixel. Das Geldfluss-Diagramm hat einen festen viewBox von 1200
  // Einheiten Breite und wird auf die Containerbreite skaliert — am Handy auf
  // 319px, also Faktor 0,266. Eine feste px-Angabe ergab dort 3,2 echte Pixel.
  // Feste Breakpoints treffen das nicht, weil der Faktor stufenlos mit der
  // Breite läuft; darum wird er einmal gemessen und als --flow-scale gesetzt,
  // die CSS-Regel rechnet die Zielgrösse daraus zurück.
  function messeFlow(svg) {
    const vb = svg.viewBox && svg.viewBox.baseVal;
    const breite = svg.getBoundingClientRect().width;
    if (!vb || !vb.width || !breite) return;
    svg.style.setProperty("--flow-scale", (breite / vb.width).toFixed(4));
  }
  // ResizeObserver statt window.resize: die Containerbreite ändert sich auch
  // ohne Fensteränderung (Karten-Layout, aufgeklappte Bereiche), und der
  // Observer feuert zuverlässig nach dem Layout — ein resize-Listener sah beim
  // Umschalten auf Handy-Breite noch die alte Breite.
  const flowRO = typeof ResizeObserver === "function"
    ? new ResizeObserver((eintraege) => eintraege.forEach((e) => {
        messeFlow(e.target);
        zentriereFlow(e.target);
      }))
    : null;
  // Passt das Diagramm nicht in die Karte (am Handy: 900px Mindestbreite gegen
  // 375px Schirm), scrollt sie. Ganz links steht dann fast nur der reservierte
  // Beschriftungsrand — der uninteressanteste Teil. Einmal in die Mitte
  // gestellt, sieht man beim Aufschlagen den Budget-Knoten mit beiden Seiten.
  // Nur EINMAL je Karte: danach gehoert die Scrollposition dem Nutzer.
  function zentriereFlow(svg) {
    const kasten = svg.parentElement;
    if (!kasten || kasten.dataset.flowZentriert) return;
    const ueberhang = kasten.scrollWidth - kasten.clientWidth;
    if (ueberhang <= 0) return;   // passt (noch) — spaeter nochmal versuchen
    kasten.dataset.flowZentriert = "1";
    kasten.scrollLeft = ueberhang / 2;
  }
  function initFlowLabels(root) {
    (root || document).querySelectorAll(".flow-svg").forEach((svg) => {
      messeFlow(svg);
      zentriereFlow(svg);
      if (flowRO && !svg.dataset.flowBound) {
        svg.dataset.flowBound = "1";
        flowRO.observe(svg);
      }
    });
  }
  // Gürtel und Hosenträger: zusätzlich am resize-Ereignis nachmessen. Kostet
  // nichts (entprellt, idempotent) und deckt den Fall ab, dass der Observer
  // nicht greift — etwa bei Geräte-Drehung in älteren WebViews.
  let flowTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(flowTimer);
    flowTimer = setTimeout(() => initFlowLabels(), 150);
  });

  // -------- Treemap: Tooltip am Finger ---------------------------------
  // Die Kachel lässt weg, was nicht hineinpasst — bei schmalen Kacheln den
  // Namen, bei flachen zusätzlich den Betrag. Vollständig steht beides nur in
  // `data-tip`, und der erscheint per `:hover`. Am Handy gibt es kein Hover:
  // dort war die weggelassene Hälfte gar nicht zu bekommen. Ein Griff auf die
  // Kachel stellt den Kasten, der nächste woanders räumt ihn weg — dieselbe
  // Mechanik wie beim Vermögens-Verlauf. Die Maus bleibt aussen vor, für sie
  // tut es `:hover` bereits.
  function initTreemapTipp(root) {
    (root || document).querySelectorAll(".treemap").forEach((karte) => {
      if (karte.dataset.tippBound) return;
      karte.dataset.tippBound = "1";
      karte.addEventListener("pointerdown", (e) => {
        if (e.pointerType === "mouse") return;
        const kachel = e.target.closest(".tm-tile");
        karte.querySelectorAll(".tm-tile.is-tipp").forEach((k) => {
          if (k !== kachel) k.classList.remove("is-tipp");
        });
        if (kachel) kachel.classList.toggle("is-tipp");
      });
    });
  }
  // Der Griff ausserhalb räumt auf. Ein einzelner Wächter am Dokument und nicht
  // einer je Karte: sonst hinge nach jedem HTMX-Tausch ein weiterer daran.
  document.addEventListener("pointerdown", (e) => {
    document.querySelectorAll(".tm-tile.is-tipp").forEach((k) => {
      if (!k.contains(e.target)) k.classList.remove("is-tipp");
    });
  }, true);

  // -------- Budget: Löschschutz fürs Soll-Feld -------------------------
  // Ein geleertes Feld löscht den Standard-Soll (Endpoint: Betrag <= 0 → delete).
  // Ein pauschales hx-confirm am Feld würde bei JEDER Zahlenänderung fragen —
  // darum nur nachfragen, wenn wirklich geleert wurde und vorher etwas drinstand.
  document.body.addEventListener("htmx:confirm", (e) => {
    const el = e.detail && e.detail.elt;
    if (!el || !el.classList || !el.classList.contains("soll-input")) return;
    const leer = !el.value.trim();
    const hatteWert = el.defaultValue && el.defaultValue.trim();
    if (!leer || !hatteWert) return;
    e.preventDefault();
    const name = el.getAttribute("aria-label") || "diese Kategorie";
    if (window.confirm("Soll für " + name.replace(/^Soll für /, "") + " entfernen?")) {
      e.detail.issueRequest(true);
    } else {
      el.value = el.defaultValue;  // Feld zurücksetzen, sonst bleibt es optisch leer
    }
  });

  // -------- Sperre beim Zurueckkehren in die App -------------------------
  // Wer die App verlaesst und spaeter zurueckkommt, soll sich neu anmelden --
  // so machen es Banking-Apps. Serverseitig laeuft die Sitzung ohnehin nach
  // kurzer Untaetigkeit ab; das hier zieht die Sperre nur vor, damit auf dem
  // Bildschirm nicht noch die alten Zahlen stehen, waehrend jemand danebensitzt.
  //
  // WICHTIG -- die Karenzzeit ist kein Schoenheitsfehler, sondern noetig:
  // Beim Beleg-Foto uebernimmt die Kamera-App, die PWA geht in den Hintergrund.
  // Ohne Karenz waere man beim Zurueckkommen abgemeldet, mitsamt der Aufnahme.
  // Dieselbe Lage beim Passkey-Dialog und beim Datei-Waehler.
  //
  // Das ist eine Bequemlichkeits- und Sichtschutz-Ebene, KEINE Zugangskontrolle:
  // ein abgeschaltetes Skript umgeht sie. Die eigentliche Sperre ist die kurze
  // Sitzungsfrist auf dem Server (config.session_max_age_seconds).
  function initSessionLock() {
    const shell = document.querySelector(".app-shell");
    if (!shell || shell.dataset.lockBound) return;
    shell.dataset.lockBound = "1";

    const karenz = (parseInt(shell.dataset.lockGrace, 10) || 45) * 1000;
    let verlassenSeit = null;

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        verlassenSeit = Date.now();
        return;
      }
      if (verlassenSeit === null) return;
      const weg = Date.now() - verlassenSeit;
      verlassenSeit = null;
      if (weg < karenz) return;
      // Ueber /logout statt nur wegnavigieren: die Route loescht das Cookie auf
      // dem Server. Ein reiner Seitenwechsel liesse die Sitzung gueltig.
      window.location.href = "/logout";
    });
  }

  // Dock = alles, was unten FEST im Fenster steht: Tab-Leiste, ein seitenweiter
  // Aktionsbalken, die Schwebeknoepfe. theme.css rechnet Position und
  // Seitenende aus --dock-nav-h/--dock-bar-h/--dock-fab-h; hier werden diese
  // drei Hoehen am echten Kasten nachgemessen.
  //
  // Warum ueberhaupt messen, wo doch Zahlen im Blatt stehen: die Hoehen haengen
  // an der Schriftgroesse, die der Nutzer im Browser einstellt. Waechst die
  // Leiste um zehn Pixel, waechst mit dieser Messung auch der reservierte
  // Streifen — sonst rutschte der letzte Knopf einer Seite unter die Knoepfe.
  // Ohne Skript bleiben die Werte aus theme.css gueltig; die sind fuer die
  // Standard-Schriftgroesse korrekt.
  function initDock() {
    const root = document.documentElement;
    const luft = parseFloat(getComputedStyle(root).getPropertyValue("--dock-gap")) || 0;

    // Hoehe eines festen Elements; 0, wenn es die Seite nicht hat oder es
    // ausgeblendet ist. `luft` kommt nur dazu, wenn die Reihe wirklich belegt
    // ist — sonst summierten sich leere Reihen zu einer Luecke.
    const hoehe = (el, nurWennFest) => {
      if (!el) return 0;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") return 0;
      if (nurWennFest && cs.position !== "fixed") return 0;
      const h = el.getBoundingClientRect().height;
      return h > 0 ? h + luft : 0;
    };

    // Eine gemessene Hoehe ins Dock schreiben — aber NIE eine Null.
    //
    // Ein Inline-Style am :root schlaegt jede Media-Query, auch die
    // Rueckfallebene in theme.css. "0" heisst hier nicht "die Reihe ist 0 hoch",
    // sondern "an dieser Stelle war nichts zu messen" — etwa, weil die Messung
    // eine Desktop-Anordnung erwischt hat, wo unten nichts feststeht. Schrieb
    // man diese Null hin, ueberlebte sie den Wechsel in den Handy-Bereich und
    // ersetzte dort die 66px/64px des Blattes: gemessen fiel --dock-h auf 10px,
    // Tab-Leiste (y958-1024), Schwebeknoepfe (y960-1014) und Aktionsbalken
    // (y951-1014) lagen uebereinander. Darum wird die Eigenschaft entfernt
    // statt genullt — dann gilt wieder, was in theme.css steht.
    const setzen = (name, wert) => {
      if (wert > 0) root.style.setProperty(name, wert + "px");
      else root.style.removeProperty(name);
    };

    const messen = () => {
      const nav = document.querySelector(".mobile-nav");
      const navH = nav && getComputedStyle(nav).display !== "none"
        ? nav.getBoundingClientRect().height : 0;
      setzen("--dock-nav-h", navH);
      setzen("--dock-bar-h", hoehe(document.querySelector(".quick-submit"), true));
      setzen("--dock-fab-h", hoehe(document.querySelector(".fab"), false));
    };

    messen();
    // Der Umbruch am Breakpoint blendet Leiste und Knoepfe ein/aus, und eine
    // Drehung aendert die Hoehen — beides muss neu gemessen werden.
    window.addEventListener("resize", messen);
    document.addEventListener("htmx:afterSettle", messen);
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(messen);
      [".mobile-nav", ".quick-submit", ".fab"].forEach((sel) => {
        const el = document.querySelector(sel);
        if (el) ro.observe(el);
      });
    }
  }

  function boot() {
    // Konten-Verwaltungsmodus (aus Einstellungen via ?manage=1): blendet die
    // Bearbeiten-/Archivieren-/Löschen-Icons + „+ Konto" ein. Bleibt über HTMX-Swaps
    // aktiv, da die Klasse am <body> hängt (nicht im neu gerenderten #accounts-root).
    if (new URLSearchParams(location.search).get("manage") === "1") document.body.classList.add("acc-manage");
    initTheme(); initPinPad(); initDonuts(); initCountUp(); initDonutDraw();
    initQuickPills(); initWebAuthn(); initCandFilter(); initCatPicker(); initSparkTooltip();
    initVerlaufHover(); initPositionsBalken();
    initImportProgress(); initTxTimeline(); initReceiptModal(); initSplitEditor(); initLohnEditor();
    initReceiptFilter(); initConfirmForms(); initLearnToggle(); initReceiptPicker(); initStressLive();
    initQuickCatSearch(); initCategoryAdmin(); initMoreSheet(); initInboxGroup(); initInboxGlobalSearch(); initBulkGuard();
    initSessionLock(); initDock();
    initUndoTriggers(); focusAmount(); initFilterMemory(true); initReceiptScan(); initFileFields(); initRowMenus(); initCmdK(); initReceiptPhotoInputs(); initCopyButtons();
    initFlowLabels(); initTreemapTipp();
    // Ein unterbrochener Beleg-Scan kommt zurück, statt still verlorenzugehen.
    // NUR beim Laden der Seite, nicht nach HTMX-Swaps: sonst spränge der Dialog
    // bei jedem Nachladen einer Liste erneut auf.
    kzRestoreDraft();
  }
  // Nach HTMX-Swaps neu gerenderte Widgets erneut binden (Events bubbeln zum document).
  // Element-Bindings nur im geswappten Teilbaum suchen (root = e.detail.target) statt
  // dokumentweit — die dataset.bound-Guards bleiben als zweite Sicherung erhalten.
  document.addEventListener("htmx:afterSwap", (e) => {
    // Bei outerHTML-Swaps kann detail.target das ERSETZTE (bereits aus dem DOM
    // entfernte) Element sein — dann dokumentweit re-initialisieren, sonst
    // blieben die neu eingeswappten Inhalte ungebunden.
    const t = e.detail && e.detail.target;
    const root = (t && document.contains(t)) ? t : document;
    initCandFilter(root); initCatPicker(root); initSparkTooltip(root); initVerlaufHover(root); initPositionsBalken(root);
    initTxTimeline();
    initReceiptModal(root); initSplitEditor(); initLohnEditor();
    initReceiptFilter(); initConfirmForms(root); initLearnToggle(root); initReceiptPicker(root); initStressLive();
    initQuickCatSearch(); initCategoryAdmin(); initMoreSheet(); initInboxGroup(root); initInboxGlobalSearch(); initBulkGuard();
    initUndoTriggers(); focusAmount(); initFilterMemory(false); initReceiptScan(); initFileFields(root); initReceiptPhotoInputs(root);
    initFlowLabels(root === document ? null : root);
    initTreemapTipp(root === document ? null : root);
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
