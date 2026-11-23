"""Small schema helpers shared by the modules that upgrade an existing database in place."""
import sqlite3


def add_column(conn, table, ddl):
    """ALTER TABLE <table> ADD COLUMN <ddl>, unless the column is there already.

    Several connections can open a database that still lacks the column at the same time (the web server's first
    requests, the local app's sync thread next to an import). Each sees it missing and adds it; the slower one then
    gets "duplicate column name", which only means another connection was first. Returns True when this call added it.
    """
    try:
        conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, ddl))
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return False
        raise
