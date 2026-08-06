#!/usr/bin/env python3
"""deploy_monitor.py — 部署+监控+回滚模块 (集成到AI修复系统 :9100)

功能:
  1. DEPLOY_CONFIG    — 多项目部署配置(HT=cloud4 / ERP=MacMini)
  2. deploy(name)     — git tag打标→SSH部署→健康检查×3→失败自动回滚→飞书通知
  3. rollback(name,t) — git checkout回滚→健康检查→飞书通知
  4. monitor(name)    — 采集P95/错误率/QPS→阈值告警→飞书
  5. FastAPI路由     — POST /api/deploy, POST /api/rollback, GET /api/monitor/{project}

约束:
  - 不引入新依赖(fastapi/urllib/subprocess均为stdlib或既有)
  - SSH用subprocess(无fabric/paramiko)
  - 飞书用urllib webhook(无requests)

使用:
  # 方式1: 集成到server.py(推荐 — 不开新端口)
  from deploy_monitor import router as deploy_router
  app.include_router(deploy_router)

  # 方式2: 独立运行测试
  python3 deploy_monitor.py  # 启 :9101
"""
import os
import sys
import json
import time
import subprocess
import threading
import urllib.request
import urllib.parse
import urllib.error
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any

# FastAPI — 既有依赖
from fastapi import FastAPI, APIRouter, HTTPException
from pydantic import BaseModel

# === 配置 ===

# 多项目部署配置
# - type=local : 本机操作(project_dir即为本地路径,exec_local=True)
# - type=ssh   : 走subprocess+SSH(cloud4=124.222.234.8)
# - gateway=ssh  : 走跳板(MacMini经Studio跳板到10.31.1.177)
DEPLOY_CONFIG: Dict[str, Dict[str, Any]] = {
    "ht": {
        "type": "ssh",
        "ssh_host": "root@124.222.234.8",
        "project_dir": "/opt/doc-generator",
        "deploy_cmd": "cd /opt/doc-generator && git pull && systemctl restart business-docs",
        "health_url": "http://localhost:8600/health",
        "service": "business-docs",
        "branch": "main",
    },
    "erp": {
        "type": "gateway",
        "ssh_host": "mac@10.31.1.177",  # 经Studio跳板
        "project_dir": "/opt/qisemi-erp",
        "deploy_cmd": "cd /opt/qisemi-erp && git pull && pm2 restart qisemi-erp",
        "health_url": "http://localhost:3000/api/health",
        "service": "qisemi-erp",
        "branch": "main",
    },
}

# 阈值(可被环境变量覆盖)
P95_THRESHOLD_MS = float(os.environ.get("DEPLOY_P95_MS", "500"))
ERROR_RATE_THRESHOLD = float(os.environ.get("DEPLOY_ERROR_RATE", "0.05"))
QPS_THRESHOLD = float(os.environ.get("DEPLOY_QPS_WARN", "100"))  # QPS基线告警

# 健康检查参数
HEALTH_CHECK_RETRIES = 3
HEALTH_CHECK_INTERVAL = 5  # 秒

# 飞书webhook
FEISHU_WEBHOOK = os.environ.get("REPAIR_FEISHU_WEBHOOK", "")
FEISHU_SECRET = os.environ.get("REPAIR_FEISHU_SECRET", "")  # 可选签名

# NewAPI key(从~/.hermes/profiles/dachui80/.env读NEWAPI_TOKEN)
DACHUI_ENV_PATH = os.path.expanduser("~/.hermes/profiles/dachui80/.env")

# 部署历史(内存,够本地监控用 — 重启清空)
DEPLOY_HISTORY: deque = deque(maxlen=200)
MONITOR_HISTORY: Dict[str, deque] = {}  # {project: [(ts, p95, err, qps), ...]}


# === 工具函数 ===

