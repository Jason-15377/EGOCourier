// EGO Courier 单页前端（纯 JS）
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];

const API = {
  async j(method, url, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  },
};

function fmtUs(us) {
  if (!us) return '-';
  const d = new Date(us / 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3,'0')}`;
}

// ---------------- 导航 ----------------
$$('.nav-btn').forEach(b => b.onclick = () => {
  $$('.nav-btn').forEach(x => x.classList.toggle('active', x === b));
  $$('.view').forEach(v => v.classList.add('hidden'));
  $('#view-' + b.dataset.view).classList.remove('hidden');
  if (b.dataset.view === 'sessions') loadSessions();
  if (b.dataset.view === 'thresholds') loadThresholds();
  if (b.dataset.view === 'wifi') loadWifi();
  if (b.dataset.view === 'export') loadExport();
});

// ---------------- 健康 ----------------
async function health() {
  try { const d = await API.j('GET', '/api/health'); $('#health').textContent = '● 服务正常'; }
  catch (e) { $('#health').textContent = '● 服务异常'; }
}
health();
loadExport();

// ---------------- 会话列表 ----------------
async function loadSessions() {
  const tb = $('#sessions-tbl tbody');
  try {
    const d = await API.j('GET', '/api/sessions');
    tb.innerHTML = d.sessions.map(s => `
      <tr>
        <td><input type="checkbox" class="sel-sess" data-id="${s.id}"></td>
        <td>${s.id}</td><td>${esc(s.device_serial || '-')}</td>
        <td><a onclick="openDetail(${s.id})">${esc(s.name)}</a></td>
        <td>${s.frames}</td>
        <td>${s.anomalies > 0 ? `<span class="badge err">${s.anomalies}</span>` : '<span class="badge ok">0</span>'}</td>
        <td class="mono">${esc((s.trace_id||'').slice(0,32)) || '-'}</td>
        <td>${fmtUs(s.start_us)}</td>
        <td><a onclick="openDetail(${s.id})">详情</a></td>
      </tr>`).join('') || '<tr><td colspan="9">暂无会话</td></tr>';
  } catch (e) { tb.innerHTML = `<tr><td colspan="9">加载失败: ${esc(e.message)}</td></tr>`; }
}
$('#btn-refresh').onclick = loadSessions;
$('#chk-all').onchange = () => { const v = $('#chk-all').checked;
  $$('.sel-sess').forEach(c => c.checked = v); };
$('#btn-sel-all').onclick = () => $$('.sel-sess').forEach(c => c.checked = true);
$('#btn-sel-none').onclick = () => $$('.sel-sess').forEach(c => c.checked = false);
const checkedIds = () => $$('.sel-sess').filter(c => c.checked).map(c => parseInt(c.dataset.id, 10));

async function compareSessions() {
  const ids = checkedIds();
  if (ids.length < 2) { alert('请至少勾选 2 条会话进行对比'); return; }
  try {
    const d = await API.j('POST', '/api/sessions/compare', { ids });
    const rows = d.rows || [];
    if (!rows.length) { alert('未获取到对比数据'); return; }
    const headers = Object.keys(rows[0]);
    let html = `<table class="tbl"><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>`;
    rows.forEach(r => {
      html += `<tr>${headers.map(h=>`<td>${esc(r[h])}</td>`).join('')}</tr>`;
    });
    html += '</tbody></table>';
    $('#compare-body').innerHTML = html;
    $('#compare-area').classList.remove('hidden');
  } catch (e) { alert('对比失败: ' + e.message); }
}
$('#btn-compare').onclick = compareSessions;

async function exportSummary() {
  const ids = checkedIds();
  if (ids.length < 2) { alert('请至少勾选 2 条会话导出汇总报告'); return; }
  try {
    const d = await API.j('POST', '/api/sessions/compare', { ids });
    const rows = d.rows || [];
    const headers = Object.keys(rows[0] || {});
    const sum = {}; headers.forEach(h => sum[h] = 0);
    rows.forEach(r => headers.forEach(h => {
      if (['帧数','异常','丢帧数','左右匹配','垃圾时间戳'].includes(h) && typeof r[h] === 'number') sum[h] += r[h];
    }));
    const n = rows.length || 1;
    const sumRow = Object.assign({}, ...headers.map(h => ({ [h]: (typeof rows[0][h] === 'number'
        && ['会话','设备','开始时间','左右同步','IMU同步'].indexOf(h) < 0) ? (sum[h]/ (['丢帧率','左右均值(µs)','IMU均值(µs)'].includes(h) ? n : 1)).toFixed(['丢帧率'].includes(h)?3:1) : (h==='会话'?'合计/平均':'—') })));
    let html = `<html lang="zh"><head><meta charset="utf-8"><title>多会话对比汇总</title><style>
      body{font-family:'Microsoft YaHei',sans-serif;background:#f5f6f8;color:#222;margin:0}
      h1{background:#1f2937;color:#fff;padding:16px 24px;margin:0;font-size:20px}.wrap{padding:20px}
      table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
      th,td{border:1px solid #e5e7eb;padding:6px 8px;text-align:left}th{background:#f9fafb}
      .sum td{background:#eef2ff;font-weight:600}</style></head><body>
      <h1>多会话对比汇总（${rows.length} 条）</h1><div class="wrap">
      <table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>`;
    rows.forEach(r => html += `<tr>${headers.map(h=>`<td>${r[h]}</td>`).join('')}</tr>`);
    html += `<tr class="sum">${headers.map(h=>`<td>${sumRow[h]}</td>`).join('')}</tr></tbody></table></div></body></html>`;
    const blob = new Blob([html], { type: 'text/html' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = 'compare_summary.html'; a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { alert('导出失败: ' + e.message); }
}
$('#btn-summary').onclick = exportSummary;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ---------------- 导入 ----------------
$('#btn-import').onclick = async () => {
  const path = $('#import-path').value.trim();
  const out = $('#import-result');
  if (!path) { out.textContent = '请先选择目录或输入路径'; return; }
  out.textContent = '导入中…';
  try {
    const d = await API.j('POST', '/api/import', { path });
    const s = d.session;
    out.textContent = `导入完成 会话 #${s.id}  ${s.name}\n` +
      `设备: ${s.device_serial || '-'}  trace_id: ${s.trace_id || '-'}\n` +
      `时间范围: ${fmtUs(s.start_us)} ~ ${fmtUs(s.end_us)}\n` +
      `对齐帧: ${d.stats.frames}  异常: ${d.stats.anomalies}`;
    loadSessions();
  } catch (e) { out.textContent = '失败: ' + e.message; }
};
$('#import-path').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-import').click(); });

async function pickAndImport(mode) {
  const out = $('#import-result');
  try {
    const d = await API.j('GET', '/api/pick?mode=' + mode);
    if (!d.path) { out.textContent = '已取消选择'; return; }
    $('#import-path').value = d.path;
    $('#btn-import').click();
  } catch (e) { out.textContent = '选择失败: ' + e.message; }
}
$('#btn-pick-dir').onclick = () => pickAndImport('dir');
$('#btn-pick-zip').onclick = () => pickAndImport('zip');

// ---------------- Trace 查询 ----------------
$('#btn-trace').onclick = async () => {
  const tid = $('#trace-input').value.trim();
  const out = $('#trace-result');
  if (!tid) { out.innerHTML = ''; return; }
  try {
    const d = await API.j('GET', '/api/traces/' + encodeURIComponent(tid));
    let html = `<div class="kpi"><b>${d.sessions.length}</b><span>关联会话</span></div>
                <div class="kpi"><b>${d.frames.length}</b><span>帧</span></div>`;
    if (d.frames.length) {
      html += `<table class="data"><tr><th>帧</th><th>侧</th><th>hw_ptp(us)</th><th>hw_ptp本地</th><th>sei(us)</th><th>app_recv(us)</th></tr>` +
        d.frames.slice(0,500).map(f => `<tr><td>${f.frame_index}</td><td>${f.side}</td>
          <td class="mono">${f.hw_ptp_us||''}</td><td>${fmtUs(f.hw_ptp_us)}</td>
          <td class="mono">${f.sei_hw_ptp_us||''}</td><td class="mono">${f.app_recv_us||''}</td></tr>`).join('') + '</table>';
    }
    out.innerHTML = html;
  } catch (e) { out.innerHTML = '<div class="result">失败: ' + esc(e.message) + '</div>'; }
};

// ---------------- WiFi ----------------
async function loadWifi() {
  const tb = $('#wifi-tbl tbody');
  try {
    const d = await API.j('GET', '/api/wifi/scan');
    const iface = d.interface || {};
    $('#wifi-status').innerHTML = `
      <div class="kpi"><b>${esc(iface.ssid || '-')}</b><span>当前 SSID</span></div>
      <div class="kpi"><b>${iface.signal ?? 0}%</b><span>信号</span></div>
      <div class="kpi"><b>${iface.connected ? '已连接' : '未连接'}</b><span>状态</span></div>`;
    tb.innerHTML = (d.networks || []).map(n => `
      <tr>
        <td>${esc(n.ssid)}</td>
        <td>${n.signal}%</td>
        <td>${esc(n.security || '-')}</td>
        <td>${n.connected ? '<span class="badge ok">已连接</span>' : ''}</td>
      </tr>`).join('') || '<tr><td colspan="4">未发现可见 WiFi（或无无线网卡）</td></tr>';
  } catch (e) { tb.innerHTML = `<tr><td colspan="4">扫描失败: ${esc(e.message)}</td></tr>`; }
}
$('#btn-wifi-refresh').onclick = loadWifi;

// ---------------- 日志导出：BLE 配网 + SSH 一键导出 ----------------
async function loadExport() {
  try { const d = await API.j('GET', '/api/ble/scan');
    $('#export-avail').textContent = '● EgoLowBle.dll 可用';
  } catch (e) { $('#export-avail').textContent = 'EgoLowBle.dll 不可用（请安装 EGOViewer）'; }
}
async function scanBle() {
  const sel = $('#ble-devices');
  $('#ble-msg').textContent = '扫描中…';
  try {
    const d = await API.j('GET', '/api/ble/scan');
    const devs = d.devices || [];
    sel.innerHTML = '<option value="">' + (devs.length ? '选择设备' : '未发现设备') + '</option>' +
      devs.map(x => `<option value="${esc(x.address)}">${esc(x.name || '(未命名)')} · ${esc(x.address || '')}</option>`).join('');
    $('#ble-msg').textContent = `发现 ${devs.length} 台设备`;
  } catch (e) { $('#ble-msg').textContent = '扫描失败: ' + e.message; }
}
async function scanWifiPick() {
  const sel = $('#wifi-pick');
  $('#ble-msg').textContent = '扫描本机 WiFi…';
  try {
    const d = await API.j('GET', '/api/wifi/scan');
    const nets = d.networks || [];
    const cur = (d.interface || {}).ssid || '';
    const curLower = cur.toLowerCase();
    sel.innerHTML = '<option value="">— 选择要配给设备的 WiFi —</option>' +
      nets.map(n => {
        const ssid = n.ssid || '(隐藏网络)';
        const mark = cur && ssid.toLowerCase() === curLower ? '  [电脑已连接]' : '';
        return `<option value="${esc(ssid)}">${esc(ssid)}${mark}（信号${n.signal}%）</option>`;
      }).join('');
    // 自动带出电脑当前连接的 WiFi
    if (cur) {
      $('#ble-ssid').value = cur;
      $('#ble-msg').textContent = `已自动填入电脑当前连接的 WiFi：${cur}（可再从下拉另选）`;
    } else {
      $('#ble-msg').textContent = `扫描完成，发现 ${nets.length} 台可见 WiFi（电脑当前未连接，请手动选择）`;
    }
  } catch (e) { $('#ble-msg').textContent = 'WiFi 扫描失败: ' + e.message; }
}
$('#wifi-pick').onchange = () => {
  const v = $('#wifi-pick').value;
  if (v) $('#ble-ssid').value = v;
};
async function provisionBle() {
  const address = $('#ble-devices').value;
  let ssid = $('#ble-ssid').value.trim();
  const pwd = $('#ble-pwd').value;
  if (!address) { $('#ble-msg').textContent = '请先扫描并选择蓝牙设备'; return; }
  if (!ssid) {
    // 未填名称时自动带出电脑当前连接的 WiFi
    try {
      const d = await API.j('GET', '/api/wifi/scan');
      const cur = (d.interface || {}).ssid || '';
      if (cur) { ssid = cur; $('#ble-ssid').value = cur; $('#ble-msg').textContent = `已自动填入电脑当前连接的 WiFi：${cur}`; }
    } catch (e) { /* ignore */ }
  }
  if (!ssid) { $('#ble-msg').textContent = 'Wi-Fi 名称不能为空'; return; }
  $('#ble-msg').textContent = `配置 ${ssid} 中，等待设备加入局域网…`;
  try {
    const d = await API.j('POST', '/api/ble/provision', { address, ssid, password: pwd });
    if (d.ip) { $('#exp-ip').value = d.ip; $('#ble-msg').textContent = `配网成功，设备 IP = ${d.ip}（已回填）`; }
    else $('#ble-msg').textContent = '配网已下发，但未在超时内拿到 IP';
  } catch (e) { $('#ble-msg').textContent = '配网失败: ' + e.message; }
}
async function probeDevice() {
  const host = $('#exp-ip').value.trim();
  $('#probe-msg').textContent = '';
  if (!host) { $('#probe-msg').textContent = '请先填写设备 IP（或配网回填）'; return; }
  $('#probe-msg').textContent = '预检中…';
  try {
    const d = await API.j('GET', '/api/device/probe?host=' + encodeURIComponent(host));
    const ok = d.same_net && d.reachable;
    let msg = `设备 ${host} · 电脑本机 ${d.local_ip || '未知'} · ${d.same_net ? '同网段' : '⚠ 不在同一网段'} · ${d.reachable ? 'ping 通' : '⚠ ping 不通'}`;
    $('#probe-msg').textContent = msg;
    $('#probe-msg').style.color = ok ? '#16a34a' : '#dc2626';
    if (!d.reachable) $('#exp-msg').textContent = '⚠ 设备不可达，导出很可能失败，请确认设备已接入电脑所在 WiFi';
  } catch (e) { $('#probe-msg').textContent = '预检失败: ' + e.message; }
}
async function doExport() {
  const body = {
    host: $('#exp-ip').value.trim(),
    user: $('#exp-user').value.trim(),
    password: $('#exp-pwd').value,
    remote_dirs: $('#exp-dirs').value.trim() || '/ego/logs',
    local_dir: $('#exp-local').value.trim(),
    zip_first: $('#exp-zip').checked,
    auto_import: $('#exp-import').checked,
  };
  const out = $('#exp-result');
  $('#exp-msg').textContent = '';
  if (!body.host || !body.user || !body.password) { $('#exp-msg').textContent = '请填写设备 IP 与 SSH 账号密码'; return; }
  out.textContent = '导出中（拉取 + 自动导入，可能较慢）…';
  try {
    const d = await API.j('POST', '/api/device/export', body);
    out.textContent = '已下载文件:\n' + (d.files || []).map(f => '  ' + f).join('\n');
    if (d.import) {
      const s = d.import.session, st = d.import.stats;
      out.textContent += `\n\n自动导入成功: 会话 #${s.id} ${s.name}\n  对齐帧 ${st.frames} · 异常 ${st.anomalies}`;
      loadSessions();
    }
    $('#exp-msg').textContent = '完成';
  } catch (e) { out.textContent = '失败: ' + e.message; }
}
$('#btn-ble-scan').onclick = scanBle;
$('#btn-wifi-search').onclick = scanWifiPick;
$('#btn-ble-apply').onclick = provisionBle;
$('#btn-exp').onclick = doExport;
$('#btn-probe').onclick = probeDevice;

// ---------------- 阈值 ----------------
async function loadThresholds() {
  const d = await API.j('GET', '/api/config/thresholds');
  const ed = $('#thr-editor');
  ed.innerHTML = Object.entries(d.thresholds_us).map(([k,v]) => `
    <div class="row" style="align-items:center">
      <label style="width:220px">${k} <span style="color:#999">(μs)</span></label>
      <input class="input thr-in" data-key="${k}" value="${v}" style="width:120px">
    </div>`).join('');
}
$('#btn-thr-save').onclick = async () => {
  const t = {};
  $$('.thr-in').forEach(i => t[i.dataset.key] = parseInt(i.value, 10));
  try { await API.j('PUT', '/api/config/thresholds', { thresholds_us: t });
    $('#thr-msg').textContent = '已保存'; setTimeout(()=>$('#thr-msg').textContent='', 2000);
  } catch (e) { $('#thr-msg').textContent = '失败: ' + e.message; }
};

// ---------------- 会话详情 ----------------
let curSession = null;
async function openDetail(id) {
  $('#detail-mask').classList.remove('hidden');
  const d = await API.j('GET', '/api/sessions/' + id);
  curSession = d.session;
  $('#detail-title').textContent = `会话 #${d.session.id} · ${d.session.name}`;
  $('#detail-stats').innerHTML = `
    <div class="kpi"><b>${d.stats.frames}</b><span>对齐帧</span></div>
    <div class="kpi"><b>${d.stats.anomalies_by_rule.reduce((a,x)=>a+x.n,0)}</b><span>异常</span></div>
    <div class="kpi"><b>${esc(d.session.device_serial || '-')}</b><span>设备</span></div>
    <div class="kpi"><b class="mono" style="font-size:14px">${esc((d.session.trace_id||'').slice(0,16))||'-'}</b><span>trace_id</span></div>`;
  switchTab('frames');
}
$('#btn-detail-close').onclick = () => $('#detail-mask').classList.add('hidden');
$('#btn-detail-export').onclick = () => { if (curSession) location.href = `/api/sessions/${curSession.id}/export`; };
$('#btn-detail-attach').onclick = async () => {
  if (!curSession) return;
  const p = prompt('输入 EGOViewer app 日志（ego-viewer-*.log）绝对路径：\n系统会复制进附件区并就地重算，从而让 trace_id 和应用接收延迟可计算。');
  if (!p || !p.trim()) return;
  try {
    await API.j('POST', `/api/sessions/${curSession.id}/attach`, { path: p.trim() });
    alert('已关联并重算完成');
    openDetail(curSession.id);
    loadSessions();
  } catch (e) { alert('失败: ' + e.message); }
};
$$('.tab').forEach(t => t.onclick = () => switchTab(t.dataset.tab));

async function switchTab(tab) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  $('#frames-filters').style.display = (tab === 'frames') ? '' : 'none';
  const body = $('#detail-body');
  if (!curSession) return;
  const id = curSession.id;
  if (tab === 'files') {
    const f = (curSession.meta || {}).files || [];
    body.innerHTML = `<table class="data"><tr><th>类型</th><th>侧</th><th>行数</th><th>文件</th></tr>` +
      f.map(x => `<tr><td>${x.source_type}</td><td>${x.side||'-'}</td><td>${x.count}</td><td class="mono">${esc(x.file)}</td></tr>`).join('') + '</table>';
    return;
  }
  if (tab === 'anomalies') {
    const d = await API.j('GET', `/api/sessions/${id}/anomalies`);
    const a = d.anomalies;
    body.innerHTML = `<div class="kpi"><b>${a.length}</b><span>异常</span></div>
      <table class="data"><tr><th>规则</th><th>帧</th><th>侧</th><th>偏差(μs)</th><th>阈值(μs)</th><th>级别</th><th>截帧</th></tr>` +
      (a.map(x => `<tr class="${x.severity==='error'?'anom':''}"><td>${esc(x.rule)}</td><td>${x.frame_index}</td>
        <td>${esc(x.side)}</td><td>${x.delta_us}</td><td>${x.threshold_us}</td><td>${esc(x.severity)}</td>
        <td>${x.frame_image ? `<img src="/api/anomalies/${x.id}/image" width="110" style="border:1px solid #ccc" onerror="this.style.display='none'">` : ''}</td></tr>`).join('')
        || '<tr><td colspan="7">无异常</td></tr>') + '</table>';
    return;
  }
  // frames
  const side = $('#f-side').value, only = $('#f-anomaly').checked;
  const d = await API.j('GET', `/api/sessions/${id}/frames?side=${side}&anomaly_only=${only?1:0}&limit=5000`);
  const fs = d.frames;
  const anomKeys = new Set();
  try { const ad = await API.j('GET', `/api/sessions/${id}/anomalies`); ad.anomalies.forEach(x => anomKeys.add(x.frame_index + ':' + x.side)); } catch(e){}
  body.innerHTML = `<div class="kpi"><b>${d.total}</b><span>帧</span></div>
    <table class="data"><tr><th>帧</th><th>侧</th><th>hw_ptp(us)</th><th>hw_ptp 本地</th><th>sei(us)</th><th>app_recv(us)</th><th>imu(us)</th></tr>` +
    fs.map(f => {
      const anom = anomKeys.has(f.frame_index + ':' + f.side);
      return `<tr class="${anom?'anom':''}"><td>${f.frame_index}</td><td>${f.side}</td>
        <td class="mono">${f.hw_ptp_us||''}</td><td>${fmtUs(f.hw_ptp_us)}</td>
        <td class="mono">${f.sei_hw_ptp_us||''}</td><td class="mono">${f.app_recv_us||''}</td>
        <td class="mono">${f.imu_us||''}</td></tr>`; }).join('') + '</table>';
}
$('#f-side').onchange = () => switchTab('frames');
$('#f-anomaly').onchange = () => switchTab('frames');
