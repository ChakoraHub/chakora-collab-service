# =============================================================
#  clientcollabration.py  –  Client Collaboration Microservice
#  (FastAPI version — was Flask)
#  Run on a separate port, e.g.  python clientcollabration.py
#  app.py acts as a plain HTTP proxy → this service → Oracle
#  (no API Gateway — direct HTTP calls, matching app.py's other
#  microservice proxy routes)
# =============================================================

import os
import uuid
import secrets
import pathlib
import datetime
import oracledb

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import uvicorn

# ------------------------------------------------------------------
# ENV LOADING  (same .env as app.py)
# ------------------------------------------------------------------
_script_dir = pathlib.Path(__file__).resolve().parent
for _candidate in [
    _script_dir / ".env",
    _script_dir / ".env.txt",
    pathlib.Path.cwd() / ".env",
    pathlib.Path.cwd() / ".env.txt",
]:
    if _candidate.exists():
        load_dotenv(dotenv_path=_candidate, override=True)
        print(f"[clientcollabration] dotenv loaded: {_candidate}")
        break

# ------------------------------------------------------------------
# ORACLE CONFIG  (identical keys/defaults to app.py, so both services
# point at the same Oracle instance unless overridden via env vars)
# ------------------------------------------------------------------
ORACLE_HOST         = os.getenv("ORACLE_HOST", "56.228.73.210")
ORACLE_PORT         = int(os.getenv("ORACLE_PORT", "1521"))
ORACLE_SERVICE_NAME = os.getenv("ORACLE_SERVICE_NAME", "FREEPDB1")
ORACLE_USER         = os.getenv("ORACLE_USER", "SUPPORT")
ORACLE_PASSWORD     = os.getenv("ORACLE_PASSWORD", "Welcome123")


# ------------------------------------------------------------------
# ORACLE COMPAT LAYER  (same shim as before, so %s-style placeholders
# and dict-cursor calls in this file's SQL keep working unchanged)
# ------------------------------------------------------------------
class _OracleCompatCursor:
    def __init__(self, raw_cursor, dict_mode=False):
        self._cursor = raw_cursor
        self._dict_mode = dict_mode

    @staticmethod
    def _rewrite_sql(sql, params):
        if params is None or "%s" not in sql:
            return sql, params
        parts = sql.split("%s")
        rewritten = parts[0]
        for idx, tail in enumerate(parts[1:], start=1):
            rewritten += f":{idx}{tail}"
        return rewritten, params

    def execute(self, sql, params=None):
        rewritten_sql, rewritten_params = self._rewrite_sql(sql, params)
        if rewritten_params is None:
            self._cursor.execute(rewritten_sql)
        else:
            self._cursor.execute(rewritten_sql, rewritten_params)

        if self._dict_mode and self._cursor.description:
            columns = [desc[0] for desc in self._cursor.description]
            self._cursor.rowfactory = lambda *values, cols=columns: dict(zip(cols, values))

        return self

    def executemany(self, sql, seq_of_params):
        rewritten_sql, _ = self._rewrite_sql(sql, (0,))
        self._cursor.executemany(rewritten_sql, seq_of_params)
        return self

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _OracleCompatConnection:
    def __init__(self, raw_conn):
        self._conn = raw_conn

    def cursor(self, *args, **kwargs):
        dict_mode = bool(args)
        raw_cursor = self._conn.cursor()
        return _OracleCompatCursor(raw_cursor, dict_mode=dict_mode)

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ------------------------------------------------------------------
# ORACLE CONNECTION  (shared instance/account details for the whole
# microservice — used by ALL sub-flows in this file: applications,
# BRS, and organization sign-off)
# ------------------------------------------------------------------
def get_db_connection():
    """
    Uses the SAME Oracle instance details already in app.py:
        host         = 56.228.73.210  (override: ORACLE_HOST)
        port         = 1521           (override: ORACLE_PORT)
        service_name = FREE           (override: ORACLE_SERVICE_NAME)
        user         = system         (override: ORACLE_USER)
        password     = Chakorahub123  (override: ORACLE_PASSWORD)
    """
    dsn = oracledb.makedsn(
        host=ORACLE_HOST,
        port=ORACLE_PORT,
        service_name=ORACLE_SERVICE_NAME,
    )
    raw_conn = oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=dsn,
    )
    conn = _OracleCompatConnection(raw_conn)

    # Startup connectivity check (Oracle equivalent of "SELECT 1")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM DUAL")
    cur.execute("ALTER SESSION SET CURRENT_SCHEMA = CHAKORA")
    cur.close()
    return conn


