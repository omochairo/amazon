// hugo/assets/js/diagnosis_core.js
// おもちゃ診断の「設問定義・推薦ロジック・結果の言語化」。DOM に触れない純粋な部分だけを
// ここに置き、画面側 (diagnosis.js) と node のテスト (tests/js/diagnosis_core.test.mjs) の
// 両方から使う。window.OmochaDiagnosis に公開する。
//
// 設計の要点 (2026-10 の作り直し):
//   - 親が本当に知りたいのは「うちの子の年齢で安全に遊べて、好きな遊びに合っていて、
//     予算内で、なぜそれなのか」。そこで (1) 対象年齢がお子さんの年齢を超える商品は
//     どの段階のフォールバックでも出さない、(2) 1 件ごとに「選んだ理由」を返す、
//     (3) 「伸ばしたい力」という抽象的な問いを「いま夢中な遊び」に言い換える。
//   - 誰のための買い物か (わが子 / プレゼント / 出産祝い) で設問が分岐する。
//     プレゼントでは「持っているおもちゃ」を聞かない (贈る側は知らないことが多い)。
//   - search.json の content は価格表の定型文で、遊びの内容をほぼ含まない
//     (実データで確認)。照合は tags と商品名だけで行い、部分一致にする
//     (タグは 3,800 種以上あり「ジグソーパズル」「マグネットブロック」のように揺れる)。
(function (global) {
  "use strict";

  // ---------------------------------------------------------------- 設問
  var WHO = [
    { value: "child", emoji: "🧒", label: "わが子に", sub: "毎日の遊びにぴったりを探す" },
    { value: "gift", emoji: "🎁", label: "プレゼントに", sub: "誕生日・クリスマス・お祝い" },
    { value: "birth", emoji: "👶", label: "出産祝いに", sub: "生まれたばかりの赤ちゃんへ" }
  ];

  // lo / hi は月齢。対象年齢 (age_min_months) が hi を超える商品は出さない。
  var AGES = [
    { value: "0-5m", emoji: "🍼", label: "0〜5か月", sub: "ねんね期", lo: 0, hi: 5 },
    { value: "6-11m", emoji: "🧸", label: "6〜11か月", sub: "おすわり・はいはい", lo: 6, hi: 11 },
    { value: "1y", emoji: "🚶", label: "1歳", sub: "よちよち歩き", lo: 12, hi: 23 },
    { value: "2y", emoji: "🧃", label: "2歳", sub: "イヤイヤ期・まねっこ", lo: 24, hi: 35 },
    { value: "3y", emoji: "🧩", label: "3歳", sub: "ごっこ遊びが本格化", lo: 36, hi: 47 },
    { value: "4y", emoji: "🎨", label: "4歳", sub: "ルールのある遊びへ", lo: 48, hi: 59 },
    { value: "5y", emoji: "🎒", label: "5歳", sub: "年長・就学前", lo: 60, hi: 71 },
    { value: "6-7y", emoji: "📚", label: "6〜7歳", sub: "小学校低学年", lo: 72, hi: 95 },
    { value: "8y", emoji: "🔭", label: "8歳以上", sub: "小学校中学年〜", lo: 96, hi: 155 }
  ];

  // 「いま夢中な遊び」。kw は tags / 商品名に部分一致させる語。
  // minM / maxM はその選択肢を出す月齢の範囲 (赤ちゃんに「プログラミング」を出さない等)。
  var INTERESTS = [
    { value: "sense", emoji: "🤲", label: "さわる・にぎる", sub: "音や感触を確かめる", maxM: 35,
      kw: ["ラトル", "歯固め", "ベビージム", "布絵本", "にぎにぎ", "ガラガラ", "メリー", "ファーストトイ", "型はめ", "ひもとおし", "指先", "感触", "モンテッソーリ", "プレイジム", "ベビー"] },
    { value: "build", emoji: "🧱", label: "つくる・組み立てる", sub: "ブロック・積み木",
      kw: ["ブロック", "積み木", "つみき", "レゴ", "lego", "デュプロ", "laq", "マグ・フォーマー", "ピカソタイル", "picassotiles", "スロープ", "gravitrax", "ドミノ", "組み立て"] },
    { value: "pretend", emoji: "🍳", label: "なりきる・ごっこ遊び", sub: "ままごと・お世話・変身", minM: 12,
      kw: ["ごっこ", "ままごと", "キッチン", "お医者さん", "お店", "人形", "メルちゃん", "シルバニア", "ドールハウス", "なりきり", "変身", "ぬいぐるみ", "お世話"] },
    { value: "vehicle", emoji: "🚗", label: "のりもの・うごくもの", sub: "電車・車・レール",
      kw: ["トミカ", "プラレール", "電車", "ミニカー", "車", "のりもの", "乗り物", "はたらくくるま", "木製レール", "brio", "ブリオ", "ホットウィール", "新幹線"] },
    { value: "think", emoji: "🧩", label: "考える・ゲーム", sub: "パズル・ボードゲーム", minM: 18,
      kw: ["パズル", "ボードゲーム", "カードゲーム", "バランスゲーム", "脳トレ", "知恵の輪", "すごろく", "迷路", "オセロ", "将棋", "かるた", "パーティーゲーム", "アナログゲーム"] },
    { value: "art", emoji: "🎨", label: "描く・工作", sub: "お絵かき・ねんど・シール", minM: 18,
      kw: ["お絵かき", "お絵描き", "おえかき", "工作", "粘土", "ねんど", "シール", "ぬりえ", "塗り絵", "アート", "クレヨン", "ペイント", "diy", "メイキング", "ビーズ"] },
    { value: "words", emoji: "🔤", label: "ことば・数・英語", sub: "ひらがな・算数・えいご",
      kw: ["ことば", "英語", "えいご", "ひらがな", "文字", "もじ", "数字", "かず", "算数", "絵本", "図鑑", "フォニックス", "フラッシュカード", "くもん", "kumon", "学習", "時計", "知育パッド"] },
    { value: "music", emoji: "🎵", label: "音・リズム", sub: "楽器・うた",
      kw: ["楽器", "リズム", "ピアノ", "太鼓", "ドラム", "木琴", "鉄琴", "マラカス", "メロディ", "うた", "歌"] },
    { value: "science", emoji: "🔬", label: "しくみ・科学", sub: "プログラミング・実験", minM: 36,
      kw: ["stem", "プログラミング", "ロボット", "科学", "実験", "回路", "snap circuits", "自由研究", "顕微鏡", "天体", "磁石"] },
    { value: "active", emoji: "⚽", label: "体を動かす", sub: "外遊び・水遊び・乗用",
      kw: ["外遊び", "スポーツ", "ボール", "水遊び", "乗用", "三輪車", "キックバイク", "トランポリン", "お風呂", "砂場", "シャボン"] },
    { value: "any", emoji: "✨", label: "おまかせ", sub: "知育スコアの高い定番から", kw: [] }
  ];

  // 予算の上限 (円)。null はこだわらない。
  var BUDGETS = [
    { value: "3000", emoji: "👛", label: "〜3,000円", sub: "ちょっとしたご褒美に", max: 3000 },
    { value: "5000", emoji: "🎀", label: "〜5,000円", sub: "誕生日プレゼントの相場", max: 5000 },
    { value: "10000", emoji: "🎁", label: "〜10,000円", sub: "クリスマス・特別な日に", max: 10000 },
    { value: "any", emoji: "👑", label: "こだわらない", sub: "良いものならOK", max: null }
  ];

  // 大事にしたいこと → ivs_axes (scripts/score_calculator.py compute_ivs_axes, 2.0〜5.0)。
  var PRIORITIES = [
    { value: "safety", emoji: "🛡️", label: "安全・安心", sub: "素材や安全基準を重視", axis: "safety", axisLabel: "安全性" },
    { value: "longevity", emoji: "⏳", label: "長く遊べる", sub: "成長しても飽きにくい", axis: "longevity", axisLabel: "長く遊べる度" },
    { value: "education", emoji: "📚", label: "知育効果", sub: "学びにつながる", axis: "education", axisLabel: "知育効果" },
    { value: "cost", emoji: "💰", label: "コスパ", sub: "価格以上の満足度", axis: "cost_performance", axisLabel: "コスパ" },
    { value: "balance", emoji: "⚖️", label: "バランスよく", sub: "総合点の高いものを", axis: null, axisLabel: "" }
  ];

  // わが子のときだけ聞く「もう持っているもの」(複数選択・任意)。INTERESTS の値を使う。
  var OWNED_VALUES = ["build", "pretend", "vehicle", "think", "art", "words", "music", "sense"];

  function find(list, value) {
    for (var i = 0; i < list.length; i++) if (list[i].value === value) return list[i];
    return null;
  }

  function ageMonthsOf(a) {
    var age = find(AGES, a.age);
    return age ? age.lo : 0;
  }

  // 回答状況から、いま出すべき設問の並びを返す (分岐はここだけで決める)。
  function stepsFor(a) {
    var steps = ["who"];
    if (a.who !== "birth") steps.push("age");
    steps.push("interest", "budget", "priority");
    if (a.who === "child") steps.push("owned");
    return steps;
  }

  function interestOptionsFor(months) {
    return INTERESTS.filter(function (o) {
      if (o.minM != null && months < o.minM) return false;
      if (o.maxM != null && months > o.maxM) return false;
      return true;
    });
  }

  // 設問 id → 画面に出す内容。options は回答状況 (年齢) で絞り込まれる。
  function question(id, a) {
    var gift = a.who === "gift" || a.who === "birth";
    var kid = gift ? "贈るお子さん" : "お子さん";
    switch (id) {
      case "who":
        return { id: id, title: "だれのためのおもちゃ？", hint: "選び方のポイントが変わります", options: WHO };
      case "age":
        return { id: id, title: kid + "の年齢は？", hint: "対象年齢を超えるおもちゃは出さないので安心です", options: AGES };
      case "interest":
        if (a.who === "birth") {
          return { id: id, title: "どんなものを贈りたい？", hint: "迷ったら「おまかせ」で定番から選びます",
            options: INTERESTS.filter(function (o) { return ["sense", "music", "words", "any"].indexOf(o.value) !== -1; }) };
        }
        return { id: id, title: kid + "がいま夢中なのは？", hint: gift ? "わからなければ「おまかせ」でOK" : "いちばん近いものをひとつ",
          options: interestOptionsFor(ageMonthsOf(a)) };
      case "budget":
        return { id: id, title: "ご予算は？", hint: "最安値 (Amazon・楽天・Yahoo!) で判定します", options: BUDGETS };
      case "priority":
        return { id: id, title: "いちばん大事にしたいことは？", hint: "知育スコアのどの項目を重く見るかが変わります", options: PRIORITIES };
      case "owned": {
        var months = ageMonthsOf(a);
        var opts = interestOptionsFor(months).filter(function (o) { return OWNED_VALUES.indexOf(o.value) !== -1; });
        return { id: id, title: "もう持っているおもちゃは？", hint: "選んだ種類は控えめにして、かぶりを減らします (いくつでも・なしでもOK)",
          multi: true, options: opts };
      }
    }
    return null;
  }

  // ---------------------------------------------------------------- 照合
  function minPrice(item) {
    var ps = [item.price_amazon, item.price_rakuten, item.price_yahoo].filter(function (p) { return p > 0; });
    return ps.length ? Math.min.apply(null, ps) : 0;
  }

  function haystack(item) {
    var tags = (item.tags || []).map(function (t) { return String(t).toLowerCase(); });
    var name = String(item.product_name || item.title || "").toLowerCase();
    return { tags: tags, name: name };
  }

  // 興味カテゴリへの一致。タグで当たれば強く、商品名だけなら弱く。
  // 当たった語を返す (理由の文言に使う)。
  function matchInterest(hs, value) {
    var it = find(INTERESTS, value);
    if (!it || !it.kw.length) return null;
    for (var i = 0; i < it.kw.length; i++) {
      var kw = it.kw[i].toLowerCase();
      for (var j = 0; j < hs.tags.length; j++) {
        if (hs.tags[j].indexOf(kw) !== -1) return { strength: "tag", word: it.kw[i] };
      }
    }
    for (var k = 0; k < it.kw.length; k++) {
      if (hs.name.indexOf(it.kw[k].toLowerCase()) !== -1) return { strength: "name", word: it.kw[k] };
    }
    return null;
  }

  function ageMinOf(item) {
    var v = parseInt(item.age_min_months, 10);
    return isNaN(v) ? 0 : v;
  }

  // 月齢の幅に対して、どこまで下の対象年齢を許すか (それより幼い物は物足りない)。
  function lowerWindow(lo) {
    if (lo < 24) return 12;
    if (lo < 72) return 24;
    return 36;
  }

  function formatMonths(m) {
    if (m <= 0) return "0か月";
    if (m < 12) return m + "か月";
    var y = Math.floor(m / 12);
    var r = m % 12;
    return r ? y + "歳" + r + "か月" : y + "歳";
  }

  function yen(n) {
    return "¥" + Number(n).toLocaleString("ja-JP");
  }

  // 1 商品の採点。null は「出さない」。relax で段階的に条件を緩めるが、
  // 対象年齢の上限 (お子さんより上の対象年齢) だけは安全のため決して緩めない。
  function scoreItem(item, a, relax) {
    relax = relax || {};
    var age = find(AGES, a.who === "birth" ? "0-5m" : a.age) || AGES[0];
    var ageMin = ageMinOf(item);
    var reasons = [];
    var score = 0;

    if (ageMin > age.hi) return null; // 安全側: 対象年齢がお子さんの年齢を超える
    var win = lowerWindow(age.lo) + (relax.age ? 24 : 0);
    if (ageMin < age.lo - win) return null; // 幼すぎて物足りない
    if (ageMin >= age.lo) {
      score += 20;
      reasons.push({ kind: "age", text: formatMonths(ageMin) + "から・いまがちょうどいい時期" });
    } else if (ageMin >= age.lo - 12) {
      score += 14;
      reasons.push({ kind: "age", text: formatMonths(ageMin) + "から・すぐ遊べる" });
    } else {
      score += 6;
      reasons.push({ kind: "age", text: formatMonths(ageMin) + "から遊べる" });
    }

    var budget = find(BUDGETS, a.budget);
    var price = minPrice(item);
    if (budget && budget.max) {
      if (!price) {
        if (!relax.price) return null;
      } else if (price > budget.max) {
        if (!relax.price || price > budget.max * 1.3) return null;
        reasons.push({ kind: "price", text: "最安 " + yen(price) + " (予算を少しこえます)" });
      } else {
        // 上限の 3 割未満は「安すぎて物足りない」可能性があるので加点を控えめに。
        score += price >= budget.max * 0.3 ? 14 : 7;
        reasons.push({ kind: "price", text: "最安 " + yen(price) + " で予算内" });
      }
    } else if (price) {
      reasons.push({ kind: "price", text: "最安 " + yen(price) });
    }

    var hs = haystack(item);
    if (a.interest && a.interest !== "any") {
      var m = matchInterest(hs, a.interest);
      var it = find(INTERESTS, a.interest);
      if (m) {
        score += m.strength === "tag" ? 26 : 16;
        reasons.push({ kind: "interest", text: "「" + it.label + "」が好きな子に" });
      } else if (!relax.interest) {
        return null;
      }
    }

    if (a.who === "birth") {
      if (hs.tags.some(function (t) { return t.indexOf("出産祝い") !== -1 || t.indexOf("ギフト") !== -1; })) {
        score += 16;
        reasons.push({ kind: "gift", text: "出産祝いの定番" });
      }
    }

    var owned = a.owned || [];
    for (var i = 0; i < owned.length; i++) {
      if (owned[i] === a.interest) continue; // 好きな遊びの「買い足し」は減点しない
      if (matchInterest(hs, owned[i])) { score -= 14; break; }
    }

    var pr = find(PRIORITIES, a.priority);
    var axes = item.ivs_axes || {};
    if (pr && pr.axis) {
      var v = Number(axes[pr.axis]) || 0;
      if (v) {
        score += Math.max(0, v - 2) / 3 * 24;
        if (v >= 4) reasons.push({ kind: "priority", text: pr.axisLabel + " " + v.toFixed(1) + " / 5" });
      }
    }
    var s100 = Number(item.ivs_score_100) || 0;
    score += s100 * (pr && pr.value === "balance" ? 0.45 : 0.25);
    if (s100 >= 85) reasons.push({ kind: "score", text: "知育スコア " + s100 + "点" });

    // 理由は「好きな遊び → 年齢 → 予算 → 大事にしたいこと」の順で 4 つまで
    var order = { gift: 0, interest: 1, age: 2, price: 3, priority: 4, score: 5 };
    reasons.sort(function (x, y) { return order[x.kind] - order[y.kind]; });
    return { score: score, reasons: reasons.slice(0, 4) };
  }

  function brandKey(item) {
    return String(item.brand || "").trim().toLowerCase();
  }

  // 同じ商品の別出品 (「トイローヤル たたいてベビードラム」と「たたいて ベビードラム」等) を
  // 1 つにまとめるための名前キー。ブランド名・空白・記号を落として比べる。
  function nameKey(item) {
    var n = String(item.product_name || item.title || "").toLowerCase();
    var b = brandKey(item);
    if (b) n = n.split(b).join("");
    return n.replace(/[\s　・、。,.!！?？()（）「」『』【】\[\]\-—ー_/]/g, "");
  }

  function sameProduct(k, seenKeys) {
    if (k.length < 4) return false;
    for (var i = 0; i < seenKeys.length; i++) {
      var s = seenKeys[i];
      if (s.length < 4) continue;
      if (s === k || (s.length >= 6 && k.indexOf(s) !== -1) || (k.length >= 6 && s.indexOf(k) !== -1)) return true;
    }
    return false;
  }

  // 同じブランドばかり並ばないよう 1 ブランド 2 件までに散らし、同じ商品の重複を除く。
  // seenKeys は呼び出し側と共有する (緩和段階をまたいでも重複させない)。
  function diversify(scored, limit, seenKeys) {
    var out = [];
    var rest = [];
    var perBrand = {};
    if (limit <= 0) return out;
    seenKeys = seenKeys || [];
    for (var i = 0; i < scored.length; i++) {
      var nk = nameKey(scored[i].item);
      if (sameProduct(nk, seenKeys)) continue;
      seenKeys.push(nk);
      var b = brandKey(scored[i].item);
      if (b && (perBrand[b] || 0) >= 2) { rest.push(scored[i]); continue; }
      if (b) perBrand[b] = (perBrand[b] || 0) + 1;
      out.push(scored[i]);
      if (out.length >= limit) return out;
    }
    return out.concat(rest).slice(0, limit);
  }

  var RELAX_STEPS = [
    { key: "", relax: {} },
    { key: "interest", relax: { interest: true } },
    { key: "price", relax: { interest: true, price: true } },
    { key: "age", relax: { interest: true, price: true, age: true } }
  ];

  // 推薦。条件に合う商品が少ないとき (< 6 件) は段階的に条件を緩めて補う。
  // 返す relaxed は最終的に使った緩和段階 ("" は緩和なし)。
  function recommend(items, a, limit) {
    limit = limit || 12;
    var picked = [];
    var seen = {};
    var relaxed = "";
    var seenKeys = [];
    // 6 件 (結果画面の初期表示数) に満たなければ次の緩和段階で補う。
    var want = Math.min(6, limit);
    for (var s = 0; s < RELAX_STEPS.length && picked.length < want; s++) {
      var batch = [];
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        var key = it.permalink || it.title || String(i);
        if (seen[key]) continue;
        var r = scoreItem(it, a, RELAX_STEPS[s].relax);
        if (r) batch.push({ item: it, score: r.score, reasons: r.reasons, key: key });
      }
      if (!batch.length) continue;
      batch.sort(function (x, y) {
        if (y.score !== x.score) return y.score - x.score;
        return (y.item.ivs_score_100 || 0) - (x.item.ivs_score_100 || 0);
      });
      var add = diversify(batch, limit - picked.length, seenKeys);
      for (var k = 0; k < add.length; k++) { seen[add[k].key] = true; picked.push(add[k]); }
      if (s > 0 && add.length) relaxed = RELAX_STEPS[s].key;
    }
    return { picks: picked, relaxed: relaxed };
  }

  // 年齢タイムライン (?age=0-1 など、ホームの age_timeline.html) 用の旧バンド。
  var LEGACY_AGE_BANDS = {
    "0-1": { label: "0〜1歳", emoji: "👶", lo: 0, hi: 23, age: "6-11m" },
    "1-2": { label: "1〜2歳", emoji: "🚶", lo: 12, hi: 35, age: "1y" },
    "3-4": { label: "3〜4歳", emoji: "🧩", lo: 36, hi: 59, age: "3y" },
    "5-6": { label: "5〜6歳", emoji: "🎒", lo: 60, hi: 83, age: "5y" }
  };

  // 年齢帯の知育スコア上位。対象年齢が帯の上限を超えるものは出さない。
  function ageBest(items, band, limit) {
    var b = LEGACY_AGE_BANDS[band];
    if (!b) return [];
    var list = items.filter(function (it) {
      var m = ageMinOf(it);
      return m <= b.hi && m >= b.lo - 12;
    }).map(function (it) { return { item: it, score: Number(it.ivs_score_100) || 0, reasons: [] }; });
    list.sort(function (x, y) { return y.score - x.score; });
    return diversify(list, limit || 10, []);
  }

  // ---------------------------------------------------------------- 結果の言語化
  var PERSONAS = {
    sense: { emoji: "🤲", title: "さわって発見タイプ", text: "手にしたものを握る・振る・なめるのも大切な学びの時間。音や手ざわりの変化があるおもちゃに夢中になりやすい時期です。" },
    build: { emoji: "🧱", title: "つくって発見タイプ", text: "積んで・崩して・また作る。正解がひとつではない遊びは、成長に合わせて遊び方が変わるので長く使えます。" },
    pretend: { emoji: "🍳", title: "なりきり名人タイプ", text: "大人のまねをしながら、言葉ややりとりを覚えていく時期。家族が一緒に「お客さん役」になると盛り上がります。" },
    vehicle: { emoji: "🚗", title: "のりもの博士タイプ", text: "走らせる・並べる・つなげる。好きなものへの「こだわり」は集中力の芽です。セットを少しずつ広げる楽しみもあります。" },
    think: { emoji: "🧩", title: "ひらめき作戦タイプ", text: "考えて、試して、できた！をくり返す遊びが好き。少しだけ難しいものを選ぶと、達成感が次の挑戦につながります。" },
    art: { emoji: "🎨", title: "ひらめきアーティストタイプ", text: "描く・貼る・こねる。自由に表現できる素材があると、思いがけない作品が生まれます。汚れ対策もしやすいものが安心です。" },
    words: { emoji: "🔤", title: "ことば・かず探検タイプ", text: "文字や数、英語への「なんで？」が増えてくる時期。遊びの中で自然に触れられるものだと、無理なく続きます。" },
    music: { emoji: "🎵", title: "リズムでノリノリタイプ", text: "音が鳴ると体が動く！音やリズムは、聞く力やまねする力につながります。音量を調節できるものだと家でも使いやすいです。" },
    science: { emoji: "🔬", title: "しくみ研究家タイプ", text: "「どうして動くの？」を確かめたい好奇心のかたまり。組み立てて動かすおもちゃは、試行錯誤そのものが学びになります。" },
    active: { emoji: "⚽", title: "元気いっぱい冒険家タイプ", text: "体を動かすのが何より好き。外でも家の中でも使えるものを選ぶと、天気に左右されずに遊べます。" },
    any: { emoji: "✨", title: "なんでも楽しむオールラウンダー", text: "いろいろな遊びに興味がある時期。まずは知育スコアの高い定番から、反応が良かった遊びを広げていきましょう。" },
    birth: { emoji: "👶", title: "はじめてのおもちゃ", text: "赤ちゃんの最初のおもちゃは、安全な素材と、見る・聞く・握るを楽しめるものが定番。名入れやギフト包装の可否も商品ページで確認を。" }
  };

  // 年齢ごとの「この時期のポイント」(一般的な目安として控えめに書く)。
  var AGE_TIPS = {
    "0-5m": "目で追う・音に反応する時期。口に入れても安心な素材と、軽くて握りやすい大きさを。",
    "6-11m": "おすわりやはいはいで世界が広がる時期。引っぱる・落とす・出し入れする遊びが大好きです。",
    "1y": "歩き始めて「自分で！」が増える時期。押す・はめる・積むなど、単純でくり返せる遊びが◎。",
    "2y": "まねっこと「いや！」の時期。できた！が感じられる少し簡単めのものが自信につながります。",
    "3y": "ごっこ遊びや会話がぐんと増える時期。家族や友だちと一緒に遊べるものも楽しめます。",
    "4y": "ルールを理解して遊べるようになる時期。かんたんなゲームや、作って遊ぶものがおすすめ。",
    "5y": "就学前で、文字や数への興味が出てくる時期。自分で考えて工夫できる遊びを。",
    "6-7y": "小学校が始まり、できることが一気に増える時期。友だちと競える・自慢できるものも人気です。",
    "8y": "本格的な仕組みや戦略を楽しめる時期。大人も一緒に夢中になれるものを選ぶと長続きします。"
  };

  function persona(a) {
    if (a.who === "birth" && (!a.interest || a.interest === "any")) return PERSONAS.birth;
    return PERSONAS[a.interest] || PERSONAS.any;
  }

  function ageTip(a) {
    return AGE_TIPS[a.who === "birth" ? "0-5m" : a.age] || "";
  }

  // 結果画面の「条件」チップ。step はタップで戻る先の設問。
  function summaryChips(a) {
    var chips = [];
    var who = find(WHO, a.who);
    if (who) chips.push({ step: "who", text: who.emoji + " " + who.label });
    var age = find(AGES, a.age);
    if (age && a.who !== "birth") chips.push({ step: "age", text: age.label });
    var it = find(INTERESTS, a.interest);
    if (it) chips.push({ step: "interest", text: it.emoji + " " + it.label });
    var b = find(BUDGETS, a.budget);
    if (b) chips.push({ step: "budget", text: b.label });
    var p = find(PRIORITIES, a.priority);
    if (p) chips.push({ step: "priority", text: p.emoji + " " + p.label });
    if (a.who === "child" && a.owned && a.owned.length) {
      chips.push({ step: "owned", text: "持っている: " + a.owned.map(function (v) { var o = find(INTERESTS, v); return o ? o.label : ""; }).filter(Boolean).join("・") });
    }
    return chips;
  }

  var RELAX_NOTES = {
    interest: "ぴったりの遊びの商品が少なかったため、近い条件のおもちゃも加えています。",
    price: "予算内の商品が少なかったため、予算を少しこえるものも加えています。",
    age: "条件に合う商品が少なかったため、少し幼い子向けのものも加えています (対象年齢をこえるものは出していません)。"
  };

  // ---------------------------------------------------------------- 回答の保存・共有
  var PARAM_KEYS = { who: "w", age: "a", interest: "i", budget: "b", priority: "p", owned: "o" };

  function emptyAnswers() {
    return { who: null, age: null, interest: null, budget: null, priority: null, owned: [] };
  }

  // 回答が結果を出せる状態か (分岐を考慮して必要な設問がすべて有効値か)。
  function isComplete(a) {
    if (!a || !find(WHO, a.who)) return false;
    var steps = stepsFor(a);
    for (var i = 0; i < steps.length; i++) {
      var s = steps[i];
      if (s === "owned") continue; // 任意
      var q = question(s, a);
      if (!q || !find(q.options, a[s])) return false;
    }
    return true;
  }

  function toQuery(a) {
    var parts = [];
    ["who", "age", "interest", "budget", "priority"].forEach(function (k) {
      if (a[k] && !(k === "age" && a.who === "birth")) parts.push(PARAM_KEYS[k] + "=" + encodeURIComponent(a[k]));
    });
    if (a.who === "child" && a.owned && a.owned.length) parts.push("o=" + a.owned.map(encodeURIComponent).join(","));
    return parts.join("&");
  }

  // URL (共有リンク) から回答を復元。不正値は捨て、揃っていなければ null。
  function fromParams(params) {
    if (!params || !params.get("w")) return null;
    var a = emptyAnswers();
    a.who = params.get("w");
    a.age = params.get("a");
    a.interest = params.get("i");
    a.budget = params.get("b");
    a.priority = params.get("p");
    var o = params.get("o");
    a.owned = o ? o.split(",").filter(function (v) { return OWNED_VALUES.indexOf(v) !== -1; }) : [];
    if (a.who === "birth") a.age = null;
    return isComplete(a) ? a : null;
  }

  // localStorage の保存形式。v2 = この作り直し以降、v1 = 旧 5 問 (q1..q5)。
  function fromSaved(saved) {
    if (!saved || !saved.answers) return null;
    if (saved.v === 2) {
      var a = emptyAnswers();
      for (var k in PARAM_KEYS) if (saved.answers[k] != null) a[k] = saved.answers[k];
      if (!Array.isArray(a.owned)) a.owned = [];
      return isComplete(a) ? a : null;
    }
    return migrateV1(saved.answers);
  }

  // 旧 5 問の回答を新しい回答に読み替える (ホームの「前回の結果を見る」を壊さない)。
  function migrateV1(q) {
    if (!q || !q.q1) return null;
    var ageMap = { "0-1": "6-11m", "1-2": "1y", "3-4": "3y", "5-6": "5y" };
    var a = emptyAnswers();
    a.who = "child";
    a.age = ageMap[q.q1] || null;
    var months = ageMonthsOf(a);
    var interestMap = { dexterity: months < 36 ? "sense" : "build", language: "words", imagination: "art", social: "pretend" };
    a.interest = interestMap[q.q2] || "any";
    if (!find(interestOptionsFor(months), a.interest)) a.interest = "any";
    a.budget = { "3000": "3000", "8000": "10000", "8000+": "any" }[q.q4] || "any";
    a.priority = "balance";
    a.owned = { block: ["build"], puzzle: ["think"], gokko: ["pretend"] }[q.q5] || [];
    return isComplete(a) ? a : null;
  }

  function toSaved(a, picks) {
    var top = picks && picks[0] ? picks[0].item : null;
    return {
      v: 2,
      answers: { who: a.who, age: a.age, interest: a.interest, budget: a.budget, priority: a.priority, owned: (a.owned || []).slice() },
      top: top ? { title: top.product_name || top.title || "", permalink: top.permalink || "", image: top.image || "" } : null,
      count: picks ? picks.length : 0,
      ts: new Date().toISOString()
    };
  }

  global.OmochaDiagnosis = {
    WHO: WHO, AGES: AGES, INTERESTS: INTERESTS, BUDGETS: BUDGETS, PRIORITIES: PRIORITIES,
    LEGACY_AGE_BANDS: LEGACY_AGE_BANDS, RELAX_NOTES: RELAX_NOTES,
    find: find, stepsFor: stepsFor, question: question, emptyAnswers: emptyAnswers, isComplete: isComplete,
    scoreItem: scoreItem, recommend: recommend, ageBest: ageBest, minPrice: minPrice, formatMonths: formatMonths,
    persona: persona, ageTip: ageTip, summaryChips: summaryChips,
    toQuery: toQuery, fromParams: fromParams, fromSaved: fromSaved, toSaved: toSaved
  };
})(typeof window !== "undefined" ? window : this);
