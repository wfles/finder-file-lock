from contextlib import contextmanager
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory

from lock_file import RENAME_EXCL, rename_exclusive

DEFAULTS = "/usr/bin/defaults"
FINDER_DOMAIN = "com.apple.finder"
FINDER_RESOURCES = Path("/System/Library/CoreServices/Finder.app/Contents/Resources")
PBS = "/System/Library/CoreServices/pbs"
OWNER = "io.github.wfles.finder-file-lock"
FINDER_SETTINGS = (FINDER_DOMAIN, "NSUserKeyEquivalents")
SERVICE_SETTINGS = ("pbs", "NSServicesStatus")
ACTIONS = (
    ("文件加锁", "lock", "finder-lock", "l", "Library", "资源库"),
    ("解除编辑限制", "unlock", "finder-unlock", "u", "Utilities", "实用工具"),
)


def finder_titles(english, simplified):
    titles = {english, simplified}
    for localization in FINDER_RESOURCES.glob("*.lproj"):
        for table in ("Localizable.strings", "LocalizableMerged.strings"):
            try:
                data = plistlib.loads((localization / table).read_bytes())
                title = data.get(english)
                if isinstance(title, str) and title:
                    titles.add(title)
            except (OSError, ValueError):
                continue
    return titles


def key_code(value):
    if isinstance(value, str) and value:
        return "".join(sorted(value[:-1])) + value[-1]
    return ""


def shortcut_updates(current):
    updates, skipped = {}, []
    for name, _, _, letter, english, simplified in ACTIONS:
        desired = f"@${letter}"
        alternate = f"@~${letter}"
        titles = finder_titles(english, simplified)
        reserved = {name, *titles}
        occupied = any(
            key_code(value) == key_code(desired)
            for title, value in current.items()
            if title not in reserved
        )
        alternate_taken = any(
            key_code(value) == key_code(alternate)
            for title, value in current.items()
            if title not in reserved
        )
        move_default = [
            title for title in sorted(titles)
            if title not in current or key_code(current[title]) == key_code(desired)
        ]
        custom = name in current and key_code(current[name]) != key_code(desired)
        if custom or occupied or (alternate_taken and move_default):
            skipped.append(name)
            continue
        updates[name] = desired
        updates.update(dict.fromkeys(move_default, alternate))
    return updates, skipped


def read_preferences(domain, key):
    result = subprocess.run([DEFAULTS, "export", domain, "-"], capture_output=True, check=False)
    if result.returncode:
        return {}
    value = plistlib.loads(result.stdout).get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"Expected a dictionary for {domain}:{key}")
    return value


def add_preferences(domain, key, updates):
    if not updates:
        return
    settings = []
    for name, value in updates.items():
        settings.append(json.dumps(name, ensure_ascii=False))
        if isinstance(value, str):
            settings.append(json.dumps(value, ensure_ascii=False))
        else:
            settings.append(plistlib.dumps(value).decode("utf-8"))
    subprocess.run([DEFAULTS, "write", domain, key, "-dict-add", *settings], check=True)


def service_updates(current):
    updates = {}
    for name, *_ in ACTIONS:
        key = f"(null) - {name} - runWorkflowAsService"
        entry = dict(current.get(key, {}))
        entry["presentation_modes"] = {
            "ContextMenu": True, "FinderPreview": True, "ServicesMenu": True,
            "TouchBar": entry.get("presentation_modes", {}).get("TouchBar", False),
        }
        updates[key] = entry
    return updates


def remember_changes(records, current, updates):
    for name, value in updates.items():
        previous = records.get(name)
        if previous and previous["after"] == current.get(name):
            previous["after"] = value
        elif current.get(name) != value:
            record = {"after": value}
            if name in current:
                record["before"] = current[name]
            records[name] = record


def restore_changes(current, records):
    for name, record in records.items():
        if name not in current or current[name] != record["after"]:
            continue
        if "before" in record:
            current[name] = record["before"]
        else:
            del current[name]


def write_state(path, state):
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            plistlib.dump(state, stream)
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def owned_workflow(bundle):
    if not bundle.is_dir() or bundle.is_symlink():
        return False
    try:
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    except (OSError, ValueError):
        return False
    return info.get("FinderFileLockOwner") == OWNER


