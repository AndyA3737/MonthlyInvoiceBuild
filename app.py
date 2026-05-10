#!/usr/bin/env python3
"""SalonIQ Monthly Invoice Builder — fetches Stripe invoices and exports to Xero."""

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

# Token persistence — survives restarts when a Railway Volume is mounted at /data
# Set XERO_TOKEN_FILE env var to override (default: /data/xero_tokens.json)
TOKEN_FILE   = os.environ.get('XERO_TOKEN_FILE',   '/data/xero_tokens.json')
MAPPING_FILE = os.environ.get('XERO_MAPPING_FILE', '/data/salon_mapping.json')

_xero_state    = None   # CSRF token for OAuth flow
_xero_tokens   = {}     # access_token, refresh_token, expires_at, tenant_id, tenant_name
# ACCOUNTCODE -> {salonName, xeroContactId, xeroContactName}
_salon_mapping = {}


def _load_tokens():
    """Read persisted tokens from disk on startup."""
    try:
        with open(TOKEN_FILE) as f:
            _xero_tokens.update(json.load(f))
        app.logger.info("Loaded Xero tokens from %s", TOKEN_FILE)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass  # No saved tokens yet — user will connect via UI


def _save_tokens():
    """Write current tokens to disk so they survive restarts."""
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, 'w') as f:
            json.dump(dict(_xero_tokens), f)
    except OSError as e:
        app.logger.warning("Could not save Xero tokens to %s: %s", TOKEN_FILE, e)


_load_tokens()


def _load_mapping():
    try:
        with open(MAPPING_FILE) as f:
            _salon_mapping.update(json.load(f))
        app.logger.info("Loaded salon mapping from %s", MAPPING_FILE)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save_mapping_file():
    try:
        os.makedirs(os.path.dirname(MAPPING_FILE), exist_ok=True)
        with open(MAPPING_FILE, 'w') as f:
            json.dump(dict(_salon_mapping), f, indent=2)
    except OSError as e:
        app.logger.warning("Could not save mapping to %s: %s", MAPPING_FILE, e)


_load_mapping()


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
    _save_tokens()


