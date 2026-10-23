"use strict";
const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="session-token"]').content;
let rows = [], interfaces = [], filter = 'all', targetType = 'host', preview = null, busy = false, stopped = false, loading = false;
const labels = {saved:'网卡保存', local:'本地配置', tunnel:'隧道路由', runtime:'临时路由'};
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const displayTarget = (r) => r.cidr === '0.0.0.0/0' ? '默认路由' : r.cidr.replace(/\/32$/, '');
const via = (r) => r.mode === 'gateway' ? r.gateway : '接口直连';
function notice(message, type = '') { const n = $('#notice'); n.textContent = message; n.className = `notice ${type}`; n.hidden = !message; }
async function api(path, data) {
  const options = {method:data === undefined ? 'GET' : 'POST', headers:{'X-Session-Token':token}, cache:'no-store'};
  if (data !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data); }
  let response;
  try { response = await fetch(`/api/${path}`, options); }
  catch { throw new Error('无法连接后台。请检查启动终端，或重新双击启动.command。'); }
  let result;
  try { result = await response.json(); } catch { throw new Error('后台返回了无法识别的响应。'); }
  if (!response.ok) throw new Error(result.error || result.detail || '操作失败。');
  return result;
}
function setBusy(value) {
  busy = value;
  $('#shutdown').disabled = value || stopped;
  $('#refresh').disabled = value || stopped;
  $('#add-button').disabled = value || stopped;
  $('#confirm-apply').disabled = value || stopped;
  $('#cancel-dialog').disabled = value;
  $('#close-dialog').disabled = value;
  document.querySelectorAll('.toggle').forEach(el => el.disabled = value || stopped);
  $('#add-form').querySelectorAll('input,select,button').forEach(el => el.disabled = value || stopped);
}
function render() {
  const search = $('#search').value.trim().toLowerCase();
  const visible = rows.filter(r => r.editable && (filter === 'all' || (filter === 'enabled' ? r.enabled : !r.enabled)) && `${r.cidr} ${r.gateway} ${r.interface}`.toLowerCase().includes(search));
  $('#routes').innerHTML = visible.length ? visible.map(r => {
    const status = r.status === 'missing' ? '未生效 / 接口或路由不可用' : r.status === 'restored' ? '已被系统或其他软件重新添加' : '';
    const source = r.status === 'disabled' ? '已关闭' : labels[r.source] || '系统路由';
    return `<tr class="${r.enabled?'':'disabled-row'}"><td class="mono">${escape(displayTarget(r))}</td><td class="mono">${escape(via(r))}</td><td class="mono">${escape(r.interface)}</td><td><span class="badge ${r.source==='tunnel'?'tunnel':''}">${source}</span>${status?`<span class="status-note">${status}</span>`:''}</td><td class="switch-col"><button type="button" class="toggle" role="switch" aria-checked="${r.enabled}" aria-label="${r.enabled?'关闭':'开启'} ${escape(r.cidr)}" data-id="${escape(r.id)}" ${busy||stopped?'disabled':''}></button></td></tr>`;
  }).join('') : '<tr><td colspan="5" class="empty">没有匹配的路由。可尝试清除筛选，或添加一条新路由。</td></tr>';
  const systemRows = rows.filter(r => !r.editable);
  $('#system-routes').innerHTML = systemRows.map(r => `<tr><td class="mono">${escape(displayTarget(r))}</td><td class="mono">${escape(via(r))}</td><td class="mono">${escape(r.interface)}</td><td class="lock">只读</td></tr>`).join('');
  $('#system-count').textContent = `${systemRows.length} 条`;
}
async function refresh({quiet=false}={}) {
  if (loading || busy || stopped) return;
  loading = true; $('#refresh').disabled = true;
  try {
    const data = await api('routes'); rows = data.routes; interfaces = data.interfaces;
    const previous = $('#interface').value;
    const tunnel = interfaces.filter(i => i.name.startsWith('utun') && i.up);
    $('#interface').innerHTML = tunnel.length ? tunnel.map(i => `<option value="${escape(i.name)}">${escape(i.name)}${i.addresses.length?' · '+escape(i.addresses.join(', ')):''}</option>`).join('') : '<option value="">暂无可用隧道接口</option>';
    if (tunnel.some(i => i.name === previous)) $('#interface').value = previous;
    render(); updatePath();
    $('#updated').textContent = `更新于 ${new Date(data.updated*1000).toLocaleTimeString('zh-CN',{hour12:false})}`;
    $('#connection').textContent = '后台已连接';
    if (data.warnings.length) notice(data.warnings.join('\n'), 'error'); else if (!quiet) notice('');
    if (data.busy) notice('另一个页面正在修改路由，请等待其完成后刷新。');
    return data;
  } catch (e) {
    notice(e.message, 'error'); $('#connection').textContent = '后台连接失败';
    if (!rows.length) $('#routes').innerHTML = `<tr><td colspan="5" class="empty">${escape(e.message)}<br>点击“刷新”重试。</td></tr>`;
    return null;
  } finally { loading = false; $('#refresh').disabled = stopped || busy; }
}
function updatePath() {
  const mode = $('#add-form').elements.mode.value;
  $('#gateway-field').hidden = mode !== 'gateway'; $('#interface-field').hidden = mode !== 'interface';
  $('#gateway').required = mode === 'gateway'; $('#interface').required = mode === 'interface';
  $('#path-via').textContent = (mode === 'gateway' ? $('#gateway').value : $('#interface').value) || '待填写出口';
  $('#path-target').textContent = $('#destination').value || '待填写目标';
}
async function openPreview(data) {
  if (busy || stopped) return;
  setBusy(true);
  try {
    preview = await api('preview', data);
    const name = {add:'添加',enable:'开启',disable:'关闭'}[preview.action];
    $('#preview-title').textContent = `${name}路由`;
    $('#preview-summary').textContent = preview.persistent ? '本次同步更新网卡中保存的额外路由配置，其他规则保留。' : preview.action === 'disable' ? '从系统移除这条路由，并保留恢复配置。' : '添加运行时路由，并在本地保存配置。';
    $('#preview-target').textContent = preview.route.cidr;
    $('#preview-gateway').textContent = via(preview.route);
    $('#preview-interface').textContent = preview.route.interface;
    $('#preview-commands').textContent = preview.commands.join('\n') || '系统状态已匹配，无需执行命令。';
    $('#preview-warning').textContent = preview.warning; $('#preview-warning').hidden = !preview.warning;
    $('#apply-status').textContent = '确认后会打开 macOS 管理员授权窗口。';
    $('#confirm-apply').textContent = '确认并授权';
    $('#preview-dialog').showModal();
  } catch (e) { notice(e.message, 'error'); }
  finally { setBusy(false); }
}
$('#routes').addEventListener('click', e => {const button = e.target.closest('.toggle'); if (!button) return; const r = rows.find(r => r.id === button.dataset.id); if (r) openPreview({action:r.enabled?'disable':'enable',id:r.id});});
$('#refresh').addEventListener('click', () => refresh());
$('#search').addEventListener('input', render);
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b => {b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});render();}));
document.querySelectorAll('[data-target]').forEach(button => button.addEventListener('click', () => {targetType=button.dataset.target;document.querySelectorAll('[data-target]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});$('#destination-help').textContent=targetType==='host'?'单个 IP，只针对这一台设备。':'CIDR 网段，例如 198.18.4.0/24。';}));
$('#add-form').addEventListener('input', updatePath); $('#add-form').addEventListener('change', updatePath);
$('#add-form').addEventListener('reset', () => { targetType='host'; document.querySelectorAll('[data-target]').forEach(b=>{b.classList.toggle('active',b.dataset.target==='host');b.setAttribute('aria-pressed',String(b.dataset.target==='host'));});$('#destination-help').textContent='单个 IP，只针对这一台设备。';setTimeout(updatePath,0); });
$('#add-form').addEventListener('submit', e => {e.preventDefault(); const destination=$('#destination').value.trim(); if (targetType==='host' && destination.includes('/') && !destination.endsWith('/32')) {notice('单个 IP 模式只支持一个地址；添加网段请切换到“网段”。','error');return;} if (targetType==='network' && !destination.includes('/')) {notice('网段请使用 CIDR 格式，例如 198.18.4.0/24。','error');return;} openPreview({action:'add',destination,mode:$('#add-form').elements.mode.value,gateway:$('#gateway').value.trim(),interface:$('#interface').value});});
function closeDialog() {if (!busy) {$('#preview-dialog').close();preview=null;}}
$('#close-dialog').addEventListener('click',closeDialog); $('#cancel-dialog').addEventListener('click',closeDialog);
$('#preview-dialog').addEventListener('cancel',e=>{if(busy)e.preventDefault();else preview=null;});
$('#confirm-apply').addEventListener('click',async()=>{
  if(!preview||busy)return;setBusy(true);$('#apply-status').textContent='等待管理员授权并核对系统结果，请勿关闭启动终端…';$('#confirm-apply').textContent='处理中…';
  try {const result=await api('apply',{id:preview.id});$('#preview-dialog').close();preview=null;setBusy(false);await refresh({quiet:true});notice(result.message,'success');}
  catch(e){$('#preview-dialog').close();preview=null;setBusy(false);await refresh({quiet:true});notice(e.message,'error');}
  finally{setBusy(false);}
});
$('#shutdown').addEventListener('click',async()=>{
  if(busy||stopped)return;setBusy(true);$('#shutdown').textContent='正在关闭…';
  try {
    await api('shutdown',{}); $('#preview-dialog').close();
    let disconnected=false;
    for(let i=0;i<15;i++){await new Promise(resolve=>setTimeout(resolve,400));try{await fetch('/api/routes',{headers:{'X-Session-Token':token},cache:'no-store'});}catch{disconnected=true;break;}}
    if(!disconnected)throw new Error('关闭请求已发送，但后台仍在运行。请查看启动终端。');
    stopped=true;document.body.classList.add('stopped');$('#shutdown').textContent='后台已关闭';$('#connection').textContent='后台已关闭';notice('后台已关闭，已生效的系统路由保持不变。再次使用请双击启动.command。','success');$('#search').disabled=true;document.querySelectorAll('[data-filter]').forEach(b=>b.disabled=true);
  }catch(e){notice(e.message,'error');$('#shutdown').textContent='关闭后台';}
  finally{setBusy(false);}
});
// Optional page-scoped agent tools reuse the same read and preview journeys.
// They never bypass the visible confirmation or macOS administrator dialog.
const context = document.modelContext;
if (context?.registerTool) {
  const lifecycle = new AbortController();
  const tools = [{
    name:'read_local_routes', title:'读取本机 IPv4 路由',
    description:'刷新并读取本机路由及保存的启停状态，不修改路由。',
    inputSchema:{type:'object',properties:{},additionalProperties:false},
    annotations:{readOnlyHint:true,untrustedContentHint:false},
    async execute(){if(stopped||busy||loading)throw new Error('后台忙碌、正在刷新或已关闭。');const data=await refresh({quiet:true});if(!data)throw new Error($('#notice').textContent||'读取路由失败。');return {routes:rows.map(r=>({id:r.id,destination:r.cidr,gateway:r.gateway,interface:r.interface,enabled:r.enabled,live:r.live,status:r.status,editable:r.editable}))};}
  },{
    name:'start_route_addition_preview',title:'预览新增路由',
    description:'填写添加表单并打开路由变更预览，尚不执行修改。用户仍须确认并通过 macOS 管理员授权。',
    inputSchema:{type:'object',properties:{destination:{type:'string'},mode:{type:'string',enum:['gateway','interface']},gateway:{type:'string'},interface:{type:'string'}},required:['destination','mode'],additionalProperties:false},
    annotations:{readOnlyHint:false,untrustedContentHint:false},
    async execute(input){
      if(busy||stopped)throw new Error('后台忙碌或已关闭。');
      if(!input||typeof input.destination!=='string'||!['gateway','interface'].includes(input.mode))throw new Error('路由参数不正确。');
      $('#preview-dialog').close();preview=null;await openPreview({action:'add',...input});
      if(!preview)throw new Error($('#notice').textContent||'路由预览失败。');
      $('#destination').value=input.destination;$('#gateway').value=input.gateway||'';
      $('#add-form').elements.mode.value=input.mode;if(input.interface)$('#interface').value=input.interface;
      targetType=input.destination.includes('/')&&!input.destination.endsWith('/32')?'network':'host';
      $('#destination-help').textContent=targetType==='host'?'单个 IP，只针对这一台设备。':'CIDR 网段，例如 198.18.4.0/24。';
      document.querySelectorAll('[data-target]').forEach(b=>{b.classList.toggle('active',b.dataset.target===targetType);b.setAttribute('aria-pressed',String(b.dataset.target===targetType));});
      updatePath();return {stage:'preview',route:preview.route,commands:preview.commands};
    }
  }];
  tools.forEach(tool=>{try{Promise.resolve(context.registerTool(tool,{signal:lifecycle.signal})).catch(()=>{});}catch{}});
  window.addEventListener('pagehide',()=>lifecycle.abort(),{once:true});
}
refresh();
