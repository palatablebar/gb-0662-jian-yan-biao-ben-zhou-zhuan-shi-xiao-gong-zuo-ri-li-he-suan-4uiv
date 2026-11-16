import os,sqlite3,json,hashlib
from pathlib import Path
from app.errors import RuleError

SCHEMA='''
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, reviewed INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS responsibility_conclusions(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, pause_digest TEXT NOT NULL, balanced INTEGER NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS responsibility_current(slot INTEGER PRIMARY KEY CHECK(slot=1), conclusion_id TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS responsibility_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, conclusion_id TEXT NOT NULL, action TEXT NOT NULL, previous_id TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handover_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, specimen TEXT NOT NULL, handover_id TEXT, action TEXT NOT NULL, raw TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS current_run(slot INTEGER PRIMARY KEY CHECK(slot=1), run_id TEXT NOT NULL, updated TEXT NOT NULL);
'''

RAW_SCHEMA = 'CREATE TABLE IF NOT EXISTS run_inputs(run_id TEXT PRIMARY KEY, raw BLOB NOT NULL, sha256 TEXT NOT NULL)'

def connect():
    path=os.environ.get('DB_PATH','/tmp/labclock.sqlite3')
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=15);con.row_factory=sqlite3.Row
    con.executescript(SCHEMA)
    return con

def _register(con,responsibility,run_id,created):
    """责任结论按稳定标识幂等登记；同样交接与同样分摊不追加记录。"""
    cid=responsibility['conclusion_id']
    con.execute('INSERT OR IGNORE INTO responsibility_conclusions(id,run_id,pause_digest,balanced,payload,created) VALUES(?,?,?,?,?,?)',
        (cid,run_id,responsibility.get('pause_digest',''),1 if responsibility.get('balanced') else 0,json.dumps(responsibility,ensure_ascii=False),created))
    row=con.execute('SELECT * FROM responsibility_conclusions WHERE id=?',(cid,)).fetchone()
    return bool(row['balanced'])

def _set_current(con,cid,action,created):
    row=con.execute('SELECT conclusion_id FROM responsibility_current WHERE slot=1').fetchone()
    previous=row['conclusion_id'] if row else None
    if previous==cid:return previous
    con.execute('INSERT INTO responsibility_current(slot,conclusion_id,updated) VALUES(1,?,?) ON CONFLICT(slot) DO UPDATE SET conclusion_id=excluded.conclusion_id,updated=excluded.updated',(cid,created))
    con.execute('INSERT INTO responsibility_events(conclusion_id,action,previous_id,created) VALUES(?,?,?,?)',(cid,action,previous,created))
    return previous

def save(bundle,raw=None):
    rid=bundle['manifest']['run_id']
    created=bundle['manifest']['created_at']
    con=connect()
    try:
        # 一次事务内同时落批次、原始输入与责任结论；任一步失败都不留下半批。
        con.execute('BEGIN IMMEDIATE')
        con.execute('CREATE TABLE IF NOT EXISTS run_inputs(run_id TEXT PRIMARY KEY, raw BLOB NOT NULL, sha256 TEXT NOT NULL)')
        con.execute('INSERT OR IGNORE INTO runs(id,payload,version,reviewed,created) VALUES(?,?,1,0,?)',(rid,json.dumps(bundle,ensure_ascii=False),created))
        con.execute('INSERT INTO current_run(slot,run_id,updated) VALUES(1,?,?) ON CONFLICT(slot) DO UPDATE SET run_id=excluded.run_id,updated=excluded.updated',(rid,created))
        if raw is not None:
            con.execute('INSERT OR REPLACE INTO run_inputs(run_id,raw,sha256) VALUES(?,?,?)',(rid,raw,hashlib.sha256(raw).hexdigest()))
        responsibility=bundle.get('responsibility')
        if isinstance(responsibility,dict) and responsibility.get('conclusion_id'):
            balanced=_register(con,responsibility,rid,created)
            # 只有责任对平的结论才能成为现行结论；不平的仅留证据，旧版继续生效。
            if balanced:_set_current(con,responsibility['conclusion_id'],'compute',created)
        con.commit()
        row=con.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    out=json.loads(row['payload']);out['version']=row['version'];out['reviewed']=bool(row['reviewed'])
    return out

