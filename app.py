#!/usr/bin/env python3
"""SalonIQ Monthly Invoice Builder — fetches Stripe invoices and exports to Xero."""

import os
import base64
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

# ── Xero OAuth config ────────────────────────────────────────────────────────
# Set these as environment variables on Heroku:
#   XERO_CLIENT_ID      — from developer.xero.com app settings
#   XERO_CLIENT_SECRET  — from developer.xero.com app settings
#   XERO_REDIRECT_URI   — https://<your-heroku-app>.herokuapp.com/auth/xero/callback
#   XERO_ACCOUNT_CODE   — Xero chart-of-accounts code for line items (default 200)

XERO_CLIENT_ID     = os.environ.get('XERO_CLIENT_ID', '')
XERO_CLIENT_SECRET = os.environ.get('XERO_CLIENT_SECRET', '')
XERO_REDIRECT_URI  = os.environ.get('XERO_REDIRECT_URI', 'http://localhost:5000/auth/xero/callback')
XERO_ACCOUNT_CODE  = os.environ.get('XERO_ACCOUNT_CODE', '200')

# Set XERO_AMOUNTS_IN_PENCE=false if your SalonIQ data already stores amounts in pounds
XERO_AMOUNTS_IN_PENCE = os.environ.get('XERO_AMOUNTS_IN_PENCE', 'true').lower() != 'false'

XERO_AUTH_URL        = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL       = "https://identity.xero.com/connect/token"
XERO_API_BASE        = "https://api.xero.com/api.xro/2.0"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"

# In-memory state — fine for a single-user admin tool on one Heroku dyno
_xero_state  = None   # CSRF token for OAuth flow
_xero_tokens = {}     # access_token, refresh_token, expires_at, tenant_id, tenant_name


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


# ── SalonIQ LIVE API ─────────────────────────────────────────────────────────

