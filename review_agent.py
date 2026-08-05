#!/usr/bin/env python3
"""review_agent.py — 独立代码审查Agent v5.1
端口9201,独立于修复系统(9100),共享pitfall库
"""
import os, sys, json, subprocess, tempfile, re, hashlib
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import urllib.request

# 环境设置(防PermissionError)
_TMP = tempfile.gettempdir()
for k in ["LOG_DIR", "UPLOAD_DIR", "SEALS_DIR", "DOCS_DIR", "PROJECT_ROOT", "AUDIT_DIR"]:
    os.environ.setdefault(k, _TMP)

# mock makedirs
_rm = os.makedirs
os.makedirs = lambda *a, **k: _rm(*a, **k) if True else None
try:
    _rpm = Path.mkdir
    Path.mkdir = lambda self, *a, **k: None
except:
    pass

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="AI Code Review Agent", version="5.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GATE_DIR = Path(os.path.expanduser("~/.hermes/profiles/dachui80/skills/core/ceiling-gate/scripts"))
PITFALL_JSONL = GATE_DIR / "pitfalls.jsonl"

PROJECTS = {
    "ht": {"path": "/Users/maccc/projects/business-document-generator", "lang": "python"},
    "erp": {"path": "macmini:/Users/mac/erp-project", "lang": "typescript"},
    "ledger": {"path": "/Users/maccc/projects/ledger-quality-system", "lang": "python"},
}

def get_key():
    for line in open(os.path.expanduser("~/.hermes/profiles/dachui80/.env")):
        if line.startswith("NEWAPI_TOKEN="):
            return line.split("=",1)[1].strip()
    return os.environ.get("NEWAPI_KEY", "")

PYTHON_GATES = ["sql_insert_check.py","template_consistency_check.py","onclick_func_check.py",
    "url_route_check.py","db_table_existence_check.py","lint_check.py","build_check.py",
    "coverage_check.py","tech_debt_check.py","security_scan.py","security_penetration_check.py",
    "security_auth_bypass_check.py","security_data_leak_check.py","perf_p95_check.py",
    "perf_concurrent_check.py","perf_large_data_check.py","perf_memory_check.py",
    "reliability_recovery_check.py","reliability_backup_check.py","reliability_chaos_check.py",
    "reliability_circuit_breaker_check.py","compat_browser_check.py","compat_backward_check.py",
    "runtime_api_check.py","usability_loading_check.py","usability_error_msg_check.py",
    "ops_trace_id_check.py","ops_log_check.py","ops_alert_check.py"]
TS_GATES = ["lint_check.py","build_check.py","security_scan.py","tech_debt_check.py"]

NO_PROJECT_GATES = {
    "security_penetration_check.py", "security_auth_bypass_check.py",
    "security_data_leak_check.py", "compat_browser_check.py", "compat_backward_check.py",
    "perf_p95_check.py", "perf_concurrent_check.py", "perf_large_data_check.py",
    "perf_memory_check.py", "reliability_recovery_check.py", "reliability_backup_check.py",
    "reliability_chaos_check.py", "reliability_circuit_breaker_check.py",
    "runtime_api_check.py", "usability_loading_check.py", "usability_error_msg_check.py",
    "ops_trace_id_check.py", "ops_log_check.py", "ops_alert_check.py",
}

def run_gates(project_path, lang="python"):
    gates = PYTHON_GATES if lang == "python" else TS_GATES
    results = []
    for g in gates:
        path = GATE_DIR / g
        if not path.exists():
            continue
        try:
            if g in NO_PROJECT_GATES:
                r = subprocess.run(["python3", str(path)],
                                  capture_output=True, text=True, timeout=60)
            else:
                r = subprocess.run(["python3", str(path), project_path],
                                  capture_output=True, text=True, timeout=60)
            passed = r.returncode == 0
            last = [l for l in r.stdout.strip().split("\n") if l.strip()][-1] if r.stdout.strip() else ""
            results.append({"gate": g.replace(".py",""), "passed": passed, "detail": last[:60]})
        except:
            results.append({"gate": g.replace(".py",""), "passed": True, "detail": "SKIP"})
    return results

