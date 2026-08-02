#!/usr/bin/env python3
"""ai_repair_cli.py — AI修复系统统一CLI入口

用法:
  ai-repair repair --project X --bug "..."
  ai-repair benchmark
  ai-repair stats
  ai-repair config --list
  ai-repair serve [--port 9100]
"""
import sys, os, json, argparse

def cmd_repair(args):
    """触发修复"""
    sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
    from ai_auto_repair import auto_repair
    
    project = args.project
    if not project:
        config_path = os.path.expanduser("~/.hermes/scripts/ai-repair-config.json")
        if os.path.exists(config_path):
            config = json.loads(open(config_path).read())
            project = config["projects"][0]["dir"]
    
    result = auto_repair(args.bug, project)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    sys.exit(0 if result.get("success") else 1)

def cmd_benchmark(args):
    """跑benchmark"""
    sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
    import subprocess
    env = os.environ.copy()
    r = subprocess.run(["python3", os.path.expanduser("~/.hermes/scripts/mini_benchmark.py")],
                       env=env)
    sys.exit(r.returncode)

def cmd_stats(args):
    """查看统计"""
    # 从SQLite读
    import sqlite3
    db = os.path.expanduser("~/.hermes/ai-repair/repair.db")
    if not os.path.exists(db):
        print("无统计数据库(服务未启动过)")
        # 从/tmp JSON读
        stats_file = "/tmp/ai-repair-stats.json"
        if os.path.exists(stats_file):
            stats = json.loads(open(stats_file).read())
            print(f"Runner统计: 扫描{stats.get('total_scanned',0)} / 修复{stats.get('total_fixed',0)}")
        return
    
    conn = sqlite3.connect(db)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM repairs")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM repairs WHERE status='success'")
    success = c.fetchone()[0]
    
    print(f"=== AI修复系统 v2.0.0 统计 ===")
    print(f"总修复: {total}")
    print(f"成功: {success}")
    print(f"失败: {total - success}")
    print(f"成功率: {success*100//max(total,1)}%")
    
    c.execute("SELECT id, ts, project, status, elapsed FROM repairs ORDER BY ts DESC LIMIT 5")
    print(f"\n最近5次:")
    for r in c.fetchall():
        print(f"  {r[0]} | {r[1][:19]} | {r[3]} | {r[4] or '?'}s | {r[2][:30]}")
    conn.close()

def cmd_serve(args):
    """启动API服务"""
    sys.path.insert(0, os.path.expanduser("~/.hermes/scripts/ai_repair"))
    import uvicorn
    print(f"=== AI全能修复系统 v2.0.0 ===")
    print(f"API: http://localhost:{args.port}")
    print(f"Docs: http://localhost:{args.port}/docs")
    uvicorn.run("server:app", host="127.0.0.1", port=args.port, reload=False)

def cmd_config(args):
    """配置管理"""
    config_path = os.path.expanduser("~/.hermes/scripts/ai-repair-config.json")
    if args.list:
        if os.path.exists(config_path):
            print(open(config_path).read())
        else:
            print("无配置文件")
    elif args.add_project:
        name, durl = args.add_project.split(":", 1)
        config = json.loads(open(config_path).read()) if os.path.exists(config_path) else {"projects": []}
        config["projects"].append({"name": name, "dir": durl, "lang": "python"})
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"已添加项目: {name}")

def main():
    parser = argparse.ArgumentParser(prog="ai-repair", description="AI全能修复系统 v2.0.0")
    sub = parser.add_subparsers(dest="command")
    
    # repair
    p_repair = sub.add_parser("repair", help="触发修复")
    p_repair.add_argument("--project", "-p", help="项目目录")
    p_repair.add_argument("--bug", "-b", required=True, help="根因描述")
    p_repair.set_defaults(func=cmd_repair)
    
    # benchmark
    p_bench = sub.add_parser("benchmark", help="跑mini benchmark")
    p_bench.set_defaults(func=cmd_benchmark)
    
    # stats
    p_stats = sub.add_parser("stats", help="查看统计")
    p_stats.set_defaults(func=cmd_stats)
    
    # serve
    p_serve = sub.add_parser("serve", help="启动API服务")
    p_serve.add_argument("--port", type=int, default=9100)
    p_serve.set_defaults(func=cmd_serve)
    
    # config
    p_config = sub.add_parser("config", help="配置管理")
    p_config.add_argument("--list", action="store_true")
    p_config.add_argument("--add-project", help="格式: 名称:路径")
    p_config.set_defaults(func=cmd_config)
    
    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