LIVE_SERVER = {
    "base":     "https://apihub.saloniq.co.uk/api/GetAPIReport",
    "token":    "517a41d9-48e3-4af7-ae6c-0e30688f9325",
    "tenant":   "1E7D7624-FEB7-4950-A6BE-5FBB1498EE39",
    "date_fmt": "%m/%d/%Y",
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


# ── Xero helpers ─────────────────────────────────────────────────────────────

def _xero_refresh_if_needed():
    if time.time() < _xero_tokens.get('expires_at', 0) - 60:
        return
    rt = _xero_tokens.get('refresh_token', '')
    if not rt:
        raise RuntimeError("No Xero refresh token — please reconnect.")
    r = requests.post(
        XERO_TOKEN_URL,
        auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": rt},
    )
    r.raise_for_status()
    t = r.json()
    _xero_tokens['access_token']  = t['access_token']
    _xero_tokens['refresh_token'] = t.get('refresh_token', rt)
    _xero_tokens['expires_at']    = time.time() + t.get('expires_in', 1800)


def _xero_headers():
    _xero_refresh_if_needed()
    return {
        "Authorization":  f"Bearer {_xero_tokens['access_token']}",
        "Xero-tenant-id": _xero_tokens.get('tenant_id', ''),
        "Content-Type":   "application/json",
    }


def _get(row, *keys, default=""):
    """Return the first non-empty value from row for any of the given keys."""
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return v
    return default


def _parse_date(val):
    """Convert various date strings to YYYY-MM-DD for Xero."""
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

    If XERO_AMOUNTS_IN_PENCE is true (default), whole-number values >= 100
    are assumed to be pence and are divided by 100 to convert to pounds.
    Set env var XERO_AMOUNTS_IN_PENCE=false if amounts are already in pounds.
    """
    try:
        amount = float(str(val).replace(',', ''))
    except (ValueError, TypeError):
        return 0.0
    if XERO_AMOUNTS_IN_PENCE and amount == int(amount) and amount >= 100:
        return amount / 100
    return amount


def map_to_xero_invoice(row):
    """Map a SalonIQ StripeInvoices row to a Xero invoice dict.

    Field names are guessed from common Stripe/SalonIQ export patterns.
    Adjust the candidate key lists below once you know your actual field names.
    """
    contact = str(_get(row,
        'SalonName', 'CustomerName', 'ClientName', 'CompanyName',
        'Name', 'BusinessName', 'AccountName', 'Customer',
        default="Unknown Customer",
    ))

    inv_date = _parse_date(_get(row,
        'InvoiceDate', 'Date', 'Created', 'DateCreated',
        'BillingDate', 'PeriodStart', 'IssueDate',
    ))
    due_date = _parse_date(_get(row,
        'DueDate', 'Due', 'PaymentDue', 'DueAt',
    )) or inv_date

    description = str(_get(row,
        'Description', 'ServiceDescription', 'LineItem', 'Product',
        'PlanName', 'SubscriptionPlan', 'Desc',
        default="SalonIQ Subscription",
    ))

    inv_number = str(_get(row,
        'InvoiceNumber', 'InvoiceID', 'StripeInvoiceID',
        'Reference', 'Ref', 'Number', 'ID',
        default="",
    ))

    amount = _parse_amount(_get(row,
        'AmountDue', 'Amount', 'Total', 'AmountPaid',
        'InvoiceAmount', 'Value', 'NetAmount', 'GrossAmount',
        default="0",
    ))

    xero_inv = {
        "Type": "ACCREC",
        "Contact": {"Name": contact},
        "LineItems": [{
            "Description": description,
            "Quantity":    1.0,
            "UnitAmount":  amount,
            "AccountCode": XERO_ACCOUNT_CODE,
            "TaxType":     "NONE",
        }],
        "Status": "DRAFT",
    }
    if inv_date:
        xero_inv["Date"] = inv_date
    if due_date:
        xero_inv["DueDate"] = due_date
    if inv_number:
        xero_inv["InvoiceNumber"] = inv_number

    return xero_inv


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
@require_auth
def index():
    return send_from_directory(BASE_DIR, 'index.html')


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

    try:
        sd, ed = month_date_range(month, year)
        rows = fetch("XXX_Export_Admin_TUBR_StripeInvoices", sd, ed)
        return jsonify({"data": rows, "count": len(rows)})
    except Exception as e:
        app.logger.exception("Error fetching invoices")
        return jsonify({"error": str(e)}), 500


@app.route('/auth/xero')
@require_auth
def auth_xero():
    global _xero_state
    if not XERO_CLIENT_ID:
        return "XERO_CLIENT_ID environment variable is not set.", 500
    _xero_state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id":     XERO_CLIENT_ID,
        "redirect_uri":  XERO_REDIRECT_URI,
        "scope": "openid offline_access accounting.transactions accounting.contacts",
        "state": _xero_state,
    }
    return redirect(XERO_AUTH_URL + "?" + urlencode(params))


@app.route('/auth/xero/callback')
def auth_xero_callback():
    # Not protected by require_auth — Xero redirects here without credentials.
    # Security is provided by the state parameter check below.
    global _xero_state

    error = request.args.get('error')
    if error:
        return redirect('/?xero_error=' + quote(error))

    code  = request.args.get('code', '')
    state = request.args.get('state', '')

    if not _xero_state or state != _xero_state:
        return redirect('/?xero_error=state_mismatch')
    _xero_state = None

    try:
        r = requests.post(
            XERO_TOKEN_URL,
            auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET),
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": XERO_REDIRECT_URI,
            },
        )
        r.raise_for_status()
        tokens = r.json()
        _xero_tokens['access_token']  = tokens['access_token']
        _xero_tokens['refresh_token'] = tokens.get('refresh_token', '')
        _xero_tokens['expires_at']    = time.time() + tokens.get('expires_in', 1800)

        tr = requests.get(
            XERO_CONNECTIONS_URL,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        tr.raise_for_status()
        connections = tr.json()
        if connections:
            _xero_tokens['tenant_id']   = connections[0]['tenantId']
            _xero_tokens['tenant_name'] = connections[0]['tenantName']

    except Exception as e:
        app.logger.exception("Xero OAuth callback failed")
        return redirect('/?xero_error=' + quote(str(e)))

    return redirect('/?xero=connected')


@app.route('/api/xero/status')
@require_auth
def xero_status():
    if not _xero_tokens.get('access_token'):
        return jsonify({"connected": False})
    expired = time.time() > _xero_tokens.get('expires_at', 0)
    return jsonify({
        "connected": True,
        "tenant":    _xero_tokens.get('tenant_name', 'Unknown Org'),
        "expired":   expired,
    })


@app.route('/api/xero/disconnect', methods=['POST'])
@require_auth
def xero_disconnect():
    _xero_tokens.clear()
    return jsonify({"success": True})


@app.route('/api/xero/export', methods=['POST'])
@require_auth
def xero_export():
    if not _xero_tokens.get('access_token'):
        return jsonify({"error": "Not connected to Xero. Please connect first."}), 401

    body = request.get_json(silent=True) or {}
    invoices = body.get('invoices', [])
    if not invoices:
        return jsonify({"error": "No invoices provided"}), 400

    try:
        xero_invs = [map_to_xero_invoice(row) for row in invoices]
        created_total = 0
        errors = []
        BATCH_SIZE = 50

        for i in range(0, len(xero_invs), BATCH_SIZE):
            batch = xero_invs[i:i + BATCH_SIZE]
            r = requests.post(
                f"{XERO_API_BASE}/Invoices",
                headers=_xero_headers(),
                json={"Invoices": batch},
            )
            if r.ok:
                created_total += len(r.json().get("Invoices", []))
            else:
                errors.append(r.text[:300])

        if errors:
            return jsonify({
                "success": False,
                "created": created_total,
                "errors":  errors,
            }), 207

        return jsonify({"success": True, "created": created_total})

    except Exception as e:
        app.logger.exception("Xero export failed")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
