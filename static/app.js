/* =====================================================================
   후판 자동배차 UI (Vanilla JS, 빌드 도구 없음)
   - 데이터는 항상 서버 REST API(/api/*)에서 받는다.
   - localStorage 에는 화면 상태(탭, 필터, 입력 중인 질문, 대화 내역)만 저장한다.
   ===================================================================== */
'use strict';

const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---------------------------------------------------------------- 저장소 (실패해도 동작)
const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 사생활 보호 모드 등 */ }
  },
  del(key) { try { localStorage.removeItem(key); } catch { /* 무시 */ } },
};

// ---------------------------------------------------------------- 공통 유틸
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const fmtTon = (t) => (Number(t) || 0).toLocaleString('ko-KR', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
const fmtInt = (n) => (Number(n) || 0).toLocaleString('ko-KR');
const fmtMm = (n) => (n == null ? '' : fmtInt(Math.round(n)));

const STATUS_ORDER = ['편성', '미배차', '출하완료', '입금대기', '데이터오류', '낙찰취소'];
const REASON = {
  OVER_MAX: { label: '단일 중량 상한 초과', desc: '제품 하나의 중량이 차량 상한을 넘어 실을 수 없습니다.' },
  UNDER_MIN: { label: '조합 부족 (25톤 미달)', desc: '같은 분류·같은 지역에서 25톤을 채울 제품이 부족합니다. 다음 업로드 때 다시 배차됩니다.' },
  DATA_ERROR: { label: '데이터 오류', desc: '폭·두께·중량·인도처·지역 정보가 없거나 잘못되었습니다. 원본을 고쳐 다시 올려 주세요.' },
};
const CAT_LABEL = { WIDE: '광폭재', NORMAL: '일반재' };

const badge = (status) => `<span class="badge st-${esc(status)}">${esc(status)}</span>`;
function catBadges(category, isLong) {
  if (!category) return '';
  return `<span class="badge cat-${category}">${CAT_LABEL[category]}</span>${isLong ? '<span class="badge cat-LONG">장척</span>' : ''}`;
}
const regionText = (region, source) => (region ? `${esc(region)}${source === 'route' ? ' <small>운송경로</small>' : ''}` : '<span class="muted">-</span>');

function emptyState(title, desc = '') {
  return `<div class="empty"><strong>${esc(title)}</strong>${desc ? `<span>${desc}</span>` : ''}</div>`;
}

function table(cols, rows, { rowClass } = {}) {
  if (!rows.length) return emptyState('데이터가 없습니다.');
  const head = cols.map((c) => `<th class="${c.num ? 'num' : ''}">${esc(c.label)}</th>`).join('');
  const body = rows.map((r) => `<tr class="${rowClass ? rowClass(r) : ''}">${cols.map((c) => {
    const cls = [c.num ? 'num' : '', c.wrap ? 'wrap' : ''].join(' ');
    return `<td class="${cls}">${c.html ? c.html(r) : esc(c.get(r))}</td>`;
  }).join('')}</tr>`).join('');
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// 제품 표 공통 컬럼
const COL = {
  pno: { label: '제품번호', get: (r) => r.product_no },
  order: {
    label: '주문번호',
    html: (r) => (r.order_no ? esc(r.order_no)
      : r.order_candidates?.length ? `<span class="badge b-warn" title="${esc(r.order_candidates.join(', '))}">매칭 확인 필요</span>` : '<span class="muted">-</span>'),
  },
  customer: { label: '고객사', get: (r) => r.customer },
  dlv: { label: '인도처', get: (r) => r.dlv_name },
  region: { label: '지역', html: (r) => regionText(r.region, r.region_source) },
  cat: { label: '분류', html: (r) => catBadges(r.category, r.is_long) },
  status: { label: '상태', html: (r) => badge(r.status) },
  truck: { label: '차량', get: (r) => r.truck_id || '' },
  reason: { label: '사유', html: (r) => (r.unassigned_reason ? esc(REASON[r.unassigned_reason]?.label || r.unassigned_reason) : '') },
  width: { label: '폭(mm)', num: true, get: (r) => fmtMm(r.width_mm) },
  length: { label: '길이(mm)', num: true, html: (r) => (r.length_mm ? `${fmtMm(r.length_mm)}${r.length_source === 'calc' ? '<span class="muted" title="중량·두께·폭으로 계산한 값">*</span>' : ''}` : '') },
  ton: { label: '중량(톤)', num: true, get: (r) => fmtTon(r.weight_ton) },
  carry: { label: '이월', num: true, get: (r) => (r.carryover_days ? `${r.carryover_days}일` : '-') },
  refund: { label: '환불 안내', html: (r) => (r.refund_notice ? '<span class="refund">대상</span>' : '') },
  plan: { label: '출하예정', get: (r) => r.ship_plan_date || '' },
  note: { label: '비고', wrap: true, get: (r) => r.review_note || '' },
};

// ---------------------------------------------------------------- 토스트
function toast(msg, type = 'info', ms = 3500) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), type === 'error' ? Math.max(ms, 6000) : ms);
}

// ---------------------------------------------------------------- API
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    throw new Error('서버에 연결할 수 없습니다. 프로그램(run.bat)이 실행 중인지 확인해 주세요.');
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    if (typeof d === 'string') throw new Error(d);
    if (res.status === 422 && Array.isArray(d)) throw new Error('입력값이 올바르지 않습니다.');
    throw new Error(`요청을 처리하지 못했습니다 (${res.status}).`);
  }
  return body;
}
const sendJson = (method, path, data) => api(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });

async function withLoading(btn, fn) {
  btn.classList.add('loading');
  btn.disabled = true;
  try { return await fn(); } finally { btn.classList.remove('loading'); btn.disabled = false; }
}

// ---------------------------------------------------------------- 상태
const state = {
  rules: { truck_min_ton: 25, wide_max_ton: 26, normal_max_ton: 27, long_max_ton: 26 },
  dispatch: null,             // GET /api/dispatch 결과
  expanded: new Set(store.get('ui.expanded', [])),
  drag: null,                 // { product, fromTruck }
  previewOk: false,
  files: [],
};

// ---------------------------------------------------------------- 헤더 상태
async function checkHealth() {
  const el = $('health');
  try {
    const h = await api('/api/health');
    state.rules = h.settings.rules;
    el.textContent = `서버 정상 · 제품 ${fmtInt(h.db.products)}건${h.settings.embedding_fake ? ' · 테스트 임베딩' : ''}`;
    el.className = 'pill ok';
    el.title = `벡터 ${fmtInt(h.faiss.vectors)}건 · 임베딩 ${h.settings.embedding_model}${h.settings.api_key_configured ? '' : ' (API 키 없음)'}`;
    $('job').hidden = !h.current_job;
    $('job').textContent = h.current_job ? `${h.current_job} 진행 중…` : '';
  } catch (e) {
    el.textContent = '서버 연결 끊김';
    el.title = e.message;
    el.className = 'pill error';
  }
}

// ---------------------------------------------------------------- 탭
const TAB_LOADERS = {
  upload: loadUploadTab,
  dispatch: () => loadDispatch(),
  unassigned: () => loadDispatch(),
  chat: initChatExamples,
  dashboard: loadDashboard,
};

function showTab(name, { focusInput = false } = {}) {
  if (!TAB_LOADERS[name]) name = 'upload';
  $$('.tabs [role=tab]').forEach((b) => {
    const on = b.dataset.tab === name;
    b.setAttribute('aria-selected', on);
    b.tabIndex = on ? 0 : -1;
  });
  $$('.panel').forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
  store.set('ui.tab', name);
  TAB_LOADERS[name]();
  // 클릭으로 챗봇 탭에 들어왔을 때만 입력창으로 (화살표 키 탐색 중에는 포커스를 탭에 둔다)
  if (name === 'chat' && focusInput) $('chat-input').focus();
}

