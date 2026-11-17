/*
 * 0-Smart 智能家居 — 主前端逻辑 v2（管理员/用户双角色）
 * ===================================================
 * 公共模块：认证、传感器、历史弹窗、AI 对话（含工具调用可视化）
 * 管理员模块：Tab 导航、设备状态/配置、灯带、红外、固件、自动化规则
 */
(function () {
  "use strict";

  var IS_ADMIN = window.IS_ADMIN === true;
  // 版本标记：刷新后控制台会打印，便于确认浏览器加载的是哪一版脚本（排查缓存问题）
  var MI_JS_VERSION = "5.6-2026-10-11-1835";
  if (window.console && console.log) {
    console.log("[main.js] 版本 " + MI_JS_VERSION + " IS_ADMIN=" + IS_ADMIN
      + " miToggle=" + (typeof window.miToggle));
  }
  var isLoading = false;

  // ==================== 通用工具 ====================
  function $(id) { return document.getElementById(id); }

  async function api(path, opts) {
    var resp = await fetch(path, opts);
    if (resp.status === 403) { alert("需要管理员权限"); throw new Error("403"); }
    if (resp.status === 401) { location.href = "/register.html"; throw new Error("401"); }
    return resp;
  }

  // ==================== 认证 / 顶栏 ====================
  function initAuth() {
    $("usernameDisplay").textContent = "当前用户";
    fetch("/api/check_login")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.logged_in) $("usernameDisplay").textContent = d.username;
        else location.href = "/register.html";
      });
    $("logoutBtn").addEventListener("click", function () {
      fetch("/api/logout", { method: "POST" }).then(function () {
        location.href = "/register.html";
      });
    });
  }

  // ==================== 传感器（用户+管理员共用） ====================
  var currentLocation = "卧室";
  var historyLocation = "卧室";
  var historyMetric = "lux";
  var historyHours = 1;

  function loadSensors() {
    fetch("/api/esp32/sensors/latest?location=" + encodeURIComponent(currentLocation))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        renderLocationTabs(d.locations || []);
        var avg = {
          lux: d.avg_lux, temperature: d.avg_temperature, humidity: d.avg_humidity
        };
        setSensor("sensorLux", "luxBar", avg.lux, 0, 200, " lux");
        setSensor("sensorTemp", "tempBar", avg.temperature, 0, 40, "°C");
        setSensor("sensorHumidity", "humBar", avg.humidity, 0, 100, "%");
        $("sensorUpdateTime").textContent = d.last_update ? ("更新于 " + d.last_update) : "";
        var st = $("sensorStatus");
        if (d.online_count > 0) { st.textContent = "🟢 " + d.online_count + " 台设备在线"; st.className = "sensor-status online"; }
        else { st.textContent = "🔴 无在线设备"; st.className = "sensor-status offline"; }
        $("sensorAlert").textContent = (d.alerts && d.alerts.length) ? d.alerts.join("；") : "";
        if (IS_ADMIN) renderDevices(d.devices);
      })
      .catch(function () {});
  }

  function setSensor(valueId, barId, v, min, max, unit) {
    var el = $(valueId), bar = $(barId);
    if (v == null || v < 0) { el.textContent = "--"; bar.style.width = "0%"; return; }
    el.textContent = (Math.round(v * 10) / 10) + unit;
    var pct = Math.max(0, Math.min(100, ((v - min) / (max - min)) * 100));
    bar.style.width = pct + "%";
  }

  function renderLocationTabs(locations) {
    var box = $("locationTabs");
    if (!box) return;
    box.innerHTML = "";
    (locations || ["卧室"]).forEach(function (loc) {
      var b = document.createElement("button");
      b.className = "loc-tab" + (loc === currentLocation ? " active" : "");
      b.textContent = loc;
      b.onclick = function () {
        currentLocation = loc;
        historyLocation = loc;
        historyDevice = "";
        loadSensors();
      };
      box.appendChild(b);
    });
  }

  // ==================== 设备状态（管理员） ====================
  function renderDevices(devices) {
    var box = $("deviceStatusList");
    if (!box) return;
    var ids = Object.keys(devices || {});
    if (!ids.length) { box.innerHTML = '<div style="color:#999;padding:8px">暂无设备</div>'; return; }
    box.innerHTML = "";
    ids.forEach(function (id) {
      var d = devices[id];
      var item = document.createElement("div");
      item.className = "device-item";
      var name = d.alias || id;

      // 5.8 毫米波人体感知（person<0 表示该节点未装毫米波/未收到有效帧）
      // 距离单位说明：LD2410 协议原生单位是 **厘米**，此处直接按 cm 显示
      // （不转米：协议分辨率就是 cm，转米会产生"0.7m"这种虚假精度）
      var mmTxt = "";
      if (d.person != null && d.person >= 0) {
        var stateMap = { 0: "无人", 1: "有人·运动", 2: "有人·静止", 3: "有人·动静" };
        var label = stateMap[d.mm_state] || (d.person ? "有人" : "无人");
        var dist = (d.mm_detect_dist >= 0) ? d.mm_detect_dist
                 : (d.mm_still_dist >= 0 ? d.mm_still_dist
                 : (d.mm_move_dist >= 0 ? d.mm_move_dist : -1));
        mmTxt = ' · <span style="color:' + (d.person ? "#2ecc71" : "#95a5a6") +
                ';font-weight:600">' + label + "</span>";
        // 5.8.2 LD2450：显示平面坐标（2D）——x 右为正、y 前方为正，单位 m
        if (d.mm_proto === 2 && d.mm_x != null && d.mm_x > -99999) {
          var xm = (d.mm_x / 1000).toFixed(1);
          var ym = (d.mm_y / 1000).toFixed(1);
          mmTxt += ' <span style="color:#3498db">位置(' + xm + "m," + ym + "m)</span>";
          if (d.mm_speed != null && d.mm_speed > -99999 && d.mm_speed !== 0) {
            mmTxt += '<span style="color:#8a97a8;font-size:11px"> ' +
                     (d.mm_speed > 0 ? "靠近" : "远离") + Math.abs(d.mm_speed) + "cm/s</span>";
          }
          if (dist >= 0) mmTxt += '<span style="color:#8a97a8;font-size:11px"> ' + dist + "cm</span>";
        } else if (dist >= 0) {
          mmTxt += " " + dist + "cm";
          // 低于官方保证下限（0.75m）时标注，避免误读为"精度内结果"
          if (dist < 75) mmTxt += '<span style="color:#e67e22;font-size:11px">(近场)</span>';
          if (d.mm_still_energy >= 0) {
            mmTxt += '<span style="color:#8a97a8;font-size:11px"> 能量' +
                     (d.mm_move_energy || 0) + "/" + d.mm_still_energy + "</span>";
          }
        }
      }

      item.innerHTML =
        '<div><span class="dot ' + (d.online ? "online" : "offline") + '"></span>' +
        '<span class="device-name">' + escapeHtml(name) + "</span>" +
        '<div class="device-sub">' + escapeHtml(d.location || "") +
        " · 信号 " + (d.rssi != null ? d.rssi : "--") + " dBm" + mmTxt +
        (d.last_update ? " · " + d.last_update : "") + "</div></div>" +
        '<span style="font-size:12px;color:#8a97a8">' + (d.online ? "在线" : "离线") + " ›</span>";
      item.onclick = function () { openDeviceConfig(id); };
      box.appendChild(item);
    });
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ==================== 设备配置弹窗（管理员） ====================
  var configDeviceId = "";

  function openDeviceConfig(id) {
    configDeviceId = id;
    $("deviceConfigTitle").textContent = "设备配置 — " + (id.slice(0, 8) + "…");
    fetch("/api/esp32/config?device_id=" + encodeURIComponent(id))
      .then(function (r) { return r.json(); })
      .then(function (c) {
        $("configAlias").value = c.alias || "";
        $("configInterval").value = c.send_interval || 5000;
        var sel = $("configLocation");
        sel.innerHTML = "";
        ["卧室", "客厅", "厨房", "室外", "书房", "未分类"].forEach(function (l) {
          var o = document.createElement("option");
          o.value = l; o.textContent = l;
          if (l === c.location) o.selected = true;
          sel.appendChild(o);
        });
        $("deviceConfigModal").classList.add("open");
      });
  }
  window.closeDeviceConfig = function () {
    $("deviceConfigModal").classList.remove("open");
  };
  window.saveDeviceConfig = async function () {
    var payload = {
      device_id: configDeviceId,
      send_interval: parseInt($("configInterval").value, 10),
      location: $("configLocation").value
    };
    await api("/api/esp32/config/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    await api("/api/esp32/config/alias", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: configDeviceId, alias: $("configAlias").value })
    });
    window.closeDeviceConfig();
    loadSensors();
  };

  // ==================== 历史数据弹窗 ====================
  var historyDevice = "";   // "" = 全部设备

  window.openHistory = function (metric) {
    historyMetric = metric;
    $("historyModal").classList.add("open");
    loadHistoryDevices();
    loadHistory();
  };
  window.closeHistory = function () { $("historyModal").classList.remove("open"); };
  window.setHistoryHours = function (h) {
    historyHours = h;
    document.querySelectorAll(".time-btn").forEach(function (b) {
      b.classList.toggle("active", parseInt(b.getAttribute("data-hours"), 10) === h);
    });
    loadHistory();
  };
  window.exportHistory = function () {
    var url = "/api/esp32/sensors/export?location=" + encodeURIComponent(historyLocation) +
      "&hours=" + historyHours;
    if (historyDevice) url += "&id=" + encodeURIComponent(historyDevice);
    window.open(url, "_blank");
  };

  function loadHistoryDevices() {
    fetch("/api/esp32/sensors/latest")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var box = $("deviceTabs");
        if (!box) return;
        box.innerHTML = "";
        var ids = Object.keys(d.devices || {});
        var all = document.createElement("button");
        all.className = "loc-tab" + (historyDevice === "" ? " active" : "");
        all.textContent = "全部";
        all.onclick = function () { historyDevice = ""; loadHistoryDevices(); loadHistory(); };
        box.appendChild(all);
        ids.forEach(function (id) {
          var b = document.createElement("button");
          b.className = "loc-tab" + (historyDevice === id ? " active" : "");
          b.textContent = (d.devices[id].alias || id.slice(0, 8));
          b.onclick = function () { historyDevice = id; loadHistoryDevices(); loadHistory(); };
          box.appendChild(b);
        });
      })
      .catch(function () {});
  }

  function loadHistory() {
    $("historyTitle").textContent = "历史数据 — " + historyLocation + "（" + historyHours + "小时）";
    var url = "/api/esp32/sensors/history?location=" + encodeURIComponent(historyLocation) +
      "&hours=" + historyHours;
    if (historyDevice) url += "&id=" + encodeURIComponent(historyDevice);
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (rows) {
        if (!Array.isArray(rows)) rows = [];
        var lux = [], temp = [], hum = [];
        rows.forEach(function (r) {
          lux.push({ value: (r.lux != null && r.lux >= 0) ? r.lux : null });
          // 温湿度 0 为传感器失效哨兵值 → 断点（gap）
          temp.push({ value: (r.temperature != null && r.temperature > 0) ? r.temperature : null });
          hum.push({ value: (r.humidity != null && r.humidity > 0) ? r.humidity : null });
        });
        drawChart("chartLux", lux, "#f5576c");
        drawChart("chartTemp", temp, "#4facfe");
        drawChart("chartHum", hum, "#a18cd1");
        renderStats(lux, temp, hum);
      })
      .catch(function () {});
  }

  function renderStats(lux, temp, hum) {
    function agg(arr) {
      var vals = arr.map(function (x) { return x.value; }).filter(function (v) { return v != null; });
      if (!vals.length) return "无数据";
      var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
      var sum = vals.reduce(function (a, b) { return a + b; }, 0);
      return (Math.round(min * 10) / 10) + " / " + (Math.round(max * 10) / 10) + " / " +
        (Math.round((sum / vals.length) * 10) / 10);
    }
    $("statLux").textContent = agg(lux);
    $("statTemp").textContent = agg(temp);
    $("statHum").textContent = agg(hum);
  }

  function drawChart(canvasId, data, color) {
    var c = $(canvasId);
    if (!c) return;
    var ctx = c.getContext("2d");
    var w = c.width, h = c.height;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "#eef0f5";
    ctx.lineWidth = 1;
    for (var g = 1; g < 4; g++) {
      ctx.beginPath(); ctx.moveTo(32, (h * g) / 4); ctx.lineTo(w, (h * g) / 4); ctx.stroke();
    }
    var vals = data.map(function (d) { return d.value; }).filter(function (v) { return v != null; });
    if (!vals.length) {
      ctx.fillStyle = "#bbb"; ctx.font = "12px sans-serif"; ctx.textAlign = "center";
      ctx.fillText("暂无数据", w / 2, h / 2);
      return;
    }
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    if (min === max) { min -= 1; max += 1; }
    var n = data.length;
    function X(i) { return 32 + (w - 46) * i / (n > 1 ? n - 1 : 1); }
    function Y(v) { return 8 + (h - 24) * (1 - (v - min) / (max - min)); }
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
    var started = false;
    for (var i = 0; i < n; i++) {
      var d = data[i];
      if (d.value == null) { started = false; continue; }
      if (!started) { ctx.moveTo(X(i), Y(d.value)); started = true; }
      else ctx.lineTo(X(i), Y(d.value));
    }
    ctx.stroke();
    // 标注范围
    ctx.fillStyle = "#999"; ctx.font = "10px sans-serif"; ctx.textAlign = "left";
    ctx.fillText(Math.round(max * 10) / 10, 2, 12);
    ctx.fillText(Math.round(min * 10) / 10, 2, h - 6);
  }

  // ==================== AI 对话（含工具调用可视化 P0） ====================
  var chatMessages = $("chatMessages");
  var typing = $("typingIndicator");

  function addMsg(text, who) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + who;
    var b = document.createElement("div");
    b.className = "msg-bubble";
    b.textContent = text;
    wrap.appendChild(b);
    chatMessages.appendChild(wrap);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return wrap;
  }

  function addToolCall(text) {
    var d = document.createElement("div");
    d.className = "tool-call";
    d.textContent = "🔧 " + text.replace(/^\[TOOL\]\s*/, "");
    chatMessages.appendChild(d);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  window.sendMessage = async function () {
    var input = $("chatInput");
    var text = input.value.trim();
    if (!text || isLoading) return;
    input.value = "";
    isLoading = true;
    $("sendBtn").disabled = true;
    typing.classList.add("active");

    addMsg(text, "user");
    try {
      var resp = await api("/api/llm/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text })
      });
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var fullText = "";
      var streamingDiv = null;

      while (true) {
        var result = await reader.read();
        if (result.done) break;
        var chunk = decoder.decode(result.value);
        var lines = chunk.split("\n");
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i];
          if (line.indexOf("data: ") !== 0) continue;
          var data = line.substring(6);
          if (data === "[START]" || data === "[DONE]") continue;
      // 2026-11-16：LLM 推理约 3 秒，显示"思考中"占位
      if (data.indexOf("[THINK]") === 0) { addToolCall(data); continue; }
          if (data.indexOf("[ERROR]") === 0) { addMsg(data.substring(7), "bot"); continue; }
          if (data.indexOf("[TOOL]") === 0) { addToolCall(data); continue; }
          if (!streamingDiv) {
            streamingDiv = addMsg("", "bot");
          }
          fullText += data;
          streamingDiv.querySelector(".msg-bubble").innerText = fullText;
          chatMessages.scrollTop = chatMessages.scrollHeight;
        }
      }
      if (!fullText && !streamingDiv) addMsg("模型无返回，请重试", "bot");
    } catch (e) {
      addMsg("错误: " + e.message, "bot");
    } finally {
      isLoading = false;
      $("sendBtn").disabled = false;
      typing.classList.remove("active");
      checkModelStatus();
    }
  };

  function checkModelStatus() {
    fetch("/api/llm/status")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var el = $("modelStatus");
        el.textContent = d.status === "running" ? "● 模型在线" : "● 模型离线";
        el.className = "model-status " + (d.status === "running" ? "online" : "offline");
      })
      .catch(function () {});
  }

  // ==================== 语音模式（管理员） ====================
  var voiceMode = "off";
  var voicePollTimer = null;

  function setVoiceMode(mode) {
    voiceMode = mode;
    var btns = ["voiceWakeBtn", "voiceAlwaysBtn", "voiceOffBtn"];
    btns.forEach(function (id) {
      var b = $(id);
      if (b) b.classList.toggle("active", id === "voiceWakeBtn" && mode === "wake" ||
        id === "voiceAlwaysBtn" && mode === "always" || id === "voiceOffBtn" && mode === "off");
    });
    var st = $("voiceModeStatus");
    st.textContent = mode === "wake" ? "唤醒式监听中" : mode === "always" ? "全程监听中" : "已关闭";
    if (voicePollTimer) { clearInterval(voicePollTimer); voicePollTimer = null; }
    if (mode !== "off") voicePollTimer = setInterval(checkVoiceResult, 1500);
  }

  function checkVoiceResult() {
    fetch("/api/mic/upload/result")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (voiceMode === "wake" && !d.wake) return;
        var text = d.text || d.transcript || d.result || "";
        if (!text) return;
        setVoiceMode("off");
        $("chatInput").value = text;
        window.sendMessage();
      })
      .catch(function () {});
  }

  // ==================== Tab 导航（管理员） ====================
  function initTabs() {
    document.querySelectorAll(".nav-item").forEach(function (btn) {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".nav-item").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        var name = btn.getAttribute("data-tab");
        document.querySelectorAll(".tab-panel").forEach(function (p) {
          p.classList.toggle("active", p.id === "tab-" + name);
        });
        if (name === "control") { loadLightDeviceList(); loadIrDeviceList(); loadIrCodes(); }
        if (name === "rules") loadRules();
        if (name === "firmware") loadFirmwareInfo();
      });
    });
  }

  // ==================== 灯带（管理员） ====================
  function fillDeviceSelect(selId, devices) {
    var sel = $(selId);
    if (!sel) return;
    sel.innerHTML = "";
    Object.keys(devices || {}).forEach(function (id) {
      var o = document.createElement("option");
      o.value = id;
      o.textContent = (devices[id].alias || id);   // 无别名显示完整 12 位 ID
      sel.appendChild(o);
    });
  }

  function loadLightDeviceList() {
    fetch("/api/esp32/sensors/latest").then(function (r) { return r.json(); }).then(function (d) {
      fillDeviceSelect("lightDevice", d.devices);
      onLightDeviceChange();
    }).catch(function () {});
  }

  window.onLightDeviceChange = function () {
    var id = $("lightDevice").value;
    if (!id) return;
    fetch("/api/admin/light/state?device_id=" + encodeURIComponent(id))
      .then(function (r) { return r.json(); })
      .then(function (s) {
        $("lightToggleBtn").textContent = s.on ? "关灯" : "开灯";
        $("lightBrightness").value = s.brightness || 128;
        $("lightBrightnessVal").textContent = s.brightness || 128;
        var lc = s.led_count || 10;
        $("lightCount").value = lc;
        $("lightCountVal").textContent = lc;
        if (s.r != null) $("lightColor").value = rgbToHex(s.r, s.g, s.b);
      })
      .catch(function () {});
  };

  function rgbToHex(r, g, b) {
    function h(n) { var s = n.toString(16); return s.length === 1 ? "0" + s : s; }
    return "#" + h(r) + h(g) + h(b);
  }

  window.toggleLight = function () {
    var id = $("lightDevice").value;
    if (!id) return;
    var turnOn = $("lightToggleBtn").textContent === "开灯";
    fetch("/api/admin/light/set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: id, on: turnOn })
    }).then(function (r) { return r.json(); }).then(function () { onLightDeviceChange(); });
  };

  window.applyLight = function () {
    var id = $("lightDevice").value;
    if (!id) return;
    var hex = $("lightColor").value;
    var r = parseInt(hex.substr(1, 2), 16), g = parseInt(hex.substr(3, 2), 16), b = parseInt(hex.substr(5, 2), 16);
    fetch("/api/admin/light/set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_id: id, on: true,
        r: r, g: g, b: b,
        brightness: parseInt($("lightBrightness").value, 10),
        led_count: parseInt($("lightCount").value, 10)
      })
    }).then(function (r) { return r.json(); }).then(function () {
      $("lightMsg").textContent = "✅ 已应用（设备最长 2 秒响应）";
      onLightDeviceChange();
    });
  };

  // ==================== 红外（管理员） ====================
  var irLearnedData = null;

  function loadIrDeviceList() {
    fetch("/api/esp32/sensors/latest").then(function (r) { return r.json(); }).then(function (d) {
      fillDeviceSelect("irDevice", d.devices);
    }).catch(function () {});
  }

  function loadIrCodes() {
    fetch("/api/admin/ir/codes").then(function (r) { return r.json(); }).then(function (d) {
      var tbody = $("irCodeTable");
      var codes = d.codes || [];
      if (!codes.length) {
        tbody.innerHTML = '<tr><td colspan="3" style="color:#999;padding:6px 0">暂无已保存的遥控码</td></tr>';
        return;
      }
      tbody.innerHTML = "";
      codes.forEach(function (c) {
        var tr = document.createElement("tr");
        tr.innerHTML =
          "<td>" + escapeHtml(c.name) + "</td>" +
          "<td style='font-family:monospace'>0x" + (c.data >>> 0).toString(16).toUpperCase().padStart(8, "0") + "</td>" +
          "<td><button class='btn btn-primary btn-sm' onclick='sendIrCode(" + c.data + ")'>发送</button> " +
          "<button class='btn btn-red btn-sm' onclick='deleteIrCode(" + JSON.stringify(c.name) + ")'>删除</button></td>";
        tbody.appendChild(tr);
      });
    }).catch(function () {});
  }

  // ==================== 米家设备（5.6）====================
  // 防抖：同一目标操作后 3 秒内锁定按钮，避免连点导致反复触发云端下发
  var MI_COOLDOWN = 3000;
  var miLocked = {};        // target -> 解锁时间戳
  var miLastDevs = [];      // 最近一次设备数据（用于锁定期间重绘）

  function miLockApply() {
    var now = Date.now();
    Object.keys(miLocked).forEach(function (t) {
      var left = miLocked[t] - now;
      var btn = document.querySelector('[data-mi-target="' + t + '"]');
      if (left <= 0) {
        delete miLocked[t];
        if (btn) { btn.disabled = false; btn.style.opacity = ""; }
      } else if (btn) {
        btn.disabled = true;
        btn.style.opacity = "0.5";
        btn.textContent = "冷却 " + Math.ceil(left / 1000) + "s";
      }
    });
  }

  // 属性取值转义：必须转义引号，否则 onclick 属性会被提前截断
  // （2026-10-11 实机故障：onclick="miToggle("oit_cb7",false,"8")" → 浏览器报
  //  "Uncaught SyntaxError: Unexpected end of input"，按钮能显示但点击无效）
  function attrEsc(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  // 产出安全的 onclick 属性片段：miToggle("key","true","target")
  function miOnclick(key, on, target) {
    return "miToggle(" + JSON.stringify(String(key)) + "," +
      (on ? "true" : "false") + "," + JSON.stringify(String(target)) + ")";
  }

  function miBtn(key, label, on, target) {
    var con = on === true;
    var locked = !!miLocked[target];
    return '<button class="btn ' + (con ? "btn-green" : "") + ' btn-sm" ' +
      'data-mi-target="' + attrEsc(target) + '" ' +
      (locked ? 'disabled ' : '') +
      'style="' + (con ? "" : "background:#e8ecf3;color:#66738a") +
      (locked ? ";opacity:0.5" : "") + '" ' +
      'onclick="' + attrEsc(miOnclick(key, !con, target)) + '" title="target=' +
      attrEsc(target) + '">' +
      escapeHtml(label) + " " + (on === null ? "?" : (con ? "●开" : "○关")) + "</button>";
  }

  function loadMiDevices() {
    if (!IS_ADMIN) return;
    fetch("/api/esp32/mi/devices").then(function (r) { return r.json(); }).then(function (d) {
      applyMiDevices(d.devices || []);
    }).catch(function () {});
  }

  function applyMiDevices(devs) {
    var box = $("miList");
    if (!box) return;
    miLastDevs = devs;
    if (!devs.length) {
      box.innerHTML = '<div style="color:#999;padding:6px 0">未配置米家设备</div>';
      return;
    }
      var html = "";
      devs.forEach(function (m) {
        var on = m.power === true;
        var statusColor = m.online ? "#2ecc71" : "#e74c3c";
        var statusText = m.online === null ? "状态未知" : (m.online ? "在线" : "离线");
        var err = m.error ? '<div style="font-size:11px;color:#e67e22;margin-top:2px">' + escapeHtml(m.error) + "</div>" : "";
        html += '<div style="padding:8px 0;border-bottom:1px solid #f0f3f8">' +
          '<div style="display:flex;align-items:center;gap:10px">' +
          '<div style="flex:1">' +
          '<div style="font-weight:600">' + escapeHtml(m.name) + "</div>" +
          '<div style="font-size:12px;color:#8a97a8">' + escapeHtml(m.model) + " · " + escapeHtml(m.ip || "-") +
          ' · <span style="color:' + statusColor + '">' + statusText + "</span>" +
          (m.power === null ? "" : ' · 总开关 <strong style="color:' + (on ? "#2ecc71" : "#95a5a6") + '">' + (on ? "开" : "关") + "</strong>") +
          "</div>" + err + "</div>" +
          miBtn(m.key, on ? "总关" : "总开", on, "main") +
          "</div>";
        // 5 个物理孔位
        var ch = m.channels || [];
        if (ch.length) {
          html += '<div style="margin-top:8px"><div style="font-size:12px;color:#8a97a8;margin-bottom:4px">物理孔位</div>' +
                  '<div style="display:flex;flex-wrap:wrap;gap:6px">';
          ch.forEach(function (c) {
            html += miBtn(m.key, c.name, c.on, String(c.siid));
          });
          html += "</div></div>";
        }
        // 指示灯 + 童锁 + 故障
        var faultTxt = (m.fault === undefined || m.fault === null) ? "" :
          ' · <span style="color:' + (m.fault ? "#e74c3c" : "#8a97a8") + '">故障:' + (m.fault ? "有" : "无") + "</span>";
        html += '<div style="margin-top:8px"><div style="font-size:12px;color:#8a97a8;margin-bottom:4px">其它' + faultTxt + '</div>' +
                '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
                miBtn(m.key, "指示灯", m.indicator, "indicator") +
                miBtn(m.key, "童锁", m.child_lock, "child_lock") +
                "</div></div>";
        html += "</div>";
      });
    box.innerHTML = html;
    miLockApply();          // 重绘后恢复锁定（否则锁定状态会被轮询覆盖）
  }

  function miSay(text, isErr) {
    var el = $("miFeedback");
    if (!el) return;
    el.textContent = new Date().toLocaleTimeString() + " · " + text;
    el.style.color = isErr ? "#e74c3c" : "#2ecc71";
    console.log("[米家]", text);
  }

  window.miToggle = function (key, on, target) {
    var t = (target === undefined) ? "main" : String(target);
    miSay("点击 target=" + t + " → " + (on ? "开" : "关") + "（发送中…）", false);
    if (miLocked[t] && miLocked[t] > Date.now()) {
      miSay("冷却中，已忽略（target=" + t + "）", true);
      return;
    }
    miLocked[t] = Date.now() + MI_COOLDOWN;                // 先上锁，防连点
    miLockApply();

    var body = { key: key, on: on, target: t };
    fetch("/api/admin/mi/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) {
        // 被服务端防抖拦下时不算失败，提示即可
        if (d.message && d.message.indexOf("过于频繁") >= 0) {
          miSay(d.message, true);
        } else {
          miSay("失败: " + (d.message || "未知错误"), true);
          alert(d.message || "控制失败");
        }
      } else {
        miSay("成功: " + (d.message || "ok"), false);
      }
      // 服务端已等云端生效后回读，直接采用返回值，避免"点了没反应"
      if (d.status) {
        var devs = miLastDevs.map(function (m) {
          if (m.key !== key) return m;
          var c = JSON.parse(JSON.stringify(m));
          if (t === "main") c.power = d.status.power;
          if (t === "indicator") c.indicator = d.status.indicator;
          if (t === "child_lock") c.child_lock = d.status.child_lock;
          if (d.status.channels) c.channels = d.status.channels;
          return c;
        });
        applyMiDevices(devs);
      }
      setTimeout(loadMiDevices, 1200);
    }).catch(function (e) {
      miSay("网络错误: " + (e && e.message ? e.message : e), true);
      alert("网络错误");
      miLocked[t] = 0;
      miLockApply();
    });
  };

  // ==================== 离线告警（5.7）====================
  function loadAlerts() {
    if (!IS_ADMIN) return;
    fetch("/api/esp32/alerts").then(function (r) { return r.json(); }).then(function (d) {
      var box = $("alertBanner");
      var body = $("alertBannerBody");
      if (!box || !body) return;
      var list = d.active_alerts || [];
      if (!list.length) {
        box.style.display = "none";
        return;
      }
      box.style.display = "block";
      var html = "";
      list.forEach(function (a) {
        var cls = a.status === "failed" ? "critical" : "warning";
        var color = cls === "critical" ? "#e74c3c" : "#e67e22";
        var label = a.status === "failed" ? "需人工介入" : (a.status === "rebooting" ? "重启中" : "离线");
        html += '<div style="display:flex;align-items:center;gap:8px;padding:3px 0">' +
          '<span style="color:' + color + ';font-weight:600">● ' + label + "</span>" +
          '<span>设备 ' + escapeHtml(a.device_id) + "</span>" +
          '<span style="color:#8a97a8">静默 ' +
          (a.silent_s === undefined || a.silent_s === null ? "-" : Math.round(a.silent_s) + "s") +
          " · 已重启 " + (a.attempts || 0) + " 次</span>" +
          '<span style="flex:1"></span>' +
          '<button class="btn btn-sm" onclick="manualReboot(' + JSON.stringify(a.device_id) + ')">远控重启</button>' +
          '<button class="btn btn-sm" onclick="clearAlert(' + JSON.stringify(a.device_id) + ')">忽略</button>' +
          "</div>";
      });
      var recent = (d.events || []).filter(function (e) { return e.action === "recovered"; })[0];
      if (recent) {
        html += '<div style="margin-top:4px;color:#2ecc71;font-size:12px">最近恢复：' +
          escapeHtml(recent.device_id) + " @ " + escapeHtml(recent.ts) + "</div>";
      }
      body.innerHTML = html;
    }).catch(function () {});
  }

  window.manualReboot = function (deviceId) {
    if (!confirm("确定要远控重启 " + deviceId + " 吗？")) return;
    fetch("/api/admin/device/reboot", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: deviceId })
    }).then(function (r) { return r.json(); }).then(function (d) {
      alert(d.message || (d.ok ? "已下发" : "下发失败"));
      setTimeout(loadAlerts, 1000);
    }).catch(function () { alert("网络错误"); });
  };

  window.clearAlert = function (deviceId) {
    fetch("/api/admin/selfheal/clear", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: deviceId })
    }).then(function () { loadAlerts(); }).catch(function () {});
  };

  window.startIrLearn = function () {
    var id = $("irDevice").value;
    if (!id) return;
    irLearnedData = null;
    $("irLearnedBox").style.display = "none";
    $("irLearnStatus").textContent = "⏳ 学习模式开启，60 秒内用遥控器对准接收头按键...";
    fetch("/api/admin/ir/learn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: id })
    }).then(function (r) { return r.json(); }).then(function () {
      var timer = setInterval(function () {
        fetch("/api/admin/ir/learned?device_id=" + encodeURIComponent(id))
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d.learned && d.learned.data) {
              clearInterval(timer);
              irLearnedData = d.learned.data;
              $("irLearnedCode").textContent = "0x" + (d.learned.data >>> 0).toString(16).toUpperCase().padStart(8, "0");
              $("irLearnedBox").style.display = "flex";
              $("irLearnStatus").textContent = "✅ 学习成功，命名后保存";
            } else if (!d.learning) {
              clearInterval(timer);
              $("irLearnStatus").textContent = "⏰ 学习超时，请重试";
            }
          });
      }, 1500);
    });
  };

  window.saveIrCode = function () {
    var name = $("irCodeName").value.trim();
    if (!name || irLearnedData == null) return;
    fetch("/api/admin/ir/codes/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, data: irLearnedData })
    }).then(function (r) { return r.json(); }).then(function () {
      $("irCodeName").value = "";
      $("irLearnedBox").style.display = "none";
      loadIrCodes();
    });
  };

  window.sendIrCode = function (code) {
    var id = $("irDevice").value;
    if (!id) return;
    fetch("/api/admin/ir/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: id, data: code })
    }).then(function (r) { return r.json(); }).then(function () {
      $("irLearnStatus").textContent = "✅ 红外码已下发";
    });
  };

  window.deleteIrCode = function (name) {
    fetch("/api/admin/ir/codes/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name })
    }).then(function (r) { return r.json(); }).then(loadIrCodes);
  };

  // ==================== 固件 OTA（管理员） ====================
  function loadFirmwareInfo() {
    fetch("/api/admin/firmware/manifest")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var info = d.manifest || {};
        $("fwCurrentInfo").textContent = info.version
          ? "最新固件: v" + info.version + "（" + (info.note || "无备注") + "，上传于 " + (info.uploaded_at || "?") + "）"
          : "暂无固件，上传后 ESP32 将自动升级";
        var tbody = $("fwDeviceTable");
        var devs = d.devices || {};
        var ids = Object.keys(devs);
        if (!ids.length) { tbody.innerHTML = '<tr><td colspan="4" style="color:#999;padding:6px 0">暂无设备上报</td></tr>'; return; }
        tbody.innerHTML = "";
        ids.forEach(function (id) {
          var v = devs[id];
          var tr = document.createElement("tr");
          tr.innerHTML = "<td>" + escapeHtml(id.slice(0, 8) + "…") + "</td>" +
            "<td>v" + (v.fw || "?") + "</td>" +
            "<td>" + (v.last_seen || "?") + "</td>" +
            "<td>" + (v.fw === (info.version || null) ? "<span style='color:#28a745'>最新</span>" : "<span style='color:#f59e0b'>可升级</span>") + "</td>";
          tbody.appendChild(tr);
        });
      })
      .catch(function () {});
  }

  window.uploadFirmware = async function () {
    var file = $("fwFile").files[0];
    var version = $("fwVersion").value.trim();
    if (!file || !version) { $("fwUploadMsg").textContent = "请选择 .bin 文件并填写版本号"; return; }
    var fd = new FormData();
    fd.append("file", file);
    fd.append("version", version);
    fd.append("note", $("fwNote").value.trim());
    $("fwUploadMsg").textContent = "上传中...";
    var resp = await api("/api/admin/firmware/upload", { method: "POST", body: fd });
    var d = await resp.json();
    $("fwUploadMsg").textContent = d.status === "ok" ? "✅ " + (d.message || "上传成功") : "❌ " + (d.message || "失败");
    if (d.status === "ok") loadFirmwareInfo();
  };

  // ==================== 自动化规则（P1，管理员） ====================
  var editRuleId = null;

  function loadRules() {
    fetch("/api/admin/automation")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        renderRules(d.rules || []);
        var e = d.engine || {};
        $("ruleEngineInfo").textContent =
          "引擎状态: " + (e.running ? "🟢 运行中" : "🔴 未运行") +
          " · 检查周期 " + (e.interval || "?") + "s" +
          (e.last_check ? " · 最近检查 " + e.last_check : "");
      })
      .catch(function () {});
  }

  function renderRules(rules) {
    var box = $("ruleList");
    if (!rules.length) {
      box.innerHTML = '<div style="color:#999;padding:12px">暂无规则，点击"+ 新建规则"创建第一条自动化</div>';
      return;
    }
    box.innerHTML = "";
    var metricName = { lux: "光照", temperature: "温度", humidity: "湿度" };
    var actionName = { light_on: "开灯", light_off: "关灯", notify: "告警记录" };
    rules.forEach(function (r) {
      var item = document.createElement("div");
      item.className = "rule-item" + (r.enabled ? "" : " disabled");
      item.innerHTML =
        '<div>' +
        '<div class="rule-name">' + escapeHtml(r.name) + '</div>' +
        '<div class="rule-cond">' + escapeHtml(r.location) + " " +
        (metricName[r.metric] || r.metric) + " " + (r.operator === ">" ? ">" : "<") + " " + r.threshold +
        " → " + (actionName[r.action] || r.action) +
        " · 冷却 " + r.cooldown + "s" +
        (r.in_cooldown ? ' <span style="color:#f59e0b">(冷却中)</span>' : "") +
        "</div></div>" +
        '<div class="rule-actions">' +
        '<label class="switch"><input type="checkbox" ' + (r.enabled ? "checked" : "") +
        ' onchange="toggleRule(' + r.id + ", this.checked)" + '><span class="switch-slider"></span></label>' +
        '<button class="btn btn-sm" style="background:#eef1fb" onclick="editRule(' + r.id + ')">编辑</button>' +
        '<button class="btn btn-red btn-sm" onclick="deleteRule(' + r.id + ')">删除</button>' +
        "</div>";
      box.appendChild(item);
    });
  }

  window.openRuleModal = function () {
    editRuleId = null;
    $("ruleModalTitle").textContent = "新建规则";
    $("ruleId").value = "";
    $("ruleName").value = "";
    $("ruleThreshold").value = "";
    $("ruleCooldown").value = "300";
    if ($("ruleHold")) $("ruleHold").value = "0";             // 5.8 持续满足
    if ($("ruleDeviceId")) $("ruleDeviceId").value = "";      // 5.8 米家分控目标
    fillRuleLocation("卧室");
    $("ruleModal").classList.add("open");
  };

  window.editRule = function (id) {
    fetch("/api/admin/automation").then(function (r) { return r.json(); }).then(function (d) {
      var r = (d.rules || []).filter(function (x) { return x.id === id; })[0];
      if (!r) return;
      editRuleId = id;
      $("ruleModalTitle").textContent = "编辑规则";
      $("ruleId").value = id;
      $("ruleName").value = r.name;
      $("ruleThreshold").value = r.threshold;
      $("ruleCooldown").value = r.cooldown;
      if ($("ruleHold")) $("ruleHold").value = r.hold_s || 0;
      if ($("ruleDeviceId")) $("ruleDeviceId").value = r.device_id || "";
      $("ruleMetric").value = r.metric;
      $("ruleOperator").value = r.operator;
      $("ruleAction").value = r.action;
      fillRuleLocation(r.location);
      $("ruleModal").classList.add("open");
    });
  };

  window.closeRuleModal = function () { $("ruleModal").classList.remove("open"); };

  function fillRuleLocation(selected) {
    var sel = $("ruleLocation");
    sel.innerHTML = "";
    ["卧室", "客厅", "厨房", "室外", "书房", "未分类"].forEach(function (l) {
      var o = document.createElement("option");
      o.value = l; o.textContent = l;
      if (l === selected) o.selected = true;
      sel.appendChild(o);
    });
  }

  window.saveRule = async function (ev) {
    ev.preventDefault();
    var payload = {
      name: $("ruleName").value.trim(),
      location: $("ruleLocation").value,
      metric: $("ruleMetric").value,
      operator: $("ruleOperator").value,
      threshold: parseFloat($("ruleThreshold").value),
      action: $("ruleAction").value,
      cooldown: parseInt($("ruleCooldown").value, 10),
      hold_s: $("ruleHold") ? parseInt($("ruleHold").value || "0", 10) : 0,
      device_id: $("ruleDeviceId") ? $("ruleDeviceId").value.trim() : ""
    };
    var path = "/api/admin/automation";
    var opts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    };
    if (editRuleId) {
      path += "/" + editRuleId;
      opts.method = "PUT";
    }
    var resp = await api(path, opts);
    var d = await resp.json();
    if (d.status !== "ok") { alert(d.message || "保存失败"); return false; }
    window.closeRuleModal();
    loadRules();
    return false;
  };

  window.toggleRule = async function (id, checked) {
    await api("/api/admin/automation/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: checked })
    });
    loadRules();
  };

  window.deleteRule = async function (id) {
    if (!confirm("确认删除该规则？")) return;
    await api("/api/admin/automation/" + id, { method: "DELETE" });
    loadRules();
  };

  // ==================== 初始化 ====================
  // ==================== 5.5 服务器/节点/链路可观测性 ====================
  var _sysPrevNet = null;   // 上一采样网络字节，用于算速率

  function _bar(label, value, unit, percent, color) {
    var pct = (percent === null || percent === undefined) ? null : percent;
    var txt = (value === null || value === undefined) ? "—" : value + (unit || "");
    var w = pct === null ? 0 : Math.max(0, Math.min(100, pct));
    var c = color || "#4facfe";
    if (pct !== null && pct >= 85) c = "#f5576c";
    else if (pct !== null && pct >= 70) c = "#f6a623";
    return '<div style="margin-bottom:8px">' +
      '<div style="display:flex;justify-content:space-between;font-size:12px;color:#5a6b7f">' +
      '<span>' + label + '</span><span>' + txt + (pct === null ? "" : " · " + pct.toFixed(0) + "%") + '</span></div>' +
      '<svg width="100%" height="6" style="display:block;margin-top:3px">' +
      '<rect x="0" y="0" width="100%" height="6" rx="3" fill="#eef0f5"></rect>' +
      '<rect x="0" y="0" width="' + w + '%" height="6" rx="3" fill="' + c + '"></rect>' +
      '</svg></div>';
  }

  function _fmtUptime(s) {
    if (!s) return "—";
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + "天" : "") + h + "小时" + m + "分";
  }

  function _svcName(k) { return { "smart-server": "Flask", "smart-usb-gateway": "网关", "mosquitto": "MQTT" }[k] || k; }

  function renderSystem(d) {
    if (!$("sysBody")) return;
    var h = d.host || {}, html = "";

    // --- Pi 主机 ---
    html += '<div style="font-size:12px;color:#8a97a8;margin-bottom:6px">主机（Orange Pi 5 Pro）</div>';
    html += _bar("CPU", h.cpu_percent === null || h.cpu_percent === undefined ? null : h.cpu_percent,
      "%", h.cpu_percent, "#4facfe");
    if (h.memory) html += _bar("内存", h.memory.used_mb + "/" + h.memory.total_mb, " MB",
      h.memory.percent, "#a18cd1");
    if (h.disk) html += _bar("数据盘", h.disk.used_gb + "/" + h.disk.total_gb, " GB",
      h.disk.percent, "#43cea2");

    var bits = [];
    if (h.temperature_c !== null && h.temperature_c !== undefined) bits.push("🌡 " + h.temperature_c + "°C");
    if (h.uptime_s) bits.push("⏱ " + _fmtUptime(h.uptime_s));
    if (h.network && h.network.rate) bits.push("⬇ " + h.network.rate.rx_kbps + " ⬆ " + h.network.rate.tx_kbps + " KB/s");
    else if (h.network) bits.push("网络字节 " + Math.round(h.network.rx_bytes / 1048576) + " MB ↓");
    if (h.load_avg) bits.push("负载 " + h.load_avg.join(" / "));
    html += '<div style="font-size:12px;color:#5a6b7f;margin:4px 0 10px">' + (bits.join("　") || "—") + '</div>';

    // --- 服务 ---
    var svc = d.services || {}, svcHtml = [];
    Object.keys(svc).forEach(function (k) {
      var v = svc[k];
      var icon = v === true ? "🟢" : (v === false ? "🔴" : "⚪");
      svcHtml.push(icon + " " + _svcName(k));
    });
    html += '<div style="font-size:12px;color:#5a6b7f;margin-bottom:10px">服务　' + svcHtml.join("　") + '</div>';

    // --- 节点静默时长 ---
    var devs = (d.devices && d.devices.devices) || [];
    if (devs.length) {
      html += '<div style="font-size:12px;color:#8a97a8;margin-bottom:4px">节点</div>';
      devs.forEach(function (dv) {
        var silent = dv.silent_s === null || dv.silent_s === undefined ? "—" : dv.silent_s + "s";
        var icon = dv.online ? "🟢" : "🔴";
        html += '<div style="font-size:12px;color:#5a6b7f;display:flex;justify-content:space-between">' +
          '<span>' + icon + ' ' + (dv.location || dv.device_id) + ' <span style="color:#a8b3c0">' +
          dv.device_id.slice(-4) + ' / ' + dv.transport + '</span></span>' +
          '<span>静默 ' + silent + '</span></div>';
      });
    }

    // --- 链路 ---
    var ls = d.link || {};
    var dl = ls.downlink_stats || {};
    if (dl && (dl.sent || dl.retry || dl.timeout)) {
      html += '<div style="font-size:12px;color:#8a97a8;margin:10px 0 4px">链路</div>' +
        '<div style="font-size:12px;color:#5a6b7f">下行 发送' + (dl.sent || 0) + ' 重发' + (dl.retry || 0) +
        ' 超时' + (dl.timeout || 0) + '　待回执 ' + (ls.downlink_pending || 0) + '</div>';
    }
    var gw = ls.gateway || {};
    if (gw && gw.stats) {
      var st = gw.stats;
      html += '<div style="font-size:12px;color:#5a6b7f">网关 USB帧' + (st.usb_frames || 0) +
        ' 数据' + (st.data_events || 0) + ' CRC错' + (st.crc_bad || 0) + '</div>';
    }

    $("sysBody").innerHTML = html;

    // --- 顶部状态摘要 ---
    var online = devs.filter(function (x) { return x.online; }).length;
    var bad = Object.keys(svc).filter(function (k) { return svc[k] === false; });
    var st = $("sysStatus");
    if (bad.length) { st.textContent = "🔴 " + bad.map(_svcName).join("/") + " 异常"; st.style.color = "#dc3545"; }
    else if (devs.length && online < devs.length) { st.textContent = "🟡 " + online + "/" + devs.length + " 节点在线"; st.style.color = "#f6a623"; }
    else { st.textContent = "🟢 正常"; st.style.color = "#28a745"; }
    if (d.parts_age_ms) {
      var svcAge = d.parts_age_ms.services, devAge = d.parts_age_ms.devices;
      st.textContent += "　主机实时 / 服务 " + (svcAge / 1000).toFixed(1) + "s / 节点 " + (devAge / 1000).toFixed(1) + "s";
    }
  }

  // ==================== 5.14 能耗与用电画像 ====================
  // 数据源：sensor_history.current_ma（INA219，仅 04B 有）
  // 电量口径：P(W)=I(mA)*5V/1000；E(kWh)=ΣP*Δt/3600，Δt=5s
  function renderEnergy(d) {
    var html = "";
    var t = d.today || {};
    var noData = !(t.samples > 0);

    // 实时概览
    html += '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px">';
    html += '<div><div style="font-size:11px;color:#8a97a8">今日用电</div>' +
            '<div style="font-size:19px;font-weight:600;color:#2ecc71">' +
            (t.kwh || 0).toFixed(4) + ' <span style="font-size:12px">kWh</span></div></div>';
    html += '<div><div style="font-size:11px;color:#8a97a8">平均功率</div>' +
            '<div style="font-size:19px;font-weight:600">' + (t.avg_power_w || 0).toFixed(3) +
            ' <span style="font-size:12px">W</span></div></div>';
    html += '<div><div style="font-size:11px;color:#8a97a8">峰值电流</div>' +
            '<div style="font-size:19px;font-weight:600">' + (t.peak_ma || 0).toFixed(1) +
            ' <span style="font-size:12px">mA</span></div></div>';
    html += '<div><div style="font-size:11px;color:#8a97a8">本周 / 本月</div>' +
            '<div style="font-size:13px;color:#5a6b7f">' + ((d.week || {}).kwh || 0).toFixed(4) +
            ' / ' + ((d.month || {}).kwh || 0).toFixed(4) + ' kWh</div></div>';
    html += '</div>';

    // 近 7 天柱状图
    var daily = d.daily || [];
    if (daily.length) {
      var maxK = 0;
      daily.forEach(function (x) { if (x.kwh > maxK) maxK = x.kwh; });
      if (maxK <= 0) maxK = 0.001;
      var barW = 100 / daily.length;
      html += '<div style="font-size:11px;color:#8a97a8;margin:6px 0 2px">近 ' + daily.length + ' 天用电量</div>';
      html += '<div style="display:flex;align-items:flex-end;height:70px;gap:3px;border-bottom:1px solid #e8ecf3;padding-bottom:2px">';
      daily.forEach(function (x) {
        var h = Math.max(2, Math.round(x.kwh / maxK * 62));
        var col = x.kwh > 0 ? "#3498db" : "#e8ecf3";
        html += '<div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center" ' +
                'title="' + x.date + ' ' + x.kwh.toFixed(4) + ' kWh / ' + x.avg_ma.toFixed(0) + ' mA">' +
                '<div style="width:70%;height:' + h + 'px;background:' + col + ';border-radius:2px 2px 0 0"></div>' +
                '</div>';
      });
      html += '</div><div style="display:flex;gap:3px;margin-top:3px">';
      daily.forEach(function (x) {
        html += '<div style="flex:1;text-align:center;font-size:9px;color:#8a97a8">' +
                x.date.slice(5) + '</div>';
      });
      html += '</div>';
    }

    // 设备用电排行
    var devs = d.devices || [];
    if (devs.length) {
      html += '<div style="font-size:11px;color:#8a97a8;margin:10px 0 4px">设备用电（近 ' +
              (d.daily || []).length + ' 天）</div>';
      devs.forEach(function (x) {
        html += '<div style="display:flex;justify-content:space-between;font-size:12px;color:#5a6b7f;padding:2px 0">' +
                '<span>' + attrEsc(x.device_id.slice(0, 8)) + '</span>' +
                '<span>' + x.kwh.toFixed(4) + ' kWh · ' + x.avg_ma.toFixed(1) + ' mA</span></div>';
      });
    }

    if (noData) {
      html += '<div style="font-size:11px;color:#e67e22;margin-top:8px">' +
              '电流数据从 ' + attrEsc(d.data_start || '') + ' 开始采集，需积累数日后曲线才有意义</div>';
    }
    html += '<div style="font-size:10px;color:#a8b3bf;margin-top:6px">' +
            '口径：P = I × ' + d.voltage_v + 'V；E = ΣP × ' + d.sample_interval_s + 's / 3600</div>';

    $("energyBody").innerHTML = html;
    var st2 = $("energyStatus");
    if (st2) {
      st2.textContent = noData ? "⚠ 数据积累中" : "🟢 " + (t.avg_power_w || 0).toFixed(2) + " W";
      st2.style.color = noData ? "#f6a623" : "#28a745";
    }
  }

  function loadEnergy() {
    if (!$("energyBody")) return;
    fetch("/api/admin/energy/summary?days=7")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.status === "ok") renderEnergy(d);
      })
      .catch(function () {});
  }

  // ==================== 5.13 操作审计 ====================
  // 数据源：core/audit.py（所有下行在 link/downlink.send_downlink 统一记录）
  // 来源：web / voice / ai_tool / automation / system / undo
  var AUDIT_SRC_COLOR = {
    web: "#3498db", voice: "#9b59b6", ai_tool: "#e67e22",
    automation: "#16a085", system: "#7f8c8d", undo: "#e74c3c"
  };
  var AUDIT_SRC_NAME = {
    web: "网页", voice: "语音", ai_tool: "AI工具",
    automation: "自动化", system: "系统", undo: "撤销"
  };
  var AUDIT_ACT_NAME = {
    light_set: "灯带", ir_send: "红外", tts_speak: "播报",
    device_reboot: "重启", mi_control: "米家", device_config: "配置"
  };
  var AUDIT_UNDOABLE = { light_set: 1, mi_control: 1 };

  function renderAudit(d) {
    var items = d.items || [];
    var st = d.stats || {};
    var html = "";
    html += '<div style="font-size:12px;color:#5a6b7f;margin-bottom:6px">' +
            '共 ' + (st.total || 0) + ' 条 · 今日 ' + (st.today || 0) +
            ' · 失败 ' + (st.failed || 0) + ' · 已撤销 ' + (st.undone || 0) + '</div>';
    if (!items.length) {
      html += '<div style="color:#999;padding:8px">暂无操作记录</div>';
    } else {
      html += '<div style="max-height:260px;overflow-y:auto">';
      items.forEach(function (x) {
        var srcColor = AUDIT_SRC_COLOR[x.source] || "#95a5a6";
        var srcName = AUDIT_SRC_NAME[x.source] || x.source;
        var actName = AUDIT_ACT_NAME[x.action] || x.action;
        var okMark = x.result === "ok"
          ? '<span style="color:#28a745">&#10003;</span>'
          : '<span style="color:#dc3545">&#10007;</span>';
        var dev = (x.device_id || "").slice(0, 12);
        var undoBtn = "";
        if (AUDIT_UNDOABLE[x.action] && !x.undone) {
          undoBtn = ' <button onclick="undoAction(' + x.id + ')" ' +
                    'style="font-size:10px;padding:1px 5px;border:1px solid #e8ecf3;' +
                    'background:#fff;border-radius:3px;cursor:pointer;color:#e74c3c">撤销</button>';
        } else if (x.undone) {
          undoBtn = '<span style="font-size:10px;color:#95a5a6"> 已撤销</span>';
        }
        html += '<div style="display:flex;align-items:center;gap:6px;font-size:11px;' +
                'padding:3px 0;border-bottom:1px solid #f2f5f9">' +
                '<span style="color:#a8b3bf;min-width:64px">' + (x.ts || "").slice(5, 19) + '</span>' +
                '<span style="background:' + srcColor + ';color:#fff;border-radius:3px;' +
                'padding:0 4px;font-size:10px">' + srcName + '</span>' +
                '<span style="color:#5a6b7f;min-width:40px">' + (x.actor || "-") + '</span>' +
                '<span style="color:#2c3e50;min-width:44px">' + actName + '</span>' +
                '<span style="color:#8a97a8;flex:1;overflow:hidden;text-overflow:ellipsis;' +
                'white-space:nowrap">' + attrEsc(dev) + '</span>' +
                okMark + undoBtn + '</div>';
      });
      html += '</div>';
    }
    $("auditBody").innerHTML = html;
    var s2 = $("auditStatus");
    if (s2) {
      s2.textContent = "🟢 " + (st.today || 0) + " 条今日操作";
      s2.style.color = "#28a745";
    }
  }

  function loadAudit() {
    if (!$("auditBody")) return;
    Promise.all([
      fetch("/api/admin/audit?limit=10").then(function (r) { return r.json(); }),
      fetch("/api/admin/audit/stats").then(function (r) { return r.json(); })
    ]).then(function (arr) {
      var d = arr[0] || {};
      d.stats = arr[1] || {};
      if (d.status === "ok") renderAudit(d);
    }).catch(function () {});
  }

  window.undoAction = function (id) {
    if (!confirm("确定撤销这次操作？将恢复到操作前的状态。")) return;
    fetch("/api/admin/action/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: id })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.status === "ok") {
        alert("已恢复到操作前状态");
      } else {
        alert("撤销失败：" + (d.message || "未知错误"));
      }
      loadAudit();
    }).catch(function () { alert("请求失败"); });
  };

  function loadSystem() {
    if (!IS_ADMIN) return;
    fetch("/api/admin/system")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) renderSystem(d); })
      .catch(function () {});
  }

  function init() {
    initAuth();
    $("sendBtn").addEventListener("click", window.sendMessage);
    $("chatInput").addEventListener("keydown", function (e) {
      if (e.key === "Enter") window.sendMessage();
    });
    $("voiceBtn").addEventListener("click", function () { if (IS_ADMIN) setVoiceMode("wake"); });
    if (IS_ADMIN) {
      if ($("voiceWakeBtn")) $("voiceWakeBtn").addEventListener("click", function () { setVoiceMode("wake"); });
      if ($("voiceAlwaysBtn")) $("voiceAlwaysBtn").addEventListener("click", function () { setVoiceMode("always"); });
      if ($("voiceOffBtn")) $("voiceOffBtn").addEventListener("click", function () { setVoiceMode("off"); });
      $("lightBrightness").addEventListener("input", function () {
        $("lightBrightnessVal").textContent = $("lightBrightness").value;
      });
      $("lightCount").addEventListener("input", function () {
        $("lightCountVal").textContent = $("lightCount").value;
      });
      initTabs();
    }
    loadSensors();
    checkModelStatus();
    if (IS_ADMIN) loadSystem();
    if (IS_ADMIN) loadMiDevices();
    if (IS_ADMIN) loadAlerts();
    if (IS_ADMIN) loadEnergy();       // 5.14 能耗
    if (IS_ADMIN) loadAudit();        // 5.13 操作审计
    setInterval(loadSensors, 5000);
    setInterval(checkModelStatus, 15000);
    if (IS_ADMIN) setInterval(loadSystem, 3000);
    if (IS_ADMIN) setInterval(loadEnergy, 30000);   // 5.14 能耗（30s 刷新，数据 5s 一条）
    if (IS_ADMIN) setInterval(loadAudit, 20000);    // 5.13 审计（20s 刷新）
    if (IS_ADMIN) setInterval(loadMiDevices, 10000);
    if (IS_ADMIN) setInterval(loadAlerts, 10000);
    setInterval(miLockApply, 500);    // 冷却倒计时刷新
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
