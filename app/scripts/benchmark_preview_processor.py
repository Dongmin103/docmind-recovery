"""Metadata-only HWP smoke. Run with the production limits and /fixtures read-only."""
import json
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, '/opt/docmind-preview')
import processor

ROOT = Path('/run/docmind-previews')
CGROUP = Path('/sys/fs/cgroup')


def main():
    results = []
    for fixture in sorted(Path('/fixtures').iterdir()):
        kind = fixture.suffix.lstrip('.').lower()
        if kind not in {'hwp', 'hwpx'}:
            continue
        directory = ROOT / ('smoke-' + kind)
        directory.mkdir(mode=0o700)
        source = directory / ('input.' + kind)
        peak = {'memory_bytes': 0, 'tmpfs_bytes': 0, 'pids': 0}
        stop = threading.Event()

        def sample(stop=stop, peak=peak):
            while not stop.wait(0.02):
                peak['memory_bytes'] = max(peak['memory_bytes'], int((CGROUP / 'memory.current').read_text()))
                peak['pids'] = max(peak['pids'], int((CGROUP / 'pids.current').read_text()))
                peak['tmpfs_bytes'] = max(peak['tmpfs_bytes'], shutil.disk_usage(ROOT).used)

        monitor = threading.Thread(target=sample, daemon=True)
        monitor.start()
        cpu_before = (CGROUP / 'cpu.stat').read_text()
        started = time.perf_counter()
        try:
            shutil.copyfile(fixture, source)
            result = processor.process(directory.name, str(source), kind, str(directory))
            timings = [{'page': 1, 'milliseconds': round((time.perf_counter() - started) * 1000, 1)}]
            count = result['page_count']
            for number in ([2, 1] if count > 1 else [1]):
                started = time.perf_counter()
                page = processor.page(directory.name, str(source), kind, str(directory), number)
                current = Path(page['page_path'])
                for previous in directory.glob('page-*.svg'):
                    if previous != current:
                        previous.unlink()
                timings.append({'page': number, 'milliseconds': round((time.perf_counter() - started) * 1000, 1)})
                assert len(list(directory.glob('page-*.svg'))) == 1
            results.append({'format': kind, 'input_bytes': source.stat().st_size, 'pages': count,
                            'timings': timings, 'peak_sampled': peak, 'status': 'PASS',
                            'cpu_before': cpu_before, 'cpu_after': (CGROUP / 'cpu.stat').read_text()})
        except processor.PreviewError as error:
            results.append({'format': kind, 'status': str(error), 'peak_sampled': peak})
        finally:
            stop.set()
            monitor.join()
            shutil.rmtree(directory)
    limits = {name: (CGROUP / name).read_text().strip() for name in ('memory.max', 'memory.swap.max', 'cpu.max', 'pids.max')}
    print(json.dumps({'limits': limits, 'samples': results, 'remaining_entries': [p.name for p in ROOT.iterdir()]}, indent=2))
    return 0 if results and all(row['status'] == 'PASS' for row in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
