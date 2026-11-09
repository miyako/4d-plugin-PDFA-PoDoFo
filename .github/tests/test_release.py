import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


REPO = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("TEST_SOURCE_ROOT", REPO))
spec = importlib.util.spec_from_file_location("release", ROOT / ".github/scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
SHA = "a" * 40


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "PDFA").mkdir()
        self.write_version("7.8.9")

    def write_version(self, version, bundle=None):
        (self.root / "VERSION").write_text(version + "\n")
        (self.root / "PDFA/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleVersion": version if bundle is None else bundle,
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": "PDFA",
        }))

    def test_derivation_and_all_bumps(self):
        for mode, expected in (("patch", "7.8.10"), ("minor", "7.9.0"), ("major", "8.0.0")):
            with self.subTest(mode=mode):
                self.write_version("7.8.9")
                original = (self.root / "PDFA/Info.plist").read_text()
                self.assertEqual(release.source_version(self.root), "7.8.9")
                self.assertEqual(release.bump(self.root, mode), expected)
                self.assertEqual((self.root / "PDFA/Info.plist").read_text(),
                                 original.replace("<string>7.8.9</string>",
                                                  f"<string>{expected}</string>"))

    def test_invalid_versions(self):
        for version in ("", "1.2", "v1.2.3", "01.2.3", "1.02.3", "1.2.03",
                        "1.2.3-beta", "1.2.3+4", "1.2.3 ", "1.2.3\n", "１.2.3"):
            with self.subTest(version=version):
                self.write_version(version)
                with self.assertRaises(ValueError):
                    release.source_version(self.root)

    def test_drift_fails_before_writes(self):
        self.write_version("7.8.9", "7.8.8")
        before = {p: p.read_bytes() for p in (self.root / "VERSION",
                                             self.root / "PDFA/Info.plist")}
        with self.assertRaisesRegex(ValueError, "match"):
            release.bump(self.root, "patch")
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_missing_or_malformed_plist(self):
        path = self.root / "PDFA/Info.plist"
        path.write_bytes(plistlib.dumps({"CFBundleName": "PDFA"}))
        with self.assertRaises(ValueError):
            release.source_version(self.root)
        path.write_text("invalid plist")
        with self.assertRaises(plistlib.InvalidFileException):
            release.source_version(self.root)

    def test_live_consistency(self):
        expected = (ROOT / "VERSION").read_text().removesuffix("\n")
        self.assertRegex(expected, r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
        self.assertEqual(release.source_version(ROOT), expected)
        self.assertEqual(plistlib.loads((ROOT / "PDFA/Info.plist").read_bytes())[
            "CFBundleVersion"], expected)

    def test_suite_survives_temporary_copy_bump(self):
        if os.environ.get("TEST_SOURCE_ROOT"):
            return
        copy = self.root / "copy"
        for name in ("VERSION", "PDFA/Info.plist", ".github/scripts/release.py",
                     ".github/workflows/release.yml", ".github/workflows/bump-version.yml",
                     ".github/workflows/test.yml"):
            target = copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, target)
        original = release.source_version(copy)
        release.bump(copy, "patch")
        self.assertNotEqual(release.source_version(copy), original)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(REPO / ".github/tests"), "-v"],
            env={**os.environ, "TEST_SOURCE_ROOT": str(copy), "PYTHONDONTWRITEBYTECODE": "1"},
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def api_result(status, code=1):
    return subprocess.CompletedProcess([], code, f"HTTP/2.0 {status} Test\n\n{{}}\n", "API error")


def release_pages(*pages):
    return subprocess.CompletedProcess([], 0, json.dumps(pages), "")


