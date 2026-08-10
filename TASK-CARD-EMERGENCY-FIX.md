# 任务卡: AI修复系统紧急止血 — 超时配置修正+熔断开关

## 背景
AI修复系统连续10次修复失败（每次2-4秒）。根因：OpenHands subprocess timeout配置不当 + NewAPI 504。投票通过(3/5 APPROVE)。

## 项目位置
`/Users/maccc/.hermes/scripts/ai_repair/`

## 需要修改的文件

### 1. server.py — 加超时配置+熔断开关

在server.py的修复入口处（POST /api/repair处理函数内）：

```python
# 熔断开关
REPAIR_ENABLED = os.environ.get("AI_REPAIR_ENABLED", "1") == "1"
if not REPAIR_ENABLED:
    return {"status": "skipped", "reason": "ai_repair_disabled_by_env"}

# 统一超时配置（替代硬编码的timeout值）
SUBPROCESS_TIMEOUT = int(os.environ.get("AI_REPAIR_SUBPROCESS_TIMEOUT", "360"))  # 6分钟
OPENHANDS_TIMEOUT = int(os.environ.get("AI_REPAIR_TASK_TIMEOUT", "300"))  # 5分钟
```

找到所有`subprocess.run`或`subprocess.call`调用，把timeout改为从环境变量读取（默认360秒，不是当前的2-4秒）。

### 2. 检查当前timeout值

搜索server.py和ai_auto_repair.py中所有`timeout=`出现的位置，确认是否有错误的小值（如timeout=3或timeout=5）。

```bash
grep -n "timeout=" /Users/maccc/.hermes/scripts/ai_repair/server.py
grep -n "timeout=" /Users/maccc/.hermes/scripts/ai_repair/ai_auto_repair.py
```

如果找到timeout=3或timeout=5这种小值，改为360。

### 3. 加API健康检查

在server.py加一个函数，修复前先探测NewAPI是否可用：

```python
import httpx

async def check_api_health():
    """修复前检查NewAPI是否可用"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=20)) as client:
            resp = await client.post(
                f"{NEWAPI_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"model": "glm-5.2", "messages": [{"role": "user", "content": "OK"}], "max_tokens": 4},
            )
            return resp.status_code == 200
    except:
        return False
```

在POST /api/repair处理函数开头调用。如果API不可用→直接skip不浪费OpenHands调用。

## 验收标准
- [ ] server.py有AI_REPAIR_ENABLED熔断开关
- [ ] 所有subprocess timeout≥300秒
- [ ] 有API健康检查函数
- [ ] py_compile server.py通过
- [ ] curl http://127.0.0.1:9100/api/health返回200

## 约束
- 只修改/Users/maccc/.hermes/scripts/ai_repair/server.py
- 不修改ai_auto_repair.py（那是远程引擎）
- 不引入新依赖
- 全英文代码注释
