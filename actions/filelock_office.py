import base64
from copy import copy
import hashlib
from pathlib import PurePosixPath
import posixpath
import secrets
from shutil import copyfileobj
from urllib.parse import quote, unquote
from zipfile import BadZipFile, ZipFile

from lxml import etree

OFFICE_FORMATS = {
    **dict.fromkeys((".docx", ".docm", ".dotx", ".dotm"), ("wordprocessing", "document")),
    **dict.fromkeys((".xlsx", ".xlsm", ".xltx", ".xltm", ".xlam"), ("spreadsheet", "workbook")),
    **dict.fromkeys((".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm", ".ppam"), ("presentation", "presentation")),
}
PASSWORD_ITERATIONS = 100000
PASSWORD_ALGORITHM = {
    "cryptProviderType": "rsaAES",
    "cryptAlgorithmClass": "hash",
    "cryptAlgorithmType": "typeAny",
    "cryptAlgorithmSid": "14",
}
CONTENT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "{" + REL_NS + "}"
WORD_SETTINGS = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
SHEET_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.chartsheet+xml",
}
STYLES_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
CFB_HEADER = bytes.fromhex("d0cf11e0a1b11ae1")
FALSE = {"0", "false", "off"}


class OfficeLockError(Exception):
    pass


def parse_xml(data):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser=parser)
    if root.getroottree().docinfo.doctype:
        raise OfficeLockError("Office 文件包含不受支持的 XML 定义")
    return root


def xml_bytes(root):
    return etree.tostring(root.getroottree(), encoding="UTF-8", xml_declaration=True, standalone=True)


def resolve_part(owner, target, names):
    if target.startswith("/"):
        candidate = target.lstrip("/")
    else:
        candidate = posixpath.normpath(posixpath.join(posixpath.dirname(owner), target))
    return candidate if candidate in names else unquote(candidate)


def structure(archive):
    entries = archive.infolist()
    names = set(archive.namelist())
    if len(names) != len(entries):
        raise OfficeLockError("Office 文件包含重复的内部文件")
    if any(entry.flag_bits & 1 for entry in entries):
        raise OfficeLockError("文件需要打开密码")
    manifest = parse_xml(archive.read("[Content_Types].xml"))
    if manifest.tag != CONTENT + "Types":
        raise OfficeLockError("无法识别 Office 文件结构")
    types = {
        resolve_part("", item.attrib["PartName"], names): item.attrib["ContentType"]
        for item in manifest.findall(CONTENT + "Override")
    }
    rels = parse_xml(archive.read("_rels/.rels")).findall(REL + "Relationship")
    if (
        any(name.casefold().startswith("_xmlsignatures/") for name in names)
        or any("digital-signature" in kind for kind in types.values())
        or any("/digital-signature/" in link.get("Type", "") for link in rels)
    ):
        raise OfficeLockError("文件带有数字签名，改写会影响签名")
    primary = [
        link for link in rels
        if link.get("Type", "").endswith("/officeDocument") and link.get("TargetMode") != "External"
    ]
    if len(primary) != 1:
        raise OfficeLockError("无法识别 Office 主文档")
    link = primary[0]
    main = resolve_part("", link.attrib["Target"], names)
    relation_prefix = link.attrib["Type"].rsplit("/", 1)[0]
    return names, types, manifest, main, relation_prefix


def insert_after(root, element, predecessors):
    index = 0
    for sibling in root:
        if isinstance(sibling.tag, str) and etree.QName(sibling).localname in predecessors:
            index = root.index(sibling) + 1
    root.insert(index, element)


def office_password_bytes(password):
    if not password.isascii() or not 1 <= len(password) <= 15:
        raise OfficeLockError("Word 和 Excel 的编辑密码需要 1–15 个 ASCII 字符")
    return password.encode("ascii")


def excel_verifier(letters):
    value = 0
    for byte in reversed(letters):
        value = (((value << 1) & 0x7FFF) | (value >> 14)) ^ byte
    value = ((value << 1) & 0x7FFF) | (value >> 14)
    return value ^ len(letters) ^ 0xCE4B


def word_verifier_input(letters):
    # ECMA-376 Word first derives the legacy verifier, then hashes its hex form.
    def advance(value):
        return ((value << 1) & 0xFFFF) ^ (0x1021 if value & 0x8000 else 0)

    high = 0xFFFF
    for _ in range(8 * len(letters)):
        high = advance(high)
    polynomial = 0x1021
    for byte in reversed(letters):
        for bit in range(8):
            if bit < 7 and byte & (1 << bit):
                high ^= polynomial
            polynomial = advance(polynomial)
    low = excel_verifier(letters)
    return (low.to_bytes(2, "little") + high.to_bytes(2, "little")).hex().upper()


