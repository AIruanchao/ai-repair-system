#!/usr/bin/env python3
"""server.py — AI修复系统 FastAPI 服务化 v1.0

把散装脚本变成REST API微服务。
端口9100(MacMini上跟agentops-hub不冲突)

端点:
  GET  /api/health       — 健康检查
  GET  /api/stats        — 修复统计
  GET  /api/pitfalls     — pitfall知识库
  POST /api/repair       — 触发修复(异步)
  GET  /api/repair/{id}  — 查询修复状态
  GET  /api/config       — 当前配置
  POST /api/config       — 更新配置
  POST /api/benchmark    — 跑mini benchmark
"""
import os, sys, json, time, asyncio, sqlite3, hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

# 导入核心引擎
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))

app = FastAPI(
    title="AI全能修复系统",
    version="2.0.0",
    description="自主AI修复服务 — 定位bug→生成补丁→安全apply→防回归→自进化",
)

# CORS(允许Web Dashboard访问)


# 速率限制(PIT-RATE-001: 简单内存计数,60s内最多20次)
from collections import defaultdict
from time import time as _time
_rate_store = defaultdict(list)

@app.middleware("http")
async def rate_limit_middleware(request, call_next):
    if request.url.path.startswith("/api/repair"):
        client = request.client.host if request.client else "?"
        now = _time()
        _rate_store[client] = [t for t in _rate_store[client] if now - t < 60]
        if len(_rate_store[client]) >= 20:
            return JSONResponse(status_code=429, content={"detail": "Too many requests"})
        _rate_store[client].append(now)
    return await call_next(request)

# API认证(PIT-AUTH-001)
API_TOKEN = os.environ.get("AI_REPAIR_API_TOKEN", "")  # 空=不认证(本地工具)

@app.middleware("http")
async def auth_middleware(request, call_next):
    if API_TOKEN and request.url.path.startswith("/api/"):
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {API_TOKEN}":
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本机工具,不限源
    allow_methods=["*"],
    allow_headers=["*"],
)

# SQLite存储(替代/tmp散文件)
DB_PATH = os.path.expanduser("~/.hermes/ai-repair/repair.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS repairs (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            project TEXT NOT NULL,
            root_cause TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            result TEXT,
            elapsed REAL,
            files_changed TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# 配置
CONFIG_PATH = os.path.expanduser("~/.hermes/scripts/ai-repair-config.json")
def load_config():
    if os.path.exists(CONFIG_PATH):
        return json.loads(open(CONFIG_PATH).read())
    return {"version": "2.0.0", "projects": [], "limits": {}}

# === 数据模型 ===

class RepairRequest(BaseModel):
    project: str = None  # 项目目录(不传则用默认)
    root_cause: str  # 根因描述
    model: str = "glm-5.2"
    dry_run: bool = False
    mode: str = "auto"  # auto|openhands|patch|recursive

class ConfigUpdate(BaseModel):
    projects: Optional[list] = None
    limits: Optional[dict] = None

# === API端点 ===

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Web Dashboard"""
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    if os.path.exists(dashboard_path):
        return FileResponse(dashboard_path)
    return HTMLResponse("<h1>dashboard.html not found</h1>")

@app.get("/api/health")
async def health():
    """健康检查"""
    return {
        "status": "ok",
        "version": "2.0.0",
        "uptime": time.time(),
        "pitfall_count": _count_pitfalls(),
    }

@app.get("/api/stats")
async def get_stats():
    """修复统计"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 修复历史
    c.execute("SELECT COUNT(*) FROM repairs")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM repairs WHERE status='success'")
    success = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM repairs WHERE status='failed'")
    failed = c.fetchone()[0]
    
    # 最近10次
    c.execute("SELECT id, ts, project, status, elapsed FROM repairs ORDER BY ts DESC LIMIT 10")
    recent = [{"id": r[0], "ts": r[1], "project": r[2], "status": r[3], "elapsed": r[4]} for r in c.fetchall()]
    
    conn.close()
    
    # /tmp stats文件
    stats_file = "/tmp/ai-repair-stats.json"
    runner_stats = {}
    if os.path.exists(stats_file):
        try:
            runner_stats = json.loads(open(stats_file).read())
        except: pass
    
    return {
        "total_repairs": total,
        "success": success,
        "failed": failed,
        "success_rate": f"{success*100//max(total,1)}%",
        "recent": recent,
        "runner_stats": {
            "total_scanned": runner_stats.get("total_scanned", 0),
            "total_fixed": runner_stats.get("total_fixed", 0),
            "manual_tests": len(runner_stats.get("manual_tests", [])),
        },
        "pitfall_count": _count_pitfalls(),
    }

@app.get("/api/pitfalls")
async def get_pitfalls(limit: int = 20, quality: str = "all"):
    """pitfall知识库"""
    pitfall_path = os.path.expanduser(
        "~/.hermes/profiles/dachui80/skills/core/ceiling-gate/scripts/pitfalls.jsonl")
    if not os.path.exists(pitfall_path):
        return {"pitfalls": [], "total": 0}
    
    lines = open(pitfall_path).readlines()
    pitfalls = []
    for line in lines[-limit*2:]:  # 多读然后过滤
        try:
            entry = json.loads(line)
            if quality == "good" and entry.get("root_cause") == "unknown":
                continue  # 过滤垃圾
            pitfalls.append(entry)
            if len(pitfalls) >= limit:
                break
        except: continue
    
    return {
        "pitfalls": pitfalls,
        "total": len(lines),
        "filtered": len(pitfalls),
    }

@app.post("/api/repair")
async def trigger_repair(req: RepairRequest, bg: BackgroundTasks):
    """触发修复(异步)"""
    config = load_config()
    project = req.project or config["projects"][0]["dir"] if config.get("projects") else None
    if not project:
        raise HTTPException(400, "未指定项目目录")
    
    # 生成任务ID
    task_id = f"R{hashlib.md5(f'{project}{req.root_cause}{time.time()}'.encode()).hexdigest()[:8]}"
    
    # 写DB
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO repairs (id, ts, project, root_cause, status) VALUES (?, ?, ?, ?, 'pending')",
              (task_id, datetime.now().isoformat(), project, req.root_cause))
    conn.commit()
    conn.close()
    
    # 异步执行修复
    bg.add_task(_execute_repair, task_id, project, req.root_cause, req.model)
    
    return {"task_id": task_id, "status": "pending", "message": "修复已提交"}

