"""Read-only restoration of the existing UP-only PAPER dashboard."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# This entrypoint restores reporting, never the producer or retro initializer.
os.environ["WORKER_ENABLED"] = "false"
os.environ["PGOPTIONS"] = (
    os.environ.get("PGOPTIONS", "")
    + " -c default_transaction_read_only=on -c statement_timeout=20000"
).strip()

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from . import db
from . import main as history_api
from .config import SETTINGS

REVISION = "fusion-222-up-only-readonly-dashboard-2026-11-24"


@asynccontextmanager
async def reporting_lifespan(app: FastAPI):
    if not SETTINGS.up_only:
        raise RuntimeError("This dashboard requires the existing UP-only configuration")
    with db.conn() as connection, connection.cursor() as cursor:
        db._resolve_base_tables(cursor)
    state = db.get_state()
    if not state or not state.get("retro_initialized"):
        raise RuntimeError("Existing initialized UP-only state is required; no reset performed")
    seed = db._up_seed()
    if int(state.get("retro_cutoff_epoch") or 0) != int(seed["cutoff_epoch"]):
        raise RuntimeError("Saved history cutoff differs from the original UP-only replay")
    if int(state.get("retro_trades_count") or 0) != len(seed["bets"]):
        raise RuntimeError("Saved retro count differs from the original UP-only replay")
    yield


app = FastAPI(
    title="FUSION-222 ONLY UP — test history",
    version=REVISION,
    lifespan=reporting_lifespan,
)


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(
        DASHBOARD_HTML,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/healthz")
@app.get("/health")
def health():
    return {
        "ok": db.ping(),
        "service": "fusion-222-up-only",
        "mode": "PAPER",
        "reporting_only": True,
        "worker_enabled": False,
        "build_revision": REVISION,
    }


@app.get("/status")
def status(
    history: str = Query("none", pattern="^(recent|all|none)$"),
    limit: int = Query(30, ge=1),
    offset: int = Query(0, ge=0),
):
    result = history_api.status(history=history, limit=limit, offset=offset)
    latest = db.history(1, 0)
    last_decision = latest[0] if latest else {}
    result.update({
        "mode": "PAPER",
        "reporting_only": True,
        "reporting_revision": REVISION,
        "last_decision_at": last_decision.get("created_at"),
        "last_decision_epoch": last_decision.get("betting_epoch"),
    })
    return history_api._json_safe(result)


# Keep the existing history and CSV contracts and their exact accounting.
for route in history_api.app.router.routes:
    if getattr(route, "path", "").startswith("/history/"):
        app.router.routes.append(route)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#071018">
<title>FUSION-222 ONLY UP — тестовая история</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#eef6fa;background:#071018;font-size:16px;color-scheme:dark}
*{box-sizing:border-box}body{margin:0}main{max-width:1480px;margin:auto;padding:32px 24px 60px}
header{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:28px}
.logo{font-weight:850;letter-spacing:-1px;font-size:26px;color:#4dddb3}.logo small{font-weight:500;font-size:16px;color:#a1b3c3;letter-spacing:0}
h1{font-size:clamp(27px,4vw,42px);margin:10px 0}h2{font-size:22px;margin:0 0 16px}
p{line-height:1.6;color:#adbdca;margin:8px 0}.badge{display:inline-block;background:#14252c;color:#5ce2bc;padding:9px 13px;border-radius:10px;font-size:14px}
button,a.button{display:inline-block;background:#182e3d;color:#eaf5fa;border:1px solid #294353;border-radius:10px;padding:11px 15px;font:inherit;cursor:pointer;text-decoration:none}
button:disabled{opacity:.5;cursor:default}button:hover,a.button:hover{background:#203d50}button.active{border-color:#42d8ad;color:#5ce2bc}
.status{padding:18px 20px;border:1px solid #244357;background:#0c202c;border-radius:14px;margin:24px 0}
.status strong{display:block;margin-bottom:6px}.muted{color:#9aafbe;font-size:14px}.error{background:#321b20;border:1px solid #713742;color:#ffb1b5;padding:16px;border-radius:12px;margin:16px 0}
.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:22px 0}
.card,.panel{background:#0d1922;border:1px solid #20313d;border-radius:16px;padding:20px;min-width:0}
.label{color:#a9bdca;font-size:14px;margin-bottom:14px}.value{font-weight:750;font-size:clamp(23px,2.5vw,34px);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.card .muted{margin-top:10px}.positive{color:#4dddb3}.negative{color:#ff8080}.parts{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:22px}
.parts .value{font-size:26px}.parts p{font-size:14px}.toolbar,.downloads,.tabs{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.toolbar{justify-content:space-between;margin-bottom:18px}
.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px;white-space:nowrap}th{font-weight:500;color:#a9bdca;text-align:right}
td,th{padding:15px 12px;border-bottom:1px solid #20313d;text-align:right;font-variant-numeric:tabular-nums}th:first-child,td:first-child{text-align:left}
td:nth-child(2),th:nth-child(2){text-align:left}.pagination{display:flex;justify-content:space-between;align-items:center;margin-top:18px;gap:15px}
.disclosure{margin-top:24px;font-size:14px}.caption{font-size:14px;margin-top:10px}
@media(max-width:1100px){.metrics{grid-template-columns:repeat(3,1fr)}}
@media(max-width:650px){main{padding:20px 14px 40px}header{align-items:flex-start}.logo small{display:block;margin-top:4px}.metrics{grid-template-columns:repeat(2,1fr)}.card,.panel{padding:16px}.parts{grid-template-columns:1fr}.toolbar{align-items:flex-start}.value{font-size:25px}button,a.button{font-size:14px}.pagination{flex-wrap:wrap}}
</style>
</head>
<body><main>
<header><div class="logo">FUSION <small>Исследовательская лаборатория</small></div><span class="badge">ТЕСТОВАЯ · PAPER</span></header>
<h1>FUSION-222 ONLY UP</h1>
<p>История тестовой стратегии PancakeSwap Prediction. Только направление UP · фиксированная ставка <strong id="stake">—</strong>.</p>
<div class="status"><strong id="connection">Загрузка сохранённой истории…</strong><span id="last-data" class="muted">Проверка базы данных</span><p class="caption">Режим просмотра истории. Тестовая торговля приостановлена.</p></div>
<div id="error" class="error" role="alert" hidden></div>
<div class="toolbar"><span id="period" class="muted">Период истории: —</span><button id="refresh" type="button">Обновить данные</button></div>
<section class="metrics" aria-label="Результаты теста">
<div class="card"><div class="label">Виртуальный банк</div><div class="value" id="bank">—</div><div class="muted" id="start-bank">Начальный банк: —</div></div>
<div class="card"><div class="label">Чистая прибыль</div><div class="value" id="pnl">—</div><div class="muted" id="return">Доходность: —</div></div>
<div class="card"><div class="label">Сделки</div><div class="value" id="trades">—</div><div class="muted" id="wl">Победы / убытки: —</div></div>
<div class="card"><div class="label">Доля побед</div><div class="value" id="wr">—</div><div class="muted">По закрытым сделкам</div></div>
<div class="card"><div class="label">Profit Factor</div><div class="value" id="pf">—</div><div class="muted">Прибыль / убыток</div></div>
<div class="card"><div class="label">Макс. просадка</div><div class="value negative" id="dd">—</div><div class="muted" id="streak">Макс. серия убытков: —</div></div>
</section>
<section class="parts" aria-label="Состав истории">
<div class="card"><div class="label">Ретроспективный пересчёт</div><div class="value" id="retro-pnl">—</div><p id="retro-count">—</p><p>Модельный пересчёт по ранее сохранённым решениям. Эти сделки не исполнялись ботом в момент события.</p></div>
<div class="card"><div class="label">Тест после запуска</div><div class="value" id="forward-pnl">—</div><p id="forward-count">—</p><p>Сохранённые тестовые сделки, принятые работающим PAPER-ботом.</p></div>
</section>
<section class="panel">
<div class="toolbar"><h2>История сделок</h2><div class="tabs"><button class="active" data-mode="combined">Все закрытые</button><button data-mode="retro">Пересчёт</button><button data-mode="live">Тест после запуска</button></div></div>
<div class="table-wrap"><table><thead><tr><th>Время · Дубай</th><th>Epoch</th><th>Источник</th><th>Направление</th><th>Ставка</th><th>Результат</th><th>Чистая прибыль</th><th>Банк после</th></tr></thead><tbody id="history"><tr><td colspan="8">Загрузка…</td></tr></tbody></table></div>
<div class="pagination"><button id="previous" disabled>Предыдущие</button><span id="page" class="muted">—</span><button id="next" disabled>Следующие</button></div>
</section>
<div class="downloads" style="margin-top:20px"><a class="button" href="/history/export-combined.csv">CSV · вся история</a><a class="button" href="/history/export-retro.csv">CSV · пересчёт</a><a class="button" href="/history/export-live.csv">CSV · тест после запуска</a></div>
<p class="disclosure">Банк и прибыль включают ретроспективный пересчёт и последующий тест. Это виртуальные результаты PAPER, не доход реального кошелька. Условия исходной модели: EV ≥ 2%, вероятность ≥ 53%, готовый payout bucket, Shadow Recent, Quality PF ≥ 0,85.</p>
</main>
<script>
(function(){
"use strict";
const $=id=>document.getElementById(id);
const fmt=new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",minimumFractionDigits:2,maximumFractionDigits:2});
const count=new Intl.NumberFormat("ru-RU");
let mode="combined",offset=0,total=0,busy=false;
const limit=50;
function numeric(v){return v!==null&&v!==undefined&&v!==""&&Number.isFinite(Number(v));}
function money(v){return numeric(v)?fmt.format(Number(v)):"—";}
function date(v){if(!v)return"—";const d=new Date(typeof v==="number"?v*1000:v);return Number.isNaN(d.getTime())?"—":d.toLocaleString("ru-RU",{timeZone:"Asia/Dubai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});}
function set(id,value){$(id).textContent=value;}
function colored(id,value){set(id,money(value));$(id).className="value "+(Number(value)>0?"positive":Number(value)<0?"negative":"");}
function esc(v){return String(v??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
async function api(path){const c=new AbortController(),t=setTimeout(()=>c.abort(),30000);try{const r=await fetch(path,{cache:"no-store",signal:c.signal});if(!r.ok)throw Error("HTTP "+r.status);return await r.json();}finally{clearTimeout(t);}}
function pagination(){const n=total?Math.floor(offset/limit)+1:0;set("page","Страница "+n+" из "+Math.ceil(total/limit)+" · записей "+count.format(total));$("previous").disabled=busy||offset===0;$("next").disabled=busy||offset+limit>=total;}
function renderStatus(data){const s=data.paper_state||{};
set("connection","История доступна · торговля приостановлена");
set("last-data","Последнее сохранённое решение: "+date(data.last_decision_at)+" · Дубай, UTC+4");
set("period","Начало истории: "+date(data.version_started_at)+" · Дубай, UTC+4");
set("stake",money(s.fixed_stake));set("bank",money(s.bank));set("start-bank","Начальный банк: "+money(s.start_bank));colored("pnl",s.pnl);
set("return","Доходность: "+(numeric(s.start_bank)&&Number(s.start_bank)>0?(100*Number(s.pnl)/Number(s.start_bank)).toLocaleString("ru-RU",{maximumFractionDigits:2})+"%":"—"));
set("trades",count.format(s.trades_count??0));set("wl","W "+(s.wins??0)+" / L "+(s.losses??0));
set("wr",numeric(s.win_rate)?(100*Number(s.win_rate)).toLocaleString("ru-RU",{maximumFractionDigits:2})+"%":"—");set("pf",numeric(s.profit_factor)?Number(s.profit_factor).toFixed(3)+"×":"—");
set("dd",money(s.max_drawdown));set("streak","Макс. серия убытков: "+(s.max_loss_streak??"—"));
colored("retro-pnl",s.retro_pnl);set("retro-count",count.format(s.retro_trades_count??0)+" закрытых сделок");
colored("forward-pnl",numeric(s.pnl)&&numeric(s.retro_pnl)?Number(s.pnl)-Number(s.retro_pnl):null);
set("forward-count",count.format(Math.max(0,Number(s.trades_count||0)-Number(s.retro_trades_count||0)))+" закрытых сделок · решений "+count.format(data.live_decision_count??0));
}
function renderHistory(data){total=Number(data.count)||0;const rows=data.history||[];
$("history").innerHTML=rows.length?rows.map(x=>{const pnl=x.projected_pnl??x.pnl,bank=x.version_bank_after??x.bank_after,when=x.created_at??x.locked_at_chain_timestamp;
const result=x.outcome||((x.settled===false||!numeric(pnl))?"Ожидает":"—");const origin=x.origin==="RETRO"?"Пересчёт":"Тест";return "<tr><td>"+esc(date(when))+"</td><td>"+esc(x.betting_epoch)+"</td><td>"+origin+"</td><td>"+esc(x.signal)+"</td><td>"+esc(money(x.projected_stake??x.stake))+"</td><td>"+esc(result)+"</td><td class='"+(Number(pnl)>0?"positive":Number(pnl)<0?"negative":"")+"'>"+esc(money(pnl))+"</td><td>"+esc(money(bank))+"</td></tr>";}).join(""):'<tr><td colspan="8">Сохранённых сделок нет</td></tr>';
pagination();}
async function load(all=true){if(busy)return;busy=true;$("refresh").disabled=true;pagination();$("error").hidden=true;set("connection","Чтение сохранённой истории…");
try{const path="/history/"+mode+"?limit="+limit+"&offset="+offset+(mode==="live"?"&trades_only=true":"");
if(all){const [s,h]=await Promise.all([api("/status?history=none"),api(path)]);if(!s.ok||!h.ok)throw Error("Данные недоступны");renderStatus(s);renderHistory(h);}
else{const h=await api(path);if(!h.ok)throw Error("История недоступна");renderHistory(h);set("connection","История доступна · торговля приостановлена");}
}catch(e){set("connection","Ошибка чтения истории");$("error").hidden=false;set("error","Не удалось получить данные: "+(e.name==="AbortError"?"истекло время ожидания":e.message)+". Последние показанные значения сохранены.");}
finally{busy=false;$("refresh").disabled=false;pagination();}}
$("refresh").addEventListener("click",()=>load(true));
document.querySelectorAll("[data-mode]").forEach(b=>b.addEventListener("click",()=>{if(busy)return;mode=b.dataset.mode;offset=0;document.querySelectorAll("[data-mode]").forEach(x=>x.classList.toggle("active",x===b));load(false);}));
$("previous").addEventListener("click",()=>{if(!busy){offset=Math.max(0,offset-limit);load(false);}});
$("next").addEventListener("click",()=>{if(!busy&&offset+limit<total){offset+=limit;load(false);}});
load();
})();
</script></body></html>
"""
