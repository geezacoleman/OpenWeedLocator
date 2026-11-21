"""utils/model_catalog.py: what is in models/, which build runs, class names,
uploads and storage (docs/HAILO.md §3). No Hailo needed: parse-hef is faked
from the file's own text, so a test HEF says what chip it is built for."""

import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils import model_catalog as mc  # noqa: E402

pytestmark = pytest.mark.unit


def parse_hef_text(arch, classes=2, size=640):
    return (f'Architecture HEF was compiled for: {arch}\n'
            f'Network group name: m, Single Context\n'
            f'    Network name: m/m\n'
            f'        VStream infos:\n'
            f'            Input  m/input_layer1 UINT8, NHWC({size}x{size}x3)\n'
            f'            Output m/yolov8_nms_postprocess FLOAT32, HAILO NMS BY CLASS(number of classes: '
            f'{classes}, maximum bounding boxes per class: 100, maximum frame size: 4008)\n')


def fake_hailortcli(argv, **_kw):
    """`hailortcli parse-hef <file>`: the test HEF holds its own parse output."""
    return SimpleNamespace(stdout=Path(argv[-1]).read_text(), stderr='')


@pytest.fixture(autouse=True)
def fake_parse_hef(monkeypatch):
    real = mc.hef_info
    monkeypatch.setattr(mc, 'hef_info', lambda path, runner=None: real(path, runner or fake_hailortcli))


def hef(path, arch='HAILO8L', classes=2):
    path.write_text(parse_hef_text(arch, classes))
    return path


def hailo_folder(models, name, arch='HAILO8L', names=('weed', 'crop')):
    folder = models / name
    folder.mkdir()
    hef(folder / 'model.hef', arch, len(names))
    lines = ['task: detect', 'imgsz:', '- 640', '- 640', 'names:'] + [f'  {i}: {n}' for i, n in enumerate(names)]
    (folder / 'metadata.yaml').write_text('\n'.join(lines) + '\n')
    return folder


def ncnn_folder(models, name, names=None):
    folder = models / name
    folder.mkdir()
    (folder / 'model.param').write_text('param')
    (folder / 'model.bin').write_bytes(b'\0' * 10)
    if names:
        (folder / 'metadata.yaml').write_text('task: detect\nnames:\n' +
                                              ''.join(f'  {i}: {n}\n' for i, n in enumerate(names)))
    return folder


# ---------------------------------------------------------------------------

class TestNames:
    @pytest.mark.parametrize('name, base', [
        ('weeds-v3.pt', 'weeds-v3'),
        ('weeds-v3_ncnn_model', 'weeds-v3'),
        ('weeds-v3_h8l_hailo_model', 'weeds-v3'),
        ('weeds-v3_hailo8_hailo_model', 'weeds-v3'),
        ('weeds-v3_h8.hef', 'weeds-v3'),
        ('yolo11n', 'yolo11n'),
        ('.pt', '.pt'),                       # never empty
    ])
    def test_base_name(self, name, base):
        assert mc.base_name(name) == base

    def test_parse_hef_output(self):
        info = mc.parse_hef_output(parse_hef_text('HAILO8L', classes=5, size=320))
        assert (info.arch, info.imgsz, info.nms, info.num_classes) == ('hailo8l', (320, 320), True, 5)

    def test_parse_hef_hailo8_is_not_read_as_8l(self):
        assert mc.parse_hef_output(parse_hef_text('HAILO8')).arch == 'hailo8'

    def test_unreadable_output_means_chip_unknown(self):
        assert mc.parse_hef_output('Failed to parse HEF').arch == ''

    def test_metadata_yaml_names_and_list_imgsz(self, tmp_path):
        folder = hailo_folder(tmp_path, 'w_hailo_model', names=('ryegrass', 'wheat'))
        meta = mc.read_metadata(folder)
        assert meta['names'] == ['ryegrass', 'wheat']
        assert meta['imgsz'] == [640, 640] and meta['task'] == 'detect'


class TestScan:
    def test_builds_group_by_base_name(self, tmp_path):
        (tmp_path / 'weeds.pt').write_bytes(b'pt')
        ncnn_folder(tmp_path, 'weeds_ncnn_model')
        hailo_folder(tmp_path, 'weeds_h8l_hailo_model')
        (tmp_path / 'other.pt').write_bytes(b'pt')
        (tmp_path / 'weeds.pt.sha256').write_text('x')
        (tmp_path / '.dl-123.tmp').write_bytes(b'x')
        models = mc.scan(tmp_path)
        assert [m.name for m in models] == ['other', 'weeds']
        weeds = mc.get(models, 'weeds')
        assert sorted(b.kind for b in weeds.builds) == ['hailo', 'ncnn', 'pt']
        assert weeds.cpu_build.kind == 'ncnn', 'NCNN is the faster CPU build on a Pi'
        assert weeds.hailo_builds[0].arch == 'hailo8l'

    def test_sidecar_is_read(self, tmp_path):
        (tmp_path / 'weeds.pt').write_bytes(b'pt')
        (tmp_path / 'weeds.json').write_text(json.dumps({'classes': ['a'], 'run_on': 'cpu'}))
        model = mc.scan(tmp_path)[0]
        assert model.run_on == 'cpu' and model.sidecar['classes'] == ['a']

    def test_missing_folder_is_empty(self, tmp_path):
        assert mc.scan(tmp_path / 'nope') == []


