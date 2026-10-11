"""In-cab controller: POST /api/collect/cloud/provision (open to the rig LAN)
refuses a pasted command for any broker but Noktura's. The pin is in
cloud_provision.provision(); this checks the route really goes through it."""

from unittest.mock import patch

import pytest

FOREIGN = ("sudo bash owl_cloud_provision.sh \\\n    --host mqtt.attacker.example --port 8883 \\\n"
           "    --device-id rig-farm-1 \\\n    --username rig-farm-1 \\\n"
           "    --password 'AbCdEfGhIjKlMnOpQrStUvWxYz012345' \\\n    --system-ca")


@pytest.mark.unit
def test_foreign_broker_paste_is_400_and_root_never_runs(networked_test_client, tmp_path):
    client, ctrl = networked_test_client
    ctrl.config_file = tmp_path / 'CONTROLLER.ini'
    ctrl.config_file.write_text('[MQTT]\nbroker_ip = localhost\n')
    with patch('subprocess.run') as run:
        resp = client.post('/api/collect/cloud/provision', json={'command': FOREIGN})
    assert resp.status_code == 400
    assert 'noktura.tech' in resp.get_json()['error']
    assert not any('owl-cloud-provision' in str(c) for c in run.call_args_list)
    ctrl.reload_cloud_config.assert_not_called()
