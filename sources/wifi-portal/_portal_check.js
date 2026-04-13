
    const TAB_CONFIG = {
      home: [{ key: 'status', label: '首页' }, { key: 'services', label: '服务状态' }],
      network: [{ key: 'overview', label: '概览' }, { key: 'wifi', label: 'Wi-Fi' }],
      openclash: [{ key: 'overview', label: '概览' }, { key: 'subscriptions', label: '配置订阅' }, { key: 'nodes', label: '节点切换' }, { key: 'config', label: '配置管理' }, { key: 'log', label: '服务日志' }],
      console: [{ key: 'terminal', label: 'Web SSH' }],
      gpio: [{ key: 'control', label: 'GPIO' }]
    };

    const APP_STATE = {
      status: null,
      openclash: null,
      openclashConfig: null,
      openclashSubscriptions: { active: '', items: [] },
      proxyGroups: [],
      sshCommand: '',
      activeTabs: { home: 'status', network: 'overview', openclash: 'overview', console: 'terminal', gpio: 'control' },
      loaded: { config: false, subscriptions: false, proxies: false, log: false, webssh: false },
      traffic: { last: null, samples: [] }
    };

    function messageOf(error) {
      if (!error) return '未知错误';
      if (typeof error === 'string') return error;
      return String(error.message || error);
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function setText(id, value) {
      const el = document.getElementById(id);
      if (el) el.textContent = value == null ? '' : String(value);
    }

    function setHtml(id, html) {
      const el = document.getElementById(id);
      if (el) el.innerHTML = html;
    }

    function renderRows(id, rows, emptyText) {
      setHtml(id, rows.length ? rows.join('') : `<div class="box">${escapeHtml(emptyText)}</div>`);
    }

    async function api(url, options) {
      const response = await fetch(url, options || {});
      const text = await response.text();
      let data = {};
      try {
        data = text ? JSON.parse(text) : {};
      } catch (_) {
        throw new Error(text || '接口返回异常');
      }
      if (!response.ok || data.ok === false) throw new Error(data.message || text || '请求失败');
      return data;
    }

    function percent(part, total) {
      if (!total) return 0;
      return Math.max(0, Math.min(100, part / total * 100));
    }

    function formatSpeed(bytes) {
      const value = Number(bytes || 0);
      if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB/s`;
      if (value >= 1024) return `${(value / 1024).toFixed(1)} KB/s`;
      return `${value.toFixed(0)} B/s`;
    }

    function formatTime(ts) {
      if (!ts) return '-';
      try { return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }); } catch (_) { return String(ts); }
    }

    function connectivityText(value) {
      const key = String(value || '').toLowerCase();
      const map = {
        full: '已联网',
        limited: '受限连接',
        portal: '认证门户',
        none: '未联网',
        unknown: '未知'
      };
      return map[key] || (value || '未知');
    }

    function renderTabs(view) {
      const tabs = TAB_CONFIG[view] || [];
      const active = APP_STATE.activeTabs[view] || (tabs[0] && tabs[0].key) || '';
      setHtml('tabmenu', `<ul class="tabs">${tabs.map((tab) => `<li class="${tab.key === active ? 'active' : ''}"><button type="button" onclick="activateTopTab('${view}', '${tab.key}')">${escapeHtml(tab.label)}</button></li>`).join('')}</ul>`);
    }

    function applyTopTab(view, key) {
      APP_STATE.activeTabs[view] = key;
      if (view === 'openclash') {
        showOcTab(key, document.querySelector(`.subtabs [data-oc-tab="${key}"]`), true);
        return;
      }
      document.querySelectorAll(`#view-${view} [data-pane-group="${view}"]`).forEach((el) => {
        el.style.display = el.dataset.pane === key ? '' : 'none';
      });
      if (view === 'console') loadWebSSH(false);
    }

    function activateTopTab(view, key) {
      APP_STATE.activeTabs[view] = key;
      renderTabs(view);
      applyTopTab(view, key);
    }

    function showView(name, button) {
      document.querySelectorAll('.view').forEach((el) => el.classList.toggle('active', el.id === `view-${name}`));
      document.querySelectorAll('.nav button').forEach((el) => el.classList.toggle('active', el.dataset.view === name));
      if (button) button.classList.add('active');
      renderTabs(name);
      applyTopTab(name, APP_STATE.activeTabs[name] || (TAB_CONFIG[name] && TAB_CONFIG[name][0] && TAB_CONFIG[name][0].key) || '');
    }

    function showViewByName(name) {
      showView(name, document.querySelector(`.nav button[data-view="${name}"]`));
    }

    function filterMenu(keyword) {
      const key = String(keyword || '').trim().toLowerCase();
      document.querySelectorAll('.nav button').forEach((button) => {
        button.style.display = !key || button.textContent.toLowerCase().includes(key) ? '' : 'none';
      });
    }

    function tone(item) {
      return item && item.active ? 'ok' : 'warn';
    }

    function stateText(item) {
      if (!item) return '未知';
      if (item.state === 'missing' || item.state === 'missing-docker') return '未就绪';
      return item.active ? '运行中' : '已停止';
    }

    function appendTraffic(stats) {
      if (!stats) return;
      const current = {
        sampleAt: Number(stats.sample_at || 0),
        rx: Number(stats.rx_bytes || 0),
        tx: Number(stats.tx_bytes || 0)
      };
      const last = APP_STATE.traffic.last;
      APP_STATE.traffic.last = current;
      if (!last || current.sampleAt <= last.sampleAt) {
        if (!APP_STATE.traffic.samples.length) APP_STATE.traffic.samples.push({ down: 0, up: 0 });
        return;
      }
      const dt = Math.max(current.sampleAt - last.sampleAt, 1);
      APP_STATE.traffic.samples.push({
        down: Math.max(0, (current.rx - last.rx) / dt),
        up: Math.max(0, (current.tx - last.tx) / dt)
      });
      if (APP_STATE.traffic.samples.length > 24) APP_STATE.traffic.samples.shift();
    }

    function renderTrafficChart() {
      const svg = document.getElementById('traffic-chart');
      if (!svg) return;
      const samples = APP_STATE.traffic.samples.length ? APP_STATE.traffic.samples.slice(-24) : Array.from({ length: 16 }, () => ({ down: 0, up: 0 }));
      const width = 600;
      const height = 320;
      const pad = 18;
      const maxValue = Math.max(1, ...samples.map((item) => Math.max(item.down, item.up)));
      const step = samples.length > 1 ? (width - pad * 2) / (samples.length - 1) : 0;
      const point = (value, index) => {
        const x = pad + step * index;
        const y = height - pad - (value / maxValue) * (height - pad * 2);
        return `${x.toFixed(2)} ${y.toFixed(2)}`;
      };
      const linePath = (key) => samples.map((item, index) => `${index === 0 ? 'M' : 'L'} ${point(item[key], index)}`).join(' ');
      const areaPath = (key) => `M ${pad} ${height - pad} ${samples.map((item, index) => `L ${point(item[key], index)}`).join(' ')} L ${width - pad} ${height - pad} Z`;
      const last = samples[samples.length - 1] || { down: 0, up: 0 };
      setText('traffic-up-rate', formatSpeed(last.up));
      setText('traffic-down-rate', formatSpeed(last.down));
      svg.innerHTML = `
        <defs>
          <linearGradient id="oc-down-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#5fcaf3" stop-opacity="0.72"/>
            <stop offset="100%" stop-color="#5fcaf3" stop-opacity="0.08"/>
          </linearGradient>
          <linearGradient id="oc-up-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#7364f4" stop-opacity="0.58"/>
            <stop offset="100%" stop-color="#7364f4" stop-opacity="0.06"/>
          </linearGradient>
        </defs>
        <g opacity="0.18">
          <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="#7b87c9"/>
          <line x1="${pad}" y1="${height - pad - (height - pad * 2) / 3}" x2="${width - pad}" y2="${height - pad - (height - pad * 2) / 3}" stroke="#7b87c9"/>
          <line x1="${pad}" y1="${height - pad - (height - pad * 2) * 2 / 3}" x2="${width - pad}" y2="${height - pad - (height - pad * 2) * 2 / 3}" stroke="#7b87c9"/>
        </g>
        <path d="${areaPath('down')}" fill="url(#oc-down-fill)"></path>
        <path d="${areaPath('up')}" fill="url(#oc-up-fill)"></path>
        <path d="${linePath('down')}" fill="none" stroke="#5fcaf3" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>
        <path d="${linePath('up')}" fill="none" stroke="#7364f4" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>
      `;
    }

    function renderIndicators(data) {
      const services = data.services || {};
      setHtml('tabmenu', document.getElementById('tabmenu').innerHTML);
      document.querySelector('header .header-note').textContent = `${data.uplink ? '已联网' : '离线'} · 代理 ${services.proxy && services.proxy.active ? '运行中' : '已停止'}`;
    }

    function serviceLogCommand(service) {
      const services = APP_STATE.status && APP_STATE.status.services ? APP_STATE.status.services : {};
      const map = {
        proxy: APP_STATE.openclash && APP_STATE.openclash.service || services.proxy && services.proxy.name || 'mihomo.service',
        ssh: services.ssh && services.ssh.name || 'ssh.service',
        docker: services.docker && services.docker.name || 'docker.service',
        supervisor: services.supervisor && services.supervisor.name || 'hassio-supervisor.service',
        agent: services.agent && services.agent.name || 'haos-agent.service',
        portal: services.portal && services.portal.name || 'mido-wifi-portal.service'
      };
      if (service === 'homeassistant') return 'ha core logs --lines 80';
      return `journalctl -u ${map[service]} -n 120 --no-pager`;
    }

    function serviceActionButtons(key) {
      if (key === 'homeassistant') return `<div class="service-actions"><button class="btn soft" onclick="setConsoleCommand('ha core restart')">重启核心</button><button class="btn dark" onclick="serviceLog('homeassistant')">日志</button></div>`;
      const stopButton = key === 'ssh' ? '' : `<button class="btn warn" onclick="serviceAction('${key}', 'stop')">停止</button>`;
      return `<div class="service-actions"><button class="btn ok" onclick="serviceAction('${key}', 'start')">启动</button><button class="btn soft" onclick="serviceAction('${key}', 'restart')">重启</button>${stopButton}<button class="btn dark" onclick="serviceLog('${key}')">日志</button></div>`;
    }

    function renderServiceGrid(services) {
      const order = ['proxy', 'ssh', 'docker', 'supervisor', 'agent', 'portal', 'homeassistant'];
      renderRows('service-grid', order.map((key) => {
        const item = services[key];
        if (!item) return '';
        return `<div class="row"><div><div class="row-title">${escapeHtml(item.label || key)}</div><div class="row-sub">${escapeHtml(item.detail || item.name || '-')}</div></div><span class="pill ${tone(item)}">${escapeHtml(stateText(item))}</span><div>${serviceActionButtons(key)}</div></div>`;
      }).filter(Boolean), '暂无服务状态');
    }

    function renderHomeServiceSummary(services) {
      const order = ['proxy', 'docker', 'portal', 'homeassistant'];
      renderRows('home-service-summary', order.map((key) => {
        const item = services[key];
        if (!item) return '';
        return `<div class="row"><div><div class="row-title">${escapeHtml(item.label || key)}</div><div class="row-sub">${escapeHtml(item.detail || item.name || '-')}</div></div><span class="pill ${tone(item)}">${escapeHtml(stateText(item))}</span><span class="signal">${escapeHtml(item.name || '-')}</span></div>`;
      }).filter(Boolean), '暂无服务摘要');
    }

    function renderStatus(data) {
      APP_STATE.status = data;
      const sys = data.system || {};
      const services = data.services || {};
      appendTraffic(data.network_stats || {});
      renderTrafficChart();
      renderIndicators(data);
      renderServiceGrid(services);
      renderHomeServiceSummary(services);
      setText('home-online-state', data.uplink ? '已联网' : '离线');
      setText('home-online-sub', data.uplink ? connectivityText(data.connectivity) : '外网不可用');
      setText('home-device-count', (data.active_connections || []).length);
      setText('home-device-sub', `${data.saved_wifi_count || 0} 个已保存 Wi-Fi`);
      setText('home-ip-address', data.ip4 || '无地址');
      setHtml('home-dns-list', (data.dns_servers || []).length ? data.dns_servers.map((item) => `<span>${escapeHtml(item)}</span>`).join('') : '<span>未获取到 DNS</span>');
      setText('home-hotspot-line', data.hotspot_active ? `开启 · ${data.hotspot_ssid}` : '未开启');
      setText('home-cpu-text', sys.cpu_percent_text || '-');
      setText('home-disk-root-text', `${sys.disk_used || '-'} / ${sys.disk_total || '-'}`);
      setText('home-memory-text', `${sys.memory_used || '-'} / ${sys.memory_total || '-'}`);
      const cpuBar = document.getElementById('home-cpu-bar');
      const diskBar = document.getElementById('home-disk-root-bar');
      const memoryBar = document.getElementById('home-memory-bar');
      if (cpuBar) cpuBar.style.width = `${Math.max(0, Math.min(100, Number(sys.cpu_percent || 0)))}%`;
      if (diskBar) diskBar.style.width = `${percent(sys.disk_used_bytes || 0, sys.disk_total_bytes || 0)}%`;
      if (memoryBar) memoryBar.style.width = `${percent(sys.memory_used_bytes || 0, sys.memory_total_bytes || 0)}%`;
      const coreSummary = (sys.cpu_cores || []).map((item) => `${item.name} ${item.text}`).join(' · ');
      const tempSummary = (sys.temperatures || []).map((item) => `${item.name} ${item.text}`).join(' · ');
      const maxTemp = (sys.temperatures || []).reduce((max, item) => Math.max(max, Number(item.celsius || 0)), 0);
      renderRows('home-extra-resources', [
        coreSummary ? `<div class="row"><div><div class="row-title">多核占用</div><div class="row-sub">${escapeHtml(coreSummary)}</div></div><span class="pill">${escapeHtml(String((sys.cpu_cores || []).length))} cores</span><span class="signal">${escapeHtml(sys.cpu_percent_text || '-')}</span></div>` : '',
        tempSummary ? `<div class="row"><div><div class="row-title">温度信息</div><div class="row-sub">${escapeHtml(tempSummary)}</div></div><span class="pill warn">temp</span><span class="signal">${maxTemp ? `${maxTemp.toFixed(1)}°C` : '-'}</span></div>` : ''
      ].filter(Boolean), '未读取到多核或温度信息');
      setHtml('home-overview', [
        `<div class="kv-row"><span>主机名</span><strong>${escapeHtml(data.hostname || '-')}</strong></div>`,
        `<div class="kv-row"><span>接口</span><strong>${escapeHtml(data.wifi_interface || '-')}</strong></div>`,
        `<div class="kv-row"><span>运行时长</span><strong>${escapeHtml(sys.uptime || '-')}</strong></div>`,
        `<div class="kv-row"><span>系统负载</span><strong>${escapeHtml(sys.load || '-')}</strong></div>`,
        `<div class="kv-row"><span>连通性</span><strong>${escapeHtml(connectivityText(data.connectivity))}</strong></div>`,
        `<div class="kv-row"><span>热点 SSID</span><strong>${escapeHtml(data.hotspot_ssid || '-')}</strong></div>`
      ].join(''));
      setText('home-message', data.last_message || '状态正常');
      setText('home-hint', data.hotspot_active ? `热点广播中：${data.hotspot_ssid}` : (data.uplink ? '外网已恢复，热点已停用。' : `外网离线时会自动回退到热点 ${data.hotspot_ssid}`));
      setHtml('network-overview', [
        `<div class="kv-row"><span>接口名</span><strong>${escapeHtml(data.wifi_interface || '-')}</strong></div>`,
        `<div class="kv-row"><span>当前 IPv4</span><strong>${escapeHtml(data.ip4 || '无地址')}</strong></div>`,
        `<div class="kv-row"><span>连通性</span><strong>${escapeHtml(connectivityText(data.connectivity))}</strong></div>`,
        `<div class="kv-row"><span>已保存 Wi-Fi</span><strong>${escapeHtml(String(data.saved_wifi_count || 0))}</strong></div>`
      ].join(''));
      setText('network-hint', data.hotspot_active ? `热点开启中：${data.hotspot_ssid}` : (data.uplink ? '外网正常。' : '外网离线。'));
    }

    function fillForm(ssid) {
      document.getElementById('ssid').value = ssid || '';
      showViewByName('network');
      activateTopTab('network', 'wifi');
    }

    function renderNetworks(data) {
      setText('network-note', data.message || '扫描结果如下');
      renderRows('network-list', (data.networks || []).map((item) => `<div class="row"><div><div class="row-title">${escapeHtml(item.ssid || '(隐藏网络)')}</div><div class="row-sub">${escapeHtml(item.security || '开放网络')}${item.in_use ? ' · 当前连接' : ''}</div></div><span class="signal">${escapeHtml(item.signal || '?')}%</span><button class="btn soft" onclick='fillForm(${JSON.stringify(item.ssid || '')})'>填入</button></div>`), '暂无扫描结果');
    }

    function renderSavedWifi(data) {
      renderRows('saved-wifi-list', (data.profiles || []).map((item) => `<div class="row"><div><div class="row-title">${escapeHtml(item.name || '(未命名)')}</div><div class="row-sub">UUID: ${escapeHtml(item.uuid || '-')}<br>${item.active ? '当前活动连接' : '未连接'} · 自动连接 ${escapeHtml(item.autoconnect || '-')}</div></div><div class="row-actions"><button class="btn soft" onclick='activateSavedWifi(${JSON.stringify(item.uuid || '')})'>重连</button><button class="btn danger" onclick='deleteSavedWifi(${JSON.stringify(item.uuid || '')}, ${JSON.stringify(item.name || '')})'>删除</button></div><span class="signal">${item.active ? '已连接' : '空闲'}</span></div>`), '当前没有保存的 Wi-Fi 配置');
    }

    async function refreshStatus() { renderStatus(await api('/api/status')); }
    async function loadNetworks() { renderNetworks(await api('/api/networks')); }
    async function loadSavedWifi() { renderSavedWifi(await api('/api/wifi/saved')); }
    async function toggleHotspot(action) {
      const data = await api('/api/hotspot', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
      setText('network-log', data.message || '操作完成');
      setTimeout(refreshAll, 600);
    }
    async function connectWifi() {
      const payload = { ssid: document.getElementById('ssid').value.trim(), password: document.getElementById('password').value, hidden: document.getElementById('hidden').checked };
      const data = await api('/api/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      setText('network-log', data.message || '已提交连接');
      setTimeout(refreshAll, 1200);
    }
    async function activateSavedWifi(uuid) {
      const data = await api('/api/wifi/activate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ uuid }) });
      setText('network-log', data.message || '已请求重连');
      setTimeout(refreshAll, 1200);
    }
    async function deleteSavedWifi(uuid, name) {
      if (!confirm(`确认删除 Wi-Fi 配置：${name || uuid}？`)) return;
      const data = await api('/api/wifi/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ uuid }) });
      setText('network-log', data.message || '已删除配置');
      await loadSavedWifi();
      await refreshStatus();
    }

    function openClashLogCommand() {
      return serviceLogCommand('proxy');
    }

    function updateOpenClashSummary() {
      if (!APP_STATE.openclash) return;
      const activeSub = APP_STATE.openclashSubscriptions.active || '未启用';
      const groups = APP_STATE.loaded.proxies ? APP_STATE.proxyGroups.length : '-';
      setText('oc-message', `${APP_STATE.openclash.active ? '服务运行中' : '服务已停止'} · 当前订阅 ${activeSub} · 策略组 ${groups}`);
    }

    function renderOpenClash(data) {
      APP_STATE.openclash = data;
      setHtml('oc-overview', [
        `<div class="kv-row"><span>服务状态</span><strong>${escapeHtml(data.active ? '运行中' : (data.installed ? '已停止' : '未安装'))}</strong></div>`,
        `<div class="kv-row"><span>服务名</span><strong>${escapeHtml(data.service || '-')}</strong></div>`,
        `<div class="kv-row"><span>已启用</span><strong>${escapeHtml(data.installed ? (data.enabled ? 'yes' : 'no') : '-')}</strong></div>`,
        `<div class="kv-row"><span>监听端口</span><strong>${escapeHtml((data.ports || []).map((item) => item.port).join(', ') || '-')}</strong></div>`
      ].join(''));
      renderRows('oc-ports', (data.ports || []).map((item) => `<div class="row"><div><div class="row-title">端口 ${escapeHtml(item.port)}</div><div class="row-sub">${escapeHtml(item.listen)}</div></div><span class="pill">listen</span><span class="signal">${escapeHtml(data.name || '-')}</span></div>`), '当前未检测到监听端口');
      setText('oc-log-source', data.service || 'journalctl');
      updateOpenClashSummary();
    }

    function renderOpenClashConfig(data, preserve) {
      APP_STATE.openclashConfig = data;
      APP_STATE.loaded.config = true;
      setText('oc-config-path', data.path || '-');
      setText('oc-config-meta', `${(data.content || '').split(/\r?\n/).length} 行 · ${(data.content || '').length} 字符`);
      const editor = document.getElementById('oc-config-editor');
      if (editor && (!preserve || !editor.value)) editor.value = data.content || '';
    }

    function clearSubscriptionForm() {
      document.getElementById('oc-sub-name').value = '';
      document.getElementById('oc-sub-url').value = '';
      setText('oc-sub-log', '订阅表单已清空');
    }

    function editSubscription(index) {
      const item = APP_STATE.openclashSubscriptions.items[index];
      if (!item) return;
      document.getElementById('oc-sub-name').value = item.name || '';
      document.getElementById('oc-sub-url').value = item.url || '';
      setText('oc-sub-log', `已载入订阅 ${item.name}，保存会覆盖同名订阅。`);
    }

    function renderOpenClashSubscriptions(data) {
      APP_STATE.openclashSubscriptions = { active: data.active || '', items: data.items || [] };
      APP_STATE.loaded.subscriptions = true;
      renderRows('oc-subscriptions', APP_STATE.openclashSubscriptions.items.map((item, index) => `<div class="row"><div><div class="row-title">${escapeHtml(item.name)} ${item.active ? '<span class="pill ok">启用中</span>' : ''}</div><div class="row-sub">${escapeHtml(item.url || '-')}<br>文件：${escapeHtml(item.path || '-')}<br>更新时间：${escapeHtml(formatTime(item.updated_at))}</div></div><div class="row-actions"><button class="btn soft" onclick="editSubscription(${index})">编辑</button><button class="btn soft" onclick="refreshSubscription(${index})">刷新</button>${item.active ? '' : `<button class="btn ok" onclick="activateSubscription(${index})">启用</button>`}<button class="btn danger" onclick="deleteSubscription(${index})">删除</button></div><span class="signal">${item.active ? '已启用' : (item.exists ? '就绪' : '缺失')}</span></div>`), '当前没有订阅，先在右侧添加订阅地址');
      updateOpenClashSummary();
    }

    function renderOpenClashProxies(data) {
      APP_STATE.proxyGroups = data.groups || [];
      APP_STATE.loaded.proxies = true;
      renderRows('oc-groups', APP_STATE.proxyGroups.map((item, index) => `<div class="row"><div><div class="row-title">${escapeHtml(item.name)}</div><div class="row-sub">${escapeHtml(item.type || '-')} · 当前 ${escapeHtml(item.current || '-')} · ${escapeHtml(String((item.options || []).length))} 个节点</div></div><div class="row-actions"><select id="oc-group-select-${index}" style="width:220px;">${(item.options || []).map((option) => `<option value="${escapeHtml(option)}" ${option === item.current ? 'selected' : ''}>${escapeHtml(option)}</option>`).join('')}</select><button class="btn primary" onclick="switchProxy(${index})">切换</button></div><span class="signal">${escapeHtml(item.current || '-')}</span></div>`), '当前没有策略组。通常是还没导入订阅，或者 controller 还没起来。');
      updateOpenClashSummary();
    }

    async function loadOpenClash() { renderOpenClash(await api('/api/openclash/status')); }
    async function loadOpenClashConfig(notify) {
      const data = await api('/api/openclash/config');
      renderOpenClashConfig(data, false);
      if (notify) setText('oc-message', `已读取配置：${data.path}`);
    }
    async function loadOpenClashSubscriptions() { renderOpenClashSubscriptions(await api('/api/openclash/subscriptions')); }
    async function loadOpenClashProxies() { renderOpenClashProxies(await api('/api/openclash/proxies')); }
    async function loadOpenClashLog() {
      const data = await api('/api/console', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: openClashLogCommand() }) });
      APP_STATE.loaded.log = true;
      setText('oc-log-output', `[exit=${data.exit_code}${data.timeout ? ', timeout' : ''}]\n${data.output || '(no output)'}`);
    }
    async function openClashAction(action) {
      const data = await api('/api/openclash/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
      setText('oc-message', data.message || '服务操作完成');
      await loadOpenClash();
      await refreshStatus();
      setTimeout(loadOpenClashLog, 800);
    }
    async function saveOpenClash(restart) {
      const data = await api('/api/openclash/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: document.getElementById('oc-config-editor').value, restart }) });
      setText('oc-message', data.message || '配置已保存');
      await loadOpenClashConfig(false);
      if (restart) {
        await loadOpenClash();
        setTimeout(loadOpenClashLog, 800);
      }
    }
    async function saveSubscription(activate) {
      const payload = { name: document.getElementById('oc-sub-name').value.trim(), url: document.getElementById('oc-sub-url').value.trim(), activate };
      const data = await api('/api/openclash/subscription', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      setText('oc-sub-log', data.message || '订阅已保存');
      await loadOpenClashSubscriptions();
      await loadOpenClash();
      await loadOpenClashConfig(false);
      if (activate) setTimeout(loadOpenClashProxies, 800);
    }
    async function refreshSubscription(index) {
      const item = APP_STATE.openclashSubscriptions.items[index];
      if (!item) return;
      const data = await api('/api/openclash/subscription/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: item.name }) });
      setText('oc-sub-log', data.message || '订阅已刷新');
      await loadOpenClashSubscriptions();
      await loadOpenClash();
      setTimeout(loadOpenClashProxies, 800);
    }
    async function activateSubscription(index) {
      const item = APP_STATE.openclashSubscriptions.items[index];
      if (!item) return;
      const data = await api('/api/openclash/subscription/activate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: item.name }) });
      setText('oc-sub-log', data.message || '订阅已启用');
      await loadOpenClashSubscriptions();
      await loadOpenClash();
      await loadOpenClashConfig(false);
      setTimeout(loadOpenClashProxies, 800);
    }
    async function deleteSubscription(index) {
      const item = APP_STATE.openclashSubscriptions.items[index];
      if (!item || !confirm(`确认删除订阅：${item.name}？`)) return;
      const data = await api('/api/openclash/subscription/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: item.name }) });
      setText('oc-sub-log', data.message || '订阅已删除');
      await loadOpenClashSubscriptions();
    }
    async function switchProxy(index) {
      const group = APP_STATE.proxyGroups[index];
      const select = document.getElementById(`oc-group-select-${index}`);
      if (!group || !select) return;
      const data = await api('/api/openclash/proxy/select', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ group: group.name, name: select.value }) });
      setText('oc-node-log', data.message || '已切换节点');
      await loadOpenClashProxies();
    }

    function setConsoleCommand(command) {
      APP_STATE.sshCommand = command || '';
      setText('ssh-command-suggestion', APP_STATE.sshCommand || '无');
      showViewByName('console');
    }
    function clearSuggestedCommand() {
      APP_STATE.sshCommand = '';
      setText('ssh-command-suggestion', '无');
    }
    async function copySuggestedCommand() {
      if (!APP_STATE.sshCommand) return;
      await navigator.clipboard.writeText(APP_STATE.sshCommand);
    }
    function webSshUrl() {
      return `${location.protocol}//${location.hostname}:7681/`;
    }
    function loadWebSSH(force) {
      const frame = document.getElementById('webssh-frame');
      if (!frame) return;
      frame.src = force ? `${webSshUrl()}?t=${Date.now()}` : webSshUrl();
      APP_STATE.loaded.webssh = true;
    }
    function openWebSSHWindow() {
      window.open(webSshUrl(), '_blank', 'noopener');
    }
    async function serviceAction(service, action) {
      const data = await api('/api/service/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ service, action }) });
      setText('home-message', data.message || '服务操作完成');
      await refreshStatus();
      if (service === 'proxy') await loadOpenClash();
    }
    function serviceLog(service) {
      setConsoleCommand(serviceLogCommand(service));
    }

    function formatChipName(chip) {
      const match = String(chip || '').match(/gpiochip(\d+)/i);
      return `GPIO ${match ? match[1] : (chip || '-')}`;
    }
    function updateGpioRangeHint() {
      const select = document.getElementById('gpio-chip');
      const option = select && select.selectedOptions && select.selectedOptions[0];
      if (!option) {
        setText('gpio-range-hint', '请选择芯片后再填写线号偏移。');
        return;
      }
      const lines = Number(option.dataset.lines || 0);
      setText('gpio-range-hint', `${formatChipName(option.value)} 的有效范围：0 到 ${Math.max(0, lines - 1)}`);
    }
    function renderGPIO(data) {
      const select = document.getElementById('gpio-chip');
      if (!data.available) {
        select.innerHTML = '<option value="">未安装 gpiod</option>';
        setText('gpio-range-hint', '系统未安装 gpiod。');
        renderRows('gpio-list', [], '系统未安装 gpiod，GPIO 功能不可用。');
        return;
      }
      const chips = data.chips || [];
      const current = select.value;
      select.innerHTML = chips.map((item) => `<option value="${escapeHtml(item.chip)}" data-lines="${escapeHtml(String(item.lines))}">${escapeHtml(formatChipName(item.chip))} · ${escapeHtml(String(item.lines))} 线</option>`).join('');
      if (current && chips.some((item) => item.chip === current)) select.value = current;
      updateGpioRangeHint();
      const holds = new Map((data.holds || []).map((item) => [`${item.chip}:${item.line}`, item.value]));
      renderRows('gpio-list', chips.map((item) => {
        const active = [];
        holds.forEach((value, key) => { if (key.startsWith(`${item.chip}:`)) active.push(`${key.split(':')[1]}=${value}`); });
        return `<div class="row"><div><div class="row-title">${escapeHtml(formatChipName(item.chip))}</div><div class="row-sub">${escapeHtml(String(item.lines))} 线${active.length ? ` · 保持中：${escapeHtml(active.join(', '))}` : ''}</div></div><span class="pill ${active.length ? 'ok' : ''}">${active.length ? '占用中' : '空闲'}</span><span class="signal">0-${Math.max(0, Number(item.lines || 0) - 1)}</span></div>`;
      }), '未检测到 GPIO 芯片');
    }
    async function refreshGPIO() { renderGPIO(await api('/api/gpio/chips')); }
    function gpioPayload() {
      return { chip: document.getElementById('gpio-chip').value, line: document.getElementById('gpio-line').value.trim() };
    }
    function validateGpioPayload() {
      const payload = gpioPayload();
      if (!payload.chip) throw new Error('请选择 GPIO 芯片');
      if (payload.line === '') throw new Error('请输入线号偏移');
      const select = document.getElementById('gpio-chip');
      const option = select && select.selectedOptions && select.selectedOptions[0];
      if (!option || option.value !== payload.chip) throw new Error('未找到对应的 GPIO 芯片');
      const lineValue = Number(payload.line);
      if (!Number.isInteger(lineValue) || lineValue < 0) throw new Error('线号偏移必须是非负整数');
      const maxLine = Math.max(0, Number(option.dataset.lines || 0) - 1);
      if (lineValue > maxLine) throw new Error(`${formatChipName(payload.chip)} 的有效范围是 0 到 ${maxLine}`);
      return payload;
    }
    async function readGPIO() {
      const data = await api('/api/gpio/read', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(validateGpioPayload()) });
      setText('gpio-log', `GPIO ${data.chip}:${data.line} 当前值 ${data.value}${data.held ? '，由面板保持中' : ''}`);
    }
    async function writeGPIO(value) {
      const payload = validateGpioPayload();
      payload.value = value;
      const data = await api('/api/gpio/write', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      setText('gpio-log', `GPIO ${data.chip}:${data.line} 已保持为 ${data.value}`);
      await refreshGPIO();
    }
    async function releaseGPIO() {
      const data = await api('/api/gpio/release', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(validateGpioPayload()) });
      setText('gpio-log', data.released ? `GPIO ${data.chip}:${data.line} 已释放` : `GPIO ${data.chip}:${data.line} 当前没有被面板保持`);
      await refreshGPIO();
    }

    function showOcTab(name, button, fromTop) {
      APP_STATE.activeTabs.openclash = name;
      if (!fromTop) renderTabs('openclash');
      document.querySelectorAll('.subtabs [data-oc-tab]').forEach((el) => el.classList.toggle('active', el.dataset.ocTab === name));
      document.querySelectorAll('#view-openclash .subview').forEach((el) => el.classList.toggle('active', el.id === `oc-tab-${name}`));
      if (button) button.classList.add('active');
      if (name === 'subscriptions' && !APP_STATE.loaded.subscriptions) loadOpenClashSubscriptions().catch(handlePageError);
      if (name === 'nodes' && !APP_STATE.loaded.proxies) loadOpenClashProxies().catch(handlePageError);
      if (name === 'config' && !APP_STATE.loaded.config) loadOpenClashConfig(false).catch(handlePageError);
      if (name === 'log' && !APP_STATE.loaded.log) loadOpenClashLog().catch(handlePageError);
    }

    async function refreshAll() {
      await Promise.all([refreshStatus(), loadNetworks(), loadSavedWifi(), loadOpenClash(), refreshGPIO()]);
      if (!APP_STATE.loaded.config) loadOpenClashConfig(false).catch(() => {});
      if (!APP_STATE.loaded.subscriptions) loadOpenClashSubscriptions().catch(() => {});
    }

    function handlePageError(error) {
      const message = messageOf(error);
      ['home-message', 'network-log', 'oc-message', 'oc-sub-log', 'oc-node-log', 'gpio-log', 'oc-log-output'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = message;
      });
    }

    document.querySelectorAll('.nav button').forEach((button) => button.addEventListener('click', () => showView(button.dataset.view, button)));
    document.getElementById('menu_search').addEventListener('input', (event) => filterMenu(event.target.value));
    window.addEventListener('unhandledrejection', (event) => handlePageError(event.reason));
    window.addEventListener('error', (event) => handlePageError(event.error || event.message));

    renderTabs('home');
    renderTrafficChart();
    showViewByName('home');
    refreshAll().catch(handlePageError);
  
