"""Web 控制台：调参数、管人设要求、翻记忆库、管图片策略、看表情包库。

挂在 NoneBot 自己的 FastAPI 实例上（driver.server_app），所以不需要另起进程。
前提是 driver 不是纯 websockets —— 见 .env 里的 DRIVER=~fastapi+~websockets。
若 driver 不支持，这里会打一条日志并跳过注册，机器人本身照常工作。

参数表由 settings.describe() 动态生成，加参数不用改这个文件。
人设要求 / 记忆 / 图片策略是改造后新增的三块 —— 它们跟参数的区别在于
**是运行时长出来的数据，不是配置**，所以各有各的读写接口，不塞进 settings。

标签页与后端能力的对应：

| 标签 | 后端 | 说明 |
|---|---|---|
| 参数 | `settings.describe()` | .env 可调项，改完即时生效 |
| 人格三层 | `persona.layers()` | **只读**：看三层、看自动改动日志、手动触发一次反思、撤回最近一次改动 |
| 记忆库 | `memory.all_facts()` | 看 / 搜 / 删 / 保护 / 手动补一条 |
| 图片策略 | `state.snapshot()` | 哪些会话被设成了不存图 |
"""

import asyncio
import json
import logging
import time

from nonebot import get_driver

from . import (
    clock,
    config,
    greetings,
    llm,
    memory,
    persona,
    persona_eval,
    persona_iter,
    proactive,
    search,
    settings,
    state,
    stickers,
)

logger = logging.getLogger("ai_chat.webui")

