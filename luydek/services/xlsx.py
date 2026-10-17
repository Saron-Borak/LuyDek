"""Write Excel workbooks (.xlsx) with the standard library: no spreadsheet package.

A .xlsx is a zip of XML files. This writes what a shop's stock sheet needs and nothing more: several
sheets, text and numbers and dates, a few styles (bold headings, money, dates, coloured states, yellow
cells to fill in), column widths, a frozen heading row, filters and drop-down lists. Reading is in
shelfsheet.py.

    book = Workbook()
    sheet = book.sheet("Stock", widths=[30, 10], freeze=1, filters=True)
    sheet.row(["Product", "On hand"], style="head")
    sheet.row(["Coca-Cola can 330ml", 37], styles=[None, "int"])
    data = book.save()
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime
from xml.sax.saxutils import escape

EXCEL_EPOCH = date(1899, 12, 30)

# A style is a few properties; the names below are what the pages use.
STYLES = {
    "text": {},
    "wrap": {"wrap": True},
    "muted": {"color": "5F5E5A"},
    "title": {"bold": True, "size": 15, "color": "26215C"},
    "subtitle": {"color": "5F5E5A", "size": 10},
    "label": {"bold": True},
    "head": {"bold": True, "fill": "EEEDFE", "color": "26215C", "border": True, "wrap": True},
    "head_edit": {"bold": True, "fill": "FFE699", "color": "412402", "border": True, "wrap": True},
    "key": {"color": "888780"},
    "int": {"fmt": "0"},
    "num": {"fmt": "0.0"},
    "money": {"fmt": '"$"#,##0.00'},
    "money_big": {"fmt": '"$"#,##0', "bold": True, "size": 13},
    "big": {"fmt": "0", "bold": True, "size": 13},
    "date": {"fmt": "d mmm yyyy"},
    "datetime": {"fmt": "d mmm yyyy, hh:mm"},
    "edit": {"fill": "FFF4CC", "border": True},
    "edit_int": {"fill": "FFF4CC", "border": True, "fmt": "0"},
    "edit_money": {"fill": "FFF4CC", "border": True, "fmt": '"$"#,##0.00'},
    "edit_date": {"fill": "FFF4CC", "border": True, "fmt": "d mmm yyyy"},
    "count": {"fill": "FFE699", "border": True, "fmt": "0", "bold": True},
    "bad": {"fill": "FCEBEB", "color": "A32D2D", "bold": True},
    "warn": {"fill": "FAEEDA", "color": "854F0B", "bold": True},
    "ok": {"fill": "EAF3DE", "color": "3B6D11"},
    "sleep": {"fill": "EEEDFE", "color": "534AB7", "bold": True},
    "new": {"fill": "F4F2EC", "color": "5F5E5A"},
    "bad_date": {"fill": "FCEBEB", "color": "A32D2D", "bold": True, "fmt": "d mmm yyyy"},
    "warn_date": {"fill": "FAEEDA", "color": "854F0B", "bold": True, "fmt": "d mmm yyyy"},
}


def column(n: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def absolute(ref: str) -> str:
    """A1:Q46 -> $A$1:$Q$46"""
    return ":".join(re.sub(r"([A-Z]+)(\d+)", r"$\1$\2", part) for part in ref.split(":"))


class Sheet:
    def __init__(self, name: str, widths: list[float] | None = None, freeze: int = 0, filters: bool = False):
        self.name = name[:31]
        self.widths = widths or []
        self.freeze = freeze
        self.filters = filters
        self.rows: list[list[tuple[object, str | None]]] = []
        self.lists: list[tuple[str, list[str]]] = []   # (range, choices): drop-down lists
        self.merges: list[str] = []

    def row(self, values: list, style: str | None = None, styles: list[str | None] | None = None) -> int:
        """Add a row; `style` for every cell, or `styles` one per cell. Returns its row number (1-based)."""
        cells = [(v, (styles[i] if styles and i < len(styles) and styles[i] else style)) for i, v in enumerate(values)]
        self.rows.append(cells)
        return len(self.rows)

    def blank(self, n: int = 1) -> None:
        for _ in range(n):
            self.rows.append([])

    def choices(self, col: int, first: int, last: int, options: list[str]) -> None:
        """A drop-down list in a column, from row `first` to `last` (1-based)."""
        text = ",".join(options)
        if options and len(text) <= 250 and '"' not in text:
            self.lists.append((f"{column(col)}{first}:{column(col)}{last}", options))

    def merge(self, ref: str) -> None:
        self.merges.append(ref)


class Workbook:
    def __init__(self) -> None:
        self.sheets: list[Sheet] = []
        self._styles: list[dict] = [{}]       # cellXfs; 0 is the default

    def sheet(self, name: str, **kw) -> Sheet:
        s = Sheet(name, **kw)
        self.sheets.append(s)
        return s

    def _xf(self, style: str | None) -> int:
        spec = STYLES.get(style or "text", {})
        if not spec:
            return 0
        if spec not in self._styles:
            self._styles.append(spec)
        return self._styles.index(spec)

    # --- the parts of the file ---------------------------------------------------------------------

    def _cell(self, ref: str, value, style: str | None) -> str:
        if isinstance(value, datetime) and style in (None, "text"):
            style = "datetime"
        elif isinstance(value, date) and not isinstance(value, datetime) and style in (None, "text"):
            style = "date"
        s = self._xf(style)
        attr = f' s="{s}"' if s else ""
        if value is None or value == "":
            return f'<c r="{ref}"{attr}/>' if s else ""
        if isinstance(value, bool):
            return f'<c r="{ref}"{attr} t="b"><v>{int(value)}</v></c>'
        if isinstance(value, datetime):
            serial = (value - datetime(1899, 12, 30)).total_seconds() / 86400
            return f'<c r="{ref}"{attr}><v>{serial:.6f}</v></c>'
        if isinstance(value, date):
            return f'<c r="{ref}"{attr}><v>{(value - EXCEL_EPOCH).days}</v></c>'
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"{attr}><v>{value}</v></c>'
        text = escape(str(value)).replace("\n", "&#10;")
        return f'<c r="{ref}"{attr} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

    def _sheet_xml(self, sh: Sheet, selected: bool) -> str:
        width = max((len(r) for r in sh.rows), default=1)
        parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
                 f'<dimension ref="A1:{column(max(width, 1) - 1)}{max(len(sh.rows), 1)}"/>',
                 '<sheetViews><sheetView workbookViewId="0"' + (' tabSelected="1"' if selected else "") + ">"]
        if sh.freeze:
            top = f"A{sh.freeze + 1}"
            parts.append(f'<pane ySplit="{sh.freeze}" topLeftCell="{top}" activePane="bottomLeft" state="frozen"/>'
                         f'<selection pane="bottomLeft" activeCell="{top}" sqref="{top}"/>')
        parts.append('</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>')
        if sh.widths:
            parts.append("<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                                            for i, w in enumerate(sh.widths)) + "</cols>")
        parts.append("<sheetData>")
        for r, cells in enumerate(sh.rows, start=1):
            xml = "".join(self._cell(f"{column(c)}{r}", v, st) for c, (v, st) in enumerate(cells))
            parts.append(f'<row r="{r}">{xml}</row>' if xml else f'<row r="{r}"/>')
        parts.append("</sheetData>")
        if sh.filters and sh.rows:
            parts.append(f'<autoFilter ref="{self._filter_ref(sh)}"/>')
        if sh.merges:
            parts.append(f'<mergeCells count="{len(sh.merges)}">' + "".join(f'<mergeCell ref="{m}"/>' for m in sh.merges)
                         + "</mergeCells>")
        if sh.lists:
            parts.append(f'<dataValidations count="{len(sh.lists)}">' + "".join(
                f'<dataValidation type="list" allowBlank="1" showErrorMessage="1" sqref="{ref}">'
                f'<formula1>"{escape(",".join(options))}"</formula1></dataValidation>' for ref, options in sh.lists)
                + "</dataValidations>")
        parts.append('<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
                     '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>')
        return "".join(parts)

    def _filter_ref(self, sh: Sheet) -> str:
        head = sh.freeze or 1
        width = len(sh.rows[head - 1]) if len(sh.rows) >= head else 1
        return f"A{head}:{column(max(width, 1) - 1)}{max(len(sh.rows), head)}"

    def _styles_xml(self) -> str:
        fmts, fonts, fills = {}, [{}], [None, "gray125"]
        xfs = []
        for spec in self._styles:
            fmt = spec.get("fmt")
            fmt_id = 0
            if fmt:
                fmts.setdefault(fmt, 164 + len(fmts))
                fmt_id = fmts[fmt]
            font = {k: spec[k] for k in ("bold", "size", "color") if k in spec}
            if font not in fonts:
                fonts.append(font)
            fill = spec.get("fill")
            if fill not in fills:
                fills.append(fill)
            xfs.append((fmt_id, fonts.index(font), fills.index(fill), int(bool(spec.get("border"))), spec.get("wrap")))

        def font_xml(f: dict) -> str:
            return ("<font>" + ("<b/>" if f.get("bold") else "") + f'<sz val="{f.get("size", 11)}"/>'
                    + (f'<color rgb="FF{f["color"]}"/>' if f.get("color") else "") + '<name val="Calibri"/><family val="2"/></font>')

        def fill_xml(f) -> str:
            if f is None:
                return '<fill><patternFill patternType="none"/></fill>'
            if f == "gray125":
                return '<fill><patternFill patternType="gray125"/></fill>'
            return f'<fill><patternFill patternType="solid"><fgColor rgb="FF{f}"/><bgColor indexed="64"/></patternFill></fill>'

        border = ('<border><left/><right/><top/><bottom/><diagonal/></border>',
                  '<border><left style="thin"><color rgb="FFD3D1C7"/></left><right style="thin"><color rgb="FFD3D1C7"/></right>'
                  '<top style="thin"><color rgb="FFD3D1C7"/></top><bottom style="thin"><color rgb="FFD3D1C7"/></bottom>'
                  '<diagonal/></border>')
        out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
        if fmts:
            out.append(f'<numFmts count="{len(fmts)}">' + "".join(
                f'<numFmt numFmtId="{i}" formatCode="{escape(code, {chr(34): "&quot;"})}"/>' for code, i in fmts.items())
                + "</numFmts>")
        out.append(f'<fonts count="{len(fonts)}">' + "".join(font_xml(f) for f in fonts) + "</fonts>")
        out.append(f'<fills count="{len(fills)}">' + "".join(fill_xml(f) for f in fills) + "</fills>")
        out.append('<borders count="2">' + border[0] + border[1] + "</borders>")
        out.append('<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>')
        cells = []
        for fmt_id, font, fill, bord, wrap in xfs:
            applied = (' applyNumberFormat="1"' if fmt_id else "") + (' applyFont="1"' if font else "") \
                + (' applyFill="1"' if fill else "") + (' applyBorder="1"' if bord else "")
            align = '<alignment vertical="top" wrapText="1"/>' if wrap else ""
            cells.append(f'<xf numFmtId="{fmt_id}" fontId="{font}" fillId="{fill}" borderId="{bord}" xfId="0"{applied}'
                         + (f' applyAlignment="1">{align}</xf>' if align else "/>"))
        out.append(f'<cellXfs count="{len(cells)}">' + "".join(cells) + "</cellXfs>")
        out.append('<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        return "".join(out)

    def save(self) -> bytes:
        sheets = [self._sheet_xml(sh, i == 0) for i, sh in enumerate(self.sheets)]   # styles collected while writing
        names = [sh.name for sh in self.sheets]
        defined = "".join(
            f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
            f"&apos;{escape(sh.name)}&apos;!{absolute(self._filter_ref(sh))}</definedName>"
            for i, sh in enumerate(self.sheets) if sh.filters and sh.rows)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml",
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                       '<Default Extension="xml" ContentType="application/xml"/>'
                       '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                       + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                                 'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                                 for i in range(len(sheets)))
                       + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                       '</Types>')
            z.writestr("_rels/.rels",
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                       'Target="xl/workbook.xml"/></Relationships>')
            z.writestr("xl/workbook.xml",
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                       '<bookViews><workbookView activeTab="0"/></bookViews><sheets>'
                       + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
                       + "</sheets>" + (f"<definedNames>{defined}</definedNames>" if defined else "") + "</workbook>")
            z.writestr("xl/_rels/workbook.xml.rels",
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                                 f'relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets)))
                       + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                       'relationships/styles" Target="styles.xml"/></Relationships>')
            for i, xml in enumerate(sheets):
                z.writestr(f"xl/worksheets/sheet{i + 1}.xml", xml)
            z.writestr("xl/styles.xml", self._styles_xml())
        return buf.getvalue()
