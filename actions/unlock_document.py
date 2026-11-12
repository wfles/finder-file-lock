import ctypes
import errno
import json
import os
from collections import Counter
from copy import copy
from pathlib import Path
import re
from shutil import copyfileobj
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import unquote
from zipfile import BadZipFile, ZipFile

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "runtime" / "python-packages"))
QPDF = str(Path(sys.executable).resolve().parents[2] / "qpdf" / "bin" / "qpdf")

from lxml import etree
SUFFIX = "_已解锁"
UNRESTRICTED = "没有发现可解除的编辑限制"
WORD = {".docx", ".docm", ".dotx", ".dotm"}
EXCEL = {".xlsx", ".xlsm", ".xltx", ".xltm", ".xlam"}
POWERPOINT = {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm", ".ppam"}
BINARY_OFFICE = {".doc", ".dot", ".xls", ".xlt", ".xlsb", ".ppt", ".pps", ".pot", ".wps", ".et", ".dps"}

OFFICE_NAMESPACES = {
    family: {
        f"http://schemas.openxmlformats.org/{family}ml/2006/main",
        f"http://purl.oclc.org/ooxml/{family}ml/main",
    }
    for family in ("wordprocessing", "spreadsheet", "presentation")
}
CONTENT_TYPES = "{http://schemas.openxmlformats.org/package/2006/content-types}"
RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
WORD_SETTINGS = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
EXCEL_SHEETS = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.chartsheet+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.dialogsheet+xml",
    "application/vnd.ms-excel.macrosheet+xml",
    "application/vnd.ms-excel.intlmacrosheet+xml",
}
CUSTOM_PROPERTIES = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
CUSTOM_NAMESPACES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
    "http://purl.oclc.org/ooxml/officeDocument/custom-properties",
}
CFB_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
OFFSET_WARNING = re.compile(
    r"WARNING: input PDF \(object [0-9]+ [0-9]+\): object has offset 0 - "
    r"a common error handled correctly by qpdf and most other applications"
)
WARNING_SUMMARY = "qpdf: operation succeeded with warnings; resulting file may have some problems"

rename_exclusive = ctypes.CDLL(None, use_errno=True).renamex_np
rename_exclusive.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
rename_exclusive.restype = ctypes.c_int


class Skipped(Exception):
    pass


