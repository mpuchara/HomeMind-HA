// Each point is the server's rolling 60-second p95, sampled at most every 5 s.
// Missing windows remain gaps; the chart never manufactures latency samples.
(() => {
  window.latencySparkline = metric => {
    const rows=(metric?.trend||[]).filter(p=>Number.isFinite(p.age_seconds)&&p.age_seconds>=0&&p.age_seconds<=600);
    const valid=p=>p.p95_ms!=null&&Number.isFinite(p.p95_ms)&&p.p95_ms>=0&&p.count>0;
    const values=rows.filter(valid).map(p=>p.p95_ms);
    if(!values.length)return '';
    const ceiling=Math.max(100,...values)*1.1;
    const point=p=>[2+(600-p.age_seconds)/600*296,28-p.p95_ms/ceiling*24];
    const paths=[];let segment=[],previous=null;
    for(const row of rows){
      if(previous!=null&&previous-row.age_seconds>15&&segment.length){paths.push(segment);segment=[];}
      if(valid(row))segment.push(point(row));
      else if(segment.length){paths.push(segment);segment=[];}
      previous=row.age_seconds;
    }
    if(segment.length)paths.push(segment);
    const lines=paths.map(ps=>ps.length===1
      ?`<circle cx="${ps[0][0].toFixed(2)}" cy="${ps[0][1].toFixed(2)}" r="1.6"/>`
      :`<polyline points="${ps.map(p=>p.map(v=>v.toFixed(2)).join(',')).join(' ')}"/>`).join('');
    return `<svg class="latency-sparkline" viewBox="0 0 300 32" preserveAspectRatio="none" role="img" aria-label="Trend of rolling 60-second p95 latency over the last 10 minutes"><title>p95 of the last 60 seconds; up to 10 minutes of history. Gaps mean no samples.</title><path class="sparkline-guide" d="M2 29H298"/>${lines}</svg>`;
  };
})();
