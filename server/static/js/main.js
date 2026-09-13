/*
 * 0-Smart 智能家居 — 主前端逻辑 v2（管理员/用户双角色）
 * ===================================================
 * 公共模块：认证、传感器、历史弹窗、AI 对话（含工具调用可视化）
 * 管理员模块：Tab 导航、设备状态/配置、灯带、红外、固件、自动化规则
 */
(function () {
  "use strict";

  var IS_ADMIN = window.IS_ADMIN === true;
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
      item.innerHTML =
        '<div><span class="dot ' + (d.online ? "online" : "offline") + '"></span>' +
        '<span class="device-name">' + escapeHtml(name) + "</span>" +
        '<div class="device-sub">' + escapeHtml(d.location || "") +
        " · 信号 " + (d.rssi != null ? d.rssi : "--") + " dBm" +
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
      cooldown: parseInt($("ruleCooldown").value, 10)
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
    setInterval(loadSensors, 5000);
    setInterval(checkModelStatus, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
