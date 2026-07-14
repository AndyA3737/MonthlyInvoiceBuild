#!/usr/bin/env python3
"""SalonIQ Monthly Invoice Builder — fetches Stripe invoices and exports to QuickBooks Online."""

import os
import base64
import json
import secrets
import time
from calendar import monthrange
from datetime import date, datetime
from functools import wraps
from urllib.parse import urlencode, quote

from flask import Flask, jsonify, request, Response, send_from_directory, redirect
import requests

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_USER = os.environ.get('DASHBOARD_USER', 'admin').strip()
DASHBOARD_PASS = os.environ.get('DASHBOARD_PASS', 'changeme').strip()

# ── QuickBooks Online OAuth config ───────────────────────────────────────────
# Set these as environment variables on Railway:
#   QB_CLIENT_ID      — from developer.intuit.com app settings
#   QB_CLIENT_SECRET  — from developer.intuit.com app settings
#   QB_REDIRECT_URI   — https://<your-app>/auth/quickbooks/callback (must match the app's Redirect URI exactly)
#   QB_ENVIRONMENT    — "sandbox" or "production" (default sandbox)

QB_CLIENT_ID     = os.environ.get('QB_CLIENT_ID', '')
QB_CLIENT_SECRET = os.environ.get('QB_CLIENT_SECRET', '')
QB_REDIRECT_URI  = os.environ.get('QB_REDIRECT_URI', 'http://localhost:5000/auth/quickbooks/callback')
QB_ENVIRONMENT   = os.environ.get('QB_ENVIRONMENT', 'sandbox').strip().lower()

# Set QB_AMOUNTS_IN_PENCE=false if your SalonIQ data already stores amounts in pounds
QB_AMOUNTS_IN_PENCE = os.environ.get('QB_AMOUNTS_IN_PENCE', 'true').lower() != 'false'

# Names of the QuickBooks Tax Codes (Settings > Taxes in QBO) to apply to invoice lines.
# Looked up by name at export time and resolved to a TaxCodeRef — override if your
# company's codes are named differently than QBO's UK defaults.
QB_VAT_TAX_CODE    = os.environ.get('QB_VAT_TAX_CODE', '20.0% S')
QB_NO_VAT_TAX_CODE = os.environ.get('QB_NO_VAT_TAX_CODE', 'No VAT')

QB_AUTH_URL   = "https://appcenter.intuit.com/connect/oauth2"
QB_TOKEN_URL  = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QB_REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

QB_API_BASE = (
    "https://quickbooks.api.intuit.com/v3/company"
    if QB_ENVIRONMENT == 'production' else
    "https://sandbox-quickbooks.api.intuit.com/v3/company"
)

# Without a minorversion, QBO serves its oldest baseline schema, which is missing
# fields like Customer.CurrencyRef. Bump this if a future API feature needs a newer one.
QB_MINOR_VERSION = "75"

# Token persistence — survives restarts when a Railway Volume is mounted at /data
# Set QB_TOKEN_FILE env var to override (default: /data/qb_tokens.json)
TOKEN_FILE = os.environ.get('QB_TOKEN_FILE', '/data/qb_tokens.json')

_qb_state  = None   # CSRF token for OAuth flow
_qb_tokens = {}     # access_token, refresh_token, expires_at, realm_id, company_name


def _load_tokens():
    """Read persisted tokens from disk on startup."""
    try:
        with open(TOKEN_FILE) as f:
            _qb_tokens.update(json.load(f))
        app.logger.info("Loaded QuickBooks tokens from %s", TOKEN_FILE)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass  # No saved tokens yet — user will connect via UI


def _save_tokens():
    """Write current tokens to disk so they survive restarts."""
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, 'w') as f:
            json.dump(dict(_qb_tokens), f)
    except OSError as e:
        app.logger.warning("Could not save QuickBooks tokens to %s: %s", TOKEN_FILE, e)


_load_tokens()


