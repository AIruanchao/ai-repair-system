# AI全能修复系统 — 架构设计文档 v2.0.0

## 1. 系统架构

```
┌──────────────────────────────────────────────────────────┐
│                    用户层                                  │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐             │
│  │ CLI      │  │ REST API │  │ Web Docs   │             │
│  │ cli.py   │  │ :9100    │  │ /docs      │             │
│  └────┬─────┘  └────┬─────┘  └─────┬──────┘             │
├───────┼──────────────┼──────────────┼────────────────────┤
│       │     服务层 (FastAPI)         │                    │
│       ▼              ▼               ▼                    │
│  ┌─────────────────────────────────────────┐             │
│  │  server.py (路由+中间件+异步任务)         │             │
│  │  POST /api/repair → BackgroundTasks      │             │
│  └──────────────────┬──────────────────────┘             │
├─────────────────────┼────────────────────────────────────┤
│                     ▼ 核心引擎层                           │
│  ┌───────────┐ ┌───────────┐ ┌────────────┐ ┌─────────┐ │
│  │ 定位引擎   │ │ 补丁生成   │ │ 安全Apply   │ │ 回归守卫 │ │
│  │ step1     │ │ step2     │ │ step3      │ │ step4    │ │
│  │ LLM+pitfall│ │ 多候选    │ │ 白名单+原子 │ │ pytest   │ │
│  └───────────┘ └───────────┘ └────────────┘ └─────────┘ │
│  ┌───────────┐ ┌───────────┐ ┌────────────┐             │
│  │ 自进化     │ │ 4模型投票  │ │ 通知+审计   │             │
│  │ step5     │ │ vote.py   │ │ notify     │             │
│  │ 写pitfall  │ │ 并行+多数决│ │ webhook+JSONL│            │
│  └───────────┘ └───────────┘ └────────────┘             │
├─────────────────────────────────────────────────────────┤
│                    数据层                                 │
│  ┌──────────┐ ┌──────────┐ ┌────────────┐              │
│  │ SQLite   │ │ JSONL    │ │ 配置文件    │              │
│  │ repair.db│ │ pitfalls │ │ config.json│              │
│  └──────────┘ └──────────┘ └────────────┘              │
├─────────────────────────────────────────────────────────┤
│                    基础设施层                              │
│  ┌──────────┐ ┌──────────┐ ┌────────────┐              │
│  │ launchd  │ │ Docker   │ │ cron       │              │
│  │ 每2h自主  │ │ 容器化   │ │ 复盘+进化   │              │
│  └──────────┘ └──────────┘ └────────────┘              │
└─────────────────────────────────────────────────────────┘
```

## 2. 模块边界

| 模块 | 职责 | 输入 | 输出 | 依赖 |
|------|------|------|------|------|
| **定位引擎(step1)** | LLM分析根因→定位文件 | root_cause+file_list+pitfalls | target_files[] | NewAPI |
| **补丁生成(step2)** | 读目标文件→生成2-3候选补丁 | target_files+root_cause | patches[] | NewAPI |
| **安全Apply(step3)** | 白名单→备份→原子替换→验证 | patches+project_dir | apply_result | 文件系统/SSH |
| **回归守卫(step4)** | pytest/tsc验证→失败回滚 | project_dir+test_cmd | pass/fail | pytest/tsc |
| **自进化(step5)** | 写pitfall库+通知 | bug_info+fix_info | pitfall_entry | JSONL |
| **4模型投票** | 并行调4模型→多数决 | fail_text | root_cause+confidence | NewAPI |
| **server.py** | REST API+异步调度+SQLite | HTTP请求 | JSON响应 | FastAPI |
| **cli.py** | 命令行入口 | argparse | stdout | server/核心引擎 |

## 3. REST API契约

### POST /api/repair
```json
// 请求
{"project": "cloud4:/opt/erp-project", "root_cause": "parseInt缺radix", "model": "glm-5.2", "dry_run": false}

// 响应 202 Accepted
{"task_id": "R242de4cf", "status": "pending", "message": "修复已提交"}

// 错误
{"detail": "未指定项目目录"} // 400
```

### GET /api/repair/{task_id}
```json
// 200
{"id": "R242de4cf", "ts": "2026-08-02T12:34:13", "project": "...", "root_cause": "...",
 "status": "success|failed|pending", "result": {"stdout": "..."}, "elapsed": 15.2,
 "files_changed": ["src/app/api/products/route.ts"]}

// 404
{"detail": "修复任务不存在"}
```

### GET /api/stats
```json
{"total_repairs": 15, "success": 12, "failed": 3, "success_rate": "80%",
 "recent": [...], "pitfall_count": 82}
```

### GET /api/pitfalls?limit=20&quality=good
```json
{"pitfalls": [...], "total": 82, "filtered": 15}
```

## 4. 数据模型(SQLite)

```sql
CREATE TABLE repairs (
    id TEXT PRIMARY KEY,         -- R+md5[:8]
    ts TEXT NOT NULL,            -- ISO时间戳
    project TEXT NOT NULL,       -- 项目路径
    root_cause TEXT NOT NULL,    -- 根因描述
    status TEXT DEFAULT 'pending', -- pending|success|failed
    result TEXT,                 -- JSON(stdout+stderr)
    elapsed REAL,                -- 耗时秒
    files_changed TEXT           -- JSON数组
);

CREATE TABLE stats (
    key TEXT PRIMARY KEY,
    value TEXT                   -- JSON
);
```

## 5. 迁移策略

| 步骤 | 做什么 | 风险 |
|------|--------|------|
| 1 | 核心引擎(ai_auto_repair.py等)保持原位不动 | 低(不破坏) |
| 2 | server.py通过sys.path引用核心引擎 | 低(薄封装) |
| 3 | Docker可选(当前launchd够用) | 无(不强制) |
| 4 | API+CLI作为新入口,旧脚本继续工作 | 低(并行) |
