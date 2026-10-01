"""Contract tests: source content, relationships, table spans and failure visibility."""

from io import BytesIO
from zipfile import ZipFile

import pytest
from lxml import etree

from rag.parser_platform.pptx_native_extractor import extract
from tools.pptx_native_trial.fixtures import rewrite, synthetic_deck

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


@pytest.fixture(scope="module")
def source():
    return synthetic_deck()


def xml_edit(raw, edit):
    root = etree.fromstring(raw)
    edit(root)
    return etree.tostring(root)


def test_text_groups_and_literal_markup_are_preserved(source):
    result = extract(source)
    assert [block.text for block in result.blocks if block.kind == "text"] == [
        "시험 슬라이드 1",
        "품질 & 속도 <검증>\n둘째 문단",
        "그룹 첫째",
        "그룹 둘째",
    ]
    assert result.slide_count == 1
    assert len({block.locator for block in result.blocks}) == len(result.blocks)
    assert all(block.slide == 1 for block in result.blocks)
    assert not result.warnings


def test_table_preserves_merged_cell_and_does_not_duplicate(source):
    table = next(block for block in extract(source).blocks if block.kind == "table")
    assert table.cells == (
        (0, 0, 1, 1, "항목"),
        (0, 1, 1, 2, "병합 제목"),
        (1, 0, 1, 1, "서울"),
        (1, 1, 1, 1, "12"),
        (1, 2, 1, 1, "-3"),
        (2, 0, 1, 1, "부산"),
        (2, 1, 1, 1, "0"),
        (2, 2, 1, 1, "8"),
    )
    assert 'colspan="2"' in table.html
    assert table.html.count("병합 제목") == 1


def test_chart_pairs_categories_and_values_including_zero_and_negative(source):
    chart = next(block for block in extract(source).blocks if block.kind == "chart")
    assert chart.chart_values == (
        ("매출", "Q1", "-3"),
        ("매출", "Q2", "0"),
        ("매출", "Q3", "12"),
        ("비용", "Q1", "1"),
        ("비용", "Q2", "2"),
        ("비용", "Q3", "3"),
    )
    assert "매출" in chart.text and "Q1" in chart.text and "-3" in chart.text


def test_presentation_relationship_order_is_authoritative():
    def reverse(root):
        ids = root.find("p:sldIdLst", NS)
        ids[:] = list(ids)[::-1]

    source = rewrite(
        synthetic_deck(2), {"ppt/presentation.xml": lambda raw: xml_edit(raw, reverse)}
    )
    titles = [b for b in extract(source).blocks if b.text.startswith("시험 슬라이드")]
    assert [(b.slide, b.text) for b in titles] == [
        (1, "시험 슬라이드 2"),
        (2, "시험 슬라이드 1"),
    ]


def test_break_and_field_text_are_not_silently_concatenated(source):
    def change(root):
        p = root.find(".//p:sp/p:txBody/a:p", NS)
        p.clear()
        for fragment in (
            "<a:r><a:t>A</a:t></a:r>",
            "<a:br/>",
            '<a:fld id="x" type="slidenum"><a:t>7</a:t></a:fld>',
        ):
            p.append(etree.fromstring(f'<x xmlns:a="{NS["a"]}">{fragment}</x>')[0])

    changed = rewrite(
        source, {"ppt/slides/slide1.xml": lambda raw: xml_edit(raw, change)}
    )
    assert extract(changed).blocks[0].text == "A\n7"


def test_missing_chart_cache_warns_instead_of_silent_success(source):
    def remove(root):
        for cache in root.findall(".//c:numCache", NS):
            cache.getparent().remove(cache)

    changed = rewrite(
        source, {"ppt/charts/chart1.xml": lambda raw: xml_edit(raw, remove)}
    )
    result = extract(changed)
    assert "CHART_DATA_INCOMPLETE" in result.warnings
    assert result.coverage_complete is False


def test_group_transform_keeps_global_coordinates(source):
    result = extract(source)
    child = next(b for b in result.blocks if b.text == "그룹 첫째")
    assert child.bbox == pytest.approx((432, 72, 576, 100.8))


