from fastapi import APIRouter,Request,Body
from app.web import page
from app import store
from app.pipeline import execute,sample_bytes,ensure_responsibility
from app.modules.responsibility.service import edit_handover,remove_handover,promote
from app.errors import RuleError
router=APIRouter()

def _bundle():
    bundle=store.latest()
    if bundle is None:raise RuleError('NOT_FOUND','尚无核算批次',404)
    return ensure_responsibility(bundle)

@router.get('/responsibility')
def view(request:Request):return page(request,'responsibility')

@router.get('/api/responsibility')
def result():return _bundle()['responsibility']

def _brief(row):
    if not row:return None
    out={key:row[key] for key in ('conclusion_id','run_id','pause_digest','balanced','created')}
    out['net_total']=row['conclusion'].get('net_total')
    out['pause_total']=row['conclusion'].get('pause_total')
    out['responsibility_total']=row['conclusion'].get('responsibility_total')
    return out

@router.get('/api/responsibility/conclusions')
def conclusions():return {'current':_brief(store.current_conclusion()),'history':store.concl_history()}

@router.get('/api/responsibility/handovers')
def handover_audit(run_id:str=''):
    return {'handovers':_bundle()['responsibility']['handovers'],'events':store.handover_events(run_id or None)}
@router.post('/api/responsibility')
def trial(payload:dict=Body(default={})):
    raw=payload.get('csv_text');data=execute(raw.encode() if isinstance(raw,str) else sample_bytes(payload.get('sample','dirty')),payload.get('grace','0'))
    return data['responsibility']
@router.post('/api/responsibility/handovers')
def handover_edit(payload:dict=Body(...)):
    run_id=payload.get('run_id','')
    handover=payload.get('handover',payload)
    if not isinstance(handover,dict):raise RuleError('HANDOVER','交接暂停必须为对象')
    if not handover.get('specimen') and payload.get('specimen'):handover=dict(handover,specimen=payload['specimen'])
    return edit_handover(run_id,handover)
@router.post('/api/responsibility/handovers/remove')
def handover_remove(payload:dict=Body(...)):
    return remove_handover(payload.get('run_id',''),payload.get('specimen',''),payload.get('handover_id',''))
@router.post('/api/responsibility/promote')
def conclusion_promote(payload:dict=Body(...)):
    cid=payload.get('conclusion_id','')
    if not cid:raise RuleError('PARAMETER','必须指定责任结论')
    return promote(cid)