def _load_newapi_token() -> str:
    """从dachui80 profile env读NEWAPI_TOKEN"""
    if not os.path.exists(DACHUI_ENV_PATH):
        return ""
    try:
        with open(DACHUI_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("NEWAPI_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _run_ssh(ssh_target: str, command: str, timeout: int = 120) -> Dict[str, Any]:
    """subprocess跑SSH(免fabric/paramiko)。返回{ok, stdout, stderr, exit_code}"""
    full_cmd = ["ssh", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", ssh_target, command]
    try:
        r = subprocess.run(
            full_cmd, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "ok": r.returncode == 0,
            "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
            "exit_code": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"SSH timeout ({timeout}s)", "exit_code": -1}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": f"SSH error: {e}", "exit_code": -1}


def _run_local(command: str, cwd: Optional[str] = None, timeout: int = 120) -> Dict[str, Any]:
    """本地shell — type=local时用"""
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        return {
            "ok": r.returncode == 0,
            "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
            "exit_code": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"Local timeout ({timeout}s)", "exit_code": -1}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": f"Local error: {e}", "exit_code": -1}


def _execute_on_target(project: str, command: str, timeout: int = 120) -> Dict[str, Any]:
    """根据type选择本地或SSH执行"""
    cfg = DEPLOY_CONFIG[project]
    if cfg["type"] == "local":
        return _run_local(command, cwd=cfg.get("project_dir"), timeout=timeout)
    elif cfg["type"] in ("ssh", "gateway"):
        return _run_ssh(cfg["ssh_host"], command, timeout=timeout)
    else:
        return {"ok": False, "stdout": "", "stderr": f"unknown type: {cfg['type']}", "exit_code": -1}


def _health_check(project: str, retries: int = HEALTH_CHECK_RETRIES) -> bool:
    """健康检查(本地+远程URL都走urllib)"""
    cfg = DEPLOY_CONFIG[project]
    url = cfg.get("health_url", "")
    if not url:
        return True  # 没配就放行
    # MacMini本地项目走本机;远程项目走SSH curl
    if cfg["type"] in ("ssh", "gateway"):
        check_cmd = f"curl -sf --max-time 5 {url}"
        for i in range(retries):
            r = _run_ssh(cfg["ssh_host"], check_cmd, timeout=10)
            if r["ok"]:
                return True
            time.sleep(HEALTH_CHECK_INTERVAL)
        return False
    else:
        for i in range(retries):
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except Exception:
                pass
            time.sleep(HEALTH_CHECK_INTERVAL)
        return False


def _feishu_notify(message: str, level: str = "info") -> bool:
    """飞书webhook通知。level=info/warn/error"""
    if not FEISHU_WEBHOOK:
        # 没配webhook,降级到print
        print(f"[feishu:{level}] {message}")
        return True
    color = {"info": "blue", "warn": "orange", "error": "red"}.get(level, "blue")
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"AI修复系统 [{level.upper()}]"},
                "template": color,
            },
            "elements": [
                {"tag": "markdown", "content": message[:4000]},
            ],
        },
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            FEISHU_WEBHOOK, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[feishu] notify failed: {e}", file=sys.stderr)
        return False


def _git_tag(project: str, tag: str) -> Dict[str, Any]:
    """git打tag(在远程项目上)"""
    cfg = DEPLOY_CONFIG[project]
    project_dir = cfg.get("project_dir", "")
    if not project_dir:
        return {"ok": False, "error": "no project_dir"}
    cmd = f"cd {project_dir} && git tag -f {tag} && git push origin {tag} --force"
    return _execute_on_target(project, cmd, timeout=30)


def _git_current_tag(project: str) -> str:
    """读当前deploy用的tag(优先)"""
    cfg = DEPLOY_CONFIG[project]
    project_dir = cfg.get("project_dir", "")
    if not project_dir:
        return ""
    r = _execute_on_target(project, f"cd {project_dir} && git describe --tags --abbrev=0 2>/dev/null", timeout=15)
    return r.get("stdout", "").strip() if r.get("ok") else ""


def _git_rollback(project: str, tag: str) -> Dict[str, Any]:
    """git checkout到指定tag"""
    cfg = DEPLOY_CONFIG[project]
    project_dir = cfg.get("project_dir", "")
    if not project_dir:
        return {"ok": False, "error": "no project_dir"}
    cmd = f"cd {project_dir} && git fetch --tags && git checkout {tag} && git pull"
    return _execute_on_target(project, cmd, timeout=60)


# === 核心功能函数 ===

