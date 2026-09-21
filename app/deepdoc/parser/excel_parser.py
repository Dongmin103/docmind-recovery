#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import logging
import re
import sys
from io import BytesIO

import pandas as pd
from openpyxl import Workbook, load_workbook

from rag.nlp import find_codec
from rag.utils.lazy_image import LazyImage

# copied from `/openpyxl/cell/cell.py`
ILLEGAL_CHARACTERS_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")


class RAGFlowExcelParser:
    @staticmethod
    def _load_excel_to_workbook(file_like_object):
        if isinstance(file_like_object, bytes):
            file_like_object = BytesIO(file_like_object)

        # Read first 4 bytes to determine file type
        file_like_object.seek(0)
        file_head = file_like_object.read(4)
        file_like_object.seek(0)

        if not (file_head.startswith(b"PK\x03\x04") or file_head.startswith(b"\xd0\xcf\x11\xe0")):
            logging.info("Not an Excel file, converting CSV to Excel Workbook")

            try:
                file_like_object.seek(0)
                df = pd.read_csv(file_like_object, header=None, dtype=object, keep_default_na=False, on_bad_lines="skip")
                return RAGFlowExcelParser._dataframe_to_workbook(df)

            except Exception as e_csv:
                raise Exception(f"Failed to parse CSV and convert to Excel Workbook: {e_csv}")

        try:
            return load_workbook(file_like_object, data_only=True)
        except Exception as e:
            logging.info(f"openpyxl load error: {e}, try pandas instead")
            try:
                file_like_object.seek(0)
                try:
                    dfs = pd.read_excel(file_like_object, sheet_name=None, header=None, dtype=object, keep_default_na=False)
                    return RAGFlowExcelParser._dataframe_to_workbook(dfs)
                except Exception as ex:
                    logging.info(f"pandas with default engine load error: {ex}, try calamine instead")
                    file_like_object.seek(0)
                    df = pd.read_excel(file_like_object, engine="calamine", sheet_name=None, header=None, dtype=object, keep_default_na=False)
                    return RAGFlowExcelParser._dataframe_to_workbook(df)
            except Exception as e_pandas:
                raise Exception(f"pandas.read_excel error: {e_pandas}, original openpyxl error: {e}")

    @staticmethod
    def _clean_dataframe(df: pd.DataFrame):
        def clean_string(s):
            if isinstance(s, str):
                return ILLEGAL_CHARACTERS_RE.sub(" ", s)
            return s

        return df.apply(lambda col: col.map(clean_string))

    @staticmethod
    def _fill_worksheet_from_dataframe(ws, df: pd.DataFrame):
        for row_num, row in enumerate(df.values, 1):
            for col_num, value in enumerate(row, 1):
                ws.cell(row=row_num, column=col_num, value=None if RAGFlowExcelParser._is_empty(value) else value)

    @staticmethod
    def _dataframe_to_workbook(df):
        if isinstance(df, dict):
            return RAGFlowExcelParser._dataframes_to_workbook(df)

        df = RAGFlowExcelParser._clean_dataframe(df)
        wb = Workbook()
        ws = wb.active
        ws.title = "Data"
        RAGFlowExcelParser._fill_worksheet_from_dataframe(ws, df)
        return wb

    @staticmethod
    def _dataframes_to_workbook(dfs: dict):
        wb = Workbook()
        default_sheet = wb.active
        wb.remove(default_sheet)

        for sheet_name, df in dfs.items():
            df = RAGFlowExcelParser._clean_dataframe(df)
            ws = wb.create_sheet(title=sheet_name)
            RAGFlowExcelParser._fill_worksheet_from_dataframe(ws, df)
        return wb

    @staticmethod
    def _extract_images_from_worksheet(ws, sheetname=None):
        """
        Extract images from a worksheet and enrich them with vision-based descriptions.

        Returns: List[dict]
        """
        images = getattr(ws, "_images", [])
        if not images:
            return []

        raw_items = []

        for img in images:
            try:
                img_bytes = img._data()
                lazy_img = LazyImage([img_bytes])

                anchor = img.anchor
                if hasattr(anchor, "_from") and hasattr(anchor, "_to"):
                    r1, c1 = anchor._from.row + 1, anchor._from.col + 1
                    r2, c2 = anchor._to.row + 1, anchor._to.col + 1
                    if r1 == r2 and c1 == c2:
                        span = "single_cell"
                    else:
                        span = "multi_cell"
                else:
                    r1, c1 = anchor._from.row + 1, anchor._from.col + 1
                    r2, c2 = r1, c1
                    span = "single_cell"

                item = {
                    "sheet": sheetname or ws.title,
                    "image": lazy_img,
                    "image_description": "",
                    "row_from": r1,
                    "col_from": c1,
                    "row_to": r2,
                    "col_to": c2,
                    "span_type": span,
                }
                raw_items.append(item)
            except Exception:
                continue
        return raw_items

    @staticmethod
    def _get_actual_row_count(ws):
        max_row = ws.max_row
        if not max_row:
            return 0
        if max_row <= 10000:
            return max_row

        max_col = min(ws.max_column or 1, 50)

        def row_has_data(row_idx):
            for col_idx in range(1, max_col + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                if cell.value is not None and str(cell.value).strip():
                    return True
            return False

        if not any(row_has_data(i) for i in range(1, min(101, max_row + 1))):
            return 0

        left, right = 1, max_row
        last_data_row = 1

        while left <= right:
            mid = (left + right) // 2
            found = False
            for r in range(mid, min(mid + 10, max_row + 1)):
                if row_has_data(r):
                    found = True
                    last_data_row = max(last_data_row, r)
                    break
            if found:
                left = mid + 1
            else:
                right = mid - 1

        for r in range(last_data_row, min(last_data_row + 500, max_row + 1)):
            if row_has_data(r):
                last_data_row = r

        return last_data_row

    @staticmethod
    def _get_rows_limited(ws):
        actual_rows = RAGFlowExcelParser._get_actual_row_count(ws)
        if actual_rows == 0:
            return []
        return list(ws.iter_rows(min_row=1, max_row=actual_rows))

    def html(self, fnm, chunk_rows=256):
        from html import escape

        if chunk_rows <= 0:
            raise ValueError("chunk_rows must be positive")
        file_like_object = BytesIO(fnm) if not isinstance(fnm, str) else fnm
        wb = RAGFlowExcelParser._load_excel_to_workbook(file_like_object)
        result = []
        for name in wb.sheetnames:
            ws = wb[name]
            records = RAGFlowExcelParser._worksheet_records(ws)
            if not records:
                continue
            header_row = records[0]["header_row"]
            prefix = [r for r in records if r["row"] < header_row]
            data = [r for r in records if r["row"] > header_row]
            for start in range(0, max(1, len(data)), chunk_rows):
                selected = (prefix if start == 0 else []) + [{"row": header_row}] + data[start:start + chunk_rows]
                caption = name + (" (" + records[0]["context"] + ")" if records[0]["context"] else "")
                table = f"<table><caption>{escape(caption)}</caption>"
                for record in selected:
                    tag = "th" if record["row"] == header_row else "td"
                    table += "<tr>"
                    for cell in ws[record["row"]]:
                        value = RAGFlowExcelParser._format_cell(cell.value)
                        if tag == "th":
                            value = records[0]["column_headers"].get(cell.column, value)
                        table += f"<{tag}>{escape(value)}</{tag}>"
                    table += "</tr>"
                result.append(table + "</table>\n")
        return result

    def markdown(self, fnm):
        import pandas as pd

        file_like_object = BytesIO(fnm) if not isinstance(fnm, str) else fnm
        try:
            file_like_object.seek(0)
            df = pd.read_excel(file_like_object)
        except Exception as e:
            logging.warning(f"Parse spreadsheet error: {e}, trying to interpret as CSV file")
            file_like_object.seek(0)
            df = pd.read_csv(file_like_object, on_bad_lines="skip")
        df = df.replace(r"^\s*$", "", regex=True)
        return df.to_markdown(index=False)

    @staticmethod
    def _is_empty(value):
        if isinstance(value, str):
            return not value.strip()
        return value is None or bool(pd.isna(value))

    @staticmethod
    def _format_cell(value):
        if RAGFlowExcelParser._is_empty(value):
            return ""
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _worksheet_records(ws):
        """Serialize populated cells without losing their column or header identity."""
        rows = RAGFlowExcelParser._get_rows_limited(ws)
        populated = [r for r in rows if any(not RAGFlowExcelParser._is_empty(c.value) for c in r)]
        if not populated:
            return []

        def label(value):
            return re.sub(r"[\s_]+", "", RAGFlowExcelParser._format_cell(value)).casefold()

        header = None
        matrix = False
        definition = False
        table_list = False
        for row in populated:
            labels = {label(c.value) for c in row}
            if {"프로그램id", "프로그램명"} <= labels or {"programid", "programname"} <= labels:
                header, matrix = row, True
                break
            if {"테이블명", "테이블id"} <= labels and any(v in labels for v in {"길이(bytes)", "발생건수", "비고"}):
                header, table_list = row, True
                break
            if {"컬럼명", "컬럼id", "타입"} <= labels or {"columnname", "columnid", "type"} <= labels:
                header, definition = row, True
                break
        if header is None:
            header = next((r for r in populated if sum(isinstance(c.value, str) and not RAGFlowExcelParser._is_empty(c.value) for c in r) >= 2), populated[0])
        header_row = header[0].row
        headers = {c.column: (RAGFlowExcelParser._format_cell(c.value), header_row) for c in header if not RAGFlowExcelParser._is_empty(c.value)}
        if matrix:
            # Lower header cells identify row dimensions. Upper cells identify
            # matrix columns; never shift them across empty intersection cells.
            for row in reversed([r for r in populated if r[0].row < header_row]):
                for cell in row:
                    if cell.column not in headers and isinstance(cell.value, str) and not RAGFlowExcelParser._is_empty(cell.value):
                        headers[cell.column] = (RAGFlowExcelParser._format_cell(cell.value), cell.row)

        context_fields = []
        context_cells = []
        if definition:
            for row in populated:
                if row[0].row >= header_row:
                    break
                values = [c for c in row if not RAGFlowExcelParser._is_empty(c.value)]
                for i, cell in enumerate(values[:-1]):
                    if label(cell.value) in {"테이블id", "테이블명", "tableid", "tablename"}:
                        following = values[i + 1]
                        if label(following.value) in {"테이블id", "테이블명", "tableid", "tablename"}:
                            continue
                        context_fields.append(f"{RAGFlowExcelParser._format_cell(cell.value)}：{RAGFlowExcelParser._format_cell(following.value)}")
                        context_cells.extend([
                            {"row": cell.row, "column": cell.column, "coordinate": cell.coordinate, "role": "context"},
                            {"row": following.row, "column": following.column, "coordinate": following.coordinate, "role": "context"},
                        ])
        context = "; ".join(context_fields)
        records = []
        active_headers = headers
        active_header_row = header_row
        section = "matrix" if matrix else "definition" if definition else "table_list" if table_list else "table"
        program_ids = {"프로그램id", "programid"}
        program_names = {"프로그램명", "programname"}
        ordinal_labels = {"순번", "번호", "구분", "number", "sequence"}
        for row in populated:
            row_number = row[0].row
            values = [c for c in row if not RAGFlowExcelParser._is_empty(c.value)]
            labels = {label(c.value) for c in values}
            role = "prelude" if row_number < header_row else "header" if row_number == header_row else "data"
            if definition and row_number > header_row:
                cue = label(values[0].value) if len(values) == 1 else ""
                if cue in {"index정의", "indexdefinition"}:
                    section, role = "index", "section_title"
                    active_headers, active_header_row = {}, None
                elif "script" in cue and "index" in cue and ("생성" in cue or "create" in cue):
                    section, role = "script", "section_title"
                    active_headers, active_header_row = {}, None
                elif {"컬럼명", "컬럼id", "타입"} <= labels or {"columnname", "columnid", "type"} <= labels:
                    section, role = "definition", "header"
                    active_header_row = row_number
                    active_headers = {c.column: (RAGFlowExcelParser._format_cell(c.value), row_number) for c in values}
                elif section == "index" and {"번호", "index명", "컬럼id"} <= labels:
                    role = "header"
                    active_header_row = row_number
                    active_headers = {c.column: (RAGFlowExcelParser._format_cell(c.value), row_number) for c in values}
            elif table_list and row_number > header_row and len(labels) >= 2 and labels <= {"초기발생", "최대발생", "월증가량"}:
                role = "header"
                active_header_row = row_number
                active_headers = dict(active_headers)
                active_headers.update({c.column: (RAGFlowExcelParser._format_cell(c.value), row_number) for c in values})

            row_headers = active_headers
            summary_anchors = []
            if matrix and row_number > header_row:
                id_columns = [col for col, (title, _) in active_headers.items() if label(title) in program_ids]
                name_columns = [col for col, (title, _) in active_headers.items() if label(title) in program_names]
                ordinal_columns = [col for col, (title, _) in active_headers.items() if label(title) in ordinal_labels]
                dimensions = set(id_columns + name_columns + ordinal_columns)
                table_cells = [c for c in values if c.column in active_headers and c.column not in dimensions]
                def numeric(value):
                    return isinstance(value, (int, float)) and not isinstance(value, bool) and not RAGFlowExcelParser._is_empty(value)
                if (len(id_columns) == len(name_columns) == 1
                        and all(RAGFlowExcelParser._is_empty(row[col - 1].value) for col in ordinal_columns)
                        and numeric(row[id_columns[0] - 1].value)
                        and RAGFlowExcelParser._format_cell(row[name_columns[0] - 1].value) in {"C", "R", "U", "D"}
                        and len(table_cells) >= 2 and all(numeric(c.value) for c in table_cells)):
                    role = "matrix_summary"
                    summary_anchors = id_columns + name_columns
                    row_headers = {col: title for col, title in active_headers.items() if col not in summary_anchors}

            fields, cells = [], []
            for cell in values:
                value = RAGFlowExcelParser._format_cell(cell.value)
                title, title_row = row_headers.get(cell.column, ("", None)) if role in {"data", "matrix_summary"} else ("", None)
                cells.append({"row": cell.row, "column": cell.column, "coordinate": cell.coordinate,
                              "value": value, "header": title, "header_row": title_row})
                fields.append(f"{cell.coordinate} {title + '：' if title else ''}{value}")
            prefix = f"[{ws.title}!{row_number}]"
            if context and row_number > header_row:
                prefix += f" ({context})"
            records.append({"sheet": ws.title, "row": row_number, "header_row": active_header_row,
                            "column_headers": {col: value[0] for col, value in row_headers.items()},
                            "context": context, "context_cells": context_cells, "cells": cells, "row_role": role,
                            "active_section": section, "summary_anchor_columns": summary_anchors,
                            "text": prefix + " " + "; ".join(fields)})
        return records

    def __call__(self, fnm):
        file_like_object = BytesIO(fnm) if not isinstance(fnm, str) else fnm
        wb = RAGFlowExcelParser._load_excel_to_workbook(file_like_object)
        return [record["text"] for name in wb.sheetnames
                for record in RAGFlowExcelParser._worksheet_records(wb[name])]

    @staticmethod
    def row_number(fnm, binary):
        if fnm.split(".")[-1].lower().find("xls") >= 0:
            wb = RAGFlowExcelParser._load_excel_to_workbook(BytesIO(binary))
            total = 0

            for sheetname in wb.sheetnames:
                try:
                    ws = wb[sheetname]
                    total += RAGFlowExcelParser._get_actual_row_count(ws)
                except Exception as e:
                    logging.warning(f"Skip sheet '{sheetname}' due to rows access error: {e}")
                    continue
            return total

        if fnm.split(".")[-1].lower() in ["csv", "txt"]:
            encoding = find_codec(binary)
            txt = binary.decode(encoding, errors="ignore")
            return len(txt.split("\n"))


if __name__ == "__main__":
    psr = RAGFlowExcelParser()
    psr(sys.argv[1])
