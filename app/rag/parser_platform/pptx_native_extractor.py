"""Selective OOXML text extraction for PPTX search ingestion.

Coverage means supported native content, never image OCR or pixel-level fidelity.
Unsupported native structures are reported for fail-closed indexing.
"""

from __future__ import annotations

import hashlib
import html
import math
import posixpath
from dataclasses import dataclass, field
from io import BytesIO
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

from lxml import etree

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
}
VERSION = "1.0.0"
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
EMU_PER_POINT = 12700


@dataclass(frozen=True)
class Block:
    kind: str
    slide: int
    locator: str
    text: str
    bbox: tuple[float, float, float, float] | None = None
    html: str | None = None
    cells: tuple[tuple[int, int, int, int, str], ...] = ()
    chart_values: tuple[tuple[str, str, str], ...] = ()
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Extraction:
    source_hash: str
    slide_count: int
    blocks: tuple[Block, ...]
    warnings: tuple[str, ...]
    coverage_complete: bool
    xml_bytes: int


class _Package:
    def __init__(self, archive):
        self.archive = archive
        entries = archive.infolist()
        if len(entries) > 20_000 or len({i.filename for i in entries}) != len(entries):
            raise ValueError("PPTX entry limit or duplicate entry")
        if sum(i.file_size for i in entries) > 2 * 1024**3:
            raise ValueError("PPTX expanded size limit")
        self.names = {i.filename for i in entries}
        self.cache = {}
        self.xml_bytes = 0

    def xml(self, part):
        if part in self.cache:
            return self.cache[part]
        if part not in self.names:
            raise ValueError("Required PPTX part missing")
        info = self.archive.getinfo(part)
        if (
            info.file_size > 16 * 1024**2
            or self.xml_bytes + info.file_size > 64 * 1024**2
        ):
            raise ValueError("PPTX XML size limit")
        parser = etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            huge_tree=False,
            recover=False,
        )
        raw = self.archive.read(part)
        root = etree.fromstring(raw, parser)
        if root.getroottree().docinfo.doctype:
            raise ValueError("PPTX DTD not allowed")
        self.xml_bytes += len(raw)
        self.cache[part] = root
        return root

    def relations(self, part):
        path = posixpath.join(
            posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
        )
        if path not in self.names:
            return {}
        result = {}
        for relation in self.xml(path):
            rid = relation.get("Id")
            if not rid or rid in result:
                raise ValueError("Duplicate or missing PPTX relationship id")
            kind = relation.get("Type", "").rsplit("/", 1)[-1]
            if relation.get("TargetMode") == "External":
                result[rid] = (kind, None)
                continue
            target = unquote(relation.get("Target", ""))
            if (
                not target
                or "\\" in target
                or "\x00" in target
                or urlsplit(target).scheme
                or "?" in target
                or "#" in target
            ):
                raise ValueError("Unsafe PPTX relationship")
            name = posixpath.normpath(
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.join(posixpath.dirname(part), target)
            )
            if name.startswith("../") or name in {".", ".."}:
                raise ValueError("Escaping PPTX relationship")
            result[rid] = (kind, name)
        return result


def _text(body):
    if body is None:
        return ""
    paragraphs = []
    for paragraph in body.findall("a:p", NS):
        parts = []
        for child in paragraph:
            if child.tag in {f"{{{NS['a']}}}r", f"{{{NS['a']}}}fld"}:
                parts.extend(item.text or "" for item in child.findall("a:t", NS))
            elif child.tag == f"{{{NS['a']}}}br":
                parts.append("\n")
            elif child.tag == f"{{{NS['a']}}}tab":
                parts.append("\t")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs).strip()


def _mul(x, y):
    a, b, c, d, e, f = x
    g, h, i, j, k, offset_y = y
    return (
        a * g + c * h,
        b * g + d * h,
        a * i + c * j,
        b * i + d * j,
        a * k + c * offset_y + e,
        b * k + d * offset_y + f,
    )


