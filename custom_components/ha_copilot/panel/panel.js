// HA-Copilot sidebar panel - a dependency-free custom element.
// Home Assistant injects `hass`, `narrow`, `route` and `panel` properties.

class HaCopilotPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._history = [];
    this._busy = false;
    this._rendered = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) {
      this._render();
      this._rendered = true;
      this._loadConfig();
    }
  }

  get hass() {
    return this._hass;
  }

  async _loadConfig() {
    try {
      const cfg = await this._hass.callApi("GET", "ha_copilot/config");
      const el = this.querySelector("#status");
      if (el) {
        el.textContent = `model: ${cfg.model}  ·  ${cfg.base_url}  ·  write:${cfg.allow_write}`;
      }
    } catch (e) {
      /* non-fatal */
    }
  }

  _render() {
    this.innerHTML = `
      <style>
        .wrap { display:flex; flex-direction:column; height:100%; background:var(--primary-background-color,#fafafa); color:var(--primary-text-color,#212121); font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
        .bar { padding:12px 16px; background:var(--app-header-background-color, var(--primary-color,#03a9f4)); color:var(--app-header-text-color,#fff); display:flex; align-items:center; gap:10px; }
        .bar h1 { font-size:18px; margin:0; font-weight:500; }
        .bar .sub { font-size:11px; opacity:.85; margin-left:auto; }
        .log { flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:12px; }
        .msg { max-width:80%; padding:10px 14px; border-radius:14px; white-space:pre-wrap; word-wrap:break-word; line-height:1.45; box-shadow:0 1px 2px rgba(0,0,0,.08); }
        .user { align-self:flex-end; background:var(--primary-color,#03a9f4); color:#fff; border-bottom-right-radius:4px; }
        .assistant { align-self:flex-start; background:var(--card-background-color,#fff); border-bottom-left-radius:4px; }
        .steps { align-self:flex-start; max-width:80%; font-size:12px; background:rgba(0,0,0,.04); border-left:3px solid var(--primary-color,#03a9f4); padding:8px 12px; border-radius:8px; font-family:var(--code-font-family, monospace); color:var(--secondary-text-color,#727272); }
        .steps b { color:var(--primary-text-color,#212121); }
        .input { display:flex; gap:8px; padding:12px 16px; border-top:1px solid var(--divider-color,#e0e0e0); background:var(--card-background-color,#fff); }
        .input textarea { flex:1; resize:none; border:1px solid var(--divider-color,#e0e0e0); border-radius:10px; padding:10px 12px; font-size:14px; font-family:inherit; background:var(--primary-background-color,#fafafa); color:inherit; min-height:42px; max-height:140px; }
        .input button { border:none; background:var(--primary-color,#03a9f4); color:#fff; border-radius:10px; padding:0 20px; font-size:14px; cursor:pointer; font-weight:500; }
        .input button:disabled { opacity:.5; cursor:default; }
        .hint { color:var(--secondary-text-color,#727272); font-size:12px; padding:0 16px 8px; }
        .typing { font-style:italic; color:var(--secondary-text-color,#727272); }
      </style>
      <div class="wrap">
        <div class="bar">
          <h1>HA-Copilot</h1>
          <span class="sub" id="status">connecting...</span>
        </div>
        <div class="log" id="log">
          <div class="msg assistant">你好！我是 HA-Copilot，已深度融合进这台 Home Assistant。我可以读写配置、调用服务、查实体、建自动化、校验配置并自我验证。直接告诉我你想做什么，例如：「创建一个自动化：每天晚上7点打开所有灯」。</div>
        </div>
        <div class="hint">提示：试试「列出所有灯并全部打开」「创建一个日落自动化」「检查配置是否有错误」</div>
        <div class="input">
          <textarea id="box" placeholder="给 HA-Copilot 下达指令…  (Enter 发送, Shift+Enter 换行)"></textarea>
          <button id="send">发送</button>
        </div>
      </div>`;

    const box = this.querySelector("#box");
    const send = this.querySelector("#send");
    send.addEventListener("click", () => this._send());
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this._send();
      }
    });
  }

  _append(cls, text) {
    const log = this.querySelector("#log");
    const div = document.createElement("div");
    div.className = "msg " + cls;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  _appendSteps(steps) {
    if (!steps || !steps.length) return;
    const log = this.querySelector("#log");
    const div = document.createElement("div");
    div.className = "steps";
    div.innerHTML =
      "<b>操作轨迹 (" + steps.length + " 步)</b><br>" +
      steps
        .map((s, i) => {
          const r = JSON.stringify(s.result);
          const short = r.length > 220 ? r.slice(0, 220) + "…" : r;
          return `${i + 1}. <b>${s.tool}</b>(${JSON.stringify(s.args)}) → ${short}`;
        })
        .join("<br>");
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  async _send() {
    if (this._busy) return;
    const box = this.querySelector("#box");
    const text = box.value.trim();
    if (!text) return;
    box.value = "";
    this._append("user", text);
    this._busy = true;
    const sendBtn = this.querySelector("#send");
    sendBtn.disabled = true;
    const typing = this._append("assistant typing", "思考中…");

    try {
      const res = await this._hass.callApi("POST", "ha_copilot/chat", {
        message: text,
        history: this._history,
      });
      typing.remove();
      this._appendSteps(res.steps);
      const reply = res.reply || "(no reply)";
      this._append("assistant", reply);
      this._history.push({ role: "user", content: text });
      this._history.push({ role: "assistant", content: reply });
      if (this._history.length > 16) this._history = this._history.slice(-16);
    } catch (e) {
      typing.remove();
      this._append("assistant", "出错了: " + (e && e.message ? e.message : e));
    } finally {
      this._busy = false;
      sendBtn.disabled = false;
    }
  }
}

customElements.define("ha-copilot-panel", HaCopilotPanel);
