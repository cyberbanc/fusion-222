import concurrent.futures
import json
import time
import urllib.parse
import urllib.request

BASE='https://www.okx.com'


def request(path, **params):
    url=BASE+path+'?'+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={'User-Agent':'Fusion-R3-Paper/1.0','Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=12) as response:
        data=json.load(response)
    if str(data.get('code'))!='0':
        raise RuntimeError('OKX '+str(data.get('code'))+': '+str(data.get('msg'))[:180])
    if not isinstance(data.get('data'),list):raise ValueError('Missing OKX data array')
    return data['data']


class Market:
    def __init__(self):
        self.instruments={}

    def snapshot(self, symbol, after_minute=None):
        inst=symbol+'-USDT-SWAP'
        tasks={
            'ticker':('/api/v5/market/ticker',{'instId':inst}),
            'bars1':('/api/v5/market/candles',{'instId':inst,'bar':'1m','limit':300}),
            'book':('/api/v5/market/books',{'instId':inst,'sz':50}),
            'trades':('/api/v5/market/trades',{'instId':inst,'limit':500}),
            'oi':('/api/v5/public/open-interest',{'instType':'SWAP','instId':inst}),
            'funding':('/api/v5/public/funding-rate',{'instId':inst}),
            'ratio':('/api/v5/rubik/stat/contracts/long-short-account-ratio',{'ccy':symbol,'period':'5m'}),
            'funding_history':('/api/v5/public/funding-rate-history',{'instId':inst,'limit':20}),
        }
        for tf in [5,15,30,60,240]:
            bar={60:'1H',240:'4H'}.get(tf,str(tf)+'m')
            tasks['bars_'+str(tf)]=('/api/v5/market/candles',{'instId':inst,'bar':bar,'limit':300 if tf==5 else 120})
        if symbol not in self.instruments:
            tasks['instrument']=('/api/v5/public/instruments',{'instType':'SWAP','instId':inst})
        result={}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            jobs={pool.submit(request,path,**params):name for name,(path,params) in tasks.items()}
            for future in concurrent.futures.as_completed(jobs):
                name=jobs[future]
                result[name]=future.result()
        if 'instrument' in result:
            self.instruments[symbol]=result['instrument'][0]
        result['instrument']=self.instruments[symbol]
        for name in ['ticker','book','oi','funding']:
            if not result[name]:raise ValueError('Missing required market source: '+name)
            result[name]=result[name][0]
        if not result['ratio']:raise ValueError('No long/short ratio available')
        now=int(time.time())
        # Complete minute bars only. Backfill a bounded outage through paginated
        # history; never silently jump over unobserved stop/TP crossings.
        bars={int(b[0]):b for b in result['bars1'] if b[8]=='1'}
        if after_minute and bars and min(bars)//1000>after_minute+60:
            cursor=min(bars)
            for _ in range(24):
                page=request('/api/v5/market/history-candles',instId=inst,bar='1m',limit=300,after=cursor)
                if not page:break
                for b in page:
                    if b[8]=='1':bars[int(b[0])]=b
                new=min(int(b[0]) for b in page)
                if new>=cursor:break
                cursor=new
                if cursor//1000<=after_minute+60:break
        result['bars1']=sorted(bars.values(),key=lambda b:int(b[0]))
        result['bars_by_timeframe']={}
        for tf in [5,15,30,60,240]:
            result['bars_by_timeframe'][str(tf)]=sorted([b for b in result.pop('bars_'+str(tf)) if b[8]=='1'],key=lambda b:int(b[0]))
        result.update(symbol=symbol,instrument_id=inst,ts=now,source='OKX_PUBLIC',
            source_urls={k:BASE+p+'?'+urllib.parse.urlencode(q) for k,(p,q) in tasks.items()})
        return result
