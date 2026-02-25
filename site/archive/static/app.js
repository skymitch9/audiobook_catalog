// Table-first behavior: no automatic switch to cards on mobile.
// Cards remain available via the "Toggle View" button.

(function(){
  function getCellVal(row, idx, numeric){
    var cell = row.children[idx];
    if (!cell) return "";
    if (numeric){
      var key = cell.getAttribute("data-sort");
      if (key !== null && key !== ""){
        var n = parseFloat(key);
        if (!isNaN(n)) return n;
      }
      var txt = cell.innerText || cell.textContent || "";
      var n2 = parseFloat(txt);
      return isNaN(n2) ? -Infinity : n2;
    }
    return cell.innerText || cell.textContent || "";
  }
  function cmp(a,b,numeric){ return numeric ? (a-b) : (""+a).localeCompare(""+b,undefined,{numeric:true,sensitivity:"base"}) }
  function sortTableBy(table, idx, numeric, asc){
    var tbody = table.tBodies[0];
    var rows = Array.from(tbody.querySelectorAll("tr"));
    rows.sort(function(r1,r2){
      var v1 = getCellVal(r1, idx, numeric), v2 = getCellVal(r2, idx, numeric);
      return asc ? cmp(v1,v2,numeric) : cmp(v2,v1,numeric);
    });
    rows.forEach(function(r){ tbody.appendChild(r); });
    currentPage = 1; renderPage();
  }
  function makeTableSortable(table){
    var ths = table.querySelectorAll("th");
    ths.forEach(function(th, idx){
      var asc = true;
      if (th.getAttribute("data-key")==="cover_href"){ return; } // Cover not sortable
      th.style.cursor="pointer";
      th.addEventListener("click", function(){
        var numeric = th.dataset.type === "num";
        sortTableBy(table, idx, numeric, asc);
        asc = !asc;
      });
    });
  }

  function getCardSortKey(card, key, numeric){
    var v = card.getAttribute("data-" + key) || "";
    return numeric ? (isNaN(parseFloat(v)) ? -Infinity : parseFloat(v)) : v;
  }
  function sortCards(container, key, numeric, asc){
    var cards = Array.from(container.querySelectorAll(".ab-card"));
    cards.sort(function(a,b){
      var v1 = getCardSortKey(a,key,numeric), v2 = getCardSortKey(b,key,numeric);
      return asc ? (numeric ? (v1-v2) : (""+v1).localeCompare(""+v2,undefined,{numeric:true,sensitivity:"base"}))
                 : (numeric ? (v2-v1) : (""+v2).localeCompare(""+v1,undefined,{numeric:true,sensitivity:"base"}));
    });
    cards.forEach(function(c){ container.appendChild(c); });
    currentPage = 1; renderPage();
  }

  function applySearch(q){
    q = (q||"").toLowerCase();
    // table
    document.querySelectorAll("#ab-table tbody tr").forEach(function(r){
      r.style.display = (r.innerText || "").toLowerCase().indexOf(q) >= 0 ? "" : "none";
    });
    // cards
    document.querySelectorAll("#ab-cards .ab-card").forEach(function(c){
      c.style.display = (c.innerText || "").toLowerCase().indexOf(q) >= 0 ? "" : "none";
    });
    currentPage = 1; renderPage();
  }

  // Pagination
  var pageSizeEl, pageNav, pageInfo, table, cardsWrap;
  var currentPage = 1;
  function visibleRows(){
    return Array.from(document.querySelectorAll("#ab-table tbody tr")).filter(function(r){ return r.style.display !== "none"; });
  }
  function visibleCards(){
    return Array.from(document.querySelectorAll("#ab-cards .ab-card")).filter(function(c){ return c.style.display !== "none"; });
  }
  function pageSize(){
    var v = pageSizeEl ? pageSizeEl.value : "100";
    if (v === "All") return Infinity;
    var n = parseInt(v, 10);
    return isNaN(n) ? 100 : n;
  }
  function renderPage(){
    var ps = pageSize();
    var isCards = document.body.classList.contains("cards");

    if (isCards){
      var items = visibleCards();
      var total = items.length;
      var pages = Math.max(1, Math.ceil(total / ps));
      if (currentPage > pages) currentPage = pages;
      items.forEach(function(c,i){
        var start = (currentPage-1)*ps, end = start+ps;
        c.style.display = (ps===Infinity || (i >= start && i < end)) ? "" : "none";
      });
      drawPager(pages, total);
    }else{
      var rows = visibleRows();
      var total = rows.length;
      var pages = Math.max(1, Math.ceil(total / ps));
      if (currentPage > pages) currentPage = pages;
      rows.forEach(function(r,i){
        var start = (currentPage-1)*ps, end = start+ps;
        r.style.display = (ps===Infinity || (i >= start && i < end)) ? "" : "none";
      });
      drawPager(pages, total);
    }
  }
  function drawPager(pages, total){
    if (!pageNav) return;
    pageNav.innerHTML = "";
    function pill(label, handler, active){
      var b = document.createElement("button");
      b.className = "page-btn" + (active ? " active" : "");
      b.textContent = label;
      b.addEventListener("click", handler);
      pageNav.appendChild(b);
    }
    // First/Prev/Next/Last
    document.getElementById("ab-page-first").onclick = function(){ currentPage = 1; renderPage(); }
    document.getElementById("ab-page-prev").onclick  = function(){ currentPage = Math.max(1, currentPage-1); renderPage(); }
    document.getElementById("ab-page-next").onclick  = function(){ currentPage = Math.min(pages, currentPage+1); renderPage(); }
    document.getElementById("ab-page-last").onclick  = function(){ currentPage = pages; renderPage(); }

    // Compact numeric pills
    var windowSize = 2;
    var start = Math.max(1, currentPage - windowSize);
    var end   = Math.min(pages, currentPage + windowSize);
    if (start > 1){ pill(1, function(){ currentPage=1; renderPage(); }, currentPage===1); if (start>2) pageNav.append("…"); }
    for (var i=start;i<=end;i++){
      (function(i0){ pill(i0, function(){ currentPage=i0; renderPage(); }, i0===currentPage); })(i);
    }
    if (end < pages){ if (end < pages-1) pageNav.append("…"); pill(pages, function(){ currentPage=pages; renderPage(); }, currentPage===pages); }

    // Info
    var ps = pageSize();
    if (pageInfo){
      if (ps===Infinity) pageInfo.textContent = "Showing all " + total + " items";
      else {
        var sIdx = (currentPage-1)*ps + 1;
        var eIdx = Math.min(total, currentPage*ps);
        pageInfo.textContent = "Showing " + sIdx + "–" + eIdx + " of " + total;
      }
    }
  }

  document.addEventListener("DOMContentLoaded", function(){
    table     = document.getElementById("ab-table");
    cardsWrap = document.getElementById("ab-cards");
    pageSizeEl= document.getElementById("ab-page-size");
    pageNav   = document.getElementById("ab-page-nav");
    pageInfo  = document.getElementById("ab-page-info");

    if (table) makeTableSortable(table);

    var search = document.getElementById("ab-search");
    if (search) search.addEventListener("input", function(){ applySearch(search.value); });

    var sortSelect = document.getElementById("ab-sort");
    var sortAscBtn = document.getElementById("ab-sort-asc");
    var sortDescBtn= document.getElementById("ab-sort-desc");
    function doSort(asc){
      var parts = sortSelect.value.split("|");
      var key = parts[0], numeric = (parts[1]==="num");

      // table header index
      var idx = Array.from(document.querySelectorAll("#ab-table thead th")).findIndex(function(th){
        return th.getAttribute("data-key") === key;
      });
      if (idx >= 0) sortTableBy(table, idx, numeric, asc);

      // cards
      sortCards(cardsWrap, key, numeric, asc);
      currentPage = 1; renderPage();
    }
    if (sortAscBtn) sortAscBtn.addEventListener("click", function(){ doSort(true); });
    if (sortDescBtn)sortDescBtn.addEventListener("click", function(){ doSort(false); });

    var toggle = document.getElementById("ab-toggle-view");
    if (toggle){
      toggle.addEventListener("click", function(){
        document.body.classList.toggle("cards");
        currentPage = 1; renderPage();
      });
    }
    if (pageSizeEl){
      pageSizeEl.addEventListener("change", function(){ currentPage=1; renderPage(); });
    }

    // IMPORTANT: do NOT auto-switch to cards on narrow screens.
    // We start in TABLE view for consistent, constrained covers.

    renderPage();
  });
})();
