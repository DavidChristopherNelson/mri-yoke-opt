"""Browser dashboard over every run under results/: summary table with the last slice image per run, and a detail
page per run (metrics, history plot, iteration player over the slice images, embedded 3D viewer, config, log).

  python scripts/dashboard.py                 # writes results/dashboard/{index.html,data.json} once
  python scripts/dashboard.py --serve 8765    # also serves results/ on http://localhost:8765/dashboard/ and
                                              # rewrites data.json every 60 s while running
"""
import argparse, csv, glob, http.server, json, os, re, socketserver, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")
OUT = os.path.join(RES, "dashboard")


def num(x):
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else None
    except (TypeError, ValueError):
        return None


def scan():
    runs = []
    for hist in sorted(glob.glob(os.path.join(RES, "**", "history.csv"), recursive=True)):
        d = os.path.dirname(hist)
        rel = os.path.relpath(d, RES)
        if rel.startswith("dashboard"):
            continue
        try:
            rows = list(csv.DictReader(open(hist)))
        except OSError:
            continue
        if not rows:
            continue
        last = rows[-1]
        if "F" not in last:                                    # pre-Plan-X run (misfit / ppm objective)
            continue
        st = {}
        try:
            st = json.load(open(os.path.join(d, "status.json")))
        except (OSError, ValueError):
            pass
        cfg = {}
        try:
            cfg = json.load(open(os.path.join(d, "config.json")))
        except (OSError, ValueError):
            pass
        pngs = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "iter_*.png")))
        log_tail = ""
        try:
            with open(os.path.join(d, "run.log"), errors="replace") as f:
                log_tail = "".join(f.readlines()[-60:])
        except OSError:
            pass
        init = cfg.get("init")
        if init is None:                                       # config.json is written at the end; read the log header
            try:
                head = open(os.path.join(d, "run.log"), errors="replace").readline()
                m = re.search(r"init='([^']*)'", head)
                init = m.group(1) if m else "?"
            except OSError:
                init = "?"
        best = max(rows, key=lambda r: num(r.get("N_green")) or 0)
        keys = ["it", "F", "F_smooth", "N_green", "N_smooth", "blob_mean", "cost", "cost_fe", "cost_f", "iron_kg", "ferrite_kg",
                "demag_frac", "residual", "J", "kappa", "width", "moved_fe_kg", "moved_f_kg", "turn_deg", "dt"]
        series = {k: [num(r.get(k)) for r in rows] for k in keys if k in rows[0]}
        state = st.get("state", "unknown")
        if state == "running" and st.get("updated"):
            try:
                age = time.time() - time.mktime(time.strptime(st["updated"], "%Y-%m-%d %H:%M:%S"))
                if age > 1800:
                    state = "stale (no update for %.0f h)" % (age / 3600)
            except ValueError:
                pass
        runs.append(dict(
            path=rel, session=os.path.dirname(rel) or "(top)", name=os.path.basename(rel), init=init, state=state,
            it=int(float(last["it"])), iter_max=st.get("iter_max"), s_per_it=st.get("s_per_it"), elapsed_h=st.get("elapsed_h"),
            started=st.get("started"), updated=st.get("updated"), solves=st.get("solves"),
            F=num(last["F"]), N_green=num(last["N_green"]), best_N=num(best["N_green"]), best_it=int(float(best["it"])),
            N_smooth=num(last.get("N_smooth")), blob_mT=(num(last["blob_mean"]) or 0) * 1e3, cost=num(last["cost"]),
            iron_kg=num(last["iron_kg"]), ferrite_kg=num(last["ferrite_kg"]), demag=num(last.get("demag_frac")),
            resid_mT=(num(last.get("residual")) or 0) * 1e3, B0_mT=(num(rows[0]["blob_mean"]) or 0) * 1e3,
            iron0=num(rows[0]["iron_kg"]), ferrite0=num(rows[0]["ferrite_kg"]),
            last_png=pngs[-1] if pngs else None, pngs=pngs, has_viewer=os.path.exists(os.path.join(d, "viewer.html")),
            has_history_png=os.path.exists(os.path.join(d, "history.png")), series=series, config=cfg, log_tail=log_tail,
            mtime=os.path.getmtime(hist)))
    sessions = {}
    for r in runs:
        sessions.setdefault(r["session"], []).append(r["name"])
    notes = {}
    for s in sessions:
        p = os.path.join(RES, s, "status.txt")
        if os.path.exists(p):
            try:
                notes[s] = open(p).read()
            except OSError:
                pass
    return dict(generated=time.strftime("%Y-%m-%d %H:%M:%S"), runs=runs, sessions=sorted(sessions), session_status=notes)


HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MRI magnet runs</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1a1d21;--muted:#6b7280;--line:#e3e6ea;--acc:#2563eb;--good:#15803d;--warn:#b45309;--bad:#b91c1c}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0 12px 0 0}header .meta{color:var(--muted);font-size:12px}
header select,header input,header button{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:#fff}
header button{cursor:pointer}header button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}
main{padding:14px 16px;max-width:1800px;margin:0 auto}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;vertical-align:middle}
th:first-child,td:first-child,th.l,td.l{text-align:left}th{background:#f0f2f5;cursor:pointer;user-select:none;position:sticky;top:50px;font-weight:600}
th.sorted:after{content:" \25BE";color:var(--acc)}th.sorted.asc:after{content:" \25B4"}
tr.run{cursor:pointer}tr.run:hover{background:#f3f6ff}td img.thumb{height:110px;display:block;border:1px solid var(--line);border-radius:4px;background:#fff}
.state{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;background:#eef}.state.running{background:#dcfce7;color:var(--good)}
.state.finished{background:#e5e7eb;color:#374151}.state.stale,.state.crashed{background:#fee2e2;color:var(--bad)}
.F{font-weight:600}.sess{color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:12px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px}.kpi .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.03em}.kpi .v{font-size:19px;font-weight:600;margin-top:2px}.kpi .s{color:var(--muted);font-size:12px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:12px 0}section h2{font-size:15px;margin:0 0 8px}
.player{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}.player input[type=range]{flex:1;min-width:240px}
.player button{font:inherit;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;cursor:pointer}
#frame{width:100%;max-width:1700px;border:1px solid var(--line);border-radius:4px;background:#fff}
#histimg{width:100%;max-width:1100px;border:1px solid var(--line);border-radius:4px}
iframe{width:100%;height:640px;border:1px solid var(--line);border-radius:4px;background:#111}
pre{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:6px;overflow:auto;max-height:420px;font-size:12px}
details summary{cursor:pointer;color:var(--acc)}a{color:var(--acc)}.back{margin-right:8px}
canvas.spark{width:100%;height:120px;background:#fff;border:1px solid var(--line);border-radius:4px}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}.charts .c .k{font-size:12px;color:var(--muted);margin-bottom:2px}
.hint{color:var(--muted);font-size:12px}
@media (max-width:700px){th,td{padding:4px 5px}td img.thumb{height:70px}iframe{height:420px}}
</style></head><body>
<header><h1>MRI magnet runs</h1>
<span id="hdr-list"><label>session <select id="sess"></select></label> <input id="q" placeholder="filter runs" size="14">
<label><input type="checkbox" id="auto" checked> auto-refresh</label></span>
<span id="hdr-run" style="display:none"><button class="back" onclick="location.hash=''">&larr; all runs</button><b id="run-title"></b></span>
<button id="reload" onclick="load()">refresh</button><span class="meta" id="gen"></span></header>
<main><div id="view"></div></main>
<script>
let DATA=null, sortKey='F', sortAsc=true, timer=null, player=null;
const fmt=(v,d=1)=>v==null?'–':(Math.abs(v)>=1e5?v.toExponential(2):v.toFixed(d));
const money=v=>v==null?'–':'$'+v.toFixed(v<100?2:0);
const stateCls=s=>s.startsWith('running')?'running':s.startsWith('finished')?'finished':s.startsWith('stale')?'stale':s.startsWith('crashed')?'crashed':'';
async function load(){
  try{const r=await fetch('data.json?t='+Date.now());DATA=await r.json();}catch(e){document.getElementById('gen').textContent='data.json not found: run scripts/dashboard.py';return;}
  document.getElementById('gen').textContent='data '+DATA.generated+' · '+DATA.runs.length+' runs';
  const sel=document.getElementById('sess'),cur=sel.value;sel.innerHTML='<option value="">all sessions</option>'+DATA.sessions.map(s=>`<option ${s===cur?'selected':''}>${s}</option>`).join('');
  render();
}
function render(){
  const h=decodeURIComponent(location.hash.replace(/^#/,''));
  const run=DATA&&DATA.runs.find(r=>r.path===h);
  document.getElementById('hdr-list').style.display=run?'none':'';document.getElementById('hdr-run').style.display=run?'':'none';
  if(run)renderRun(run);else renderList();
}
function renderList(){
  if(player){clearInterval(player);player=null;}
  const sess=document.getElementById('sess').value,q=document.getElementById('q').value.toLowerCase();
  let runs=DATA.runs.filter(r=>(!sess||r.session===sess)&&(!q||(r.path+' '+r.init+' '+r.state).toLowerCase().includes(q)));
  runs.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(x==null)x=sortAsc?Infinity:-Infinity;if(y==null)y=sortAsc?Infinity:-Infinity;if(typeof x==='string')return sortAsc?x.localeCompare(y):y.localeCompare(x);return sortAsc?x-y:y-x;});
  const cols=[['path','run','l'],['init','start','l'],['state','state','l'],['it','it'],['F','F $/voxel'],['N_green','N green'],['best_N','best N (it)'],['N_smooth','N smooth'],['blob_mT','centre B mT'],['cost','cost'],['iron_kg','iron kg'],['ferrite_kg','ferrite kg'],['demag','demag'],['resid_mT','resid mT'],['s_per_it','s/it'],['last_png','last iteration','l']];
  let html='<table><thead><tr>'+cols.map(c=>`<th class="${c[2]||''} ${sortKey===c[0]?'sorted':''} ${sortAsc?'asc':''}" onclick="setSort('${c[0]}')">${c[1]}</th>`).join('')+'</tr></thead><tbody>';
  for(const r of runs){
    html+=`<tr class="run" onclick="location.hash='${encodeURIComponent(r.path)}'">
<td class="l"><div>${r.name}</div><div class="sess">${r.session}</div></td><td class="l">${r.init}</td><td class="l"><span class="state ${stateCls(r.state)}">${r.state.replace('finished: ','')}</span></td>
<td>${r.it}${r.iter_max?'/'+r.iter_max:''}</td><td class="F">${money(r.F)}</td><td>${fmt(r.N_green,0)}</td><td>${fmt(r.best_N,0)} (${r.best_it})</td><td>${fmt(r.N_smooth,0)}</td>
<td>${fmt(r.blob_mT,2)}</td><td>${money(r.cost)}</td><td>${fmt(r.iron_kg,0)}</td><td>${fmt(r.ferrite_kg,0)}</td><td>${fmt(r.demag,2)}</td><td>${fmt(r.resid_mT,2)}</td><td>${fmt(r.s_per_it,0)}</td>
<td class="l">${r.last_png?`<img class="thumb" loading="lazy" src="../${r.path}/${r.last_png}?m=${Math.round(r.mtime)}">`:'–'}</td></tr>`;
  }
  html+='</tbody></table><p class="hint">Click a column to sort, a row to open the run. F = (cost of iron + ferrite + fixed) / green voxels, full magnet; N green = largest connected component of voxels inside the field band with low slope (exact count); N smooth = the differentiable surrogate the optimiser works on. On the coarse mesh the exact count is noise-limited.</p>';
  if(sess&&DATA.session_status[sess])html+=`<section><h2>session status</h2><pre>${esc(DATA.session_status[sess])}</pre></section>`;
  document.getElementById('view').innerHTML=html;
}
function setSort(k){if(sortKey===k)sortAsc=!sortAsc;else{sortKey=k;sortAsc=!(k==='N_green'||k==='best_N'||k==='N_smooth');}renderList();}
const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function renderRun(r){
  document.getElementById('run-title').textContent=r.path;
  const base='../'+r.path+'/';
  const kpi=(k,v,s='')=>`<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  let html=`<div class="grid">${kpi('state',`<span class="state ${stateCls(r.state)}">${r.state.replace('finished: ','')}</span>`,(r.started?'started '+r.started:'')+(r.updated?' · updated '+r.updated:''))}
${kpi('F',money(r.F),'$ per green voxel')}${kpi('green voxels',fmt(r.N_green,0),'best '+fmt(r.best_N,0)+' at it '+r.best_it+' · smooth '+fmt(r.N_smooth,0))}
${kpi('centre field',fmt(r.blob_mT,2)+' mT','start '+fmt(r.B0_mT,1)+' mT · target 159.2')}${kpi('cost',money(r.cost),'iron '+fmt(r.iron_kg,0)+' kg · ferrite '+fmt(r.ferrite_kg,0)+' kg')}
${kpi('iterations',r.it+(r.iter_max?' / '+r.iter_max:''),(r.s_per_it?fmt(r.s_per_it,0)+' s/it · ':'')+(r.elapsed_h?fmt(r.elapsed_h,2)+' h · ':'')+(r.solves?r.solves+' solves':''))}
${kpi('demag fraction',fmt(r.demag,2),'ferrite below the gate')}${kpi('projection residual',fmt(r.resid_mT,3)+' mT','band is ±0.15 mT')}</div>`;
  html+=`<section><h2>iterations</h2><div class="player"><button id="play">&#9654; play</button><input type="range" id="sl" min="0" max="${Math.max(r.pngs.length-1,0)}" value="${Math.max(r.pngs.length-1,0)}"><span id="lab"></span></div>
<img id="frame" alt="slice"></section>`;
  html+=`<section><h2>history</h2>${r.has_history_png?`<img id="histimg" src="${base}history.png?m=${Math.round(r.mtime)}">`:''}<div class="charts" id="charts"></div></section>`;
  html+=`<section><h2>3D model</h2>${r.has_viewer?`<p class="hint">iron grey · ferrite coloured by m<sub>z</sub> (red +z, white transverse, blue −z) with direction arrows · green = largest usable imaging region · blue box = patient envelope, dashed = access corridor. Drag to orbit, wheel to zoom, slider for iterations. <a href="${base}viewer.html" target="_blank">open full screen</a></p><iframe loading="lazy" src="${base}viewer.html"></iframe>`:'<p class="hint">no viewer.html for this run</p>'}</section>`;
  html+=`<section><details><summary>config</summary><pre>${esc(JSON.stringify(r.config,null,1))}</pre></details></section>`;
  html+=`<section><details open><summary>log (last 60 lines) — <a href="${base}run.log" target="_blank">full log</a></summary><pre>${esc(r.log_tail)}</pre></details></section>`;
  document.getElementById('view').innerHTML=html;
  const sl=document.getElementById('sl'),fr=document.getElementById('frame'),lab=document.getElementById('lab');
  const show=()=>{const i=+sl.value;if(!r.pngs.length){lab.textContent='no images';return;}fr.src=base+r.pngs[i];lab.textContent=r.pngs[i].replace('.png','')+' / '+r.pngs.length;};
  sl.oninput=show;show();
  if(player){clearInterval(player);player=null;}
  document.getElementById('play').onclick=function(){if(player){clearInterval(player);player=null;this.innerHTML='&#9654; play';return;}
    this.innerHTML='&#10074;&#10074; pause';if(+sl.value>=r.pngs.length-1)sl.value=0;player=setInterval(()=>{sl.value=(+sl.value+1)%r.pngs.length;show();},350);};
  drawCharts(r);
}
function drawCharts(r){
  const s=r.series,box=document.getElementById('charts');const specs=[['F','F $/voxel (log)',true],['N_green','N green (log)',true],['N_smooth','N smooth (log)',true],['blob_mean','centre B [mT]',false,1e3],['iron_kg','iron kg'],['ferrite_kg','ferrite kg'],['demag_frac','demag fraction'],['J','J'],['turn_deg','direction misalignment [deg]'],['dt','s per iteration']];
  for(const [k,label,log,scale] of specs){if(!s[k])continue;const c=document.createElement('div');c.className='c';c.innerHTML=`<div class="k">${label}</div><canvas class="spark"></canvas>`;box.appendChild(c);
    const cv=c.querySelector('canvas');cv.width=cv.clientWidth*2||600;cv.height=240;const ctx=cv.getContext('2d');
    let ys=s[k].map(v=>v==null?null:v*(scale||1));if(log)ys=ys.map(v=>v==null||v<=0?null:Math.log10(v));
    const xs=s.it,pts=ys.map((y,i)=>[xs[i],y]).filter(p=>p[1]!=null);if(!pts.length)continue;
    const x0=Math.min(...pts.map(p=>p[0])),x1=Math.max(...pts.map(p=>p[0]))||1,y0=Math.min(...pts.map(p=>p[1])),y1=Math.max(...pts.map(p=>p[1]));const yr=(y1-y0)||1;
    const X=x=>20+(x-x0)/((x1-x0)||1)*(cv.width-30),Y=y=>cv.height-18-(y-y0)/yr*(cv.height-30);
    ctx.strokeStyle='#2563eb';ctx.lineWidth=2;ctx.beginPath();pts.forEach((p,i)=>i?ctx.lineTo(X(p[0]),Y(p[1])):ctx.moveTo(X(p[0]),Y(p[1])));ctx.stroke();
    if(k==='blob_mean'){ctx.strokeStyle='#dc2626';ctx.setLineDash([6,4]);ctx.beginPath();ctx.moveTo(20,Y(159.2));ctx.lineTo(cv.width-10,Y(159.2));ctx.stroke();ctx.setLineDash([]);}
    ctx.fillStyle='#6b7280';ctx.font='20px system-ui';const f=v=>log?('1e'+v.toFixed(1)):v.toFixed(v<10?2:0);ctx.fillText(f(y1),22,22);ctx.fillText(f(y0),22,cv.height-22);ctx.fillText('it '+x1,cv.width-90,cv.height-2);}
}
window.onhashchange=render;document.getElementById('sess').onchange=renderList;document.getElementById('q').oninput=renderList;
document.getElementById('auto').onchange=function(){if(timer)clearInterval(timer);timer=null;if(this.checked)timer=setInterval(()=>{if(!location.hash)load();},60000);};
timer=setInterval(()=>{if(!location.hash)load();},60000);load();
</script></body></html>
"""


def write():
    os.makedirs(OUT, exist_ok=True)
    data = scan()
    tmp = os.path.join(OUT, "data.json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, os.path.join(OUT, "data.json"))
    with open(os.path.join(OUT, "index.html"), "w") as f:
        f.write(HTML)
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", type=int, default=0, help="port; serves results/ and refreshes data.json every 60 s")
    a = ap.parse_args()
    d = write()
    print(f"{len(d['runs'])} runs -> {OUT}/index.html")
    if a.serve:
        def refresh():
            while True:
                time.sleep(60)
                try:
                    write()
                except Exception as e:                        # keep serving even if one scan fails
                    print("refresh failed:", e)
        threading.Thread(target=refresh, daemon=True).start()

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kw):
                super().__init__(*args, directory=RES, **kw)

            def log_message(self, *args):
                pass

            def end_headers(self):
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.ThreadingTCPServer(("127.0.0.1", a.serve), Quiet) as srv:
            print(f"serving http://localhost:{a.serve}/dashboard/")
            srv.serve_forever()
