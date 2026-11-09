'use strict';

/* ShareBox 对等版网页端（原生 JS，无第三方依赖） */

const $ = (id) => document.getElementById(id);

const state = {
  token: localStorage.getItem('sb.token') || '',
  companyToken: sessionStorage.getItem('sb.company') || '',
  company: null,
  companyName: '',
  user: null,
  account: null,
  folders: [],
  peers: [],
  mirrors: [],
  folderId: null,
  shareRoot: true,
  folder: null,
  path: '',
  files: [],
  dirs: [],
  filter: '',
  view: 'mine',
  loading: false,
};

const IMAGE_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'];
const TEXT_EXT = ['txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'xml', 'log', 'ini', 'yml', 'yaml',
  'js', 'css', 'html', 'htm', 'py', 'ps1', 'bat', 'sh', 'sql', 'conf', 'toml'];

/* ------------------------------------------------------------------ 基础 */
function h(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === 'value') node.value = value;
      else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, value);
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function fmtSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return value.toFixed(value >= 10 ? 0 : 1) + ' ' + units[index];
}

function fmtTime(seconds) {
  if (!seconds) return '—';
  const date = new Date(Number(seconds) * 1000);
  if (Number.isNaN(date.getTime())) return '—';
  const pad = (x) => String(x).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fmtIso(text) {
  if (!text) return '—';
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  const pad = (x) => String(x).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function extOf(name) {
  const index = name.lastIndexOf('.');
  return index > 0 ? name.slice(index + 1).toLowerCase() : '';
}

function joinPath(dir, name) { return dir ? dir.replace(/\/+$/, '') + '/' + name : name; }
function basename(path) { const i = path.lastIndexOf('/'); return i < 0 ? path : path.slice(i + 1); }

function iconFor(name) {
  const ext = extOf(name);
  if (IMAGE_EXT.includes(ext)) return { cls: 'img', label: '图' };
  if (['doc', 'docx', 'rtf', 'wps', 'odt'].includes(ext)) return { cls: 'doc', label: '文' };
  if (['xls', 'xlsx', 'csv', 'et', 'ods'].includes(ext)) return { cls: 'sheet', label: '表' };
  if (['ppt', 'pptx', 'dps', 'odp'].includes(ext)) return { cls: 'doc', label: '演' };
  if (ext === 'pdf') return { cls: 'pdf', label: 'P' };
  if (['zip', 'rar', '7z', 'gz', 'tar', 'bz2'].includes(ext)) return { cls: 'zip', label: '包' };
  if (TEXT_EXT.includes(ext)) return { cls: '', label: 'T' };
  return { cls: '', label: '·' };
}

let toastTimer = null;
function toast(message, kind = '') {
  const node = $('toast');
  node.textContent = message;
  node.className = 'toast ' + kind;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, kind === 'error' ? 4200 : 2400);
}

async function pickFolder() {
  try {
    const data = await api('/api/pick-folder', { method: 'POST' });
    return data.path || '';
  } catch (err) {
    toast(err.message, 'error');
    return '';
  }
}

async function api(path, options = {}) {
  const opts = Object.assign({}, options);
  opts.headers = Object.assign({}, options.headers || {});
  if (state.token) opts.headers['Authorization'] = 'Bearer ' + state.token;
  if (opts.json !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  let res;
  try { res = await fetch(path, opts); }
  catch (err) { throw new Error('无法连接本机服务，请确认 ShareBox 正在运行'); }
  if (res.status === 401) {
    signOut(true);
    throw new Error('登录已过期，请重新登录');
  }
  const type = res.headers.get('Content-Type') || '';
  const data = type.includes('application/json') ? await res.json().catch(() => null) : await res.text();
  if (!res.ok) throw new Error((data && data.error) || ('请求失败（HTTP ' + res.status + '）'));
  return data;
}

/* ------------------------------------------------------------------ 弹窗 */
function openModal(title, body, foot, wide) {
  $('modal-title').textContent = title;
  $('modal-body').replaceChildren(body);
  $('modal-foot').replaceChildren(...(foot || []));
  document.querySelector('.modal-card').classList.toggle('wide', !!wide);
  $('modal').hidden = false;
  setTimeout(() => {
    const field = $('modal-body').querySelector('input, select, textarea, button');
    if (field) field.focus();
  }, 50);
}

function closeModal() {
  $('modal').hidden = true;
  $('modal-body').replaceChildren();
  $('modal-foot').replaceChildren();
}

$('modal-close').addEventListener('click', closeModal);
$('modal').addEventListener('click', (e) => { if (e.target === $('modal')) closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('modal').hidden) closeModal(); });

function confirmDialog(message, detail) {
  return new Promise((resolve) => {
    const done = (value) => { closeModal(); resolve(value); };
    openModal('请确认', h('div', {},
      h('p', { style: 'margin:0 0 6px;font-size:14.5px' }, message),
      detail ? h('p', { class: 'hint', style: 'margin:0' }, detail) : null,
    ), [
      h('button', { class: 'btn ghost', onclick: () => done(false) }, '取消'),
      h('button', { class: 'btn danger', onclick: () => done(true) }, '确定'),
    ]);
  });
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = h('a', { href: url, download: filename });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 20000);
}

function recoveryFile(code) {
  const account = state.account || {};
  return `ShareBox 恢复码\n` +
    `========================================\n` +
    `用户名：${account.username || ''}\n` +
    `设备名：${account.device_name || ''}\n` +
    `节点标识：${account.node_id || ''}\n` +
    `恢复码：${code}\n` +
    `生成时间：${new Date().toLocaleString()}\n\n` +
    `忘记密码时：打开 ShareBox 登录页 → 忘记密码 → 输入上面的恢复码 → 设置新密码。\n` +
    `请把这个文件保存在安全的地方，不要和电脑放在一起。\n`;
}

/* ------------------------------------------------------------------ 公司 / 登录 */
const COMPANY_KEY = 'sb.company';
const AUTH_FORMS = ['company-create-form', 'company-join-form', 'company-verify-form', 'login-form'];

function showOnlyForm(id) {
  for (const form of AUTH_FORMS) {
    const node = $(form);
    if (node) node.hidden = form !== id;
  }
  $('app-view').hidden = true;
  $('login-view').hidden = false;
}