def _point(matrix, x, y):
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def _geometry(shape, parent, *, group=False):
    xfrm = shape.find("p:grpSpPr/a:xfrm" if group else "p:spPr/a:xfrm", NS)
    if xfrm is None:
        xfrm = shape.find("p:xfrm", NS)
    if xfrm is None:
        return None, parent
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    if off is None or ext is None:
        return None, parent
    x, y = float(off.get("x", 0)), float(off.get("y", 0))
    w, h = float(ext.get("cx", 0)), float(ext.get("cy", 0))
    if not all(math.isfinite(v) for v in (x, y, w, h)) or min(w, h) < 0:
        raise ValueError("Invalid PPTX geometry")
    angle = math.radians(float(xfrm.get("rot", 0)) / 60_000)
    sx = -1 if xfrm.get("flipH") in {"1", "true"} else 1
    sy = -1 if xfrm.get("flipV") in {"1", "true"} else 1
    turn = (
        math.cos(angle) * sx,
        math.sin(angle) * sx,
        -math.sin(angle) * sy,
        math.cos(angle) * sy,
        0,
        0,
    )
    cx, cy = x + w / 2, y + h / 2
    rotate = _mul((1, 0, 0, 1, cx, cy), _mul(turn, (1, 0, 0, 1, -cx, -cy)))
    world = _mul(parent, rotate)
    corners = [
        _point(world, px, py)
        for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h))
    ]
    bbox = (
        min(v[0] for v in corners) / EMU_PER_POINT,
        min(v[1] for v in corners) / EMU_PER_POINT,
        max(v[0] for v in corners) / EMU_PER_POINT,
        max(v[1] for v in corners) / EMU_PER_POINT,
    )
    child_matrix = world
    if group:
        child_off, child_ext = xfrm.find("a:chOff", NS), xfrm.find("a:chExt", NS)
        if child_off is not None and child_ext is not None:
            cw, ch = float(child_ext.get("cx", 0)), float(child_ext.get("cy", 0))
            if min(cw, ch) <= 0:
                raise ValueError("Invalid PPTX group geometry")
            child_matrix = _mul(
                world,
                (
                    w / cw,
                    0,
                    0,
                    h / ch,
                    x - float(child_off.get("x", 0)) * w / cw,
                    y - float(child_off.get("y", 0)) * h / ch,
                ),
            )
    return bbox, child_matrix


def _table(table):
    cells, html_rows, lines = [], [], []
    rows = table.findall("a:tr", NS)
    columns = len(table.findall("a:tblGrid/a:gridCol", NS))
    occupied = set()
    for row_index, row in enumerate(rows):
        items = row.findall("a:tc", NS)
        if len(items) != columns:
            raise ValueError("Invalid PPTX table grid")
        html_cells, line = [], []
        for column, cell in enumerate(items):
            if cell.get("hMerge") in {"1", "true"} or cell.get("vMerge") in {
                "1",
                "true",
            }:
                if (row_index, column) not in occupied:
                    raise ValueError("Orphan merged PPTX cell")
                continue
            rowspan, colspan = int(cell.get("rowSpan", 1)), int(cell.get("gridSpan", 1))
            if (
                min(rowspan, colspan) < 1
                or row_index + rowspan > len(rows)
                or column + colspan > columns
            ):
                raise ValueError("Invalid PPTX table span")
            region = {
                (r, c)
                for r in range(row_index, row_index + rowspan)
                for c in range(column, column + colspan)
            }
            if region & occupied:
                raise ValueError("Overlapping PPTX table span")
            occupied.update(region)
            value = _text(cell.find("a:txBody", NS))
            cells.append((row_index, column, rowspan, colspan, value))
            line.append(value)
            html_cells.append(
                f'<td rowspan="{rowspan}" colspan="{colspan}">{html.escape(value)}</td>'
            )
        html_rows.append("<tr>" + "".join(html_cells) + "</tr>")
        lines.append("\t".join(line))
    return tuple(cells), "<table>" + "".join(html_rows) + "</table>", "\n".join(lines)


def _cache(parent):
    if parent is None:
        return {}, False, ""
    node = next(
        (
            item
            for item in parent.iter()
            if etree.QName(item).localname
            in {"strCache", "numCache", "strLit", "numLit"}
        ),
        None,
    )
    if node is None:
        return {}, False, ""
    points = {}
    for point in node.findall("c:pt", NS):
        index = int(point.get("idx", -1))
        value = point.find("c:v", NS)
        if index < 0 or index in points or value is None:
            return points, False, ""
        points[index] = value.text or ""
    count = node.find("c:ptCount", NS)
    valid = (
        count is not None
        and int(count.get("val", -1)) == len(points)
        and set(points) == set(range(len(points)))
    )
    return points, valid, node.findtext("c:formatCode", "", NS)