_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>鲸鱼娘 · 控制台</title>
<style>
  :root { --bg:#0f1720; --card:#182430; --line:#25384a; --fg:#e6eef6; --dim:#8fa6bb;
          --accent:#4aa3df; --ok:#3fb950; --warn:#d29922; --bad:#f85149; }
  * { box-sizing:border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
         font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--dim); margin-bottom:16px; font-size:13px; }
  .badges { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
  .badge { background:var(--card); border:1px solid var(--line); border-radius:999px;
           padding:4px 12px; font-size:12px; color:var(--dim); }
  .badge b { color:var(--fg); font-weight:600; }
  .tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:18px;
          flex-wrap:wrap; }
  .tab { background:none; border:0; border-bottom:2px solid transparent; color:var(--dim);
         padding:8px 16px; cursor:pointer; font-size:14px; border-radius:0; }
  .tab:hover { color:var(--fg); background:rgba(255,255,255,.03); }
  .tab.active { color:var(--accent); border-bottom-color:var(--accent); font-weight:600; }
  .panel { display:none; }
  .panel.active { display:block; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px;
          margin-bottom:16px; }
  .card h2 { font-size:14px; margin:0 0 12px; color:var(--accent); font-weight:600; }
  .card h2 .count { color:var(--dim); font-weight:400; }
  /* 参数页改成**折叠列表**：一行一个组（原来是 auto-fit 网格，一组占一个方块），
     点标题才展开。`#settings` 用 id 选择器覆盖 `.grid` 的 grid。 */
  #settings { display:flex; flex-direction:column; gap:10px; }
  .card.acc { padding:0; margin-bottom:0; overflow:hidden; }
  .acc-head { display:flex; align-items:center; gap:10px; width:100%; text-align:left;
              background:none; border:0; border-radius:0; padding:12px 16px; color:var(--fg);
              font-size:14px; cursor:pointer; }
  .acc-head:hover { background:rgba(255,255,255,.04); }
  .acc-head .caret { color:var(--dim); font-size:11px; transition:transform .15s; }
  .card.acc.open .acc-head .caret { transform:rotate(90deg); }
  .acc-head .title { color:var(--accent); font-weight:600; }
  .acc-head .meta { margin-left:auto; color:var(--dim); font-size:11px; }
  .acc-body { display:none; padding:2px 16px 14px; border-top:1px solid var(--line); }
  .card.acc.open .acc-body { display:block; }
  .row { display:flex; align-items:center; justify-content:space-between; gap:12px;
         padding:7px 0; border-bottom:1px dashed rgba(255,255,255,.05); }
  .row:last-child { border-bottom:0; }
  .row label { flex:1; }
  .row .hint { display:block; color:var(--dim); font-size:11px; margin-top:2px; }
  input[type=number],input[type=text],select,textarea { width:110px; background:#0d151d; color:var(--fg);
        border:1px solid var(--line); border-radius:6px; padding:5px 8px; font-size:13px; text-align:right; }
  input[type=text],select,textarea { text-align:left; width:180px; }
  input[type=checkbox] { width:18px; height:18px; accent-color:var(--accent); }
  input:focus,textarea:focus,select:focus { outline:1px solid var(--accent); }
  button { background:#22384d; color:var(--fg); border:1px solid var(--line); border-radius:6px;
           padding:6px 14px; cursor:pointer; font-size:13px; }
  button:hover { background:#2b465f; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#04121c; font-weight:600; }
  button.mini { padding:2px 8px; font-size:11px; }
  button.danger:hover { background:#7d2222; }
  .toolbar { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 16px; align-items:center; }
  .gallery { display:grid; grid-template-columns:repeat(auto-fill,minmax(110px,1fr)); gap:10px; }
  .thumb { position:relative; background:#0d151d; border:1px solid var(--line); border-radius:8px;
           overflow:hidden; aspect-ratio:1; display:flex; align-items:center; justify-content:center; }
  .thumb img { max-width:100%; max-height:100%; object-fit:contain; }
  .thumb .meta { position:absolute; left:0; right:0; bottom:0; background:rgba(0,0,0,.72);
                 font-size:10px; padding:3px 5px; color:var(--dim); }
  .thumb .del { position:absolute; top:4px; right:4px; background:rgba(200,40,40,.85); border:0;
                border-radius:4px; color:#fff; padding:1px 6px; font-size:11px; cursor:pointer; }
  .empty { color:var(--dim); padding:24px; text-align:center; }
  table.items { width:100%; border-collapse:collapse; font-size:13px; }
  table.items th { text-align:left; color:var(--dim); font-weight:500; font-size:11px;
                   padding:4px 6px; border-bottom:1px solid var(--line); }
  table.items td { padding:6px; border-bottom:1px dashed rgba(255,255,255,.05); vertical-align:top; }
  table.items td.act { white-space:nowrap; width:1%; }
  .tag { display:inline-block; background:#0d151d; border:1px solid var(--line); border-radius:4px;
         padding:0 5px; font-size:11px; color:var(--dim); margin-right:4px; }
  .tag.on { color:var(--ok); border-color:#1f4d2b; }
  .tag.lock { color:var(--warn); border-color:#5c4415; }
  .muted { color:var(--dim); font-size:12px; }
  #toast { position:fixed; right:20px; bottom:20px; background:var(--card); border:1px solid var(--line);
           border-left:3px solid var(--ok); border-radius:8px; padding:10px 16px; opacity:0;
           transition:opacity .2s; pointer-events:none; max-width:420px; }
  #toast.show { opacity:1; }
  #toast.err { border-left-color:var(--bad); }
</style>
</head>
<body>
  <h1>🐋 鲸鱼娘 · 控制台</h1>
  <div class="sub">改完即时生效，不需要重启机器人。参数存 data/settings.json，人格是三个文本文件（底色 / 禁止事项 / 表层），记忆存 data/memory.db。</div>
  <div class="badges" id="badges"></div>

  <div class="tabs">
    <button class="tab active" data-tab="settings" onclick="tab('settings')">参数</button>
    <button class="tab" data-tab="model" onclick="tab('model')">模型</button>
    <button class="tab" data-tab="persona" onclick="tab('persona')">人设要求</button>
    <button class="tab" data-tab="memory" onclick="tab('memory')">记忆库</button>
    <button class="tab" data-tab="image" onclick="tab('image')">图片策略</button>
    <button class="tab" data-tab="sticker" onclick="tab('sticker')">表情包库</button>
  </div>

  <!-- ---------------------------------------------------------- 参数 -->
  <div class="panel active" id="panel-settings">
    <div class="toolbar">
      <button class="primary" onclick="speak()">让它现在说一句</button>
      <button onclick="greet()">现在问候一次</button>
      <button onclick="resetAll()">全部恢复 .env 默认</button>
      <button onclick="load()">刷新</button>
      <button onclick="accAll(true)">全部展开</button>
      <button onclick="accAll(false)">全部收起</button>
    </div>
    <div class="card acc open">
      <button class="acc-head" onclick="toggleAcc(this)">
        <span class="caret">▸</span><span class="title">时间校准</span>
        <span class="meta">宿主时钟漂了会连带把定时问候带到错误钟点</span>
      </button>
      <div class="acc-body"><div id="timeCard"></div></div>
    </div>
    <div class="grid" id="settings"></div>
  </div>

  <!-- ---------------------------------------------------------- 模型 -->
  <div class="panel" id="panel-model">
    <div class="toolbar">
      <button class="primary" onclick="addModelTemplate()">加一个档案（模板）</button>
      <button onclick="testModel('')">测一下当前档案</button>
      <button onclick="load()">刷新</button>
      <button onclick="accAll(true)">全部展开</button>
      <button onclick="accAll(false)">全部收起</button>
    </div>
    <div class="card">
      <h2>接口档案 <span class="count" id="modelCount"></span></h2>
      <div class="muted" style="margin-bottom:12px">
        一个档案 = <b>一家接口 + 一个模型</b>（地址、密钥来源、模型名、能力标记）。
        想换一家（中转站、自建服务、别的厂商的 OpenAI 兼容端点）就在这里加一个，
        点「设为当前」<b>下一条消息就生效，不用重启</b>。
        <b>能力标记不是装饰</b>：端点不支持 tools 却收到工具表会直接报错，
        所以「工具 / 读图 / logprobs」要按实际情况勾。
      </div>
      <div id="modelList"></div>
    </div>
    <div class="card acc" data-acc="model:json">
      <button class="acc-head" onclick="toggleAcc(this)">
        <span class="caret">▸</span><span class="title">直接编辑档案 JSON</span>
        <span class="meta">细调 / 批量改；密钥只显示 ***，保存时原样保留</span>
      </button>
      <div class="acc-body">
        <div class="muted" style="margin-bottom:8px">
          字段：<code>id</code>（只能用字母数字与 <code>_ . -</code>）、<code>label</code>、
          <code>base_url</code>、<code>api_key</code>（<b>不建议</b>直接写在这里）、
          <code>api_key_env</code>（推荐：写 <code>.env</code> 里的变量名）、
          <code>model</code>、<code>vision</code> / <code>tools</code> / <code>logprobs</code>。
          本机端点（127.0.0.1 / localhost）留空密钥即可；<code>active</code> 是当前选中的档案 id。
          <code>deepseek</code> 这个播种档案删不掉 —— 它是「回到 .env 默认」唯一的路。
        </div>
        <textarea id="modelJson" spellcheck="false"
          style="width:100%;height:260px;font-family:ui-monospace,Consolas,monospace;font-size:12px;
                 background:#0d1117;color:#d7e2ee;border:1px solid var(--line);border-radius:8px;padding:10px"></textarea>
        <div style="margin-top:8px">
          <button class="primary" onclick="saveModelJson()">保存这份 JSON</button>
          <button onclick="load()">放弃改动并重载</button>
        </div>
        <div id="modelJsonMsg" class="muted" style="margin-top:6px"></div>
      </div>
    </div>
  </div>

  <!-- ---------------------------------------------------------- 人格三层 -->
  <div class="panel" id="panel-persona">
    <div class="card">
      <h2>人格三层 <span class="count" id="personaCount"></span></h2>
      <div class="muted" style="margin-bottom:12px">
        人格现在分三层，由<b>文件</b>划分权限：
        <b>底层人设</b>（它是谁）与<b>禁止事项</b>（铁律）
        <b>只能由你直接编辑文件修改</b> —— 聊天指令、控制台、自动迭代都改不了它们。
        <b>表层人设</b>（怎么说话）是唯一会自动迭代的部分，
        而且与上面两层冲突的条目会被<b>直接丢弃、不写入</b>。
        改完三个文件<b>不用重启</b>。
      </div>
      <div id="personaList"></div>
    </div>
  </div>

  <!-- ---------------------------------------------------------- 记忆库 -->
  <div class="panel" id="panel-memory">
    <div class="toolbar">
      <button class="primary" onclick="memAdd()">手动记一条</button>
      <button onclick="load()">刷新</button>
    </div>
    <div class="card">
      <h2>人物画像 <span class="count" id="memProfileCount"></span></h2>
      <div id="memProfile"></div>
    </div>
    <div class="card">
      <h2>记住的事 <span class="count" id="memFactCount"></span></h2>
      <div id="memFacts"></div>
    </div>
    <div class="card">
      <h2>群里发生过的 <span class="count" id="memEventCount"></span></h2>
      <div id="memEvents"></div>
    </div>
  </div>

  <!-- ---------------------------------------------------------- 图片策略 -->
  <div class="panel" id="panel-image">
    <div class="toolbar">
      <button class="primary" onclick="imgGlobal()">设为全局「不再保存」</button>
      <button onclick="imgGlobalReset()">撤销全局设置</button>
      <button onclick="load()">刷新</button>
    </div>
    <div class="card">
      <h2>会话图片策略 <span class="count" id="imgGlobalLabel"></span></h2>
      <div class="muted" style="margin-bottom:12px">
        跟聊天里的 <code>/图 忽略</code> 是一回事。设成「不保存」之后这个会话的图片
        不再进表情包库，但仍然可以看图对话。
      </div>
      <div id="imgList"></div>
    </div>
  </div>

  <!-- ---------------------------------------------------------- 表情包库 -->
  <div class="panel" id="panel-sticker">
    <div class="card">
      <h2>表情包库 <span class="count" id="stickerCount"></span></h2>
      <div class="gallery" id="gallery"></div>
    </div>
  </div>

  <div id="toast"></div>
<script>
const PREFIX = "__PREFIX__";
let GROUPS = [];
let DATA = {};

function toast(msg, isErr) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  el.classList.toggle('err', !!isErr);
  setTimeout(() => el.classList.remove('show'), 2200);
}

async function api(path, opts) {
  const r = await fetch(PREFIX + path, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
  if (!r.ok) throw new Error(await r.text());
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.text();
}

function tab(name) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + name));
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

// ---------------------------------------------------------------- 参数
function renderSettings(groups) {
  GROUPS = groups;
  const box = document.getElementById('settings');
  box.innerHTML = groups.map(g => `
    <div class="card acc" data-acc="${esc(g.group)}">
      <button class="acc-head" onclick="toggleAcc(this)">
        <span class="caret">▸</span><span class="title">${esc(g.group)}</span>
        <span class="meta">${g.items.length} 项</span>
      </button>
      <div class="acc-body">${g.items.map(it => row(it)).join('')}</div>
    </div>`).join('');
  applyAccState();
}

// 折叠状态记在浏览器本地（localStorage）：下次打开还是上次的样子。
// 只影响这一页的显示 —— **接口与载荷完全不变**（/api/settings 的 key-value 形状照旧），
// 所以控制台的任何调用方都不受影响。
const ACC_KEY = 'qqbot.acc.v1';
function accState() {
  try { return JSON.parse(localStorage.getItem(ACC_KEY) || '{}'); } catch (e) { return {}; }
}
function toggleAcc(btn) {
  const card = btn.closest('.card');
  card.classList.toggle('open');
  const name = card.dataset.acc;
  if (name) {
    const st = accState();
    st[name] = card.classList.contains('open');
    localStorage.setItem(ACC_KEY, JSON.stringify(st));
  }
}
function applyAccState() {
  const st = accState();
  // 只挑带 data-acc 的卡片：没有 key 的（如"时间校准"）状态不持久化，
  // 保持它出生时的样子。选择器不限定 #settings —— 模型页的卡片也是同一套。
  document.querySelectorAll('.card.acc[data-acc]').forEach(c => {
    if (c.dataset.acc) c.classList.toggle('open', !!st[c.dataset.acc]);
  });
}
function accAll(open) {
  const st = accState();
  document.querySelectorAll('.card.acc[data-acc]').forEach(c => {
    c.classList.toggle('open', open);
    if (c.dataset.acc) st[c.dataset.acc] = open;
  });
  localStorage.setItem(ACC_KEY, JSON.stringify(st));
}

function row(it) {
  const id = 'p_' + it.key;
  let ctl;
  if (it.kind === 'bool') {
    ctl = `<input type="checkbox" id="${id}" ${it.value ? 'checked' : ''} onchange="save('${it.key}', this.checked)">`;
  } else if (it.kind === 'str' && it.choices && it.choices.length) {
    const opts = it.choices.map(c =>
      `<option value="${c}"${c === it.value ? ' selected' : ''}>${c}</option>`).join('');
    ctl = `<select id="${id}" onchange="save('${it.key}', this.value)">${opts}</select>`;
  } else if (it.kind === 'str') {
    ctl = `<input type="text" id="${id}" value="${esc(it.value)}" onchange="save('${it.key}', this.value)">`;
  } else {
    const step = it.kind === 'float' ? '0.01' : '1';
    const mn = (it.min !== null && it.min !== undefined) ? `min="${it.min}"` : '';
    const mx = (it.max !== null && it.max !== undefined) ? `max="${it.max}"` : '';
    ctl = `<input type="number" id="${id}" step="${step}" ${mn} ${mx} value="${it.value}" onchange="save('${it.key}', this.value)">`;
  }
  const hint = it.hint ? `<span class="hint">${esc(it.hint)}</span>` : '';
  return `<div class="row"><label for="${id}">${esc(it.label)}${hint}</label>${ctl}</div>`;
}

async function save(key, value) {
  try {
    const r = await api('/api/settings', {method:'POST', body: JSON.stringify({[key]: value})});
    if (r && r.error) toast('保存失败：' + r.error, true); else toast('已保存');
  } catch (e) { toast('保存失败：' + e.message, true); }
}

async function resetAll() {
  if (!confirm('把所有参数恢复成 .env 里的默认值？（人设要求与记忆不受影响）')) return;
  await api('/api/settings/reset', {method:'POST'});
  toast('已恢复默认'); load();
}

async function speak() {
  try {
    const r = await api('/api/speak', {method:'POST', body: '{}'});
    toast(r.said ? '说了一句，去群里看看' : ('没有发言：' + (r.reason || '未知')));
  } catch (e) { toast('触发失败：' + e.message, true); }
}

async function greet() {
  try {
    const r = await api('/api/greet', {method:'POST', body: '{}'});
    toast(r.said ? ('已发出「' + (r.label || '') + '」，去私聊 / 群里看看')
                 : ('没发出去：' + (r.reason || '看机器人日志')));
  } catch (e) { toast('触发失败：' + e.message, true); }
}

// ---------------------------------------------------------------- 人格三层
// 这一页从「改人设槽位」改成**只读的三层视图 + 自动改动日志**。
// 页面上不再有任何"改人设"的输入框 —— 改人格只能直接编辑文件（见 README 5.6.7.2）。
function renderPersona(p) {
  const st = p.stats || {}, iter = p.iter || {};
  const box = document.getElementById('personaList');
  document.getElementById('personaCount').textContent =
    `（底色 ${st.base_chars || 0} 字 / 铁律 ${(p.forbidden || []).length} 条 / 表层 ${st.surface_chars || 0} 字）`;

  const rows = [
    ['底层人设', st.base_chars, st.base_file, '只有你能改'],
    ['禁止事项', `${st.forbidden_chars} 字 · ${(p.forbidden || []).length} 条`, st.forbidden_file, '只有你能改'],
    ['表层人设', st.surface_chars, st.surface_file, '**自动迭代只写这一层**'],
  ];
  let html = `<div class="kv">
    ${rows.map(r => `<div><b>${r[0]}</b>：${r[1]} 字 <span class="muted">（${r[3]}）</span>
      <br><code class="muted">${esc(r[2] || '')}</code></div>`).join('')}
  </div>
  <div class="muted" style="margin:8px 0">
    改人格直接编辑上面三个文件，<b>改完不用重启</b>（每轮回复前重新读）。
    自动迭代每 ${iter.interval || 0} 秒跑一次，最多写入 ${iter.max || 0} 条；
    与上面两层冲突的条目会被<b>直接丢弃</b>。
  </div>
  <div style="margin:8px 0">
    <button onclick="piUndo()">撤回最近一次自动改动</button>
    <button onclick="piReflect()">立刻反思一次</button>
  </div>`;

  // ---- 人设评估台：论文那套「对比素材 + 0-100 打分」 ----
  // 开关本身在「参数 → 人设评估」组里（那是 settings._SPECS 自动渲染的），
  // 这里只放两个**显式动作**：评估要花 token，花不花由主人点。
  const ev = p.eval || {};
  const evAll = (ev.traits || []).length;
  html += `<h3 style="margin:18px 0 6px">人设评估台
    <span class="muted">${ev.enabled ? '已启用' : '未启用 —— 去「参数 → 人设评估」打开'}</span></h3>
    <div class="muted" style="margin:4px 0">
      裁判 ${esc(ev.judge_model || '')}　每题采样 ${ev.rollouts || 0} 次　每特质 ${ev.questions || 0} 题<br>
      素材 ${ev.with_artifacts || 0} / ${evAll}　基线 ${ev.with_baseline || 0} / ${evAll}
      ${ev.last_run ? '　最近一轮 ' + esc(ev.last_run) : ''}
    </div>
    <div style="margin:8px 0">
      <button onclick="piEvalArtifacts()">生成测评素材</button>
      <button onclick="piEvalRound()">跑一轮基线分</button>
    </div>`;
  const scored = (ev.traits || []).filter(x => x.score !== null && x.score !== undefined);
  if (scored.length) {
    scored.sort((a, b) => (b.score || 0) - (a.score || 0));
    html += `<table class="items"><tr><th>特质</th><th>分数</th><th>题数</th><th>时间</th></tr>` +
      scored.map(x => {
        let d = '';
        if (x.prev !== null && x.prev !== undefined) {
          const dd = Math.round((x.score - x.prev) * 10) / 10;
          if (Math.abs(dd) >= 3) d = dd > 0 ? ` <b style="color:#c00">↑${dd}</b>`
                                            : ` <b style="color:#080">↓${dd}</b>`;
        }
        return `<tr><td>${esc(x.trait || x.slug)}</td><td>${x.score}${d}</td>
          <td class="muted">${x.questions || 0}</td>
          <td class="muted">${esc((x.at || '').slice(0, 16))}</td></tr>`;
      }).join('') + `</table>`;
  } else {
    html += '<div class="empty">还没有基线分：先「生成测评素材」，再「跑一轮基线分」。</div>';
  }

  // 自动改动日志：**被拦下的也列出来** —— 那是判断闸门松紧的唯一依据
  const log = (p.changelog || []).slice().reverse();
  const mark = {added: '✅ 写入', rejected: '⛔ 丢弃', undone: '↩️ 撤回'};
  if (!log.length) {
    html += '<div class="empty">还没有任何自动改动。</div>';
  } else {
    html += `<table class="items"><tr><th>动作</th><th>内容</th><th>时间 / 理由</th></tr>` +
      log.slice(0, 20).map(x => `<tr>
        <td>${mark[x.action] || esc(x.action)}</td>
        <td>${esc((x.text || '').slice(0, 70))}</td>
        <td class="muted">${esc((x.at || '').slice(0, 16))}${x.reason ? '<br>' + esc(x.reason.slice(0, 60)) : ''}</td>
      </tr>`).join('') + `</table>`;
  }

  if ((p.forbidden || []).length) {
    html += `<h4>禁止事项（自动迭代撞不过去的那几条）</h4><ul class="muted">` +
      p.forbidden.slice(0, 20).map(f => `<li>${esc(f.slice(0, 80))}</li>`).join('') + `</ul>`;
  }
  box.innerHTML = html;
}

async function piUndo() {
  try {
    const r = await api('/api/persona/undo', {method:'POST', body: '{}'});
    toast(r.ok ? ('已撤回：' + (r.note || '')) : ('没撤回什么：' + (r.note || '')), !r.ok);
    load();
  } catch (e) { toast('撤回失败：' + e.message, true); }
}

async function piReflect() {
  toast('反思中，可能要十几秒…');
  try {
    const r = await api('/api/persona/reflect', {method:'POST', body: '{}'});
    if (!r.ok) { toast('没跑成：' + (r.why || ''), true); return; }
    toast(`候选 ${r.candidates} 条 → 写入 ${r.written}、拦下 ${r.rejected}`);
    load();
  } catch (e) { toast('反思失败：' + e.message, true); }
}

// 评估台的两个显式动作。**先问一句再花 token** —— 这两个按钮是真花钱的。
async function piEvalArtifacts() {
  if (!confirm('给还没有素材的特质各生成一次测评素材（每个特质一次模型调用）。继续？')) return;
  toast('生成中，每个特质一次调用，可能要一分钟…');
  try {
    const r = await api('/api/persona/eval', {method:'POST', body: JSON.stringify({mode:'artifacts'})});
    if (!r.ok) { toast('没跑成：' + (r.why || ''), true); return; }
    toast(`新生成 ${(r.generated||[]).length} 个，跳过 ${(r.skipped||[]).length} 个，失败 ${(r.failed||[]).length} 个`);
    load();
  } catch (e) { toast('生成失败：' + e.message, true); }
}

async function piEvalRound() {
  if (!confirm('跑一轮基线分：特质数 × 题数 × 采样次数 × 2 次调用，比较慢也比较费。继续？')) return;
  toast('跑分中，可能要几分钟…');
  try {
    const r = await api('/api/persona/eval', {method:'POST', body: JSON.stringify({mode:'round'})});
    if (!r.ok) { toast('没跑成：' + (r.why || ''), true); return; }
    toast(`跑完 ${r.traits} 个特质`);
    load();
  } catch (e) { toast('跑分失败：' + e.message, true); }
}

// ---------------------------------------------------------------- 记忆库
function renderMemory(mem) {
  const sf = mem.facts || [], se = mem.events || [], sp = mem.profile || [];
  document.getElementById('memProfileCount').textContent = `（${sp.length} 人）`;
  document.getElementById('memFactCount').textContent = `（${sf.length} 条）`;
  document.getElementById('memEventCount').textContent = `（${se.length} 条）`;

  document.getElementById('memProfile').innerHTML = sp.length
    ? `<table class="items"><tr><th>谁</th><th>喜欢</th><th>不喜欢</th><th>习惯</th></tr>` +
      sp.map(p => `<tr>
        <td>${esc(p.display || p.key)}</td>
        <td>${(p.love||[]).map(x=>`<span class="tag">${esc(x)}</span>`).join('') || '<span class="muted">—</span>'}</td>
        <td>${(p.dislike||[]).map(x=>`<span class="tag">${esc(x)}</span>`).join('') || '<span class="muted">—</span>'}</td>
        <td>${(p.habit||[]).map(x=>`<span class="tag">${esc(x)}</span>`).join('') || '<span class="muted">—</span>'}</td>
      </tr>`).join('') + `</table>`
    : '<div class="empty">还没有人物画像。聊得多一些，或者勾上 memory_extract。</div>';

  document.getElementById('memFacts').innerHTML = sf.length
    ? `<table class="items"><tr><th>#</th><th>内容</th><th>重要度</th><th>来源</th><th class="act"></th></tr>` +
      sf.map(f => `<tr>
        <td class="muted">${f.id}</td>
        <td>${esc(f.text)}${f.protected ? ' <span class="tag lock">保护</span>' : ''}
            ${f.prev_text ? `<div class="muted">改口前：${esc(f.prev_text)}</div>` : ''}</td>
        <td>${(f.importance||0).toFixed ? f.importance.toFixed(2) : f.importance}</td>
        <td class="muted">${esc(f.time || '')}<br>${esc(f.source || '')}</td>
        <td class="act">
          <button class="mini" onclick="memLock(${f.id}, ${f.protected ? 'false' : 'true'})">${f.protected ? '解锁' : '保护'}</button>
          <button class="mini danger" onclick="memDel(${f.id})">删</button>
        </td>
      </tr>`).join('') + `</table>`
    : '<div class="empty">还没有记住的事。</div>';

  document.getElementById('memEvents').innerHTML = se.length
    ? `<table class="items"><tr><th>时间</th><th>会话</th><th>内容</th><th class="act"></th></tr>` +
      se.map(e => `<tr>
        <td class="muted">${esc(e.time || '')}</td><td class="muted">${esc(e.conv || '')}</td>
        <td>${esc(e.text)}</td>
        <td class="act"><button class="mini danger" onclick="memDel(${e.id})">删</button></td>
      </tr>`).join('') + `</table>`
    : '<div class="empty">还没有群事件。</div>';
}

async function memAdd() {
  const text = prompt('要让它记住什么？（一句话）');
  if (!text) return;
  try {
    await api('/api/memory', {method:'POST', body: JSON.stringify({text})});
    toast('已记下'); load();
  } catch (e) { toast('失败：' + e.message, true); }
}

async function memDel(id) {
  if (!confirm('删掉这条记忆？')) return;
  await api('/api/memory/' + id, {method:'DELETE'});
  toast('已删除'); load();
}

async function memLock(id, locked) {
  await api('/api/memory/' + id + '/protect', {method:'POST', body: JSON.stringify({locked})});
  toast(locked ? '已保护，不会被自动淘汰' : '已取消保护'); load();
}

// ---------------------------------------------------------------- 图片策略
function renderImage(im) {
  const g = im.global_mode || 'normal';
  document.getElementById('imgGlobalLabel').textContent =
    g === 'normal' ? '（全局：正常）' : `（全局：${g}）`;
  const rows = Object.entries(im.convs || {});
  document.getElementById('imgList').innerHTML = rows.length
    ? `<table class="items"><tr><th>会话</th><th>生效策略</th><th>单独设置</th><th>点名不存</th><th class="act"></th></tr>` +
      rows.map(([conv, v]) => `<tr>
        <td>${esc(conv)}</td>
        <td>${esc(v.label || v.effective)}</td>
        <td class="muted">${esc(v.mode || '—')}</td>
        <td class="muted">${v.ignored || 0} 张</td>
        <td class="act">
          <button class="mini" onclick="imgSet('${conv}','ignore')">不保存</button>
          <button class="mini" onclick="imgSet('${conv}','normal')">恢复</button>
          <button class="mini danger" onclick="imgSet('${conv}','')">清除</button>
        </td>
      </tr>`).join('') + `</table>`
    : '<div class="empty">所有会话都是默认策略（会看图，符合条件就收进表情库）。</div>';
}

async function imgSet(conv, mode) {
  await api('/api/image-policy', {method:'POST', body: JSON.stringify({conv, mode})});
  toast('已更新'); load();
}

async function imgGlobal() {
  if (!confirm('把所有会话设成「不再保存图片」？')) return;
  await api('/api/image-policy', {method:'POST', body: JSON.stringify({global_mode:'ignore'})});
  toast('已设为全局不保存'); load();
}

async function imgGlobalReset() {
  await api('/api/image-policy', {method:'POST', body: JSON.stringify({global_mode:'normal'})});
  toast('已撤销全局设置'); load();
}

// ---------------------------------------------------------------- 表情包
function renderGallery(items) {
  document.getElementById('stickerCount').textContent = items.length ? `（${items.length} 张）` : '';
  const box = document.getElementById('gallery');
  if (!items.length) {
    box.innerHTML = '<div class="empty">还没有收藏到表情包。等群里有人发图，或者先把 sticker_min_score 调低试试。</div>';
    return;
  }
  box.innerHTML = items.map(it => {
    // 以文件形式发来的图打个标：它们被降权过，用途上偏"素材"而不是"表情"
    const tags = [];
    if (it.file_sent) tags.push('<span class="tag">文件</span>');
    if (!it.phash) tags.push('<span class="tag">无感知哈希</span>');
    const dims = it.width && it.height ? `${it.width}×${it.height}` : '';
    return `
    <div class="thumb" title="${esc(it.reason || '')}">
      <img src="${PREFIX}/api/stickers/${it.hash}" loading="lazy" alt="">
      <button class="del" onclick="del('${it.hash}')">×</button>
      <div class="meta">${it.score} · 用${it.uses}次${it.seen ? ' · 见' + it.seen + '次' : ''}<br>
        ${esc(it.sub_type_label)}${dims ? ' · ' + dims : ''}<br>${tags.join('')}</div>
    </div>`;
  }).join('');
}

async function del(hash) {
  if (!confirm('删除这张表情包？')) return;
  await api('/api/stickers/' + hash, {method:'DELETE'});
  toast('已删除'); load();
}

function renderTime(t) {
  // 宿主时钟漂了、或 NTP 没通，这里是唯一能一眼看见的地方
  const box = document.getElementById('timeCard');
  if (!box || !t) return;
  const label = {
    ok: t.offset_seconds ? '已校准' : '已校准（偏差近 0）',
    pending: '还没同步过',
    failed: '同步失败（用系统时钟）',
    disabled: '已关闭',
  }[t.status] || t.status;
  const off = t.offset_seconds
    ? `<b style="color:var(--warn)">系统时钟${t.offset_seconds > 0 ? '快' : '慢'} ${Math.abs(t.offset_seconds).toFixed(3)} 秒</b>`
    : '<span class="muted">系统时钟与标准时间一致</span>';
  box.innerHTML = `
    <div class="row"><label>当前时间（校准后）</label><span>${esc(t.now)}</span></div>
    <div class="row"><label>系统时钟</label><span>${esc(t.raw)}</span></div>
    <div class="row"><label>偏差</label><span>${off}</span></div>
    <div class="row"><label>状态</label><span>${esc(label)}${t.stale ? ' <span class="tag">可能过期</span>' : ''}</span></div>
    ${t.server ? `<div class="row"><label>来源</label><span class="muted">${esc(t.server)} · stratum ${t.stratum} · ${t.delay_ms} ms · ${esc(t.synced_at)}</span></div>` : ''}
    ${t.last_error ? `<div class="row"><label>最近失败</label><span class="muted">${esc(t.last_error)}</span></div>` : ''}`;
}

// ---------------------------------------------------------------- 模型档案
// 「哪家接口 + 哪个模型」现在是一份可切换的档案表。这一页只做四件事：
// 看现状、切当前、测通不通、改 JSON —— 所有写操作都打服务端接口，
// **页面不自己推导任何"生效的模型名"**（那由 settings.model 覆盖 + 档案一起决定）。
function renderModels(m) {
  const box = document.getElementById('modelList');
  if (!box || !m) return;
  document.getElementById('modelCount').textContent = `（${m.profiles.length} 个，当前用 ${m.active}）`;
  box.innerHTML = m.profiles.map(p => {
    const cur = p.id === m.active;
    const tags = [['读图', p.vision], ['工具', p.tools], ['logprobs', p.logprobs]]
      .filter(t => t[1]).map(t => `<span class="tag">${t[0]}</span>`).join(' ');
    const key = p.has_key
      ? `<span class="tag">${esc(p.key_hint)}</span>`
      : '<span class="tag" style="color:var(--warn)">没配密钥</span>';
    const ov = (cur && m.override)
      ? ` <span class="tag">被 settings.model 覆盖成 ${esc(m.override)}</span>` : '';
    return `<div class="card acc" data-acc="model:${esc(p.id)}">
      <button class="acc-head" onclick="toggleAcc(this)">
        <span class="caret">▸</span>
        <span class="title">${cur ? '● ' : ''}${esc(p.label || p.id)}</span>
        <span class="meta">${esc(p.effective_model)} · ${esc(p.base_url)}</span>
      </button>
      <div class="acc-body">
        <div class="row"><label>档案 id</label><span><code>${esc(p.id)}</code></span></div>
        <div class="row"><label>接口地址</label><span><code>${esc(p.base_url)}</code></span></div>
        <div class="row"><label>模型名</label><span><b>${esc(p.effective_model)}</b>${ov}
          <span class="muted">（档案里写的是 ${esc(p.model)}）</span></span></div>
        <div class="row"><label>密钥</label><span>${key}
          <span class="muted">${p.key_env ? '来自 .env 的 ' + esc(p.key_env) : '直接写在档案里'}</span></span></div>
        <div class="row"><label>能力标记</label><span>${tags || '<span class="muted">（都没开：只发纯文本对话）</span>'}</span></div>
        <div class="row"><label>操作</label><span>
          ${cur ? '<span class="tag">当前在用</span>'
                : `<button onclick="useModel('${p.id}')">设为当前</button>`}
          <button onclick="testModel('${p.id}')">测一下</button>
          ${p.removable ? `<button onclick="delModel('${p.id}')">删除</button>` : ''}
        </span><span class="muted" id="mt_${p.id}"></span></div>
      </div>
    </div>`;
  }).join('');
  applyAccState();
}

function renderModelJson(text) {
  const el = document.getElementById('modelJson');
  // 正在编辑时不要覆盖用户敲了一半的内容（刷新按钮会重载，但别在打字时抢）
  if (el && document.activeElement !== el) el.value = text || '';
}

async function useModel(id) {
  try {
    const r = await api('/api/model/active', {method:'POST', body: JSON.stringify({id})});
    toast(r.ok ? ('已切到 ' + id) : ('没切成：' + (r.detail || '')), !r.ok);
    if (r.ok) load();
  } catch (e) { toast('切换失败：' + e.message, true); }
}

async function testModel(id) {
  const box = document.getElementById(id ? ('mt_' + id) : 'modelJsonMsg');
  if (box) box.textContent = '测试中…（真连一次接口，几秒）';
  try {
    const r = await api('/api/model/test', {method:'POST', body: JSON.stringify({id})});
    const line = (r.ok ? '✅ ' : '❌ ') + (r.detail || '')
      + (r.models && r.models.length ? `：${r.models.slice(0, 12).join(' / ')}` : '');
    if (box) box.textContent = line;
    toast(r.ok ? '接口通了' : '接口不通', !r.ok);
  } catch (e) {
    if (box) box.textContent = '❌ ' + e.message;
    toast('测试失败：' + e.message, true);
  }
}

async function delModel(id) {
  if (!confirm('删掉档案「' + id + '」？它的接口地址、密钥来源与模型名一起消失。')) return;
  try {
    const r = await api('/api/model/delete', {method:'POST', body: JSON.stringify({id})});
    toast(r.ok ? ('已删除 ' + id) : ('没删成：' + (r.detail || '')), !r.ok);
    if (r.ok) load();
  } catch (e) { toast('删除失败：' + e.message, true); }
}

function addModelTemplate() {
  const el = document.getElementById('modelJson');
  let data;
  try { data = JSON.parse(el.value); } catch (e) { toast('先修好 JSON 语法再加：' + e.message, true); return; }
  if (!data || !Array.isArray(data.profiles)) { toast('JSON 里没有 profiles 数组', true); return; }
  data.profiles.push({
    id: 'newapi', label: '新接口', base_url: 'https://example.com/v1',
    api_key: '', api_key_env: 'NEWAPI_API_KEY', model: '填对方的模型名',
    vision: false, tools: false, logprobs: false,
  });
  el.value = JSON.stringify(data, null, 2);
  const box = document.querySelector('.card.acc[data-acc="model:json"]');
  if (box) box.classList.add('open');
  toast('已插入模板，改完点「保存这份 JSON」');
}

async function saveModelJson() {
  const el = document.getElementById('modelJson');
  const msg = document.getElementById('modelJsonMsg');
  try {
    const r = await api('/api/model/save', {method:'POST', body: JSON.stringify({json: el.value})});
    if (msg) msg.textContent = (r.ok ? '已保存，当前档案：' : '没保存：') + (r.detail || '');
    toast(r.ok ? '已保存' : '没保存', !r.ok);
    if (r.ok) load();
  } catch (e) { toast('保存失败：' + e.message, true); }
}

// ---------------------------------------------------------------- 装载
async function load() {
  const data = await api('/api/state');
  DATA = data;
  renderSettings(data.settings);
  renderModels(data.models);
  renderModelJson(data.models_editor);
  renderPersona(data.persona);
  renderMemory(data.memory);
  renderImage(data.image);
  renderTime(data.status.time);
  renderGallery(data.stickers_list);

  const st = data.status;
  const gr = st.greet || {sent:{}, today:''};
  const greeted = Object.values(gr.sent).filter(d => d === gr.today).length;
  const mem = data.memory.facts.length;
  // 头部徽章原来报"人设要求 N 条"（槽位机制已删）。现在报**自动改动**的条数 ——
  // 那是这一页现在唯一会变的东西。
  const pChanges = ((data.persona || {}).changelog || []).length;
  const sk = st.stickers;
  const dupBadge = sk.duplicates
    ? `<span class="badge" style="border-color:#5c4415;color:var(--warn)">疑似重复 <b>${sk.duplicates}</b> 组</span>`
    : '';
  // 搜索开着但没配端点，是最常见的"看起来没用"的原因 —— 直接摆在徽章上
  const se = st.search || {};
  const searchBadge = se.available
    ? `<span class="badge" style="color:var(--ok)">联网搜索 <b>可用</b>（${esc(se.backend)}）</span>`
    : `<span class="badge" style="color:var(--dim)" title="${esc(se.reason || '')}">联网搜索 <b>${se.enabled ? '未配端点' : '关'}</b></span>`;
  document.getElementById('badges').innerHTML = `
    <span class="badge">表情包 <b>${sk.count}</b> 张</span>
    <span class="badge">占用 <b>${(sk.total_bytes/1024/1024).toFixed(2)}</b> MB</span>
    <span class="badge">已使用 <b>${sk.used}</b> 次</span>
    ${dupBadge}
    <span class="badge">长期记忆 <b>${mem}</b> 条</span>
    <span class="badge">人格自动改动 <b>${pChanges}</b> 条</span>
    ${searchBadge}
    <span class="badge">群 <b>${st.groups.length}</b> 个</span>
    <span class="badge">主动发言 <b>${st.proactive_enabled ? '开' : '关'}</b></span>
    <span class="badge">定时问候 <b>${gr.enabled ? '开' : '关'}</b>／今日 <b>${greeted}</b></span>
    <span class="badge">模型 <b>${esc(st.model)}</b> <span class="muted">@ ${esc(st.model_profile || '')}</span></span>`;
}

load().catch(e => toast('加载失败：' + e.message, true));
</script>
</body>
</html>
"""


def _register() -> bool:
    driver = get_driver()
    app = getattr(driver, "server_app", None)
    if app is None:
        logger.warning(
            "当前 driver 没有 server_app，Web 控制台未启用。"
            "把 .env 的 DRIVER 改成 ~fastapi+~websockets 后重启即可。"
        )
        return False

    try:
        from fastapi import Request
        from fastapi.responses import HTMLResponse, JSONResponse, Response
    except ImportError:
        logger.warning("未安装 fastapi，Web 控制台未启用（pip install fastapi uvicorn）")
        return False

    prefix = config.WEBUI_PREFIX.rstrip("/")

    async def _body(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 空 body 也允许
            return {}
        return body if isinstance(body, dict) else {}

    @app.get(prefix + "/", response_class=HTMLResponse)
    async def _index() -> HTMLResponse:  # noqa: ANN202
        return HTMLResponse(_HTML.replace("__PREFIX__", prefix))

    @app.get(prefix + "/api/state")
    async def _state():  # noqa: ANN202
        lib = await stickers.get_library()
        return JSONResponse(
            {
                "settings": settings.describe(),
                # 模型档案：哪家接口 + 哪个模型（**只给掩码，不给密钥明文**）
                "models": llm.snapshot(),
                # 编辑框那份文本是单独一份（api_key 显示成 ***，保存时原样保留）
                "models_editor": llm.editor_text(),
                # 人设：三层结构。`catalog()`（旧槽位目录）已随分层一起删除 ——
                # 控制台上的人设页现在只展示三层 + 自动改动日志 + 手动触发反思。
                "persona": {
                    "stats": persona.stats(),
                    "forbidden": persona.forbidden_items(),
                    "layers": persona.layers(),
                    "changelog": persona.changelog(30),
                    "iter": persona_iter.stats(),
                    # 评估台现状：开关、素材/基线覆盖、各特质分数（只读）
                    "eval": persona_eval.status(),
                },
                "memory": {
                    "facts": memory.all_facts(),
                    "events": memory.all_events(),
                    "profile": [
                        dict(info, key=key) for key, info in sorted(memory._db.profile.items())  # noqa: SLF001
                    ],
                    "stats": memory.stats(),
                },
                "image": state.snapshot(),
                "status": {
                    "stickers": lib.stats(),
                    "groups": [f"g{g}" for g in await proactive.known_groups()],
                    "proactive_enabled": bool(settings.get("proactive_enabled")),
                    "proactive": proactive.status(),
                    "greet": greetings.status(),
                    "model": llm.model_name(),
                    "model_profile": llm.active_id(),
                    # 时间状态：宿主时钟漂了、或 NTP 没通，控制台上这里是唯一能看见的地方
                    "time": {
                        "now": clock.strftime("%Y-%m-%d %H:%M:%S"),
                        "raw": time.strftime("%Y-%m-%d %H:%M:%S"),
                        **clock.status(),
                    },
                    # 联网搜索状态：开着但没配端点是最常见的"看起来没用"的原因
                    "search": search.stats(),
                },
                "stickers_list": sorted(
                    lib.items, key=lambda x: float(x.get("ts", 0)), reverse=True
                ),
            }
        )

    # ---- 模型档案：看 / 切 / 测 / 整份改。接口是**新增的**，旧的 /api/settings 形状不变 ----
    @app.post(prefix + "/api/model/active")
    async def _model_active(request: Request):  # noqa: ANN202
        body = await _body(request)
        pid = str(body.get("id") or "").strip()
        had = str(settings.get("model") or "").strip()
        ok, why = llm.set_active(pid)
        note = f"当前用 {why}" + (f"（顺带清掉了模型名覆盖「{had}」）" if ok and had else "")
        return JSONResponse({"ok": ok, "detail": why if not ok else note, "state": llm.snapshot()})

    @app.post(prefix + "/api/model/test")
    async def _model_test(request: Request):  # noqa: ANN202
        body = await _body(request)
        pid = str(body.get("id") or "").strip()
        prof = llm.get(pid) if pid else None
        if pid and prof is None:
            return JSONResponse({"ok": False, "detail": f"没有这个档案：{pid}", "models": []})
        try:
            got = await llm.probe(prof)
        except Exception as exc:  # noqa: BLE001 - 探测本身不该把控制台打成 500
            got = {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "models": []}
        got["profile"] = (prof or llm.active())["id"]
        return JSONResponse(got)

    @app.post(prefix + "/api/model/save")
    async def _model_save(request: Request):  # noqa: ANN202
        body = await _body(request)
        try:
            data = json.loads(str(body.get("json") or ""))
        except ValueError as exc:
            return JSONResponse({"ok": False, "detail": f"JSON 解析失败：{exc}"})
        if not isinstance(data, dict):
            return JSONResponse({"ok": False, "detail": "顶层必须是一个对象"})
        ok, why = llm.replace_all(data)
        return JSONResponse({"ok": ok, "detail": why if not ok else f"当前用 {why}",
                             "state": llm.snapshot() if ok else None})

    @app.post(prefix + "/api/model/delete")
    async def _model_delete(request: Request):  # noqa: ANN202
        body = await _body(request)
        ok, why = llm.del_profile(str(body.get("id") or "").strip())
        return JSONResponse({"ok": ok, "detail": why, "state": llm.snapshot() if ok else None})

    @app.post(prefix + "/api/settings")
    async def _update(request: Request):  # noqa: ANN202
        # 注意：本文件不能加 `from __future__ import annotations`。
        # 那会把注解变成字符串，FastAPI 就解析不出 Request 依赖，转而当成查询参数，
        # 结果所有 POST 都返回 422（missing query "request"）。这个坑踩过一次。
        body = await _body(request)
        if not body:
            return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
        applied: dict[str, object] = {}
        for key, value in body.items():
            try:
                applied[key] = settings.set_value(key, value)
            except KeyError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "applied": applied})

    @app.post(prefix + "/api/settings/reset")
    async def _reset():  # noqa: ANN202
        settings.reset()
        return JSONResponse({"ok": True})

    # -------------------------------------------------------- 人格（三层，只读 + 收尾）
    # 人格分层之后，控制台上的「改人设」入口**整个删掉**。
    # 为什么连控制台也不留：边界要由代码保证 —— 底层人设与禁止事项"只有用户能改"，
    # 而"用户能改"的准确定义就是**直接编辑文件**（见 README 5.6.7.2）。
    # 控制台保留三件事：看三层、看自动改动日志、**手动触发一次反思**。
    # 下面是替代原来 `/api/persona` 的只读端点，旧路径显式返回 410，
    # 这样"还能不能改"这个问题在 API 层面也有明确答案（而不是 404 让人以为写错了）。
    @app.post(prefix + "/api/persona")
    async def _persona_gone() -> JSONResponse:  # noqa: ANN202
        return JSONResponse(
            {
                "ok": False,
                "error": "改人设的接口已删除：现在只能直接编辑三个文件"
                         "（底层人设 / 禁止事项 / 表层人设），改完不用重启",
            },
            status_code=410,
        )

    @app.post(prefix + "/api/persona/clear")
    async def _persona_clear_gone() -> JSONResponse:  # noqa: ANN202
        return JSONResponse(
            {"ok": False, "error": "撤销人设要求的接口已删除；自动改动用 /人设 撤回"},
            status_code=410,
        )

    @app.post(prefix + "/api/persona/undo")
    async def _persona_undo() -> JSONResponse:  # noqa: ANN202
        """撤回最近一次**自动迭代**写入（唯一保留的写动作，且只动表层）。"""
        got = persona.undo_last()
        return JSONResponse({"ok": bool(got.get("ok")), "note": got.get("why") or got.get("text", "")})

    @app.post(prefix + "/api/persona/reflect")
    async def _persona_reflect() -> JSONResponse:  # noqa: ANN202
        """手动触发一次自我反思（后台也会按 `persona_iter_interval` 定期跑）。"""
        got = await persona_iter.reflect_once(notify=False)
        return JSONResponse(got)

    @app.post(prefix + "/api/persona/eval")
    async def _persona_eval(request: Request) -> JSONResponse:  # noqa: ANN202
        """人设评估台：`mode=artifacts` 生成测评素材；`mode=round` 跑一轮基线分。

        **总闸是 `eval_enabled`**（控制台「参数 → 人设评估」）—— 关着时这里会直接
        返回 `ok=False` 且一次模型都不调。
        为什么不自动跑：评估要花 token，而"要不要花"是主人的决定 ——
        与 `/人设 重跑`（自我反思）同一个取舍。
        """
        body = await _body(request)
        mode = str(body.get("mode") or "round")
        if mode == "artifacts":
            return JSONResponse(await persona_eval.generate_artifacts())
        return JSONResponse(await persona_eval.run_round())

    # -------------------------------------------------------- 记忆
    @app.post(prefix + "/api/memory")
    async def _memory_add(request: Request) -> JSONResponse:  # noqa: ANN202
        body = await _body(request)
        text = str(body.get("text") or "").strip()
        if len(text) < 2:
            return JSONResponse({"ok": False, "error": "内容太短"}, status_code=400)
        item, created = await memory.remember(
            text,
            subject=str(body.get("subject") or "主人"),
            importance=float(body.get("importance") or 0.85),
            source="manual",
        )
        return JSONResponse({"ok": True, "created": created, "id": item.get("id")})

    @app.delete(prefix + "/api/memory/{item_id}")
    async def _memory_delete(item_id: int) -> JSONResponse:  # noqa: ANN202
        return JSONResponse({"ok": await memory.forget(item_id)})

    @app.post(prefix + "/api/memory/{item_id}/protect")
    async def _memory_protect(item_id: int, request: Request) -> JSONResponse:  # noqa: ANN202
        body = await _body(request)
        locked = bool(body.get("locked", True))
        return JSONResponse({"ok": await memory.set_protected(item_id, locked)})

    # -------------------------------------------------------- 图片策略
    @app.post(prefix + "/api/image-policy")
    async def _image_policy(request: Request) -> JSONResponse:  # noqa: ANN202
        body = await _body(request)
        global_mode = str(body.get("global_mode") or "").strip()
        if global_mode:
            if global_mode not in state.VALID_MODES:
                return JSONResponse(
                    {"ok": False, "error": f"可选：{'/'.join(state.VALID_MODES)}"}, status_code=400
                )
            state.set_global_image_policy(global_mode)
            return JSONResponse({"ok": True, "global_mode": global_mode})
        conv = str(body.get("conv") or "").strip()
        if not conv:
            return JSONResponse({"ok": False, "error": "缺少 conv"}, status_code=400)
        mode = str(body.get("mode") or "")
        if mode and mode not in state.VALID_MODES:
            return JSONResponse(
                {"ok": False, "error": f"可选：{'/'.join(state.VALID_MODES)}"}, status_code=400
            )
        return JSONResponse({"ok": True, "effective": state.set_from_web(conv, mode)})

    # -------------------------------------------------------- 表情包
    @app.get(prefix + "/api/stickers")
    async def _stickers():  # noqa: ANN202
        lib = await stickers.get_library()
        return JSONResponse({"items": lib.items, "stats": lib.stats()})

    @app.get(prefix + "/api/stickers/{digest}")
    async def _sticker_file(digest: str):  # noqa: ANN202
        lib = await stickers.get_library()
        item = lib.find(digest)
        if item is None:
            return Response(status_code=404)
        path = lib.path_of(item)
        if not path.exists():
            return Response(status_code=404)
        data = await asyncio.to_thread(path.read_bytes)
        ext = path.suffix.lstrip(".").lower()
        media = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "bmp": "image/bmp",
        }.get(ext, "application/octet-stream")
        return Response(content=data, media_type=media)

    @app.delete(prefix + "/api/stickers/{digest}")
    async def _sticker_delete(digest: str):  # noqa: ANN202
        lib = await stickers.get_library()
        ok = await asyncio.to_thread(lib.remove, digest)
        return JSONResponse({"ok": ok})

    @app.post(prefix + "/api/speak")
    async def _speak():  # noqa: ANN202
        groups = await proactive.known_groups()
        if not groups:
            return JSONResponse({"said": False, "reason": "还没有任何群聊记录"})
        for group_id in groups:
            if await proactive.speak(group_id, "manual", force=True):
                return JSONResponse({"said": True, "group_id": group_id})
        return JSONResponse(
            {"said": False, "reason": "模型选择了不发言、或没有活跃的群（看机器人日志有 [SKIP] 记录）"}
        )

    @app.post(prefix + "/api/greet")
    async def _greet(request: Request) -> JSONResponse:  # noqa: ANN202
        """手动问候一次，用来确认话术与发送通道。

        绕开时间点与总开关，也**不写**"今天已发"状态 —— 手动触发多半是在试效果，
        不该把当天的自动问候吃掉。body 里带 {"slot": "morning|noon|night"} 可指定时段，
        不带（或给了非法值）就按当前钟点推断。
        """
        body = await _body(request)
        wanted = str(body.get("slot") or "")
        slot = wanted if greetings.label_of(wanted) else greetings.current_slot()
        said = await greetings.greet(slot, force=True)
        return JSONResponse(
            {
                "said": said,
                "slot": slot,
                "label": greetings.label_of(slot),
                "reason": "" if said else "生成或发送失败，看机器人日志（也可能是模型选择了 [SKIP]）",
            }
        )

    logger.info("Web 控制台已挂载：http://127.0.0.1:%s%s/", getattr(driver.config, "port", 8080), prefix)
    return True


try:
    _register()
except Exception:  # noqa: BLE001 - UI 挂了也不能影响机器人本体
    logger.exception("注册 Web 控制台失败")
