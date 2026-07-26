// Knowledge Archive — small progressive-enhancement script.
// No frameworks, no build step needed for this file.
(function () {
  "use strict";

  var scriptEl = document.currentScript;
  var ROOT = (scriptEl && scriptEl.getAttribute("data-root")) || "";

  // ---- theme toggle ----------------------------------------------------
  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      var next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("theme", next);
    });
  }

  // ---- random article ----------------------------------------------------
  var randomBtn = document.getElementById("random-btn");
  if (randomBtn) {
    randomBtn.addEventListener("click", function () {
      fetch(ROOT + "search-index.json")
        .then(function (r) { return r.json(); })
        .then(function (items) {
          if (!items.length) return;
          var pick = items[Math.floor(Math.random() * items.length)];
          window.open(pick.url, "_blank", "noopener");
        })
        .catch(function () { /* fail silently, non-essential feature */ });
    });
  }

  // ---- search page --------------------------------------------------------
  var input = document.getElementById("search-input");
  var results = document.getElementById("search-results");
  var status = document.getElementById("search-status");
  var categoryFilter = document.getElementById("category-filter");

  if (input && results) {
    var indexData = [];

    fetch(ROOT + "search-index.json")
      .then(function (r) { return r.json(); })
      .then(function (items) {
        indexData = items;
        status.textContent = items.length + " articles indexed. Start typing to filter.";
        var params = new URLSearchParams(window.location.search);
        var q = params.get("q");
        if (q) { input.value = q; render(q); }
      })
      .catch(function () {
        status.textContent = "Could not load the search index.";
      });

    function render(query) {
      var q = (query || "").trim().toLowerCase();
      var cat = categoryFilter ? categoryFilter.value : "";
      var matches = indexData.filter(function (item) {
        var matchesText = !q || item.title.toLowerCase().indexOf(q) !== -1 ||
          item.extract.toLowerCase().indexOf(q) !== -1;
        var matchesCat = !cat || item.category_slug === cat;
        return matchesText && matchesCat;
      }).slice(0, 100);

      results.innerHTML = "";
      matches.forEach(function (item) {
        var li = document.createElement("li");

        var meta = document.createElement("div");
        meta.className = "result-meta";
        meta.textContent = item.category;

        var h2 = document.createElement("h2");
        var a = document.createElement("a");
        a.href = item.url;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = item.title;
        h2.appendChild(a);

        var p = document.createElement("p");
        p.className = "result-extract";
        p.textContent = item.extract;

        li.appendChild(meta);
        li.appendChild(h2);
        if (item.extract) li.appendChild(p);
        results.appendChild(li);
      });

      if (q || cat) {
        status.textContent = matches.length + " result" + (matches.length === 1 ? "" : "s");
      } else {
        status.textContent = indexData.length + " articles indexed. Start typing to filter.";
      }
    }

    input.addEventListener("input", function () { render(input.value); });
    if (categoryFilter) {
      categoryFilter.addEventListener("change", function () { render(input.value); });
    }
  }
})();
