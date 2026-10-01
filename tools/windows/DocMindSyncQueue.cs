using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace DocMind
{
    public sealed class SyncEntry
    {
        public string SourceId, RelativePath, PathKey, Kind, OldRelativePath;
        public long Generation, LastChangedAt, DueAt;
        public int StableCount;
    }

    public sealed class SyncOutbox
    {
        public string SourceId, RequestId, Payload;
        public long Epoch, Sequence, NextAttemptAt;
        public int Attempts;
        public bool Held;
    }

    // The host owns this file outside watched roots. All values are bound, and
    // one connection serializes event capture against freezing/acknowledgement.
    public sealed class SyncQueue : IDisposable
    {
        private IntPtr db;
        private readonly object gate = new object();
        private static readonly IntPtr Transient = new IntPtr(-1);
        private const int Row = 100, Done = 101;

        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_open_v2(byte[] filename, out IntPtr handle, int flags, IntPtr vfs);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_close_v2(IntPtr handle);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_busy_timeout(IntPtr handle, int ms);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_prepare_v2(IntPtr handle, byte[] sql, int length, out IntPtr statement, IntPtr tail);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_bind_text(IntPtr statement, int index, byte[] value, int length, IntPtr destructor);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_bind_int64(IntPtr statement, int index, long value);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_bind_null(IntPtr statement, int index);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_step(IntPtr statement);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_finalize(IntPtr statement);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_column_count(IntPtr statement);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_column_type(IntPtr statement, int column);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern long sqlite3_column_int64(IntPtr statement, int column);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern IntPtr sqlite3_column_text(IntPtr statement, int column);
        [DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
        private static extern int sqlite3_column_bytes(IntPtr statement, int column);

        private static byte[] Utf8(string text) { return Encoding.UTF8.GetBytes(text + "\0"); }
        private static void Check(int code)
        {
            if (code != 0) throw new IOException("SQLite operation failed (code " + code + ").");
        }

        private List<object[]> Query(string sql, params object[] values)
        {
            if (db == IntPtr.Zero) throw new ObjectDisposedException("SyncQueue");
            IntPtr statement;
            Check(sqlite3_prepare_v2(db, Utf8(sql), -1, out statement, IntPtr.Zero));
            try
            {
                for (int i = 0; i < values.Length; i++)
                {
                    object value = values[i];
                    if (value == null) Check(sqlite3_bind_null(statement, i + 1));
                    else if (value is string) Check(sqlite3_bind_text(statement, i + 1, Utf8((string)value), -1, Transient));
                    else Check(sqlite3_bind_int64(statement, i + 1, Convert.ToInt64(value)));
                }
                List<object[]> rows = new List<object[]>();
                int result;
                while ((result = sqlite3_step(statement)) == Row)
                {
                    object[] row = new object[sqlite3_column_count(statement)];
                    for (int i = 0; i < row.Length; i++)
                    {
                        int type = sqlite3_column_type(statement, i);
                        if (type == 5) row[i] = null;
                        else if (type == 1) row[i] = sqlite3_column_int64(statement, i);
                        else
                        {
                            byte[] bytes = new byte[sqlite3_column_bytes(statement, i)];
                            Marshal.Copy(sqlite3_column_text(statement, i), bytes, 0, bytes.Length);
                            row[i] = Encoding.UTF8.GetString(bytes);
                        }
                    }
                    rows.Add(row);
                }
                if (result != Done) Check(result);
                return rows;
            }
            finally { sqlite3_finalize(statement); }
        }

        private void Transaction(Action action)
        {
            Query("BEGIN IMMEDIATE");
            try { action(); Query("COMMIT"); }
            catch { Query("ROLLBACK"); throw; }
        }

        public SyncQueue(string databasePath)
        {
            string fullPath = Path.GetFullPath(databasePath);
            Directory.CreateDirectory(Path.GetDirectoryName(fullPath));
            int code = sqlite3_open_v2(Utf8(fullPath), out db, 2 | 4 | 0x10000, IntPtr.Zero);
            if (code != 0) { if (db != IntPtr.Zero) sqlite3_close_v2(db); db = IntPtr.Zero; Check(code); }
            try
            {
                Check(sqlite3_busy_timeout(db, 5000));
                Query("PRAGMA journal_mode=WAL");
                Query("PRAGMA synchronous=FULL");
                Query("CREATE TABLE IF NOT EXISTS counter(id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL)");
                Query("INSERT OR IGNORE INTO counter VALUES(1,0)");
                Query("CREATE TABLE IF NOT EXISTS pending(source TEXT NOT NULL,path_key TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,old_path TEXT,generation INTEGER NOT NULL,changed_at INTEGER NOT NULL,due_at INTEGER NOT NULL,stable_count INTEGER NOT NULL DEFAULT 0,dirty_sent INTEGER NOT NULL DEFAULT 0,state INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(source,path_key))");
                Query("CREATE INDEX IF NOT EXISTS pending_due ON pending(source,state,due_at)");
                Query("CREATE INDEX IF NOT EXISTS pending_dirty ON pending(source,state,dirty_sent)");
                Query("CREATE TABLE IF NOT EXISTS outbox(source TEXT PRIMARY KEY,epoch INTEGER NOT NULL,sequence INTEGER NOT NULL,request_id TEXT NOT NULL,payload TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at INTEGER NOT NULL DEFAULT 0,held INTEGER NOT NULL DEFAULT 0)");
                Query("CREATE TABLE IF NOT EXISTS outbox_item(source TEXT NOT NULL,ordinal INTEGER NOT NULL,path_key TEXT NOT NULL,generation INTEGER NOT NULL,PRIMARY KEY(source,ordinal))");
            }
            catch { Dispose(); throw; }
        }

        public static string NormalizePath(string path)
        {
            if (path == null) throw new ArgumentException("Relative path required.");
            path = path.Trim().Replace('\\', '/').Normalize(NormalizationForm.FormC);
            if (path.Length == 0 || path.Length > 1024 || path[0] == '/' || path.IndexOf(':') >= 0 || path.IndexOf('\0') >= 0)
                throw new ArgumentException("Invalid relative path.");
            foreach (string part in path.Split('/'))
                if (part.Length == 0 || part == "." || part == "..") throw new ArgumentException("Invalid relative path.");
            return path;
        }

        public long Enqueue(string source, string path, string kind, string oldPath, long now)
        {
            if (String.IsNullOrWhiteSpace(source)) throw new ArgumentException("Source required.");
            if (kind != "upsert" && kind != "delete" && kind != "move") throw new ArgumentException("Invalid change kind.");
            path = NormalizePath(path);
            // PowerShell marshals a null string argument as String.Empty.
            oldPath = String.IsNullOrEmpty(oldPath) ? null : NormalizePath(oldPath);
            string key = path.ToUpperInvariant();
            lock (gate)
            {
                long generation = 0;
                Transaction(delegate
                {
                    Query("UPDATE counter SET generation=generation+1 WHERE id=1");
                    generation = (long)Query("SELECT generation FROM counter WHERE id=1")[0][0];
                    // Preserve the earliest old path across autosave notifications.
                    List<object[]> previous = Query("SELECT old_path FROM pending WHERE source=? AND path_key=?", source, key);
                    if (oldPath == null && previous.Count != 0) oldPath = (string)previous[0][0];
                    Query("INSERT OR REPLACE INTO pending(source,path_key,path,kind,old_path,generation,changed_at,due_at) VALUES(?,?,?,?,?,?,?,?)",
                        source, key, path, kind, oldPath, generation, now, kind == "delete" ? now : checked(now + 120000));
                });
                return generation;
            }
        }

        private SyncEntry[] Entries(string source, long now, int limit, bool dirty)
        {
            if (limit < 1 || limit > 250) throw new ArgumentOutOfRangeException("limit");
            string condition = dirty ? "dirty_sent=0" : "due_at<=?";
            string sql = "SELECT source,path_key,path,kind,old_path,generation,changed_at,due_at,stable_count FROM pending WHERE source=? AND state=0 AND " + condition + " AND NOT EXISTS(SELECT 1 FROM outbox WHERE source=?) ORDER BY due_at,path_key LIMIT ?";
            List<object[]> rows = dirty ? Query(sql, source, source, limit) : Query(sql, source, now, source, limit);
            List<SyncEntry> result = new List<SyncEntry>();
            foreach (object[] row in rows)
                result.Add(new SyncEntry { SourceId = (string)row[0], PathKey = (string)row[1], RelativePath = (string)row[2], Kind = (string)row[3], OldRelativePath = (string)row[4], Generation = (long)row[5], LastChangedAt = (long)row[6], DueAt = (long)row[7], StableCount = (int)(long)row[8] });
            return result.ToArray();
        }

        public SyncEntry[] Due(string source, long now, int limit) { lock (gate) { return Entries(source, now, limit, false); } }
        public SyncEntry[] Dirty(string source, int limit) { lock (gate) { return Entries(source, 0, limit, true); } }
        public long Count(string source) { lock (gate) { return (long)Query("SELECT count(*) FROM pending WHERE source=?", source)[0][0]; } }

        public void Delay(SyncEntry entry, long now, long milliseconds, int stableCount)
        {
            lock (gate)
                Query("UPDATE pending SET due_at=?,stable_count=? WHERE source=? AND path_key=? AND generation=?",
                    checked(now + milliseconds), stableCount, entry.SourceId, entry.PathKey, entry.Generation);
        }

        public bool Freeze(string source, long epoch, long sequence, string requestId, string payload, SyncEntry[] entries)
        {
            if (entries == null || entries.Length == 0 || entries.Length > 250 || Encoding.UTF8.GetByteCount(payload) > 1048576)
                throw new ArgumentException("Invalid outbox batch.");
            lock (gate)
            {
                bool frozen = false;
                Transaction(delegate
                {
                    if (Query("SELECT source FROM outbox WHERE source=?", source).Count != 0) return;
                    HashSet<string> seen = new HashSet<string>();
                    foreach (SyncEntry entry in entries)
                    {
                        if (entry.SourceId != source || !seen.Add(entry.PathKey)) throw new ArgumentException("Invalid outbox identity.");
                        List<object[]> current = Query("SELECT generation FROM pending WHERE source=? AND path_key=? AND state=0", source, entry.PathKey);
                        if (current.Count == 0 || (long)current[0][0] != entry.Generation) return;
                    }
                    Query("INSERT INTO outbox(source,epoch,sequence,request_id,payload) VALUES(?,?,?,?,?)", source, epoch, sequence, requestId, payload);
                    for (int i = 0; i < entries.Length; i++)
                        Query("INSERT INTO outbox_item VALUES(?,?,?,?)", source, i, entries[i].PathKey, entries[i].Generation);
                    frozen = true;
                });
                return frozen;
            }
        }

        public SyncOutbox Outbox(string source)
        {
            lock (gate)
            {
                List<object[]> rows = Query("SELECT epoch,sequence,request_id,payload,attempts,next_attempt_at,held FROM outbox WHERE source=?", source);
                if (rows.Count == 0) return null;
                object[] row = rows[0];
                return new SyncOutbox { SourceId = source, Epoch = (long)row[0], Sequence = (long)row[1], RequestId = (string)row[2], Payload = (string)row[3], Attempts = (int)(long)row[4], NextAttemptAt = (long)row[5], Held = (long)row[6] != 0 };
            }
        }

        // outcomes: 0=dirty stored, 1=observe again, 2=complete, 3=held.
        // Only call after validating the server signature and response identity.
        public void Acknowledge(string source, string requestId, int[] outcomes, long now)
        {
            lock (gate)
            {
                Transaction(delegate
                {
                    SyncOutbox box = Outbox(source);
                    if (box == null || box.RequestId != requestId) throw new InvalidOperationException("Outbox acknowledgement mismatch.");
                    List<object[]> entries = Query("SELECT path_key,generation FROM outbox_item WHERE source=? ORDER BY ordinal", source);
                    if (outcomes == null || outcomes.Length != entries.Count) throw new ArgumentException("Incomplete acknowledgement.");
                    for (int i = 0; i < entries.Count; i++)
                    {
                        string key = (string)entries[i][0]; long generation = (long)entries[i][1];
                        if (outcomes[i] == 0)
                            Query("UPDATE pending SET dirty_sent=1 WHERE source=? AND path_key=? AND generation=?", source, key, generation);
                        else if (outcomes[i] == 1)
                            Query("UPDATE pending SET dirty_sent=1,stable_count=1,due_at=? WHERE source=? AND path_key=? AND generation=?", checked(now + 10000), source, key, generation);
                        else if (outcomes[i] == 2)
                            Query("DELETE FROM pending WHERE source=? AND path_key=? AND generation=?", source, key, generation);
                        else if (outcomes[i] == 3)
                            Query("UPDATE pending SET state=1 WHERE source=? AND path_key=? AND generation=?", source, key, generation);
                        else throw new ArgumentException("Invalid acknowledgement outcome.");
                    }
                    Query("DELETE FROM outbox_item WHERE source=?", source);
                    Query("DELETE FROM outbox WHERE source=?", source);
                });
            }
        }

        public void Retry(string source, string requestId, long now, bool permanent)
        {
            lock (gate)
            {
                SyncOutbox box = Outbox(source);
                if (box == null || box.RequestId != requestId) throw new InvalidOperationException("Outbox retry mismatch.");
                long delay = Math.Min(300000L, 5000L * (1L << Math.Min(box.Attempts, 6)));
                Query("UPDATE outbox SET attempts=attempts+1,next_attempt_at=?,held=? WHERE source=? AND request_id=?",
                    checked(now + delay), permanent ? 1 : 0, source, requestId);
            }
        }

        public void Dispose()
        {
            lock (gate)
            {
                if (db != IntPtr.Zero) { Check(sqlite3_close_v2(db)); db = IntPtr.Zero; }
            }
        }
    }
}
