// hugo/assets/js/article_feedback.js
// 役立ち度フィードバック (epic #1365 Layer 3-②)
//
// 記事 (商品ページ) 末尾に「この記事は役に立ちましたか？」3 段階フィードバック
// (😊 役立った / 😐 ふつう / 😢 物足りない) を注入する。
//   - 投票は localStorage に per-path 記録 (再訪で再表示・二重投票防止)
//   - GA4 イベント article_feedback を送出 (rating + page_path) → 月次集計の素データ
//
// score_explain.js の score_feedback widget と同型だが、対象が「スコア」ではなく
// 「記事全体の役立ち度」で 3 段階な点が異なる。記事下に置くことで、読了後の満足度を
// 拾い、低評価ページを月次で洗い出して改善優先度づけに使う。
(function (global) {
  var FB_KEY = "omcha_article_feedback";
  var RATINGS = [
    { v: "good", emoji: "😊", label: "役立った" },
    { v: "ok", emoji: "😐", label: "ふつう" },
    { v: "bad", emoji: "😢", label: "物足りない" }
  ];

  function _q(sel, root) { return (root || document).querySelector(sel); }

  function _readFeedback() {
    try { return JSON.parse(global.localStorage.getItem(FB_KEY) || "{}") || {}; }
    catch (e) { return {}; }
  }

  function _saveFeedback(map) {
    try { global.localStorage.setItem(FB_KEY, JSON.stringify(map)); }
    catch (e) { /* quota / private mode — UI は動かす */ }
  }

  function _track(rating) {
    try {
      if (typeof global.gtag === "function") {
        global.gtag("event", "article_feedback", {
          rating: rating,
          page_path: global.location.pathname
        });
      }
    } catch (e) { /* analytics 失敗で UI を止めない */ }
  }

  function _renderVoted(widget) {
    widget.classList.add("is-voted");
    var btns = widget.querySelectorAll(".article-fb-btn");
    for (var i = 0; i < btns.length; i++) { btns[i].disabled = true; }
    var thanks = _q(".article-fb-thanks", widget);
    if (thanks) thanks.hidden = false;
  }

  function _wire(widget, pathKey) {
    var map = _readFeedback();
    if (map[pathKey] && map[pathKey].v) {
      var prev = widget.querySelector('.article-fb-btn[data-vote="' + map[pathKey].v + '"]');
      if (prev) prev.classList.add("is-chosen");
      _renderVoted(widget);
      return;
    }
    var btns = widget.querySelectorAll(".article-fb-btn");
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
    wrap.className = "article-fb";
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "この記事への役立ち度フィードバック");
    var html =
      '<span class="article-fb-q">この記事は役に立ちましたか？</span>' +
      '<span class="article-fb-actions">';
    for (var i = 0; i < RATINGS.length; i++) {
      var r = RATINGS[i];
      html +=
        '<button type="button" class="article-fb-btn" data-vote="' + r.v + '" ' +
        'aria-label="' + r.label + '"><span class="article-fb-emoji">' + r.emoji +
        '</span>' + r.label + '</button>';
    }
    html +=
      '</span>' +
      '<span class="article-fb-thanks" hidden>フィードバックありがとうございました。今後の改善に役立てます。</span>';
    wrap.innerHTML = html;
    _wire(wrap, pathKey);
    return wrap;
  }

  function boot() {
    try {
      if (_q(".article-fb")) return; // 二重 mount 防止
      var anchor = _q(".post-content");
      if (!anchor) return; // 本文無しページは no-op
      var pathKey = global.location.pathname || "/";
      anchor.appendChild(_build(pathKey));
    } catch (e) { /* never break page */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(typeof window !== "undefined" ? window : this);
