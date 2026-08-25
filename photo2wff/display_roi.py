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

from PIL import Image, ImageDraw


def _largest_dark_component(image: Image.Image, threshold: int) -> tuple[int, int, int, int] | None:
    rgb = image.convert("RGB")
    width, height = rgb.size
    mask = bytearray(width * height)
    pixels = rgb.load()
    for y in range(height):
        for x in range(width):
            if max(pixels[x, y]) < threshold:
                mask[y * width + x] = 1
    best: tuple[int, int, int, int, int] | None = None
    for start in range(width * height):
        if not mask[start]:
            continue
        queue = [start]
        mask[start] = 0
        count = 0
        min_x = max_x = start % width
        min_y = max_y = start // width
        while queue:
            position = queue.pop()
            x, y = position % width, position // width
            count += 1
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for next_position in (position - 1, position + 1, position - width, position + width):
                if next_position < 0 or next_position >= width * height or not mask[next_position]:
                    continue
                if next_position == position - 1 and x == 0:
                    continue
                if next_position == position + 1 and x == width - 1:
                    continue
                mask[next_position] = 0
                queue.append(next_position)
        candidate = (min_x, min_y, max_x + 1, max_y + 1, count)
        if best is None or candidate[4] > best[4]:
            best = candidate
    return best[:4] if best else None


