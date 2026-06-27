// HA-Copilot Workspace — a dependency-free, model-free custom element that turns
// Home Assistant into a Cursor/VS-Code-style workspace.
//
// Home Assistant injects `hass`, `narrow`, `route` and `panel` properties.
// The left activity bar switches between deterministic views (Overview,
// Devices, Automations, Editor, Logs, Integrations) that read/write the live
// instance directly through `hass` + the `ha_copilot.run_tool` service. The
// right-docked Operator Console runs any single tool deterministically against
// the live instance — the SAME tool layer that is exposed to external agents
// over MCP. No inference endpoint is ever called from here.

const LS = {
  view: "ha_copilot_view",
  consoleOpen: "ha_copilot_console_open",
  oplog: "ha_copilot_oplog",
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
const DELETABLE_DOMAINS = [
  "scene", "script", "input_boolean", "input_number", "input_text",
  "input_select", "input_datetime", "timer", "counter",
];
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
    this._consoleOpen = localStorage.getItem(LS.consoleOpen) !== "0";
    this._oplog = this._loadOplog();
    this._tools = [];
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
  _loadOplog() {
    try {
      const raw = localStorage.getItem(LS.oplog);
      const h = raw ? JSON.parse(raw) : [];
      return Array.isArray(h) ? h.slice(-50) : [];
    } catch (e) {
      return [];
    }
  }

  _saveOplog() {
    try {
      localStorage.setItem(LS.oplog, JSON.stringify(this._oplog.slice(-50)));
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
    try {
      const t = await this._api("GET", "ha_copilot/tools");
      this._tools = (t && t.tools) || [];
      this._renderToolPicker();
    } catch (e) {
      this._tools = [];
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
            <button class="cp-chip" id="cp-toggle-chat" title="切换命令台">☰ 命令台</button>
          </div>
          <div class="cp-view" id="cp-view"></div>
        </section>
        <aside class="cp-chat" id="cp-chat">
          <div class="cp-chat-head">
            <span class="cp-dot" id="cp-dot"></span>
            <b>命令台</b>
            <span class="cp-chat-status" id="cp-chat-status">—</span>
            <button class="cp-iconbtn" id="cp-clear" title="清空记录">⟲</button>
          </div>
          <div class="cp-console-form">
            <select class="cp-select" id="cp-tool"></select>
            <div class="cp-tool-desc" id="cp-tool-desc"></div>
            <textarea id="cp-args" class="cp-args" placeholder='参数 JSON，例如 {"domain":"light"}'></textarea>
            <button id="cp-run" class="cp-run-btn">执行工具</button>
          </div>
          <div class="cp-log" id="cp-log"></div>
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

    // Console wiring
    this._renderToolPicker();
    this._renderOplog();
    this.querySelector("#cp-run").addEventListener("click", () => this._runConsole());
    this.querySelector("#cp-tool").addEventListener("change", () => this._onToolPick());
    this.querySelector("#cp-clear").addEventListener("click", () => {
      this._oplog = [];
      this._saveOplog();
      this.querySelector("#cp-log").innerHTML = "";
      this._renderOplog();
    });
    this.querySelector("#cp-toggle-chat").addEventListener("click", () => this._toggleConsole());

    this._applyConsoleOpen();
  }

  _toggleConsole() {
    this._consoleOpen = !this._consoleOpen;
    localStorage.setItem(LS.consoleOpen, this._consoleOpen ? "1" : "0");
    this._applyConsoleOpen();
  }

  _applyConsoleOpen() {
    this.querySelector(".cp-root").classList.toggle("chat-closed", !this._consoleOpen);
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
      (cfg.tool_count != null ? `<span>${cfg.tool_count} 工具</span>` : "") +
      (cfg.mcp_endpoint ? `<span class="dim">MCP ${this._esc(cfg.mcp_endpoint)}</span>` : "") +
      `<span class="cp-write ${cfg.allow_write ? "ok" : "ro"}">${write}</span>`;
    const cs = this.querySelector("#cp-chat-status");
    if (cs) cs.textContent = cfg.tool_count != null ? `${cfg.tool_count} 工具` : "ready";
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
    const domain = entity_id.split(".")[0];
    this._appendControls(modal, s, entity_id, domain, back);
    const ftr = this._el("div", { class: "cp-modal-ftr" });
    ftr.appendChild(this._actionBtn("复制 entity_id", () => {
      this._copy(entity_id);
    }));
    if (DELETABLE_DOMAINS.includes(domain)) {
      const delBtn = this._actionBtn("删除", async () => {
        const label = s.attributes.friendly_name || entity_id;
        if (!window.confirm(`删除「${label}」(${entity_id})？此操作将从配置中移除并清理残留。`)) return;
        delBtn.textContent = "删除中…"; delBtn.disabled = true;
        const r = await this._deleteEntity(entity_id, domain, label);
        if (r && r.error) { delBtn.textContent = "失败: " + r.error; delBtn.disabled = false; return; }
        back.remove();
        this._renderView();
      });
      delBtn.classList.add("cp-btn-danger");
      ftr.appendChild(delBtn);
    }
    modal.appendChild(ftr);
    back.appendChild(modal);
    this.querySelector(".cp-root").appendChild(back);
  }

  async _deleteEntity(entity_id, domain, label) {
    if (domain === "scene") return this._runTool("delete_scene", { identifier: label });
    if (domain === "script") return this._runTool("delete_script", { identifier: entity_id });
    return this._runTool("delete_helper", { entity_id });
  }

  // Domain-specific control affordances inside the entity-detail modal.
  // These call HA services directly and read state back so the user can
  // operate devices end-to-end without leaving the panel.
  _appendControls(modal, s, entity_id, domain, back) {
    const ctl = this._el("div", { class: "cp-modal-ctl" });
    const refresh = () => {
      const ns = this._hass.states[entity_id];
      if (ns) {
        const sub = modal.querySelector(".cp-modal-sub");
        if (sub) sub.textContent = entity_id + "  ·  " + this._fmtState(ns);
      }
    };
    const callAndShow = async (btn, dom, service, data, busyText) => {
      const orig = btn.textContent;
      btn.textContent = busyText || "执行中…"; btn.disabled = true;
      try {
        await this._hass.callService(dom, service, { entity_id, ...(data || {}) });
        setTimeout(refresh, 400);
        btn.textContent = "已执行"; setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 900);
      } catch (e) {
        btn.textContent = "失败"; setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1200);
      }
    };

    if (domain === "scene") {
      const b = this._actionBtn("激活场景", () => callAndShow(b, "scene", "turn_on", null, "激活中…"));
      b.classList.add("cp-chip-accent"); ctl.appendChild(b);
    } else if (domain === "script") {
      const b = this._actionBtn("运行脚本", () => callAndShow(b, "script", "turn_on", null, "运行中…"));
      b.classList.add("cp-chip-accent"); ctl.appendChild(b);
    } else if (TOGGLE_DOMAINS.includes(domain)) {
      const on = s.state === "on";
      const b = this._actionBtn(on ? "关闭" : "打开",
        () => callAndShow(b, domain, s.state === "on" ? "turn_off" : "turn_on"));
      b.classList.add("cp-chip-accent"); ctl.appendChild(b);
      // Brightness for lights that support it.
      if (domain === "light" && (s.attributes.supported_color_modes || []).some(
        (m) => ["brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww"].includes(m))) {
        const pct = s.attributes.brightness != null ? Math.round((s.attributes.brightness / 255) * 100) : 100;
        const wrap = this._el("label", { class: "cp-slider-wrap", text: "亮度 " });
        const val = this._el("span", { class: "cp-slider-val", text: pct + "%" });
        const sl = this._el("input", {
          class: "cp-slider", type: "range", min: 1, max: 100, value: pct,
          oninput: (e) => { val.textContent = e.target.value + "%"; },
          onchange: (e) => {
            this._hass.callService("light", "turn_on", { entity_id, brightness_pct: Number(e.target.value) });
            setTimeout(refresh, 400);
          },
        });
        wrap.appendChild(sl); wrap.appendChild(val); ctl.appendChild(wrap);
      }
    }
    if (ctl.children.length) modal.appendChild(ctl);
  }

  // ---- AUTOMATIONS -------------------------------------------------------
  _viewAutomations(host, actions) {
    actions.appendChild(this._actionBtn("编辑 automations.yaml", () => {
      this._editorPath = "automations.yaml";
      localStorage.setItem(LS.editorFile, this._editorPath);
      this._switchView("editor");
    }));
    const autos = Object.values(this._hass.states || {})
      .filter((s) => s.entity_id.startsWith("automation."))
      .sort((a, b) => (a.attributes.friendly_name || a.entity_id).localeCompare(b.attributes.friendly_name || b.entity_id));
    host.innerHTML = "";
    if (!autos.length) {
      host.appendChild(this._el("div", { class: "cp-empty", text: "还没有自动化。点右上「编辑 automations.yaml」直接编写，或在命令台调用 create_automation 工具。" }));
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
      const del = this._el("button", {
        class: "cp-chip cp-chip-danger", text: "删除",
        onclick: async () => {
          const label = s.attributes.friendly_name || s.entity_id;
          if (!window.confirm(`删除自动化「${label}」？将从 automations.yaml 移除（保留备份）。`)) return;
          del.textContent = "删除中…"; del.disabled = true;
          try {
            const r = await this._runTool("delete_automation", { identifier: s.attributes.id || label });
            if (r && r.error) { del.textContent = "失败"; del.disabled = false; return; }
          } catch (e) { del.textContent = "失败"; del.disabled = false; return; }
          setTimeout(() => this._renderView(), 600);
        },
      });
      right.appendChild(del);
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
      // Drive through the same capability layer that MCP exposes, so the panel
      // and external operators see identical integration state.
      const r = await this._runTool("list_config_entries");
      const entries = (r && r.entries) || [];
      list.innerHTML = "";
      if (!entries.length) {
        list.appendChild(this._el("div", { class: "cp-empty", text: "暂无集成条目" }));
        return;
      }
      for (const en of entries) {
        const row = this._el("div", { class: "cp-row" });
        const left = this._el("div", { class: "cp-row-main" });
        left.appendChild(this._el("div", { class: "cp-row-name", text: en.title || en.domain }));
        left.appendChild(this._el("div", { class: "cp-row-id", text: en.domain }));
        row.appendChild(left);
        const right = this._el("div", { class: "cp-row-ctrl" });
        const st = (en.state || "").toLowerCase();
        right.appendChild(this._el("span", {
          class: "cp-pill " + (st === "loaded" ? "ok" : st ? "warn" : ""),
          text: en.state || "—",
        }));
        if (en.entry_id) {
          right.appendChild(this._el("button", {
            class: "cp-chip", text: "重载", title: "重载该集成（不重启 HA）",
            onclick: async (e) => {
              const btn = e.currentTarget;
              btn.disabled = true;
              btn.textContent = "重载中…";
              try {
                const rr = await this._runTool("reload_config_entry", { entry_id: en.entry_id });
                btn.textContent = rr && rr.ok ? "已重载" : "失败";
              } catch (err) {
                btn.textContent = "失败";
              }
              setTimeout(() => this._renderView(), 800);
            },
          }));
        }
        row.appendChild(right);
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

  // ---- operator console (deterministic, no model) ------------------------
  _renderToolPicker() {
    const sel = this.querySelector("#cp-tool");
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = "";
    if (!this._tools.length) {
      sel.appendChild(this._el("option", { value: "", text: "加载工具中…" }));
      return;
    }
    for (const t of this._tools) {
      sel.appendChild(this._el("option", { value: t.name, text: t.name }));
    }
    if (prev) sel.value = prev;
    this._onToolPick();
  }

  _onToolPick() {
    const sel = this.querySelector("#cp-tool");
    const desc = this.querySelector("#cp-tool-desc");
    const argsBox = this.querySelector("#cp-args");
    if (!sel || !desc) return;
    const t = this._tools.find((x) => x.name === sel.value);
    if (!t) {
      desc.textContent = "";
      return;
    }
    const schema = t.inputSchema || {};
    const props = schema.properties || {};
    const required = schema.required || [];
    // Description + which params are required vs optional (human-readable hint).
    const names = Object.keys(props);
    let hint = t.description || "";
    if (names.length) {
      const parts = names.map((n) => (required.includes(n) ? `${n}*` : n));
      hint += `  ·  参数: ${parts.join(", ")}（*必填）`;
    }
    desc.textContent = hint;
    // Auto-prefill an arg skeleton (only when switching tools, so we never clobber
    // an edit in progress). Fill all params so the operator just edits values.
    if (argsBox && this._lastPickedTool !== t.name) {
      this._lastPickedTool = t.name;
      argsBox.value = names.length
        ? JSON.stringify(this._schemaSkeleton(props), null, 2)
        : "";
    }
  }

  _schemaSkeleton(props) {
    const out = {};
    for (const [key, spec] of Object.entries(props)) {
      const type = (spec && spec.type) || "string";
      if (type === "boolean") out[key] = false;
      else if (type === "integer" || type === "number") out[key] = 0;
      else if (type === "array") out[key] = [];
      else if (type === "object") out[key] = {};
      else out[key] = "";
    }
    return out;
  }

  _renderOplog() {
    const log = this.querySelector("#cp-log");
    if (!log) return;
    log.innerHTML = "";
    if (!this._oplog.length) {
      log.appendChild(this._el("div", { class: "cp-empty", text: "命令台：选择一个工具并执行，直接操作 Home Assistant 本源（无模型）。同一工具层也经 MCP 暴露给外部 agent。" }));
      return;
    }
    for (const e of this._oplog) this._appendOp(e);
  }

  _appendOp(e) {
    const log = this.querySelector("#cp-log");
    if (!log) return;
    const div = this._el("div", { class: "cp-op" });
    const r = JSON.stringify(e.result);
    div.innerHTML =
      `<div class="cp-op-call"><b>${this._esc(e.tool)}</b>(${this._esc(JSON.stringify(e.args))})</div>` +
      `<div class="cp-op-res ${e.ok ? "ok" : "err"}">${this._esc(r)}</div>`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  async _runConsole() {
    if (this._busy) return;
    const sel = this.querySelector("#cp-tool");
    const argsBox = this.querySelector("#cp-args");
    const tool = sel.value;
    if (!tool) return;
    let args = {};
    const raw = (argsBox.value || "").trim();
    if (raw) {
      try {
        args = JSON.parse(raw);
      } catch (e) {
        this._appendOp({ tool, args: raw, result: { error: "参数不是合法 JSON" }, ok: false });
        return;
      }
    }
    this._busy = true;
    const runBtn = this.querySelector("#cp-run");
    runBtn.disabled = true;
    try {
      const result = await this._runTool(tool, args);
      const ok = !(result && typeof result === "object" && "error" in result);
      const entry = { tool, args, result, ok, ts: Date.now() };
      this._oplog.push(entry);
      if (this._oplog.length > 50) this._oplog = this._oplog.slice(-50);
      this._saveOplog();
      this._appendOp(entry);
      // The tool may have mutated HA — refresh the active workspace view.
      this._renderView();
    } catch (e) {
      this._appendOp({ tool, args, result: { error: String(e && e.message ? e.message : e) }, ok: false });
    } finally {
      this._busy = false;
      runBtn.disabled = false;
    }
  }

  _copy(text) {
    try {
      navigator.clipboard.writeText(text);
    } catch (e) {
      /* ignore */
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
    .cp-chip-danger:hover{border-color:var(--error-color,#db4437);color:var(--error-color,#db4437);}
    .cp-chip[disabled]{opacity:.5;cursor:default;}
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
    .cp-console-form{display:flex;flex-direction:column;gap:8px;padding:12px;border-bottom:1px solid var(--divider-color,#333);}
    .cp-tool-desc{font-size:11px;color:var(--secondary-text-color,#9e9e9e);min-height:14px;line-height:1.4;}
    .cp-args{resize:vertical;min-height:60px;border:1px solid var(--divider-color,#3c3c3c);border-radius:9px;padding:9px;font-size:12px;font-family:var(--code-font-family,monospace);background:var(--secondary-background-color,#2d2d2d);color:inherit;}
    .cp-run-btn{border:none;background:var(--primary-color,#03a9f4);color:#fff;border-radius:9px;padding:9px 16px;cursor:pointer;font-weight:500;}
    .cp-run-btn:disabled{opacity:.5;}
    .cp-op{font-size:11px;background:rgba(0,0,0,.25);border-left:3px solid var(--primary-color,#03a9f4);padding:8px 10px;border-radius:8px;font-family:var(--code-font-family,monospace);word-break:break-all;}
    .cp-op-call{color:var(--primary-text-color,#ddd);margin-bottom:4px;}
    .cp-op-res.ok{color:#7bd88a;}
    .cp-op-res.err{color:#f48fb1;}
    .cp-status{grid-area:status;display:flex;align-items:center;gap:18px;padding:0 16px;background:var(--app-header-background-color,#007acc);color:#fff;font-size:11px;}
    .cp-status .dim{opacity:.7;}
    .cp-status .cp-write.ok{color:#c8f7c5;}.cp-status .cp-write.ro{color:#ffe0b2;}
    /* modal */
    .cp-modal-back{position:absolute;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:20;}
    .cp-modal{background:var(--card-background-color,#252526);border:1px solid var(--divider-color,#333);border-radius:12px;width:min(560px,90%);max-height:80%;display:flex;flex-direction:column;padding:16px;}
    .cp-modal-head{display:flex;justify-content:space-between;align-items:center;font-size:15px;}
    .cp-modal-sub{font-size:12px;color:var(--secondary-text-color,#9e9e9e);font-family:var(--code-font-family,monospace);margin:6px 0;}
    .cp-json{flex:1;overflow:auto;background:#1e1e1e;color:#d4d4d4;border-radius:8px;padding:12px;font-size:12px;}
    .cp-modal-ftr{display:flex;justify-content:flex-end;gap:8px;margin-top:10px;}
    .cp-btn-danger{border-color:var(--error-color,#db4437)!important;color:var(--error-color,#db4437)!important;}
    .cp-btn-danger:hover{background:var(--error-color,#db4437)!important;color:#fff!important;}
    .cp-modal-ctl{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:10px;padding-top:10px;border-top:1px solid var(--divider-color,#3c3c3c);}
    .cp-chip-accent{border-color:var(--primary-color,#03a9f4)!important;color:var(--primary-color,#03a9f4)!important;}
    .cp-chip-accent:hover{background:var(--primary-color,#03a9f4)!important;color:#fff!important;}
    .cp-slider-wrap{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--secondary-text-color,#9b9b9b);}
    .cp-slider{vertical-align:middle;}
    .cp-slider-val{min-width:38px;text-align:right;color:inherit;}
    `;
  }
}

customElements.define("ha-copilot-panel", HaCopilotPanel);
