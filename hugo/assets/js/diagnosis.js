document.addEventListener("DOMContentLoaded", () => {
    let currentStep = 1;
    const answers = { q1: null, q2: null, q3: null, q4: null, q5: null };
    let itemsCache = null;

    // DOM Elements
    const steps = document.querySelectorAll(".diagnosis-step");
    const prevBtn = document.getElementById("diagnosis-prev-btn");
    const retryBtn = document.getElementById("diagnosis-retry-btn");
    const progressBar = document.getElementById("diagnosis-progress-bar");
    const progressText = document.getElementById("diagnosis-progress-text");
    const wizardContainer = document.getElementById("diagnosis-wizard");
    const resultContainer = document.getElementById("diagnosis-result-container");
    const resultGrid = document.getElementById("diagnosis-result-grid");
    const fallbackBadge = document.getElementById("diagnosis-fallback-badge");

    // index.json の非同期フェッチ
    fetch("/index.json")
        .then(res => res.json())
        .then(data => {
            itemsCache = data;
        })
        .catch(err => {
            console.error("Failed to load index.json", err);
        });

    // カード描画は window.OmochaUtils.renderProductCard に集約済 (Issue #745 Phase 2)。
    // hugo/assets/js/utils/product-card.js を参照。
    const renderProductCard = (window.OmochaUtils && window.OmochaUtils.renderProductCard) || null;

    function getKeywordsForPower(q2) {
        switch (q2) {
            case 'dexterity': return ['指先', '手先', '器用', 'パズル', 'ブロック', 'つみき', 'ひもとおし', '握る', 'つまむ'];
            case 'language': return ['ことば', '英語', 'えいご', '数', 'かず', 'えほん', '絵本', '図鑑', 'ずかん', '学習', '文字', 'もじ', '音', '声', '発音'];
            case 'imagination': return ['想像', '創造', '自由', '組み立て', 'アート', 'ペイント', 'らくがき', 'お絵かき', '粘土', 'ねんど', '造形'];
            case 'social': return ['ごっこ', 'ままごと', 'メルちゃん', 'トミカ', 'プラレール', '社会', 'お医者さん', 'お店屋さん', 'コミュニケーション'];
            default: return [];
        }
    }

    function getKeywordsForOwned(q5) {
        switch (q5) {
            case 'block': return ['つみき', '積み木', 'ブロック', 'LEGO', 'レゴ', 'マグネット', 'LaQ', 'ニューブロック'];
            case 'puzzle': return ['パズル', 'ボードゲーム', 'カードゲーム', '知恵の輪', 'すごろく', 'オセロ', '将棋'];
            case 'gokko': return ['ままごと', 'ごっこ', 'キッチン', 'お医者さん', '人形', 'メルちゃん', 'シルバニア', 'ドールハウス'];
            default: return [];
        }
    }

    function scoreItem(item, q1, q2, q3, q4, q5, config = { relaxAge: false, relaxPrice: false }) {
        let score = 0;

        // 最安値の算出
        const prices = [item.price_amazon, item.price_rakuten, item.price_yahoo].filter(p => p > 0);
        const minPrice = prices.length > 0 ? Math.min(...prices) : 0;

        // 年齢適合性 (Q1)
        let minTargetMonths = 0;
        let maxTargetMonths = 72;
        if (q1 === '0-1') { maxTargetMonths = 23; }
        else if (q1 === '1-2') { minTargetMonths = 12; maxTargetMonths = 35; }
        else if (q1 === '3-4') { minTargetMonths = 24; maxTargetMonths = 59; }
        else if (q1 === '5-6') { minTargetMonths = 48; }

        // フォールバック時は年齢許容範囲を広げる
        if (config.relaxAge) {
            minTargetMonths = Math.max(0, minTargetMonths - 12);
            maxTargetMonths = maxTargetMonths + 12;
        }

        const ageMin = item.age_min_months !== undefined ? parseInt(item.age_min_months, 10) : 0;

        // 年齢適合チェック (下限は緩めに、上限は厳しめに)
        if (ageMin > maxTargetMonths || (ageMin + 12) < minTargetMonths) {
            return null; // スコアリング対象外
        }
        score += 10; // 年齢適合ボーナス

        // 予算適合性 (Q4)
        if (!config.relaxPrice) {
            if (q4 === '3000' && minPrice > 3600) return null; // 予算を20%以上オーバーするものは除外
            if (q4 === '8000' && minPrice > 9600) return null;
        }

        if (q4 === '3000' && minPrice <= 3000) score += 15;
        else if (q4 === '8000' && minPrice <= 8000) score += 15;
        else if (q4 === '8000+' && minPrice > 8000) score += 15;

        // 文字列の正規化
        const title = (item.title || "").toLowerCase();
        const content = (item.content || "").toLowerCase();
        const tags = (item.tags || []).map(t => t.toLowerCase());

        // 伸ばしたい力 (Q2)
        const powerKeywords = getKeywordsForPower(q2);
        powerKeywords.forEach(kw => {
            const lkw = kw.toLowerCase();
            if (tags.includes(lkw) || title.includes(lkw)) score += 12;
            else if (content.includes(lkw)) score += 4;
        });

        // 遊ぶ場所 (Q3)
        if (q3 === 'outdoor') {
            const outdoorKeywords = ['おでかけ', '外遊び', 'ベビーカー', 'コンパクト', '持ち運び', '公園'];
            outdoorKeywords.forEach(kw => {
                const lkw = kw.toLowerCase();
                if (tags.includes(lkw) || title.includes(lkw) || content.includes(lkw)) score += 8;
            });
        }

        // 持っているおもちゃ (Q5)
        if (q5 !== 'few') {
            const ownedKeywords = getKeywordsForOwned(q5);
            ownedKeywords.forEach(kw => {
                const lkw = kw.toLowerCase();
                if (tags.includes(lkw) || title.includes(lkw) || content.includes(lkw)) {
                    score -= 15; // ダブりを避けるための大幅なマイナス補正
                }
            });
        } else {
            score += 10; // 定番ボーナス
        }

        // 基本値として IVS スコア（知育スコア）のベース値を加算
        score += ((item.ivs_score_100 || 0) * 0.3);

        return score;
    }

    function getRecommendations(items, q1, q2, q3, q4, q5) {
        let results = [];
        let fallbackType = "";

        // 通常検索
        results = items.map(item => {
            const score = scoreItem(item, q1, q2, q3, q4, q5);
            return score !== null ? { item, score } : null;
        }).filter(x => x !== null);

        // フォールバック1: 予算緩和
        if (results.length === 0) {
            results = items.map(item => {
                const score = scoreItem(item, q1, q2, q3, q4, q5, { relaxAge: false, relaxPrice: true });
                return score !== null ? { item, score } : null;
            }).filter(x => x !== null);
            if (results.length > 0) {
                fallbackType = "price";
            }
        }

        // フォールバック2: 年齢緩和
        if (results.length === 0) {
            results = items.map(item => {
                const score = scoreItem(item, q1, q2, q3, q4, q5, { relaxAge: true, relaxPrice: false });
                return score !== null ? { item, score } : null;
            }).filter(x => x !== null);
            if (results.length > 0) {
                fallbackType = "age";
            }
        }

        // フォールバック3: 両方緩和
        if (results.length === 0) {
            results = items.map(item => {
                const score = scoreItem(item, q1, q2, q3, q4, q5, { relaxAge: true, relaxPrice: true });
                return score !== null ? { item, score } : null;
            }).filter(x => x !== null);
            if (results.length > 0) {
                fallbackType = "both";
            }
        }

        // フォールバック4: 全商品からIVSスコア上位
        if (results.length === 0) {
            results = items.map(item => {
                const score = (item.ivs_score_100 || 0);
                return { item, score };
            });
            fallbackType = "general";
        }

        // ソート (スコアの高い順、IVSスコアの高い順)
        results.sort((a, b) => {
            if (b.score !== a.score) return b.score - a.score;
            return (b.item.ivs_score_100 || 0) - (a.item.ivs_score_100 || 0);
        });

        return {
            recommendations: results.slice(0, 3).map(x => x.item),
            fallbackType
        };
    }

    function updateStepUI() {
        steps.forEach(step => {
            const stepNum = parseInt(step.getAttribute("data-step"), 10);
            if (stepNum === currentStep) {
                step.classList.add("active");
            } else {
                step.classList.remove("active");
            }
        });

        // プログレスバーの更新
        const progressPercent = ((currentStep - 1) / 5) * 100;
        progressBar.style.width = `${progressPercent}%`;
        progressText.textContent = `質問 ${currentStep} / 5`;

        // 戻るボタンの表示制御
        if (currentStep > 1 && currentStep <= 5) {
            prevBtn.style.display = "inline-block";
        } else {
            prevBtn.style.display = "none";
        }
    }

    // 選択ボタンのハンドラー
    const optionBtns = document.querySelectorAll(".diagnosis-option-btn");
    optionBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            const name = btn.getAttribute("data-name");
            const val = btn.getAttribute("data-value");
            answers[name] = val;

            if (currentStep < 5) {
                currentStep++;
                updateStepUI();
            } else {
                // すべて回答完了
                currentStep = 6;
                updateStepUI();
                progressBar.style.width = "100%";
                progressText.textContent = "集計中...";
                showResults();
            }
        });
    });

    // 戻るボタン
    prevBtn.addEventListener("click", () => {
        if (currentStep > 1) {
            currentStep--;
            updateStepUI();
        }
    });

    // やり直しボタン
    retryBtn.addEventListener("click", () => {
        currentStep = 1;
        // 回答をクリア
        for (let key in answers) answers[key] = null;
        
        resultContainer.style.display = "none";
        wizardContainer.style.display = "block";
        prevBtn.style.display = "none";
        progressBar.parentElement.style.display = "block"; // プログレスバーを再表示
        
        updateStepUI();
    });

    function showResults() {
        if (!itemsCache) {
            // ロードがまだ終わっていない場合はリトライ
            setTimeout(showResults, 100);
            return;
        }

        const { recommendations, fallbackType } = getRecommendations(
            itemsCache,
            answers.q1,
            answers.q2,
            answers.q3,
            answers.q4,
            answers.q5
        );

        // UI表示の切り替え
        wizardContainer.style.display = "none";
        progressBar.parentElement.style.display = "none"; // プログレスバーを非表示に
        prevBtn.style.display = "none";
        resultContainer.style.display = "block";

        // フォールバックバッジの表示
        if (fallbackType) {
            fallbackBadge.style.display = "inline-block";
            if (fallbackType === "price") {
                fallbackBadge.textContent = "💡 条件緩和: 予算の上限を広げておもちゃをお探ししました。";
            } else if (fallbackType === "age") {
                fallbackBadge.textContent = "💡 条件緩和: 対象年齢の幅を広げておもちゃをお探ししました。";
            } else if (fallbackType === "both") {
                fallbackBadge.textContent = "💡 条件緩和: 予算と対象年齢を広げておもちゃをお探ししました。";
            } else if (fallbackType === "general") {
                fallbackBadge.textContent = "💡 条件緩和: 該当商品が少なかったため、全商品から人気の定番知育玩具をご案内します。";
            }
        } else {
            fallbackBadge.style.display = "none";
        }

        // 結果カードの動的生成
        resultGrid.innerHTML = "";
        if (renderProductCard) {
            recommendations.forEach(item => {
                resultGrid.appendChild(renderProductCard(item));
            });
        }

        // #1365 Layer 1-③ 診断結果の永続化。次回訪問時にホーム上部の
        // 「前回の診断」バナー (last_diagnosis.js) で再表示する。
        // ASIN は index.json に無く、推薦は回答から決定的に再現できるため
        // 回答そのものを保存し、再閲覧時 (?restore=1) はその場で再計算する。
        // top はバナーのサムネ/タイトル表示用の軽量スナップショットのみ。
        try {
            const top = recommendations[0] || null;
            localStorage.setItem("omcha_last_diagnosis", JSON.stringify({
                answers: { q1: answers.q1, q2: answers.q2, q3: answers.q3, q4: answers.q4, q5: answers.q5 },
                top: top ? {
                    title: top.product_name || top.title || "",
                    permalink: top.permalink || "",
                    image: top.image || ""
                } : null,
                count: recommendations.length,
                ts: new Date().toISOString()
            }));
        } catch (e) {
            // localStorage が無効/満杯の環境では永続化を諦める (診断自体は成立)
        }
    }

    // #1365 Layer 1-③ 前回診断の再閲覧。ホームの「前回の診断」バナーから
    // /diagnosis/?restore=1 で遷移してきた場合、保存済みの回答を復元して
    // ウィザードを飛ばし結果だけ即再表示する (推薦は最新カタログで再計算)。
    function restoreFromSaved() {
        const params = new URLSearchParams(window.location.search);
        if (params.get("restore") !== "1") return;
        let saved;
        try {
            saved = JSON.parse(localStorage.getItem("omcha_last_diagnosis") || "null");
        } catch (e) {
            return;
        }
        if (!saved || !saved.answers || !saved.answers.q1) return;
        answers.q1 = saved.answers.q1;
        answers.q2 = saved.answers.q2;
        answers.q3 = saved.answers.q3;
        answers.q4 = saved.answers.q4;
        answers.q5 = saved.answers.q5;
        currentStep = 6;
        updateStepUI();
        progressText.textContent = "前回の結果";
        showResults();
    }

    // #1365 Layer 3-③ 年齢タイムライン。ホームの横スクロール年齢バンド
    // (age_timeline.html) から /diagnosis/?age=0-1 等で遷移してきた場合、
    // ウィザードを飛ばしてその年齢帯の知育スコア上位 (ベスト10) を即表示する
    // (診断の簡易版)。バンド値は Q1 (data-value) / age_timeline.html と一致。
    const AGE_BANDS = {
        "0-1": { label: "0〜1歳", emoji: "👶" },
        "1-2": { label: "1〜2歳", emoji: "🚶" },
        "3-4": { label: "3〜4歳", emoji: "🧩" },
        "5-6": { label: "5〜6歳", emoji: "🎒" }
    };

    // 指定年齢帯で適合する商品を IVS スコア順に上位 limit 件返す。
    // scoreItem は年齢帯外を null で弾き、適合品には 10 + IVS*0.3 を返すため
    // (q2-q5=null で keyword/予算ボーナスはゼロ)、score 降順 = IVS 降順になる。
    function getAgeBest(items, q1, limit) {
        return items
            .map(item => {
                const score = scoreItem(item, q1, null, null, null, null);
                return score !== null ? { item, score } : null;
            })
            .filter(x => x !== null)
            .sort((a, b) => {
                if (b.score !== a.score) return b.score - a.score;
                return (b.item.ivs_score_100 || 0) - (a.item.ivs_score_100 || 0);
            })
            .slice(0, limit)
            .map(x => x.item);
    }

    function showAgeBest(q1) {
        if (!itemsCache) {
            setTimeout(() => showAgeBest(q1), 100);
            return;
        }
        const band = AGE_BANDS[q1];
        const items = getAgeBest(itemsCache, q1, 10);

        wizardContainer.style.display = "none";
        progressBar.parentElement.style.display = "none";
        prevBtn.style.display = "none";
        resultContainer.style.display = "block";
        fallbackBadge.style.display = "none";

        // 結果見出しを「年齢ベスト10」に差し替える。
        const header = resultContainer.querySelector(".diagnosis-result-header");
        if (header) {
            const h = header.querySelector("h2");
            const p = header.querySelector("p");
            if (h) h.textContent = `${band.emoji} ${band.label}のベスト10`;
            if (p) p.textContent = `${band.label}のお子さんに、知育スコアの高い定番おもちゃを集めました。もっと細かく選ぶなら下の「もう一度診断する」から 5 問診断もどうぞ。`;
        }

        resultGrid.innerHTML = "";
        if (renderProductCard) {
            items.forEach(item => resultGrid.appendChild(renderProductCard(item)));
        }
    }

    function restoreFromAge() {
        const params = new URLSearchParams(window.location.search);
        const age = params.get("age");
        if (!age || !AGE_BANDS[age]) return false;
        answers.q1 = age;
        currentStep = 6;
        updateStepUI();
        progressText.textContent = `${AGE_BANDS[age].label}のベスト10`;
        showAgeBest(age);
        return true;
    }

    // ?age=<band> が最優先。無効/未指定なら前回診断 (?restore=1) を試す。
    if (!restoreFromAge()) {
        restoreFromSaved();
    }
});
