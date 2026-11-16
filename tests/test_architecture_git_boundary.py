"""Offline regression checks for material copies and tracked source subtrees."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "architecture_git_boundary", ROOT / "stepwise/engine/framework_architecture.py",
)
architecture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = architecture
spec.loader.exec_module(architecture)


@unittest.skipUnless(shutil.which("git"), "Git is unavailable")
class GitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        (self.root / "parent.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.commit()

    def git(self, *args):
        result = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            cwd=self.root, check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "test: source fixture")
        return self.git("rev-parse", "HEAD")

    def source_copy(self):
        source = self.root / "materials" / "codes"
        source.mkdir(parents=True)
        (source / "agent.py").write_text("def run():\n    return 2\n", encoding="utf-8")
        return source

    def test_untracked_copy_ignores_ancestor_repository(self):
        source = self.source_copy()
        self.assertFalse(architecture._is_git_repository(source))
        self.assertEqual(architecture.python_sources(source, include=["*.py"]), ["agent.py"])
        files = {"agent.py": architecture.sha256(source / "agent.py")}
        revision = architecture._source_revision(source, files)
        self.assertTrue(revision.startswith("content-"))
        self.assertNotEqual(revision, self.git("rev-parse", "HEAD"))

    def test_tracked_subtree_keeps_git_revision(self):
        source = self.source_copy()
        head = self.commit()
        self.assertTrue(architecture._is_git_repository(source))
        self.assertEqual(architecture.python_sources(source, include=["*.py"]), ["agent.py"])
        compiled = architecture.compile_repository(source, "agent_s2", include=["*.py"])
        self.assertEqual(compiled["repo_revision"], head)

    def test_dirty_tracked_subtree_is_rejected(self):
        source = self.source_copy()
        self.commit()
        (source / "agent.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.assertEqual(architecture._dirty_sources(source, {"agent.py"}), ["agent.py"])
        with self.assertRaisesRegex(architecture.ArchitectureError, "uncommitted changes"):
            architecture.compile_repository(source, "agent_s2", include=["*.py"])

    def test_other_subtree_changes_do_not_mark_selected_sources_dirty(self):
        source = self.source_copy()
        self.commit()
        (self.root / "parent.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.assertEqual(architecture._dirty_sources(source, {"agent.py"}), [])

    def test_worktree_root_and_revision_guard_remain_valid(self):
        self.assertTrue(architecture._is_git_repository(self.root))
        with self.assertRaisesRegex(architecture.ArchitectureError, "revision does not match"):
            architecture.compile_repository(self.root, "agent_s2", revision="0" * 40, include=["*.py"])

    def test_linked_worktree_git_file_is_supported(self):
        worktree = self.root / "linked"
        self.git("worktree", "add", "--detach", str(worktree), "HEAD")
        self.assertTrue((worktree / ".git").is_file())
        self.assertTrue(architecture._is_git_repository(worktree))
        self.assertEqual(architecture.python_sources(worktree, include=["*.py"]), ["parent.py"])


if __name__ == "__main__":
    unittest.main()
