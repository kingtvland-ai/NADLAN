# src/facebook_marketplace_scraper/dashboard.py
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator

from . import __version__
from .city_resolver import resolve_city_id
from .israel_cities import ISRAEL_CITY_IDS
from .llm_classifier import LlamaClassifierSettings, LocalLlamaClassifier
from .models import SearchSpec, Watchlist
from .notifications import NotificationManager, NotificationSettings
from .service import MarketplaceCollector
from .storage import LATEST_SCHEMA_VERSION, MarketplaceStore

logger = logging.getLogger(__name__)

DEFAULT_SESSION = Path("data/facebook_storage_state.json")

# The query text and minimum price are always fixed by this dashboard; only
# the city is user-selected. Requests carry a plain city name, which the
# backend resolves to Facebook's numeric city_id via israel_cities.py.
FIXED_QUERY_BASE = "מכירות בתים"
FIXED_MIN_PRICE = 400_000.0
FIXED_RADIUS_KM = 5.0


class WatchlistWrite(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    city_name: str = Field(min_length=1, max_length=64, description="Official Hebrew city name, see israel_cities.py")
    category_id: str | None = None
    max_price: float | None = Field(default=None, ge=0)
    target_price: float | None = Field(default=None, ge=0)
    max_items: int = Field(default=50, ge=1, le=500)
    default_currency: str = Field(default="ILS", min_length=3, max_length=8)
    interval_seconds: int = Field(default=1800, ge=60)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_price_range(self) -> WatchlistWrite:
        if self.max_price is not None and self.max_price < FIXED_MIN_PRICE:
            raise ValueError(f"max_price cannot be less than the fixed min_price ({FIXED_MIN_PRICE:g})")
        return self


class WatchlistPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    city_name: str | None = Field(default=None, min_length=1, max_length=64)
    category_id: str | None = None
    max_price: float | None = Field(default=None, ge=0)
    target_price: float | None = Field(default=None, ge=0)
    max_items: int | None = Field(default=None, ge=1, le=500)
    default_currency: str | None = Field(default=None, min_length=3, max_length=8)
    interval_seconds: int | None = Field(default=None, ge=60)
    enabled: bool | None = None


class SearchRunRequest(BaseModel):
    city_name: str = Field(min_length=1)
    max_price: float | None = Field(default=None, ge=0)
    category_id: str | None = None
    max_items: int = Field(default=30, ge=1, le=500)
    default_currency: str = Field(default="ILS", min_length=3, max_length=8)

    @model_validator(mode="after")
    def validate_price_range(self) -> SearchRunRequest:
        if self.max_price is not None and self.max_price < FIXED_MIN_PRICE:
            raise ValueError(f"max_price cannot be less than the fixed min_price ({FIXED_MIN_PRICE:g})")
        return self


_DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Marketplace Research Dashboard</title><style>
:root{color-scheme:dark;background:#0b0f14;color:#e7edf5;font-family:system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;background:#0b0f14}.wrap{max-width:1480px;margin:auto;padding:24px}.muted{color:#91a0b4}.ok{color:#6ee7a0}.bad{color:#ff8a8a}h1,h2{margin:0 0 8px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}.card,.panel{background:#121923;border:1px solid #243044;border-radius:12px;padding:16px}.value{font-size:25px;font-weight:700}.grid{display:grid;grid-template-columns:minmax(320px,420px) 1fr;gap:16px;margin-bottom:20px}@media(max-width:900px){.grid{grid-template-columns:1fr}}form{display:grid;grid-template-columns:1fr 1fr;gap:10px}form .wide{grid-column:1/-1}label{font-size:12px;color:#91a0b4}input,button,select{width:100%;border-radius:8px;border:1px solid #34445d;background:#0b111a;color:#e7edf5;padding:10px}button{cursor:pointer;background:#1b2b40}button.danger{background:#3c1c22}button.primary{background:#1c4c3c}button.small{width:auto;padding:6px 9px;margin-right:5px}table{width:100%;border-collapse:collapse;background:#121923;border-radius:12px;overflow:hidden;margin-bottom:18px}th,td{padding:10px;border-bottom:1px solid #243044;text-align:left;vertical-align:top}th{color:#91a0b4}.score{font-weight:700}.price{white-space:nowrap}a{color:#70b7ff;text-decoration:none}img{width:64px;height:48px;object-fit:cover;border-radius:6px;background:#202b3b}.error{max-width:420px;white-space:normal;color:#ff9d9d}.pill{display:inline-block;border:1px solid #34445d;border-radius:999px;padding:2px 7px;margin:2px;font-size:12px;color:#b8c5d8}#runStatus{margin-top:8px;font-size:13px}.details-row td{background:#0d141e}.details{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:6px 16px;padding:6px 0;font-size:13px}.details b{color:#91a0b4;font-weight:600}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0 14px}.filters input,.filters select{width:auto;min-width:150px}.colToggle{display:flex;align-items:center;gap:5px;font-size:12px;color:#91a0b4;width:auto}.colToggle input{width:auto}
th.sortable{cursor:pointer;user-select:none}
.chartbars{margin:6px 0 18px;display:flex;flex-direction:column;gap:6px}.chartrow{display:grid;grid-template-columns:120px 1fr 34px;gap:8px;align-items:center;font-size:12px}.chartlabel{color:#b8c5d8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.chartbar{background:#0b111a;border-radius:6px;overflow:hidden;height:14px}.chartfill{background:#2a6f57;height:100%}.chartcount{color:#91a0b4;text-align:right}
.clickable{cursor:pointer}.clickable:hover{background:#16202e}
.modal-overlay{position:fixed;inset:0;background:rgba(4,7,11,.7);display:flex;align-items:center;justify-content:center;z-index:50;padding:16px}.modal-overlay[hidden]{display:none}.modal{background:#121923;border:1px solid #243044;border-radius:12px;padding:20px;max-width:480px;width:100%;position:relative}.modal-close{position:absolute;top:8px;right:8px;background:none;border:none;color:#91a0b4;font-size:20px;cursor:pointer;width:auto;padding:4px 8px}.modal img{width:100%;height:180px;object-fit:cover;border-radius:8px;margin-bottom:10px;background:#202b3b}
</style></head><body><div class="wrap"><h1>Marketplace Research Dashboard</h1><div class="muted">Collection, valuation, watchlists, health, notifications and run timing</div><div id="stats" class="stats"></div>
<div class="grid"><section class="panel"><h2>Run a scan now</h2><form id="runForm"><div class="wide"><label>City</label><select id="runCity" required></select></div><div><label>Max price</label><input id="runMaxPrice" type="number" min="0" step="1"></div><div><label>Category id (optional)</label><input id="runCategoryId" placeholder="1270772586445798"></div><div><label>Max items</label><input id="runMaxItems" type="number" min="1" max="500" value="30"></div><div class="wide"><button class="primary" type="submit">הרץ סריקה עכשיו</button></div></form><div id="runStatus" class="muted"></div></section><section class="panel"><h2 id="formTitle">Add watchlist</h2><form id="watchForm"><input id="watchId" type="hidden"><div class="wide"><label>Name</label><input id="name" required maxlength="120"></div><div class="wide"><label>City</label><select id="watchCity" required></select></div><div><label>Max price</label><input id="maxPrice" type="number" min="0" step="0.01"></div><div><label>Target price</label><input id="targetPrice" type="number" min="0" step="0.01"></div><div><label>Category id (optional)</label><input id="categoryId"></div><div><label>Interval minutes</label><input id="interval" type="number" min="1" value="30"></div><div><label>Max items</label><input id="maxItems" type="number" min="1" max="500" value="50"></div><div><label>Currency</label><input id="currency" value="ILS" maxlength="8"></div><div class="wide"><button type="submit">Save watchlist</button></div><div class="wide"><button id="cancelEdit" type="button" hidden>Cancel edit</button></div></form></section></div>
<h2>Listings <button class="small danger" onclick="deleteAllListings()">מחק הכל</button></h2>
<div class="filters"><input id="filterSearch" placeholder="חיפוש בכותרת..."><select id="filterCategory"><option value="">כל הקטגוריות</option></select><input id="filterMinScore" type="number" min="0" max="100" placeholder="ציון מינימלי"><label class="colToggle"><input type="checkbox" id="colMetadata" checked> מטא-דאטה</label><label class="colToggle"><input type="checkbox" id="colConfidence" checked> ביטחון</label></div>
<div id="categoryChart"></div>
<table id="listingsTable"><thead><tr><th></th><th>Listing</th><th class="colMetadataHeader">Metadata</th><th class="sortable" data-sort="price" data-label="Price" onclick="setSort('price')">Price</th><th class="sortable" data-sort="score" data-label="Score" onclick="setSort('score')">Score</th><th class="colConfidenceHeader sortable" data-sort="confidence" data-label="Confidence" onclick="setSort('confidence')">Confidence</th><th class="sortable" data-sort="last_seen" data-label="Last seen" onclick="setSort('last_seen')">Last seen</th><th></th></tr></thead><tbody id="rows"></tbody></table>
<div id="emptyState" class="muted" style="display:none;padding:4px 0 18px">אין מודעות התואמות את הסינון.</div>
<div class="grid"><section class="panel"><h2>Watchlists</h2><div id="watchlists"></div></section><section class="panel"><h2>Recent runs</h2><div id="runs"></div></section></div>
<div class="grid"><section class="panel"><h2>Recent notifications</h2><div id="notifications"></div></section></div></div>
<div id="notifModal" class="modal-overlay" hidden><div class="modal"><button class="modal-close" onclick="closeNotifModal()">×</button><div id="notifModalBody"></div></div></div>
<script>
const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));const num=v=>v===''?null:Number(v);let watchCache=[];let cityCache={};let listingCache={};let listingsData=[];let sortKey='score';let sortDir='desc';let filters={search:'',category:'',minScore:0};async function api(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});if(!r.ok){let d={};try{d=await r.json()}catch{}throw new Error(d.detail||r.statusText)}return r.status===204?null:r.json()}
async function loadCities(){cityCache=await api('/api/cities');const opts=cityCache.slice().sort().map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join('');document.getElementById('runCity').innerHTML=opts;document.getElementById('watchCity').innerHTML=opts}
async function load(){const [s,l,w,h,n,runs]=await Promise.all([api('/api/stats'),api('/api/listings?limit=100'),api('/api/watchlists'),api('/api/health'),api('/api/notifications?limit=20'),api('/api/runs?limit=20')]);watchCache=w;const daemon=h.daemon||{};const healthClass=h.status==='ok'?'ok':'bad';const cards={...s,health:`<span class="${healthClass}">${esc(h.status)}</span>`,daemon:esc(daemon.effective_state||'unknown')};document.getElementById('stats').innerHTML=Object.entries(cards).map(([k,v])=>`<div class="card"><div class="muted">${esc(k.replaceAll('_',' '))}</div><div class="value">${typeof v==='string'&&v.startsWith('<span')?v:esc(v)}</div></div>`).join('');
document.getElementById('watchlists').innerHTML=w.length?`<table><tbody>${w.map(x=>`<tr><td><b>${esc(x.name)}</b><div class="muted">${esc(x.query)} · ${esc(x.city_id)}</div>${x.last_error?`<div class="error">${esc(x.last_error)}</div>`:''}</td><td>${Math.round(x.interval_seconds/60)}m<br>${x.enabled?'enabled':'disabled'}</td><td><button class="small" onclick="editWatch(${x.id})">Edit</button><button class="small" onclick="toggleWatch(${x.id},${!x.enabled})">${x.enabled?'Disable':'Enable'}</button><button class="small danger" onclick="removeWatch(${x.id})">Delete</button></td></tr>`).join('')}</tbody></table>`:'<div class="muted">No watchlists yet.</div>';
listingsData=l;populateCategoryFilter();renderRows();
document.getElementById('notifications').innerHTML=n.length?`<table><tbody>${n.map(x=>`<tr class="clickable" onclick="openNotifModal('${esc(x.listing_id)}')"><td><b>${esc(x.payload?.title||x.listing_id)}</b><div class="muted">${esc(x.event_type)} · score ${esc(x.score)} · ${esc(x.created_at)}</div></td></tr>`).join('')}</tbody></table>`:'<div class="muted">No notification events.</div>';document.getElementById('runs').innerHTML=runs.length?`<table><tbody>${runs.map(x=>`<tr><td>${esc(x.query)}<div class="muted">${esc(x.extracted)} extracted / ${esc(x.normalized)} stored</div></td><td>${x.duration_ms==null?'—':Math.round(x.duration_ms)+' ms'}</td></tr>`).join('')}</tbody></table>`:'<div class="muted">No search runs.</div>'}
function applyFilters(arr){const q=filters.search.trim().toLowerCase();return arr.filter(x=>{if(q&&!String(x.title||'').toLowerCase().includes(q))return false;if(filters.category&&x.category!==filters.category)return false;if(filters.minScore&&Number(x.deal_score)<filters.minScore)return false;return true})}
function sortVal(x,key){switch(key){case 'confidence':return Number(x.score_confidence)||0;case 'last_seen':return x.last_seen||'';case 'price':return x.latest_price_value==null?-Infinity:Number(x.latest_price_value);default:return Number(x.deal_score)||0}}
function sortedListings(arr){const dir=sortDir==='asc'?1:-1;return [...arr].sort((a,b)=>{const av=sortVal(a,sortKey),bv=sortVal(b,sortKey);if(av<bv)return -1*dir;if(av>bv)return 1*dir;return 0})}
function setSort(key){if(sortKey===key){sortDir=sortDir==='desc'?'asc':'desc'}else{sortKey=key;sortDir='desc'}renderRows()}
function updateSortHeaders(){document.querySelectorAll('#listingsTable th.sortable').forEach(th=>{const key=th.dataset.sort;th.textContent=th.dataset.label+(sortKey===key?(sortDir==='desc'?' ▼':' ▲'):'')})}
function populateCategoryFilter(){const cats=[...new Set(listingsData.map(x=>x.category).filter(Boolean))].sort();const sel=document.getElementById('filterCategory');const current=sel.value;sel.innerHTML=`<option value="">כל הקטגוריות</option>`+cats.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');sel.value=cats.includes(current)?current:''}
function renderCategoryChart(list){const counts={};list.forEach(x=>{const c=x.category||'other';counts[c]=(counts[c]||0)+1});const entries=Object.entries(counts).sort((a,b)=>b[1]-a[1]);const el=document.getElementById('categoryChart');if(!entries.length){el.innerHTML='';return}const max=entries[0][1];el.innerHTML=`<div class="chartbars">${entries.map(([cat,count])=>`<div class="chartrow"><div class="chartlabel">${esc(cat)}</div><div class="chartbar"><div class="chartfill" style="width:${Math.max(4,Math.round(count/max*100))}%"></div></div><div class="chartcount">${count}</div></div>`).join('')}</div>`}
function renderRows(){const showMeta=document.getElementById('colMetadata').checked;const showConf=document.getElementById('colConfidence').checked;document.querySelectorAll('.colMetadataCell,.colMetadataHeader').forEach(el=>el.style.display=showMeta?'':'none');document.querySelectorAll('.colConfidenceCell,.colConfidenceHeader').forEach(el=>el.style.display=showConf?'':'none');const filtered=sortedListings(applyFilters(listingsData));document.getElementById('rows').innerHTML=filtered.map(x=>{listingCache[x.listing_id]=x;return `<tr><td>${x.image_url?`<img src="${esc(x.image_url)}" loading="lazy">`:''}</td><td><a href="/listing/${encodeURIComponent(x.listing_id)}">${esc(x.title)}</a><div class="muted"><a href="${esc(x.url)}" target="_blank" rel="noreferrer">Marketplace</a> · ${esc(x.source_query)}</div></td><td class="colMetadataCell"><span class="pill">${esc(x.category)}</span><span class="pill">${esc(x.condition)}</span><div class="muted">${esc(x.classification_source)}</div></td><td class="price">${esc(x.latest_price_text??'—')}<div class="muted">${esc(x.location??'—')}</div></td><td class="score">${Number(x.deal_score).toFixed(1)}</td><td class="colConfidenceCell">${Math.round(Number(x.score_confidence)*100)}%</td><td>${esc(x.last_seen)}</td><td><button class="small" onclick="toggleDetails('${x.listing_id}')">פרטים</button><button class="small danger" onclick="deleteListing('${x.listing_id}')">מחק</button></td></tr><tr class="details-row" id="details-${esc(x.listing_id)}" hidden><td colspan="8"></td></tr>`}).join('');updateSortHeaders();renderCategoryChart(filtered);document.getElementById('emptyState').style.display=filtered.length?'none':''}
async function deleteAllListings(){if(!listingsData.length){alert('אין מודעות למחיקה.');return}if(!confirm(`למחוק את כל ${listingsData.length} המודעות מה-DB? הפעולה בלתי הפיכה.`))return;await api('/api/listings',{method:'DELETE'});await load()}
async function openNotifModal(listingId){let x;try{x=await api(`/api/listings/${encodeURIComponent(listingId)}`)}catch(err){alert('המודעה לא נמצאה (יכול להיות שנמחקה).');return}document.getElementById('notifModalBody').innerHTML=`${x.image_url?`<img src="${esc(x.image_url)}">`:''}<h3 style="margin:0 0 6px">${esc(x.title)}</h3><div class="muted" style="margin-bottom:14px">${esc(x.latest_price_text??'—')} · ${esc(x.location??'—')}</div><div style="display:flex;gap:8px"><a href="/listing/${encodeURIComponent(listingId)}" style="flex:1"><button style="width:100%" type="button">פרטים מלאים</button></a><a href="${esc(x.url)}" target="_blank" rel="noreferrer" style="flex:1"><button class="primary" style="width:100%" type="button">מעבר למודעה בפייסבוק</button></a></div>`;document.getElementById('notifModal').hidden=false}
function closeNotifModal(){document.getElementById('notifModal').hidden=true}
function resetForm(){for(const id of ['watchId','name','maxPrice','targetPrice','categoryId'])document.getElementById(id).value='';document.getElementById('interval').value=30;document.getElementById('maxItems').value=50;document.getElementById('currency').value='ILS';document.getElementById('formTitle').textContent='Add watchlist';document.getElementById('cancelEdit').hidden=true}function editWatch(id){const x=watchCache.find(v=>v.id===id);if(!x)return;document.getElementById('watchId').value=x.id;document.getElementById('name').value=x.name;document.getElementById('watchCity').value=(x.query||'').replace('מכירות בתים ','');document.getElementById('maxPrice').value=x.max_price??'';document.getElementById('targetPrice').value=x.target_price??'';document.getElementById('categoryId').value=x.category_id??'';document.getElementById('interval').value=x.interval_seconds/60;document.getElementById('maxItems').value=x.max_items;document.getElementById('currency').value=x.default_currency;document.getElementById('formTitle').textContent='Edit watchlist';document.getElementById('cancelEdit').hidden=false;window.scrollTo({top:200,behavior:'smooth'})}async function toggleWatch(id,enabled){await api(`/api/watchlists/${id}`,{method:'PATCH',body:JSON.stringify({enabled})});await load()}async function removeWatch(id){if(!confirm('Delete this watchlist?'))return;await api(`/api/watchlists/${id}`,{method:'DELETE'});resetForm();await load()}
function toggleDetails(id){const row=document.getElementById(`details-${id}`);if(!row)return;if(row.hidden){const x=listingCache[id]||{};row.querySelector('td').innerHTML=`<div class="details"><div><b>עיר:</b> ${esc(x.city_id??'—')}</div><div><b>נראה לראשונה:</b> ${esc(x.first_seen??'—')}</div><div><b>שאילתת חיפוש:</b> ${esc(x.source_query??'—')}</div><div><b>מטבע:</b> ${esc(x.currency??'—')}</div><div><b>סיווג:</b> ${esc(x.classification_source??'—')} (${Math.round(Number(x.classification_confidence??0)*100)}%)</div><div><b>סיבות ניקוד:</b> ${(x.score_reasons||[]).map(r=>esc(r)).join(', ')||'—'}</div>${x.description?`<div><b>תיאור:</b> ${esc(x.description)}</div>`:''}</div>`;row.hidden=false}else{row.hidden=true}}
async function deleteListing(id){if(!confirm('למחוק את המודעה הזו?'))return;await api(`/api/listings/${encodeURIComponent(id)}`,{method:'DELETE'});await load()}document.getElementById('cancelEdit').onclick=resetForm;document.getElementById('watchForm').onsubmit=async e=>{e.preventDefault();const id=document.getElementById('watchId').value;const body={name:document.getElementById('name').value.trim(),city_name:document.getElementById('watchCity').value,category_id:document.getElementById('categoryId').value.trim()||null,max_price:num(document.getElementById('maxPrice').value),target_price:num(document.getElementById('targetPrice').value),interval_seconds:Math.max(60,Math.round(Number(document.getElementById('interval').value)*60)),max_items:Number(document.getElementById('maxItems').value),default_currency:document.getElementById('currency').value.trim()||'ILS'};try{await api(id?`/api/watchlists/${id}`:'/api/watchlists',{method:id?'PATCH':'POST',body:JSON.stringify(body)});resetForm();await load()}catch(err){alert(err.message)}};
document.getElementById('runForm').onsubmit=async e=>{e.preventDefault();const status=document.getElementById('runStatus');const btn=e.target.querySelector('button');btn.disabled=true;status.textContent='סורק... זה יכול לקחת חצי דקה';const body={city_name:document.getElementById('runCity').value,max_price:num(document.getElementById('runMaxPrice').value),category_id:document.getElementById('runCategoryId').value.trim()||null,max_items:Number(document.getElementById('runMaxItems').value)};try{const r=await api('/api/search/run',{method:'POST',body:JSON.stringify(body)});status.textContent=`הסתיים: נמצאו ${r.extracted}, נשמרו ${r.normalized} (חדשים ${r.inserted}, עודכנו ${r.updated})`;await load()}catch(err){status.textContent='שגיאה: '+err.message}finally{btn.disabled=false}};
document.getElementById('filterSearch').oninput=e=>{filters.search=e.target.value;renderRows()};
document.getElementById('filterCategory').onchange=e=>{filters.category=e.target.value;renderRows()};
document.getElementById('filterMinScore').oninput=e=>{filters.minScore=Number(e.target.value)||0;renderRows()};
document.getElementById('colMetadata').onchange=renderRows;
document.getElementById('colConfidence').onchange=renderRows;
loadCities().then(load);setInterval(load,30000);
</script></body></html>"""

_DETAIL = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Listing detail</title><style>:root{color-scheme:dark;background:#0b0f14;color:#e7edf5;font-family:system-ui,sans-serif}body{margin:0}.wrap{max-width:1100px;margin:auto;padding:24px}.panel{background:#121923;border:1px solid #243044;border-radius:12px;padding:18px;margin:16px 0}.muted{color:#91a0b4}a{color:#70b7ff;text-decoration:none}.meta{display:flex;gap:8px;flex-wrap:wrap}.pill{border:1px solid #34445d;border-radius:999px;padding:3px 8px}svg{width:100%;max-width:640px;height:150px;background:#0b111a;border-radius:10px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:760px){.grid{grid-template-columns:1fr}}</style></head><body><div class="wrap"><a href="/">← Dashboard</a><div id="content"></div></div><script>const listingId=__LISTING_ID__;const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));async function api(u){const r=await fetch(u);if(!r.ok)throw new Error((await r.json()).detail||r.statusText);return r.json()}function chart(history){const pts=history.filter(x=>x.price_value!=null).reverse();if(!pts.length)return '<div class="muted">No numeric price history.</div>';const w=640,h=150,p=24;const vals=pts.map(x=>Number(x.price_value));const min=Math.min(...vals),max=Math.max(...vals);const span=Math.max(1,max-min);const xy=pts.map((x,i)=>{const px=p+(i*(w-2*p)/Math.max(1,pts.length-1));const py=h-p-((Number(x.price_value)-min)/span)*(h-2*p);return [px,py]});return `<svg viewBox="0 0 ${w} ${h}" role="img"><polyline fill="none" stroke="currentColor" stroke-width="3" points="${xy.map(p=>p.join(',')).join(' ')}"/>${xy.map((p,i)=>`<circle cx="${p[0]}" cy="${p[1]}" r="4"><title>${esc(pts[i].price_text)} · ${esc(pts[i].captured_at)}</title></circle>`).join('')}<text x="${p}" y="20" fill="currentColor">${esc(max.toFixed(2))}</text><text x="${p}" y="${h-6}" fill="currentColor">${esc(min.toFixed(2))}</text></svg>`}async function load(){const [x,h]=await Promise.all([api(`/api/listings/${encodeURIComponent(listingId)}`),api(`/api/listings/${encodeURIComponent(listingId)}/history`)]);document.getElementById('content').innerHTML=`<div class="panel"><h1>${esc(x.title)}</h1><div class="meta"><span class="pill">${esc(x.category)}</span><span class="pill">${esc(x.condition)}</span><span class="pill">score ${Number(x.deal_score).toFixed(1)}</span><span class="pill">confidence ${Math.round(Number(x.score_confidence)*100)}%</span></div><p><b>${esc(x.latest_price_text??'—')}</b> · ${esc(x.location??'location unknown')}</p><p class="muted">classified by ${esc(x.classification_source)} (${Math.round(Number(x.classification_confidence)*100)}%)</p><p><a href="${esc(x.url)}" target="_blank" rel="noreferrer">Open Marketplace listing</a></p></div><div class="panel"><h2>Price history</h2>${chart(h)}</div><div class="grid"><div class="panel"><h2>Score reasons</h2><ul>${(x.score_reasons||[]).map(r=>`<li>${esc(r)}</li>`).join('')}</ul></div><div class="panel"><h2>Observed metadata</h2><p>First seen: ${esc(x.first_seen)}</p><p>Last seen: ${esc(x.last_seen)}</p><p>Source query: ${esc(x.source_query)}</p></div></div>`}load().catch(e=>document.getElementById('content').textContent=e.message);</script></body></html>"""


def _watchlist_payload(item: Watchlist) -> dict[str, object]:
    return item.model_dump(mode="json")


def create_dashboard_app(
    db_path: Path,
    session_path: Path | None = None,
    *,
    open_browser: bool = True,
    dashboard_url: str = "http://127.0.0.1:8000/",
) -> FastAPI:
    store = MarketplaceStore(db_path)
    session = session_path or DEFAULT_SESSION

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.initialize()
        if open_browser and not os.environ.get("MARKETPLACE_DASHBOARD_NO_BROWSER"):
            # Server needs a moment to accept connections before the tab loads.
            threading.Timer(1.0, lambda: webbrowser.open(dashboard_url)).start()
        yield

    app = FastAPI(title="Facebook Marketplace Scraper Dashboard", version=__version__, lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> Response:
        return HTMLResponse(_DASHBOARD, headers={"Cache-Control": "no-store"})

    @app.get("/listing/{listing_id}", response_class=HTMLResponse)
    async def listing_page(listing_id: str) -> Response:
        if await store.listing_detail(listing_id) is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        return HTMLResponse(
            _DETAIL.replace("__LISTING_ID__", json.dumps(listing_id)),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        schema_version = await store.schema_version()
        daemon = await store.daemon_status()
        degraded = schema_version != LATEST_SCHEMA_VERSION or daemon.get("effective_state") in {"error", "stale"}
        return {"status": "degraded" if degraded else "ok", "schema_version": schema_version, "latest_schema_version": LATEST_SCHEMA_VERSION, "daemon": daemon}

    @app.get("/api/stats")
    async def stats() -> dict[str, object]:
        return await store.dashboard_stats()

    @app.get("/api/cities")
    async def cities() -> list[str]:
        return sorted(ISRAEL_CITY_IDS.keys())

    @app.get("/api/listings")
    async def listings(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, object]]:
        return await store.recent_listings(limit=limit)

    @app.get("/api/listings/{listing_id}")
    async def listing_detail(listing_id: str) -> dict[str, object]:
        result = await store.listing_detail(listing_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        return result

    @app.get("/api/listings/{listing_id}/history")
    async def history(listing_id: str) -> list[dict[str, object]]:
        if await store.listing_detail(listing_id) is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        return await store.listing_history(listing_id)

    @app.delete("/api/listings/{listing_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_listing(listing_id: str) -> Response:
        if not await store.delete_listing(listing_id):
            raise HTTPException(status_code=404, detail="Listing not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete("/api/listings")
    async def delete_all_listings_endpoint() -> dict[str, int]:
        deleted = await store.delete_all_listings()
        return {"deleted": deleted}

    @app.get("/api/notifications")
    async def notifications(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, object]]:
        return await store.recent_notifications(limit=limit)

    @app.get("/api/runs")
    async def runs(limit: int = Query(default=30, ge=1, le=200)) -> list[dict[str, object]]:
        return await store.search_run_metrics(limit=limit)

    @app.get("/api/watchlists")
    async def watchlists() -> list[dict[str, object]]:
        return [_watchlist_payload(item) for item in await store.list_watchlists()]

    @app.post("/api/watchlists", status_code=status.HTTP_201_CREATED)
    async def create_watchlist(payload: WatchlistWrite) -> dict[str, object]:
        city_id = await resolve_city_id(store, payload.city_name)
        if not city_id:
            raise HTTPException(
                status_code=400,
                detail=f"'{payload.city_name}' is not a supported city. See israel_cities.py.",
            )
        watchlist = Watchlist(
            name=payload.name,
            query=f"{FIXED_QUERY_BASE} {payload.city_name}",
            city_id=city_id,
            radius_km=FIXED_RADIUS_KM,
            category_id=payload.category_id,
            min_price=FIXED_MIN_PRICE,
            max_price=payload.max_price,
            target_price=payload.target_price,
            max_items=payload.max_items,
            default_currency=payload.default_currency,
            interval_seconds=payload.interval_seconds,
            enabled=payload.enabled,
        )
        try:
            watchlist_id = await store.create_watchlist(watchlist)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Watchlist name already exists") from exc
        created = await store.get_watchlist(watchlist_id)
        if created is None:
            raise HTTPException(status_code=500, detail="Watchlist was not persisted")
        return _watchlist_payload(created)

    @app.patch("/api/watchlists/{watchlist_id}")
    async def update_watchlist(watchlist_id: int, payload: WatchlistPatch) -> dict[str, object]:
        current = await store.get_watchlist(watchlist_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Watchlist not found")
        updates = payload.model_dump(exclude_unset=True)
        city_name = updates.pop("city_name", None)
        if city_name is not None:
            city_id = await resolve_city_id(store, city_name)
            if not city_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{city_name}' is not a supported city. See israel_cities.py.",
                )
            query = f"{FIXED_QUERY_BASE} {city_name}"
        else:
            city_id = current.city_id
            query = current.query
        max_price = updates.get("max_price", current.max_price)
        if max_price is not None and max_price < FIXED_MIN_PRICE:
            raise HTTPException(
                status_code=400,
                detail=f"max_price cannot be less than the fixed min_price ({FIXED_MIN_PRICE:g})",
            )
        merged = {
            "name": updates.get("name", current.name),
            "query": query,
            "city_id": city_id,
            "radius_km": FIXED_RADIUS_KM,
            "category_id": updates.get("category_id", current.category_id),
            "min_price": FIXED_MIN_PRICE,
            "max_price": max_price,
            "target_price": updates.get("target_price", current.target_price),
            "max_items": updates.get("max_items", current.max_items),
            "default_currency": updates.get("default_currency", current.default_currency),
            "interval_seconds": updates.get("interval_seconds", current.interval_seconds),
            "enabled": updates.get("enabled", current.enabled),
        }
        try:
            updated = await store.update_watchlist(watchlist_id, merged)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Watchlist name already exists") from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Watchlist not found")
        return _watchlist_payload(updated)

    @app.delete("/api/watchlists/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_watchlist(watchlist_id: int) -> Response:
        if not await store.delete_watchlist(watchlist_id):
            raise HTTPException(status_code=404, detail="Watchlist not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/search/run")
    async def run_search(payload: SearchRunRequest) -> dict[str, object]:
        city_id = await resolve_city_id(store, payload.city_name)
        if not city_id:
            raise HTTPException(
                status_code=400,
                detail=f"'{payload.city_name}' is not a supported city. See israel_cities.py.",
            )
        llm_settings = LlamaClassifierSettings.from_env()
        classifier = LocalLlamaClassifier(llm_settings) if llm_settings.enabled else None
        notifier = NotificationManager(store, NotificationSettings.from_env())
        try:
            collector = MarketplaceCollector(
                store=store,
                storage_state_path=session,
                headless=True,
                classifier=classifier,
                notifier=notifier,
            )
            result = await collector.collect(
                SearchSpec(
                    query=f"{FIXED_QUERY_BASE} {payload.city_name}",
                    city_id=city_id,
                    radius_km=FIXED_RADIUS_KM,
                    category_id=payload.category_id,
                    max_items=payload.max_items,
                    min_price=FIXED_MIN_PRICE,
                    max_price=payload.max_price,
                    default_currency=payload.default_currency,
                )
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            await notifier.close()
            if classifier is not None:
                await classifier.close()
        return {
            "run_id": result.run_id,
            "extracted": result.extracted,
            "normalized": result.normalized,
            "inserted": result.inserted,
            "updated": result.updated,
            "price_changes": result.price_changes,
            "notifications": result.notifications,
        }

    return app
