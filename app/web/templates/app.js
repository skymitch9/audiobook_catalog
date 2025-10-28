/* ===== Theme bootstrap (default Dark ON) ===== */
(function () {
  var saved = localStorage.getItem("ab_theme");
  var startDark = (saved ? saved === "dark" : true);
  if (startDark) document.body.classList.add("dark");
  if (window.matchMedia("(max-width: 820px)").matches) { document.body.classList.add("cards"); }
  var tg = document.getElementById("ab-dark-toggle");
  if (tg) tg.checked = document.body.classList.contains("dark");
})();

/* ===== App logic ===== */
(function () {
  /* Dark toggle */
  var darkToggle = document.getElementById("ab-dark-toggle");
  if (darkToggle) {
    darkToggle.addEventListener("change", function () {
      if (darkToggle.checked) { document.body.classList.add("dark"); localStorage.setItem("ab_theme", "dark"); }
      else { document.body.classList.remove("dark"); localStorage.setItem("ab_theme", "light"); }
    });
  }

  /* Utils */
  function _debounce(fn, ms) { var t; return function () { clearTimeout(t); var a = arguments, c = this; t = setTimeout(function () { fn.apply(c, a); }, ms); }; }
  function _normalize(s) { return (s || "").toString().normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/\s+/g, " ").trim(); }
  function _tokens(q) { return _normalize(q).split(" ").filter(Boolean); }

  /* Hash helpers */
  function _parseHash() { var h = (location.hash || "").replace(/^#/, ""); var sp = new URLSearchParams(h); var q = sp.get("q") || ""; var p = parseInt(sp.get("p") || "1", 10); return { q: q, p: isNaN(p) ? 1 : p }; }
  function _writeHash(q, p) { var sp = new URLSearchParams(); if (q && q.trim()) sp.set("q", q); if (p && p > 1) sp.set("p", String(p)); var nh = "#" + sp.toString(); if (location.hash !== nh) history.replaceState(null, "", nh); }

  /* Sorting */
  function getCellVal(row, idx, numeric) { var cell = row.children[idx]; if (!cell) return ""; if (numeric) { var key = cell.getAttribute("data-sort"); if (key !== null && key !== "") { var n = parseFloat(key); if (!isNaN(n)) return n; } var txt = cell.innerText || cell.textContent || ""; var n2 = parseFloat(txt); return isNaN(n2) ? -Infinity : n2; } return cell.innerText || cell.textContent || ""; }
  function cmp(a, b, numeric) { return numeric ? (a - b) : ("" + a).localeCompare("" + b, undefined, { numeric: true, sensitivity: "base" }); }
  function sortTableBy(table, idx, numeric, asc) { var tbody = table.tBodies[0]; var rows = Array.from(tbody.querySelectorAll("tr")); rows.sort(function (r1, r2) { var v1 = getCellVal(r1, idx, numeric), v2 = getCellVal(r2, idx, numeric); return asc ? cmp(v1, v2, numeric) : cmp(v2, v1, numeric); }); rows.forEach(function (r) { tbody.appendChild(r); }); currentPage = 1; renderPage(); }
  function makeTableSortable(table) { var ths = table.querySelectorAll("th"); ths.forEach(function (th, idx) { var asc = true; if (th.getAttribute("data-key") === "cover_href") return; th.style.cursor = "pointer"; th.addEventListener("click", function () { var numeric = th.dataset.type === "num"; sortTableBy(table, idx, numeric, asc); asc = !asc; }); }); }
  function getCardSortKey(card, key, numeric) { var v = card.getAttribute("data-" + key) || ""; return numeric ? (isNaN(parseFloat(v)) ? -Infinity : parseFloat(v)) : v; }
  function sortCards(container, key, numeric, asc) { var cards = Array.from(container.querySelectorAll(".ab-card")); cards.sort(function (a, b) { var v1 = getCardSortKey(a, key, numeric), v2 = getCardSortKey(b, key, numeric); return asc ? (numeric ? (v1 - v2) : ("" + v1).localeCompare("" + v2, undefined, { numeric: true, sensitivity: "base" })) : (numeric ? (v2 - v1) : ("" + v2).localeCompare("" + v1, undefined, { numeric: true, sensitivity: "base" })); }); cards.forEach(function (c) { container.appendChild(c); }); currentPage = 1; renderPage(); }

  /* Pagination */
  var pageSizeEl, pageNav, pageInfo, table, cardsWrap, emptyEl, emptyQEl;
  var currentPage = 1;
  function pageSize() { var v = pageSizeEl ? pageSizeEl.value : "100"; if (v === "All") return Infinity; var n = parseInt(v, 10); return isNaN(n) ? 100 : n; }
  function renderPage() {
    var ps = pageSize();
    var rows = Array.from(document.querySelectorAll("#ab-table tbody tr"));
    var cards = Array.from(document.querySelectorAll("#ab-cards .ab-card"));

    rows.forEach(function (r) { var m = (r.dataset.searchMatch || "1") === "1"; r.style.display = m ? "" : "none"; });
    cards.forEach(function (c) { var m = (c.dataset.searchMatch || "1") === "1"; c.style.display = m ? "" : "none"; });

    rows = rows.filter(function (r) { return (r.dataset.searchMatch || "1") === "1"; });
    cards = cards.filter(function (c) { return (c.dataset.searchMatch || "1") === "1"; });

    var totalRows = rows.length;
    var pagesRows = Math.max(1, Math.ceil(totalRows / ps));
    if (currentPage > pagesRows) currentPage = pagesRows;

    rows.forEach(function (r, i) { var s = (currentPage - 1) * ps, e = s + ps; r.style.display = (ps === Infinity || (i >= s && i < e)) ? "" : "none"; });

    var pagesCards = Math.max(1, Math.ceil(cards.length / ps));
    var pages = Math.max(pagesRows, pagesCards);
    cards.forEach(function (c, i) { var s = (currentPage - 1) * ps, e = s + ps; c.style.display = (ps === Infinity || (i >= s && i < e)) ? "" : "none"; });

    var qVal = (document.getElementById("ab-search") && document.getElementById("ab-search").value) || "";
    if (emptyEl) {
      emptyEl.style.display = (totalRows === 0 && qVal.trim()) ? "block" : "none";
      if (totalRows === 0 && qVal.trim()) { emptyQEl.textContent = qVal; }
    }
    drawPager(pages, totalRows);
    _writeHash(qVal, currentPage);
  }
  function drawPager(pages, total) {
    if (!pageNav) return;
    pageNav.innerHTML = "";
    function pill(label, handler, active) { var b = document.createElement("button"); b.className = "page-btn" + (active ? " active" : ""); b.textContent = label; b.addEventListener("click", handler); pageNav.appendChild(b); }
    document.getElementById("ab-page-first").onclick = function () { currentPage = 1; renderPage(); }
    document.getElementById("ab-page-prev").onclick = function () { currentPage = Math.max(1, currentPage - 1); renderPage(); }
    document.getElementById("ab-page-next").onclick = function () { currentPage = Math.min(pages, currentPage + 1); renderPage(); }
    document.getElementById("ab-page-last").onclick = function () { currentPage = pages; renderPage(); }

    var windowSize = 2, start = Math.max(1, currentPage - windowSize), end = Math.min(pages, currentPage + windowSize);
    if (start > 1) { pill(1, function () { currentPage = 1; renderPage(); }, currentPage === 1); if (start > 2) pageNav.append("…"); }
    for (var i = start; i <= end; i++) { (function (i0) { pill(i0, function () { currentPage = i0; renderPage(); }, i0 === currentPage); })(i); }
    if (end < pages) { if (end < pages - 1) pageNav.append("…"); pill(pages, function () { currentPage = pages; renderPage(); }, currentPage === pages); }

    var ps = pageSize();
    if (pageInfo) {
      if (total === 0) { pageInfo.textContent = "No results"; }
      else if (ps === Infinity) { pageInfo.textContent = "Showing all " + total + " items"; }
      else { var sIdx = (currentPage - 1) * ps + 1, eIdx = Math.min(total, currentPage * ps); pageInfo.textContent = "Showing " + sIdx + "–" + eIdx + " of " + total; }
    }
  }

  /* Search cache */
  function _buildSearchCache() {
    document.querySelectorAll("#ab-table tbody tr").forEach(function (r) {
      if (!r.dataset._searchText) {
        var parts = [];
        Array.from(r.cells).forEach(function (td) {
          parts.push(td.innerText || td.textContent || "");
          var ds = td.getAttribute("data-sort"); if (ds) parts.push(ds);
        });
        r.dataset._searchText = _normalize(parts.join(" "));
        if (!("searchMatch" in r.dataset)) r.dataset.searchMatch = "1";
      }
    });
    document.querySelectorAll("#ab-cards .ab-card").forEach(function (c) {
      if (!c.dataset._searchText) {
        var keys = ["title", "series", "series_index_sort", "author", "narrator", "year", "genre", "duration_hhmm"];
        var parts = keys.map(function (k) { return c.getAttribute("data-" + k) || ""; });
        parts.push(c.innerText || c.textContent || "");
        c.dataset._searchText = _normalize(parts.join(" "));
        if (!("searchMatch" in c.dataset)) c.dataset.searchMatch = "1";
      }
    });
  }
  function _applySearch(q) {
    var toks = _tokens(q);
    var matchesAll = function (h) { return toks.every(function (t) { return h.indexOf(t) !== -1; }); };
    document.querySelectorAll("#ab-table tbody tr").forEach(function (r) {
      var hay = r.dataset._searchText || ""; r.dataset.searchMatch = (!toks.length || matchesAll(hay)) ? "1" : "0";
    });
    document.querySelectorAll("#ab-cards .ab-card").forEach(function (c) {
      var hay = c.dataset._searchText || ""; c.dataset.searchMatch = (!toks.length || matchesAll(hay)) ? "1" : "0";
    });
  }

  /* Author → Drive map from inline JSON */
  var AUTHOR_MAP = {};
  function _loadAuthorMapInline() {
    try {
      var el = document.getElementById("ab-author-map-json");
      if (!el) return;
      var txt = (el.textContent || "").trim();
      if (!txt) return;
      var parsed = JSON.parse(txt);
      if (parsed && typeof parsed === "object") { AUTHOR_MAP = parsed; }
    } catch (e) { /* ignore parse errors */ }
  }
  function _resolveAuthorFolder(author) {
    if (!author) return null;
    if (AUTHOR_MAP[author]) return AUTHOR_MAP[author];
    var aNorm = author.toLowerCase().trim();
    for (var k in AUTHOR_MAP) {
      if (!Object.prototype.hasOwnProperty.call(AUTHOR_MAP, k)) continue;
      if (k.toLowerCase().trim() === aNorm) return AUTHOR_MAP[k];
    }
    return null;
  }
  function _authorFolderHref(author) {
    var id = _resolveAuthorFolder(author);
    return (id && String(id).trim())
      ? ("https://drive.google.com/drive/folders/" + encodeURIComponent(String(id).trim()))
      : null;
  }
  function _authorSearchHref(author) {
    return author && author.trim()
      ? ("https://drive.google.com/drive/search?q=" + encodeURIComponent(author.trim()))
      : null;
  }
  function _bookSearchHref(title) {
    var clean = (title || "").toString().trim();
    if (clean.startsWith('"') && clean.endsWith('"')) clean = clean.slice(1, -1).trim();
    return clean ? ("https://drive.google.com/drive/search?q=" + encodeURIComponent(clean)) : null;
  }

  /* Modal logic */
  var modal = document.getElementById("modal");
  var modalCover = document.getElementById("modal-cover");
  var modalTitle = document.getElementById("modal-title");
  var mSeries = document.getElementById("m-series");
  var mIndex = document.getElementById("m-index");
  var mAuthor = document.getElementById("m-author");
  var mNarrator = document.getElementById("m-narrator");
  var mYear = document.getElementById("m-year");
  var mGenre = document.getElementById("m-genre");
  var mDuration = document.getElementById("m-duration");
  var mDesc = document.getElementById("m-desc");

  var mAuthorDrive = document.getElementById("m-author-drive");
  var mAuthorFolder = document.getElementById("m-author-folder");
  var mBookDrive = document.getElementById("m-book-drive");
  var modalClose = document.getElementById("modal-close");

  function openModal(payload) {
    modalCover.src = payload.cover || "";
    modalCover.alt = payload.title || "";
    modalTitle.textContent = payload.title || "";
    mSeries.textContent = payload.series || "";
    mIndex.textContent = payload.index || "";
    mAuthor.textContent = payload.author || "";
    mNarrator.textContent = payload.narrator || "";
    mYear.textContent = payload.year || "";
    mGenre.textContent = payload.genre || "";
    mDuration.textContent = payload.duration || "";
    mDesc.textContent = payload.desc ? payload.desc : "No description available.";

    var author = payload.author || "";
    var folderHref = _authorFolderHref(author);
    var authorSearchHref = _authorSearchHref(author);
    var bookHref = _bookSearchHref(payload.title || "");

    if (folderHref) {
      mAuthorFolder.href = folderHref;
      mAuthorFolder.style.display = "inline-block";
    } else {
      mAuthorFolder.removeAttribute("href");
      mAuthorFolder.style.display = "none";
    }

    if (authorSearchHref) {
      mAuthorDrive.href = authorSearchHref;
      mAuthorDrive.style.display = "inline-block";
    } else {
      mAuthorDrive.removeAttribute("href");
      mAuthorDrive.style.display = "none";
    }

    if (bookHref) {
      mBookDrive.href = bookHref;
      mBookDrive.style.display = "inline-block";
    } else {
      mBookDrive.removeAttribute("href");
      mBookDrive.style.display = "none";
    }

    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
  }
  function closeModal() { modal.classList.remove("open"); modal.setAttribute("aria-hidden", "true"); }

  // Cover button -> open
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button.cover-btn");
    if (btn) {
      var d = btn.dataset;
      openModal({
        cover: d.cover || "", title: d.title || "", series: d.series || "", index: d.index || "",
        author: d.author || "", narrator: d.narrator || "", year: d.year || "",
        genre: d.genre || "", duration: d.duration || "", desc: d.desc || ""
      });
    }
  });

  // Any cell / row click -> open (unless clicking an interactive element)
  document.addEventListener("click", function (e) {
    var td = e.target.closest("#ab-table tbody td");
    if (!td) return;
    if (e.target.closest("a,button") && !e.target.closest("button.cover-btn")) return;
    var tr = td.parentElement; if (!tr) return;
    var coverBtn = tr.querySelector("button.cover-btn"); if (!coverBtn) return;
    var d = coverBtn.dataset;
    openModal({
      cover: d.cover || "", title: d.title || "", series: d.series || "", index: d.index || "",
      author: d.author || "", narrator: d.narrator || "", year: d.year || "",
      genre: d.genre || "", duration: d.duration || "", desc: d.desc || ""
    });
  });

  // Card click -> open modal (improved functionality)
  document.addEventListener("click", function (e) {
    var card = e.target.closest(".ab-card");
    if (!card) return;
    if (e.target.closest("a,button")) return; // Don't trigger on interactive elements

    var d = card.dataset;
    openModal({
      cover: d.cover || "", title: d.title || "", series: d.series || "", index: d.index || "",
      author: d.author || "", narrator: d.narrator || "", year: d.year || "",
      genre: d.genre || "", duration: d.duration || "", desc: d.desc || ""
    });
  });

  if (modalClose) modalClose.addEventListener("click", closeModal);
  modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });

  document.addEventListener("DOMContentLoaded", function () {
    _loadAuthorMapInline();

    table = document.getElementById("ab-table");
    cardsWrap = document.getElementById("ab-cards");
    pageSizeEl = document.getElementById("ab-page-size");
    pageNav = document.getElementById("ab-page-nav");
    pageInfo = document.getElementById("ab-page-info");
    emptyEl = document.getElementById("ab-empty");
    emptyQEl = document.getElementById("ab-empty-q");

    if (table) makeTableSortable(table);

    document.querySelectorAll("#ab-table tbody tr").forEach(function (r) { r.dataset.searchMatch = "1"; });
    document.querySelectorAll("#ab-cards .ab-card").forEach(function (c) { c.dataset.searchMatch = "1"; });
    _buildSearchCache();

    var search = document.getElementById("ab-search");
    if (search) search.addEventListener("input", _debounce(function () { _applySearch(search.value); currentPage = 1; renderPage(); }, 120));

    var sortSelect = document.getElementById("ab-sort");
    var sortAscBtn = document.getElementById("ab-sort-asc");
    var sortDescBtn = document.getElementById("ab-sort-desc");
    function doSort(asc) {
      var parts = sortSelect.value.split("|");
      var key = parts[0], numeric = (parts[1] === "num");
      var idx = Array.from(document.querySelectorAll("#ab-table thead th")).findIndex(function (th) { return th.getAttribute("data-key") === key; });
      if (idx >= 0) { sortTableBy(table, idx, numeric, asc); }
      var cards = Array.from(cardsWrap.querySelectorAll(".ab-card"));
      cards.sort(function (a, b) {
        var v1 = a.getAttribute("data-" + key) || "", v2 = b.getAttribute("data-" + key) || "";
        if (numeric) { v1 = parseFloat(v1); v2 = parseFloat(v2); if (isNaN(v1)) v1 = -Infinity; if (isNaN(v2)) v2 = -Infinity; }
        return asc ? ((numeric ? (v1 - v2) : ("" + v1).localeCompare("" + v2, undefined, { numeric: true, sensitivity: "base" })))
          : ((numeric ? (v2 - v1) : ("" + v2).localeCompare("" + v1, undefined, { numeric: true, sensitivity: "base" })));
      });
      cards.forEach(function (c) { cardsWrap.appendChild(c); });
      currentPage = 1; renderPage();
    }
    if (sortAscBtn) sortAscBtn.addEventListener("click", function () { doSort(true); });
    if (sortDescBtn) sortDescBtn.addEventListener("click", function () { doSort(false); });

    var toggle = document.getElementById("ab-toggle-view");
    if (toggle) { toggle.addEventListener("click", function () { document.body.classList.toggle("cards"); currentPage = 1; renderPage(); }); }

    if (pageSizeEl) { pageSizeEl.addEventListener("change", function () { currentPage = 1; renderPage(); }); }

    var hashState = _parseHash();
    if (search && hashState.q) { search.value = hashState.q; _applySearch(hashState.q); }
    if (hashState.p > 1) { currentPage = hashState.p; }
    if (!hashState.q) { _applySearch((search || {}).value || ""); }
    renderPage();

    window.addEventListener("hashchange", function () {
      var s2 = _parseHash();
      var curQ = (search && search.value) || "";
      if (search && s2.q !== curQ) { search.value = s2.q || ""; _applySearch(search.value); }
      currentPage = s2.p || 1; renderPage();
    });
  });
})();