def password_verifier(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.sha512(salt + password.encode("utf-16le")).digest()
    for index in range(PASSWORD_ITERATIONS):
        digest = hashlib.sha512(digest + index.to_bytes(4, "little")).digest()
    return base64.b64encode(digest).decode("ascii"), base64.b64encode(salt).decode("ascii")


def word_attributes(password):
    digest, salt = password_verifier(word_verifier_input(password))
    return {
        **PASSWORD_ALGORITHM,
        "edit": "readOnly",
        "enforcement": "1",
        "cryptSpinCount": str(PASSWORD_ITERATIONS),
        "hash": digest,
        "salt": salt,
    }


def word_changes(archive, package, namespace, password):
    names, types, manifest, main, relation_prefix = package
    location = PurePosixPath(main)
    rel_part = str(location.parent / "_rels" / (location.name + ".rels"))
    if rel_part in names:
        rel_root = parse_xml(archive.read(rel_part))
    else:
        rel_root = etree.Element(REL + "Relationships", nsmap={None: REL_NS})
    links = rel_root.findall(REL + "Relationship")
    settings_links = [link for link in links if link.get("Type", "").endswith("/settings")]
    word = "{" + namespace + "}"
    changes = {}
    if settings_links:
        if len(settings_links) != 1 or settings_links[0].get("TargetMode") == "External":
            raise OfficeLockError("无法识别 Word 的文档设置")
        settings_part = resolve_part(main, settings_links[0].attrib["Target"], names)
        if types.get(settings_part) != WORD_SETTINGS:
            raise OfficeLockError("无法识别 Word 的文档设置")
        settings = parse_xml(archive.read(settings_part))
        if settings.tag != word + "settings":
            raise OfficeLockError("无法识别 Word 的文档设置")
    else:
        index = 1
        settings_part = str(location.parent / "settings.xml")
        while settings_part in names:
            index += 1
            settings_part = str(location.parent / f"settings{index}.xml")
        identifiers = {link.get("Id") for link in links}
        index = 1
        while f"rId{index}" in identifiers:
            index += 1
        etree.SubElement(rel_root, REL + "Relationship", Id=f"rId{index}", Type=relation_prefix + "/settings", Target=quote(PurePosixPath(settings_part).name))
        etree.SubElement(manifest, CONTENT + "Override", PartName="/" + quote(settings_part, safe="/"), ContentType=WORD_SETTINGS)
        changes[rel_part] = xml_bytes(rel_root)
        changes["[Content_Types].xml"] = xml_bytes(manifest)
        settings = etree.Element(word + "settings", nsmap={"w": namespace})

    if settings.find(word + "writeProtection") is not None:
        raise OfficeLockError("Word 文件已有打开时的编辑保护或只读提示")
    old = settings.find(word + "documentProtection")
    if old is not None:
        if old.get(word + "enforcement") not in FALSE:
            raise OfficeLockError("Word 文件已有编辑保护")
        settings.remove(old)

    restriction = etree.Element(word + "documentProtection")
    for name, value in word_attributes(password).items():
        restriction.set(word + name, value)
    insert_after(settings, restriction, {
        "view", "zoom", "removePersonalInformation", "removeDateAndTime",
        "doNotDisplayPageBoundaries", "displayBackgroundShape", "printPostScriptOverText",
        "printFractionalCharacterWidth", "printFormsData", "embedTrueTypeFonts",
        "embedSystemFonts", "saveSubsetFonts", "saveFormsData", "mirrorMargins",
        "alignBordersAndEdges", "bordersDoNotSurroundHeader", "bordersDoNotSurroundFooter",
        "gutterAtTop", "hideSpellingErrors", "hideGrammaticalErrors", "activeWritingStyle",
        "proofState", "formsDesign", "attachedTemplate", "linkStyles", "stylePaneFormatFilter",
        "stylePaneSortMethod", "documentType", "mailMerge", "revisionView", "trackRevisions",
        "doNotTrackMoves", "doNotTrackFormatting",
    })
    changes[settings_part] = xml_bytes(settings)
    return changes


def excel_changes(archive, package, namespace, workbook, password):
    _, types, _, main, _ = package
    excel = "{" + namespace + "}"
    existing = workbook.find(excel + "workbookProtection")
    sharing = workbook.find(excel + "fileSharing")
    if existing is not None:
        if any(value not in FALSE for value in existing.attrib.values()):
            raise OfficeLockError("Excel 文件已有工作簿保护")
        workbook.remove(existing)
    if sharing is not None and (
        sharing.get("reservationPassword") or sharing.get("hashValue")
        or sharing.get("readOnlyRecommended") in {"1", "true", "on"}
    ):
        raise OfficeLockError("Excel 文件已有打开时的编辑保护或只读提示")

    verifier = f"{excel_verifier(password):04X}"
    workbook_lock = etree.Element(excel + "workbookProtection", workbookPassword=verifier, lockStructure="1")
    insert_after(workbook, workbook_lock, {"fileVersion", "fileSharing", "workbookPr"})
    changes = {main: xml_bytes(workbook)}
    sheet_count = 0
    for part, kind in types.items():
        if kind == STYLES_TYPE:
            styles = parse_xml(archive.read(part))
            unlocked = [item for item in styles.iter(excel + "protection") if item.get("locked") in FALSE]
            for item in unlocked:
                item.set("locked", "1")
            if unlocked:
                changes[part] = xml_bytes(styles)
        elif kind in SHEET_TYPES:
            sheet = parse_xml(archive.read(part))
            if sheet.tag not in {excel + "worksheet", excel + "chartsheet"}:
                raise OfficeLockError("无法识别 Excel 工作表")
            if sheet.find(excel + "sheetProtection") is not None or sheet.find(excel + "protectedRanges") is not None:
                raise OfficeLockError("Excel 文件已有工作表保护或可编辑区域")
            if sheet.tag == excel + "worksheet":
                restriction = etree.Element(excel + "sheetProtection", password=verifier, sheet="1", objects="1", scenarios="1")
                insert_after(sheet, restriction, {"sheetPr", "dimension", "sheetViews", "sheetFormatPr", "cols", "sheetData", "sheetCalcPr"})
            else:
                restriction = etree.Element(excel + "sheetProtection", password=verifier, content="1", objects="1")
                insert_after(sheet, restriction, {"sheetPr", "sheetViews"})
            changes[part] = xml_bytes(sheet)
            sheet_count += 1
        elif kind in {
            "application/vnd.ms-excel.macrosheet+xml",
            "application/vnd.ms-excel.intlmacrosheet+xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.dialogsheet+xml",
        }:
            raise OfficeLockError("该 Excel 文件包含暂不支持加锁的特殊工作表")
    sheets = workbook.find(excel + "sheets")
    if sheets is None or sheet_count != len(sheets.findall(excel + "sheet")):
        raise OfficeLockError("无法识别 Excel 工作表")
    return changes


def powerpoint_changes(main, presentation, namespace, password):
    tag = "{" + namespace + "}"
    if presentation.find(tag + "modifyVerifier") is not None:
        raise OfficeLockError("PowerPoint 文件已有编辑密码")
    password = password.removeprefix("\ufeff")
    if not 1 <= len(password.encode("utf-16le")) // 2 <= 255:
        raise OfficeLockError("PowerPoint 的编辑密码长度无效")
    digest, salt = password_verifier(password)
    verifier = etree.Element(
        tag + "modifyVerifier",
        **PASSWORD_ALGORITHM,
        spinCount=str(PASSWORD_ITERATIONS),
        hashData=digest,
        saltData=salt,
    )
    extensions = presentation.find(tag + "extLst")
    index = presentation.index(extensions) if extensions is not None else len(presentation)
    presentation.insert(index, verifier)
    return {main: xml_bytes(presentation)}


def lock_office(source, destination, password):
    family, expected = OFFICE_FORMATS[source.suffix.lower()]
    with source.open("rb") as stream:
        if stream.read(len(CFB_HEADER)) == CFB_HEADER:
            raise OfficeLockError("文件需要打开密码、组织授权，或使用了旧版 Office 格式")
    try:
        with ZipFile(source) as archive:
            package = structure(archive)
            names, _, _, main, _ = package
            with archive.open(main) as stream:
                first = next(etree.iterparse(stream, events=("start",), resolve_entities=False, no_network=True))[1]
                name = etree.QName(first)
            if name.localname != expected or name.namespace not in {
                f"http://schemas.openxmlformats.org/{family}ml/2006/main",
                f"http://purl.oclc.org/ooxml/{family}ml/main",
            }:
                raise OfficeLockError("实际文档类型与文件扩展名不一致")
            if family == "wordprocessing":
                changes = word_changes(archive, package, name.namespace, office_password_bytes(password))
            else:
                document = parse_xml(archive.read(main))
                if family == "spreadsheet":
                    changes = excel_changes(archive, package, name.namespace, document, office_password_bytes(password))
                else:
                    changes = powerpoint_changes(main, document, name.namespace, password)

            with ZipFile(destination, "w") as output:
                output.comment = archive.comment
                for entry in archive.infolist():
                    if entry.filename in changes:
                        output.writestr(copy(entry), changes[entry.filename])
                    else:
                        with archive.open(entry) as reader, output.open(copy(entry), "w") as writer:
                            copyfileobj(reader, writer, length=1024 * 1024)
                for part in changes.keys() - names:
                    output.writestr(part, changes[part])
    except NotImplementedError:
        raise OfficeLockError("Office 文件使用了本机不支持的压缩方式") from None
    except (BadZipFile, KeyError, RuntimeError, StopIteration, etree.XMLSyntaxError):
        raise OfficeLockError("Office 文件结构异常，无法读取") from None
