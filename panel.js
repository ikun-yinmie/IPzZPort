const defaultTheme = 'purple';
const themeKey = 'ipzzport.theme';
const themes = new Map([
  ['light', { color: '#f4f6fb', scheme: 'light' }],
  ['dark', { color: '#080d18', scheme: 'dark' }],
  ['purple', { color: '#100c1c', scheme: 'dark' }],
]);

function applyTheme(requested) {
  const theme = themes.has(requested) ? requested : defaultTheme;
  const { color, scheme } = themes.get(theme);
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]').content = color;
  document.querySelector('meta[name="color-scheme"]').content = scheme;
  return theme;
}

// Restore the theme in the head before the stylesheet is applied.
let savedTheme;
try {
  savedTheme = localStorage.getItem(themeKey);
} catch {
  // Storage may be disabled; the current page can still switch themes.
}
applyTheme(savedTheme);

const select = (id) => document.getElementById(id);
let editor;
let form;
let confirm;
let editing = null;
let deleting = null;
let busy = false;

function text(id, value) {
  select(id).textContent = value;
}

function showMessage(id, message, isError = false) {
  const element = select(id);
  element.textContent = message;
  element.hidden = !message;
  element.classList.toggle('error', isError);
}

function addText(parent, tag, value, className) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = value;
  parent.append(element);
  return element;
}

function openEditor(rule = {}, id = null) {
  if (busy) return;
  editing = id;
  form.elements.name.value = rule.name ?? '';
  form.elements.listen.value = rule.listen ?? '';
  form.elements.target.value = rule.target ?? '';
  form.elements.enabled.checked = rule.enabled ?? true;
  text('editor-title', id ? '编辑规则' : '新增规则');
  showMessage('form-error', '');
  editor.showModal();
}

function actionButton(parent, label, handler) {
  const button = addText(parent, 'button', label);
  button.type = 'button';
  button.addEventListener('click', handler);
}

function render(rules) {
  const rows = select('rules');
  rows.replaceChildren();
  text('total', rules.length);
  text('running', rules.filter((rule) => rule.running).length);
  text('connections', rules.reduce((total, rule) => total + rule.connections, 0));
  select('empty-view').hidden = rules.length > 0;
  text('empty', '还没有转发规则。可以新增规则，或填入下方的 OneNav 示例。');
  select('rules-view').hidden = rules.length === 0;

  for (const rule of rules) {
    const row = rows.insertRow();
    row.insertCell().textContent = rule.name;
    addText(row.insertCell(), 'code', rule.listen);
    addText(row.insertCell(), 'code', rule.target);
    const status = row.insertCell();
    if (rule.running) {
      addText(status, 'span', '监听中', 'badge on');
      addText(status, 'span', `${rule.connections} 个连接`, 'detail');
    } else if (rule.error) {
      addText(status, 'span', '启动失败', 'badge fail');
      addText(status, 'span', rule.error, 'detail');
    } else {
      addText(status, 'span', '已停止', 'badge');
    }
    const actions = addText(row.insertCell(), 'div', '', 'actions');
    actionButton(actions, rule.running ? '停止' : '启动', () => {
      change({ action: rule.running ? 'stop' : 'start', id: rule.id }, rule.running ? '规则已停止' : '规则已启动');
    });
    actionButton(actions, '编辑', () => openEditor(rule, rule.id));
    actionButton(actions, '删除', () => {
      if (busy) return;
      deleting = rule.id;
      text('delete-info', `删除「${rule.name}」将停止该规则的转发。`);
      confirm.showModal();
    });
  }
}

async function request(data) {
  const options = data ? {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  } : {};
  const response = await fetch('/api/rules', options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作失败');
  return result.rules;
}

async function change(data, success = '', dialog = null) {
  if (busy) return;
  busy = true;
  const buttons = [...document.querySelectorAll('button')];
  for (const button of buttons) button.disabled = true;
  showMessage('notice', '');
  showMessage('form-error', '');
  try {
    render(await request(data));
    dialog?.close();
    showMessage('notice', success);
  } catch (error) {
    if (dialog === confirm) dialog.close();
    if (!data && !select('empty-view').hidden) text('empty', '规则未加载。');
    showMessage(dialog === editor && editor.open ? 'form-error' : 'notice', error.message, true);
  } finally {
    busy = false;
    for (const button of buttons) button.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const themePicker = select('panel-theme');
  themePicker.value = document.documentElement.dataset.theme;
  themePicker.addEventListener('change', () => {
    const theme = applyTheme(themePicker.value);
    themePicker.value = theme;
    try {
      localStorage.setItem(themeKey, theme);
    } catch {
      // Keep the selected theme for this page even if it cannot be saved.
    }
  });

  editor = select('editor');
  form = select('rule-form');
  confirm = select('confirm');
  select('new-rule').addEventListener('click', () => openEditor());
  select('example').addEventListener('click', () => openEditor({
    name: 'OneNav', listen: '[::]:9632', target: '127.0.0.1:9632',
  }));
  select('cancel').addEventListener('click', () => editor.close());
  select('keep').addEventListener('click', () => confirm.close());
  select('refresh').addEventListener('click', () => change(null, '状态已刷新'));
  for (const dialog of [editor, confirm]) {
    dialog.addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
  }
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const rule = {
      name: form.elements.name.value,
      listen: form.elements.listen.value,
      target: form.elements.target.value,
      enabled: form.elements.enabled.checked,
    };
    change({ action: editing ? 'update' : 'create', id: editing, rule }, '规则已保存', editor);
  });
  select('delete-form').addEventListener('submit', (event) => {
    event.preventDefault();
    change({ action: 'delete', id: deleting }, '规则已删除', confirm);
  });
  change(null);
}, { once: true });