def deploy(project: str, tag: Optional[str] = None) -> Dict[str, Any]:
    """部署:打tag→SSH部署→健康检查3次→失败自动回滚→飞书通知

    Returns: {ok, tag, steps: [...], error?}
    """
    if project not in DEPLOY_CONFIG:
        return {"ok": False, "error": f"unknown project: {project}"}
    cfg = DEPLOY_CONFIG[project]
    steps: List[Dict[str, Any]] = []
    rollback_tag = _git_current_tag(project)  # 记下当前tag,失败时回滚
    if not tag:
        tag = f"deploy-{int(time.time())}"

    # 1. 打tag
    t0 = time.time()
    r = _git_tag(project, tag)
    steps.append({"step": "tag", "ok": r.get("ok"), "detail": r.get("stdout") or r.get("stderr"), "elapsed": round(time.time() - t0, 1)})
    if not r.get("ok"):
        _feishu_notify(f"❌ 部署失败 [{project}] — git tag打标失败\n{r.get('stderr', '')}", "error")
        DEPLOY_HISTORY.append({"ts": _now(), "project": project, "ok": False, "step": "tag", "tag": tag})
        return {"ok": False, "tag": tag, "steps": steps, "error": "tag_failed"}

    # 2. SSH部署
    t0 = time.time()
    r = _execute_on_target(project, cfg["deploy_cmd"], timeout=180)
    steps.append({"step": "deploy", "ok": r.get("ok"), "detail": r.get("stdout") or r.get("stderr"), "elapsed": round(time.time() - t0, 1)})
    if not r.get("ok"):
        # 部署命令失败,尝试回滚到上一个tag
        if rollback_tag:
            _git_rollback(project, rollback_tag)
        _feishu_notify(f"❌ 部署失败 [{project}] — SSH部署命令失败\n{r.get('stderr', '')}", "error")
        DEPLOY_HISTORY.append({"ts": _now(), "project": project, "ok": False, "step": "deploy", "tag": tag})
        return {"ok": False, "tag": tag, "steps": steps, "error": "deploy_failed", "rolled_back_to": rollback_tag}

    # 3. 健康检查 ×3
    t0 = time.time()
    health_ok = _health_check(project, retries=HEALTH_CHECK_RETRIES)
    steps.append({"step": "health_check", "ok": health_ok, "retries": HEALTH_CHECK_RETRIES, "elapsed": round(time.time() - t0, 1)})
    if not health_ok:
        # 健康检查失败,自动回滚
        rollback_result = None
        if rollback_tag:
            rollback_result = _git_rollback(project, rollback_tag)
        _feishu_notify(
            f"❌ 部署失败 [{project}] — 健康检查{HEALTH_CHECK_RETRIES}次未通过,已自动回滚到 {rollback_tag or 'N/A'}\n请人工介入!",
            "error",
        )
        DEPLOY_HISTORY.append({"ts": _now(), "project": project, "ok": False, "step": "health_check", "tag": tag, "rolled_back_to": rollback_tag})
        return {"ok": False, "tag": tag, "steps": steps, "error": "health_check_failed", "rolled_back_to": rollback_tag, "rollback_result": rollback_result}

    # 4. 成功通知
    _feishu_notify(
        f"✅ 部署成功 [{project}] — tag={tag}\n步骤: {' → '.join(s['step'] for s in steps)}\n总耗时: {sum(s['elapsed'] for s in steps):.1f}s",
        "info",
    )
    DEPLOY_HISTORY.append({"ts": _now(), "project": project, "ok": True, "tag": tag})
    return {"ok": True, "tag": tag, "steps": steps}


def rollback(project: str, tag: Optional[str] = None) -> Dict[str, Any]:
    """回滚:git checkout到tag→健康检查→飞书通知"""
    if project not in DEPLOY_CONFIG:
        return {"ok": False, "error": f"unknown project: {project}"}
    if not tag:
        # 不指定tag,回滚到上一个
        tag = _git_previous_tag(project)
        if not tag:
            return {"ok": False, "error": "no_previous_tag_and_none_specified"}

    steps: List[Dict[str, Any]] = []

    # 1. git checkout
    t0 = time.time()
    r = _git_rollback(project, tag)
    steps.append({"step": "checkout", "ok": r.get("ok"), "detail": r.get("stdout") or r.get("stderr"), "elapsed": round(time.time() - t0, 1)})
    if not r.get("ok"):
        _feishu_notify(f"❌ 回滚失败 [{project}] — git checkout {tag} 失败\n{r.get('stderr', '')}", "error")
        DEPLOY_HISTORY.append({"ts": _now(), "project": project, "ok": False, "step": "rollback", "tag": tag})
        return {"ok": False, "tag": tag, "steps": steps, "error": "checkout_failed"}

    # 2. 重启服务(走deploy_cmd不含git pull的精简版 — 重新跑一次让服务加载)
    cfg = DEPLOY_CONFIG[project]
    service = cfg.get("service", "")
    if service:
        if cfg["type"] == "local":
            restart_cmd = f"cd {cfg['project_dir']} && (systemctl restart {service} || pm2 restart {service} || true)"
        else:
            restart_cmd = f"cd {cfg['project_dir']} && (systemctl restart {service} || pm2 restart {service} || true)"
        t0 = time.time()
        r = _execute_on_target(project, restart_cmd, timeout=60)
        steps.append({"step": "restart", "ok": True, "detail": r.get("stdout") or r.get("stderr"), "elapsed": round(time.time() - t0, 1)})

    # 3. 健康检查
    t0 = time.time()
    health_ok = _health_check(project, retries=HEALTH_CHECK_RETRIES)
    steps.append({"step": "health_check", "ok": health_ok, "elapsed": round(time.time() - t0, 1)})
    if not health_ok:
        _feishu_notify(f"⚠️ 回滚后健康检查未通过 [{project}] — tag={tag}\n请人工验证!", "warn")
        DEPLOY_HISTORY.append({"ts": _now(), "project": project, "ok": False, "step": "rollback_health", "tag": tag})
        return {"ok": False, "tag": tag, "steps": steps, "error": "health_check_failed_after_rollback"}

    _feishu_notify(
        f"🔄 回滚成功 [{project}] — tag={tag}\n步骤: {' → '.join(s['step'] for s in steps)}",
        "info",
    )
    DEPLOY_HISTORY.append({"ts": _now(), "project": project, "ok": True, "step": "rollback", "tag": tag})
    return {"ok": True, "tag": tag, "steps": steps}