def _chart(root, warnings):
    values, series_details = [], []

    def title_text(node):
        if node is None:
            return ""
        direct = " ".join(node.xpath(".//a:t/text()", namespaces=NS))
        if direct:
            return direct
        cached, valid, _ = _cache(node.find("c:tx", NS))
        if node.find("c:tx", NS) is not None and not valid:
            warnings.add("CHART_TITLE_UNRESOLVED")
        return " ".join(cached[index] for index in sorted(cached))

    title = title_text(root.find("c:chart/c:title", NS))
    axis_labels = [
        title_text(node) for node in root.findall("c:chart/c:plotArea/*/c:title", NS)
    ]
    if root.findall(".//c:dLbl/c:tx", NS) or root.findall(".//c:dLbls/c:tx", NS):
        warnings.add("CHART_CUSTOM_LABEL_NOT_EXTRACTED")
    if root.findall(".//c:dispUnits", NS):
        warnings.add("CHART_DISPLAY_UNITS_NOT_EXTRACTED")
    supported = {
        "barChart",
        "lineChart",
        "pieChart",
        "doughnutChart",
        "areaChart",
        "radarChart",
        "scatterChart",
    }
    series = root.findall(".//c:ser", NS)
    if not series:
        warnings.add("CHART_DATA_INCOMPLETE")
    for ordinal, item in enumerate(series):
        kind = etree.QName(item.getparent()).localname
        if kind not in supported:
            warnings.add("CHART_TYPE_UNSUPPORTED")
        name = item.findtext("c:tx/c:v", "", NS)
        if not name:
            names, _, _ = _cache(item.find("c:tx", NS))
            name = names.get(0, "")
        if not name:
            name = f"Series {ordinal + 1}"
            warnings.add("CHART_SERIES_NAME_MISSING")
        scatter = kind == "scatterChart"
        categories, categories_valid, _ = _cache(
            item.find("c:xVal" if scatter else "c:cat", NS)
        )
        numbers, numbers_valid, format_code = _cache(
            item.find("c:yVal" if scatter else "c:val", NS)
        )
        if (
            not categories_valid
            or not numbers_valid
            or categories.keys() != numbers.keys()
        ):
            warnings.add("CHART_DATA_INCOMPLETE")
        for point in sorted(categories.keys() | numbers.keys()):
            values.append((name, categories.get(point, ""), numbers.get(point, "")))
        series_details.append({"name": name, "type": kind, "format_code": format_code})
    lines = [line for line in (title, " ".join(axis_labels)) if line]
    lines.extend(
        f"{item['name']} [number format: {item['format_code']}]"
        for item in series_details
        if item["format_code"] and item["format_code"] != "General"
    )
    lines.extend("\t".join(row) for row in values)
    return (
        tuple(values),
        "\n".join(lines),
        {"series": series_details, "title": title, "axis_labels": axis_labels},
    )