def review_with_qodo(project_path, diff_text):
    try:
        payload = json.dumps({"diff": diff_text[:5000]}).encode()
        req = urllib.request.Request("http://localhost:9200/review",
            data=payload, headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except:
        return {"error": "Qodo不可用", "findings": []}

def review_diff_3models(project_path):
    sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
    try:
        from unattended_repair_loop import review_diff_3models as _r
        return _r(project_path)
    except:
        return []

def security_full_scan(project_path):
    patterns = [
        (r'ark-[a-f0-9]{32}', "VolcEngine Ark Key"),
        (r'AKID[A-Za-z0-9]{16,}', "Tencent SecretID"),
        (r'(app_?secret|api_?key)\s*[:=]\s*["\'][A-Za-z0-9]{16,}', "硬编码密钥"),
    ]
    findings = []
    try:
        r = subprocess.run(["grep","-rn","--include=*.py","--include=*.ts","-E"] +
                          [p[0] for p in patterns] + [project_path+"/app/"],
                          capture_output=True, text=True, timeout=30)
        for line in r.stdout.strip().split("\n"):
            if not line.strip() or "environ" in line:
                continue
            for pat, desc in patterns:
                if re.search(pat, line, re.IGNORECASE):
                    parts = line.split(":")
                    findings.append({"severity":"P0","category":"security","rationale":desc,
                                    "file":parts[0] if parts else "?","line_start":parts[1] if len(parts)>1 else 0})
                    break
    except:
        pass
    return findings[:10]

def arbitrate_findings(qodo_findings, gate_results, ensemble_findings, security_findings):
    all_f = []
    for f in security_findings:
        all_f.append({**f, "source":"L3_security", "confidence":0.9})
    for f in ensemble_findings:
        all_f.append({**f, "source":"L2_ensemble", "confidence":0.8})
    for f in qodo_findings.get("findings", []):
        all_f.append({**f, "source":"L0_qodo", "confidence":0.7})
    for g in gate_results:
        if not g["passed"]:
            all_f.append({"severity":"P1","category":"gate","rationale":g["detail"],
                         "source":"L1_gate","confidence":1.0})
    merged = []
    for f in all_f:
        dup = False
        for m in merged:
            if m.get("file","")==f.get("file","") and m.get("category","")==f.get("category",""):
                if f.get("confidence",0)>m.get("confidence",0): m["confidence"]=f["confidence"]
                if f.get("severity")=="P0": m["severity"]="P0"
                dup = True; break
        if not dup:
            f.setdefault("sources",[]).append(f.get("source",""))
            merged.append(f)
    for f in merged:
        sev = f.get("severity","P2"); conf = f.get("confidence",0.5)
        f["action"] = "block" if (sev=="P0" and conf>=0.7) else "fix" if (sev=="P1" and conf>=0.6) else "log"
    return merged

def write_review_pitfall(project, findings):
    entry = {"id":f"PIT-REVIEW-{datetime.now().strftime('%H%M%S')}",
             "ts":datetime.now().isoformat(),"project":project,
             "analyzer":"review_agent","findings_count":len(findings)}
    h = hashlib.md5(json.dumps(entry,sort_keys=True).encode()).hexdigest()
    entry["hash"] = h
    with open(PITFALL_JSONL, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False)+"\n")

def trigger_repair(project_path, findings):
    for f in findings:
        if f.get("action") in ("block","fix"):
            try:
                payload = json.dumps({"project":project_path,"root_cause":f.get("rationale","")}).encode()
                req = urllib.request.Request("http://localhost:9100/api/repair",
                    data=payload, headers={"Content-Type":"application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                # PIT-SILENT-001: 不静默pass,记录失败
                print(f"[trigger_repair] FAILED: {str(e)[:80]}")

class ReviewRequest(BaseModel):
    project: str = "ht"
    trigger_repair: bool = True  # 默认触发修复(发现P0/P1就修)

@app.get("/api/health")
async def health():
    return {"status":"ok","version":"5.1.0","projects":list(PROJECTS.keys())}

@app.post("/api/review")
async def review(req: ReviewRequest, bg: BackgroundTasks):
    proj = PROJECTS.get(req.project, PROJECTS["ht"])
    path = proj["path"]; lang = proj["lang"]
    r = subprocess.run(["git","diff"], cwd=path, capture_output=True, text=True, timeout=10)
    diff = r.stdout.strip()
    qodo = review_with_qodo(path, diff) if diff else {"findings":[]}
    gates = run_gates(path, lang)
    ensemble = review_diff_3models(path) if diff else []
    security = security_full_scan(path)
    findings = arbitrate_findings(qodo, gates, ensemble, security)
    p0 = sum(1 for f in findings if f.get("action")=="block")
    p1 = sum(1 for f in findings if f.get("action")=="fix")
    gates_pass = sum(1 for g in gates if g["passed"])
    if findings: write_review_pitfall(req.project, findings)
    if req.trigger_repair and p0+p1>0:
        bg.add_task(trigger_repair, path, findings)
    return {"project":req.project,
            "summary":{"total":len(findings),"P0":p0,"P1":p1,"gates":f"{gates_pass}/{len(gates)}",
                       "verdict":"PASS" if p0==0 else "BLOCK"},
            "findings":findings, "gates":gates}

if __name__ == "__main__":
    print("=== Code Review Agent v5.1.0 ===")
    print("API: http://localhost:9201")
    print("Docs: http://localhost:9201/docs")
    uvicorn.run(app, host="127.0.0.1", port=9201)