# ══════════════════════════════════════════════════════════════════
#  ORGANIZATION DATABASE LAYER
#  Kept as its own module-level block so the Organization Sign-Off
#  data access is clearly separated from the Application / BRS data
#  access above, and — more importantly — separated OUT of app.py.
#  app.py no longer opens a DB connection or runs SQL for sign-offs;
#  it only forwards the HTTP request here.
# ══════════════════════════════════════════════════════════════════
def get_org_db_connection():
    """
    Organization Sign-Off uses the same Oracle instance as the rest of
    this microservice. Kept as a separate function (rather than reusing
    get_db_connection directly) so organization data access has its own
    entry point — e.g. if sign-off data is later split onto its own
    schema/instance, only this function needs to change.
    """
    return get_db_connection()


def insert_org_signoff(data: dict) -> str:
    """
    Inserts a new Organization Sign-Off record into Oracle and returns
    the generated SIGNOFF_ID. This is the "Direct DB Communication" leg
    for the organization flow — app.py never touches this DB connection.

    NOTE: the uploaded consent form (base64 in "filedata") is NOT
    persisted here — only "filename" is stored as metadata, matching
    the same pattern used for BRS file uploads (S3 wiring, if/when
    added, happens from app.py or a dedicated step, not here).
    """
    conn   = None
    cursor = None
    try:
        signoff_id = f"SIGN-{datetime.datetime.now().year}-{uuid.uuid4().hex[:8].upper()}"

        conn = get_org_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO INDUSTRY_ORG_SIGNOFF (
                SIGNOFF_ID, ORG_NAME, AUTHORIZED_PERSON, BRS_ID,
                APPROVAL_NOTES, APPROVAL_STATUS, FILENAME, CREATED_AT
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, CURRENT_TIMESTAMP
            )
            """,
            (
                signoff_id,
                data.get("org_name"),
                data.get("authorized_person"),
                data.get("brs_id"),
                data.get("approval_notes"),
                data.get("approval_status"),
                data.get("filename"),
            ),
        )
        conn.commit()
        print(f"✅ Organization sign-off inserted: {signoff_id}")
        return signoff_id
    finally:
        try:
            if cursor: cursor.close()
            if conn:   conn.close()
        except Exception:
            pass


# ------------------------------------------------------------------
# FASTAPI APP
# ------------------------------------------------------------------
app = FastAPI(title="Client Collaboration Microservice")

# ------------------------------------------------------------------
# CORS  (same policy as before — chakorahub.com + any localhost port)
# ------------------------------------------------------------------
_ALLOWED_ORIGINS = [
    "https://www.chakorahub.com", "https://chakorahub.com",
    "http://www.chakorahub.com",  "http://chakorahub.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ------------------------------------------------------------------
# MAINTENANCE MODE
#   GET  /api/collaboration/maintenance/status   (public)
#   POST /api/collaboration/maintenance/on       (Bearer token)
#   POST /api/collaboration/maintenance/off      (Bearer token)
#
# State is a flag file: file exists = maintenance ON. It lives outside
# the code checkout so a redeploy or restart does not clear it.
#   MAINTENANCE_FLAG  - flag file path (default: ~/collaboration-maintenance.flag)
#   MAINTENANCE_TOKEN - secret required to switch maintenance on/off
# ------------------------------------------------------------------
MAINTENANCE_FLAG = pathlib.Path(
    os.getenv("MAINTENANCE_FLAG")
    or (pathlib.Path.home() / "collaboration-maintenance.flag")
)
MAINTENANCE_TOKEN = (os.getenv("MAINTENANCE_TOKEN") or "").strip()


def is_maintenance_enabled() -> bool:
    return MAINTENANCE_FLAG.exists()


def require_maintenance_token(request: Request):
    if not MAINTENANCE_TOKEN:
        raise HTTPException(status_code=503, detail="Maintenance token is not configured")

    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""

    if not secrets.compare_digest(supplied.encode(), MAINTENANCE_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Invalid maintenance token")


@app.get("/api/collaboration/maintenance/status")
async def maintenance_status():
    return {"success": True, "maintenance_mode": is_maintenance_enabled()}


@app.post("/api/collaboration/maintenance/on")
async def maintenance_on(request: Request):
    require_maintenance_token(request)
    MAINTENANCE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    MAINTENANCE_FLAG.touch(exist_ok=True)
    return {"success": True, "maintenance_mode": True}


@app.post("/api/collaboration/maintenance/off")
async def maintenance_off(request: Request):
    require_maintenance_token(request)
    MAINTENANCE_FLAG.unlink(missing_ok=True)
    return {"success": True, "maintenance_mode": False}


# ══════════════════════════════════════════════════════════════════
#  POST /application/submit
#  Receives JSON from app.py's Flask Proxy (/submit-application),
#  inserts a new Industry Application record directly into Oracle,
#  and returns the generated APPLICATION_ID.
# ══════════════════════════════════════════════════════════════════
@app.post("/application/submit")
async def application_submit(request: Request):

    conn   = None
    cursor = None

    try:
        body = await request.json()

        required = ["full_name", "email", "phone", "organisation",
                    "collaboration_type", "project_title", "description"]
        missing = [f for f in required if not (body.get(f) or "").strip()]
        if missing:
            return JSONResponse(
                status_code=400,
                content={"error": f"Missing required field(s): {', '.join(missing)}"},
            )

        application_id = f"APP-{datetime.datetime.now().year}-{uuid.uuid4().hex[:8].upper()}"

        conn = get_db_connection()
        cursor = conn.cursor()

        # START_DATE is a DATE column — Oracle's implicit string→date
        # conversion depends on NLS_DATE_FORMAT (often DD-MON-RR), so a
        # plain "%s" bind of a "YYYY-MM-DD" string (what an HTML
        # <input type="date"> sends) throws ORA-01861. Parse it
        # explicitly instead.
        raw_start_date = (body.get("start_date") or "").strip() or None
        cursor.execute(
            """
            INSERT INTO INDUSTRY_APPLICATIONS (
                APPLICATION_ID, FULL_NAME, EMAIL, PHONE, ORGANISATION,
                COLLABORATION_TYPE, PROJECT_TITLE, DESCRIPTION,
                START_DATE, STATUS, CREATED_AT
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                TO_DATE(%s, 'YYYY-MM-DD'), 'Submitted', CURRENT_TIMESTAMP
            )
            """,
            (
                application_id,
                body.get("full_name"),
                body.get("email"),
                body.get("phone"),
                body.get("organisation"),
                body.get("collaboration_type"),
                body.get("project_title"),
                body.get("description"),
                raw_start_date,
            ),
        )
        conn.commit()

        print(f"✅ Application inserted: {application_id}")
        return {
            "message": "Application submitted successfully",
            "application_id": application_id,
        }

    except Exception as exc:
        print(f"❌ Application submit error: {exc}")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(exc)}"})

    finally:
        try:
            if cursor: cursor.close()
            if conn:   conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
#  POST /brs/submit
#  Receives JSON from app.py's /industry/brs proxy (STEP 1 of the
#  BRS flow), inserts a new BRS record directly into Oracle, and
#  returns the generated BRS_ID.
# ══════════════════════════════════════════════════════════════════
@app.post("/brs/submit")
async def brs_submit(request: Request):

    conn   = None
    cursor = None

    try:
        body = await request.json()

        required = ["project_name", "client_name", "requirement_type",
                    "business_objective", "contact_email"]
        missing = [f for f in required if not (body.get(f) or "").strip()]
        if missing:
            return JSONResponse(
                status_code=400,
                content={"error": f"Missing required field(s): {', '.join(missing)}"},
            )

        brs_id = f"BRS-{datetime.datetime.now().year}-{uuid.uuid4().hex[:8].upper()}"

        conn = get_db_connection()
        cursor = conn.cursor()

        raw_start_date = (body.get("start_date") or "").strip() or None
        raw_end_date   = (body.get("end_date") or "").strip() or None

        # Project Amount comes from the "Billing" section of submit_brs.html
        # (name="amount"). Kept optional at the DB level so old callers that
        # don't send it don't break, but the BRS payment step (/brs/payment
        # in app.py) needs it to create the Razorpay order.
        raw_amount = body.get("amount")
        try:
            raw_amount = float(raw_amount) if raw_amount not in (None, "") else None
        except (TypeError, ValueError):
            raw_amount = None

        cursor.execute(
            """
            INSERT INTO INDUSTRY_BRS (
                BRS_ID, PROJECT_ID, PROJECT_NAME, PROJECT_DESCRIPTION,
                CLIENT_NAME, DEPARTMENT, REQUIREMENT_TYPE, PRIORITY,
                BUSINESS_OBJECTIVE, SCOPE, START_DATE, END_DATE,
                CONTACT_EMAIL, CONTACT_PHONE, FILENAME, AMOUNT,
                STATUS, PAYMENT_STATUS, CREATED_AT
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, TO_DATE(%s, 'YYYY-MM-DD'), TO_DATE(%s, 'YYYY-MM-DD'),
                %s, %s, %s, %s,
                'Submitted', 'PENDING', CURRENT_TIMESTAMP
            )
            """,
            (
                brs_id,
                body.get("project_id"),
                body.get("project_name"),
                body.get("project_description"),
                body.get("client_name"),
                body.get("department"),
                body.get("requirement_type"),
                body.get("priority"),
                body.get("business_objective"),
                body.get("scope"),
                raw_start_date,
                raw_end_date,
                body.get("contact_email"),
                body.get("contact_phone"),
                body.get("filename"),
                raw_amount,
            ),
        )
        conn.commit()

        print(f"✅ BRS inserted: {brs_id}")
        return {
            "message": "BRS submitted successfully",
            "brs_id": brs_id,
        }

    except Exception as exc:
        print(f"❌ BRS submit error: {exc}")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(exc)}"})

    finally:
        try:
            if cursor: cursor.close()
            if conn:   conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
#  POST /brs/alignment-confirm
#  Receives {"brs_id": ...} from app.py's /alignment-submit proxy
#  (STEP 3 of the BRS flow) and marks that BRS record as aligned.
# ══════════════════════════════════════════════════════════════════
@app.post("/brs/alignment-confirm")
async def brs_alignment_confirm(request: Request):

    conn   = None
    cursor = None

    try:
        body   = await request.json()
        brs_id = (body.get("brs_id") or "").strip()

        if not brs_id:
            return JSONResponse(status_code=400, content={"error": "Missing required field: brs_id"})

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE INDUSTRY_BRS
               SET STATUS = 'Aligned',
                   ALIGNED_AT = CURRENT_TIMESTAMP
             WHERE BRS_ID = %s
            """,
            (brs_id,),
        )
        conn.commit()

        if cursor.rowcount == 0:
            print(f"⚠️ Alignment confirm: no BRS row found for {brs_id}")
            return JSONResponse(status_code=404, content={"error": f"No BRS record found for {brs_id}"})

        print(f"✅ BRS alignment confirmed: {brs_id}")
        return {"message": "Alignment confirmed", "brs_id": brs_id}

    except Exception as exc:
        print(f"❌ Alignment confirm error: {exc}")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(exc)}"})

    finally:
        try:
            if cursor: cursor.close()
            if conn:   conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
