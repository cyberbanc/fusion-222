import collections
import csv
import datetime as dt
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs
from .config import CONFIG,REASONS
from .storage import Store,KINDS
from .market import Market
from .engine import Engine


def json_bytes(obj):return json.dumps(obj,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()


def summary(store):
    now=int(time.time());all_accounts=store.rows('account',limit=None)
    accounts=[a for a in all_accounts if a['account_id'] in CONFIG['accounts']]
    for a in accounts:
        a['profit_factor']=a['gross_profit']/a['gross_loss'] if a['gross_loss']>0 else None
        a['win_rate']=a['wins']/a['trades'] if a['trades'] else None
        a['pnl']=a['equity']-a['initial_bank']
        a['settled_pnl']=a['bank']-a['initial_bank']
        a['decisions']=store.count('decision',a['account_id'])
        a['positions']=store.rows('position',account=a['account_id'],status='OPEN',limit=2)
        a['next_signal_ts']=(now//CONFIG['decision_interval_seconds']+1)*CONFIG['decision_interval_seconds']
        entries=store.rows('position',account=a['account_id'],limit=None)
        new=[p for p in entries if p.get('entry_policy_id')==CONFIG['entry_policy_id']]
        activation=store.get('meta','policy_'+CONFIG['entry_policy_id'],{}).get('started_ts',now)
        hours=max((now-activation)/3600,1/60)
        a['cadence']={'entries_last_hour':sum(now-3600<p['entry_ts']<=now for p in new),
            'entries_since_policy':len(new),'average_entries_per_hour':len(new)/hours,
            'elapsed_hours':hours,'policy_started_ts':activation,
            'closed_pnl':sum(p.get('pnl',0) for p in new if p['status']=='CLOSED'),
            'provisional_entries':sum(p.get('learning_phase')=='FIXED_FALLBACK_UNVALIDATED' for p in new)}
    runtime=store.get('meta','runtime',{})
    runtime['stale']=now-runtime.get('last_poll_ts',0)>120
    return {'server_ts':now,'config':CONFIG,'reasons':REASONS,'runtime':runtime,
        'archived_account_count':len(all_accounts)-len(accounts),
        'started':store.get('meta','started',{}),'accounts':accounts,
        'markets':{s:store.get('meta','latest_'+s) for s in CONFIG['symbols']},
        'history_counts':{k:store.count(k) for k in ['decision','position','trial','signal','snapshot','candle1','study']}}


def study_summary(store,account):
    result={};studies=store.rows('study',account=account,limit=None)
    for study in studies:
        for key,case in study['cases'].items():
            cell=result.setdefault(key,{'tp_r':case['study_tp_r'],'stop_r':case['study_stop_r'],
                'closed':0,'pending':0,'pnl_r':0.0,'pnl_usd':0.0,'wins':0,
                'gross_profit_r':0.0,'gross_loss_r':0.0,'ambiguous':0})
            if case['status']!='CLOSED':cell['pending']+=1;continue
            cell['closed']+=1;cell['pnl_r']+=case['pnl_r'];cell['pnl_usd']+=case['pnl']
            cell['wins']+=int(case['pnl']>0)
            cell['gross_profit_r']+=max(case['pnl_r'],0);cell['gross_loss_r']+=max(-case['pnl_r'],0)
            cell['ambiguous']+=int(bool(case.get('excursion_order_ambiguous')))
    for c in result.values():
        c['pf']=c['gross_profit_r']/c['gross_loss_r'] if c['gross_loss_r']>0 else None
    positions=store.rows('position',account=account,status='CLOSED',limit=None)
    excursions={'closed':len(positions),'near_tp_reversals':sum(bool(p.get('near_tp_reversal')) for p in positions),
        'near_tp_reversals_possible':sum(bool(p.get('near_tp_reversal_possible')) for p in positions),
        'missed_2r':sum(bool(p.get('missed_2r')) for p in positions),
        'missed_2_5r':sum(bool(p.get('missed_2_5r')) for p in positions),
        'ambiguous':sum(bool(p.get('excursion_order_ambiguous')) for p in positions)}
    return {'account_id':account,'cells':list(result.values()),'excursions':excursions,'note':CONFIG['study_note']}


def history_archive(store):
    output=tempfile.SpooledTemporaryFile(max_size=8*1024*1024,mode='w+b')
    counts={};hashes={};export_ts=int(time.time())
    with store.export_snapshot() as reader,zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=5) as z:
        for kind in sorted(KINDS):
            name=kind+'.jsonl';h=hashlib.sha256();count=0
            with z.open(name,'w') as f:
                for batch in reader.export_batches(kind):
                    for row in batch:
                        b=json_bytes(row)+b'\n';h.update(b);f.write(b);count+=1
            counts[kind]=count;hashes[name]=h.hexdigest()
        for kind in ['decision','position','trial','equity']:
            # All fields retained, including nested excursion/threshold metadata.
            keys=set()
            for batch in reader.export_batches(kind):
                for row in batch:keys.update(row)
            keys=sorted(keys or ({'created_ts','account_id','signal','reason','executed'} if kind=='decision' else
                {'entry_ts','account_id','side','entry_price','stop_price','take_price','pnl','mfe_r','mae_r','tp_price_progress'} if kind in ['position','trial'] else
                {'ts','account_id','bank','equity','drawdown_usd','drawdown_pct'}))
            with z.open(kind+'.csv','w') as binary:
                text=io.TextIOWrapper(binary,encoding='utf-8-sig',newline='',write_through=True)
                writer=csv.DictWriter(text,fieldnames=keys)
                writer.writeheader()
                for batch in reader.export_batches(kind):
                    for row in batch:
                        writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})
                text.flush();text.detach()
        manifest={'exported_ts':export_ts,'exported_utc':dt.datetime.fromtimestamp(export_ts,dt.timezone.utc).isoformat(),
            'mode':'PAPER','version':CONFIG['version'],'counts':counts,'sha256_jsonl':hashes,
            'config':CONFIG,'consistency':'Repeatable-read database snapshot; full history, no pagination cutoff.',
            'excursion_note':'mfe_r/mae_r are confirmed observations; *_upper_r are candle bounds. Closing-minute order can be ambiguous. TP price progress and net-R progress are separate.',
            'study_note':CONFIG['study_note'],'source':'OKX public market data; simulated fills and fee assumptions.',
            'accounts':CONFIG['bank_note'],'active_accounts':CONFIG['accounts']}
        z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        z.writestr('CURRENT_POLICY_RU.txt','Текущая экспериментальная политика CADENCE_30M_V1.\n'
            'Проверка каждые 5 минут; выбранный ТФ задаёт контекст. Пауза между входами 30 минут, максимум 2 за скользящий час, удержание до 30 минут. Минимум частоты и положительное преимущество не гарантированы.\n'
            'Синтетическая бумажная модель без проверки обеспечения: TP +$22 / SL -$7.3333 до будущего funding. Объём и required_exposure_to_bank записаны в позиции; margin_usd/leverage=null. Это не подтверждение доступности исполнения на реальной бирже с данным банком.\n'
            'Стоп от ATR пятиминутных свечей, с нижней границей по расходам. Сохраняются комиссии, проскальзывание, спред, funding, шаг контракта и глубина стакана. Банк меньше денежного риска блокирует вход.\n'
            '53%, EV, прогрев 15 Shadow и качество теперь диагностические метрики; они не запрещают новые экспериментальные paper-входы. Старые исходы не смешиваются с калибровкой новых правил.\n'
            'LEARNED при отсутствии проверенной модели использует явно помеченный FIXED_FALLBACK_UNVALIDATED; это не проверенная обученная стратегия. После успешной проверки используются обученные веса.\n'
            'Записи до обновления и открытые позиции сохраняют исходную версию и временной лимит. Ретроспективные результаты не вставляются в торговую историю. Исторические правила описаны в README_RU.txt; текущие — в CURRENT_POLICY_RU.txt и manifest.config.\n')
        z.writestr('README_RU.txt','FUSION R3 — полная история бумажной торговли.\n'
            '24 активных независимых счёта: SOL/ETH × 5/15/30/60 минут × long-only/long+short/short-only.\n'
            'С версии 1.3.1 прежние 8 TREND-счетов отключены для новых входов и скрыты с основного дэшборда. Их счета, решения, Shadow, позиции и капитал сохранены в этом архиве. Трендовая лаборатория с 24 новыми вариантами имеет отдельный сервис, таблицу, дэшборд и экспорт.\n'
            'Трендовые счета добавлены с версии 1.3 и начинают с $500 в момент активации. Тренд: EMA20/50, ADX14/DMI; флэт: ADX<=20; пробой канала предыдущих 20 свечей с буфером 0.1 ATR по закрытию. Отдельная калибровка Shadow, без подстановки прошлых результатов.\n'
            'С версии 1.1 торговые позиции имеют фиксированную стоимость $22 с округлением количества вниз по шагу контракта. Плановый риск по стопу меняется вместе с ATR и расходами.\n'
            'С версии 1.2 новые входы имеют цель +$22 после модельных торговых расходов и плановый стоп -$7.3333 до будущего funding. Объём рассчитывается от 1.5 ATR; после округления количества стоп уточняется до денежного риска. При превышении лимита залога/плеча вход пропускается.\n'
            'Версия и денежные параметры зафиксированы в каждой позиции. Старые позиции и результаты сохранены. Фактический убыток может превысить план при гэпе или задержке опроса; funding меняет итог.\n'
            'Теневые испытания сохраняют нормированный риск $1 для сопоставимой калибровки. Старые записи не пересчитываются.\n'
            'В архиве все решения, позиции, теневые испытания, исходные снимки, свечи, модельные настройки и альтернативные выходы.\n'
            'position.csv: mfe_r — подтверждённый максимум чистой прибыли в R; mae_r — подтверждённый минимум; верхние границы по свечам помечены отдельно.\n'
            'tp_price_progress=0.8 означает прохождение 80% расстояния от входа до целевой цены.\n'
            'near_tp_reversal — подтверждённое прохождение 80% цели с последующим стопом.\n'
            'study.jsonl: альтернативные тейки 1.5/2/2.5/3 R и стопы 0.75/1/1.25/1.5 R при одинаковых входах и количестве монет.\n'
            'Это анализ выходов на тех же сделках, а не полный бэктест новых стратегий: альтернативное раннее закрытие могло бы разрешить другие будущие входы.\n'
            'При неизвестном порядке касаний в минутной свече выбран стоп первым. Внутри первой неполной минуты учитываются только наблюдаемые котировки.\n'
            'Параметры комиссий и проскальзывания — допущения. Funding учитывается по опубликованной ставке с минутным приближением цены.\n'
            'Исходные JSONL содержат вложенные данные полностью. Их SHA256 и количества указаны в manifest.json.\n')
    output.seek(0)
    return output,counts


