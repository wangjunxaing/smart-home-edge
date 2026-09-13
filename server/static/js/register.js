/*
 * 注册/登录页 JavaScript
 * =======================
 * 功能：
 *   1. 密码显示/隐藏切换（点击眼睛图标）
 *   2. 登录/注册面板切换动画（通过 .active 类控制 CSS transform）
 *   3. 表单数据保存/恢复（切换面板时保留已填写内容）
 *   4. 前端表单验证（密码长度 >= 6、两次密码一致）
 *   5. 异步 API 请求（登录 → /api/login，注册 → /api/register）
 *
 * 面板切换原理：
 *   - register.css 中 .login__register 默认 transform: translateX(15rem) + opacity:0
 *   - JS 添加/移除 .active 类来触发 CSS transition 动画
 *   - 切换时自动保存/恢复表单内容，避免用户数据丢失
 */

/* ==================== 密码显示/隐藏切换 ==================== */
// 通过切换 input[type] 在 password 和 text 之间转换，同时切换闭眼/睁眼图标
const passwordAccess = (loginPass, loginEye) => {
  const input = document.getElementById(loginPass);
  const iconEye = document.getElementById(loginEye);

  if (input && iconEye) {
    iconEye.addEventListener("click", () => {
      if (input.type === "password") {
        input.type = "text";                        // 显示密码
        iconEye.classList.remove("icon-eye-slash"); // 闭眼 → 睁眼图标
        iconEye.classList.add("icon-eye");
      } else {
        input.type = "password";                    // 隐藏密码
        iconEye.classList.remove("icon-eye");       // 睁眼 → 闭眼图标
        iconEye.classList.add("icon-eye-slash");
      }
    });
  }
};
passwordAccess("loginPasswordInput", "loginPasswordEye");    // 登录页密码框

/* ==================== 注册页密码显示/隐藏 ==================== */
// 复用同一逻辑，为注册页的两个密码框绑定
const passwordRegister = (loginPass, loginEye) => {
  const input = document.getElementById(loginPass);
  const iconEye = document.getElementById(loginEye);
  if (input && iconEye) {
    iconEye.addEventListener("click", () => {
      if (input.type === "password") {
        input.type = "text";
        iconEye.classList.remove("icon-eye-slash");
        iconEye.classList.add("icon-eye");
      } else {
        input.type = "password";
        iconEye.classList.remove("icon-eye");
        iconEye.classList.add("icon-eye-slash");
      }
    });
  }
};
passwordRegister("regPassword", "regPasswordEye");          // 注册密码框
passwordRegister("confirmPassword", "confirmPasswordEye");  // 确认密码框

/* ==================== 获取表单元素引用 ==================== */
const loginUsername = document.getElementById("loginUsername");
const loginPassword = document.getElementById("loginPasswordInput");
const regUsername = document.getElementById("regUsername");
const regPassword = document.getElementById("regPassword");
const regConfirmPassword = document.getElementById("confirmPassword");

/* ==================== 表单数据保存与恢复 ==================== */
// 用户在登录和注册面板之间切换时，保留已填写的内容
let savedRegisterData = {
  username: "",
  password: "",
  confirmPassword: "",
};

// 保存注册表单数据到内存对象
const saveRegisterFormData = () => {
  if (regUsername) savedRegisterData.username = regUsername.value;
  if (regPassword) savedRegisterData.password = regPassword.value;
  if (regConfirmPassword)
    savedRegisterData.confirmPassword = regConfirmPassword.value;
};

