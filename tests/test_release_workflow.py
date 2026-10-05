"""Tests for the merge-release automation and the release workflows.

Two things are covered here:

* the workflow files' structure - every merge to main must call the reusable
  installer build and publish a release, and the platform build steps must
  exist in exactly one workflow;
* the publish scripts themselves, run against stubbed ``gh``/``git`` binaries
  so the tag/asset handling can be checked offline (no network, no releases).

Run with either:
    python -m pytest tests/test_release_workflow.py
    python tests/test_release_workflow.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPTS = REPO_ROOT / ".github" / "scripts"
REUSABLE = "./.github/workflows/build-installers.yml"
PLATFORM_BUILDS = ("build/build_linux.sh", "build/build_macos.sh", "build_windows.bat")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def triggers(text: str) -> str:
    """The `on:` block of a workflow file (up to `jobs:`), as text."""
    return text.split("\non:", 1)[1].split("\njobs:", 1)[0]


def without_comments(text: str) -> str:
    """The file with its comment lines dropped, for counting real YAML keys."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# --------------------------------------------------------------------------- #
# Stubs for gh/git, so the publish scripts can run offline.
# --------------------------------------------------------------------------- #
GH_STUB = '''#!{python}
"""A fake `gh` that records its argv instead of talking to GitHub."""
import os
import sys

log = os.environ["CALL_LOG"]
args = sys.argv[1:]
with open(log, "a", encoding="utf-8") as handle:
    handle.write("gh " + " ".join(args) + "\\n")

if args[:2] == ["repo", "view"]:
    print("https://github.com/example/Sentinel")
    sys.exit(0)

if args[:2] == ["release", "view"]:
    if "--json" in args:
        # The rolling release's asset list, for the stale-asset sweep.
        print(os.environ.get("STUB_ASSETS", ""))
        sys.exit(0)
    sys.exit(0 if os.environ.get("STUB_RELEASE_EXISTS") == "1" else 1)

if args[:2] == ["release", "create"] and "--target" in args:
    pass

sys.exit(0)
'''

GIT_STUB = '''#!{python}
"""A fake `git`: enough for tag -f/-d, push and rev-parse."""
import os
import sys

log = os.environ["CALL_LOG"]
args = sys.argv[1:]
with open(log, "a", encoding="utf-8") as handle:
    handle.write("git " + " ".join(args) + "\\n")

if args[:1] == ["rev-parse"]:
    print("0" * 40)
    sys.exit(0)
if args[:1] == ["push"] and os.environ.get("STUB_TAG_PUSH_FAILS") == "1":
    sys.exit(1)
if args[:1] == ["rev-parse"] or args[:1] == ["tag"]:
    sys.exit(0)
sys.exit(0)
'''


