"""
What is in models/, as models rather than files (docs/HAILO.md §3).

A model is every file and folder that shares a base name:

    weeds-v3.pt                  CPU build
    weeds-v3_ncnn_model/         CPU build (NCNN)
    weeds-v3_h8_hailo_model/     Hailo build (Ultralytics export: .hef + metadata.yaml)
    weeds-v3_h8l.hef             Hailo build, bare file (adopt_bare_hefs() wraps it)
    weeds-v3.json                optional sidecar: {"classes": [...], "run_on": "auto"}

Which build runs is decided here (build_order), not by file name: a Hailo
build's chip comes from the HEF itself (hailortcli parse-hef). A hailo8l HEF
runs on a Hailo-8 and an 8L; a hailo8 HEF runs only on a Hailo-8.

Shared by owl.py (what to load), mqtt_manager (state, downloads) and both
dashboards (upload, delete). Nothing here imports ultralytics or HailoRT.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

HAILO_ARCHS = ('hailo8', 'hailo8l')
# What each chip can run, best first
RUNNABLE = {'hailo8': ('hailo8', 'hailo8l'), 'hailo8l': ('hailo8l',)}
CHIP_NAMES = {'hailo8': 'Hailo-8', 'hailo8l': 'Hailo-8L', 'hailo10h': 'Hailo-10H'}
RUN_ON = ('auto', 'hailo', 'cpu')
DEFAULT_STORAGE_GB = 3

# Stripped from the end of a name, repeatedly, to get the base name
_SUFFIXES = ('.pt', '.hef', '_ncnn_model', '_hailo_model',
             '_hailo8l', '_hailo8', '_h8l', '_h8')
# Ultralytics only loads a Hailo folder whose name contains this
HAILO_FOLDER_SUFFIX = '_hailo_model'
PARSE_HEF_TIMEOUT_S = 10

# Warnings a model can carry (ai.models[].warning)
NO_LABELS = 'no_labels'
LABELS_MISMATCH = 'labels_mismatch'
NEEDS_8L_BUILD = 'needs_8l_build'


@dataclass
class HefInfo:
    arch: str = ''               # hailo8 | hailo8l | hailo10h | '' (unknown)
    imgsz: tuple = ()            # (h, w) of the input
    nms: bool = False            # NMS built into the HEF (boxes per class come out)
    num_classes: int = 0         # from the NMS output, 0 when unknown


@dataclass
class Build:
    path: Path
    kind: str                    # pt | ncnn | hailo | hef
    arch: str                    # cpu | hailo8 | hailo8l | hailo10h | '' (Hailo, chip unknown)
    size_bytes: int = 0

    @property
    def is_hailo(self):
        return self.kind in ('hailo', 'hef')

    @property
    def name(self):
        return self.path.name


@dataclass
class Model:
    name: str
    builds: list = field(default_factory=list)
    sidecar: dict = field(default_factory=dict)

    @property
    def run_on(self):
        value = str(self.sidecar.get('run_on', '')).strip().lower()
        return value if value in RUN_ON else None

    @property
    def cpu_build(self):
        """NCNN first (faster on a Pi), then .pt."""
        for kind in ('ncnn', 'pt'):
            for build in self.builds:
                if build.kind == kind:
                    return build
        return None

    @property
    def hailo_builds(self):
        return [b for b in self.builds if b.is_hailo]

    @property
    def size_bytes(self):
        return sum(b.size_bytes for b in self.builds)


def base_name(name):
    """'weeds-v3_h8l_hailo_model' -> 'weeds-v3'. Never returns ''."""
    stem = name
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if stem.lower().endswith(suffix) and len(stem) > len(suffix):
                stem = stem[:-len(suffix)]
                changed = True
    return stem


def _size(path):
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _hef_in(folder):
    return next(iter(sorted(folder.glob('*.hef'))), None)


# ---------------------------------------------------------------------------
# HEF inspection (hailortcli parse-hef: reads the file, never the device)
# ---------------------------------------------------------------------------

_hef_cache = {}


def parse_hef_output(text):
    """HefInfo from `hailortcli parse-hef` output."""
    info = HefInfo()
    for line in text.splitlines():
        if 'rchitecture' in line:
            m = re.search(r'HAILO-?(8L|8|10H|15H|15L)\b', line, re.IGNORECASE)
            if m:
                info.arch = 'hailo' + m.group(1).lower()
        if not info.imgsz:
            m = re.search(r'NHWC\((\d+)x(\d+)x\d+\)', line)
            if m and 'Input' in line:
                info.imgsz = (int(m.group(1)), int(m.group(2)))
        if 'NMS' in line:
            info.nms = True
            m = re.search(r'number of classes:\s*(\d+)', line, re.IGNORECASE)
            if m:
                info.num_classes = int(m.group(1))
    return info


def hef_info(hef_path, runner=None):
    """HefInfo for a .hef, cached per path and mtime. Empty when hailortcli is
    missing or fails (a CPU-only machine): the caller treats the chip as
    unknown."""
    hef_path = Path(hef_path)
    try:
        key = (str(hef_path), hef_path.stat().st_mtime_ns)
    except OSError:
        return HefInfo()
    if runner is None and key in _hef_cache:
        return _hef_cache[key]
    run = runner or subprocess.run
    if runner is None and not shutil.which('hailortcli'):
        return HefInfo()
    try:
        result = run(['hailortcli', 'parse-hef', str(hef_path)],
                     capture_output=True, text=True, timeout=PARSE_HEF_TIMEOUT_S)
        info = parse_hef_output((result.stdout or '') + '\n' + (result.stderr or ''))
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f'parse-hef failed for {hef_path.name}: {e}')
        info = HefInfo()
    if runner is None:
        _hef_cache[key] = info
    return info


# ---------------------------------------------------------------------------
# metadata.yaml (Ultralytics export sidecar). Only the keys used here, so no
# yaml dependency: ultralytics writes `names:` as an indented `id: name` map.
# ---------------------------------------------------------------------------

def read_metadata(folder):
    """{'names': [...], 'task': str, 'imgsz': [h, w]} from folder/metadata.yaml,
    or {} when there is none."""
    path = Path(folder) / 'metadata.yaml'
    if not path.is_file():
        return {}
    meta, names, current = {}, {}, None
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('- '):                       # a list item of the current key
            if current == 'imgsz' and line[2:].strip().isdigit():
                meta.setdefault('imgsz', []).append(int(line[2:].strip()))
            continue
        key, _, value = line.partition(':')
        value = value.strip().strip('\'"')
        if raw[:1] in (' ', '\t'):                      # an indented `id: name` under names:
            if current == 'names' and key.strip().isdigit():
                names[int(key)] = value
            continue
        current = key
        if key == 'names' and value.startswith('{'):
            for k, v in re.findall(r'(\d+)\s*:\s*([^,}]+)', value):
                names[int(k)] = v.strip().strip('\'"')
        elif key == 'task':
            meta['task'] = value
        elif key == 'imgsz' and value:
            meta['imgsz'] = [int(n) for n in re.findall(r'\d+', value)]
    if names:
        meta['names'] = [names[i] for i in sorted(names)]
    return meta


def write_metadata(folder, names, task='detect', imgsz=None, nms=True):
    """A metadata.yaml Ultralytics' Hailo backend can load."""
    h, w = (imgsz or (640, 640))[:2]
    lines = ['# Written by the OWL (utils/model_catalog.py) for a bare .hef',
             f'task: {task}', 'stride: 32', 'batch: 1',
             'imgsz:', f'- {h}', f'- {w}', f'nms: {"true" if nms else "false"}', 'names:']
    lines += [f"  {i}: '{str(n).replace(chr(39), '')}'" for i, n in enumerate(names)]
    (Path(folder) / 'metadata.yaml').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _sidecar_path(models_dir, name):
    """models/<name>.json, refusing any name that is not a bare file name
    (names arrive in LAN and cloud commands)."""
    name = str(name or '')
    if (not name or name.startswith('.') or '/' in name or '\\' in name
            or '\x00' in name or name in ('.', '..')):
        raise ValueError(f'not a model name: {name!r}')
    return Path(models_dir) / f'{name}.json'


