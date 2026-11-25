import csv
import io
import json
import os
import re
import shutil
import smtplib
import sqlite3
import traceback
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import List, Optional

import pdfplumber
import requests
import uvicorn
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

# ==========================================
# COMMERCIAL ENGINE CONFIGURATION
# ==========================================
ENGINE_VERSION = "v2.5.6-COMMERCIAL"
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "qwen2.5-coder:14b"
DB_FILE = "jim_invoices.db"
UPLOAD_DIR = "./invoices"
SUMMARY_CSV = "invoices_commercial_ledger.csv"
API_KEY = os.getenv("JIM_API_KEY", "jim-dev-key")
DEFAULT_SME_CURRENCY = "GBP"
LIVE_FX_URL = os.getenv("JIM_LIVE_FX_URL", "https://api.frankfurter.app/latest?from=GBP&to=USD,EUR")

DEFAULT_FX_TO_GBP = {
    "GBP": 1.0,
    "USD": float(os.getenv("JIM_FX_USD_TO_GBP", "0.76")),
    "EUR": float(os.getenv("JIM_FX_EUR_TO_GBP", "0.84")),
}

os.makedirs(UPLOAD_DIR, exist_ok=True)
app = FastAPI(title=f"JIM Commercial Document Engine {ENGINE_VERSION}")


# ==========================================
# AUTHENTICATION DEPENDENCY
# ==========================================
def verify_api_key(x_api_key: Optional[str] = Header(default=None)):
    if x_api_key is None:
        return True
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")
    return True


# ==========================================
# STRICT PYDANTIC SCHEMAS
# ==========================================
class LineItem(BaseModel):
    description: str = Field(default="Unspecified Item")
    quantity: float = Field(default=1.0)
    unit_price: float = Field(default=0.0)
    amount: float = Field(default=0.0)


class ExtractedInvoice(BaseModel):
    vendor_name: str = Field(default="Unknown Vendor")
    vat_number: str = Field(default="Not Found")
    invoice_number: str = Field(default="N/A")
    invoice_date: str = Field(default="N/A")
    currency: str = Field(default=DEFAULT_SME_CURRENCY)
    subtotal: float = Field(default=0.0)
    vat_amount: float = Field(default=0.0)
    total: float = Field(default=0.0)
    line_items: List[LineItem] = Field(default_factory=list)


class CurrencyUpdateRequest(BaseModel):
    currency: str
    convert_amounts: bool = False
    fx_rate: Optional[float] = None


class FxRatesUpdateRequest(BaseModel):
    usd_to_gbp: float = Field(gt=0)
    eur_to_gbp: float = Field(gt=0)


class ConnectAccountPayload(BaseModel):
    stripe_account_id: str
    business_name: str = ""
    fee_disclaimer_accepted: bool = False


class PaymentStatusPayload(BaseModel):
    payment_status: str  # "UNPAID" or "PAID"


class FeedbackPayload(BaseModel):
    category: str = "Extraction / Math"
    invoice_ref: str = ""
    message: str


# ==========================================
# SQLITE COMMERCIAL LEDGER, FX, TAX & PAYMENTS
# ==========================================
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_feedback_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT DEFAULT 'General',
            invoice_ref TEXT DEFAULT '',
            message TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def email_feedback_note(category: str, invoice_ref: str, message: str):
    work_email = os.getenv("JIM_WORK_EMAIL", "").strip()
    smtp_user = os.getenv("JIM_SMTP_USER", "").strip()
    smtp_pass = os.getenv("JIM_SMTP_PASS", "").replace(" ", "").strip()
    smtp_host = os.getenv("JIM_SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("JIM_SMTP_PORT", "587"))

    missing = [
        name for name, val in [
            ("JIM_WORK_EMAIL", work_email),
            ("JIM_SMTP_USER", smtp_user),
            ("JIM_SMTP_PASS", smtp_pass),
        ] if not val
    ]
    if missing:
        print(f"[JIM Feedback Email Skipped] Missing env vars: {', '.join(missing)}")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"[JIM Feedback] {category}" + (f" ({invoice_ref})" if invoice_ref else "")
    msg["From"] = smtp_user
    msg["To"] = work_email
    msg.set_content(
        f"Category: {category}\n"
        f"Invoice / File: {invoice_ref or 'N/A'}\n\n"
        f"Message:\n{message}\n"
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
    print(f"[JIM Feedback Email Sent] Delivered to {work_email}")
    return True


def init_payment_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            stripe_account_id TEXT DEFAULT '',
            business_name TEXT DEFAULT '',
            onboarding_complete INTEGER DEFAULT 0,
            fee_disclaimer_accepted INTEGER DEFAULT 0
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO merchant_settings (id) VALUES (1)")

    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()}
    migrations = {
        "payment_status": "TEXT DEFAULT 'UNPAID'",
        "payment_link": "TEXT DEFAULT ''",
        "payment_provider": "TEXT DEFAULT 'stripe'",
        "est_stripe_fee": "REAL DEFAULT 0.0",
        "net_payout": "REAL DEFAULT 0.0",
        "paid_at": "TEXT DEFAULT ''",
    }
    for col, col_def in migrations.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {col_def}")
    conn.commit()


def init_tax_columns(conn):
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()}
    tax_migrations = {
        "subtotal": "REAL DEFAULT 0.0",
        "vat_amount": "REAL DEFAULT 0.0",
        "vat_rate_label": "TEXT DEFAULT 'Standard / Itemised'",
        "gbp_vat_amount": "REAL DEFAULT 0.0",
    }
    for col, col_def in tax_migrations.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {col_def}")
    conn.commit()


def sync_invoice_tax_fields(conn, invoice_id: int, fx_rates_to_gbp: dict[str, float]):
    row = conn.execute(
        "SELECT currency, subtotal, vat_amount, total FROM invoices WHERE id = ?",
        (invoice_id,),
    ).fetchone()
    if not row:
        return

    curr = (row["currency"] or "GBP").upper()
    gross = float(row["total"] or 0.0)
    vat = float(row["vat_amount"] or 0.0)
    net = float(row["subtotal"] or 0.0)

    if net == 0.0 and gross > 0.0:
        net = round(max(0.0, gross - vat), 2)

    rate_to_gbp = 1.0 if curr == "GBP" else float(fx_rates_to_gbp.get(curr, 0.0) or 0.0)
    gbp_vat = round(vat * rate_to_gbp, 2) if rate_to_gbp > 0 else round(vat, 2)

    conn.execute(
        "UPDATE invoices SET subtotal = ?, vat_amount = ?, gbp_vat_amount = ? WHERE id = ?",
        (net, vat, gbp_vat, invoice_id),
    )


def estimate_uk_stripe_fee(total_amount: float, currency: str = "GBP", business_card: bool = False) -> dict:
    """
    Illustrative UK Stripe fee estimator (1.5% + £0.20 standard UK card;
    2.8% + £0.20 commercial/business UK card; +2% FX if non-GBP settlement).
    """
    rate = 0.028 if business_card else 0.015
    if (currency or "GBP").upper() != "GBP":
        rate += 0.02
    fixed = 0.20
    est_fee = round((total_amount * rate) + fixed, 2) if total_amount > 0 else 0.0
    net_amount = round(max(0.0, total_amount - est_fee), 2)
    return {
        "rate_pct": round(rate * 100, 2),
        "fixed_fee": fixed,
        "est_fee": est_fee,
        "net_payout": net_amount,
    }


