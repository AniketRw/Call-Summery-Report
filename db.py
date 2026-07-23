import pyodbc
import time
import os
import re

from testnew import load_env_file
load_env_file()

DB_SERVER = os.getenv("DB_SERVER", r"localhost")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if DB_USER and DB_PASSWORD:
    CONN_STR = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        f"UID={DB_USER};"
        f"PWD={DB_PASSWORD};"
    )
else:
    CONN_STR = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_NAME};"
        f"Trusted_Connection=yes;"
    )


def get_conn():
    return pyodbc.connect(CONN_STR)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # "Main" table — short, structured fields. Easy to browse in SSMS.
    cur.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='call_summaries' AND xtype='U')
        CREATE TABLE call_summaries (
            id INT IDENTITY(1,1) PRIMARY KEY,
            filename NVARCHAR(500) NOT NULL UNIQUE,
            customer_mood NVARCHAR(100),
            mood_reason NVARCHAR(MAX),
            executive_name NVARCHAR(200),
            greetings NVARCHAR(MAX),
            closings NVARCHAR(MAX),
            executive_tone NVARCHAR(100),
            keywords NVARCHAR(MAX),
            mobile_no NVARCHAR(20),
            customer_name NVARCHAR(300),
            token_total INT,
            elapsed_seconds FLOAT,
            created_at DATETIME2 DEFAULT SYSDATETIME()
        )
    """)

    # "Details" table — the long text blocks, kept out of the main table.
    cur.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='call_summary_details' AND xtype='U')
        CREATE TABLE call_summary_details (
            call_id INT PRIMARY KEY,
            facts NVARCHAR(MAX),
            short_summary NVARCHAR(MAX),
            long_summary NVARCHAR(MAX),
            transcript NVARCHAR(MAX),
            analysis_raw NVARCHAR(MAX),
            CONSTRAINT FK_details_call FOREIGN KEY (call_id)
                REFERENCES call_summaries(id)
        )
    """)

    # Safety net: if call_summaries already existed from before (without the
    # new columns), add them now so old installs pick up the new feature too.
    cur.execute("""
        IF NOT EXISTS (
            SELECT * FROM sys.columns
            WHERE object_id = OBJECT_ID('call_summaries') AND name = 'mobile_no'
        )
        ALTER TABLE call_summaries ADD mobile_no NVARCHAR(20);
    """)
    cur.execute("""
        IF NOT EXISTS (
            SELECT * FROM sys.columns
            WHERE object_id = OBJECT_ID('call_summaries') AND name = 'customer_name'
        )
        ALTER TABLE call_summaries ADD customer_name NVARCHAR(300);
    """)

    conn.commit()
    cur.close()
    conn.close()


def extract_mobile_from_filename(filename):
    """Pull a 10-digit mobile number out of filenames like
    '28-05-2026-1779961732.22548-+919860531564.wav'.
    Looks for a '+' followed by digits (right before the file extension),
    then keeps only the last 10 digits (drops the '91' country code)."""
    if not filename:
        return None

    # safe_filename() replaces '+' with '_', so the stored filename may end
    # in either '+919916539593' or '_919916539593' before the extension.
    stem = os.path.splitext(filename)[0]
    match = re.search(r'[+_](\d{10,13})$', stem)
    if not match:
        return None

    digits = match.group(1)
    return digits[-10:] if len(digits) >= 10 else digits


def extract_uniqueid_from_filename(filename):
    """Pull the CDR 'uniqueid' out of filenames like
    '23-07-2026-1784780888.30006-126.wav' (extension-only call, no mobile
    number embedded in the filename) -> '1784780888.30006'.
    The uniqueid always looks like <10 digits>.<4-6 digits>, and appears
    right after the date prefix in the filename."""
    if not filename:
        return None
    stem = os.path.splitext(filename)[0]
    match = re.search(r'(\d{10}\.\d{4,6})', stem)
    return match.group(1) if match else None


