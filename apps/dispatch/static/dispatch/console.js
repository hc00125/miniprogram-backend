/* Standalone staff console. No client wallet calls or third-party scripts. */
(function (root) {
  'use strict';
  function submissionController(io) {
    let pending = io.load() || null;
    let busy = false;
    const clear = () => { io.save(null); pending = null; };
    async function check() {
      if (!pending) throw new Error('没有待查询请求');
      const result = await io.query(pending.request_key);
      if (!result || typeof result.order_no !== 'string' || !result.order_no) throw new Error('订单结果格式异常，请保留原请求查询');
      clear(); return result;
    }
    async function submit(payload) {
      if (busy) throw new Error('正在处理，请勿重复点击');
      if (pending && JSON.stringify(pending) !== JSON.stringify(payload)) throw new Error('原请求尚未核实，不能创建新单');
      busy = true;
      try {
        // Persist before dispatch; on timeout only query/retry this exact intent.
        if (!pending) { io.save(payload); pending = payload; }
        let failure;
        try { await io.send(pending); } catch (error) { failure = error; }
        try { return await check(); } catch (error) {
          if (failure && [400, 409, 422].includes(failure.status) && error.status === 404) { clear(); throw failure; }
          throw new Error('发单结果待确认。请查询原请求或原请求重试，不要重新录单。');
        }
      } finally { busy = false; }
    }
    return { submit, check, retry: () => submit(pending), current: () => pending, busy: () => busy };
  }
  function cancellationController(io) {
    let busy = false;
    async function submit(reason) {
      if (busy) throw new Error('正在取消，请勿重复点击');
      reason = String(reason || '').trim();
      if (!reason || reason.length > 200) throw new Error('请填写1至200字的取消原因');
      busy = true;
      try {
        let failure;
        try { await io.send(reason); } catch (error) { failure = error; }
        let order;
        try { order = await io.query(); } catch (_) { /* Never infer success from a POST or timeout. */ }
        if (order && order.order_no === io.orderNo && order.status === '已取消') return order;
        if (failure && [400, 403, 404, 409].includes(failure.status)) throw failure;
        throw new Error('取消结果待确认，请重新打开订单详情核实；不要按已退款处理。');
      } finally { busy = false; }
    }
    return { submit };
  }
  function message(data) {
    if (typeof data === 'string') return data;
    if (Array.isArray(data)) return data.map(message).join('；');
    if (data && typeof data === 'object') return message(data.detail || Object.values(data));
    return '请求失败，请稍后重试';
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { submissionController, cancellationController, message };
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  const element = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const notice = (text, error) => { $('notice').textContent = text; $('notice').className = error ? 'error' : ''; $('notice').hidden = false; };
  async function api(path, body) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch('/dispatch/api/' + path, { method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: body ? { 'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value } : {}, body: body ? JSON.stringify(body) : undefined });
      let data; try { data = await response.json(); } catch (_) { data = { detail: '服务暂时不可用，请查询原请求，不要重复发单' }; }
      if (!response.ok) { const error = new Error(response.status === 403 ? '登录失效或权限不足，请重新登录；未确认的发单请求会保留' : message(data)); error.status = response.status; throw error; }
      return data;
    } finally { clearTimeout(timer); }
  }
  const run = handler => async event => { if (event) event.preventDefault(); try { await handler(event); } catch (error) { notice(error.message || '操作失败', true); } };
  const uid = () => { if (crypto.randomUUID) return crypto.randomUUID(); const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128; const s = Array.from(bytes, n => n.toString(16).padStart(2, '0')).join(''); return s.slice(0, 8) + '-' + s.slice(8, 12) + '-' + s.slice(12, 16) + '-' + s.slice(16, 20) + '-' + s.slice(20); };
  document.addEventListener('DOMContentLoaded', () => {
    if (!$('publish')) return;
    let selected = null, packages = [], quoted = null, quoteBody = '', quoteGeneration = 0, customerGeneration = 0, page = 1, pages = 1, sending = false;
    const storageKey = 'dispatch-intent-' + document.body.dataset.operator;
    const ctl = submissionController({ load: () => { try { return JSON.parse(sessionStorage.getItem(storageKey)); } catch (_) { return null; } },
      save: value => value ? sessionStorage.setItem(storageKey, JSON.stringify(value)) : sessionStorage.removeItem(storageKey),
      send: body => api('orders/', body), query: key => api('submissions/' + encodeURIComponent(key) + '/') });
    function controls() { const pending = !!ctl.current(); $('entry-fields').disabled = pending || sending; $('pending').hidden = !pending; $('receipt-note').disabled = pending || sending; $('received').disabled = pending || sending; $('publish').disabled = pending || sending || !quoted || !$('received').checked || !$('receipt-note').value.trim(); $('check-submission').disabled = sending; $('retry-submission').disabled = sending; }
    function invalidate() { quoteGeneration++; quoted = null; $('amount').textContent = '未核价'; $('quote-summary').textContent = '内容有变化，请重新核价'; $('received').checked = false; controls(); }
    function choose(customer) { selected = customer; $('selected-customer').hidden = false; $('selected-customer').textContent = customer.nickname + ' · ' + (customer.id ? '客户编号 ' + customer.id : '已有小程序账号') + ' · ' + (customer.bound ? '已绑定' : '未绑定'); $('customer-results').replaceChildren(); $('new-customer').hidden = true; invalidate(); }
    async function searchCustomers() {
      const generation = ++customerGeneration;
      const response = await api('customers/?q=' + encodeURIComponent($('customer-search').value.trim()));
      if (generation !== customerGeneration) return;
      $('customer-results').replaceChildren();
      response.results.forEach(customer => { const button = element('button', null, 'customer-item'); button.append(element('strong', customer.nickname), element('small', (customer.id ? '#' + customer.id + ' · ' : '') + (customer.bound ? '已绑定' : '未绑定') + ' · ' + (customer.note || '无内部备注'))); button.onclick = () => choose(customer); $('customer-results').append(button); });
      if (!response.results.length) $('customer-results').append(element('p', '没有找到，可新增老板档案。', 'hint'));
    }
    $('search-customer').onclick = run(searchCustomers);
    $('customer-search').addEventListener('keydown', event => { if (event.key === 'Enter') run(searchCustomers)(event); });
    $('show-new').onclick = () => { $('new-customer').hidden = !$('new-customer').hidden; $('new-name').value = $('customer-search').value.trim(); };
    $('save-customer').onclick = run(async () => {
      const nickname = $('new-name').value.trim(), note = $('new-note').value.trim(); if (!nickname) throw new Error('请填写老板称呼');
      $('save-customer').disabled = true;
      try {
        const found = await api('customers/?q=' + encodeURIComponent(nickname));
        if (found.results.some(c => c.nickname === nickname) && !confirm('存在同名客户。请先核对，确认是另一位老板才继续新建。')) return;
        const customer = await api('customers/', { nickname, note }); choose(customer); notice('客户档案已建立，编号 ' + customer.id + '。以后派单选择此档案即可。');
      } finally { $('save-customer').disabled = false; }
    });
    function packageChange() {
      const p = packages.find(item => String(item.id) === $('package').value);
      $('spec').replaceChildren();
      if (!p) { $('spec').append(new Option('先选择商品', '')); $('players').textContent = '由商品自动确定'; invalidate(); return; }
      $('spec').append(new Option(p.specs.length ? '请选择规格' : '默认规格', ''));
      p.specs.forEach(spec => $('spec').append(new Option(spec.name + ' · ¥' + spec.price_yuan, String(spec.id))));
      if (p.specs.length === 1) $('spec').value = String(p.specs[0].id);
      $('players').textContent = p.players + ' 人（由商品确定）'; $('hours').max = p.max_hours; $('hours').value = '1'; $('hours').disabled = p.max_hours === 1;
      $('hours-hint').textContent = p.max_hours === 1 ? '该商品按单收费，固定 1 份' : '小时制商品 1–24 小时'; invalidate();
    }
    $('package').onchange = packageChange;
    ['spec', 'hours', 'game-id', 'boss-note'].forEach(id => $(id).addEventListener('input', invalidate));
    ['receipt-note', 'received'].forEach(id => $(id).addEventListener('input', controls));
    function payload() { if (!selected) throw new Error('请先搜索并选择老板'); if (!$('package').value) throw new Error('请选择商品'); const hours = Number($('hours').value); if (!Number.isInteger(hours) || hours < 1 || hours > Number($('hours').max)) throw new Error('请填写有效整数时长'); return { customer_ref: selected.ref, package_id: Number($('package').value), spec_id: $('spec').value ? Number($('spec').value) : null, hours, game_id: $('game-id').value.trim(), boss_note: $('boss-note').value.trim() }; }
    $('quote').onclick = run(async () => { const data = payload(), generation = ++quoteGeneration; $('quote').disabled = true; try { const result = await api('quote/', data); if (generation !== quoteGeneration || JSON.stringify(data) !== JSON.stringify(payload())) return; quoted = result; quoteBody = JSON.stringify(data); $('amount').textContent = '¥' + result.total_amount_yuan; $('quote-summary').textContent = result.required_players + ' 人 · ' + result.hours + ' 小时 / 份 · 线下收款'; $('received').checked = false; controls(); } finally { $('quote').disabled = false; } });
    function completed(result) { invalidate(); $('receipt-note').value = ''; notice('派单成功：' + result.order_no + (result.customer_id ? '\n客户编号：' + result.customer_id : '') + '\n已进入小程序抢单大厅，不用在旧系统重复录单。'); controls(); }
    $('publish').onclick = run(async () => {
      if (sending) return;
      const data = payload(); if (!quoted || quoteBody !== JSON.stringify(data)) throw new Error('内容已变化，请重新核价');
      if (!$('received').checked || !$('receipt-note').value.trim()) throw new Error('请核实线下收款并填写收款记录');
      if (!confirm('为「' + selected.nickname + '」派单，总额 ¥' + quoted.total_amount_yuan + '。\n确认已在线下收款？本操作不会扣钻石。')) return;
      sending = true; controls();
      try { completed(await ctl.submit({ ...data, quote_version: quoted.quote_version, received: true, receipt_note: $('receipt-note').value.trim(), request_key: uid() })); }
      finally { sending = false; controls(); }
    });
    $('check-submission').onclick = run(async () => { if (sending) return; sending = true; controls(); try { completed(await ctl.check()); } finally { sending = false; controls(); } });
    $('retry-submission').onclick = run(async () => { if (sending) return; sending = true; controls(); try { completed(await ctl.retry()); } finally { sending = false; controls(); } });
    async function showOrder(number) {
      const o = await api('orders/' + encodeURIComponent(number) + '/'); $('detail-content').replaceChildren();
      const lines = ['订单号：' + o.order_no, '老板：' + o.nickname + (o.customer_id ? ' / 客户编号 ' + o.customer_id : ''), '商品：' + o.package_name + ' / ' + o.spec_name,
        '金额：¥' + o.amount_yuan + (o.source === 'staff' ? '（线下已收款，不扣钻石）' : '（人民币等值）'), '状态：' + o.status, '服务：' + o.required_players + ' 人 / ' + o.hours + ' 小时或份',
        '阵容：' + (o.players.join('、') || '等待接单'), '房间 / 游戏：' + o.game_id, '服务备注：' + o.boss_note, '收款记录（内部）：' + (o.receipt_note || '无'), '发单人：' + o.created_by];
      lines.forEach(text => $('detail-content').append(element('div', text, 'detail-line')));
      (o.logs || []).forEach(log => $('detail-content').append(element('p', new Date(log.created_at).toLocaleString() + ' · ' + log.reason, 'hint')));
      if (o.source === 'staff') {
        const section = element('section', null, 'inset');
        section.append(element('h3', '取消派单'), element('p', '仅支持未开打的订单。取消后停止接单/服务；不会自动退钻石，线下退款须原客服单独核实处理。', 'hint'));
        const feedback = element('p', o.cancel_block_reason || '', 'hint'); feedback.id = 'cancel-feedback'; feedback.setAttribute('role', 'status');
        const label = element('label', '取消原因（必填）'), reason = element('textarea');
        reason.id = 'cancel-reason'; reason.maxLength = 200; reason.placeholder = '例如：老板临时取消、重复派单'; reason.disabled = !o.can_cancel_dispatch;
        const button = element('button', '取消派单'); button.id = 'cancel-dispatch'; button.type = 'button'; button.disabled = !o.can_cancel_dispatch;
        const cancel = cancellationController({ orderNo: o.order_no,
          send: text => api('orders/' + encodeURIComponent(o.order_no) + '/cancel/', { reason: text }),
          query: () => api('orders/' + encodeURIComponent(o.order_no) + '/') });
        button.onclick = async () => {
          const text = reason.value.trim();
          if (!text || text.length > 200) { feedback.textContent = '请填写1至200字的取消原因'; feedback.className = 'error'; reason.focus(); return; }
          if (!confirm('确认取消「' + o.nickname + '」的订单 ' + o.order_no + '（¥' + o.amount_yuan + '）？\n仅取消派单，不会自动退款。线下退款仍须原客服核实处理。')) return;
          button.disabled = reason.disabled = true; feedback.textContent = '正在取消并核实订单状态…'; feedback.className = 'hint';
          try {
            await cancel.submit(text);
            notice('已取消派单：' + o.order_no + '。未退钻石，线下退款未确认，请原客服单独处理。');
            await showOrder(o.order_no); await loadOrders();
          } catch (error) { feedback.textContent = error.message || '取消结果待确认，请重新查询订单'; feedback.className = 'error'; }
          finally { button.disabled = reason.disabled = !o.can_cancel_dispatch; }
        };
        if (o.cancel_reason) section.append(element('p', '取消原因：' + o.cancel_reason));
        label.append(reason); section.append(label, feedback, button); $('detail-content').append(section);
      }
      if (!$('detail-dialog').open) $('detail-dialog').showModal();
    }
    $('close-detail').onclick = () => $('detail-dialog').close();
    async function loadOrders() {
      const params = new URLSearchParams({ q: $('order-search').value.trim(), source: $('order-source').value, status: $('order-status').value, mine: $('mine').checked ? '1' : '', page: String(page) });
      const response = await api('orders/?' + params); page = response.page; pages = response.pages; $('order-rows').replaceChildren();
      response.results.forEach(o => { const tr = element('tr'); const cells = [
        [o.nickname + (o.customer_id ? ' · #' + o.customer_id : ''), o.order_no], [o.package_name, o.spec_name + ' · ' + o.hours + '小时/份'],
        ['¥' + o.amount_yuan, o.source === 'staff' ? '客服代派 · 线下已收款' : '老板自助 · 人民币等值'], [o.status, o.players.join('、') || '等待接单'], [o.created_by, new Date(o.created_at).toLocaleString()]];
        cells.forEach(([title, sub]) => { const td = element('td', title); td.append(element('small', sub)); tr.append(td); });
        const td = element('td'), btn = element('button', '详情'); btn.onclick = run(() => showOrder(o.order_no)); td.append(btn); tr.append(td); $('order-rows').append(tr); });
      if (!response.results.length) { const tr = element('tr'), td = element('td', '没有符合条件的订单'); td.colSpan = 6; tr.append(td); $('order-rows').append(tr); }
      $('page-info').textContent = '共 ' + response.count + ' 单 · ' + page + '/' + pages + ' 页'; $('prev').disabled = page <= 1; $('next').disabled = page >= pages;
    }
    $('refresh-orders').onclick = run(() => { page = 1; return loadOrders(); }); $('prev').onclick = run(() => { page--; return loadOrders(); }); $('next').onclick = run(() => { page++; return loadOrders(); });
    async function loadClaims() {
      const response = await api('claims/'); $('claim-rows').replaceChildren();
      response.results.forEach(c => {
        const card = element('article', null, 'card'); card.append(element('h2', '申请 #' + c.id + ' · 客户编号 ' + c.customer_id));
        const meta = element('div', null, 'claim-meta'); const left = element('div'); left.append(element('strong', '原档案：' + c.customer_name), element('p', '内部备注：' + (c.customer_note || '无'), 'hint'));
        const right = element('div'); right.append(element('strong', '申请账号：' + c.applicant_name + '（用户#' + c.applicant_user_id + '）'), element('p', '申请说明：' + c.message)); meta.append(left, right); card.append(meta);
        const note = element('textarea'); note.maxLength = 300; note.placeholder = '填写核实依据或驳回原因（内部记录）';
        const label = element('label', null, 'check'), checked = element('input'); checked.type = 'checkbox'; label.append(checked, element('span', '已通过原群或私聊确认申请人为本人'));
        const actions = element('div', null, 'claim-action'), yes = element('button', '确认关联历史订单', 'primary'), no = element('button', '驳回申请');
        function review(decision) { return run(async () => { if (!note.value.trim()) throw new Error('请填写核实记录或驳回原因'); if (decision === 'approve' && !checked.checked) throw new Error('请先核实本人身份'); if (!confirm(decision === 'approve' ? '确认将该客户档案关联到此微信用户？账务不会重算。' : '确认驳回此申请？')) return; yes.disabled = no.disabled = true; try { await api('claims/' + c.id + '/review/', { decision, verified: checked.checked, note: note.value.trim() }); notice('认领申请已处理'); await loadClaims(); } finally { yes.disabled = no.disabled = false; } }); }
        yes.onclick = review('approve'); no.onclick = review('reject'); actions.append(yes, no); card.append(note, label, actions); $('claim-rows').append(card);
      });
      if (!response.results.length) $('claim-rows').append(element('div', '当前没有待核实的认领申请。', 'card muted'));
    }
    $('refresh-claims').onclick = run(loadClaims);
    document.querySelectorAll('[data-tab]').forEach(button => button.onclick = run(async () => { document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b === button)); ['create', 'orders', 'claims'].forEach(id => $(id).hidden = id !== button.dataset.tab); if (button.dataset.tab === 'orders') await loadOrders(); if (button.dataset.tab === 'claims') await loadClaims(); }));
    run(async () => { controls(); if (ctl.current()) notice('已恢复上次待确认的发单请求，请先查询结果，勿重复发单。', true); const response = await api('catalog/'); packages = response.packages; $('package').replaceChildren(new Option('请选择商品', '')); packages.forEach(p => $('package').append(new Option(p.name + ' · ' + p.players + '人', String(p.id)))); await searchCustomers(); })();
  });
})(typeof window !== 'undefined' ? window : globalThis);