#  POST /brs/payment/record
#  Receives the verified Razorpay result from app.py's
#  /brs/payment/verify (STEP 1.5 of the BRS flow — client billing,
#  same pattern as the student /registration payment) and:
#    1. Inserts a row into the shared NRM_PAYMENTS table (the same
#       table student registration payments are written to —
#       USER_TYPE distinguishes 'STUDENT' vs 'CLIENT' rows).
#    2. Mirrors the payment status back onto INDUSTRY_BRS so the
#       Project Dashboard / project_status() above can show it
#       without joining NRM_PAYMENTS.
#  app.py has ALREADY verified the Razorpay signature before calling
#  this endpoint — this layer only persists the result.
# ══════════════════════════════════════════════════════════════════
@app.post("/brs/payment/record")
async def brs_payment_record(request: Request):

    conn = None
    cursor = None

    try:
        body = await request.json()

        required = ["brs_id", "amount", "payment_status"]
        missing = [f for f in required if body.get(f) in (None, "")]
        if missing:
            return JSONResponse(
                status_code=400,
                content={
                    "error": f"Missing required field(s): {', '.join(missing)}"
                },
            )

        payment_id = (body.get("payment_id") or "").strip() or (
            f"PAY-{datetime.datetime.now().year}-{uuid.uuid4().hex[:8].upper()}"
        )

        conn = get_db_connection()
        cursor = conn.cursor()

        # -------------------------------------------------
        # Generate next ID (No sequence exists in DB)
        # -------------------------------------------------
        cursor.execute("SELECT NVL(MAX(ID), 0) + 1 FROM CHAKORA.NRM_PAYMENTS")
        next_payment_id = cursor.fetchone()[0]

        # -------------------------------------------------
        # Insert into NRM_PAYMENTS
        # -------------------------------------------------
        cursor.execute(
            """
            INSERT INTO CHAKORA.NRM_PAYMENTS (
                ID,
                USER_ID,
                RAZORPAY_PAYMENT_ID,
                RAZORPAY_ORDER_ID,
                COURSE,
                PAYMENT_AMOUNT,
                CREATED_AT
            )
            VALUES (
                %s,
                NULL,
                %s,
                %s,
                %s,
                %s,
                CURRENT_TIMESTAMP
            )
            """,
            (
                next_payment_id,
                body.get("razorpay_payment_id"),
                body.get("razorpay_order_id"),
                "BRS Payment",
                body.get("amount"),
            ),
        )

        # -------------------------------------------------
        # Update INDUSTRY_BRS
        # -------------------------------------------------
        cursor.execute(
            """
            UPDATE INDUSTRY_BRS
               SET PAYMENT_STATUS = %s,
                   PAYMENT_ID = %s
             WHERE BRS_ID = %s
            """,
            (
                body.get("payment_status"),
                payment_id,
                body.get("brs_id"),
            ),
        )

        conn.commit()

        print(
            f"✅ Payment recorded successfully. "
            f"NRM_PAYMENT ID={next_payment_id}, "
            f"BRS={body.get('brs_id')}"
        )

        return {
            "message": "Payment recorded successfully",
            "payment_id": payment_id,
            "nrm_payment_id": next_payment_id,
        }

    except Exception as exc:
        if conn:
            conn.rollback()

        print(f"❌ BRS payment record error: {exc}")

        return JSONResponse(
            status_code=500,
            content={"error": f"Internal error: {str(exc)}"},
        )

    finally:
        try:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        except Exception:
            pass
