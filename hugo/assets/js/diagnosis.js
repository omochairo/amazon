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

    function formatAge(m) {
        if (m === undefined || m === null) return "";
        m = parseInt(m, 10);
        if (m === 0) return "0歳〜";
        if (m < 12) return `${m}ヶ月〜`;
        const years = Math.floor(m / 12);
        const rem = m % 12;
        if (rem === 0) return `${years}歳〜`;
        if (rem === 6) return `${years}.5歳〜`;
        return `${(m / 12).toFixed(1)}歳〜`;
    }

    function formatPrice(p) {
        if (!p) return "";
        return new Intl.NumberFormat('ja-JP').format(p);
    }

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
        recommendations.forEach(item => {
            const cardHtml = createProductCardHtml(item);
            resultGrid.appendChild(cardHtml);
        });
    }

    function createProductCardHtml(item) {
        const prices = [item.price_amazon, item.price_rakuten, item.price_yahoo].filter(p => p > 0);
        const minPrice = prices.length > 0 ? Math.min(...prices) : 0;
        const score100 = item.ivs_score_100 || 0;
        const score = item.ivs_score || 0;

        let tierClass = "score-bronze";
        if (score100 >= 90) tierClass = "score-gold";
        else if (score100 >= 75) tierClass = "score-silver";

        const ageStr = formatAge(item.age_min_months);

        const card = document.createElement("a");
        card.className = "product-card";
        card.href = item.permalink;
        card.setAttribute("aria-label", `${item.product_name || item.title} の比較レビューを読む`);

        let badgeHtml = "";
        if (score100) {
            badgeHtml = `
                <div class="product-card-badges">
                    <span class="product-card-score ${tierClass}">
                        🏆 <strong>${score100}</strong><span class="product-card-score-suffix">点</span>
                        ${minPrice > 0 ? `<span class="product-card-score-price">最安 &yen;${formatPrice(minPrice)}</span>` : ""}
                    </span>
                </div>
            `;
        } else if (score) {
            badgeHtml = `
                <div class="product-card-badges">
                    <span class="product-card-score score-bronze">
                        🏆 <strong>${score.toFixed(1)}</strong><span class="product-card-score-suffix">/5</span>
                        ${minPrice > 0 ? `<span class="product-card-score-price">最安 &yen;${formatPrice(minPrice)}</span>` : ""}
                    </span>
                </div>
            `;
        }

        const tagsHtml = (item.tags || []).slice(0, 2).map(t => `<span class="product-card-tag">#${t}</span>`).join("");

        card.innerHTML = `
            <div class="product-card-image">
                ${item.image ? `<img src="${item.image}" alt="${item.product_name || item.title}" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : `<div class="product-card-image-fallback">&#129528;</div>`}
                ${badgeHtml}
                ${ageStr ? `<span class="product-card-age" title="対象年齢">👶 ${ageStr}</span>` : ""}
            </div>
            <div class="product-card-body">
                ${item.brand ? `<div class="product-card-brand">${item.brand}</div>` : ""}
                <h3 class="product-card-title">${item.title}</h3>
                <div class="product-card-meta">
                    <time datetime="${item.date}">${item.date}</time>
                    ${tagsHtml ? `<span class="product-card-tags">${tagsHtml}</span>` : ""}
                </div>
            </div>
        `;

        return card;
    }

    // 自動テスト用シミュレータ
    if (window.location.search.includes("test=true")) {
        console.log("DIAGNOSIS_TEST: Auto test mode detected. Waiting for data load...");
        
        function runAutoTest() {
            if (!itemsCache) {
                setTimeout(runAutoTest, 100);
                return;
            }
            console.log("DIAGNOSIS_TEST: Data loaded. Starting simulation...");
            
            let stepTimer = setInterval(() => {
                const activeStep = document.querySelector(".diagnosis-step.active");
                if (activeStep) {
                    const stepNum = activeStep.getAttribute("data-step");
                    const firstOption = activeStep.querySelector(".diagnosis-option-btn");
                    if (firstOption) {
                        console.log(`DIAGNOSIS_TEST: Clicking step ${stepNum} option: ${firstOption.querySelector(".label").textContent}`);
                        firstOption.click();
                    } else {
                        console.error(`DIAGNOSIS_TEST: No options found in step ${stepNum}`);
                        clearInterval(stepTimer);
                    }
                } else {
                    // 5問回答完了して結果表示に移行しているはず
                    clearInterval(stepTimer);
                    
                    // 結果の確認
                    setTimeout(() => {
                        const resultCards = document.querySelectorAll("#diagnosis-result-grid .product-card");
                        console.log(`DIAGNOSIS_TEST: Simulation completed. Cards count: ${resultCards.length}`);
                        if (resultCards.length > 0) {
                            const titles = Array.from(resultCards).map(c => c.querySelector(".product-card-title").textContent);
                            console.log("DIAGNOSIS_TEST_SUCCESS: Recommended toys:", JSON.stringify(titles));
                        } else {
                            console.error("DIAGNOSIS_TEST_ERROR: No cards rendered.");
                        }
                    }, 500);
                }
            }, 300);
        }
        
        runAutoTest();
    }
});
