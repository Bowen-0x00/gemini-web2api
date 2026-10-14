"""ChromeDriver / CDP Native Driver Backend for Gemini Web2API.

Allows driving Gemini Web directly via ChromeDriver / Chrome DevTools Protocol without Tampermonkey.
Supports:
- Auto-connecting to existing Chrome (--remote-debugging-port=9222 / debuggerAddress)
- Launching dedicated Chrome profile with anti-throttling keep-alive flags
- Background/minimized/occluded window execution without timer clamping
- Model selection (e.g. gemini-4-argon) and thinking level control (:high, :medium, :low, :off)
- Real-time incremental streaming and auto-braking on </tool_call>
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

logger = logging.getLogger("gemini_chromedriver")

# Ensure localhost requests don't get routed to system HTTP proxy
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"


def find_chromedriver() -> str:
    """Find a suitable chromedriver executable."""
    # 1. Project local drivers directory
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(base_dir, "drivers", "chromedriver.exe"),
        os.path.join(base_dir, "drivers", "chromedriver"),
        os.path.join(base_dir, "bin", "chromedriver.exe"),
        os.path.join(base_dir, "bin", "chromedriver"),
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c

    # 2. Check wdm cache
    user_home = os.path.expanduser("~")
    wdm_glob = os.path.join(user_home, ".wdm", "drivers", "chromedriver")
    if os.path.exists(wdm_glob):
        for root, _, files in os.walk(wdm_glob):
            for f in files:
                if f.lower() in ["chromedriver.exe", "chromedriver"]:
                    full_p = os.path.join(root, f)
                    if os.access(full_p, os.X_OK):
                        return full_p

    # 3. Check system PATH
    which_driver = shutil.which("chromedriver")
    if which_driver:
        return which_driver

    # 4. Attempt webdriver-manager install
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        return ChromeDriverManager().install()
    except Exception as e:
        logger.warning(f"webdriver-manager failed to resolve driver: {e}")

    raise FileNotFoundError("Could not locate a suitable chromedriver binary. Please place it in ./drivers/")


class ChromeDriverBridge:
    """Bridges Gemini Web in a Chrome browser instance to the Gemini Web2API proxy server."""

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:38765",
        debugger_address: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        profile_directory: Optional[str] = None,
        headless: bool = False,
        chromedriver_path: Optional[str] = None,
        tab_id: Optional[str] = None,
        preferred_model: str = "gemini-4-argon",
    ):
        self.server_url = server_url.rstrip("/")
        self.debugger_address = debugger_address
        self.user_data_dir = user_data_dir
        self.profile_directory = profile_directory
        self.headless = headless
        self.chromedriver_path = chromedriver_path or find_chromedriver()
        self.tab_id = tab_id or f"cdp_{uuid.uuid4().hex[:8]}"
        self.preferred_model = preferred_model

        self.driver: Optional[webdriver.Chrome] = None
        self.running = False
        self.session = requests.Session()
        self.session.trust_env = False  # Avoid proxy issues on localhost

        self.current_request_id: Optional[str] = None
        self.is_generating = False
        self.last_streamed_length = 0

    def init_driver(self):
        """Initialize the Selenium Chrome WebDriver."""
        logger.info(f"Initializing ChromeDriver with binary: {self.chromedriver_path}")
        options = Options()

        # Anti-throttling flags: keep JavaScript timers running at full speed even when minimized or occluded
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-features=CalculateNativeWinOcclusion")
        options.add_argument("--remote-allow-origins=*")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        if self.debugger_address:
            logger.info(f"Connecting to existing Chrome instance at debuggerAddress={self.debugger_address}")
            options.add_experimental_option("debuggerAddress", self.debugger_address)
        else:
            if self.user_data_dir:
                logger.info(f"Using user-data-dir: {self.user_data_dir}")
                options.add_argument(f"--user-data-dir={self.user_data_dir}")
            if self.profile_directory:
                options.add_argument(f"--profile-directory={self.profile_directory}")
            if self.headless:
                logger.info("Running in headless mode (--headless=new)")
                options.add_argument("--headless=new")

        service = Service(executable_path=self.chromedriver_path)
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.set_page_load_timeout(60)

        # Navigate to Gemini Web if not already there
        curr_url = self.driver.current_url
        if "gemini.google.com" not in curr_url:
            logger.info("Navigating to https://gemini.google.com/app...")
            self.driver.get("https://gemini.google.com/app")
            time.sleep(3)

        # Inject background silent audio keep-alive
        self.enable_audio_keep_alive()
        logger.info(f"ChromeDriver bridge ready on tab: {self.driver.title} ({self.driver.current_url})")

    def enable_audio_keep_alive(self):
        """Inject Web Audio silent keep-alive into the page to prevent Chrome throttling when minimized."""
        script = """
        if (!window._geminiAudioKeepAlive) {
            try {
                const AudioCtx = window.AudioContext || window.webkitAudioContext;
                if (AudioCtx) {
                    const ctx = new AudioCtx();
                    const osc = ctx.createOscillator();
                    const gain = ctx.createGain();
                    gain.gain.value = 0.00001; // virtually silent
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start();
                    window._geminiAudioKeepAlive = ctx;
                    console.log('[ChromeDriverBridge] Silent audio keep-alive activated');
                }
            } catch (e) {
                console.warn('[ChromeDriverBridge] Audio keep-alive failed:', e);
            }
        }
        """
        try:
            self.driver.execute_script(script)
        except Exception as e:
            logger.warning(f"Failed to enable audio keep-alive: {e}")

    def detect_model(self) -> str:
        """Detect the active model name from the UI."""
        script = """
        const picker = document.querySelector("[aria-label*='mode picker' i], [aria-label*='模式选择器'], .input-area-switch, .model-picker-btn, [data-test-id='model-picker'], .header-model-name");
        if (picker) {
            return (picker.getAttribute('aria-label') || picker.innerText || '').trim();
        }
        return 'gemini-4-argon';
        """
        try:
            val = self.driver.execute_script(script) or ""
            if "argon" in val.lower() or "4 argon" in val.lower():
                return "gemini-4-argon"
            if "pro" in val.lower():
                return "gemini-2.5-pro"
            if "flash" in val.lower():
                return "gemini-2.5-flash"
            return val or self.preferred_model
        except Exception:
            return self.preferred_model

    def ensure_model(self, model_name: str, thinking_level: Optional[str] = None):
        """Ensure the requested model and thinking level are selected."""
        script = f"""
        const targetModel = {json.dumps(model_name.lower())};
        const thinking = {json.dumps(thinking_level.lower() if thinking_level else "")};

        // Check if Argon is requested
        if (targetModel.includes("argon") || targetModel.includes("gemini-4")) {{
            const picker = document.querySelector("[aria-label*='mode picker' i], [aria-label*='模式选择器'], .input-area-switch, .model-picker-btn, [data-test-id='model-picker']");
            if (picker) {{
                const currentText = (picker.getAttribute('aria-label') || picker.innerText || '').toLowerCase();
                if (!currentText.includes("argon")) {{
                    picker.click();
                    // Find 4 Argon option
                    setTimeout(() => {{
                        const items = Array.from(document.querySelectorAll("gem-menu-item, [role='menuitem'], [role='option'], button"));
                        const argonItem = items.find(el => el.textContent.includes("4 Argon") || el.textContent.includes("Argon"));
                        if (argonItem && argonItem.getAttribute('aria-disabled') !== 'true') {{
                            argonItem.click();
                        }}
                    }}, 200);
                }}
            }}
        }}

        // Handle thinking level if requested
        if (thinking) {{
            const targetKeywords = {{
                "high": ["高点", "高", "high"],
                "medium": ["中", "适中", "medium"],
                "low": ["低", "low"],
                "off": ["关闭", "off", "none"]
            }}[thinking] || ["高"];

            const buttons = Array.from(document.querySelectorAll("button, gem-menu-item"));
            const pickerBtn = buttons.find(b => b.textContent.includes("Argon") || b.getAttribute("aria-label")?.includes("Argon"));
            if (pickerBtn) {{
                pickerBtn.click();
                setTimeout(() => {{
                    const menuItems = Array.from(document.querySelectorAll("div[role='menuitem'], button, .mat-menu-item, span"));
                    const thinkingItem = menuItems.find(el => el.textContent.trim().startsWith("思考等级"));
                    if (thinkingItem) {{
                        thinkingItem.click();
                        setTimeout(() => {{
                            const subOptions = Array.from(document.querySelectorAll("div[role='menuitem'], button, .mat-menu-item, mat-option, span"));
                            const targetOpt = subOptions.find(el => targetKeywords.some(kw => el.textContent.trim() === kw || el.textContent.includes(kw)));
                            if (targetOpt) targetOpt.click();
                        }}, 200);
                    }}
                }}, 200);
            }}
        }}
        """
        try:
            self.driver.execute_script(script)
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"Error ensuring model '{model_name}': {e}")

    def trigger_new_chat(self):
        """Reset conversation by navigating or clicking new chat."""
        logger.info("Resetting conversation to fresh /app state...")
        try:
            curr = self.driver.current_url
            if curr.rstrip("/") != "https://gemini.google.com/app":
                self.driver.get("https://gemini.google.com/app")
                time.sleep(2.0)
            else:
                self.driver.execute_script("""
                    const b = document.querySelector("a[aria-label*='发起新对话'], button[aria-label*='发起新对话'], a[aria-label*='New chat' i], button[aria-label*='New chat' i], a[href='/app']");
                    if (b) b.click();
                """)
                time.sleep(1.0)
        except Exception as e:
            logger.warning(f"Failed to reset conversation: {e}")

    def inject_text_and_send(self, text: str):
        """Inject prompt text into input field and click send."""
        script = f"""
        const text = {json.dumps(text)};
        const input = (
            document.querySelector("div.ql-editor[contenteditable='true']") ||
            document.querySelector("rich-textarea div[contenteditable='true']") ||
            document.querySelector("[contenteditable='true'][role='textbox']") ||
            document.querySelector("textarea[aria-label*='prompt' i]") ||
            document.querySelector("div[role='textbox']") ||
            document.querySelector("textarea")
        );
        if (!input) return false;
        input.focus();

        let p = input.querySelector('p');
        if (!p) {{
            p = document.createElement('p');
            input.appendChild(p);
        }}
        p.textContent = text;
        input.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: text}}));
        input.dispatchEvent(new Event('input', {{bubbles: true}}));
        input.dispatchEvent(new Event('change', {{bubbles: true}}));

        setTimeout(() => {{
            const btn = (
                document.querySelector("button[aria-label*='发送' i]") ||
                document.querySelector("button[aria-label*='send' i]") ||
                document.querySelector(".send-button-container button") ||
                document.querySelector("button.send-button") ||
                document.querySelector("button[mat-icon-button][aria-label*='prompt' i]") ||
                document.querySelector("button[aria-label*='Submit' i]")
            );
            if (btn) {{
                btn.removeAttribute('disabled');
                btn.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                btn.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                btn.click();
            }} else {{
                input.dispatchEvent(new KeyboardEvent("keydown", {{
                    key: "Enter",
                    code: "Enter",
                    keyCode: 13,
                    which: 13,
                    bubbles: true,
                    cancelable: true
                }}));
            }}
        }}, 150);
        return true;
        """
        success = self.driver.execute_script(script)
        if not success:
            raise RuntimeError("Failed to inject prompt text into Gemini input")

    def get_message_count(self) -> int:
        """Count the current completed response messages."""
        try:
            return int(self.driver.execute_script("return document.querySelectorAll('message-content').length;"))
        except Exception:
            return 0

    def get_latest_response_text(self, prior_count: int = 0) -> Tuple[str, bool]:
        """Fetch the current accumulated text from the latest model response element and whether stop button exists."""
        script = f"""
        const mcs = document.querySelectorAll("message-content");
        const stopBtn = (
            document.querySelector("button[aria-label*='stop' i]") ||
            document.querySelector("button[aria-label*='停止' i]") ||
            document.querySelector("button.stop-button") ||
            document.querySelector("button[aria-label*='Cancel' i]")
        );

        if (mcs.length <= {prior_count}) {{
            return {{ text: "", isGenerating: !!stopBtn }};
        }}

        const latest = mcs[mcs.length - 1];
        let text = (latest.textContent || latest.innerText || "").trim();
        if (text.startsWith('Gemini 说\\n\\n') || text.startsWith('Gemini said\\n\\n')) {{
            text = text.replace(/^Gemini (说|said)\\n\\n/i, '');
        }}

        return {{
            text: text,
            isGenerating: !!stopBtn
        }};
        """
        try:
            res = self.driver.execute_script(script) or {}
            return res.get("text", ""), res.get("isGenerating", False)
        except Exception:
            return "", False

    def click_stop_button(self):
        """Click the stop button to abort generation."""
        script = """
        const stopBtn = (
            document.querySelector("button[aria-label*='stop' i]") ||
            document.querySelector("button[aria-label*='停止' i]") ||
            document.querySelector("button.stop-button") ||
            document.querySelector("button[aria-label*='Cancel' i]")
        );
        if (stopBtn) {
            stopBtn.click();
            return true;
        }
        return false;
        """
        try:
            self.driver.execute_script(script)
        except Exception:
            pass

    def register_tab(self):
        """Register with the local proxy server."""
        model = self.detect_model()
        url = self.driver.current_url if self.driver else "https://gemini.google.com/app"
        res = self.session.post(
            f"{self.server_url}/browser/register",
            json={"tab_id": self.tab_id, "model": model, "url": url},
            timeout=5.0,
        )
        if res.status_code == 200:
            logger.info(f"Registered with proxy server as tab '{self.tab_id}' (model: {model})")
        else:
            raise RuntimeError(f"Server registration failed: {res.status_code} {res.text}")

    def handle_chat_request(self, task: Dict[str, Any]):
        """Handle a single chat completion turn from the proxy server."""
        req_id = task.get("request_id")
        text = task.get("text", "")
        need_reset = task.get("need_reset", False)
        thinking_level = task.get("thinking_level")

        logger.info(f"Handling chat_request {req_id} (len={len(text)}, need_reset={need_reset}, thinking={thinking_level})")

        self.enable_audio_keep_alive()

        if need_reset:
            self.trigger_new_chat()
            prior_count = 0
        else:
            prior_count = self.get_message_count()

        self.ensure_model(self.preferred_model, thinking_level=thinking_level)

        self.last_streamed_length = 0
        self.is_generating = True
        self.current_request_id = req_id

        # Inject and send
        self.inject_text_and_send(text)

        # Allow initial DOM render
        start_time = time.time()
        time.sleep(0.5)

        idle_cycles = 0
        prev_len = 0
        stopped_due_to_brake = False
        seen_generating = False

        # Observation loop
        while self.is_generating:
            time.sleep(0.15)
            curr_text, is_generating_in_ui = self.get_latest_response_text(prior_count=prior_count)

            if is_generating_in_ui:
                seen_generating = True

            # Stream delta chunk
            if len(curr_text) > self.last_streamed_length:
                chunk = curr_text[self.last_streamed_length:]
                self.last_streamed_length = len(curr_text)
                seen_generating = True

                try:
                    resp = self.session.post(
                        f"{self.server_url}/browser/chunk",
                        json={
                            "tab_id": self.tab_id,
                            "request_id": req_id,
                            "chunk": chunk,
                        },
                        timeout=5.0,
                    )
                    data = resp.json() if resp.status_code == 200 else {}
                    if data.get("abort"):
                        logger.info("Received abort signal from server, clicking stop button")
                        self.click_stop_button()
                        stopped_due_to_brake = True
                except Exception as e:
                    logger.warning(f"Error posting chunk: {e}")

                # Auto-braking: if </tool_call> is encountered, stop immediately
                if "</tool_call>" in curr_text and not stopped_due_to_brake:
                    logger.info("Detected </tool_call> in output, auto-braking (clicking Stop)...")
                    self.click_stop_button()
                    stopped_due_to_brake = True

            # Track progress to detect completion
            if len(curr_text) > prev_len:
                idle_cycles = 0
                prev_len = len(curr_text)
            elif len(curr_text) > 0:
                idle_cycles += 1

            elapsed = time.time() - start_time
            # Done condition:
            # 1. UI was generating and is no longer generating (stop button disappeared), and text settled for >= 8 cycles (1.2s)
            # 2. Or text has settled for >= 20 cycles (3.0s) and at least 2.5s have elapsed
            is_done = (
                (seen_generating and not is_generating_in_ui and idle_cycles >= 8 and len(curr_text) > 0 and elapsed >= 1.5)
                or (idle_cycles >= 20 and len(curr_text) > 0 and elapsed >= 2.5)
            )

            if is_done:
                logger.info(f"Generation complete for request {req_id} (total length: {len(curr_text)})")
                self.is_generating = False
                break

        # Send done signal
        try:
            self.session.post(
                f"{self.server_url}/browser/done",
                json={"tab_id": self.tab_id, "request_id": req_id},
                timeout=5.0,
            )
        except Exception as e:
            logger.warning(f"Error posting done: {e}")

    def run_polling_loop(self):
        """Poll the server for pending chat tasks and execute them."""
        self.running = True
        logger.info(f"Starting ChromeDriver polling loop on {self.server_url}...")

        last_heartbeat = 0.0

        while self.running:
            # Periodic heartbeat
            now = time.time()
            if now - last_heartbeat > 15.0:
                try:
                    self.session.get(f"{self.server_url}/browser/heartbeat?tab_id={self.tab_id}", timeout=3.0)
                    last_heartbeat = now
                except Exception:
                    pass

            try:
                # Poll server with 30s timeout
                resp = self.session.get(
                    f"{self.server_url}/browser/poll?tab_id={self.tab_id}&timeout=30.0",
                    timeout=35.0,
                )
                if resp.status_code != 200:
                    time.sleep(2.0)
                    continue

                task = resp.json()
                task_type = task.get("type")

                if task_type == "re_register":
                    self.register_tab()
                elif task_type == "chat_request":
                    self.handle_chat_request(task)
                elif task_type == "idle":
                    continue

            except (requests.Timeout, requests.ConnectionError):
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"Error in driver polling loop: {e}")
                time.sleep(2.0)

    def close(self):
        """Clean up driver and resources."""
        self.running = False
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        logger.info("ChromeDriverBridge closed.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ChromeDriver Native Driver for Gemini Web2API")
    parser.add_argument("--server", default="http://127.0.0.1:38765", help="Proxy server base URL")
    parser.add_argument("--debugger-address", default="127.0.0.1:9222", help="Existing Chrome debugger address (CDP)")
    parser.add_argument("--user-data-dir", default=None, help="Custom Chrome user profile data directory")
    parser.add_argument("--profile-directory", default=None, help="Profile sub-directory (e.g. 'Default' or 'Profile 1')")
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    parser.add_argument("--model", default="gemini-4-argon", help="Preferred Gemini model name")
    parser.add_argument("--chromedriver", default=None, help="Path to chromedriver executable")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    bridge = ChromeDriverBridge(
        server_url=args.server,
        debugger_address=args.debugger_address,
        user_data_dir=args.user_data_dir,
        profile_directory=args.profile_directory,
        headless=args.headless,
        chromedriver_path=args.chromedriver,
        preferred_model=args.model,
    )

    try:
        bridge.init_driver()
        bridge.register_tab()
        bridge.run_polling_loop()
    except KeyboardInterrupt:
        logger.info("Stopping ChromeDriver bridge...")
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
