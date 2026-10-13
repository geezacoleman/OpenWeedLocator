"""
NCNN model zips: hashing and extraction, shared by the networked controller's
upload route and the OWL's model download.

Two hashes exist in the field. Noktura hashes the zip file itself; the
networked controller hashes the extracted files (sorted by path, contents
concatenated) because it deletes the zip after upload. The OWL accepts
either, zip bytes first.
"""

import hashlib
import os
import shutil
import zipfile
from pathlib import Path

CHUNK = 65536


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(CHUNK), b''):
            h.update(chunk)
    return h.hexdigest()


def dir_sha256(path):
    """The controller's directory hash: every file under `path`, sorted by
    path, contents concatenated (names are not hashed)."""
    h = hashlib.sha256()
    for fp in sorted(Path(path).rglob('*')):
        if fp.is_file():
            with open(fp, 'rb') as f:
                for chunk in iter(lambda: f.read(CHUNK), b''):
                    h.update(chunk)
    return h.hexdigest()


def _single_top_folder(names):
    """'model/' when every member sits under one top-level folder, else ''."""
    tops = {name.split('/', 1)[0] if '/' in name else '' for name in names}
    if len(tops) == 1 and '' not in tops:
        return tops.pop() + '/'
    return ''


def extract_model_zip(zip_path, dest_dir):
    """Extract into dest_dir (created), dropping a single top-level folder
    so `yolo_ncnn_model/model.param` lands as `model.param`. Members that
    would land outside dest_dir are refused. Raises zipfile.BadZipFile or
    ValueError."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path, 'r') as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        prefix = _single_top_folder([m.filename for m in members])
        for member in members:
            name = member.filename[len(prefix):]
            if not name:
                continue
            target = (dest / name).resolve()
            if root not in target.parents:
                raise ValueError(f'unsafe path in zip: {member.filename!r}')
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
    if not any(p.is_file() for p in dest.rglob('*')):
        raise ValueError('the zip is empty')


def replace_dir(staging_dir, target_dir):
    """Move staging_dir into place as target_dir. The old folder is renamed
    aside (a dot name, so model listings skip it) and only deleted once the
    new one is in place."""
    old = None
    if os.path.isdir(target_dir):
        parent, name = os.path.split(target_dir)
        old = os.path.join(parent, f'.{name}.old')
        if os.path.exists(old):
            shutil.rmtree(old)
        os.rename(target_dir, old)
    try:
        os.rename(staging_dir, target_dir)
    except OSError:
        if old:
            os.rename(old, target_dir)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)