def qpdf(*args):
    return subprocess.run(
        [QPDF, *map(str, args)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def pdf_succeeded(result, source):
    if result.returncode == 0:
        return True
    if result.returncode != 3:
        return False
    diagnostics = result.stderr.replace(str(source), "input PDF")
    warnings = [
        line for line in diagnostics.splitlines()
        if line.strip() and line != WARNING_SUMMARY
    ]
    return bool(warnings) and all(OFFSET_WARNING.fullmatch(line) for line in warnings)


def unlock_pdf(source, destination):
    info = qpdf("--json", "--json-key=encrypt", "--json-key=acroform", source)
    if not pdf_succeeded(info, source):
        if "invalid password" in info.stderr.lower():
            raise Skipped("需要输入 PDF 的打开密码")
        raise Skipped("PDF 无法读取或有未能自动处理的结构异常")

    metadata = json.loads(info.stdout)
    fields = metadata["acroform"]["fields"]
    if any(field["fieldtype"] == "/Sig" and field.get("value") not in (None, "") for field in fields):
        raise Skipped("PDF 带有数字签名，改写会影响签名")
    if not metadata["encrypt"]["encrypted"]:
        raise Skipped(UNRESTRICTED)

    result = qpdf("--decrypt", "--stream-data=preserve", source, destination)
    if not pdf_succeeded(result, source):
        raise Skipped("无法生成解锁副本")
    unlocked = qpdf("--is-encrypted", destination)
    if unlocked.returncode != 2 or not destination.is_file() or destination.stat().st_size == 0:
        raise Skipped("未能确认 PDF 的编辑限制已解除")


def read_xml(data):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser=parser)
    if root.getroottree().docinfo.doctype:
        raise Skipped("Office 文件包含无法安全处理的 XML 定义")
    return root


def part_name(uri, names):
    name = uri.lstrip("/")
    return name if name in names else unquote(name)


def document_parts(archive):
    names = set(archive.namelist())
    if len(names) != len(archive.infolist()):
        raise Skipped("Office 文件包含重复的内部文件")
    if any(entry.flag_bits & 1 for entry in archive.infolist()):
        raise Skipped("Office 文件需要打开密码")
    manifest = read_xml(archive.read("[Content_Types].xml"))
    if manifest.tag != CONTENT_TYPES + "Types":
        raise Skipped("无法识别 Office 文件结构")
    types = {
        part_name(entry.attrib["PartName"], names): entry.attrib["ContentType"]
        for entry in manifest.findall(CONTENT_TYPES + "Override")
    }
    if any(name.casefold().startswith("_xmlsignatures/") for name in names) or any(
        "digital-signature" in content_type for content_type in types.values()
    ):
        raise Skipped("Office 文件带有数字签名，改写会影响签名")

    relationships = read_xml(archive.read("_rels/.rels"))
    links = relationships.findall(RELATIONSHIPS + "Relationship")
    if any("/digital-signature/" in item.attrib.get("Type", "") for item in links):
        raise Skipped("Office 文件带有数字签名，改写会影响签名")
    main_parts = [
        part_name(item.attrib["Target"], names)
        for item in links
        if item.attrib.get("Type", "").endswith("/officeDocument")
        and item.attrib.get("TargetMode") != "External"
    ]
    if len(main_parts) != 1 or main_parts[0] not in names:
        raise Skipped("无法识别 Office 主文档")
    return types, main_parts[0]


def without_protection(data, namespaces, tags):
    if b"\x00" not in data and not any(tag.encode() in data for tag in tags):
        return None
    root = read_xml(data)
    removed = False
    for namespace in namespaces:
        for tag in tags:
            for element in list(root.iter(f"{{{namespace}}}{tag}")):
                parent = element.getparent()
                if parent is None:
                    continue
                if tag == "property" and element.get("name", "").casefold() != "_markasfinal":
                    continue
                parent.remove(element)
                removed = True
    if removed:
        return etree.tostring(root.getroottree(), encoding="UTF-8", xml_declaration=True)
    return None


def xml_rules(types, main, extension):
    rules = {
        name: (CUSTOM_NAMESPACES, ("property",))
        for name, content_type in types.items()
        if content_type == CUSTOM_PROPERTIES
    }
    if extension in WORD:
        for name, content_type in types.items():
            if content_type == WORD_SETTINGS:
                rules[name] = OFFICE_NAMESPACES["wordprocessing"], ("documentProtection", "writeProtection")
    elif extension in EXCEL:
        excel = OFFICE_NAMESPACES["spreadsheet"]
        rules[main] = excel, ("workbookProtection", "fileSharing")
        for name, content_type in types.items():
            if content_type in EXCEL_SHEETS:
                rules[name] = excel, ("sheetProtection",)
    else:
        rules[main] = OFFICE_NAMESPACES["presentation"], ("modifyVerifier",)
    return rules


def unlock_office(source, destination):
    with source.open("rb") as stream:
        if stream.read(len(CFB_SIGNATURE)) == CFB_SIGNATURE:
            raise Skipped("文件需要打开密码、组织授权，或使用了旧版 Office 格式")

    try:
        with ZipFile(source) as archive:
            types, main = document_parts(archive)
            extension = source.suffix.lower()
            family = "wordprocessing" if extension in WORD else "spreadsheet" if extension in EXCEL else "presentation"
            expected = {"wordprocessing": "document", "spreadsheet": "workbook", "presentation": "presentation"}
            with archive.open(main) as stream:
                first = next(etree.iterparse(stream, events=("start",), resolve_entities=False, no_network=True))[1]
                name = etree.QName(first)
            if name.namespace not in OFFICE_NAMESPACES[family] or name.localname != expected[family]:
                raise Skipped("实际文档类型与文件扩展名不一致")
            rules = xml_rules(types, main, extension)
            changes = {}
            for name, (namespaces, tags) in rules.items():
                edited = without_protection(archive.read(name), namespaces, tags)
                if edited is not None:
                    changes[name] = edited
            if not changes:
                raise Skipped(UNRESTRICTED)

            with ZipFile(destination, "w") as output:
                output.comment = archive.comment
                for entry in archive.infolist():
                    if entry.filename in changes:
                        output.writestr(copy(entry), changes[entry.filename])
                    else:
                        with archive.open(entry) as reader, output.open(copy(entry), "w") as writer:
                            copyfileobj(reader, writer, length=1024 * 1024)
    except NotImplementedError:
        raise Skipped("Office 文件使用了本机不支持的压缩方式") from None
    except (BadZipFile, KeyError, RuntimeError, StopIteration, etree.XMLSyntaxError):
        raise Skipped("Office 文件结构异常，无法读取") from None


def publish(temporary, source):
    number = 1
    while True:
        version = "" if number == 1 else f"_{number}"
        destination = source.with_name(f"{source.stem}{SUFFIX}{version}{source.suffix}")
        if rename_exclusive(os.fsencode(temporary), os.fsencode(destination), 0x00000004) == 0:
            return
        error = ctypes.get_errno()
        if error != errno.EEXIST:
            raise OSError(error, os.strerror(error), str(destination))
        number += 1


def unlock(source):
    extension = source.suffix.lower()
    if extension in BINARY_OFFICE:
        raise Skipped("此操作暂不支持这种二进制 Office 格式")
    if not source.is_file() or extension not in WORD | EXCEL | POWERPOINT | {".pdf"}:
        raise Skipped("请选择 PDF 或现代 Word、Excel、PowerPoint 文件")
    with TemporaryDirectory(prefix=".document-unlock-", dir=source.parent) as directory:
        temporary = Path(directory) / ("copy" + extension)
        process = unlock_pdf if extension == ".pdf" else unlock_office
        process(source, temporary)
        publish(temporary, source)


def show_result(success, failures):
    if not failures:
        title = "解锁完成"
        body = f"已生成 {success} 份解锁副本。"
        action = 'display notification (item 2 of argv) with title (item 1 of argv)'
    else:
        title = "部分文件未解锁" if success else "未生成解锁副本"
        paragraphs = [f"已生成 {success} 份副本，{len(failures)} 份未解锁。"] if success else []
        for name, reason in failures[:5]:
            label = " ".join(name.splitlines())
            paragraphs.append(f"「{label}」\n{reason}")
        if len(failures) > 5:
            counts = Counter(reason for _, reason in failures[5:])
            details = "\n".join(f"{reason}（{count} 份）" for reason, count in counts.items())
            paragraphs.append(f"其余 {len(failures) - 5} 份\n{details}")
        body = "\n\n".join(paragraphs)
        action = 'display alert (item 1 of argv) message (item 2 of argv) as warning buttons {"好"} default button "好"'
    subprocess.run(
        ["/usr/bin/osascript", "-e", f"on run argv\n{action}\nend run", title, body],
        capture_output=True, check=False,
    )


def main():
    paths = sys.argv[1:]
    no_ui = paths[:1] == ["--no-ui"]
    if no_ui:
        paths = paths[1:]
    success = 0
    failures = []
    for path in paths:
        source = Path(path).absolute()
        try:
            unlock(source)
        except Skipped as error:
            failures.append((source.name, str(error)))
        except OSError as error:
            if error.errno == errno.ENOSPC:
                reason = "存储空间不足，无法保存副本"
            elif error.filename == QPDF:
                reason = "本机的 PDF 工具无法运行"
            else:
                reason = "无法读取文件或在原文件夹保存副本"
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
