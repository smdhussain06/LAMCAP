import json
import sqlite3
import requests
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

DB_PATH = os.path.join(os.getcwd(), 'lamcap.db')

def get_gh_token():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("SELECT value FROM app_settings WHERE key = 'gh_token'")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except:
        return None

def get_copilot_token(gh_token):
    res = requests.get(
        "https://api.github.com/copilot_internal/v2/token",
        headers={
            "Authorization": f"token {gh_token}",
            "Accept": "application/json",
            "User-Agent": "GitHubCopilotChat/0.11.1",
            "Editor-Version": "vscode/1.85.0",
            "Editor-Plugin-Version": "copilot-chat/0.11.1",
        }
    )
    if res.status_code == 200:
        return res.json().get("token")
    print(f"Token Error: {res.text}")
    return None

class CopilotProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            req = json.loads(post_data.decode('utf-8'))
        except:
            self.send_error(400, "Invalid JSON payload mapping from Anthropic to Copilot")
            return

        gh_token = get_gh_token()
        if not gh_token:
            self.send_error(401, "No GitHub Device token found. Run Option 3 in LAMCAP first.")
            return

        copilot_token = get_copilot_token(gh_token)
        if not copilot_token:
            self.send_error(401, "Failed to authenticate Internal JWT via GitHub Copilot")
            return

        print("\n⚡ [Copilot Proxy] Active intercept. Contacting Microsoft/GitHub servers for real inference...")

        # Transpile Anthropic format -> OpenAI format for Copilot Endpoint
        openai_msgs = []
        if "system" in req and req["system"]:
            openai_msgs.append({"role": "system", "content": req["system"]})
        
        for msg in req.get("messages", []):
            openai_msgs.append({"role": msg["role"], "content": msg["content"]})
            
        openai_req = {
            "model": "gpt-4",  # We force Github Copilot's preferred turbo router
            "messages": openai_msgs,
            "stream": req.get("stream", False),
            "temperature": 0.1
        }

        # Issue native Copilot Query
        res = requests.post(
            "https://api.githubcopilot.com/chat/completions",
            headers={
                "Authorization": f"Bearer {copilot_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.85.0"
            },
            json=openai_req,
            stream=openai_req["stream"]
        )

        if res.status_code != 200:
            self.send_error(500, f"Copilot Internal Server Error: {res.text}")
            return

        if openai_req["stream"]:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            
            for line in res.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            # Map OpenAI Server-Sent Events to Anthropic Server-Sent Events dynamically!
                            data = json.loads(line[6:])
                            content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content:
                                anthropic_chunk = {
                                    "type": "content_block_delta",
                                    "delta": {"type": "text_delta", "text": content}
                                }
                                self.wfile.write(f"event: content_block_delta\ndata: {json.dumps(anthropic_chunk)}\n\n".encode('utf-8'))
                                self.wfile.flush()
                        except: pass
            
            self.wfile.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
            self.wfile.flush()
            print("✓ [Copilot Proxy] Successfully streamed live generated code to LAMCAP Executor!")
            return

        out_data = res.json()
        generated_text = out_data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Map response object smoothly back entirely mimicking Anthropic formats back to LAMCAP Planner
        anthropic_res = {
            "id": "msg_proxy123",
            "type": "message",
            "role": "assistant",
            "model": req.get("model", "gpt-4.1"),
            "content": [
                {
                    "type": "text",
                    "text": generated_text
                }
            ]
        }

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(anthropic_res).encode('utf-8'))
        print("✓ [Copilot Proxy] Successfully transmitted live generated code to LAMCAP Executor!")

if __name__ == '__main__':
    server = HTTPServer(('localhost', 4141), CopilotProxyHandler)
    print("🚀 [Copilot Proxy] Server online! Listening on http://localhost:4141 and bridging active connection directly to GitHub Copilot's Neural Net...")
    server.serve_forever()