// =====================================================================
// 업로드
// =====================================================================
function setFiles(fileList) {
  const files = [...fileList].filter((f) => /\.xlsx?$/i.test(f.name));
  if (fileList.length && !files.length) toast('엑셀 파일(.xlsx, .xls)만 올릴 수 있습니다.', 'error');
  state.files = files;
  $('upload-result').innerHTML = '';
  $('btn-clear-files').hidden = !files.length;
  if (files.length) previewUpload(); else { $('preview').innerHTML = ''; setUploadEnabled(false); }
}

function setUploadEnabled(ok) { state.previewOk = ok; $('btn-upload').disabled = !ok; }

function filesForm(field, files) {
  const fd = new FormData();
  files.forEach((f) => fd.append(field, f));
  return fd;
}

const SHEET_KIND = { product: '제품 시트', auction: '경매 시트', delivery: '주소표', unknown: '사용 안 함' };

async function previewUpload() {
  $('preview').innerHTML = '<p class="muted small">파일을 확인하는 중…</p>';
  setUploadEnabled(false);
  try {
    const r = await api('/api/upload?preview=true', { method: 'POST', body: filesForm('files', state.files) });
    $('preview').innerHTML = r.files.map((f) => (f.error
      ? `<div class="file-card err"><div class="file-name">${esc(f.file_name)} <span class="badge b-danger">읽을 수 없음</span></div><div class="note danger">${esc(f.error)}</div></div>`
      : `<div class="file-card">
          <div class="file-name">${esc(f.file_name)} <span class="badge">미리보기</span></div>
          <div class="sheet-list">${f.sheets.map((s) => `<span class="chip">${esc(s.name)} · ${SHEET_KIND[s.kind]} <b>${fmtInt(s.rows)}</b>행</span>`).join('')}</div>
          <div class="muted small">제품 ${fmtInt(f.products)}건 · 주문 ${fmtInt(f.orders)}건 · 오류 ${fmtInt(f.errors)}건 ·
            길이 ${f.has_length_column ? '원본 컬럼' : '계산값 사용'} · 출하여부 ${f.has_shipped_column ? '컬럼 있음 (자동 반영)' : '컬럼 없음'}</div>
          ${f.duplicate_file ? `<div class="note warn">이미 올린 적 있는 파일입니다 (${esc(f.previous_upload_at)}). 반영해도 바뀌는 내용은 없습니다.</div>` : ''}
        </div>`)).join('');
    setUploadEnabled(r.files.some((f) => !f.error));
  } catch (e) {
    $('preview').innerHTML = `<div class="note danger">${esc(e.message)}</div>`;
  }
}

async function doUpload() {
  if (!state.previewOk) return;
  await withLoading($('btn-upload'), async () => {
    try {
      const r = await api('/api/upload', { method: 'POST', body: filesForm('files', state.files) });
      $('upload-result').innerHTML = r.files.map(renderUploadResult).join('') + renderEmbedding(r.embedding);
      $('preview').innerHTML = '';
      state.files = [];
      $('upload-files').value = '';
      $('btn-clear-files').hidden = true;
      setUploadEnabled(false);
      state.dispatch = null;
      toast('업로드를 반영했습니다. 배차결과 탭에서 [배차 실행]을 눌러 주세요.', 'success');
      checkHealth();
      loadUploadTab();
      refreshCounts();
    } catch (e) { toast(e.message, 'error'); }
  });
  // withLoading 이 버튼을 다시 켜므로, 반영할 파일이 남아 있을 때만 활성화
  setUploadEnabled(state.previewOk && state.files.length > 0);
}

function renderUploadResult(f) {
  if (f.error) return `<div class="file-card err"><div class="file-name">${esc(f.file_name)}</div><div class="note danger">${esc(f.error)}</div></div>`;
  const changes = Object.entries(f.status_changes);
  return `<div class="file-card">
    <div class="file-name">${esc(f.file_name)} <span class="badge st-편성">반영 완료</span>
      ${f.duplicate_file ? '<span class="badge b-warn">이미 올린 파일</span>' : ''}</div>
    <div class="kpis">
      <div class="kpi k-new"><span>신규</span><b>${fmtInt(f.new_cnt)}</b></div>
      <div class="kpi"><span>중복</span><b>${fmtInt(f.dup_cnt)}</b></div>
      <div class="kpi"><span>갱신</span><b>${fmtInt(f.updated_cnt)}</b></div>
      <div class="kpi${f.error_cnt ? ' k-err' : ''}"><span>오류</span><b>${fmtInt(f.error_cnt)}</b></div>
    </div>
    ${changes.length ? `<div class="note info">상태 변경: ${changes.map(([k, v]) => `${esc(k)} <b>${v}</b>건`).join(' · ')}</div>` : ''}
    ${f.stale_groups?.length ? `<div class="note warn"><div><b>예전 데이터로 보이는 주문 ${f.stale_groups.length}건은 상태를 바꾸지 않았습니다.</b>
      이미 반영된 상태(확정·취소)보다 이전 단계라서입니다. 최신 파일인지 확인해 주세요.<br>
      ${f.stale_groups.slice(0, 5).map((g) => `${esc(g.customer || g.customer_code)} · 경매 ${esc(g.auction_no)}: 현재 ${esc(g.db)} → 파일 ${esc(g.file)}`).join('<br>')}</div></div>` : ''}
    ${f.review_needed.length ? `<div class="note danger"><div><b>확인 필요 ${f.review_needed.length}건</b> — 편성된 차량의 제품이 바뀌었거나, 취소 여부를 판단할 수 없어 배차를 보류했습니다.<br>
      ${f.review_needed.slice(0, 5).map((x) => `${esc(x.truck_id || '배차 보류')} · ${esc(x.product_no)}: ${esc(x.note)}`).join('<br>')}</div></div>` : ''}
    ${f.order_match_pending.length ? `<details class="fold"><summary>주문 매칭 확인 필요 ${f.order_match_pending.length}건</summary>${table([
      { label: '제품번호', get: (x) => x.product_no }, { label: '후보 주문번호', get: (x) => x.candidates.join(', ') },
    ], f.order_match_pending)}</details>` : ''}
    ${f.errors.length ? `<details class="fold"><summary>데이터 오류 ${f.errors.length}건 (배차 대상에서 제외)</summary>${table([
      { label: '시트', get: (x) => x.sheet }, { label: '행', num: true, get: (x) => x.row },
      { label: '제품번호', get: (x) => x.product_no || '' }, { label: '사유', wrap: true, get: (x) => x.reason },
    ], f.errors)}</details>` : ''}
  </div>`;
}

function renderEmbedding(e) {
  if (e.error) return `<div class="note warn">검색용 임베딩을 만들지 못했습니다 (${esc(e.error)}). 번호·이름 검색은 그대로 되며, 다음 업로드 때 다시 시도합니다.</div>`;
  return e.embedded ? `<p class="muted small">검색용 임베딩 ${fmtInt(e.embedded)}건 갱신</p>` : '';
}

async function uploadPlaces() {
  const input = $('places-file');
  if (!input.files.length) return toast('주소표 파일을 선택해 주세요.', 'error');
  await withLoading($('btn-places'), async () => {
    try {
      const r = await api('/api/delivery-places', { method: 'POST', body: filesForm('file', [...input.files]) });
      $('places-result').innerHTML = `<div class="note info">주소 ${fmtInt(r.updated)}건을 반영했습니다.</div>${r.unresolved_addresses.length
        ? `<div class="note warn"><div>시·군을 찾지 못한 주소 ${r.unresolved_addresses.length}건 (운송경로로 판단):<br>${r.unresolved_addresses.slice(0, 5).map((x) => `${esc(x.dlv_code)} ${esc(x.address)}`).join('<br>')}</div></div>` : ''}`;
      input.value = '';
      state.dispatch = null;
      loadUploadTab();
    } catch (e) { toast(e.message, 'error'); }
  });
}

