/* ============================================================
   Loom Dashboard v4 — Canvas 2D glassy + live SSE + full UX
   Zero dependencies. All charts hand-drawn on <canvas>.
   ============================================================ */
(function(){
'use strict';
var $ = function(id){ return document.getElementById(id); };

/* ---------- palette ---------- */
var THEMES = {
  sky:    ['#38bdf8','#a78bfa','#34d399','#fbbf24','#fb7185','#22d3ee','#f472b6','#60a5fa','#818cf8','#c084fc'],
  violet: ['#a78bfa','#818cf8','#c084fc','#e879f9','#38bdf8','#34d399','#fbbf24','#fb7185','#22d3ee','#f472b6'],
  emerald:['#34d399','#22d3ee','#38bdf8','#a78bfa','#fbbf24','#fb7185','#818cf8','#c084fc','#60a5fa','#f472b6'],
  rose:   ['#fb7185','#f472b6','#e879f9','#c084fc','#38bdf8','#a78bfa','#34d399','#fbbf24','#22d3ee','#818cf8']
};
var PAL = THEMES.sky;
var ACCENTS = { sky:'#38bdf8', violet:'#a78bfa', emerald:'#34d399', rose:'#fb7185' };
var curTheme = 'sky';
function color(i){ return PAL[i % PAL.length]; }
function hexA(hex,a){ var n=parseInt(hex.slice(1),16); return 'rgba('+((n>>16)&255)+','+((n>>8)&255)+','+(n&255)+','+a+')'; }
function setTheme(t){
  curTheme=t; PAL=THEMES[t]||THEMES.sky;
  var acc=ACCENTS[t]||'#38bdf8';
  document.documentElement.style.setProperty('--accent', acc);
  document.querySelectorAll('.accent-btn').forEach(function(b){ b.classList.toggle('on', b.dataset.theme===t); });
  if(lastSnap) redrawAll(lastSnap);
}

/* ---------- state ---------- */
var es=null, paused=false, lastSnap=null, lastMsg=0, activeSection='overview';
var lastDB='', totalEvents=0, rateBuf=[], latencyHist=[], tokenHist=[];
var timeRange=15, chartMode='area', autoScale=true, compactMode=false;
var evSortCol='ts', evSortDir=-1;

/* ---------- toast ---------- */
var toastT=null;
function toast(msg){
  var t=$('toast'); t.textContent=msg; t.classList.add('show');
  clearTimeout(toastT); toastT=setTimeout(function(){ t.classList.remove('show'); },2200);
}

/* ---------- canvas setup (DPR) ---------- */
function setup(id){
  var c=$(id); if(!c) return null;
  var dpr=window.devicePixelRatio||1;
  var w=c.clientWidth, h=c.clientHeight;
  if(w<2||h<2) return null;
  if(c.width!==Math.round(w*dpr)||c.height!==Math.round(h*dpr)){ c.width=Math.round(w*dpr); c.height=Math.round(h*dpr); }
  var ctx=c.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  return {ctx:ctx,w:w,h:h};
}
function clear(ctx,w,h){ ctx.clearRect(0,0,w,h); }
function grid(ctx,w,h,pad,rows){
  ctx.strokeStyle='rgba(124,168,220,0.07)'; ctx.lineWidth=1;
  for(var i=0;i<=rows;i++){
    var y=pad.t + (h-pad.t-pad.b)*(i/rows);
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(w-pad.r,y); ctx.stroke();
  }
}
function rr(ctx,x,y,w,hh,r){
  ctx.beginPath(); ctx.moveTo(x+r,y);
  ctx.arcTo(x+w,y,x+w,y+hh,r); ctx.arcTo(x+w,y+hh,x,y+hh,r);
  ctx.arcTo(x,y+hh,x,y,r); ctx.arcTo(x,y,x+w,y,r); ctx.closePath();
}

/* ---------- smooth path ---------- */
function linePath(ctx,pts){
  if(pts.length<2) return;
  ctx.beginPath(); ctx.moveTo(pts[0][0],pts[0][1]);
  for(var i=1;i<pts.length;i++){ var p=pts[i],pp=pts[i-1],mx=(pp[0]+p[0])/2,my=(pp[1]+p[1])/2; ctx.quadraticCurveTo(pp[0],pp[1],mx,my); }
  ctx.lineTo(pts[pts.length-1][0],pts[pts.length-1][1]);
}

/* ============================================================
   DERIVE SERIES FROM RECENT EVENTS (the key fix)
   ============================================================ */
function deriveSeries(audit, rangeMin){
  rangeMin=rangeMin||timeRange;
  var evs=(audit.recent_events||[]).slice();
  if(!evs.length) return {tools:[],labels:[],counts:{},p50:[],p95:[]};
  var parsed=evs.map(function(e){
    var t=e.ts;
    if(typeof t==='string'){ var d=new Date(t); t=isNaN(d)?0:d.getTime()/1000; }
    return {tool:e.tool||'unknown', session:e.session_id||'', outcome:e.outcome||'ok', lat:e.latency_ms||0, ts:t};
  }).filter(function(e){ return e.ts>0; });
  if(!parsed.length) return {tools:[],labels:[],counts:{},p50:[],p95:[]};
  var nowTs=Math.max.apply(null,parsed.map(function(e){return e.ts;}))+1;
  var bucketSec=60, nBuckets=rangeMin;
  var buckets=[];
  for(var i=nBuckets-1;i>=0;i--){
    var start=nowTs-(i+1)*bucketSec, end=nowTs-i*bucketSec;
    buckets.push(parsed.filter(function(e){ return e.ts>=start && e.ts<end; }));
  }
  var labels=buckets.map(function(_,i){ return '-'+(nBuckets-1-i)+'m'; });
  labels[labels.length-1]='now';
  var toolSet={}; parsed.forEach(function(e){ toolSet[e.tool]=1; });
  var tools=Object.keys(toolSet).sort();
  var counts={};
  tools.forEach(function(t){
    counts[t]=buckets.map(function(b){ return b.filter(function(e){return e.tool===t;}).length; });
  });
  function pct(arr,p){
    if(!arr.length) return 0;
    var s=arr.slice().sort(function(a,b){return a-b;});
    var idx=Math.min(s.length-1, Math.floor(s.length*p));
    return s[idx];
  }
  var p50=buckets.map(function(b){ return b.length?pct(b.map(function(e){return e.lat;}),0.5):0; });
  var p95=buckets.map(function(b){ return b.length?pct(b.map(function(e){return e.lat;}),0.95):0; });
  return {tools:tools, labels:labels, counts:counts, p50:p50, p95:p95};
}

/* ============================================================
   CHART: stacked area / bar (calls per tool)
   ============================================================ */
function chartCalls(id, audit){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h, pad={l:34,r:14,t:14,b:22};
  clear(ctx,w,h);
  var derived=deriveSeries(audit);
  var tools=audit.series_tools&&audit.series_tools.length?audit.series_tools:derived.tools;
  var counts=audit.series_counts&&Object.keys(audit.series_counts).length?audit.series_counts:derived.counts;
  var labels=audit.series_labels&&audit.series_labels.length?audit.series_labels:derived.labels;
  var n=labels.length;
  if(!n||!tools.length){ empty(ctx,w,h,'Waiting for events…'); return; }
  var stacks=[]; for(var i=0;i<n;i++) stacks.push(0);
  tools.forEach(function(t){ var a=counts[t]||[]; for(var k=0;k<n;k++) stacks[k]+=(a[k]||0); });
  var maxV=Math.max(1,Math.max.apply(null,stacks));
  grid(ctx,w,h,pad,4);
  ctx.fillStyle='rgba(86,98,122,0.85)'; ctx.textAlign='right'; ctx.font='10px JetBrains Mono';
  for(var s=0;s<=4;s++){ ctx.fillText(Math.round(maxV*s/4), pad.l-6, pad.t+(h-pad.t-pad.b)*(1-s/4)+3); }
  ctx.textAlign='center';
  var bw=w-pad.l-pad.r;
  var xAt=function(i){ return n>1? pad.l+bw*(i/(n-1)) : pad.l+bw/2; };
  labels.forEach(function(lb,i){ if(i%3===0||i===n-1) ctx.fillText(lb, xAt(i), h-pad.b+14); });

  if(chartMode==='bar'){
    var barW=Math.min(28, bw/n*0.6);
    var acc=new Array(n).fill(0);
    tools.forEach(function(t,ti){
      var a=counts[t]||[], c=color(ti);
      for(var k=0;k<n;k++){
        var v=a[k]||0; if(!v) continue;
        var y=pad.t+(h-pad.t-pad.b)*(1-(acc[k]+v)/maxV);
        var bh=(h-pad.t-pad.b)*(v/maxV);
        var grad=ctx.createLinearGradient(0,y,0,y+bh); grad.addColorStop(0,c); grad.addColorStop(1,hexA(c,0.5));
        ctx.fillStyle=grad; rr(ctx,xAt(k)-barW/2,y,barW,bh,3); ctx.fill();
        acc[k]+=v;
      }
    });
  } else {
    var acc2=new Array(n).fill(0);
    tools.forEach(function(t,ti){
      var a=counts[t]||[], c=color(ti);
      var top=[]; for(var k=0;k<n;k++){ acc2[k]+=(a[k]||0); top.push([xAt(k), pad.t+(h-pad.t-pad.b)*(1-acc2[k]/maxV)]); }
      var bot=[]; var prev=acc2.slice(); for(var k=0;k<n;k++){ prev[k]-=(a[k]||0); bot.push([xAt(k), pad.t+(h-pad.t-pad.b)*(1-prev[k]/maxV)]); }
      var grad=ctx.createLinearGradient(0,pad.t,0,h-pad.b); grad.addColorStop(0,hexA(c,0.36)); grad.addColorStop(1,hexA(c,0.05));
      ctx.beginPath(); ctx.moveTo(top[0][0],top[0][1]);
      for(var k=1;k<n;k++) ctx.lineTo(top[k][0],top[k][1]);
      for(var k=n-1;k>=0;k--) ctx.lineTo(bot[k][0],bot[k][1]);
      ctx.closePath(); ctx.fillStyle=grad; ctx.fill();
      ctx.beginPath(); ctx.moveTo(top[0][0],top[0][1]);
      for(var k=1;k<n;k++) ctx.lineTo(top[k][0],top[k][1]);
      ctx.strokeStyle=hexA(c,0.9); ctx.lineWidth=1.6; ctx.shadowColor=c; ctx.shadowBlur=6; ctx.stroke(); ctx.shadowBlur=0;
    });
  }
  legend(ctx,tools,w,pad,12);
  var total=stacks.reduce(function(a,b){return a+b;},0);
  set('calls-total', total);
}

/* ============================================================
   CHART: dual line p50/p95 latency (with derived fallback)
   ============================================================ */
function chartLat(id, audit, chipId){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h, pad={l:38,r:14,t:14,b:22};
  clear(ctx,w,h);
  var derived=deriveSeries(audit);
  var labels=(audit.series_labels&&audit.series_labels.length)?audit.series_labels:derived.labels;
  var p50=(audit.latency_p50_series&&audit.latency_p50_series.some(function(v){return v!=null;}))?audit.latency_p50_series:derived.p50;
  var p95=(audit.latency_p95_series&&audit.latency_p95_series.some(function(v){return v!=null;}))?audit.latency_p95_series:derived.p95;
  p50=p50.map(function(v){return v==null?0:v;}); p95=p95.map(function(v){return v==null?0:v;});
  if(!labels.length||!p50.length){ empty(ctx,w,h,'—'); return; }
  var n=labels.length;
  var maxV=Math.max(1,Math.max.apply(null,p50.concat(p95)));
  grid(ctx,w,h,pad,4);
  ctx.fillStyle='rgba(86,98,122,0.85)'; ctx.textAlign='right'; ctx.font='10px JetBrains Mono';
  for(var s=0;s<=4;s++){ ctx.fillText(Math.round(maxV*s/4)+'', pad.l-6, pad.t+(h-pad.t-pad.b)*(1-s/4)+3); }
  ctx.textAlign='center';
  var bw=w-pad.l-pad.r;
  var xAt=function(i){ return n>1? pad.l+bw*(i/(n-1)) : pad.l+bw/2; };
  labels.forEach(function(lb,i){ if(i%3===0||i===n-1) ctx.fillText(lb, xAt(i), h-pad.b+14); });
  var yAt=function(v){ return pad.t+(h-pad.t-pad.b)*(1-v/maxV); };
  [['#38bdf8',p50],['#fb7185',p95]].forEach(function(s){
    var c=s[0],data=s[1],pts=[];
    for(var k=0;k<n;k++){ if(data[k]>0) pts.push([xAt(k),yAt(data[k])]); }
    if(pts.length<2) return;
    var grad=ctx.createLinearGradient(0,pad.t,0,h-pad.b); grad.addColorStop(0,hexA(c,0.28)); grad.addColorStop(1,hexA(c,0.02));
    ctx.save(); linePath(ctx,pts); ctx.lineTo(pts[pts.length-1][0],h-pad.b); ctx.lineTo(pts[0][0],h-pad.b); ctx.closePath(); ctx.fillStyle=grad; ctx.fill(); ctx.restore();
    linePath(ctx,pts); ctx.strokeStyle=c; ctx.lineWidth=2; ctx.shadowColor=c; ctx.shadowBlur=8; ctx.stroke(); ctx.shadowBlur=0;
    pts.forEach(function(p){ ctx.beginPath(); ctx.arc(p[0],p[1],2.8,0,7); ctx.fillStyle=c; ctx.fill(); ctx.beginPath(); ctx.arc(p[0],p[1],5.5,0,7); ctx.fillStyle=hexA(c,0.2); ctx.fill(); });
  });
  legend(ctx,['p50','p95'],w,pad,12,[['#38bdf8'],['#fb7185']]);
  if(chipId) set(chipId,'p95 '+Math.round(Math.max.apply(null,p95))+' ms');
}

/* ============================================================
   CHART: donut
   ============================================================ */
function chartDonut(id, items, chipId){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h, cx=w/2, cy=h/2, R=Math.min(w,h)/2-22, r=R*0.58;
  clear(ctx,w,h);
  if(!items.length){ empty(ctx,w,h,'—'); return; }
  var total=items.reduce(function(s,k){return s+(k.n||k.value||0);},0);
  if(total<=0){ empty(ctx,w,h,'—'); return; }
  var start=-Math.PI/2, gap=0.012;
  items.forEach(function(it,i){
    var v=it.n||it.value||0, frac=v/total, ang=frac*Math.PI*2;
    var c=color(i);
    ctx.beginPath(); ctx.arc(cx,cy,R,start+gap,start+ang-gap); ctx.arc(cx,cy,r,start+ang-gap,start+gap,true);
    ctx.closePath();
    var grad=ctx.createLinearGradient(cx-R,cy-R,cx+R,cy+R); grad.addColorStop(0,c); grad.addColorStop(1,hexA(c,0.55));
    ctx.fillStyle=grad; ctx.shadowColor=c; ctx.shadowBlur=12; ctx.fill(); ctx.shadowBlur=0;
    ctx.strokeStyle='rgba(6,9,18,0.7)'; ctx.lineWidth=2; ctx.stroke();
    start+=ang;
  });
  ctx.textAlign='center'; ctx.font='700 22px JetBrains Mono'; ctx.fillStyle='#e9eef7';
  ctx.fillText(total, cx, cy-2);
  ctx.font='10px Inter'; ctx.fillStyle='#8a96ad'; ctx.fillText('total', cx, cy+16);
  legV(ctx,items.map(function(it,i){
    var lab=(it.kind||it.label||it.tool||'')+'';
    return [color(i), lab, it.n||it.value||0, total];
  }), w, h);
  if(chipId) set(chipId, total);
}

/* ============================================================
   CHART: radial gauge (savings ratio)
   ============================================================ */
function chartGauge(id, pct, label){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h, cx=w/2, cy=h*0.62, R=Math.min(w,h)*0.38;
  clear(ctx,w,h);
  ctx.strokeStyle='rgba(124,168,220,0.12)'; ctx.lineWidth=R*0.18; ctx.lineCap='round';
  ctx.beginPath(); ctx.arc(cx,cy,R,Math.PI*0.75,Math.PI*2.25); ctx.stroke();
  var val=Math.max(0,Math.min(1,pct/100));
  var ang=Math.PI*0.75+val*Math.PI*1.5;
  var c=color(0);
  ctx.strokeStyle=c; ctx.shadowColor=c; ctx.shadowBlur=16;
  ctx.beginPath(); ctx.arc(cx,cy,R,Math.PI*0.75,ang); ctx.stroke(); ctx.shadowBlur=0;
  ctx.textAlign='center'; ctx.font='700 26px JetBrains Mono'; ctx.fillStyle='#e9eef7';
  ctx.fillText(Math.round(pct)+'%', cx, cy+2);
  ctx.font='10px Inter'; ctx.fillStyle='#8a96ad'; ctx.fillText(label||'', cx, cy+20);
}

/* ============================================================
   CHART: sparkline (mini line for KPI cards)
   ============================================================ */
function chartSpark(id, data){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h; clear(ctx,w,h);
  if(!data||data.length<2) return;
  var maxV=Math.max.apply(null,data), minV=Math.min.apply(null,data), range=maxV-minV||1;
  var pts=data.map(function(v,i){ return [w*(i/(data.length-1)), h-2-(h-4)*((v-minV)/range)]; });
  var c=color(0);
  var grad=ctx.createLinearGradient(0,0,0,h); grad.addColorStop(0,hexA(c,0.35)); grad.addColorStop(1,hexA(c,0.02));
  ctx.save(); linePath(ctx,pts); ctx.lineTo(pts[pts.length-1][0],h); ctx.lineTo(pts[0][0],h); ctx.closePath(); ctx.fillStyle=grad; ctx.fill(); ctx.restore();
  linePath(ctx,pts); ctx.strokeStyle=c; ctx.lineWidth=1.5; ctx.stroke();
  var last=pts[pts.length-1]; ctx.beginPath(); ctx.arc(last[0],last[1],2.5,0,7); ctx.fillStyle=c; ctx.fill();
}

/* ============================================================
   CHART: horizontal bars (tokens per source)
   ============================================================ */
function chartTokens(id, db){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h, pad={l:120,r:46,t:14,b:14};
  clear(ctx,w,h);
  var data=(db.tokens_per_store||[]).slice(0,12);
  if(!data.length){ empty(ctx,w,h,'—'); return; }
  var maxV=Math.max.apply(null,data.map(function(d){return Math.max(d.tokens_before||0,d.anchor_tokens||0);}))||1;
  var rowH=(h-pad.t-pad.b)/data.length, barH=Math.min(14,rowH*0.34);
  ctx.font='11px Inter'; ctx.textAlign='right'; ctx.fillStyle='#8a96ad';
  data.forEach(function(d,i){
    var y=pad.t+i*rowH, label=(d.label||('#'+d.id))+'';
    ctx.fillText(label.length>16?label.slice(0,16)+'…':label, pad.l-8, y+barH/2+4);
  });
  data.forEach(function(d,i){
    var y=pad.t+i*rowH, bef=d.tokens_before||0, anc=d.anchor_tokens||0;
    ctx.fillStyle='rgba(124,168,220,0.10)'; rr(ctx,pad.l,y,w-pad.l-pad.r,barH*0.5,3); ctx.fill();
    var bw2=(anc/maxV)*(w-pad.l-pad.r);
    var grad=ctx.createLinearGradient(pad.l,0,pad.l+bw2,0); grad.addColorStop(0,color(0)); grad.addColorStop(1,color(1));
    ctx.fillStyle=grad; ctx.shadowColor=color(0); ctx.shadowBlur=6; rr(ctx,pad.l,y,bw2,barH*0.62,3); ctx.fill(); ctx.shadowBlur=0;
    ctx.font='600 10px JetBrains Mono'; ctx.fillStyle='#e9eef7'; ctx.textAlign='left';
    ctx.fillText(anc, pad.l+bw2+6, y+barH*0.62/2+3);
  });
  legend(ctx,['anchored','before'],w,pad,12,[[color(1)],[hexA(color(0),0.4)]]);
  set('saved-chip', Math.round((db.saved_ratio||0)*100)+'% saved');
}

/* ============================================================
   CHART: chunk heatmap scatter
   ============================================================ */
function chartChunkmap(id, db){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h, pad={l:14,r:14,t:14,b:24};
  clear(ctx,w,h);
  var cm=db.chunk_map||[];
  set('chunks-chip', cm.length+' chunks');
  if(!cm.length){ empty(ctx,w,h,'—'); return; }
  var maxHits=Math.max(1,Math.max.apply(null,cm.map(function(c){return c.hits||0;})));
  var cols=Math.ceil(Math.sqrt(cm.length*(w/h))), rows=Math.ceil(cm.length/cols);
  var cw=(w-pad.l-pad.r)/cols, chh=(h-pad.t-pad.b)/rows;
  cm.forEach(function(c,i){
    var col=i%cols, row=Math.floor(i/cols), x=pad.l+col*cw, y=pad.t+row*chh;
    var hits=c.hits||0, intensity=hits/maxHits;
    var sz=Math.min(cw,chh)*0.34*(0.5+intensity*0.5);
    var cxx=x+cw/2, cyy=y+chh/2;
    if(hits>0 && isFinite(sz) && sz>0.5){
      var g2=ctx.createRadialGradient(cxx,cyy,0,cxx,cyy,sz); g2.addColorStop(0,hexA(color(0),0.3+intensity*0.6)); g2.addColorStop(1,hexA(color(0),0));
      ctx.fillStyle=g2; ctx.fillRect(x,y,cw,chh);
      ctx.beginPath(); ctx.arc(cxx,cyy,sz*0.5,0,7); ctx.fillStyle=hexA(color(2),0.4+intensity*0.5); ctx.fill();
    } else { ctx.fillStyle='rgba(124,168,220,0.05)'; ctx.beginPath(); ctx.arc(cxx,cyy,Math.min(cw,chh)*0.18,0,7); ctx.fill(); }
  });
  ctx.font='10px Inter'; ctx.fillStyle='#8a96ad'; ctx.textAlign='left';
  ctx.fillText('● '+cm.filter(function(c){return (c.hits||0)>0;}).length+' actifs / '+cm.length+' total', pad.l, h-6);
}

/* ============================================================
   CHART: vertical bars (sessions hits)
   ============================================================ */
function chartBars(id, items, labelKey, valKey, chipId){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h, pad={l:14,r:14,t:18,b:38};
  clear(ctx,w,h);
  items=items.slice(0,24);
  if(!items.length){ empty(ctx,w,h,'—'); return; }
  var maxV=Math.max(1,Math.max.apply(null,items.map(function(d){return d[valKey]||0;})));
  var bw=w-pad.l-pad.r, barW=Math.min(42,bw/items.length*0.7), gap=(bw-barW*items.length)/(items.length+1);
  grid(ctx,w,h,pad,4);
  items.forEach(function(d,i){
    var v=d[valKey]||0, x=pad.l+gap+i*(barW+gap), bh=(v/maxV)*(h-pad.t-pad.b), y=h-pad.b-bh;
    var c=color(i);
    var grad=ctx.createLinearGradient(0,y,0,h-pad.b); grad.addColorStop(0,c); grad.addColorStop(1,hexA(c,0.25));
    ctx.fillStyle=grad; ctx.shadowColor=c; ctx.shadowBlur=8; rr(ctx,x,y,barW,bh,4); ctx.fill(); ctx.shadowBlur=0;
    if(v>0){ ctx.font='600 10px JetBrains Mono'; ctx.fillStyle='#e9eef7'; ctx.textAlign='center'; ctx.fillText(v, x+barW/2, y-5); }
    var lab=(d[labelKey]||'').toString();
    ctx.font='10px JetBrains Mono'; ctx.fillStyle='#56627a'; ctx.textAlign='center';
    ctx.save(); ctx.translate(x+barW/2, h-pad.b+6); ctx.rotate(-0.5); ctx.fillText(lab.length>10?lab.slice(0,10)+'…':lab,0,0); ctx.restore();
  });
  if(chipId) set(chipId, items.length+' sessions');
}

/* ============================================================
   CHART: throughput timeline (events per bucket)
   ============================================================ */
function chartThroughput(id, audit){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h, pad={l:34,r:14,t:14,b:22};
  clear(ctx,w,h);
  var derived=deriveSeries(audit, timeRange);
  var labels=derived.labels, data=labels.map(function(_,i){
    return Object.keys(derived.counts).reduce(function(s,t){ return s+(derived.counts[t][i]||0); },0);
  });
  if(!labels.length){ empty(ctx,w,h,'—'); return; }
  var n=labels.length, maxV=Math.max(1,Math.max.apply(null,data));
  grid(ctx,w,h,pad,4);
  ctx.fillStyle='rgba(86,98,122,0.85)'; ctx.textAlign='right'; ctx.font='10px JetBrains Mono';
  for(var s=0;s<=4;s++){ ctx.fillText(Math.round(maxV*s/4), pad.l-6, pad.t+(h-pad.t-pad.b)*(1-s/4)+3); }
  ctx.textAlign='center';
  var bw=w-pad.l-pad.r;
  var xAt=function(i){ return n>1? pad.l+bw*(i/(n-1)) : pad.l+bw/2; };
  labels.forEach(function(lb,i){ if(i%3===0||i===n-1) ctx.fillText(lb, xAt(i), h-pad.b+14); });
  var barW=Math.min(20,bw/n*0.7);
  data.forEach(function(v,i){
    var x=xAt(i), bh=(v/maxV)*(h-pad.t-pad.b), y=h-pad.b-bh;
    var c=color(0);
    var grad=ctx.createLinearGradient(0,y,0,h-pad.b); grad.addColorStop(0,c); grad.addColorStop(1,hexA(c,0.2));
    ctx.fillStyle=grad; ctx.shadowColor=c; ctx.shadowBlur=6; rr(ctx,x-barW/2,y,barW,bh,3); ctx.fill(); ctx.shadowBlur=0;
  });
  var pts=data.map(function(v,i){ return [xAt(i), h-pad.b-(v/maxV)*(h-pad.t-pad.b)]; });
  if(pts.length>1){ linePath(ctx,pts); ctx.strokeStyle=color(1); ctx.lineWidth=1.5; ctx.shadowColor=color(1); ctx.shadowBlur=6; ctx.stroke(); ctx.shadowBlur=0; }
}

/* ============================================================
   CHART: pie (tool distribution)
   ============================================================ */
function chartPie(id, items, chipId){
  var g=setup(id); if(!g) return;
  var ctx=g.ctx,w=g.w,h=g.h, cx=w/2, cy=h/2, R=Math.min(w,h)/2-22;
  clear(ctx,w,h);
  if(!items.length){ empty(ctx,w,h,'—'); return; }
  var total=items.reduce(function(s,k){return s+(k.n||0);},0);
  if(total<=0){ empty(ctx,w,h,'—'); return; }
  var start=-Math.PI/2;
  items.forEach(function(it,i){
    var v=it.n||0, frac=v/total, ang=frac*Math.PI*2;
    var c=color(i);
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.arc(cx,cy,R,start,start+ang); ctx.closePath();
    var grad=ctx.createRadialGradient(cx,cy,R*0.3,cx,cy,R); grad.addColorStop(0,c); grad.addColorStop(1,hexA(c,0.6));
    ctx.fillStyle=grad; ctx.shadowColor=c; ctx.shadowBlur=10; ctx.fill(); ctx.shadowBlur=0;
    ctx.strokeStyle='rgba(6,9,18,0.5)'; ctx.lineWidth=2; ctx.stroke();
    if(frac>0.06){ var mid=start+ang/2, lx=cx+Math.cos(mid)*R*0.65, ly=cy+Math.sin(mid)*R*0.65;
      ctx.font='600 11px Inter'; ctx.fillStyle='#fff'; ctx.textAlign='center'; ctx.fillText(it.tool||'', lx, ly); }
    start+=ang;
  });
  ctx.beginPath(); ctx.arc(cx,cy,R*0.4,0,7); ctx.fillStyle='rgba(6,9,18,0.85)'; ctx.fill();
  ctx.font='700 18px JetBrains Mono'; ctx.fillStyle='#e9eef7'; ctx.textAlign='center'; ctx.fillText(total, cx, cy-2);
  ctx.font='10px Inter'; ctx.fillStyle='#8a96ad'; ctx.fillText('calls', cx, cy+16);
  legV(ctx,items.map(function(it,i){ return [color(i), it.tool||'', it.n||0, total]; }), w, h);
  if(chipId) set(chipId, items.length+' outils');
}

/* ---------- legend helpers ---------- */
function legend(ctx,names,w,pad,top,colors){
  var x=pad.l, y=pad.t+(top||12);
  ctx.font='10.5px Inter'; ctx.textAlign='left';
  colors=colors||[];
  names.forEach(function(nm,i){
    var c=(colors[i]&&colors[i][0])||color(i);
    ctx.fillStyle=c; ctx.beginPath(); ctx.arc(x+4,y,3.5,0,7); ctx.fill();
    ctx.fillStyle='#8a96ad'; ctx.fillText(nm, x+12, y+3.5);
    x+=12+ctx.measureText(nm).width+14;
    if(x>w-60){ x=pad.l; y+=14; }
  });
}
function legV(ctx,pairs,w,h){
  var x=w*0.62, y=24, lineH=Math.min(22,(h-30)/Math.max(1,pairs.length));
  ctx.font='11px Inter';
  pairs.forEach(function(p,i){
    var c=p[0],name=p[1],v=p[2],tot=p[3];
    var yy=y+i*lineH;
    ctx.fillStyle=c; rr(ctx,x,yy-7,9,9,2); ctx.fill();
    ctx.fillStyle='#cdd6e6'; ctx.textAlign='left'; ctx.fillText(name.length>14?name.slice(0,14)+'…':name, x+14, yy);
    ctx.fillStyle='#56627a'; ctx.textAlign='right'; ctx.fillText((tot?Math.round(v/tot*100):0)+'%', w-14, yy);
  });
}

/* ---------- empty state ---------- */
function empty(ctx,w,h,msg){
  ctx.fillStyle='#56627a'; ctx.font='12px Inter'; ctx.textAlign='center';
  ctx.fillText(msg||'No data', w/2, h/2);
}

/* ============================================================
   COMPUTED METRICS
   ============================================================ */
function computeMetrics(db, audit){
  var evs=audit.recent_events||[];
  var byTool={}, byOutcome={success:0,error:0,denied:0,cache_hit:0,cache_miss:0,other:0};
  var lats=[], errs=0;
  evs.forEach(function(e){
    byTool[e.tool]=(byTool[e.tool]||0)+1;
    var oc=(e.outcome||'').toLowerCase();
    if(oc.indexOf('err')>=0){ byOutcome.error++; errs++; }
    else if(oc.indexOf('denied')>=0){ byOutcome.denied++; errs++; }
    else if(oc.indexOf('hit')>=0) byOutcome.cache_hit++;
    else if(oc.indexOf('miss')>=0) byOutcome.cache_miss++;
    else byOutcome.other++;
    if(e.latency_ms) lats.push(e.latency_ms);
  });
  lats.sort(function(a,b){return a-b;});
  var p50=lats.length?lats[Math.floor(lats.length*0.5)]:0;
  var p95=lats.length?lats[Math.min(lats.length-1,Math.floor(lats.length*0.95))]:0;
  var peak=lats.length?lats[lats.length-1]:0;
  var avg=lats.length?lats.reduce(function(a,b){return a+b;},0)/lats.length:0;
  var totalBefore=(db.tokens_per_store||[]).reduce(function(s,d){return s+(d.tokens_before||0);},0);
  var totalAnchor=(db.tokens_per_store||[]).reduce(function(s,d){return s+(d.anchor_tokens||0);},0);
  var totalSaved=totalBefore-totalAnchor;
  var toolItems=Object.keys(byTool).map(function(t){return {tool:t,n:byTool[t]};}).sort(function(a,b){return b.n-a.n;});
  var avgChunk=db.chunks>0?(totalBefore/db.chunks):0;
  var hits=(db.hits_per_session||[]).reduce(function(s,x){return s+(x.hits||0);},0);
  var hitRate=evs.length>0?(byOutcome.cache_hit/evs.length*100):0;
  return {
    byTool:byTool, toolItems:toolItems, byOutcome:byOutcome,
    p50:p50, p95:p95, peak:peak, avgLat:avg,
    totalBefore:totalBefore, totalAnchor:totalAnchor, totalSaved:totalSaved,
    avgChunk:avgChunk, hits:hits, hitRate:hitRate, errs:errs,
    activeTools:Object.keys(byTool).length
  };
}

/* ============================================================
   RENDER
   ============================================================ */
function fmtB(b){ if(b==null) return '—'; if(b<1024) return b+' B'; if(b<1048576) return (b/1024).toFixed(1)+' KB'; if(b<1073741824) return (b/1048576).toFixed(1)+' MB'; return (b/1073741824).toFixed(2)+' GB'; }
function fmtT(ts){ if(!ts) return '—'; var d=new Date(ts); if(isNaN(d)) return ts; return d.toLocaleTimeString('fr-CA',{hour12:false}); }
function fmtN(n){ if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1e3) return (n/1e3).toFixed(1)+'K'; return ''+n; }

function kpiCard(label,val,sub,ci,sparkId){
  var c=ci!=null?color(ci):'#38bdf8';
  var sp=sparkId?'<canvas class="kpi-spark" id="'+sparkId+'"></canvas>':'';
  return '<div class="kpi-card"><div class="kc-glow" style="background:'+c+'"></div>'+
    '<div class="kpi-label">'+label+'</div><div class="kpi-value" style="color:'+c+'">'+val+'</div>'+
    (sub?'<div class="kpi-sub">'+sub+'</div>':'')+sp+'</div>';
}

/* ============================================================
   SIDE PANEL (drawer)
   ============================================================ */
var curDrawer=null;
var drawerRenderers={
  kpi:function(body,snap){
    var db=snap.db||{},a=snap.audit||{},m=computeMetrics(db,a);
    var savedPct=Math.round((db.saved_ratio||0)*100);
    body.innerHTML='<div class="kpi-grid">'+[
      kpiCard('Sessions',db.sessions||0,'context actifs',0),
      kpiCard('Chunks',db.chunks||0,(db.sources||0)+' sources',1),
      kpiCard('Anchors',db.anchors||0,null,2),
      kpiCard('Invariants',db.invariants||0,null,3),
      kpiCard('Tokens saved',fmtN(m.totalSaved),'sur '+fmtN(m.totalBefore),4),
      kpiCard('Avg latence',(Math.round(m.avgLat*100)/100)+' ms',null,0),
      kpiCard('Hit rate',Math.round(m.hitRate)+'%','cache',4),
      kpiCard('Taille DB',fmtB(db.size_bytes),db.encrypted?'encrypted':'plain',5),
      kpiCard('Erreurs',m.errs,'sur '+evsLen(a)+' evts',8),
      kpiCard('Savings',savedPct+'%','tokens saved',4)
    ].join('')+'</div>';
  },
  sources:function(body,snap){
    var db=snap.db||{};
    var data=db.tokens_per_store||[];
    if(!data.length){ body.innerHTML='<p style="color:var(--dim)">No sources.</p>'; return; }
    body.innerHTML='<table class="data-table"><thead><tr><th>Label</th><th>Before</th><th>Anchored</th><th>Savings</th></tr></thead><tbody>'+
      data.map(function(d){
        var bef=d.tokens_before||0, anc=d.anchor_tokens||0, saved=bef>0?Math.round((1-anc/bef)*100):0;
        return '<tr><td>'+(d.label||('#'+d.id))+'</td><td class="mono">'+bef+'</td><td class="mono">'+anc+'</td><td class="mono">'+saved+'%</td></tr>';
      }).join('')+'</tbody></table>';
  },
  events:function(body,snap){
    var evs=(snap.audit||{}).recent_events||[];
    body.innerHTML='<input type="text" class="search-input" id="dw-search" placeholder="Filtrer…" style="width:100%;margin-bottom:10px">'+
      '<table class="data-table"><thead><tr><th>Heure</th><th>Outil</th><th>Session</th><th>Outcome</th><th>Latence</th></tr></thead><tbody id="dw-events"></tbody></table>';
    renderEvents(evs,'dw-events','');
    var inp=$('dw-search');
    if(inp) inp.addEventListener('input',function(){ renderEvents(evs,'dw-events',inp.value); });
  }
};
function openDrawer(kind){
  var r=drawerRenderers[kind]; if(!r||!lastSnap) return;
  var titles={kpi:'Details — Overview',sources:'Details — Sources',events:'Event stream'};
  set('drawer-title', titles[kind]||'Details');
  curDrawer=kind;
  r($('drawer-body'), lastSnap);
  $('drawer').classList.add('open'); $('drawer-scrim').classList.add('open');
  $('drawer').setAttribute('aria-hidden','false');
}
function closeDrawer(){
  curDrawer=null;
  $('drawer').classList.remove('open'); $('drawer-scrim').classList.remove('open');
  $('drawer').setAttribute('aria-hidden','true');
}

function render(snap){
  if(!snap) return;
  lastSnap=snap; lastMsg=Date.now();
  var db=snap.db||{}, a=snap.audit||{};
  var m=computeMetrics(db,a);
  totalEvents=a.total_events||0;
  set('ss-updated', new Date((snap.ts||0)*1000).toLocaleTimeString('fr-CA',{hour12:false}));
  if(db.db_path){ lastDB=db.db_path; var el=$('ss-db'); if(el){ el.textContent=lastDB.split(/[\\/]/).pop(); el.title=lastDB; } }
  set('ss-events', totalEvents);
  set('last-update', new Date((snap.ts||0)*1000).toLocaleTimeString('fr-CA',{hour12:false}));
  set('db-info', (db.encrypted?'🔒 ':'')+fmtB(db.size_bytes)+' · '+db.chunks+' chunks');
  rateBuf.push(a.calls_per_min||0); if(rateBuf.length>20) rateBuf.shift();
  latencyHist.push(m.p95); if(latencyHist.length>20) latencyHist.shift();
  tokenHist.push(m.totalSaved); if(tokenHist.length>20) tokenHist.shift();
  set('rate-pill',(Math.round((a.calls_per_min||0)*10)/10)+' evt/min');
  set('tb-evcount', fmtN(totalEvents)+' events');
  var savedPct=Math.round((db.saved_ratio||0)*100);

  // ---- OVERVIEW (clean: 4 KPI + 3 charts) ----
  $('kpi-grid').innerHTML=[
    kpiCard('Sessions',db.sessions||0,'context actifs',0,'spark-sess'),
    kpiCard('Chunks',db.chunks||0,db.sources+' sources',1,'spark-chunks'),
    kpiCard('Savings',savedPct+'%',fmtN(m.totalSaved)+' tok',4),
    kpiCard('Latence p95',Math.round(m.p95)+' ms','peak '+Math.round(m.peak),7,'spark-lat')
  ].join('');
  chartCalls('chart-calls', a);
  chartLat('chart-lat', a, 'lat-max');
  renderEvents(a.recent_events||[], 'events-tbody', $('event-search')?$('event-search').value:'');
  chartSpark('spark-sess', rateBuf);
  chartSpark('spark-chunks', rateBuf);
  chartSpark('spark-lat', latencyHist);
  if($('chart-gauge')) chartGauge('chart-gauge', savedPct, 'saved');
  if(curDrawer && $('drawer').classList.contains('open')) openDrawer(curDrawer);

  // ---- STORAGE ----
  $('store-kpi').innerHTML=[
    kpiCard('Sources',db.sources||0,null,1),
    kpiCard('Chunks',db.chunks||0,'avg '+Math.round(m.avgChunk)+' tok',0),
    kpiCard('Anchors',db.anchors||0,null,2),
    kpiCard('Savings',savedPct+'%',fmtN(m.totalSaved)+' tok saved',4),
    kpiCard('Size',fmtB(db.size_bytes),db.encrypted?'encrypted':'plain',5),
    kpiCard('Tokens avant',fmtN(m.totalBefore),'total',6),
    kpiCard('Anchored tokens',fmtN(m.totalAnchor),'total',0),
    kpiCard('Avg chunk',Math.round(m.avgChunk)+' tok','par chunk',3)
  ].join('');
  chartTokens('chart-tokens', db);
  chartChunkmap('chart-chunkmap', db);
  renderSources(db.tokens_per_store||[]);
  set('src-count', (db.tokens_per_store||[]).length);
  if($('chart-gauge2')) chartGauge('chart-gauge2', savedPct, 'saved');

  // ---- AUDIT ----
  $('audit-kpi').innerHTML=[
    kpiCard('Events',totalEvents,'total',3),
    kpiCard('Appels/min',Math.round((a.calls_per_min||0)*10)/10,'60 s',6),
    kpiCard('p50',Math.round(m.p50)+' ms',null,0),
    kpiCard('p95',Math.round(m.p95)+' ms',null,7),
    kpiCard('Peak',Math.round(m.peak)+' ms',null,8),
    kpiCard('Avg',Math.round(m.avgLat*100)/100+' ms',null,4),
    kpiCard('Erreurs',m.errs,((m.errs/Math.max(1,evsLen(a)))*100).toFixed(1)+'%',8),
    kpiCard('Outils actifs',m.activeTools,null,2)
  ].join('');
  chartCalls('chart-calls2', a);
  chartLat('chart-lat2', a, 'lat2-chip');
  chartOutcomes('chart-outcomes', a);
  chartThroughput('chart-throughput', a);
  renderEvents(a.recent_events||[], 'audit-tbody', $('audit-search')?$('audit-search').value:'');

  // ---- TOOLS ----
  var ti=m.toolItems;
  $('tools-kpi').innerHTML=[
    kpiCard('Outils',m.activeTools,'actifs',2),
    kpiCard('Total calls',fmtN(evsLen(a)),null,6),
    kpiCard('Top outil',ti[0]?ti[0].tool:'—',ti[0]?ti[0].n:0,0),
    kpiCard('Hit rate',Math.round(m.hitRate)+'%','cache',4)
  ].join('');
  chartPie('chart-pie', ti, 'tools-chip');
  chartBars('chart-tools-bar', ti, 'tool', 'n', null);
  renderToolsTable(ti, m);

  // ---- INVARIANTS ----
  var inv=db.invariants_by_kind||[];
  $('inv-kpi').innerHTML=[
    kpiCard('Invariants',db.invariants||0,null,3),
    kpiCard('Types',inv.length,null,2),
    kpiCard('Top',inv[0]?inv[0].kind:'—',inv[0]?inv[0].n:0,4),
    kpiCard('Total',inv.reduce(function(s,k){return s+(k.n||0);},0),null,0)
  ].join('');
  chartDonut('chart-inv', inv, 'inv-total');
  renderInvariants(inv);

  // ---- SESSIONS ----
  var sess=db.hits_per_session||[];
  $('sess-kpi').innerHTML=[
    kpiCard('Sessions',db.sessions||0,'total',0),
    kpiCard('Total hits',m.hits,null,2),
    kpiCard('Avg hits',db.sessions>0?Math.round(m.hits/db.sessions):0,'par session',4),
    kpiCard('Active',db.sessions||0,null,6)
  ].join('');
  chartBars('chart-sessions', sess, 'session_id', 'hits', 'sess-chip');
  renderSessions(sess);

  // ---- UPGRADE P1-P3 ----
  renderUpgrade(db, a, m);
}
function evsLen(a){ return (a.recent_events||[]).length; }

/* ---------- tables ---------- */
function renderEvents(evs, tbodyId, filter){
  var tb=$(tbodyId); if(!tb) return;
  filter=(filter||'').toLowerCase().trim();
  var rows=evs.slice(0,200);
  if(filter){ rows=rows.filter(function(e){ return ((e.tool||'')+(e.session_id||'')+(e.outcome||'')).toLowerCase().indexOf(filter)>=0; }); }
  rows.sort(function(a,b){
    var va=a[evSortCol], vb=b[evSortCol];
    if(evSortCol==='ts'){ va=typeof va==='string'?new Date(va).getTime():va; vb=typeof vb==='string'?new Date(vb).getTime():vb; }
    if(evSortCol==='latency_ms'){ va=va||0; vb=vb||0; }
    else { va=(''+va).toLowerCase(); vb=(''+vb).toLowerCase(); }
    return (va<vb?-1:va>vb?1:0)*evSortDir;
  });
  rows=rows.slice(0,100);
  if(!rows.length){ tb.innerHTML='<tr><td colspan="5" style="text-align:center;color:#56627a;padding:24px">No events</td></tr>'; return; }
  tb.innerHTML=rows.map(function(e){
    var bad=(e.outcome||'').toLowerCase().indexOf('err')>=0||(e.outcome||'').toLowerCase().indexOf('denied')>=0;
    var hit=(e.outcome||'').toLowerCase().indexOf('hit')>=0;
    var cls=bad?'err':hit?'ok':'warn';
    return '<tr><td class="mono">'+fmtT(e.ts)+'</td><td><b>'+(e.tool||'')+'</b></td><td class="mono" style="color:#8a96ad">'+short(e.session_id)+'</td><td><span class="tag '+cls+'">'+(e.outcome||'')+'</span></td><td class="mono">'+(e.latency_ms!=null?Math.round(e.latency_ms*100)/100+' ms':'—')+'</td></tr>';
  }).join('');
}
function short(s){ if(!s) return '—'; s=s.toString(); return s.length>14?s.slice(0,8)+'…'+s.slice(-4):s; }

function renderSources(data){
  var tb=$('sources-tbody'); if(!tb) return;
  if(!data.length){ tb.innerHTML='<tr><td colspan="5" style="text-align:center;color:#56627a;padding:24px">No sources</td></tr>'; return; }
  tb.innerHTML=data.map(function(d){
    var bef=d.tokens_before||0, anc=d.anchor_tokens||0, saved=bef>0?Math.round((1-anc/bef)*100):0;
    var lbl=(d.label||('#'+d.id))+'';
    return '<tr><td class="mono">'+(d.id||'')+'</td><td>'+lbl+'</td><td class="mono">'+bef+'</td><td class="mono">'+anc+'</td><td><div class="bar-cell"><div class="mini-bar"><span style="width:'+(bef>0?Math.max(4,(anc/bef*100)):'0')+'%"></span></div><span class="mono">'+saved+'%</span></div></td></tr>';
  }).join('');
}
function renderInvariants(inv){
  var tb=$('inv-tbody'); if(!tb) return;
  var total=inv.reduce(function(s,k){return s+(k.n||0);},0)||1;
  if(!inv.length){ tb.innerHTML='<tr><td colspan="3" style="text-align:center;color:#56627a;padding:24px">No invariants</td></tr>'; return; }
  tb.innerHTML=inv.map(function(k,i){
    var n=k.n||0, pct=Math.round(n/total*100);
    return '<tr><td><b style="color:'+color(i)+'">'+(k.kind||'')+'</b></td><td class="mono">'+n+'</td><td><div class="bar-cell"><div class="mini-bar"><span style="width:'+pct+'%;background:'+color(i)+'"></span></div><span class="mono">'+pct+'%</span></div></td></tr>';
  }).join('');
}
function renderSessions(sess){
  var tb=$('sess-tbody'); if(!tb) return;
  if(!sess.length){ tb.innerHTML='<tr><td colspan="2" style="text-align:center;color:#56627a;padding:24px">No sessions</td></tr>'; return; }
  tb.innerHTML=sess.map(function(s,i){
    return '<tr><td class="mono">'+(s.session_id||'')+'</td><td><div class="bar-cell"><div class="mini-bar"><span style="width:'+Math.min(100,(s.hits||0)*10)+'%"></span></div><span class="mono">'+(s.hits||0)+'</span></div></td></tr>';
  }).join('');
}
function renderToolsTable(ti, m){
  var tb=$('tools-tbody'); if(!tb) return;
  if(!ti.length){ tb.innerHTML='<tr><td colspan="4" style="text-align:center;color:#56627a;padding:24px">No tools</td></tr>'; return; }
  var maxN=Math.max.apply(null,ti.map(function(t){return t.n;}))||1;
  var tot=ti.reduce(function(s,t){return s+t.n;},0)||1;
  tb.innerHTML=ti.map(function(t,i){
    var pct=Math.round(t.n/tot*100);
    return '<tr><td><b style="color:'+color(i)+'">'+(t.tool||'')+'</b></td><td class="mono">'+t.n+'</td><td><div class="bar-cell"><div class="mini-bar"><span style="width:'+(t.n/maxN*100)+'%;background:'+color(i)+'"></span></div><span class="mono">'+pct+'%</span></div></td><td class="mono">'+Math.round((t.n/tot)*100)/100+'</td></tr>';
  }).join('');
}
function chartOutcomes(id, audit){
  var o=audit.outcomes||{};
  var items=Object.keys(o).map(function(k){ return {kind:k, n:o[k]}; }).filter(function(x){return x.n>0;});
  if(!items.length){
    var m=computeMetrics({},audit);
    items=Object.keys(m.byOutcome).filter(function(k){return m.byOutcome[k]>0;}).map(function(k){return {kind:k,n:m.byOutcome[k]};});
  }
  chartDonut(id, items, null);
}

/* ============================================================
   NAVIGATION
   ============================================================ */
var TITLES={
  overview:['Overview','Real-time context cache observability'],
  storage:['Storage','Base de contexte & sources'],
  audit:['Audit Live','Live event stream'],
  tools:['Outils','Distribution & performance par outil'],
  invariants:['Invariants','Stable value detection'],
  sessions:['Sessions','Activity by session'],
  quickstart:['Quickstart','Get started in 3 steps'],
  howto:['How-to','Recettes pour tâches courantes'],
  help:['Help','Glossaire & FAQ']
};
function switchSection(sec){
  activeSection=sec;
  document.querySelectorAll('.nav-item').forEach(function(b){ b.classList.toggle('active', b.dataset.section===sec); });
  document.querySelectorAll('.view').forEach(function(v){ v.classList.remove('view-active'); });
  var v=$('view-'+sec); if(v) v.classList.add('view-active');
  var t=TITLES[sec]||['','']; set('tb-title', t[0]); set('tb-sub', t[1]);
  setTimeout(function(){ if(lastSnap) redrawAll(lastSnap); }, 60);
}
function redrawAll(snap){
  var db=snap.db||{}, a=snap.audit||{}, m=computeMetrics(db,a);
  chartCalls('chart-calls',a); chartLat('chart-lat',a,'lat-max');
  chartTokens('chart-tokens',db); chartChunkmap('chart-chunkmap',db);
  chartCalls('chart-calls2',a); chartLat('chart-lat2',a,'lat2-chip'); chartOutcomes('chart-outcomes',a); chartThroughput('chart-throughput',a);
  chartPie('chart-pie',m.toolItems,'tools-chip'); chartBars('chart-tools-bar',m.toolItems,'tool','n',null);
  chartDonut('chart-inv',db.invariants_by_kind||[],'inv-total');
  chartBars('chart-sessions',db.hits_per_session||[],'session_id','hits','sess-chip');
  if($('chart-gauge')) chartGauge('chart-gauge',Math.round((db.saved_ratio||0)*100),'saved');
  if($('chart-gauge2')) chartGauge('chart-gauge2',Math.round((db.saved_ratio||0)*100),'saved');
  chartSpark('spark-sess',rateBuf); chartSpark('spark-rate',rateBuf); chartSpark('spark-lat',latencyHist); chartSpark('spark-tok',tokenHist);
}
function set(id,v){ var e=$(id); if(e) e.textContent=v; }

/* ============================================================
   SSE + buttons
   ============================================================ */
function setState(state){
  var dot=$('stream-dot'), st=$('stream-state'), pill=$('status-pill'), pt=$('pill-txt');
  dot.className='ss-dot '+(state==='live'?'live':state==='dead'?'dead':'');
  if(state==='live'){ st.textContent='live'; pill.className='pill'; pt.textContent='LIVE'; }
  else if(state==='dead'){ st.textContent='disconnected'; pill.className='pill dead'; pt.textContent='OFF'; }
  else if(state==='paused'){ st.textContent='paused'; pill.className='pill warn'; pt.textContent='PAUSED'; }
  else { st.textContent='connecting…'; pill.className='pill warn'; pt.textContent='…'; }
}
function connect(){
  if(es){ try{es.close();}catch(e){} }
  setState('connecting');
  try{ es=new EventSource('/api/stream'); }catch(e){ setState('dead'); setTimeout(connect,2000); return; }
  es.onopen=function(){ if(!paused) setState('live'); };
  es.onerror=function(){ setState('dead'); try{es.close();}catch(e){} if(!paused) setTimeout(connect,1800); };
  es.onmessage=function(e){
    if(paused) return;
    try{ render(JSON.parse(e.data)); if(es.readyState===1) setState('live'); }catch(err){ console.error(err); }
  };
}
function fetchSnap(){
  fetch('/api/snapshot').then(function(r){return r.json();}).then(function(s){ render(s); }).catch(function(e){ toast('snapshot failed'); });
}
setInterval(function(){
  if(paused) return;
  if(lastMsg && Date.now()-lastMsg>5000){ if($('stream-state').textContent==='live') setState('dead'); }
},1500);

function initButtons(){
  document.querySelectorAll('.nav-item').forEach(function(b){ b.addEventListener('click',function(){ switchSection(b.dataset.section); }); });
  document.querySelectorAll('.more-btn').forEach(function(b){ b.addEventListener('click',function(){ openDrawer(b.dataset.drawer); }); });
  var dc=$('drawer-close'); if(dc) dc.addEventListener('click',closeDrawer);
  var ds=$('drawer-scrim'); if(ds) ds.addEventListener('click',closeDrawer);
  document.addEventListener('keydown',function(e){ if(e.key==='Escape') closeDrawer(); });
  $('btn-pause').addEventListener('click',function(){
    paused=!paused; $('pause-ico').textContent=paused?'▶':'⏸'; $('pause-txt').textContent=paused?'Resume':'Pause';
    if(paused){ setState('paused'); if(es) try{es.close();}catch(e){} toast('Stream en pause'); }
    else { connect(); toast('Stream repris'); }
  });
  $('btn-refresh').addEventListener('click',function(){ fetchSnap(); toast('Snapshot refreshed'); });
  $('btn-reconnect').addEventListener('click',function(){ connect(); toast('Reconnexion…'); });
  $('btn-export').addEventListener('click',function(){
    if(!lastSnap){ toast('No snapshot'); return; }
    var blob=new Blob([JSON.stringify(lastSnap,null,2)],{type:'application/json'});
    var a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='loom-snapshot-'+Date.now()+'.json'; a.click(); URL.revokeObjectURL(a.href);
    toast('Snapshot exported');
  });
  $('btn-expand').addEventListener('click',function(){
    document.body.classList.toggle('expanded');
    toast(document.body.classList.contains('expanded')?'Panels expanded':'Panels normal');
    if(lastSnap) setTimeout(function(){redrawAll(lastSnap);},100);
  });
  $('btn-compact').addEventListener('click',function(){
    compactMode=!compactMode;
    document.body.classList.toggle('compact');
    $('btn-compact').classList.toggle('on');
    toast(compactMode?'Mode compact':'Mode normal');
    if(lastSnap) setTimeout(function(){redrawAll(lastSnap);},100);
  });
  document.querySelectorAll('.tr-btn').forEach(function(b){
    b.addEventListener('click',function(){
      timeRange=parseInt(b.dataset.range,10);
      document.querySelectorAll('.tr-btn').forEach(function(x){x.classList.remove('on');});
      b.classList.add('on');
      if(lastSnap) redrawAll(lastSnap);
      toast('Range: '+timeRange+' min');
    });
  });
  document.querySelectorAll('.cm-btn').forEach(function(b){
    b.addEventListener('click',function(){
      chartMode=b.dataset.mode;
      document.querySelectorAll('.cm-btn').forEach(function(x){x.classList.remove('on');});
      b.classList.add('on');
      if(lastSnap) redrawAll(lastSnap);
      toast('Charts: '+chartMode);
    });
  });
  document.querySelectorAll('.accent-btn').forEach(function(b){
    b.addEventListener('click',function(){ setTheme(b.dataset.theme); toast('Theme: '+b.dataset.theme); });
  });
  $('btn-autoscale').addEventListener('click',function(){
    autoScale=!autoScale; $('btn-autoscale').classList.toggle('on',autoScale);
    if(lastSnap) redrawAll(lastSnap);
    toast('Auto-scale: '+(autoScale?'ON':'OFF'));
  });
  var s1=$('event-search'); if(s1) s1.addEventListener('input',function(){ if(lastSnap) renderEvents((lastSnap.audit||{}).recent_events||[],'events-tbody',s1.value); });
  var s2=$('audit-search'); if(s2) s2.addEventListener('input',function(){ if(lastSnap) renderEvents((lastSnap.audit||{}).recent_events||[],'audit-tbody',s2.value); });
  document.querySelectorAll('.sort-th').forEach(function(th){
    th.addEventListener('click',function(){
      var col=th.dataset.sort;
      if(evSortCol===col) evSortDir*=-1; else { evSortCol=col; evSortDir=1; }
      document.querySelectorAll('.sort-th').forEach(function(x){x.classList.remove('sorted-asc','sorted-desc');});
      th.classList.add(evSortDir>0?'sorted-asc':'sorted-desc');
      if(lastSnap) { renderEvents((lastSnap.audit||{}).recent_events||[],'events-tbody',$('event-search')?$('event-search').value:'');
        renderEvents((lastSnap.audit||{}).recent_events||[],'audit-tbody',$('audit-search')?$('audit-search').value:''); }
    });
  });
}

window.addEventListener('resize',function(){ if(lastSnap) redrawAll(lastSnap); });

/* ---------- UPGRADE P1-P3 ---------- */
var MCP_TOOLS=[
  {name:'ctx_store',p:'Core',d:'Store content + auto-embed',s:'✅'},
  {name:'ctx_search',p:'P1',d:'Semantic search (FAISS) + FTS fallback',s:'✅'},
  {name:'ctx_get',p:'Core',d:'Get chunk by ID',s:'✅'},
  {name:'ctx_list_sessions',p:'Core',d:'List all sessions',s:'✅'},
  {name:'ctx_stats',p:'Core',d:'Cache statistics',s:'✅'},
  {name:'ctx_health',p:'Core',d:'Health check',s:'✅'},
  {name:'ctx_check',p:'Core',d:'Check chunk exists',s:'✅'},
  {name:'ctx_verify',p:'Core',d:'Verify invariants',s:'✅'},
  {name:'ctx_profile',p:'Core',d:'Session profile',s:'✅'},
  {name:'ctx_purge_session',p:'Core',d:'Purge session',s:'✅'},
  {name:'ctx_purge_expired',p:'P2',d:'Purge expired sessions (TTL)',s:'✅'},
  {name:'ctx_vacuum',p:'Core',d:'Vacuum DB',s:'✅'},
  {name:'ctx_store_cmd',p:'Core',d:'Store command output',s:'✅'},
  {name:'ctx_store_file',p:'Core',d:'Store file content',s:'✅'},
  {name:'ctx_transfer',p:'P2',d:'Cross-session chunk transfer',s:'NEW'},
  {name:'ctx_rebuild_index',p:'P1',d:'Rebuild FAISS HNSW index',s:'NEW'},
  {name:'ctx_vector_stats',p:'P1',d:'Vector index statistics',s:'NEW'}
];
function renderUpgrade(db,a,m){
  var vi=db.vector_index||{};
  var xfers=db.cross_session_transfers||0;
  var linked=db.cross_session_linked||0;
  var autoHits=db.auto_store_hits||0;
  var cov=db.chunks>0?Math.round((vi.n_vectors||0)/db.chunks*100):0;
  // KPIs
  $('upg-kpi').innerHTML=[
    kpiCard('Vectors',vi.n_vectors||0,vi.model||'all-MiniLM-L6-v2',4),
    kpiCard('Index size',fmtB(vi.index_size_bytes||0),'HNSW',5),
    kpiCard('Coverage',cov+'%',(vi.n_vectors||0)+' / '+db.chunks+' chunks',2),
    kpiCard('Transfers',xfers,linked+' sessions linked',0),
    kpiCard('Auto-store',autoHits,'hits (threshold 500)',6),
    kpiCard('Backend',vi.backend||'—','embedding model',1),
    kpiCard('Dim',vi.dim||384,'vector dimensions',3),
    kpiCard('Tools',17,'MCP exposed',7)
  ].join('');
  // P1 detail
  set('p1-backend',vi.backend||'—');
  set('p1-model',vi.model||'all-MiniLM-L6-v2');
  set('p1-dim',vi.dim||384);
  set('p1-vecs',vi.n_vectors||0);
  set('p1-size',fmtB(vi.index_size_bytes||0));
  set('p1-cov',cov+'%');
  // P2 detail
  set('p2-xfers',xfers);
  set('p2-linked',linked);
  // P3 detail
  set('p3-hits',autoHits);
  // Charts — use existing chart functions
  chartDonut('chart-vector', [
    {label:'Indexed', n:vi.n_vectors||0},
    {label:'Unindexed', n:Math.max(0,(db.chunks||0)-(vi.n_vectors||0))}
  ], 'vec-chip');
  chartBars('chart-xfer', [
    {label:'Transfers', n:xfers},
    {label:'Linked sessions', n:linked}
  ], 'label', 'n', 'xfer-chip');
  chartSpark('chart-autostore', [autoHits, autoHits, autoHits]);
  set('auto-chip', autoHits+' hits');
  set('tools-chip', '17 tools');
  // Tools table
  var tb=$('upg-tools-tbody'); if(tb){
    tb.innerHTML=MCP_TOOLS.map(function(t){
      var tagClass=t.p==='P1'?'tag-green':t.p==='P2'?'tag-blue':t.p==='P3'?'tag-violet':'';
      var sTag=t.s==='NEW'?'<span class="tag warn">NEW</span>':'<span class="tag ok">✅</span>';
      return '<tr><td class="mono">'+t.name+'</td><td><span class="tag '+tagClass+'">'+t.p+'</span></td><td>'+t.d+'</td><td>'+sTag+'</td></tr>';
    }).join('');
  }
}
document.addEventListener('DOMContentLoaded',function(){ initButtons(); connect(); fetchSnap(); });
})();
