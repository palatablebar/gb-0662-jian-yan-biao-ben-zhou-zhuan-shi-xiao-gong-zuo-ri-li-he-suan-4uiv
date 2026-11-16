from fastapi.templating import Jinja2Templates
from app.config import ROOT
from app import store
from app.pipeline import ensure_responsibility
PAGES=[('intake','标本采信'),('contract','项目契约'),('model','节假日历'),('calculate','时效计算'),('quarantine','异常隔离'),('reconcile','科室对账'),('responsibility','责任用时'),('batches','批次复核')]
templates=Jinja2Templates(directory=str(ROOT/'app/templates'))
def timing_rows(bundle):
    """把标本结论与责任科室用时并排，供时效页面逐条核对。"""
    responsibility=bundle.get('responsibility') or {}
    by_specimen={item['specimen']:item for item in responsibility.get('specimens',[])}
    rows=[]
    for row in bundle['calculate']['rows']:
        summary=by_specimen.get(row['specimen'],{})
        rows.append(dict(row,
            pause_minutes=row.get('pause_minutes','0'),
            responsibility_text=summary.get('responsibility_text',''),
            handover_text=summary.get('handover_text','无'),
            responsibility_total=summary.get('responsibility_total',''),
            gap_minutes=summary.get('gap_minutes',''),
            balanced=summary.get('balanced',False)))
    return rows

def page(request,slug):
    from datetime import date,timedelta
    stored=store.latest();bundle=ensure_responsibility(stored);history=store.history();calendar=bundle['model']['calendar'];first=date(2026,11,2)
    days=[{'day':(first+timedelta(days=i)).strftime('%d'),'working':(first+timedelta(days=i)).weekday() in calendar['weekdays'] and (first+timedelta(days=i)).isoformat() not in calendar['holidays'],'hours':calendar['hours']} for i in range(7)]
    responsibility=bundle['responsibility']
    counts=[(bundle['intake']['input'],len(bundle['intake']['rows']),len(bundle['intake']['issues'])),(len(bundle['intake']['rows']),len(bundle['contract']['rows']),len(bundle['contract']['issues'])),(len(bundle['contract']['rows']),len(bundle['model']['rows']),len(bundle['model']['issues'])),(len(bundle['model']['rows']),bundle['calculate']['total'],0),(bundle['reconcile']['input'],bundle['quarantine']['total'],bundle['quarantine']['total']),(bundle['reconcile']['input'],bundle['reconcile']['output'],bundle['reconcile']['isolated']),(len(responsibility['rows']),len(responsibility['departments']),len(responsibility['issues'])),(len(history),len(history),0)]
    return templates.TemplateResponse(request=request,name=slug+'.html',context={'bundle':bundle,'responsibility':responsibility,'timing_rows':timing_rows(bundle),'pages':PAGES,'slug':slug,'title':dict(PAGES)[slug],'history':history,'calendar_days':days,'stage_counts':counts,'current_conclusion':store.current_conclusion(),'handover_events':store.handover_events(bundle['manifest']['run_id'])})
