#!/usr/bin/env python3
"""Entry point for running the Gemini Web2API reverse proxy."""

import argparse
import sys
import threading
import time
import uvicorn


def start_driver_worker(args):
    """Run ChromeDriver bridge in a background daemon thread."""
    from bridge.chrome_driver import ChromeDriverBridge

    # Wait briefly for uvicorn server to bind
    time.sleep(1.5)
    server_url = f"http://127.0.0.1:{args.port}"
    bridge = ChromeDriverBridge(
        server_url=server_url,
        debugger_address=args.debugger_address,
        user_data_dir=args.user_data_dir,
        headless=args.headless,
        preferred_model=args.model,
    )
    try:
        bridge.init_driver()
        bridge.register_tab()
        bridge.run_polling_loop()
    except Exception as e:
        print(f"[DriverWorker] Error: {e}", file=sys.stderr)
    finally:
        bridge.close()

def main():
    parser = argparse.ArgumentParser(description="Gemini Web2API Reverse Proxy Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=38765, help="Port to bind (default: 38765)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")

    # Driver mode options
    parser.add_argument("--driver", action="store_true", help="Automatically launch ChromeDriver bridge")
    parser.add_argument("--debugger-address", default="127.0.0.1:9222", help="Existing Chrome debugger address (CDP)")
    parser.add_argument("--user-data-dir", default=None, help="Custom Chrome user profile data directory")
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    parser.add_argument("--model", default="gemini-4-argon", help="Preferred Gemini model name")

    args = parser.parse_args()

    print("================================================================")
    print("  Gemini Web2API Reverse Proxy Starting")
    print(f"  HTTP & WebSocket URL: http://{args.host}:{args.port}")
    print(f"  OpenAI API Endpoint:   http://{args.host}:{args.port}/v1")
    print(f"  WebSocket Bridge:     ws://127.0.0.1:{args.port}/ws")
    if args.driver:
        print(f"  ChromeDriver Mode:    ACTIVE (CDP: {args.debugger_address})")
    print("================================================================")

    if args.driver:
        t = threading.Thread(target=start_driver_worker, args=(args,), daemon=True)
        t.start()

    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