function showCompanyCreate() {
  showOnlyForm('company-create-form');
  $('cc-error').textContent = '';
  setTimeout(() => $('cc-name').focus(), 30);
}

function showCompanyJoin() {
  showOnlyForm('company-join-form');
  $('cj-error').textContent = '';
  setTimeout(() => $('cj-id').focus(), 30);
}

function showCompanyVerify(message) {
  showOnlyForm('company-verify-form');
  $('verify-company-name').textContent = state.companyName || '公司验证';
  $('verify-error').textContent = message || '';
  $('verify-code').value = '';
  setTimeout(() => $('verify-code').focus(), 30);
}

function showLogin(message) {
  showOnlyForm('login-form');
  $('login-company').textContent = state.companyName ? `公司：${state.companyName}` : '公司已验证';
  $('login-error').textContent = message || '';
  $('login-pass').value = '';
  setTimeout(() => $('login-user').focus(), 30);
}

function setCompanyToken(token, company) {
  state.companyToken = token;
  sessionStorage.setItem(COMPANY_KEY, token);
  if (company) {
    state.company = company;
    state.companyName = company.name || state.companyName;
  }
}

function clearCompanyToken() {
  state.companyToken = '';
  sessionStorage.removeItem(COMPANY_KEY);
}

function signOut(silent) {
  state.token = '';
  state.user = null;
  state.account = null;
  state.folders = [];
  state.peers = [];
  state.mirrors = [];
  localStorage.removeItem('sb.token');
  clearCompanyToken();
  if (state.companyName) showCompanyVerify(silent ? '登录状态已失效，请重新验证公司' : '');
  else showCompanyCreate();
}

async function enterApp() {
  $('login-view').hidden = true;
  $('app-view').hidden = false;
  if (!state.user) {
    const me = await api('/api/me');
    state.user = me.user;
  }
  state.account = await api('/api/account');
  if (state.account.company) {
    state.company = state.account.company;
    state.companyName = state.account.company.name;
  }
  $('who').textContent = state.account.device_name || state.user.username;
  await Promise.all([loadFolders(), loadPeers(), loadMirrors()]);
  setView('mine');
}

async function afterJoin(data) {
  state.token = data.token;
  state.user = data.user;
  localStorage.setItem('sb.token', data.token);
  setCompanyToken(data.company_token, data.company);
  state.account = {
    username: data.user.username,
    device_name: data.device_name,
    node_id: data.node_id,
  };
  showRecovery(data.recovery_code, true);
}