class PublishScriptTestCase(unittest.TestCase):
    """Shared fixture: a repo-shaped temp dir with stubbed binaries on PATH."""

    SCRIPT = ""

    def setUp(self) -> None:
        if os.name == "nt":
            self.skipTest("bash scripts")
        self.tmp = Path(tempfile.mkdtemp(prefix="pm-release-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        (self.tmp / ".github" / "scripts").mkdir(parents=True)
        shutil.copy2(SCRIPTS / self.SCRIPT, self.tmp / ".github" / "scripts" / self.SCRIPT)
        (self.tmp / "VERSION").write_text("3.0.0\n", encoding="utf-8")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.write_stub("gh", GH_STUB)
        self.write_stub("git", GIT_STUB)

    def write_stub(self, name: str, template: str) -> None:
        path = self.bin / name
        path.write_text(template.format(python=sys.executable), encoding="utf-8")
        path.chmod(0o755)

    def add_artifacts(self, *names: str) -> None:
        directory = self.tmp / "artifacts"
        directory.mkdir(exist_ok=True)
        for name in names:
            (directory / name).write_text("installer", encoding="utf-8")

    def run_script(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        environment = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "CALL_LOG": str(self.log),
            "GITHUB_SHA": "a" * 40,
            "GITHUB_RUN_NUMBER": "42",
        }
        environment.update(env or {})
        return subprocess.run(
            ["bash", str(self.tmp / ".github" / "scripts" / self.SCRIPT), *args],
            capture_output=True,
            text=True,
            env=environment,
            cwd=str(self.tmp),
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]


class MergePublishScriptTests(PublishScriptTestCase):
    """`.github/scripts/publish-merge-release.sh` (merge-release.yml)."""

    SCRIPT = "publish-merge-release.sh"

    def setUp(self) -> None:
        super().setUp()
        # One installer per platform and architecture, as the reusable build
        # uploads them (both .deb architectures, both .dmg architectures, both
        # .exe architectures).
        self.add_artifacts(
            "Sentinel-Setup-3.0.0.exe",
            "Sentinel-Setup-3.0.0-arm64.exe",
            "Sentinel-3.0.0-x86_64.dmg",
            "Sentinel-3.0.0-arm64.dmg",
            "sentinel_3.0.0_amd64.deb",
            "sentinel_3.0.0_arm64.deb",
        )

    def test_it_publishes_a_rolling_and_a_per_merge_release(self) -> None:
        result = self.run_script("artifacts")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()

        # Rolling release: the tag follows the merge, the assets are replaced.
        self.assertIn(f"git tag -f latest-build {'a' * 40}", calls)
        self.assertIn("git push --force origin refs/tags/latest-build", calls)
        self.assertTrue(any(call.startswith("gh release create latest-build") for call in calls))
        self.assertTrue(any(call.startswith("gh release edit latest-build") for call in calls))
        upload = [call for call in calls if call.startswith("gh release upload latest-build")]
        self.assertEqual(len(upload), 1)
        self.assertIn("--clobber", upload[0])
        for name in (".deb", ".dmg", ".exe"):
            self.assertIn(name, upload[0])

        # Per-merge release: named from VERSION and the run number, target the
        # merge commit, and attached with the generated tag.
        create = [call for call in calls if call.startswith("gh release create v3.0.0-build.42")]
        self.assertEqual(len(create), 1, calls)
        self.assertIn(f"--target {'a' * 40}", create[0])
        self.assertIn("--prerelease", create[0])
        self.assertIn("Sentinel-Setup-3.0.0.exe", create[0])

    def test_both_releases_are_prereleases(self) -> None:
        # A build from main must never become /releases/latest.
        result = self.run_script("artifacts")
        self.assertEqual(result.returncode, 0, result.stderr)
        creates = [call for call in self.calls() if call.startswith("gh release create")]
        self.assertEqual(len(creates), 2, creates)
        for call in creates:
            self.assertIn("--prerelease", call)

    def test_an_existing_rolling_release_is_refreshed_not_recreated(self) -> None:
        result = self.run_script("artifacts", env={"STUB_RELEASE_EXISTS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertFalse(
            [call for call in calls if call.startswith("gh release create latest-build")],
            "an existing rolling release must keep its id (and asset URLs)",
        )
        self.assertTrue(
            [call for call in calls if call.startswith("gh release edit latest-build")]
        )

    def test_stale_assets_are_dropped_from_the_rolling_release(self) -> None:
        # e.g. last merge's 2.9.0 installers after a version bump.
        result = self.run_script(
            "artifacts",
            env={
                "STUB_RELEASE_EXISTS": "1",
                "STUB_ASSETS": "Sentinel-Setup-2.9.0.exe\nSentinel-Setup-3.0.0.exe",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        deleted = [call for call in self.calls() if call.startswith("gh release delete-asset")]
        self.assertEqual(
            deleted, ["gh release delete-asset latest-build Sentinel-Setup-2.9.0.exe --yes"]
        )

    def test_a_blocked_tag_push_falls_back_to_recreating_the_release(self) -> None:
        result = self.run_script("artifacts", env={"STUB_TAG_PUSH_FAILS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn(
            "gh release delete latest-build --yes --cleanup-tag",
            calls,
        )
        create = [call for call in calls if call.startswith("gh release create latest-build")]
        self.assertEqual(len(create), 1)
        self.assertIn(f"--target {'a' * 40}", create[0])

    def test_missing_installers_fail_before_anything_is_published(self) -> None:
        result = self.run_script("empty")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no installers", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_both_architectures_are_published(self) -> None:
        result = self.run_script("artifacts")
        self.assertEqual(result.returncode, 0, result.stderr)
        upload = [call for call in self.calls() if call.startswith("gh release upload latest-build")]
        self.assertEqual(len(upload), 1)
        for name in (
            "sentinel_3.0.0_amd64.deb",
            "sentinel_3.0.0_arm64.deb",
            "Sentinel-3.0.0-x86_64.dmg",
            "Sentinel-3.0.0-arm64.dmg",
            "Sentinel-Setup-3.0.0.exe",
            "Sentinel-Setup-3.0.0-arm64.exe",
        ):
            with self.subTest(asset=name):
                self.assertIn(name, upload[0])

    def test_a_partial_build_still_publishes_what_exists(self) -> None:
        shutil.rmtree(self.tmp / "artifacts")
        self.add_artifacts("sentinel_3.0.0_amd64.deb")
        result = self.run_script("artifacts")
        self.assertEqual(result.returncode, 0, result.stderr)
        upload = [call for call in self.calls() if call.startswith("gh release upload latest-build")]
        self.assertEqual(len(upload), 1)
        self.assertIn(".deb", upload[0])
        self.assertNotIn(".exe", upload[0])


class TagPublishScriptTests(PublishScriptTestCase):
    """`.github/scripts/publish-tag-release.sh` (release.yml)."""

    SCRIPT = "publish-tag-release.sh"

    def setUp(self) -> None:
        super().setUp()
        self.add_artifacts(
            "Sentinel-Setup-3.0.0.exe",
            "Sentinel-Setup-3.0.0-arm64.exe",
            "Sentinel-3.0.0-x86_64.dmg",
            "Sentinel-3.0.0-arm64.dmg",
            "sentinel_3.0.0_amd64.deb",
            "sentinel_3.0.0_arm64.deb",
        )

    def test_a_plain_version_tag_becomes_a_normal_release(self) -> None:
        result = self.run_script("artifacts", env={"GITHUB_REF_NAME": "v3.0.0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        creates = [call for call in self.calls() if call.startswith("gh release create")]
        self.assertEqual(len(creates), 1, creates)
        self.assertIn("--verify-tag", creates[0])
        self.assertIn("--generate-notes", creates[0])
        self.assertNotIn("--prerelease", creates[0])
        for name in (".deb", ".dmg", ".exe"):
            self.assertIn(name, creates[0])

    def test_a_hyphenated_tag_is_a_prerelease(self) -> None:
        result = self.run_script("artifacts", env={"GITHUB_REF_NAME": "v3.0.0-rc1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        creates = [call for call in self.calls() if call.startswith("gh release create")]
        self.assertIn("--prerelease", creates[0])

    def test_an_existing_release_gets_its_assets_replaced(self) -> None:
        result = self.run_script(
            "artifacts",
            env={"GITHUB_REF_NAME": "v3.0.0", "STUB_RELEASE_EXISTS": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertFalse([call for call in calls if "release create" in call])
        upload = [call for call in calls if call.startswith("gh release upload v3.0.0")]
        self.assertEqual(len(upload), 1)
        self.assertIn("--clobber", upload[0])

    def test_a_tag_that_disagrees_with_version_is_flagged(self) -> None:
        result = self.run_script("artifacts", env={"GITHUB_REF_NAME": "v9.9.9"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VERSION says 3.0.0", result.stderr)

    def test_a_release_prerelease_tag_matching_version_is_not_flagged(self) -> None:
        result = self.run_script("artifacts", env={"GITHUB_REF_NAME": "v3.0.0-rc1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("VERSION says", result.stderr)

    def test_it_refuses_to_run_without_a_tag(self) -> None:
        result = self.run_script("artifacts", env={"GITHUB_REF_NAME": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GITHUB_REF_NAME", result.stderr)

    def test_missing_installers_fail_before_anything_is_published(self) -> None:
        result = self.run_script("empty", env={"GITHUB_REF_NAME": "v3.0.0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no installers", result.stderr)
        self.assertEqual(self.calls(), [])


class WorkflowWiringTests(unittest.TestCase):
    """The workflow files must build in one place and publish on every merge."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = {
            name: read(WORKFLOWS / name)
            for name in (
                "build.yml",
                "build-installers.yml",
                "release.yml",
                "merge-release.yml",
            )
        }

    def test_merges_to_main_build_and_publish(self) -> None:
        merge = self.files["merge-release.yml"]
        block = triggers(merge)
        self.assertIn("push:", block)
        self.assertIn("branches: [main]", block)
        self.assertIn(f"uses: {REUSABLE}", merge)
        self.assertIn("publish-merge-release.sh", merge)
        self.assertIn("contents: write", merge)
        self.assertIn("download-artifact", merge)

    def test_the_reusable_workflow_builds_all_three_platforms(self) -> None:
        reusable = self.files["build-installers.yml"]
        self.assertIn("workflow_call", triggers(reusable))
        # Both architectures of all three platforms: PyInstaller cannot
        # cross-compile, so each one needs a runner of its own architecture.
        for runner, artifact in (
            ("ubuntu-24.04-arm", "sentinel-linux-arm64"),
            ("ubuntu-24.04", "sentinel-linux-amd64"),
            ("macos-latest", "sentinel-macos-arm64"),
            ("macos-15-intel", "sentinel-macos-x86_64"),
            ("windows-11-arm", "sentinel-windows-arm64"),
            ("windows-latest", "sentinel-windows-x64"),
        ):
            with self.subTest(runner=runner):
                self.assertIn(runner, reusable)
                self.assertIn(artifact, reusable)
        # No release logic belongs in the reusable build.
        self.assertNotIn("softprops/action-gh-release", reusable)
        self.assertNotIn("gh release", reusable)

    def test_every_architecture_leg_states_what_it_is_building(self) -> None:
        """A leg must fail on the wrong architecture, not mislabel it."""
        reusable = self.files["build-installers.yml"]
        # The build scripts compare this against the runner's interpreter and
        # against the binary they produce.
        self.assertIn("SENTINEL_TARGET_ARCH: ${{ matrix.arch }}", reusable)
        self.assertIn("check_arch.py host --expect ${{ matrix.arch }}", reusable)
        # One matrix per platform, and a failure on one architecture must not
        # cancel the other's result.
        yaml_only = without_comments(reusable)
        self.assertEqual(yaml_only.count("fail-fast: false"), 3)
        self.assertEqual(yaml_only.count("strategy:"), 3)
        self.assertEqual(yaml_only.count("- arch: "), 6)

    def test_the_windows_arm_leg_asks_for_a_native_interpreter(self) -> None:
        reusable = self.files["build-installers.yml"]
        self.assertIn("architecture: ${{ matrix.py_arch }}", reusable)
        # If the tool cache has no arm64 Python, the job installs one from
        # python.org rather than building with an emulated x64 interpreter.
        self.assertIn("install-windows-deps.ps1 ${{ matrix.arch }}", reusable)

    def test_the_linux_legs_use_the_shared_dependency_helper(self) -> None:
        reusable = self.files["build-installers.yml"]
        self.assertIn("install-linux-deps.sh", reusable)
        # The .deb's own metadata is checked against the leg's architecture.
        self.assertIn('test "$(dpkg-deb -f dist/*.deb Architecture)"', reusable)

    def test_build_steps_live_in_exactly_one_workflow(self) -> None:
        holders = [
            name
            for name, text in self.files.items()
            if any(step in text for step in PLATFORM_BUILDS)
        ]
        self.assertEqual(holders, ["build-installers.yml"], holders)

    def test_every_workflow_that_builds_calls_the_reusable_one(self) -> None:
        for name in ("build.yml", "release.yml", "merge-release.yml"):
            with self.subTest(workflow=name):
                self.assertIn(f"uses: {REUSABLE}", self.files[name])

    def test_pr_checks_survive_and_main_is_not_built_twice(self) -> None:
        build = triggers(self.files["build.yml"])
        self.assertIn("pull_request:", build)
        self.assertIn("branches: [main]", build.split("pull_request:", 1)[1])
        # Main pushes are merge-release.yml's job; building them here as well
        # would run all three platforms twice per merge.
        push = build.split("pull_request:", 1)[0]
        self.assertIn("push:", push)
        self.assertNotIn("main", push)

    def test_tagged_releases_still_trigger_on_v_tags(self) -> None:
        release = self.files["release.yml"]
        block = triggers(release)
        self.assertIn("push:", block)
        self.assertIn("tags:", block)
        self.assertIn("'v*'", block)
        self.assertIn("publish-tag-release.sh", release)
        self.assertIn("contents: write", release)
        # The generated per-merge tags (`v3.0.0-build.42`) are already
        # published by merge-release.yml; this workflow must skip them.
        self.assertIn("-build.", release)

    def test_publish_scripts_are_present_and_executable(self) -> None:
        for name in ("publish-merge-release.sh", "publish-tag-release.sh"):
            with self.subTest(script=name):
                path = SCRIPTS / name
                self.assertTrue(path.is_file(), f"missing {name}")
                self.assertTrue(os.access(path, os.X_OK), f"{name} is not executable")

    def test_the_publish_scripts_read_the_version_file(self) -> None:
        merge = read(SCRIPTS / "publish-merge-release.sh")
        self.assertIn('VERSION', merge)
        self.assertIn('GITHUB_RUN_NUMBER', merge)
        self.assertIn('latest-build', merge)

    def test_setup_workflows_lists_the_new_files(self) -> None:
        helper = read(REPO_ROOT / "scripts" / "setup_workflows.sh")
        for name in (
            ".github/workflows/build-installers.yml",
            ".github/workflows/merge-release.yml",
            ".github/scripts/publish-merge-release.sh",
            ".github/scripts/publish-tag-release.sh",
        ):
            with self.subTest(file=name):
                self.assertIn(name, helper)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