def lookup_mobile_from_cdr(cur, uniqueid):
    """For extension-only recordings (no mobile number in filename), look up
    src/dst from SynapseCDR..RecordsFrom_SIP_MySQL by uniqueid, and return
    whichever of the two looks like a real 10-digit mobile number. The other
    side is normally a short internal code (queue '6000', extension '126',
    trunk prefix '63'/'81'/'101' etc.), so a plain 10-digit match is enough
    to tell them apart."""
    if not uniqueid:
        return None
    try:
        cur.execute("""
            SELECT TOP 1 src, dst
            FROM SynapseCDR.dbo.RecordsFrom_SIP_MySQL
            WHERE uniqueid = ?
        """, (uniqueid,))
        row = cur.fetchone()
        if not row:
            return None
        for val in row:
            if val and re.fullmatch(r'\d{10}', str(val).strip()):
                return str(val).strip()
    except Exception as e:
        print(f"[DB] CDR lookup failed for uniqueid={uniqueid}: {e}")
    return None


def resolve_mobile_no(cur, filename):
    """Single entry point to get the customer's mobile number for a given
    recording filename, trying both known filename patterns in order:
      1) mobile number embedded directly in the filename (+91XXXXXXXXXX)
      2) extension-only filename -> resolve via CDR uniqueid -> src/dst"""
    mobile_no = extract_mobile_from_filename(filename)
    if mobile_no:
        return mobile_no

    uniqueid = extract_uniqueid_from_filename(filename)
    return lookup_mobile_from_cdr(cur, uniqueid)


def lookup_customer_name(cur, mobile_no):
    """Look up mobile_no first in CustomerMaster directly. If not found there,
    check CustDependents for a matching DepMobileNo — but even then, pull the
    real customer/business name from CustomerMaster via the dependent's CustId,
    instead of using CustDependents.Name (which is often just a role like
    'Oprator', not the actual customer name)."""
    if not mobile_no:
        return None

    def name_from_master_row(row):
        parts = [p.strip() for p in row if p and p.strip()]
        return " ".join(parts) if parts else None

    try:
        # 1) Direct match: mobile belongs to the customer itself
        cur.execute("""
            SELECT TOP 1 Name, MiddleName, Lastname
            FROM CustomerMaster
            WHERE MobileNo = ?
        """, (mobile_no,))
        row = cur.fetchone()
        if row:
            name = name_from_master_row(row)
            if name:
                return name

        # 2) Mobile belongs to a dependent -> find their CustId,
        #    then fetch the real name from CustomerMaster using that CustId
        cur.execute("""
            SELECT TOP 1 CustId
            FROM CustDependents
            WHERE DepMobileNo = ?
        """, (mobile_no,))
        row = cur.fetchone()
        if row and row[0]:
            cust_id = row[0]
            cur.execute("""
                SELECT TOP 1 Name, MiddleName, Lastname
                FROM CustomerMaster
                WHERE CustId = ?
            """, (cust_id,))
            master_row = cur.fetchone()
            if master_row:
                name = name_from_master_row(master_row)
                if name:
                    return name
    except Exception as e:
        print(f"[DB] Customer lookup failed for {mobile_no}: {e}")

    return None


def split_summary(combined_summary):
    """Split the combined 'SHORT SUMMARY\\n====\\n...\\n\\nLONG SUMMARY\\n====\\n...'
    text (from testnew.analyze_audio_with_gemini) into clean short/long parts."""
    text = combined_summary or ""

    long_idx = text.find("LONG SUMMARY")
    if long_idx == -1:
        # No recognizable split marker — treat everything as the long summary
        return "", text.strip()

    short_block = text[:long_idx]
    long_block = text[long_idx:]

    # Strip the "SHORT SUMMARY" / "LONG SUMMARY" headers and the "====" divider lines
    def clean(block, header):
        lines = block.splitlines()
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if stripped == header:
                continue
            if stripped and set(stripped) == {"="}:
                continue
            cleaned.append(line)
        return "\n".join(cleaned).strip()

    short_part = clean(short_block, "SHORT SUMMARY")
    long_part = clean(long_block, "LONG SUMMARY")
    return short_part, long_part