async function saveAddress(code, btn) {
  const input = document.querySelector(`input[data-addr="${CSS.escape(code)}"]`);
  if (!input.value.trim()) return toast('주소를 입력해 주세요.', 'error');
  await withLoading(btn, async () => {
    try {
      await sendJson('PUT', `/api/delivery-places/${encodeURIComponent(code)}`, { address: input.value.trim() });
      toast(`${code} 주소를 저장했습니다.`, 'success');
      state.dispatch = null;
      loadUploadTab();
    } catch (e) { toast(e.message, 'error'); }
  });
}

async function loadUploadTab() {
  try {
    const [p, u] = await Promise.all([api('/api/delivery-places'), api('/api/uploads')]);
    const noAddr = p.places.filter((x) => !x.address && x.active > 0);
    $('cnt-noaddr').textContent = noAddr.length || '';
    $('places-list').innerHTML = noAddr.length ? table([
      { label: '인도처', html: (x) => `<b>${esc(x.dlv_name || '')}</b><div class="muted small">${esc(x.dlv_code)} · 운송경로 ${esc(x.route || '-')}</div>` },
      { label: '제품', num: true, get: (x) => fmtInt(x.active) },
      { label: '주소 입력', html: (x) => `<div class="addr-form"><input class="addr-input" data-addr="${esc(x.dlv_code)}" placeholder="예: 경남 창원시 성산구 공단로 1"><button class="small" data-save="${esc(x.dlv_code)}">저장</button></div>` },
    ], noAddr) : emptyState(p.places.length ? '모든 인도처에 주소가 있습니다.' : '아직 인도처가 없습니다.', p.places.length ? '' : '낙찰현황을 먼저 업로드해 주세요.');
    $('upload-history').innerHTML = u.uploads.length ? table([
      { label: '일시', get: (x) => x.uploaded_at }, { label: '파일', get: (x) => x.file_name },
      { label: '신규', num: true, get: (x) => fmtInt(x.new_cnt) }, { label: '중복', num: true, get: (x) => fmtInt(x.dup_cnt) },
      { label: '갱신', num: true, get: (x) => fmtInt(x.updated_cnt) }, { label: '오류', num: true, get: (x) => fmtInt(x.error_cnt) },
    ], u.uploads) : emptyState('업로드 이력이 없습니다.', '위에서 낙찰현황 엑셀을 올려 주세요.');
  } catch (e) { toast(e.message, 'error'); }
}

// =====================================================================
// 배차결과
// =====================================================================
const filters = Object.assign({ date: '', cat: '', region: '', cust: '' }, store.get('ui.filters', {}));

async function loadDispatch(force = false) {
  if (!state.dispatch || force) {
    try { state.dispatch = await api('/api/dispatch'); } catch (e) { toast(e.message, 'error'); return; }
  }
  renderDispatch();
  renderUnassigned();
  refreshCountsFrom(state.dispatch);
}

function refreshCountsFrom(d) {
  $('cnt-trucks').textContent = d.trucks.length || '';
  $('cnt-unassigned').textContent = d.unassigned.length || '';
}
async function refreshCounts() {
  try { refreshCountsFrom(await api('/api/dispatch')); } catch { /* 헤더 숫자만 */ }
}

function fillSelect(sel, values, current, labelFn = (v) => v) {
  const first = sel.options[0].outerHTML;
  sel.innerHTML = first + values.map((v) => `<option value="${esc(v)}">${esc(labelFn(v))}</option>`).join('');
  sel.value = values.includes(current) ? current : '';
}

function capFor(category, hasLong) {
  const r = state.rules;
  if (category === 'WIDE') return r.wide_max_ton;
  return hasLong ? r.long_max_ton : r.normal_max_ton;
}

function liveProducts(t) { return t.products.filter((p) => p.status === '편성'); }

function truckMatchesFilter(t) {
  if (filters.date && t.dispatch_date !== filters.date) return false;
  if (filters.cat && t.category !== filters.cat) return false;
  if (filters.region && t.region !== filters.region) return false;
  if (filters.cust) {
    const q = filters.cust.toLowerCase();
    if (!t.customers.some((c) => c.toLowerCase().includes(q)) && !t.dlv_names.some((c) => c.toLowerCase().includes(q))) return false;
  }
  return true;
}

function productMatchesFilter(p) {
  if (filters.cat && p.category !== filters.cat) return false;
  if (filters.region && p.region !== filters.region) return false;
  if (filters.cust) {
    const q = filters.cust.toLowerCase();
    if (!(p.customer || '').toLowerCase().includes(q) && !(p.dlv_name || '').toLowerCase().includes(q)) return false;
  }
  return true;
}

function productRow(p, truckId) {
  const held = p.status === '미배차' && p.needs_review;  // 주문 취소 확인 전 배차 보류 (R-01)
  const locked = (p.status !== '편성' && p.status !== '미배차') || held;
  const tags = `${p.category === 'WIDE' && !truckId ? '<span class="tag cat-WIDE">광폭</span>' : ''}${p.is_long ? '<span class="tag cat-LONG">장척</span>' : ''}${held ? '<span class="tag b-danger">확인 필요</span>' : locked ? `<span class="tag st-${esc(p.status)}">${esc(p.status)}</span>` : ''}`;
  return `<li class="prow${locked ? ' locked' : ''}" ${locked ? '' : 'draggable="true"'} data-pno="${esc(p.product_no)}" data-truck="${esc(truckId || '')}"
      title="${esc(`${p.product_no} · ${p.customer || ''} · ${p.dlv_name || ''}\n폭 ${fmtMm(p.width_mm)} · 길이 ${fmtMm(p.length_mm)}${p.length_source === 'calc' ? '(계산)' : ''} · ${fmtTon(p.weight_ton)}톤`)}">
    <span class="grip" aria-hidden="true">${locked ? '' : '⋮⋮'}</span>
    <span class="pno">${esc(p.product_no)}</span>
    <span class="who">${esc(p.customer || '')} · ${esc(p.dlv_name || '')}${tags}</span>
    <span class="spec"><span class="dims">${fmtMm(p.width_mm)}×${fmtMm(p.length_mm)}</span><b>${fmtTon(p.weight_ton)}</b></span>
  </li>`;
}

function truckCard(t) {
  const cap = t.max_ton || capFor(t.category, t.has_long);
  const min = state.rules.truck_min_ton;
  // 막대 시작점: 보통은 20톤(25톤 주변 차이가 잘 보이도록), 20톤 미만인 확인 필요 차량은 0톤부터
  const base = t.total_ton < 20 ? 0 : 20;
  const pct = (v) => Math.max(0, Math.min(100, ((v - base) / (cap - base)) * 100));
  const fillCls = t.total_ton > cap + 1e-9 ? 'over' : t.total_ton < min - 1e-9 ? 'under' : '';
  const open = state.expanded.has(t.truck_id);
  const live = liveProducts(t);
  return `<article class="truck${t.needs_review ? ' review' : ''}${open ? ' open' : ''}" data-truck="${esc(t.truck_id)}">
    <div class="drop-hint"></div>
    <div class="truck-top">
      <div class="truck-title">
        <span class="truck-id">${esc(t.truck_id)}</span>${catBadges(t.category, t.has_long)}
        ${t.needs_review ? '<span class="badge b-danger">확인 필요</span>' : ''}
        <span class="region">${regionText(t.region, t.region_source)}</span>
      </div>
      <div class="load" title="하한 ${min}톤 · 상한 ${cap}톤${t.has_long && t.category === 'NORMAL' ? ' (장척 포함)' : ''}">
        <div class="bar"><span class="fill ${fillCls}" style="width:${pct(t.total_ton)}%"></span><span class="min" style="left:${pct(min)}%"></span></div>
        <span class="load-num"><b>${fmtTon(t.total_ton)}</b> / ${cap}톤</span>
      </div>
      <div class="truck-meta">
        <span>${live.length}건 · 고객사 ${esc(t.customers.join(', ') || '-')}</span>
        <span>인도처 ${esc(t.dlv_names.join(', ') || '-')}</span>
      </div>
    </div>
    ${t.needs_review ? `<div class="truck-review note danger"><div>${esc([...new Set([...(t.review_note || '').split(' / '), ...t.products.map((p) => p.review_note)].filter(Boolean))].join(' / ') || '확인이 필요합니다.')}
      <br><span class="small">제품을 다른 차량·미배차로 옮기거나 [해체]해서 정리하세요.</span></div></div>` : ''}
    <div class="truck-foot">
      <button class="truck-toggle" data-toggle="${esc(t.truck_id)}" aria-expanded="${open}">${open ? '제품 접기 ▲' : `제품 ${t.products.length}건 보기 ▼`}</button>
      <button class="truck-dismantle${t.needs_review ? ' danger' : ''}" data-dismantle="${esc(t.truck_id)}" title="편성된 제품을 모두 미배차로 되돌립니다">해체</button>
    </div>
    <ul class="plist">${open ? t.products.map((p) => productRow(p, t.truck_id)).join('') : ''}</ul>
  </article>`;
}

