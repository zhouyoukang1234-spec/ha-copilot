// HA-Copilot Workspace — a dependency-free custom element that turns Home
// Assistant into a Cursor/VS-Code-style workspace driven by conversation.
//
// Home Assistant injects `hass`, `narrow`, `route` and `panel` properties.
// The left activity bar switches between deterministic views (Overview,
// Devices, Automations, Editor, Logs, Integrations) that read/write the live
// instance directly through `hass` + the `ha_copilot.run_tool` service, while
// the right-docked Copilot drives everything in natural language. The two are
// fused: anything the Copilot changes refreshes the active view, and every
// view exposes a "tell Copilot" shortcut.

const LS = {
  view: "ha_copilot_view",
  chatOpen: "ha_copilot_chat_open",
  history: "ha_copilot_history",
  editorFile: "ha_copilot_editor_file",
};

const VIEWS = [
  { id: "overview", icon: "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z", label: "总览" },
  { id: "devices", icon: "M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z", label: "设备/实体" },
  { id: "automations", icon: "M13 3v6h8l-9 13v-9H4l9-10z", label: "自动化" },
  { id: "editor", icon: "M9.4 16.6 4.8 12l4.6-4.6L8 6l-6 6 6 6 1.4-1.4zm5.2 0L19.2 12l-4.6-4.6L16 6l6 6-6 6-1.4-1.4z", label: "配置编辑" },
  { id: "logs", icon: "M3 3h18v2H3zm0 4h18v2H3zm0 4h12v2H3zm0 4h18v2H3zm0 4h12v2H3z", label: "日志" },
  { id: "integrations", icon: "M10 4h4v2h2a2 2 0 0 1 2 2v3h2v4h-2v3a2 2 0 0 1-2 2h-3v2h-4v-2H8a2 2 0 0 1-2-2v-3H4v-4h2V8a2 2 0 0 1 2-2h2V4z", label: "集成" },
];

const TOGGLE_DOMAINS = ["light", "switch", "input_boolean", "fan", "siren", "humidifier"];
const DOMAIN_LABELS = {
  light: "灯光", switch: "开关", input_boolean: "布尔量", input_number: "数值",
  fan: "风扇", sensor: "传感器", binary_sensor: "二元传感器", climate: "温控",
  automation: "自动化", scene: "场景", script: "脚本", person: "人员",
  weather: "天气", sun: "太阳", media_player: "媒体", cover: "窗帘",
  camera: "摄像头", lock: "门锁", device_tracker: "设备追踪", zone: "区域",
};

class HaCopilotPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._rendered = false;
    this._busy = false;
    this._view = localStorage.getItem(LS.view) || "overview";
    this._chatOpen = localStorage.getItem(LS.chatOpen) !== "0";
    this._history = this._loadHistory();
    this._cfg = null;
    this._search = "";
    this._domainFilter = "";
    this._editorPath = localStorage.getItem(LS.editorFile) || "configuration.yaml";
    this._editorDirty = false;
    this._refreshTimer = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._rendered) {
      this._render();
      this._rendered = true;
      this._loadConfig();
      this._switchView(this._view, true);
    } else if (this._isLiveView()) {
      // Debounce live refresh on the stream of hass updates.
      clearTimeout(this._refreshTimer);
      this._refreshTimer = setTimeout(() => this._renderView(), 400);
    }
    if (first) this._updateStatus();
  }

  get hass() {
    return this._hass;
  }

  // ---- persistence -------------------------------------------------------
  _loadHistory() {
    try {
      const raw = localStorage.getItem(LS.history);
      const h = raw ? JSON.parse(raw) : [];
      return Array.isArray(h) ? h.slice(-16) : [];
    } catch (e) {
      return [];
    }
  }

  _saveHistory() {
    try {
      localStorage.setItem(LS.history, JSON.stringify(this._history.slice(-16)));
    } catch (e) {
      /* quota — non-fatal */
    }
  }

  // ---- backend helpers ---------------------------------------------------
  async _api(method, path, body) {
    return this._hass.callApi(method, path, body);
  }

  async _ws(msg) {
    return this._hass.callWS(msg);
  }

  async _runTool(tool, args = {}) {
    const res = await this._hass.connection.sendMessagePromise({
      type: "call_service",
      domain: "ha_copilot",
      service: "run_tool",
      service_data: { tool, args },
      return_response: true,
    });
    return res && res.response ? res.response : res;
  }

  async _loadConfig() {
    try {
      this._cfg = await this._api("GET", "ha_copilot/config");
    } catch (e) {
      this._cfg = null;
    }
    this._updateStatus();
  }

  // ---- DOM utilities -----------------------------------------------------
  _el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "html") node.innerHTML = v;
        else if (k.startsWith("on") && typeof v === "function") {
          node.addEventListener(k.slice(2), v);
        } else if (v !== undefined && v !== null) {
          node.setAttribute(k, v);
        }
      }
    }
    for (const c of [].concat(children || [])) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  _icon(d, size = 22) {
    return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="currentColor"><path d="${d}"/></svg>`;
  }

  // ---- top-level render --------------------------------------------------
  _render() {
    this.innerHTML = `
      <style>${this._css()}</style>
      <div class="cp-root">
        <nav class="cp-activity" id="cp-activity"></nav>
        <section class="cp-main">
          <div class="cp-viewbar">
            <div class="cp-viewtitle" id="cp-viewtitle">总览</div>
            <div class="cp-viewactions" id="cp-viewactions"></div>
            <button class="cp-chip" id="cp-toggle-chat" title="切换 Copilot">☰ Copilot</button>
          </div>
          <div class="cp-view" id="cp-view"></div>
        </section>
        <aside class="cp-chat" id="cp-chat">
          <div class="cp-chat-head">
            <span class="cp-dot" id="cp-dot"></span>
            <b>Copilot</b>
            <span class="cp-chat-status" id="cp-chat-status">connecting…</span>
            <button class="cp-iconbtn" id="cp-clear" title="清空对话">⟲</button>
          </div>
          <div class="cp-log" id="cp-log"></div>
          <div class="cp-input">
            <textarea id="cp-box" placeholder="给 Copilot 下达指令…  (Enter 发送, Shift+Enter 换行)"></textarea>
            <button id="cp-send">发送</button>
          </div>
        </aside>
        <footer class="cp-status" id="cp-status"></footer>
      </div>`;

    // Activity bar
    const act = this.querySelector("#cp-activity");
    for (const v of VIEWS) {
      const b = this._el("button", {
        class: "cp-act" + (v.id === this._view ? " active" : ""),
        title: v.label,
        "data-view": v.id,
        html: this._icon(v.icon),
        onclick: () => this._switchView(v.id),
      });
      act.appendChild(b);
    }

    // Chat wiring
    this._renderChatHistory();
    const box = this.querySelector("#cp-box");
    this.querySelector("#cp-send").addEventListener("click", () => this._send());
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this._send();
      }
    });
    this.querySelector("#cp-clear").addEventListener("click", () => {
      this._history = [];
      this._saveHistory();
      this.querySelector("#cp-log").innerHTML = "";
      this._greet();
    });
    this.querySelector("#cp-toggle-chat").addEventListener("click", () => this._toggleChat());

    this._applyChatOpen();
  }

  _toggleChat() {
    this._chatOpen = !this._chatOpen;
    localStorage.setItem(LS.chatOpen, this._chatOpen ? "1" : "0");
    this._applyChatOpen();
  }

  _applyChatOpen() {
    this.querySelector(".cp-root").classList.toggle("chat-closed", !this._chatOpen);
  }

  // ---- status bar / chat header -----------------------------------------
  _updateStatus() {
    const sb = this.querySelector("#cp-status");
    if (!sb) return;
    const states = this._hass ? Object.keys(this._hass.states || {}).length : 0;
    const cfg = this._cfg || {};
    const write = cfg.allow_write ? "可写" : "只读";
    const conn = this._hass && this._hass.connected !== false ? "已连接" : "连接中";
    sb.innerHTML =
      `<span>● ${conn}</span>` +
      `<span>${states} 实体</span>` +
      (cfg.model ? `<span>模型 ${this._esc(cfg.model)}</span>` : "") +
      (cfg.base_url ? `<span class="dim">${this._esc(cfg.base_url)}</span>` : "") +
      `<span class="cp-write ${cfg.allow_write ? "ok" : "ro"}">${write}</span>`;
    const cs = this.querySelector("#cp-chat-status");
    if (cs) cs.textContent = cfg.model ? `${cfg.model}` : "ready";
    const dot = this.querySelector("#cp-dot");
    if (dot) dot.className = "cp-dot ok";
  }

  // ---- view routing ------------------------------------------------------
  _isLiveView() {
    return ["overview", "devices", "automations"].includes(this._view);
  }

  _switchView(id, skipStore) {
    this._view = id;
    if (!skipStore) localStorage.setItem(LS.view, id);
    this.querySelectorAll(".cp-act").forEach((b) =>
      b.classList.toggle("active", b.getAttribute("data-view") === id)
    );
    const v = VIEWS.find((x) => x.id === id);
    this.querySelector("#cp-viewtitle").textContent = v ? v.label : id;
    this._renderView();
  }

  async _renderView() {
    const host = this.querySelector("#cp-view");
    const actions = this.querySelector("#cp-viewactions");
    if (!host) return;
    actions.innerHTML = "";
    try {
      if (this._view === "overview") return this._viewOverview(host, actions);
      if (this._view === "devices") return this._viewDevices(host, actions);
      if (this._view === "automations") return this._viewAutomations(host, actions);
      if (this._view === "editor") return this._viewEditor(host, actions);
      if (this._view === "logs") return this._viewLogs(host, actions);
      if (this._view === "integrations") return this._viewIntegrations(host, actions);
    } catch (e) {
      host.innerHTML = "";
      host.appendChild(this._el("div", { class: "cp-empty", text: "加载失败: " + (e && e.message ? e.message : e) }));
    }
  }

  // ---- OVERVIEW ----------------------------------------------------------
  _viewOverview(host, actions) {
    const states = Object.values(this._hass.states || {});
    const byDomain = {};
    for (const s of states) {
      const d = s.entity_id.split(".")[0];
      byDomain[d] = (byDomain[d] || 0) + 1;
    }
    const autos = states.filter((s) => s.entity_id.startsWith("automation."));
    const autoOn = autos.filter((s) => s.state === "on").length;
    const areas = this._hass.areas ? Object.keys(this._hass.areas).length : "—";

    actions.appendChild(this._actionBtn("检查配置", () => this._runConfigCheck(host)));
    actions.appendChild(this._actionBtn("重载 YAML", () => this._reloadAll(host)));

    host.innerHTML = "";
    const cards = this._el("div", { class: "cp-cards" });
    cards.appendChild(this._statCard(String(states.length), "实体总数"));
    cards.appendChild(this._statCard(String(autos.length), `自动化 (${autoOn} 启用)`));
    cards.appendChild(this._statCard(String(byDomain.light || 0), "灯光"));
    cards.appendChild(this._statCard(String(areas), "区域"));
    host.appendChild(cards);

    const result = this._el("div", { class: "cp-result", id: "cp-ov-result" });
    host.appendChild(result);

    const h = this._el("div", { class: "cp-section-h", text: "按域分布" });
    host.appendChild(h);
    const grid = this._el("div", { class: "cp-domgrid" });
    for (const [d, n] of Object.entries(byDomain).sort((a, b) => b[1] - a[1])) {
      const chip = this._el("button", {
        class: "cp-dombadge",
        onclick: () => {
          this._domainFilter = d;
          this._switchView("devices");
        },
      });
      chip.appendChild(this._el("span", { class: "cp-dombadge-n", text: String(n) }));
      chip.appendChild(this._el("span", { text: (DOMAIN_LABELS[d] || d) }));
      grid.appendChild(chip);
    }
    host.appendChild(grid);
  }

  async _runConfigCheck(host) {
    const out = this.querySelector("#cp-ov-result") || host;
    out.innerHTML = "正在检查配置…";
    try {
      const r = await this._runTool("check_config");
      if (r.valid) {
        out.className = "cp-result ok";
        out.textContent = "✓ 配置有效" + (r.warnings && r.warnings.length ? ` (${r.warnings.length} 条警告)` : "");
      } else {
        out.className = "cp-result err";
        out.textContent = "✗ 配置错误:\n" + (r.errors || []).join("\n");
      }
    } catch (e) {
      out.className = "cp-result err";
      out.textContent = "检查失败: " + (e.message || e);
    }
  }

  async _reloadAll(host) {
    const out = this.querySelector("#cp-ov-result") || host;
    out.innerHTML = "正在重载…";
    try {
      const r = await this._runTool("reload", { domain: "core" });
      out.className = "cp-result ok";
      out.textContent = "✓ 已重载: " + (r.reloaded || "core");
    } catch (e) {
      out.className = "cp-result err";
      out.textContent = "重载失败: " + (e.message || e);
    }
  }

  // ---- DEVICES -----------------------------------------------------------
  _viewDevices(host, actions) {
    const states = Object.values(this._hass.states || {}).sort((a, b) =>
      a.entity_id.localeCompare(b.entity_id)
    );
    const domains = [...new Set(states.map((s) => s.entity_id.split(".")[0]))].sort();

    // controls row
    const ctrl = this._el("div", { class: "cp-controls" });
    const search = this._el("input", {
      class: "cp-input-text",
      placeholder: "搜索实体 / 名称…",
      value: this._search,
      oninput: (e) => {
        this._search = e.target.value;
        this._fillDeviceList();
      },
    });
    ctrl.appendChild(search);
    const sel = this._el("select", {
      class: "cp-select",
      onchange: (e) => {
        this._domainFilter = e.target.value;
        this._fillDeviceList();
      },
    });
    sel.appendChild(this._el("option", { value: "", text: "全部域" }));
    for (const d of domains) {
      const o = this._el("option", { value: d, text: (DOMAIN_LABELS[d] || d) + ` (${states.filter((s) => s.entity_id.startsWith(d + ".")).length})` });
      if (d === this._domainFilter) o.selected = true;
      sel.appendChild(o);
    }
    ctrl.appendChild(sel);

    host.innerHTML = "";
    host.appendChild(ctrl);
    const list = this._el("div", { class: "cp-list", id: "cp-devlist" });
    host.appendChild(list);
    this._fillDeviceList();
  }

  _fillDeviceList() {
    const list = this.querySelector("#cp-devlist");
    if (!list) return;
    const q = this._search.trim().toLowerCase();
    const states = Object.values(this._hass.states || {}).sort((a, b) =>
      a.entity_id.localeCompare(b.entity_id)
    );
    const rows = states.filter((s) => {
      if (this._domainFilter && !s.entity_id.startsWith(this._domainFilter + ".")) return false;
      if (!q) return true;
      const fn = (s.attributes.friendly_name || "").toLowerCase();
      return s.entity_id.toLowerCase().includes(q) || fn.includes(q);
    });
    list.innerHTML = "";
    if (!rows.length) {
      list.appendChild(this._el("div", { class: "cp-empty", text: "没有匹配的实体" }));
      return;
    }
    for (const s of rows.slice(0, 300)) {
      list.appendChild(this._deviceRow(s));
    }
    if (rows.length > 300) {
      list.appendChild(this._el("div", { class: "cp-empty", text: `… 还有 ${rows.length - 300} 个，请用搜索缩小范围` }));
    }
  }

  _deviceRow(s) {
    const domain = s.entity_id.split(".")[0];
    const row = this._el("div", { class: "cp-row" });
    const left = this._el("div", { class: "cp-row-main" });
    left.appendChild(this._el("div", { class: "cp-row-name", text: s.attributes.friendly_name || s.entity_id }));
    left.appendChild(this._el("div", { class: "cp-row-id", text: s.entity_id }));
    row.appendChild(left);

    const right = this._el("div", { class: "cp-row-ctrl" });
    if (TOGGLE_DOMAINS.includes(domain)) {
      const on = s.state === "on";
      const sw = this._el("button", {
        class: "cp-switch" + (on ? " on" : ""),
        title: on ? "关闭" : "打开",
        onclick: async (e) => {
          e.stopPropagation();
          sw.classList.add("pending");
          try {
            await this._hass.callService(
              domain === "input_boolean" ? "input_boolean" : domain,
              on ? "turn_off" : "turn_on",
              { entity_id: s.entity_id }
            );
          } finally {
            sw.classList.remove("pending");
          }
        },
      });
      sw.appendChild(this._el("span", { class: "cp-knob" }));
      right.appendChild(sw);
    } else if (domain === "input_number" || domain === "number") {
      const inp = this._el("input", {
        class: "cp-num", type: "number",
        value: s.state,
        min: s.attributes.min, max: s.attributes.max, step: s.attributes.step || 1,
        onchange: (e) =>
          this._hass.callService(domain, "set_value", { entity_id: s.entity_id, value: Number(e.target.value) }),
      });
      right.appendChild(inp);
    } else {
      right.appendChild(this._el("span", { class: "cp-state", text: this._fmtState(s) }));
    }
    right.appendChild(this._el("button", {
      class: "cp-iconbtn small", title: "详情", text: "⋯",
      onclick: (e) => { e.stopPropagation(); this._showEntityDetail(s.entity_id); },
    }));
    row.appendChild(right);
    row.addEventListener("click", () => this._showEntityDetail(s.entity_id));
    return row;
  }

  _fmtState(s) {
    const u = s.attributes.unit_of_measurement;
    return u ? `${s.state} ${u}` : s.state;
  }

  async _showEntityDetail(entity_id) {
    const s = this._hass.states[entity_id];
    if (!s) return;
    const back = this._el("div", { class: "cp-modal-back", onclick: (e) => { if (e.target === back) back.remove(); } });
    const modal = this._el("div", { class: "cp-modal" });
    modal.appendChild(this._el("div", { class: "cp-modal-head" }, [
      this._el("b", { text: s.attributes.friendly_name || entity_id }),
      this._el("button", { class: "cp-iconbtn", text: "✕", onclick: () => back.remove() }),
    ]));
    modal.appendChild(this._el("div", { class: "cp-modal-sub", text: entity_id + "  ·  " + this._fmtState(s) }));
    const attrs = this._el("pre", { class: "cp-json", text: JSON.stringify(s.attributes, null, 2) });
    modal.appendChild(attrs);
    const ftr = this._el("div", { class: "cp-modal-ftr" });
    ftr.appendChild(this._actionBtn("交给 Copilot", () => {
      back.remove();
      this._prefillChat(`针对实体 ${entity_id}：`);
    }));
    modal.appendChild(ftr);
    back.appendChild(modal);
    this.querySelector(".cp-root").appendChild(back);
  }

  // ---- AUTOMATIONS -------------------------------------------------------
  _viewAutomations(host, actions) {
    actions.appendChild(this._actionBtn("新建自动化", () =>
      this._prefillChat("创建一个自动化：")
    ));
    const autos = Object.values(this._hass.states || {})
      .filter((s) => s.entity_id.startsWith("automation."))
      .sort((a, b) => (a.attributes.friendly_name || a.entity_id).localeCompare(b.attributes.friendly_name || b.entity_id));
    host.innerHTML = "";
    if (!autos.length) {
      host.appendChild(this._el("div", { class: "cp-empty", text: "还没有自动化。点右上「新建自动化」让 Copilot 帮你创建。" }));
      return;
    }
    const list = this._el("div", { class: "cp-list" });
    for (const s of autos) {
      const on = s.state === "on";
      const row = this._el("div", { class: "cp-row" });
      const left = this._el("div", { class: "cp-row-main" });
      left.appendChild(this._el("div", { class: "cp-row-name", text: s.attributes.friendly_name || s.entity_id }));
      const lt = s.attributes.last_triggered;
      left.appendChild(this._el("div", { class: "cp-row-id", text: lt ? "上次触发: " + new Date(lt).toLocaleString() : "从未触发" }));
      row.appendChild(left);
      const right = this._el("div", { class: "cp-row-ctrl" });
      right.appendChild(this._el("button", {
        class: "cp-chip", text: "触发",
        onclick: () => this._hass.callService("automation", "trigger", { entity_id: s.entity_id }),
      }));
      const sw = this._el("button", {
        class: "cp-switch" + (on ? " on" : ""),
        onclick: () => this._hass.callService("automation", on ? "turn_off" : "turn_on", { entity_id: s.entity_id }),
      });
      sw.appendChild(this._el("span", { class: "cp-knob" }));
      right.appendChild(sw);
      row.appendChild(right);
      list.appendChild(row);
    }
    host.appendChild(list);
  }

  // ---- EDITOR ------------------------------------------------------------
  async _viewEditor(host, actions) {
    host.innerHTML = "";
    const wrap = this._el("div", { class: "cp-editor" });
    const tree = this._el("div", { class: "cp-tree", id: "cp-tree" });
    const pane = this._el("div", { class: "cp-edpane" });
    pane.appendChild(this._el("div", { class: "cp-edbar" }, [
      this._el("span", { class: "cp-edpath", id: "cp-edpath", text: this._editorPath }),
      this._el("span", { class: "cp-edmsg", id: "cp-edmsg" }),
    ]));
    const ta = this._el("textarea", {
      class: "cp-code", id: "cp-code", spellcheck: "false",
      oninput: () => { this._editorDirty = true; this.querySelector("#cp-edmsg").textContent = "● 未保存"; },
    });
    pane.appendChild(ta);
    pane.appendChild(this._el("div", { class: "cp-edftr" }, [
      this._actionBtn("保存", () => this._saveEditor()),
      this._actionBtn("检查配置", () => this._editorCheck()),
      this._actionBtn("重载", () => this._editorReload()),
    ]));
    wrap.appendChild(tree);
    wrap.appendChild(pane);
    host.appendChild(wrap);
    await this._loadTree("");
    if (this._editorPath) this._openFile(this._editorPath);
  }

  async _loadTree(path) {
    const tree = this.querySelector("#cp-tree");
    if (!tree) return;
    try {
      const r = await this._runTool("list_dir", { path });
      const container = path ? tree.querySelector(`[data-dir="${path}"]`) : tree;
      if (path && container) container.innerHTML = "";
      else tree.innerHTML = "";
      for (const e of r.entries || []) {
        if (e.type === "dir") {
          const d = this._el("div", { class: "cp-tnode dir", text: "▸ " + e.name, title: e.path });
          const kids = this._el("div", { class: "cp-tkids", "data-dir": e.path });
          let open = false;
          d.addEventListener("click", async (ev) => {
            ev.stopPropagation();
            open = !open;
            d.textContent = (open ? "▾ " : "▸ ") + e.name;
            kids.style.display = open ? "block" : "none";
            if (open && !kids.dataset.loaded) {
              kids.dataset.loaded = "1";
              await this._loadTree(e.path);
            }
          });
          (path ? container : tree).appendChild(d);
          (path ? container : tree).appendChild(kids);
        } else {
          const f = this._el("div", { class: "cp-tnode file", text: e.name, title: e.path, "data-file": e.path });
          f.addEventListener("click", (ev) => { ev.stopPropagation(); this._openFile(e.path); });
          (path ? container : tree).appendChild(f);
        }
      }
    } catch (e) {
      if (!path) tree.innerHTML = `<div class="cp-empty">无法列目录: ${this._esc(e.message || e)}</div>`;
    }
  }

  async _openFile(path) {
    const ta = this.querySelector("#cp-code");
    const msg = this.querySelector("#cp-edmsg");
    if (!ta) return;
    msg.textContent = "加载中…";
    try {
      const r = await this._runTool("read_config_file", { path });
      if (r.error) { msg.textContent = r.error; return; }
      ta.value = r.content || "";
      this._editorPath = path;
      this._editorDirty = false;
      localStorage.setItem(LS.editorFile, path);
      this.querySelector("#cp-edpath").textContent = path;
      msg.textContent = "";
      this.querySelectorAll(".cp-tnode.file").forEach((n) =>
        n.classList.toggle("active", n.getAttribute("data-file") === path)
      );
    } catch (e) {
      msg.textContent = "打开失败: " + (e.message || e);
    }
  }

  async _saveEditor() {
    const ta = this.querySelector("#cp-code");
    const msg = this.querySelector("#cp-edmsg");
    if (!ta) return;
    msg.textContent = "保存中…";
    try {
      const r = await this._runTool("write_config_file", { path: this._editorPath, content: ta.value });
      if (r.error) { msg.textContent = "✗ " + r.error; return; }
      this._editorDirty = false;
      // Auto-validate after each save (VS-Code-style problem feedback).
      const chk = await this._runTool("check_config");
      msg.textContent = chk.valid ? `✓ 已保存 (${r.bytes}B) · 配置有效` : "✓ 已保存，但配置有错误，见日志";
    } catch (e) {
      msg.textContent = "保存失败: " + (e.message || e);
    }
  }

  async _editorCheck() {
    const msg = this.querySelector("#cp-edmsg");
    msg.textContent = "检查中…";
    const r = await this._runTool("check_config");
    msg.textContent = r.valid ? "✓ 配置有效" : "✗ " + (r.errors || []).join("; ").slice(0, 200);
  }

  async _editorReload() {
    const msg = this.querySelector("#cp-edmsg");
    const domain = (this._editorPath || "").startsWith("automations") ? "automation"
      : (this._editorPath || "").startsWith("scripts") ? "script"
      : (this._editorPath || "").startsWith("scenes") ? "scene" : "core";
    msg.textContent = "重载 " + domain + "…";
    const r = await this._runTool("reload", { domain });
    msg.textContent = r.error ? "✗ " + r.error : "✓ 已重载 " + (r.reloaded || domain);
  }

  // ---- LOGS --------------------------------------------------------------
  async _viewLogs(host, actions) {
    actions.appendChild(this._actionBtn("刷新", () => this._loadLogs()));
    host.innerHTML = "";
    const pre = this._el("pre", { class: "cp-logout", id: "cp-logout", text: "加载日志…" });
    host.appendChild(pre);
    this._loadLogs();
  }

  async _loadLogs() {
    const pre = this.querySelector("#cp-logout");
    if (!pre) return;
    try {
      const r = await this._runTool("read_logs", { lines: 200 });
      pre.textContent = r.log_tail || r.error || "(空)";
      pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      pre.textContent = "读取失败: " + (e.message || e);
    }
  }

  // ---- INTEGRATIONS ------------------------------------------------------
  async _viewIntegrations(host, actions) {
    actions.appendChild(this._actionBtn("添加集成", () => window.open("/config/integrations/dashboard", "_blank")));
    host.innerHTML = "";
    host.appendChild(this._el("div", { class: "cp-note", text: "已配置的集成（条目）。添加/配置账号请用「添加集成」进入 HA 原生配置流程。" }));
    const list = this._el("div", { class: "cp-list", id: "cp-intlist" });
    host.appendChild(list);
    list.appendChild(this._el("div", { class: "cp-empty", text: "加载中…" }));
    try {
      const entries = await this._ws({ type: "config_entries/get" });
      list.innerHTML = "";
      if (!entries || !entries.length) {
        list.appendChild(this._el("div", { class: "cp-empty", text: "暂无集成条目" }));
        return;
      }
      for (const en of entries.sort((a, b) => a.domain.localeCompare(b.domain))) {
        const row = this._el("div", { class: "cp-row" });
        const left = this._el("div", { class: "cp-row-main" });
        left.appendChild(this._el("div", { class: "cp-row-name", text: en.title || en.domain }));
        left.appendChild(this._el("div", { class: "cp-row-id", text: en.domain }));
        row.appendChild(left);
        const st = (en.state || "").toLowerCase();
        row.appendChild(this._el("span", {
          class: "cp-pill " + (st === "loaded" ? "ok" : st ? "warn" : ""),
          text: en.state || "—",
        }));
        list.appendChild(row);
      }
    } catch (e) {
      list.innerHTML = "";
      list.appendChild(this._el("div", { class: "cp-empty", text: "无法读取集成: " + (e.message || e) }));
    }
  }

  // ---- shared widgets ----------------------------------------------------
  _actionBtn(label, onclick) {
    return this._el("button", { class: "cp-chip", text: label, onclick });
  }

  _statCard(value, label) {
    return this._el("div", { class: "cp-card" }, [
      this._el("div", { class: "cp-card-v", text: value }),
      this._el("div", { class: "cp-card-l", text: label }),
    ]);
  }

  // ---- Copilot chat ------------------------------------------------------
  _greet() {
    this._appendMsg(
      "assistant",
      "你好，我是 HA-Copilot。我已深度融合进这台 Home Assistant：可读写配置、调用服务、建自动化/场景/脚本、管理实体与区域、校验配置并自我验证。左侧是工作区（设备/自动化/配置/日志/集成），我做的任何改动都会同步刷新到对应视图。直接告诉我你想做什么。"
    );
  }

  _renderChatHistory() {
    const log = this.querySelector("#cp-log");
    log.innerHTML = "";
    if (!this._history.length) {
      this._greet();
      return;
    }
    for (const m of this._history) {
      if (m.role === "user") this._appendMsg("user", m.content);
      else if (m.role === "assistant") this._appendMsg("assistant", m.content);
    }
  }

  _appendMsg(cls, text) {
    const log = this.querySelector("#cp-log");
    const div = this._el("div", { class: "cp-msg " + cls, text });
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  _appendSteps(steps) {
    if (!steps || !steps.length) return;
    const log = this.querySelector("#cp-log");
    const div = this._el("div", { class: "cp-steps" });
    div.innerHTML =
      `<b>操作轨迹 (${steps.length} 步)</b><br>` +
      steps
        .map((s, i) => {
          const r = JSON.stringify(s.result);
          const short = r.length > 220 ? r.slice(0, 220) + "…" : r;
          return `${i + 1}. <b>${this._esc(s.tool)}</b>(${this._esc(JSON.stringify(s.args))}) → ${this._esc(short)}`;
        })
        .join("<br>");
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  _prefillChat(text) {
    this._chatOpen = true;
    localStorage.setItem(LS.chatOpen, "1");
    this._applyChatOpen();
    const box = this.querySelector("#cp-box");
    box.value = text;
    box.focus();
  }

  async _send() {
    if (this._busy) return;
    const box = this.querySelector("#cp-box");
    const text = box.value.trim();
    if (!text) return;
    box.value = "";
    this._appendMsg("user", text);
    this._busy = true;
    const sendBtn = this.querySelector("#cp-send");
    sendBtn.disabled = true;
    const typing = this._appendMsg("assistant typing", "思考中…");

    try {
      const res = await this._api("POST", "ha_copilot/chat", {
        message: text,
        history: this._history,
      });
      typing.remove();
      this._appendSteps(res.steps);
      const reply = res.reply || "(无回复)";
      this._appendMsg("assistant", reply);
      this._history.push({ role: "user", content: text });
      this._history.push({ role: "assistant", content: reply });
      if (this._history.length > 16) this._history = this._history.slice(-16);
      this._saveHistory();
      // The Copilot may have mutated HA — refresh the active workspace view.
      if (res.steps && res.steps.length) this._renderView();
    } catch (e) {
      typing.remove();
      this._appendMsg("assistant", "出错了: " + (e && e.message ? e.message : e));
    } finally {
      this._busy = false;
      sendBtn.disabled = false;
      box.focus();
    }
  }

  _esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // ---- styles ------------------------------------------------------------
  _css() {
    return `
    .cp-root{display:grid;grid-template-columns:56px 1fr 360px;grid-template-rows:1fr 26px;grid-template-areas:"act main chat" "act status status";height:100%;background:var(--primary-background-color,#1e1e1e);color:var(--primary-text-color,#e0e0e0);font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif);overflow:hidden;}
    .cp-root.chat-closed{grid-template-columns:56px 1fr 0;}
    .cp-root.chat-closed .cp-chat{display:none;}
    .cp-activity{grid-area:act;background:var(--app-header-background-color,#252526);display:flex;flex-direction:column;align-items:center;padding-top:8px;gap:4px;border-right:1px solid var(--divider-color,#333);}
    .cp-act{width:44px;height:44px;border:none;background:transparent;color:var(--secondary-text-color,#9e9e9e);border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;border-left:2px solid transparent;}
    .cp-act:hover{color:var(--primary-text-color,#fff);background:rgba(255,255,255,.06);}
    .cp-act.active{color:var(--primary-color,#03a9f4);border-left-color:var(--primary-color,#03a9f4);}
    .cp-main{grid-area:main;display:flex;flex-direction:column;min-width:0;overflow:hidden;}
    .cp-viewbar{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--divider-color,#333);background:var(--card-background-color,#252526);}
    .cp-viewtitle{font-size:15px;font-weight:600;}
    .cp-viewactions{display:flex;gap:6px;margin-left:6px;}
    #cp-toggle-chat{margin-left:auto;}
    .cp-view{flex:1;overflow-y:auto;padding:16px;}
    .cp-chip{border:1px solid var(--divider-color,#3c3c3c);background:var(--secondary-background-color,#2d2d2d);color:inherit;border-radius:7px;padding:5px 12px;font-size:13px;cursor:pointer;}
    .cp-chip:hover{border-color:var(--primary-color,#03a9f4);color:var(--primary-color,#03a9f4);}
    .cp-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-bottom:18px;}
    .cp-card{background:var(--card-background-color,#252526);border:1px solid var(--divider-color,#333);border-radius:12px;padding:16px;}
    .cp-card-v{font-size:30px;font-weight:700;color:var(--primary-color,#03a9f4);}
    .cp-card-l{font-size:12px;color:var(--secondary-text-color,#9e9e9e);margin-top:4px;}
    .cp-section-h{font-size:13px;font-weight:600;color:var(--secondary-text-color,#9e9e9e);margin:16px 0 8px;text-transform:uppercase;letter-spacing:.5px;}
    .cp-domgrid{display:flex;flex-wrap:wrap;gap:8px;}
    .cp-dombadge{display:flex;align-items:center;gap:8px;background:var(--card-background-color,#252526);border:1px solid var(--divider-color,#333);border-radius:20px;padding:6px 14px;color:inherit;cursor:pointer;font-size:13px;}
    .cp-dombadge:hover{border-color:var(--primary-color,#03a9f4);}
    .cp-dombadge-n{font-weight:700;color:var(--primary-color,#03a9f4);}
    .cp-controls{display:flex;gap:8px;margin-bottom:12px;}
    .cp-input-text,.cp-select{background:var(--secondary-background-color,#2d2d2d);border:1px solid var(--divider-color,#3c3c3c);color:inherit;border-radius:8px;padding:8px 10px;font-size:13px;}
    .cp-input-text{flex:1;}
    .cp-list{display:flex;flex-direction:column;gap:6px;}
    .cp-row{display:flex;align-items:center;gap:10px;background:var(--card-background-color,#252526);border:1px solid var(--divider-color,#2c2c2c);border-radius:10px;padding:10px 14px;cursor:pointer;}
    .cp-row:hover{border-color:var(--primary-color,#03a9f4);}
    .cp-row-main{flex:1;min-width:0;}
    .cp-row-name{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
    .cp-row-id{font-size:11px;color:var(--secondary-text-color,#888);font-family:var(--code-font-family,monospace);}
    .cp-row-ctrl{display:flex;align-items:center;gap:10px;}
    .cp-state{font-size:13px;color:var(--secondary-text-color,#bbb);}
    .cp-switch{width:42px;height:24px;border-radius:14px;border:none;background:var(--switch-unchecked-track-color,#555);position:relative;cursor:pointer;transition:background .15s;flex:none;}
    .cp-switch.on{background:var(--primary-color,#03a9f4);}
    .cp-switch.pending{opacity:.5;}
    .cp-knob{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s;}
    .cp-switch.on .cp-knob{left:21px;}
    .cp-num{width:80px;background:var(--secondary-background-color,#2d2d2d);border:1px solid var(--divider-color,#3c3c3c);color:inherit;border-radius:6px;padding:5px;}
    .cp-iconbtn{border:none;background:transparent;color:var(--secondary-text-color,#9e9e9e);cursor:pointer;font-size:16px;border-radius:6px;padding:3px 7px;}
    .cp-iconbtn:hover{color:var(--primary-text-color,#fff);background:rgba(255,255,255,.08);}
    .cp-iconbtn.small{font-size:14px;}
    .cp-empty,.cp-note{color:var(--secondary-text-color,#888);font-size:13px;padding:14px 4px;}
    .cp-result{white-space:pre-wrap;font-family:var(--code-font-family,monospace);font-size:12px;margin:10px 0;padding:0;}
    .cp-result.ok{color:#4caf50;}.cp-result.err{color:#f44336;}
    .cp-pill{font-size:11px;padding:3px 9px;border-radius:10px;background:rgba(255,255,255,.08);}
    .cp-pill.ok{background:rgba(76,175,80,.2);color:#7bd88a;}
    .cp-pill.warn{background:rgba(255,152,0,.2);color:#ffb74d;}
    /* editor */
    .cp-editor{display:grid;grid-template-columns:200px 1fr;gap:12px;height:100%;}
    .cp-tree{overflow-y:auto;border:1px solid var(--divider-color,#333);border-radius:10px;padding:8px;background:var(--card-background-color,#252526);}
    .cp-tnode{font-size:13px;padding:4px 6px;border-radius:6px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
    .cp-tnode:hover{background:rgba(255,255,255,.06);}
    .cp-tnode.dir{font-weight:600;}
    .cp-tnode.file.active{background:var(--primary-color,#03a9f4);color:#fff;}
    .cp-tkids{display:none;margin-left:12px;}
    .cp-edpane{display:flex;flex-direction:column;min-width:0;}
    .cp-edbar{display:flex;justify-content:space-between;font-size:12px;color:var(--secondary-text-color,#9e9e9e);padding:4px 2px;}
    .cp-edpath{font-family:var(--code-font-family,monospace);}
    .cp-edmsg{color:var(--primary-color,#03a9f4);}
    .cp-code{flex:1;resize:none;background:#1e1e1e;color:#d4d4d4;border:1px solid var(--divider-color,#333);border-radius:8px;padding:12px;font-family:var(--code-font-family,monospace);font-size:13px;line-height:1.5;white-space:pre;overflow:auto;}
    .cp-edftr{display:flex;gap:8px;margin-top:8px;}
    .cp-logout{height:100%;overflow:auto;background:#1e1e1e;color:#d4d4d4;border:1px solid var(--divider-color,#333);border-radius:8px;padding:12px;font-family:var(--code-font-family,monospace);font-size:12px;white-space:pre-wrap;margin:0;}
    /* chat */
    .cp-chat{grid-area:chat;display:flex;flex-direction:column;border-left:1px solid var(--divider-color,#333);background:var(--card-background-color,#252526);min-width:0;}
    .cp-chat-head{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--divider-color,#333);}
    .cp-chat-status{margin-left:auto;font-size:11px;color:var(--secondary-text-color,#9e9e9e);}
    .cp-dot{width:9px;height:9px;border-radius:50%;background:#888;}
    .cp-dot.ok{background:#4caf50;}
    .cp-log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;}
    .cp-msg{max-width:90%;padding:9px 12px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word;line-height:1.45;font-size:13px;}
    .cp-msg.user{align-self:flex-end;background:var(--primary-color,#03a9f4);color:#fff;border-bottom-right-radius:4px;}
    .cp-msg.assistant{align-self:flex-start;background:var(--secondary-background-color,#2d2d2d);border-bottom-left-radius:4px;}
    .cp-msg.typing{font-style:italic;color:var(--secondary-text-color,#9e9e9e);}
    .cp-steps{align-self:flex-start;max-width:95%;font-size:11px;background:rgba(0,0,0,.25);border-left:3px solid var(--primary-color,#03a9f4);padding:8px 10px;border-radius:8px;font-family:var(--code-font-family,monospace);color:var(--secondary-text-color,#bbb);word-break:break-all;}
    .cp-input{display:flex;gap:8px;padding:10px;border-top:1px solid var(--divider-color,#333);}
    .cp-input textarea{flex:1;resize:none;border:1px solid var(--divider-color,#3c3c3c);border-radius:9px;padding:9px;font-size:13px;font-family:inherit;background:var(--secondary-background-color,#2d2d2d);color:inherit;min-height:40px;max-height:120px;}
    .cp-input button{border:none;background:var(--primary-color,#03a9f4);color:#fff;border-radius:9px;padding:0 16px;cursor:pointer;font-weight:500;}
    .cp-input button:disabled{opacity:.5;}
    .cp-status{grid-area:status;display:flex;align-items:center;gap:18px;padding:0 16px;background:var(--app-header-background-color,#007acc);color:#fff;font-size:11px;}
    .cp-status .dim{opacity:.7;}
    .cp-status .cp-write.ok{color:#c8f7c5;}.cp-status .cp-write.ro{color:#ffe0b2;}
    /* modal */
    .cp-modal-back{position:absolute;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:20;}
    .cp-modal{background:var(--card-background-color,#252526);border:1px solid var(--divider-color,#333);border-radius:12px;width:min(560px,90%);max-height:80%;display:flex;flex-direction:column;padding:16px;}
    .cp-modal-head{display:flex;justify-content:space-between;align-items:center;font-size:15px;}
    .cp-modal-sub{font-size:12px;color:var(--secondary-text-color,#9e9e9e);font-family:var(--code-font-family,monospace);margin:6px 0;}
    .cp-json{flex:1;overflow:auto;background:#1e1e1e;color:#d4d4d4;border-radius:8px;padding:12px;font-size:12px;}
    .cp-modal-ftr{display:flex;justify-content:flex-end;margin-top:10px;}
    `;
  }
}

customElements.define("ha-copilot-panel", HaCopilotPanel);