def save_call_summary(filename, analysis_text, keywords, facts, summary,
                       transcript, token_info=None, elapsed_time=None):
    # analysis_text is the multi-line "Customer Mood: ... \nExecutive Name: ..." block
    fields = {"customer_mood": "", "mood_reason": "", "executive_name": "",
              "greetings": "", "closings": "", "executive_tone": ""}
    for line in (analysis_text or "").splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if key.startswith("customer mood"):
            if "(" in val and val.endswith(")"):
                mood, reason = val.split("(", 1)
                fields["customer_mood"] = mood.strip()
                fields["mood_reason"] = reason.rstrip(")").strip()
            else:
                fields["customer_mood"] = val
        elif key.startswith("executive name"):
            fields["executive_name"] = val
        elif key.startswith("greetings"):
            fields["greetings"] = val
        elif key.startswith("closings"):
            fields["closings"] = val
        elif key.startswith("executive tone"):
            fields["executive_tone"] = val

    short_summary, long_summary = split_summary(summary)

    conn = get_conn()
    cur = conn.cursor()

    # Resolve mobile number: either embedded in the filename, or (for
    # extension-only filenames) via the CDR table's uniqueid -> src/dst,
    # then look up the customer's name from that mobile number.
    mobile_no = resolve_mobile_no(cur, filename)
    customer_name = lookup_customer_name(cur, mobile_no)

    # 1) Upsert the "main" row and get its id back
    cur.execute("""
        MERGE call_summaries AS target
        USING (SELECT ? AS filename) AS src
        ON target.filename = src.filename
        WHEN MATCHED THEN
            UPDATE SET
                customer_mood = ?,
                mood_reason = ?,
                executive_name = ?,
                greetings = ?,
                closings = ?,
                executive_tone = ?,
                keywords = ?,
                mobile_no = ?,
                customer_name = ?,
                token_total = ?,
                elapsed_seconds = ?,
                created_at = SYSDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (
                filename, customer_mood, mood_reason, executive_name,
                greetings, closings, executive_tone, keywords,
                mobile_no, customer_name,
                token_total, elapsed_seconds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        OUTPUT inserted.id;
    """, (
        filename,
        fields["customer_mood"], fields["mood_reason"], fields["executive_name"],
        fields["greetings"], fields["closings"], fields["executive_tone"],
        keywords, mobile_no, customer_name,
        (token_info or {}).get("total"), elapsed_time,
        filename,
        fields["customer_mood"], fields["mood_reason"], fields["executive_name"],
        fields["greetings"], fields["closings"], fields["executive_tone"],
        keywords, mobile_no, customer_name,
        (token_info or {}).get("total"), elapsed_time
    ))
    row = cur.fetchone()
    conn.commit()

    if row:
        call_id = row[0]
    else:
        # Some ODBC drivers don't return OUTPUT rows through MERGE reliably —
        # fall back to a plain lookup.
        cur.execute("SELECT id FROM call_summaries WHERE filename = ?", (filename,))
        call_id = cur.fetchone()[0]

    # 2) Upsert the matching "details" row (the long text blocks)
    cur.execute("""
        MERGE call_summary_details AS target
        USING (SELECT ? AS call_id) AS src
        ON target.call_id = src.call_id
        WHEN MATCHED THEN
            UPDATE SET
                facts = ?,
                short_summary = ?,
                long_summary = ?,
                transcript = ?,
                analysis_raw = ?
        WHEN NOT MATCHED THEN
            INSERT (call_id, facts, short_summary, long_summary, transcript, analysis_raw)
            VALUES (?, ?, ?, ?, ?, ?);
    """, (
        call_id,
        facts, short_summary, long_summary, transcript, analysis_text,
        call_id,
        facts, short_summary, long_summary, transcript, analysis_text
    ))
    conn.commit()

    cur.close()
    conn.close()
    return call_id


