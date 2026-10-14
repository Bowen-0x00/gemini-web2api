#!/usr/bin/env python3
"""Entry point for running the Gemini Web2API reverse proxy."""

import argparse
import uvicorn

def main():
    parser = argparse.ArgumentParser(description="Gemini Web2API Reverse Proxy Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    print(f"================================================================")
    print(f"  Gemini Web2API Reverse Proxy Starting")
    print(f"  HTTP & WebSocket URL: http://{args.host}:{args.port}")
    print(f"  OpenAI API Endpoint:   http://{args.host}:{args.port}/v1")
    print(f"  WebSocket Bridge:     ws://127.0.0.1:{args.port}/ws")
    print(f"================================================================")

    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload)

if __name__ == "__main__":
    main()
