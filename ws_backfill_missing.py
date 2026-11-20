"""Fetch ONLY missing indexed archives, using existing BWF transport; separate staging."""
import argparse
import json
import time
from pathlib import Path
from bwf_sync import api_get, atomic_write_json, extract_matches
from ws_model import write


def main():
    p=argparse.ArgumentParser(); p.add_argument('--inventory',type=Path,default=Path('data/ws_v3_2/inventory.json')); p.add_argument('--staging',type=Path,required=True); p.add_argument('--limit',type=int,default=5)
    a=p.parse_args(); report=json.loads((a.staging/'report.json').read_text()) if (a.staging/'report.json').exists() else []
    checked={str(r['tournament']['id']) for r in report}
    for t in json.loads(a.inventory.read_text())['missing_indexed_elite'][:a.limit]:
        tid=str(t['id']); dest=a.staging/'matches'/f'{tid}.json.gz'
        if tid in checked or (Path('data/bwf/matches')/dest.name).exists() or dest.exists(): continue
        time.sleep(1.3)
        try:
            payload=api_get('/vue-tournament-matches',{'drawCount':0,'searchKey':'','tmtId':tid,'tmtType':0,'isPara':'false'},10)
            n=len(extract_matches(payload))
            if n: atomic_write_json(dest,payload,zipped=True)
            report.append(dict(tournament=t,status='STAGED' if n else 'EMPTY_NOT_PROOF_OF_CANCELLATION',matches=n))
        except Exception as exc:
            report.append(dict(tournament=t,status='SOURCE_UNAVAILABLE',error=str(exc)))
            break # Respect denial/rate limit; do not retry more tournaments.
    write(a.staging/'report.json',report)
    print(json.dumps(report))

if __name__=='__main__': main()
