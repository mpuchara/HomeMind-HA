"""Connection-local SQLite storage policy shared by all application writers."""

# Recycle ordinary WAL allocation, but release the high-water mark after a large
# batch when SQLite safely resets the log. This is not a limit on active frames.
WAL_RETAIN_BYTES = 16 * 1024 * 1024