def read_sidecar(models_dir, name):
    try:
        path = _sidecar_path(models_dir, name)
    except ValueError:
        return {}
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        logger.warning(f'Ignoring unreadable {path.name}: {e}')
        return {}


def write_sidecar(models_dir, name, **values):
    """Merge values into models/<name>.json."""
    path = _sidecar_path(models_dir, name)
    data = read_sidecar(models_dir, name)
    data.update({k: v for k, v in values.items() if v is not None})
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def is_build_dir(path):
    path = Path(path)
    return path.is_dir() and (any(path.glob('*.param')) or any(path.glob('*.hef')))


def classify(path, runner=None):
    """Build for one entry of models/, or None for anything that isn't one."""
    path = Path(path)
    if path.name.startswith('.'):
        return None
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix == '.pt':
            return Build(path, 'pt', 'cpu', _size(path))
        if suffix == '.hef':
            return Build(path, 'hef', hef_info(path, runner).arch, _size(path))
        return None
    if path.is_dir():
        if any(path.glob('*.param')):
            return Build(path, 'ncnn', 'cpu', _size(path))
        hef = _hef_in(path)
        if hef:
            return Build(path, 'hailo', hef_info(hef, runner).arch, _size(path))
    return None


def scan(models_dir, runner=None):
    """[Model] in models_dir, sorted by name."""
    models_dir = Path(models_dir)
    if not models_dir.is_dir():
        return []
    by_name = {}
    for entry in sorted(models_dir.iterdir()):
        build = classify(entry, runner)
        if build:
            by_name.setdefault(base_name(entry.name), Model(base_name(entry.name))).builds.append(build)
    for name, model in by_name.items():
        model.sidecar = read_sidecar(models_dir, name)
    return [by_name[n] for n in sorted(by_name)]


