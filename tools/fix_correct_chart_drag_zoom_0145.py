from pathlib import Path
import json

p = Path('adaptive_ai/src/static/agent_workflow_ui.js')
text = p.read_text(encoding='utf-8')

old_intro = '<p>Zaznacz błędny historyczny moment i podaj prawidłowe Desired. Correct nie zmienia Gen ${subject.generation_number} w miejscu — po zatwierdzeniu utworzy child Candidate.</p>'
new_intro = '<p>Kliknij wykres, aby wskazać moment, albo przeciągnij poziomo po wykresie, aby zaznaczyć zakres i go przybliżyć. Kółko myszy przybliża wokół kursora. Correct nie zmienia Gen ${subject.generation_number} w miejscu — po zatwierdzeniu utworzy child Candidate.</p>'
if old_intro not in text:
    raise SystemExit('Correct dialog intro anchor not found')
text = text.replace(old_intro, new_intro, 1)

old_zoom = "function zoom(factor){const width=Math.max(10,Math.min(31*86400,(range.end-range.start)*factor)),center=(range.start+range.end)/2;range={start:center-width/2,end:center+width/2};load();}"
new_zoom = "function zoom(factor,anchor=.5){if(!Number.isFinite(range.end-range.start))return;const oldWidth=range.end-range.start,width=Math.max(10,Math.min(31*86400,oldWidth*factor)),center=range.start+oldWidth*anchor;range={start:center-width*anchor,end:center+width*(1-anchor)};load();}"
if old_zoom not in text:
    raise SystemExit('Correct zoom function anchor not found')
text = text.replace(old_zoom, new_zoom, 1)

old_draw = '''    box.innerHTML=`<svg viewBox="0 0 1030 350" role="img" aria-label="Correct direct-parent generation history chart">${rendered.join('')}${labels}${chosen}<rect data-hit x="50" y="25" width="930" height="285" fill="transparent" style="cursor:crosshair"/></svg>`;
    box.querySelector('[data-hit]').onclick=ev=>{const svg=ev.currentTarget.ownerSVGElement,r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)*1030/r.width,ts=start+(Math.max(50,Math.min(980,px))-50)/930*width;inspect(ts);};'''
new_draw = '''    box.innerHTML=`<svg viewBox="0 0 1030 350" role="img" aria-label="Correct direct-parent generation history chart" tabindex="0">${rendered.join('')}${labels}${chosen}<rect data-selection x="50" y="25" width="0" height="285" fill="#9aa0a6" fill-opacity="0.28" stroke="#d4d7da" stroke-opacity="0.75" visibility="hidden"/><rect data-hit x="50" y="25" width="930" height="285" fill="transparent" style="cursor:crosshair;touch-action:none"/></svg>`;
    const svg=box.querySelector('svg'),hit=box.querySelector('[data-hit]'),selection=box.querySelector('[data-selection]');let down=null;
    const fraction=ev=>{const r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)*1030/r.width;return Math.max(0,Math.min(1,(px-50)/930));};
    const hideSelection=()=>selection.setAttribute('visibility','hidden');
    const showSelection=endFraction=>{if(down==null)return;const a=50+930*Math.min(down,endFraction),b=50+930*Math.max(down,endFraction);selection.setAttribute('x',a);selection.setAttribute('width',Math.max(1,b-a));selection.setAttribute('visibility','visible');};
    svg.onwheel=ev=>{ev.preventDefault();zoom(ev.deltaY<0?.5:2,fraction(ev));};
    hit.onpointerdown=ev=>{down=fraction(ev);showSelection(down);hit.setPointerCapture(ev.pointerId);};
    hit.onpointermove=ev=>{if(down!=null)showSelection(fraction(ev));};
    hit.onpointercancel=()=>{down=null;hideSelection();};
    hit.onlostpointercapture=()=>{if(down!=null){down=null;hideSelection();}};
    hit.onpointerup=ev=>{if(down==null)return;const endFraction=fraction(ev),startFraction=down;down=null;hideSelection();try{hit.releasePointerCapture(ev.pointerId);}catch(_){ }if(Math.abs(endFraction-startFraction)>.015){range={start:start+Math.min(startFraction,endFraction)*width,end:start+Math.max(startFraction,endFraction)*width};load();}else inspect(start+endFraction*width);};'''
if old_draw not in text:
    raise SystemExit('Correct chart click-only handler anchor not found')
text = text.replace(old_draw, new_draw, 1)
p.write_text(text, encoding='utf-8')

test = Path('tests/test_correct_chart_drag_zoom.py')
test.write_text('''from pathlib import Path\nimport unittest\n\nROOT = Path(__file__).resolve().parents[1]\n\n\nclass CorrectChartDragZoomContract(unittest.TestCase):\n    def test_correct_chart_supports_drag_selection_and_cursor_anchored_zoom(self):\n        text = (ROOT / "adaptive_ai/src/static/agent_workflow_ui.js").read_text(encoding="utf-8")\n        self.assertIn("przeciągnij poziomo po wykresie", text)\n        self.assertIn("data-selection", text)\n        self.assertIn("svg.onwheel=", text)\n        self.assertIn("hit.onpointerdown=", text)\n        self.assertIn("hit.onpointermove=", text)\n        self.assertIn("hit.onpointerup=", text)\n        self.assertIn("Math.abs(endFraction-startFraction)>.015", text)\n        self.assertIn("range={start:start+Math.min(startFraction,endFraction)*width,end:start+Math.max(startFraction,endFraction)*width}", text)\n        self.assertIn("else inspect(start+endFraction*width)", text)\n        self.assertIn("function zoom(factor,anchor=.5)", text)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''', encoding='utf-8')

replacements = {
    Path('adaptive_ai/config.yaml'): [('version: "0.14.4"', 'version: "0.14.5"')],
    Path('adaptive_ai/src/settings.py'): [('APP_VERSION = "0.14.4"', 'APP_VERSION = "0.14.5"')],
    Path('adaptive_ai/Dockerfile'): [('ARG BUILD_VERSION=0.14.4', 'ARG BUILD_VERSION=0.14.5')],
    Path('adaptive_ai/src/static/index.html'): [('0.14.4', '0.14.5')],
}
for path, pairs in replacements.items():
    value = path.read_text(encoding='utf-8')
    for old, new in pairs:
        if old not in value:
            raise SystemExit(f'{path}: version anchor {old!r} not found')
        value = value.replace(old, new)
    path.write_text(value, encoding='utf-8')

info_path = Path('adaptive_ai/BUILD_INFO.json')
info = json.loads(info_path.read_text(encoding='utf-8'))
info['version'] = '0.14.5'
info['release_date'] = '2026-09-16'
info['base_version'] = '0.14.4'
info['base_commit'] = '004d461c'
info['release_status'] = 'release-0.14.5-correct-chart-drag-zoom-ci-required'
info['tests_passed'] = 553
info['docker_build'] = 'github-actions-0.14.5-smoke-required'
info['correct_chart_navigation'] = 'click inspects; horizontal pointer drag selects and zooms the chosen time range; mouse wheel zooms around cursor; +/- and previous/next controls remain available'
info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