def _git_previous_tag(project: str) -> str:
    """取上一个deploy-* tag(避免回滚HEAD~1这种可能不在tag里的情况)"""
    cfg = DEPLOY_CONFIG[project]
    project_dir = cfg.get("project_dir", "")
    if not project_dir:
        return ""
    r = _execute_on_target(
        project,
        f"cd {project_dir} && git tag --sort=-creatordate | grep '^deploy-' | sed -n '2p'",
        timeout=15,
    )
    return r.get("stdout", "").strip() if r.get("ok") else ""


def monitor(project: str, window: int = 100) -> Dict[str, Any]:
    """监控:采样P95/错误率/QPS — 阈值告警

    Strategy: 拉取项目health URL的最近N次请求耗时,计算P95/错误率。
    实际生产中应该从access log/Nginx/应用stats取,这里用
    health endpoint做"金丝雀"采样 — 真实业务指标应通过PIT-DEPLOY-MON扩展。
    """
    if project not in DEPLOY_CONFIG:
        return {"ok": False, "error": f"unknown project: {project}"}
    cfg = DEPLOY_CONFIG[project]
    url = cfg.get("health_url", "")
    if not url:
        return {"ok": False, "error": "no health_url configured"}

    # 在目标机上采样health endpoint(N次)
    samples = max(10, min(window, 50))
    latencies: List[float] = []
    errors = 0
    qps_start = time.time()

    if cfg["type"] in ("ssh", "gateway"):
        # 远程:在目标机上跑一个紧凑循环,只统计耗时
        sample_cmd = (
            f"for i in $(seq 1 {samples}); do "
            f"  t=$(curl -sf -o /dev/null -w '%{{http_code}}:%{{time_total}}' --max-time 3 {url} 2>/dev/null); "
            f"  echo \"$t\"; done"
        )
        r = _execute_on_target(project, sample_cmd, timeout=60)
        elapsed_total = time.time() - qps_start
        if not r.get("ok"):
            _feishu_notify(f"⚠️ 监控采样失败 [{project}] — {r.get('stderr', '')}", "warn")
            return {"ok": False, "error": "ssh_sample_failed", "detail": r.get("stderr", "")}
        for line in r.get("stdout", "").splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            code, t = line.split(":", 1)
            try:
                latencies.append(float(t) * 1000)  # → ms
                if not code.startswith("2"):
                    errors += 1
            except ValueError:
                continue
    else:
        # 本机:直接urllib采样
        elapsed_total = time.time() - qps_start
        for i in range(samples):
            t0 = time.time()
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    latencies.append((time.time() - t0) * 1000)
                    if not (200 <= resp.status < 300):
                        errors += 1
            except Exception:
                errors += 1
            time.sleep(0.05)

    if not latencies:
        return {"ok": False, "error": "no_samples_collected"}

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)] if len(latencies) >= 2 else latencies[-1]
    p99 = latencies[int(len(latencies) * 0.99)] if len(latencies) >= 5 else latencies[-1]
    error_rate = errors / samples
    qps = samples / max(elapsed_total, 0.001)

    snapshot = {
        "ts": _now(),
        "project": project,
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "p99_ms": round(p99, 1),
        "error_rate": round(error_rate, 4),
        "qps": round(qps, 2),
        "samples": samples,
    }

    # 记录历史
    MONITOR_HISTORY.setdefault(project, deque(maxlen=200)).append(snapshot)

    # 阈值告警
    alerts: List[str] = []
    if p95 > P95_THRESHOLD_MS:
        alerts.append(f"P95={p95:.1f}ms > {P95_THRESHOLD_MS}ms阈值")
    if error_rate > ERROR_RATE_THRESHOLD:
        alerts.append(f"错误率={error_rate*100:.1f}% > {ERROR_RATE_THRESHOLD*100:.1f}%阈值")
    if alerts:
        msg = f"🚨 监控告警 [{project}]\n" + "\n".join(alerts) + f"\n\n详情:\n```\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}\n```"
        _feishu_notify(msg, "error")
        snapshot["alerts"] = alerts
    else:
        snapshot["alerts"] = []

    return {"ok": True, "snapshot": snapshot, "thresholds": {
        "p95_ms": P95_THRESHOLD_MS, "error_rate": ERROR_RATE_THRESHOLD, "qps_warn": QPS_THRESHOLD,
    }}