class Handler(BaseHTTPRequestHandler):
    store=None
    def log_message(self,*args):pass
    def headers_out(self,code=200,content_type='application/json; charset=utf-8',length=None):
        self.send_response(code)
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control','no-store')
        origin=self.headers.get('Origin','')
        allowed=os.getenv('ALLOWED_ORIGIN','')
        if origin and origin==allowed:self.send_header('Access-Control-Allow-Origin',origin)
        self.send_header('Vary','Origin')
        self.send_header('Access-Control-Expose-Headers','Content-Disposition, Content-Length')
        self.send_header('X-Content-Type-Options','nosniff')
        if length is not None:self.send_header('Content-Length',str(length))
    def send_json(self,data,code=200):
        b=json_bytes(data);self.headers_out(code,length=len(b));self.end_headers();self.wfile.write(b)
    def do_OPTIONS(self):
        self.headers_out(204);self.send_header('Access-Control-Allow-Methods','GET, OPTIONS');self.end_headers()
    def do_GET(self):
        try:self.get()
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as exc:
            self.store.event('ERROR','Ошибка чтения API',error=type(exc).__name__,path=self.path[:120])
            self.send_json({'error':'Не удалось получить данные. Повторите запрос.'},500)
    def get(self):
        path=urlparse(self.path);q=parse_qs(path.query)
        account=q.get('account',['SOL_15_LONG'])[0]
        if account not in CONFIG['accounts']:
            return self.send_json({'error':'Unknown account'},400)
        if path.path in ['/','/health']:
            return self.send_json({'ok':self.store.ping(),'database_connected':True,'mode':'PAPER','version':CONFIG['version']})
        if path.path=='/api/summary':return self.send_json(summary(self.store))
        if path.path=='/api/studies':return self.send_json(study_summary(self.store,account))
        if path.path=='/api/equity':
            rows=self.store.rows('equity',account=account,limit=2000)
            return self.send_json({'rows':list(reversed(rows))})
        if path.path=='/api/history':
            kind=q.get('kind',['position'])[0]
            if kind not in ['position','decision']:return self.send_json({'error':'Unknown history kind'},400)
            limit=max(1,min(200,int(q.get('limit',['50'])[0])))
            before=int(q['before'][0]) if 'before' in q else None
            rows=self.store.rows(kind,account=account,limit=limit,before=before)
            return self.send_json({'rows':rows,'total':self.store.count(kind,account),
                'next_before':rows[-1]['created_ts'] if len(rows)==limit else None})
        if path.path=='/exports/history.zip':
            output,_=history_archive(self.store)
            output.seek(0,2);length=output.tell();output.seek(0)
            self.headers_out(content_type='application/zip',length=length)
            self.send_header('Content-Disposition','attachment; filename="fusion-r3-full-history.zip"')
            self.end_headers()
            try:
                while True:
                    chunk=output.read(128*1024)
                    if not chunk:break
                    self.wfile.write(chunk)
            finally:output.close()
            return
        return self.send_json({'error':'Not found'},404)


def main():
    if not os.getenv('DATABASE_URL'):raise RuntimeError('DATABASE_URL is required for persistent history')
    store=Store();Handler.store=store
    engine=Engine(store,Market())
    if os.getenv('WORKER_ENABLED','true').lower()=='true':
        threading.Thread(target=engine.run,daemon=True,name='r3-paper-worker').start()
    server=ThreadingHTTPServer(('0.0.0.0',int(os.getenv('PORT','8000'))),Handler)
    print(json.dumps({'service':'FUSION R3','mode':'PAPER','accounts':len(CONFIG['accounts']),'database':True}),flush=True)
    try:server.serve_forever()
    finally:engine.stop.set();server.server_close()


if __name__=='__main__':main()