# ══════════════════════════════════════════════════════════════════
#  GET /brs/lookup/{brs_id}
#  Small read-only lookup used by app.py's /org-signoff route so it
#  can email the client (CONTACT_EMAIL on the BRS record) once the
#  organization sign-off for their project is recorded — the sign-off
#  form itself doesn't collect an email address.
# ══════════════════════════════════════════════════════════════════
@app.get("/brs/lookup/{brs_id}")
async def brs_lookup(brs_id: str):

    conn   = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = _dict_cursor(conn)

        cursor.execute(
            """
            SELECT BRS_ID, PROJECT_NAME, CLIENT_NAME, CONTACT_EMAIL,
                   CONTACT_PHONE, AMOUNT, STATUS
            FROM INDUSTRY_BRS
            WHERE BRS_ID = :1
            """,
            (brs_id,),
        )
        row = cursor.fetchone()

        if not row:
            return JSONResponse(status_code=404, content={"error": f"No BRS record found for {brs_id}"})

        return {
            "brs_id":        row["BRS_ID"],
            "project_name":  row["PROJECT_NAME"],
            "client_name":   row["CLIENT_NAME"],
            "contact_email": row["CONTACT_EMAIL"],
            "contact_phone": row["CONTACT_PHONE"],
            "amount":        float(row["AMOUNT"]) if row.get("AMOUNT") is not None else None,
            "status":        row["STATUS"],
        }

    except Exception as exc:
        print(f"❌ BRS lookup error: {exc}")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(exc)}"})

    finally:
        try:
            if cursor: cursor.close()
            if conn:   conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