def _select_current(con):
    """最近一次写入的批次优先；回到旧配置时旧批次重新成为当前批次。"""
    row=con.execute('SELECT r.* FROM current_run c JOIN runs r ON r.id=c.run_id WHERE c.slot=1').fetchone()
    return row or con.execute('SELECT * FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()

def latest():
    with connect() as c: row=_select_current(c)
    if not row:return None
    out=json.loads(row['payload']);out['version']=row['version'];out['reviewed']=bool(row['reviewed']);return out

def run(rid):
    with connect() as c: row=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
    if not row:return None
    out=json.loads(row['payload']);out['version']=row['version'];out['reviewed']=bool(row['reviewed']);return out

def raw_input(rid):
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS run_inputs(run_id TEXT PRIMARY KEY, raw BLOB NOT NULL, sha256 TEXT NOT NULL)')
        row=c.execute('SELECT raw FROM run_inputs WHERE run_id=?',(rid,)).fetchone()
    return bytes(row['raw']) if row else None

def history():
    with connect() as c: rows=c.execute('SELECT id,version,reviewed,created FROM runs ORDER BY rowid DESC').fetchall()
    return [dict(r) for r in rows]

def record_handover_event(run_id,specimen,handover_id_value,action,raw=None):
    """留存交接暂停改动审计；即使后续重算失败，原交接记录也可追溯。"""
    import time
    with connect() as c:
        c.execute('INSERT INTO handover_events(run_id,specimen,handover_id,action,raw,created) VALUES(?,?,?,?,?,?)',
            (run_id,specimen,handover_id_value,action,json.dumps(raw,ensure_ascii=False) if raw is not None else None,time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())))

def handover_events(run_id=None):
    with connect() as c:
        if run_id:rows=c.execute('SELECT * FROM handover_events WHERE run_id=? ORDER BY seq DESC',(run_id,)).fetchall()
        else:rows=c.execute('SELECT * FROM handover_events ORDER BY seq DESC').fetchall()
    out=[]
    for row in rows:
        item=dict(row)
        item['raw']=json.loads(item['raw']) if item['raw'] else None
        out.append(item)
    return out

def review(rid,version):
    """正式复核：行数、责任恒等式与现行责任结论必须一致，才能落复核记录。"""
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
        if not row or row['version']!=version:
            raise RuleError('STALE','记录已变更，请刷新后复核',409)
        bundle=json.loads(row['payload'])
        from app.pipeline import ensure_responsibility
        from app.modules.batches.review import check
        bundle=ensure_responsibility(bundle)
        check(bundle)
        responsibility=bundle.get('responsibility') or {}
        cid=responsibility.get('conclusion_id')
        current=c.execute('SELECT conclusion_id FROM responsibility_current WHERE slot=1').fetchone()
        # 责任对不平、批次结论不是现行结论、或现行结论已换版时都不允许形成正式记录。
        if not cid or cid!=(current['conclusion_id'] if current else None):
            raise RuleError('RESPONSIBILITY_STALE','现行责任结论已变更，请重新核对后再复核',409)
        cur=c.execute('UPDATE runs SET version=version+1,reviewed=1 WHERE id=? AND version=?',(rid,version))
        if cur.rowcount!=1:raise RuleError('STALE','记录已变更，请刷新后复核',409)
        c.commit()
    return {'run_id':rid,'version':version+1,'reviewed':True,'conclusion_id':cid}

def _conclusion_row(row):
    if not row:return None
    return {'conclusion_id':row['id'],'run_id':row['run_id'],'pause_digest':row['pause_digest'],'balanced':bool(row['balanced']),'created':row['created'],'conclusion':json.loads(row['payload'])}

def conclusion(cid):
    with connect() as c: row=c.execute('SELECT * FROM responsibility_conclusions WHERE id=?',(cid,)).fetchone()
    return _conclusion_row(row)

def current_conclusion():
    with connect() as c:
        row=c.execute('SELECT r.* FROM responsibility_current c JOIN responsibility_conclusions r ON r.id=c.conclusion_id WHERE c.slot=1').fetchone()
    return _conclusion_row(row)

def concl_history():
    with connect() as c: rows=c.execute('SELECT id,run_id,pause_digest,balanced,created FROM responsibility_conclusions ORDER BY rowid DESC').fetchall()
    return [dict(r) for r in rows]

def promote(cid):
    """原子更换现行责任结论；验证或写入失败时事务回滚，旧版继续生效。"""
    from app.modules.responsibility.allocate import verify
    con=connect()
    try:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM responsibility_conclusions WHERE id=?',(cid,)).fetchone()
        if not row:raise RuleError('NOT_FOUND','责任结论不存在',404)
        payload=json.loads(row['payload'])
        # 先验证再切指针；任何异常都回滚，绝不留下半换状态或丢失旧版。
        verify(payload)
        previous=_set_current(con,row['id'],'promote',row['created'])
        con.commit()
        return {'conclusion_id':row['id'],'previous_id':previous,'balanced':bool(row['balanced']),'conclusion':payload}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def restore(cid):
    return promote(cid)

def open_conclusion(cid):
    return conclusion(cid)
