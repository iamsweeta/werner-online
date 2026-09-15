"""Manual integration check against official sites; writes the normal runtime cache.

Run from the project root: python -m tests.live_check --output runtime/live_check.json
No fixture or bundled price is accepted as a successful result.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from app.v42_collectors import collect_selected
from app.v42_engine import COMPANIES, quote, live_company_state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='runtime/live_check.json')
    parser.add_argument('--companies',nargs='+',choices=COMPANIES,default=COMPANIES)
    parser.add_argument('--origin',default='Санкт-Петербург')
    parser.add_argument('--destination',default='Москва')
    parser.add_argument('--reverse',action='store_true',help='Also verify the reverse direction')
    args=parser.parse_args()
    from app.v42_engine import is_supported_route
    if not is_supported_route(args.origin,args.destination):parser.error('Choose two distinct cities from /api/options')
    output={'version':'46.0','started_at':datetime.now(timezone.utc).isoformat(),'profile':'w100','results':[]}
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    for origin,destination in [(args.origin,args.destination)]+([(args.destination,args.origin)] if args.reverse else []):
        start=time.monotonic()
        rows=collect_selected(args.companies,origin,destination,'w100')
        for row in rows:
            q=quote(row['company'],origin,destination,'w100')
            success=row['ok'] and q.get('online') is True
            output['results'].append({'origin':origin,'destination':destination,**row,'online_at_profile':success,
                'price':q.get('price') if success else None,
                'kind':('lower_bound' if q.get('price_is_minimum') else 'exact') if success else 'unavailable',
                'url':q.get('source_url'),'transport':q.get('transport'),'captured_at':q.get('captured_at'),
                'calculation_basis':q.get('calculation_basis'),
                'partial_errors':live_company_state(origin,destination,row['company']).get('partial_errors',[])})
        output['finished_at']=datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
        print('ROUTE_DONE',origin,'seconds',round(time.monotonic()-start,1),'success',sum(x['ok'] for x in rows),flush=True)


if __name__=='__main__':main()
