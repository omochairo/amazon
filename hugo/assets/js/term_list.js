/* #6206 chrome 削減: _default/term.html のインライン <script> をここへ移した。
   実測 (2026-08-31): 同一内容の <script> が term/brand 4,112 枚に焼かれ 18.5 MB。
   defer で読むので DOMContentLoaded より前に評価される = 元のリスナ登録と等価。 */
document.addEventListener("DOMContentLoaded", function() {
  const grid = document.getElementById("term-card-grid");
  if (!grid) return;
  const taxonomy = grid.getAttribute("data-taxonomy"); // "brands" or "tags"
  const term = grid.getAttribute("data-term"); // "メーカー名" or "タグ名"
  const sortBtns = document.querySelectorAll(".sort-btn");
  const paginationContainer = document.getElementById("term-pagination-container");
  
  // 年齢フィルター用の要素
  const ageBtns = document.querySelectorAll(".age-btn");
  const clearBtn = document.getElementById("age-filter-clear");
  
  let allItems = [];
  let currentSort = "score"; // デフォルト
  let activeAgeFilter = null; // 選択された月数(数値) またはオブジェクト {min, max, type: 'stage'}
  let ageFilterMode = "match"; // "match" または "all"
  let currentPage = 1;
  const ITEMS_PER_PAGE = 20;
  const AGE_STEPS = [0, 3, 6, 10, 12, 18, 24, 36, 48, 60, 72, 84, 108, 144, 180];

  // カード描画は window.OmochaUtils.renderProductCard に集約済
  // (Issue #745 Phase 2 / hugo/assets/js/utils/product-card.js)。
  const renderProductCard = (window.OmochaUtils && window.OmochaUtils.renderProductCard) || null;

  // index.json ロード用
  async function ensureItemsLoaded() {
    if (allItems.length > 0) return true;
    try {
      const res = await fetch("/index.json");
      const data = await res.json();
      allItems = data.filter(item => {
        if (taxonomy === "brands") {
          return item.brands && item.brands.includes(term);
        } else if (taxonomy === "tags") {
          return item.tags && item.tags.includes(term);
        }
        return false;
      });
      return true;
    } catch (err) {
      console.error("Failed to load index.json", err);
      return false;
    }
  }

  // フィルターとソートを適用して再描画
  function updateList() {
    let items = [...allItems];
    
    // 1. 年齢フィルター適用
    if (activeAgeFilter !== null) {
      items = items.filter(item => {
        const minMonths = parseInt(item.age_min_months || 0, 10);
        
        if (typeof activeAgeFilter === "number") {
          // 個別年齢ボタン選択時
          const targetAge = activeAgeFilter;
          if (ageFilterMode === "match") {
            const idx = AGE_STEPS.indexOf(targetAge);
            const prevAge = idx > 0 ? AGE_STEPS[idx - 1] : 0;
            return minMonths >= prevAge && minMonths <= targetAge;
          } else {
            return minMonths <= targetAge;
          }
        } else if (activeAgeFilter && typeof activeAgeFilter === "object") {
          // ステージ全体選択時
          const min = activeAgeFilter.min;
          const max = activeAgeFilter.max;
          if (ageFilterMode === "match") {
            return minMonths >= min && minMonths <= max;
          } else {
            return minMonths <= max;
          }
        }
        return true;
      });
      if (clearBtn) clearBtn.style.display = "block";
    } else {
      if (clearBtn) clearBtn.style.display = "none";
    }
    
    // 2. ソート適用
    if (currentSort === "score") {
      items.sort((a, b) => (b.ivs_score_100 || 0) - (a.ivs_score_100 || 0));
    } else if (currentSort === "price") {
      items.sort((a, b) => {
        const minA = Math.min(a.price_amazon || 99999999, a.price_rakuten || 99999999, a.price_yahoo || 99999999);
        const minB = Math.min(b.price_amazon || 99999999, b.price_rakuten || 99999999, b.price_yahoo || 99999999);
        return minA - minB;
      });
    } else if (currentSort === "date") {
      items.sort((a, b) => (b.unix_time || 0) - (a.unix_time || 0));
    }
    
    // 3. レンダリング
    currentPage = 1;
    renderCards(items, currentPage);
  }

  function renderCards(items, page) {
    if (page === 1) grid.innerHTML = "";
    
    if (items.length === 0) {
      grid.innerHTML = `<div class="no-results-message" style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-color, #6b7280); font-weight: 600;">条件に一致するおもちゃが見つかりませんでした。</div>`;
      paginationContainer.innerHTML = "";
      return;
    }

    const start = (page - 1) * ITEMS_PER_PAGE;
    const end = start + ITEMS_PER_PAGE;
    const itemsToShow = items.slice(start, end);
    
    itemsToShow.forEach(item => {
      if (!renderProductCard) return;
      grid.appendChild(renderProductCard(item));
    });

    if (items.length > end) {
      paginationContainer.innerHTML = `<div style="text-align:center; margin-top: 20px;">
        <button id="load-more-btn" class="sort-btn" style="padding: 10px 24px;">さらに読み込む</button>
      </div>`;
      document.getElementById("load-more-btn").addEventListener("click", () => {
        currentPage++;
        renderCards(items, currentPage);
      });
    } else {
      paginationContainer.innerHTML = "";
    }
  }

  // 並び替えボタン
  sortBtns.forEach(btn => {
    btn.addEventListener("click", async function() {
      const sortType = this.getAttribute("data-sort");
      currentSort = sortType;
      
      sortBtns.forEach(b => b.classList.toggle("active", b === btn));
      
      const loaded = await ensureItemsLoaded();
      if (!loaded) return;
      
      updateList();
    });
  });

  // トグルスイッチの制御
  const toggleMatch = document.getElementById("age-toggle-match");
  const toggleAll = document.getElementById("age-toggle-all");
  
  function updateToggleUI() {
    if (!toggleMatch || !toggleAll) return;
    if (ageFilterMode === "match") {
      toggleMatch.classList.add("active");
      toggleMatch.setAttribute("aria-checked", "true");
      toggleAll.classList.remove("active");
      toggleAll.setAttribute("aria-checked", "false");
    } else {
      toggleAll.classList.add("active");
      toggleAll.setAttribute("aria-checked", "true");
      toggleMatch.classList.remove("active");
      toggleMatch.setAttribute("aria-checked", "false");
    }
  }

  if (toggleMatch) {
    toggleMatch.addEventListener("click", async function() {
      if (ageFilterMode === "match") return;
      ageFilterMode = "match";
      updateToggleUI();
      const loaded = await ensureItemsLoaded();
      if (loaded) updateList();
    });
  }

  if (toggleAll) {
    toggleAll.addEventListener("click", async function() {
      if (ageFilterMode === "all") return;
      ageFilterMode = "all";
      updateToggleUI();
      const loaded = await ensureItemsLoaded();
      if (loaded) updateList();
    });
  }

  // アコーディオン開閉＆ステージ選択
  const stages = document.querySelectorAll(".age-filter-stage");
  stages.forEach(stage => {
    const trigger = stage.querySelector(".age-filter-stage-trigger");
    if (!trigger) return;

    trigger.addEventListener("click", async function() {
      const isOpen = stage.classList.contains("is-open");
      
      // 他のステージを閉じる＆ステージ選択解除
      stages.forEach(s => {
        if (s !== stage) {
          s.classList.remove("is-open");
          s.classList.remove("stage-active");
          const trig = s.querySelector(".age-filter-stage-trigger");
          if (trig) trig.setAttribute("aria-expanded", "false");
        }
      });

      // 個別年齢ボタンのアクティブも解除する
      ageBtns.forEach(b => b.classList.remove("active"));

      if (!isOpen) {
        // 開く場合：このステージを選択状態にする
        stage.classList.add("is-open");
        stage.classList.add("stage-active");
        trigger.setAttribute("aria-expanded", "true");
        
        const min = parseInt(stage.getAttribute("data-min"), 10);
        const max = parseInt(stage.getAttribute("data-max"), 10);
        activeAgeFilter = { min: min, max: max, type: "stage" };
      } else {
        // 閉じる場合：選択を解除する
        stage.classList.remove("is-open");
        stage.classList.remove("stage-active");
        trigger.setAttribute("aria-expanded", "false");
        
        // 閉じられたステージが現在選択されていたら、フィルター解除
        if (activeAgeFilter && activeAgeFilter.type === "stage" && activeAgeFilter.min === parseInt(stage.getAttribute("data-min"), 10)) {
          activeAgeFilter = null;
        }
      }

      const loaded = await ensureItemsLoaded();
      if (!loaded) return;
      updateList();
    });
  });

  // 個別年齢ボタンクリック時の処理
  ageBtns.forEach(btn => {
    btn.addEventListener("click", async function(e) {
      e.stopPropagation(); // アコーディオンのトリガー等への伝播を防ぐ
      
      const targetAge = parseInt(this.getAttribute("data-age"), 10);
      
      // 個別ボタンのアクティブ切り替え
      ageBtns.forEach(b => b.classList.toggle("active", b === btn));
      
      // 親ステージのステージ選択状態(ヘッダーハイライト)は解除する
      stages.forEach(s => s.classList.remove("stage-active"));
      
      activeAgeFilter = targetAge;
      
      const loaded = await ensureItemsLoaded();
      if (!loaded) return;
      
      updateList();
    });
  });

  // クリアボタン
  if (clearBtn) {
    clearBtn.addEventListener("click", function() {
      ageBtns.forEach(b => b.classList.remove("active"));
      stages.forEach(s => {
        s.classList.remove("is-open");
        s.classList.remove("stage-active");
        const trig = s.querySelector(".age-filter-stage-trigger");
        if (trig) trig.setAttribute("aria-expanded", "false");
      });
      activeAgeFilter = null;
      updateList();
    });
  }
});
