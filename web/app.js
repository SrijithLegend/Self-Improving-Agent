// SIA landing page: theme, Human/Agent view, scroll-spy, copy prompt, code tabs, hero canvas, mobile drawer.
// No dependencies. Everything that touches storage or the clipboard is wrapped, so the page works without them.
(function () {
  "use strict";
  var root = document.documentElement;
  var $ = function (s, el) { return (el || document).querySelector(s); };
  var $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  var live = $("#live");

  function store(k, v) {
    try { if (v === undefined) return localStorage.getItem(k); if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); }
    catch (e) { return null; }
  }
  function announce(msg) { live.textContent = ""; setTimeout(function () { live.textContent = msg; }, 30); }

  // --- repo links come from <body data-repo> --------------------------------------------------------
  var repo = document.body.getAttribute("data-repo");
  if (repo) $$("[data-repo-link]").forEach(function (a) { a.href = repo + (a.getAttribute("data-repo-path") || ""); });

  // --- radio groups (view toggle, theme switch): click + arrow keys --------------------------------
  function radioGroup(group, onPick) {
    var items = $$('[role="radio"]', group);
    function pick(btn, focus) {
      items.forEach(function (b) {
        var on = b === btn;
        b.setAttribute("aria-checked", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
      });
      if (focus) btn.focus();
    }
    items.forEach(function (b, i) {
      b.addEventListener("click", function () { pick(b); onPick(b); });
      b.addEventListener("keydown", function (e) {
        var d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
        if (!d) return;
        e.preventDefault();
        var next = items[(i + d + items.length) % items.length];
        pick(next, true); onPick(next);
      });
    });
    return function set(match) { var b = items.filter(match)[0]; if (b) pick(b); };
  }

  // --- theme: light / dark / system ----------------------------------------------------------------
  var setThemeUI = radioGroup($(".theme"), function (b) {
    var t = b.getAttribute("data-theme-set");
    if (t === "system") { root.removeAttribute("data-theme"); store("sia-theme", null); }
    else { root.setAttribute("data-theme", t); store("sia-theme", t); }
  });
  var savedTheme = root.getAttribute("data-theme") || "system";
  setThemeUI(function (b) { return b.getAttribute("data-theme-set") === savedTheme; });

  // --- Human / Agent view --------------------------------------------------------------------------
  var mdText = null;
  function loadMarkdown() {
    if (mdText !== null) return;
    mdText = "";
    fetch("/agent.md").then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (t) { mdText = t; $("#agent-text").textContent = t; })
      .catch(function () { mdText = null; $("#agent-text").textContent = "Couldn't load /agent.md. Open it directly from the link above."; });
  }
  function setView(v, push) {
    if (v === "agent") { root.setAttribute("data-view", "agent"); loadMarkdown(); }
    else root.removeAttribute("data-view");
    store("sia-view", v);
    if (push) {
      var u = new URL(location.href);
      if (v === "agent") u.searchParams.set("view", "agent"); else u.searchParams.delete("view");
      history.replaceState(null, "", u);
    }
  }
  var setViewUI = radioGroup($(".toggle"), function (b) { setView(b.getAttribute("data-view"), true); });
  var startView = root.getAttribute("data-view") === "agent" ? "agent" : "human";
  setViewUI(function (b) { return b.getAttribute("data-view") === startView; });
  setView(startView, true);

  // --- clipboard with a textarea fallback ----------------------------------------------------------
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }
  function copy(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return fallbackCopy(text); });
      }
    } catch (e) { /* fall through */ }
    return Promise.resolve(fallbackCopy(text));
  }
  function flash(el, done, msg) {
    var old = el.getAttribute("data-label") || el.textContent;
    el.setAttribute("data-label", old);
    el.textContent = done;
    announce(msg);
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.textContent = old; }, 1600);
  }

  // --- setup prompt (3 variants) -------------------------------------------------------------------
  var BASE_PROMPT = [
    "Integrate the SIA self-improving agent API into this project.",
    "- Base URL: read SIA_URL from env (default http://localhost:8000). Auth header: Authorization: Bearer $SIA_KEY.",
    "- LLM key: send X-LLM-Provider and X-LLM-Key headers (default provider gemini). Never log or store the LLM key.",
    "- Flow: POST /v1/agents to create one agent; POST /v1/agents/{id}/knowledge for docs;",
    "  POST /v1/agents/{id}/run with {\"task\": ...}; POST /v1/agents/{id}/feedback with {\"episode_id\", \"rating\", \"correction\"}.",
    "- Show the user the `answer`; keep `episode_id` so the user can rate it.",
    "- Put the client in one module with typed request/response models and handle 400/401/404/502."
  ].join("\n");
  var PROMPTS = {
    claude: BASE_PROMPT,
    // Cursor: the same instructions as a project rule (save as .cursor/rules/sia.mdc, or paste into Agent chat)
    cursor: ["---", "description: How this project talks to the SIA self-improving agent API", "alwaysApply: true", "---", "", BASE_PROMPT].join("\n"),
    curl: [
      "# SIA in 6 calls. Set SIA_KEY (your SIA key) and LLM_KEY (your model provider key) first.",
      "# helper: POSTs when -d is given, GETs otherwise",
      "sia() { curl -s -H \"Authorization: Bearer $SIA_KEY\" -H \"X-LLM-Key: $LLM_KEY\" -H \"Content-Type: application/json\" \"localhost:8000$@\"; }",
      "",
      "# 1. create an agent",
      "sia /v1/agents -d '{\"name\":\"tax-bot\",\"domain\":\"Indian personal income tax\",\"instructions\":\"Cite the section of the Act. Show calculations.\",\"rubric\":\"Correct section cited; arithmetic right; flags when a CA is needed.\"}'",
      "# 2. teach it: a document and an example with the right answer",
      "sia /v1/agents/$ID/knowledge -d '{\"title\":\"Section 80C\",\"content\":\"...\"}'",
      "sia /v1/agents/$ID/examples  -d '{\"input\":\"...\",\"expected\":\"...\"}'",
      "# 3. ask it something",
      "sia /v1/agents/$ID/run -d '{\"task\":\"Can I claim my PPF under 80C?\"}'",
      "# 4. correct it when it's wrong",
      "sia /v1/agents/$ID/feedback -d '{\"episode_id\":\"...\",\"rating\":2,\"correction\":\"...\"}'",
      "# 5. let it study, then improve",
      "sia /v1/agents/$ID/study   -d '{\"rounds\":5}'",
      "sia /v1/agents/$ID/improve -d '{\"trials\":2}'",
      "# 6. inspect it",
      "sia /v1/agents/$ID"
    ].join("\n")
  };
  var NAMES = { claude: "Claude Code", cursor: "Cursor", curl: "curl" };
  var variant = "claude";
  var copyBtn = $("[data-copy-prompt]"), longLabel = $(".setup-label .long"), shortLabel = $(".setup-label .short");
  var more = $(".setup-more"), menu = $("#setup-menu"), menuItems = $$("button", menu);

  function copyPrompt() {
    copy(PROMPTS[variant]).then(function (ok) {
      if (!ok) { announce("Copy failed. Your browser blocked clipboard access."); return; }
      [longLabel, shortLabel].forEach(function (l) { flash(l, "Copied ✓", NAMES[variant] + " setup prompt copied"); });
    });
  }
  copyBtn.addEventListener("click", copyPrompt);

  function openMenu(focusIdx) {
    menu.hidden = false; more.setAttribute("aria-expanded", "true");
    menuItems[focusIdx === undefined ? menuItems.findIndex(function (b) { return b.getAttribute("aria-checked") === "true"; }) : focusIdx].focus();
  }
  function closeMenu(refocus) {
    if (menu.hidden) return;
    menu.hidden = true; more.setAttribute("aria-expanded", "false");
    if (refocus) more.focus();
  }
  more.addEventListener("click", function () { if (menu.hidden) openMenu(); else closeMenu(true); });
  more.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); openMenu(e.key === "ArrowDown" ? 0 : menuItems.length - 1); }
  });
  menuItems.forEach(function (b, i) {
    b.addEventListener("click", function () {
      variant = b.getAttribute("data-variant");
      menuItems.forEach(function (x) { x.setAttribute("aria-checked", x === b ? "true" : "false"); });
      $("#setup-variant").textContent = "Variant: " + NAMES[variant];
      closeMenu(true);
      copyPrompt();
    });
    b.addEventListener("keydown", function (e) {
      var d = { ArrowDown: 1, ArrowUp: -1 }[e.key];
      if (d) { e.preventDefault(); menuItems[(i + d + menuItems.length) % menuItems.length].focus(); }
      else if (e.key === "Home") { e.preventDefault(); menuItems[0].focus(); }
      else if (e.key === "End") { e.preventDefault(); menuItems[menuItems.length - 1].focus(); }
      else if (e.key === "Escape") { e.preventDefault(); closeMenu(true); }
      else if (e.key === "Tab") closeMenu(false);
    });
  });
  document.addEventListener("click", function (e) { if (!e.target.closest(".setup")) closeMenu(false); });

  // --- code tabs + copy ----------------------------------------------------------------------------
  var tabs = $$('[role="tab"]');
  function selectTab(t, focus) {
    tabs.forEach(function (x) {
      var on = x === t;
      x.setAttribute("aria-selected", on ? "true" : "false");
      x.tabIndex = on ? 0 : -1;
      document.getElementById(x.getAttribute("aria-controls")).hidden = !on;
    });
    if (focus) t.focus();
  }
  tabs.forEach(function (t, i) {
    t.addEventListener("click", function () { selectTab(t); });
    t.addEventListener("keydown", function (e) {
      var d = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (d) { e.preventDefault(); selectTab(tabs[(i + d + tabs.length) % tabs.length], true); }
      else if (e.key === "Home") { e.preventDefault(); selectTab(tabs[0], true); }
      else if (e.key === "End") { e.preventDefault(); selectTab(tabs[tabs.length - 1], true); }
    });
  });
  $("[data-copy-code]").addEventListener("click", function (e) {
    var panel = $('[role="tabpanel"]:not([hidden])');
    var btn = e.currentTarget;
    copy(panel.textContent).then(function (ok) {
      if (ok) flash(btn, "Copied ✓", "Code copied"); else announce("Copy failed. Your browser blocked clipboard access.");
    });
  });
  $("[data-copy-md]").addEventListener("click", function (e) {
    var btn = e.currentTarget;
    var text = mdText || $("#agent-text").textContent;
    copy(text).then(function (ok) {
      if (ok) flash(btn, "Copied ✓", "Markdown copied"); else announce("Copy failed. Your browser blocked clipboard access.");
    });
  });

  // --- hosted waitlist: mailto only, nothing is sent from this page --------------------------------
  $("#notify").addEventListener("submit", function (e) {
    e.preventDefault();
    var email = $("#notify-email").value.trim();
    // the recipient comes from <body data-contact>; empty leaves the To: field for the visitor to fill in
    location.href = "mailto:" + encodeURIComponent(document.body.getAttribute("data-contact") || "") +
      "?subject=" + encodeURIComponent("SIA hosted: notify me") +
      "&body=" + encodeURIComponent("Please let me know when hosted SIA opens.\n\nMy email: " + email);
  });

  // --- scroll-spy ----------------------------------------------------------------------------------
  var spyItems = $$(".spy li");
  var sections = spyItems.map(function (li) { return document.getElementById(li.firstElementChild.getAttribute("href").slice(1)); });
  function activate(id) {
    spyItems.forEach(function (li) {
      var on = li.firstElementChild.getAttribute("href") === "#" + id;
      li.classList.toggle("active", on);
      if (on) li.firstElementChild.setAttribute("aria-current", "location"); else li.firstElementChild.removeAttribute("aria-current");
    });
  }
  if ("IntersectionObserver" in window) {
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) activate(en.target.id); });
    }, { rootMargin: "-40% 0px -55% 0px" });
    sections.forEach(function (s) { if (s) spy.observe(s); });
  }
  activate(sections[0].id);
  // in-page links: smooth unless reduced motion (CSS handles that), move focus to the target, close the drawer
  $$('a[href^="#"]').forEach(function (a) {
    a.addEventListener("click", function (e) {
      var id = a.getAttribute("href").slice(1);
      var target = id === "top" ? document.body : document.getElementById(id);
      if (!target) return;
      e.preventDefault();
      if (root.getAttribute("data-view") === "agent") { setViewUI(function (b) { return b.getAttribute("data-view") === "human"; }); setView("human", true); }
      closeDrawer(false);
      if (id === "top") window.scrollTo({ top: 0, behavior: reduced.matches ? "auto" : "smooth" });
      else {
        target.scrollIntoView({ behavior: reduced.matches ? "auto" : "smooth", block: "start" });
        if (spyItems.some(function (li) { return li.firstElementChild.getAttribute("href") === "#" + id; })) activate(id);
        var h = target.querySelector("h2");
        if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
      }
      history.replaceState(null, "", id === "top" ? location.pathname + location.search : "#" + id);
    });
  });

  // --- mobile drawer: Esc and backdrop close it, focus stays inside while open ---------------------
  var drawer = $("#drawer"), burger = $(".burger"), backdrop = $(".backdrop"), closeBtn = $(".drawer-close");
  var mobile = window.matchMedia("(max-width: 1023px)");
  function focusables() {
    return $$("a[href], button:not([disabled])", drawer).filter(function (el) { return el.offsetParent !== null; });
  }
  function openDrawer() {
    drawer.classList.add("open"); backdrop.hidden = false;
    burger.setAttribute("aria-expanded", "true");
    drawer.setAttribute("role", "dialog"); drawer.setAttribute("aria-modal", "true"); drawer.setAttribute("aria-label", "Menu");
    document.body.style.overflow = "hidden";
    setTimeout(function () { closeBtn.focus(); }, 30);
  }
  function closeDrawer(refocus) {
    if (!drawer.classList.contains("open")) return;
    drawer.classList.remove("open"); backdrop.hidden = true;
    burger.setAttribute("aria-expanded", "false");
    drawer.removeAttribute("role"); drawer.removeAttribute("aria-modal"); drawer.removeAttribute("aria-label");
    document.body.style.overflow = "";
    if (refocus) burger.focus();
  }
  burger.addEventListener("click", openDrawer);
  closeBtn.addEventListener("click", function () { closeDrawer(true); });
  backdrop.addEventListener("click", function () { closeDrawer(true); });
  document.addEventListener("keydown", function (e) {
    if (!drawer.classList.contains("open")) return;
    if (e.key === "Escape") { e.preventDefault(); closeDrawer(true); return; }
    if (e.key !== "Tab") return;
    var f = focusables(); if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (!drawer.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  // on desktop the drawer is the static sidebar: make sure a resize never leaves it locked open
  (mobile.addEventListener ? mobile.addEventListener.bind(mobile, "change") : mobile.addListener.bind(mobile))(function () {
    if (!mobile.matches) closeDrawer(false);
  });

  // --- hero canvas: three learning loops ------------------------------------------------------------
  var canvas = $("#hero");
  if (canvas && canvas.getContext) hero(canvas);

  function hero(cv) {
    var ctx = cv.getContext("2d");
    var TEAL = "#3DB3A0", AMBER = "#F0B457", WHITE = "rgba(255,255,255,0.4)", BG = "#0E1B2A";
    var KNOW = 0, EPI = 1, LESSON = 2;
    var W = 0, H = 0, dpr = 1, parts = [], running = false, visible = true, raf = 0, t0 = performance.now(), lastLearn = 0;
    // three tilted, flattened ellipses that all pass close to the centre (like the loop in the logo)
    var ORBITS = [{ tilt: -0.42, speed: 0.16 }, { tilt: 0.62, speed: -0.12 }, { tilt: 1.62, speed: 0.1 }];

    function rand(a, b) { return a + Math.random() * (b - a); }
    function build() {
      var n = window.innerWidth < 768 ? 500 : 900;
      parts = [];
      for (var i = 0; i < n; i++) {
        var r = Math.random();
        parts.push({
          o: i % 3, a: Math.random() * Math.PI * 2, sp: rand(0.6, 1.4),
          dr: rand(-1, 1), ph: Math.random() * Math.PI * 2, f: rand(0.4, 1.2),
          kind: r < 0.38 ? KNOW : r < 0.82 ? EPI : LESSON, size: Math.random() < 0.5 ? 2 : 3, glow: 0
        });
      }
    }
    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = cv.clientWidth; H = cv.clientHeight;
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    function readBg() { BG = getComputedStyle(cv.parentNode).backgroundColor || BG; }
    function pos(p, t) {
      var orb = ORBITS[p.o], R = Math.min(W * 0.42, H * 0.78);
      var a = R * 0.66, b = R * 0.19;
      var noise = 1 + 0.06 * p.dr + 0.025 * Math.sin(t * p.f + p.ph);
      var th = p.a;
      var x = Math.cos(th) * a * noise, y = Math.sin(th) * b * noise;
      var c = Math.cos(orb.tilt), s = Math.sin(orb.tilt);
      return [W / 2 + x * c - y * s, H / 2 + x * s + y * c, Math.sin(th)];
    }
    function frame(now, step) {
      var t = (now - t0) / 1000;
      ctx.fillStyle = BG; ctx.fillRect(0, 0, W, H);
      // a lesson is learned: about every 3 s, white episodes near the centre turn amber; old lessons retire
      if (step && now - lastLearn > 3000) {
        lastLearn = now;
        var near = parts.filter(function (p) {
          if (p.kind !== EPI) return false;
          var q = pos(p, t), dx = q[0] - W / 2, dy = q[1] - H / 2;
          return dx * dx + dy * dy < Math.pow(Math.min(W, H) * 0.09, 2);
        }).slice(0, 6);
        near.forEach(function (p) { p.kind = LESSON; p.glow = 1; });
        var lessons = parts.filter(function (p) { return p.kind === LESSON && !p.glow; });
        for (var k = 0; k < near.length && lessons.length; k++) lessons.splice(Math.floor(Math.random() * lessons.length), 1)[0].kind = EPI;
      }
      var buckets = [[], [], []];
      for (var i = 0; i < parts.length; i++) {
        var p = parts[i];
        if (step) p.a += ORBITS[p.o].speed * p.sp * step;
        buckets[p.kind].push(p);
      }
      var colors = [TEAL, WHITE, AMBER];
      for (var kind = 0; kind < 3; kind++) {
        ctx.fillStyle = colors[kind];
        var list = buckets[kind];
        for (var j = 0; j < list.length; j++) {
          var q = pos(list[j], t), s = list[j].size;
          ctx.globalAlpha = 0.55 + 0.45 * (q[2] * 0.5 + 0.5);  // back of the orbit is dimmer
          ctx.fillRect(q[0] - s / 2, q[1] - s / 2, s, s);
          if (list[j].glow > 0) {
            var g = 3 + 6 * list[j].glow;
            ctx.globalAlpha = 0.5 * list[j].glow;
            ctx.fillRect(q[0] - g / 2, q[1] - g / 2, g, g);
            if (step) list[j].glow = Math.max(0, list[j].glow - step * 0.6);
          }
        }
      }
      // the centre: the amber dot from the logo
      ctx.globalAlpha = 1; ctx.fillStyle = AMBER;
      var c = Math.max(5, Math.min(W, H) * 0.018);
      ctx.beginPath(); ctx.arc(W / 2, H / 2, c, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 0.18; ctx.beginPath(); ctx.arc(W / 2, H / 2, c * 2.6, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
    }
    var last = 0;
    function loop(now) {
      var step = last ? Math.min((now - last) / 1000, 0.05) : 0;
      last = now;
      frame(now, step);
      raf = requestAnimationFrame(loop);
    }
    function update() {
      var should = visible && !document.hidden && !reduced.matches;
      if (should && !running) { running = true; last = 0; raf = requestAnimationFrame(loop); }
      else if (!should && running) { running = false; cancelAnimationFrame(raf); }
      if (!should) frame(performance.now(), 0);  // one static frame
      cv.setAttribute("data-running", running ? "1" : "0");
    }
    build(); resize();
    var lastW = window.innerWidth;
    if ("ResizeObserver" in window) new ResizeObserver(function () {
      resize();
      if ((window.innerWidth < 768) !== (lastW < 768)) build();
      lastW = window.innerWidth;
      if (!running) frame(performance.now(), 0);
    }).observe(cv);
    if ("IntersectionObserver" in window) new IntersectionObserver(function (en) { visible = en[0].isIntersecting; update(); }).observe(cv);
    document.addEventListener("visibilitychange", update);
    (reduced.addEventListener ? reduced.addEventListener.bind(reduced, "change") : reduced.addListener.bind(reduced))(update);
    // re-read the background (--hero-bg) when the theme changes
    function themeChanged() { readBg(); if (!running) frame(performance.now(), 0); }
    new MutationObserver(themeChanged).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    var scheme = window.matchMedia("(prefers-color-scheme: dark)");
    (scheme.addEventListener ? scheme.addEventListener.bind(scheme, "change") : scheme.addListener.bind(scheme))(themeChanged);
    readBg();
    update();
  }
})();