$('company-create-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('cc-error');
  error.textContent = '';
  const code = $('cc-code').value;
  if (code !== $('cc-code2').value) { error.textContent = '两次输入的公司验证码不一致'; return; }
  if (code.length < 8) { error.textContent = '公司验证码至少 8 位'; return; }
  const button = $('company-create-form').querySelector('button[type=submit]');
  button.disabled = true;
  try {
    const data = await api('/api/company/create', {
      method: 'POST',
      json: {
        company_name: $('cc-name').value.trim(),
        code,
        username: $('cc-user').value.trim(),
        password: $('cc-pass').value,
        device_name: $('cc-device').value.trim(),
      },
    });
    await afterJoin(data);
  } catch (err) {
    error.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

$('company-join-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('cj-error');
  error.textContent = '';
  const button = $('company-join-form').querySelector('button[type=submit]');
  button.disabled = true;
  try {
    const data = await api('/api/company/join', {
      method: 'POST',
      json: {
        company_id: $('cj-id').value.trim().toUpperCase(),
        code: $('cj-code').value,
        username: $('cj-user').value.trim(),
        password: $('cj-pass').value,
        device_name: $('cj-device').value.trim(),
      },
    });
    await afterJoin(data);
  } catch (err) {
    error.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

$('company-verify-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('verify-error');
  error.textContent = '';
  const button = $('company-verify-form').querySelector('button[type=submit]');
  button.disabled = true;
  try {
    const data = await api('/api/company/verify', {
      method: 'POST',
      json: { code: $('verify-code').value },
    });
    setCompanyToken(data.company_token, data.company);
    showLogin('');
  } catch (err) {
    error.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

$('go-join').addEventListener('click', showCompanyJoin);
$('go-create').addEventListener('click', showCompanyCreate);

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('login-submit');
  button.disabled = true;
  button.textContent = '登录中…';
  $('login-error').textContent = '';
  try {
    const data = await api('/api/login', {
      method: 'POST',
      json: {
        username: $('login-user').value.trim(),
        password: $('login-pass').value,
        company_token: state.companyToken,
      },
    });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem('sb.token', data.token);
    await enterApp();
  } catch (err) {
    $('login-error').textContent = err.message;
    if (String(err.message).includes('公司')) {
      clearCompanyToken();
      showCompanyVerify('公司验证已过期，请重新验证');
    }
  } finally {
    button.disabled = false;
    button.textContent = '登 录';
  }
});

$('btn-logout').addEventListener('click', () => {
  api('/api/logout', { method: 'POST' }).catch(() => {});
  signOut(false);
});

function showRecovery(code, firstTime) {
  openModal(firstTime ? '请保存你的恢复码' : '新的恢复码', h('div', {},
    h('p', { class: 'hint' }, firstTime
      ? '以后忘记密码时，只能靠这个恢复码重置。请现在保存到安全的地方（U 盘、密码管理器或打印出来）。'
      : '旧恢复码已失效，请重新保存。'),
    h('div', { class: 'recovery-code' }, code),
    h('p', { class: 'hint' }, '这个码只显示这一次，关闭后无法再查看，只能重新生成。'),
  ), [
    h('button', {
      class: 'btn ghost',
      onclick: () => downloadText('ShareBox-恢复码.txt', recoveryFile(code)),
    }, '下载恢复文件'),
    h('button', {
      class: 'btn primary',
      onclick: async () => {
        closeModal();
        if (firstTime) await enterApp();
      },
    }, firstTime ? '我已保存，开始使用' : '我已保存'),
  ]);
}

$('btn-forgot').addEventListener('click', () => {
  const code = h('input', { placeholder: '例如：ABCD-EFGH-IJKL-MNOP-QRST' });
  const pass = h('input', { type: 'password', autocomplete: 'new-password' });
  const again = h('input', { type: 'password', autocomplete: 'new-password' });
  const error = h('div', { class: 'form-error' });
  openModal('用恢复文件重置密码', h('div', {},
    h('p', { class: 'hint' }, '打开你保存的 ShareBox-恢复码.txt，把里面的恢复码填到这里。'),
    h('label', { class: 'field' }, h('span', {}, '恢复码'), code),
    h('label', { class: 'field' }, h('span', {}, '新密码（至少 8 位）'), pass),
    h('label', { class: 'field' }, h('span', {}, '再次输入新密码'), again),
    error,
  ), [
    h('button', { class: 'btn ghost', onclick: closeModal }, '取消'),
    h('button', {
      class: 'btn primary',
      onclick: async () => {
        error.textContent = '';
        if (pass.value !== again.value) { error.textContent = '两次输入的新密码不一致'; return; }
        try {
          await api('/api/recover', { method: 'POST', json: { code: code.value.trim(), new_password: pass.value } });
          closeModal();
          toast('密码已重置，请用新密码登录', 'ok');
          showLogin('');
        } catch (err) { error.textContent = err.message; }
      },
    }, '重置密码'),
  ]);
});


/* ------------------------------------------------------------------ 导航 */
$('nav').addEventListener('click', (event) => {
  const button = event.target.closest('.nav-btn');
  if (button) setView(button.dataset.view);
});

function setView(view) {
  state.view = view;
  document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $('sidebar').hidden = view !== 'mine';
  $('mine-view').hidden = view !== 'mine';
  $('page-view').hidden = view === 'mine';
  if (view === 'mine') { renderFolders(); renderFiles(); }
  else if (view === 'peers') renderPeersPage();
  else if (view === 'devices') renderDevicesPage();
  else if (view === 'logs') renderLogsPage();
  else if (view === 'account') renderAccountPage();
}

/* ------------------------------------------------------------------ 我的共享 */
async function loadFolders() {
  const data = await api('/api/folders');
  state.folders = data.folders || [];
  if (!state.folders.length) {
    state.folderId = null;
    state.folder = null;
    state.files = [];
  } else if (!state.folders.some((f) => f.id === state.folderId)) {
    state.folderId = state.folders[0].id;
  }
  if (state.folderId) {
    state.shareRoot = true;
    state.path = '';
    state.files = [];
    state.dirs = [];
    renderCrumb();
    renderFiles();
    renderPermBadge();
  } else {
    $('table-wrap').hidden = true;
    $('btn-upload').disabled = true;
    $('btn-mkdir').disabled = true;
    $('btn-refresh').disabled = true;
    showEmpty('还没有共享文件夹', '先添加一个本地文件夹，才能上传或共享给同事。');
    $('empty').append(h('button', {
      class: 'btn primary', style: 'margin-top:14px',
      onclick: () => $('btn-new-folder').click(),
    }, '＋ 添加共享文件夹'));
  }
}

function renderFolders() {
  const list = $('folder-list');
  if (!state.folders.length) {
    list.replaceChildren(h('li', { class: 'hint', style: 'padding:8px 10px' }, '暂无共享'));
    return;
  }
  list.replaceChildren(...state.folders.map((folder) => h('li', {
    class: 'folder-item' + (folder.id === state.folderId ? ' active' : ''),
    title: folder.local_path || folder.name,
    onclick: () => selectFolder(folder.id),
  },
    h('span', { class: 'fi-icon' }, '📁'),
    h('span', { class: 'fi-name' }, folder.name),
  )));
}

async function selectFolder(id) {
  state.folderId = id;
  state.shareRoot = true;
  state.path = '';
  state.filter = '';
  $('filter').value = '';
  renderFolders();
  renderCrumb();
  renderFiles();
  renderPermBadge();
}

async function refreshManifest(showLoading) {
  if (!state.folderId) return;
  if (state.loading) return;
  state.loading = true;
  if (showLoading) showEmpty('正在读取…', '');
  try {
    const data = await api(`/api/folders/${state.folderId}/manifest`);
    state.folder = data.folder;
    state.files = data.files || [];
    state.dirs = data.dirs || [];
    renderCrumb();
    renderFiles();
    renderPermBadge();
  } catch (err) {
    showEmpty('读取失败', err.message);
  } finally {
    state.loading = false;
  }
}

function renderPermBadge() {
  const badge = $('perm-badge');
  const folder = state.folders.find((f) => f.id === state.folderId);
  const hasFolder = !!folder && !state.shareRoot;
  $('btn-upload').disabled = !hasFolder;
  $('btn-mkdir').disabled = !hasFolder;
  $('btn-refresh').disabled = !hasFolder;
  if (!folder) { badge.hidden = true; return; }
  badge.textContent = folder.name;
  badge.hidden = false;
  $('btn-perms').hidden = false;
}

function showEmpty(title, detail) {
  const box = $('empty');
  box.hidden = false;
  box.replaceChildren(h('span', { class: 'big' }, '🗂'), h('div', {}, title),
    detail ? h('div', { class: 'hint' }, detail) : '');
  $('table-wrap').hidden = true;
}

function hideEmpty() { $('empty').hidden = true; $('table-wrap').hidden = false; }

function renderCrumb() {
  const folder = state.folders.find((f) => f.id === state.folderId);
  const nodes = [];
  const backToRoot = () => {
    state.shareRoot = true;
    state.path = '';
    renderCrumb();
    renderFiles();
    renderPermBadge();
  };
  nodes.push(h('button', {
    class: 'crumb-item' + (state.shareRoot ? ' current' : ''),
    onclick: backToRoot,
  }, '我的共享'));

  if (!state.shareRoot && folder) {
    nodes.push(h('span', { class: 'sep' }, '/'));
    nodes.push(h('button', {
      class: 'crumb-item' + (state.path ? '' : ' current'),
      onclick: () => {
        state.path = '';
        renderCrumb();
        renderFiles();
      },
    }, folder.name));
    const parts = state.path ? state.path.split('/') : [];
    parts.forEach((part, index) => {
      const target = parts.slice(0, index + 1).join('/');
      nodes.push(h('span', { class: 'sep' }, '/'));
      nodes.push(h('button', {
        class: 'crumb-item' + (index === parts.length - 1 ? ' current' : ''),
        onclick: () => {
          state.path = target;
          renderCrumb();
          renderFiles();
        },
      }, part));
    });
  }
  $('crumb').replaceChildren(...nodes);
}

function currentEntries() {
  if (state.shareRoot) {
    const folder = state.folders.find((f) => f.id === state.folderId);
    return folder ? [{ type: 'dir', name: folder.name, count: '—', virtual: true }] : [];
  }
  const prefix = state.path ? state.path + '/' : '';
  const dirs = new Map();
  const files = [];
  for (const dir of state.dirs) {
    if (!dir.startsWith(prefix)) continue;
    const rest = dir.slice(prefix.length);
    if (!rest) continue;
    const slash = rest.indexOf('/');
    const name = slash >= 0 ? rest.slice(0, slash) : rest;
    dirs.set(name, (dirs.get(name) || 0) + 1);
  }
  for (const file of state.files) {
    if (!file.path.startsWith(prefix)) continue;
    const rest = file.path.slice(prefix.length);
    if (!rest) continue;
    const slash = rest.indexOf('/');
    if (slash >= 0) {
      const name = rest.slice(0, slash);
      dirs.set(name, (dirs.get(name) || 0) + 1);
    } else {
      files.push(Object.assign({}, file, { name: rest }));
    }
  }
  const keyword = state.filter.trim().toLowerCase();
  const match = (name) => !keyword || name.toLowerCase().includes(keyword);
  const dirRows = [...dirs.entries()].filter(([name]) => match(name))
    .map(([name, count]) => ({ type: 'dir', name, count }));
  const fileRows = files.filter((file) => match(file.name));
  dirRows.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'));
  fileRows.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'));
  return dirRows.concat(fileRows);
}

function renderFiles() {
  if (state.view !== 'mine') return;
  const entries = currentEntries();
  const body = $('file-body');
  if (!entries.length) {
    showEmpty(state.filter ? '没有匹配的文件' : '这个目录还是空的',
      state.filter ? '' : '把文件拖进来，或点右上角「上传」。');
    body.replaceChildren();
    return;
  }
  hideEmpty();
  body.replaceChildren(...entries.map((entry) => {
    if (entry.type === 'dir') {
      const openDir = () => {
        if (entry.virtual) {
          state.shareRoot = false;
          state.path = '';
          refreshManifest(true);
        } else {
          state.path = joinPath(state.path, entry.name);
          renderFiles();
          renderCrumb();
        }
      };
      return h('tr', {},
        h('td', {}, h('div', { class: 'name-cell' },
          h('span', { class: 'icon dir' }, '📁'),
          h('span', { class: 'name dirname', onclick: openDir }, entry.name))),
        h('td', { class: 'size' }, entry.count === '—' ? '—' : entry.count + ' 项'),
        h('td', { class: 'time' }, '—'),
        h('td', {}, h('div', { class: 'ops' },
          entry.virtual ? null : h('button', { onclick: () => removeDir(joinPath(state.path, entry.name)) }, '删除'))));
    }
    const icon = iconFor(entry.name);
    return h('tr', {},
      h('td', {}, h('div', { class: 'name-cell' },
        h('span', { class: 'icon ' + icon.cls }, icon.label),
        h('span', { class: 'name', onclick: () => preview(entry.path, entry.name) }, entry.name))),
      h('td', { class: 'size' }, fmtSize(entry.size)),
      h('td', { class: 'time' }, fmtTime(entry.mtime)),
      h('td', {}, h('div', { class: 'ops' },
        h('button', { onclick: () => preview(entry.path, entry.name) }, '预览'),
        h('button', { onclick: () => download(entry.path, entry.name) }, '下载'),
        h('button', { class: 'danger', onclick: () => removeFile(entry.path, entry.name) }, '删除'))));
  }));
}

function fileUrl(path) {
  return `/api/folders/${state.folderId}/file?path=${encodeURIComponent(path)}`;
}

async function download(path, name) {
  try {
    const res = await fetch(fileUrl(path), { headers: state.token ? { Authorization: 'Bearer ' + state.token } : {} });
    if (!res.ok) throw new Error('下载失败');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = h('a', { href: url, download: name || basename(path) });
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 20000);
  } catch (err) { toast(err.message, 'error'); }
}

async function preview(path, name) {
  const ext = extOf(name);
  const isImage = IMAGE_EXT.includes(ext);
  const isText = TEXT_EXT.includes(ext);
  if (!isImage && !isText) { toast('该类型不支持预览，请下载查看'); return; }
  openModal('预览 · ' + name, h('div', { class: 'preview-wrap' }, h('div', { class: 'hint' }, '正在加载…')), null, true);
  try {
    const res = await fetch(fileUrl(path), { headers: state.token ? { Authorization: 'Bearer ' + state.token } : {} });
    if (!res.ok) throw new Error('读取失败');
    if (isImage) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      $('modal-body').replaceChildren(h('div', { class: 'preview-wrap' },
        h('img', { src: url, alt: name, onload: () => URL.revokeObjectURL(url) })));
    } else {
      const text = await res.text();
      $('modal-body').replaceChildren(h('div', { class: 'preview-wrap' },
        h('pre', {}, text.length > 200000 ? text.slice(0, 200000) + '\n……' : text)));
    }
  } catch (err) {
    $('modal-body').replaceChildren(h('div', { class: 'hint' }, err.message));
  }
}

async function removeFile(path, name) {
  if (!await confirmDialog(`确定删除「${name}」吗？`, '对端如果也有这个文件，会同步删除。')) return;
  try {
    await api(fileUrl(path), { method: 'DELETE' });
    toast('已删除', 'ok');
    await refreshManifest();
  } catch (err) { toast(err.message, 'error'); }
}

async function removeDir(path) {
  if (!await confirmDialog(`确定删除空目录「${basename(path)}」吗？`)) return;
  try {
    await api(`/api/folders/${state.folderId}/dir?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
    toast('已删除', 'ok');
    if (state.path === path) state.path = '';
    await refreshManifest();
  } catch (err) { toast(err.message, 'error'); }
}

/* 上传 */
let uploadCount = 0;
function uploadFiles(fileList) {
  if (!state.folderId || state.shareRoot) {
    toast('请先进入一个共享文件夹，再上传文件', 'error');
    return;
  }
  const files = [...fileList];
  if (!files.length) return;
  const dir = state.path;
  uploadCount += files.length;
  toast(`开始上传 ${files.length} 个文件…`);
  (async () => {
    for (const file of files) {
      try {
        await uploadOne(file, dir);
      } catch (err) {
        toast(`「${file.name}」上传失败：${err.message}`, 'error');
      }
    }
    await refreshManifest();
  })();
}

function uploadOne(file, dir) {
  return new Promise((resolve, reject) => {
    const rel = joinPath(dir, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/folders/${state.folderId}/file?path=${encodeURIComponent(rel)}`);
    xhr.setRequestHeader('Authorization', 'Bearer ' + state.token);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else {
        let message = 'HTTP ' + xhr.status;
        try { message = JSON.parse(xhr.responseText).error || message; } catch (err) { /* ignore */ }
        reject(new Error(message));
      }
    };
    xhr.onerror = () => reject(new Error('网络中断'));
    xhr.send(file);
  });
}

