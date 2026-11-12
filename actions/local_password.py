import fcntl
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile


class PasswordCancelled(Exception):
    pass


def ask(message):
    dialog = '''
on run argv
    set answerRecord to display dialog (item 1 of argv) with title "设置文件加锁密码" default answer "" with hidden answer buttons {"取消", "继续"} default button "继续" cancel button "取消"
    return text returned of answerRecord
end run
'''
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", dialog, message],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode:
        if "(-128)" in result.stderr:
            raise PasswordCancelled()
        raise OSError("Unable to show password dialog")
    return result.stdout.removesuffix("\n")


def valid_password(value):
    return 1 <= len(value) <= 15 and all("!" <= char <= "~" for char in value)


def choose_password():
    message = "第一次使用，请设置你自己的编辑密码。请输入 1–15 位英文字母、数字或英文符号。"
    while True:
        password = ask(message)
        if not valid_password(password):
            message = "密码须为 1–15 位英文字母、数字或英文符号。请重新输入。"
            continue
        if password == ask("请再输入一次，确认你的编辑密码。"):
            return password
        message = "两次输入不同，请重新设置密码。"


def get_password(directory: Path, *, prompt: bool):
    directory.mkdir(mode=0o700, exist_ok=True)
    password_file = directory / "permissions-password"
    with open(directory / "setup.lock", "ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            password = password_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            if not prompt:
                raise
            password = choose_password()
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as temp:
                temporary = Path(temp.name)
                try:
                    temp.write(password)
                    temp.flush()
                    os.replace(temporary, password_file)
                finally:
                    temporary.unlink(missing_ok=True)
        if not valid_password(password):
            raise ValueError("Invalid local editing password")
        return password