@app.get("/api/repair/{task_id}")
async def get_repair_status(task_id: str):
    """查询修复状态"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM repairs WHERE id=?", (task_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "修复任务不存在")
    
    return {
        "id": row[0], "ts": row[1], "project": row[2],
        "root_cause": row[3], "status": row[4],
        "result": json.loads(row[5]) if row[5] else None,
        "elapsed": row[6], "files_changed": json.loads(row[7]) if row[7] else [],
    }

@app.get("/api/config")
async def get_config():
    return load_config()

@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    config = load_config()
    if update.projects is not None:
        config["projects"] = update.projects
    if update.limits is not None:
        config["limits"] = update.limits
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return {"status": "updated", "config": config}

@app.post("/api/benchmark")
async def run_benchmark(bg: BackgroundTasks):
    """异步跑benchmark"""
    task_id = f"B{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"
    bg.add_task(_execute_benchmark, task_id)
    return {"task_id": task_id, "status": "benchmark running"}

# === 后台任务 ===

def _count_pitfalls():
    pitfall_path = os.path.expanduser(
        "~/.hermes/profiles/dachui80/skills/core/ceiling-gate/scripts/pitfalls.jsonl")
    if not os.path.exists(pitfall_path):
        return 0
    return len(open(pitfall_path).readlines())

def _execute_repair(task_id: str, project: str, root_cause: str, model: str):
    """执行修复(后台) — 自动选择OpenHands(本地)或ai_auto_repair(远程)"""
    import subprocess
    env = os.environ.copy()
    # 读API key
    env_path = os.path.expanduser("~/.hermes/profiles/dachui80/.env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("NEWAPI_TOKEN="):
                env["NEWAPI_KEY"] = line.split("=", 1)[1].strip()
                break
    
    t0 = time.time()
    
    # PIT-ENGINE-001: 自动选择修复引擎
    is_remote = ":" in project and not project.startswith("/")
    
    if not is_remote and os.path.exists("/Users/maccc/.hermes/openhands-venv/bin/python3"):
        # 本地项目 → OpenHands Agent(CodeAct模式,更强)
        print(f"[repair] 使用OpenHands Agent(CodeAct) for {project}")
        r = _execute_openhands(task_id, project, root_cause, model, env)
    else:
        # 远程项目或OpenHands不可用 → ai_auto_repair
        print(f"[repair] 使用ai_auto_repair for {project}")
        r = subprocess.run(
            ["python3", os.path.expanduser("~/.hermes/scripts/ai_auto_repair.py"),
             "--root-cause", root_cause, "--project", project, "--model", model],
            capture_output=True, text=True, timeout=300, env=env)
        r.stdout = r.stdout  # 保持格式一致
    
    elapsed = round(time.time() - t0, 1)
    success = "RESULT: success" in r.stdout or "成功: True" in r.stdout
    
    # 解析改了哪些文件
    files = []
    for line in r.stdout.split('\n'):
        if 'file:' in line.lower() or '文件:' in line or 'changed' in line.lower():
            files.append(line.strip()[:100])
    
    # 更新DB
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE repairs SET status=?, result=?, elapsed=?, files_changed=? WHERE id=?",
              ("success" if success else "failed",
               json.dumps({"stdout": r.stdout[-500:], "engine": "openhands" if not is_remote else "ai_auto_repair"}),
               elapsed,
               json.dumps(files),
               task_id))
    conn.commit()
    conn.close()

def _execute_openhands(task_id, project, root_cause, model, env):
    """用OpenHands Agent执行修复(CodeAct模式)"""
    import tempfile, subprocess
    script = f'''
import os, sys
sys.path.insert(0, "/Users/maccc/.hermes/openhands-venv/lib/python3.12/site-packages")
from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool
from pydantic import SecretStr

key = os.environ.get("NEWAPI_KEY", "")
if not key:
    for line in open(os.path.expanduser("~/.hermes/profiles/dachui80/.env")):
        if line.startswith("NEWAPI_TOKEN="):
            key = line.split("=",1)[1].strip()

llm = LLM(model="openai/{model}", base_url="https://ai.nenie.vip/v1",
          api_key=SecretStr(key), api_mode="chat")
agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)])
conv = Conversation(agent=agent, workspace="{project}")
conv.send_message("Fix this bug: " + root_cause + ". Read the file, understand the code, then fix it. After fixing, run python3 -m py_compile to verify syntax. If a test exists, run it. If it fails, read the error and try a different approach. Max 5 rounds. Do not git commit.")
try:
    conv.run()
    print("RESULT: success")
except Exception as e:
    print(f"RESULT: error - {{str(e)[:100]}}")
'''
    fd, spath = tempfile.mkstemp(suffix='.py')
    os.close(fd)
    with open(spath, 'w') as f:
        f.write(script)
    
    try:
        r = subprocess.run(
            ["/Users/maccc/.hermes/openhands-venv/bin/python3", spath],
            capture_output=True, text=True, timeout=300, env=env, cwd=project)
        return r
    finally:
        os.unlink(spath)

def _execute_benchmark(task_id: str):
    """执行benchmark(后台)"""
    import subprocess
    env = os.environ.copy()
    env_path = os.path.expanduser("~/.hermes/profiles/dachui80/.env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("NEWAPI_TOKEN="):
                env["NEWAPI_KEY"] = line.split("=", 1)[1].strip()
                break
    
    r = subprocess.run(
        ["python3", os.path.expanduser("~/.hermes/scripts/mini_benchmark.py")],
        capture_output=True, text=True, timeout=600, env=env)
    
    # 写结果到stats
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO stats (key, value) VALUES (?, ?)",
              ("last_benchmark", json.dumps({"ts": datetime.now().isoformat(), "output": r.stdout[-1000:]})))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    # 集成deploy_monitor路由(部署+回滚+监控)
    try:
        from deploy_monitor import router as deploy_router
        app.include_router(deploy_router)
        print("✅ deploy_monitor路由已集成")
    except Exception as e:
        print(f"⚠️ deploy_monitor集成失败: {e}")
    
    import uvicorn
    print(f"=== AI全能修复系统 v6.3.0 ===")
    print(f"Dashboard: http://localhost:9100")
    print(f"API Docs: http://localhost:9100/docs")
    uvicorn.run(app, host="127.0.0.1", port=9100)