def get(models, name):
    return next((m for m in models if m.name == name), None)


def storage_used_bytes(models_dir):
    models_dir = Path(models_dir)
    return _size(models_dir) if models_dir.is_dir() else 0


def storage_refusal(models_dir, need_bytes, budget_gb=DEFAULT_STORAGE_GB,
                    replacing=(), min_free_gb=0):
    """Why writing need_bytes into models_dir must be refused, or None.
    replacing: paths the write overwrites (their space comes back). The
    recording floor (min_free_gb) is protected before the model budget."""
    models_dir = Path(models_dir)
    freed = sum(_size(Path(p)) for p in replacing if Path(p).exists())
    used = max(0, storage_used_bytes(models_dir) - freed)
    budget = float(budget_gb) * 1e9
    if used + need_bytes > budget:
        return (f'Not enough model space ({used / 1e9:.1f} of {float(budget_gb):g} GB used). '
                f'Delete a model first.')
    if min_free_gb:
        probe = models_dir if models_dir.exists() else models_dir.parent
        free = shutil.disk_usage(probe).free
        if free - need_bytes < float(min_free_gb) * 1e9:
            return (f'Not enough free disk space: the model needs {need_bytes / 1e9:.1f} GB '
                    f'and {float(min_free_gb):g} GB is kept free for recording.')
    return None


def delete_model(models_dir, name):
    """Remove every build of a model, its .json sidecar and .sha256 files.
    Returns the names removed."""
    models_dir = Path(models_dir)
    model = get(scan(models_dir), name)
    removed = []
    paths = [b.path for b in model.builds] if model else []
    paths.append(_sidecar_path(models_dir, name))
    for path in paths:
        for p in (path, path.with_name(path.name + '.sha256')):
            if p.is_dir():
                shutil.rmtree(p)
                removed.append(p.name)
            elif p.is_file():
                p.unlink()
                removed.append(p.name)
    return removed


# ---------------------------------------------------------------------------
# Choosing what runs
# ---------------------------------------------------------------------------

def runnable_archs(accelerator):
    return RUNNABLE.get(accelerator or '', ())


def best_hailo_build(model, accelerator):
    """The Hailo build this chip runs best, or None. A build whose chip could
    not be read is tried last (hailortcli failed; loading it will say)."""
    ok = runnable_archs(accelerator)
    if not ok:
        return None
    ranked = [b for arch in ok for b in model.hailo_builds if b.arch == arch]
    unknown = [b for b in model.hailo_builds if not b.arch]
    return (ranked + unknown or [None])[0]


