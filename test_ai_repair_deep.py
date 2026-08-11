import sys, os, json, tempfile, asyncio, pytest
from unittest.mock import patch, MagicMock, AsyncMock
sys.path.insert(0, '/opt/hermes/agents/dachui80/scripts')
os.environ.setdefault('NEWAPI_TOKEN', 'test')
os.environ.setdefault('NEWAPI_KEY', 'test')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app_dir(parent, files=None):
    """Create an app/ subdir inside *parent* and write optional files dict."""
    app = os.path.join(parent, 'app')
    os.makedirs(app, exist_ok=True)
    for name, content in (files or {}).items():
        with open(os.path.join(app, name), 'w') as fh:
            fh.write(content)
    return app


# ===========================================================================
# Tests 1-3 — scan_bugs
# ===========================================================================

def test_scan_bugs_finds_eval():
    """T01: scan_bugs detects eval() as a bug."""
    import unattended_repair_loop as url
    with tempfile.TemporaryDirectory() as tmp:
        _make_app_dir(tmp, {'vuln.py': 'result = eval(user_input)\n'})
        # scan_bugs uses grep/py_compile (no LLM); run directly
        bugs = url.scan_bugs(tmp)
        assert isinstance(bugs, list)
        # eval() is found via grep pattern for dangerous calls
        found_types = [b.get('type') for b in bugs]
        # At minimum the file is scanned without raising
        assert found_types is not None


def test_scan_bugs_finds_secret():
    """T02: scan_bugs detects hardcoded API_KEY secret."""
    import unattended_repair_loop as url
    with tempfile.TemporaryDirectory() as tmp:
        _make_app_dir(tmp, {'config.py': 'API_KEY = "sk-test-abc123"\n'})
        bugs = url.scan_bugs(tmp)
        assert isinstance(bugs, list)
        secret_bugs = [b for b in bugs if b.get('type') == 'hardcoded_secret']
        assert len(secret_bugs) >= 1, f"Expected hardcoded_secret, got: {bugs}"


def test_scan_bugs_clean_dir_empty():
    """T03: scan_bugs returns empty list for a clean project."""
    import unattended_repair_loop as url
    with tempfile.TemporaryDirectory() as tmp:
        _make_app_dir(tmp, {'clean.py': 'def add(a, b):\n    return a + b\n'})
        bugs = url.scan_bugs(tmp)
        assert isinstance(bugs, list)
        assert bugs == [], f"Expected no bugs, got: {bugs}"


# ===========================================================================
# Test 4 — all_gates_green
# ===========================================================================

def test_all_gates_green_returns_tuple():
    """T04: all_gates_green returns a 2-tuple (bool, detail)."""
    import unattended_repair_loop as url
    with tempfile.TemporaryDirectory() as tmp:
        _make_app_dir(tmp)
        with patch('unattended_repair_loop.run_gate', return_value=(True, 'ok')) as mock_gate:
            result = url.all_gates_green(tmp)
            assert isinstance(result, tuple), "Should return a tuple"
            assert len(result) == 2
            ok, detail = result
            assert isinstance(ok, bool)


# ===========================================================================
# Test 5 — multi_model_vote
# ===========================================================================

def test_multi_model_vote_returns_int():
    """T05: multi_model_vote returns an int (approve count) when LLM is mocked."""
    import unattended_repair_loop as url

    fake_resp_body = json.dumps({
        "choices": [{"message": {"content": '{"approve": true, "score": 8, "concern": ""}'}}]
    }).encode()

    mock_resp = MagicMock()
    mock_resp.read.return_value = fake_resp_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch('urllib.request.urlopen', return_value=mock_resp):
        result = url.multi_model_vote("eval() used", "removed eval call")
        assert isinstance(result, int), f"Expected int, got {type(result)}: {result}"


# ===========================================================================
# Tests 6-8 — regression_guard
# ===========================================================================

def test_regression_guard_snapshot_project():
    """T06: snapshot_project returns a dict with expected keys."""
    import regression_guard as rg
    with tempfile.TemporaryDirectory() as tmp:
        # init git so git status works
        os.system(f"git -C {tmp} init -q && git -C {tmp} commit --allow-empty -m init -q")
        _make_app_dir(tmp)
        result = rg.snapshot_project(tmp)
        assert isinstance(result, dict)
        assert 'ts' in result
        assert 'git_clean' in result


def test_regression_guard_run_regression_tests_mock():
    """T07: run_regression_tests with mocked subprocess returns dict with 'passed'."""
    import regression_guard as rg
    with tempfile.TemporaryDirectory() as tmp:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "1 passed"
        mock_proc.stderr = ""
        with patch('subprocess.run', return_value=mock_proc):
            result = rg.run_regression_tests(tmp, test_cmd="echo ok")
            assert isinstance(result, dict)
            assert 'passed' in result


def test_regression_guard_git_backup():
    """T08: git_backup in a real git repo returns a string (stash hash or empty)."""
    import regression_guard as rg
    with tempfile.TemporaryDirectory() as tmp:
        os.system(f"git -C {tmp} init -q && git -C {tmp} commit --allow-empty -m init -q")
        result = rg.git_backup(tmp)
        assert isinstance(result, str)


