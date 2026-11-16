def test_pages(client):
    for key in ['intake','contract','model','calculate','quarantine','reconcile','responsibility','batches']:
        r=client.get('/'+key);assert r.status_code==200;assert 'stage-track' in r.text

def test_module_trials(client):
    for key in ['intake','contract','model','calculate','quarantine','reconcile','responsibility']:
        assert client.post('/api/'+key,json={'sample':'dirty'}).status_code==200

def test_export(client):
    r=client.get('/api/quarantine/export');assert r.status_code==200;assert r.text.count('\n')==10

def test_fail_recover(client):
    assert client.post('/api/runs',json={'grace':'bad'}).status_code==422;assert client.post('/api/runs',json={'sample':'clean'}).json()['reconcile']['balanced']

def test_timing_page_side_by_side(client):
    r=client.get('/calculate');assert '责任科室用时' in r.text;assert '标本净用时' in r.text

def test_responsibility_page_identity(client):
    r=client.get('/responsibility');assert '责任对平' in r.text;assert '分摊等待' in r.text
