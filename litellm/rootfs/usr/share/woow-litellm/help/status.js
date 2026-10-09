// Woow LiteLLM help page: poll ./status (nginx → LiteLLM /health/readiness) every 10 s.
(function () {
  "use strict";
  var el = document.getElementById("status");
  var labels = { starting: "啟動中…", ready: "就緒", db: "資料庫異常", unknown: "無法取得狀態" };
  function show(state) {
    el.textContent = labels[state] || labels.unknown;
    el.setAttribute("data-state", state);
  }
  function poll() {
    fetch("status", { cache: "no-store" })
      .then(function (r) {
        return r.json().then(function (j) { return { code: r.status, body: j }; });
      })
      .then(function (res) {
        var b = res.body || {};
        if (b.status === "starting") show("starting");
        else if (res.code === 200 && b.db === "connected") show("ready");
        else show("db");
      })
      .catch(function () { show("unknown"); });
  }
  poll();
  setInterval(poll, 10000);
})();
