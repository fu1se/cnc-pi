'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

let ws = null, state = null, job = null;
let droMode = 'wpos', step = 1, selected = null;
let settings = {}, dirty = {};
const toolPos = [0, 0, 0];

/* ============ WebSocket ============ */
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = ev => handle(JSON.parse(ev.data));
  ws.onclose = () => { setPill('нет связи', 'alarm'); setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
}

function handle(m) {
  if (m.t === 'hello') {
    $('#log').innerHTML = '';
    (m.log || []).forEach(addLog);
    settings = m.settings || {};
    renderSettings();
    applyState(m.s, m.job);
  } else if (m.t === 'state') {
    applyState(m.s, m.job);
  } else if (m.t === 'log') {
    addLog(m);
  }
}

function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
const cmd = line => send({ t: 'cmd', line });
const rt = name => send({ t: 'rt', name });

async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  const j = await r.json().catch(() => ({ ok: false, error: 'нет ответа' }));
  if (!j.ok) toast(j.error || 'ошибка', true);
  return j;
}

/* ============ состояние ============ */
function applyState(s, j) {
  state = s; job = j;
  const st = (s.state || '—').split(':')[0];
  setPill(RU[st] || st, st.toLowerCase());
  $('#portInfo').textContent = s.connected
    ? `${s.port} · ${s.version || 'GRBL'}`
    : (s.port ? 'порт закрыт' : 'станок не подключён');

  const p = s[droMode] || [0, 0, 0];
  ['X', 'Y', 'Z'].forEach((a, i) => $('#dro' + a).textContent = (p[i] ?? 0).toFixed(3));
  toolPos[0] = (s.wpos || [0, 0, 0])[0]; toolPos[1] = (s.wpos || [0, 0, 0])[1];
  $('#feedVal').textContent = Math.round(s.feed || 0);
  $('#spVal').textContent = Math.round(s.spindle || 0);
  $('#bufVal').textContent = s.buf && s.buf[1] ? `${s.buf[0]}/${s.buf[1]}` : '—';

  // баннер аварии: заметная кнопка разблокировки
  const alarm = st === 'Alarm';
  $('#alarmBar').classList.toggle('hidden', !alarm);
  if (alarm) $('#alarmMsg').textContent = s.message || 'Авария — станок заблокирован';

  const msg = (s.message || '');
  const msgShown = (!alarm && msg.startsWith('ALARM')) ? '' : msg;  // не тянуть старую аварию
  $('#msgLine').textContent = s.pins ? 'Активные входы: ' + s.pins : msgShown;

  // оверрайды (Ov: подача / быстрый ход / шпиндель)
  const ov = s.ov || [100, 100, 100];
  $('#ovFeed').textContent = ov[0] + '%';
  $('#ovRapid').textContent = ov[1] + '%';
  $('#ovSpindle').textContent = ov[2] + '%';

  if (j.running || (j.name && j.total)) {
    $('#progWrap').classList.remove('hidden');
    const pct = j.total ? Math.min(100, (j.done || 0) / j.total * 100) : 0;
    $('#jobName').textContent = j.name || '';
    $('#jobPct').textContent = pct.toFixed(1) + '%';
    $('#barFill').style.width = pct + '%';
    $('#jobLines').textContent = `${j.done || 0} / ${j.total}` +
      (j.line > (j.done || 0) ? ` (буфер ${j.line - j.done})` : '');
    $('#jobTime').textContent = dur(j.elapsed);
    $('#jobEta').textContent = (j.running && pct > 1)
      ? '~' + dur(j.elapsed * (100 - pct) / pct) : '';
    $('#btnJobPause').textContent = j.paused ? 'Продолжить' : 'Пауза';
    $('#btnJobPause').disabled = !j.running;
  } else {
    $('#progWrap').classList.add('hidden');
  }
  draw();
}

const RU = {
  Idle: 'простой', Run: 'работа', Hold: 'пауза', Jog: 'джог', Alarm: 'АВАРИЯ',
  Door: 'дверь', Check: 'проверка', Home: 'базир.', Sleep: 'сон'
};

function setPill(text, cls) {
  const el = $('#statePill');
  el.textContent = text;
  el.className = 'pill ' + (cls || '');
}

