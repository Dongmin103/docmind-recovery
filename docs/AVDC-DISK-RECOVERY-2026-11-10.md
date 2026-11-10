# AVDC disk investigation and cleanup — 2026-11-10

The Ubuntu root filesystem reached 100% usage. Elasticsearch data occupied about 0.83 GiB, not most of the disk. At initial investigation, the VM had a 120 GiB virtual disk, but its root logical volume was approximately 58.5 GiB; approximately 58.47 GiB remained unallocated in the LVM volume group. The initial cleanup did not resize storage. The subsequent user-authorized expansion is recorded below. Elasticsearch disk thresholds were not changed.

## Main consumers before cleanup

- Docker/containerd image storage: about 18.2 GiB; active images were preserved.
- MySQL volume: about 18.4 GiB, predominantly binary transaction logs. Binary-log retention was 2,592,000 seconds (30 days). The largest document-related tables were only a few MiB each. This establishes the storage category, not the application operation responsible for every log event.
- Installation archives: 5,940,406,616 bytes.
- Archived duplicate experiment models: 2,510,186,936 bytes.
- Container logs and metadata: approximately 3 GiB. Historical application logs contained file-descriptor exhaustion errors; the API recovered after space was freed without an API restart. The root cause of those errors was not established in this task.
- Elasticsearch volume: about 0.83 GiB; preserved.

## Cleanup performed

1. Removed the two installed image archives only after SHA-256 matched the copies retained under `D:\DocMind\archive\avdc-experiments\cpu-noocr-20261102` (also present in the next day's package).
2. Removed the unused `docmind-embedding:2026.10.19-cpu` image tag using Docker after verifying no container referenced its image ID. Shared image layers were left to Docker; no manual containerd cleanup was performed.
3. Removed two archived experiment `model.bin` copies after SHA-256 matched the active model and Docker mount inspection confirmed those archive files were unused. The active OpenVINO model remains intact.
4. Archived closed MySQL binary logs 000001–000013 to `D:\DocMind\backups\avdc-disk-20261110\closed-binlogs-000001-000013.tar.gz`. All 13 files were verified against source SHA-256 hashes before using MySQL `PURGE BINARY LOGS TO 'binlog.000014'`. Source size: 3,314,339,940 bytes; compressed archive: 207,244,636 bytes. No replica was registered and the active log was 000050. Database rows and current logs were not deleted.

After cleanup, available root space was approximately 7.3 GiB and `df` usage was 87%, down from 100%. Existing document chunks and active chunk-set identity were checked before the search comparison.

## Elasticsearch recovery

Disk-full errors were recorded on November 7. On November 10, existing index reads worked, but cluster-management tasks had been queued for 3.4 days and new test-index creation timed out. With no actively processing ingestion/parser jobs observed, only the Elasticsearch container was restarted, preserving its data volume. It returned to green with zero pending tasks and 45,636 existing index entries, unchanged. Groupware and the Windows host were not restarted.

## Limits and follow-up

The resumed comparison completed all eight questions for both baseline and second candidate: visible evidence rank 1 was 8/8 in each. Candidate chunks decreased from 794 to 440; chunks under 48 tokens decreased from 533 to 44. A transient KURE read timeout interrupted the first attempt after three completed question pairs; only unfinished pairs were resumed. These calls are quality checks, not a controlled latency benchmark. The candidate was subsequently approved and [deployed to AVDC](AVDC-CHUNKING-DEPLOYMENT-2026-11-10.md); Kordoc and existing indices remain unchanged.

This cleanup is not a permanent capacity policy. MySQL retention and container log rotation remain unchanged. Continued log growth can consume space even after expansion. Search-quality evidence is recorded separately in `PDF-SHORT-HEADING-DOWNSTREAM-V2-2026-11-06.json`.

## Subsequent image cleanup and online expansion

The user authorized further unused-image cleanup and additional disk allocation on November 10.

- Removed the remaining `docmind-ovfull-base:20261106` reference to the unused CPU image after checking every container's image ID. Docker then deleted image `4c0a891dbb00`; active shared layers were preserved.
- Removed the unused `docmind-ovfull:20261106` alias. Its image remains available under the active `docmind-gte-openvino:20261106` tag.
- Saved LVM metadata and before/after inventories under `/opt/docmind-avdc/management/disk-expand-20261110`.
- Verified the root mount was ext4 on `/dev/ubuntu-vg/ubuntu-lv`, with sufficient free extents. Tested and executed `lvextend -L 100G`, then performed online `resize2fs`.
- Root logical volume: **58.47 → 100 GiB**. Remaining unallocated VG space: **16.95 GiB**. Hyper-V virtual disk remains **120 GiB**; no Windows partition or groupware storage was resized.
- After expansion, `df` reported approximately **99 GiB filesystem, 45 GiB used, 50 GiB available, 48% usage**.
- All container IDs remained unchanged during this operation. No service or host reboot was required. Production index and document preservation were checked after expansion.

The saved LVM metadata documents the previous layout; it is not a substitute for data backups and should not be used to shrink the now-expanded mounted filesystem.