function renderDispatch() {
  const d = state.dispatch;
  if (!d) return;
  const dates = [...new Set(d.trucks.map((t) => t.dispatch_date))].sort().reverse();
  const regions = [...new Set([...d.trucks.map((t) => t.region), ...d.unassigned.map((p) => p.region)].filter(Boolean))].sort();
  fillSelect($('f-date'), dates, filters.date);
  fillSelect($('f-region'), regions, filters.region);
  $('f-cat').value = filters.cat;
  $('f-cust').value = filters.cust;
  $('btn-export').href = `/api/export${filters.date ? `?date=${encodeURIComponent(filters.date)}` : ''}`;
  $('truck-ids').innerHTML = d.trucks.map((t) => `<option value="${esc(t.truck_id)}">`).join('');

  const trucks = d.trucks.filter(truckMatchesFilter).sort((a, b) => (b.needs_review - a.needs_review) || (a.dispatch_date < b.dispatch_date ? 1 : a.dispatch_date > b.dispatch_date ? -1 : a.truck_id.localeCompare(b.truck_id)));
  const tons = trucks.reduce((s, t) => s + t.total_ton, 0);
  $('truck-count').innerHTML = d.trucks.length ? `차량 <b>${trucks.length}</b>대${trucks.length !== d.trucks.length ? ` / 전체 ${d.trucks.length}대` : ''} · ${fmtTon(tons)}톤` : '';
  $('truck-grid').innerHTML = trucks.length ? trucks.map(truckCard).join('')
    : d.trucks.length ? emptyState('조건에 맞는 차량이 없습니다.', '필터를 바꾸거나 [초기화]를 눌러 주세요.')
      : emptyState('편성된 차량이 없습니다.', '업로드 탭에서 낙찰현황을 올린 뒤 [배차 실행]을 눌러 주세요.');

  // 오른쪽 미배차 목록 (배차 대상인 '미배차'만, 데이터오류 제외)
  const un = d.unassigned.filter((p) => p.status === '미배차');
  const shown = un.filter(productMatchesFilter);
  $('side-total').textContent = `${shown.length}건 · ${fmtTon(shown.reduce((s, p) => s + p.weight_ton, 0))}톤`;
  const groups = new Map();
  shown.forEach((p) => {
    const k = `${p.category}|${p.region_source}|${p.region}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(p);
  });
  $('side-list').innerHTML = shown.length ? [...groups.entries()].sort((a, b) => sumTon(b[1]) - sumTon(a[1])).map(([, ps]) => `
    <div class="side-group">
      <div class="side-group-head"><span>${catBadges(ps[0].category)} <b>${esc(ps[0].region || '-')}</b></span><span>${ps.length}건 · ${fmtTon(sumTon(ps))}톤</span></div>
      <ul class="plist" style="border:0;max-height:none;padding:0">${ps.map((p) => productRow(p, null)).join('')}</ul>
    </div>`).join('')
    : emptyState(un.length ? '조건에 맞는 미배차 제품이 없습니다.' : '미배차 제품이 없습니다.');
}

const sumTon = (ps) => ps.reduce((s, p) => s + (p.weight_ton || 0), 0);

function setFilter(key, value) {
  filters[key] = value;
  store.set('ui.filters', filters);
  renderDispatch();
}

async function runDispatch() {
  await withLoading($('btn-dispatch'), async () => {
    try {
      const r = await sendJson('POST', '/api/dispatch/run', {});
      const reasons = Object.entries(r.unassigned_by_reason).map(([k, v]) => `${REASON[k]?.label || k} ${v}건`).join(', ');
      $('dispatch-summary').innerHTML = `<div class="run-summary">
        <span>배차일 <b>${esc(r.dispatch_date)}</b></span>
        <span>새 차량 <b>${r.truck_count}</b>대 (광폭 ${r.wide_trucks} · 일반 ${r.normal_trucks})</span>
        <span>편성 <b>${fmtInt(r.assigned_products)}</b>건 · <b>${fmtTon(r.assigned_ton)}</b>톤</span>
        <span>미배차 <b>${fmtInt(r.unassigned_products)}</b>건${reasons ? ` (${esc(reasons)})` : ''}</span>
        <span class="muted">${r.elapsed_sec}초</span></div>`;
      toast(r.truck_count ? `차량 ${r.truck_count}대를 새로 편성했습니다.` : '새로 편성할 수 있는 차량이 없습니다.', r.truck_count ? 'success' : 'info');
      await loadDispatch(true);
    } catch (e) { toast(e.message, 'error'); }
  });
}

// ---------------------------------------------------------------- 드래그 앤 드롭
function findProduct(pno) {
  const d = state.dispatch;
  for (const t of d.trucks) {
    const p = t.products.find((x) => x.product_no === pno);
    if (p) return { product: p, truck: t };
  }
  const p = d.unassigned.find((x) => x.product_no === pno);
  return p ? { product: p, truck: null } : null;
}

/** 차량의 규칙 위반 종류 (서버 dispatch._issues 와 같음): MIX(R2), REGION(R3), UNDER/OVER(R1). */
function truckIssues(category, products) {
  const out = new Set();
  if (!products.length) return { set: out, total: 0, cap: 0, hasLong: false };
  if (new Set(products.map((p) => p.category)).size > 1 || !products.some((p) => p.category === category)) out.add('MIX');
  if (new Set(products.map((p) => `${p.region_source}|${p.region}`)).size > 1) out.add('REGION');
  const total = products.reduce((s, p) => s + p.weight_ton, 0);
  const hasLong = products.some((p) => p.is_long);
  const cap = capFor(category, hasLong);
  if (total < state.rules.truck_min_ton - 1e-9) out.add('UNDER');
  if (total > cap + 1e-9) out.add('OVER');
  return { set: out, total, cap, hasLong };
}

/** 서버(move_product)와 같은 규칙으로 미리 검사. 문제가 없으면 null, 있으면 사유. */
function checkMove(product, fromTruck, toTruck) {
  const r = state.rules;
  if (toTruck && fromTruck && toTruck.truck_id === fromTruck.truck_id) return '같은 차량입니다.';
  if (!toTruck && !fromTruck) return '이미 미배차 상태입니다.';
  if (toTruck && product.status === '미배차' && product.needs_review) return product.review_note || '확인이 필요한 제품입니다.';
  if (toTruck) {
    if (toTruck.shipped) return '이미 출하된 차량입니다.';
    if (product.category !== toTruck.category) return 'R2: 광폭재와 일반재는 한 차량에 실을 수 없습니다.';
    if (product.region !== toTruck.region || product.region_source !== toTruck.region_source) {
      return `R3: 지역이 다릅니다 (${product.region || '-'} → ${toTruck.region}).`;
    }
    const before = truckIssues(toTruck.category, liveProducts(toTruck));
    const after = truckIssues(toTruck.category, [...liveProducts(toTruck), product]);
    if (after.set.has('OVER')) {
      return `R1: ${fmtTon(after.total)}톤으로 상한 ${after.cap}톤${after.hasLong && toTruck.category === 'NORMAL' ? '(장척 포함)' : ''}을 넘습니다.`;
    }
    // 이미 25톤 미만이던 차량에 채워 넣는 것은 허용 (R-02)
    if (after.set.has('UNDER') && !before.set.has('UNDER')) return `R1: ${fmtTon(after.total)}톤으로 하한 ${r.truck_min_ton}톤 미만입니다.`;
  }
  if (fromTruck) {
    const live = liveProducts(fromTruck);
    const broken = fromTruck.needs_review || truckIssues(fromTruck.category, live).set.size > 0;
    const remain = live.filter((p) => p.product_no !== product.product_no);
    // 이미 규칙을 벗어났거나 '확인 필요'인 차량에서는 빼는 이동을 허용 (R-02)
    if (!broken && remain.length && truckIssues(fromTruck.category, remain).set.has('UNDER')) {
      const t = remain.reduce((s, p) => s + p.weight_ton, 0);
      return `R1: 출발 차량 ${fromTruck.truck_id}이(가) ${fmtTon(t)}톤으로 하한 ${r.truck_min_ton}톤보다 적어집니다.`;
    }
  }
  return null;
}

function dropTargetOf(el) { return el.closest('.truck, #unassigned-drop'); }

function clearDropMarks() {
  $$('.drop-ok, .drop-no').forEach((el) => {
    el.classList.remove('drop-ok', 'drop-no');
    const hint = el.querySelector(':scope > .drop-hint');
    if (hint) hint.textContent = '';
  });
}

function targetTruck(el) {
  return el.id === 'unassigned-drop' ? null : state.dispatch.trucks.find((t) => t.truck_id === el.dataset.truck);
}

document.addEventListener('dragstart', (e) => {
  const row = e.target.closest?.('.prow[draggable=true]');
  if (!row || !state.dispatch) return;
  const found = findProduct(row.dataset.pno);
  if (!found) return;
  state.drag = { product: found.product, fromTruck: found.truck };
  row.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', row.dataset.pno);
});

document.addEventListener('dragover', (e) => {
  if (!state.drag) return;
  const target = dropTargetOf(e.target);
  if (!target) { state.drag.lastReason = null; return; } // 빈 곳 위: 놓아도 아무 일 없음
  e.preventDefault();
  const reason = checkMove(state.drag.product, state.drag.fromTruck, targetTruck(target));
  if (!target.classList.contains(reason ? 'drop-no' : 'drop-ok')) {
    clearDropMarks();
    target.classList.add(reason ? 'drop-no' : 'drop-ok');
    const hint = target.querySelector(':scope > .drop-hint');
    if (hint) hint.textContent = reason ? `✕ ${reason}` : '여기에 놓기';
  }
  // 금지된 곳은 브라우저가 drop 자체를 취소한다(커서: 금지). 그래서 사유를 기억해 두었다가 dragend 에서 알려준다.
  e.dataTransfer.dropEffect = reason ? 'none' : 'move';
  state.drag.lastReason = reason;
});

document.addEventListener('dragleave', (e) => {
  const target = dropTargetOf(e.target);
  // lastReason 은 여기서 지우지 않는다: 금지된 곳에 놓으면 브라우저가 dragend 직전에 dragleave 를 보낸다.
  if (target && !target.contains(e.relatedTarget)) {
    target.classList.remove('drop-ok', 'drop-no');
    const hint = target.querySelector(':scope > .drop-hint');
    if (hint) hint.textContent = '';
  }
});

document.addEventListener('drop', async (e) => {
  if (!state.drag) return;
  const target = dropTargetOf(e.target);
  e.preventDefault();
  clearDropMarks();
  const { product, fromTruck } = state.drag;
  state.drag = null;
  if (!target) return;
  const to = targetTruck(target);
  const reason = checkMove(product, fromTruck, to);
  if (reason) return toast(`이동할 수 없습니다 — ${reason}`, 'error');
  await moveProduct(product.product_no, to ? to.truck_id : null);
});

document.addEventListener('dragend', () => {
  $$('.prow.dragging').forEach((r) => r.classList.remove('dragging'));
  clearDropMarks();
  // drop 이 일어났다면 drop 핸들러가 state.drag 를 비웠다. 남아 있으면 금지된 곳에 놓은 것.
  if (state.drag?.lastReason) toast(`이동할 수 없습니다 — ${state.drag.lastReason}`, 'error');
  state.drag = null;
});

async function moveProduct(pno, toTruckId) {
  try {
    const r = await sendJson('POST', '/api/dispatch/move', { product_no: pno, to_truck_id: toTruckId });
    toast(`${r.product_no}: ${r.from || '미배차'} → ${r.to || '미배차'}`, 'success');
    if (toTruckId) state.expanded.add(toTruckId);
    await loadDispatch(true);
  } catch (e) { toast(`이동할 수 없습니다 — ${e.message}`, 'error'); }
}

// =====================================================================
// 미배차 탭
// =====================================================================
function renderUnassigned() {
  const d = state.dispatch;
  if (!d) return;
  const sort = store.get('ui.unsort', 'carry');
  $('un-sort').value = sort;
  const sorter = sort === 'weight'
    ? (a, b) => b.weight_ton - a.weight_ton
    : (a, b) => (b.carryover_days - a.carryover_days) || (b.weight_ton - a.weight_ton);

  const un = d.unassigned;
  const by = (reason) => un.filter((p) => p.unassigned_reason === reason).sort(sorter);
  const under = by('UNDER_MIN');
  const over = by('OVER_MAX');
  const err = un.filter((p) => p.status === '데이터오류').sort(sorter);
  const held = un.filter((p) => p.status === '미배차' && p.needs_review).sort(sorter);
  const pending = un.filter((p) => p.status === '미배차' && !p.unassigned_reason && !p.needs_review).sort(sorter);
  const refundCnt = un.filter((p) => p.refund_notice).length;

  $('un-stats').innerHTML = `
    <span class="chip">미배차 <b>${fmtInt(un.length - err.length)}</b>건 · ${fmtTon(sumTon(un.filter((p) => p.status === '미배차')))}톤</span>
    <span class="chip">환불 안내 대상 <b class="refund">${fmtInt(refundCnt)}</b>건</span>
    <span class="chip">데이터 오류 <b>${fmtInt(err.length)}</b>건</span>
    <span class="chip">입금대기 <b>${fmtInt(d.waiting_payment.length)}</b>건</span>`;

  if (!un.length && !d.waiting_payment.length) {
    $('un-content').innerHTML = `<div class="card">${emptyState('미배차 제품이 없습니다.', '모든 배차 대상 제품이 차량에 편성되었습니다.')}</div>`;
    return;
  }

  // 조합 부족: 분류·지역별 그룹
  const groups = new Map();
  under.forEach((p) => {
    const k = `${p.category}|${p.region_source}|${p.region}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(p);
  });
  const min = state.rules.truck_min_ton;
  const groupCards = [...groups.values()].sort((a, b) => sumTon(b) - sumTon(a)).map((ps) => {
    const tons = sumTon(ps);
    const lack = Math.max(0, min - tons);
    return `<div class="card group-card">
      <div class="card-head">
        <h2>${catBadges(ps[0].category)} ${regionText(ps[0].region, ps[0].region_source)}</h2>
        <span class="muted">${ps.length}건 · <b>${fmtTon(tons)}</b>톤</span>
      </div>
      <div class="gauge"><div class="bar"><span class="fill under" style="width:${Math.min(100, (tons / min) * 100)}%"></span></div>
        <span>${min}톤까지 <b>${fmtTon(lack)}</b>톤 부족</span></div>
      ${table([COL.pno, COL.order, COL.customer, COL.dlv, COL.width, COL.length, COL.ton, COL.carry, COL.refund], ps)}
    </div>`;
  }).join('');

  const section = (title, desc, body, count) => (count ? `<h2 class="section-title">${title} <span class="count">${count}</span></h2>
    <p class="muted small" style="margin:0">${desc}</p>${body}` : '');

  $('un-content').innerHTML = [
    section('확인 필요 (배차 보류)', '같은 경매의 주문 중 일부가 취소되었는데 어느 제품이 취소분인지 판단할 수 없어 배차하지 않았습니다. 제품 시트가 포함된 최신 파일을 올리면 자동으로 정리됩니다.',
      `<div class="card">${table([COL.pno, COL.order, COL.customer, COL.dlv, COL.region, COL.ton, COL.note], held)}</div>`, held.length),
    section(REASON.UNDER_MIN.label, REASON.UNDER_MIN.desc, `<div class="group-grid">${groupCards}</div>`, under.length),
    section(REASON.OVER_MAX.label, REASON.OVER_MAX.desc,
      `<div class="card">${table([COL.pno, COL.order, COL.customer, COL.cat, COL.region, COL.width, COL.length, COL.ton, COL.carry, COL.refund], over)}</div>`, over.length),
    section('배차 대기', '아직 배차를 실행하지 않았거나 차량에서 수동으로 뺀 제품입니다. 배차결과 탭에서 [배차 실행]을 누르면 다시 배차됩니다.',
      `<div class="card">${table([COL.pno, COL.order, COL.customer, COL.cat, COL.region, COL.ton], pending)}</div>`, pending.length),
    section(REASON.DATA_ERROR.label, REASON.DATA_ERROR.desc,
      `<div class="card">${table([COL.pno, COL.order, COL.customer, COL.dlv, COL.region, COL.ton, COL.note], err)}</div>`, err.length),
    section('입금대기', '진행상태가 "낙찰"(미입금)인 주문의 제품입니다. "낙찰확정"이 되면 다음 배차에 자동으로 들어갑니다.',
      `<div class="card">${table([COL.pno, COL.customer, COL.dlv, COL.cat, COL.region, COL.ton], d.waiting_payment)}</div>`, d.waiting_payment.length),
  ].join('');
}

