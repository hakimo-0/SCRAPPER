# -*- coding: utf-8 -*-
"""CSV, JSON and Excel exports for normalized BA listings."""
from __future__ import annotations

import csv
from io import BytesIO, StringIO
import json


EXPORT_COLUMNS = (
    ("reference_number", "Referenznummer"),
    ("offer_category", "Angebotsart"),
    ("title", "Stellentitel"),
    ("name", "Arbeitgeber"),
    ("city", "Arbeitsort"),
    ("zip", "PLZ"),
    ("street", "Straße"),
    ("start_date", "Beginn"),
    ("publication_date", "Erstveröffentlichung"),
    ("last_modified_date", "Änderungsdatum"),
    ("employment_type", "Beschäftigungsart"),
    ("working_hours", "Arbeitszeit"),
    ("salary", "Vergütung"),
    ("description", "Beschreibung"),
    ("qualifications", "Qualifikationen"),
    ("language_skills", "Sprachkenntnisse"),
    ("application_instructions", "Bewerbungshinweise"),
    ("public_contact", "Öffentlicher Kontakt"),
    ("url", "Original-URL"),
)


def rows(listings: list[dict]) -> list[dict]:
    return [
        {label: listing.get(key, "") for key, label in EXPORT_COLUMNS}
        for listing in listings
    ]


def csv_bytes(listings: list[dict]) -> bytes:
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=[label for _, label in EXPORT_COLUMNS])
    writer.writeheader()
    writer.writerows(rows(listings))
    return buffer.getvalue().encode("utf-8-sig")


def json_bytes(listings: list[dict]) -> bytes:
    return json.dumps(listings, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def excel_bytes(listings: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Jobsuche"
    headers = [label for _, label in EXPORT_COLUMNS]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for row in rows(listings):
        sheet.append([row[header] for header in headers])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(
            55, max(12, max(len(str(cell.value or "")) for cell in column) + 2)
        )
        for cell in column:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()