def build_order(model, accelerator, run_on=None):
    """([Build to try, in order], reason when the list is empty).

    run_on (model sidecar first, then the [GreenOnGreen] default):
      auto  - best Hailo build for this chip, then the CPU build
      hailo - the Hailo build only
      cpu   - the CPU build only
    The CPU build after a Hailo one is the fallback the fault ladder uses."""
    run_on = model.run_on or (run_on if run_on in RUN_ON else 'auto')
    hailo = best_hailo_build(model, accelerator)
    cpu = model.cpu_build
    chip = CHIP_NAMES.get(accelerator or '', 'Hailo')
    if run_on == 'cpu':
        return ([cpu], None) if cpu else ([], f'{model.name} has no CPU version (.pt or NCNN)')
    if run_on == 'hailo':
        if hailo:
            return [hailo], None
        if not runnable_archs(accelerator):
            return [], f'{model.name} is set to run on the Hailo, but this OWL has no working Hailo'
        return [], f'No Hailo version of {model.name} runs on this {chip}'
    order = [b for b in (hailo, cpu) if b]
    if order:
        return order, None
    if model.hailo_builds:
        if not runnable_archs(accelerator):
            return [], f'{model.name} only has a Hailo version and this OWL has no working Hailo'
        # Only a Hailo-8L gets here: it can't run a hailo8 build
        return [], (f'{model.name} has no version this {chip} can run: '
                    f'it needs a Hailo-8L version or a CPU version')
    return [], f'{model.name} has nothing this OWL can run'


def model_warning(model, accelerator):
    """needs_8l_build when a Hailo-8L can't run any of the model's Hailo builds."""
    if accelerator == 'hailo8l' and model.hailo_builds and not best_hailo_build(model, accelerator):
        return NEEDS_8L_BUILD
    return None


def default_model(models, accelerator):
    """With model_path = models (a folder, not a model): the first model by
    name that has a Hailo build this chip runs, else today's order (first
    NCNN, then first .pt)."""
    if runnable_archs(accelerator):
        for model in models:
            if best_hailo_build(model, accelerator):
                return model
    for kind in ('ncnn', 'pt'):
        for model in models:
            if any(b.kind == kind for b in model.builds):
                return model
    return None


def locate(model_path, home=None):
    """(models_dir, model name or None) for a [GreenOnGreen] model_path.

    'models'                         -> (models, None): the default pick
    'models/weeds-v3'                -> (models, 'weeds-v3')   base name
    'models/weeds-v3.pt', '..._ncnn_model', '..._hailo_model', '.hef'
                                     -> (models, 'weeds-v3')   old file names map to their model
    A relative path is under the OWL home."""
    path = Path(model_path)
    if not path.is_absolute() and home is not None:
        path = Path(home) / path
    if path.is_dir() and not is_build_dir(path):
        return path, None
    return path.parent, base_name(path.name)


# ---------------------------------------------------------------------------
# Class names
# ---------------------------------------------------------------------------

def _labels_txt(folder):
    path = Path(folder) / 'labels.txt'
    if not path.is_file():
        return None
    names = [line.strip() for line in path.read_text(encoding='utf-8', errors='replace').splitlines()
             if line.strip()]
    return names or None


def class_names(model, build, num_classes=0, pt_names=None):
    """(names or None, warning or None) for a Hailo build. First found wins:
    the build's metadata.yaml, the model's .json sidecar, another build's
    metadata.yaml (NCNN), the .pt's own names (pt_names(path) -> list, only
    when given: it needs ultralytics), labels.txt in the Hailo folder. A list
    whose length differs from the HEF's class count is skipped. None found:
    the caller numbers the classes and targets them all."""
    folder = build.path if build.path.is_dir() else None
    sources = []
    if folder:
        sources.append(lambda: read_metadata(folder).get('names'))
    sources.append(lambda: model.sidecar.get('classes'))
    for other in model.builds:
        if other is not build and other.path.is_dir():
            sources.append(lambda o=other: read_metadata(o.path).get('names'))
    if pt_names:
        for other in model.builds:
            if other.kind == 'pt':
                sources.append(lambda o=other: pt_names(o.path))
    if folder:
        sources.append(lambda: _labels_txt(folder))

    mismatch = False
    for source in sources:
        try:
            names = source()
        except Exception as e:
            logger.warning(f'Could not read class names for {model.name}: {e}')
            continue
        if not names or not isinstance(names, (list, tuple)):
            continue
        names = [str(n) for n in names]
        if num_classes and len(names) != num_classes:
            mismatch = True
            continue
        return names, None
    return None, LABELS_MISMATCH if mismatch else NO_LABELS


def numbered_names(num_classes):
    return [f'class {i}' for i in range(max(1, num_classes))]