class TestBuildOrder:
    def model(self, tmp_path, archs=('HAILO8L',), cpu=True):
        if cpu:
            (tmp_path / 'weeds.pt').write_bytes(b'pt')
        for arch in archs:
            hailo_folder(tmp_path, f'weeds_{arch.lower()}_hailo_model', arch)
        return mc.scan(tmp_path)[0]

    def test_hailo8_prefers_its_own_build_then_cpu(self, tmp_path):
        model = self.model(tmp_path, ('HAILO8L', 'HAILO8'))
        order, reason = mc.build_order(model, 'hailo8')
        assert [b.arch for b in order] == ['hailo8', 'cpu'] and reason is None

    def test_hailo8_runs_an_8l_build(self, tmp_path):
        order, _ = mc.build_order(self.model(tmp_path, ('HAILO8L',)), 'hailo8')
        assert order[0].arch == 'hailo8l'

    def test_8l_never_starts_a_hailo8_build(self, tmp_path):
        model = self.model(tmp_path, ('HAILO8',))
        order, _ = mc.build_order(model, 'hailo8l')
        assert [b.arch for b in order] == ['cpu']
        assert mc.model_warning(model, 'hailo8l') == mc.NEEDS_8L_BUILD

    def test_8l_with_only_a_hailo8_build_says_why(self, tmp_path):
        order, reason = mc.build_order(self.model(tmp_path, ('HAILO8',), cpu=False), 'hailo8l')
        assert order == [] and 'Hailo-8L version' in reason

    def test_cpu_unit_runs_the_cpu_build(self, tmp_path):
        order, _ = mc.build_order(self.model(tmp_path), 'cpu')
        assert [b.arch for b in order] == ['cpu']

    def test_run_on_cpu(self, tmp_path):
        order, _ = mc.build_order(self.model(tmp_path), 'hailo8', run_on='cpu')
        assert [b.arch for b in order] == ['cpu']

    def test_run_on_hailo_has_no_cpu_fallback(self, tmp_path):
        order, _ = mc.build_order(self.model(tmp_path), 'hailo8', run_on='hailo')
        assert [b.arch for b in order] == ['hailo8l']

    def test_run_on_hailo_without_a_hailo(self, tmp_path):
        order, reason = mc.build_order(self.model(tmp_path), 'cpu', run_on='hailo')
        assert order == [] and 'no working Hailo' in reason

    def test_sidecar_run_on_wins(self, tmp_path):
        model = self.model(tmp_path)
        mc.write_sidecar(tmp_path, 'weeds', run_on='cpu')
        model = mc.scan(tmp_path)[0]
        order, _ = mc.build_order(model, 'hailo8', run_on='auto')
        assert [b.arch for b in order] == ['cpu']

    def test_default_pick_prefers_a_runnable_hailo_model(self, tmp_path):
        ncnn_folder(tmp_path, 'aaa_ncnn_model')
        hailo_folder(tmp_path, 'zzz_hailo_model', 'HAILO8L')
        models = mc.scan(tmp_path)
        assert mc.default_model(models, 'hailo8l').name == 'zzz'
        assert mc.default_model(models, 'cpu').name == 'aaa', "today's order without a Hailo"

    def test_default_pick_skips_a_hailo8_model_on_an_8l(self, tmp_path):
        ncnn_folder(tmp_path, 'aaa_ncnn_model')
        hailo_folder(tmp_path, 'zzz_hailo_model', 'HAILO8')
        assert mc.default_model(mc.scan(tmp_path), 'hailo8l').name == 'aaa'

    @pytest.mark.parametrize('model_path, name', [
        ('models', None),
        ('models/weeds', 'weeds'),
        ('models/weeds.pt', 'weeds'),
        ('models/weeds_ncnn_model', 'weeds'),
    ])
    def test_locate(self, tmp_path, model_path, name):
        (tmp_path / 'models').mkdir()
        ncnn_folder(tmp_path / 'models', 'weeds_ncnn_model')
        models_dir, found = mc.locate(model_path, home=tmp_path)
        assert models_dir == tmp_path / 'models' and found == name