class AvailabilityTests(unittest.TestCase):
    @patch.object(release.subprocess, "run")
    def test_only_confirmed_missing_tag_and_release_are_available(self, run):
        run.side_effect = [api_result(200, 0), api_result(404), release_pages([])]
        release.check_available("owner/repo", "v7.8.9")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0], ["gh", "api", "repos/owner/repo"])
        self.assertIn("repos/owner/repo/git/ref/tags/v7.8.9", run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[2].args[0], [
            "gh", "api", "--paginate", "--slurp", "repos/owner/repo/releases?per_page=100",
        ])

    @patch.object(release.subprocess, "run")
    def test_existing_tag_or_release(self, run):
        for results in ([api_result(200, 0)], [api_result(404), release_pages([
                {"tag_name": "v7.8.9", "draft": False}])]):
            run.side_effect = [api_result(200, 0)] + results
            with self.assertRaisesRegex(ValueError, "already exists"):
                release.check_available("owner/repo", "v7.8.9")

    @patch.object(release.subprocess, "run")
    def test_api_errors_fail_closed_on_either_lookup(self, run):
        for error in (api_result(401), api_result(403), api_result(429), api_result(500),
                      subprocess.CompletedProcess([], 1, "", "network failed"),
                      subprocess.CompletedProcess([], 0, "invalid response", ""),
                      api_result(404, 0)):
            for prefix in ([], [api_result(404)]):
                run.side_effect = [api_result(200, 0)] + prefix + [error]
                with self.assertRaisesRegex(RuntimeError, "Cannot establish"):
                    release.check_available("owner/repo", "v7.8.9")

    @patch.object(release.subprocess, "run")
    def test_same_tag_draft_without_git_tag_on_page_two_rejects(self, run):
        run.side_effect = [api_result(200, 0), api_result(404), release_pages(
            [{"tag_name": "v6.0.0", "draft": False}],
            [{"tag_name": "v7.8.9", "draft": True}],
        )]
        with self.assertRaisesRegex(ValueError, "published or draft"):
            release.check_available("owner/repo", "v7.8.9")

    @patch.object(release.subprocess, "run")
    def test_complete_paginated_no_match_including_other_drafts_is_available(self, run):
        run.side_effect = [api_result(200, 0), api_result(404), release_pages(
            [{"tag_name": "v6.0.0", "draft": False}],
            [{"tag_name": "v8.0.0", "draft": True}],
        )]
        release.check_available("owner/repo", "v7.8.9")

    @patch.object(release.subprocess, "run")
    def test_listing_failure_after_partial_page_is_not_absence(self, run):
        run.side_effect = [api_result(200, 0), api_result(404), subprocess.CompletedProcess(
            [], 1, json.dumps([[{"tag_name": "v6.0.0", "draft": False}]]),
            "page 2 failed",
        )]
        with self.assertRaisesRegex(RuntimeError, "page 2 failed"):
            release.check_available("owner/repo", "v7.8.9")

    @patch.object(release.subprocess, "run")
    def test_malformed_listing_is_not_absence(self, run):
        for data in ("", "not JSON", "null", "{}", "[]", "[{}]", "[null]",
                     "[[null]]", "[[{}]]", '[[{"tag_name": null, "draft": true}]]',
                     '[[{"tag_name": "v6.0.0"}]]',
                     '[[{"tag_name": "v6.0.0", "draft": "true"}]]',
                     '[[], {"message": "page 2 failed"}]'):
            with self.subTest(data=data):
                run.side_effect = [api_result(200, 0), api_result(404),
                                   subprocess.CompletedProcess([], 0, data, "")]
                with self.assertRaisesRegex(RuntimeError, "malformed"):
                    release.check_available("owner/repo", "v7.8.9")

    @patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess(
        [], 1, "", "repository access failed"))
    def test_repository_access_errors_are_not_absence(self, run):
        with self.assertRaisesRegex(RuntimeError, "repository access failed"):
            release.check_available("owner/repo", "v7.8.9")
        self.assertEqual(run.call_count, 1)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.asset = Path(self.temp.name) / "PDFA.zip"
        self.asset.write_bytes(b"package")

    @patch.object(release, "publish")
    def test_publication_cli_passes_selected_sha_and_assets(self, publish):
        argv = ["release.py", "publish", "--root", str(ROOT), "--sha", SHA, str(self.asset)]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
            with contextlib.redirect_stdout(io.StringIO()):
                release.main()
        publish.assert_called_once_with(ROOT, "owner/repo", SHA, [str(self.asset)])

    @patch.object(release, "selected_checkout")
    @patch.object(release, "check_available")
    def test_preflight_validates_selected_sha(self, available, selected):
        with patch.object(sys, "argv", ["release.py", "available", "--root", str(ROOT),
                                       "--sha", SHA]), patch.dict(
                os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
            with contextlib.redirect_stdout(io.StringIO()):
                release.main()
        selected.assert_called_once_with(ROOT, SHA)
        available.assert_called_once_with("owner/repo", "v" + release.source_version(ROOT))

    @patch.object(release.subprocess, "run")
    @patch.object(release.subprocess, "check_output", return_value=SHA + "\n")
    @patch.object(release, "check_available")
    def test_recheck_then_nonforce_tag_exact_sha_then_create(self, available, head, run):
        events = []
        requests = []
        available.side_effect = lambda *args: events.append("check")

        def record(command, **kwargs):
            events.append(command)
            requests.append(kwargs)

        run.side_effect = record
        release.publish(ROOT, "owner/repo", SHA, [str(self.asset)])
        tag = "v" + release.source_version(ROOT)
        self.assertEqual(events[0], "check")
        self.assertEqual(events[1], ["git", "push", "--no-follow-tags", "origin",
                                   f"{SHA}:refs/tags/{tag}"])
        self.assertEqual(events[2], ["gh", "api", "--method", "POST",
                                     "repos/owner/repo/releases", "--input", "-", "--silent"])
        self.assertEqual(json.loads(requests[1]["input"]), {
            "tag_name": tag, "target_commitish": SHA, "name": tag,
            "draft": False, "prerelease": False,
        })
        self.assertEqual(events[3], ["gh", "release", "upload", tag, "--repo",
                                     "owner/repo", str(self.asset)])

    @patch.object(release.subprocess, "run")
    @patch.object(release.subprocess, "check_output", return_value=SHA + "\n")
    @patch.object(release, "check_available", side_effect=ValueError("exists"))
    def test_failed_recheck_has_no_mutation(self, available, head, run):
        with self.assertRaises(ValueError):
            release.publish(ROOT, "owner/repo", SHA, [str(self.asset)])
        run.assert_not_called()

    @patch.object(release.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git push"))
    @patch.object(release.subprocess, "check_output", return_value=SHA + "\n")
    @patch.object(release, "check_available")
    def test_push_race_stops_release_creation(self, available, head, run):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(subprocess.CalledProcessError):
            release.publish(ROOT, "owner/repo", SHA, [str(self.asset)])
        self.assertEqual(run.call_count, 1)
        self.assertIn("remote state may be uncertain", stderr.getvalue())

    @patch.object(release.subprocess, "run")
    @patch.object(release.subprocess, "check_output", return_value=SHA + "\n")
    @patch.object(release, "check_available")
    def test_partial_failure_is_explicit_and_never_deletes(self, available, head, run):
        for successes in (1, 2):
            with self.subTest(successes=successes):
                run.reset_mock()
                run.side_effect = [None] * successes + [subprocess.CalledProcessError(1, "gh")]
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(subprocess.CalledProcessError):
                    release.publish(ROOT, "owner/repo", SHA, [str(self.asset)])
                self.assertIn("was pushed", stderr.getvalue())
                self.assertEqual(run.call_count, successes + 1)
                commands = [call.args[0] for call in run.call_args_list]
                for command in commands:
                    self.assertNotIn("DELETE", command)
                    self.assertNotIn("--clobber", command)
                    self.assertNotEqual(command[:3], ["gh", "release", "create"])

    @patch.object(release.subprocess, "run")
    @patch.object(release.subprocess, "check_output", return_value="b" * 40)
    def test_moving_head_invalid_sha_and_missing_assets_fail(self, head, run):
        for sha in ("main", SHA):
            with self.assertRaises(ValueError):
                release.publish(ROOT, "owner/repo", sha, [str(self.asset)])
        head.return_value = SHA
        for assets in ([], [str(self.asset) + ".missing"]):
            with self.assertRaises(ValueError):
                release.publish(ROOT, "owner/repo", SHA, assets)
        self.asset.write_bytes(b"")
        with self.assertRaises(ValueError):
            release.publish(ROOT, "owner/repo", SHA, [str(self.asset)])
        run.assert_not_called()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        # BaseLoader keeps GitHub's "on" key a string rather than YAML 1.1 true.
        self.build = yaml.load((ROOT / ".github/workflows/release.yml").read_text(),
                               Loader=yaml.BaseLoader)
        self.bump = yaml.load((ROOT / ".github/workflows/bump-version.yml").read_text(),
                              Loader=yaml.BaseLoader)

    def test_manual_no_input_and_global_serialization(self):
        self.assertEqual(self.build["name"], "Build")
        self.assertEqual(self.build["on"], {"workflow_dispatch": ""})
        self.assertEqual(self.build["concurrency"],
                         {"group": "release-publication", "cancel-in-progress": "false"})
        for job in self.build["jobs"].values():
            self.assertNotIn("if", job)
            self.assertNotIn("environment", job)

    def test_all_build_checkouts_are_immutable(self):
        self.assertEqual(set(self.build["jobs"]), {"version", "build-windows", "build-macos"})
        for job in self.build["jobs"].values():
            checkouts = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
            self.assertEqual(len(checkouts), 1)
            self.assertEqual(checkouts[0]["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(self.build["jobs"]["build-windows"]["needs"], "version")
        self.assertEqual(self.build["jobs"]["build-macos"]["needs"], ["version", "build-windows"])
        preflight = self.build["jobs"]["version"]["steps"][-1]
        self.assertIn('release.py available --sha "$SELECTED_SHA"', preflight["run"])
        self.assertEqual(preflight["env"]["SELECTED_SHA"], "${{ github.sha }}")

    def test_publish_is_last_and_after_every_gate(self):
        steps = self.build["jobs"]["build-macos"]["steps"]
        names = [s.get("name", "") for s in steps]
        self.assertIn("release.py publish", steps[-1]["run"])
        self.assertEqual(steps[-1]["env"]["SELECTED_SHA"], "${{ github.sha }}")
        for name in ("verify bundle exports and resources", "codesign",
                     "package, notarize & staple", "upload verified release packages"):
            self.assertLess(names.index(name), len(steps) - 1)
        self.assertIn("codesign --verify", steps[names.index("codesign")]["run"])
        package = steps[names.index("package, notarize & staple")]["run"]
        self.assertIn("stapler validate", package)
        self.assertIn('== "Accepted"', package)
        self.assertEqual(package.count("notarytool submit"), 2)
        for step in steps:
            self.assertNotIn("continue-on-error", step)
            self.assertNotIn("if", step)

    def test_native_build_and_secret_contract(self):
        windows = self.build["jobs"]["build-windows"]["steps"]
        body = "\n".join(step.get("run", "") for step in windows)
        for expected in ("vcpkg.exe install podofo:x64-windows-static-release",
                         "verify-windows-deps.ps1", "cmake --build . --config Release",
                         "VCPKG_OVERLAY_TRIPLETS"):
            self.assertIn(expected, body)
        mac = self.build["jobs"]["build-macos"]["steps"]
        self.assertIn("brew install podofo", "\n".join(s.get("run", "") for s in mac))
        self.assertEqual({value for step in mac for value in step.get("env", {}).values()
                          if "secrets." in value}, {
            "${{ secrets.APPLE_DEVELOPER_ID_CERTIFICATE }}",
            "${{ secrets.APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD }}",
            "${{ secrets.KEYCHAIN_PASSWORD }}", "${{ secrets.NOTARYTOOL_APPLE_ID }}",
            "${{ secrets.NOTARYTOOL_TEAM_ID }}", "${{ secrets.NOTARYTOOL_PASSWORD }}",
        })
        self.assertEqual(self.build["env"]["MACOS_DEPLOYMENT_TARGET"], "10.13")

    def test_bump_metadata_only_selected_branch(self):
        self.assertEqual(self.bump["on"]["workflow_dispatch"]["inputs"]["bump"]["options"],
                         ["patch", "minor", "major"])
        self.assertEqual(self.bump["concurrency"]["group"], "bump-version-${{ github.ref }}")
        self.assertEqual(self.bump["concurrency"]["cancel-in-progress"], "false")
        job = self.bump["jobs"]["bump"]
        self.assertEqual(job["if"], "github.ref_type == 'branch'")
        steps = job["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(steps[1]["env"]["BUMP"], "${{ inputs.bump }}")
        self.assertEqual(steps[-1]["env"]["BRANCH"], "${{ github.ref_name }}")
        body = "\n".join(step.get("run", "") for step in steps)
        self.assertIn("git add VERSION PDFA/Info.plist", body)
        self.assertIn('git push --no-follow-tags origin "HEAD:refs/heads/$BRANCH"', body)
        for forbidden in ("git tag", "--tags", "--force", "gh release", "workflow run"):
            self.assertNotIn(forbidden, body)

    def test_unrelated_ci_remains_automatic(self):
        ci = yaml.load((ROOT / ".github/workflows/test.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(ci["on"], {"push": {"branches": ["main"]},
                                    "pull_request": {"branches": ["main"]}})


class GitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.work = self.root / "work"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", str(self.work)], check=True)
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("remote", "add", "origin", str(self.remote))
        (self.work / "PDFA").mkdir()
        (self.work / "VERSION").write_text("7.8.9\n")
        (self.work / "PDFA/Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": "7.8.9"}))
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git("push", "-q", "origin", "HEAD:refs/heads/selected")
        self.git("config", "push.followTags", "true")
        self.git("tag", "-am", "must not follow", "unrelated")

    def git(self, *args, check=True):
        result = subprocess.run(["git", *args], cwd=self.work, text=True,
                                capture_output=True, check=check)
        return result.stdout

    def test_bump_selected_branch_no_tags_and_stale_push_rejected(self):
        release.bump(self.work, "patch")
        self.git("add", "VERSION", "PDFA/Info.plist")
        self.git("commit", "-qm", "metadata only")
        first = self.git("rev-parse", "HEAD").strip()
        self.git("push", "--no-follow-tags", "origin", "HEAD:refs/heads/selected")
        self.assertEqual(self.git("ls-remote", "--tags", "origin"), "")
        self.assertIn(first, self.git("ls-remote", "origin", "refs/heads/selected"))
        self.git("checkout", "--detach", self.sha)
        release.bump(self.work, "minor")
        self.git("add", "VERSION", "PDFA/Info.plist")
        self.git("commit", "-qm", "stale metadata")
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("push", "--no-follow-tags", "origin", "HEAD:refs/heads/selected")
        self.assertIn(first, self.git("ls-remote", "origin", "refs/heads/selected"))

    def test_publish_exact_sha_and_remote_tag_race_never_overwrites(self):
        asset = self.root / "PDFA.zip"
        asset.write_bytes(b"verified fixture")
        real_run = subprocess.run
        commands = []

        def intercept(command, **kwargs):
            commands.append(command)
            if command[0] == "gh":
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, **kwargs)

        with patch.object(release, "check_available"), patch.object(
                release.subprocess, "run", side_effect=intercept):
            release.publish(self.work, "owner/repo", self.sha, [str(asset)])
        self.assertEqual(self.git("ls-remote", "--tags", "origin"),
                         f"{self.sha}\trefs/tags/v7.8.9\n")
        (self.work / "change").write_text("another commit")
        self.git("add", ".")
        self.git("commit", "-qm", "new selected SHA")
        later = self.git("rev-parse", "HEAD").strip()
        commands.clear()
        with patch.object(release, "check_available"), patch.object(
                release.subprocess, "run", side_effect=intercept):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(subprocess.CalledProcessError):
                release.publish(self.work, "owner/repo", later, [str(asset)])
        self.assertFalse(any(c[0] == "gh" for c in commands))
        self.assertEqual(self.git("ls-remote", "--tags", "origin"),
                         f"{self.sha}\trefs/tags/v7.8.9\n")


class PackageGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(),
                             Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["build-macos"]["steps"]
        self.script = next(s["run"] for s in steps if s.get("name") == "package, notarize & staple")
        self.script = self.script.replace("${{ needs.version.outputs.version }}", "7.8.9")
        self.script = self.script.replace("${{ env.PRODUCT_NAME }}", "PDFA")
        self.output = self.root / "env"
        self.log = self.root / "commands"
        stub = self.root / "stub"
        stub.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as stream:
    stream.write(name + " " + " ".join(args) + "\n")
if name in ("ditto", "hdiutil"):
    Path(args[-1]).write_bytes(b"fixture package")
elif args[:2] == ["notarytool", "submit"]:
    kind = "ZIP" if args[2].endswith(".zip") else "DMG"
    status = os.environ.get(kind + "_STATUS", "Accepted")
    if status == "command-failed":
        sys.exit(1)
    if status == "malformed":
        print("not JSON")
    else:
        print(json.dumps({} if status == "missing" else {"status": status}))
elif len(args) > 1 and args[0] == "stapler" and os.environ.get("FAIL_STAPLER") == args[1]:
    sys.exit(1)
''')
        stub.chmod(0o755)
        for name in ("ditto", "hdiutil", "xcrun"):
            (self.root / name).symlink_to(stub)

    def run_package(self, **overrides):
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", self.script], cwd=self.root,
            env={**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                 "RUNNER_TEMP": str(self.root), "GITHUB_ENV": str(self.output),
                 "COMMAND_LOG": str(self.log), "BUNDLE_PATH": "fixture.bundle",
                 "KEYCHAIN_PATH": "fixture.keychain", **overrides},
            text=True, capture_output=True,
        )

    def test_accepted_packages_staple_validate_and_export_asset_names(self):
        result = self.run_package()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(),
                         "ZIP_PATH=PDFA-7.8.9.zip\nDMG_PATH=PDFA-7.8.9.dmg\n")
        self.assertIn("xcrun stapler staple PDFA-7.8.9.dmg", self.log.read_text())
        self.assertIn("xcrun stapler validate PDFA-7.8.9.dmg", self.log.read_text())

    def test_rejected_missing_malformed_or_failed_notarization_stops_packaging(self):
        for kind in ("ZIP", "DMG"):
            for status in ("Invalid", "In Progress", "missing", "malformed", "command-failed"):
                with self.subTest(kind=kind, status=status):
                    self.log.write_text("")
                    result = self.run_package(**{kind + "_STATUS": status})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("stapler", self.log.read_text())
                    self.assertFalse(self.output.exists())

    def test_stapling_and_validation_failures_do_not_export_assets(self):
        for operation in ("staple", "validate"):
            with self.subTest(operation=operation):
                result = self.run_package(FAIL_STAPLER=operation)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
