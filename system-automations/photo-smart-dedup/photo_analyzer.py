import os
import sys
import hashlib
import json
import re
from datetime import datetime
from collections import defaultdict

def get_quick_sample_hash(filepath, size):
    """Hash first 16KB and last 16KB plus size for extremely fast pre-filtering."""
    try:
        hasher = hashlib.md5()
        hasher.update(str(size).encode())
        with open(filepath, 'rb') as f:
            if size <= 32768:
                hasher.update(f.read())
            else:
                hasher.update(f.read(16384))
                f.seek(size - 16384)
                hasher.update(f.read(16384))
        return hasher.hexdigest()
    except Exception:
        return None

def get_full_md5(filepath, block_size=1024*1024):
    hasher = hashlib.md5()
    try:
        with open(filepath, 'rb') as f:
            while chunk := f.read(block_size):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None

def parse_filename_time(filename):
    """Extract standard datetime from smartphone photo/video filename conventions."""
    # Pattern: IMG_20240826_124233 / 20240826_124233 / VID_20240826_124233
    m = re.search(r'(?:IMG_|VID_|PANO_|Screenshot_|wx_camera_)?(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', filename, re.IGNORECASE)
    if m:
        try:
            year, month, day, hour, minute, second = map(int, m.groups())
            if 2010 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59:
                return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"
        except Exception:
            pass
    # Pattern: 13-digit millisecond timestamp (e.g. mmexport1698765432123)
    m = re.search(r'mmexport(\d{13})', filename, re.IGNORECASE)
    if m:
        try:
            ts = int(m.group(1)) / 1000.0
            dt = datetime.fromtimestamp(ts)
            if 2010 <= dt.year <= 2030:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return None

def extract_exif_time(filepath):
    """Extract EXIF DateTimeOriginal from JPEG files."""
    try:
        with open(filepath, 'rb') as f:
            header = f.read(2)
            if header != b'\xff\xd8':
                return None
            while True:
                marker_bytes = f.read(2)
                if len(marker_bytes) < 2 or marker_bytes[0] != 0xff:
                    break
                marker = marker_bytes[1]
                if marker in (0xd8, 0xd9):
                    continue
                length_bytes = f.read(2)
                if len(length_bytes) < 2:
                    break
                length = int.from_bytes(length_bytes, 'big') - 2
                if marker == 0xe1: # APP1
                    data = f.read(length)
                    if data.startswith(b'Exif\x00\x00'):
                        tiff_data = data[6:]
                        m = re.search(rb'(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})', tiff_data)
                        if m:
                            return m.group(0).decode('ascii').replace(':', '-', 2)
                    break
                else:
                    f.seek(length, 1)
    except Exception:
        pass
    return None

