"""极简 Web 管理面板（单文件 HTML，内嵌 JS）。"""

ADMIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>polygate · 控制台</title>
<style>
:root{--bg:#0f1115;--card:#181b21;--border:#2a2f3a;--fg:#e6e8ec;--muted:#8a93a5;--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg);font-size:14px}
header{padding:14px 22px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:18px;margin:0}
.badge{background:var(--blue);color:#04121f;border-radius:10px;padding:1px 8px;font-size:12px;font-weight:600}
.toolbar{display:flex;gap:8px;align-items:center;margin-left:auto}
input{background:#0a0c0f;border:1px solid var(--border);color:var(--fg);border-radius:6px;padding:5px 8px;font-size:13px;width:150px}
button{background:var(--blue);color:#04121f;border:0;border-radius:6px;padding:5px 12px;font-size:13px;cursor:pointer;font-weight:600}
#status{color:var(--muted);min-width:120px;text-align:right}
main{padding:22px;display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:18px;max-width:1500px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
.card h2{font-size:15px;margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:500}
.pill{display:inline-block;border-radius:8px;padding:1px 8px;font-size:12px;font-weight:600}
.ok{background:rgba(63,185,80,.15);color:var(--green)}
.err{background:rgba(248,81,73,.15);color:var(--red)}
.warn{background:rgba(210,153,34,.15);color:var(--yellow)}
.muted{color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>polygate 控制台</h1><span class="badge" id="version">…</span>
  <div class="toolbar">
    <input id="key" placeholder="admin key" value="change-me">
    <button onclick="saveKey()">保存</button>
    <span id="status">连接中…</span>
  </div>
</header>
<main>
  <section class="card"><h2>Providers</h2><table id="providers"><thead><tr><th>名称</th><th>熔断状态</th><th>连续失败</th><th>Key 数</th><th>限流(r/s)</th></tr></thead><tbody></tbody></table></section>
  <section class="card"><h2>Models</h2><table id="models"><thead><tr><th>模型</th><th>类型</th><th>路由</th></tr></thead><tbody></tbody></table></section>
  <section class="card"><h2>实时用量</h2><table id="usage"><thead><tr><th>模型</th><th>调用</th><th>错误</th><th>Tokens</th><th>平均延迟</th></tr></thead><tbody></tbody></table></section>
  <section class="card"><h2>每日历史</h2><table id="history"><thead><tr><th>日期</th><th>调用</th><th>错误</th><th>Tokens</th></tr></thead><tbody></tbody></table></section>
  <section class="card"><h2>成本</h2><div id="cost-total" class="muted"></div><table id="costs"><thead><tr><th>Key</th><th>成本</th><th>调用</th><th>Tokens</th></tr></thead><tbody></tbody></table></section>
  <section class="card"><h2>最近请求</h2><table id="requests"><thead><tr><th>端点</th><th>模型</th><th>Provider</th><th>状态</th><th>延迟</th></tr></thead><tbody></tbody></table></section>
  <section class="card" style="grid-column:1/-1">
    <h2>⚙️ 配置 · 在线修改（保存后热重载，无需重启）</h2>
    <table id="cfg-providers"><thead><tr><th style="width:130px">Provider</th><th>Base URL</th><th>API Key（留空 = 不改）</th><th style="width:45px">Key</th></tr></thead><tbody></tbody></table>
    <div class="toolbar" style="margin-top:12px">
      <button onclick="saveConfig()">保存全部</button>
      <span id="cfg-msg" class="muted"></span>
    </div>
    <hr style="border:0;border-top:1px solid var(--border);margin:16px 0">
    <h2>➕ 新增 Provider</h2>
    <div class="toolbar" style="flex-wrap:wrap">
      <input id="new-name" placeholder="名称，如 deepseek" style="width:150px">
      <select id="new-preset" onchange="fillPreset()" style="background:#0a0c0f;border:1px solid var(--border);color:var(--fg);border-radius:6px;padding:5px 8px;font-size:13px">
        <option value="">选择预设…</option>
        <option value="deepseek">DeepSeek</option>
        <option value="qwen">通义千问</option>
        <option value="zhipu">智谱 GLM</option>
        <option value="moonshot">Kimi</option>
        <option value="openai">OpenAI</option>
        <option value="custom">自定义</option>
      </select>
      <input id="new-url" placeholder="https://.../v1" style="width:300px">
      <input id="new-key" type="password" placeholder="API Key">
      <input id="new-model" placeholder="可选：模型名" style="width:130px">
      <button onclick="addProvider()">添加</button>
    </div>
    <p class="muted" style="margin:8px 0 0">提示：填了「模型名」会自动创建一条 chat 模型映射，添加后即可直接调用。</p>
  </section>
</main>
<script>
function k(){return localStorage.getItem('polygate_key')||'change-me';}
document.getElementById('key').value=k();
function saveKey(){localStorage.setItem('polygate_key',document.getElementById('key').value.trim());refresh();}
function H(){return {'Authorization':'Bearer '+k()};}
async function getJSON(path){const r=await fetch(path,{headers:H()});if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
function pill(s){const c=s==='closed'?'ok':(s==='open'?'err':'warn');return '<span class="pill '+c+'">'+s+'</span>';}
async function refresh(){
  const st=document.getElementById('status');
  try{
    const prov=await getJSON('/v1/status');
    const usage=await getJSON('/v1/usage');
    document.getElementById('version').textContent='v'+prov.version;
    const pt=document.querySelector('#providers tbody');pt.innerHTML='';
    for(const [name,p] of Object.entries(prov.providers)){
      const rl=p.rate_limit?p.rate_limit[0]:'—';
      pt.insertAdjacentHTML('beforeend','<tr><td>'+name+'</td><td>'+pill(p.state)+'</td><td>'+p.consecutive_failures+'</td><td>'+p.keys+'</td><td>'+rl+'</td></tr>');
    }
    const mt=document.querySelector('#models tbody');mt.innerHTML='';
    for(const m of prov.models){
      const routes=m.routes.map(function(r){return r.provider+':'+r.model+'('+r.weight+')';}).join(', ');
      mt.insertAdjacentHTML('beforeend','<tr><td>'+m.id+'</td><td>'+m.kind+'</td><td class="muted">'+routes+'</td></tr>');
    }
    const ut=document.querySelector('#usage tbody');ut.innerHTML='';
    for(const [m,u] of Object.entries(usage.models)){
      ut.insertAdjacentHTML('beforeend','<tr><td>'+m+'</td><td>'+u.calls+'</td><td>'+u.errors+'</td><td>'+u.total_tokens+'</td><td>'+u.avg_latency_ms+'ms</td></tr>');
    }
    let hist=[];
    try{hist=await getJSON('/v1/usage/history?days=7');}catch(e){}
    const ht=document.querySelector('#history tbody');ht.innerHTML='';
    for(const h of hist){
      ht.insertAdjacentHTML('beforeend','<tr><td>'+h.day+'</td><td>'+h.calls+'</td><td>'+h.errors+'</td><td>'+(h.prompt_tokens+h.completion_tokens)+'</td></tr>');
    }
    let costs=[];
    try{costs=await getJSON('/v1/costs');}catch(e){}
    document.getElementById('cost-total').textContent='总成本: '+costs.total;
    const cst=document.querySelector('#costs tbody');cst.innerHTML='';
    for(const c of (costs.by_key||[])){
      cst.insertAdjacentHTML('beforeend','<tr><td>'+c.key+'</td><td>'+c.cost+'</td><td>'+c.calls+'</td><td>'+(c.prompt_tokens+c.completion_tokens)+'</td></tr>');
    }
    let reqs=[];
    try{reqs=await getJSON('/v1/requests?limit=10');}catch(e){}
    const rt=document.querySelector('#requests tbody');rt.innerHTML='';
    for(const r of reqs){
      rt.insertAdjacentHTML('beforeend','<tr><td>'+r.endpoint+'</td><td>'+r.model+'</td><td>'+(r.provider||'')+'</td><td>'+r.status+'</td><td>'+r.latency_ms+'ms</td></tr>');
    }
    st.textContent='已连接 '+new Date().toLocaleTimeString();
    st.className='ok';
  }catch(e){st.textContent='连接失败: '+e.message;st.className='err';}
}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];});}
let PRESETS={};
async function loadConfig(){
  try{
    const cfg=await getJSON('/v1/config');
    PRESETS=cfg.presets||{};
    const tb=document.querySelector('#cfg-providers tbody');tb.innerHTML='';
    for(const [name,p] of Object.entries(cfg.providers)){
      const ph=p.has_key?('已设置 '+p.key_hint):'未设置';
      tb.insertAdjacentHTML('beforeend','<tr><td>'+esc(name)+'</td>'
        +'<td><input class="cfg-url" data-name="'+esc(name)+'" value="'+esc(p.base_url)+'" style="width:100%"></td>'
        +'<td><input class="cfg-key" data-name="'+esc(name)+'" type="password" placeholder="'+esc(ph)+'" style="width:100%"></td>'
        +'<td class="muted">'+(p.has_key?'✓':'—')+'</td></tr>');
    }
  }catch(e){const m=document.getElementById('cfg-msg');if(m){m.textContent='配置加载失败: '+e.message;m.style.color='var(--red)';}}
}
async function putConfig(payload){
  const r=await fetch('/v1/config',{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},H()),body:JSON.stringify(payload)});
  if(!r.ok){throw new Error(await r.text());}
  return r.json();
}
async function saveConfig(){
  const msg=document.getElementById('cfg-msg');
  try{
    const providers={};
    document.querySelectorAll('.cfg-url').forEach(function(inp){providers[inp.dataset.name]={base_url:inp.value};});
    document.querySelectorAll('.cfg-key').forEach(function(inp){if(inp.value){providers[inp.dataset.name]=providers[inp.dataset.name]||{};providers[inp.dataset.name].api_key=inp.value;}});
    await putConfig({providers:providers});
    msg.textContent='已保存并热重载 ✓';msg.style.color='var(--green)';
    await loadConfig();refresh();
  }catch(e){msg.textContent='保存失败: '+e.message;msg.style.color='var(--red)';}
}
function fillPreset(){
  const v=document.getElementById('new-preset').value;
  const urlEl=document.getElementById('new-url');
  if(v==='custom'){urlEl.value='';return;}
  if(PRESETS[v]){urlEl.value=PRESETS[v];}
}
async function addProvider(){
  const name=document.getElementById('new-name').value.trim();
  const url=document.getElementById('new-url').value.trim();
  const key=document.getElementById('new-key').value;
  const model=document.getElementById('new-model').value.trim();
  const msg=document.getElementById('cfg-msg');
  if(!name||!url){msg.textContent='请填写名称和 URL';msg.style.color='var(--red)';return;}
  try{
    const providers={};providers[name]={base_url:url};if(key){providers[name].api_key=key;}
    const payload={providers:providers};
    if(model){payload.models=[{name:model,kind:'chat',providers:[{provider:name,model:model}]}];}
    await putConfig(payload);
    msg.textContent='已添加 '+name+(model?(' + 模型 '+model):'')+' ✓';msg.style.color='var(--green)';
    document.getElementById('new-name').value='';document.getElementById('new-url').value='';document.getElementById('new-key').value='';document.getElementById('new-model').value='';
    await loadConfig();refresh();
  }catch(e){msg.textContent='添加失败: '+e.message;msg.style.color='var(--red)';}
}
refresh();
loadConfig();
setInterval(refresh,5000);
</script>
</body>
</html>
"""
