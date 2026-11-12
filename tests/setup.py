#!/usr/bin/env python3
from contextlib import ExitStack
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "actions"))
import install_workflows as setup
import local_password as passwords


def test_passwords(root):
    private = root / "private"
    with patch.object(passwords, "ask", side_effect=["空格", "Mismatch1", "Mismatch2", "FixtureTest-783", "FixtureTest-783"]) as prompt:
        assert passwords.get_password(private, prompt=True) == "FixtureTest-783"
        assert prompt.call_count == 5
    with patch.object(passwords, "ask", side_effect=AssertionError("already configured")):
        assert passwords.get_password(private, prompt=True) == "FixtureTest-783"
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE((private / "permissions-password").stat().st_mode) == 0o600
    with patch.object(passwords, "ask", side_effect=passwords.PasswordCancelled):
        try:
            passwords.get_password(root / "cancelled", prompt=True)
        except passwords.PasswordCancelled:
            pass
        else:
            raise AssertionError("Cancelled setup did not stop")
        assert not (root / "cancelled/permissions-password").exists()


def test_shortcuts():
    updates, skipped = setup.shortcut_updates({})
    assert not skipped
    for key, value in {
        "文件加锁": "@$l", "解除编辑限制": "@$u", "Library": "@~$l",
        "Utilities": "@~$u", "资源库": "@~$l", "实用工具": "@~$u",
    }.items():
        assert updates[key] == value
    updates, skipped = setup.shortcut_updates({"Existing": "$@l", "Utilities": "@^u", "Other": "@$z"})
    assert skipped == ["文件加锁"] and "文件加锁" not in updates
    assert "Utilities" not in updates and "Other" not in updates and updates["解除编辑限制"] == "@$u"
    updates, skipped = setup.shortcut_updates({"解除编辑限制": "@^u"})
    assert "解除编辑限制" not in updates and skipped == ["解除编辑限制"]


def test_install(payload, root):
    runtime = root / "Application Support/FinderFileLock"
    runtime.mkdir(parents=True)
    for name in ("bin", "templates"):
        (runtime / name).symlink_to(payload / name)
    services = root / "Services"
    services.mkdir()
    old = services / "文件加锁.workflow"
    old.mkdir()
    (old / "keep.txt").write_text("existing workflow")
    try:
        with setup.install_workflows(runtime, services):
            raise AssertionError("Unrelated same-name workflow was overwritten")
    except FileExistsError:
        assert (old / "keep.txt").read_text() == "existing workflow"
        assert not (services / "解除编辑限制.workflow").exists()
    old.rename(root / "Personal.workflow")

    domain = f"io.github.wfles.finder-file-lock.test{os.getpid()}"
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(setup, "FINDER_SETTINGS", (domain, "NSUserKeyEquivalents")))
            stack.enter_context(patch.object(setup, "SERVICE_SETTINGS", (domain, "NSServicesStatus")))
            stack.enter_context(patch.object(setup, "refresh_services"))
            setup.add_preferences(*setup.FINDER_SETTINGS, {"Prior": "@$z", "Utilities": "@^u"})
            setup.install(runtime, services)
            setup.install(runtime, services)
            for name, mode, icon, *_ in setup.ACTIONS:
                bundle = services / (name + ".workflow")
                assert setup.owned_workflow(bundle)
                assert subprocess.check_output(["/usr/bin/xattr", "-p", "com.apple.FinderInfo", bundle])
                assert subprocess.check_output(["/usr/bin/xattr", "-px", "com.apple.ResourceFork", bundle / "Icon\r"])
                service = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())["NSServices"][0]
                assert service["NSRequiredContext"]["NSApplicationIdentifier"] == "com.apple.finder"
                assert service["NSIconName"] == icon
                workflow = plistlib.loads((bundle / "Contents/document.wflow").read_bytes())
                command = workflow["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"]
                assert command.endswith(f'run-action" {mode} "$@"')
            preferences = setup.read_preferences(*setup.FINDER_SETTINGS)
            assert preferences["文件加锁"] == "@$l" and preferences["解除编辑限制"] == "@$u"
            assert preferences["Utilities"] == "@^u"
            statuses = setup.read_preferences(*setup.SERVICE_SETTINGS)
            assert statuses["(null) - 文件加锁 - runWorkflowAsService"]["presentation_modes"]["ContextMenu"] is True
            setup.add_preferences(*setup.FINDER_SETTINGS, {"解除编辑限制": "@^u"})
            setup.uninstall(runtime, services)
            assert not list(services.iterdir())
            assert (root / "Personal.workflow/keep.txt").exists()
            assert setup.read_preferences(*setup.FINDER_SETTINGS) == {
                "Prior": "@$z", "Utilities": "@^u", "解除编辑限制": "@^u"
            }
            assert not setup.read_preferences(*setup.SERVICE_SETTINGS)
    finally:
        subprocess.run([setup.DEFAULTS, "delete", domain], check=False)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="finder-file-lock-setup-") as temp:
        root = Path(temp)
        test_passwords(root)
        test_shortcuts()
        test_install(Path(sys.argv[1]).resolve(), root)
    print("Same-name workflow protection, Finder-only preferences, icons, upgrade, password setup and uninstall passed")