$('btn-upload').addEventListener('click', () => $('file-input').click());

$('btn-mkdir').addEventListener('click', () => {
  if (!state.folderId || state.shareRoot) return;
  const input = h('input', { placeholder: '例如：2026 项目资料', maxlength: '80' });
  const error = h('div', { class: 'form-error' });
  openModal('新建文件夹', h('div', {},
    h('label', { class: 'field' }, h('span', {}, '文件夹名称'), input),
    h('p', { class: 'hint' }, '会创建在当前目录下。'),
    error,
  ), [
    h('button', { class: 'btn ghost', onclick: closeModal }, '取消'),
    h('button', {
      class: 'btn primary',
      onclick: async () => {
        const name = input.value.trim();
        if (!name) { error.textContent = '请输入名称'; return; }
        try {
          await api(`/api/folders/${state.folderId}/mkdir`, {
            method: 'POST', json: { path: joinPath(state.path, name) },
          });
          closeModal();
          toast('已创建', 'ok');
          await refreshManifest();
        } catch (err) { error.textContent = err.message; }
      },
    }, '创建'),
  ]);
});
$('file-input').addEventListener('change', () => {
  const files = [...$('file-input').files];
  $('file-input').value = '';
  uploadFiles(files);
});
$('btn-refresh').addEventListener('click', () => refreshManifest(true));
$('filter').addEventListener('input', (e) => { state.filter = e.target.value; renderFiles(); });

