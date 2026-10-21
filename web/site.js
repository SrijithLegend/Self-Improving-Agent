// Shared by every page: the saved theme (applied before first paint, so this loads in <head> without defer)
// and the contact links, which read the address from /site-config.json (SIA_CONTACT_EMAIL on the server).
(function () {
  "use strict";
  var t = null;
  try { t = localStorage.getItem("sia-theme"); } catch (e) {}
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);

  // without an address (unset, or the fetch failed) the links keep their GitHub-issues href, so they're never dead
  var config = fetch("/site-config.json").then(function (r) { return r.ok ? r.json() : {}; }, function () { return {}; });
  document.addEventListener("DOMContentLoaded", function () {
    config.then(function (c) {
      var email = c && typeof c.contact_email === "string" ? c.contact_email : "";
      if (!email) return;
      Array.prototype.forEach.call(document.querySelectorAll("[data-contact-link]"), function (a) {
        a.href = "mailto:" + email;
        if (a.hasAttribute("data-contact-show")) a.textContent = email;
      });
    });
  });
})();