# ── Basic auth ───────────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if auth:
            if auth.username == DASHBOARD_USER and auth.password == DASHBOARD_PASS:
                return f(*args, **kwargs)
        else:
            raw = request.headers.get('Authorization') or request.environ.get('HTTP_AUTHORIZATION', '')
            if raw.startswith('Basic '):
                try:
                    creds = base64.b64decode(raw[6:]).decode('utf-8')
                    user, pwd = creds.split(':', 1)
                    if user == DASHBOARD_USER and pwd == DASHBOARD_PASS:
                        return f(*args, **kwargs)
                except Exception:
                    pass
        return Response(
            'Authentication required.',
            401,
            {'WWW-Authenticate': 'Basic realm="SalonIQ Invoice Builder"'},
        )
    return decorated


# ── Invoice source configuration ─────────────────────────────────────────────
# Each source defines the SalonIQ report and the QuickBooks Item names to use.
# Item names must exist as Items in the QuickBooks company — they're looked up
# by name and resolved to QBO Item IDs at export time (see _fetch_qb_items()).
# vat_inclusive: True  = amount includes VAT (divide by 1.2 for net)
#                False = amount is net

INVOICE_SOURCES = {
    "stripe": {
        "label":          "Stripe Invoices",
        "report":         "XXX_Export_Admin_TUBR_StripeInvoices",
        "item_code":      "IQPay",
        "amount_field":   "TotalBill",
        "item_terminal":  "IQPayTerminal",
        "vat_inclusive":  True,
        "terminal_vat_inclusive": False,
        "invoice_date":   "last",
    },
    "subscription": {
        "label":              "Subscription Invoices",
        "report":             "XXX_Export_Admin_TUBR_SubscriptionInvoices",
        "item_code":          "Monthly",
        "amount_field":       "MonthlyAmount",
        "item_terminal":      None,
        "vat_inclusive":      False,
        "terminal_vat_inclusive": False,
        "invoice_date":       "first",
        "item_sms":           "SMS",
        "sms_qty_field":      "SMSCredits",
        "sms_price_field":    "SmsUnitPrice",
        "item_salonspy":      "Salon Spy",
        "salonspy_field":     "SalonSPYFee",
        "item_postcode":      "PostCode",
        "postcode_qty_field": "PostCodeCredits",
        "postcode_price_field": "PricePerPostcodeLookup",
        "item_hardware":      "Hardware Maintenance",
        "hardware_field":     "MonthlyHardwareAmount",
        "item_salonapp":      "Salon Booking App - Monthly Subscription",
        "salonapp_field":     "MonthlySalonAppAmount",
        "item_twowaysms":             "Two Way SMS",
        "twowaysms_fixed_field":      "SMSTextBackMonthlyAmount",
        "twowaysms_qty_field":        "IncomingMessagecount",
        "twowaysms_price_field":      "IncomingSMSCost",
    },
}

# ── SalonIQ LIVE API ─────────────────────────────────────────────────────────

LIVE_SERVER = {
    "base":     "https://apihub.saloniq.co.uk/api/GetAPIReport",
    "token":    "517a41d9-48e3-4af7-ae6c-0e30688f9325",
    "tenant":   "1E7D7624-FEB7-4950-A6BE-5FBB1498EE39",
    "date_fmt": "%d/%m/%Y",
}

API_COMMON = dict(Salonid="", UserID="", data1="", data2="", data3="", data4="")


def fetch(report_name, sd="", ed=""):
    """POST a report request to SalonIQ LIVE and return the data array."""
    srv = LIVE_SERVER
    app.logger.info("FETCH %s [%s → %s]", report_name, sd, ed)
    t0 = time.time()
    params = {
        **API_COMMON,
        "TokenID":    srv["token"],
        "TenantID":   srv["tenant"].upper(),
        "ReportName": report_name,
        "startdate":  sd,
        "enddate":    ed,
    }
    r = requests.post(
        srv["base"], params=params,
        headers={"Content-Length": "0"}, timeout=180,
    )
    r.raise_for_status()
    payload = r.json()
    result = (payload.get("Data") or {}).get("Array") or []
    app.logger.info("FETCH DONE %s rows=%d elapsed=%.1fs",
                    report_name, len(result), time.time() - t0)
    return result


def month_date_range(month, year):
    """Return (start_str, end_str) for the full calendar month in LIVE date format."""
    _, days = monthrange(year, month)
    fmt = LIVE_SERVER["date_fmt"]
    return date(year, month, 1).strftime(fmt), date(year, month, days).strftime(fmt)


# ── QuickBooks helpers ───────────────────────────────────────────────────────