def test_unused_media_is_not_inflated(source, monkeypatch):
    source = rewrite(source, additions={"ppt/media/unreferenced.bin": b"x" * 1_000_000})
    actual_read = ZipFile.read

    def checked_read(self, name, *args, **kwargs):
        assert not str(name).startswith("ppt/media/")
        return actual_read(self, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "read", checked_read)
    assert extract(source).slide_count == 1


def test_duplicate_zip_entries_are_rejected(source):
    with ZipFile(BytesIO(source)) as archive:
        existing = archive.read("ppt/presentation.xml")
    with pytest.warns(UserWarning):
        changed = rewrite(source, additions={"ppt/presentation.xml": existing})
    with pytest.raises(ValueError):
        extract(changed)


def test_xml_entity_and_escaping_relationship_are_rejected(source):
    changed = rewrite(
        source,
        {
            "ppt/presentation.xml": lambda _: (
                b'<!DOCTYPE x [<!ENTITY a "bad">]><x>&a;</x>'
            )
        },
    )
    with pytest.raises(ValueError):
        extract(changed)

    def escape(raw):
        return raw.replace(b'Target="slides/slide1.xml"', b'Target="../../outside.xml"')

    changed = rewrite(source, {"ppt/_rels/presentation.xml.rels": escape})
    with pytest.raises(ValueError):
        extract(changed)


def test_missing_required_slide_is_rejected(source):
    changed = rewrite(
        source,
        {
            "ppt/_rels/presentation.xml.rels": lambda raw: raw.replace(
                b"slides/slide1.xml", b"slides/absent.xml"
            )
        },
    )
    with pytest.raises(ValueError):
        extract(changed)


def test_smartart_reads_related_data_and_keeps_node_identity(source):
    dgm = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
    frame = f'''<p:graphicFrame xmlns:p="{NS["p"]}" xmlns:a="{NS["a"]}" xmlns:r="{NS["r"]}" xmlns:dgm="{dgm}">
      <p:nvGraphicFramePr><p:cNvPr id="99" name="Diagram"/></p:nvGraphicFramePr>
      <a:graphic><a:graphicData uri="{dgm}"><dgm:relIds r:dm="smartData"/></a:graphicData></a:graphic>
    </p:graphicFrame>'''
    data = f'''<dgm:dataModel xmlns:dgm="{dgm}" xmlns:a="{NS["a"]}"><dgm:ptLst>
      <dgm:pt modelId="nodeA"><dgm:t><a:p><a:r><a:t>기획</a:t></a:r></a:p></dgm:t></dgm:pt>
      <dgm:pt modelId="nodeB"><dgm:t><a:p><a:r><a:t>검증</a:t></a:r></a:p></dgm:t></dgm:pt>
    </dgm:ptLst><dgm:cxnLst><dgm:cxn srcId="nodeA" destId="nodeB" type="parOf"/></dgm:cxnLst></dgm:dataModel>'''

    def add_frame(root):
        root.find("p:cSld/p:spTree", NS).append(etree.fromstring(frame))

    def add_relation(root):
        etree.SubElement(
            root,
            "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
            Id="smartData",
            Type=NS["r"] + "/diagramData",
            Target="../diagrams/data1.xml",
        )

    changed = rewrite(
        source,
        {
            "ppt/slides/slide1.xml": lambda raw: xml_edit(raw, add_frame),
            "ppt/slides/_rels/slide1.xml.rels": lambda raw: xml_edit(raw, add_relation),
        },
        {"ppt/diagrams/data1.xml": data.encode()},
    )
    result = extract(changed)
    smart = [b for b in result.blocks if b.kind == "smartart"]
    assert [b.text for b in smart] == ["기획", "검증"]
    assert smart[0].details["node_id"] == "nodeA"
    assert smart[0].details["connections"] == [("nodeA", "nodeB", "parOf")]
    assert smart[0].locator != smart[1].locator


