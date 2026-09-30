import json, time, sys, urllib.request
from pathlib import Path
d=json.load(open('espn_weekly.json')); fcp=json.load(open('fc_players.json'))
by_espn={str(p['espnId']):p for p in fcp if p.get('espnId')}
POS={1:'QB',2:'RB',3:'WR',4:'TE'}
want=[]
for x in d['2']['players']:
    pl=x['player']
    if pl.get('defaultPositionId') in POS and str(pl['id']) in by_espn:
        want.append((pl['id'], by_espn[str(pl['id'])]['id']))
out_path=Path('fc_hist.json')
out=json.loads(out_path.read_text()) if out_path.exists() else {}
print(len(want), 'players', len(out), 'cached', flush=True)
for espn_id, fc_id in want:
    if str(espn_id) in out: continue
    req=urllib.request.Request(f'https://api.fantasycalc.com/trades/historical/{fc_id}?isDynasty=false&numQbs=1', headers={'User-Agent':'fantasy-ai/0.1 (personal, low volume)'})
    try:
        data=json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as e:
        print('err', espn_id, e, flush=True); data=None
    out[str(espn_id)]={'fc_id':fc_id,'hist':[h for h in (data or []) if h['date'].startswith(('08/','09/'))]}
    out_path.write_text(json.dumps(out))
    time.sleep(1.0)
print('done', len(out))
