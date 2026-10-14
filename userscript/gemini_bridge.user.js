// ==UserScript==
// @name         Gemini Web2API Bridge for CLI Agents
// @namespace    https://github.com/Bowen-0x00/gemini-web2api
// @version      1.3.0
// @description  Bridges local CLI agents to Gemini Web & Google AI Studio via CSP-proof GM_xmlhttpRequest
// @match        https://gemini.google.com/*
// @match        https://aistudio.google.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';
  if (window.top !== window.self) {
    // Ignore all iframes (such as bscframe)
    return;
  }

  const DEFAULT_BASE_URL = "http://127.0.0.1:38765";
  let baseUrl = (typeof GM_getValue !== "undefined" ? GM_getValue("bridge_url", DEFAULT_BASE_URL) : localStorage.getItem("gemini_bridge_url")) || DEFAULT_BASE_URL;
  baseUrl = baseUrl.replace(/\/+$/, "");

  let tabId = "tab_" + Math.random().toString(36).substring(2, 9);
  let currentRequestId = null;
  let responseObserver = null;
  let lastStreamedLength = 0;
  let isGenerating = false;
  let isPolling = false;

  const isAIStudio = window.location.hostname.includes("aistudio.google.com");
  const isGeminiWeb = window.location.hostname.includes("gemini.google.com");

  console.log(`[GeminiBridge] Starting on ${window.location.hostname} (tabId: ${tabId})`);

  function logToServer(msg) {
    console.log("[GeminiBridge]", msg);
    httpPost(`${baseUrl}/browser/log`, { tab_id: tabId, message: String(msg) }).catch(() => {});
  }

  // ==========================================
  // GM_xmlhttpRequest Wrappers (Bypasses CSP)
  // ==========================================
  function httpPost(url, data) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url: url,
        headers: { "Content-Type": "application/json" },
        data: JSON.stringify(data),
        timeout: 15000,
        onload: (res) => {
          try {
            resolve(JSON.parse(res.responseText));
          } catch (e) {
            resolve({ raw: res.responseText });
          }
        },
        onerror: (err) => reject(err),
        ontimeout: () => reject(new Error("Request timed out"))
      });
    });
  }

  function httpGet(url, timeoutMs = 35000) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "GET",
        url: url,
        timeout: timeoutMs,
        onload: (res) => {
          try {
            resolve(JSON.parse(res.responseText));
          } catch (e) {
            resolve({ raw: res.responseText });
          }
        },
        onerror: (err) => reject(err),
        ontimeout: () => resolve({ type: "idle" })
      });
    });
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // ==========================================
  // UI Status Indicator
  // ==========================================
  function createUI() {
    if (document.getElementById("gemini-bridge-indicator")) return;

    const el = document.createElement("div");
    el.id = "gemini-bridge-indicator";
    el.style.position = "fixed";
    el.style.bottom = "20px";
    el.style.right = "20px";
    el.style.zIndex = "999999";
    el.style.padding = "8px 14px";
    el.style.borderRadius = "20px";
    el.style.fontSize = "12px";
    el.style.fontWeight = "500";
    el.style.fontFamily = "-apple-system, BlinkMacSystemFont, sans-serif";
    el.style.boxShadow = "0 2px 10px rgba(0,0,0,0.2)";
    el.style.cursor = "pointer";
    el.style.transition = "all 0.3s ease";
    el.style.userSelect = "none";
    el.title = "Click to configure Bridge Server URL";

    el.onclick = () => {
      const newUrl = prompt("Enter Bridge Server HTTP URL (e.g. http://127.0.0.1:38765):", baseUrl);
      if (newUrl && newUrl !== baseUrl) {
        baseUrl = newUrl.replace(/\/+$/, "");
        if (typeof GM_setValue !== "undefined") {
          GM_setValue("bridge_url", baseUrl);
        } else {
          localStorage.setItem("gemini_bridge_url", baseUrl);
        }
        location.reload();
      }
    };

    document.body.appendChild(el);
    updateUI("disconnected", "Bridge: Connecting...");
  }

  function updateUI(status, text) {
    const el = document.getElementById("gemini-bridge-indicator");
    if (!el) return;
    if (status === "connected") {
      el.style.backgroundColor = "#e6f4ea";
      el.style.color = "#137333";
      el.style.border = "1px solid #ceead6";
      el.innerText = `🟢 ${text || 'Bridge: Ready'}`;
    } else if (status === "busy") {
      el.style.backgroundColor = "#fef7e0";
      el.style.color = "#b06000";
      el.style.border = "1px solid #fdd663";
      el.innerText = `🟡 ${text || 'Bridge: Generating...'}`;
    } else {
      el.style.backgroundColor = "#fce8e6";
      el.style.color = "#c5221f";
      el.style.border = "1px solid #fad2cf";
      el.innerText = `🔴 ${text || 'Bridge: Disconnected'}`;
    }
  }

  function detectCurrentModel() {
    if (isGeminiWeb) {
      const picker = document.querySelector(".model-picker-btn, [data-test-id='model-picker'], .header-model-name");
      if (picker && picker.textContent) return picker.textContent.trim();
    } else if (isAIStudio) {
      const modelSelect = document.querySelector("ms-model-selector, .model-selector-text");
      if (modelSelect && modelSelect.textContent) return modelSelect.textContent.trim();
    }
    return "gemini-4-argon";
  }

  // ==========================================
  // Registration and Polling Loop
  // ==========================================
  async function registerTab() {
    const model = detectCurrentModel();
    const res = await httpPost(`${baseUrl}/browser/register`, {
      tab_id: tabId,
      model: model,
      url: window.location.href
    });
    if (res && res.status === "ok") {
      logToServer("Tab registered successfully with model " + model);
      updateUI("connected", "Bridge: Connected (Idle)");
      return res;
    } else {
      throw new Error("Target is not Gemini Web2API Proxy: " + JSON.stringify(res));
    }
  }

  async function startPollingLoop() {
    if (isPolling) return;
    isPolling = true;

    while (true) {
      try {
        const data = await httpGet(`${baseUrl}/browser/poll?tab_id=${tabId}`, 30000);

        if (data.type === "re_register") {
          await registerTab();
          continue;
        }

        if (data.type === "chat_request") {
          logToServer("Received chat_request " + data.request_id);
          updateUI("busy", "Bridge: Generating...");
          await handleChatRequest(data);
        } else if (data.type === "idle") {
          updateUI("connected", "Bridge: Connected (Idle)");
        }
      } catch (err) {
        console.warn("[GeminiBridge] Polling error (retrying in 2s):", err);
        updateUI("disconnected", "Bridge: Disconnected (Retrying...)");
        await sleep(2000);
        try {
          await registerTab();
        } catch (_) {}
      }
    }
  }

  // Periodic heartbeat
  setInterval(async () => {
    try {
      await httpGet(`${baseUrl}/browser/heartbeat?tab_id=${tabId}`, 5000);
    } catch (_) {}
  }, 15000);

  // ==========================================
  // DOM Automation
  // ==========================================

  function getInputElement() {
    if (isGeminiWeb) {
      return (
        document.querySelector("div.ql-editor[contenteditable='true']") ||
        document.querySelector("rich-textarea div[contenteditable='true']") ||
        document.querySelector("[contenteditable='true'][role='textbox']") ||
        document.querySelector("textarea[aria-label*='prompt' i]") ||
        document.querySelector("div[role='textbox']") ||
        document.querySelector("textarea")
      );
    } else if (isAIStudio) {
      return (
        document.querySelector("textarea[placeholder*='prompt' i]") ||
        document.querySelector("ms-prompt-input textarea") ||
        document.querySelector("textarea")
      );
    }
    return document.querySelector("textarea, [contenteditable='true']");
  }

  function getSendButton() {
    if (isGeminiWeb) {
      return (
        document.querySelector("button[aria-label*='send' i]") ||
        document.querySelector("button[aria-label*='发送' i]") ||
        document.querySelector(".send-button-container button") ||
        document.querySelector("button.send-button") ||
        document.querySelector("button[mat-icon-button][aria-label*='prompt' i]") ||
        document.querySelector("button[aria-label*='Submit' i]")
      );
    } else if (isAIStudio) {
      return (
        document.querySelector("button[aria-label*='Run' i]") ||
        document.querySelector("button.run-button") ||
        document.querySelector("button.primary-button")
      );
    }
    return null;
  }

  function getStopButton() {
    if (isGeminiWeb) {
      return (
        document.querySelector("button[aria-label*='stop' i]") ||
        document.querySelector("button[aria-label*='停止' i]") ||
        document.querySelector("button.stop-button")
      );
    } else if (isAIStudio) {
      return document.querySelector("button[aria-label*='Cancel' i], button.cancel-button");
    }
    return null;
  }

  function triggerNewChat() {
    logToServer("triggerNewChat: Resetting to fresh conversation...");
    if (isGeminiWeb) {
      const selectors = [
        "a[aria-label*='New chat' i]",
        "button[aria-label*='New chat' i]",
        "a[aria-label*='新对话' i]",
        "button[aria-label*='新对话' i]",
        "button[aria-label*='新聊天' i]",
        "button[aria-label*='发起新对话' i]",
        "[data-test-id='new-chat-button']",
        ".new-chat-button",
        "a[href='/app']"
      ];
      for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn) {
          logToServer(`Clicked new chat button: ${sel}`);
          btn.click();
          return true;
        }
      }
      const logo = document.querySelector("a[href='/app'], a[aria-label*='Gemini' i]");
      if (logo) {
        logToServer("Clicked Gemini logo to start new chat");
        logo.click();
        return true;
      }
    } else if (isAIStudio) {
      const resetBtn = document.querySelector("button[aria-label*='Clear chat' i], button.clear-button");
      if (resetBtn) {
        resetBtn.click();
        return true;
      }
    }
    return false;
  }

  // Silent Audio keep-alive: prevents Chrome from throttling/suspending when minimized/in background
  let audioKeepAliveStarted = false;
  function enableBackgroundKeepAlive() {
    if (audioKeepAliveStarted) return;
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      gain.gain.value = 0.00001; // virtually silent
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      audioKeepAliveStarted = true;
      console.log("[GeminiBridge] Background audio keep-alive active (prevents tab throttling when minimized)");
    } catch (_) {}
  }
  document.addEventListener("click", enableBackgroundKeepAlive, { once: true });

  async function setThinkingLevel(level) {
    if (!level) return;
    try {
      const targetKeywords = {
        "high": ["高点", "高", "high"],
        "medium": ["中", "适中", "medium"],
        "low": ["低", "low"],
        "off": ["关闭", "off", "none"]
      }[level.toLowerCase()] || ["高点", "高"];

      const buttons = Array.from(document.querySelectorAll("button"));
      const pickerBtn = buttons.find(b => b.textContent.includes("Argon") || b.getAttribute("aria-label")?.includes("Argon"));
      if (!pickerBtn) return;

      if (targetKeywords.some(kw => pickerBtn.textContent.includes(kw))) {
        logToServer(`Thinking level already matches ${level}`);
        return;
      }

      logToServer(`Switching thinking level to ${level}`);
      pickerBtn.click();
      await sleep(350);

      const menuItems = Array.from(document.querySelectorAll("div[role='menuitem'], button, .mat-menu-item, span"));
      const thinkingItem = menuItems.find(el => el.textContent.trim().startsWith("思考等级"));
      if (thinkingItem) {
        thinkingItem.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
        thinkingItem.click();
        await sleep(350);

        const subOptions = Array.from(document.querySelectorAll("div[role='menuitem'], button, .mat-menu-item, mat-option, span"));
        const targetOption = subOptions.find(el => targetKeywords.some(kw => el.textContent.trim() === kw || el.textContent.includes(kw)));
        if (targetOption) {
          targetOption.click();
          logToServer(`Thinking level set to: ${targetOption.textContent.trim()}`);
          await sleep(200);
        }
      }

      const backdrop = document.querySelector(".cdk-overlay-backdrop, [role='region']");
      if (backdrop) backdrop.click();
    } catch (e) {
      logToServer(`Failed to switch thinking level: ${e.message}`);
    }
  }

  async function attachTextAsFile(text, filename = "context_data.txt") {
    try {
      const fileInput = document.querySelector("input[type='file']");
      if (!fileInput) return false;
      const file = new File([text], filename, { type: "text/plain" });
      const dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
      fileInput.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    } catch (e) {
      return false;
    }
  }

  async function injectTextAndSend(text, needReset = false) {
    logToServer(`injectTextAndSend: starting (length=${text.length}, needReset=${needReset})`);
    if (needReset) {
      triggerNewChat();
      await sleep(1000);
    }

    const input = getInputElement();
    if (!input) {
      logToServer("ERROR: Could not find prompt input field");
      throw new Error("Could not find prompt input field in current tab");
    }
    logToServer(`Input element found: <${input.tagName.toLowerCase()}> class="${input.className}"`);

    if (text.length > 4000) {
      const attached = await attachTextAsFile(text);
      if (attached) {
        text = "Please examine the attached context and instructions.";
      }
    }

    input.focus();
    await sleep(150);

    let inserted = false;

    // Method 1: ClipboardEvent paste (native to Quill/rich-textarea)
    try {
      const dt = new DataTransfer();
      dt.setData("text/plain", text);
      const pasteEvt = new ClipboardEvent("paste", {
        clipboardData: dt,
        bubbles: true,
        cancelable: true
      });
      input.dispatchEvent(pasteEvt);
      const val = input.innerText || input.value || "";
      if (val.trim().length > 0) {
        inserted = true;
        logToServer(`ClipboardEvent paste successful, length=${val.trim().length}`);
      }
    } catch (e) {
      logToServer(`ClipboardEvent paste failed: ${e.message}`);
    }

    // Method 2: document.execCommand insertText
    if (!inserted) {
      try {
        document.execCommand("selectAll", false, null);
        const execSuccess = document.execCommand("insertText", false, text);
        logToServer(`execCommand insertText result: ${execSuccess}`);
        const val = input.innerText || input.value || "";
        if (val.trim().length > 0) inserted = true;
      } catch (err) {
        logToServer(`execCommand error: ${err.message}`);
      }
    }

    // Method 3: Fallback DOM insertion
    if (!inserted) {
      logToServer("Falling back to innerHTML injection");
      if (input.tagName === "TEXTAREA" || input.tagName === "INPUT") {
        input.value = text;
      } else {
        input.innerHTML = `<p>${text.replace(/\n/g, '<br>')}</p>`;
      }
      input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    await sleep(500);

    let sendBtn = getSendButton();
    if (!sendBtn) {
      const parentArea = input.closest(".input-area, .chat-input-container, form, rich-textarea")?.parentElement || document;
      const buttons = parentArea.querySelectorAll("button");
      logToServer(`Searching buttons, found ${buttons.length} candidate buttons`);
      for (const btn of buttons) {
        const label = (btn.getAttribute("aria-label") || btn.className || "").toLowerCase();
        if (label.includes("send") || label.includes("发送") || label.includes("prompt") || btn.querySelector("svg, mat-icon")) {
          sendBtn = btn;
          logToServer(`Found candidate button: aria-label="${btn.getAttribute('aria-label')}"`);
          break;
        }
      }
    }

    if (sendBtn) {
      sendBtn.removeAttribute("disabled");
      sendBtn.click();
      logToServer("Clicked send button");
    } else {
      logToServer("Dispatching Enter keydown to input");
      input.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter",
        code: "Enter",
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true
      }));
    }
    logToServer("Prompt injection and submission complete");
  }

  // ==========================================
  // Observation & Streaming
  // ==========================================

  function getLatestResponseElement() {
    if (isGeminiWeb) {
      const candidates = document.querySelectorAll(
        "message-content, .model-response-text, .response-container, .markdown, .model-turn .message-body"
      );
      if (candidates.length > 0) return candidates[candidates.length - 1];
    } else if (isAIStudio) {
      const candidates = document.querySelectorAll(
        "ms-chat-turn.model .response-content, .rendered-markdown, .model-response"
      );
      if (candidates.length > 0) return candidates[candidates.length - 1];
    }
    return null;
  }

  function startObservingResponse(requestId) {
    currentRequestId = requestId;
    lastStreamedLength = 0;
    isGenerating = true;
    logToServer("startObservingResponse started for " + requestId);

    setTimeout(() => {
      let target = getLatestResponseElement();
      const parentToWatch = target ? (target.parentElement || target) : document.body;
      logToServer("Watching response target: " + (target ? target.tagName : "document.body"));

      if (responseObserver) responseObserver.disconnect();

      responseObserver = new MutationObserver(() => {
        handleResponseMutation(requestId);
      });

      responseObserver.observe(parentToWatch, {
        childList: true,
        subtree: true,
        characterData: true
      });

      const pollInterval = setInterval(() => {
        if (!isGenerating || currentRequestId !== requestId) {
          clearInterval(pollInterval);
          return;
        }
        handleResponseMutation(requestId);
      }, 150);

      checkCompletionInterval(requestId);
    }, 600);
  }

  async function handleResponseMutation(requestId) {
    if (!isGenerating || currentRequestId !== requestId) return;

    const respEl = getLatestResponseElement();
    if (!respEl) return;

    const currentText = respEl.innerText || respEl.textContent || "";
    if (currentText.length > lastStreamedLength) {
      const chunk = currentText.substring(lastStreamedLength);
      lastStreamedLength = currentText.length;

      try {
        const res = await httpPost(`${baseUrl}/browser/chunk`, {
          tab_id: tabId,
          request_id: requestId,
          chunk: chunk
        });
        if (res && res.abort) {
          logToServer("Received abort signal from server, clicking stop button");
          const stopBtn = getStopButton();
          if (stopBtn) stopBtn.click();
        }
      } catch (e) {
        console.error("[GeminiBridge] Error sending chunk:", e);
      }

      if (currentText.includes("</tool_call>")) {
        logToServer("Detected </tool_call> in output, clicking stop button");
        const stopBtn = getStopButton();
        if (stopBtn) stopBtn.click();
      }
    }
  }

  function checkCompletionInterval(requestId) {
    let idleCounter = 0;
    let prevLen = 0;

    const interval = setInterval(() => {
      if (!isGenerating || currentRequestId !== requestId) {
        clearInterval(interval);
        return;
      }

      const stopBtn = getStopButton();
      const respEl = getLatestResponseElement();
      const currentLen = respEl ? (respEl.innerText || "").length : 0;

      if (currentLen > prevLen) {
        idleCounter = 0;
        prevLen = currentLen;
      } else if (currentLen > 0) {
        idleCounter++;
      }

      // Finish conditions: Stop button disappeared, or no text change for 2.5 seconds
      const generationStopped = (!stopBtn && currentLen > 0) || (idleCounter >= 18);

      if (generationStopped) {
        clearInterval(interval);
        logToServer(`Generation complete for ${requestId} (len=${currentLen})`);
        finishResponse(requestId);
      }
    }, 200);
  }

  async function finishResponse(requestId) {
    if (!isGenerating || currentRequestId !== requestId) return;
    isGenerating = false;

    if (responseObserver) {
      responseObserver.disconnect();
      responseObserver = null;
    }

    const respEl = getLatestResponseElement();
    if (respEl) {
      const currentText = respEl.innerText || respEl.textContent || "";
      if (currentText.length > lastStreamedLength) {
        const chunk = currentText.substring(lastStreamedLength);
        try {
          await httpPost(`${baseUrl}/browser/chunk`, {
            tab_id: tabId,
            request_id: requestId,
            chunk: chunk
          });
        } catch (_) {}
      }
    }

    try {
      await httpPost(`${baseUrl}/browser/done`, {
        tab_id: tabId,
        request_id: requestId
      });
      logToServer("Sent /browser/done for " + requestId);
    } catch (_) {}

    updateUI("connected", "Bridge: Connected (Idle)");
    currentRequestId = null;
  }

  async function handleChatRequest(data) {
    const { request_id, text, need_reset, thinking_level } = data;
    enableBackgroundKeepAlive();
    try {
      if (thinking_level) {
        await setThinkingLevel(thinking_level);
      }
      await injectTextAndSend(text, need_reset);
      startObservingResponse(request_id);
    } catch (err) {
      logToServer("Error in handleChatRequest: " + err.message);
      try {
        await httpPost(`${baseUrl}/browser/error`, {
          tab_id: tabId,
          request_id: request_id,
          message: err.message || "Failed to inject prompt into web page"
        });
      } catch (_) {}
      updateUI("connected", "Bridge: Connected (Idle)");
    }
  }

  // Init UI and polling loop
  createUI();
  registerTab().then(() => {
    startPollingLoop();
  }).catch((err) => {
    logToServer("Initial registration failed: " + err.message);
    startPollingLoop();
  });
})();