// =====================================================================
// 챗봇
// =====================================================================
const CHAT_KEY = 'ui.chat.history';
const MAX_HISTORY = 15;
let chatHistory = store.get(CHAT_KEY, []).map((h) => ({ ...h, stale: true }));

function mmsBytes(text) {
  // 서버(EUC-KR)와 같은 기준: ASCII 1바이트, 그 외 2바이트
  let n = 0;
  for (const ch of text) n += ch.charCodeAt(0) < 128 ? 1 : 2;
  return n;
}

function saveChat() {
  // 저장 공간을 아끼기 위해 제품 표는 최대 50건만 보관
  const slim = chatHistory.slice(-MAX_HISTORY).map(({ stale, ...h }) => ({
    ...h, res: h.res ? { ...h.res, rows: (h.res.rows || []).slice(0, 50) } : h.res,
  }));
  store.set(CHAT_KEY, slim);
}

function renderChatEntry(h, idx) {
  const r = h.res;
  let bot;
  if (h.error) {
    bot = `<div class="note danger">${esc(h.error)}</div>`;
  } else if (!r.found) {
    bot = `<div class="note warn">${esc(r.message)}</div><span class="muted small">제품번호(예: FD48282003), 주문번호(D로 시작하는 10자리), 경매번호(10자리 숫자), 고객사명·인도처명으로 조회할 수 있습니다.</span>`;
  } else {
    const counts = STATUS_ORDER.filter((s) => r.status_counts[s]).map((s) => `${badge(s)} <b>${r.status_counts[s]}</b>`).join(' ');
    bot = `
      <div class="msg-meta"><b>${esc(r.message)}</b> ${counts}
        ${r.matched?.length ? `<span class="muted small">· 검색어: ${esc(r.matched.slice(0, 3).join(', '))}${r.matched.length > 3 ? ' 외' : ''}</span>` : ''}
        ${r.match_type === 'vector' ? '<span class="badge b-warn">유사 검색 결과</span>' : ''}</div>
      <div class="mms-grid">${r.messages.map((m, i) => {
        const id = `mms-${idx}-${i}`;
        return `<div class="mms">
          <div class="mms-head">${esc(m.customer)} <span class="muted small">제품 ${m.product_count}건</span>
            ${m.refund_notice ? '<span class="badge b-danger">환불 안내 포함</span>' : ''}${m.truncated ? '<span class="badge b-warn">일부 요약</span>' : ''}</div>
          <textarea id="${id}" aria-label="${esc(m.customer)} MMS 문구" spellcheck="false">${esc(m.text)}</textarea>
          <div class="mms-foot"><span class="bytes${m.bytes > m.max_bytes ? ' over' : ''}" data-bytes-for="${id}">${fmtInt(m.bytes)} / ${fmtInt(m.max_bytes)} bytes</span>
            <button class="small primary" data-copy="${id}">📋 복사</button></div>
        </div>`;
      }).join('')}</div>
      <details class="fold"><summary>제품 ${fmtInt(r.rows.length)}건 상세</summary>
        ${table([COL.pno, COL.order, COL.customer, COL.dlv, COL.status, COL.truck, COL.reason, COL.plan, COL.ton], r.rows, { rowClass: (x) => (x.status === '낙찰취소' ? 'cancelled' : '') })}
      </details>`;
  }
  return `<div class="msg-user">${esc(h.q)}</div>
    <div class="msg-bot${h.stale ? ' stale' : ''}">${h.stale ? `<div class="note info"><span>새로고침 전에 조회한 결과입니다. 지금 상태와 다를 수 있습니다.</span> <button class="small" data-reask="${idx}">다시 조회</button></div>` : ''}${bot}</div>`;
}

