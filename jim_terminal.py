import os
import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager
import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "qwen2.5-coder:14b"
DB_FILE = "jim_memory.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                role TEXT,
                content TEXT
            )
        ''')
        conn.commit()

def save_memory(role: str, content: str):
    if not content.strip():
        return
    with get_db() as conn:
        conn.execute(
            "INSERT INTO history (timestamp, role, content) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), role, content)
        )
        conn.commit()

def fetch_memory_context(limit: int = 8):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM history WHERE role IN ('user', 'assistant') ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()[::-1]
    return [{"role": r[0], "content": r[1]} for r in rows]

init_db()

def tool_get_system_time() -> str:
    return f"System Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

def tool_list_desktop_files() -> str:
    desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
    try:
        files = os.listdir(desktop_path)[:15]
        return f"Desktop Files: {', '.join(files) if files else 'Empty'}"
    except Exception as e:
        return f"File error: {str(e)}"

def tool_scan_airspace_telemetry() -> str:
    simulated_contacts = [
        {"id": "TGT-01", "type": "Avian (Seagull)", "rcs_m2": 0.012, "rf_sig": "None", "threat_score": 0.04},
        {"id": "TGT-02", "type": "Quadcopter (5-inch FPV)", "rcs_m2": 0.035, "rf_sig": "5.8GHz VTX", "threat_score": 0.89, "est_intercept_cost_gbp": 210}
    ]
    return json.dumps({
        "sensor_status": "ONLINE (SIMULATED)",
        "active_contacts": simulated_contacts,
        "timestamp": datetime.now().strftime("%H:%M:%S")
    })

AVAILABLE_TOOLS = {
    "get_system_time": tool_get_system_time,
    "list_desktop_files": tool_list_desktop_files,
    "scan_airspace_telemetry": tool_scan_airspace_telemetry
}

OLLAMA_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_system_time",
            "description": "Get the current local system date and time.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_desktop_files",
            "description": "List files currently located on the user's Desktop directory.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scan_airspace_telemetry",
            "description": "Run a sensor-fusion airspace scan for drones, RF signals, radar contacts, and threat scores.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    }
]

JIM_SYSTEM_PROMPT = """
You are JIM, which stands for 'Just an Intelligent Machine'.
You are an independent, self-hosted, highly capable personal AI and tactical engineering assistant created by your user.
You are direct, sharp, tech-savvy, concise, and loyal.

CRITICAL BEHAVIORAL RULES:
1. ZERO FLUFF / ANSWER FIRST: Provide strictly the final numerical result, specification, or direct conclusion immediately.
2. MANDATORY FORMAT: You MUST append ', sir.' to the end of every single response without exception (e.g., '80%, sir.').
3. CRITICAL RULE: If the user asks multiple questions or requests multiple pieces of information in one prompt, you must call tools across turns until you have gathered ALL requested data. Do not give a final answer until every part of the user's request has been fulfilled.
4. Never invent sensor readings, times, or file names; rely strictly on tool outputs.
"""

def extract_fallback_tool_calls(text: str) -> list:
    found_calls = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                tool_name = obj.get("name")
                if tool_name in AVAILABLE_TOOLS:
                    found_calls.append({
                        "function": {
                            "name": tool_name,
                            "arguments": obj.get("arguments", {})
                        }
                    })
            except json.JSONDecodeError:
                pass
    return found_calls

def process_command(user_input: str):
    save_memory("user", user_input)
    history = fetch_memory_context(limit=8)
    messages = [{"role": "system", "content": JIM_SYSTEM_PROMPT}] + history
    tools_used = []

    try:
        for _ in range(3):
            res = requests.post(
                OLLAMA_URL,
                json={
                    "model": MODEL_NAME,
                    "messages": messages,
                    "tools": OLLAMA_TOOLS_SCHEMA,
                    "think": False,
                    "stream": False
                },
                timeout=45
            )
            res.raise_for_status()
            msg = res.json().get("message", {})
            content = msg.get("content", "").strip()
            
            tool_calls = msg.get("tool_calls") or extract_fallback_tool_calls(content)

            if not tool_calls:
                if not content.lower().endswith("sir."):
                    content += ", sir."
                save_memory("assistant", content)
                return content, tools_used

            # Append assistant message with tool calls to context
            messages.append({
                "role": "assistant",
                "content": content if not extract_fallback_tool_calls(content) else "",
                "tool_calls": tool_calls
            })

            # Execute every requested tool and feed back the result
            for call in tool_calls:
                fn_info = call.get("function", {})
                tool_name = fn_info.get("name")
                if tool_name in AVAILABLE_TOOLS:
                    tools_used.append(tool_name)
                    tool_output = AVAILABLE_TOOLS[tool_name]()
                    messages.append({
                        "role": "tool",
                        "tool_name": tool_name,
                        "content": str(tool_output)
                    })

        return "JIM Diagnostic: Max tool execution depth reached, sir.", tools_used

    except Exception as e:
        return f"JIM Diagnostic: Local engine unreachable ({str(e)}), sir.", tools_used

def main():
    print("=" * 60)
    print(" JIM 3.5 // LOCAL TERMINAL ENGINEERING WORKSTATION")
    print(" Status: ONLINE | Multi-Turn Execution Loop Active")
    print(" Type 'exit' or 'quit' to terminate session.")
    print("=" * 60 + "\n")

    while True:
        try:
            user_input = input(">> USER:\n> ").strip()
            if not user_input:
                continue
            if user_input.lower() in ["exit", "quit"]:
                print("\n>> JIM:\nShutting down core engine. Farewell, sir.")
                break

            print("\nProcessing...")
            response, tools_used = process_command(user_input)

            if tools_used:
                print(f"[⚙ Executed Tools: {', '.join(tools_used)}]")

            print(f"\n>> JIM:\n{response}\n" + "-" * 60)

        except KeyboardInterrupt:
            print("\n\n>> JIM:\nSession terminated by user interrupt. Goodbye, sir.")
            break
        except Exception as e:
            print(f"\n>> SYSTEM ERROR: {e}\n" + "-" * 60)

if __name__ == "__main__":
    main()