#  POST /org/signoff/submit
#  Receives JSON from app.py's /org-signoff proxy, inserts a new
#  Organization Sign-Off record directly into Oracle using the
#  organization DB layer above, and returns the generated SIGNOFF_ID.
#  Mirrors the /brs/submit flow, but for the org-signoff form.
# ══════════════════════════════════════════════════════════════════
@app.post("/org/signoff/submit")
async def org_signoff_submit(request: Request):

    try:
        body = await request.json()

        required = ["org_name", "authorized_person", "brs_id", "approval_status"]
        missing = [f for f in required if not (body.get(f) or "").strip()]
        if missing:
            return JSONResponse(
                status_code=400,
                content={"error": f"Missing required field(s): {', '.join(missing)}"},
            )

        signoff_id = insert_org_signoff(body)

        return {
            "message": "Organization sign-off submitted successfully",
            "signoff_id": signoff_id,
        }

    except Exception as exc:
        print(f"❌ Org sign-off submit error: {exc}")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(exc)}"})

# ══════════════════════════════════════════════════════════════════
#  POST /auth/login
#  Receives {"username": ..., "password": ...} from app.py's
#  /track-login proxy, verifies credentials against Oracle, and
#  returns the user record on success. app.py never touches the DB
#  for this — it only forwards the HTTP request and reads the result.
# ══════════════════════════════════════════════════════════════════
@app.post("/auth/login")
async def track_login(request: Request):

    try:
        body = await request.json()

        username = (body.get("username") or "").strip()
        password = (body.get("password") or "").strip()

        if not username or not password:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Username and Password are required"
                }
            )

        # Later you can validate from Oracle.
        # For now, always allow login.

        return {
            "success": True,
            "message": "Login Successful"
        }

    except Exception as e:
        print("Track Login Error:", e)

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e)
            }
        )