function renderChat(scroll = true) {
  $('chat-log').innerHTML = chatHistory.length ? chatHistory.map(renderChatEntry).join('')
    : `<div class="card">${emptyState('무엇을 조회할까요?', '아래 입력창에 제품번호·주문번호·고객사명 등을 입력하거나 예시를 눌러 보세요.')}</div>`;
  if (scroll) requestAnimationFrame(() => $('chat-log').lastElementChild?.scrollIntoView({ block: 'start', behavior: 'smooth' }));
}

async function ask(question) {
  const q = (question ?? $('chat-input').value).trim();
  if (!q) return;
  $('chat-input').value = '';
  autoGrow();
  store.del('ui.chat.draft');
  const entry = { q, ts: Date.now(), res: null };
  chatHistory.push(entry);
  const btn = $('btn-chat');
  await withLoading(btn, async () => {
    try { entry.res = await sendJson('POST', '/api/chat', { question: q }); } catch (e) { entry.error = e.message; }
  });
  chatHistory = chatHistory.slice(-MAX_HISTORY);
  saveChat();
  renderChat();
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

function autoGrow() {
  const ta = $('chat-input');
  ta.style.height = 'auto';
  ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
}

/** '으로'/'로': 받침이 없거나 ㄹ 받침이면 '로' (본사로, 공장으로, 울산로) */
function josaRo(word) {
  const code = word.trim().charCodeAt(word.trim().length - 1) - 0xac00;
  if (code < 0 || code > 11171) return '로';
  const jong = code % 28;
  return jong === 0 || jong === 8 ? '로' : '으로';
}

async function initChatExamples() {
  const box = $('chat-examples');
  if (box.dataset.ready) return;
  try {
    const d = state.dispatch || await api('/api/dispatch');
    state.dispatch = d;
    const all = [...d.trucks.flatMap((t) => t.products), ...d.unassigned, ...d.waiting_payment];
    const p = all.find((x) => x.order_no) || all[0];
    if (!p) { box.innerHTML = '<span class="muted small">데이터를 업로드하면 예시가 나타납니다.</span>'; return; }
    const examples = [
      [p.product_no, '제품번호'],
      p.order_no && [`${p.order_no} 배차됐어?`, '주문번호'],
      p.customer && [p.customer.replace(/\(주\)|주식회사|㈜/g, '').trim(), '고객사'],
      p.dlv_name && [`${p.dlv_name}${josaRo(p.dlv_name)} 가는 제품`, '인도처'],
    ].filter(Boolean);
    box.innerHTML = '<span class="muted small">예시</span>' + examples.map(([q, label]) => `<button class="chip" data-example="${esc(q)}" title="${esc(label)}로 조회">${esc(q)}</button>`).join('');
    box.dataset.ready = '1';
  } catch { box.innerHTML = ''; }
}

// =====================================================================
// 대시보드
// =====================================================================
async function loadDashboard() {
  try {
    const d = await api('/api/dashboard');
    const s = (k) => d.status[k] || { count: 0, ton: 0 };
    const kinds = {};
    d.by_category.forEach((x) => { (kinds[x.kind] ||= {})[x.status] = x; });
    const catRows = Object.entries(kinds).map(([kind, st]) => {
      const total = ['편성', '미배차', '출하완료'].reduce((a, k) => a + (st[k]?.ton || 0), 0) || 1;
      return { kind, st, total };
    });
    $('dashboard').innerHTML = `
      <div class="stats">
        <div class="stat"><span>총 제품</span><b>${fmtInt(d.total_products)}</b><small>누적 업로드 기준</small></div>
        <div class="stat s-assigned"><span>편성 차량 (출하 전)</span><b>${fmtInt(d.active_trucks)}대</b><small>${fmtTon(d.assigned_ton)}톤</small></div>
        <div class="stat s-assigned"><span>편성 제품</span><b>${fmtInt(s('편성').count)}</b><small>${fmtTon(s('편성').ton)}톤</small></div>
        <div class="stat s-unassigned"><span>미배차</span><b>${fmtInt(s('미배차').count)}</b><small>${fmtTon(s('미배차').ton)}톤</small></div>
        <div class="stat s-shipped"><span>출하완료</span><b>${fmtInt(s('출하완료').count)}</b><small>${fmtTon(s('출하완료').ton)}톤</small></div>
        <div class="stat"><span>입금대기</span><b>${fmtInt(s('입금대기').count)}</b><small>${fmtTon(s('입금대기').ton)}톤</small></div>
        <div class="stat ${d.review_trucks ? 's-danger' : ''}"><span>확인 필요 차량</span><b>${fmtInt(d.review_trucks)}</b><small>편성 후 정보 변경·취소</small></div>
        <div class="stat ${s('데이터오류').count ? 's-danger' : ''}"><span>데이터 오류</span><b>${fmtInt(s('데이터오류').count)}</b><small>${d.embedding_pending ? `임베딩 대기 ${fmtInt(d.embedding_pending)}건` : '원본 확인 필요'}</small></div>
      </div>
      <div class="dash-cols">
        <div class="card">
          <div class="card-head"><h2>분류별 현황</h2></div>
          <div class="legend"><span><i style="background:var(--assigned)"></i>편성</span><span><i style="background:var(--unassigned)"></i>미배차</span><span><i style="background:var(--shipped)"></i>출하완료</span></div>
          ${catRows.length ? table([
            { label: '분류', html: (x) => `<b>${esc(x.kind)}</b>` },
            { label: '비율', html: (x) => `<div class="hbar">${['편성', '미배차', '출하완료'].map((k) => `<i class="h-${k}" style="width:${((x.st[k]?.ton || 0) / x.total) * 100}%" title="${k} ${fmtTon(x.st[k]?.ton)}톤"></i>`).join('')}</div>` },
            { label: '편성', num: true, get: (x) => `${fmtInt(x.st['편성']?.count)}건 · ${fmtTon(x.st['편성']?.ton)}톤` },
            { label: '미배차', num: true, get: (x) => `${fmtInt(x.st['미배차']?.count)}건 · ${fmtTon(x.st['미배차']?.ton)}톤` },
            { label: '출하완료', num: true, get: (x) => `${fmtInt(x.st['출하완료']?.count)}건` },
          ], catRows) : emptyState('아직 데이터가 없습니다.')}
        </div>
        <div class="card">
          <div class="card-head"><h2>최근 업로드</h2></div>
          ${d.recent_uploads.length ? table([
            { label: '일시', get: (x) => x.uploaded_at }, { label: '파일', get: (x) => x.file_name },
            { label: '신규', num: true, get: (x) => fmtInt(x.new_cnt) }, { label: '중복', num: true, get: (x) => fmtInt(x.dup_cnt) },
            { label: '오류', num: true, get: (x) => fmtInt(x.error_cnt) },
          ], d.recent_uploads) : emptyState('업로드 이력이 없습니다.')}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>이월 미배차 TOP 20</h2><span class="muted">여러 번 배차를 시도했지만 남아 있는 제품입니다.</span></div>
        ${d.carryover_top.length ? table([COL.pno, COL.order, COL.customer, COL.dlv, COL.cat, COL.region, COL.ton, COL.carry, COL.reason], d.carryover_top)
          : emptyState('이월된 미배차 제품이 없습니다.')}
      </div>
      <div class="card admin-card">
        <div class="card-head"><h2>데이터 관리</h2></div>
        <div class="admin-row">
          <p>업로드한 데이터와 배차 결과를 모두 지우고 처음 상태로 돌립니다. 지우기 직전에 자동으로 백업하므로 되돌릴 수 있습니다.</p>
          <button id="btn-reset" class="danger" data-total="${d.total_products}" data-trucks="${d.active_trucks}">데이터 초기화</button>
        </div>
        <details class="fold" id="backup-fold"><summary>백업에서 되돌리기 (최근 7일)</summary><div id="backup-list" class="muted small">불러오는 중…</div></details>
      </div>`;
    loadBackups();
  } catch (e) { toast(e.message, 'error'); }
}

// =====================================================================
// 이벤트 연결
// =====================================================================
// 탭 (클릭 + 좌우 화살표)
$$('.tabs [role=tab]').forEach((b) => b.addEventListener('click', (e) => showTab(b.dataset.tab, { focusInput: e.detail > 0 })));
document.querySelector('.tabs').addEventListener('keydown', (e) => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
  const tabs = $$('.tabs [role=tab]');
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].focus();
  showTab(tabs[next].dataset.tab);
  e.preventDefault();
});

