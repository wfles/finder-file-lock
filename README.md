# Finder File Lock

在 macOS 访达里批量给 PDF、Word、Excel 和 PowerPoint 添加或解除编辑限制。文件在本机处理，每份结果另存为副本。

## 安装

1. 前往 [GitHub Releases](https://github.com/wfles/finder-file-lock/releases)，下载 `Finder-File-Lock-版本-macOS.pkg`。同一份安装包适用于 macOS 15 及以上、Apple 芯片和 Intel Mac。
2. 双击安装包，按提示装到当前账户。安装会重新载入访达，请先等访达里的文件复制完成。安装包内置所有运行依赖。
3. 在访达选中文件，从右键「快速操作」运行。第一次加锁时，设置并确认自己的编辑密码。

**首发安装包尚未签名和公证。** macOS 可能显示安全提示。确认下载来源后，按 [Apple 的安装说明](https://support.apple.com/zh-cn/102445) 处理。可下载同一版本的 `SHA256SUMS.txt`，在两个文件所在目录运行以下命令，确认安装包与发布页上的校验值一致。

```sh
shasum -a 256 -c SHA256SUMS.txt
```

如果之前通过其他方式装过同名快速操作，安装会中止。可以继续用旧版，或先把 `~/Library/Services/` 中对应的 `.workflow` 移到备份文件夹，再运行安装器。

## 使用

| 快速操作 | 访达快捷键 | 副本名称 |
| --- | --- | --- |
| 文件加锁 | `⌘⇧L` | `原文件名_已加锁.扩展名` |
| 解除编辑限制 | `⌘⇧U` | `原文件名_已解锁.扩展名` |

可选一份或多份文件。副本生成在各文件的原文件夹，同名时从 `_2` 开始编号，原件保留。若某份文件无法处理，动作会列出原因并继续处理同一批的其余文件。

快捷键只在访达中生效。安装时会为原有「资源库」「实用工具」默认快捷键各加上 `⌥`。遇到自定义快捷键冲突时保留已有设置，两个动作仍可从右键运行。

| 格式 | 加锁后的表现 |
| --- | --- |
| PDF `.pdf` | 可直接打开、高清打印和复制。编辑、批注、填表和页面调整受权限限制。具体提示由 PDF 软件决定。 |
| Word `.docx` | 可直接打开，编辑受文档保护限制。 |
| Excel `.xlsx` | 可直接打开，工作表编辑和工作簿结构受保护。 |
| PowerPoint `.pptx` | 打开时可选「只读打开」，也可输入编辑密码。 |

- 首次设置的编辑密码为 1–15 位英文字母、数字或英文符号。密码以文件形式存放在当前账户的应用支持目录，其他普通账户没有读取权限。文件与密码处理均在本机完成。
- 「解除编辑限制」适用于有权处理且已能打开的文件。需要打开密码或组织授权的文件、旧版 `.doc`、`.xls`、`.ppt` 等格式会跳过。对数字签名文件，请使用未签名原稿。
- 编辑限制依赖阅读和编辑软件的支持。私密资料请使用打开密码或有访问控制的分享方式。参见 [qpdf 权限说明](https://qpdf.readthedocs.io/en/stable/encryption.html)和 [Microsoft PowerPoint 密码说明](https://support.microsoft.com/en-us/powerpoint/password-protection-for-presentations-in-powerpoint)。

## 卸载

关闭访达中正在进行的文件复制，然后在终端运行：

```sh
"$HOME/Library/Application Support/FinderFileLock/bin/uninstall-actions"
```

卸载程序会移除本项目安装的工作流，恢复仍保留安装时设置的访达快捷键，并重新载入访达。随后可将 `~/Library/Application Support/FinderFileLock` 移到废纸篓，这也会删除本机保存的编辑密码。请先记下仍要用来编辑旧副本的密码。

## 从源码构建

在 macOS 15 及以上安装 Xcode 命令行工具。从仓库根目录运行：

```sh
/usr/bin/python3 scripts/build_pkg.py
```

构建脚本下载并校验 `installer/dependencies.json` 中列出的资源，产物位于 `dist/`。内置 [qpdf 12.4.2](https://github.com/qpdf/qpdf/releases/tag/v12.4.2) 处理 PDF、[CPython 3.13.18](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110) 运行工作流，以及 [lxml 6.1.1](https://pypi.org/project/lxml/6.1.1/) 处理 Office 文件。第三方许可随安装包提供，项目源码采用 [MIT 许可](LICENSE)。

### 本地验证

```sh
pkgutil --expand-full dist/Finder-File-Lock-1.0.0-macOS.pkg /tmp/finder-file-lock-test
payload=/tmp/finder-file-lock-test/file-actions-component.pkg/Payload
/usr/bin/python3 -B tests/setup.py "$payload"
/usr/bin/python3 -B tests/smoke.py "$payload" --arch "$(uname -m)"
```

首发验证涵盖 Apple 芯片原生运行和 Rosetta 下的 Intel 版运行，测试了四类文件的批量处理。在 WPS PDF 验证了直接打开、编辑密码和打印面板，在 WPS 演示验证了只读打开。原生 Intel Mac、macOS 15 的桌面交互及 Microsoft Office 桌面版仍待实机验证。