/* 拖拽 */
const dropZone = $('drop-zone');
let dragDepth = 0;
document.querySelector('.main').addEventListener('dragenter', (e) => {
  if (!e.dataTransfer || ![...(e.dataTransfer.types || [])].includes('Files')) return;
  e.preventDefault(); dragDepth++; dropZone.hidden = state.view !== 'mine';
});
document.querySelector('.main').addEventListener('dragover', (e) => {
  if (!e.dataTransfer || ![...(e.dataTransfer.types || [])].includes('Files')) return;
  e.preventDefault();
});
document.querySelector('.main').addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dropZone.hidden = true;
});
document.querySelector('.main').addEventListener('drop', (e) => {
  if (!e.dataTransfer) return;
  e.preventDefault(); dragDepth = 0; dropZone.hidden = true;
  if (state.view === 'mine') uploadFiles(e.dataTransfer.files);
});

/* 新建共享 */
$('btn-new-folder').addEventListener('click', () => {
  const name = h('input', { placeholder: '例如：项目资料' });
  const path = h('input', { placeholder: '例如：D:\\工作\\项目资料' });
  const pick = h('button', {
    class: 'btn ghost small', type: 'button',
    onclick: async () => {
      const chosen = await pickFolder();
      if (!chosen) return;
      path.value = chosen;
      if (!name.value.trim()) name.value = chosen.split(/[\\/]/).filter(Boolean).pop() || '';
    },
  }, '选择文件夹…');
  const error = h('div', { class: 'form-error' });
  openModal('共享一个本地文件夹', h('div', {},
    h('label', { class: 'field' }, h('span', {}, '显示名称'), name),
    h('label', { class: 'field' }, h('span', {}, '文件夹完整路径'),
      h('div', { class: 'path-row' }, path, pick)),
    h('p', { class: 'hint' }, '点「选择文件夹…」选一个本地文件夹；文件仍留在原位置，不会搬走。'),
    error,
  ), [
    h('button', { class: 'btn ghost', onclick: closeModal }, '取消'),
    h('button', {
      class: 'btn primary',
      onclick: async () => {
        error.textContent = '';
        if (!path.value.trim()) { error.textContent = '请填写文件夹路径'; return; }
        try {
          const data = await api('/api/folders', {
            method: 'POST',
            json: { name: name.value.trim(), local_path: path.value.trim() },
          });
          closeModal();
          await loadFolders();
          await selectFolder(data.folder.id);
          toast('已共享，去「设备」授权给同事', 'ok');
        } catch (err) { error.textContent = err.message; }
      },
    }, '开始共享'),
  ]);
});

/* 权限 */
$('btn-perms').addEventListener('click', async () => {
  if (!state.folderId) return;
  let peers = state.peers;
  if (!peers.length) { toast('还没有配对设备，请先到「设备」添加', 'error'); return; }
  let members = [];
  try { members = (await api(`/api/folders/${state.folderId}/perms`)).members || []; }
  catch (err) { toast(err.message, 'error'); return; }
  const levels = new Map(members.map((m) => [m.user_id, m.level]));
  const rows = peers.map((peer) => {
    const select = h('select', {},
      h('option', { value: 'none' }, '无权限'),
      h('option', { value: 'read' }, '只读'),
      h('option', { value: 'write' }, '完全控制'));
    select.value = levels.get(peer.user_id) || 'none';
    select.addEventListener('change', async () => {
      try {
        await api(`/api/folders/${state.folderId}/perms`, {
          method: 'PUT', json: { user_id: peer.user_id, level: select.value },
        });
        toast('权限已保存', 'ok');
      } catch (err) { toast(err.message, 'error'); }
    });
    return h('tr', {},
      h('td', {}, peer.name, h('span', { class: 'tag' }, peer.address + ':' + peer.port)),
      h('td', { style: 'width:200px' }, select));
  });
  openModal('谁能访问「' + (state.folder?.name || '') + '」', h('div', {},
    h('p', { class: 'hint' }, '只读：对方只能下载；完全控制：对方可以上传、修改和删除。'),
    h('table', { class: 'grid-table' },
      h('thead', {}, h('tr', {}, h('th', {}, '设备'), h('th', {}, '权限'))),
      h('tbody', {}, ...rows)),
    h('p', { class: 'hint' }, '删除整个共享请在左侧文件夹上右键。'),
  ), [h('button', { class: 'btn primary', onclick: closeModal }, '完成')], true);
});