// 업로드
const dz = $('dropzone');
$('upload-files').addEventListener('change', (e) => setFiles(e.target.files));
dz.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('upload-files').click(); } });
['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => { if (state.drag) return; e.preventDefault(); dz.classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, () => dz.classList.remove('over')));
dz.addEventListener('drop', (e) => { if (state.drag) return; e.preventDefault(); setFiles(e.dataTransfer.files); });
// 창 아무 곳에나 파일을 떨어뜨려 브라우저가 파일을 열어버리는 것을 막는다
window.addEventListener('dragover', (e) => { if (!state.drag && e.dataTransfer?.types?.includes('Files')) e.preventDefault(); });
window.addEventListener('drop', (e) => { if (!state.drag && e.dataTransfer?.types?.includes('Files')) e.preventDefault(); });
$('btn-clear-files').addEventListener('click', () => { $('upload-files').value = ''; setFiles([]); });
$('btn-upload').addEventListener('click', doUpload);
$('btn-places').addEventListener('click', uploadPlaces);

// 배차
$('btn-dispatch').addEventListener('click', runDispatch);
$('f-date').addEventListener('change', (e) => setFilter('date', e.target.value));
$('f-cat').addEventListener('change', (e) => setFilter('cat', e.target.value));
$('f-region').addEventListener('change', (e) => setFilter('region', e.target.value));
let custTimer;
$('f-cust').addEventListener('input', (e) => { clearTimeout(custTimer); custTimer = setTimeout(() => setFilter('cust', e.target.value.trim()), 200); });
$('btn-reset-filters').addEventListener('click', () => { Object.assign(filters, { date: '', cat: '', region: '', cust: '' }); store.set('ui.filters', filters); renderDispatch(); });
$('btn-move').addEventListener('click', () => {
  const pno = $('move-product').value.trim();
  if (!pno) return toast('제품번호를 입력해 주세요.', 'error');
  moveProduct(pno, $('move-truck').value.trim() || null);
});
$('un-sort').addEventListener('change', (e) => { store.set('ui.unsort', e.target.value); renderUnassigned(); });

// 챗봇
const chatInput = $('chat-input');
chatInput.value = store.get('ui.chat.draft', '');
chatInput.addEventListener('input', () => { store.set('ui.chat.draft', chatInput.value); autoGrow(); });
chatInput.addEventListener('keydown', (e) => {
  // 한글 조합 중 Enter 는 글자 확정이므로 전송하지 않는다
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); ask(); }
});
$('btn-chat').addEventListener('click', () => ask());
$('btn-clear-chat').addEventListener('click', () => { chatHistory = []; store.del(CHAT_KEY); renderChat(false); });