def propose_display_roi(reference: Path) -> dict[str, Any]:
    """Suggest a display ROI without cropping, resizing, or normalizing the source."""

    image = Image.open(reference).convert("RGB")
    body = _largest_dark_component(image, threshold=75)
    strict_display = _largest_dark_component(image, threshold=10)
    if body is None or strict_display is None:
        raise ValueError("could not propose a dark display ROI")
    min_x, _, max_x, _ = body
    _, min_y, _, max_y = strict_display
    body_width = max_x - min_x
    crop_box = {
        "x": min_x + round(body_width * 0.05),
        "y": min_y,
        "width": (max_x - round(body_width * 0.10)) - (min_x + round(body_width * 0.05)),
        "height": max_y - min_y,
    }
    crop_box["radius"] = min(round(crop_box["height"] * 0.10), min(crop_box["width"], crop_box["height"]) // 2)
    return {
        "schemaVersion": "1.0",
        "confirmed": False,
        "source": str(reference),
        "sourceSize": {"width": image.width, "height": image.height},
        "displayRoi": crop_box,
        "proposal": {"method": "dark-body plus strict-display bounds", "confidence": 0.65},
    }


def _validate_roi(value: dict[str, Any], source_size: tuple[int, int]) -> dict[str, int]:
    width, height = source_size
    raw = value.get("displayRoi", value)
    result = {key: int(round(float(raw[key]))) for key in ("x", "y", "width", "height", "radius")}
    if result["width"] <= 0 or result["height"] <= 0 or result["radius"] < 0:
        raise ValueError("ROI dimensions must be positive and radius must be non-negative")
    if result["x"] < 0 or result["y"] < 0 or result["x"] + result["width"] > width or result["y"] + result["height"] > height:
        raise ValueError("ROI must remain inside the original image")
    if result["radius"] > min(result["width"], result["height"]) // 2:
        raise ValueError("ROI radius is larger than the display bounds")
    return result


def load_confirmed_display_roi(path: Path, source_size: tuple[int, int] | None = None) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("confirmed") is not True:
        raise ValueError(f"display ROI has not been approved: {path}")
    if source_size is not None:
        payload["displayRoi"] = _validate_roi(payload, source_size)
    return payload


def _data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def generate_display_roi_review(reference: Path, output_root: Path) -> dict[str, Any]:
    """Create a human-editable ROI proposal; no normalized image is produced."""

    output_root.mkdir(parents=True, exist_ok=True)
    image = Image.open(reference).convert("RGB")
    proposal = propose_display_roi(reference)
    overlay = image.copy()
    draw = ImageDraw.Draw(overlay)
    roi = proposal["displayRoi"]
    draw.rounded_rectangle((roi["x"], roi["y"], roi["x"] + roi["width"] - 1, roi["y"] + roi["height"] - 1), radius=roi["radius"], outline=(255, 220, 50), width=max(2, image.width // 300))
    overlay.save(output_root / "display-roi-proposal.png")
    proposal["reviewRoot"] = str(output_root)
    (output_root / "display-roi-proposal.json").write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html = _editor_html(proposal, _data_url(image), _data_url(overlay))
    (output_root / "editor.html").write_text(html, encoding="utf-8")
    return {"status": "awaiting_display_roi_confirmation", "editor": str(output_root / "editor.html"), "proposal": str(output_root / "display-roi-proposal.json"), "overlay": str(output_root / "display-roi-proposal.png")}


def _editor_html(metadata: dict[str, Any], source_url: str, overlay_url: str) -> str:
    template = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>Photo2WFF Display ROI Confirmation</title>
<style>body{font:14px system-ui;background:#171717;color:#eee;margin:20px;max-width:1100px}label{display:inline-flex;flex-direction:column;margin:5px}input{width:100px;padding:6px}button{padding:8px;margin:5px}.status{color:#9f9}.warning{color:#ffcf66}img{max-width:92vw;border:1px solid #666;image-rendering:auto}</style></head><body>
<h2>Display ROI Confirmation Gate</h2>
<p class="warning">노란 선은 자동 제안입니다. 실제 display 내부만 포함하고 device frame은 제외한 뒤 승인하세요.</p>
<img id="preview" src="__OVERLAY__"><p>
<label>X<input id="x" type="number"></label><label>Y<input id="y" type="number"></label><label>Width<input id="width" type="number"></label><label>Height<input id="height" type="number"></label><label>Radius<input id="radius" type="number"></label></p>
<button id="refresh">미리보기 갱신</button><button id="save">ROI 승인 및 pipeline에 적용</button><div id="status" class="status"></div>
<script>
const meta=__META__, sourceUrl=__SOURCE__, overlay=document.getElementById('preview');
const fields=['x','y','width','height','radius']; fields.forEach(k=>document.getElementById(k).value=meta.displayRoi[k]);
function values(){const result={};fields.forEach(k=>result[k]=Number(document.getElementById(k).value));return result}
function redraw(){const image=new Image();image.onload=()=>{const canvas=document.createElement('canvas');canvas.width=image.width;canvas.height=image.height;const c=canvas.getContext('2d');c.drawImage(image,0,0);const r=values();c.strokeStyle='#ffdc32';c.lineWidth=Math.max(2,image.width/300);c.beginPath();c.roundRect(r.x,r.y,r.width,r.height,r.radius);c.stroke();overlay.src=canvas.toDataURL('image/png')};image.src=sourceUrl}
document.getElementById('refresh').onclick=redraw;
document.getElementById('save').onclick=()=>{const payload={...meta,confirmed:true,displayRoi:values(),approvedBy:'human',approvedAt:new Date().toISOString()};const status=document.getElementById('status');if(location.protocol==='http:'||location.protocol==='https:'){fetch('/__photo2wff_roi_apply__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(async response=>{const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'저장 실패');status.textContent='승인됨: '+data.path}).catch(error=>status.textContent='승인 실패: '+error.message)}else{const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='display-roi.json';link.click();status.textContent='파일로 저장했습니다. 로컬 서버에서 열면 바로 적용됩니다.'}}
</script></body></html>"""
    return template.replace("__META__", json.dumps(metadata)).replace("__SOURCE__", json.dumps(source_url)).replace("__OVERLAY__", json.dumps(overlay_url))


def serve_display_roi_review(review_root: Path, *, host: str = "127.0.0.1", port: int = 8766, open_browser: bool = False) -> None:
    root = Path(review_root).resolve()
    if not (root / "editor.html").exists():
        raise FileNotFoundError(f"ROI editor not found: {root / 'editor.html'}")
    proposal = json.loads((root / "display-roi-proposal.json").read_text(encoding="utf-8"))
    source_size = (int(proposal["sourceSize"]["width"]), int(proposal["sourceSize"]["height"]))

    class Handler(SimpleHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            if urlsplit(self.path).path != "/__photo2wff_roi_apply__":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                roi = _validate_roi(payload, source_size)
                saved = {**proposal, **payload, "confirmed": True, "displayRoi": roi}
                destination = root / "display-roi.json"
                destination.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self._send_json(200, {"ok": True, "path": destination.name})
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
                self._send_json(400, {"ok": False, "error": str(error)})

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), partial(Handler, directory=str(root)))
    url = f"http://{host}:{server.server_port}/editor.html"
    print(f"Photo2WFF Display ROI editor: {url}")
    if open_browser:
        webbrowser.open(url)
    print("승인 후 Ctrl+C로 종료합니다.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