def init_db():
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                processed_at TEXT,
                filename TEXT,
                vendor_name TEXT,
                vat_number TEXT,
                invoice_number TEXT,
                invoice_date TEXT,
                currency TEXT,
                subtotal REAL,
                vat_amount REAL,
                total REAL,
                math_valid INTEGER,
                is_duplicate INTEGER DEFAULT 0,
                duplicate_of_id INTEGER,
                discrepancy_note TEXT,
                line_items_json TEXT,
                payment_status TEXT DEFAULT 'UNPAID',
                payment_link TEXT DEFAULT '',
                payment_provider TEXT DEFAULT 'stripe',
                est_stripe_fee REAL DEFAULT 0.0,
                net_payout REAL DEFAULT 0.0,
                paid_at TEXT DEFAULT '',
                vat_rate_label TEXT DEFAULT 'Standard / Itemised',
                gbp_vat_amount REAL DEFAULT 0.0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS fx_rates (
                currency TEXT PRIMARY KEY,
                rate_to_gbp REAL NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        init_payment_schema(conn)
        init_tax_columns(conn)
        init_feedback_table(conn)

        cols = {row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()}
        if "is_duplicate" not in cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN is_duplicate INTEGER DEFAULT 0")
        if "duplicate_of_id" not in cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN duplicate_of_id INTEGER")

        now = datetime.now().isoformat(timespec="seconds")
        for curr, rate in DEFAULT_FX_TO_GBP.items():
            conn.execute(
                """INSERT OR IGNORE INTO fx_rates (currency, rate_to_gbp, updated_at)
                   VALUES (?, ?, ?)""",
                (curr, rate, now),
            )

        fx_rows = conn.execute("SELECT currency, rate_to_gbp FROM fx_rates").fetchall()
        fx_map = {"GBP": 1.0, **DEFAULT_FX_TO_GBP}
        for fr in fx_rows:
            fx_map[(fr["currency"] or "GBP").upper()] = float(fr["rate_to_gbp"] or 0.0)

        rows = conn.execute("SELECT id, currency, total, est_stripe_fee, net_payout FROM invoices").fetchall()
        for r in rows:
            tot = float(r["total"] or 0.0)
            curr = r["currency"] or DEFAULT_SME_CURRENCY
            fee_info = estimate_uk_stripe_fee(tot, curr, business_card=False)
            if float(r["est_stripe_fee"] or 0.0) == 0.0 and tot > 0:
                conn.execute(
                    "UPDATE invoices SET est_stripe_fee = ?, net_payout = ? WHERE id = ?",
                    (fee_info["est_fee"], fee_info["net_payout"], r["id"]),
                )
            sync_invoice_tax_fields(conn, int(r["id"]), fx_map)

        conn.commit()


def get_fx_rates_map() -> dict:
    rates = {
        "GBP": 1.0,
        "USD": DEFAULT_FX_TO_GBP["USD"],
        "EUR": DEFAULT_FX_TO_GBP["EUR"],
        "updated_at": None,
        "source": "stored",
    }
    with get_conn() as conn:
        rows = conn.execute("SELECT currency, rate_to_gbp, updated_at FROM fx_rates").fetchall()
    for r in rows:
        code = normalize_currency(r["currency"])
        rates[code] = float(r["rate_to_gbp"])
        if r["updated_at"] and (not rates["updated_at"] or r["updated_at"] > rates["updated_at"]):
            rates["updated_at"] = r["updated_at"]
    rates["GBP"] = 1.0
    return rates


def set_fx_rates_map(usd_to_gbp: float, eur_to_gbp: float, source: str = "manual") -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        for curr, val in [("GBP", 1.0), ("USD", float(usd_to_gbp)), ("EUR", float(eur_to_gbp))]:
            conn.execute(
                """INSERT INTO fx_rates (currency, rate_to_gbp, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(currency) DO UPDATE SET rate_to_gbp = excluded.rate_to_gbp, updated_at = excluded.updated_at""",
                (curr, val, now),
            )
        fx_map = {"GBP": 1.0, "USD": float(usd_to_gbp), "EUR": float(eur_to_gbp)}
        for inv_row in conn.execute("SELECT id FROM invoices").fetchall():
            sync_invoice_tax_fields(conn, int(inv_row["id"]), fx_map)
        conn.commit()
    out = get_fx_rates_map()
    out["source"] = source
    out["updated_at"] = now
    return out


def refresh_live_fx_rates() -> dict:
    res = requests.get(LIVE_FX_URL, timeout=15)
    res.raise_for_status()
    payload = res.json()
    rates_from_gbp = payload.get("rates") or {}

    usd_per_gbp = float(rates_from_gbp.get("USD") or 0)
    eur_per_gbp = float(rates_from_gbp.get("EUR") or 0)
    if usd_per_gbp <= 0 or eur_per_gbp <= 0:
        raise ValueError("Live FX provider did not return valid USD/EUR rates.")

    usd_to_gbp = round(1.0 / usd_per_gbp, 6)
    eur_to_gbp = round(1.0 / eur_per_gbp, 6)
    updated = set_fx_rates_map(usd_to_gbp=usd_to_gbp, eur_to_gbp=eur_to_gbp, source="live_market")
    updated["market_date"] = payload.get("date")
    return updated


def normalize_currency(code: str) -> str:
    cleaned = (code or DEFAULT_SME_CURRENCY).strip().upper()
    mapping = {"£": "GBP", "$": "USD", "€": "EUR", "UKP": "GBP"}
    return mapping.get(cleaned, cleaned if len(cleaned) == 3 else DEFAULT_SME_CURRENCY)


def _norm_key(val: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (val or "").upper())


def _is_same_invoice(a: dict, b: dict) -> bool:
    vendor_a = _norm_key(a.get("vendor_name"))
    vendor_b = _norm_key(b.get("vendor_name"))
    inv_raw_a = (a.get("invoice_number") or "").strip().upper()
    inv_raw_b = (b.get("invoice_number") or "").strip().upper()
    inv_a = _norm_key(inv_raw_a)
    inv_b = _norm_key(inv_raw_b)
    date_a = (a.get("invoice_date") or "").strip()
    date_b = (b.get("invoice_date") or "").strip()
    total_a = round(float(a.get("total") or 0.0), 2)
    total_b = round(float(b.get("total") or 0.0), 2)

    if vendor_a and vendor_b and vendor_a == vendor_b:
        if inv_a and inv_b and inv_raw_a not in ("N/A", "NONE", "UNKNOWN") and inv_raw_b not in ("N/A", "NONE", "UNKNOWN"):
            return inv_a == inv_b
        if total_a > 0 and abs(total_a - total_b) < 0.01 and date_a and date_b:
            return date_a == date_b
    return False


def _build_chronological_display_map(conn) -> dict[int, int]:
    """Maps internal database id -> contiguous 1..N chronological number."""
    rows = conn.execute(
        """SELECT id,
                  ROW_NUMBER() OVER (ORDER BY processed_at ASC, id ASC) AS display_num
           FROM invoices"""
    ).fetchall()
    return {int(r["id"]): int(r["display_num"]) for r in rows}


def find_duplicate_invoice(record: dict, exclude_id: Optional[int] = None) -> Optional[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, vendor_name, invoice_number, invoice_date, total FROM invoices ORDER BY processed_at ASC, id ASC"
        ).fetchall()

    for r in rows:
        if exclude_id is not None and int(r["id"]) == exclude_id:
            continue
        if _is_same_invoice(record, dict(r)):
            return int(r["id"])
    return None


def rescan_all_duplicates() -> dict:
    with get_conn() as conn:
        display_map = _build_chronological_display_map(conn)
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT id, vendor_name, invoice_number, invoice_date, total, math_valid, discrepancy_note
                   FROM invoices
                   ORDER BY processed_at ASC, id ASC"""
            ).fetchall()
        ]

        seen: list[dict] = []
        updated_count = 0
        flagged_count = 0

        for r in rows:
            dup_of_id = None
            for prev in seen:
                if _is_same_invoice(r, prev):
                    dup_of_id = int(prev["id"])
                    break

            seen.append(r)
            is_dup = 1 if dup_of_id is not None else 0
            dup_display_num = display_map.get(dup_of_id) if dup_of_id is not None else None

            base_note = re.sub(r"^Duplicate of (?:invoice )?#\d+\s*\|\s*", "", r.get("discrepancy_note") or "")
            new_note = (
                f"Duplicate of invoice #{dup_display_num} | {base_note}"
                if dup_display_num is not None
                else base_note
            )

            conn.execute(
                "UPDATE invoices SET is_duplicate = ?, duplicate_of_id = ?, discrepancy_note = ? WHERE id = ?",
                (is_dup, dup_of_id, new_note, r["id"]),
            )
            updated_count += 1
            if is_dup:
                flagged_count += 1

        conn.commit()
    return {"scanned": updated_count, "duplicates_flagged": flagged_count}


def save_invoice_record(record: dict) -> int:
    fee_info = estimate_uk_stripe_fee(float(record["total"]), record["currency"], business_card=False)
    est_fee = float(record.get("est_stripe_fee", fee_info["est_fee"]))
    net_payout = float(record.get("net_payout", fee_info["net_payout"]))
    fx_map = get_fx_rates_map()

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO invoices (
                processed_at, filename, vendor_name, vat_number, invoice_number,
                invoice_date, currency, subtotal, vat_amount, total,
                math_valid, is_duplicate, duplicate_of_id, discrepancy_note, line_items_json,
                payment_status, payment_link, payment_provider, est_stripe_fee, net_payout, paid_at,
                vat_rate_label, gbp_vat_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["processed_at"],
                record["filename"],
                record["vendor_name"],
                record["vat_number"],
                record["invoice_number"],
                record["invoice_date"],
                record["currency"],
                record["subtotal"],
                record["vat_amount"],
                record["total"],
                1 if record["math_valid"] else 0,
                1 if record.get("is_duplicate") else 0,
                record.get("duplicate_of_id"),
                record["discrepancy_note"],
                json.dumps(record["line_items"]),
                record.get("payment_status", "UNPAID"),
                record.get("payment_link", ""),
                record.get("payment_provider", "stripe"),
                est_fee,
                net_payout,
                record.get("paid_at", ""),
                record.get("vat_rate_label", "Standard / Itemised"),
                float(record.get("gbp_vat_amount", 0.0)),
            ),
        )
        new_id = int(cur.lastrowid)
        sync_invoice_tax_fields(conn, new_id, fx_map)
        conn.commit()
        return new_id


def update_invoice_currency(
    invoice_id: int, new_currency: str, convert_amounts: bool = False, fx_rate: Optional[float] = None
) -> Optional[dict]:
    curr = normalize_currency(new_currency)
    fx_map = get_fx_rates_map()

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if not row:
            return None

        display_map = _build_chronological_display_map(conn)
        old_curr = normalize_currency(row["currency"])
        subtotal = float(row["subtotal"])
        vat_amount = float(row["vat_amount"])
        line_items = json.loads(row["line_items_json"] or "[]")

        rate_used = 1.0
        if convert_amounts and old_curr != curr:
            if fx_rate and fx_rate > 0:
                rate_used = float(fx_rate)
            else:
                from_gbp = fx_map.get(old_curr)
                to_gbp = fx_map.get(curr)
                if from_gbp and to_gbp:
                    rate_used = from_gbp / to_gbp
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"No configured FX rate from {old_curr} to {curr}. Provide fx_rate.",
                    )

            subtotal = round(subtotal * rate_used, 2)
            vat_amount = round(vat_amount * rate_used, 2)
            total = round(subtotal + vat_amount, 2)
            for item in line_items:
                item["unit_price"] = round(float(item.get("unit_price", 0.0)) * rate_used, 2)
                item["amount"] = round(float(item.get("amount", 0.0)) * rate_used, 2)
        else:
            total = round(float(row["total"]), 2)

        fee_info = estimate_uk_stripe_fee(total, curr, business_card=False)
        est_fee = fee_info["est_fee"]
        net_payout = fee_info["net_payout"]

        model_check = ExtractedInvoice(
            vendor_name=row["vendor_name"],
            vat_number=row["vat_number"],
            invoice_number=row["invoice_number"],
            invoice_date=row["invoice_date"],
            currency=curr,
            subtotal=subtotal,
            vat_amount=vat_amount,
            total=total,
            line_items=line_items,
        )
        math_valid, discrepancy_note = validate_financials(model_check)
        if convert_amounts and old_curr != curr:
            discrepancy_note = f"{discrepancy_note} [FX {old_curr}->{curr} @ {rate_used:.4f}]"
        if row["is_duplicate"]:
            dup_id = row["duplicate_of_id"]
            dup_disp = display_map.get(int(dup_id)) if dup_id else None
            dup_ref = f" (#{dup_disp})" if dup_disp else ""
            discrepancy_note = f"Duplicate of invoice{dup_ref} | {discrepancy_note}"

        conn.execute(
            """UPDATE invoices
               SET currency = ?, subtotal = ?, vat_amount = ?, total = ?,
                   est_stripe_fee = ?, net_payout = ?,
                   math_valid = ?, discrepancy_note = ?, line_items_json = ?
               WHERE id = ?""",
            (
                curr,
                subtotal,
                vat_amount,
                total,
                est_fee,
                net_payout,
                1 if math_valid else 0,
                discrepancy_note,
                json.dumps(line_items),
                invoice_id,
            ),
        )
        sync_invoice_tax_fields(conn, invoice_id, fx_map)
        conn.commit()
        updated_map = _build_chronological_display_map(conn)
        updated = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        out = dict(updated)
        out["display_num"] = updated_map.get(int(invoice_id))
        out["line_items"] = line_items
        out["math_valid"] = bool(out["math_valid"])
        out["is_duplicate"] = bool(out["is_duplicate"])
        dup_id = out.get("duplicate_of_id")
        out["duplicate_of_display_num"] = updated_map.get(int(dup_id)) if dup_id else None
        return out


def fetch_invoices(
    search: str = "",
    status: str = "ALL",
    currency: str = "ALL",
    payment_status: str = "ALL",
) -> list[dict]:
    query = """
        WITH numbered AS (
            SELECT *,
                   ROW_NUMBER() OVER (ORDER BY processed_at ASC, id ASC) AS display_num
            FROM invoices
        )
        SELECT * FROM numbered WHERE 1=1
    """
    params: list = []

    if status == "PASS":
        query += " AND math_valid = 1 AND is_duplicate = 0"
    elif status == "REVIEW":
        query += " AND math_valid = 0"
    elif status == "DUPLICATE":
        query += " AND is_duplicate = 1"

    if payment_status and payment_status.upper() in ("UNPAID", "PAID"):
        query += " AND UPPER(COALESCE(payment_status, 'UNPAID')) = ?"
        params.append(payment_status.upper())

    if currency and currency.upper() != "ALL":
        query += " AND UPPER(currency) = ?"
        params.append(normalize_currency(currency))

    if search.strip():
        like_term = f"%{search.strip()}%"
        query += (
            " AND (filename LIKE ? OR vendor_name LIKE ? OR vat_number LIKE ? OR invoice_number LIKE ?)"
        )
        params.extend([like_term, like_term, like_term, like_term])

    query += " ORDER BY display_num ASC"

    fx_map = get_fx_rates_map()
    with get_conn() as conn:
        display_map = _build_chronological_display_map(conn)
        rows = conn.execute(query, params).fetchall()

    results = []
    for r in rows:
        item = dict(r)
        try:
            item["line_items"] = json.loads(item.get("line_items_json") or "[]")
        except Exception:
            item["line_items"] = []
        tot = float(item.get("total") or 0.0)
        vat = float(item.get("vat_amount") or 0.0)
        sub = float(item.get("subtotal") or 0.0)
        if sub == 0.0 and tot > 0.0:
            sub = round(max(0.0, tot - vat), 2)
        curr = normalize_currency(item.get("currency") or DEFAULT_SME_CURRENCY)
        rate_to_gbp = 1.0 if curr == "GBP" else float(fx_map.get(curr, 0.0) or 0.0)
        gbp_vat = float(item.get("gbp_vat_amount") if item.get("gbp_vat_amount") is not None else 0.0)
        if gbp_vat == 0.0 and vat > 0.0 and rate_to_gbp > 0.0:
            gbp_vat = round(vat * rate_to_gbp, 2)

        fee_info = estimate_uk_stripe_fee(tot, curr, business_card=False)
        item["subtotal"] = round(sub, 2)
        item["vat_amount"] = round(vat, 2)
        item["gbp_vat_amount"] = round(gbp_vat, 2)
        item["vat_rate_label"] = item.get("vat_rate_label") or "Standard / Itemised"
        item["payment_status"] = (item.get("payment_status") or "UNPAID").upper()
        item["payment_link"] = item.get("payment_link") or ""
        item["payment_provider"] = item.get("payment_provider") or "stripe"
        item["est_stripe_fee"] = round(float(item.get("est_stripe_fee") if item.get("est_stripe_fee") is not None else fee_info["est_fee"]), 2)
        item["net_payout"] = round(float(item.get("net_payout") if item.get("net_payout") is not None else fee_info["net_payout"]), 2)
        item["paid_at"] = item.get("paid_at") or ""
        item["math_valid"] = bool(item["math_valid"])
        item["is_duplicate"] = bool(item["is_duplicate"])
        dup_id = item.get("duplicate_of_id")
        item["duplicate_of_display_num"] = display_map.get(int(dup_id)) if dup_id else None
        results.append(item)
    return results


def compute_reconciliation_summary(records: list[dict]) -> dict:
    gross_total = round(sum(float(r.get("total") or 0.0) for r in records), 2)
    fee_total = round(sum(float(r.get("est_stripe_fee") or 0.0) for r in records), 2)
    net_total = round(sum(float(r.get("net_payout") or 0.0) for r in records), 2)
    gbp_vat_total = round(sum(float(r.get("gbp_vat_amount") or 0.0) for r in records), 2)
    paid_gross = round(sum(float(r.get("total") or 0.0) for r in records if (r.get("payment_status") or "UNPAID").upper() == "PAID"), 2)
    unpaid_gross = round(sum(float(r.get("total") or 0.0) for r in records if (r.get("payment_status") or "UNPAID").upper() != "PAID"), 2)
    paid_count = sum(1 for r in records if (r.get("payment_status") or "UNPAID").upper() == "PAID")
    unpaid_count = len(records) - paid_count
    return {
        "invoice_count": len(records),
        "gross_total": gross_total,
        "est_fee_total": fee_total,
        "net_payout_total": net_total,
        "gbp_vat_total": gbp_vat_total,
        "paid_gross": paid_gross,
        "unpaid_gross": unpaid_gross,
        "paid_count": paid_count,
        "unpaid_count": unpaid_count,
    }


def delete_invoice_record(invoice_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
        conn.commit()
        deleted = cur.rowcount > 0
    if deleted:
        rescan_all_duplicates()
    return deleted


def append_commercial_csv(record: dict):
    file_exists = os.path.exists(SUMMARY_CSV)
    fields = [
        "Invoice #",
        "Vendor",
        "VAT Reg",
        "Date",
        "Currency",
        "Net Amount",
        "VAT Amount",
        "VAT (GBP)",
        "Gross Total",
        "Est. Stripe Fee",
        "Est. Net Payout",
        "Payment Status",
        "Payment Link",
    ]
    row = {
        "Invoice #": record.get("invoice_number") or "",
        "Vendor": record.get("vendor_name") or "",
        "VAT Reg": record.get("vat_number") or "",
        "Date": record.get("invoice_date") or "",
        "Currency": record.get("currency") or "GBP",
        "Net Amount": f"{float(record.get('subtotal') or 0.0):.2f}",
        "VAT Amount": f"{float(record.get('vat_amount') or 0.0):.2f}",
        "VAT (GBP)": f"{float(record.get('gbp_vat_amount') or 0.0):.2f}",
        "Gross Total": f"{float(record.get('total') or 0.0):.2f}",
        "Est. Stripe Fee": f"{float(record.get('est_stripe_fee') or 0.0):.2f}",
        "Est. Net Payout": f"{float(record.get('net_payout') or 0.0):.2f}",
        "Payment Status": record.get("payment_status") or "UNPAID",
        "Payment Link": record.get("payment_link") or "",
    }
    with open(SUMMARY_CSV, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


init_db()


# ==========================================
# DETERMINISTIC MATH & VAT VALIDATION
# ==========================================
def validate_financials(data: ExtractedInvoice) -> tuple[bool, str]:
    issues = []

    expected_total = round(data.subtotal + data.vat_amount, 2)
    diff = round(data.total - expected_total, 2)
    if abs(diff) > 0.02:
        issues.append(
            f"Math mismatch: Subtotal ({data.subtotal:.2f}) + VAT ({data.vat_amount:.2f}) = {expected_total:.2f}, but Total is {data.total:.2f} (Diff: {diff:+.2f})"
        )

    if data.line_items:
        items_sum = round(sum(item.amount for item in data.line_items), 2)
        item_diff = round(data.subtotal - items_sum, 2)
        if data.subtotal > 0 and abs(item_diff) > 0.05:
            issues.append(
                f"Line-item sum ({items_sum:.2f}) differs from Subtotal ({data.subtotal:.2f})"
            )

    if data.vat_number and data.vat_number != "Not Found":
        compact_vat = re.sub(r"[\s\-]", "", data.vat_number.upper())
        if compact_vat.startswith("GB") and not re.match(r"^GB(\d{9}|\d{12})$", compact_vat):
            issues.append(f"Non-standard UK VAT format: {data.vat_number}")

    if issues:
        return False, " | ".join(issues)
    return True, "Verified: Subtotal + VAT reconciles with Total."


# ==========================================
# STRUCTURED LLM EXTRACTION ENGINE
# ==========================================
EXTRACTION_SYSTEM_PROMPT = """
You are a deterministic commercial financial data extraction engine for UK small businesses.
Extract invoice fields from raw PDF text into strict JSON matching this exact schema:
{
  "vendor_name": "string",
  "vat_number": "string (e.g. GB123456789 or 'Not Found')",
  "invoice_number": "string",
  "invoice_date": "YYYY-MM-DD or string",
  "currency": "GBP | USD | EUR",
  "subtotal": 0.00,
  "vat_amount": 0.00,
  "total": 0.00,
  "line_items": [
    {"description": "string", "quantity": 1.0, "unit_price": 0.00, "amount": 0.00}
  ]
}
RULES:
1. Return ONLY valid JSON. No markdown, no explanations, no conversational text.
2. Convert all monetary values to raw floats (strip £, $, €, and commas).
3. Default currency to "GBP" unless another currency symbol or ISO code is explicitly shown on the invoice.
4. Never invent numbers not present in the source text. Use 0.00 if missing.
"""


def extract_and_validate_invoice(file_path: str, filename: str, force_currency: Optional[str] = None) -> dict:
    with pdfplumber.open(file_path) as pdf:
        raw_text = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()

    if not raw_text:
        raise ValueError("PDF contains no extractable text (scanned image PDF requires OCR).")

    res = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"INVOICE FILENAME: {filename}\n\nRAW TEXT:\n{raw_text}"},
            ],
            "format": "json",
            "options": {"temperature": 0.0},
            "stream": False,
        },
        timeout=90,
    )
    res.raise_for_status()
    raw_json_str = res.json().get("message", {}).get("content", "{}")

    parsed_dict = json.loads(raw_json_str)
    parsed_dict["currency"] = normalize_currency(force_currency or parsed_dict.get("currency", DEFAULT_SME_CURRENCY))
    validated_model = ExtractedInvoice(**parsed_dict)
    math_valid, note = validate_financials(validated_model)

    fee_info = estimate_uk_stripe_fee(validated_model.total, validated_model.currency, business_card=False)
    dup_of_id = find_duplicate_invoice(validated_model.model_dump())
    is_duplicate = dup_of_id is not None

    record = {
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "filename": filename,
        **validated_model.model_dump(),
        "est_stripe_fee": fee_info["est_fee"],
        "net_payout": fee_info["net_payout"],
        "payment_status": "UNPAID",
        "payment_link": "",
        "payment_provider": "stripe",
        "paid_at": "",
        "vat_rate_label": "Standard / Itemised",
        "gbp_vat_amount": 0.0,
        "math_valid": math_valid,
        "is_duplicate": is_duplicate,
        "duplicate_of_id": dup_of_id,
        "discrepancy_note": note,
    }

    record["id"] = save_invoice_record(record)
    rescan_all_duplicates()

    with get_conn() as conn:
        disp_map = _build_chronological_display_map(conn)
        saved_row = dict(conn.execute("SELECT * FROM invoices WHERE id = ?", (record["id"],)).fetchone())

    record["display_num"] = disp_map.get(int(record["id"]))
    record["duplicate_of_display_num"] = disp_map.get(int(dup_of_id)) if dup_of_id else None
    record["discrepancy_note"] = saved_row.get("discrepancy_note") or note
    record["gbp_vat_amount"] = float(saved_row.get("gbp_vat_amount") or 0.0)
    append_commercial_csv(record)
    return record


# ==========================================
# ACCOUNTING CSV EXPORTER (NET / VAT / GBP VAT / GROSS / FEE / NET)
# ==========================================
def build_accounting_csv(records: list[dict], export_format: str) -> tuple[str, str]:
    buf = io.StringIO()
    fmt = export_format.lower().strip()

    if fmt == "xero":
        headers = [
            "*ContactName",
            "VATNumber",
            "*InvoiceNumber",
            "*InvoiceDate",
            "*DueDate",
            "*Description",
            "*Quantity",
            "*UnitAmount",
            "*AccountCode",
            "*TaxType",
            "Currency",
            "VATAmount",
            "VAT (GBP)",
            "GrossTotal",
            "EstStripeFee",
            "NetPayout",
            "PaymentStatus",
            "PaymentLink",
            "ReconciliationStatus",
            "DuplicateFlag",
        ]
        writer = csv.DictWriter(buf, fieldnames=headers)
        writer.writeheader()
        for r in records:
            items = r.get("line_items") or [
                {"description": r["filename"], "quantity": 1.0, "unit_price": r["subtotal"], "amount": r["subtotal"]}
            ]
            tax_type = "20% (VAT on Expenses)" if r["vat_amount"] > 0 else "Tax Exempt (0%)"
            for line in items:
                writer.writerow(
                    {
                        "*ContactName": r["vendor_name"],
                        "VATNumber": r["vat_number"],
                        "*InvoiceNumber": r["invoice_number"],
                        "*InvoiceDate": r["invoice_date"],
                        "*DueDate": r["invoice_date"],
                        "*Description": line.get("description", r["filename"]),
                        "*Quantity": line.get("quantity", 1.0),
                        "*UnitAmount": f"{float(line.get('unit_price', 0.0)):.2f}",
                        "*AccountCode": "400",
                        "*TaxType": tax_type,
                        "Currency": r["currency"],
                        "VATAmount": f"{float(r.get('vat_amount') or 0.0):.2f}",
                        "VAT (GBP)": f"{float(r.get('gbp_vat_amount') or 0.0):.2f}",
                        "GrossTotal": f"{float(r['total']):.2f}",
                        "EstStripeFee": f"{float(r.get('est_stripe_fee') or 0.0):.2f}",
                        "NetPayout": f"{float(r.get('net_payout') or 0.0):.2f}",
                        "PaymentStatus": r.get("payment_status", "UNPAID"),
                        "PaymentLink": r.get("payment_link", ""),
                        "ReconciliationStatus": "PASS" if r["math_valid"] else "REVIEW_REQUIRED",
                        "DuplicateFlag": "DUPLICATE" if r.get("is_duplicate") else "UNIQUE",
                    }
                )
        return buf.getvalue(), "jim_xero_bills_export.csv"

    if fmt == "quickbooks":
        headers = [
            "Vendor",
            "VAT_ID",
            "Bill_No",
            "Bill_Date",
            "Category_Account",
            "Description",
            "Qty",
            "Rate",
            "Amount",
            "VAT_Amount",
            "VAT (GBP)",
            "Gross_Total",
            "Est_Stripe_Fee",
            "Net_Payout",
            "Currency",
            "Payment_Status",
            "Payment_Link",
            "Audit_Status",
            "Duplicate_Flag",
        ]
        writer = csv.DictWriter(buf, fieldnames=headers)
        writer.writeheader()
        for r in records:
            items = r.get("line_items") or [
                {"description": r["filename"], "quantity": 1.0, "unit_price": r["subtotal"], "amount": r["subtotal"]}
            ]
            for line in items:
                writer.writerow(
                    {
                        "Vendor": r["vendor_name"],
                        "VAT_ID": r["vat_number"],
                        "Bill_No": r["invoice_number"],
                        "Bill_Date": r["invoice_date"],
                        "Category_Account": "Cost of Goods Sold",
                        "Description": line.get("description", r["filename"]),
                        "Qty": line.get("quantity", 1.0),
                        "Rate": f"{float(line.get('unit_price', 0.0)):.2f}",
                        "Amount": f"{float(line.get('amount', 0.0)):.2f}",
                        "VAT_Amount": f"{float(r['vat_amount']):.2f}",
                        "VAT (GBP)": f"{float(r.get('gbp_vat_amount') or 0.0):.2f}",
                        "Gross_Total": f"{float(r['total']):.2f}",
                        "Est_Stripe_Fee": f"{float(r.get('est_stripe_fee') or 0.0):.2f}",
                        "Net_Payout": f"{float(r.get('net_payout') or 0.0):.2f}",
                        "Currency": r["currency"],
                        "Payment_Status": r.get("payment_status", "UNPAID"),
                        "Payment_Link": r.get("payment_link", ""),
                        "Audit_Status": "PASS" if r["math_valid"] else "REVIEW_REQUIRED",
                        "Duplicate_Flag": "DUPLICATE" if r.get("is_duplicate") else "UNIQUE",
                    }
                )
        return buf.getvalue(), "jim_quickbooks_bills_export.csv"

    headers = [
        "Invoice #",
        "Vendor",
        "VAT Reg",
        "Date",
        "Currency",
        "Net Amount",
        "VAT Amount",
        "VAT (GBP)",
        "Gross Total",
        "Est. Stripe Fee",
        "Est. Net Payout",
        "Payment Status",
        "Payment Link",
    ]
    writer = csv.DictWriter(buf, fieldnames=headers)
    writer.writeheader()
    for r in records:
        csv_row = {
            "Invoice #": r.get("invoice_number") or "",
            "Vendor": r.get("vendor_name") or "",
            "VAT Reg": r.get("vat_number") or "",
            "Date": r.get("invoice_date") or "",
            "Currency": r.get("currency") or "GBP",
            "Net Amount": f"{float(r.get('subtotal') or 0.0):.2f}",
            "VAT Amount": f"{float(r.get('vat_amount') or 0.0):.2f}",
            "VAT (GBP)": f"{float(r.get('gbp_vat_amount') or 0.0):.2f}",
            "Gross Total": f"{float(r.get('total') or 0.0):.2f}",
            "Est. Stripe Fee": f"{float(r.get('est_stripe_fee') or 0.0):.2f}",
            "Est. Net Payout": f"{float(r.get('net_payout') or 0.0):.2f}",
            "Payment Status": r.get("payment_status") or "UNPAID",
            "Payment Link": r.get("payment_link") or "",
        }
        writer.writerow(csv_row)
    return buf.getvalue(), "jim_commercial_ledger.csv"


# ==========================================
# MERCHANT CONNECT, PAYMENT LINK, STATUS & FEEDBACK API
# ==========================================
@app.post("/api/feedback", dependencies=[Depends(verify_api_key)])
def submit_feedback(payload: FeedbackPayload):
    msg = payload.message.strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Please enter a feedback message.")
    cat = payload.category.strip() or "General"
    ref = payload.invoice_ref.strip()

    with get_conn() as conn:
        init_feedback_table(conn)
        conn.execute(
            "INSERT INTO feedback_notes (category, invoice_ref, message) VALUES (?, ?, ?)",
            (cat, ref, msg),
        )
        conn.commit()

    emailed = False
    try:
        emailed = email_feedback_note(cat, ref, msg)
    except Exception as exc:
        print(f"[JIM Feedback Email Error] {repr(exc)}")
        traceback.print_exc()

    return {
        "ok": True,
        "emailed": emailed,
        "message": "Feedback logged and emailed!" if emailed else "Feedback logged locally!",
    }


@app.get("/api/merchant/payment-account", dependencies=[Depends(verify_api_key)])
def get_merchant_payment_account():
    with get_conn() as conn:
        init_payment_schema(conn)
        row = conn.execute("SELECT * FROM merchant_settings WHERE id = 1").fetchone()
    data = dict(row) if row else {}
    data["live_stripe_configured"] = bool(os.getenv("STRIPE_SECRET_KEY", "").strip())
    return data


@app.post("/api/merchant/payment-account", dependencies=[Depends(verify_api_key)])
def save_merchant_payment_account(payload: ConnectAccountPayload):
    if not payload.fee_disclaimer_accepted:
        raise HTTPException(
            status_code=400,
            detail="Please confirm that you understand Stripe processing fees are deducted from customer payouts.",
        )
    acct = payload.stripe_account_id.strip()
    if acct and not acct.startswith("acct_"):
        raise HTTPException(status_code=400, detail="Stripe Connected Account ID must start with 'acct_'.")

    with get_conn() as conn:
        init_payment_schema(conn)
        conn.execute(
            """UPDATE merchant_settings
               SET stripe_account_id = ?, business_name = ?, onboarding_complete = ?, fee_disclaimer_accepted = 1
               WHERE id = 1""",
            (acct, payload.business_name.strip(), 1 if acct else 0),
        )
        conn.commit()
    return {"ok": True, "stripe_account_id": acct}


@app.post("/api/invoices/{invoice_id}/payment-link", dependencies=[Depends(verify_api_key)])
def create_invoice_payment_link(invoice_id: int, business_card: bool = False):
    with get_conn() as conn:
        init_payment_schema(conn)
        inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        merchant = conn.execute("SELECT * FROM merchant_settings WHERE id = 1").fetchone()
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")

        total = float(inv["total"] or 0.0)
        currency = (inv["currency"] or "GBP").upper()
        fee_info = estimate_uk_stripe_fee(total, currency=currency, business_card=business_card)

        stripe_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
        connected_acct = (merchant["stripe_account_id"] or "").strip() if merchant else ""

        if stripe_key and connected_acct:
            import stripe

            stripe.api_key = stripe_key
            session = stripe.checkout.Session.create(
                mode="payment",
                line_items=[
                    {
                        "price_data": {
                            "currency": currency.lower(),
                            "unit_amount": int(round(total * 100)),
                            "product_data": {
                                "name": f"Invoice {inv['invoice_number'] or invoice_id} - {inv['vendor_name'] or 'Client Invoice'}"
                            },
                        },
                        "quantity": 1,
                    }
                ],
                metadata={"jim_invoice_id": str(invoice_id)},
                success_url="http://localhost:8000/?paid=1",
                cancel_url="http://localhost:8000/?canceled=1",
                stripe_account=connected_acct,
            )
            pay_url = session.url
            mode = "stripe_connect"
        else:
            inv_ref = (inv["invoice_number"] or f"INV-{invoice_id}").replace(" ", "-")
            pay_url = f"https://checkout.stripe.com/pay/demo_{inv_ref}_{currency}_{int(round(total * 100))}"
            mode = "demo_preview"

        conn.execute(
            """UPDATE invoices
               SET payment_link = ?, payment_provider = ?, est_stripe_fee = ?, net_payout = ?
               WHERE id = ?""",
            (pay_url, mode, fee_info["est_fee"], fee_info["net_payout"], invoice_id),
        )
        conn.commit()

    return {
        "ok": True,
        "mode": mode,
        "payment_link": pay_url,
        "gross_total": total,
        "currency": currency,
        "fee_estimate": fee_info,
    }


@app.patch("/api/invoices/{invoice_id}/payment-status", dependencies=[Depends(verify_api_key)])
def update_invoice_payment_status(invoice_id: int, payload: PaymentStatusPayload):
    status = payload.payment_status.strip().upper()
    if status not in {"UNPAID", "PAID"}:
        raise HTTPException(status_code=400, detail="Status must be UNPAID or PAID")

    paid_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if status == "PAID" else ""
    with get_conn() as conn:
        inv = conn.execute(
            "SELECT total, currency, est_stripe_fee, net_payout FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if not inv:
            raise HTTPException(status_code=404, detail="Invoice not found")

        fee_info = estimate_uk_stripe_fee(float(inv["total"] or 0.0), inv["currency"] or "GBP")
        est_fee = float(inv["est_stripe_fee"] or fee_info["est_fee"])
        net_payout = float(inv["net_payout"] or fee_info["net_payout"])

        conn.execute(
            """UPDATE invoices
               SET payment_status = ?, paid_at = ?, est_stripe_fee = ?, net_payout = ?
               WHERE id = ?""",
            (status, paid_at, est_fee, net_payout, invoice_id),
        )
        conn.commit()
    return {"ok": True, "payment_status": status, "paid_at": paid_at}


# ==========================================
# COMMERCIAL PORTAL & REMAINING API ENDPOINTS
# ==========================================
@app.get("/", response_class=HTMLResponse)
def commercial_portal():
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>JIM {ENGINE_VERSION} // UK SME Invoice, VAT & Stripe Reconciliation</title>
        <style>
            body {{ background: #090d16; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 24px; margin: 0; }}
            .container {{ max-width: 1420px; margin: 0 auto; background: #131c2e; padding: 24px; border-radius: 14px; border: 1px solid #1e293b; }}
            .header {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; border-bottom: 1px solid #1e293b; padding-bottom: 16px; margin-bottom: 18px; }}
            h1 {{ color: #38bdf8; font-size: 20px; margin: 0; }}
            .badge {{ font-size: 12px; background: rgba(16,185,129,0.12); color: #10b981; border: 1px solid rgba(16,185,129,0.3); padding: 4px 10px; border-radius: 99px; }}
            .kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 12px; }}
            @media (max-width: 900px) {{ .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
            .kpi-card {{ background: #0f172a; border: 1px solid #1e293b; border-radius: 10px; padding: 14px; }}
            .kpi-label {{ color: #94a3b8; font-size: 12px; }}
            .kpi-val {{ font-size: 18px; font-weight: 700; margin-top: 4px; color: #f8fafc; }}
            #selectedInvoiceBanner {{ display: none; justify-content: space-between; align-items: center; background: rgba(56,189,248,0.1); border: 1px solid rgba(56,189,248,0.35); color: #e2e8f0; border-radius: 10px; padding: 9px 14px; margin-bottom: 14px; font-size: 13px; }}
            .dropzone {{ border: 2px dashed #334155; border-radius: 10px; padding: 22px; text-align: center; cursor: pointer; background: #0f172a; transition: 0.2s; }}
            .dropzone:hover {{ border-color: #38bdf8; }}
            .btn {{ background: #2563eb; color: white; border: none; padding: 9px 14px; border-radius: 8px; font-weight: 600; cursor: pointer; text-decoration: none; font-size: 13px; display: inline-block; }}
            .btn-primary {{ background: #2563eb; color: white; border: none; padding: 7px 12px; border-radius: 7px; font-weight: 600; cursor: pointer; font-size: 12px; }}
            .btn-secondary {{ background: #1e293b; color: #e2e8f0; border: 1px solid #334155; padding: 6px 10px; border-radius: 7px; font-weight: 600; cursor: pointer; font-size: 12px; }}
            .btn-paid {{ background: rgba(16,185,129,0.16); color: #10b981; border: 1px solid rgba(16,185,129,0.4); padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; cursor: pointer; }}
            .btn-unpaid {{ background: rgba(245,158,11,0.16); color: #f59e0b; border: 1px solid rgba(245,158,11,0.4); padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; cursor: pointer; }}
            .btn-danger {{ background: #7f1d1d; color: #fecaca; border: 1px solid #ef4444; padding: 5px 9px; border-radius: 6px; font-size: 12px; cursor: pointer; }}
            select, input[type="text"], input[type="number"], textarea {{ background: #0f172a; color: #f8fafc; border: 1px solid #334155; border-radius: 8px; padding: 7px 11px; font-size: 13px; box-sizing: border-box; }}
            input[type="number"].fx-input {{ width: 88px; padding: 5px 8px; }}
            .curr-select {{ padding: 4px 7px; font-size: 12px; }}
            .panel-bar {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; background: #0f172a; border: 1px solid #1e293b; border-radius: 10px; padding: 12px 16px; margin-bottom: 14px; font-size: 12px; }}
            .result-card {{ margin-top: 14px; background: #0f172a; border: 1px solid #1e293b; border-radius: 10px; padding: 15px; }}
            .badge-math, .badge-dup {{ display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; margin-right: 4px; }}
            .badge-math.ok {{ background: rgba(16,185,129,0.14); color: #10b981; border: 1px solid rgba(16,185,129,0.35); }}
            .badge-math.warn {{ background: rgba(245,158,11,0.14); color: #f59e0b; border: 1px solid rgba(245,158,11,0.35); }}
            .badge-dup.alert {{ background: rgba(248,113,113,0.14); color: #f87171; border: 1px solid rgba(248,113,113,0.35); }}
            .badge-dup.clean {{ background: rgba(56,189,248,0.12); color: #38bdf8; border: 1px solid rgba(56,189,248,0.3); }}
            .ledger-section {{ margin-top: 22px; background: #0f172a; border: 1px solid #1e293b; border-radius: 10px; padding: 18px; overflow-x: auto; }}
            .toolbar {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px; margin-bottom: 14px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12.5px; }}
            th, td {{ text-align: left; padding: 8px 8px; border-bottom: 1px solid #1e293b; vertical-align: middle; }}
            th {{ color: #94a3b8; font-weight: 600; }}
            tbody tr.invoice-row {{ cursor: pointer; transition: background 0.15s; }}
            tbody tr.invoice-row:hover {{ background: rgba(56,189,248,0.06); }}
            tbody tr.invoice-row.selected-row {{ background: rgba(56,189,248,0.15); }}
            a.pay-link {{ color: #38bdf8; text-decoration: none; font-weight: 600; }}
            a.pay-link:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div>
                    <h1 style="color:#38bdf8; margin:0;">JIM Ledger <span style="font-size:14px; color:#94a3b8;">v2.5.6</span></h1>
                    <p style="color:#94a3b8; margin:4px 0 12px;">UK SME Invoice Extraction, VAT (GBP) Tracking, Payment Links &amp; Gross/Fee/Net Reconciliation</p>
                </div>
                <div style="display:flex; flex-wrap:wrap; gap:10px; align-items:center;">
                    <button type="button" onclick="openFeedbackModal()" class="btn-secondary">Send Feedback</button>
                    <label style="font-size:12px; color:#94a3b8;">Upload Currency:
                        <select id="uploadCurrency">
                            <option value="AUTO">Auto-Detect (Fallback GBP)</option>
                            <option value="GBP" selected>GBP (£)</option>
                            <option value="USD">USD ($)</option>
                            <option value="EUR">EUR (€)</option>
                        </select>
                    </label>
                    <select id="exportFormat">
                        <option value="standard">Standard Ledger CSV</option>
                        <option value="xero">Xero Bills CSV</option>
                        <option value="quickbooks">QuickBooks Bills CSV</option>
                    </select>
                    <button class="btn" onclick="downloadExport()">Download CSV</button>
                </div>
            </div>

            <div id="feedbackBox" style="display:none; margin-bottom:14px; padding:12px; border:1px solid #334155; border-radius:8px; background:#0f172a; font-size:13px;">
              Email us at <a href="mailto:kianhiatt2011@gmail.com" style="color:#38bdf8;">kianhiatt2011@gmail.com</a>
            </div>

            <div class="kpi-grid">
                <div class="kpi-card">
                    <div class="kpi-label">Gross Invoiced</div>
                    <div class="kpi-val" id="cardGross">0.00</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Total VAT (GBP Equiv.)</div>
                    <div class="kpi-val" style="color:#38bdf8;" id="cardVatGbp">£0.00</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Est. UK Stripe Fees / Net</div>
                    <div class="kpi-val" style="color:#10b981;" id="cardFeeNet">0.00 / 0.00</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">Payment Split (Paid / Unpaid)</div>
                    <div class="kpi-val" style="color:#f59e0b;" id="cardSplit">0 Paid / 0 Unpaid</div>
                </div>
            </div>

            <div id="selectedInvoiceBanner"></div>

            <div class="panel-bar">
                <div style="display:flex; flex-wrap:wrap; gap:12px; align-items:center;">
                    <strong style="color:#38bdf8;">FX Rates (to GBP):</strong>
                    <label style="color:#94a3b8;">1 USD = £
                        <input type="number" step="0.0001" min="0.0001" id="fxUsdToGbp" class="fx-input" value="0.7600">
                    </label>
                    <label style="color:#94a3b8;">1 EUR = £
                        <input type="number" step="0.0001" min="0.0001" id="fxEurToGbp" class="fx-input" value="0.8400">
                    </label>
                    <button class="btn-secondary" onclick="refreshLiveFx()">Fetch Live Rates</button>
                    <button class="btn-secondary" onclick="saveFxRates()">Save FX</button>
                </div>
                <span id="fxStatus" style="color:#94a3b8;">Loading FX status...</span>
            </div>

            <div class="panel-bar">
                <div style="display:flex; flex-wrap:wrap; gap:10px; align-items:center;">
                    <strong style="color:#38bdf8;">UK Merchant / Stripe Connect:</strong>
                    <input type="text" id="merchantBusinessName" placeholder="UK Business Name" style="width:170px;">
                    <input type="text" id="merchantStripeAcct" placeholder="acct_... (Connect ID)" style="width:175px;">
                    <label style="color:#94a3b8;"><input type="checkbox" id="merchantFeeAck"> Confirm Stripe fees are deducted from payouts</label>
                    <button class="btn-secondary" onclick="saveMerchantSettings()">Save Merchant</button>
                </div>
                <span id="merchantStatus" style="color:#94a3b8;">Illustrative UK fee: 1.5%+20p UK card / 2.8%+20p business card (+2% FX)</span>
            </div>

            <div id="dropzone" class="dropzone" onclick="document.getElementById('fileInput').click()">
                <div style="font-size:15px; color:#e2e8f0; margin-bottom:6px;">Drop PDF Invoices Here for Extraction, VAT (GBP) Sync, Payment Links & Reconciliation</div>
                <div style="font-size:12px; color:#64748b;">Defaults to GBP (£) • Click any ledger row to focus top summary cards on that invoice</div>
                <input type="file" id="fileInput" style="display:none;" accept=".pdf" multiple>
            </div>

            <button type="button" onclick="loadSampleInvoice()"
        style="background:#1e293b; color:#e2e8f0; border:1px solid #475569; padding:8px 12px; border-radius:6px; cursor:pointer;">
  Load Sample Invoice
</button>
<p style="font-size:12px; color:#94a3b8; margin-top:6px;">
  Illustrative UK Stripe fee estimates only; confirm VAT &amp; tax records with your accountant.
</p>
            <div id="resultsContainer"></div>

            <div class="ledger-section">
                <div class="toolbar">
                    <h2 style="color:#38bdf8; font-size:16px; margin:0;">Live Invoice & Payment Ledger</h2>
                    <div style="display:flex; flex-wrap:wrap; gap:8px; align-items:center;">
                        <button class="btn-secondary" onclick="scanDuplicates()">Scan Duplicates</button>
                        <label style="color:#94a3b8; font-size:12px;"><input type="checkbox" id="bizCardDefault"> Business Card Rate (2.8%)</label>
                        <label style="color:#94a3b8; font-size:12px;"><input type="checkbox" id="convertFxCheckbox" checked> FX Convert</label>
                        <select id="paymentFilter" onchange="loadLedger()">
                            <option value="ALL">All Payments</option>
                            <option value="UNPAID">UNPAID Only</option>
                            <option value="PAID">PAID Only</option>
                        </select>
                        <select id="currencyFilter" onchange="loadLedger()">
                            <option value="ALL">All Currencies</option>
                            <option value="GBP">GBP (£)</option>
                            <option value="USD">USD ($)</option>
                            <option value="EUR">EUR (€)</option>
                        </select>
                        <input type="text" id="searchInput" placeholder="Search vendor, VAT, invoice #, file..." oninput="loadLedger()"/>
                        <select id="statusFilter" onchange="loadLedger()">
                            <option value="ALL">All Audits</option>
                            <option value="PASS">PASS Only</option>
                            <option value="REVIEW">REVIEW REQUIRED</option>
                            <option value="DUPLICATE">DUPLICATES Only</option>
                        </select>
                    </div>
                </div>

                <!-- Clear reminder on individual VAT & Stripe tax receipts -->
                <div style="font-size:12px; color:#94a3b8; margin-bottom:12px;">
                  <strong style="color:#e2e8f0;">Tax &amp; Receipt Note:</strong>
                  Ledger rows show <strong>Net</strong>, <strong>VAT</strong>, and <strong>Gross</strong> per invoice so individual taxes are easy to verify.
                  Payment links collect the invoice amount—for official <strong>Stripe fee tax receipts/invoices</strong>, check your Stripe Dashboard billing documents directly.
                </div>

                <table>
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Vendor</th>
                            <th>Invoice #</th>
                            <th>Curr</th>
                            <th>Net</th>
                            <th>VAT</th>
                            <th>VAT (GBP)</th>
                            <th>Gross</th>
                            <th>Est. Fee</th>
                            <th>Net Payout</th>
                            <th>Payment</th>
                            <th>Payment Link</th>
                            <th>Audit</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="ledgerBody">
                        <tr><td colspan="14" style="color:#64748b;">Loading ledger...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <script>
            const DEFAULT_API_KEY = "{API_KEY}";
            const dropzone = document.getElementById('dropzone');
            const fileInput = document.getElementById('fileInput');
            const resultsContainer = document.getElementById('resultsContainer');

            let currentFilteredInvoices = [];
            let selectedInvoiceId = null;

            function openFeedbackModal() {{
                const box = document.getElementById("feedbackBox");
                box.style.display = (box.style.display === "block") ? "none" : "block";
            }}

            async function loadSampleInvoice() {{
                try {{
                    const res = await fetch('/api/sample-invoice', {{
                        method: 'POST',
                        headers: {{ 'X-API-Key': DEFAULT_API_KEY }}
                    }});
                    const data = await res.json();
                    if (!res.ok || data.detail) {{
                        alert(data.detail || 'Could not load sample invoice.');
                        return;
                    }}
                    const inv = data.invoice || {{}};
                    const card = document.createElement('div');
                    card.className = 'result-card';
                    card.innerHTML = `
                        <div style="display:flex; justify-content:space-between; margin-bottom:8px; gap:10px;">
                            <strong>${{inv.vendor || inv.vendor_name || 'Northgate Cloud Ltd'}} <span style="color:#64748b; font-weight:normal;">(${{inv.invoice_number || 'INV-8841'}})</span></strong>
                            <span style="color:#94a3b8; font-size:12px;">${{inv.audit_note || inv.discrepancy_note || ''}}</span>
                        </div>
                        <div style="display:grid; grid-template-columns: repeat(5, 1fr); gap:10px; font-size:13px; background:#131c2e; padding:10px; border-radius:8px;">
                            <div><span style="color:#94a3b8;">Net:</span> <b>${{inv.currency || 'GBP'}} ${{Number(inv.net_amount ?? inv.subtotal ?? 0).toFixed(2)}}</b></div>
                            <div><span style="color:#94a3b8;">VAT:</span> <b>${{inv.currency || 'GBP'}} ${{Number(inv.vat_amount ?? 0).toFixed(2)}}</b></div>
                            <div><span style="color:#94a3b8;">VAT (GBP):</span> <b style="color:#38bdf8;">£${{Number(inv.vat_gbp ?? inv.gbp_vat_amount ?? 0).toFixed(2)}}</b></div>
                            <div><span style="color:#94a3b8;">Gross:</span> <b>${{inv.currency || 'GBP'}} ${{Number(inv.gross_amount ?? inv.total ?? 0).toFixed(2)}}</b></div>
                            <div><span style="color:#94a3b8;">Est. Net:</span> <b style="color:#10b981;">${{inv.currency || 'GBP'}} ${{Number(inv.est_net_payout ?? inv.net_payout ?? 0).toFixed(2)}}</b></div>
                        </div>
                    `;
                    resultsContainer.prepend(card);
                    loadLedger();
                }} catch (err) {{
                    alert('Connection error loading sample invoice.');
                }}
            }}
            function selectInvoiceRow(id) {{
                selectedInvoiceId = (selectedInvoiceId === id) ? null : id;
                renderTopSummary(currentFilteredInvoices);
                renderLedgerRows(currentFilteredInvoices);
            }}

            function getSummarySource(filteredRows) {{
                if (selectedInvoiceId !== null) {{
                    const found = filteredRows.find(r => r.id === selectedInvoiceId);
                    if (found) return {{ rows: [found], isSingle: true, selected: found }};
                }}
                return {{ rows: filteredRows, isSingle: false, selected: null }};
            }}

            function renderTopSummary(filteredRows) {{
                const {{ rows, isSingle, selected }} = getSummarySource(filteredRows);

                const grossTotal = rows.reduce((acc, r) => acc + Number(r.total || 0), 0);
                const vatGbpTotal = rows.reduce((acc, r) => acc + Number(r.gbp_vat_amount ?? r.vat_amount ?? 0), 0);
                const feeTotal = rows.reduce((acc, r) => acc + Number(r.est_stripe_fee || 0), 0);
                const netTotal = rows.reduce((acc, r) => acc + Number(r.net_payout || 0), 0);
                const paidCount = rows.filter(r => (r.payment_status || "UNPAID") === "PAID").length;

                document.getElementById("cardGross").textContent =
                    isSingle ? `${{selected.currency || "GBP"}} ${{grossTotal.toFixed(2)}}` : grossTotal.toFixed(2);

                document.getElementById("cardVatGbp").textContent = `£${{vatGbpTotal.toFixed(2)}}`;

                document.getElementById("cardFeeNet").textContent =
                    `${{feeTotal.toFixed(2)}} / ${{netTotal.toFixed(2)}}`;

                document.getElementById("cardSplit").textContent = isSingle
                    ? `${{selected.payment_status || "UNPAID"}} (${{selected.invoice_number || "#" + selected.id}})`
                    : `${{paidCount}} Paid / ${{rows.length - paidCount}} Unpaid`;

                const indicator = document.getElementById("selectedInvoiceBanner");
                if (indicator) {{
                    if (isSingle) {{
                        indicator.style.display = "flex";
                        indicator.innerHTML = `
                            <span>Focused on Invoice <strong>${{selected.invoice_number || "#" + selected.id}}</strong> (${{selected.vendor_name || "Vendor"}})</span>
                            <button type="button" class="btn-secondary" onclick="selectInvoiceRow(${{selected.id}})">Show All Invoices</button>
                        `;
                    }} else {{
                        indicator.style.display = "none";
                    }}
                }}
            }}

            dropzone.addEventListener('dragover', (e) => {{ e.preventDefault(); dropzone.style.borderColor = '#38bdf8'; }});
            dropzone.addEventListener('dragleave', () => {{ dropzone.style.borderColor = '#334155'; }});
            dropzone.addEventListener('drop', (e) => {{
                e.preventDefault();
                dropzone.style.borderColor = '#334155';
                if (e.dataTransfer.files.length > 0) processFiles(e.dataTransfer.files);
            }});
            fileInput.onchange = (e) => {{ if (e.target.files.length > 0) processFiles(e.target.files); }};

            function getFilterQuery() {{
                const fmt = document.getElementById('exportFormat').value;
                const curr = document.getElementById('currencyFilter').value;
                const status = document.getElementById('statusFilter').value;
                const pay = document.getElementById('paymentFilter').value;
                const search = document.getElementById('searchInput').value;
                return `format=${{encodeURIComponent(fmt)}}&currency=${{encodeURIComponent(curr)}}&status=${{encodeURIComponent(status)}}&payment_status=${{encodeURIComponent(pay)}}&search=${{encodeURIComponent(search)}}`;
            }}

            function downloadExport() {{
                window.location.href = `/api/export-csv?${{getFilterQuery()}}`;
            }}

            async function loadMerchantSettings() {{
                try {{
                    const res = await fetch('/api/merchant/payment-account', {{ headers: {{ 'X-API-Key': DEFAULT_API_KEY }} }});
                    const m = await res.json();
                    document.getElementById('merchantBusinessName').value = m.business_name || '';
                    document.getElementById('merchantStripeAcct').value = m.stripe_account_id || '';
                    document.getElementById('merchantFeeAck').checked = !!m.fee_disclaimer_accepted;
                    const modeLabel = m.live_stripe_configured ? 'Live Stripe Key Detected' : 'Local / Demo Payment Link Mode';
                    document.getElementById('merchantStatus').textContent = `${{modeLabel}} • Illustrative UK fee: 1.5%+20p UK / 2.8%+20p business (+2% FX)`;
                }} catch (err) {{
                    console.error(err);
                }}
            }}

            async function saveMerchantSettings() {{
                const payload = {{
                    business_name: document.getElementById('merchantBusinessName').value,
                    stripe_account_id: document.getElementById('merchantStripeAcct').value,
                    fee_disclaimer_accepted: document.getElementById('merchantFeeAck').checked,
                }};
                const res = await fetch('/api/merchant/payment-account', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json', 'X-API-Key': DEFAULT_API_KEY }},
                    body: JSON.stringify(payload)
                }});
                const data = await res.json();
                if (!res.ok) {{
                    alert(data.detail || 'Could not save merchant settings');
                    return;
                }}
                loadMerchantSettings();
            }}

            async function scanDuplicates() {{
                const res = await fetch('/api/invoices/scan-duplicates', {{
                    method: 'POST',
                    headers: {{ 'X-API-Key': DEFAULT_API_KEY }}
                }});
                if (res.ok) loadLedger();
            }}

            function applyFxDataToUi(data, labelPrefix = 'FX') {{
                if (data.USD) document.getElementById('fxUsdToGbp').value = Number(data.USD).toFixed(4);
                if (data.EUR) document.getElementById('fxEurToGbp').value = Number(data.EUR).toFixed(4);
                const statusEl = document.getElementById('fxStatus');
                statusEl.style.color = '#10b981';
                const extra = data.market_date ? ` (mkt ${{data.market_date}})` : '';
                statusEl.textContent = `${{labelPrefix}}: USD=${{Number(data.USD).toFixed(4)}}, EUR=${{Number(data.EUR).toFixed(4)}}${{extra}}`;
            }}

            async function loadFxRates() {{
                const res = await fetch('/api/fx-rates', {{ headers: {{ 'X-API-Key': DEFAULT_API_KEY }} }});
                if (res.ok) applyFxDataToUi(await res.json(), 'Stored FX');
            }}

            async function refreshLiveFx() {{
                const statusEl = document.getElementById('fxStatus');
                statusEl.style.color = '#38bdf8';
                statusEl.textContent = 'Fetching live market rates...';
                const res = await fetch('/api/fx-rates/refresh', {{
                    method: 'POST',
                    headers: {{ 'X-API-Key': DEFAULT_API_KEY }}
                }});
                const data = await res.json();
                if (!res.ok) {{
                    statusEl.style.color = '#f87171';
                    statusEl.textContent = data.detail || 'Live FX fetch failed';
                    return;
                }}
                applyFxDataToUi(data, 'Live Market FX');
                loadLedger();
            }}

            async function saveFxRates() {{
                const usd = parseFloat(document.getElementById('fxUsdToGbp').value);
                const eur = parseFloat(document.getElementById('fxEurToGbp').value);
                const res = await fetch('/api/fx-rates', {{
                    method: 'PUT',
                    headers: {{ 'Content-Type': 'application/json', 'X-API-Key': DEFAULT_API_KEY }},
                    body: JSON.stringify({{ usd_to_gbp: usd, eur_to_gbp: eur }})
                }});
                if (res.ok) {{
                    applyFxDataToUi(await res.json(), 'Manual Override Saved');
                    loadLedger();
                }}
            }}

            async function changeRowCurrency(id, newCurrency) {{
                const convertAmounts = document.getElementById('convertFxCheckbox').checked;
                const res = await fetch(`/api/invoices/${{id}}/currency`, {{
                    method: 'PATCH',
                    headers: {{ 'Content-Type': 'application/json', 'X-API-Key': DEFAULT_API_KEY }},
                    body: JSON.stringify({{ currency: newCurrency, convert_amounts: convertAmounts }})
                }});
                if (res.ok) loadLedger();
            }}

            async function generatePaymentLink(id) {{
                const bizCard = document.getElementById('bizCardDefault').checked;
                const res = await fetch(`/api/invoices/${{id}}/payment-link?business_card=${{bizCard}}`, {{
                    method: 'POST',
                    headers: {{ 'X-API-Key': DEFAULT_API_KEY }}
                }});
                const data = await res.json();
                if (!res.ok) {{
                    alert(data.detail || 'Payment link generation failed');
                    return;
                }}
                loadLedger();
            }}

            async function setPaymentStatus(id, nextStatus) {{
                const res = await fetch(`/api/invoices/${{id}}/payment-status`, {{
                    method: 'PATCH',
                    headers: {{ 'Content-Type': 'application/json', 'X-API-Key': DEFAULT_API_KEY }},
                    body: JSON.stringify({{ payment_status: nextStatus }})
                }});
                const data = await res.json();
                if (!res.ok) {{
                    alert(data.detail || 'Payment status update failed');
                    return;
                }}
                loadLedger();
            }}

            function renderStatusBadges(row) {{
                const titleAttr = row.discrepancy_note ? ` title="${{String(row.discrepancy_note).replace(/"/g, '&quot;')}}"` : '';
                const dupRef = row.duplicate_of_display_num || row.duplicate_of_id || 1;
                return `
                    <span class="badge-math ${{row.math_valid ? 'ok' : 'warn'}}"${{titleAttr}}>
                        ${{row.math_valid ? 'Math: Pass' : 'Math: Check Total'}}
                    </span>
                    <span class="badge-dup ${{row.is_duplicate ? 'alert' : 'clean'}}"${{titleAttr}}>
                        ${{row.is_duplicate ? `Duplicate of #${{dupRef}}` : 'Unique Invoice'}}
                    </span>
                `;
            }}

            function renderLedgerRows(rows) {{
                const tbody = document.getElementById('ledgerBody');
                if (!rows.length) {{
                    tbody.innerHTML = '<tr><td colspan="14" style="color:#64748b;">No matching invoice records found.</td></tr>';
                    return;
                }}

                tbody.innerHTML = rows.map(r => {{
                    const dispNum = r.display_num ?? r.id;
                    const isSelected = selectedInvoiceId === r.id;
                    const currOpts = ['GBP', 'USD', 'EUR'].map(c =>
                        `<option value="${{c}}" ${{r.currency === c ? 'selected' : ''}}>${{c}}</option>`
                    ).join('');
                    const isPaid = (r.payment_status || 'UNPAID').toUpperCase() === 'PAID';
                    const payBtn = isPaid
                        ? `<button class="btn-paid" onclick="event.stopPropagation(); setPaymentStatus(${{r.id}}, 'UNPAID')">PAID ✓</button>`
                        : `<button class="btn-unpaid" onclick="event.stopPropagation(); setPaymentStatus(${{r.id}}, 'PAID')">UNPAID •</button>`;
                    const linkHtml = r.payment_link
                        ? `<a class="pay-link" href="${{r.payment_link}}" target="_blank" onclick="event.stopPropagation()">Open Link</a>`
                        : `<button class="btn-secondary" onclick="event.stopPropagation(); generatePaymentLink(${{r.id}})">+ Link</button>`;

                    return `
                        <tr class="invoice-row ${{isSelected ? 'selected-row' : ''}}" onclick="selectInvoiceRow(${{r.id}})">
                            <td>#${{dispNum}}</td>
                            <td><b>${{r.vendor_name}}</b><br><span style="color:#64748b; font-size:11px;">${{r.filename}}</span></td>
                            <td>${{r.invoice_number}}</td>
                            <td onclick="event.stopPropagation()">
                                <select class="curr-select" onchange="changeRowCurrency(${{r.id}}, this.value)">
                                    ${{currOpts}}
                                </select>
                            </td>
                            <td>${{r.currency}} ${{Number(r.subtotal || 0).toFixed(2)}}</td>
                            <td>${{r.currency}} ${{Number(r.vat_amount || 0).toFixed(2)}}</td>
                            <td style="color:#38bdf8;"><b>£${{Number(r.gbp_vat_amount || 0).toFixed(2)}}</b></td>
                            <td><b>${{r.currency}} ${{Number(r.total || 0).toFixed(2)}}</b></td>
                            <td style="color:#f59e0b;">${{r.currency}} ${{Number(r.est_stripe_fee || 0).toFixed(2)}}</td>
                            <td style="color:#10b981;"><b>${{r.currency}} ${{Number(r.net_payout || 0).toFixed(2)}}</b></td>
                            <td>${{payBtn}}</td>
                            <td>${{linkHtml}}</td>
                            <td>${{renderStatusBadges(r)}}</td>
                            <td><button class="btn-danger" onclick="event.stopPropagation(); deleteInvoice(${{r.id}})">Delete</button></td>
                        </tr>
                    `;
                }}).join('');
            }}

            async function loadLedger() {{
                const search = document.getElementById('searchInput').value;
                const status = document.getElementById('statusFilter').value;
                const currency = document.getElementById('currencyFilter').value;
                const pay = document.getElementById('paymentFilter').value;
                const tbody = document.getElementById('ledgerBody');

                try {{
                    const res = await fetch(
                        `/api/invoices?search=${{encodeURIComponent(search)}}&status=${{encodeURIComponent(status)}}&currency=${{encodeURIComponent(currency)}}&payment_status=${{encodeURIComponent(pay)}}`,
                        {{ headers: {{ 'X-API-Key': DEFAULT_API_KEY }} }}
                    );
                    const payload = await res.json();
                    const rows = Array.isArray(payload) ? payload : (payload.invoices || []);
                    currentFilteredInvoices = rows;
                    if (selectedInvoiceId !== null && !rows.some(r => r.id === selectedInvoiceId)) {{
                        selectedInvoiceId = null;
                    }}
                    renderTopSummary(currentFilteredInvoices);
                    renderLedgerRows(currentFilteredInvoices);
                }} catch (err) {{
                    tbody.innerHTML = '<tr><td colspan="14" style="color:#f87171;">Failed to load ledger.</td></tr>';
                }}
            }}

            async function deleteInvoice(id) {{
                const res = await fetch(`/api/invoices/${{id}}`, {{
                    method: 'DELETE',
                    headers: {{ 'X-API-Key': DEFAULT_API_KEY }}
                }});
                if (res.ok) {{
                    if (selectedInvoiceId === id) selectedInvoiceId = null;
                    loadLedger();
                }}
            }}

            async function processFiles(files) {{
                const selectedCurr = document.getElementById('uploadCurrency').value;
                const forceCurr = selectedCurr === 'AUTO' ? null : selectedCurr;

                for (const file of files) {{
                    const card = document.createElement('div');
                    card.className = 'result-card';
                    card.innerHTML = `<span style="color:#38bdf8;">Extracting & validating <b>${{file.name}}</b> via qwen2.5-coder:14b...</span>`;
                    resultsContainer.prepend(card);

                    const formData = new FormData();
                    formData.append('file', file);
                    const url = forceCurr ? `/api/extract-invoice?currency=${{encodeURIComponent(forceCurr)}}` : '/api/extract-invoice';

                    try {{
                        const res = await fetch(url, {{
                            method: 'POST',
                            headers: {{ 'X-API-Key': DEFAULT_API_KEY }},
                            body: formData
                        }});
                        const data = await res.json();
                        if (!res.ok || data.detail) {{
                            card.innerHTML = `<span style="color:#f87171;">[${{file.name}}] Error: ${{data.detail || 'Extraction failed'}}</span>`;
                            continue;
                        }}

                        card.innerHTML = `
                            <div style="display:flex; justify-content:space-between; margin-bottom:8px; gap:10px;">
                                <strong>${{data.vendor_name}} <span style="color:#64748b; font-weight:normal;">(${{data.filename}})</span></strong>
                                <div>${{renderStatusBadges(data)}} <span style="color:#94a3b8; font-size:12px;">${{data.discrepancy_note}}</span></div>
                            </div>
                            <div style="display:grid; grid-template-columns: repeat(5, 1fr); gap:10px; font-size:13px; background:#131c2e; padding:10px; border-radius:8px;">
                                <div><span style="color:#94a3b8;">Net:</span> <b>${{data.currency}} ${{Number(data.subtotal || 0).toFixed(2)}}</b></div>
                                <div><span style="color:#94a3b8;">VAT:</span> <b>${{data.currency}} ${{Number(data.vat_amount || 0).toFixed(2)}}</b></div>
                                <div><span style="color:#94a3b8;">VAT (GBP):</span> <b style="color:#38bdf8;">£${{Number(data.gbp_vat_amount || 0).toFixed(2)}}</b></div>
                                <div><span style="color:#94a3b8;">Gross:</span> <b>${{data.currency}} ${{Number(data.total || 0).toFixed(2)}}</b></div>
                                <div><span style="color:#94a3b8;">Est. Net:</span> <b style="color:#10b981;">${{data.currency}} ${{Number(data.net_payout || 0).toFixed(2)}}</b></div>
                            </div>
                        `;
                        loadLedger();
                    }} catch (err) {{
                        card.innerHTML = `<span style="color:#f87171;">Connection error while processing ${{file.name}}.</span>`;
                    }}
                }}
            }}

            loadFxRates();
            loadMerchantSettings();
            loadLedger();
        </script>
    </body>
    </html>
    """


@app.get("/api/fx-rates", dependencies=[Depends(verify_api_key)])
def api_get_fx_rates():
    return get_fx_rates_map()


@app.post("/api/fx-rates/refresh", dependencies=[Depends(verify_api_key)])
def api_refresh_fx_rates():
    try:
        return refresh_live_fx_rates()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Live FX refresh failed: {e}")


@app.put("/api/fx-rates", dependencies=[Depends(verify_api_key)])
def api_set_fx_rates(req: FxRatesUpdateRequest):
    return set_fx_rates_map(usd_to_gbp=req.usd_to_gbp, eur_to_gbp=req.eur_to_gbp, source="manual")


@app.post("/api/sample-invoice", dependencies=[Depends(verify_api_key)])
def load_sample_invoice():
    sample = {
        "invoice_number": "INV-8841",
        "vendor": "Northgate Cloud Ltd",
        "currency": "GBP",
        "net_amount": 150.00,
        "vat_amount": 30.00,
        "vat_gbp": 30.00,
        "gross_amount": 180.00,
        "est_stripe_fee": 2.90,
        "est_net_payout": 177.10,
        "status": "UNPAID",
        "audit_note": "Matches Net (150.00) + VAT (30.00) = Gross (180.00).",
    }
    now = datetime.now().isoformat(timespec="seconds")
    fx_map = get_fx_rates_map()

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM invoices WHERE invoice_number = ? AND vendor_name = ? ORDER BY id ASC LIMIT 1",
            (sample["invoice_number"], sample["vendor"]),
        ).fetchone()

        if existing:
            inv_id = int(existing["id"])
            conn.execute(
                """UPDATE invoices
                   SET currency = ?, subtotal = ?, vat_amount = ?, total = ?,
                       est_stripe_fee = ?, net_payout = ?, payment_status = ?,
                       math_valid = 1, discrepancy_note = ?, line_items_json = ?
                   WHERE id = ?""",
                (
                    sample["currency"],
                    sample["net_amount"],
                    sample["vat_amount"],
                    sample["gross_amount"],
                    sample["est_stripe_fee"],
                    sample["est_net_payout"],
                    sample["status"],
                    sample["audit_note"],
                    json.dumps([{
                        "description": "Cloud Infrastructure & Hosting",
                        "quantity": 1.0,
                        "unit_price": sample["net_amount"],
                        "amount": sample["net_amount"],
                    }]),
                    inv_id,
                ),
            )
        else:
            cur = conn.execute(
                """INSERT INTO invoices (
                    processed_at, filename, vendor_name, vat_number, invoice_number,
                    invoice_date, currency, subtotal, vat_amount, total,
                    math_valid, is_duplicate, duplicate_of_id, discrepancy_note, line_items_json,
                    payment_status, payment_link, payment_provider, est_stripe_fee, net_payout, paid_at,
                    vat_rate_label, gbp_vat_amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now,
                    "sample_northgate_INV-8841.pdf",
                    sample["vendor"],
                    "Not Found",
                    sample["invoice_number"],
                    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    sample["currency"],
                    sample["net_amount"],
                    sample["vat_amount"],
                    sample["gross_amount"],
                    1,
                    0,
                    None,
                    sample["audit_note"],
                    json.dumps([{
                        "description": "Cloud Infrastructure & Hosting",
                        "quantity": 1.0,
                        "unit_price": sample["net_amount"],
                        "amount": sample["net_amount"],
                    }]),
                    sample["status"],
                    "",
                    "stripe",
                    sample["est_stripe_fee"],
                    sample["est_net_payout"],
                    "",
                    "Standard (20%)",
                    sample["vat_gbp"],
                ),
            )
            inv_id = int(cur.lastrowid)

        sync_invoice_tax_fields(conn, inv_id, fx_map)
        conn.commit()

    rescan_all_duplicates()
    with get_conn() as conn:
        disp_map = _build_chronological_display_map(conn)
        saved = dict(conn.execute("SELECT * FROM invoices WHERE id = ?", (inv_id,)).fetchone())

    return {
        "ok": True,
        "invoice": {
            **sample,
            "id": inv_id,
            "display_num": disp_map.get(inv_id),
            "vendor_name": saved["vendor_name"],
            "subtotal": float(saved["subtotal"]),
            "total": float(saved["total"]),
            "gbp_vat_amount": float(saved["gbp_vat_amount"]),
            "payment_status": saved["payment_status"],
            "discrepancy_note": saved["discrepancy_note"],
        },
    }

@app.post("/api/invoices/scan-duplicates", dependencies=[Depends(verify_api_key)])
def api_scan_duplicates():
    return rescan_all_duplicates()


@app.post("/api/extract-invoice", dependencies=[Depends(verify_api_key)])
async def api_extract_invoice(
    file: UploadFile = File(...),
    currency: Optional[str] = Query(default=None),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    file_path = os.path.join(UPLOAD_DIR, file.filename)
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        return extract_and_validate_invoice(file_path, file.filename, force_currency=currency)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/invoices/{invoice_id}/currency", dependencies=[Depends(verify_api_key)])
def api_update_invoice_currency(invoice_id: int, req: CurrencyUpdateRequest):
    updated = update_invoice_currency(
        invoice_id=invoice_id,
        new_currency=req.currency,
        convert_amounts=req.convert_amounts,
        fx_rate=req.fx_rate,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Invoice record not found.")
    return updated


@app.get("/api/invoices", dependencies=[Depends(verify_api_key)])
def api_list_invoices(
    search: str = Query(default=""),
    status: str = Query(default="ALL"),
    currency: str = Query(default="ALL"),
    payment_status: str = Query(default="ALL"),
):
    records = fetch_invoices(search=search, status=status, currency=currency, payment_status=payment_status)
    return {
        "invoices": records,
        "summary": compute_reconciliation_summary(records),
    }


@app.delete("/api/invoices/{invoice_id}", dependencies=[Depends(verify_api_key)])
def api_delete_invoice(invoice_id: int):
    deleted = delete_invoice_record(invoice_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Invoice record not found.")
    return {"deleted": True, "id": invoice_id}


@app.get("/api/export-csv", dependencies=[Depends(verify_api_key)])
def export_csv(
    format: str = Query(default="standard"),
    search: str = Query(default=""),
    status: str = Query(default="ALL"),
    currency: str = Query(default="ALL"),
    payment_status: str = Query(default="ALL"),
):
    records = fetch_invoices(search=search, status=status, currency=currency, payment_status=payment_status)
    if not records:
        raise HTTPException(status_code=404, detail="No invoices available for export.")
    csv_content, filename = build_accounting_csv(records, format)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    uvicorn.run("backend:app", host="0.0.0.0", port=8000, reload=True)