def test_multilevel_chart_categories_are_flagged_not_flattened(source):
    def replace(root):
        cat = root.find(".//c:cat", NS)
        cat.clear()
        etree.SubElement(cat, f"{{{NS['c']}}}multiLvlStrRef")

    changed = rewrite(
        source, {"ppt/charts/chart1.xml": lambda raw: xml_edit(raw, replace)}
    )
    assert not extract(changed).coverage_complete


def test_common_adapter_preserves_native_identity_and_single_copy_table(source):
    from tools.pptx_native_trial.docmind_adapter import to_parsed_document
    from rag.parser_platform.chunk_adapter import CommonToStandardChunkAdapter

    parsed = to_parsed_document(extract(source))
    assert parsed.parser_name == "pptx-native"
    chunks = CommonToStandardChunkAdapter().adapt(parsed)
    table = next(c for c in chunks if c["doc_type_kwd"] == "table")
    assert table["content_with_weight"].count("병합 제목") == 1
    assert all(
        c["metadata"]["parser_platform"]["office_locator"]["slide"] == 1 for c in chunks
    )
    assert all(
        c["metadata"]["parser_platform"]["parser_name"] == "pptx-native" for c in chunks
    )


def test_f1_counts_duplicate_and_missing_items():
    from tools.pptx_native_trial.evaluation import score

    assert score(["A", "A", "B"], ["A", "B", "C"]) == {
        "precision": 2 / 3,
        "recall": 2 / 3,
        "f1": 2 / 3,
        "matched": 2,
        "predicted": 3,
        "expected": 3,
    }


def test_visible_master_text_is_extracted_for_each_slide(source):
    def add_text(root):
        shape = etree.fromstring(f'''<p:sp xmlns:p="{NS["p"]}" xmlns:a="{NS["a"]}">
        <p:nvSpPr><p:cNvPr id="99" name="Master notice"/></p:nvSpPr>
        <p:txBody><a:p><a:r><a:t>VISIBLE MASTER NOTICE</a:t></a:r></a:p></p:txBody></p:sp>''')
        root.find("p:cSld/p:spTree", NS).append(shape)

    changed = rewrite(
        synthetic_deck(slides=2),
        {"ppt/slideMasters/slideMaster1.xml": lambda raw: xml_edit(raw, add_text)},
    )
    result = extract(changed)
    assert [block.slide for block in result.blocks if block.text == "VISIBLE MASTER NOTICE"] == [1, 2]
    assert "INHERITED_CONTENT_NOT_EXTRACTED" not in result.warnings
    assert result.coverage_complete


def test_master_visibility_and_slide_override_are_respected(source):
    def add_text(root):
        shape = etree.fromstring(f'''<p:sp xmlns:p="{NS["p"]}" xmlns:a="{NS["a"]}">
        <p:nvSpPr><p:cNvPr id="99" name="Master notice"/></p:nvSpPr>
        <p:txBody><a:p><a:r><a:t>VISIBLE MASTER NOTICE</a:t></a:r></a:p></p:txBody></p:sp>''')
        root.find("p:cSld/p:spTree", NS).append(shape)

    hidden = rewrite(source, {
        "ppt/slideMasters/slideMaster1.xml": lambda raw: xml_edit(raw, add_text),
        "ppt/slides/slide1.xml": lambda raw: xml_edit(raw, lambda root: root.set("showMasterSp", "0")),
    })
    assert all(block.text != "VISIBLE MASTER NOTICE" for block in extract(hidden).blocks)