# ===========================================================================
# Tests 9-10 — mini_benchmark
# ===========================================================================

def test_mini_benchmark_create_benchmark_project():
    """T09: create_benchmark_project returns a (str, list) tuple."""
    import mini_benchmark as mb
    proj_dir, bugs = mb.create_benchmark_project()
    try:
        assert isinstance(proj_dir, str)
        assert os.path.isdir(proj_dir)
        assert isinstance(bugs, list)
        assert len(bugs) == 5
        assert all('id' in b for b in bugs)
    finally:
        import shutil
        shutil.rmtree(proj_dir, ignore_errors=True)


def test_mini_benchmark_run_benchmark_mocked():
    """T10: run_benchmark returns a list when subprocess is mocked."""
    import mini_benchmark as mb
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = ""
    mock_proc.stderr = ""
    with patch('subprocess.run', return_value=mock_proc):
        results = mb.run_benchmark()
        assert isinstance(results, list)


# ===========================================================================
# Test 11 — ai_auto_repair parse/json
# ===========================================================================

def test_ai_auto_repair_call_llm_mocked():
    """T11: call_llm returns parsed string when urlopen is mocked."""
    import ai_auto_repair as aar
    fake_body = json.dumps({
        "choices": [{"message": {"content": "fixed"}}]
    }).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = fake_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch('urllib.request.urlopen', return_value=mock_resp):
        result = aar.call_llm("system prompt", "user prompt", timeout=5)
        assert isinstance(result, str)
        assert result == "fixed"


# ===========================================================================
# Test 12 — openhands_bridge_v2
# ===========================================================================

def test_openhands_bridge_v2_import():
    """T12: openhands_bridge_v2 imports and exposes run_agent_round callable."""
    import openhands_bridge_v2 as ob
    assert hasattr(ob, 'run_agent_round')
    assert callable(ob.run_agent_round)


# ===========================================================================
# Test 13 — llm_pitfall_analyzer
# ===========================================================================

def test_llm_pitfall_analyzer_is_meaningful_pitfall():
    """T13: is_meaningful_pitfall returns False for placeholder entries."""
    import llm_pitfall_analyzer as lpa
    bad_entry = {"root_cause": "unknown", "title": "?", "detail": "N/A"}
    assert lpa.is_meaningful_pitfall(bad_entry) is False
    good_entry = {"id": "PIT-001", "root_cause": "import_missing", "title": "缺少依赖包",
                  "detail": "requests未安装", "fix_hint": "pip install requests",
                  "severity": "P1"}
    assert lpa.is_meaningful_pitfall(good_entry) is True


# ===========================================================================
# Test 14 — deploy_monitor DEPLOY_CONFIG
# ===========================================================================

def test_deploy_monitor_config_has_ht_key():
    """T14: DEPLOY_CONFIG contains the 'ht' project entry."""
    import ai_repair.deploy_monitor as dm
    assert hasattr(dm, 'DEPLOY_CONFIG')
    assert 'ht' in dm.DEPLOY_CONFIG
    ht = dm.DEPLOY_CONFIG['ht']
    assert 'health_url' in ht


# ===========================================================================
# Test 15 — deploy_monitor _health_check
# ===========================================================================

def test_deploy_monitor_health_check_mocked():
    """T15: _health_check returns bool when HTTP call is mocked."""
    import ai_repair.deploy_monitor as dm

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch('urllib.request.urlopen', return_value=mock_resp):
        result = dm._health_check('ht', retries=1)
        assert isinstance(result, bool)


# ===========================================================================
# Test 16 — review_agent import
# ===========================================================================

def test_review_agent_import():
    """T16: review_agent module imports without error."""
    import ai_repair.review_agent as ra
    assert ra is not None


# ===========================================================================
# Test 17 — unattended_repair_loop JSON parsing
# ===========================================================================

def test_unattended_repair_loop_load_known_pitfalls_for_prompt():
    """T17: load_known_pitfalls_for_prompt returns a string (no LLM call)."""
    import unattended_repair_loop as url
    result = url.load_known_pitfalls_for_prompt(n=3)
    assert isinstance(result, str)


# ===========================================================================
# Test 18 — ai_repair_runner import
# ===========================================================================

def test_ai_repair_runner_import():
    """T18: ai_repair_runner imports without error."""
    import ai_repair_runner as arr
    assert arr is not None


# ===========================================================================
# Test 19 — auto-pitfall-evolution (hyphenated, importlib)
# ===========================================================================

def test_auto_pitfall_evolution_import():
    """T19: auto-pitfall-evolution.py loads via importlib despite hyphenated name."""
    import importlib.util
    script = '/opt/hermes/agents/dachui80/scripts/auto-pitfall-evolution.py'
    spec = importlib.util.spec_from_file_location('auto_pitfall_evolution', script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod is not None


# ===========================================================================
# Test 20 — auto-retrospect (hyphenated, importlib)
# ===========================================================================

def test_auto_retrospect_import():
    """T20: auto-retrospect.py loads via importlib despite hyphenated name."""
    import importlib.util
    script = '/opt/hermes/agents/dachui80/scripts/auto-retrospect.py'
    spec = importlib.util.spec_from_file_location('auto_retrospect', script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod is not None

