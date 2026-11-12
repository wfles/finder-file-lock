#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DOCUMENTS = {
    ".docx": ("word/settings.xml", b"documentProtection"),
    ".xlsx": ("xl/worksheets/sheet1.xml", b"sheetProtection"),
    ".pptx": ("ppt/presentation.xml", b"modifyVerifier"),
}
TEST_PASSWORD = "FixtureTest-783"


def run(launcher, arch, action, *inputs, expected=0):
    result = subprocess.run(
        ["/usr/bin/arch", f"-{arch}", launcher, action, "--no-ui", *inputs],
        text=True, capture_output=True,
    )
    assert result.returncode == expected, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def pdf_metadata(qpdf, document, password=None):
    args = [f"--password={password}"] if password is not None else []
    return json.loads(subprocess.check_output(
        [qpdf, *args, "--json", "--json-key=encrypt", "--json-key=pages", "--json-key=outlines", document]
    ))


def verify_pdf(qpdf, locked, unlocked):
    restricted = pdf_metadata(qpdf, locked)
    removed = pdf_metadata(qpdf, unlocked)
    for metadata in (restricted, removed):
        assert len(metadata["pages"]) == 2
        assert metadata["outlines"][0]["title"] == "测试书签"
    permissions = restricted["encrypt"]
    assert permissions["encrypted"] and permissions["userpasswordmatched"]
    assert permissions["capabilities"]["printhigh"] and permissions["capabilities"]["extract"]
    for permission in ("modify", "modifyannotations", "modifyassembly", "modifyforms", "modifyother"):
        assert not permissions["capabilities"][permission]
    assert pdf_metadata(qpdf, locked, TEST_PASSWORD)["encrypt"]["ownerpasswordmatched"]
    wrong = subprocess.run(
        [qpdf, "--password=fixture-wrong", "--show-encryption", locked], capture_output=True, text=True
    )
    assert "Incorrect password supplied" in wrong.stdout
    assert not removed["encrypt"]["encrypted"]


def test_files(payload, arch, export):
    with TemporaryDirectory(prefix=f"finder-file-lock-{arch}-") as tmp:
        target = Path(tmp)
        (target / "runtime").symlink_to(payload / "runtime")
        for name in ("bin", "actions"):
            shutil.copytree(payload / name, target / name)
        selected = target / "files"
        shutil.copytree(FIXTURES, selected)
        documents = sorted(p for p in selected.iterdir() if p.suffix in {".pdf", *DOCUMENTS})
        original = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in documents}
        launcher = target / "bin/run-action"
        assert run(launcher, arch, "lock", documents[0], expected=1)["created"] == 0
        password = target / "private/permissions-password"
        password.write_text(TEST_PASSWORD, encoding="utf-8")
        password.chmod(0o600)
        assert run(launcher, arch, "lock", documents[0])["created"] == 1
        mixed = run(launcher, arch, "lock", *documents, selected / "略过.txt", expected=1)
        assert mixed["created"] == 4 and len(mixed["skipped"]) == 1, mixed
        assert documents[0].with_stem(documents[0].stem + "_已加锁_2").is_file()
        locked = [p.with_stem(p.stem + "_已加锁") for p in documents]
        assert run(launcher, arch, "unlock", *locked)["created"] == 4
        assert run(launcher, arch, "unlock", *locked)["created"] == 4
        assert run(launcher, arch, "lock", locked[0], expected=1)["created"] == 0
        for item in locked:
            unlocked = item.with_stem(item.stem + "_已解锁")
            assert item.with_stem(item.stem + "_已解锁_2").is_file()
            if item.suffix == ".pdf":
                verify_pdf(payload / "runtime" / arch / "qpdf/bin/qpdf", item, unlocked)
            else:
                entry, tag = DOCUMENTS[item.suffix]
                with ZipFile(item) as archive:
                    assert tag in archive.read(entry)
                with ZipFile(unlocked) as archive:
                    assert tag not in archive.read(entry)
        assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in original.items())
        if export:
            shutil.copytree(selected, export)
    print(f"{arch}: PDF, DOCX, XLSX, PPTX, mixed selection, file names, originals and permissions passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    parser.add_argument("--arch", required=True, choices=("arm64", "x86_64"))
    parser.add_argument("--export-fixtures", type=Path)
    args = parser.parse_args()
    test_files(args.payload.resolve(), args.arch, args.export_fixtures)