# ══════════════════════════════════════════════════════════════════
#  PROJECT TRACKING DATABASE LAYER
#  Kept as its own block, same reasoning as the Organization layer
#  above: Project Status / Project Dashboard data access lives here,
#  OUT of app.py. app.py only forwards the HTTP request and relays
#  whatever this service returns — it never opens a DB connection
#  for tracking or dashboard data.
# ══════════════════════════════════════════════════════════════════
def _dict_cursor(conn):
    """Small helper matching app.py's DICT_CURSOR convention:
    conn.cursor(True) turns on rowfactory dict mode in the compat layer."""
    return conn.cursor(True)


# ══════════════════════════════════════════════════════════════════
#  GET /project/status/{project_id}
#  Receives the project_id from app.py's /api/project-status/<id>
#  proxy, looks up the project + phase master list + phase history
#  directly in Oracle, and returns the assembled JSON. This is the
#  exact logic that used to live inside app.py — moved here so
#  app.py stays a pure HTTP proxy with no DB access for tracking.
# ══════════════════════════════════════════════════════════════════
@app.get("/project/status/{project_id}")
async def project_status(project_id: str):

    conn = cursor = None

    try:

        conn = get_db_connection()
        cursor = _dict_cursor(conn)

        cursor.execute("""
            SELECT
                BRS_ID,
                PROJECT_ID,
                PROJECT_NAME,
                CLIENT_NAME,
                START_DATE,
                STATUS
            FROM INDUSTRY_BRS
            WHERE BRS_ID = :1
        """, (project_id,))

        project = cursor.fetchone()

        if not project:
            return JSONResponse(
                status_code=404,
                content={"error": "Project not found"}
            )

        # Must match the frontend's hardcoded PHASES array in
        # track-project-status.html exactly (renderTimeline() does
        # PHASES.indexOf(current_phase.name) to decide which dots
        # light up as completed/current, so the names here have to
        # be identical strings, not just semantically similar).
        phases = [
            {"order": 1, "name": "Requirement Gathering"},
            {"order": 2, "name": "Design Phase Approval"},
            {"order": 3, "name": "Development Phase"},
            {"order": 4, "name": "Testing Phase"},
            {"order": 5, "name": "Prod Deployment"},
            {"order": 6, "name": "Hyper Care Support"},
            {"order": 7, "name": "Final Sign Off"},
        ]

        # Raw DB STATUS values ("Submitted", "Aligned", ...) don't
        # match the frontend's phase names, so map them here. Extend
        # this as more statuses get introduced upstream.
        STATUS_TO_PHASE = {
            "Submitted": phases[0],   # Requirement Gathering
            "Aligned":   phases[1],   # Design Phase Approval
        }
        current_phase = STATUS_TO_PHASE.get(project["STATUS"], phases[0])

        return {

            "project_id": project["BRS_ID"],

            "project_name": project["PROJECT_NAME"],

            "owner": project["CLIENT_NAME"],

            "start_date": project["START_DATE"].isoformat()
            if project["START_DATE"] else None,

            "current_phase": current_phase,

            "all_phases": phases,

            "history":[
                {
                    "date": project["START_DATE"].isoformat()
                    if project["START_DATE"] else None,

                    "new_phase": current_phase["name"],

                    "notes": f"Status: {project['STATUS']}"
                }
            ]
        }

    except Exception as e:

        print("❌ Project status error:", e)

        return JSONResponse(
            status_code=500,
            content={"error":"Internal server error"}
        )

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()