def _xero_headers():
    _xero_refresh_if_needed()
    return {
        "Authorization":  f"Bearer {_xero_tokens['access_token']}",
        "Xero-tenant-id": _xero_tokens.get('tenant_id', ''),
        "Content-Type":   "application/json",
        "Accept":         "application/json",
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


def map_to_xero_invoice(row, item_code=None):
    """Map a SalonIQ StripeInvoices row to a Xero invoice dict."""
    item_code = item_code or XERO_ACCOUNT_CODE
    # Use salonid (UUID) as the stable mapping key; fall back to AccountCode
    salon_key = str(row.get('salonid') or row.get('SalonId') or
                    row.get('AccountCode') or row.get('ACCOUNTCODE') or '')
    mapped = _salon_mapping.get(salon_key)

    if mapped and mapped.get('xeroContactId'):
        contact = {"ContactID": mapped['xeroContactId']}
    else:
        name = (row.get('SalonName') or row.get('SALONNAME') or
                row.get('Tenantname') or row.get('TENANTNAME') or 'Unknown Customer')
        contact = {"Name": str(name)}

    # InvoiceDate format from LIVE API: "4/30/2026 12:00:00 AM"
    inv_date = _parse_date(row.get('InvoiceDate') or row.get('INVOICEDATE') or '')

    # Determine currency from mapping — drives VAT treatment
    currency = (_salon_mapping.get(salon_key) or {}).get('xeroContactCurrency', '') or 'GBP'
    is_gbp   = currency.upper() == 'GBP'

    # TotalBill: GBP = VAT-inclusive (divide by 1.2), non-GBP = no VAT (pass as-is)
    try:
        gross  = float(str(row.get('TotalBill') or row.get('TOTALBILL') or '0').replace(',', ''))
        amount = round(gross / 1.2, 2) if is_gbp else round(gross, 2)
    except (ValueError, TypeError):
        amount = 0.0

    # TerminalBill: GBP = VAT-exclusive (Xero adds VAT), non-GBP = no VAT
    try:
        terminal_amount = round(float(str(row.get('TerminalBill') or row.get('TERMINALBILL') or '0').replace(',', '')), 2)
    except (ValueError, TypeError):
        terminal_amount = 0.0

    # AccountCode (e.g. ABS003) used as the Xero invoice reference
    reference = str(row.get('AccountCode') or row.get('ACCOUNTCODE') or '')

    # Build line items — non-GBP invoices explicitly set TaxType NONE
    tax_override = {} if is_gbp else {"TaxType": "NONE"}
    line_items = [{"Quantity": 1.0, "UnitAmount": amount, "ItemCode": item_code, **tax_override}]
    if terminal_amount > 0:
        line_items.append({"Quantity": 1.0, "UnitAmount": terminal_amount, "ItemCode": "IQPayTerminal", **tax_override})

    xero_inv = {
        "Type":    "ACCREC",
        "Contact": contact,
        "LineItems": line_items,
        "Status":  "DRAFT",
    }
    if currency and currency.upper() != 'GBP':
        xero_inv["CurrencyCode"] = currency.upper()
    if inv_date:
        xero_inv["Date"]    = inv_date
        xero_inv["DueDate"] = inv_date
    if reference:
        xero_inv["Reference"] = reference

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

        # Register any new salons in the mapping (without Xero contact yet)
        changed = False
        for row in rows:
            key = str(row.get('salonid') or row.get('SalonId') or
                      row.get('AccountCode') or row.get('ACCOUNTCODE') or '')
            if key and key not in _salon_mapping:
                _salon_mapping[key] = {
                    "accountCode":     str(row.get('AccountCode') or row.get('ACCOUNTCODE') or ''),
                    "salonName":       (row.get('SalonName') or row.get('SALONNAME') or
                                        row.get('Tenantname') or row.get('TENANTNAME') or key),
                    "xeroContactId":   None,
                    "xeroContactName": None,
                }
                changed = True
        if changed:
            _save_mapping_file()

        return jsonify({"data": rows, "count": len(rows)})
    except Exception as e:
        app.logger.exception("Error fetching invoices")
        return jsonify({"error": str(e)}), 500


@app.route('/api/xero/contacts')
@require_auth
def xero_contacts():
    if not _xero_tokens.get('access_token'):
        return jsonify({"error": "Not connected to Xero — please click Connect Xero"}), 403
    try:
        all_contacts, page = [], 1
        while True:
            r = requests.get(
                f"{XERO_API_BASE}/Contacts",
                headers=_xero_headers(),
                params={"page": page, "includeArchived": "false"},
            )
            if not r.ok:
                return jsonify({"error": f"Xero {r.status_code}: {r.text[:400]}"}), 500
            if not r.text.strip():
                return jsonify({"error": f"Xero returned empty body (status {r.status_code})"}), 500
            try:
                payload = r.json()
            except ValueError:
                return jsonify({"error": f"Xero non-JSON (status {r.status_code}): {r.text[:400]}"}), 500
            batch = payload.get("Contacts", [])
            all_contacts.extend(
                {
                    "id":       c["ContactID"],
                    "name":     c["Name"],
                    "currency": c.get("DefaultCurrency", ""),
                }
                for c in batch if c.get("ContactStatus") == "ACTIVE"
            )
            if len(batch) < 100:
                break
            page += 1
        all_contacts.sort(key=lambda c: c["name"].lower())
        return jsonify({"contacts": all_contacts})
    except Exception as e:
        app.logger.exception("Failed to fetch Xero contacts")
        return jsonify({"error": str(e)}), 500


@app.route('/api/mapping', methods=['GET'])
@require_auth
def get_mapping():
    return jsonify(_salon_mapping)


@app.route('/api/mapping/register', methods=['POST'])
@require_auth
def register_salons():
    """Register salons sent from the frontend after invoice load."""
    salons = (request.get_json(silent=True) or {}).get('salons', [])
    changed = False
    for s in salons:
        key = s.get('salonId', '') or s.get('accountCode', '')
        if key and key not in _salon_mapping:
            _salon_mapping[key] = {
                'accountCode':     s.get('accountCode', ''),
                'salonName':       s.get('salonName', key),
                'xeroContactId':   None,
                'xeroContactName': None,
            }
            changed = True
    if changed:
        _save_mapping_file()
    app.logger.info("register_salons: %d total entries in mapping", len(_salon_mapping))
    return jsonify({'success': True, 'total': len(_salon_mapping)})


@app.route('/api/mapping', methods=['POST'])
@require_auth
def save_mapping():
    data = request.get_json(silent=True) or {}
    _salon_mapping.clear()
    _salon_mapping.update(data)
    _save_mapping_file()
    mapped = sum(1 for v in _salon_mapping.values() if v.get('xeroContactId'))
    return jsonify({"success": True, "total": len(_salon_mapping), "mapped": mapped})


@app.route('/api/mapping/clear', methods=['POST'])
@require_auth
def clear_mapping():
    _salon_mapping.clear()
    _save_mapping_file()
    return jsonify({"success": True})


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
        "scope": "openid offline_access accounting.invoices accounting.contacts",
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

        _save_tokens()

    except Exception as e:
        app.logger.exception("Xero OAuth callback failed")
        return redirect('/?xero_error=' + quote(str(e)))

    return redirect('/?xero=connected')


@app.route('/api/xero/status')
@require_auth
def xero_status():
    if not _xero_tokens.get('access_token'):
        return jsonify({"connected": False})
    # Proactively refresh if expired so the UI always shows connected
    if time.time() > _xero_tokens.get('expires_at', 0) - 60:
        try:
            _xero_refresh_if_needed()
        except Exception:
            return jsonify({"connected": False})
    return jsonify({
        "connected": True,
        "tenant":    _xero_tokens.get('tenant_name', 'Unknown Org'),
        "expired":   False,
    })


@app.route('/api/xero/disconnect', methods=['POST'])
@require_auth
def xero_disconnect():
    _xero_tokens.clear()
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass
    return jsonify({"success": True})


@app.route('/api/xero/export', methods=['POST'])
@require_auth
def xero_export():
    if not _xero_tokens.get('access_token'):
        return jsonify({"error": "Not connected to Xero — please click Connect Xero"}), 403

    body = request.get_json(silent=True) or {}
    invoices = body.get('invoices', [])
    item_code = "IQPay"
    if not invoices:
        return jsonify({"error": "No invoices provided"}), 400

    try:
        xero_invs = [map_to_xero_invoice(row, item_code) for row in invoices]
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
                result = r.json()
                # Count created and capture any per-invoice validation errors
                for inv in result.get("Invoices", []):
                    if inv.get("HasErrors"):
                        for ve in inv.get("ValidationErrors", []):
                            errors.append(f"{inv.get('Reference','?')}: {ve.get('Message','')}")
                    else:
                        created_total += 1
            else:
                try:
                    err_body = r.json()
                    for elem in err_body.get("Elements", []):
                        ref = elem.get("Reference", "?")
                        for ve in elem.get("ValidationErrors", []):
                            errors.append(f"{ref}: {ve.get('Message', '')}")
                    if not errors:
                        errors.append(err_body.get("Message", r.text[:300]))
                except Exception:
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
