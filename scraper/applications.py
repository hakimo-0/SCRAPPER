"""Prepare application-email exports and a user-run Google Apps Script."""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit


SHEET_HEADERS = (
    "Firma",
    "Anrede",
    "Person",
    "Email",
    "Betreff",
    "Nachricht",
    "Status",
    "Date Sent",
)

_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
_SHEETS_URL_RE = re.compile(
    r"^https://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]+(?:[/?#][^\s]*)?$"
)
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _file_id_from_url(value: str, *, resource: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https":
        return ""
    host = (parts.hostname or "").lower()
    path_parts = [part for part in parts.path.split("/") if part]
    if resource == "document" and host == "docs.google.com":
        if not path_parts or path_parts[0] != "document":
            return ""
    elif resource == "file" and host == "drive.google.com":
        if not path_parts or path_parts[0] not in {"file", "open", "uc"}:
            return ""
    else:
        return ""

    if "d" in path_parts:
        index = path_parts.index("d")
        if index + 1 < len(path_parts):
            candidate = path_parts[index + 1]
            return candidate if _DRIVE_ID_RE.fullmatch(candidate) else ""
    query_id = parse_qs(parts.query).get("id", [""])[0]
    return query_id if _DRIVE_ID_RE.fullmatch(query_id) else ""


def normalize_script_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize Google resource URLs and IDs without altering valid Sheets URLs."""
    normalized = dict(config)
    spreadsheet = str(config.get("spreadsheet_url") or "").strip()
    if _DRIVE_ID_RE.fullmatch(spreadsheet):
        spreadsheet = f"https://docs.google.com/spreadsheets/d/{spreadsheet}/edit"
    normalized["spreadsheet_url"] = spreadsheet

    for key, resource in (("template_doc_id", "document"), ("static_pdf_id", "file")):
        value = str(config.get(key) or "").strip()
        if not _DRIVE_ID_RE.fullmatch(value):
            value = _file_id_from_url(value, resource=resource) or value
        normalized[key] = value
    return normalized


def _email(value: Any) -> str:
    email = str(value or "").strip().lower()
    return email if _EMAIL_RE.fullmatch(email) else ""


def _person_fields(value: Any, salutation: Any = "") -> tuple[str, str]:
    person = " ".join(str(value or "").split()).strip()
    form = " ".join(str(salutation or "").split()).strip()
    match = re.match(r"^(Herr|Frau)\s+(.+)$", person, re.IGNORECASE)
    if match:
        form = form or match.group(1).capitalize()
        person = match.group(2).strip()
    if form.casefold() not in {"herr", "frau"}:
        form = ""
    return form, person


def build_sheet_rows(
    leads: list[dict[str, Any]],
    selected_ids: list[int],
    *,
    subject_template: str,
    message_template: str,
) -> list[dict[str, str]]:
    """Build ordered, deduplicated sheet rows from explicitly selected CRM leads."""
    selected = {int(lead_id) for lead_id in selected_ids}
    seen_emails: set[str] = set()
    result: list[dict[str, str]] = []
    for lead in leads:
        if int(lead.get("id", -1)) not in selected or lead.get("status") == "opt_out":
            continue
        email = _email(lead.get("email"))
        company = " ".join(str(lead.get("firma") or "").split()).strip()
        if not email or not company or email in seen_emails:
            continue
        seen_emails.add(email)
        salutation, person = _person_fields(lead.get("person"), lead.get("anrede"))
        result.append({
            "Firma": company,
            "Anrede": salutation,
            "Person": person,
            "Email": email,
            "Betreff": subject_template.strip(),
            "Nachricht": message_template.strip(),
            "Status": "",
            "Date Sent": "",
        })
    return result


def csv_bytes(rows: list[dict[str, str]]) -> bytes:
    """Return UTF-8-with-BOM CSV for direct import into Google Sheets."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=SHEET_HEADERS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe_row = dict(row)
        for field in ("Firma", "Anrede", "Person", "Betreff", "Nachricht"):
            value = str(safe_row.get(field, "") or "")
            if re.match(r"^\s*[=+\-@]", value):
                safe_row[field] = "'" + value
        writer.writerow(safe_row)
    return stream.getvalue().encode("utf-8-sig")


def validate_script_config(config: dict[str, Any]) -> None:
    """Reject incomplete or unsafe values before rendering executable code."""
    config = normalize_script_config(config)
    spreadsheet_url = str(config.get("spreadsheet_url") or "").strip()
    sheet_name = str(config.get("sheet_name") or "").strip()
    template_id = str(config.get("template_doc_id") or "").strip()
    pdf_id = str(config.get("static_pdf_id") or "").strip()
    sender_name = str(config.get("sender_name") or "").strip()
    timezone = str(config.get("timezone") or "").strip()
    try:
        daily_limit = int(config.get("max_per_day"))
        min_delay = int(config.get("min_delay_minutes"))
        max_delay = int(config.get("max_delay_minutes"))
        max_attempts = int(config.get("max_attempts", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("Versandlimit und Verzögerungen müssen ganze Zahlen sein.") from exc

    if not _SHEETS_URL_RE.fullmatch(spreadsheet_url):
        raise ValueError("Bitte eine Google-Sheets-URL im Format docs.google.com/spreadsheets eingeben.")
    if not sheet_name or len(sheet_name) > 100:
        raise ValueError("Tabellenblattname erforderlich (1–100 Zeichen).")
    if not _DRIVE_ID_RE.fullmatch(template_id) or not _DRIVE_ID_RE.fullmatch(pdf_id):
        raise ValueError("Ungültige Google-Drive-Datei-IDs für Docs-Vorlage und PDF.")
    if not sender_name or not timezone:
        raise ValueError("Absendername und Zeitzone sind erforderlich.")
    if daily_limit < 0:
        raise ValueError("Tageslimit darf nicht negativ sein; 0 deaktiviert den Versand.")
    if min_delay < 1 or max_delay < min_delay or max_delay > 1440:
        raise ValueError("Verzögerungen müssen zwischen 1 und 1440 Minuten liegen; Maximum muss mindestens Minimum sein.")
    if max_attempts < 1 or max_attempts > 5:
        raise ValueError("Die Anzahl der Versuche muss zwischen 1 und 5 liegen.")


def _js_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def apps_script_source(config: dict[str, Any]) -> str:
    """Generate an Apps Script for manual installation and user-authorized sending."""
    config = normalize_script_config(config)
    validate_script_config(config)
    rendered = f"""const CONFIG = {{
  SPREADSHEET_URL: {_js_string(config["spreadsheet_url"].strip())},
  SHEET_NAME: {_js_string(config["sheet_name"].strip())},
  HEADER_ROW: 1,
  MAX_PER_DAY: {int(config["max_per_day"])},
  MIN_DELAY_MINUTES: {int(config["min_delay_minutes"])},
  MAX_DELAY_MINUTES: {int(config["max_delay_minutes"])},
  MAX_ATTEMPTS: {int(config.get("max_attempts", 3))},
  CONTINUE_ON_ERROR: {str(bool(config.get("continue_on_error", True))).lower()},
  TEMPLATE_DOC_ID: {_js_string(config["template_doc_id"].strip())},
  STATIC_PDF_ID: {_js_string(config["static_pdf_id"].strip())},
  SENDER_NAME: {_js_string(config["sender_name"].strip())},
  TIMEZONE: {_js_string(config["timezone"].strip())}
}};
const HEADERS = {json.dumps(SHEET_HEADERS, ensure_ascii=False)};

function onOpen() {{
  SpreadsheetApp.getUi().createMenu("Bewerbungen")
    .addItem("CSV aus Drive importieren", "importCsvFromDrive")
    .addSeparator()
    .addItem("Täglichen Start um 04:03 programmieren", "setupDailyTrigger")
    .addItem("Nächste Bewerbung jetzt senden", "confirmAndSendNext")
    .addItem("Versand pausieren / stoppen", "deleteAllEmailTriggers")
    .addToUi();
}}

function confirmAndSendNext() {{
  const ui = SpreadsheetApp.getUi();
  const answer = ui.alert(
    "E-Mail-Versand starten",
    "Eine Bewerbung aus dem Tabellenblatt wird jetzt versendet. Gmail-Kontingente und deine rechtlichen Pflichten gelten. Fortfahren?",
    ui.ButtonSet.YES_NO
  );
  if (answer === ui.Button.YES) processNextApplication();
}}

function setupDailyTrigger() {{
  deleteAllEmailTriggers();
  ScriptApp.newTrigger("startChainReaction")
    .timeBased()
    .everyDays(1)
    .atHour(4)
    .nearMinute(3)
    .inTimezone(CONFIG.TIMEZONE)
    .create();
}}

function startChainReaction() {{
  if (!isWorkingDay() || CONFIG.MAX_PER_DAY <= 0) return;
  const props = PropertiesService.getScriptProperties();
  const today = getTodayKey();
  if (props.getProperty("lastRunDate") !== today) {{
    props.setProperty("lastRunDate", today);
    props.setProperty("emailsSentToday", "0");
  }}
  processNextApplication();
}}

function processNextApplication() {{
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) return;
  try {{
    if (!isWorkingDay() || CONFIG.MAX_PER_DAY <= 0) {{
      deleteNextEmailTriggers();
      return;
    }}
    const props = PropertiesService.getScriptProperties();
    const today = getTodayKey();
    if (props.getProperty("lastRunDate") !== today) {{
      props.setProperty("lastRunDate", today);
      props.setProperty("emailsSentToday", "0");
    }}
    let count = Number(props.getProperty("emailsSentToday") || "0");
    if (count >= CONFIG.MAX_PER_DAY) {{
      deleteNextEmailTriggers();
      return;
    }}

    const sheet = getSheet();
    const rows = sheet.getDataRange().getValues();
    let index = -1;
    let earliestRetryAt = 0;
    for (let i = CONFIG.HEADER_ROW; i < rows.length; i++) {{
      const email = normalizeEmail(rows[i][3]);
      const status = String(rows[i][6] || "").trim();
      if (!email) continue;
      if (!isValidEmail(email)) {{
        setRowState(sheet, i + 1, "ERROR", "Ungültige E-Mail-Adresse.");
        continue;
      }}
      const sentRecord = getSentLedger(email);
      if (sentRecord || status === "SENT" || status === "Gesendet") {{
        if (!sentRecord && status === "Gesendet") saveLegacySentLedger(email);
        else if (status !== "SENT" && sentRecord && Number(sentRecord.rowNumber) === i + 1) {{
          setRowState(sheet, i + 1, "SENT", "");
        }} else if (status !== "SENT" && status !== "Gesendet") {{
          setRowState(sheet, i + 1, "DUPLICATE", "E-Mail-Adresse wurde bereits erfolgreich verarbeitet.");
        }}
        continue;
      }}
      if (status === "SENDING" || status === "VERIFY_REQUIRED") {{
        const attempt = getAttempt(email);
        const verified = verifySent(email, attempt.subject || safeCell(rows[i][4]), attempt.bodyHash || "", attempt.startedAt || 0);
        if (verified) {{
          markSent(sheet, i + 1, email, verified.id, attempt.subject || "");
          count++;
          props.setProperty("emailsSentToday", String(count));
          continue;
        }}
        attempt.verifyCount = Number(attempt.verifyCount || 0) + 1;
        if (attempt.verifyCount >= CONFIG.MAX_ATTEMPTS) {{
          saveAttempt(email, attempt);
          setRowState(sheet, i + 1, "VERIFY_REQUIRED", "Vorheriger Sendeversuch nicht verifizierbar; bitte Sent-Ordner prüfen.");
          if (!CONFIG.CONTINUE_ON_ERROR) return;
          continue;
        }}
        const recoveryDelay = retryDelay(attempt.verifyCount);
        attempt.nextRetryAt = Date.now() + recoveryDelay;
        attempt.lastError = "Sendeergebnis unklar; Gmail Sent-Ordner wird erneut geprüft.";
        saveAttempt(email, attempt);
        setRowState(sheet, i + 1, "VERIFY_REQUIRED", attempt.lastError);
        scheduleTriggerAfter(recoveryDelay);
        return;
      }}
      if (status === "ERROR" || status === "DUPLICATE") continue;
      if (status && status !== "READY" && status !== "RETRY") continue;
      const previousAttempt = getAttempt(email);
      if (status === "RETRY" && previousAttempt.nextRetryAt > Date.now()) {{
        if (!earliestRetryAt || previousAttempt.nextRetryAt < earliestRetryAt) {{
          earliestRetryAt = previousAttempt.nextRetryAt;
        }}
        continue;
      }}
      if (status === "RETRY" && previousAttempt.count >= CONFIG.MAX_ATTEMPTS) {{
        setRowState(sheet, i + 1, "ERROR", previousAttempt.lastError || "Maximale Anzahl von Versuchen erreicht.");
        if (!CONFIG.CONTINUE_ON_ERROR) return;
        continue;
      }}
      index = i;
      break;
    }}
    if (index === -1) {{
      if (earliestRetryAt) scheduleTriggerAfter(earliestRetryAt - Date.now());
      else deleteNextEmailTriggers();
      return;
    }}

    const row = rows[index];
    const company = safeCell(row[0]).trim();
    const salutation = String(row[1] || "").trim().toLowerCase();
    const person = safeCell(row[2]).trim();
    const email = normalizeEmail(row[3]);
    let subject = safeCell(row[4]).trim();
    let message = safeCell(row[5]).trim();
    let greeting = "Sehr geehrte Damen und Herren,";
    if (person && salutation === "herr") greeting = `Sehr geehrter Herr ${{person}},`;
    else if (person && salutation === "frau") greeting = `Sehr geehrte Frau ${{person}},`;
    else if (person) greeting = `Sehr geehrte/r Frau/Herr ${{person}},`;
    message = message.replace(/{{SALUTATION}}/g, greeting)
      .replace(/{{COMPANY}}/g, company).replace(/{{PERSON}}/g, person);
    subject = subject.replace(/{{COMPANY}}/g, company).replace(/{{PERSON}}/g, person);

    const attempt = getAttempt(email);
    attempt.count = Number(attempt.count || 0) + 1;
    attempt.verifyCount = 0;
    attempt.startedAt = Date.now();
    attempt.subject = subject;
    attempt.bodyHash = hashText(message);
    attempt.nextRetryAt = 0;
    attempt.lastError = "";
    saveAttempt(email, attempt);
    setRowState(sheet, index + 1, "SENDING", "");
    let mailAttempted = false;
    let mailAccepted = false;
    let coverLetter;
    let dossier;
    try {{
      coverLetter = createCustomPdf(company, greeting);
      if (!coverLetter) throw new Error("Anschreiben PDF konnte nicht erstellt werden.");
      dossier = DriveApp.getFileById(CONFIG.STATIC_PDF_ID)
        .getAs("application/pdf").setName("Lebenslauf_und_Anlagen.pdf");
      SpreadsheetApp.flush();
      mailAttempted = true;
      MailApp.sendEmail({{
        to: email, subject: subject, body: message, name: CONFIG.SENDER_NAME,
        attachments: [coverLetter, dossier]
      }});
      mailAccepted = true;
      markSent(sheet, index + 1, email, "mailapp:" + Date.now(), subject);
      SpreadsheetApp.flush();
      count = Number(props.getProperty("emailsSentToday") || "0");
    }} catch (error) {{
      const detail = safeError(error);
      attempt.lastError = detail;
      if (mailAttempted) {{
        if (!mailAccepted && isRetryableSendRejection(detail) && attempt.count < CONFIG.MAX_ATTEMPTS) {{
          const delay = retryDelay(attempt.count);
          attempt.nextRetryAt = Date.now() + delay;
          saveAttempt(email, attempt);
          setRowState(sheet, index + 1, "RETRY", detail);
          scheduleTriggerAfter(delay);
          return;
        }}
        attempt.nextRetryAt = Date.now() + retryDelay(attempt.count);
        saveAttempt(email, attempt);
        setRowState(sheet, index + 1, "VERIFY_REQUIRED", detail);
        scheduleTriggerAfter(retryDelay(attempt.count));
        console.error("Sendeergebnis unklar für " + email + ": " + detail);
        return;
      }}
      if (isTransientError(detail) && attempt.count < CONFIG.MAX_ATTEMPTS) {{
        const delay = retryDelay(attempt.count);
        attempt.nextRetryAt = Date.now() + delay;
        saveAttempt(email, attempt);
        setRowState(sheet, index + 1, "RETRY", detail);
        scheduleTriggerAfter(delay);
        return;
      }}
      saveAttempt(email, attempt);
      setRowState(sheet, index + 1, "ERROR", detail);
      console.error("Dauerhafter Fehler für " + email + ": " + detail);
      if (CONFIG.CONTINUE_ON_ERROR) scheduleNextTrigger();
      else deleteNextEmailTriggers();
      return;
    }}
    if (count < CONFIG.MAX_PER_DAY) scheduleNextTrigger();
    else deleteNextEmailTriggers();
  }} finally {{
    lock.releaseLock();
  }}
}}

function hashText(value) {{
  return Utilities.base64EncodeWebSafe(
    Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, String(value || ""))
  ).replace(/=+$/g, "");
}}

function attemptKey(email) {{
  return "attempt_" + hashText(normalizeEmail(email));
}}

function sentKey(email) {{
  return "sent_" + hashText(normalizeEmail(email));
}}

function getAttempt(email) {{
  const raw = PropertiesService.getScriptProperties().getProperty(attemptKey(email));
  if (!raw) return {{count: 0, startedAt: 0, nextRetryAt: 0, lastError: ""}};
  try {{ return JSON.parse(raw); }}
  catch (error) {{ return {{count: 0, startedAt: 0, nextRetryAt: 0, lastError: "Versuchsstatus beschädigt."}}; }}
}}

function saveAttempt(email, attempt) {{
  PropertiesService.getScriptProperties().setProperty(attemptKey(email), JSON.stringify(attempt));
}}

function getSentLedger(email) {{
  const raw = PropertiesService.getScriptProperties().getProperty(sentKey(email));
  if (!raw) return null;
  try {{ return JSON.parse(raw); }}
  catch (error) {{ return {{email: normalizeEmail(email), rowNumber: 0}}; }}
}}

function saveLegacySentLedger(email) {{
  PropertiesService.getScriptProperties().setProperty(
    sentKey(email),
    JSON.stringify({{email: normalizeEmail(email), rowNumber: 0, messageId: "legacy-sheet-status", sentAt: new Date().toISOString()}})
  );
}}

function markSent(sheet, rowNumber, email, messageId, subject) {{
  const sentAt = new Date();
  const props = PropertiesService.getScriptProperties();
  if (!props.getProperty(sentKey(email))) {{
    const today = getTodayKey();
    if (props.getProperty("lastRunDate") !== today) {{
      props.setProperty("lastRunDate", today);
      props.setProperty("emailsSentToday", "0");
    }}
    const sentToday = Number(props.getProperty("emailsSentToday") || "0") + 1;
    props.setProperty("emailsSentToday", String(sentToday));
  }}
  const ledger = {{
    email: normalizeEmail(email),
    rowNumber: rowNumber,
    subject: subject || "",
    messageId: messageId || "",
    sentAt: sentAt.toISOString()
  }};
  props.setProperty(sentKey(email), JSON.stringify(ledger));
  setRowState(sheet, rowNumber, "SENT", "");
  sheet.getRange(rowNumber, 8).setValue(formatDate(sentAt));
}}

function setRowState(sheet, rowNumber, state, lastError) {{
  sheet.getRange(rowNumber, 7).setValue(state);
  if (state !== "SENT") sheet.getRange(rowNumber, 8).setValue(formatDate(new Date()));
  const attempt = getAttempt(sheet.getRange(rowNumber, 4).getValue());
  attempt.lastError = lastError || "";
  saveAttempt(sheet.getRange(rowNumber, 4).getValue(), attempt);
}}

function verifySent(email, subject, bodyHash, startedAt) {{
  if (!startedAt) return null;
  const after = Utilities.formatDate(new Date(startedAt - 86400000), CONFIG.TIMEZONE, "yyyy/MM/dd");
  const threads = GmailApp.search("in:sent after:" + after, 0, 100);
  for (const thread of threads) {{
    for (const message of thread.getMessages()) {{
      const recipients = String(message.getTo() || "").match(/[A-Z0-9.!#$%&'*+/=?^_`{{|}}~-]+@[A-Z0-9.-]+/ig) || [];
      if (message.getDate().getTime() < startedAt) continue;
      if (message.getSubject() !== subject) continue;
      if (!recipients.some(address => normalizeEmail(address) === normalizeEmail(email))) continue;
      if (bodyHash && hashText(String(message.getPlainBody() || "").trim()) !== bodyHash) continue;
      return {{id: message.getId()}};
    }}
  }}
  return null;
}}

function retryDelay(attemptCount) {{
  return Math.min(30 * 60 * 1000, 60 * 1000 * Math.pow(2, Math.max(0, attemptCount - 1)));
}}

function isTransientError(message) {{
  return /(429|quota|rate.?limit|temporar|timeout|timed out|unavailable|internal|backend|try again|service invoked too many)/i.test(String(message || ""));
}}

function isRetryableSendRejection(message) {{
  return /(429|quota|rate.?limit|service invoked too many)/i.test(String(message || ""));
}}

function safeError(error) {{
  const message = error && error.message ? String(error.message) : String(error || "Unknown error");
  return message.replace(/[\\r\\n]+/g, " ").slice(0, 300);
}}

function scheduleTriggerAfter(delayMs) {{
  deleteNextEmailTriggers();
  if (CONFIG.MAX_PER_DAY <= 0) return;
  ScriptApp.newTrigger("processNextApplication").timeBased().after(Math.max(1000, delayMs)).create();
}}

function importCsvFromDrive() {{
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) throw new Error("Ein anderer Versand- oder Importvorgang läuft bereits.");
  try {{
    const ui = SpreadsheetApp.getUi();
    const response = ui.prompt(
      "Bewerbungs-CSV importieren",
      "Google-Drive-Datei-ID der zuvor hochgeladenen CSV eingeben:",
      ui.ButtonSet.OK_CANCEL
    );
    if (response.getSelectedButton() !== ui.Button.OK) return;
    const fileId = response.getResponseText().trim();
    if (!/^[A-Za-z0-9_-]+$/.test(fileId)) throw new Error("Ungültige Drive-Datei-ID.");
    let text = DriveApp.getFileById(fileId).getBlob().getDataAsString("UTF-8");
    text = text.replace(/^\\uFEFF/, "");
    const csvRows = Utilities.parseCsv(text);
    if (!csvRows.length || HEADERS.some((header, i) => csvRows[0][i] !== header)) {{
      throw new Error("CSV-Kopfzeile stimmt nicht mit dem Bewerbungsformat überein.");
    }}
    const sheet = getSheet();
    const knownEmails = new Set();
    const existing = sheet.getDataRange().getValues();
    for (let i = CONFIG.HEADER_ROW; i < existing.length; i++) {{
      const email = normalizeEmail(existing[i][3]);
      if (email) knownEmails.add(email);
    }}
    const toAppend = [];
    for (let i = 1; i < csvRows.length; i++) {{
      const row = csvRows[i];
      if (row.length < HEADERS.length) continue;
      const email = normalizeEmail(row[3]);
      if (!isValidEmail(email) || knownEmails.has(email)) continue;
      [0, 1, 2, 4, 5].forEach(i => {{ row[i] = safeCell(row[i]); }});
      row[6] = "";
      row[7] = "";
      toAppend.push(row.slice(0, HEADERS.length));
      knownEmails.add(email);
    }}
    if (toAppend.length) {{
      sheet.getRange(sheet.getLastRow() + 1, 1, toAppend.length, HEADERS.length).setValues(toAppend);
    }}
    ui.alert(toAppend.length + " neue Zeile(n) importiert; vorhandene E-Mail-Adressen wurden übersprungen.");
  }} finally {{
    lock.releaseLock();
  }}
}}

function getSheet() {{
  const spreadsheet = SpreadsheetApp.openByUrl(CONFIG.SPREADSHEET_URL);
  let sheet = spreadsheet.getSheetByName(CONFIG.SHEET_NAME);
  if (!sheet) sheet = spreadsheet.insertSheet(CONFIG.SHEET_NAME);
  if (sheet.getLastRow() === 0) {{
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
  }} else {{
    const actual = sheet.getRange(1, 1, 1, HEADERS.length).getValues()[0];
    if (HEADERS.some((header, i) => actual[i] !== header)) {{
      throw new Error("Die Tabellenkopfzeile muss exakt lauten: " + HEADERS.join(" | "));
    }}
  }}
  return sheet;
}}

function createCustomPdf(company, greeting) {{
  let copy = null;
  try {{
    copy = DriveApp.getFileById(CONFIG.TEMPLATE_DOC_ID).makeCopy("TEMP_Anschreiben_" + Date.now());
    const doc = DocumentApp.openById(copy.getId());
    const body = doc.getBody();
    body.replaceText("{{COMPANY}}", company || "");
    body.replaceText("{{SALUTATION}}", greeting);
    body.replaceText("{{DATE}}", Utilities.formatDate(new Date(), CONFIG.TIMEZONE, "dd.MM.yyyy"));
    doc.saveAndClose();
    return copy.getAs("application/pdf").setName("Anschreiben.pdf");
  }} finally {{
    if (copy) copy.setTrashed(true);
  }}
}}

function scheduleNextTrigger() {{
  deleteNextEmailTriggers();
  if (!isWorkingDay() || CONFIG.MAX_PER_DAY <= 0) return;
  const count = Number(PropertiesService.getScriptProperties().getProperty("emailsSentToday") || "0");
  if (count >= CONFIG.MAX_PER_DAY) return;
  const delay = Math.floor(Math.random() * (CONFIG.MAX_DELAY_MINUTES - CONFIG.MIN_DELAY_MINUTES + 1))
    + CONFIG.MIN_DELAY_MINUTES;
  ScriptApp.newTrigger("processNextApplication").timeBased().after(delay * 60 * 1000).create();
}}

function isWorkingDay() {{
  const day = Number(Utilities.formatDate(new Date(), CONFIG.TIMEZONE, "u"));
  return day >= 1 && day <= 5;
}}

function normalizeEmail(value) {{
  return String(value || "").trim().toLowerCase();
}}

function isValidEmail(value) {{
  return /^[A-Z0-9.!#$%&'*+/=?^_`{{|}}~-]+@[A-Z0-9](?:[A-Z0-9-]{{0,61}}[A-Z0-9])?(?:\\.[A-Z0-9](?:[A-Z0-9-]{{0,61}}[A-Z0-9])?)+$/i.test(value);
}}

function safeCell(value) {{
  const text = String(value || "");
  return /^'[\\s]*[=+\\-@]/.test(text) ? text.slice(1) : text;
}}

function getTodayKey() {{
  return Utilities.formatDate(new Date(), CONFIG.TIMEZONE, "yyyy-MM-dd");
}}

function formatDate(date) {{
  return Utilities.formatDate(date, CONFIG.TIMEZONE, "dd.MM.yyyy HH:mm");
}}

function deleteNextEmailTriggers() {{
  ScriptApp.getProjectTriggers().forEach(trigger => {{
    if (trigger.getHandlerFunction() === "processNextApplication") ScriptApp.deleteTrigger(trigger);
  }});
}}

function deleteAllEmailTriggers() {{
  ScriptApp.getProjectTriggers().forEach(trigger => {{
    if (["startChainReaction", "processNextApplication"].includes(trigger.getHandlerFunction())) {{
      ScriptApp.deleteTrigger(trigger);
    }}
  }});
}}
"""
    return rendered