/* 右键删除共享 */
$('folder-list').addEventListener('contextmenu', async (event) => {
  event.preventDefault();
  const item = event.target.closest('.folder-item');
  if (!item) return;
  const index = [...$('folder-list').children].indexOf(item);
  const folder = state.folders[index];
  if (!folder) return;
  if (!await confirmDialog(`不再共享「${folder.name}」？`, '只移除共享关系，不会删除你电脑上的文件。')) return;
  try {
    await api(`/api/folders/${folder.id}`, { method: 'DELETE' });
    toast('已移除共享', 'ok');
    state.folderId = null;
    await loadFolders();
  } catch (err) { toast(err.message, 'error'); }
});

/* ------------------------------------------------------------------ 同事共享 */
async function loadPeers() { state.peers = (await api('/api/peers')).peers || []; }
async function loadMirrors() { state.mirrors = (await api('/api/mirrors')).mirrors || []; }

async function renderPeersPage() {
  const page = $('page-view');
  page.replaceChildren(h('div', { class: 'page-head' },
    h('div', {}, h('h2', {}, '同事共享'), h('p', { class: 'hint' }, '选择对方共享给你的文件夹，同步到本机。')),
    h('button', { class: 'btn ghost small', onclick: async () => { await Promise.all([loadPeers(), loadMirrors()]); renderPeersPage(); } }, '刷新')));

  if (!state.peers.length) {
    page.append(h('div', { class: 'empty' }, h('span', { class: 'big' }, '🔗'),
      h('div', {}, '还没有配对的同事'),
      h('div', { class: 'hint' }, '到「设备」页添加对方，配对后这里会显示对方的共享。')));
    return;
  }

  page.append(h('div', { class: 'card-list' }, ...state.peers.map((peer) => h('div', { class: 'card' },
    h('div', { class: 'card-head' },
      h('strong', {}, peer.name),
      h('span', { class: 'tag' }, peer.address + ':' + peer.port),
      h('span', { class: 'dot ' + (peer.online ? 'on' : '') }, peer.online ? '在线' : '离线')),
    h('div', { class: 'card-actions' },
      h('button', { class: 'btn primary small', onclick: () => openPeerFolders(peer) }, '查看对方的共享')),
  ))));

  const sections = h('div', { class: 'section' },
    h('h3', {}, '正在同步'),
    state.mirrors.length
      ? h('table', { class: 'grid-table' },
        h('thead', {}, h('tr', {},
          h('th', {}, '同事'), h('th', {}, '对方的文件夹'), h('th', {}, '保存到本机'),
          h('th', {}, '状态'), h('th', {}, ''))),
        h('tbody', {}, ...state.mirrors.map((mirror) => h('tr', {},
          h('td', {}, mirror.peer_name),
          h('td', {}, mirror.remote_name),
          h('td', { class: 'hint' }, mirror.local_path),
          h('td', {},
            mirror.status && mirror.status.error
              ? h('span', { class: 'tag off' }, mirror.status.error)
              : h('span', { class: 'tag ok' }, mirror.status && mirror.status.last
                ? '已同步 ' + fmtIso(mirror.status.last) : '等待同步')),
          h('td', { style: 'text-align:right;white-space:nowrap' },
            h('button', {
              class: 'btn ghost small',
              onclick: async () => {
                try {
                  await api(`/api/mirrors/${mirror.id}/toggle`, { method: 'POST', json: { enabled: !mirror.enabled } });
                  await loadMirrors(); renderPeersPage();
                } catch (err) { toast(err.message, 'error'); }
              },
            }, mirror.enabled ? '暂停' : '继续'),
            ' ',
            h('button', {
              class: 'btn ghost small',
              onclick: async () => {
                if (!await confirmDialog('停止同步这个文件夹？', '本机已下载的文件不会被删除。')) return;
                try {
                  await api(`/api/mirrors/${mirror.id}`, { method: 'DELETE' });
                  await loadMirrors(); renderPeersPage();
                } catch (err) { toast(err.message, 'error'); }
              },
            }, '移除'))),
        )))
      : h('p', { class: 'hint' }, '还没有同步任何文件夹。点击上方同事的「查看对方的共享」开始。'),
  );
  page.append(sections);
}

async function openPeerFolders(peer) {
  let data;
  try { data = await api(`/api/remote/${peer.id}/folders`); }
  catch (err) { toast(err.message, 'error'); return; }
  const folders = data.folders || [];
  if (!folders.length) {
    openModal(peer.name + ' 的共享', h('div', {},
      h('p', { class: 'hint' }, '对方还没有共享任何文件夹给你，或者还没有给你授权。')));
    return;
  }
  openModal(peer.name + ' 的共享', h('div', {},
    h('p', { class: 'hint' }, '选择要同步到本机的文件夹；只读的文件夹不会被上传修改。'),
    h('table', { class: 'grid-table' },
      h('thead', {}, h('tr', {}, h('th', {}, '文件夹'), h('th', {}, '你的权限'), h('th', {}, ''))),
      h('tbody', {}, ...folders.map((folder) => h('tr', {},
        h('td', {}, folder.name),
        h('td', {}, folder.level === 'write'
          ? h('span', { class: 'tag ok' }, '完全控制')
          : h('span', { class: 'tag' }, '只读')),
        h('td', { style: 'text-align:right' },
          h('button', { class: 'btn primary small', onclick: () => openAddMirror(peer, folder) }, '同步到本地')),
      )))),
  ), [h('button', { class: 'btn ghost', onclick: closeModal }, '关闭')], true);
}