def adopt_bare_hefs(models_dir, runner=None, pt_names=None):
    """Move each bare models/<x>.hef into models/<x>_hailo_model/ with a
    metadata.yaml, so Ultralytics loads it like an export. Returns the new
    folders. Logged: it moves a file the user put there."""
    models_dir = Path(models_dir)
    adopted = []
    for model in scan(models_dir, runner):
        for build in model.builds:
            if build.kind != 'hef':
                continue
            folder = models_dir / f'{build.path.stem}{HAILO_FOLDER_SUFFIX}'
            if folder.exists():
                logger.warning(f'Not adopting {build.name}: {folder.name} already exists')
                continue
            info = hef_info(build.path, runner)
            names, warning = class_names(model, build, info.num_classes, pt_names)
            if not names:
                names = numbered_names(info.num_classes)
                logger.warning(f'{build.name} has no class names ({warning}): using '
                               f'numbered classes. Add models/{model.name}.json '
                               f'with {{"classes": [...]}} to name them.')
            tmp = models_dir / f'.{folder.name}.tmp'
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            shutil.move(str(build.path), str(tmp / build.name))
            write_metadata(tmp, names, imgsz=info.imgsz or None, nms=info.nms or not info.arch)
            os.replace(tmp, folder)
            logger.info(f'Adopted {build.name} as {folder.name}/ ({len(names)} classes)')
            adopted.append(folder)
    return adopted


# ---------------------------------------------------------------------------
# Uploads (standalone: into models/; networked: into the uploads/ library)
# ---------------------------------------------------------------------------

UPLOAD_EXTENSIONS = ('.pt', '.zip', '.hef', '.json')
MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024 ** 3      # zip bomb guard (a large .pt export is ~140 MB)


def check_model_zip(zip_path):
    """'ncnn' or 'hailo' for a safe model zip; raises ValueError otherwise."""
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        raise ValueError('The model zip is damaged (not a valid zip file)')
    if sum(i.file_size for i in infos) > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise ValueError('The zip unpacks to more than 2 GB')
    names = [i.filename for i in infos]
    for info in infos:
        if info.filename.startswith('/') or '..' in info.filename.split('/'):
            raise ValueError(f'Unsafe path in zip: {info.filename}')
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f'Symlink in zip: {info.filename}')
    if any(n.endswith('.hef') for n in names):
        return 'hailo'
    if any(n.endswith('.param') for n in names) and any(n.endswith('.bin') for n in names):
        return 'ncnn'
    raise ValueError('The zip holds no model: an NCNN zip needs .param and .bin files, '
                     'a Hailo zip a .hef')


