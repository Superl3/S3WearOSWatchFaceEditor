from __future__ import annotations

import base64
import io
import json
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image


def _data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def detect_ambiguous_adjacent_pairs(
    slots: dict[int, list[tuple[int, int]]],
    point_s: dict[tuple[int, int], float],
    centers: list[float],
    circular_distance: Any,
    *,
    minimum_pixels: int = 8,
    minimum_ratio: float = 0.003,
) -> list[dict[str, Any]]:
    """Find adjacent slots whose assigned pixels disagree with the nearest prior."""

    pair_counts: dict[tuple[int, int], int] = {}
    for slot_index, points in slots.items():
        for point in points:
            s = point_s[point]
            nearest = min(range(len(centers)), key=lambda candidate: circular_distance(s, centers[candidate]))
            if nearest == slot_index or (nearest - slot_index) % len(centers) not in (1, len(centers) - 1):
                continue
            pair = tuple(sorted((slot_index, nearest)))
            pair_counts[pair] = pair_counts.get(pair, 0) + 1
    result = []
    for (slot_a, slot_b), count in sorted(pair_counts.items()):
        denominator = max(1, min(len(slots.get(slot_a, [])), len(slots.get(slot_b, []))))
        ratio = count / denominator
        if count >= minimum_pixels and ratio >= minimum_ratio:
            result.append({"slotA": slot_a, "slotB": slot_b, "disputedPixelCount": count, "disputedRatio": round(ratio, 6)})
    return result


