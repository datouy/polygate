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
    st.textContent='已连接 '+new Date().toLocaleTimeString();
    st.className='ok';
  }catch(e){st.textContent='连接失败: '+e.message;st.className='err';}
}
refresh();
setInterval(refresh,5000);
</script>
</body>
</html>
"""