@contextmanager
def install_workflows(root, services):
    services.mkdir(parents=True, exist_ok=True)
    helper = root / "bin" / "setup-icon"

    with TemporaryDirectory(prefix=".finder-file-lock-", dir=services.parent) as directory:
        staging = Path(directory)
        planned = []
        for name, mode, icon, *_ in ACTIONS:
            filename = name + ".workflow"
            destination = services / filename
            if (destination.exists() or destination.is_symlink()) and not owned_workflow(destination):
                raise FileExistsError(f"已有同名快速操作「{name}」，请移开旧工作流后再安装")
            bundle = staging / filename
            shutil.copytree(root / "templates" / mode, bundle)
            image = bundle / "Contents" / "Resources" / (icon + ".icns")
            subprocess.run([helper, image, bundle], check=True)
            planned.append((bundle, destination))

        moved, installed = [], []
        try:
            for bundle, destination in planned:
                if owned_workflow(destination):
                    saved = staging / (destination.name + ".previous")
                    destination.rename(saved)
                    moved.append((saved, destination))
                # Exclusive rename closes the gap between the ownership check and publishing.
                if rename_exclusive(os.fsencode(bundle), os.fsencode(destination), RENAME_EXCL):
                    raise OSError("无法安装快速操作", destination)
                installed.append((destination, bundle))
            yield
        except BaseException:
            for destination, bundle in reversed(installed):
                destination.rename(bundle)
            for saved, destination in reversed(moved):
                saved.rename(destination)
            raise


def refresh_services(root):
    subprocess.run([PBS, "-update"], check=False)
    subprocess.run([root / "bin" / "setup-icon", "refresh"], check=False)


def read_state(path):
    if not path.exists():
        return {}
    return plistlib.loads(path.read_bytes())


def restore_preferences(settings, records):
    current = read_preferences(*settings)
    updated = current.copy()
    restore_changes(updated, records)
    if updated != current:
        subprocess.run(
            [DEFAULTS, "write", *settings, plistlib.dumps(updated).decode("utf-8")],
            check=True,
        )


def restart_finder():
    finder = subprocess.run(
        ["/usr/bin/pgrep", "-u", str(os.getuid()), "-x", "Finder"],
        capture_output=True, text=True, check=False,
    )
    for pid in finder.stdout.split():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            continue


def install(root, services):
    state_path = root / "installation-state.plist"
    with install_workflows(root, services):
        finder = read_preferences(*FINDER_SETTINGS)
        statuses = read_preferences(*SERVICE_SETTINGS)
        shortcuts, skipped = shortcut_updates(finder)
        entries = service_updates(statuses)
        state = read_state(state_path)
        remember_changes(state.setdefault("finder", {}), finder, shortcuts)
        remember_changes(state.setdefault("services", {}), statuses, entries)
        write_state(state_path, state)
        add_preferences(*FINDER_SETTINGS, shortcuts)
        add_preferences(*SERVICE_SETTINGS, entries)
        refresh_services(root)

    report = root / "安装结果.txt"
    report.write_text(
        "两个快速操作和图标已安装。\n"
        + (("下列动作未设置默认快捷键，可从右键菜单使用：" + "、".join(skipped) + "。\n")
           if skipped else "两个访达快捷键已设置。\n"),
        encoding="utf-8",
    )


def uninstall(root, services):
    state_path = root / "installation-state.plist"
    state = read_state(state_path)
    restore_preferences(FINDER_SETTINGS, state.get("finder", {}))
    restore_preferences(SERVICE_SETTINGS, state.get("services", {}))
    for name, *_ in ACTIONS:
        bundle = services / (name + ".workflow")
        if owned_workflow(bundle):
            shutil.rmtree(bundle)
    state_path.unlink(missing_ok=True)
    refresh_services(root)


def main():
    root = Path(__file__).resolve().parent.parent
    services = Path.home() / "Library" / "Services"
    if sys.argv[1:] == ["--uninstall"]:
        uninstall(root, services)
        print("已移除 Finder File Lock 的快速操作，并恢复仍由它设置的访达快捷键。")
    elif not sys.argv[1:]:
        install(root, services)
    else:
        return 2
    restart_finder()
    return 0


if __name__ == "__main__":
    sys.exit(main())