def main():
    backup_root = "/volume2/homes/zhangzheren/Photos/MobileBackup"
    if not os.path.exists(backup_root):
        print(f"Error: {backup_root} does not exist!", flush=True)
        return

    print(f"=== Scanning Photo Backups: {backup_root} ===", flush=True)
    devices = [d for d in os.listdir(backup_root) if os.path.isdir(os.path.join(backup_root, d))]
    print(f"Discovered device folders: {devices}\n", flush=True)

    all_files = []
    device_stats = defaultdict(lambda: {'count': 0, 'bytes': 0, 'extensions': defaultdict(int)})

    for dev in sorted(devices):
        dev_dir = os.path.join(backup_root, dev)
        for root, dirs, files in os.walk(dev_dir):
            # Crucial: ignore Synology thumbnail directories and hidden folders!
            dirs[:] = [d for d in dirs if not d.startswith('@') and not d.startswith('.')]
            for fname in files:
                if fname.startswith('@') or fname.startswith('.'):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    st = os.stat(fpath)
                    if st.st_size == 0:
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    device_stats[dev]['count'] += 1
                    device_stats[dev]['bytes'] += st.st_size
                    device_stats[dev]['extensions'][ext] += 1

                    rel_path = os.path.relpath(fpath, backup_root)
                    all_files.append({
                        'device': dev,
                        'name': fname,
                        'path': fpath,
                        'rel': rel_path,
                        'size': st.st_size,
                        'mtime': st.st_mtime,
                        'ext': ext
                    })
                except Exception:
                    pass

    print("=== DEVICE INVENTORY ===", flush=True)
    for dev, st in device_stats.items():
        gb = st['bytes'] / (1024**3)
        top_ext = sorted(st['extensions'].items(), key=lambda x: x[1], reverse=True)[:5]
        ext_str = ", ".join([f"{k}: {v}" for k, v in top_ext])
        print(f"Device: {dev:12s} | Files: {st['count']:5d} | Size: {gb:6.2f} GB | Top Ext: {ext_str}", flush=True)
    print(f"Total user media files: {len(all_files)}\n", flush=True)

    # -------------------------------------------------------------
    # 1. Exact Bit-for-Bit Duplicates
    # -------------------------------------------------------------
    print(">>> Stage 1: Grouping by size & sample hash...", flush=True)
    size_groups = defaultdict(list)
    for f in all_files:
        size_groups[f['size']].append(f)

    # Candidate files with identical size
    size_candidates = []
    for s, flist in size_groups.items():
        if len(flist) > 1:
            size_candidates.extend(flist)

    print(f"Files with identical size: {len(size_candidates)}", flush=True)

    sample_groups = defaultdict(list)
    for f in size_candidates:
        sh = get_quick_sample_hash(f['path'], f['size'])
        if sh:
            sample_groups[(f['size'], sh)].append(f)

    full_hash_candidates = []
    for (sz, sh), flist in sample_groups.items():
        if len(flist) > 1:
            full_hash_candidates.extend(flist)

    print(f"Files with identical sample hash: {len(full_hash_candidates)}", flush=True)

    # Compute full MD5 for remaining candidates
    md5_groups = defaultdict(list)
    for idx, f in enumerate(full_hash_candidates):
        h = get_full_md5(f['path'])
        if h:
            f['md5'] = h
            md5_groups[h].append(f)

    exact_duplicates = {h: flist for h, flist in md5_groups.items() if len(flist) > 1}
    exact_dup_file_count = sum(len(flist) - 1 for flist in exact_duplicates.values())
    exact_dup_bytes = sum(flist[0]['size'] * (len(flist) - 1) for flist in exact_duplicates.values())

    print(f"\n[Result] EXACT BIT-FOR-BIT DUPLICATES:", flush=True)
    print(f"  Exact Duplicate Groups : {len(exact_duplicates)}", flush=True)
    print(f"  Redundant Files        : {exact_dup_file_count}", flush=True)
    print(f"  Reclaimable Space      : {exact_dup_bytes / (1024**2):.2f} MB ({exact_dup_bytes / (1024**3):.3f} GB)", flush=True)

    # -------------------------------------------------------------
    # 2. Filename Overlap & Cross-Device Migration
    # -------------------------------------------------------------
    print("\n>>> Stage 2: Cross-Device Filename Overlap Analysis...", flush=True)
    dev_names = sorted(list(device_stats.keys()))
    dev_file_map = {}
    for dev in dev_names:
        dev_file_map[dev] = {f['name'].lower(): f for f in all_files if f['device'] == dev}

    pair_overlaps = []
    for i in range(len(dev_names)):
        for j in range(i + 1, len(dev_names)):
            d1, d2 = dev_names[i], dev_names[j]
            names1 = set(dev_file_map[d1].keys())
            names2 = set(dev_file_map[d2].keys())
            common = names1 & names2
            identical_size = sum(1 for n in common if dev_file_map[d1][n]['size'] == dev_file_map[d2][n]['size'])
            d1_total_bytes = sum(dev_file_map[d1][n]['size'] for n in common)
            d2_total_bytes = sum(dev_file_map[d2][n]['size'] for n in common)
            pair_overlaps.append({
                'pair': (d1, d2),
                'common_count': len(common),
                'identical_size_count': identical_size,
                'd1_size_mb': round(d1_total_bytes / (1024**2), 1),
                'd2_size_mb': round(d2_total_bytes / (1024**2), 1),
                'common_names': sorted(list(common))
            })
            print(f"  Overlap [{d1}] <-> [{d2}]:", flush=True)
            print(f"    - Same Filename Count : {len(common)} files", flush=True)
            print(f"    - Exact Same Size     : {identical_size} files", flush=True)
            print(f"    - Total volume in {d1}: {d1_total_bytes / (1024**3):.2f} GB", flush=True)
            print(f"    - Total volume in {d2}: {d2_total_bytes / (1024**3):.2f} GB", flush=True)

    # -------------------------------------------------------------
    # 3. Timestamp Matching for Re-encoded / Re-saved Migrated Photos
    # -------------------------------------------------------------
    print("\n>>> Stage 3: Shooting Timestamp Matching across devices...", flush=True)
    for f in all_files:
        fn_time = parse_filename_time(f['name'])
        exif_time = None
        if f['ext'] in ('.jpg', '.jpeg'):
            exif_time = extract_exif_time(f['path'])
        f['shot_time'] = exif_time or fn_time

    time_groups = defaultdict(list)
    for f in all_files:
        if f['shot_time']:
            time_groups[f['shot_time']].append(f)

    # Filter to groups with files from at least 2 distinct devices
    cross_dev_time_dups = []
    cross_dev_reclaimable_bytes = 0
    cross_dev_redundant_files = 0

    for stime, flist in time_groups.items():
        dev_set = set(f['device'] for f in flist)
        if len(dev_set) > 1:
            # Sort by file size descending (keep highest quality)
            flist_sorted = sorted(flist, key=lambda x: x['size'], reverse=True)
            # The extra files beyond the largest one
            redundant = flist_sorted[1:]
            cross_dev_redundant_files += len(redundant)
            cross_dev_reclaimable_bytes += sum(f['size'] for f in redundant)
            cross_dev_time_dups.append({
                'time': stime,
                'files': [{'device': f['device'], 'name': f['name'], 'rel': f['rel'], 'size': f['size']} for f in flist_sorted]
            })

    print(f"\n[Result] CROSS-DEVICE MIGRATED / TIMESTAMP DUPLICATES:", flush=True)
    print(f"  Cross-Device Timestamp Matches: {len(cross_dev_time_dups)} groups", flush=True)
    print(f"  Redundant Copies Across Devices: {cross_dev_redundant_files} files", flush=True)
    print(f"  Potential Reclaimable Space   : {cross_dev_reclaimable_bytes / (1024**3):.2f} GB", flush=True)

    # Print 5 concrete samples
    print("\nSamples of Cross-Device Matches:", flush=True)
    for grp in cross_dev_time_dups[:5]:
        print(f"  Timestamp: {grp['time']}", flush=True)
        for f in grp['files']:
            print(f"    * [{f['device']}] {f['name']} ({f['size'] / (1024**2):.2f} MB)", flush=True)

    # Save detailed JSON for analysis
    output_path = "/tmp/photo_duplicate_report.json"
    report = {
        'timestamp': datetime.now().isoformat(),
        'backup_root': backup_root,
        'devices': {d: {'count': st['count'], 'gb': round(st['bytes'] / (1024**3), 2)} for d, st in device_stats.items()},
        'exact_duplicates': {
            'count': exact_dup_file_count,
            'reclaimable_bytes': exact_dup_bytes,
            'reclaimable_mb': round(exact_dup_bytes / (1024**2), 2),
            'groups': [
                [{'device': f['device'], 'rel': f['rel'], 'size': f['size']} for f in flist]
                for flist in list(exact_duplicates.values())
            ]
        },
        'cross_device_overlaps': pair_overlaps,
        'cross_device_time_duplicates': {
            'count': cross_dev_redundant_files,
            'reclaimable_bytes': cross_dev_reclaimable_bytes,
            'reclaimable_gb': round(cross_dev_reclaimable_bytes / (1024**3), 2),
            'samples': cross_dev_time_dups[:30]
        }
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nFull structured report saved to {output_path}", flush=True)

if __name__ == '__main__':
    main()