class TestClassNames:
    def test_hailo_metadata_first(self, tmp_path):
        folder = hailo_folder(tmp_path, 'w_hailo_model', names=('a', 'b'))
        model = mc.scan(tmp_path)[0]
        assert mc.class_names(model, mc.classify(folder)) == (['a', 'b'], None)

    def test_borrowed_from_the_ncnn_build(self, tmp_path):
        hef(tmp_path / 'w_h8l.hef', classes=2)
        ncnn_folder(tmp_path, 'w_ncnn_model', names=['weed', 'crop'])
        model = mc.scan(tmp_path)[0]
        bare = next(b for b in model.builds if b.kind == 'hef')
        assert mc.class_names(model, bare, 2) == (['weed', 'crop'], None)

    def test_wrong_length_is_not_borrowed(self, tmp_path):
        hef(tmp_path / 'w.hef', classes=5)
        ncnn_folder(tmp_path, 'w_ncnn_model', names=['weed', 'crop'])
        model = mc.scan(tmp_path)[0]
        bare = next(b for b in model.builds if b.kind == 'hef')
        assert mc.class_names(model, bare, 5) == (None, mc.LABELS_MISMATCH)

    def test_none_anywhere(self, tmp_path):
        hef(tmp_path / 'w.hef')
        model = mc.scan(tmp_path)[0]
        assert mc.class_names(model, model.builds[0], 2) == (None, mc.NO_LABELS)


class TestAdoptBareHef:
    def test_wrapped_with_names_ultralytics_can_load(self, tmp_path):
        hef(tmp_path / 'w_h8l.hef', classes=2)
        (tmp_path / 'w.json').write_text(json.dumps({'classes': ['weed', 'crop']}))
        [folder] = mc.adopt_bare_hefs(tmp_path)
        assert folder.name == 'w_h8l_hailo_model'
        assert (folder / 'w_h8l.hef').is_file() and not (tmp_path / 'w_h8l.hef').exists()
        meta = mc.read_metadata(folder)
        assert meta['names'] == ['weed', 'crop'] and meta['imgsz'] == [640, 640]
        assert 'nms: true' in (folder / 'metadata.yaml').read_text()

    def test_no_names_gets_numbered_classes(self, tmp_path):
        hef(tmp_path / 'w.hef', classes=3)
        [folder] = mc.adopt_bare_hefs(tmp_path)
        assert mc.read_metadata(folder)['names'] == ['class 0', 'class 1', 'class 2']

    def test_never_overwrites_a_folder(self, tmp_path):
        hef(tmp_path / 'w.hef')
        (tmp_path / 'w_hailo_model').mkdir()
        assert mc.adopt_bare_hefs(tmp_path) == []
        assert (tmp_path / 'w.hef').exists()


def zip_of(path, files):
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


class TestUpload:
    def test_pt(self, tmp_path):
        src = tmp_path / 'up.tmp'
        src.write_bytes(b'pt')
        models = tmp_path / 'models'
        result = mc.add_upload(models, src, 'weeds.pt')
        assert result['build'] == 'weeds.pt' and (models / 'weeds.pt').exists()

    def test_hailo_zip_gets_a_folder_ultralytics_recognises(self, tmp_path):
        src = zip_of(tmp_path / 'up.tmp', {'best_hailo_model/best.hef': parse_hef_text('HAILO8L'),
                                           'best_hailo_model/metadata.yaml': 'names:\n  0: weed\n'})
        result = mc.add_upload(tmp_path / 'm', src, 'weeds.zip', accelerator='hailo8')
        assert result['build'] == 'weeds_hailo_model' and result['arch'] == 'hailo8l'

    def test_ncnn_zip(self, tmp_path):
        src = zip_of(tmp_path / 'up.tmp', {'x/model.param': 'p', 'x/model.bin': 'b'})
        assert mc.add_upload(tmp_path / 'm', src, 'weeds.zip')['build'] == 'weeds', 'NCNN keeps its name (found by its files)'

    def test_a_hailo8_build_is_refused_on_an_8l(self, tmp_path):
        src = zip_of(tmp_path / 'up.tmp', {'m.hef': parse_hef_text('HAILO8')})
        with pytest.raises(ValueError, match='Hailo-8L version'):
            mc.add_upload(tmp_path / 'm', src, 'weeds.zip', accelerator='hailo8l')
        assert mc.scan(tmp_path / 'm') == []

    def test_bare_hef(self, tmp_path):
        src = hef(tmp_path / 'up.tmp')
        result = mc.add_upload(tmp_path / 'm', src, 'weeds_h8l.hef', accelerator='hailo8l')
        assert result['build'] == 'weeds_h8l_hailo_model'

    def test_labels_json(self, tmp_path):
        src = tmp_path / 'up.tmp'
        src.write_text(json.dumps({'classes': ['weed', 'crop']}))
        mc.add_upload(tmp_path / 'm', src, 'weeds.json')
        assert mc.read_sidecar(tmp_path / 'm', 'weeds') == {'classes': ['weed', 'crop']}

    def test_zip_without_a_model(self, tmp_path):
        src = zip_of(tmp_path / 'up.tmp', {'readme.txt': 'hi'})
        with pytest.raises(ValueError, match='holds no model'):
            mc.add_upload(tmp_path / 'm', src, 'x.zip')

    def test_unsafe_zip(self, tmp_path):
        src = zip_of(tmp_path / 'up.tmp', {'../evil.param': 'p', 'x.bin': 'b'})
        with pytest.raises(ValueError, match='Unsafe path'):
            mc.add_upload(tmp_path / 'm', src, 'x.zip')

    @pytest.mark.parametrize('name', ['x.exe', '.pt', '../x.pt'])
    def test_bad_names(self, tmp_path, name):
        src = tmp_path / 'up.tmp'
        src.write_bytes(b'x')
        with pytest.raises(ValueError):
            mc.add_upload(tmp_path / 'm', src, name)

    def test_over_budget(self, tmp_path):
        models = tmp_path / 'm'
        models.mkdir()
        (models / 'big.pt').write_bytes(b'\0' * 2000)
        src = tmp_path / 'up.tmp'
        src.write_bytes(b'\0' * 2000)
        with pytest.raises(ValueError, match='Not enough model space'):
            mc.add_upload(models, src, 'more.pt', budget_gb=3e-6)