def test_inherited_groups_apply_transform_and_hidden_ancestor_is_omitted(source):
    def add_groups(root):
        tree = root.find("p:cSld/p:spTree", NS)
        for group_id, hidden in ((90, "0"), (91, "1")):
            tree.append(etree.fromstring(f'''<p:grpSp xmlns:p="{NS["p"]}" xmlns:a="{NS["a"]}">
              <p:nvGrpSpPr><p:cNvPr id="{group_id}" name="group" hidden="{hidden}"/></p:nvGrpSpPr>
              <p:grpSpPr><a:xfrm><a:off x="127000" y="127000"/><a:ext cx="127000" cy="127000"/>
              <a:chOff x="0" y="0"/><a:chExt cx="127000" cy="127000"/></a:xfrm></p:grpSpPr>
              <p:sp><p:nvSpPr><p:cNvPr id="{group_id + 10}" name="notice"/></p:nvSpPr>
              <p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="12700" cy="12700"/></a:xfrm></p:spPr>
              <p:txBody><a:p><a:r><a:t>GROUP {group_id}</a:t></a:r></a:p></p:txBody></p:sp>
              </p:grpSp>'''))

    changed = rewrite(source, {"ppt/slideMasters/slideMaster1.xml": lambda raw: xml_edit(raw, add_groups)})
    result = extract(changed)
    inherited = [block for block in result.blocks if block.text.startswith("GROUP ")]
    assert [block.text for block in inherited] == ["GROUP 90"]
    assert inherited[0].bbox[0] >= 10


def test_distinct_inherited_shapes_without_geometry_are_not_collapsed(source):
    def add_duplicates(root):
        tree = root.find("p:cSld/p:spTree", NS)
        for shape_id in (90, 91):
            tree.append(etree.fromstring(f'''<p:sp xmlns:p="{NS["p"]}" xmlns:a="{NS["a"]}">
              <p:nvSpPr><p:cNvPr id="{shape_id}" name="notice"/></p:nvSpPr>
              <p:txBody><a:p><a:r><a:t>SAME NOTICE</a:t></a:r></a:p></p:txBody></p:sp>'''))

    changed = rewrite(source, {"ppt/slideMasters/slideMaster1.xml": lambda raw: xml_edit(raw, add_duplicates)})
    result = extract(changed)
    assert len([block for block in result.blocks if block.text == "SAME NOTICE"]) == 2


def test_cached_chart_titles_and_format_codes_reach_searchable_text(source):
    def add_title(root):
        title = etree.fromstring(f'''<c:title xmlns:c="{NS["c"]}"><c:tx><c:strRef><c:strCache>
        <c:ptCount val="1"/><c:pt idx="0"><c:v>CACHED CHART TITLE</c:v></c:pt>
        </c:strCache></c:strRef></c:tx></c:title>''')
        root.find("c:chart", NS).insert(0, title)
        root.find(".//c:numCache/c:formatCode", NS).text = "0.0%"

    changed = rewrite(
        source, {"ppt/charts/chart1.xml": lambda raw: xml_edit(raw, add_title)}
    )
    chart = next(b for b in extract(changed).blocks if b.kind == "chart")
    assert "CACHED CHART TITLE" in chart.text
    assert "0.0%" in chart.text


def test_incomplete_warning_survives_common_chunk_adapter(source):
    from rag.parser_platform.chunk_adapter import CommonToStandardChunkAdapter
    from tools.pptx_native_trial.docmind_adapter import to_parsed_document

    def remove(root):
        for cache in root.findall(".//c:numCache", NS):
            cache.getparent().remove(cache)

    changed = rewrite(
        source, {"ppt/charts/chart1.xml": lambda raw: xml_edit(raw, remove)}
    )
    parsed = to_parsed_document(extract(changed))
    chunks = CommonToStandardChunkAdapter().adapt(parsed)
    assert chunks
    assert all(
        "CHART_DATA_INCOMPLETE" in c["metadata"]["parser_platform"]["warning_codes"]
        for c in chunks
    )


def test_custom_chart_label_is_not_silently_ignored(source):
    def add_label(root):
        labels = etree.fromstring(f'''<c:dLbls xmlns:c="{NS["c"]}" xmlns:a="{NS["a"]}"><c:dLbl>
        <c:idx val="0"/><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>CUSTOM LABEL</a:t></a:r></a:p></c:rich></c:tx>
        </c:dLbl></c:dLbls>''')
        root.find(".//c:ser", NS).append(labels)

    changed = rewrite(
        source, {"ppt/charts/chart1.xml": lambda raw: xml_edit(raw, add_label)}
    )
    assert "CHART_CUSTOM_LABEL_NOT_EXTRACTED" in extract(changed).warnings