# === FastAPI路由 ===

router = APIRouter(prefix="/api", tags=["deploy"])


class DeployRequest(BaseModel):
    project: str
    tag: Optional[str] = None


class RollbackRequest(BaseModel):
    project: str
    tag: Optional[str] = None


@router.post("/deploy")
async def api_deploy(req: DeployRequest):
    """POST /api/deploy  触发部署(异步后台跑)"""
    if req.project not in DEPLOY_CONFIG:
        raise HTTPException(400, f"unknown project: {req.project}. available: {list(DEPLOY_CONFIG.keys())}")

    result_holder: Dict[str, Any] = {"status": "running"}

    def _bg():
        try:
            r = deploy(req.project, tag=req.tag)
            result_holder.update(r)
            result_holder["status"] = "completed"
        except Exception as e:
            result_holder["status"] = "error"
            result_holder["error"] = str(e)

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return {"status": "started", "project": req.project, "tag": req.tag or f"deploy-{int(time.time())}"}


@router.post("/rollback")
async def api_rollback(req: RollbackRequest):
    """POST /api/rollback  触发回滚(同步)"""
    if req.project not in DEPLOY_CONFIG:
        raise HTTPException(400, f"unknown project: {req.project}. available: {list(DEPLOY_CONFIG.keys())}")
    r = rollback(req.project, tag=req.tag)
    return r


@router.get("/monitor/{project}")
async def api_monitor(project: str, window: int = 100):
    """GET /api/monitor/{project}  采集一次指标 + 阈值告警"""
    if project not in DEPLOY_CONFIG:
        raise HTTPException(400, f"unknown project: {project}. available: {list(DEPLOY_CONFIG.keys())}")
    r = monitor(project, window=window)
    return r


@router.get("/deploy/history")
async def api_deploy_history(project: Optional[str] = None, limit: int = 50):
    """GET /api/deploy/history  部署历史"""
    items = list(DEPLOY_HISTORY)
    if project:
        items = [i for i in items if i.get("project") == project]
    return {"history": items[-limit:], "total": len(items)}


@router.get("/deploy/config")
async def api_deploy_config():
    """GET /api/deploy/config  当前部署配置(脱敏 — 不含ssh_host凭据)"""
    safe = {}
    for k, v in DEPLOY_CONFIG.items():
        safe[k] = {**v, "ssh_host": "<redacted>" if v.get("ssh_host") else ""}
    return {"config": safe, "projects": list(DEPLOY_CONFIG.keys())}


# === 独立运行(测试用) ===

app = FastAPI(title="Deploy Monitor Module", version="1.0")
app.include_router(router)


@app.get("/api/deploy/_health")
async def _module_health():
    return {
        "status": "ok",
        "module": "deploy_monitor",
        "projects": list(DEPLOY_CONFIG.keys()),
        "feishu_configured": bool(FEISHU_WEBHOOK),
        "newapi_token_loaded": bool(_load_newapi_token()),
    }


if __name__ == "__main__":
    import uvicorn
    print("=== Deploy Monitor Module (standalone :9101) ===")
    print(f"Projects: {list(DEPLOY_CONFIG.keys())}")
    print(f"Feishu webhook: {'configured' if FEISHU_WEBHOOK else 'NOT configured (will print to stdout)'}")
    print(f"NewAPI token: {'loaded' if _load_newapi_token() else 'NOT found in ' + DACHUI_ENV_PATH}")
    uvicorn.run(app, host="127.0.0.1", port=9101)