def extract(source: bytes) -> Extraction:
    """Extract native slide content; source bytes and output remain in memory."""
    if not source or len(source) > 64 * 1024**2:
        raise ValueError("PPTX source size limit")
    blocks, warnings = [], set()
    try:
        with ZipFile(BytesIO(source)) as archive:
            package = _Package(archive)
            presentation = package.xml("ppt/presentation.xml")
            if presentation.tag != f"{{{NS['p']}}}presentation":
                raise ValueError("Unsupported presentation namespace")
            relations = package.relations("ppt/presentation.xml")
            slide_ids = presentation.findall("p:sldIdLst/p:sldId", NS)
            if not slide_ids:
                raise ValueError("PPTX has no slides")
            seen = set()
            inspected_inheritance = set()
            for number, slide_id in enumerate(slide_ids, 1):
                kind, part = relations.get(
                    slide_id.get(f"{{{NS['r']}}}id"), (None, None)
                )
                if kind != "slide" or part is None or part in seen:
                    raise ValueError("Invalid PPTX slide relationship")
                seen.add(part)
                slide = package.xml(part)
                if slide.get("show") in {"0", "false"}:
                    warnings.add("HIDDEN_SLIDE_INCLUDED")
                slide_relations = package.relations(part)
                # Native extraction does not flatten master/layout inheritance. Detect visible
                # inherited content so absence cannot become a complete-coverage claim.
                pending = [
                    target
                    for relation_kind, target in slide_relations.values()
                    if relation_kind == "slideLayout" and target
                ]
                while pending:
                    inherited_part = pending.pop()
                    if inherited_part in inspected_inheritance:
                        continue
                    inspected_inheritance.add(inherited_part)
                    inherited = package.xml(inherited_part)
                    for shape in inherited.findall(".//p:sp", NS):
                        if shape.find(".//p:ph", NS) is None and _text(
                            shape.find("p:txBody", NS)
                        ):
                            warnings.add("INHERITED_CONTENT_NOT_EXTRACTED")
                    if inherited.findall(".//p:graphicFrame", NS):
                        warnings.add("INHERITED_CONTENT_NOT_EXTRACTED")
                    if inherited.findall(".//p:pic", NS):
                        warnings.add("IMAGE_OCR_NOT_RUN")
                    pending.extend(
                        target
                        for relation_kind, target in package.relations(
                            inherited_part
                        ).values()
                        if relation_kind == "slideMaster" and target
                    )
                tree = slide.find("p:cSld/p:spTree", NS)
                if tree is None:
                    raise ValueError("PPTX slide shape tree missing")

                def visit(
                    container, matrix=IDENTITY, path="", depth=0, *,
                    part=part, number=number, slide_relations=slide_relations,
                ):
                    if depth > 64:
                        raise ValueError("PPTX group depth limit")
                    for index, shape in enumerate(container):
                        tag = etree.QName(shape).localname
                        if tag in {"nvGrpSpPr", "grpSpPr", "extLst"}:
                            continue
                        identity = shape.find(".//p:cNvPr", NS)
                        sid = identity.get("id") if identity is not None else str(index)
                        locator = f"{part}#{path}shape/{sid}"
                        if identity is not None and identity.get("hidden") in {
                            "1",
                            "true",
                        }:
                            warnings.add("HIDDEN_SHAPE_INCLUDED")
                        bbox, child_matrix = _geometry(
                            shape, matrix, group=tag == "grpSp"
                        )
                        if tag == "grpSp":
                            visit(shape, child_matrix, f"{path}group/{sid}/", depth + 1)
                        elif tag in {"sp", "cxnSp"}:
                            body = shape.find("p:txBody", NS)
                            value = _text(body)
                            if value:
                                levels = [
                                    int(p.find("a:pPr", NS).get("lvl", 0))
                                    if p.find("a:pPr", NS) is not None
                                    else 0
                                    for p in body.findall("a:p", NS)
                                ]
                                blocks.append(
                                    Block(
                                        "text",
                                        number,
                                        locator,
                                        value,
                                        bbox,
                                        details={"paragraph_levels": levels},
                                    )
                                )
                        elif tag == "graphicFrame":
                            table = shape.find("a:graphic/a:graphicData/a:tbl", NS)
                            chart = shape.find("a:graphic/a:graphicData/c:chart", NS)
                            if table is not None:
                                cells, rendered, value = _table(table)
                                blocks.append(
                                    Block(
                                        "table",
                                        number,
                                        locator,
                                        value,
                                        bbox,
                                        rendered,
                                        cells,
                                    )
                                )
                            elif chart is not None:
                                rel_kind, target = slide_relations.get(
                                    chart.get(f"{{{NS['r']}}}id"), (None, None)
                                )
                                if rel_kind != "chart" or target is None:
                                    warnings.add("CHART_RELATIONSHIP_UNRESOLVED")
                                    continue
                                values, value, details = _chart(
                                    package.xml(target), warnings
                                )
                                blocks.append(
                                    Block(
                                        "chart",
                                        number,
                                        locator,
                                        value,
                                        bbox,
                                        chart_values=values,
                                        details=details,
                                    )
                                )
                            else:
                                diagram = shape.find(
                                    "a:graphic/a:graphicData/dgm:relIds", NS
                                )
                                if diagram is None:
                                    warnings.add("GRAPHIC_UNSUPPORTED")
                                    continue
                                rel_kind, target = slide_relations.get(
                                    diagram.get(f"{{{NS['r']}}}dm"), (None, None)
                                )
                                if rel_kind != "diagramData" or target is None:
                                    warnings.add("SMARTART_DATA_UNRESOLVED")
                                    continue
                                data = package.xml(target)
                                connections = [
                                    (
                                        item.get("srcId"),
                                        item.get("destId"),
                                        item.get("type"),
                                    )
                                    for item in data.findall("dgm:cxnLst/dgm:cxn", NS)
                                ]
                                for point in data.findall("dgm:ptLst/dgm:pt", NS):
                                    value = _text(point.find("dgm:t", NS))
                                    if value:
                                        node_id = point.get("modelId")
                                        if not node_id:
                                            raise ValueError(
                                                "SmartArt node identity missing"
                                            )
                                        blocks.append(
                                            Block(
                                                "smartart",
                                                number,
                                                f"{locator}/node/{node_id}",
                                                value,
                                                bbox,
                                                details={
                                                    "node_id": node_id,
                                                    "connections": connections,
                                                    "bbox_scope": "diagram",
                                                },
                                            )
                                        )
                        elif tag == "pic":
                            warnings.add("IMAGE_OCR_NOT_RUN")
                        else:
                            warnings.add("SHAPE_UNSUPPORTED")

                visit(tree)
            if len({b.locator for b in blocks}) != len(blocks):
                raise ValueError("Duplicate PPTX block identity")
            if not blocks:
                warnings.add("NO_NATIVE_TEXT")
            incomplete = warnings - {
                "IMAGE_OCR_NOT_RUN",
                "HIDDEN_SLIDE_INCLUDED",
                "HIDDEN_SHAPE_INCLUDED",
            }
            return Extraction(
                hashlib.sha256(source).hexdigest(),
                len(slide_ids),
                tuple(blocks),
                tuple(sorted(warnings)),
                not incomplete,
                package.xml_bytes,
            )
    except (BadZipFile, KeyError, etree.XMLSyntaxError, OverflowError) as error:
        raise ValueError("Invalid PPTX package or XML") from error