def backfill_customer_names():
    """One-time fix for existing rows: re-resolves the mobile number (using
    both the filename pattern and, for extension-only filenames, the CDR
    uniqueid fallback) and re-runs lookup_customer_name() for every row,
    then updates mobile_no / customer_name with the corrected values.
    Needed because:
      - old rows were saved before the CustDependents -> CustomerMaster fix,
        so they still hold stale values like 'Oprator' instead of the real
        customer/business name, and
      - old rows from extension-only recordings never had a mobile_no at all
        (the old filename-only extractor couldn't find one)."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id, filename, mobile_no FROM call_summaries")
    rows = cur.fetchall()

    updated = 0
    for call_id, filename, existing_mobile_no in rows:
        mobile_no = existing_mobile_no or resolve_mobile_no(cur, filename)
        new_name = lookup_customer_name(cur, mobile_no)

        if mobile_no != existing_mobile_no or new_name:
            cur.execute(
                "UPDATE call_summaries SET mobile_no = ?, customer_name = COALESCE(?, customer_name) WHERE id = ?",
                (mobile_no, new_name, call_id)
            )
            updated += 1

    conn.commit()
    cur.close()
    conn.close()
    print(f"Updated {updated} row(s)")


def delete_call_summary(call_id):
    """Delete a call_summaries row (and its matching call_summary_details row),
    then renumber all remaining ids back to a clean 1,2,3... sequence.

    Example: rows 1,2,3,4,5 exist -> delete id=2 -> remaining rows are
    renumbered so they become 1,2,3,4 again (no gaps)."""
    conn = get_conn()
    cur = conn.cursor()

    try:
        # 1) Remove the details row first (it has the FK), then the main row
        cur.execute("DELETE FROM call_summary_details WHERE call_id = ?", (call_id,))
        cur.execute("DELETE FROM call_summaries WHERE id = ?", (call_id,))

        # 2) Close the gap left behind by renumbering everything sequentially
        _renumber_ids(cur)

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()


def _renumber_ids(cur):
    """Renumber call_summaries.id (and the matching call_summary_details.call_id)
    to a clean 1,2,3... sequence, ordered by the current id.
    Internal helper — always call this from inside a function that already
    has an open cursor/transaction (e.g. delete_call_summary)."""

    # FK has to go temporarily, otherwise the id UPDATE below will be blocked
    cur.execute("""
        IF EXISTS (SELECT * FROM sys.foreign_keys WHERE name = 'FK_details_call')
            ALTER TABLE call_summary_details DROP CONSTRAINT FK_details_call
    """)

    # Update child table first (old id -> new sequential id)
    cur.execute("""
        ;WITH ordered AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS new_id
            FROM call_summaries
        )
        UPDATE d
        SET d.call_id = o.new_id
        FROM call_summary_details d
        JOIN ordered o ON d.call_id = o.id
    """)

    # Then update the parent table the same way
    cur.execute("""
        ;WITH ordered AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS new_id
            FROM call_summaries
        )
        UPDATE c
        SET c.id = o.new_id
        FROM call_summaries c
        JOIN ordered o ON c.id = o.id
    """)

    # Reset the IDENTITY seed so the next insert continues right after the
    # current max id (e.g. if 4 rows remain, next insert becomes id 5)
    cur.execute("""
        DECLARE @max INT = (SELECT ISNULL(MAX(id), 0) FROM call_summaries);
        DBCC CHECKIDENT ('call_summaries', RESEED, @max)
    """)

    # Put the FK back
    cur.execute("""
        ALTER TABLE call_summary_details
        ADD CONSTRAINT FK_details_call FOREIGN KEY (call_id)
            REFERENCES call_summaries(id)
    """)