// 从内存对象恢复注册表单数据（切换回注册面板时调用）
const restoreRegisterFormData = () => {
  if (regUsername) regUsername.value = savedRegisterData.username;
  if (regPassword) regPassword.value = savedRegisterData.password;
  if (regConfirmPassword)
    regConfirmPassword.value = savedRegisterData.confirmPassword;

  // 触发 input 事件以更新 Float Label 状态
  [regUsername, regPassword, regConfirmPassword].forEach((input) => {
    if (input && input.value) {
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
};

// 清空注册表单（注册成功后调用）
const clearRegisterFormData = () => {
  if (regUsername) regUsername.value = "";
  if (regPassword) regPassword.value = "";
  if (regConfirmPassword) regConfirmPassword.value = "";
  savedRegisterData = { username: "", password: "", confirmPassword: "" };
  // 触发 input 事件以重置 Float Label
  [regUsername, regPassword, regConfirmPassword].forEach((input) => {
    if (input) {
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
};

// 清空登录表单
const clearLoginFormData = () => {
  if (loginUsername) loginUsername.value = "";
  if (loginPassword) loginPassword.value = "";
  [loginUsername, loginPassword].forEach((input) => {
    if (input && input.value === "") {
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
};

/* ==================== 登录/注册面板切换 ==================== */
const loginAccessRegister = document.getElementById("loginAccessRegister");
const buttonRegister = document.getElementById("loginButtonRegister");  // "立即注册" 按钮
const buttonAccess = document.getElementById("loginButtonAccess");      // "前往登录" 按钮

let isRegisterCompleted = false;  // 注册是否刚完成（用于清空表单逻辑）

// 点击 "立即注册" → 切换到注册面板
if (buttonRegister) {
  buttonRegister.addEventListener("click", () => {
    clearLoginFormData();           // 清空登录表单
    if (isRegisterCompleted) {       // 刚注册完 → 清空注册表单
      clearRegisterFormData();
      isRegisterCompleted = false;
    } else {
      restoreRegisterFormData();    // 正常切换 → 恢复之前填的内容
    }
    loginAccessRegister.classList.add("active");  // 触发 CSS 动画
  });
}

// 点击 "前往登录" → 切换回登录面板
if (buttonAccess) {
  buttonAccess.addEventListener("click", () => {
    clearLoginFormData();
    saveRegisterFormData();         // 保存注册表单内容
    loginAccessRegister.classList.remove("active");  // 触发 CSS 反向动画
  });
}

// 输入时自动保存注册表单（切换面板时不丢失）
if (regUsername) regUsername.addEventListener("input", saveRegisterFormData);
if (regPassword) regPassword.addEventListener("input", saveRegisterFormData);
if (regConfirmPassword) regConfirmPassword.addEventListener("input", saveRegisterFormData);

// URL hash 路由：支持直接跳转到注册面板
if (window.location.hash === "#loginButtonRegister") {
  loginAccessRegister.classList.add("active");
}

/* ==================== 返回主页按钮 ==================== */
const backToMainFromLogin = document.getElementById("backToMainFromLogin");
if (backToMainFromLogin) {
  backToMainFromLogin.addEventListener("click", () => {
    window.location.href = "/";
  });
}

const backToMainFromRegister = document.getElementById("backToMainFromRegister");
if (backToMainFromRegister) {
  backToMainFromRegister.addEventListener("click", () => {
    window.location.href = "/";
  });
}

/* ==================== 注册表单前端验证 ==================== */
// 两次密码一致性检查（在失去焦点时触发）
if (regConfirmPassword) {
  regConfirmPassword.addEventListener("change", () => {
    if (regPassword.value !== regConfirmPassword.value) {
      regConfirmPassword.setCustomValidity("两次输入的密码不一致");
    } else {
      regConfirmPassword.setCustomValidity("");
    }
  });
}

// 密码长度检查（至少 6 位）+ 二次确认联动
if (regPassword) {
  regPassword.addEventListener("change", () => {
    if (regPassword.value.length < 6) {
      regPassword.setCustomValidity("密码长度至少6位");
    } else {
      regPassword.setCustomValidity("");
    }
    // 密码修改后重新校验确认密码
    if (regConfirmPassword && regConfirmPassword.value) {
      if (regPassword.value !== regConfirmPassword.value) {
        regConfirmPassword.setCustomValidity("两次输入的密码不一致");
      } else {
        regConfirmPassword.setCustomValidity("");
      }
    }
  });
}

/* ==================== 登录 API 请求 ==================== */
const loginForm = document.getElementById("loginForm");
if (loginForm) {
  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();   // 阻止表单默认提交行为（页面刷新）
    const username = document.getElementById("loginUsername").value;
    const password = document.getElementById("loginPasswordInput").value;

    if (!username || !password) {
      alert("请填写账号和密码！");
      return;
    }

    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const data = await response.json();

      if (data.success) {
        // 登录成功 → 保存用户名到 localStorage → 跳转主页
        localStorage.setItem("username", data.username);
        localStorage.setItem("isLoggedIn", "true");
        window.location.href = "/main.html";
      } else {
        alert(data.message || "登录失败");
      }
    } catch (error) {
      console.error("登录错误:", error);
      alert("网络错误，请稍后重试");
    }
  });
}

/* ==================== 注册 API 请求 ==================== */
const registerForm = document.getElementById("registerForm");
if (registerForm) {
  registerForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = document.getElementById("regUsername").value;
    const password = document.getElementById("regPassword").value;
    const confirmPassword = document.getElementById("confirmPassword").value;

    // 前端二次验证（即使 HTML5 validation 已做，保险起见）
    if (!username) { alert("请输入账号！"); return; }
    if (password.length < 6) { alert("密码长度至少6位！"); return; }
    if (password !== confirmPassword) { alert("两次输入的密码不一致！"); return; }

    try {
      const response = await fetch("/api/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const data = await response.json();

      if (data.success) {
        alert("注册成功！请登录");
        // 注册成功 → 自动切换到登录面板 → 清空注册表单
        document.getElementById("loginButtonAccess").click();
        document.getElementById("regUsername").value = "";
        document.getElementById("regPassword").value = "";
        document.getElementById("confirmPassword").value = "";
      } else {
        alert(data.message || "注册失败");
      }
    } catch (error) {
      console.error("注册错误:", error);
      alert("网络错误，请稍后重试");
    }
  });
}