def generate_manual_boundary_review(
    image: Image.Image,
    slots: dict[int, list[tuple[int, int]]],
    pairs: list[dict[str, Any]],
    output_root: Path,
    *,
    padding: int = 8,
) -> list[dict[str, Any]]:
    """Create a local pixel editor for each uncertain adjacent marker boundary."""

    output_root.mkdir(parents=True, exist_ok=True)
    reports = []
    for pair in pairs:
        slot_a, slot_b = int(pair["slotA"]), int(pair["slotB"])
        points_a = slots.get(slot_a, [])
        points_b = slots.get(slot_b, [])
        all_points = points_a + points_b
        if not all_points:
            continue
        left = max(0, min(x for x, _ in all_points) - padding)
        top = max(0, min(y for _, y in all_points) - padding)
        right = min(image.width, max(x for x, _ in all_points) + padding + 1)
        bottom = min(image.height, max(y for _, y in all_points) + padding + 1)
        roi = (left, top, right, bottom)
        pair_root = output_root / f"slot-{slot_a:02d}-{slot_b:02d}"
        pair_root.mkdir(parents=True, exist_ok=True)
        source = image.convert("RGB").crop(roi)
        mask_a = Image.new("L", source.size, 0)
        mask_b = Image.new("L", source.size, 0)
        pixels_a, pixels_b = mask_a.load(), mask_b.load()
        for x, y in points_a:
            if left <= x < right and top <= y < bottom:
                pixels_a[x - left, y - top] = 255
        for x, y in points_b:
            if left <= x < right and top <= y < bottom:
                pixels_b[x - left, y - top] = 255
        source.save(pair_root / "source-roi.png")
        mask_a.save(pair_root / f"auto-slot-{slot_a:02d}.png")
        mask_b.save(pair_root / f"auto-slot-{slot_b:02d}.png")
        metadata = {
            **pair,
            "fullSize": {"width": image.width, "height": image.height},
            "roi": {"x": left, "y": top, "width": right - left, "height": bottom - top},
            "manualOwnershipFile": "manual-ownership.png",
            "palette": {"slotA": "#FF0000", "slotB": "#00FFFF", "background": "#000000"},
            "requiresHumanReview": True,
        }
        (pair_root / "boundary-review.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        html = _editor_html_v2(metadata, _data_url(source), _data_url(mask_a), _data_url(mask_b))
        (pair_root / "editor.html").write_text(html, encoding="utf-8")
        reports.append({**metadata, "editor": str(pair_root / "editor.html")})
    return reports


def load_manual_boundary_ownership(root: Path | None, image_size: tuple[int, int]) -> list[dict[str, Any]]:
    """Load user-painted full-canvas ownership PNGs from a previous review bundle."""

    if root is None or not root.exists():
        return []
    result = []
    for metadata_path in sorted(root.rglob("boundary-review.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        ownership_path = metadata_path.parent / metadata.get("manualOwnershipFile", "manual-ownership.png")
        if not ownership_path.exists():
            continue
        ownership = Image.open(ownership_path).convert("RGB")
        if ownership.size != image_size:
            raise ValueError(f"manual ownership size mismatch: {ownership_path}: {ownership.size} != {image_size}")
        result.append({**metadata, "path": str(ownership_path), "image": ownership})
    return result


def _editor_html(metadata: dict[str, Any], source_url: str, mask_a_url: str, mask_b_url: str) -> str:
    data = json.dumps(metadata)
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>Photo2WFF boundary review</title>
<style>
body{{font:14px system-ui;background:#161616;color:#eee;margin:20px}} button,select{{margin:4px;padding:7px}}
canvas{{image-rendering:pixelated;border:1px solid #666;cursor:crosshair;max-width:92vw;background:#000}}
.legend span{{display:inline-block;width:12px;height:12px;margin:0 5px 0 12px}}
</style></head><body>
<h2>Perimeter slots {metadata['slotA']} / {metadata['slotB']}</h2>
<p>확실한 픽셀만 각 slot에 칠하고, 어느 글자에도 속하지 않는 접촉부는 Background로 지정하세요.</p>
<div class="legend"><span style="background:#f00"></span>slot {metadata['slotA']} <span style="background:#0ff"></span>slot {metadata['slotB']} <span style="background:#000;border:1px solid #888"></span>background</div>
<div><button data-tool="1">Slot {metadata['slotA']}</button><button data-tool="2">Slot {metadata['slotB']}</button><button data-tool="0">Background</button>
Brush <select id="brush"><option>1</option><option>3</option><option>5</option></select><button id="undo">Undo</button><button id="reset">Reset</button><button id="save">Download ownership PNG</button></div>
<canvas id="canvas"></canvas>
<script>
const meta={data}; const sourceUrl={json.dumps(source_url)}, maskAUrl={json.dumps(mask_a_url)}, maskBUrl={json.dumps(mask_b_url)};
const canvas=document.getElementById('canvas'),ctx=canvas.getContext('2d',{{willReadFrequently:true}}); let owner,initial,tool=1,history=[],drawing=false;
const load=url=>new Promise(r=>{{const i=new Image();i.onload=()=>r(i);i.src=url}});
Promise.all([load(sourceUrl),load(maskAUrl),load(maskBUrl)]).then(([source,a,b])=>{{
 canvas.width=meta.roi.width;canvas.height=meta.roi.height;canvas.style.width=(meta.roi.width*4)+'px';canvas.style.height=(meta.roi.height*4)+'px';
 const t=document.createElement('canvas'),x=t.getContext('2d');t.width=canvas.width;t.height=canvas.height;x.drawImage(a,0,0);const ap=x.getImageData(0,0,t.width,t.height).data;x.clearRect(0,0,t.width,t.height);x.drawImage(b,0,0);const bp=x.getImageData(0,0,t.width,t.height).data;
 owner=new Uint8Array(canvas.width*canvas.height);for(let i=0;i<owner.length;i++)owner[i]=ap[i*4]>127?1:(bp[i*4]>127?2:0);initial=owner.slice();window.sourceImage=source;render();
}});
function render(){{ctx.drawImage(window.sourceImage,0,0);const im=ctx.getImageData(0,0,canvas.width,canvas.height);for(let i=0;i<owner.length;i++){{if(owner[i]===1){{im.data[i*4]=255;im.data[i*4+1]*=.35;im.data[i*4+2]*=.35}}else if(owner[i]===2){{im.data[i*4]*=.25;im.data[i*4+1]=255;im.data[i*4+2]=255}}}}ctx.putImageData(im,0,0)}}
function paint(e){{const r=canvas.getBoundingClientRect(),x=Math.floor((e.clientX-r.left)*canvas.width/r.width),y=Math.floor((e.clientY-r.top)*canvas.height/r.height),s=+document.getElementById('brush').value,h=Math.floor(s/2);for(let yy=y-h;yy<=y+h;yy++)for(let xx=x-h;xx<=x+h;xx++)if(xx>=0&&yy>=0&&xx<canvas.width&&yy<canvas.height)owner[yy*canvas.width+xx]=tool;render()}}
canvas.onpointerdown=e=>{{history.push(owner.slice());if(history.length>20)history.shift();drawing=true;canvas.setPointerCapture(e.pointerId);paint(e)}};canvas.onpointermove=e=>{{if(drawing)paint(e)}};canvas.onpointerup=()=>drawing=false;
document.querySelectorAll('[data-tool]').forEach(b=>b.onclick=()=>tool=+b.dataset.tool);document.getElementById('undo').onclick=()=>{{if(history.length){{owner=history.pop();render()}}}};document.getElementById('reset').onclick=()=>{{history.push(owner.slice());owner=initial.slice();render()}};
document.getElementById('save').onclick=()=>{{const full=document.createElement('canvas'),x=full.getContext('2d'),im=x.createImageData(meta.fullSize.width,meta.fullSize.height);full.width=meta.fullSize.width;full.height=meta.fullSize.height;for(let y=0;y<canvas.height;y++)for(let xx=0;xx<canvas.width;xx++){{const v=owner[y*canvas.width+xx],i=((y+meta.roi.y)*full.width+xx+meta.roi.x)*4;if(v===1)im.data[i]=255;else if(v===2){{im.data[i+1]=255;im.data[i+2]=255}}im.data[i+3]=255}}x.putImageData(im,0,0);full.toBlob(blob=>{{const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=meta.manualOwnershipFile;a.click();URL.revokeObjectURL(a.href)}},'image/png')}};
</script></body></html>"""


def _editor_html_v2(metadata: dict[str, Any], source_url: str, mask_a_url: str, mask_b_url: str) -> str:
    template = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>Photo2WFF boundary review</title>
<style>
body{font:14px system-ui;background:#161616;color:#eee;margin:20px;max-width:1100px}
button,select{margin:4px;padding:7px}.step{padding:12px 0}.hidden{display:none}
canvas{image-rendering:pixelated;border:1px solid #666;cursor:crosshair;max-width:92vw;background:#000}
.legend span{display:inline-block;width:12px;height:12px;margin:0 5px 0 12px}.status{color:#9f9;margin:8px 0}.warning{color:#ffcf66}
</style></head><body>
<h2>Perimeter slots __A__ / __B__</h2>
<div id="step1" class="step"><h3>1. 원본 확인</h3><p>인접한 두 slot의 pixel ownership을 정합니다. Transparent 영역은 편집 대상이 아니며, 색칠해도 저장 결과에 영향을 주지 않습니다.</p><p class="warning">실제 foreground pixel만 Slot A, Slot B 또는 Background로 지정하세요.</p><button id="toStep2">다음 단계로 넘어가기</button></div>
<div id="step2" class="step hidden"><h3>2. 픽셀 소유권 편집</h3><div class="legend"><span style="background:#f00"></span>slot __A__ <span style="background:#0ff"></span>slot __B__ <span style="background:#000;border:1px solid #888"></span>background</div><div><button data-tool="1">Slot __A__</button><button data-tool="2">Slot __B__</button><button data-tool="0">Background</button> Brush <select id="brush"><option>1</option><option>3</option><option>5</option></select><button id="undo">Undo</button><button id="reset">Reset</button></div><canvas id="canvas"></canvas><p><button id="toStep3">다음 단계로 넘어가기</button></p></div>
<div id="step3" class="step hidden"><h3>3. 결과 검토 및 저장</h3><p>Transparent 영역은 저장에서 제외됩니다. 통계를 확인한 뒤 ownership PNG를 저장하세요.</p><div id="stats" class="status"></div><canvas id="preview"></canvas><p><button id="back">이전 단계</button><button id="save">파이프라인에 바로 적용</button></p><div id="saveStatus" class="status"></div></div>
<script>
const meta=__META__,sourceUrl=__SOURCE__,maskAUrl=__MASKA__,maskBUrl=__MASKB__;
const canvas=document.getElementById('canvas'),ctx=canvas.getContext('2d',{willReadFrequently:true}),preview=document.getElementById('preview'),pctx=preview.getContext('2d');let owner,initial,editable,tool=1,history=[],drawing=false;
const load=url=>new Promise(resolve=>{const image=new Image();image.onload=()=>resolve(image);image.src=url});
function setStep(n){for(let i=1;i<=3;i++)document.getElementById('step'+i).classList.toggle('hidden',i!==n);render();updateStats()}
Promise.all([load(sourceUrl),load(maskAUrl),load(maskBUrl)]).then(([source,a,b])=>{canvas.width=meta.roi.width;canvas.height=meta.roi.height;canvas.style.width=(meta.roi.width*4)+'px';canvas.style.height=(meta.roi.height*4)+'px';const scratch=document.createElement('canvas'),sx=scratch.getContext('2d');scratch.width=canvas.width;scratch.height=canvas.height;sx.drawImage(a,0,0);const ap=sx.getImageData(0,0,scratch.width,scratch.height).data;sx.clearRect(0,0,scratch.width,scratch.height);sx.drawImage(b,0,0);const bp=sx.getImageData(0,0,scratch.width,scratch.height).data;owner=new Uint8Array(canvas.width*canvas.height);editable=new Uint8Array(owner.length);for(let i=0;i<owner.length;i++){const aa=ap[i*4]>127,bb=bp[i*4]>127;editable[i]=(aa||bb)?1:0;owner[i]=aa?1:(bb?2:0)}initial=owner.slice();window.sourceImage=source;setStep(1)});
function render(){if(!window.sourceImage||!owner)return;ctx.drawImage(window.sourceImage,0,0);const im=ctx.getImageData(0,0,canvas.width,canvas.height);for(let i=0;i<owner.length;i++){if(!editable[i])continue;if(owner[i]===1){im.data[i*4]=255;im.data[i*4+1]*=.35;im.data[i*4+2]*=.35}else if(owner[i]===2){im.data[i*4]*=.25;im.data[i*4+1]=255;im.data[i*4+2]=255}else{im.data[i*4]*=.35;im.data[i*4+1]*=.35;im.data[i*4+2]*=.35}}ctx.putImageData(im,0,0);if(preview.width!==canvas.width||preview.height!==canvas.height){preview.width=canvas.width;preview.height=canvas.height;preview.style.width=(canvas.width*4)+'px';preview.style.height=(canvas.height*4)+'px'}pctx.drawImage(canvas,0,0)}
function updateStats(){if(!owner)return;let a=0,b=0,bg=0,ignored=0;for(let i=0;i<owner.length;i++){if(!editable[i]){ignored++;continue}if(owner[i]===1)a++;else if(owner[i]===2)b++;else bg++}document.getElementById('stats').textContent='slot __A__: '+a+' px | slot __B__: '+b+' px | background: '+bg+' px | transparent ignored: '+ignored+' px'}
function paint(e){const r=canvas.getBoundingClientRect(),x=Math.floor((e.clientX-r.left)*canvas.width/r.width),y=Math.floor((e.clientY-r.top)*canvas.height/r.height),s=+document.getElementById('brush').value,h=Math.floor(s/2);for(let yy=y-h;yy<=y+h;yy++)for(let xx=x-h;xx<=x+h;xx++)if(xx>=0&&yy>=0&&xx<canvas.width&&yy<canvas.height&&editable[yy*canvas.width+xx])owner[yy*canvas.width+xx]=tool;render();updateStats()}
canvas.onpointerdown=e=>{history.push(owner.slice());if(history.length>20)history.shift();drawing=true;canvas.setPointerCapture(e.pointerId);paint(e)};canvas.onpointermove=e=>{if(drawing)paint(e)};canvas.onpointerup=()=>drawing=false;
document.querySelectorAll('[data-tool]').forEach(button=>button.onclick=()=>tool=+button.dataset.tool);document.getElementById('undo').onclick=()=>{if(history.length){owner=history.pop();render();updateStats()}};document.getElementById('reset').onclick=()=>{history.push(owner.slice());owner=initial.slice();render();updateStats()};document.getElementById('toStep2').onclick=()=>setStep(2);document.getElementById('toStep3').onclick=()=>setStep(3);document.getElementById('back').onclick=()=>setStep(2);
document.getElementById('save').onclick=()=>{const full=document.createElement('canvas');full.width=meta.fullSize.width;full.height=meta.fullSize.height;const fx=full.getContext('2d'),im=fx.createImageData(full.width,full.height);for(let y=0;y<canvas.height;y++)for(let xx=0;xx<canvas.width;xx++){const local=y*canvas.width+xx;if(!editable[local])continue;const v=owner[local],i=((y+meta.roi.y)*full.width+xx+meta.roi.x)*4;if(v===1)im.data[i]=255;else if(v===2){im.data[i+1]=255;im.data[i+2]=255}im.data[i+3]=255}fx.putImageData(im,0,0);full.toBlob(blob=>{const status=document.getElementById('saveStatus');if(location.protocol==='http:'||location.protocol==='https:'){const reader=new FileReader();reader.onload=()=>{fetch('/__photo2wff_apply__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slotA:meta.slotA,slotB:meta.slotB,png:reader.result})}).then(async response=>{const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'저장 실패');status.textContent='파이프라인에 적용됨: '+data.path}).catch(error=>{status.textContent='적용 실패: '+error.message})};reader.readAsDataURL(blob)}else{const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=meta.manualOwnershipFile;link.click();URL.revokeObjectURL(link.href);status.textContent='파일로 저장했습니다. 로컬 편집기 서버를 실행하면 바로 적용할 수 있습니다.'}},'image/png')};
</script></body></html>"""
    return (template.replace("__META__", json.dumps(metadata)).replace("__SOURCE__", json.dumps(source_url)).replace("__MASKA__", json.dumps(mask_a_url)).replace("__MASKB__", json.dumps(mask_b_url)).replace("__A__", str(metadata["slotA"])).replace("__B__", str(metadata["slotB"])))


def serve_manual_boundary_review(
    review_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    """Serve boundary editors and persist their save action into the review bundle.

    A file:// page cannot write into the workspace. This small local server keeps
    the editor self-contained while giving the Save button a safe, explicit
    endpoint that writes the expected per-slot ownership PNG.
    """

    root = Path(review_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"manual boundary review root does not exist: {root}")
    editors = sorted(root.rglob("editor.html"))
    if not editors:
        raise FileNotFoundError(f"no editor.html found below: {root}")

    class Handler(SimpleHTTPRequestHandler):
        server_version = "Photo2WFFBoundaryReview/1.0"

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            if urlsplit(self.path).path != "/__photo2wff_apply__":
                self.send_error(404)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 20 * 1024 * 1024:
                    raise ValueError("request body is missing or too large")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                slot_a, slot_b = sorted((int(payload["slotA"]), int(payload["slotB"])))
                if not (0 <= slot_a < 12 and 0 <= slot_b < 12 and slot_a != slot_b):
                    raise ValueError("invalid perimeter slot pair")
                pair_root = (root / f"slot-{slot_a:02d}-{slot_b:02d}").resolve()
                pair_root.relative_to(root)
                metadata_path = pair_root / "boundary-review.json"
                if not metadata_path.exists():
                    raise ValueError(f"review pair does not exist: {slot_a}, {slot_b}")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if sorted((int(metadata["slotA"]), int(metadata["slotB"]))) != [slot_a, slot_b]:
                    raise ValueError("slot pair does not match boundary-review.json")
                encoded = str(payload["png"])
                if "," in encoded:
                    encoded = encoded.split(",", 1)[1]
                png_bytes = base64.b64decode(encoded, validate=True)
                with Image.open(io.BytesIO(png_bytes)) as ownership:
                    expected_size = (int(metadata["fullSize"]["width"]), int(metadata["fullSize"]["height"]))
                    if ownership.size != expected_size:
                        raise ValueError(f"ownership image size mismatch: {ownership.size} != {expected_size}")
                    if ownership.format != "PNG":
                        raise ValueError("ownership image must be PNG")
                ownership_path = pair_root / str(metadata.get("manualOwnershipFile", "manual-ownership.png"))
                ownership_path.resolve().relative_to(pair_root)
                ownership_path.write_bytes(png_bytes)
                relative_path = ownership_path.relative_to(root).as_posix()
                self._send_json(200, {"ok": True, "path": relative_path})
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
                self._send_json(400, {"ok": False, "error": str(error)})

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), partial(Handler, directory=str(root)))
    base_url = f"http://{host}:{server.server_port}"
    print(f"Photo2WFF boundary editor: {base_url}")
    for editor in editors:
        print(f"  {base_url}/{editor.relative_to(root).as_posix()}")
    if open_browser:
        webbrowser.open(f"{base_url}/{editors[0].relative_to(root).as_posix()}")
    print("저장 후 이 창에서 Ctrl+C를 누르면 종료됩니다.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
