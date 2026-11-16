import json,hashlib,time
from app.config import SAMPLES
from app.errors import RuleError
from app.manifest import make_manifest
from app.modules.intake.service import ingest
from app.modules.contract.service import normalize
from app.modules.model.service import build
from app.modules.calculate.service import calculate
from app.modules.quarantine.service import quarantine
from app.modules.reconcile.service import reconcile
from app.modules.responsibility.service import attribute

def _canonical(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def _rules(pauses=None):
    refs={};objects={}
    for name in ('versions','calendar','pauses'):
        data=(SAMPLES/(name+'.json')).read_bytes();objects[name]=json.loads(data)
    if pauses is not None:
        # 交接暂停改动进入同一摘要，改后必须产生新批次并同步重算责任与标本结论。
        if not isinstance(pauses,dict):raise RuleError('PAUSE','暂停规则必须为按标本号索引的对象')
        objects['pauses']=pauses
    for name in ('versions','calendar','pauses'):
        # 规则摘要按解析后的规范内容计算，保证交接改动往返后回到同一批次与同一责任结论。
        refs[name]=_canonical(objects[name])
    return refs,objects

def execute(raw,grace='0',pauses=None):
    start=time.perf_counter()
    refs,objects=_rules(pauses)
    intake=ingest(raw);contract=normalize(intake['rows'],objects['versions']);model=build(contract['rows'],objects['calendar'],objects['pauses']);calc=calculate(model['rows'],grace)
    isolated=quarantine(intake['issues'],contract['issues'],model['issues']);checked=reconcile(intake,calc,isolated)
    responsibility=attribute(calc['rows'],objects['pauses'])
    manifest=make_manifest(raw,{'grace':str(grace)},refs);manifest['elapsed_ms']=round((time.perf_counter()-start)*1000,3)
    return {'manifest':manifest,'intake':intake,'contract':contract,'model':model,'calculate':calc,'quarantine':isolated,'reconcile':checked,'responsibility':responsibility}

def ensure_responsibility(bundle):
    """旧批次没有责任结论时按同一计算口径补齐，保证页面与复核始终可核对。"""
    if isinstance(bundle.get('responsibility'),dict):return bundle
    pauses=bundle.get('model',{}).get('pauses')
    rows=bundle.get('calculate',{}).get('rows')
    if pauses is None or rows is None:return bundle
    bundle['responsibility']=attribute(rows,pauses)
    return bundle

def sample_bytes(name='dirty'):
    if name not in ('dirty','clean','gb18030'):raise RuleError('SAMPLE','标本样例不存在',404)
    return (SAMPLES/(name+'.csv')).read_bytes()
