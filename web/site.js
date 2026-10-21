// Shared by every page: the saved theme (applied before first paint, so this loads in <head> without defer)
// and the contact links, which read the address from <body data-contact>.
(function () {
  "use strict";
  var t = null;
  try { t = localStorage.getItem("sia-theme"); } catch (e) {}
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);

  document.addEventListener("DOMContentLoaded", function () {
    var email = document.body.getAttribute("data-contact") || "";
    var repo = document.body.getAttribute("data-repo") || "https://github.com/SrijithLegend/Self-Improving-Agent";
    Array.prototype.forEach.call(document.querySelectorAll("[data-contact-link]"), function (a) {
      // without an address, point at the issue tracker so the link is never dead
      a.href = email ? "mailto:" + email : repo + "/issues";
      if (a.hasAttribute("data-contact-show")) a.textContent = email || "the issue tracker on GitHub";
    });
  });
})();