def add_upload(dest_dir, src_path, filename, budget_gb=None, floor_gb=0,
               accelerator=None, write_sha=False):
    """Put an uploaded file into dest_dir as a model build and return
    {'model', 'build', 'kind', 'arch', 'size'}; raises ValueError with a
    sentence for the screen. src_path is a temp file; it is consumed.

    .pt      -> <name>.pt
    .zip     -> <name>_ncnn_model/ or <name>_hailo_model/ (Ultralytics export zips)
    .hef     -> wrapped into <name>_hailo_model/ with a metadata.yaml
    .json    -> the model's sidecar ({"classes": [...], "run_on": ...})
    accelerator: refuse a Hailo build this chip can't run (standalone);
    None accepts any (the networked library serves several chips)."""
    from utils.model_archive import dir_sha256, extract_model_zip, file_sha256

    dest_dir, src_path = Path(dest_dir), Path(src_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    lower = filename.lower()
    ext = next((e for e in UPLOAD_EXTENSIONS if lower.endswith(e)), None)
    if not ext or filename.startswith('.') or '/' in filename or '\\' in filename:
        raise ValueError('Choose a .pt, .zip, .hef or .json file')
    stem = filename[:-len(ext)]
    name = base_name(stem)
    size = src_path.stat().st_size

    if ext == '.json':
        try:
            data = json.loads(src_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('That .json file is not valid JSON')
        classes = data.get('classes') if isinstance(data, dict) else data
        if not (isinstance(classes, list) and classes and all(isinstance(c, str) for c in classes)):
            raise ValueError('The .json needs a "classes" list of names')
        write_sidecar(dest_dir, name, classes=classes,
                      run_on=data.get('run_on') if isinstance(data, dict) and data.get('run_on') in RUN_ON else None)
        src_path.unlink(missing_ok=True)
        return {'model': name, 'build': f'{name}.json', 'kind': 'labels', 'arch': None, 'size': size}

    if budget_gb is not None:
        need = size * (3 if ext == '.zip' else 1)
        reason = storage_refusal(dest_dir, need, budget_gb, (), floor_gb)
        if reason:
            raise ValueError(reason)

    if ext == '.pt':
        target = dest_dir / filename
        os.replace(src_path, target)
        build = classify(target)
    elif ext == '.hef':
        staging = dest_dir / f'.{stem}.upload'
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        os.replace(src_path, staging / filename)
        build = classify(staging / filename)
        if accelerator is not None and build.arch and build.arch not in runnable_archs(accelerator):
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError(f'This file is built for the {CHIP_NAMES.get(build.arch, build.arch)} '
                             f'and cannot run on this {CHIP_NAMES.get(accelerator, "OWL")}. '
                             f'Upload a Hailo-8L version.')
        os.replace(staging / filename, dest_dir / filename)
        shutil.rmtree(staging, ignore_errors=True)
        folder = adopt_bare_hefs(dest_dir)
        target = next((f for f in folder if f.name.startswith(stem)), dest_dir / f'{stem}{HAILO_FOLDER_SUFFIX}')
        build = classify(target)
    else:
        kind = check_model_zip(src_path)
        # Ultralytics only loads a Hailo folder by its name; NCNN by its files
        folder_name = stem if (kind == 'ncnn' or HAILO_FOLDER_SUFFIX in stem.lower()) \
            else f'{stem}{HAILO_FOLDER_SUFFIX}'
        target = dest_dir / folder_name
        staging = dest_dir / f'.{folder_name}.upload'
        shutil.rmtree(staging, ignore_errors=True)
        try:
            extract_model_zip(str(src_path), str(staging))
        finally:
            src_path.unlink(missing_ok=True)
        # The staging folder is dot-named (scan skips it), so look inside directly
        hef_file = _hef_in(staging)
        if (kind == 'hailo' and hef_file is None) or (kind == 'ncnn' and not any(staging.glob('*.param'))):
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError('The zip holds no model this OWL can load: put the model files '
                             'at the top of the zip or in one folder')
        arch = hef_info(hef_file).arch if hef_file else 'cpu'
        if (hef_file and accelerator is not None and arch
                and arch not in runnable_archs(accelerator)):
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError(f'This model is built for the {CHIP_NAMES.get(arch, arch)} '
                             f'and cannot run on this {CHIP_NAMES.get(accelerator, "OWL")}. '
                             f'Upload a Hailo-8L version.')
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
        build = classify(target)

    if budget_gb is not None and storage_used_bytes(dest_dir) > float(budget_gb) * 1e9:
        delete_path = build.path
        shutil.rmtree(delete_path) if delete_path.is_dir() else delete_path.unlink()
        raise ValueError(f'Not enough model space ({storage_used_bytes(dest_dir) / 1e9:.1f} of '
                         f'{float(budget_gb):g} GB used). Delete a model first.')
    if write_sha:
        digest = dir_sha256(build.path) if build.path.is_dir() else file_sha256(build.path)
        build.path.with_name(build.name + '.sha256').write_text(digest)
    logger.info(f'Model uploaded: {build.name} ({build.kind}, {build.arch or "chip unknown"})')
    return {'model': name, 'build': build.name, 'kind': build.kind, 'arch': build.arch,
            'size': build.size_bytes}


def fleet_files(model, accelerator):
    """The builds an OWL with this accelerator gets on deploy (D10): its best
    Hailo build and the CPU build as the fallback; only the CPU build for a
    CPU OWL or one that has not said what it has."""
    files = []
    hailo = best_hailo_build(model, accelerator)
    if hailo:
        files.append(hailo)
    if model.cpu_build:
        files.append(model.cpu_build)
    return files


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

MAX_MODELS_IN_STATE = 50


def summary(models, accelerator, running=None, run_on_default='auto'):
    """ai.models for MQTT state. running: (model name, Build) or None."""
    out = []
    for model in models[:MAX_MODELS_IN_STATE]:
        order, _reason = build_order(model, accelerator, run_on_default)
        runs_on = None
        if running and running[0] == model.name and running[1] is not None:
            runs_on = running[1].arch if running[1].is_hailo else 'cpu'
        warning = model_warning(model, accelerator)
        if not warning and order and order[0].is_hailo:
            _names, warning = class_names(model, order[0])
        out.append({
            'name': model.name,
            'builds': [{'file': b.name, 'arch': b.arch or 'unknown',
                        'size_mb': round(b.size_bytes / 1e6, 1)} for b in model.builds],
            'run_on': model.run_on or run_on_default,
            'runs_on': runs_on,
            'will_run_on': (order[0].arch if order[0].is_hailo else 'cpu') if order else None,
            'warning': warning,
        })
    return out
