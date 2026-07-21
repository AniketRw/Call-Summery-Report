import pyodbc
import time
import os

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

    conn.commit()
    cur.close()
    conn.close()


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
                token_total = ?,
                elapsed_seconds = ?,
                created_at = SYSDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (
                filename, customer_mood, mood_reason, executive_name,
                greetings, closings, executive_tone, keywords,
                token_total, elapsed_seconds
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        OUTPUT inserted.id;
    """, (
        filename,
        fields["customer_mood"], fields["mood_reason"], fields["executive_name"],
        fields["greetings"], fields["closings"], fields["executive_tone"],
        keywords, (token_info or {}).get("total"), elapsed_time,
        filename,
        fields["customer_mood"], fields["mood_reason"], fields["executive_name"],
        fields["greetings"], fields["closings"], fields["executive_tone"],
        keywords, (token_info or {}).get("total"), elapsed_time
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