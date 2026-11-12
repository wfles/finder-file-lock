import ctypes
import errno
import json
import os
from collections import Counter
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "runtime" / "python-packages"))
QPDF = str(Path(sys.executable).resolve().parents[2] / "qpdf" / "bin" / "qpdf")
from local_password import PasswordCancelled, get_password
SUFFIX = "_已加锁"
RENAME_EXCL = 0x00000004
OFFSET_WARNING = re.compile(
    r"WARNING: input PDF \(object [0-9]+ [0-9]+\): object has offset 0 - "
    r"a common error handled correctly by qpdf and most other applications"
)
WARNING_SUMMARY = "qpdf: operation succeeded with warnings; resulting file may have some problems"

rename_exclusive = ctypes.CDLL(None, use_errno=True).renamex_np
rename_exclusive.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
rename_exclusive.restype = ctypes.c_int


class FileSkipped(Exception):
    pass


def run_qpdf(*arguments, job=None):
    return subprocess.run(
        [QPDF, *arguments] if job is None else [QPDF, "--job-json-file=/dev/stdin"],
        input=json.dumps(job) if job is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def publish_copy(temporary, source, extension=".pdf"):
    stem = source.stem + SUFFIX
    number = 1
    while True:
        version = "" if number == 1 else f"_{number}"
        destination = source.with_name(f"{stem}{version}{extension}")
        if rename_exclusive(os.fsencode(temporary), os.fsencode(destination), RENAME_EXCL) == 0:
            return
        error = ctypes.get_errno()
        if error != errno.EEXIST:
            raise OSError(error, os.strerror(error), str(destination))
        number += 1


def only_zero_offset_warnings(diagnostics, source):
    diagnostics = diagnostics.replace(str(source), "input PDF")
    warnings = [
        line for line in diagnostics.splitlines()
        if line.strip() and line != WARNING_SUMMARY
    ]
    return bool(warnings) and all(OFFSET_WARNING.fullmatch(line) for line in warnings)


def encryption_error(diagnostics):
    diagnostic = diagnostics.lower()
    if "no space left on device" in diagnostic:
        return "存储空间不足，无法保存副本"
    if "permission denied" in diagnostic or "operation not permitted" in diagnostic:
        return "没有权限读取文件或在原文件夹保存副本"
    if "can't find pdf header" in diagnostic or "unable to find trailer dictionary" in diagnostic:
        return "PDF 结构损坏，无法读取"
    if "invalid password" in diagnostic:
        return "文件已有密码或权限限制"
    return "加密工具未能完成处理"


def lock_pdf(source, password):
    if source.suffix.lower() != ".pdf" or not source.is_file():
        raise FileSkipped("请选择 PDF 文件")

    status = run_qpdf("--is-encrypted", str(source))
    if status.returncode == 0:
        raise FileSkipped("文件已有密码或权限限制")

    with TemporaryDirectory(prefix=".pdf-lock-", dir=source.parent) as directory:
        temporary = Path(directory) / "copy.pdf"
        encrypted = run_qpdf(job={
            "inputFile": str(source),
            "outputFile": str(temporary),
            "streamData": "preserve",
            "encrypt": {
                "userPassword": "",
                "ownerPassword": password,
                "256bit": {"modify": "none", "print": "full", "extract": "y"},
            },
        })
        if encrypted.returncode == 3:
            if not only_zero_offset_warnings(encrypted.stderr, source):
                raise FileSkipped("PDF 有未能自动处理的结构异常")
        elif encrypted.returncode != 0:
            raise FileSkipped(encryption_error(encrypted.stderr))
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise FileSkipped("加密工具未生成可保存的副本")
        publish_copy(temporary, source)


BINARY_OFFICE = {
    ".doc", ".dot", ".xls", ".xlt", ".xlsb", ".wps", ".et", ".ppt", ".pps", ".pot", ".dps",
}


def lock_file(source, password):
    extension = source.suffix.lower()
    if extension == ".pdf":
        lock_pdf(source, password)
        return
    if extension in BINARY_OFFICE:
        raise FileSkipped("此操作暂不支持这种二进制 Office 格式")

    from filelock_office import OFFICE_FORMATS, OfficeLockError, lock_office

    if extension not in OFFICE_FORMATS or not source.is_file():
        raise FileSkipped("请选择 PDF 或现代 Word、Excel、PowerPoint 文件")

    with TemporaryDirectory(prefix=".file-lock-", dir=source.parent) as directory:
        temporary = Path(directory) / ("copy" + extension)
        try:
            lock_office(source, temporary, password)
        except OfficeLockError as error:
            raise FileSkipped(str(error)) from error
        publish_copy(temporary, source, source.suffix)


def result_message(success, failures):
    if not failures:
        return "加锁完成", f"已生成 {success} 份加锁副本。"

    title = "部分文件未加锁" if success else "未能生成加锁副本"
    paragraphs = []
    if success:
        paragraphs.append(f"已生成 {success} 份副本，{len(failures)} 份未加锁。")
    elif len(failures) > 1:
        paragraphs.append(f"有 {len(failures)} 份文件未加锁。")

    for name, reason in failures[:5]:
        label = " ".join(name.splitlines())
        paragraphs.append(f"「{label}」\n{reason}" if label else reason)
    if failures[5:]:
        counts = Counter(reason for _, reason in failures[5:])
        details = "\n".join(f"{reason}（{count} 份）" for reason, count in counts.items())
        paragraphs.append(f"其余 {len(failures) - 5} 份\n{details}")
    return title, "\n\n".join(paragraphs)


def show_result(success, failures):
    title, message = result_message(success, failures)
    if failures:
        action = 'display alert (item 1 of argv) message (item 2 of argv) as warning buttons {"好"} default button "好"'
    else:
        action = 'display notification (item 2 of argv) with title (item 1 of argv)'
    subprocess.run(
        ["/usr/bin/osascript", "-e", f"on run argv\n{action}\nend run", title, message],
        capture_output=True,
        check=False,
    )

def main():
    arguments = sys.argv[1:]
    no_ui = arguments[:1] == ["--no-ui"]
    if no_ui:
        arguments = arguments[1:]

    if not arguments:
        return 0

    try:
        password = get_password(APP_ROOT / "private", prompt=not no_ui)
    except PasswordCancelled:
        return 0
    except (OSError, ValueError):
        failures = [("", "无法设置或读取本机的权限密码")]
        success = 0
    else:
        success = 0
        failures = []
        for argument in arguments:
            source = Path(argument).absolute()
            try:
                lock_file(source, password)
            except FileSkipped as error:
                failures.append((source.name, str(error)))
            except PermissionError:
                failures.append((source.name, "无法读取文件或写入所在文件夹"))
            except OSError as error:
                if error.errno == errno.ENOSPC:
                    reason = "存储空间不足，无法保存副本"
                elif error.filename == QPDF:
                    reason = "本机的 PDF 加密工具无法运行"
                else:
                    reason = "无法在原文件夹保存副本"
                failures.append((source.name, reason))
            else:
                success += 1

    if no_ui:
        print(json.dumps({"created": success, "skipped": failures}, ensure_ascii=False))
    elif success or failures:
        show_result(success, failures)
    return bool(failures) if no_ui else 0


if __name__ == "__main__":
    sys.exit(main())
