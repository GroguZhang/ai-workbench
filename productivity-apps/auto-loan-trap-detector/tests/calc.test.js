#!/usr/bin/env node
/**
 * 车贷工具财务计算的回归测试。
 *
 * 测试目标不是复制品，而是 index.html 里的**真实函数**：
 * 运行时从 HTML 抽出 <script> 内容，在一个最小 DOM 桩里求值，
 * 然后直接调用 computeIRR / calcTab* / setMode。
 * 这样单文件形态不用被破坏，逻辑却也进了测试。
 *
 * 运行： node tests/calc.test.js
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML_PATH = path.join(__dirname, '..', 'index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');

const match = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(match, 'index.html 里找不到内联 <script> 块');

// ---------------------------------------------------------------- DOM 桩

const inputs = Object.create(null);   // id -> 输入值
const alerts = [];                    // 记录 alert 调用

function makeEl(id) {
    const el = {
        id,
        get value() {
            return id in inputs ? String(inputs[id]) : '';
        },
        set value(v) {
            inputs[id] = v;
        },
        innerHTML: '',
        innerText: '',
        textContent: '',
        className: '',
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {},
        removeEventListener() {},
        appendChild() {},
        setAttribute() {},
        getAttribute() { return null; },
        querySelector() { return makeEl(id + '-child'); },
        querySelectorAll() { return []; },
        focus() {},
        click() {},
    };
    return el;
}

const elCache = Object.create(null);
const documentStub = {
    getElementById(id) {
        if (!elCache[id]) elCache[id] = makeEl(id);
        return elCache[id];
    },
    querySelector(sel) { return makeEl('sel:' + sel); },
    querySelectorAll() { return []; },
    addEventListener() {},
    createElement(tag) { return makeEl('new:' + tag); },
    body: makeEl('body'),
};

const sandbox = {
    document: documentStub,
    window: {},
    console,
    alert(msg) { alerts.push(String(msg)); },
    localStorage: { getItem() { return null; } , setItem() {}, removeItem() {} },
    Math, Date, Number, String, Array, Object, JSON, parseFloat, parseInt, isNaN,
};
const ctx = vm.createContext(sandbox);
vm.runInContext(match[1], ctx, { filename: 'auto-loan-inline.js' });

// renderGrid 是全局函数声明，可以被覆写来捕获计算输出
let lastGrid = null;
ctx.renderGrid = function (...args) { lastGrid = args; };

// ---------------------------------------------------------------- 断言工具

let passed = 0;
const failures = [];
function test(name, fn) {
    lastGrid = null;
    alerts.length = 0;
    try {
        fn();
        passed++;
        console.log('  ✓ ' + name);
    } catch (err) {
        failures.push({ name, err });
        console.log('  ✗ ' + name + '\n      ' + err.message);
    }
}

/** 把 computeIRR 返回的年化百分比换算成月利率，再算一次 NPV 验证自洽 */
function npvAtAnnualPct(cashflows, annualPct) {
    const monthly = annualPct / 12 / 100;
    let npv = 0;
    for (let j = 0; j < cashflows.length; j++) {
        npv += cashflows[j] / Math.pow(1 + monthly, j);
    }
    return npv;
}

// ---------------------------------------------------------------- 用例

console.log('computeIRR');

test('零利率贷款返回 0', () => {
    // 借 100、还 1 期 100：月利率恰为 0
    assert.strictEqual(ctx.computeIRR([100, -100]), 0);
});

test('正利率贷款：返回利率处的 NPV 应趋近 0', () => {
    const n = 120;
    const cf = [100000];
    for (let i = 0; i < n; i++) cf.push(-1000);
    const annual = ctx.computeIRR(cf);
    assert.ok(annual > 0, '有息贷款的年化必须为正，实际=' + annual);
    // 自洽性检验：用返回的利率回代，NPV 必须接近 0（相对本金 10 万，误差 < 1 元）
    const npv = npvAtAnnualPct(cf, annual);
    assert.ok(Math.abs(npv) < 1, 'NPV 未收敛到 0，实际=' + npv);
});

test('比等额本金还便宜的情形被夹到 0，不返回负数', () => {
    // 借 100、只需还 200 但只还 1 期 —— 数学上月利率为负，业务上应夹到 0
    const annual = ctx.computeIRR([100, -200]);
    assert.ok(annual >= 0, 'IRR 不应为负，实际=' + annual);
});

test('退化输入不抛异常且返回有限数', () => {
    for (const cf of [[0, 0, 0], [0], [100]]) {
        const v = ctx.computeIRR(cf);
        assert.ok(Number.isFinite(v), '非有限值：' + v + ' for ' + JSON.stringify(cf));
    }
});

console.log('calcTab1（常规直贷 · 等额本息）');

test('已知利率下的月供/利息/总还款与年金公式一致', () => {
    ctx.setMode('t1', 'rate');
    inputs['t1_p'] = 10;       // 万元
    inputs['t1_n'] = 60;       // 期
    inputs['t1_r'] = 4.73;     // 年化 %

    ctx.calcTab1();
    assert.ok(lastGrid, 'calcTab1 没有产出结果');

    const p = 100000;
    const n = 60;
    const r = 4.73 / 100 / 12;
    const pmt = (p * (r * Math.pow(1 + r, n))) / (Math.pow(1 + r, n) - 1);
    const total = pmt * n;
    const interest = total - p;

    const num = (s) => Number(String(s).replace(/[^0-9.]/g, ''));
    assert.strictEqual(num(lastGrid[0]), Math.round(interest), '总利息不符');
    assert.strictEqual(num(lastGrid[1]), Math.round(total), '总还款不符');
    assert.strictEqual(num(lastGrid[2]), Math.round(pmt), '月供不符');
    assert.strictEqual(String(lastGrid[3]), '4.73%', '年化应原样回显');
});

test('月供模式：真实年化由 IRR 反推，且与输入现金流自洽', () => {
    ctx.setMode('t1', 'pmt');
    inputs['t1_p'] = 20;       // 20 万
    inputs['t1_n'] = 60;
    inputs['t1_pmt'] = 4000;   // 每月 4000

    ctx.calcTab1();
    assert.ok(lastGrid, 'calcTab1 没有产出结果');

    const cf = [200000];
    for (let i = 0; i < 60; i++) cf.push(-4000);
    const expected = ctx.computeIRR(cf);
    assert.strictEqual(String(lastGrid[3]), expected.toFixed(2) + '%');
    assert.ok(Math.abs(npvAtAnnualPct(cf, expected)) < 1, 'IRR 未收敛');
});

console.log('validateSanity');

test('总还款少于本金时拦截并告警', () => {
    const ok = ctx.validateSanity(100000, 60, 1000); // 60×1000 = 6 万 < 10 万
    assert.strictEqual(ok, false, '应判定为不合理输入');
    assert.strictEqual(alerts.length, 1, '应向用户告警');
    assert.ok(alerts[0].includes('不合理'));
});

test('正常输入放行且不告警', () => {
    const ok = ctx.validateSanity(100000, 60, 2000);
    assert.strictEqual(ok, true);
    assert.strictEqual(alerts.length, 0);
});

// ---------------------------------------------------------------- 结论

console.log('');
if (failures.length) {
    console.error(failures.length + ' 个用例失败，' + passed + ' 个通过');
    process.exit(1);
}
console.log(passed + ' passed');
