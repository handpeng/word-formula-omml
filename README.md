# Word Formula OMML

A Codex skill for converting LaTeX-like or plain-text mathematical notation in existing Microsoft Word documents into native, editable Office Math (OMML) equations.

The workflow is designed for academic, legal, business, and submission documents where formatting, tracked changes, images, relationships, and package integrity must be preserved.

## Features

- Converts raw notation such as `x_i`, `\gamma`, `+/-`, inequalities, intervals, and scientific notation to editable Word equations.
- Uses Pandoc to generate trusted OMML equation templates instead of hand-building complex math XML.
- Supports occurrence-level manifests so repeated expressions can retain different styles and colors.
- Produces redlined and clean DOCX outputs for reviewed documents.
- Preserves revisions from other authors.
- Audits formula counts, residual raw syntax, Cambria Math usage, media hashes, relationships, and protected OOXML parts.
- Requires native Microsoft Word inspection before high-confidence delivery.

## Installation

Clone the repository into the Codex skills directory:

```bash
git clone https://github.com/handpeng/word-formula-omml.git \
  ~/.codex/skills/word-formula-omml
```

Start a new Codex session if the skill list does not refresh automatically.

This skill is designed to work alongside the Codex `docx` skill. Install or enable that skill before editing existing Word documents.

## Requirements

- Python 3.10 or later
- Pandoc 3.x
- The Codex `docx` skill and its OOXML validation tools
- Microsoft Word for final native-open and visual validation

## Usage

Invoke the skill explicitly:

```text
Use $word-formula-omml to convert the formula-like text in this DOCX into
native editable Word equations and produce redlined and clean outputs.
```

The skill first inventories candidate formulas and creates an occurrence-level manifest. Ambiguous notation must be resolved before any OOXML is changed.

## Included Tools

Generate a labeled OMML template library from a reviewed manifest:

```bash
python3 scripts/generate_omml_library.py formula-manifest.json omml-library.docx
```

Audit a converted document against its source:

```bash
python3 scripts/audit_docx_formulas.py corrected.docx \
  --baseline source.docx \
  --expected-formulas 95 \
  --require-cambria-math \
  --residual 'L_Total|gamma_|\+/-|>=|10\^-'
```

See [SKILL.md](SKILL.md) for the operating rules, [references/workflow.md](references/workflow.md) for the complete conversion process, and [references/manifest.md](references/manifest.md) for the manifest format.

## Safety Boundary

The repository intentionally does not provide a blind global-replacement command. Word formula text may cross runs or intersect revisions, hyperlinks, bookmarks, fields, drawings, or content controls. The skill generates a task-specific application step only after those structures have been inventoried and reviewed.

The source document remains immutable. Validated results are written to new files.

## License

MIT

---

## 中文说明

这是一个用于 Codex 的 Word 公式转换 skill，可将 DOCX 中以 LaTeX 或普通文本形式存在的公式转换为 Word 原生、可编辑的 OMML 公式。

它适用于论文、审稿回复信和其他需要保留修订记录、颜色、样式、图片及文档结构的专业文件。默认工作流会先建立逐公式清单，再生成修订痕迹版和清洁版，并进行 OOXML 结构、公式数量、字体、媒体及残留语法检查。

调用示例：

```text
使用 $word-formula-omml 将这个 Word 文件中的公式文本转换为原生 Word
公式，同时保留既有格式和修订记录。
```
