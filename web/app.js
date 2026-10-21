// SIA landing page: theme, Human/Agent view, scroll-spy, copy prompt, code tabs, hero diagram, mobile drawer.
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
  var mdText = null, mdLoading = false;
  function loadMarkdown() {
    if (mdText !== null || mdLoading) return;
    var pre = $("#agent-text"), err = $("#agent-error");
    mdLoading = true; err.hidden = true; pre.hidden = false;
    pre.setAttribute("data-state", "loading"); pre.setAttribute("aria-busy", "true"); pre.textContent = "Loading agent.md…";
    fetch("/agent.md").then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (t) {
      mdText = t; pre.textContent = t; pre.removeAttribute("data-state");
    }).catch(function () {
      pre.hidden = true; err.hidden = false;
    }).then(function () { mdLoading = false; pre.removeAttribute("aria-busy"); });
  }
  $("[data-retry-md]").addEventListener("click", loadMarkdown);
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
    "Add SIA DevTools to this project so prompt changes are tested before they ship.",
    "- Install: pip install \"sia-devtools[server] @ git+https://github.com/SrijithLegend/Self-Improving-Agent\"",
    "- Create one client: sia = SIAClient(project=\"<this app>\"). Local mode needs no key; for the hosted API read",
    "  SIA_API_KEY and SIA_BASE_URL from env and use an ingest-scope key in production. Never log either.",
    "- Decorate the function that calls the model with @sia.trace (it records input, output, latency, errors).",
    "- Read the system prompt with sia.get_prompt(default=<current prompt text>) and pass it to the model.",
    "- Make the target testable as def answer(input, prompt=None).",
    "- Create a dataset of 10+ real inputs with expected outputs; mark must-not-break cases critical=True.",
    "- Write deterministic scorers with @scorer(\"name\") and run sia.evaluate(dataset, answer, [scorers]).",
    "- Add a CI step: sia eval gate --dataset <name> --target <module>:answer --candidate-file <prompt file>",
    "  --max-regressions 0 (exit 0 pass, 1 fail, 3 inconclusive)."
  ].join("\n");
  var PROMPTS = {
    claude: BASE_PROMPT,
    // Cursor: the same instructions as a project rule (save as .cursor/rules/sia.mdc, or paste into Agent chat)
    cursor: ["---", "description: How this project tests prompt changes with SIA DevTools", "alwaysApply: true", "---", "", BASE_PROMPT].join("\n"),
    shell: [
      "# SIA DevTools from the shell. Local mode: no account, writes to sia_local.db.",
      "pip install \"sia-devtools[server] @ git+https://github.com/SrijithLegend/Self-Improving-Agent\"",
      "",
      "# the whole loop with fictional data (scripted model, no key needed)",
      "python examples/devtools_demo.py --offline",
      "",
      "# your runs, datasets and evaluations",
      "sia dashboard",
      "",
      "# gate a prompt change against the active version",
      "sia eval gate --dataset regression --target app:answer --scorer exact_match \\",
      "  --candidate-file prompts/system_prompt.txt --min-score 0.85 --max-regressions 0",
      "echo $?   # 0 pass, 1 fail, 3 inconclusive"
    ].join("\n")
  };
  var NAMES = { claude: "Claude Code", cursor: "Cursor", shell: "Shell" };
  var variant = "claude";
  var copyBtn = $("[data-copy-prompt]"), longLabel = $(".setup-label .long"), shortLabel = $(".setup-label .short");
  var more = $(".setup-more"), menu = $("#setup-menu"), menuItems = $$("button", menu);

  function copyPrompt() {
    copy(PROMPTS[variant]).then(function (ok) {
      [longLabel, shortLabel].forEach(function (l) {
        if (ok) flash(l, "Copied", NAMES[variant] + " setup prompt copied");
        else flash(l, "Copy failed", "Copy failed. Your browser blocked clipboard access.");
      });
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
      if (ok) flash(btn, "Copied", "Code copied"); else flash(btn, "Copy failed", "Copy failed. Your browser blocked clipboard access.");
    });
  });
  $("[data-copy-md]").addEventListener("click", function (e) {
    var btn = e.currentTarget;
    var text = mdText || $("#agent-text").textContent;
    copy(text).then(function (ok) {
      if (ok) flash(btn, "Copied", "Markdown copied"); else flash(btn, "Copy failed", "Copy failed. Your browser blocked clipboard access.");
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
  // The observer only says *when* something crossed the band; the active section is then the last one whose top
  // is above the band (so fast scrolls and jumps can't skip one), or the last section once the page bottom shows.
  var foot = $(".foot");
  function spyUpdate() {
    if (root.getAttribute("data-view") === "agent") return;
    var line = window.innerHeight * 0.45, current = sections[0];
    sections.forEach(function (s) { if (s.getBoundingClientRect().top <= line) current = s; });
    if (foot.getBoundingClientRect().bottom <= window.innerHeight + 2 && window.scrollY > 0) current = sections[sections.length - 1];
    activate(current.id);
  }
  if ("IntersectionObserver" in window) {
    var spy = new IntersectionObserver(spyUpdate, { rootMargin: "-40% 0px -55% 0px" });
    sections.forEach(function (s) { spy.observe(s); });
    new IntersectionObserver(spyUpdate, { threshold: [0, 1] }).observe(foot);
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

  // --- hero diagram: the page's one animation. A failed run travels through the five steps. ---------
  var loops = $("#loops");
  if (loops) heroLoops(loops);

  function heroLoops(svg) {
    var path = $("#loops-path", svg), token = $("#loops-token", svg);
    if (!path.getTotalLength) return;
    var len = path.getTotalLength(), LOOP_MS = 14000, running = false, visible = true, raf = 0, start = 0;
    function place(frac) {
      var pt = path.getPointAtLength(frac * len);
      token.setAttribute("x", (pt.x - 6).toFixed(1)); token.setAttribute("y", (pt.y - 6).toFixed(1));
    }
    function tick(now) {
      if (!start) start = now;
      place(((now - start) % LOOP_MS) / LOOP_MS);
      raf = requestAnimationFrame(tick);
    }
    function update() {
      var should = visible && !document.hidden && !reduced.matches;
      if (should && !running) { running = true; start = 0; raf = requestAnimationFrame(tick); }
      else if (!should && running) { running = false; cancelAnimationFrame(raf); }
      if (reduced.matches) place(0.375);  // still: the token rests between feedback and dataset
      svg.setAttribute("data-running", running ? "1" : "0");
    }
    if ("IntersectionObserver" in window) new IntersectionObserver(function (en) { visible = en[0].isIntersecting; update(); }).observe(svg);
    document.addEventListener("visibilitychange", update);
    (reduced.addEventListener ? reduced.addEventListener.bind(reduced, "change") : reduced.addListener.bind(reduced))(update);
    update();
  }
})();
