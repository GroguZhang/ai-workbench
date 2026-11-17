/* =========================================================================
   Card Vault · UI 增强 v4
   只做"锦上添花"的动效与状态，不接管任何业务逻辑。
   若浏览器不支持 / 用户禁用了动效，全部降级为"直接可见"。
   ========================================================================= */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- 1. 毛玻璃导航：滚动后加边框 ---------- */
  function initNav() {
    var nav = document.querySelector('.vault-nav');
    if (!nav) return;
    var onScroll = function () {
      if (window.scrollY > 8) nav.classList.add('is-scrolled');
      else nav.classList.remove('is-scrolled');
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
  }

  /* ---------- 2. 滚动淡入 ---------- */
  var revealObserver = null;

  function ensureObserver() {
    if (revealObserver || reduceMotion || !('IntersectionObserver' in window)) return;
    revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-in');
          revealObserver.unobserve(entry.target);
        }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.06 });
  }

  // 需要淡入的容器：卡片、看板、统计面板、行情样本区
  var REVEAL_SELECTOR = [
    '#cards-grid > *',
    '#portfolio-dashboard',
    '#stats-panel',
    '.market-samples > *',
    '#dash-top5-list > *',
    '.stat-badge'
  ].join(',');

  function markReveal(root) {
    if (reduceMotion || !revealObserver) return;
    var scope = root && root.querySelectorAll ? root : document;
    var nodes = [];
    if (scope.matches && scope.matches(REVEAL_SELECTOR)) nodes.push(scope);
    Array.prototype.forEach.call(scope.querySelectorAll(REVEAL_SELECTOR), function (el) {
      nodes.push(el);
    });
    nodes.forEach(function (el, i) {
      if (el.dataset.revealed === '1') return;
      el.dataset.revealed = '1';
      el.classList.add('reveal');
      // 同一批节点错开 55ms，做出瀑布感
      el.style.transitionDelay = Math.min(i, 8) * 55 + 'ms';
      revealObserver.observe(el);
    });
  }

  /* ---------- 3. 动态内容（卡片列表是 JS 渲染的）跟随淡入 ---------- */
  function initReveal() {
    if (reduceMotion || !('IntersectionObserver' in window)) return;
    ensureObserver();
    markReveal(document);

    if (!('MutationObserver' in window)) return;
    var mo = new MutationObserver(function (muts) {
      var dirty = false;
      muts.forEach(function (m) {
        if (m.addedNodes && m.addedNodes.length) dirty = true;
      });
      if (dirty) markReveal(document);
    });
    ['cards-grid', 'dash-top5-list', 'detail-market-body', 'stats-dim-body'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) mo.observe(el, { childList: true, subtree: true });
    });
  }

  /* ---------- 4. 数字排版：金额统一等宽 ---------- */
  function initNumeric() {
    var SEL = '#stat-val, #stat-count, #dash-total-val, #dash-holding-count, ' +
              '#dash-top-val, #st-value, #st-delta, #st-coverage, #st-pnl, ' +
              '#fmv-val-price, #fmv-pnl-val, #fmv-max-val, #fmv-min-val, ' +
              '#val-main-price, .market-sample-price';
    Array.prototype.forEach.call(document.querySelectorAll(SEL), function (el) {
      el.style.fontVariantNumeric = 'tabular-nums';
    });
  }

  /* ---------- 5. 启动 ---------- */
  function boot() {
    initNav();
    initReveal();
    initNumeric();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
