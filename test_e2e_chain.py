import sys, os, pytest
from unittest.mock import patch, MagicMock, AsyncMock
sys.path.insert(0, '/opt/hermes/agents/dachui80/scripts')

"""
UPG-027: E2E chain tests
端到端流程测试 — scan→vote→fix→test 全mock链路
"""


# ---------------------------------------------------------------------------
# Minimal E2E pipeline (self-contained, all stages injectable)
# ---------------------------------------------------------------------------

class PipelineResult:
    def __init__(self, stage: str, status: str, detail: str = ""):
        self.stage = stage
        self.status = status   # "ok" | "skip" | "fail"
        self.detail = detail

    def __repr__(self):
        return f"PipelineResult(stage={self.stage!r}, status={self.status!r})"


class E2EPipeline:
    """
    Orchestrate: scan → vote → fix → test
    Each step is a callable injected at construction for full mockability.
    """

    def __init__(self, scanner, voter, fixer, tester):
        self._scan = scanner    # (target) -> list[dict]  (bugs found)
        self._vote = voter      # (bug) -> bool           (approved?)
        self._fix = fixer       # (bug) -> bool           (fix applied?)
        self._test = tester     # () -> bool              (tests pass?)

    def run(self, target: str) -> list[PipelineResult]:
        results = []

        # Stage 1: scan
        bugs = self._scan(target)
        results.append(PipelineResult("scan", "ok", f"{len(bugs)} bug(s) found"))

        if not bugs:
            results.append(PipelineResult("vote", "skip", "no bugs"))
            results.append(PipelineResult("fix",  "skip", "no bugs"))
            results.append(PipelineResult("test", "skip", "no bugs"))
            return results

        # Stage 2: vote (first bug only for simplicity)
        bug = bugs[0]
        approved = self._vote(bug)
        results.append(PipelineResult("vote", "ok" if approved else "skip",
                                      "approved" if approved else "rejected"))

        if not approved:
            results.append(PipelineResult("fix",  "skip", "vote rejected"))
            results.append(PipelineResult("test", "skip", "fix skipped"))
            return results

        # Stage 3: fix
        fixed = self._fix(bug)
        results.append(PipelineResult("fix", "ok" if fixed else "fail",
                                      "applied" if fixed else "fix failed"))

        # Stage 4: test
        passed = self._test()
        results.append(PipelineResult("test", "ok" if passed else "fail",
                                      "passed" if passed else "tests failed"))

        return results

    @staticmethod
    def _stage(results, name) -> PipelineResult:
        return next(r for r in results if r.stage == name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestE2EChain:

    # 1. Full chain: all stages mocked — happy path
    def test_full_chain_all_mocked(self):
        """UPG-027-1: scan→vote→fix→test all succeed (full mock)."""
        scanner = MagicMock(return_value=[{"id": "BUG-1", "type": "import_missing"}])
        voter   = MagicMock(return_value=True)
        fixer   = MagicMock(return_value=True)
        tester  = MagicMock(return_value=True)

        pipeline = E2EPipeline(scanner, voter, fixer, tester)
        results  = pipeline.run("target_module.py")

        assert len(results) == 4
        assert all(r.status == "ok" for r in results)

        scanner.assert_called_once_with("target_module.py")
        voter.assert_called_once_with({"id": "BUG-1", "type": "import_missing"})
        fixer.assert_called_once()
        tester.assert_called_once()

    # 2. Bug found → vote approves → fix applied → test passes
    def test_bug_vote_approved_fix_applied_test_passes(self):
        """UPG-027-2: scan finds bug → vote approves → fix applied → tests pass."""
        bug = {"id": "BUG-42", "type": "null_pointer", "file": "server.py", "line": 77}
        scanner = MagicMock(return_value=[bug])
        voter   = MagicMock(return_value=True)   # approved
        fixer   = MagicMock(return_value=True)   # fix applied
        tester  = MagicMock(return_value=True)   # tests pass

        pipeline = E2EPipeline(scanner, voter, fixer, tester)
        results  = pipeline.run("server.py")

        scan_r = E2EPipeline._stage(results, "scan")
        vote_r = E2EPipeline._stage(results, "vote")
        fix_r  = E2EPipeline._stage(results, "fix")
        test_r = E2EPipeline._stage(results, "test")

        assert scan_r.status == "ok"
        assert "1 bug" in scan_r.detail
        assert vote_r.status == "ok"
        assert vote_r.detail == "approved"
        assert fix_r.status == "ok"
        assert fix_r.detail == "applied"
        assert test_r.status == "ok"
        assert test_r.detail == "passed"

    # 3. Bug found → vote rejects → fix skipped
    def test_bug_vote_rejected_fix_skipped(self):
        """UPG-027-3: scan finds bug → vote rejects → fix and test are skipped."""
        bug = {"id": "BUG-99", "type": "style", "file": "cli.py", "line": 5}
        scanner = MagicMock(return_value=[bug])
        voter   = MagicMock(return_value=False)  # rejected
        fixer   = MagicMock()                    # should NOT be called
        tester  = MagicMock()                    # should NOT be called

        pipeline = E2EPipeline(scanner, voter, fixer, tester)
        results  = pipeline.run("cli.py")

        assert len(results) == 4
        vote_r = E2EPipeline._stage(results, "vote")
        fix_r  = E2EPipeline._stage(results, "fix")
        test_r = E2EPipeline._stage(results, "test")

        assert vote_r.status == "skip"
        assert vote_r.detail == "rejected"
        assert fix_r.status == "skip"
        assert fix_r.detail == "vote rejected"
        assert test_r.status == "skip"

        fixer.assert_not_called()
        tester.assert_not_called()
