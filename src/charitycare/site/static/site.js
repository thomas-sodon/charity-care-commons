// Progressive enhancement for the dashboard table: client-side filters (the table works without JS).
(function () {
  var form = document.getElementById("filters");
  var table = document.getElementById("facilities");
  if (!form || !table) return;
  form.hidden = false;
  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var count = document.getElementById("filter-count");
  function val(name) { var el = form.querySelector('[data-filter="' + name + '"]'); return el ? el.value.trim().toLowerCase() : ""; }
  function apply() {
    var free = val("free"), asset = val("asset"), screening = val("screening"), coverage = val("coverage"), q = val("q");
    var shown = 0;
    rows.forEach(function (tr) {
      var f = parseFloat(tr.dataset.free), ok = true;
      if (free === "lt200") ok = ok && f < 200;
      if (free === "eq200") ok = ok && f === 200;
      if (free === "gt200") ok = ok && f > 200;
      if (asset) ok = ok && tr.dataset.asset === asset;
      if (screening) ok = ok && tr.dataset.screening === screening;
      if (coverage) ok = ok && tr.dataset.coverage === coverage;
      if (q) ok = ok && tr.dataset.q.indexOf(q) !== -1;
      tr.hidden = !ok;
      if (ok) shown++;
    });
    if (count) count.textContent = shown + " of " + rows.length + " facilities shown";
  }
  form.addEventListener("input", apply);
  form.addEventListener("submit", function (e) { e.preventDefault(); });
  apply();
})();