function openAddMirror(peer, folder) {
  const home = (state.account && state.account.home) || 'C:\\Users\\你';
  const defaultPath = `${home}\\ShareBox\\${folder.name}`;
  const input = h('input', { value: defaultPath });
  const pick = h('button', {
    class: 'btn ghost small', type: 'button',
    onclick: async () => {
      const chosen = await pickFolder();
      if (chosen) input.value = chosen;
    },
  }, '选择文件夹…');
  const error = h('div', { class: 'form-error' });
  openModal('同步「' + folder.name + '」', h('div', {},
    h('p', { class: 'hint' }, `来自 ${peer.name}。文件会保存到这个本地目录，并保持同步。`),
    h('label', { class: 'field' }, h('span', {}, '保存到本机目录'),
      h('div', { class: 'path-row' }, input, pick)),
    error,
  ), [
    h('button', { class: 'btn ghost', onclick: closeModal }, '取消'),
    h('button', {
      class: 'btn primary',
      onclick: async () => {
        error.textContent = '';
        try {
          await api('/api/mirrors', {
            method: 'POST',
            json: { peer_id: peer.id, remote_folder_id: folder.id, local_path: input.value.trim() },
          });
          closeModal();
          await loadMirrors();
          toast('已开始同步', 'ok');
          renderPeersPage();
        } catch (err) { error.textContent = err.message; }
      },
    }, '开始同步'),
  ]);
}

/* ------------------------------------------------------------------ 设备 */
async function renderDevicesPage() {
  await loadPeers();
  const page = $('page-view');
  page.replaceChildren(h('div', { class: 'page-head' },
    h('div', {}, h('h2', {}, '设备'), h('p', { class: 'hint' }, '两台电脑各自生成/输入配对码，配对后即可互相共享。')),
    h('div', {},
      h('button', { class: 'btn ghost small', onclick: showPairingCode }, '本机配对码'),
      ' ',
      h('button', { class: 'btn primary small', onclick: showAddDevice }, '添加设备'))));

  if (state.account && state.account.company && state.account.company.company_id) {
    page.append(h('p', { class: 'hint' },
      h('span', {}, '公司编号：'),
      h('span', { class: 'mono' }, state.account.company.company_id),
      h('span', {}, '（同事「加入公司」时填这个）')));
  }

  if (!state.peers.length) {
    page.append(h('div', { class: 'empty' }, h('span', { class: 'big' }, '💻'),
      h('div', {}, '还没有配对设备'),
      h('div', { class: 'hint' }, '让同事打开「本机配对码」，你把地址和码填进来；双方都装 Tailscale 时，地址填 100.x.x.x。')));
    return;
  }

  page.append(h('table', { class: 'grid-table' },
    h('thead', {}, h('tr', {},
      h('th', {}, '设备'), h('th', {}, '地址'), h('th', {}, '状态'), h('th', {}, '最后在线'), h('th', {}, ''))),
    h('tbody', {}, ...state.peers.map((peer) => h('tr', {},
      h('td', {}, h('strong', {}, peer.name)),
      h('td', { class: 'hint' }, peer.address + ':' + peer.port),
      h('td', {}, peer.online ? h('span', { class: 'tag ok' }, '在线') : h('span', { class: 'tag' }, '离线')),
      h('td', { class: 'hint' }, peer.last_seen ? fmtIso(peer.last_seen) : '—'),
      h('td', { style: 'text-align:right' },
        h('button', {
          class: 'btn ghost small',
          onclick: async () => {
            if (!await confirmDialog(`解除与「${peer.name}」的配对？`, '会同时移除与对方相关的同步项。')) return;
            try {
              await api(`/api/peers/${peer.id}`, { method: 'DELETE' });
              await Promise.all([loadPeers(), loadMirrors()]);
              renderDevicesPage();
            } catch (err) { toast(err.message, 'error'); }
          },
        }, '解除配对'))),
    ))));
}

async function showPairingCode() {
  let data;
  try { data = await api('/api/device/code', { method: 'POST' }); }
  catch (err) { toast(err.message, 'error'); return; }
  const addresses = (data.addresses || []).map((ip) => `${ip}:${data.port}`).join('  或  ');
  openModal('本机配对码', h('div', {},
    h('p', { class: 'hint' }, '把这个码和地址告诉对方，让对方在「添加设备」里输入。15 分钟内有效，用过即失效。'),
    h('div', { class: 'recovery-code' }, data.code),
    h('p', { class: 'hint' }, '本机地址：' + (addresses || `127.0.0.1:${data.port}`)),
    h('p', { class: 'hint' }, '异地使用时，请填 Tailscale 给出的 100.x.x.x 地址，端口 ' + data.port + '。'),
  ), [h('button', { class: 'btn primary', onclick: closeModal }, '知道了')]);
}

function showAddDevice() {
  const address = h('input', { placeholder: '例如：100.101.102.103' });
  const port = h('input', { value: '8787' });
  const code = h('input', { placeholder: '对方电脑上显示的配对码' });
  const error = h('div', { class: 'form-error' });
  openModal('添加设备', h('div', {},
    h('label', { class: 'field' }, h('span', {}, '对方地址'), address),
    h('label', { class: 'field' }, h('span', {}, '端口'), port),
    h('label', { class: 'field' }, h('span', {}, '配对码'), code),
    h('p', { class: 'hint' }, '在对方电脑上点「本机配对码」，把显示的码填到这里。'),
    error,
  ), [
    h('button', { class: 'btn ghost', onclick: closeModal }, '取消'),
    h('button', {
      class: 'btn primary',
      onclick: async () => {
        error.textContent = '';
        try {
          await api('/api/device/pair', {
            method: 'POST',
            json: { address: address.value.trim(), port: Number(port.value) || 8787, code: code.value.trim() },
          });
          closeModal();
          await loadPeers();
          toast('配对成功', 'ok');
          renderDevicesPage();
        } catch (err) { error.textContent = err.message; }
      },
    }, '配对'),
  ]);
}

