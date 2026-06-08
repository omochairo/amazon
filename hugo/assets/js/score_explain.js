// hugo/assets/js/score_explain.js
// #1369 スコア根拠の inline 展開 UI (epic #1365 Layer 2-②)
//
// 商品ページの知育スコア内訳 (.score-breakdown-doc) の直後に、site-wide な
// 進行性強化として 2 つの導線を注入する:
//   1. 算出ロジック deep-dive (/about-score/) への内部リンク (issue UI 項目 #4)
//   2. 「このスコアは参考になりましたか？」フィードバック widget
//      → localStorage に記録 (per-path) + GA4 イベント score_feedback を送出
//        (KPI: 「役立った」フィードバック率の計測)
//
// hero の 4 軸レーダー / rationale 自体は post.md.j2 が描画済みのため、ここでは
// 既存記事 600+ にも次の Hugo build で即反映される導線のみを足す。
(function (global) {
  var FB_KEY = "omcha_score_feedback";
  var ABOUT_URL = "/about-score/";

  function _q(sel, root) { return (root || document).querySelector(sel); }

  function _readFeedback() {
    try { return JSON.parse(global.localStorage.getItem(FB_KEY) || "{}") || {}; }
    catch (e) { return {}; }
  }

  function _saveFeedback(map) {
    try { global.localStorage.setItem(FB_KEY, JSON.stringify(map)); }
    catch (e) { /* quota / private mode — UI は動かす */ }
  }

  function _track(vote) {
    try {
      if (typeof global.gtag === "function") {
        global.gtag("event", "score_feedback", {
          helpful: vote === "up",
          page_path: global.location.pathname
        });
      }
    } catch (e) { /* analytics 失敗で UI を止めない */ }
  }

  function _renderVoted(widget) {
    widget.classList.add("is-voted");
    var btns = widget.querySelectorAll(".score-fb-btn");
    for (var i = 0; i < btns.length; i++) { btns[i].disabled = true; }
    var thanks = _q(".score-fb-thanks", widget);
    if (thanks) thanks.hidden = false;
  }

  function _wireFeedback(widget, pathKey) {
    var map = _readFeedback();
    if (map[pathKey] && map[pathKey].v) {
      var prev = widget.querySelector('.score-fb-btn[data-vote="' + map[pathKey].v + '"]');
      if (prev) prev.classList.add("is-chosen");
      _renderVoted(widget);
      return;
    }
    var btns = widget.querySelectorAll(".score-fb-btn");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function () {
        var vote = this.getAttribute("data-vote");
        var m = _readFeedback();
        m[pathKey] = { v: vote, t: Date.now() };
        _saveFeedback(m);
        this.classList.add("is-chosen");
        _renderVoted(widget);
        _track(vote);
      });
    }
  }

  function _build(pathKey) {
    var wrap = document.createElement("div");
    wrap.className = "score-explain-cta";
    wrap.innerHTML =
      '<a class="score-explain-link" href="' + ABOUT_URL + '">' +
        '📐 知育スコアの算出ロジックを詳しく見る →</a>' +
      '<div class="score-fb" role="group" aria-label="スコアへのフィードバック">' +
        '<span class="score-fb-q">このスコアは参考になりましたか？</span>' +
        '<span class="score-fb-actions">' +
          '<button type="button" class="score-fb-btn" data-vote="up" aria-label="参考になった">👍 はい</button>' +
          '<button type="button" class="score-fb-btn" data-vote="down" aria-label="参考にならなかった">👎 いまいち</button>' +
        '</span>' +
        '<span class="score-fb-thanks" hidden>ご回答ありがとうございました。</span>' +
      '</div>';
    // about-score リンクのクリックも軽く計測
    var link = _q(".score-explain-link", wrap);
    if (link) {
      link.addEventListener("click", function () {
        try {
          if (typeof global.gtag === "function") {
            global.gtag("event", "select_content", {
              content_type: "about_score",
              page_path: global.location.pathname
            });
          }
        } catch (e) { /* no-op */ }
      });
    }
    _wireFeedback(wrap, pathKey);
    return wrap;
  }

  function boot() {
    try {
      if (_q(".score-explain-cta")) return; // 二重 mount 防止
      var anchor = _q(".score-breakdown-doc") || _q(".hero-score");
      if (!anchor) return; // 商品ページ以外 / スコア無し記事は no-op
      var pathKey = global.location.pathname || "/";
      var cta = _build(pathKey);
      anchor.parentNode.insertBefore(cta, anchor.nextSibling);
    } catch (e) { /* never break page */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(typeof window !== "undefined" ? window : this);
