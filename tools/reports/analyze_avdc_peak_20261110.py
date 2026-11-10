"""Read saved counters only; emit aggregate peak evidence without document IDs."""
import argparse
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path


def analyze(root):
    host = list(csv.DictReader((root / 'host-load.csv').open(encoding='utf-8-sig')))
    cpu = list(csv.DictReader((root / 'cpu.csv').open()))
    ts = lambda text: dt.datetime.fromisoformat(text).timestamp()
    utc = lambda epoch: dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()
    indexed = [r for r in host if r['Phase'] == 'indexing']
    peak = max(indexed, key=lambda r: float(r['HostCPU']))
    when = ts(peak['Time'])
    intervals = []
    for a, b in zip(cpu, cpu[1:]):
        start, end = float(a['time']), float(b['time'])
        elapsed = end - start
        assert elapsed > 0
        if not when - 15 <= end <= when + 15:
            continue
        rates = {k: (float(b[k]) - float(a[k])) / 1e6 / elapsed for k in ['embedding', 'other', 'parser']}
        assert all(x >= 0 for x in rates.values())
        workers = [(float(b[f'worker{i}']) - float(a[f'worker{i}'])) / 1e6 / elapsed for i in range(1, 23)]
        intervals.append({
            'start_utc': utc(start), 'end_utc': utc(end),
            'end_relative_to_peak_seconds': end - when,
            'contains_host_peak_timestamp': start <= when <= end,
            'cpu_time_equivalents': rates,
            'embedding_share_of_96_percent': rates['embedding'] / 96 * 100,
            'all_containers_share_of_96_percent': (rates['embedding'] + rates['other']) / 96 * 100,
            'workers_above_0_1_cpu': sum(x > .1 for x in workers),
            'worker_cpu_min': min(workers), 'worker_cpu_max': max(workers),
        })
    aligned = [r for r in intervals if r['contains_host_peak_timestamp']]
    assert len(aligned) == 1 and aligned[0]['workers_above_0_1_cpu'] == 22
    host_near = [{
        'utc': r['Time'], 'cpu_percent': float(r['HostCPU']),
        'groupware_cpu_integer_percent': int(r['GroupWareCPU']),
        'http_ms': float(r['HttpSeconds']) * 1000, 'http_code': int(r['HttpCode']),
        'disk_queue': int(r['DiskQueue']),
    } for r in host if abs(ts(r['Time']) - when) < 13]
    http_peak = max(indexed, key=lambda r: float(r['HttpSeconds']))
    return {
        'schema': 'docmind-avdc-cpu-peak-v1', 'source_run': root.name,
        'analysis_mode': 'saved_logs_only_no_reindex', 'host_logical_processors': 96,
        'host_peak_utc': peak['Time'], 'host_peak_kst': dt.datetime.fromtimestamp(when,dt.timezone(dt.timedelta(hours=9))).isoformat(),
        'seconds_since_first_registration': when - ts(json.loads((root/'result.json').read_text())['start_utc']),
        'host_peak_percent': float(peak['HostCPU']), 'aligned_vm_interval': aligned[0],
        'host_neighbors': host_near, 'vm_intervals': intervals,
        'http_max_separate_sample': {'utc': http_peak['Time'], 'http_ms':float(http_peak['HttpSeconds'])*1000, 'host_cpu_percent':float(http_peak['HostCPU'])},
        'limitations': ['Host and VM sampling intervals/counters are different; clocks were not independently recalibrated.', 'Other includes parser; do not add parser twice.', 'Worker activity is CPU consumed over an interval, not a request concurrency trace.', 'No per-request/token timeline or operator profile in this saved subset; exact triggering request is not identified.'],
        'source_sha256': {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ['host-load.csv','cpu.csv','sampler.py','audit-summary.json','result.json']},
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('saved_run', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    data = analyze(args.saved_run)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    print('Aggregate evidence written; host peak and 22-worker burst matched.')
