#!/usr/bin/env python3
"""test_ai_auto_repair.py — AI修复引擎单元测试

测试核心函数:
  - load_known_pitfalls (质量过滤)
  - scan_project_files (本地+远程)
  - step3_apply_patches (安全apply+白名单)
  - write_pitfall_entry (自进化闭环)
"""
import sys, os, json, tempfile, shutil
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))

def test_load_known_pitfalls_filters_unknown():
    """load_known_pitfalls应该跳过unknown条目"""
    from ai_auto_repair import load_known_pitfalls
    # 读真实pitfall库
    result = load_known_pitfalls(5)
    # 结果里不应该有unknown
    for item in result:
        assert "unknown" not in item.lower(), f"发现了unknown: {item}"
    print("✅ test_load_known_pitfalls_filters_unknown")

def test_file_whitelist_blocks_sensitive():
    """FORBIDDEN_PATHS应该拦截.git/.env等"""
    from ai_auto_repair import step3_apply_patches
    # 构造一个恶意补丁(改.env)
    patches = [{"file": ".env", "old_string": "x", "new_string": "y"}]
    tmpdir = tempfile.mkdtemp()
    results = step3_apply_patches(tmpdir, patches)
    assert not results[0]["applied"], "应该被白名单拦截"
    assert "安全拦截" in results[0]["error"]
    shutil.rmtree(tmpdir)
    print("✅ test_file_whitelist_blocks_sensitive")

def test_file_whitelist_blocks_git():
    """FORBIDDEN_PATHS应该拦截.git/config"""
    from ai_auto_repair import step3_apply_patches
    patches = [{"file": ".git/config", "old_string": "x", "new_string": "y"}]
    tmpdir = tempfile.mkdtemp()
    results = step3_apply_patches(tmpdir, patches)
    assert not results[0]["applied"], ".git/config应该被拦截"
    shutil.rmtree(tmpdir)
    print("✅ test_file_whitelist_blocks_git")

def test_scan_local_files():
    """scan_project_files应该能扫本地目录"""
    from ai_auto_repair import scan_project_files
    tmpdir = tempfile.mkdtemp()
    # 创建测试文件
    open(os.path.join(tmpdir, "test.py"), "w").write("x = 1")
    open(os.path.join(tmpdir, "test.ts"), "w").write("const x = 1")
    os.makedirs(os.path.join(tmpdir, "node_modules"), exist_ok=True)
    open(os.path.join(tmpdir, "node_modules", "junk.js"), "w").write("junk")
    
    files = scan_project_files(tmpdir)
    paths = [f["path"] for f in files]
    assert "test.py" in paths
    assert "test.ts" in paths
    assert "node_modules/junk.js" not in paths  # 应该被排除
    shutil.rmtree(tmpdir)
    print("✅ test_scan_local_files")

def test_write_pitfall_dedup():
    """write_pitfall_entry应该去重"""
    from ai_auto_repair import write_pitfall_entry, PITFALL_JSONL
    # 用临时pitfall库
    import ai_auto_repair
    original_path = ai_auto_repair.PITFALL_JSONL
    tmpdir = tempfile.mkdtemp()
    tmp_pitfall = os.path.join(tmpdir, "pitfalls.jsonl")
    ai_auto_repair.PITFALL_JSONL = type(original_path)(tmp_pitfall)
    
    bug = {"root_cause": "test_bug", "file": "test.py", "detail": "test"}
    fix = {"new_string": "fixed", "reasoning": "test"}
    
    # 写两次(应该去重)
    write_pitfall_entry(bug, fix)
    write_pitfall_entry(bug, fix)
    
    lines = open(tmp_pitfall).readlines()
    assert len(lines) == 1, f"应该只有1条(去重),实际{len(lines)}"
    
    # 恢复
    ai_auto_repair.PITFALL_JSONL = original_path
    shutil.rmtree(tmpdir)
    print("✅ test_write_pitfall_dedup")

def test_config_loading():
    """配置文件应该能正确加载"""
    config_path = os.path.expanduser("~/.hermes/scripts/ai-repair-config.json")
    if os.path.exists(config_path):
        config = json.loads(open(config_path).read())
        assert "version" in config
        assert "projects" in config
        assert len(config["projects"]) >= 1
        print(f"✅ test_config_loading (v{config['version']}, {len(config['projects'])}个项目)")
    else:
        print("⚠️ test_config_loading (配置文件不存在)")

if __name__ == "__main__":
    print("=== AI修复引擎单元测试 ===\n")
    tests = [
        test_load_known_pitfalls_filters_unknown,
        test_file_whitelist_blocks_sensitive,
        test_file_whitelist_blocks_git,
        test_scan_local_files,
        test_write_pitfall_dedup,
        test_config_loading,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"❌ {test.__name__}: {str(e)[:100]}")
            failed += 1
    print(f"\n=== {passed}/{len(tests)} passed ===")
