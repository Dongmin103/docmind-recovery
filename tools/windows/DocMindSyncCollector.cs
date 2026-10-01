using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace DocMind
{
    // Runs on FileSystemWatcher's native callback threads, independently of the
    // PowerShell hash/transmission loop. No hashes or network calls occur here.
    public sealed class SyncCollector : IDisposable
    {
        private static readonly HashSet<string> LocalOwners = new HashSet<string>();
        private readonly object callbacks = new object();
        private readonly SyncQueue queue;
        private readonly string source, prefix;
        private readonly Mutex ownership;
        private FileSystemWatcher watcher;
        private bool disposed, owns;
        public volatile bool Faulted;

        public SyncCollector(SyncQueue queue, string source, string root)
        {
            this.queue = queue; this.source = source;
            prefix = Path.GetFullPath(root).TrimEnd('\\', '/') + Path.DirectorySeparatorChar;
            string name;
            using (SHA256 sha = SHA256.Create())
                name = "Global\\DocMindSyncSource." + BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(source))).Replace("-", "");
            lock (LocalOwners)
            {
                if (LocalOwners.Contains(source)) throw new IOException("SOURCE_ALREADY_WATCHED");
                ownership = new Mutex(false, name);
                try { owns = ownership.WaitOne(0); }
                catch (AbandonedMutexException) { owns = true; }
                if (!owns) { ownership.Dispose(); throw new IOException("SOURCE_ALREADY_WATCHED"); }
                LocalOwners.Add(source);
            }
            try
            {
                if ((File.GetAttributes(root) & FileAttributes.ReparsePoint) != 0) throw new IOException("SOURCE_REPARSE_POINT");
                watcher = new FileSystemWatcher(root);
                watcher.IncludeSubdirectories = true;
                watcher.NotifyFilter = NotifyFilters.FileName | NotifyFilters.DirectoryName | NotifyFilters.LastWrite | NotifyFilters.Size | NotifyFilters.CreationTime;
                watcher.InternalBufferSize = 65536;
                watcher.Created += Changed;
                watcher.Changed += Changed;
                watcher.Deleted += Changed;
                watcher.Renamed += Renamed;
                watcher.Error += Error;
                watcher.EnableRaisingEvents = true;
            }
            catch { Dispose(); throw; }
        }

        private string Relative(string fullPath)
        {
            string full = Path.GetFullPath(fullPath);
            if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new IOException("SOURCE_PATH_ESCAPED");
            return SyncQueue.NormalizePath(full.Substring(prefix.Length));
        }

        private void Recovery()
        {
            Faulted = true;
            try { queue.EnqueueScope(source, "", null, "recovery"); }
            catch { /* Faulted stays set so the owning loop can persist recovery. */ }
        }

        private void Changed(object sender, FileSystemEventArgs args)
        {
            lock (callbacks)
            {
                if (disposed) return;
                try
                {
                    string path = Relative(args.FullPath);
                    if (Directory.Exists(args.FullPath))
                    {
                        // Directory mtime changes accompany ordinary edits. They
                        // must never expand a full folder for each changed file.
                        if (args.ChangeType == WatcherChangeTypes.Created) queue.EnqueueScope(source, path, null, "create");
                        return;
                    }
                    string kind = args.ChangeType == WatcherChangeTypes.Deleted ? "delete" : "upsert";
                    queue.Enqueue(source, path, kind, null, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
                    // Deleted events carry no file/directory discriminator. A
                    // deleted directory is recovered by the scheduled scan;
                    // ordinary file deletion must not expand the whole source.
                }
                catch { Recovery(); }
            }
        }

        private void Renamed(object sender, RenamedEventArgs args)
        {
            lock (callbacks)
            {
                if (disposed) return;
                try
                {
                    string path = Relative(args.FullPath), old = Relative(args.OldFullPath);
                    if (Directory.Exists(args.FullPath)) queue.EnqueueScope(source, path, old, "move");
                    else queue.Enqueue(source, path, "move", old, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
                }
                catch { Recovery(); }
            }
        }

        private void Error(object sender, ErrorEventArgs args)
        {
            lock (callbacks) { if (!disposed) Recovery(); }
        }

        public void Dispose()
        {
            lock (callbacks)
            {
                if (disposed) return;
                disposed = true;
                if (watcher != null) { watcher.EnableRaisingEvents = false; watcher.Dispose(); }
                if (owns)
                {
                    ownership.ReleaseMutex(); ownership.Dispose(); owns = false;
                    lock (LocalOwners) LocalOwners.Remove(source);
                }
            }
        }
    }
}
