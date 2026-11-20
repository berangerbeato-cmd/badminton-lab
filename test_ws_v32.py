import copy
import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from elo_model import Match, load_matches
from ws_data import parse, players, load_ws
from ws_model import forecast, daily
from badminton_v3_2_walkforward import build_rows_raw
from badminton_features_v3 import fit_variant, predict
from badminton_singles_value import decision,resolve
from singles_ledger import validate,summary

class WS(unittest.TestCase):
    def setUp(self):
        self.raw=dict(id=1,draw_name='WS',winner=1,start_time=1704067200,code_status=0,
            t1p1_detail={'id':1,'name_display':'A','gender_id':2},t2p1_detail={'id':2,'name_display':'B','gender_id':2},
            team1Score='<span>21</span><span>21</span>',team2Score='<span>19</span><span>12</span>')
        self.m=lambda k,d,w:Match(k,d,'T','R1','1','A','2','B',w,'21-19 21-12' if w==1 else '19-21 12-21')
        self.now=datetime(2026,11,20,12,tzinfo=timezone.utc)
    def test_parse(self):
        self.assertEqual(parse('1','T',self.raw,'2026-11-20').p1_id,'1')
    def test_discipline_and_gender(self):
        for field,val in [('draw_name','MS'),('draw_name','WD'),('t1p2_detail',{'id':3}),('t1p1_detail',{'id':1,'gender_id':1})]:
            with self.subTest(field=field,val=val),self.assertRaises(ValueError): players(dict(self.raw,**{field:val}))
    def test_invalid_scores_status_date(self):
        for change in [dict(team1Score='<span>22</span><span>21</span>'),dict(winner=2),dict(code_status=1),
                       dict(status_name='Retired'),dict(start_time=1795219200),dict(t2p1_detail={'id':1}),
                       dict(team1Score='<span>21</span><span>21</span><span>21</span>',team2Score='<span>1</span><span>2</span><span>3</span>')]:
            with self.subTest(change=change),self.assertRaises(ValueError): parse('1','T',dict(self.raw,**change),'2026-11-20')
    def test_thirty_point_game(self):
        raw=dict(self.raw,team1Score='<span>30</span><span>21</span>',team2Score='<span>29</span><span>12</span>')
        self.assertIsNotNone(parse('1','T',raw,'2026-11-20'))
    def test_no_same_day_or_future_leak(self):
        ms=[self.m('1','2024-01-01',1),self.m('2','2024-01-01',2),self.m('3','2024-01-02',1)]
        rows=build_rows_raw(ms,32)
        self.assertEqual(rows[0]['raw_p_a'],rows[1]['raw_p_a'])
        changed=build_rows_raw(ms[:2]+[self.m('3','2024-01-02',2)],32)
        self.assertEqual(rows[:2],changed[:2])
        self.assertEqual(rows[2]['raw_p_a'],changed[2]['raw_p_a'])
    def test_calibration_finite(self):
        rows=build_rows_raw([self.m(str(i),f'2024-01-{i+1:02d}',1 if i%3 else 2) for i in range(20)],32)
        model=fit_variant(rows,[])
        self.assertTrue(all(0<predict(r,model)<1 for r in rows))
    def test_unknown_player_no_probability(self):
        m=[self.m('1','2026-11-19',1)]
        f=dict(player_a_id='1',player_b_id='3',start_utc='2026-11-21T12:00:00+00:00')
        r=forecast(m,dict(discipline='WS',model_id='x',k=32,fit_end='2025-12-31'),f,self.now)
        self.assertIsNone(r['p_a'])
    def test_ev_and_quality(self):
        q=dict(odds_a=1.9,odds_b=2.0,observed_at='2026-11-20T11:50:00+00:00',start_utc='2026-11-21T12:00:00+00:00',bookmaker='TEST',evidence='fixture',start_verified=True,kickoff_source='fixture')
        r=decision(dict(p_a=.6,status='MODEL_READY'),q,self.now)
        self.assertAlmostEqual(r['ev_a'],.14); self.assertEqual(r['decision'],'BET')
        for change in [dict(odds_a=None),dict(odds_a=float('nan')),dict(start_verified=False),dict(observed_at='2026-11-19T12:00:00+00:00')]:
            self.assertEqual(decision(dict(p_a=.6,status='MODEL_READY'),dict(q,**change),self.now)['decision'],'NO BET')
        self.assertEqual(decision(dict(p_a=.6,status='STALE_NO_BET'),q,self.now)['decision'],'NO BET')
    def test_ambiguous_identity(self):
        m=[self.m('1','2024-01-01',1),Match('2','2024-01-02','T','','3','A','4','C',1,'21-1 21-1')]
        self.assertIsNone(resolve('A',None,m)); self.assertEqual(resolve('A','1',m),'1')
    def test_real_archive_isolation_and_frozen_model(self):
        ws,q,_,_=load_ws(); ms,_=load_matches(Path('data/bwf'))
        self.assertEqual(q['raw_ws'],q['valid_ws']+sum(q['rejected'].values()))
        self.assertFalse({p for m in ws for p in (m.p1_id,m.p2_id)} & {p for m in ms for p in (m.p1_id,m.p2_id)})
        artifact=json.loads(Path('data/ws_v3_2/model.json').read_text())
        self.assertEqual(artifact['fit_end'],'2025-12-31'); self.assertEqual(artifact['discipline'],'WS')
    def test_ledger_append_settle_and_duplicate(self):
        from singles_ledger import main
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'ledger'; record=Path(td)/'record.json'; event=Path(td)/'event.json'
            r=dict(id='example',match_id='1',discipline='WS',kind='simulation',prediction_at='2026-11-19T10:00:00Z',placed_at='2026-11-19T11:00:00Z',start_utc='2026-11-19T12:00:00Z',stake=10,odds=2,p_model=.6,result='pending',prediction_evidence='synthetic test')
            record.write_text(json.dumps(r))
            with patch('sys.argv',['ledger','--root',str(root),'--append',str(record)]): main()
            with patch('sys.argv',['ledger','--root',str(root),'--append',str(record)]),self.assertRaises(FileExistsError): main()
            event.write_text(json.dumps(dict(id='example',discipline='WS',kind='simulation',result='won',settled_at='2026-11-19T14:00:00Z',evidence='synthetic test')))
            with patch('sys.argv',['ledger','--root',str(root),'--settle',str(event)]): main()
            self.assertEqual(json.loads((root/'WS/simulation/example.json').read_text())['result'],'pending')
            self.assertEqual(json.loads((root/'summary.json').read_text())['summary']['simulation_WS']['net'],10)

    def test_ledger_separation(self):
        r=dict(id='a',match_id='1',discipline='WS',kind='actual',prediction_at='2026-11-19T10:00:00Z',placed_at='2026-11-19T11:00:00Z',start_utc='2026-11-19T12:00:00Z',stake=10,odds=2,p_model=.6,result='won',prediction_evidence='test',bet_receipt='test')
        result=summary([validate(r),validate(dict(r,id='b',kind='simulation',result='lost'))])
        self.assertEqual(result['actual_WS']['net'],10); self.assertEqual(result['actual_MS']['bets'],0)
        with self.assertRaises(ValueError): validate(dict(r,placed_at='2026-11-20T11:00:00Z'))

if __name__=='__main__': unittest.main()