class TestStorageAndDelete:
    def test_refusal_at_the_budget(self, tmp_path):
        (tmp_path / 'a.pt').write_bytes(b'\0' * 1000)
        assert mc.storage_refusal(tmp_path, 1000, budget_gb=2e-6) is None
        assert 'Not enough model space' in mc.storage_refusal(tmp_path, 1001, budget_gb=2e-6)

    def test_replacing_a_file_frees_its_space(self, tmp_path):
        (tmp_path / 'a.pt').write_bytes(b'\0' * 1000)
        assert mc.storage_refusal(tmp_path, 1500, 2e-6, replacing=[tmp_path / 'a.pt']) is None

    def test_recording_floor(self, tmp_path):
        assert 'kept free for recording' in mc.storage_refusal(tmp_path, 1, 3, min_free_gb=10 ** 6)

    def test_delete_removes_every_build_and_sidecars(self, tmp_path):
        (tmp_path / 'w.pt').write_bytes(b'pt')
        (tmp_path / 'w.pt.sha256').write_text('x')
        hailo_folder(tmp_path, 'w_hailo_model')
        (tmp_path / 'w.json').write_text('{}')
        (tmp_path / 'keep.pt').write_bytes(b'pt')
        mc.delete_model(tmp_path, 'w')
        assert sorted(p.name for p in tmp_path.iterdir()) == ['keep.pt']


class TestFleetAndState:
    def test_fleet_files(self, tmp_path):
        (tmp_path / 'w.pt').write_bytes(b'pt')
        hailo_folder(tmp_path, 'w_h8_hailo_model', 'HAILO8')
        hailo_folder(tmp_path, 'w_h8l_hailo_model', 'HAILO8L')
        model = mc.scan(tmp_path)[0]
        assert [b.arch for b in mc.fleet_files(model, 'hailo8')] == ['hailo8', 'cpu']
        assert [b.arch for b in mc.fleet_files(model, 'hailo8l')] == ['hailo8l', 'cpu']
        assert [b.arch for b in mc.fleet_files(model, 'cpu')] == ['cpu']
        assert [b.arch for b in mc.fleet_files(model, None)] == ['cpu']

    def test_summary(self, tmp_path):
        (tmp_path / 'w.pt').write_bytes(b'pt')
        folder = hailo_folder(tmp_path, 'w_hailo_model', 'HAILO8L')
        models = mc.scan(tmp_path)
        [entry] = mc.summary(models, 'hailo8l', running=('w', mc.classify(folder)))
        assert entry['runs_on'] == 'hailo8l' and entry['will_run_on'] == 'hailo8l'
        assert entry['warning'] is None and len(entry['builds']) == 2


class TestSidecarNames:
    """Model names arrive in LAN and cloud commands: models/<name>.json only."""

    @pytest.mark.parametrize('name', ['../owl', '..', '.hidden', 'a/b', 'a' + chr(92) + 'b', ''])
    def test_a_path_is_refused(self, tmp_path, name):
        with pytest.raises(ValueError):
            mc.write_sidecar(tmp_path / 'models', name, run_on='cpu')
        assert not list(tmp_path.rglob('*.json'))
        assert mc.read_sidecar(tmp_path / 'models', name) == {}