// 위임 이벤트
document.addEventListener('click', async (e) => {
  const t = e.target.closest('[data-toggle],[data-save],[data-copy],[data-example],[data-reask],[data-dismantle]');
  if (!t) return;
  if (t.dataset.dismantle) {
    const id = t.dataset.dismantle;
    const truck = state.dispatch.trucks.find((x) => x.truck_id === id);
    const n = truck ? liveProducts(truck).length : 0;
    if (!window.confirm(`차량 ${id}을(를) 해체하고 제품 ${n}건을 미배차로 되돌릴까요?`)) return;
    await withLoading(t, async () => {
      try {
        const r = await sendJson('POST', '/api/dispatch/dismantle', { truck_id: id });
        toast(`${r.truck_id} 해체: 제품 ${r.released}건을 미배차로 되돌렸습니다.`, 'success');
        await loadDispatch(true);
      } catch (err) { toast(err.message, 'error'); }
    });
  } else if (t.dataset.toggle) {
    const id = t.dataset.toggle;
    if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
    store.set('ui.expanded', [...state.expanded].slice(-200));
    const card = t.closest('.truck');
    const truck = state.dispatch.trucks.find((x) => x.truck_id === id);
    card.outerHTML = truckCard(truck);
  } else if (t.dataset.save) {
    saveAddress(t.dataset.save, t);
  } else if (t.dataset.copy) {
    const ok = await copyText($(t.dataset.copy).value);
    toast(ok ? '복사했습니다. 문자 앱에 붙여넣으세요.' : '복사하지 못했습니다. 문구를 직접 선택해서 복사해 주세요.', ok ? 'success' : 'error');
  } else if (t.dataset.example) {
    ask(t.dataset.example);
  } else if (t.dataset.reask) {
    ask(chatHistory[Number(t.dataset.reask)].q);
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.matches('input[data-addr]')) {
    saveAddress(e.target.dataset.addr, e.target.parentElement.querySelector('[data-save]'));
  }
});
document.addEventListener('input', (e) => {
  const id = e.target.id;
  if (!id || !id.startsWith('mms-')) return;
  const el = document.querySelector(`[data-bytes-for="${id}"]`);
  const n = mmsBytes(e.target.value);
  el.textContent = `${fmtInt(n)} / 2,000 bytes`;
  el.classList.toggle('over', n > 2000);
});

// 백업 목록 · 복원
const BACKUP_KIND = { auto: '자동(하루 1회)', manual: '수동', prerestore: '복원 직전', prereset: '초기화 직전' };
const backupTime = (name) => `${name.slice(0, 4)}-${name.slice(4, 6)}-${name.slice(6, 8)} ${name.slice(9, 11)}:${name.slice(11, 13)}:${name.slice(13, 15)}`;

async function loadBackups() {
  const box = $('backup-list');
  if (!box) return;
  try {
    const { backups } = await api('/api/backups');
    box.innerHTML = backups.length ? table([
      { label: '백업 시각', get: (b) => backupTime(b.name) },
      { label: '종류', get: (b) => BACKUP_KIND[b.kind] || b.kind },
      { label: '크기', num: true, get: (b) => `${(b.size / 1024).toFixed(0)} KB` },
      { label: '', html: (b) => `<button class="small" data-restore="${esc(b.name)}">이 시점으로 복원</button>` },
    ], backups) : emptyState('백업이 없습니다.');
  } catch (e) { box.textContent = e.message; }
}

async function restoreBackup(name, btn) {
  if (!window.confirm(`${backupTime(name)} 백업으로 되돌릴까요?\n지금 데이터는 '복원 직전' 백업으로 남겨 두므로 다시 되돌릴 수 있습니다.`)) return;
  await withLoading(btn, async () => {
    try {
      await sendJson('POST', '/api/restore', { name });
      state.dispatch = null;
      toast(`${backupTime(name)} 시점으로 되돌렸습니다.`, 'success');
      checkHealth();
      refreshCounts();
      await loadDashboard();
      $('backup-fold')?.setAttribute('open', '');
    } catch (err) { toast(err.message, 'error'); }
  });
}
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-restore]');
  if (b) restoreBackup(b.dataset.restore, b);
});

// 데이터 초기화
const resetDialog = $('reset-dialog');
document.addEventListener('click', (e) => {
  const btn = e.target.closest('#btn-reset');
  if (!btn) return;
  $('reset-count').textContent = fmtInt(btn.dataset.total);
  $('reset-trucks').textContent = fmtInt(btn.dataset.trucks);
  $('reset-confirm').value = '';
  $('reset-submit').disabled = true;
  $('reset-keep-places').checked = true;
  resetDialog.showModal();
  $('reset-confirm').focus();
});
$('reset-confirm').addEventListener('input', (e) => { $('reset-submit').disabled = e.target.value.trim() !== '초기화'; });
$('reset-cancel').addEventListener('click', () => resetDialog.close());
$('reset-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if ($('reset-confirm').value.trim() !== '초기화') return;
  await withLoading($('reset-submit'), async () => {
    try {
      const r = await sendJson('POST', '/api/reset', { confirm: '초기화', keep_places: $('reset-keep-places').checked });
      resetDialog.close();
      // 화면 상태도 새로 시작 (지워진 데이터를 가리키는 대화 내역·펼침 상태 정리)
      state.dispatch = null;
      state.expanded.clear();
      store.del('ui.expanded');
      chatHistory = [];
      store.del(CHAT_KEY);
      renderChat(false);
      delete $('chat-examples').dataset.ready;
      $('chat-examples').innerHTML = '';
      $('dispatch-summary').innerHTML = '';
      $('upload-result').innerHTML = '';
      toast(`데이터를 초기화했습니다. 되돌리려면 아래 '백업에서 되돌리기'에서 ${backupTime(r.backup)} (초기화 직전)을 복원하세요.`, 'success', 8000);
      checkHealth();
      refreshCounts();
      loadDashboard();
    } catch (err) { toast(err.message, 'error'); }
  });
  $('reset-submit').disabled = $('reset-confirm').value.trim() !== '초기화';
});

// 시작
renderChat(false);
autoGrow();
checkHealth().then(() => showTab(store.get('ui.tab', 'upload')));
refreshCounts();
setInterval(checkHealth, 20000);
