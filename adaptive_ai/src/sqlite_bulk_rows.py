"""Bounded multi-VALUES inserts through ordinary guarded connections."""

def insert_bounded_rows(connection, prefix, rows, width, suffix=''):
    """One VM step for a bounded VALUES group, with normal admission guards.

    sqlite3.executemany releases/reacquires the interpreter for every row.
    Tiny journal rows otherwise spend more time competing with inference than
    writing SQLite. Keep bind count below 999 and ordinary groups below 256 KiB;
    an individual large row still goes through the existing payload guard.
    """
    params, count, size = [], 0, 0
    def write():
        connection.execute(prefix+' VALUES '+','.join('('+','.join('?' for _ in range(width))+')' for _ in range(count))+suffix, params)
    for row in rows:
        row_size = sum(len(value.encode('utf-8')) if isinstance(value, str) else
                       len(value) if isinstance(value, (bytes, bytearray, memoryview)) else 16 for value in row)
        if count and (len(params)+width > 900 or size+row_size > 256*1024):
            write()
            params, count, size = [], 0, 0
        params.extend(row)
        count += 1
        size += row_size
    if count:
        write()
