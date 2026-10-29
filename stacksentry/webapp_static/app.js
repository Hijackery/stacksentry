(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const main = $("main");
  const view = $("view");
  const terminalView = $("terminal-view");
  const sub = $("sub");
  const toast = $("toast");
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = hash.get("tgWebAppData") || (tg && tg.initData) || "";
  const BASE_TABS = [
    ["overview", "Обзор"], ["services", "Службы"], ["panel", "Панель"],
    ["traffic", "Трафик"], ["subs", "Подписки"], ["history", "История"],
    ["help", "Помощь"],
  ];

  const requestedTab = hash.get("tab") || "overview";
  const initialTab = BASE_TABS.some(([key]) => key === requestedTab)
    ? requestedTab
    : "overview";
  const state = {
    tabs: BASE_TABS.slice(),
    active: initialTab,
    snapshot: null,
    version: "",
    update: null,
    updateAt: 0,
    updateLoading: false,
    metaLoaded: false,
    terminalOrigin: "",
    authPending: false,
    authRetry: 0,
    loading: false,
    historyHours: 24,
    historyToken: 0,
    subsQuery: "",
    subsSort: "usage",
    timer: null,
  };

  function syncTabHash() {
    const suffix = state.active !== "overview"
      ? `#tab=${encodeURIComponent(state.active)}`
      : "";
    if (location.hash !== suffix) {
      history.replaceState(null, "", location.pathname + location.search + suffix);
    }
  }

  const ICONS = {
    ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="m9 12 2 2 4-4"></path></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2.5 20.5h19L12 3Z"></path><path d="M12 9v5"></path><path d="M12 17.5h.01"></path></svg>',
    bad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="m9 9 6 6M15 9l-6 6"></path></svg>',
    inbox: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v14H4z"></path><path d="M4 14h4l2 3h4l2-3h4"></path></svg>',
  };

  const UNAVAILABLE = {
    metrics: "метрики", services: "службы", panel: "панель", subs: "подписки",
  };

  function applyTheme() {
    const scheme = hash.get("tgWebAppColorScheme") || (tg && tg.colorScheme);
    if (scheme === "light" || scheme === "dark") {
      document.documentElement.dataset.theme = scheme;
    }
    let theme = tg && tg.themeParams ? tg.themeParams : null;
    const raw = hash.get("tgWebAppThemeParams");
    if (raw) {
      try { theme = JSON.parse(decodeURIComponent(raw)); } catch (_) { /* тема не критична */ }
    }
    if (!theme) return;
    const map = {
      bg_color: "--tg-theme-bg-color",
      text_color: "--tg-theme-text-color",
      hint_color: "--tg-theme-hint-color",
      button_color: "--tg-theme-button-color",
      button_text_color: "--tg-theme-button-text-color",
      secondary_bg_color: "--tg-theme-secondary-bg-color",
      link_color: "--tg-theme-link-color",
    };
    for (const [key, variable] of Object.entries(map)) {
      if (theme[key]) document.documentElement.style.setProperty(variable, theme[key]);
    }
  }

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
  const clamp = (value, low = 0, high = 100) => {
    const number = Number(value);
    return Number.isFinite(number) ? Math.min(high, Math.max(low, number)) : low;
  };
  const tone = (percent) => percent >= 90 ? "bad" : percent >= 75 ? "warn" : "ok";
  const fmtPercent = (value) => `${Math.round(clamp(value))}%`;
  const fmtNumber = (value) => {
    const number = Number(value || 0);
    return Number.isFinite(number) ? number.toLocaleString("ru-RU") : "0";
  };
  const fmtBytes = (value) => {
    let n = Number(value || 0);
    if (!Number.isFinite(n) || n < 0) n = 0;
    let unit = 0;
    const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit += 1; }
    return `${n.toFixed(n >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
  };
  const ago = (seconds) => {
    const s = Math.max(0, Math.round(Number(seconds) || 0));
    if (s < 5) return "только что";
    if (s < 60) return `${s} с назад`;
    const minutes = Math.round(s / 60);
    if (minutes < 60) return `${minutes} мин назад`;
    return `${Math.round(minutes / 60)} ч назад`;
  };
  const formatDate = (value) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit" });
  };
  const displayName = (item) => item.email || item.sub_id || "—";

  let toastTimer = null;
  function showToast(text) {
    toast.textContent = text;
    toast.classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 2200);
  }

  function setStatus(toneName, text, extra = "") {
    sub.innerHTML = `<span class="status-dot ${toneName}"></span><span>${esc(text)}</span>${
      extra ? `<span class="status-sep">·</span><span>${esc(extra)}</span>` : ""
    }`;
  }

  function health() {
    const s = state.snapshot;
    if (!s) {
      return { tone: "warn", title: "Нет данных", verdict: "нет данных", text: "Ожидание первого среза." };
    }
    const down = (s.services || []).filter((item) => !item.active);
    if (down.length) {
      const names = down.map((item) => item.name).join(", ");
      return { tone: "bad", title: "Есть проблема", verdict: `не работают: ${names}`, text: `Службы не работают: ${names}.` };
    }
    if (s.warnings && s.warnings.length) {
      return {
        tone: "warn", title: "Требует внимания", verdict: `замечаний: ${s.warnings.length}`,
        text: `Мониторинг нашёл замечаний: ${s.warnings.length}.`,
      };
    }
    if (s.unavailable && s.unavailable.length) {
      const names = s.unavailable.map((key) => UNAVAILABLE[key] || key).join(", ");
      return { tone: "warn", title: "Требует внимания", verdict: `нет данных: ${names}`, text: `Не удалось опросить: ${names}.` };
    }
    return { tone: "ok", title: "Всё в норме", verdict: "всё в норме", text: "Службы работают, метрики в пределах нормы." };
  }

  function updateHeader() {
    const verdict = health();
    const snapshot = state.snapshot;
    setStatus(verdict.tone, verdict.verdict, snapshot ? ago(snapshot.age) : "");
  }

  function renderHero(toneName, title, text, compact = false) {
    return `<section class="hero ${toneName}${compact ? " compact" : ""}">
      <div class="hero-icon">${ICONS[toneName] || ICONS.warn}</div>
      <div><h1>${esc(title)}</h1><p>${esc(text)}</p></div>
    </section>`;
  }

  function emptyState(title, text, action = "") {
    return `<div class="empty-state">
      <div class="empty-icon">${ICONS.inbox}</div>
      <h2>${esc(title)}</h2>
      <p>${esc(text)}</p>${action}
    </div>`;
  }

  function renderError(message) {
    view.hidden = false;
    terminalView.hidden = true;
    view.removeAttribute("aria-labelledby");
    setStatus("bad", "ошибка связи");
    view.innerHTML = emptyState(
      "Не удалось загрузить данные",
      message || "Проверьте соединение и повторите.",
      '<button class="primary" type="button" data-retry>Повторить</button>',
    );
  }

  function renderMetric(label, value, percent, note) {
    const width = clamp(percent);
    return `<div class="metric-card">
      <div class="metric-head"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>
      <div class="meter"><i class="${tone(width)}" style="width:${width}%"></i></div>
      <p>${esc(note)}</p>
    </div>`;
  }

  function renderQuick(label, value) {
    return `<div class="quick"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
  }

  function renderNotices(s) {
    const warnings = (s.warnings || []).filter(Boolean);
    if (warnings.length) {
      const shown = warnings.slice(0, 6);
      const more = warnings.length - shown.length;
      return `<div class="notice warn">
        <span class="notice-icon">${ICONS.warn}</span>
        <div><strong>Требует внимания</strong>
          <ul>${shown.map((warning) => `<li>${esc(warning)}</li>`).join("")}</ul>
          ${more > 0 ? `<div class="hint">и ещё ${more}</div>` : ""}
        </div>
      </div>`;
    }
    if (s.unavailable && s.unavailable.length) {
      const names = s.unavailable.map((key) => UNAVAILABLE[key] || key).join(", ");
      return `<div class="notice bad">
        <span class="notice-icon">${ICONS.bad}</span>
        <div><strong>Нет данных</strong>Не удалось опросить: ${esc(names)}.</div>
      </div>`;
    }
    return "";
  }

  function serviceRow(item) {
    const details = [];
    if (item.sub_state) details.push(item.sub_state);
    if (item.since) details.push(`с ${item.since}`);
    if (Number(item.restarts) > 0) details.push(`рестартов: ${item.restarts}`);
    if (Number(item.memory_bytes) > 0) details.push(fmtBytes(item.memory_bytes));
    return `<div class="list-row">
      <span class="dot ${item.active ? "ok" : "bad"}"></span>
      <div class="row-main">
        <span class="row-title">${esc(item.name)}</span>
        <span class="row-sub">${esc(details.join(" · ") || "—")}</span>
      </div>
      <span class="chip ${item.active ? "ok" : "bad"}">${esc(item.active_state || item.sub_state || "—")}</span>
    </div>`;
  }

  function serviceRows(limit) {
    const services = state.snapshot.services || [];
    return services.slice(0, limit).map(serviceRow).join("");
  }

  function panelPreview() {
    const panel = state.snapshot.panel;
    if (panel.disabled) {
      return `<section class="card">
        <div class="card-head"><h2>Панель</h2><span class="chip">monitor-only</span></div>
        <p class="hint">API панели не настроен — StackSentry наблюдает сервер и службы.</p>
      </section>`;
    }
    const panelTone = !panel.reachable ? "bad" : panel.api_authorized ? "ok" : "warn";
    const label = !panel.reachable ? "недоступна" : panel.api_authorized ? "отвечает" : "без API";
    return `<section class="card">
      <div class="card-head"><h2>Панель</h2><span class="chip ${panelTone}">${label}</span></div>
      <div class="kv">
        <div class="kv-row"><span>Версия</span><strong>${esc(panel.panel_version || "—")}</strong></div>
        <div class="kv-row"><span>Задержка</span><strong>${panel.latency_ms != null ? `${Math.round(panel.latency_ms)} мс` : "—"}</strong></div>
        <div class="kv-row"><span>xray</span><strong>${esc(panel.xray_version || "—")} · ${esc(panel.xray_state || "—")}</strong></div>
      </div>
    </section>`;
  }

  function renderOverview() {
    const s = state.snapshot;
    const verdict = health();
    const system = s.system;
    const services = s.services || [];
    const activeServices = services.filter((item) => item.active).length;
    const updateLine = `Обновлено ${ago(s.age)} · ${activeServices}/${services.length} служб · ${(s.inbounds || []).length} inbound`;
    const heroText = verdict.tone === "ok" ? updateLine : `${verdict.text} ${updateLine}`;
    const metrics = system ? [
      renderMetric("CPU", fmtPercent(system.cpu_percent), system.cpu_percent, `load avg ${Number(system.load1 || 0).toFixed(2)}`),
      renderMetric("RAM", fmtPercent(system.mem_percent), system.mem_percent, `${fmtBytes(system.mem_total_bytes * system.mem_percent / 100)} из ${fmtBytes(system.mem_total_bytes)}`),
      renderMetric("Диск", fmtPercent(system.disk_percent), system.disk_percent, `${fmtBytes(system.disk_free_bytes)} свободно`),
    ] : [
      renderMetric("CPU", "—", 0, "нет данных"),
      renderMetric("RAM", "—", 0, "нет данных"),
      renderMetric("Диск", "—", 0, "нет данных"),
    ];
    return [
      renderHero(verdict.tone, verdict.title, heroText),
      `<div class="stat-grid">${metrics.join("")}</div>`,
      `<div class="quick-grid">
        ${renderQuick("Клиенты", fmtNumber(s.active_clients))}
        ${renderQuick("Трафик", fmtBytes(s.total_used_bytes))}
        ${renderQuick("Службы", `${activeServices}/${services.length}`)}
      </div>`,
      renderNotices(s),
      `<section class="card">
        <div class="card-head"><h2>Службы</h2><span class="chip ${activeServices === services.length && services.length ? "ok" : "warn"}">${activeServices}/${services.length}</span></div>
        <div class="list">${services.length ? serviceRows(6) : '<div class="hint">Службы не настроены.</div>'}</div>
        ${services.length > 6 ? '<button class="link-button" type="button" data-nav="services">Все службы</button>' : ""}
      </section>`,
      panelPreview(),
    ].filter(Boolean).join("");
  }

  function renderUpdateCard() {
    const info = state.update;
    let tone = "warn";
    let chip = "не проверено";
    let details = "";
    if (state.updateLoading) {
      chip = "проверяю";
      details = '<p class="hint">Сравниваю установленный commit с веткой main.</p>';
    } else if (info && info.error) {
      tone = "bad";
      chip = "ошибка";
      details = `<p class="hint">${esc(info.error)}</p>`;
    } else if (info) {
      if (info.update_available === true) {
        tone = "warn";
        chip = "есть обновление";
      } else if (info.update_available === false) {
        tone = "ok";
        chip = "актуально";
      } else {
        chip = "не сравнить";
      }
      details = `<div class="kv">
        <div class="kv-row"><span>Установлено</span><strong>${esc(info.current_version || "—")} · ${esc(info.current_sha || "—")}</strong></div>
        <div class="kv-row"><span>В main</span><strong>${esc(info.remote_version || "—")} · ${esc(info.remote_sha || "—")}</strong></div>
        ${info.remote_date ? `<div class="kv-row"><span>Последний commit</span><strong>${esc(info.remote_date)}</strong></div>` : ""}
      </div>
      ${info.remote_message ? `<p class="hint">${esc(info.remote_message)}</p>` : ""}
      <p class="hint">Установка: бот → «❓ Помощь» → «🔄 Проверить обновления», затем «⬆️ Обновить StackSentry».</p>`;
    } else {
      details = '<p class="hint">Проверьте, есть ли новая версия StackSentry в main.</p>';
    }
    return `<section class="card update-card" id="update-card">
      <div class="card-head"><h2>Обновления StackSentry</h2><span class="chip ${tone}">${esc(chip)}</span></div>
      ${details}
      <button class="ghost" type="button" data-check-update ${state.updateLoading ? "disabled" : ""}>${state.updateLoading ? "Проверяю…" : "Проверить обновления"}</button>
    </section>`;
  }

  function refreshUpdateBanner() {
    const banner = $("update-banner");
    if (!banner) return;
    const info = state.update;
    if (!info || info.update_available !== true) {
      banner.hidden = true;
      banner.innerHTML = "";
      return;
    }
    banner.hidden = false;
    banner.innerHTML = `<span><strong>Новая версия StackSentry</strong> · ${esc(info.current_version || "—")} → ${esc(info.remote_version || info.remote_sha || "—")}</span>
      <button class="link-button" type="button" data-update-help>Подробнее</button>`;
  }

  function refreshUpdateCard() {
    const card = $("update-card");
    if (card) card.outerHTML = renderUpdateCard();
    refreshUpdateBanner();
  }

  async function loadUpdate(force = false) {
    if (state.updateLoading) return;
    if (!force && state.update && Date.now() - state.updateAt < 60000) return;
    state.updateLoading = true;
    refreshUpdateCard();
    try {
      state.update = await api("/api/update");
    } catch (error) {
      state.update = { error: error.message || "Не удалось проверить обновления." };
    } finally {
      state.updateAt = Date.now();
      state.updateLoading = false;
      refreshUpdateCard();
    }
  }

  function renderHelp() {
    const cards = [
      ["Обзор", "Вердикт по серверу, CPU/RAM/диск, клиенты и трафик. Данные обновляются автоматически; кнопка «Обновить» перезапрашивает снимок."],
      ["Службы", "Статус systemd-служб и панели. Если служба не работает — откройте её в боте и посмотрите журнал."],
      ["Панель", "Доступность API панели, версия и состояние xray. Если API не настроен, раздел честно скажет об этом."],
      ["Трафик и подписки", "Inbound'ы, клиенты, остаток трафика и сроки подписок. В «Подписках» есть поиск и сортировка."],
      ["История", "Средние CPU/RAM/диска за 24 часа или 7 дней."],
      ["Терминал", "Появляется, только если включён веб-терминал. Это отдельная сессия со своим TOTP; при первом открытии введите код."],
      ["2FA", "Если 2FA включена при установке, вход в Mini App требует 6-значный код, а опасные действия в боте — подтверждение. Если 2FA выключена, TOTP не запрашивается нигде. Секрет и QR: <code>sudo stacksentry-menu totp</code>."],
      ["Бот", "Команды: <code>/status</code>, <code>/subs</code>, <code>/traffic</code>, <code>/history</code>, <code>/maint</code>, <code>/help</code>. Опасные действия — только в личке администратора."],
      ["Обновление", "Проверка обновлений доступна здесь, в боте «❓ Помощь» или командой <code>sudo stacksentry-menu update-check</code>; установка — из бота или <code>sudo stacksentry-menu update</code>."],
    ];
    const cardsHtml = cards.map(([title, text], index) => `<details class="card help-card"${index === 0 ? " open" : ""}>
      <summary><h2>${title}</h2><span class="help-chevron" aria-hidden="true">⌄</span></summary>
      <div class="help-body"><p class="hint">${text}</p></div>
    </details>`).join("");
    if (!state.update && !state.updateLoading) setTimeout(() => loadUpdate(false), 0);
    return renderHero("ok", "Помощь", "Короткая инструкция по разделам Mini App и боту.") +
      renderUpdateCard() + cardsHtml;
  }

  function renderServices() {
    const s = state.snapshot;
    const services = s.services || [];
    const active = services.filter((item) => item.active).length;
    const unavailable = s.unavailable && s.unavailable.includes("services");
    let heroTone = "ok";
    let title = "Службы работают";
    let text = `${active} из ${services.length} активны.`;
    if (unavailable) {
      heroTone = "warn";
      title = "Состояние служб недоступно";
      text = "Не удалось прочитать systemd. Проверьте права и журнал StackSentry.";
    } else if (services.some((item) => !item.active)) {
      heroTone = "bad";
      title = "Есть неработающие службы";
      text = "Проверьте статус и журнал проблемных служб.";
    } else if (!services.length) {
      heroTone = "warn";
      title = "Службы не найдены";
      text = "Проверьте список SERVICES в конфигурации.";
    }
    return [
      renderHero(heroTone, title, text, true),
      `<section class="card">
        <div class="card-head"><h2>Все службы</h2><span class="chip ${active === services.length && services.length ? "ok" : "warn"}">${active}/${services.length}</span></div>
        <div class="list">${services.length ? services.map(serviceRow).join("") : '<div class="hint">Список служб пуст.</div>'}</div>
      </section>`,
      renderNotices(s),
    ].join("");
  }

  function renderPanel() {
    const s = state.snapshot;
    const panel = s.panel;
    if (panel.disabled) {
      return emptyState(
        "Панель не подключена",
        "API панели сознательно отключён. StackSentry продолжает наблюдать сервер и службы.",
      );
    }
    const panelTone = !panel.reachable ? "bad" : panel.api_authorized ? "ok" : "warn";
    const title = !panel.reachable ? "Панель недоступна" : panel.api_authorized ? "Панель отвечает" : "API-токен не принят";
    const text = panel.latency_ms != null
      ? `Ответ за ${Math.round(panel.latency_ms)} мс.`
      : "Проверка доступности панели.";
    const errorNotice = panel.error
      ? `<div class="notice bad"><span class="notice-icon">${ICONS.bad}</span><div><strong>Ошибка панели</strong>${esc(panel.error)}</div></div>`
      : "";
    return [
      renderHero(panelTone, title, text, true),
      `<section class="card">
        <div class="card-head"><h2>Состояние API</h2><span class="chip ${panelTone}">${panel.reachable ? "отвечает" : "недоступна"}</span></div>
        <div class="kv">
          <div class="kv-row"><span>HTTP</span><strong>${panel.status_code != null ? esc(panel.status_code) : "—"}</strong></div>
          <div class="kv-row"><span>Задержка</span><strong>${panel.latency_ms != null ? `${Math.round(panel.latency_ms)} мс` : "—"}</strong></div>
          <div class="kv-row"><span>Версия панели</span><strong>${esc(panel.panel_version || "—")}</strong></div>
          <div class="kv-row"><span>xray</span><strong>${esc(panel.xray_version || "—")} · ${esc(panel.xray_state || "—")}</strong></div>
          <div class="kv-row"><span>API-токен</span><strong class="chip ${panel.api_authorized ? "ok" : "warn"}">${panel.api_authorized ? "принят" : "не принят"}</strong></div>
        </div>
      </section>`,
      errorNotice,
    ].filter(Boolean).join("");
  }

  function inboundCard(item) {
    const used = Number(item.up_bytes || 0) + Number(item.down_bytes || 0);
    const total = Number(item.total_bytes || 0);
    const percent = total > 0 ? clamp(used / total * 100) : 0;
    const meter = total > 0
      ? `<div class="meter"><i class="${tone(percent)}" style="width:${percent}%"></i></div>`
      : "";
    return `<div class="usage-card">
      <div class="usage-head">
        <h3>${esc(item.remark || "Без названия")}</h3>
        <span class="chip ${item.enabled ? "" : "warn"}">${esc(`${item.protocol}:${item.port}`)}</span>
      </div>
      <div class="usage-value">${fmtBytes(used)}</div>
      ${meter}
      <div class="row-sub">${fmtNumber(item.clients)} кл.${total > 0 ? ` · лимит ${fmtBytes(total)}` : ""}${item.enabled ? "" : " · отключён"}</div>
    </div>`;
  }

  function renderTraffic() {
    const s = state.snapshot;
    const inbounds = s.inbounds || [];
    const subscriptions = s.subscriptions || [];
    const top = [...subscriptions].sort((a, b) => Number(b.used_bytes) - Number(a.used_bytes)).slice(0, 10);
    let inboundsBlock = "";
    if (inbounds.length) {
      inboundsBlock = `<div class="usage-grid">${inbounds.map(inboundCard).join("")}</div>`;
    } else if (s.panel.disabled) {
      inboundsBlock = emptyState("API панели не настроен", "Inbound'ы и подписки доступны после подключения API 3X-UI или Marzban.");
    } else {
      inboundsBlock = emptyState("Inbound'ы не найдены", "Проверьте, что токен панели имеет доступ к списку inbound'ов.");
    }
    const topRows = top.length
      ? top.map((item) => `<div class="list-row">
          <span class="dot ${item.enabled ? "ok" : "warn"}"></span>
          <div class="row-main">
            <span class="row-title">${esc(displayName(item))}</span>
            <span class="row-sub">${esc(item.inbound_remark || "—")}</span>
          </div>
          <span class="row-meta">${fmtBytes(item.used_bytes)}${Number(item.total_bytes) > 0 ? ` / ${fmtBytes(item.total_bytes)}` : ""}</span>
        </div>`).join("")
      : '<div class="hint">Нет данных по клиентам.</div>';
    return [
      `<div class="quick-grid">
        ${renderQuick("Inbound'ы", fmtNumber(inbounds.length))}
        ${renderQuick("Клиенты", fmtNumber(s.active_clients))}
        ${renderQuick("Трафик", fmtBytes(s.total_used_bytes))}
      </div>`,
      inboundsBlock,
      `<section class="card">
        <div class="card-head"><h2>Топ клиентов</h2><span class="chip">${top.length}</span></div>
        <div class="list">${topRows}</div>
      </section>`,
    ].join("");
  }

  function subscriptionStatus(item) {
    const expired = Number(item.expiry_time_ms) > 0 && Number(item.expiry_time_ms) <= Date.now();
    const depleted = Number(item.total_bytes) > 0 && Number(item.used_bytes) >= Number(item.total_bytes);
    if (expired) return { tone: "bad", label: "Истекла" };
    if (depleted) return { tone: "bad", label: "Лимит" };
    if (!item.enabled) return { tone: "warn", label: "Отключена" };
    return { tone: "ok", label: "Активна" };
  }

  function expiryLabel(item) {
    if (!Number(item.expiry_time_ms)) return "без срока";
    const days = Math.ceil((Number(item.expiry_time_ms) - Date.now()) / 86400000);
    if (days < 0) return `истекла ${formatDate(Number(item.expiry_time_ms))}`;
    if (days === 0) return "истекает сегодня";
    return `${days} дн. · до ${formatDate(Number(item.expiry_time_ms))}`;
  }

  function subRow(item) {
    const status = subscriptionStatus(item);
    const used = Number(item.used_bytes || 0);
    const total = Number(item.total_bytes || 0);
    const percent = total > 0 ? clamp(used / total * 100) : 0;
    const search = [displayName(item), item.inbound_remark, item.sub_id].join(" ").toLowerCase();
    return `<div class="sub-row" data-search="${esc(search)}">
      <div class="sub-top">
        <div class="row-main">
          <span class="row-title">${esc(displayName(item))}</span>
          <span class="row-sub">${esc(item.inbound_remark || "—")}${item.sub_id ? ` · ${esc(item.sub_id)}` : ""}</span>
        </div>
        <span class="chip ${status.tone}">${status.label}</span>
      </div>
      ${total > 0 ? `<div class="meter"><i class="${tone(percent)}" style="width:${percent}%"></i></div>` : ""}
      <div class="sub-foot">
        <span>${fmtBytes(used)}${total > 0 ? ` / ${fmtBytes(total)}` : " · без лимита"}</span>
        <span>${esc(expiryLabel(item))}</span>
      </div>
    </div>`;
  }

  function sortedSubs(items) {
    const copy = [...items];
    if (state.subsSort === "name") {
      copy.sort((a, b) => displayName(a).localeCompare(displayName(b), "ru"));
    } else if (state.subsSort === "expiry") {
      const value = (item) => Number(item.expiry_time_ms) > 0 ? Number(item.expiry_time_ms) : Number.MAX_SAFE_INTEGER;
      copy.sort((a, b) => value(a) - value(b));
    } else {
      copy.sort((a, b) => Number(b.used_bytes) - Number(a.used_bytes));
    }
    return copy;
  }

  function applySubsFilter() {
    const query = state.subsQuery.trim().toLowerCase();
    const rows = [...view.querySelectorAll(".sub-row")];
    let visible = 0;
    for (const row of rows) {
      const match = !query || (row.dataset.search || "").includes(query);
      row.hidden = !match;
      if (match) visible += 1;
    }
    const count = $("subs-count");
    if (count) count.textContent = `${visible} из ${rows.length}`;
    const empty = $("subs-empty");
    if (empty) empty.hidden = visible > 0;
  }

  function renderSubs() {
    const s = state.snapshot;
    const subscriptions = s.subscriptions || [];
    if (!subscriptions.length) {
      view.innerHTML = s.panel.disabled
        ? emptyState("Подписки недоступны", "API панели не настроен. Подключите 3X-UI или Marzban.")
        : emptyState("Подписок нет", "В панели пока нет клиентов или токен не видит подписки.");
      return;
    }
    const focused = document.activeElement && document.activeElement.id === "subs-search";
    const options = [
      ["usage", "По расходу"], ["expiry", "По сроку"], ["name", "По имени"],
    ].map(([value, label]) => `<option value="${value}"${state.subsSort === value ? " selected" : ""}>${label}</option>`).join("");
    view.innerHTML = `<section class="card">
      <div class="card-head"><h2>Подписки</h2><span id="subs-count" class="chip">${subscriptions.length} из ${subscriptions.length}</span></div>
      <div class="toolbar">
        <div class="search">
          <label class="sr-only" for="subs-search">Поиск подписок</label>
          <input id="subs-search" type="search" placeholder="Email, inbound или ID" autocomplete="off" value="${esc(state.subsQuery)}">
        </div>
        <div class="select">
          <label class="sr-only" for="subs-sort">Сортировка</label>
          <select id="subs-sort">${options}</select>
        </div>
      </div>
      <div class="list">${sortedSubs(subscriptions).map(subRow).join("")}</div>
      <p id="subs-empty" class="hint" hidden>Ничего не найдено. Измените запрос.</p>
    </section>`;
    if (focused) {
      const input = $("subs-search");
      if (input) {
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      }
    }
    applySubsFilter();
  }

  function bucketLabel(bucket, hours) {
    const date = new Date(Number(bucket) * 1000);
    if (Number.isNaN(date.getTime())) return "—";
    if (hours >= 168) return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
    return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  function downsample(values, limit = 56) {
    if (values.length <= limit) return values;
    const size = Math.ceil(values.length / limit);
    const output = [];
    for (let index = 0; index < values.length; index += size) {
      const group = values.slice(index, index + size);
      output.push(group.reduce((sum, value) => sum + value, 0) / group.length);
    }
    return output;
  }

  function chartCard(rows, key, label, hours) {
    const values = rows.map((row) => Number(row[key] || 0)).filter(Number.isFinite);
    if (!values.length) return "";
    const bars = downsample(values);
    const current = values[values.length - 1];
    const average = values.reduce((sum, value) => sum + value, 0) / values.length;
    const maximum = Math.max(...values);
    const description = `${label}: сейчас ${fmtPercent(current)}, среднее ${fmtPercent(average)}, максимум ${fmtPercent(maximum)}`;
    return `<section class="card chart-card">
      <div class="card-head"><h2>${esc(label)}</h2><span class="chip ${tone(current)}">${fmtPercent(current)}</span></div>
      <div class="chart" role="img" aria-label="${esc(description)}">${bars.map((value) => `<i style="height:${clamp(value, 2, 100)}%"></i>`).join("")}</div>
      <div class="chart-axis"><span>${esc(bucketLabel(rows[0].bucket, hours))}</span><span>${esc(bucketLabel(rows[rows.length - 1].bucket, hours))}</span></div>
      <div class="chart-stats"><span>сейчас ${fmtPercent(current)}</span><span>сред. ${fmtPercent(average)}</span><span>макс. ${fmtPercent(maximum)}</span></div>
    </section>`;
  }

  function renderHistoryBody(rows, hours) {
    const body = $("history-body");
    if (!body) return;
    body.className = "";
    body.removeAttribute("aria-busy");
    if (!rows.length) {
      body.innerHTML = emptyState("Истории пока нет", "Данные появятся после нескольких срезов монитора.");
      return;
    }
    body.innerHTML = [
      chartCard(rows, "cpu", "CPU", hours),
      chartCard(rows, "mem", "RAM", hours),
      chartCard(rows, "disk", "Диск", hours),
    ].filter(Boolean).join("");
  }

  function renderHistoryShell() {
    const hours = state.historyHours === 168 ? 168 : 24;
    return `<div class="segmented" role="group" aria-label="Период истории">
        <button type="button" data-hours="24" class="${hours === 24 ? "active" : ""}">24 часа</button>
        <button type="button" data-hours="168" class="${hours === 168 ? "active" : ""}">7 дней</button>
      </div>
      <div id="history-body" class="skeleton-stack" aria-busy="true">
        <div class="skeleton-card"></div><div class="skeleton-card"></div><div class="skeleton-card"></div>
      </div>`;
  }

  async function loadHistory() {
    const token = ++state.historyToken;
    const hours = state.historyHours;
    const body = $("history-body");
    if (!body) return;
    body.className = "skeleton-stack";
    body.setAttribute("aria-busy", "true");
    body.innerHTML = '<div class="skeleton-card"></div><div class="skeleton-card"></div><div class="skeleton-card"></div>';
    try {
      const rows = await api(`/api/history?hours=${hours === 168 ? 168 : 24}`);
      if (token !== state.historyToken || state.active !== "history") return;
      renderHistoryBody(rows, hours);
    } catch (error) {
      if (token !== state.historyToken || state.active !== "history") return;
      const current = $("history-body");
      if (!current) return;
      current.className = "";
      current.removeAttribute("aria-busy");
      const message = error.status === 401
        ? "Сессия истекла. Обновите данные, чтобы войти снова."
        : (error.message || "История недоступна.");
      current.innerHTML = emptyState(
        "Не удалось загрузить историю",
        message,
        '<button class="primary" type="button" data-retry>Повторить</button>',
      );
    }
  }

  function clearTerminal() {
    terminalView.innerHTML = "";
    delete terminalView.dataset.mode;
  }

  function ensureTerminal() {
    if (!state.terminalOrigin) {
      if (terminalView.dataset.mode !== "empty") {
        terminalView.dataset.mode = "empty";
        terminalView.innerHTML = emptyState(
          "Терминал не настроен",
          "Установите терминал отдельной командой и перезапустите Mini App.",
          '<p class="hint"><code>sudo bash install.sh --terminal --root-terminal</code></p>',
        );
      }
      return;
    }
    if (terminalView.querySelector("iframe.terminal-frame")) return;
    let href = state.terminalOrigin;
    try {
      href = new URL(state.terminalOrigin).href;
    } catch (_) { /* оставляем исходный адрес */ }
    terminalView.dataset.mode = "frame";
    terminalView.innerHTML = `<div class="terminal-shell">
      <div class="terminal-bar">
        <span>Терминал сервера</span>
        <a class="link-button" href="${esc(href)}" target="_blank" rel="noopener noreferrer">Открыть отдельно</a>
      </div>
      <div class="terminal-stage">
        <div class="terminal-loader" id="terminal-loader">Подключаю терминал…</div>
        <iframe class="terminal-frame" src="${esc(href)}" title="Терминал сервера" allow="clipboard-read; clipboard-write"></iframe>
      </div>
    </div>`;
    const frame = terminalView.querySelector("iframe.terminal-frame");
    const loader = $("terminal-loader");
    // Передаём initData через postMessage, а не через URL: не попадает в
    // историю, referrer, логи прокси и не утекает при копировании ссылки.
    frame.addEventListener("load", () => {
      if (!initData) return;
      try {
        const origin = new URL(state.terminalOrigin).origin;
        frame.contentWindow.postMessage(
          { type: "stacksentry-init", initData },
          origin,
        );
      } catch (_) { /* iframe сам покажет форму входа */ }
    });
    if (frame && loader) {
      frame.addEventListener("load", () => loader.classList.add("hidden"), { once: true });
    }
    setTimeout(() => {
      if (loader && !loader.classList.contains("hidden")) {
        loader.textContent = "Терминал не ответил. Откройте его отдельно.";
      }
    }, 12000);
  }

  function updateViews() {
    const terminal = state.active === "terminal";
    view.hidden = terminal;
    terminalView.hidden = !terminal;
    const panel = terminal ? terminalView : view;
    panel.setAttribute("aria-labelledby", `tab-${state.active}`);
  }

  async function render() {
    if (!state.snapshot) return;
    updateHeader();
    updateViews();
    if (state.active === "terminal") {
      ensureTerminal();
      return;
    }
    if (state.active === "overview") view.innerHTML = renderOverview();
    else if (state.active === "services") view.innerHTML = renderServices();
    else if (state.active === "panel") view.innerHTML = renderPanel();
    else if (state.active === "traffic") view.innerHTML = renderTraffic();
    else if (state.active === "subs") renderSubs();
    else if (state.active === "help") view.innerHTML = renderHelp();
    else if (state.active === "history") {
      view.innerHTML = renderHistoryShell();
      await loadHistory();
    }
  }

  function renderTabs() {
    $("tabs").innerHTML = state.tabs.map(([key, label]) => {
      const active = key === state.active;
      return `<button id="tab-${key}" class="tab${active ? " active" : ""}" type="button"
        role="tab" aria-selected="${active}" aria-controls="${key === "terminal" ? "terminal-view" : "view"}"
        tabindex="${active ? 0 : -1}" data-tab="${key}">${esc(label)}</button>`;
    }).join("");
    const current = $(`tab-${state.active}`);
    if (current) current.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function switchTab(key, focus = false) {
    if (!state.tabs.some(([name]) => name === key)) return;
    if (key === state.active) {
      if (focus) {
        const current = $(`tab-${key}`);
        if (current) current.focus();
      }
      return;
    }
    state.active = key;
    state.historyToken += 1;
    syncTabHash();
    renderTabs();
    if (focus) {
      const current = $(`tab-${key}`);
      if (current) current.focus();
    }
    render();
  }

  class ApiError extends Error {
    constructor(message, status, code) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.code = code || "";
    }
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store",
      credentials: "same-origin",
      ...options,
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = payload && payload.detail;
      const message = typeof detail === "string"
        ? detail
        : (detail && detail.message) || `HTTP ${response.status}`;
      const code = detail && typeof detail === "object" ? detail.code : "";
      throw new ApiError(message, response.status, code);
    }
    return payload;
  }

  function authErrorText(error) {
    if (error.code === "totp_replay") return "Этот код уже использован. Дождитесь следующего.";
    if (error.code === "totp_locked") return "Слишком много попыток. Подождите минуту.";
    if (error.code === "totp_invalid") return "Неверный код. Проверьте время на устройстве.";
    if (error.code === "totp_required") return "Введите код 2FA.";
    return error.message || "Не удалось войти.";
  }

  function renderAuth(message, showTotp) {
    state.authPending = true;
    clearTerminal();
    view.hidden = false;
    terminalView.hidden = true;
    view.removeAttribute("aria-labelledby");
    $("tabs").innerHTML = "";
    setStatus("warn", "нужен вход");
    const help = showTotp
      ? `<p class="hint">Код выдаёт приложение-аутентификатор. Секрет и QR можно посмотреть командой <code>sudo stacksentry-menu totp</code>. Если 2FA выключена, этот экран не появляется.</p>`
      : initData
        ? '<p class="hint">Не удалось подтвердить подпись Telegram. Откройте Mini App заново из бота.</p>'
        : '<p class="hint">Откройте это приложение кнопкой в Telegram — вход по подписи Telegram, без паролей.</p>';
    view.innerHTML = `<div class="card auth">
      <div class="auth-mark" aria-hidden="true">SS</div>
      <h2>${showTotp ? "Подтвердите вход" : "Нужен вход"}</h2>
      <p class="hint">${esc(message || "Доступ только для администратора.")}</p>
      ${showTotp ? `<form id="totp-form" class="auth-form">
        <label for="totp-code">Код 2FA</label>
        <input id="totp-code" class="totp-input" inputmode="numeric" pattern="[0-9]*"
               autocomplete="one-time-code" maxlength="6" placeholder="000000"
               aria-describedby="totp-error" required>
        <p id="totp-error" class="error" role="alert"></p>
        <button class="primary" type="submit">Войти</button>
      </form>` : ""}
      ${help}
      <button id="retry-auth" class="ghost" type="button">Повторить</button>
    </div>`;
    $("retry-auth").addEventListener("click", () => {
      state.authRetry = 0;
      state.authPending = false;
      load();
    });
    if (!showTotp) return;
    const form = $("totp-form");
    const input = $("totp-code");
    const error = $("totp-error");
    input.addEventListener("input", () => {
      const digits = input.value.replace(/\D/g, "").slice(0, 6);
      if (input.value !== digits) input.value = digits;
    });
    input.focus();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const code = (input.value || "").replace(/\D/g, "");
      if (code.length !== 6) {
        error.textContent = "Введите 6 цифр.";
        input.focus();
        return;
      }
      const button = form.querySelector("button");
      button.disabled = true;
      button.textContent = "Проверяю…";
      try {
        await api("/api/session", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ initData, totp: code }),
        });
        state.authPending = false;
        state.authRetry = 0;
        await load();
      } catch (authError) {
        error.textContent = authErrorText(authError);
        button.disabled = false;
        button.textContent = "Войти";
        input.select();
      }
    });
  }

  async function authenticate() {
    if (!initData) {
      renderAuth("Откройте это приложение кнопкой в Telegram.", false);
      return false;
    }
    try {
      await api("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initData }),
      });
      return true;
    } catch (error) {
      if (error.code === "totp_required") {
        renderAuth("Введите код из приложения-аутентификатора.", true);
      } else {
        renderAuth(authErrorText(error), false);
      }
      return false;
    }
  }

  async function loadMeta() {
    if (state.metaLoaded) return;
    try {
      const meta = await api("/api/meta");
      state.metaLoaded = true;
      state.version = meta.version || "";
      state.terminalOrigin = meta.terminal_origin || "";
      state.tabs = state.terminalOrigin
        ? [...BASE_TABS, ["terminal", "Терминал"]]
        : BASE_TABS.slice();
      if (state.active === "terminal" && !state.terminalOrigin) state.active = "overview";
      syncTabHash();
      renderTabs();
    } catch (_) {
      renderTabs();
    }
  }

  function setBusy(value) {
    const button = $("refresh");
    button.classList.toggle("is-loading", value);
    button.disabled = value;
    main.setAttribute("aria-busy", value ? "true" : "false");
  }

  async function load() {
    if (state.authPending || state.loading) return;
    state.loading = true;
    setBusy(true);
    let retry = false;
    try {
      const snapshot = await api("/api/status");
      state.snapshot = snapshot;
      state.authRetry = 0;
      await loadMeta();
      await render();
      if (!state.update && !state.updateLoading) loadUpdate(false);
    } catch (error) {
      if (error.status === 401) {
        if (state.authRetry > 0) {
          renderAuth("Сессия не сохраняется. Откройте Mini App заново из Telegram.", false);
        } else {
          state.authRetry += 1;
          retry = await authenticate();
        }
      } else if (state.snapshot) {
        showToast("Не удалось обновить данные");
        setStatus("bad", "ошибка обновления", ago(state.snapshot.age));
      } else {
        renderError(error.message);
      }
    } finally {
      state.loading = false;
      setBusy(false);
    }
    if (retry) {
      state.authPending = false;
      await load();
    }
  }

  applyTheme();
  try {
    if (tg) {
      if (tg.ready) tg.ready();
      if (tg.expand) tg.expand();
    }
  } catch (_) { /* Telegram API не критичен */ }

  view.setAttribute("role", "tabpanel");
  terminalView.setAttribute("role", "tabpanel");

  $("update-banner").addEventListener("click", (event) => {
    if (event.target.closest("[data-update-help]")) switchTab("help");
  });

  $("refresh").addEventListener("click", async () => {
    if (state.loading || state.authPending) return;
    await load();
    if (!state.authPending && state.snapshot) showToast("Обновлено");
  });

  $("tabs").addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) switchTab(tab.dataset.tab);
  });

  $("tabs").addEventListener("keydown", (event) => {
    const tabs = [...$("tabs").querySelectorAll(".tab")];
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    let next = null;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    if (next !== null) {
      event.preventDefault();
      switchTab(tabs[next].dataset.tab, true);
    }
  });

  view.addEventListener("click", (event) => {
    const nav = event.target.closest("[data-nav]");
    if (nav) {
      switchTab(nav.dataset.nav);
      return;
    }
    if (event.target.closest("[data-check-update]")) {
      loadUpdate(true);
      return;
    }
    if (event.target.closest("[data-retry]")) {
      load();
      return;
    }
    const hours = event.target.closest("[data-hours]");
    if (hours) {
      state.historyHours = Number(hours.dataset.hours) === 168 ? 168 : 24;
      render();
    }
  });

  view.addEventListener("input", (event) => {
    if (event.target.id === "subs-search") {
      state.subsQuery = event.target.value;
      applySubsFilter();
    }
  });

  view.addEventListener("change", (event) => {
    if (event.target.id === "subs-sort") {
      state.subsSort = event.target.value;
      state.subsQuery = (($("subs-search") || {}).value || "");
      renderSubs();
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !state.authPending) load();
  });

  window.addEventListener("beforeunload", () => {
    if (state.timer) clearInterval(state.timer);
  });

  syncTabHash();
  renderTabs();
  load();
  state.timer = setInterval(() => {
    if (!document.hidden && !state.authPending) load();
  }, 20000);
})();