/* ------------------------------------------------------------------ 日志 */
const ACTION_LABEL = {
  'file.upload': '上传/修改', 'file.delete': '删除文件',
  'file.local': '本机修改', 'file.local.delete': '本机删除',
  'dir.create': '新建目录', 'dir.delete': '删除目录',
  'folder.create': '添加共享', 'folder.delete': '移除共享',
  'peer.add': '添加设备', 'peer.delete': '解除设备',
  'account.password': '修改密码', 'account.recover': '恢复密码',
  'account.recovery': '重置恢复码', 'mirror.add': '添加同步',
  'node.create': '创建节点',
};

async function renderLogsPage() {
  const page = $('page-view');
  page.replaceChildren(h('div', { class: 'page-head' },
    h('div', {}, h('h2', {}, '修改日志'), h('p', { class: 'hint' }, '记录谁在什么时候改了哪个文件。')),
    h('button', { class: 'btn ghost small', onclick: renderLogsPage }, '刷新')));
  let data;
  try { data = await api('/api/logs?limit=300'); }
  catch (err) { page.append(h('p', { class: 'hint' }, err.message)); return; }
  const items = data.items || [];
  if (!items.length) { page.append(h('p', { class: 'hint' }, '还没有记录。')); return; }
  page.append(h('table', { class: 'grid-table' },
    h('thead', {}, h('tr', {},
      h('th', {}, '时间'), h('th', {}, '操作者'), h('th', {}, '动作'), h('th', {}, '内容'))),
    h('tbody', {}, ...items.map((item) => h('tr', {},
      h('td', { class: 'hint' }, fmtIso(item.ts)),
      h('td', {}, item.display_name || item.username || '本机'),
      h('td', {}, ACTION_LABEL[item.action] || item.action),
      h('td', { class: 'hint' }, item.detail),
    )))));
}

/* ------------------------------------------------------------------ 账号 */
async function renderAccountPage() {
  state.account = await api('/api/account');
  const page = $('page-view');
  const device = h('input', { value: state.account.device_name });
  page.replaceChildren(
    h('div', { class: 'page-head' }, h('div', {}, h('h2', {}, '账号'), h('p', { class: 'hint' }, '本机账号、恢复码与设备名称。'))),
    h('div', { class: 'section' },
      h('table', { class: 'kv-table' },
        h('tr', {}, h('td', {}, '公司'), h('td', {}, (state.account.company && state.account.company.name) || '—')),
        h('tr', {}, h('td', {}, '公司编号'), h('td', { class: 'mono' }, (state.account.company && state.account.company.company_id) || '—')),
        h('tr', {}, h('td', {}, '用户名'), h('td', {}, state.account.username)),
        h('tr', {}, h('td', {}, '设备名称'), h('td', {}, state.account.device_name)),
        h('tr', {}, h('td', {}, '节点标识'), h('td', { class: 'mono' }, state.account.node_id)),
        h('tr', {}, h('td', {}, '创建时间'), h('td', {}, fmtIso(state.account.created_at))),
      )),
    h('div', { class: 'section' },
      h('h3', {}, '修改设备名称'),
      h('div', { class: 'inline-form' }, device,
        h('button', {
          class: 'btn primary small',
          onclick: async () => {
            try {
              await api('/api/account/device', { method: 'POST', json: { device_name: device.value.trim() } });
              $('who').textContent = device.value.trim();
              toast('已保存', 'ok');
            } catch (err) { toast(err.message, 'error'); }
          },
        }, '保存'))),
    h('div', { class: 'section' },
      h('h3', {}, '修改密码'),
      h('p', { class: 'hint' }, '修改后当前登录会保持，其他已登录窗口会失效。'),
      h('button', { class: 'btn ghost small', onclick: showChangePassword }, '修改密码')),
    h('div', { class: 'section' },
      h('h3', {}, '恢复码'),
      h('p', { class: 'hint' }, '忘记密码时用恢复码重置。重新生成会让旧恢复码立即失效。'),
      h('button', { class: 'btn ghost small', onclick: showRegenerateRecovery }, '重新生成恢复码')),
  );
}

function showChangePassword() {
  const oldPass = h('input', { type: 'password', autocomplete: 'current-password' });
  const newPass = h('input', { type: 'password', autocomplete: 'new-password' });
  const again = h('input', { type: 'password', autocomplete: 'new-password' });
  const error = h('div', { class: 'form-error' });
  openModal('修改密码', h('div', {},
    h('label', { class: 'field' }, h('span', {}, '原密码'), oldPass),
    h('label', { class: 'field' }, h('span', {}, '新密码'), newPass),
    h('label', { class: 'field' }, h('span', {}, '再次输入新密码'), again),
    error,
  ), [
    h('button', { class: 'btn ghost', onclick: closeModal }, '取消'),
    h('button', {
      class: 'btn primary',
      onclick: async () => {
        error.textContent = '';
        if (newPass.value !== again.value) { error.textContent = '两次输入的新密码不一致'; return; }
        try {
          const data = await api('/api/account/password', {
            method: 'POST', json: { old_password: oldPass.value, new_password: newPass.value },
          });
          state.token = data.token;
          localStorage.setItem('sb.token', data.token);
          closeModal();
          toast('密码已修改', 'ok');
        } catch (err) { error.textContent = err.message; }
      },
    }, '保存'),
  ]);
}

async function showRegenerateRecovery() {
  if (!await confirmDialog('重新生成恢复码？', '旧恢复码会立即失效，请确保保存新的。')) return;
  try {
    const data = await api('/api/account/recovery');
    state.account = await api('/api/account');
    showRecovery(data.code, false);
  } catch (err) { toast(err.message, 'error'); }
}

/* ------------------------------------------------------------------ 启动 */
async function boot() {
  let status;
  try {
    const headers = state.companyToken ? { 'X-Company-Token': state.companyToken } : {};
    status = await fetch('/api/company/status', { headers }).then((r) => r.json());
  } catch (err) {
    showLogin('无法连接本机服务');
    return;
  }
  state.companyName = status.name || '';
  if (!status.exists) { showCompanyCreate(); return; }
  if (!status.verified) {
    clearCompanyToken();
    showCompanyVerify('');
    return;
  }
  if (!state.token) { showLogin(''); return; }
  try {
    const me = await api('/api/me');
    state.user = me.user;
    await enterApp();
  } catch (err) {
    if ($('login-view').hidden) showLogin(err.message || '');
  }
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.token && state.view === 'peers') renderPeersPage();
});

boot();
