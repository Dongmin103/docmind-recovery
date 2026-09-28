"""Create non-sensitive preview fixtures outside the repository for manual QA."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import xlwt
from docx import Document
from docx.shared import Inches as DocxInches
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlsxImage
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches


def _diagram(png_path: Path, bmp_path: Path) -> None:
    """Draw a small source → preview → chunk illustration from scratch."""
    canvas = Image.new("RGB", (420, 112), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    steps = (("SOURCE", "#24579a"), ("PREVIEW", "#168a7a"), ("CHUNKS", "#995cbd"))
    for index, (label, color) in enumerate(steps):
        left = 8 + index * 142
        draw.rounded_rectangle((left, 10, left + 114, 101), radius=11, fill=color)
        draw.rectangle((left + 19, 28, left + 94, 68), outline="white", width=2)
        for row in range(3):
            draw.line((left + 29, 38 + row * 10, left + 84, 38 + row * 10), fill="white", width=2)
        draw.text((left + 24, 78), label, fill="white", font=font)
        if index < 2:
            arrow_x = left + 119
            draw.line((arrow_x, 54, arrow_x + 17, 54), fill="#344054", width=3)
            draw.polygon(((arrow_x + 17, 54), (arrow_x + 10, 49), (arrow_x + 10, 59)), fill="#344054")
    canvas.save(png_path, format="PNG")
    # xlwt embeds an uncompressed 24-bit BMP, while OOXML viewers use PNG.
    canvas.save(bmp_path, format="BMP")


def main(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="preview-diagram-", dir=output) as diagram_dir:
        png_path = Path(diagram_dir) / "diagram.png"
        bmp_path = Path(diagram_dir) / "diagram.bmp"
        _diagram(png_path, bmp_path)

        document = Document()
        document.add_heading("임시 원문 미리보기 합성 시험", 0)
        for page in range(3):
            document.add_heading(f"{page + 1}. 한국어 문서", 1)
            for paragraph in range(8):
                document.add_paragraph(f"검증용으로 생성한 문장 {paragraph + 1}. 실제 사용자 자료가 아닙니다.")
            table = document.add_table(rows=3, cols=2)
            table.cell(0, 0).text = "항목"
            table.cell(0, 1).text = "수량"
            table.cell(1, 0).text = "미리보기"
            table.cell(1, 1).text = "3"
            if page == 0:
                document.add_paragraph("합성 이미지: 원본에서 미리보기와 청크로 이어지는 흐름")
                document.add_picture(str(png_path), width=DocxInches(4.5))
            if page < 2:
                document.add_page_break()
        document.save(output / "synthetic.docx")

        slides = Presentation()
        for index in range(3):
            slide = slides.slides.add_slide(slides.slide_layouts[5])
            slide.shapes.title.text = f"한국어 슬라이드 {index + 1}"
            box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(7), Inches(1))
            box.text_frame.text = "임시 원문 미리보기 · 합성 자료"
            table = slide.shapes.add_table(3, 2, Inches(1), Inches(3), Inches(6), Inches(2)).table
            table.cell(0, 0).text = "항목"
            table.cell(0, 1).text = "값"
            table.cell(1, 0).text = "슬라이드"
            table.cell(1, 1).text = str(index + 1)
            if index == 0:
                slide.shapes.add_picture(str(png_path), Inches(1), Inches(5.4), width=Inches(5.0))
        slides.save(output / "synthetic.pptx")

        book = Workbook()
        for index, sheet_name in enumerate(("매출", "합계")):
            sheet = book.active if index == 0 else book.create_sheet()
            sheet.title = sheet_name
            sheet.append(["품목", "수량", "단가"])
            for row in range(1, 21):
                sheet.append([f"합성 항목 {row}", row, row * 100])
            sheet.freeze_panes = "A2"
            if index == 0:
                sheet.add_image(XlsxImage(str(png_path)), "E2")
        book.save(output / "synthetic.xlsx")

        binary_book = xlwt.Workbook()
        for index, sheet_name in enumerate(("매출", "합계")):
            sheet = binary_book.add_sheet(sheet_name)
            for row in range(20):
                sheet.write(row, 0, f"합성 항목 {row + 1}")
                sheet.write(row, 1, row + 1)
            if index == 0:
                sheet.insert_bitmap(str(bmp_path), 1, 4)
        binary_book.save(str(output / "synthetic.xls"))
    print("Created four synthetic Office fixtures with an embedded diagram; no source documents read.")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