def _qb_refresh_if_needed():
    if time.time() < _qb_tokens.get('expires_at', 0) - 60:
        return
    rt = _qb_tokens.get('refresh_token', '')
    if not rt:
        raise RuntimeError("No QuickBooks refresh token — please reconnect.")
    r = requests.post(
        QB_TOKEN_URL,
        auth=(QB_CLIENT_ID, QB_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        data={"grant_type": "refresh_token", "refresh_token": rt},
    )
    r.raise_for_status()
    t = r.json()
    _qb_tokens['access_token']  = t['access_token']
    _qb_tokens['refresh_token'] = t.get('refresh_token', rt)
    _qb_tokens['expires_at']    = time.time() + t.get('expires_in', 3600)
    _save_tokens()


def _qb_headers():
    _qb_refresh_if_needed()
    return {
        "Authorization": f"Bearer {_qb_tokens['access_token']}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def _qb_realm_url(*parts):
    realm_id = _qb_tokens.get('realm_id', '')
    return "/".join([f"{QB_API_BASE}/{realm_id}", *parts])


def _qb_query_all(entity, columns):
    """Run a paged SELECT against the QBO query endpoint and return all rows."""
    results, start = [], 1
    while True:
        q = f"SELECT {columns} FROM {entity} STARTPOSITION {start} MAXRESULTS 1000"
        r = requests.get(_qb_realm_url("query"), headers=_qb_headers(),
                          params={"query": q, "minorversion": QB_MINOR_VERSION})
        if not r.ok:
            app.logger.warning("QuickBooks query failed [%s]: %s | response: %s", r.status_code, q, r.text[:1000])
        r.raise_for_status()
        batch = (r.json().get("QueryResponse") or {}).get(entity, [])
        results.extend(batch)
        if len(batch) < 1000:
            break
        start += 1000
    return results


def _fetch_qb_items():
    """Return {item_name: item_id} for every Item in the QuickBooks company."""
    items = _qb_query_all("Item", "Id, Name")
    return {i["Name"]: i["Id"] for i in items}


def _fetch_qb_tax_codes():
    """Return {tax_code_name: tax_code_id} for every TaxCode in the QuickBooks company."""
    codes = _qb_query_all("TaxCode", "Id, Name")
    return {c["Name"]: c["Id"] for c in codes}


def _fetch_qb_customer_currencies():
    """Return {customer_id: currency_code} for every Customer in the QuickBooks company.

    CurrencyRef is only a queryable field when multi-currency is enabled on the
    company — if it's not, QBO returns a 400 rather than just empty values.
    Treat that as "everyone's on the home currency" instead of failing the export.
    """
    try:
        customers = _qb_query_all("Customer", "*")
    except requests.HTTPError:
        app.logger.info("QuickBooks Customer.CurrencyRef query failed — assuming multi-currency is off")
        return {}
    out = {}
    for c in customers:
        cur = (c.get("CurrencyRef") or {}).get("value")
        if cur:
            out[c["Id"]] = cur
    return out


def _parse_date(val):
    """Convert various date strings to YYYY-MM-DD for QuickBooks."""
    if not val:
        return ""
    s = str(val).strip().split(' ')[0].split('T')[0]
    for fmt in ('%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return s


def _parse_amount(val):
    """Parse amount string to float.

    If QB_AMOUNTS_IN_PENCE is true (default), whole-number values >= 100
    are assumed to be pence and are divided by 100 to convert to pounds.
    Set env var QB_AMOUNTS_IN_PENCE=false if amounts are already in pounds.
    """
    try:
        amount = float(str(val).replace(',', ''))
    except (ValueError, TypeError):
        return 0.0
    if QB_AMOUNTS_IN_PENCE and amount == int(amount) and amount >= 100:
        return amount / 100
    return amount


_MONTH_NAMES = ['January','February','March','April','May','June',
                'July','August','September','October','November','December']


def map_to_quickbooks_invoice(row, source_cfg=None, invoice_month=0, invoice_year=0,
                               contact_currency=None, item_ids=None, tax_code_ids=None):
    """Map a SalonIQ invoice row to a QuickBooks Online Invoice dict using the source config.

    Raises ValueError (with a user-facing message) if the row can't be mapped —
    e.g. no QuickBooksClientId, a required Item hasn't been created in QuickBooks yet,
    or the configured Tax Code name doesn't exist in the company.
    """
    if source_cfg is None:
        source_cfg = INVOICE_SOURCES['stripe']
    item_ids     = item_ids or {}
    tax_code_ids = tax_code_ids or {}

    customer_id = str(row.get('QuickBooksClientId') or row.get('QUICKBOOKSCLIENTID') or '').strip()
    if not customer_id:
        raise ValueError("No QuickBooksClientId on this row — salon isn't linked to a QuickBooks customer in SalonIQ.")

    item_code          = source_cfg['item_code']
    amount_field       = source_cfg.get('amount_field', 'TotalBill')
    item_terminal      = source_cfg.get('item_terminal')
    vat_inclusive      = source_cfg.get('vat_inclusive', True)
    term_vat_inclusive = source_cfg.get('terminal_vat_inclusive', False)

    # Date: driven by source config when month/year are known
    date_rule = source_cfg.get('invoice_date', '')
    if invoice_month and invoice_year and date_rule == 'first':
        inv_date = date(invoice_year, invoice_month, 1).strftime('%Y-%m-%d')
    elif invoice_month and invoice_year and date_rule == 'last':
        _, last_day = monthrange(invoice_year, invoice_month)
        inv_date = date(invoice_year, invoice_month, last_day).strftime('%Y-%m-%d')
    else:
        inv_date = _parse_date(row.get('InvoiceDate') or row.get('INVOICEDATE') or '')

    currency = ((contact_currency or {}).get(customer_id) or 'GBP').strip().upper() or 'GBP'
    is_gbp   = currency == 'GBP'

    # Main bill — use configured amount field, apply VAT based on source and currency
    try:
        raw    = row.get(amount_field) or row.get(amount_field.upper()) or '0'
        gross  = float(str(raw).replace(',', ''))
        apply_vat = vat_inclusive and is_gbp
        amount = round(gross / 1.2, 2) if apply_vat else round(gross, 2)
    except (ValueError, TypeError):
        amount = 0.0

    # Terminal bill (if this source supports it)
    try:
        terminal_gross  = float(str(row.get('TerminalBill') or row.get('TERMINALBILL') or '0').replace(',', ''))
        apply_term_vat  = term_vat_inclusive and is_gbp
        terminal_amount = round(terminal_gross / 1.2, 2) if apply_term_vat else round(terminal_gross, 2)
    except (ValueError, TypeError):
        terminal_amount = 0.0

    # AccountCode (e.g. ABS003) — recorded on the invoice for traceability, but not
    # used as the invoice number so QuickBooks assigns its own sequential numbering
    reference = str(row.get('AccountCode') or row.get('ACCOUNTCODE') or '')

    # QBO requires a TaxCodeRef on every line once the company has tax tracking on.
    # Standard-rate lines are stored net (see `amount` above) and QBO adds VAT on
    # top when saved, matching the old Xero "VAT-exclusive, provider adds VAT" setup.
    tax_code_name = QB_VAT_TAX_CODE if is_gbp else QB_NO_VAT_TAX_CODE
    tax_code_id   = tax_code_ids.get(tax_code_name)
    if not tax_code_id:
        available = ', '.join(sorted(tax_code_ids)) or '(none returned by QuickBooks)'
        raise ValueError(f"QuickBooks Tax Code '{tax_code_name}' not found. Available: {available}")

    def qb_line(code, unit_amount, description=None):
        item_id = item_ids.get(code)
        if not item_id:
            raise ValueError(f"QuickBooks Item '{code}' not found — create it in QuickBooks first.")
        line = {
            "Amount": round(unit_amount, 2),
            "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": {
                "ItemRef": {"value": item_id, "name": code},
                "Qty": 1.0,
                "UnitPrice": unit_amount,
                "TaxCodeRef": {"value": tax_code_id},
            },
        }
        if description:
            line["Description"] = description
        return line

    line_items = [qb_line(item_code, amount)]
    if item_terminal and terminal_amount > 0:
        line_items.append(qb_line(item_terminal, terminal_amount))

    item_sms = source_cfg.get('item_sms')
    if item_sms:
        try:
            sms_qty   = float(str(row.get(source_cfg.get('sms_qty_field', '')) or '0').replace(',', ''))
            sms_price = float(str(row.get(source_cfg.get('sms_price_field', '')) or '0').replace(',', ''))
            sms_amount = round(sms_qty * sms_price, 2)
        except (ValueError, TypeError):
            sms_qty, sms_price, sms_amount = 0.0, 0.0, 0.0
        if sms_amount > 0:
            prev_month = _MONTH_NAMES[(invoice_month - 2) % 12] if invoice_month else ''
            sms_desc = f"SMS Messages (Sent in {prev_month})" if prev_month else "SMS Messages"
            sms_desc += f" — {int(sms_qty)} x £{sms_price:.4f}"
            line_items.append(qb_line(item_sms, sms_amount, sms_desc))

    item_salonspy = source_cfg.get('item_salonspy')
    if item_salonspy:
        try:
            salonspy_amount = round(float(str(row.get(source_cfg.get('salonspy_field', '')) or '0').replace(',', '')), 2)
        except (ValueError, TypeError):
            salonspy_amount = 0.0
        if salonspy_amount > 0:
            line_items.append(qb_line(item_salonspy, salonspy_amount))

    item_postcode = source_cfg.get('item_postcode')
    if item_postcode:
        try:
            pc_qty   = float(str(row.get(source_cfg.get('postcode_qty_field', '')) or '0').replace(',', ''))
            pc_price = float(str(row.get(source_cfg.get('postcode_price_field', '')) or '0').replace(',', ''))
            pc_amount = round(pc_qty * pc_price, 2)
        except (ValueError, TypeError):
            pc_qty, pc_price, pc_amount = 0.0, 0.0, 0.0
        if pc_amount > 0:
            pc_desc = f"Postcode Lookups — {int(pc_qty)} x £{pc_price:.4f}"
            line_items.append(qb_line(item_postcode, pc_amount, pc_desc))

    item_hardware = source_cfg.get('item_hardware')
    if item_hardware:
        try:
            hardware_amount = round(float(str(row.get(source_cfg.get('hardware_field', '')) or '0').replace(',', '')), 2)
        except (ValueError, TypeError):
            hardware_amount = 0.0
        if hardware_amount > 0:
            line_items.append(qb_line(item_hardware, hardware_amount))

    item_salonapp = source_cfg.get('item_salonapp')
    if item_salonapp:
        try:
            salonapp_amount = round(float(str(row.get(source_cfg.get('salonapp_field', '')) or '0').replace(',', '')), 2)
        except (ValueError, TypeError):
            salonapp_amount = 0.0
        if salonapp_amount > 0:
            line_items.append(qb_line(item_salonapp, salonapp_amount))

    item_twowaysms = source_cfg.get('item_twowaysms')
    if item_twowaysms:
        try:
            fixed_amount = round(float(str(row.get(source_cfg.get('twowaysms_fixed_field', '')) or '0').replace(',', '')), 2)
        except (ValueError, TypeError):
            fixed_amount = 0.0
        if fixed_amount > 0:
            line_items.append(qb_line(item_twowaysms, fixed_amount, "Monthly SMS Text Back Charge"))
        else:
            try:
                tws_qty   = float(str(row.get(source_cfg.get('twowaysms_qty_field', '')) or '0').replace(',', ''))
                tws_price = float(str(row.get(source_cfg.get('twowaysms_price_field', '')) or '0').replace(',', ''))
                tws_amount = round(tws_qty * tws_price, 2)
            except (ValueError, TypeError):
                tws_qty, tws_price, tws_amount = 0.0, 0.0, 0.0
            if tws_amount > 0:
                prev_month = _MONTH_NAMES[(invoice_month - 2) % 12] if invoice_month else ''
                tws_desc = f"Incoming Messages in {prev_month} ({int(tws_qty)})" if prev_month else f"Incoming Messages ({int(tws_qty)})"
                tws_desc += f" x £{tws_price:.4f}"
                line_items.append(qb_line(item_twowaysms, tws_amount, tws_desc))

    qb_inv = {
        "CustomerRef": {"value": customer_id},
        "Line": line_items,
    }
    if not is_gbp:
        qb_inv["CurrencyRef"] = {"value": currency}
    if inv_date:
        qb_inv["TxnDate"] = inv_date
        qb_inv["DueDate"] = inv_date
    if reference:
        # Leave DocNumber unset so QuickBooks assigns its own next sequential invoice
        # number — the SalonIQ AccountCode goes in PrivateNote (internal-only) instead.
        qb_inv["PrivateNote"] = f"SalonIQ AccountCode: {reference}"

    return qb_inv


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
@require_auth
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/api/sources')
@require_auth
def api_sources():
    return jsonify([{"id": k, "label": v["label"]} for k, v in INVOICE_SOURCES.items()])


@app.route('/api/invoices')
@require_auth
def api_invoices():
    try:
        month = int(request.args.get('month', 0))
        year  = int(request.args.get('year', 0))
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    if not (1 <= month <= 12 and 2020 <= year <= 2099):
        return jsonify({"error": "Month must be 1–12, year must be 2020–2099"}), 400

    source_id  = request.args.get('source', 'stripe')
    source_cfg = INVOICE_SOURCES.get(source_id, INVOICE_SOURCES['stripe'])

    try:
        sd, ed = month_date_range(month, year)
        rows = fetch(source_cfg['report'], sd, ed)
        return jsonify({"data": rows, "count": len(rows)})
    except Exception as e:
        app.logger.exception("Error fetching invoices")
        return jsonify({"error": str(e)}), 500


@app.route('/auth/quickbooks')
@require_auth
def auth_quickbooks():
    global _qb_state
    if not QB_CLIENT_ID:
        return "QB_CLIENT_ID environment variable is not set.", 500
    _qb_state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id":     QB_CLIENT_ID,
        "redirect_uri":  QB_REDIRECT_URI,
        "scope":         "com.intuit.quickbooks.accounting",
        "state":         _qb_state,
    }
    return redirect(QB_AUTH_URL + "?" + urlencode(params))


@app.route('/auth/quickbooks/callback')
def auth_quickbooks_callback():
    # Not protected by require_auth — Intuit redirects here without credentials.
    # Security is provided by the state parameter check below.
    global _qb_state

    error = request.args.get('error')
    if error:
        return redirect('/?quickbooks_error=' + quote(error))

    code     = request.args.get('code', '')
    state    = request.args.get('state', '')
    realm_id = request.args.get('realmId', '')

    if not _qb_state or state != _qb_state:
        return redirect('/?quickbooks_error=state_mismatch')
    _qb_state = None

    if not realm_id:
        return redirect('/?quickbooks_error=missing_realm_id')

    try:
        r = requests.post(
            QB_TOKEN_URL,
            auth=(QB_CLIENT_ID, QB_CLIENT_SECRET),
            headers={"Accept": "application/json"},
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": QB_REDIRECT_URI,
            },
        )
        r.raise_for_status()
        tokens = r.json()
        _qb_tokens['access_token']  = tokens['access_token']
        _qb_tokens['refresh_token'] = tokens.get('refresh_token', '')
        _qb_tokens['expires_at']    = time.time() + tokens.get('expires_in', 3600)
        _qb_tokens['realm_id']      = realm_id

        try:
            cr = requests.get(_qb_realm_url("companyinfo", realm_id), headers=_qb_headers(),
                               params={"minorversion": QB_MINOR_VERSION})
            cr.raise_for_status()
            _qb_tokens['company_name'] = (cr.json().get("CompanyInfo") or {}).get("CompanyName", "")
        except Exception:
            app.logger.warning("Could not fetch QuickBooks company name")
            _qb_tokens['company_name'] = ''

        _save_tokens()

    except Exception as e:
        app.logger.exception("QuickBooks OAuth callback failed")
        return redirect('/?quickbooks_error=' + quote(str(e)))

    return redirect('/?quickbooks=connected')


@app.route('/api/quickbooks/debug/customer-currencies')
@require_auth
def quickbooks_debug_customer_currencies():
    """Read-only diagnostic: shows the raw customer currency query result/error without touching any invoices."""
    if not _qb_tokens.get('access_token'):
        return jsonify({"error": "Not connected to QuickBooks"}), 403
    try:
        customers = _qb_query_all("Customer", "*")
        summary = [{"Id": c.get("Id"), "DisplayName": c.get("DisplayName"), "CurrencyRef": c.get("CurrencyRef")}
                   for c in customers]
        return jsonify({"count": len(customers), "sample": summary[:10]})
    except requests.HTTPError as e:
        return jsonify({"error": str(e), "response_body": e.response.text[:1500] if e.response is not None else None}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/quickbooks/status')
@require_auth
def quickbooks_status():
    if not _qb_tokens.get('access_token'):
        return jsonify({"connected": False})
    # Proactively refresh if expired so the UI always shows connected
    if time.time() > _qb_tokens.get('expires_at', 0) - 60:
        try:
            _qb_refresh_if_needed()
        except Exception:
            return jsonify({"connected": False})
    return jsonify({
        "connected": True,
        "company":   _qb_tokens.get('company_name', 'Unknown Company'),
        "expired":   False,
    })


@app.route('/api/quickbooks/disconnect', methods=['POST'])
@require_auth
def quickbooks_disconnect():
    rt = _qb_tokens.get('refresh_token', '')
    if rt:
        try:
            requests.post(
                QB_REVOKE_URL,
                auth=(QB_CLIENT_ID, QB_CLIENT_SECRET),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={"token": rt},
            )
        except Exception:
            app.logger.warning("Could not revoke QuickBooks token", exc_info=True)
    _qb_tokens.clear()
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass
    return jsonify({"success": True})


@app.route('/api/quickbooks/export', methods=['POST'])
@require_auth
def quickbooks_export():
    if not _qb_tokens.get('access_token'):
        return jsonify({"error": "Not connected to QuickBooks — please click Connect QuickBooks"}), 403

    body = request.get_json(silent=True) or {}
    invoices   = body.get('invoices', [])
    source_id  = body.get('source', 'stripe')
    source_cfg = INVOICE_SOURCES.get(source_id, INVOICE_SOURCES['stripe'])
    invoice_month = int(body.get('month', 0) or 0)
    invoice_year  = int(body.get('year',  0) or 0)
    if not invoices:
        return jsonify({"error": "No invoices provided"}), 400

    try:
        item_ids         = _fetch_qb_items()
        contact_currency = _fetch_qb_customer_currencies()
        tax_code_ids     = _fetch_qb_tax_codes()

        qb_invs = []
        errors  = []
        for row in invoices:
            ref = str(row.get('AccountCode') or row.get('ACCOUNTCODE') or '?')
            try:
                qb_invs.append(map_to_quickbooks_invoice(
                    row, source_cfg, invoice_month=invoice_month, invoice_year=invoice_year,
                    contact_currency=contact_currency, item_ids=item_ids, tax_code_ids=tax_code_ids,
                ))
            except ValueError as e:
                errors.append(f"{ref}: {e}")

        created_total = 0
        BATCH_SIZE = 30  # QuickBooks Batch API limit

        for i in range(0, len(qb_invs), BATCH_SIZE):
            batch = qb_invs[i:i + BATCH_SIZE]
            batch_body = {
                "BatchItemRequest": [
                    {"bId": f"bid{i + j}", "operation": "create", "Invoice": inv}
                    for j, inv in enumerate(batch)
                ]
            }
            r = requests.post(_qb_realm_url("batch"), headers=_qb_headers(), json=batch_body,
                               params={"minorversion": QB_MINOR_VERSION})
            if r.ok:
                result = r.json()
                for item in result.get("BatchItemResponse", []):
                    fault = item.get("Fault")
                    if fault:
                        msgs = "; ".join(f"{e.get('Message','')} {e.get('Detail','')}".strip()
                                          for e in fault.get("Error", []))
                        errors.append(f"{item.get('bId','?')}: {msgs}")
                    else:
                        created_total += 1
            else:
                try:
                    err_body = r.json()
                    errors.append(err_body.get("Fault", {}).get("Error", [{}])[0].get("Message", r.text[:300]))
                except Exception:
                    errors.append(r.text[:300])

        if errors:
            app.logger.warning("QuickBooks export partial failure: created=%d errors=%s", created_total, errors)
            return jsonify({
                "success": False,
                "created": created_total,
                "errors":  errors,
            }), 207

        return jsonify({"success": True, "created": created_total})

    except Exception as e:
        app.logger.exception("QuickBooks export failed")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