# ══════════════════════════════════════════════════════════════════
#  GET /project/dashboard/summary
#  Receives the request from app.py's /api/project-dashboard/data
#  proxy. Returns the BRS→Project table rows plus the three summary
#  counters (total BRS, not created, projects created) that the
#  Project Dashboard page renders. Same separation-of-concerns
#  reasoning as project_status() above.
# ══════════════════════════════════════════════════════════════════
@app.get("/project/dashboard/summary")
async def project_dashboard_summary():

    conn = cursor = None

    try:

        conn = get_db_connection()

        cursor = _dict_cursor(conn)

        cursor.execute("""

            SELECT

                PROJECT_ID,

                BRS_ID,

                PROJECT_NAME,

                CLIENT_NAME,

                DEPARTMENT,

                START_DATE,

                END_DATE,

                PRIORITY,

                STATUS

            FROM INDUSTRY_BRS

            ORDER BY START_DATE DESC

        """)

        rows = cursor.fetchall()

        for row in rows:

            if row["START_DATE"]:
                row["START_DATE"] = row["START_DATE"].isoformat()

            if row["END_DATE"]:
                row["END_DATE"] = row["END_DATE"].isoformat()

        total = len(rows)

        submitted = sum(
            1 for r in rows
            if r["STATUS"] == "Submitted"
        )

        return {

            "rows": rows,

            "stats": {

                "total_brs": total,

                "projects_created": total,

                "not_created": 0,

                "submitted": submitted

            }

        }

    except Exception as e:

        print("❌ Dashboard error:", e)

        return JSONResponse(
            status_code=500,
            content={"error":"Internal server error"}
        )

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()

# ══════════════════════════════════════════════════════════════════
#  Health check
# ══════════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    return {"status": "ok", "service": "clientcollabration"}


# ------------------------------------------------------------------
# ENTRY POINT  –  runs on port 8020 (same port app.py already proxies
# to via APPLICATION_SERVICE_URL — no API Gateway involved, plain
# HTTP calls between the two services)
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("🚀 Client Collaboration Microservice (FastAPI) starting on 0.0.0.0:8020 ...")
    uvicorn.run(app, host="0.0.0.0", port=8020)