const dur = s => {
  s = Math.max(0, Math.round(s || 0));
  const h = (s / 3600) | 0, m = ((s % 3600) / 60) | 0, x = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}`
           : `${m}:${String(x).padStart(2, '0')}`;
};

function addLog(e) {
  if (e.dir === 'tx' && !$('#showTx').checked) return;
  const box = $('#log');
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  const div = document.createElement('div');
  div.className = 'l-' + e.dir;
  const tag = { tx: '»', rx: '«', err: '!', sys: '·' }[e.dir] || ' ';
  div.innerHTML = `<span class="tag">${tag}</span><span class="txt"></span>`;
  div.lastChild.textContent = e.line;
  box.appendChild(div);
  while (box.childElementCount > 500) box.firstChild.remove();
  if (stick) box.scrollTop = box.scrollHeight;
}

function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (bad ? ' bad' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.className = '', 2600);
}

/* ============ джог ============ */
const STEPS = [0.01, 0.1, 1, 5, 10, 50];
function buildSteps() {
  $('#steps').innerHTML = '';
  STEPS.forEach(v => {
    const b = document.createElement('button');
    b.textContent = v;
    b.classList.toggle('active', v === step);
    b.onclick = () => { step = v; buildSteps(); };
    $('#steps').appendChild(b);
  });
}

function jog(spec) {
  const axes = {};
  spec.replace(/([+-])([XYZ])/g, (_, sign, ax) => axes[ax] = (sign === '-' ? -step : step));
  if (Object.keys(axes).length) send({ t: 'jog', axes, feed: +$('#jogFeed').value });
}

/* ============ файлы ============ */
async function loadFiles() {
  const r = await fetch('/api/files').then(x => x.json());
  const ul = $('#fileList');
  ul.innerHTML = '';
  if (!r.files.length) {
    ul.innerHTML = '<li class="muted small">файлов нет</li>';
    return;
  }
  // при первой загрузке показать первый файл в визуализаторе
  if (!selected && r.files.length) { selected = r.files[0].name; preview(selected); }
  r.files.forEach(f => {
    const li = document.createElement('li');
    if (f.name === selected) li.className = 'sel';
    const name = document.createElement('span');
    name.className = 'fname'; name.textContent = f.name;
    name.onclick = () => { selected = f.name; loadFiles(); preview(f.name); };
    const size = document.createElement('span');
    size.className = 'fsize';
    size.textContent = f.size > 1e6 ? (f.size / 1e6).toFixed(1) + ' МБ' : Math.ceil(f.size / 1024) + ' КБ';
    const run = document.createElement('button');
    run.className = 'btn ok'; run.innerHTML = '<svg class="ic"><use href="#i-play"/></svg>';
    run.title = 'Запустить';
    run.onclick = async () => {
      if (!confirm(`Запустить «${f.name}»?\nПроверьте нули и высоту Z.`)) return;
      const j = await post('/api/job', { action: 'start', name: f.name });
      if (j.ok) { selected = f.name; preview(f.name); }
    };
    const del = document.createElement('button');
    del.className = 'btn'; del.innerHTML = '<svg class="ic"><use href="#i-x"/></svg>';
    del.title = 'Удалить';
    del.onclick = async () => {
      if (!confirm('Удалить ' + f.name + '?')) return;
      await fetch('/api/files/' + encodeURIComponent(f.name), { method: 'DELETE' });
      loadFiles();
    };
    li.append(name, size, run, del);
    ul.appendChild(li);
  });
}

async function upload(files) {
  const fd = new FormData();
  [...files].forEach(f => fd.append('file', f));
  const r = await fetch('/api/files', { method: 'POST', body: fd }).then(x => x.json());
  if (r.ok) { toast('загружено: ' + r.files.map(f => f.name).join(', ')); loadFiles(); }
  else toast('ошибка загрузки', true);
}

/* ============ парсер траектории ============ */
let path = { segs: [], min: [0, 0], max: [0, 0] };
let view = { scale: 1, ox: 0, oy: 0, fitted: false };

async function preview(name) {
  $('#viewInfo').textContent = 'разбор…';
  const txt = await fetch('/api/files/' + encodeURIComponent(name)).then(r => r.text());
  path = parseGcode(txt);
  view.fitted = false;
  $('#viewInfo').textContent = path.segs.length
    ? `${name} · ${path.segs.length} отр. · X ${path.min[0].toFixed(1)}…${path.max[0].toFixed(1)} · Y ${path.min[1].toFixed(1)}…${path.max[1].toFixed(1)} мм`
    : name + ' · нет перемещений';
  draw();
}

function parseGcode(text) {
  const segs = [];
  let pos = [0, 0, 0], abs = true, mm = 1, motion = 0, plane = 0;
  let min = [1e9, 1e9], max = [-1e9, -1e9];
  const bump = p => {
    min[0] = Math.min(min[0], p[0]); min[1] = Math.min(min[1], p[1]);
    max[0] = Math.max(max[0], p[0]); max[1] = Math.max(max[1], p[1]);
  };
  bump(pos);

  for (let raw of text.split('\n')) {
    const line = raw.replace(/\([^)]*\)/g, '').split(';')[0].toUpperCase();
    if (!line.trim()) continue;
    const words = line.match(/[A-Z]-?\d*\.?\d+/g);
    if (!words) continue;
    const w = {};
    for (const t of words) {
      const L = t[0], v = parseFloat(t.slice(1));
      if (L === 'G') {
        if (v === 0 || v === 1 || v === 2 || v === 3) motion = v;
        else if (v === 20) mm = 25.4;
        else if (v === 21) mm = 1;
        else if (v === 90) abs = true;
        else if (v === 91) abs = false;
        else if (v === 17) plane = 0;
        else if (v === 18) plane = 1;
        else if (v === 19) plane = 2;
      } else w[L] = v;
    }
    if (!('X' in w || 'Y' in w || 'Z' in w || 'I' in w || 'J' in w || 'R' in w)) continue;

    const tgt = pos.slice();
    ['X', 'Y', 'Z'].forEach((ax, i) => {
      if (ax in w) tgt[i] = abs ? w[ax] * mm : pos[i] + w[ax] * mm;
    });

    if ((motion === 2 || motion === 3) && plane === 0) {
      let cx, cy;
      if ('R' in w) {
        const r = w.R * mm, dx = tgt[0] - pos[0], dy = tgt[1] - pos[1];
        const d = Math.hypot(dx, dy);
        if (d < 1e-9 || d > 2 * Math.abs(r) + 1e-6) { pos = tgt; continue; }
        const h = Math.sqrt(Math.max(0, r * r - d * d / 4)) * (motion === 2 ? 1 : -1) * Math.sign(r);
        cx = pos[0] + dx / 2 - h * dy / d;
        cy = pos[1] + dy / 2 + h * dx / d;
      } else {
        cx = pos[0] + ('I' in w ? w.I * mm : 0);
        cy = pos[1] + ('J' in w ? w.J * mm : 0);
      }
      const r0 = Math.hypot(pos[0] - cx, pos[1] - cy);
      let a0 = Math.atan2(pos[1] - cy, pos[0] - cx);
      let a1 = Math.atan2(tgt[1] - cy, tgt[0] - cx);
      if (motion === 2) { while (a1 >= a0) a1 -= 2 * Math.PI; }
      else { while (a1 <= a0) a1 += 2 * Math.PI; }
      const steps = Math.max(6, Math.min(400, Math.ceil(Math.abs(a1 - a0) / 0.12)));
      let prev = pos;
      for (let k = 1; k <= steps; k++) {
        const a = a0 + (a1 - a0) * k / steps;
        const p = [cx + r0 * Math.cos(a), cy + r0 * Math.sin(a), tgt[2]];
        segs.push([prev[0], prev[1], p[0], p[1], 1]);
        bump(p);
        prev = p;
      }
      pos = tgt;
      continue;
    }

    if (tgt[0] !== pos[0] || tgt[1] !== pos[1]) {
      segs.push([pos[0], pos[1], tgt[0], tgt[1], motion === 0 ? 0 : 1]);
      bump(tgt);
    }
    pos = tgt;
    if (segs.length > 200000) break;    // защита от гигантских файлов на Pi
  }
  if (min[0] > max[0]) { min = [0, 0]; max = [0, 0]; }
  return { segs, min, max };
}

/* ============ отрисовка ============ */
const cv = $('#canvas'), ctx = cv.getContext('2d');

function fit() {
  const w = cv.width / devicePixelRatio, h = cv.height / devicePixelRatio;
  const dx = Math.max(1e-3, path.max[0] - path.min[0]);
  const dy = Math.max(1e-3, path.max[1] - path.min[1]);
  view.scale = Math.min((w - 48) / dx, (h - 48) / dy);
  view.ox = w / 2 - (path.min[0] + dx / 2) * view.scale;
  view.oy = h / 2 + (path.min[1] + dy / 2) * view.scale;
  view.fitted = true;
}

function resize() {
  const r = cv.getBoundingClientRect();
  cv.width = r.width * devicePixelRatio;
  cv.height = r.height * devicePixelRatio;
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  view.fitted = false;
  draw();
}

function draw() {
  if (!cv.width) return;
  if (!view.fitted) fit();
  const w = cv.width / devicePixelRatio, h = cv.height / devicePixelRatio;
  const X = x => view.ox + x * view.scale, Y = y => view.oy - y * view.scale;

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, w, h);

  // сетка 10 мм
  const gs = 10 * view.scale;
  if (gs > 6) {
    ctx.strokeStyle = '#eef1f4'; ctx.lineWidth = 1; ctx.beginPath();
    for (let x = X(0) % gs; x < w; x += gs) { ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, h); }
    for (let y = Y(0) % gs; y < h; y += gs) { ctx.moveTo(0, y + .5); ctx.lineTo(w, y + .5); }
    ctx.stroke();
  }
  // оси нуля детали
  ctx.strokeStyle = '#cfd6de'; ctx.lineWidth = 1; ctx.beginPath();
  ctx.moveTo(0, Y(0) + .5); ctx.lineTo(w, Y(0) + .5);
  ctx.moveTo(X(0) + .5, 0); ctx.lineTo(X(0) + .5, h);
  ctx.stroke();

  // траектория: cut = сплошной синий, rapid(G0) = серый пунктир
  // cut рисуем поверх холостых
  for (const cut of [0, 1]) {
    ctx.beginPath();
    ctx.strokeStyle = cut ? '#2563eb' : '#b6bdc7';
    ctx.lineWidth = cut ? 1.6 : 1;
    if (!cut) ctx.setLineDash([4, 4]);
    for (const s of path.segs) {
      if (s[4] !== cut) continue;
      ctx.moveTo(X(s[0]), Y(s[1]));
      ctx.lineTo(X(s[2]), Y(s[3]));
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // инструмент
  const tx = X(toolPos[0]), ty = Y(toolPos[1]);
  ctx.strokeStyle = '#e5484d'; ctx.lineWidth = 1.8;
  ctx.beginPath(); ctx.arc(tx, ty, 6, 0, 7); ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(tx - 11, ty); ctx.lineTo(tx - 3, ty);
  ctx.moveTo(tx + 3, ty); ctx.lineTo(tx + 11, ty);
  ctx.moveTo(tx, ty - 11); ctx.lineTo(tx, ty - 3);
  ctx.moveTo(tx, ty + 3); ctx.lineTo(tx, ty + 11);
  ctx.stroke();
}

// панорама и зум
function zoomBy(k, cx, cy) {
  const r = cv.getBoundingClientRect();
  if (cx === undefined) { cx = r.width / 2; cy = r.height / 2; }
  view.ox = cx - (cx - view.ox) * k;
  view.oy = cy - (cy - view.oy) * k;
  view.scale *= k; draw();
}
let drag = null;
cv.addEventListener('pointerdown', e => { drag = { x: e.offsetX, y: e.offsetY }; cv.setPointerCapture(e.pointerId); });
cv.addEventListener('pointermove', e => {
  if (!drag) return;
  view.ox += e.offsetX - drag.x; view.oy += e.offsetY - drag.y;
  drag = { x: e.offsetX, y: e.offsetY }; draw();
});
cv.addEventListener('pointerup', () => drag = null);
cv.addEventListener('wheel', e => {
  e.preventDefault();
  zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.offsetX, e.offsetY);
}, { passive: false });

/* ============ настройки GRBL ============ */
const SETTING_DESC = {
  0: 'Длительность импульса шага, мкс', 1: 'Задержка отключения моторов, мс',
  2: 'Инверсия импульса шага (маска)', 3: 'Инверсия направления (маска)',
  4: 'Инверсия сигнала enable', 5: 'Инверсия концевиков', 6: 'Инверсия щупа',
  10: 'Состав отчёта о статусе', 11: 'Отклонение на стыке, мм',
  12: 'Точность дуг, мм', 13: 'Отчёты в дюймах', 20: 'Программные пределы',
  21: 'Аппаратные концевики', 22: 'Цикл базирования', 23: 'Инверсия направления homing',
  24: 'Подача точного поиска, мм/мин', 25: 'Подача поиска, мм/мин',
  26: 'Антидребезг, мс', 27: 'Отход после homing, мм',
  30: 'Максимум оборотов шпинделя (S)', 31: 'Минимум оборотов шпинделя',
  32: 'Лазерный режим', 100: 'Шагов на мм по X', 101: 'Шагов на мм по Y',
  102: 'Шагов на мм по Z', 110: 'Макс. скорость X, мм/мин',
  111: 'Макс. скорость Y, мм/мин', 112: 'Макс. скорость Z, мм/мин',
  120: 'Ускорение X, мм/с²', 121: 'Ускорение Y, мм/с²', 122: 'Ускорение Z, мм/с²',
  130: 'Ход оси X, мм', 131: 'Ход оси Y, мм', 132: 'Ход оси Z, мм'
};

function renderSettings() {
  const t = $('#settingsTable');
  t.innerHTML = '';
  const keys = Object.keys(settings).map(Number).sort((a, b) => a - b);
  if (!keys.length) {
    t.innerHTML = '<tr><td colspan="3" class="muted">нажмите «Прочитать $$»</td></tr>';
    return;
  }
  for (const k of keys) {
    const tr = document.createElement('tr');
    const inp = document.createElement('input');
    inp.value = settings[k];
    inp.oninput = () => {
      const changed = inp.value !== settings[k];
      inp.classList.toggle('dirty', changed);
      if (changed) dirty[k] = inp.value; else delete dirty[k];
    };
    const td1 = document.createElement('td'); td1.textContent = '$' + k;
    const td2 = document.createElement('td'); td2.appendChild(inp);
    const td3 = document.createElement('td'); td3.textContent = SETTING_DESC[k] || '';
    tr.append(td1, td2, td3);
    t.appendChild(tr);
  }
}

/* ============ камера ============ */
let camAvail = false, camWanted = false;

async function initCamera() {
  let info;
  try { info = await fetch('/api/camera').then(r => r.json()); } catch { return; }
  camAvail = !!(info && info.available);
  if (!camAvail) return;               // камеры нет — карточка остаётся скрытой
  $('#camCard').classList.remove('hidden');
  const img = $('#camImg');
  img.onload = () => { $('#camMsg').textContent = ''; };
  img.onerror = () => {
    if (!(camAvail && camWanted && !document.hidden)) return;
    $('#camMsg').textContent = 'нет сигнала — переподключение…';
    img.removeAttribute('src');
    setTimeout(applyCam, 2000);
  };
  camWanted = true;
  applyCam();
  document.addEventListener('visibilitychange', applyCam);
}

function applyCam() {
  const img = $('#camImg');
  const stream = camAvail && camWanted && !document.hidden;
  if (stream) {
    if (!img.getAttribute('src')) {
      $('#camMsg').textContent = 'подключение…';
      img.src = '/camera/stream?ts=' + Date.now();
    }
    $('#camLive').classList.remove('hidden');
  } else {
    img.removeAttribute('src');
    $('#camLive').classList.add('hidden');
    $('#camMsg').textContent = camWanted ? 'пауза (вкладка неактивна)' : 'пауза';
  }
  $('#btnCamToggle').innerHTML = camWanted
    ? '<svg class="ic"><use href="#i-pause"/></svg>'
    : '<svg class="ic"><use href="#i-play"/></svg>';
}

/* ============ события ============ */
function bind() {
  $('#btnUnlock').onclick = () => cmd('$X');
  $('#btnHome').onclick = () => { if (confirm('Запустить базирование $H?')) cmd('$H'); };
  $('#btnAlarmUnlock').onclick = () => cmd('$X');
  $('#btnAlarmHome').onclick = () => { if (confirm('Запустить базирование $H?')) cmd('$H'); };
  $('#btnCamToggle').onclick = () => { camWanted = !camWanted; applyCam(); };
  $('#btnHold').onclick = () => rt('hold');
  $('#btnResume').onclick = () => rt('resume');
  $('#btnReset').onclick = () => rt('reset');
  $('#btnJogCancel').onclick = () => rt('jog_cancel');
  $('#btnGoZero').onclick = () => cmd('G90G21G0X0Y0');
  $('#btnFit').onclick = () => { view.fitted = false; draw(); };
  $('#btnZoomIn').onclick = () => zoomBy(1.25);
  $('#btnZoomOut').onclick = () => zoomBy(1 / 1.25);

  $$('[data-dro]').forEach(b => b.onclick = () => {
    droMode = b.dataset.dro;
    $$('[data-dro]').forEach(x => x.classList.toggle('active', x === b));
    if (state) applyState(state, job);
  });
  $$('[data-zero]').forEach(b => b.onclick = () =>
    post('/api/zero', { axes: b.dataset.zero.split('') }));
  $$('[data-jog]').forEach(b => b.onclick = () => jog(b.dataset.jog));
  $$('[data-cmd]').forEach(b => b.onclick = () => cmd(b.dataset.cmd));
  $$('[data-rt]').forEach(b => b.onclick = () => rt(b.dataset.rt));
  $$('[data-tab]').forEach(b => b.onclick = () => {
    $$('[data-tab]').forEach(x => x.classList.toggle('active', x === b));
    $('#tabConsole').classList.toggle('hidden', b.dataset.tab !== 'console');
    $('#tabSettings').classList.toggle('hidden', b.dataset.tab !== 'settings');
  });

  $('#jogFeed').oninput = e => $('#jogFeedOut').textContent = e.target.value;
  $('#mdiForm').onsubmit = e => {
    e.preventDefault();
    const v = $('#mdi').value.trim();
    if (v) { cmd(v); $('#mdi').value = ''; }
  };
  $('#btnJobPause').onclick = () =>
    post('/api/job', { action: job && job.paused ? 'resume' : 'pause' });
  $('#btnJobStop').onclick = () => {
    if (confirm('Прервать задание? Будет мягкий сброс контроллера.'))
      post('/api/job', { action: 'stop' });
  };

  $('#fileInput').onchange = e => { upload(e.target.files); e.target.value = ''; };
  const drop = $('#drop');
  ['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault(); drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault(); drop.classList.remove('over');
  }));
  drop.addEventListener('drop', e => upload(e.dataTransfer.files));

  $('#btnReadSettings').onclick = async () => {
    settings = {}; dirty = {};
    await fetch('/api/settings');
    setTimeout(async () => {
      const r = await fetch('/api/settings?read=1').then(x => x.json());
      settings = r.settings || {}; renderSettings();
    }, 700);
  };
  $('#btnWriteSettings').onclick = async () => {
    const keys = Object.keys(dirty);
    if (!keys.length) return toast('нет изменений');
    if (!confirm('Записать в EEPROM:\n' + keys.map(k => `$${k}=${dirty[k]}`).join('\n'))) return;
    await post('/api/settings', { values: dirty });
    Object.assign(settings, dirty); dirty = {};
    renderSettings(); toast('записано');
  };

  // клавиатура
  const KEYS = {
    ArrowLeft: '-X', ArrowRight: '+X', ArrowUp: '+Y', ArrowDown: '-Y',
    PageUp: '+Z', PageDown: '-Z'
  };
  addEventListener('keydown', e => {
    if (/^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.code === 'Escape') { rt('reset'); return; }
    if (e.code === 'Space') { e.preventDefault(); rt(state && /Hold/.test(state.state) ? 'resume' : 'hold'); return; }
    const spec = KEYS[e.key];
    if (spec) { e.preventDefault(); jog(spec); }
    const idx = '123456'.indexOf(e.key);
    if (idx >= 0 && STEPS[idx] !== undefined) { step = STEPS[idx]; buildSteps(); }
  });
  addEventListener('keyup', e => { if (KEYS[e.key]) rt('jog_cancel'); });
  addEventListener('resize', resize);
}

buildSteps();
bind();
resize();
loadFiles();
connect();
initCamera();
